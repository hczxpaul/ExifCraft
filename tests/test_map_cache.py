"""Tile cache: the missing write side of tkintermapview's database_path.

Also covers the CachedMapView wrapper: a cache hit must not touch the network,
a miss must store what it downloaded, and a corrupt entry must self-heal.
"""
import io
import threading

import pytest
from PIL import Image

import map_cache
from map_cache import TileCache, format_size


def png_bytes(color=(120, 160, 200), size=256) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "png")
    return buf.getvalue()


class TestTileCache:
    def test_roundtrip(self, tmp_path):
        c = TileCache(tmp_path / "t.db")
        data = png_bytes()
        c.put(8, 12, 34, data)
        assert c.get(8, 12, 34) == data

    def test_miss_returns_none(self, tmp_path):
        c = TileCache(tmp_path / "t.db")
        assert c.get(8, 1, 1) is None

    def test_drop(self, tmp_path):
        c = TileCache(tmp_path / "t.db")
        c.put(8, 1, 1, png_bytes())
        c.drop(8, 1, 1)
        assert c.get(8, 1, 1) is None

    def test_put_ignores_empty(self, tmp_path):
        c = TileCache(tmp_path / "t.db")
        c.put(8, 1, 1, b"")
        assert c.stats() == (0, 0)

    def test_stats(self, tmp_path):
        c = TileCache(tmp_path / "t.db")
        data = png_bytes()
        c.put(8, 1, 1, data)
        c.put(8, 2, 2, data)
        count, size = c.stats()
        assert count == 2
        assert size == len(data) * 2

    def test_clear(self, tmp_path):
        c = TileCache(tmp_path / "t.db")
        c.put(8, 1, 1, png_bytes())
        c.clear()
        assert c.stats()[0] == 0

    def test_prune_keeps_budget(self, tmp_path):
        data = png_bytes()
        # budget comfortably below 4 tiles, above 1
        c = TileCache(tmp_path / "t.db", max_bytes=len(data) * 3)
        for i in range(8):
            c.put(8, i, 0, data)
            c._conn().execute("UPDATE tiles SET used=? WHERE x=?", (100 + i, i))
            c._conn().commit()
        removed = c.prune()
        assert removed > 0
        count, size = c.stats()
        assert size <= c.max_bytes
        # the newest tile must survive
        assert c.get(8, 7, 0) is not None

    def test_prune_noop_under_budget(self, tmp_path):
        c = TileCache(tmp_path / "t.db", max_bytes=10 * 1024 * 1024)
        c.put(8, 1, 1, png_bytes())
        assert c.prune() == 0
        assert c.stats()[0] == 1

    def test_concurrent_writes(self, tmp_path):
        """The map downloads from a thread pool; inserts must not collide."""
        c = TileCache(tmp_path / "t.db")
        data = png_bytes()

        def worker(n):
            for i in range(5):
                c.put(8, n, i, data)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert c.stats()[0] == 30

    def test_format_size(self):
        assert format_size(500) == "500 B"
        assert format_size(2048) == "2 KB"
        assert format_size(5 * 1024 * 1024) == "5.0 MB"


# ----------------------------------------------------------------------
#  Widget wrapper
# ----------------------------------------------------------------------
tkintermapview = pytest.importorskip("tkintermapview")


@pytest.fixture
def cached_view(tk_root, tmp_path):
    cache = TileCache(tmp_path / "t.db")
    cls = map_cache.build_widget_class()
    view = cls(tk_root, cache, width=200, height=200)
    yield view, cache
    view.image_load_queue_tasks.clear()
    view.image_load_queue_results.clear()
    view.destroy()


class TestCachedMapView:
    def test_cache_hit_skips_network(self, cached_view, monkeypatch):
        view, cache = cached_view
        data = png_bytes()
        cache.put(8, 12, 34, data)

        def boom(*a, **k):
            raise AssertionError("network must not be used on a cache hit")
        monkeypatch.setattr("requests.get", boom)

        photo = view.request_image(8, 12, 34)
        assert photo is not None

    def test_miss_downloads_and_stores(self, cached_view, monkeypatch):
        view, cache = cached_view
        data = png_bytes()

        class Resp:
            content = data
            def raise_for_status(self): pass

        calls = []
        monkeypatch.setattr("requests.get",
                            lambda url, **k: (calls.append(url), Resp())[1])

        photo = view.request_image(9, 100, 200)
        assert photo is not None
        assert len(calls) == 1
        assert cache.get(9, 100, 200) == data

    def test_second_call_uses_cache(self, cached_view, monkeypatch):
        view, cache = cached_view
        data = png_bytes()

        class Resp:
            content = data
            def raise_for_status(self): pass

        calls = []
        monkeypatch.setattr("requests.get",
                            lambda url, **k: (calls.append(url), Resp())[1])

        view.request_image(9, 7, 7)
        view.tile_image_cache.clear()          # simulate a fresh view
        view.request_image(9, 7, 7)
        assert len(calls) == 1                 # second one came from disk

    def test_corrupt_cache_entry_is_dropped(self, cached_view, monkeypatch):
        view, cache = cached_view
        cache.put(8, 50, 60, b"definitely not a png")

        class Resp:
            content = png_bytes()
            def raise_for_status(self): pass
        monkeypatch.setattr("requests.get", lambda url, **k: Resp())

        photo = view.request_image(8, 50, 60)
        assert photo is not None
        assert cache.get(8, 50, 60) != b"definitely not a png"

    def test_network_failure_returns_blank_tile(self, cached_view, monkeypatch):
        view, cache = cached_view

        def fail(*a, **k):
            raise OSError("no route to host")
        monkeypatch.setattr("requests.get", fail)

        photo = view.request_image(8, 1, 9)
        assert photo == view.empty_tile_image
        assert cache.get(8, 1, 9) is None      # failures are not cached
