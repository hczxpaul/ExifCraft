"""Disk cache for map tiles.

tkintermapview accepts a ``database_path`` but never writes to it — the only
``INSERT INTO tiles`` lives in its offline downloader. So the map was slow on
every open: nothing survived the session. This module supplies the missing
write side.

A tile is ~11 KB. The default 100 MB budget holds roughly 9,000 tiles, which
covers dozens of locations at several zoom levels. Old tiles are evicted in
least-recently-used order once the budget is exceeded.
"""
from __future__ import annotations

import io
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_MAX_BYTES = 100 * 1024 * 1024


class TileCache:
    """SQLite-backed tile store. Safe to call from the map's worker threads."""

    def __init__(self, path: str | Path, max_bytes: int = DEFAULT_MAX_BYTES):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._local = threading.local()
        self._prune_lock = threading.Lock()
        self._since_prune = 0
        self._init_db()

    # -- connection handling ------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        """One connection per thread: sqlite connections are not shareable.

        The map downloads tiles from a thread pool, so several may insert at
        once. WAL lets them read while one writes without blocking.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS tiles (
                            z    INTEGER NOT NULL,
                            x    INTEGER NOT NULL,
                            y    INTEGER NOT NULL,
                            data BLOB    NOT NULL,
                            used INTEGER NOT NULL,
                            PRIMARY KEY (z, x, y))""")
        conn.commit()

    # -- read / write -------------------------------------------------------
    def get(self, z: int, x: int, y: int) -> bytes | None:
        try:
            row = self._conn().execute(
                "SELECT data, used FROM tiles WHERE z=? AND x=? AND y=?",
                (z, x, y)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        # Touch for LRU, but not on every hit: an UPDATE + commit per tile
        # turned a 12-tile screen into 12 fsyncs. An hour of granularity is
        # plenty for eviction order.
        now = int(time.time())
        if now - int(row[1]) > 3600:
            try:
                self._conn().execute(
                    "UPDATE tiles SET used=? WHERE z=? AND x=? AND y=?",
                    (now, z, x, y))
                self._conn().commit()
            except sqlite3.Error:
                pass
        return bytes(row[0])

    def put(self, z: int, x: int, y: int, data: bytes) -> None:
        if not data:
            return
        try:
            self._conn().execute(
                "INSERT OR REPLACE INTO tiles (z, x, y, data, used) "
                "VALUES (?, ?, ?, ?, ?)",
                (z, x, y, data, int(time.time())))
            self._conn().commit()
        except sqlite3.Error:
            return
        # Evict on the way in: without this the cache grew until the disk was
        # full and then every insert failed silently. Checking on every write
        # would mean a table scan per tile, so batch it — at most 5% over
        # budget, checked every few hundred tiles at the default size.
        self._since_prune += len(data)
        if self._since_prune >= self._prune_every:
            self._since_prune = 0
            self.prune()

    @property
    def _prune_every(self) -> int:
        return max(4096, int(self.max_bytes * 0.05))

    def drop(self, z: int, x: int, y: int) -> None:
        try:
            self._conn().execute(
                "DELETE FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y))
            self._conn().commit()
        except sqlite3.Error:
            pass

    # -- housekeeping -------------------------------------------------------
    def stats(self) -> tuple[int, int]:
        """(tile count, total bytes)."""
        try:
            row = self._conn().execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(data)), 0) FROM tiles"
            ).fetchone()
            return int(row[0]), int(row[1])
        except sqlite3.Error:
            return 0, 0

    def prune(self) -> int:
        """Drop least-recently-used tiles until under ~80% of the budget.

        Returns the number of tiles removed. Serialised with a lock: several
        download threads can cross the threshold at the same time.
        """
        with self._prune_lock:
            count, total = self.stats()
            if total <= self.max_bytes:
                return 0
            target = int(self.max_bytes * 0.8)
            removed, freed = 0, 0
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT z, x, y, LENGTH(data) FROM tiles ORDER BY used ASC"
                ).fetchall()
                for z, x, y, size in rows:
                    if total - freed <= target:
                        break
                    conn.execute("DELETE FROM tiles WHERE z=? AND x=? AND y=?",
                                 (z, x, y))
                    freed += size
                    removed += 1
                conn.commit()
            except sqlite3.Error:
                return removed
            return removed

    def clear(self) -> None:
        try:
            conn = self._conn()
            conn.execute("DELETE FROM tiles")
            conn.commit()
            conn.execute("VACUUM")
        except sqlite3.Error:
            pass


def format_size(num: int) -> str:
    if num >= 1024 * 1024:
        return f"{num / 1024 / 1024:.1f} MB"
    if num >= 1024:
        return f"{num / 1024:.0f} KB"
    return f"{num} B"


# ----------------------------------------------------------------------
#  Widget
# ----------------------------------------------------------------------
def build_widget_class():
    """Return a TkinterMapView subclass wired to the tile cache.

    Built lazily because tkintermapview may not be installed; the app already
    handles that with HAS_MAP.
    """
    from tkintermapview import TkinterMapView
    from PIL import Image, ImageTk
    import requests

    class CachedMapView(TkinterMapView):
        """TkinterMapView that reads and writes a local tile cache.

        Also gives the underlying request a timeout: the stock code calls
        ``requests.get`` with none, so a half-open proxy leaves the map
        spinning forever instead of falling back to a blank tile.
        """

        REQUEST_TIMEOUT = 6

        def __init__(self, master, cache: TileCache, **kwargs):
            super().__init__(master, **kwargs)
            self._cache = cache

        def request_image(self, zoom: int, x: int, y: int, db_cursor=None):
            key = f"{zoom}{x}{y}"
            hit = self.tile_image_cache.get(key)
            if hit is not None:
                return hit

            data = self._cache.get(zoom, x, y)
            if data is not None:
                photo = self._decode(data)
                if photo is not None:
                    self.tile_image_cache[key] = photo
                    return photo
                self._cache.drop(zoom, x, y)      # corrupt entry -> refetch

            if self.overlay_tile_server is not None:
                # overlay compositing is not cached; defer to the stock path
                return super().request_image(zoom, x, y, db_cursor=None)

            url = (self.tile_server
                   .replace("{x}", str(x))
                   .replace("{y}", str(y))
                   .replace("{z}", str(zoom)))
            try:
                resp = requests.get(url, timeout=self.REQUEST_TIMEOUT,
                                    headers={"User-Agent": "ExifCraft-X/2.1.1"})
                resp.raise_for_status()
                data = resp.content
            except Exception:
                return self.empty_tile_image

            photo = self._decode(data)
            if photo is None:
                return self.empty_tile_image
            self._cache.put(zoom, x, y, data)
            self.tile_image_cache[key] = photo
            return photo

        @staticmethod
        def _decode(data: bytes):
            try:
                return ImageTk.PhotoImage(Image.open(io.BytesIO(data)))
            except Exception:
                return None

    return CachedMapView
