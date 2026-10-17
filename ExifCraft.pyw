"""
ExifCraft  ·  Professional EXIF Editor
A clean, modern tool for reading & writing image metadata.

Author : yohoten
License: MIT
"""

import os
import sys
import csv
import json
import uuid
import atexit
import shutil
import ctypes
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass, asdict
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from fractions import Fraction
from datetime import datetime
from PIL import Image, ImageOps, ImageTk, UnidentifiedImageError

import piexif


# ---------------------------------------------------------------------
#  Application assets
# ---------------------------------------------------------------------
def _app_dir() -> str:
    """Folder that holds the script and its assets (frozen or plain .pyw)."""
    if getattr(sys, "frozen", False):            # PyInstaller / py2exe build
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR  = _app_dir()
ICON_ICO = os.path.join(APP_DIR, "ExifCraft.ico")   # Windows: multi-resolution
ICON_PNG = os.path.join(APP_DIR, "ExifCraft.png")   # fallback: any platform

# Optional widgets
try:
    from tkintermapview import TkinterMapView
    HAS_MAP = True
except ImportError:
    HAS_MAP = False

try:
    from tkcalendar import DateEntry
    HAS_CALENDAR = True
except ImportError:
    HAS_CALENDAR = False


# =====================================================================
#  Theme  ──  color tokens, fonts, and ttk styling
# =====================================================================
class Theme:
    # ---- palette
    BG          = "#F4F6FA"   # app background
    SURFACE     = "#FFFFFF"   # cards / inputs
    SURFACE_2   = "#F8FAFC"   # secondary surface (log)
    BORDER      = "#E2E8F0"   # hairline borders
    BORDER_FOCUS= "#CBD5E1"

    PRIMARY     = "#2563EB"   # brand blue
    PRIMARY_H   = "#1D4ED8"   # hover
    PRIMARY_S   = "#DBEAFE"   # soft / selected

    TEXT        = "#0F172A"   # primary text
    TEXT_2      = "#475569"   # secondary
    TEXT_3      = "#94A3B8"   # tertiary / placeholder

    SUCCESS     = "#059669"
    ERROR       = "#DC2626"
    WARNING     = "#D97706"

    # ---- typography
    if sys.platform.startswith("win"):
        FONT       = ("Segoe UI", 10)
        FONT_BOLD  = ("Segoe UI Semibold", 10)
        FONT_H1    = ("Segoe UI Semibold", 15)
        FONT_H2    = ("Segoe UI Semibold", 11)
        FONT_MONO  = ("Consolas", 9)
    elif sys.platform == "darwin":
        FONT       = ("SF Pro Text", 12)
        FONT_BOLD  = ("SF Pro Text Semibold", 12)
        FONT_H1    = ("SF Pro Display Semibold", 17)
        FONT_H2    = ("SF Pro Text Semibold", 13)
        FONT_MONO  = ("Menlo", 11)
    else:
        FONT       = ("Inter", 10)
        FONT_BOLD  = ("Inter", 10, "bold")
        FONT_H1    = ("Inter", 14, "bold")
        FONT_H2    = ("Inter", 11, "bold")
        FONT_MONO  = ("JetBrains Mono", 9)


# =====================================================================
#  EXIF helpers
# =====================================================================
def to_degrees(value: float):
    # Standard DMS: parts are always non-negative; the sign lives in the
    # N/S or E/W Ref tag. Keeping negative parts breaks third-party readers.
    v = abs(value)
    d = int(v)
    m = int((v - d) * 60)
    s = (v - d - m / 60) * 3600
    return [(d, 1), (m, 1), _to_fraction(s)]


def _to_fraction(value: float):
    f = Fraction(value).limit_denominator(1_000_000)
    return (f.numerator, f.denominator)


def set_exif_data(img_path, output_path, *, longitude, latitude, altitude,
                  focal_length, aperture, iso,
                  make, model, shot_date, overwrite=False):
    """Write EXIF into a copy of *img_path* at *output_path*."""
    try:
        img = Image.open(img_path)
    except UnidentifiedImageError:
        raise ValueError(f"Unrecognized image: {os.path.basename(img_path)}")
    apply_exif(img, output_path, longitude=longitude, latitude=latitude,
               altitude=altitude, focal_length=focal_length, aperture=aperture,
               iso=iso, make=make, model=model, shot_date=shot_date)


def apply_exif(img, output_path, *, longitude, latitude, altitude,
               focal_length, aperture, iso,
               make, model, shot_date):
    """Write EXIF from an already-opened PIL image to *output_path*.

    Split out of set_exif_data so callers can load an image into memory first:
    overwrite mode renames the source away before saving, so re-opening the
    path inside would fail.

    Full write: every field is replaced. For partial edits use
    apply_exif_fields(), which only touches the keys you pass.
    """
    fields = {
        "longitude": longitude, "latitude": latitude, "altitude": altitude,
        "focal_length": focal_length, "aperture": aperture, "iso": iso,
        "make": make, "model": model, "datetime": shot_date,
    }
    apply_exif_fields(img, output_path, fields)


# name -> (IFD, tag, value kind)
SIMPLE_FIELDS = {
    "make":         ("0th",  piexif.ImageIFD.Make,             "str"),
    "model":        ("0th",  piexif.ImageIFD.Model,            "str"),
    "datetime":     ("Exif", piexif.ExifIFD.DateTimeOriginal,  "str"),
    "focal_length": ("Exif", piexif.ExifIFD.FocalLength,       "rational"),
    "aperture":     ("Exif", piexif.ExifIFD.FNumber,           "rational"),
    "iso":          ("Exif", piexif.ExifIFD.ISOSpeedRatings,   "int"),
}


def apply_exif_fields(img, output_path, fields: dict, *, exif_dict=None):
    """Write only the EXIF fields present in *fields*; everything else is kept.

    Args:
        img: an already-opened PIL image.
        output_path: destination path.
        fields: {field_name: value}. Absent key = leave the existing tag
            untouched; value None = delete that tag ("clear this field").
            Recognised names are the SIMPLE_FIELDS keys plus
            "longitude" / "latitude" / "altitude" (GPS) and "clear_gps"
            (True → drop the whole GPS block).
        exif_dict: optional pre-loaded piexif dict (used by tests).

    Raises:
        ValueError: if a numeric field cannot be parsed.
    """
    if exif_dict is None:
        if 'exif' in img.info:
            exif_dict = piexif.load(img.info['exif'])
        else:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

    # ---- simple tags: only the keys we were given
    for name, (ifd, tag, kind) in SIMPLE_FIELDS.items():
        if name not in fields:
            continue
        value = fields[name]
        if value is None:
            exif_dict[ifd].pop(tag, None)
            continue
        if kind == "str":
            exif_dict[ifd][tag] = str(value).encode("utf-8")
        elif kind == "rational":
            exif_dict[ifd][tag] = _to_fraction(float(value))
        else:
            exif_dict[ifd][tag] = int(value)

    # ---- GPS: lat/lon carry a Ref tag, altitude is independent
    if fields.get("clear_gps"):
        exif_dict["GPS"] = {}            # verified safe: other IFDs untouched
    else:
        gps = exif_dict.setdefault("GPS", {})
        for name, tag, ref_tag, pos_ref, neg_ref in (
                ("latitude",  piexif.GPSIFD.GPSLatitude,
                 piexif.GPSIFD.GPSLatitudeRef,  "N", "S"),
                ("longitude", piexif.GPSIFD.GPSLongitude,
                 piexif.GPSIFD.GPSLongitudeRef, "E", "W")):
            if name not in fields:
                continue
            value = fields[name]
            if value is None:
                gps.pop(tag, None)
                gps.pop(ref_tag, None)
            else:
                v = float(value)
                gps[tag] = to_degrees(v)
                gps[ref_tag] = pos_ref if v >= 0 else neg_ref

        if "altitude" in fields:
            value = fields["altitude"]
            if value is None:
                gps.pop(piexif.GPSIFD.GPSAltitude, None)
                gps.pop(piexif.GPSIFD.GPSAltitudeRef, None)
            else:
                alt = float(value)
                gps[piexif.GPSIFD.GPSAltitude] = _to_fraction(alt) if alt else (0, 1)
                gps[piexif.GPSIFD.GPSAltitudeRef] = 0 if alt >= 0 else 1

    exif_bytes = piexif.dump(exif_dict)
    ext = os.path.splitext(output_path)[1].lower()
    save_kwargs = {"exif": exif_bytes}
    if ext in (".jpg", ".jpeg"):
        save_kwargs["quality"] = 100
    img.save(output_path, **save_kwargs)
    return exif_dict


def read_exif_info(img_path):
    """Return a dict of human-readable EXIF fields (missing → '')."""
    info = dict.fromkeys(
        ["make", "model", "datetime",
         "focal_length", "fnumber", "iso",
         "longitude", "latitude", "altitude"], "")
    try:
        img = Image.open(img_path)
        if 'exif' not in img.info:
            return info
        exif = piexif.load(img.info['exif'])
    except Exception:
        return info

    def _s(b):
        return b.decode("utf-8", "ignore") if isinstance(b, (bytes, bytearray)) else b

    info["make"]    = _s(exif['0th'].get(piexif.ImageIFD.Make, b''))
    info["model"]   = _s(exif['0th'].get(piexif.ImageIFD.Model, b''))
    info["datetime"]= _s(exif['Exif'].get(piexif.ExifIFD.DateTimeOriginal, b''))

    fl = exif['Exif'].get(piexif.ExifIFD.FocalLength)
    if fl and isinstance(fl, tuple) and len(fl) == 2 and fl[1]:
        info["focal_length"] = f"{fl[0]/fl[1]:.1f}"

    fn = exif['Exif'].get(piexif.ExifIFD.FNumber)
    if fn and isinstance(fn, tuple) and len(fn) == 2 and fn[1]:
        info["fnumber"] = f"{fn[0]/fn[1]:.1f}"

    iso = exif['Exif'].get(piexif.ExifIFD.ISOSpeedRatings)
    if iso:
        info["iso"] = str(iso[0] if isinstance(iso, tuple) else iso)

    gps = exif.get('GPS', {})
    def _dms(tag, ref_tag, default_ref):
        dms = gps.get(tag)
        if not dms or len(dms) != 3:
            return ""
        d, m, s = dms
        val = d[0]/d[1] + m[0]/(m[1]*60) + s[0]/(s[1]*3600)
        ref = gps.get(ref_tag, default_ref)
        # piexif returns ASCII tags (e.g. "S"/"W") as bytes — decode before comparing
        if isinstance(ref, (bytes, bytearray)):
            ref = ref.decode("ascii", "ignore")
        if ref in ("S", "W"):
            val = -val
        return f"{val:.6f}"

    info["latitude"]  = _dms(piexif.GPSIFD.GPSLatitude,  piexif.GPSIFD.GPSLatitudeRef,  "N")
    info["longitude"] = _dms(piexif.GPSIFD.GPSLongitude, piexif.GPSIFD.GPSLongitudeRef, "E")

    alt = gps.get(piexif.GPSIFD.GPSAltitude)
    if alt and isinstance(alt, tuple) and len(alt) == 2 and alt[1]:
        info["altitude"] = f"{alt[0]/alt[1]:.1f}"

    return info


# =====================================================================
#  Validators
# =====================================================================
class Validator:
    """Return (ok, [errors]).  Keyword names mirror `_collect_params`."""
    @staticmethod
    def check(longitude, latitude, altitude, focal_length, aperture, iso,
              make, model, date):
        errs = []

        def _num(name, raw, *, lo=None, hi=None, integer=False, allow_empty=False):
            s = (raw or "").strip()
            if not s:
                if allow_empty:
                    return None
                errs.append(f"{name} 不能为空"); return None
            try:
                v = int(s) if integer else float(s)
            except ValueError:
                errs.append(f"{name} 必须为{'整数' if integer else '数字'}"); return None
            if lo is not None and v < lo:
                errs.append(f"{name} 不能小于 {lo}"); return None
            if hi is not None and v > hi:
                errs.append(f"{name} 不能大于 {hi}"); return None
            return v

        _num("经度", longitude, lo=-180, hi=180)
        _num("纬度", latitude,  lo=-90,  hi=90)
        _num("海拔", altitude,  allow_empty=True)        # optional
        _num("焦距", focal_length, lo=0.01)
        _num("光圈", aperture,     lo=0.01)
        _num("ISO",  iso,          lo=1, integer=True)

        if not (make or "").strip():  errs.append("相机制造商不能为空")
        if not (model or "").strip(): errs.append("相机型号不能为空")

        d = (date or "").strip()
        if not d:
            errs.append("拍摄日期不能为空")
        else:
            try:
                datetime.strptime(d, "%Y:%m:%d %H:%M:%S")
            except ValueError:
                errs.append("日期格式错误，应为 YYYY:MM:DD HH:MM:SS")

        return len(errs) == 0, errs


# =====================================================================
#  Backup / rollback — session write journal
# =====================================================================
BAK_PREFIX = "._ExifCraft_bak_"     # dot prefix: hidden on Unix, ignored by image scanners
BAK_SUFFIX = ".exifbak"             # non-image extension: never picked up by photo tools


@dataclass
class Rec:
    """One journalled write operation."""
    src: str                 # source file
    dst: str                 # destination (copy mode: new file; overwrite: same path)
    backup: str | None       # backup path (overwrite mode only)
    mode: str                # "copy" | "overwrite"
    state: str = "pending"   # pending → done / failed
    mtime: float = 0.0       # src fingerprint before the write (audit trail)
    size: int = 0
    written_mtime: float = 0.0   # dst fingerprint right after OUR write
    written_size: int = 0        # ← what changed_since() compares against

    def as_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Rec":
        known = {f for f in Rec.__dataclass_fields__}
        return Rec(**{k: v for k, v in d.items() if k in known})


def set_hidden(path: str) -> None:
    """Mark *path* hidden on Windows; a no-op elsewhere (dot-prefix suffices)."""
    if sys.platform.startswith("win"):
        try:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)  # FILE_ATTRIBUTE_HIDDEN
        except Exception:
            pass


class BackupManager:
    """Session-scoped write journal with backup / rollback / cleanup.

    Design: every write is journalled to JSON *before* any file is touched.
    A crash mid-write is recovered at next startup by replaying pending
    records. Backups live next to the originals as hidden non-image files so
    a rollback never needs to copy data across a slow network drive — rename
    is a metadata operation.
    """

    def __init__(self, app_dir: str, session_id: str | None = None):
        self.sid = session_id or uuid.uuid4().hex[:8]
        self.dir = self._pick_dir(app_dir)
        self.path = os.path.join(self.dir, f"{self.sid}.json")
        self.recs: list[Rec] = []
        self._flush()

    # ------------------------------------------------------------------
    #  Storage location — prefer the app dir, fall back to %TEMP%
    # ------------------------------------------------------------------
    @staticmethod
    def _pick_dir(app_dir: str) -> str:
        for base in (app_dir, tempfile.gettempdir()):
            d = os.path.join(base, ".exifcraft", "sessions")
            try:
                os.makedirs(d, exist_ok=True)
                probe = os.path.join(d, ".probe")
                with open(probe, "w", encoding="utf-8") as fp:
                    fp.write("ok")
                os.remove(probe)
                return d
            except OSError:
                continue
        raise RuntimeError("no writable location for ExifCraft session data")

    # ------------------------------------------------------------------
    #  Journal
    # ------------------------------------------------------------------
    def _flush(self) -> None:
        """Persist the journal. Best-effort: disk state alone still recovers."""
        payload = {
            "session": self.sid,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "records": [r.as_dict() for r in self.recs],
        }
        try:
            with open(self.path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # ------------------------------------------------------------------
    #  Naming
    # ------------------------------------------------------------------
    def backup_name(self, src: str) -> str:
        base = os.path.basename(src)
        return os.path.join(os.path.dirname(src),
                            f"{BAK_PREFIX}{self.sid}_{base}{BAK_SUFFIX}")

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------
    def begin(self, src: str, dst: str, mode: str) -> Rec:
        """Journal intent *before* touching files. Returns the record."""
        st = os.stat(src)
        rec = Rec(
            src=src, dst=dst,
            backup=self.backup_name(src) if mode == "overwrite" else None,
            mode=mode, mtime=st.st_mtime, size=st.st_size,
        )
        self.recs.append(rec)
        self._register_dirs(os.path.dirname(src), os.path.dirname(dst))
        self._flush()
        return rec

    def _register_dirs(self, *dirs: str) -> None:
        """Remember every directory we touched, so a later startup can scan
        them for orphaned backups even if a journal was lost."""
        index_path = os.path.join(self.dir, "_dirs.json")
        known: set[str] = set()
        try:
            with open(index_path, encoding="utf-8") as fp:
                known = set(json.load(fp))
        except (OSError, json.JSONDecodeError):
            pass
        known.update(os.path.abspath(d) for d in dirs if d)
        try:
            with open(index_path, "w", encoding="utf-8") as fp:
                json.dump(sorted(known), fp)
        except OSError:
            pass

    def commit(self, rec: Rec) -> None:
        """Mark the write done and fingerprint the file *we* produced.

        The post-write fingerprint is what changed_since() compares against —
        comparing against the pre-write one would flag every successful write
        as "externally modified" and make rollback a no-op.
        """
        try:
            st = os.stat(rec.dst)
            rec.written_mtime, rec.written_size = st.st_mtime, st.st_size
        except OSError:
            pass
        rec.state = "done"
        self._flush()

    def fail(self, rec: Rec) -> None:
        rec.state = "failed"
        self._flush()

    # ------------------------------------------------------------------
    #  Introspection (status bar / dialogs)
    # ------------------------------------------------------------------
    def live_backups(self) -> list[Rec]:
        return [r for r in self.recs if r.backup and os.path.exists(r.backup)]

    def backup_count(self) -> int:
        return len(self.live_backups())

    def backup_bytes(self) -> int:
        return sum(os.path.getsize(r.backup) for r in self.live_backups())

    def changed_since(self, rec: Rec) -> bool:
        """True if dst was modified by somebody else after we wrote it.

        A never-committed record (written_* unset) counts as unchanged: that
        is an interrupted write, which rollback is supposed to undo.
        """
        if not rec.written_mtime:
            return False
        try:
            st = os.stat(rec.dst)
        except OSError:
            return False
        return (abs(st.st_mtime - rec.written_mtime) > 1e-6
                or st.st_size != rec.written_size)

    # ------------------------------------------------------------------
    #  Safe write — rename-based backup (fast on network drives)
    # ------------------------------------------------------------------
    def safe_write(self, rec: Rec, save_fn) -> None:
        """Execute a journalled write with backup protection.

        *rec* must come from begin(). *save_fn(path)* performs the actual
        save (kept as a callback so this class stays format-agnostic).

        Overwrite mode: the image is loaded into memory first, then the
        original is renamed to the backup path (a metadata operation even on
        remote HDDs), then the new content is written to the original path.
        If the rename fails (permissions, file locked), fall back to a full
        copy. Copy mode: nothing to back up — the original is never touched.

        Any exception propagates after the journal is marked failed, so the
        caller can continue with the next file.
        """
        if rec.mode == "overwrite" and rec.backup:
            try:
                os.rename(rec.src, rec.backup)       # fast path: metadata only
                set_hidden(rec.backup)
            except OSError:
                shutil.copy2(rec.src, rec.backup)    # slow path: full copy
                set_hidden(rec.backup)
        try:
            save_fn(rec.dst)
        except Exception:
            self.fail(rec)
            raise

    # ------------------------------------------------------------------
    #  Rollback & cleanup
    # ------------------------------------------------------------------
    def rollback(self, recs: list[Rec] | None = None) -> tuple[int, list[str]]:
        """Undo journalled writes. Returns (restored_count, skipped_paths).

        Skipped = files whose fingerprint changed after the write (somebody
        else edited them) — never silently overwrite foreign changes.
        """
        recs = self.recs if recs is None else recs
        target = {id(r) for r in recs}          # identity, not equality:
        done, skipped = 0, []                   # dataclass __eq__ would alias twins
        keep: list[Rec] = []
        for r in self.recs:
            if id(r) not in target:
                keep.append(r)
                continue
            try:
                if r.mode == "overwrite" and r.backup and os.path.exists(r.backup):
                    if self.changed_since(r):
                        skipped.append(r.dst)
                        keep.append(r)          # backup survives, keep the record
                        continue
                    os.replace(r.backup, r.dst)  # atomic restore
                    done += 1
                elif r.mode == "copy" and os.path.exists(r.dst):
                    os.remove(r.dst)             # remove the *_exif copy
                    done += 1
            except OSError:
                skipped.append(r.dst)
                keep.append(r)
        self.recs = keep
        self._flush()
        return done, skipped

    def cleanup(self) -> None:
        """Delete every backup we still hold + the journal itself."""
        for r in self.recs:
            if r.backup and os.path.exists(r.backup):
                try:
                    os.remove(r.backup)
                except OSError:
                    pass
        self.recs = []
        try:
            os.remove(self.path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    #  Crash recovery — module-level, run at startup
    # ------------------------------------------------------------------
    @staticmethod
    def recover_sessions(app_dir: str) -> tuple[int, int]:
        """Replay every journalled session on disk.

        Returns (restored, orphans): files restored from pending writes and
        leftover backup files that no journal references.

        Recovery rules per pending record:
          * backup exists, dst gone      → rename never finished: restore
          * dst exists (save happened)   → keep dst, journal just missed commit
        """
        sessions_dir = BackupManager._pick_dir(app_dir)
        journals = [f for f in os.listdir(sessions_dir)
                    if f.endswith(".json") and not f.startswith("_")]
        # NB: no early return when journals are empty — a fully lost journal is
        # exactly the orphan scenario, and the scan itself is free when
        # _dirs.json does not exist (no directory was ever registered).

        restored, managed = 0, set()
        for name in journals:
            try:
                with open(os.path.join(sessions_dir, name), encoding="utf-8") as fp:
                    data = json.load(fp)
            except (OSError, json.JSONDecodeError):
                continue
            for d in data.get("records", []):
                try:
                    r = Rec.from_dict(d)
                except TypeError:
                    continue
                if r.backup:
                    managed.add(os.path.abspath(r.backup))
                if r.state != "pending":
                    continue
                if r.backup and os.path.exists(r.backup) and not os.path.exists(r.dst):
                    try:
                        os.replace(r.backup, r.dst)   # interrupted rename → restore
                        restored += 1
                    except OSError:
                        pass
                # else: save completed but commit missed — disk wins, keep dst
            try:
                os.remove(os.path.join(sessions_dir, name))   # session closed
            except OSError:
                pass

        # orphan scan: every directory we ever touched, minus managed backups
        scan_dirs: set[str] = set()
        try:
            with open(os.path.join(sessions_dir, "_dirs.json"), encoding="utf-8") as fp:
                scan_dirs = set(json.load(fp))
        except (OSError, json.JSONDecodeError):
            pass
        orphans = 0
        for d in scan_dirs:
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if f.startswith(BAK_PREFIX) and f.endswith(BAK_SUFFIX):
                    if os.path.abspath(os.path.join(d, f)) not in managed:
                        orphans += 1
        return restored, orphans

    @staticmethod
    def purge_orphans(app_dir: str) -> int:
        """Delete backup files that no journal references. Returns the count.

        Only files matching our own naming convention are ever touched.
        """
        sessions_dir = BackupManager._pick_dir(app_dir)
        managed: set[str] = set()
        for name in os.listdir(sessions_dir):
            if not name.endswith(".json") or name.startswith("_"):
                continue
            try:
                with open(os.path.join(sessions_dir, name), encoding="utf-8") as fp:
                    data = json.load(fp)
            except (OSError, json.JSONDecodeError):
                continue
            for d in data.get("records", []):
                if d.get("backup"):
                    managed.add(os.path.abspath(d["backup"]))

        scan_dirs: set[str] = set()
        try:
            with open(os.path.join(sessions_dir, "_dirs.json"), encoding="utf-8") as fp:
                scan_dirs = set(json.load(fp))
        except (OSError, json.JSONDecodeError):
            pass

        removed = 0
        for d in scan_dirs:
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if not (f.startswith(BAK_PREFIX) and f.endswith(BAK_SUFFIX)):
                    continue
                p = os.path.abspath(os.path.join(d, f))
                if p in managed:
                    continue
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
        return removed


# =====================================================================
#  UI building blocks
# =====================================================================
class Card(tk.Frame):
    """A hairline-bordered surface card with an optional section title."""
    def __init__(self, master, title=None, **kw):
        super().__init__(
            master,
            bg=Theme.SURFACE,
            highlightthickness=1,
            highlightbackground=Theme.BORDER,
            highlightcolor=Theme.BORDER,
            bd=0,
            **kw,
        )
        pad = ttk.Frame(self, style="Card.TFrame")
        pad.pack(fill="both", expand=True, padx=14, pady=10)
        if title:
            ttk.Label(pad, text=title, style="CardTitle.TLabel").pack(anchor="w", pady=(0, 9))
        self.body = ttk.Frame(pad, style="Card.TFrame")
        self.body.pack(fill="both", expand=True)


# =====================================================================
#  Main application
# =====================================================================
class ExifCraft:
    APP_NAME   = "ExifCraft"
    APP_TAG    = "Professional EXIF Editor"
    APP_VER    = "2.1"

    IMG_EXTS = (".jpg", ".jpeg", ".tiff", ".tif", ".png")

    def __init__(self, root: tk.Tk):
        self.root = root
        self._apply_theme()
        root.title(f"{self.APP_NAME}  ·  {self.APP_TAG}")
        self.root.configure(bg=Theme.BG)

        # state
        self.input_files: list[str] = []
        self.worker: threading.Thread | None = None

        # preview state: LRU cache (path -> PhotoImage) + the currently shown
        # image (kept alive on the instance so Tk never GCs it) + debounce id
        self._preview_cache: OrderedDict[str, ImageTk.PhotoImage] = OrderedDict()
        self._preview_photo: ImageTk.PhotoImage | None = None
        self._preview_after_id: str | None = None

        # write journal — drives rollback. None only if no writable location
        # exists; writes then still work, just without rollback.
        self._bm_error = ""
        try:
            self.bm: BackupManager | None = BackupManager(APP_DIR)
        except Exception as e:
            self.bm = None
            self._bm_error = str(e)

        # string vars (defaults empty — let the user fill in)
        self.src_var   = tk.StringVar()
        self.out_var   = tk.StringVar()
        self.make_var  = tk.StringVar()
        self.model_var = tk.StringVar()
        self.time_var  = tk.StringVar(value="12:00:00")
        self.status_var= tk.StringVar(value="Ready")
        self.overwrite_var = tk.BooleanVar(value=False)

        self.field_vars: dict[str, tk.StringVar] = {
            "longitude":     tk.StringVar(),
            "latitude":      tk.StringVar(),
            "altitude":      tk.StringVar(),
            "focal_length":  tk.StringVar(),
            "aperture":      tk.StringVar(),
            "iso":           tk.StringVar(),
        }

        self._build()
        self._setup_window()          # needs measured widget sizes → after _build
        self._set_window_icon()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.bm is None:
            self._log(f"备份功能不可用（无可用写入位置）：{self._bm_error}", "warn")

    # ------------------------------------------------------------------
    #  Window icon
    # ------------------------------------------------------------------
    def _apply_icon(self, window):
        """Put the ExifCraft icon on any Tk window (title bar + taskbar)."""
        # Windows: a multi-resolution .ico stays sharp at every shell size.
        # NB: the path must be positional — `default=` only seeds *future*
        # windows and leaves this one with Tk's stock feather icon.
        if sys.platform.startswith("win") and os.path.exists(ICON_ICO):
            try:
                window.iconbitmap(ICON_ICO)               # this window + taskbar
                window.iconbitmap(default=ICON_ICO)       # future children
                return
            except tk.TclError:
                pass

        # Fallback for other platforms, or if the .ico is missing.
        if os.path.exists(ICON_PNG):
            try:
                img = tk.PhotoImage(file=ICON_PNG)
                window.iconphoto(True, img)
                setattr(window, "_exifcraft_icon", img)   # keep the ref alive
            except tk.TclError:
                pass

    def _set_window_icon(self):
        self._apply_icon(self.root)

    # ------------------------------------------------------------------
    #  Window sizing  (fits the content, clamped to the visible desktop)
    # ------------------------------------------------------------------
    def _setup_window(self):
        self.root.update_idletasks()
        sw, sh   = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        need_w   = self.root.winfo_reqwidth()
        need_h   = self.root.winfo_reqheight()
        w = min(max(need_w + 16, 1020), sw - 60)
        h = min(max(need_h + 20, 680),  sh - 90)
        x = max(20, (sw - w) // 2)
        y = max(20, (sh - h) // 2 - 20)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(min(1000, sw - 100), min(680, sh - 160))

    # ------------------------------------------------------------------
    #  Theming
    # ------------------------------------------------------------------
    def _apply_theme(self):
        style = ttk.Style()
        # 'clam' is the most styleable cross-platform theme.
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        s = style
        base = Theme

        # Frames
        s.configure("App.TFrame",        background=base.BG)
        s.configure("Card.TFrame",        background=base.SURFACE, relief="flat")
        s.configure("CardInner.TFrame",   background=base.SURFACE)
        s.configure("Log.TFrame",         background=base.SURFACE_2)

        # Labels
        s.configure("App.TLabel",   background=base.BG, foreground=base.TEXT,
                    font=base.FONT)
        s.configure("CardTitle.TLabel",
                    background=base.SURFACE, foreground=base.TEXT,
                    font=base.FONT_H2)
        s.configure("Field.TLabel",
                    background=base.SURFACE, foreground=base.TEXT_2,
                    font=base.FONT)
        s.configure("Hint.TLabel",
                    background=base.SURFACE, foreground=base.TEXT_3,
                    font=(base.FONT[0], base.FONT[1]-1))
        s.configure("Title.TLabel",
                    background=base.BG, foreground=base.TEXT,
                    font=base.FONT_H1)
        s.configure("Subtitle.TLabel",
                    background=base.BG, foreground=base.TEXT_2,
                    font=(base.FONT[0], base.FONT[1]+1))
        s.configure("Status.TLabel",
                    background=base.BG, foreground=base.TEXT_2,
                    font=base.FONT)
        s.configure("Err.TLabel",
                    background=base.BG, foreground=base.ERROR,
                    font=base.FONT_BOLD)

        # Buttons
        s.configure("Primary.TButton",
                    font=base.FONT_BOLD, padding=(14, 8),
                    foreground=base.SURFACE, background=base.PRIMARY,
                    borderwidth=0, relief="flat", focuscolor=base.PRIMARY,
                    bordercolor=base.PRIMARY, lightcolor=base.PRIMARY,
                    darkcolor=base.PRIMARY)
        s.map("Primary.TButton",
              background=[("active", base.PRIMARY_H), ("pressed", base.PRIMARY_H)],
              foreground=[("disabled", "#BFD3F5")])

        s.configure("Ghost.TButton",
                    font=base.FONT, padding=(12, 7),
                    foreground=base.TEXT, background=base.SURFACE,
                    borderwidth=1, relief="solid", focuscolor=base.SURFACE,
                    bordercolor=base.BORDER_FOCUS,
                    lightcolor=base.SURFACE, darkcolor=base.SURFACE)
        s.map("Ghost.TButton",
              background=[("active", base.PRIMARY_S), ("pressed", base.PRIMARY_S)],
              foreground=[("active", base.PRIMARY_H)],
              bordercolor=[("active", base.PRIMARY)])

        s.configure("Subtle.TButton",
                    font=base.FONT, padding=(10, 6),
                    foreground=base.TEXT_2, background=base.BG,
                    borderwidth=0, relief="flat")
        s.map("Subtle.TButton",
              foreground=[("active", base.PRIMARY)],
              background=[("active", base.SURFACE)])

        # Inputs
        s.configure("Field.TEntry",
                    fieldbackground=base.SURFACE, foreground=base.TEXT,
                    insertcolor=base.TEXT, borderwidth=1, relief="solid",
                    padding=6)
        s.map("Field.TEntry",
              bordercolor=[("focus", base.PRIMARY), ("!focus", base.BORDER)],
              lightcolor=[("focus", base.PRIMARY), ("!focus", base.BORDER)],
              darkcolor=[("focus", base.PRIMARY), ("!focus", base.BORDER)])

        s.configure("Field.TCombobox",
                    fieldbackground=base.SURFACE, foreground=base.TEXT,
                    borderwidth=1, relief="solid", padding=4)
        s.map("Field.TCombobox",
              bordercolor=[("focus", base.PRIMARY), ("!focus", base.BORDER)],
              lightcolor=[("focus", base.PRIMARY), ("!focus", base.BORDER)],
              darkcolor=[("focus", base.PRIMARY), ("!focus", base.BORDER)])

        # Listbox (use Tk widget, but style scrollbar)
        s.configure("Vertical.TScrollbar",
                    background=base.BG, troughcolor=base.BG,
                    bordercolor=base.BG, arrowcolor=base.TEXT_3)
        s.map("Vertical.TScrollbar",
              background=[("active", base.BORDER_FOCUS), ("pressed", base.TEXT_3)])

        # Progressbar
        s.configure("Accent.Horizontal.TProgressbar",
                    troughcolor="#E9EEF6", background=base.PRIMARY,
                    bordercolor="#E9EEF6", lightcolor="#E9EEF6",
                    darkcolor="#E9EEF6", borderwidth=0, thickness=9)

        # Checkbutton
        s.configure("App.TCheckbutton",
                    background=base.SURFACE, foreground=base.TEXT,
                    font=base.FONT, focuscolor=base.SURFACE,
                    indicatorcolor=base.SURFACE,
                    bordercolor=base.BORDER_FOCUS,
                    padding=2)
        s.map("App.TCheckbutton",
              background=[("active", base.SURFACE)],
              foreground=[("active", base.TEXT)],
              indicatorcolor=[("selected", base.PRIMARY), ("!selected", base.SURFACE)])

    # ------------------------------------------------------------------
    #  Layout
    # ------------------------------------------------------------------
    def _build(self):
        # outer canvas with padding
        outer = ttk.Frame(self.root, style="App.TFrame", padding=12)
        outer.pack(fill="both", expand=True)

        # ---- header
        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="◼", style="Title.TLabel",
                  foreground=Theme.PRIMARY).pack(side="left", padx=(0, 8))
        ttk.Label(header, text=self.APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=f"  v{self.APP_VER}", style="Subtitle.TLabel",
                  foreground=Theme.TEXT_3).pack(side="left", padx=(2, 0), pady=(4, 0))
        ttk.Label(header, text=self.APP_TAG, style="Subtitle.TLabel").pack(side="right")

        # ---- body: two-column (cards) layout
        body = ttk.Frame(outer, style="App.TFrame")
        body.pack(fill="both", expand=True, pady=(10, 0))
        body.columnconfigure(0, weight=1, uniform="col")
        body.columnconfigure(1, weight=1, uniform="col")
        body.rowconfigure(0, weight=1)

        # --- left column
        left = ttk.Frame(body, style="App.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        src_card = Card(left, title="Source  ·  源文件")
        src_card.pack(fill="both", expand=True)

        # source row
        row = ttk.Frame(src_card.body, style="CardInner.TFrame")
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Path", style="Field.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=self.src_var, style="Field.TEntry",
                  font=Theme.FONT, width=1).pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(row, text="File",  style="Ghost.TButton",
                   command=self._pick_file).pack(side="left", padx=2)
        ttk.Button(row, text="Folder", style="Ghost.TButton",
                   command=self._pick_folder).pack(side="left", padx=2)

        # output row
        row = ttk.Frame(src_card.body, style="CardInner.TFrame")
        row.pack(fill="x", pady=(6, 2))
        ttk.Label(row, text="Output", style="Field.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=self.out_var, style="Field.TEntry",
                  font=Theme.FONT, width=1).pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(row, text="Browse", style="Ghost.TButton",
                   command=self._pick_output).pack(side="left", padx=2)

        # file list
        list_holder = ttk.Frame(src_card.body, style="CardInner.TFrame")
        list_holder.pack(fill="both", expand=True, pady=(8, 4))

        self.file_list = tk.Listbox(
            list_holder, height=6, activestyle="none",
            background=Theme.SURFACE_2, foreground=Theme.TEXT,
            selectbackground=Theme.PRIMARY_S, selectforeground=Theme.PRIMARY_H,
            relief="flat", highlightthickness=1,
            highlightbackground=Theme.BORDER, highlightcolor=Theme.PRIMARY,
            font=Theme.FONT_MONO, bd=0,
        )
        self.file_list.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(list_holder, orient="vertical",
                           style="Vertical.TScrollbar", command=self.file_list.yview)
        sb.pack(side="right", fill="y")
        self.file_list.configure(yscrollcommand=sb.set)
        self.file_list.bind("<<ListboxSelect>>", self._schedule_preview)

        # list footer
        list_footer = ttk.Frame(src_card.body, style="CardInner.TFrame")
        list_footer.pack(fill="x", pady=(4, 0))
        self.list_count_lbl = ttk.Label(list_footer, text="0 files",
                                        style="Hint.TLabel")
        self.list_count_lbl.pack(side="left")
        ttk.Button(list_footer, text="Clear", style="Subtle.TButton",
                   command=self._clear_files).pack(side="right")

        # options row
        opt_row = ttk.Frame(src_card.body, style="CardInner.TFrame")
        opt_row.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(opt_row, text="Overwrite originals in-place",
                        variable=self.overwrite_var,
                        style="App.TCheckbutton").pack(side="left")

        # --- right column
        right = ttk.Frame(body, style="App.TFrame")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        # Preview card — visual confirmation of the selected file
        prev_card = Card(right, title="Preview  ·  预览")
        prev_card.pack(fill="x")

        prev_box = tk.Frame(prev_card.body, bg=Theme.SURFACE_2, height=240)
        prev_box.pack(fill="x")
        prev_box.pack_propagate(False)               # keep the box height fixed
        self._preview_lbl = tk.Label(
            prev_box, text="选择文件以预览",
            bg=Theme.SURFACE_2, fg=Theme.TEXT_3, font=Theme.FONT,
        )
        self._preview_lbl.pack(fill="both", expand=True)

        self._preview_meta = ttk.Label(prev_card.body, text="", style="Hint.TLabel")
        self._preview_meta.pack(anchor="w", pady=(2, 0))

        # Camera card
        cam_card = Card(right, title="Camera  ·  相机")
        cam_card.pack(fill="x")

        cam_grid = ttk.Frame(cam_card.body, style="CardInner.TFrame")
        cam_grid.pack(fill="x")
        cam_grid.columnconfigure(0, weight=1, uniform="cam")
        cam_grid.columnconfigure(1, weight=1, uniform="cam")
        self._labeled_entry(cam_grid, "Make",  self.make_var,  col=0, hint="e.g. Nikon")
        self._labeled_entry(cam_grid, "Model", self.model_var, col=1, hint="e.g. Z6 II")

        # Exposure card
        exp_card = Card(right, title="Exposure  ·  曝光参数")
        exp_card.pack(fill="x", pady=(10, 0))

        grid = ttk.Frame(exp_card.body, style="CardInner.TFrame")
        grid.pack(fill="x")
        for c in range(3):
            grid.columnconfigure(c, weight=1, uniform="exp")

        self._labeled_entry(grid, "Focal (mm)",   self.field_vars["focal_length"], col=0)
        self._labeled_entry(grid, "Aperture (F)", self.field_vars["aperture"],     col=1)
        self._labeled_entry(grid, "ISO",          self.field_vars["iso"],          col=2)

        # Date card
        date_card = Card(right, title="Date  ·  拍摄时间")
        date_card.pack(fill="x", pady=(10, 0))

        row = ttk.Frame(date_card.body, style="CardInner.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Date", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        if HAS_CALENDAR:
            self.date_picker = DateEntry(row, width=14, date_pattern="yyyy-MM-dd",
                                         background=Theme.SURFACE, foreground=Theme.TEXT,
                                         borderwidth=1, relief="solid")
            self.date_picker.grid(row=0, column=1, sticky="w", padx=(8, 4), pady=2)
            ttk.Entry(row, textvariable=self.time_var, width=8,
                      style="Field.TEntry", font=Theme.FONT_MONO
                      ).grid(row=0, column=2, sticky="w", padx=(4, 4))
            ttk.Label(row, text="HH:MM:SS", style="Hint.TLabel").grid(row=0, column=3, sticky="w", padx=(6, 0))
        else:
            self.date_str_var = tk.StringVar()
            ttk.Entry(row, textvariable=self.date_str_var,
                      style="Field.TEntry", font=Theme.FONT_MONO, width=24
                      ).grid(row=0, column=1, columnspan=3, sticky="we", padx=(8, 0), pady=2)
        row.columnconfigure(1, weight=1)

        # GPS card (under exposure)
        gps_card = Card(right, title="Location  ·  GPS 坐标")
        gps_card.pack(fill="x", pady=(10, 0))

        grid = ttk.Frame(gps_card.body, style="CardInner.TFrame")
        grid.pack(fill="x")
        for c in range(2):
            grid.columnconfigure(c, weight=1, uniform="gps")

        self._labeled_entry(grid, "Longitude", self.field_vars["longitude"],
                            col=0, hint="-180 ~ 180")
        self._labeled_entry(grid, "Latitude",  self.field_vars["latitude"],
                            col=1, hint="-90 ~ 90")

        self._labeled_entry(gps_card.body, "Altitude (m)", self.field_vars["altitude"])

        # map button (if available)
        if HAS_MAP:
            map_btn_row = ttk.Frame(gps_card.body, style="CardInner.TFrame")
            map_btn_row.pack(fill="x", pady=(6, 0))
            ttk.Button(map_btn_row, text="📍 Pick on map",
                       style="Ghost.TButton",
                       command=self._open_map).pack(side="right")

        # ---- bottom action bar + progress + log
        actions = ttk.Frame(outer, style="App.TFrame")
        actions.pack(fill="x", pady=(12, 5))

        ttk.Button(actions, text="Read EXIF",  style="Ghost.TButton",
                   command=self._read_selected).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Write EXIF", style="Primary.TButton",
                   command=self._write_async).pack(side="left", padx=6)
        ttk.Button(actions, text="Read folder EXIF → CSV",
                   style="Ghost.TButton",
                   command=self._export_csv).pack(side="left", padx=6)

        self.rollback_btn = ttk.Button(actions, text="↩ Rollback",
                                       style="Ghost.TButton", state="disabled",
                                       command=self._rollback_clicked)
        self.rollback_btn.pack(side="left", padx=6)

        # progress
        prog_row = ttk.Frame(outer, style="App.TFrame")
        prog_row.pack(fill="x")
        self.progress = ttk.Progressbar(prog_row, orient="horizontal", mode="determinate",
                                        style="Accent.Horizontal.TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_text = ttk.Label(prog_row, text="", style="Status.TLabel")
        self.progress_text.pack(side="left", padx=(8, 0))

        # log
        log_holder = ttk.Frame(outer, style="App.TFrame")
        log_holder.pack(fill="x", pady=(6, 0))

        log_frame = ttk.Frame(log_holder, style="Log.TFrame", padding=(8, 6))
        log_frame.pack(fill="x")
        self.log = tk.Text(log_frame, height=4, wrap="word", state="disabled",
                           background=Theme.SURFACE_2, foreground=Theme.TEXT,
                           insertbackground=Theme.TEXT,
                           relief="flat", highlightthickness=1,
                           highlightbackground=Theme.BORDER,
                           font=Theme.FONT_MONO, bd=0, padx=4, pady=4)
        self.log.pack(side="left", fill="both", expand=True)
        log_sb = ttk.Scrollbar(log_frame, orient="vertical",
                               style="Vertical.TScrollbar",
                               command=self.log.yview)
        log_sb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=log_sb.set)

        # color tags for log
        self.log.tag_configure("ok",    foreground=Theme.SUCCESS)
        self.log.tag_configure("err",   foreground=Theme.ERROR)
        self.log.tag_configure("warn",  foreground=Theme.WARNING)
        self.log.tag_configure("muted", foreground=Theme.TEXT_3)

        # status bar
        status_bar = ttk.Frame(outer, style="App.TFrame")
        status_bar.pack(fill="x", pady=(6, 0))
        self.status_lbl = ttk.Label(status_bar, textvariable=self.status_var,
                                    style="Status.TLabel")
        self.status_lbl.pack(side="left")
        self.err_lbl = ttk.Label(status_bar, text="", style="Err.TLabel")
        self.err_lbl.pack(side="right")

    def _labeled_entry(self, parent, label, var, *, hint=None, col=None):
        """Build a label + entry (+ optional hint) block. Returns its frame."""
        frame = ttk.Frame(parent, style="CardInner.TFrame")
        if col is not None:
            frame.grid(row=0, column=col, sticky="nsew",
                       padx=(0 if col == 0 else 8, 0), pady=2)
        else:
            frame.pack(fill="x", pady=2)
        ttk.Label(frame, text=label, style="Field.TLabel").pack(anchor="w")
        ttk.Entry(frame, textvariable=var, style="Field.TEntry",
                  font=Theme.FONT_MONO, width=1).pack(fill="x", pady=(3, 0))
        if hint:
            ttk.Label(frame, text=hint, style="Hint.TLabel").pack(anchor="w", pady=(2, 0))
        return frame

    # ------------------------------------------------------------------
    #  Preview states
    # ------------------------------------------------------------------
    def _preview_state(self, state: str, text: str = "", meta: str = ""):
        """Switch the preview area between its four states.

        state: "empty" (nothing selected) | "loading" | "error" | "ready"
        The "ready" state is driven by _show_preview() via the image config.
        """
        self._preview_lbl.configure(
            text=text,
            fg={"empty": Theme.TEXT_3, "error": Theme.ERROR}.get(state, Theme.TEXT_2),
        )
        self._preview_meta.configure(text=meta)

    # ------------------------------------------------------------------
    #  Preview rendering
    # ------------------------------------------------------------------
    PREVIEW_MAX   = (360, 240)   # box size; images are contain-fit inside it
    PREVIEW_CACHE = 20           # LRU entries — bounds memory on big folders

    def _load_thumb_image(self, path: str):
        """Decode + downscale *path* into a PIL image ready for PhotoImage.

        Returns (img, (w, h)) where (w, h) is the orientation-corrected
        *source* resolution for the meta line. Raises on undecodable files.

        Performance: draft() lets Pillow decode JPEG at 1/8 scale directly
        (huge on network drives); exif_transpose() fixes portrait shots;
        thumbnail() does the final contain-fit.
        """
        img = Image.open(path)
        w, h = img.size
        orient = img.getexif().get(0x0112, 1)      # 274 = Orientation
        if orient in (5, 6, 7, 8):                 # 90/270 deg rotations
            w, h = h, w
        img.draft("RGB", self.PREVIEW_MAX)
        img = ImageOps.exif_transpose(img)
        img.thumbnail(self.PREVIEW_MAX)
        return img, (w, h)

    def _make_thumb(self, path: str):
        """Tk wrapper around _load_thumb_image."""
        img, size = self._load_thumb_image(path)
        return ImageTk.PhotoImage(img), size

    def _show_preview(self, path: str):
        if path in self._preview_cache:                    # cache hit: zero decode
            self._preview_cache.move_to_end(path)
            self._preview_photo = self._preview_cache[path]
            self._preview_lbl.configure(image=self._preview_photo, text="")
            self._set_preview_meta(path)
            return

        self._preview_state("loading", "载入中…", os.path.basename(path))
        try:
            photo, (w, h) = self._make_thumb(path)
        except Exception as e:
            self._preview_state("error", "无法解码此格式", os.path.basename(path))
            self._log(f"预览失败 {os.path.basename(path)}: {e}", "err")
            return

        self._preview_cache[path] = photo                  # dict holds the ref
        self._cache_put(path, photo)
        self._preview_photo = photo                        # instance ref: no GC
        self._preview_lbl.configure(image=photo, text="")
        self._preview_state("ready", "", self._meta_text(path, w, h))

    def _cache_put(self, path: str, photo):
        """Insert into the LRU cache and evict oldest entries beyond the cap."""
        self._preview_cache[path] = photo
        self._preview_cache.move_to_end(path)
        while len(self._preview_cache) > self.PREVIEW_CACHE:
            self._preview_cache.popitem(last=False)

    def _meta_text(self, path: str, w: int, h: int) -> str:
        try:
            kb = os.path.getsize(path) / 1024
            return f"{os.path.basename(path)}  ·  {w}×{h}  ·  {kb:.0f} KB"
        except OSError:
            return os.path.basename(path)

    def _set_preview_meta(self, path: str):
        """Meta line for a cache hit — size is read back from the cached entry."""
        try:
            with Image.open(path) as im:
                size = ImageOps.exif_transpose(im).size
            self._preview_state("ready", "", self._meta_text(path, *size))
        except Exception:
            self._preview_state("ready", "", os.path.basename(path))

    def _schedule_preview(self, *_):
        """<<ListboxSelect>> handler with 100 ms debounce for rapid clicking."""
        if self._preview_after_id:
            self.root.after_cancel(self._preview_after_id)
        self._preview_after_id = self.root.after(100, self._preview_for_selection)

    def _preview_for_selection(self):
        self._preview_after_id = None
        sel = self.file_list.curselection()
        if not sel or not (0 <= sel[0] < len(self.input_files)):
            self._preview_state("empty", "选择文件以预览", "")
            return
        self._show_preview(self.input_files[sel[0]])

    # ------------------------------------------------------------------
    #  Logging & status
    # ------------------------------------------------------------------
    def _log(self, msg, level="info"):
        self.log.configure(state="normal")
        prefix = {"ok": "✔ ", "err": "✖ ", "warn": "! ", "muted": "· ", "info": ""}.get(level, "")
        self.log.insert("end", prefix + msg + "\n", (level,) if level != "info" else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, text):
        self.status_var.set(text)

    def _set_error(self, text):
        self.err_lbl.configure(text=text or "")

    # ------------------------------------------------------------------
    #  Source handling
    # ------------------------------------------------------------------
    def _pick_file(self):
        p = filedialog.askopenfilename(
            filetypes=[("Images", " ".join(f"*{e}" for e in self.IMG_EXTS)), ("All files", "*.*")]
        )
        if not p:
            return
        self.input_files = [p]
        self.src_var.set(p)
        if not self.out_var.get():
            self.out_var.set(os.path.dirname(p))
        self._refresh_list()

    def _pick_folder(self):
        d = filedialog.askdirectory()
        if not d:
            return
        files = [os.path.join(d, f) for f in os.listdir(d)
                 if f.lower().endswith(self.IMG_EXTS)]
        files.sort()
        self.input_files = files
        self.src_var.set(d)
        if not self.out_var.get():
            self.out_var.set(d)
        self._refresh_list()

    def _pick_output(self):
        d = filedialog.askdirectory()
        if d:
            self.out_var.set(d)

    def _clear_files(self):
        self.input_files.clear()
        self.src_var.set("")
        self._refresh_list()
        self._preview_state("empty", "选择文件以预览", "")
        self._log("File list cleared.", "muted")

    def _refresh_list(self):
        self.file_list.delete(0, "end")
        for f in self.input_files:
            self.file_list.insert("end", os.path.basename(f))
        n = len(self.input_files)
        self.list_count_lbl.configure(
            text=f"{n} file{'s' if n != 1 else ''} loaded"
        )
        if n:                                   # auto-select first → fires preview
            self.file_list.selection_set(0)
            # NB: Tk does not guarantee <<ListboxSelect>> for a programmatic
            # selection_set, so schedule the preview explicitly here.
            self._schedule_preview()

    # ------------------------------------------------------------------
    #  Read existing EXIF
    # ------------------------------------------------------------------
    def _read_selected(self):
        sel = self.file_list.curselection()
        if not sel:
            messagebox.showinfo(self.APP_NAME, "Select a file from the list first.")
            return
        path = self.input_files[sel[0]]
        try:
            info = read_exif_info(path)
        except Exception as e:
            messagebox.showerror(self.APP_NAME, f"Failed to read EXIF:\n{e}")
            return

        self.field_vars["longitude"].set(info["longitude"])
        self.field_vars["latitude"].set(info["latitude"])
        self.field_vars["altitude"].set(info["altitude"])
        self.field_vars["focal_length"].set(info["focal_length"])
        self.field_vars["aperture"].set(info["fnumber"])
        self.field_vars["iso"].set(info["iso"])
        self.make_var.set(info["make"])
        self.model_var.set(info["model"])

        if info["datetime"]:
            try:
                d, t = info["datetime"].split(" ")
                if HAS_CALENDAR:
                    self.date_picker.set_date(d.replace(":", "-"))
                else:
                    self.date_str_var.set(info["datetime"])
                self.time_var.set(t)
            except ValueError:
                if not HAS_CALENDAR:
                    self.date_str_var.set(info["datetime"])

        self._log(f"Loaded EXIF from {os.path.basename(path)}", "ok")

    # ------------------------------------------------------------------
    #  Map picker
    # ------------------------------------------------------------------
    def _open_map(self):
        if not HAS_MAP:
            messagebox.showinfo(self.APP_NAME,
                                "Install tkintermapview to enable:\n"
                                "  pip install tkintermapview")
            return
        win = tk.Toplevel(self.root)
        win.title(f"{self.APP_NAME}  ·  Map picker")
        win.geometry("640x520")
        win.configure(bg=Theme.BG)
        self._apply_icon(win)

        m = TkinterMapView(win, width=640, height=440, corner_radius=0)
        m.pack(fill="both", expand=True, padx=10, pady=10)

        try:
            lat = float(self.field_vars["latitude"].get())
            lon = float(self.field_vars["longitude"].get())
        except ValueError:
            lat, lon = 39.9042, 116.4074  # Beijing fallback
        m.set_position(lat, lon)
        m.set_zoom(8)

        info = ttk.Label(win, text="Click anywhere on the map to pick coordinates",
                         style="Subtitle.TLabel")
        info.pack(pady=(0, 8))

        def on_click(coords):
            la, lo = coords
            info.configure(text=f"Latitude: {la:.6f}   Longitude: {lo:.6f}")
            self.field_vars["latitude"].set(f"{la:.6f}")
            self.field_vars["longitude"].set(f"{lo:.6f}")
            self._log(f"Map pick: {la:.6f}, {lo:.6f}", "muted")

        m.add_left_click_map_command(on_click)

        ttk.Button(win, text="Close", style="Ghost.TButton",
                   command=win.destroy).pack(pady=(0, 10))

    # ------------------------------------------------------------------
    #  Writing (off the UI thread)
    # ------------------------------------------------------------------
    def _collect_params(self):
        p = {
            "longitude":     self.field_vars["longitude"].get(),
            "latitude":      self.field_vars["latitude"].get(),
            "altitude":      self.field_vars["altitude"].get(),
            "focal_length":  self.field_vars["focal_length"].get(),
            "aperture":      self.field_vars["aperture"].get(),
            "iso":           self.field_vars["iso"].get(),
            "make":          self.make_var.get(),
            "model":         self.model_var.get(),
            "date":          self._date_string(),
        }
        return p

    def _date_string(self):
        if HAS_CALENDAR:
            d = self.date_picker.get_date().strftime("%Y:%m:%d")
            t = self.time_var.get().strip() or "00:00:00"
            return f"{d} {t}"
        return self.date_str_var.get()

    def _write_async(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.input_files:
            messagebox.showerror(self.APP_NAME, "Pick at least one image first.")
            return

        params = self._collect_params()
        ok, errs = Validator.check(**params)
        if not ok:
            self._set_error("  ·  ".join(errs))
            messagebox.showerror(self.APP_NAME, "\n".join(errs))
            return
        self._set_error("")

        out_dir = self.out_var.get().strip() or os.path.dirname(self.input_files[0])
        os.makedirs(out_dir, exist_ok=True)
        overwrite = self.overwrite_var.get()

        self.progress["value"] = 0
        self.progress["maximum"] = len(self.input_files)
        self.progress_text.configure(text=f"0 / {len(self.input_files)}")
        self._set_status("Writing…")
        self._log(f"Starting write of {len(self.input_files)} file(s)…", "muted")

        self.worker = threading.Thread(
            target=self._write_worker,
            args=(list(self.input_files), out_dir, params, overwrite),
            daemon=True,
        )
        self.worker.start()

    def _write_worker(self, files, out_dir, params, overwrite):
        success = 0
        total = len(files)
        # defensive: the worker owns the destination, so make sure it exists
        if not overwrite:
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as e:
                self.root.after(0, lambda m=str(e):
                                self._log(f"Cannot create output dir: {m}", "err"))
                self.root.after(0, lambda: self._finish_write(0, total))
                return
        # normalize numeric values
        try:
            lon = float(params["longitude"])
            lat = float(params["latitude"])
            alt = float(params["altitude"]) if (params["altitude"] or "").strip() else 0.0
            fl  = float(params["focal_length"])
            ap  = float(params["aperture"])
            iso = int(params["iso"])
        except (TypeError, ValueError) as e:
            self.root.after(0, lambda: self._log(f"Bad numeric input: {e}", "err"))
            return

        for idx, src in enumerate(files, 1):
            base = os.path.basename(src)
            name, ext = os.path.splitext(base)
            if overwrite:
                dst = src
            else:
                dst = os.path.join(out_dir, f"{name}_exif{ext}")

            # Open (and fully decode, in overwrite mode) BEFORE any rename:
            # safe_write moves the source away, so reading it afterwards fails.
            try:
                img = Image.open(src)
                if overwrite:
                    img.load()
            except Exception as e:
                self.root.after(0, lambda b=base, m=str(e):
                                self._log(f"{b} failed: {m}", "err"))
                continue

            def save_to(path, _img=img):
                apply_exif(_img, path,
                           longitude=lon, latitude=lat, altitude=alt,
                           focal_length=fl, aperture=ap, iso=iso,
                           make=params["make"], model=params["model"],
                           shot_date=params["date"])

            rec = None
            if self.bm is not None:
                try:
                    rec = self.bm.begin(src, dst, "overwrite" if overwrite else "copy")
                except OSError as e:
                    self.root.after(0, lambda m=str(e):
                                    self._log(f"无法记备份 {base}: {m}", "err"))

            try:
                if rec is not None and self.bm is not None:
                    self.bm.safe_write(rec, save_to)
                    self.bm.commit(rec)
                else:
                    save_to(dst)
                success += 1
                self.root.after(0, lambda b=base, d=os.path.basename(dst):
                                self._log(f"{b}  →  {d}", "ok"))
            except Exception as e:
                self.root.after(0, lambda b=base, m=str(e):
                                self._log(f"{b} failed: {m}", "err"))

            self.root.after(0, lambda i=idx, t=total:
                            (self.progress.configure(value=i),
                             self.progress_text.configure(text=f"{i} / {t}  {self._backup_text()}")))

        self.root.after(0, lambda s=success, t=total:
                        self._finish_write(s, t))

    # ------------------------------------------------------------------
    #  Rollback (UI)
    # ------------------------------------------------------------------
    def _update_rollback_btn(self) -> None:
        n = self.bm.backup_count() if self.bm is not None else 0
        self.rollback_btn.configure(state="normal" if n else "disabled")

    def _rollback_clicked(self) -> None:
        if self.bm is None or not self.bm.recs:
            return
        n = self.bm.backup_count()
        mb = self.bm.backup_bytes() / 1024 / 1024
        if not messagebox.askyesno(
                self.APP_NAME,
                f"撤销本次会话的全部修改？\n\n"
                f"将还原 {n} 个文件（备份占用 {mb:.1f} MB）。"):
            return
        done, skipped = self.bm.rollback()
        self._log(f"回滚完成，还原 {done} 项。", "ok" if done else "warn")
        self._set_status(f"已回滚 {done} 项")
        self.progress_text.configure(text="")
        self._update_rollback_btn()
        if skipped:
            messagebox.showwarning(
                self.APP_NAME,
                "以下文件在修改后被外部改动过，已跳过（备份仍保留）：\n\n"
                + "\n".join(os.path.basename(p) for p in skipped[:12])
                + ("\n…" if len(skipped) > 12 else ""))

    def _atexit_cleanup(self) -> None:
        """Last-resort cleanup if the window died without going through _on_close."""
        if self.bm is not None and self.bm.recs:
            self.bm.cleanup()

    def _backup_text(self) -> str:
        """Status suffix showing what we currently hold for rollback."""
        if self.bm is None:
            return ""
        n = self.bm.backup_count()
        if not n:
            return ""
        mb = self.bm.backup_bytes() / 1024 / 1024
        return f"· 备份 {n} 个 / {mb:.1f} MB"

    def _finish_write(self, success, total):
        self.progress["value"] = 0
        self.progress_text.configure(text=self._backup_text().lstrip("· "))
        self._set_status(f"Done  ·  {success}/{total} succeeded  {self._backup_text()}")
        self._update_rollback_btn()
        self._log(f"Finished. {success}/{total} succeeded.", "ok"
                  if success == total else "warn")
        messagebox.showinfo(self.APP_NAME,
                            f"Processed {total} file(s).\n"
                            f"{success} succeeded, {total - success} failed.")

    # ------------------------------------------------------------------
    #  CSV export
    # ------------------------------------------------------------------
    def _export_csv(self):
        if not self.input_files:
            messagebox.showinfo(self.APP_NAME, "No files loaded.")
            return
        target = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile="exif_summary.csv",
        )
        if not target:
            return
        rows = []
        for f in self.input_files:
            info = read_exif_info(f)
            rows.append({"file": os.path.basename(f), **{k: v for k, v in info.items()}})
        with open(target, "w", newline="", encoding="utf-8") as fp:
            w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        self._log(f"Exported CSV: {target}", "ok")

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------
    def _on_close(self):
        """Exit flow: offer rollback, then always release the backups."""
        if self.bm is not None and self.bm.recs:
            n = self.bm.backup_count()
            mb = self.bm.backup_bytes() / 1024 / 1024
            choice = messagebox.askyesnocancel(
                self.APP_NAME,
                f"本次会话已修改 {len(self.bm.recs)} 个文件"
                f"（备份 {n} 个 / {mb:.1f} MB）。\n\n"
                f"【是】撤销全部修改并退出\n"
                f"【否】保留修改，删除备份并退出\n"
                f"【取消】返回程序")
            if choice is None:                       # cancel: stay
                return
            if choice:                               # yes: roll back
                done, skipped = self.bm.rollback()
                self._log(f"退出前回滚：还原 {done} 项"
                          + (f"，跳过 {len(skipped)} 项" if skipped else ""),
                          "ok" if done and not skipped else "warn")
                if skipped:
                    messagebox.showwarning(
                        self.APP_NAME,
                        f"{len(skipped)} 个文件在修改后被外部改动，未回滚（备份保留在原目录）。")
        if self.bm is not None:
            self.bm.cleanup()                        # backups + journal gone
        try:
            self.root.destroy()
        except Exception:
            pass


# =====================================================================
#  Entry point
# =====================================================================
def enable_dpi_awareness():
    """Crisp text on HiDPI displays; must run before Tk() is created."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # system-DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    enable_dpi_awareness()
    root = tk.Tk()
    app = ExifCraft(root)
    atexit.register(app._atexit_cleanup)   # safety net if mainloop dies abruptly

    # ---- crash recovery for a previous run (UI exists → findings are logged)
    try:
        restored, orphans = BackupManager.recover_sessions(APP_DIR)
        if restored:
            app._log(f"上次异常退出：已自动还原 {restored} 个文件。", "warn")
        if orphans:
            app._log(f"发现 {orphans} 个残留备份文件（无会话记录）。", "warn")
            if messagebox.askyesno(
                    app.APP_NAME,
                    f"发现 {orphans} 个残留备份文件（上次未正常退出留下）。\n\n"
                    f"现在删除它们以释放空间？"):
                removed = BackupManager.purge_orphans(APP_DIR)
                app._log(f"已清理 {removed} 个残留备份。", "ok")
    except Exception as e:
        app._log(f"启动恢复检查失败：{e}", "err")

    root.mainloop()


if __name__ == "__main__":
    main()
