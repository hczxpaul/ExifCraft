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
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from datetime import datetime
from PIL import Image, ImageOps, ImageDraw, ImageTk

import piexif
import exif_patch


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
    TEXT_2      = "#475569"   # secondary          (7.58:1 on white)
    # tertiary: darkened from #94A3B8 (2.56:1) to clear WCAG AA 4.5:1 for
    # hints, placeholders and dimmed rows — 4.76:1 on white, 4.55:1 on SURFACE_2
    TEXT_3      = "#64748B"

    # checkbox: soft style — both states keep a fill + border, so ticking
    # only changes the border colour and reveals a tick (#94A3B8 used to
    # leave the unchecked border at 2.56:1, below WCAG 1.4.11's 3:1)
    CHECK_BG    = "#FFFFFF"
    CHECK_ON_BG = "#DBEAFE"   # same as PRIMARY_S
    CHECK_BORDER = "#64748B"  # 4.76:1 on white

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
# EXIF writing lives in exif_patch.py (byte-level, never re-encodes).
# The old Pillow pipeline (to_degrees / set_exif_data / apply_exif /
# apply_exif_fields) existed only to serve PNG/TIFF, which this tool no
# longer handles: every supported file is a JPEG — plain or Live Photo.


def write_exif_from_bytes(raw: bytes, dst_path: str, fields: dict) -> None:
    """Rewrite only the EXIF tags in a JPEG; every other byte is copied.

    Both supported kinds take this one path: a plain JPG (nothing outside the
    EXIF segment changes — the pixels are never re-encoded) and a Live Photo
    (the XMP motion declaration, the MPF second image and the video appended
    after EOI are copied verbatim; saving through Pillow drops all three).

    Raises:
        exif_patch.UnsupportedFormat: when the bytes are not a JPEG.
        ValueError: when the post-write self check fails.
    """
    if exif_patch.detect_kind(raw) not in ("jpeg", "motion-jpeg"):
        raise exif_patch.UnsupportedFormat(
            f"{exif_patch.describe(raw)} 不是 JPEG，本工具只处理 JPG / Live Photo")

    new = exif_patch.patch_exif_bytes(raw, fields)
    problems = exif_patch.verify_patch(raw, new)
    if problems:
        raise ValueError("EXIF 写入自检失败：" + "；".join(problems))
    with open(dst_path, "wb") as f:
        f.write(new)


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

    # ---- partial-write validation (feature 2) ---------------------------
    FIELD_LABELS = {
        "longitude": "经度", "latitude": "纬度", "altitude": "海拔",
        "focal_length": "焦距", "aperture": "光圈", "iso": "ISO",
        "make": "相机制造商", "model": "相机型号", "datetime": "拍摄日期",
    }
    NUMERIC_RULES = {
        "longitude":    dict(lo=-180, hi=180),
        "latitude":     dict(lo=-90,  hi=90),
        "altitude":     dict(),
        "focal_length": dict(lo=0.01),
        "aperture":     dict(lo=0.01),
        "iso":          dict(lo=1, integer=True),
    }

    @classmethod
    def check_fields(cls, fields: dict) -> tuple[bool, list[str]]:
        """Validate only the fields that will actually be written.

        A field present with value None means "clear it" — nothing to check.
        An empty dict means the user asked for a no-op, which is an error.
        """
        errs: list[str] = []
        if not fields:
            return False, ["请至少勾选一个要修改的字段"]

        def _num(label, raw, *, lo=None, hi=None, integer=False):
            s = (raw or "").strip()
            if not s:
                errs.append(f"{label} 不能为空")
                return
            try:
                v = int(s) if integer else float(s)
            except (TypeError, ValueError):
                errs.append(f"{label} 必须为{'整数' if integer else '数字'}")
                return
            if lo is not None and v < lo:
                errs.append(f"{label} 不能小于 {lo}")
            if hi is not None and v > hi:
                errs.append(f"{label} 不能大于 {hi}")

        for name, rules in cls.NUMERIC_RULES.items():
            if name in fields and fields[name] is not None:
                _num(cls.FIELD_LABELS[name], fields[name], **rules)

        for name in ("make", "model"):
            if name in fields and fields[name] is not None:
                if not str(fields[name]).strip():
                    errs.append(f"{cls.FIELD_LABELS[name]}不能为空")

        if "datetime" in fields and fields["datetime"] is not None:
            d = str(fields["datetime"]).strip()
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

    IMG_EXTS = (".jpg", ".jpeg")

    def __init__(self, root: tk.Tk):
        self.root = root
        # Tk's PhotoImage does not follow the DPI scale: 1 image pixel is
        # always 1 Tk pixel. In the DPI-aware build a Tk pixel is a PHYSICAL
        # pixel while fonts render at 2x, so a hard-coded 16px icon would
        # look half-size on a 4K screen. Scale every hand-picked pixel value.
        try:
            self._ui_scale = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
        except Exception:
            self._ui_scale = 1.0
        self._apply_theme()
        # the preview budget follows the same DPI scale (PhotoImage is not
        # auto-scaled, so a 640px thumbnail would look tiny on a 4K screen)
        self.PREVIEW_MAX = (self._px(640), self._px(480))
        root.title(f"{self.APP_NAME}  ·  {self.APP_TAG}")
        self.root.configure(bg=Theme.BG)

        # state
        self.input_files: list[str] = []
        # per-file tick box: writes only touch the files the user ticked
        self.file_checked: list[bool] = []
        self.worker: threading.Thread | None = None

        # file table state — all in memory, the list never touches the disk
        self._icon_on: ImageTk.PhotoImage | None = None
        self._icon_off: ImageTk.PhotoImage | None = None
        self._status_cache: dict[str, str] = {}     # path -> pending/done/failed
        self._drag: dict | None = None              # drag-to-sweep session
        self._sort_col: str | None = None           # None = import order
        self._sort_rev: bool = False
        self._import_order: list[str] = []          # order as loaded, for "no sort"
        self._preview_refit_id: str | None = None   # preview resize debounce
        self._map_win: tk.Toplevel | None = None    # single map-picker window
        self._tile_cache = None                     # TileCache, created lazily
        self._map_cls = None                        # CachedMapView, resolved once
        self._preview_box: tk.Frame | None = None
        self._fit_after_id: str | None = None       # column auto-fit debounce
        self._name_font = tkfont.Font(font=Theme.FONT_MONO)   # for measuring names

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
            "make":          tk.StringVar(),
            "model":         tk.StringVar(),
        }
        # keep the historical names pointing at the same vars
        self.make_var  = self.field_vars["make"]
        self.model_var = self.field_vars["model"]

        # --- three-state editing: check = write this field, ✕ = clear it,
        #     unchecked = leave the file's existing value alone
        # Nothing is selected at startup: the user picks what to write (via a
        # module preset or by ticking a field). A pre-selected field list made
        # a stray click on Write EXIF try to write every tag at once.
        self.write_flags = {n: tk.BooleanVar(value=False) for n in self.field_vars}
        self.clear_flags = {n: tk.BooleanVar(value=False) for n in self.field_vars}
        for n, v in self.field_vars.items():
            # typing anything cancels a pending "clear"
            v.trace_add("write", lambda *a, _n=n: self.clear_flags[_n].set(False))

        self._field_entries: dict[str, ttk.Entry] = {}
        self._toggle_chips: dict[str, tuple[tk.Label, str]] = {}
        self._clear_btns: dict[str, tk.Button] = {}
        for n, flag in self.write_flags.items():
            flag.trace_add("write", lambda *a: self._refresh_fields_ui())

        self.date_flag  = tk.BooleanVar(value=False)   # date participates? (opt-in)
        self.date_clear = tk.BooleanVar(value=False)   # or gets removed?
        self.gps_clear  = tk.BooleanVar(value=False)   # drop the whole GPS block

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
        # DPI reality check (measured on a 4K/200% machine):
        #   venv python.exe : screen 1920, scaling 1.33  (Windows virtualises)
        #   packaged exe    : screen 3840, scaling 2.67  (manifest-aware)
        # In the aware case winfo_reqwidth is already in physical pixels but
        # the layout requests come out ~40% of what a crisp 2x render needs,
        # so compensate by screen width. A virtualised process reports 1920
        # and keeps k=1 — Windows itself scales the window up.
        k        = max(1.0, sw / 1920.0)
        w = min(max(int((need_w + 16) * k), 1020), int(sw * 0.8))
        h = min(max(int((need_h + 20) * k), 680),  int(sh * 0.8))
        x = max(20, (sw - w) // 2)
        y = max(20, (sh - h) // 2 - 20)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(min(1000, sw - 100), min(680, sh - 160))
        self.root.update_idletasks()
        self._setup_column_widths()
        # the nested Panedwindow reports a bogus height until it has been laid
        # out once — place the sashes again shortly after
        self.root.after(150, self._setup_column_widths)

    def _setup_column_widths(self) -> None:
        """Place the two sashes: files gets a fixed slab, the rest is split
        55/45 between the form and the preview.

        Panedwindow sizes panes by their content's requested width, and the
        preview label requests almost nothing — without this the preview
        column starts out as a sliver.
        """
        paned = getattr(self, "_main_paned", None)
        if paned is None or not paned.panes():
            return
        total = paned.winfo_width()
        if total < self._px(300):
            return
        files_w = min(self._px(340), int(total * 0.4))
        rest    = total - files_w
        mid_w   = int(rest * 0.55)
        paned.sashpos(0, files_w)
        paned.sashpos(1, files_w + mid_w)

        # the log pane gets a fixed slab at the bottom (4 lines + padding);
        # weight alone left it with a single visible line
        mid = getattr(self, "_mid_paned", None)
        if mid is not None and mid.panes():
            h = mid.winfo_height()
            if h > self._px(260):
                log_h = self._px(92)
                mid.sashpos(0, max(self._px(140), h - log_h))

    # ------------------------------------------------------------------
    #  Theming
    # ------------------------------------------------------------------
    def _apply_theme(self):
        # ttk styles belong to the Tcl interpreter, not to this window. Building
        # a second ExifCraft on the same root (the test suite does it hundreds
        # of times) would otherwise re-configure every style and make
        # tkcalendar rebuild its own — measured 0.5s for nothing.
        if getattr(self.root, "_exifcraft_themed", False):
            return
        self.root._exifcraft_themed = True
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

        # compact variant for the quick-select row: five buttons must fit the
        # right column even in a narrow window
        s.configure("Quick.TButton",
                    font=(base.FONT[0], max(8, base.FONT[1] - 1)),
                    padding=(4, 3), foreground=base.TEXT_2,
                    background=base.SURFACE, borderwidth=0, relief="flat")
        s.map("Quick.TButton",
              foreground=[("active", base.PRIMARY)],
              background=[("active", base.SURFACE_2)])

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

        # File table (ttk.Treeview): one row per image, three columns.
        # rowheight is set in pixels so the DPI-aware build keeps the same
        # visual rhythm; the checkbox lives in the tree column as an image.
        # Paned sashes: the clam default is a hairline that is nearly
        # impossible to grab, so give it real width and a visible colour.
        s.configure("TPanedwindow", background=base.BG)
        s.configure("Sash", sashthickness=self._px(7), gripcount=0,
                    background=base.BORDER)
        s.map("Sash", background=[("active", base.BORDER_FOCUS)])
        s.configure("Files.Treeview",
                    background=base.SURFACE, fieldbackground=base.SURFACE,
                    foreground=base.TEXT, rowheight=self._px(28), borderwidth=0,
                    font=base.FONT_MONO)
        s.map("Files.Treeview",
              background=[("selected", base.PRIMARY_S)],
              foreground=[("selected", base.PRIMARY_H)])
        s.configure("Files.Treeview.Heading",
                    background=base.SURFACE_2, foreground=base.TEXT_3,
                    font=(base.FONT[0], max(9, base.FONT[1] - 2)),
                    relief="flat", borderwidth=0, padding=(5, 4))
        s.map("Files.Treeview.Heading",
              background=[("active", base.BORDER)])
        # drop the default tree indent/expand indicator — flat table look
        s.layout("Files.Treeview",
                 [("Files.Treeview.treearea", {"sticky": "nswe"})])

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

        # Compact checkbox used next to field labels (no padding, no focus ring)
        s.configure("Field.TCheckbutton",
                    background=base.SURFACE, focuscolor=base.SURFACE,
                    indicatorcolor=base.SURFACE, bordercolor=base.BORDER_FOCUS,
                    padding=0, font=(base.FONT[0], base.FONT[1] - 1))
        s.map("Field.TCheckbutton",
              background=[("active", base.SURFACE)],
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

        # ---- body: three resizable columns (files / EXIF / preview).
        # NB: body is NOT packed here (see the bottom-up packing block at the
        # end of _build) — it must claim space last so the action bar stays on
        # screen when the window is shorter than the content wants to be.
        body = ttk.Frame(outer, style="App.TFrame")

        main_paned = ttk.Panedwindow(body, orient="horizontal")
        main_paned.pack(fill="both", expand=True)
        self._main_paned = main_paned

        files_pane = ttk.Frame(main_paned, style="App.TFrame",
                               width=self._px(340))
        prev_pane  = ttk.Frame(main_paned, style="App.TFrame",
                               width=self._px(380))
        # middle column is itself split: EXIF form above, activity log below
        mid_paned  = ttk.Panedwindow(main_paned, orient="vertical")
        form_holder = ttk.Frame(mid_paned, style="App.TFrame")
        log_holder  = ttk.Frame(mid_paned, style="App.TFrame")
        mid_paned.add(form_holder, weight=8)
        mid_paned.add(log_holder,  weight=1)
        self._mid_paned = mid_paned

        # weights are only a starting ratio — the user drags the sashes.
        # The explicit widths matter: a pane whose content has no natural
        # width (the preview label) would otherwise start out tiny.
        main_paned.add(files_pane, weight=3)
        main_paned.add(mid_paned,  weight=4)
        main_paned.add(prev_pane,  weight=4)

        # --- column 1: files
        src_card = Card(files_pane, title="Source  ·  源文件")
        src_card.pack(fill="both", expand=True)

        # source row — the path field takes the full width and the buttons
        # move to a second line: a 340px column cannot host label + field +
        # two buttons and still show a filename
        path_box = ttk.Frame(src_card.body, style="CardInner.TFrame")
        path_box.pack(fill="x", pady=2)
        head = ttk.Frame(path_box, style="CardInner.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="Path", style="Field.TLabel").pack(side="left")
        self._src_entry = self._path_entry(head, self.src_var)

        btns = ttk.Frame(path_box, style="CardInner.TFrame")
        btns.pack(fill="x", pady=(3, 0))
        ttk.Button(btns, text="模板 EXIF", style="Ghost.TButton",
                   command=self._import_template).pack(side="right", padx=(4, 0))
        ttk.Button(btns, text="Folder", style="Ghost.TButton",
                   command=self._pick_folder).pack(side="right", padx=(4, 0))
        ttk.Button(btns, text="File",  style="Ghost.TButton",
                   command=self._pick_file).pack(side="right", padx=(4, 0))

        # output row
        out_box = ttk.Frame(src_card.body, style="CardInner.TFrame")
        out_box.pack(fill="x", pady=(6, 2))
        out_head = ttk.Frame(out_box, style="CardInner.TFrame")
        out_head.pack(fill="x")
        ttk.Label(out_head, text="Output", style="Field.TLabel").pack(side="left")
        self._out_entry = self._path_entry(out_head, self.out_var)

        out_btns = ttk.Frame(out_box, style="CardInner.TFrame")
        out_btns.pack(fill="x", pady=(3, 0))
        ttk.Button(out_btns, text="Browse", style="Ghost.TButton",
                   command=self._pick_output).pack(side="right", padx=(4, 0))

        # file list — three columns: ☑ / 文件名 / 状态.
        # The checkbox needs the tree column (#0) because that is the only
        # column Tk allows an image in; the filename therefore lives in the
        # first data column. No row numbers, no format column: every cell
        # here is either unique or actually changes (status), and the table
        # is built purely from memory — it never stats or opens a file.
        list_holder = ttk.Frame(src_card.body, style="CardInner.TFrame")
        list_holder.pack(fill="both", expand=True, pady=(8, 4))

        self._icon_on  = self._checkbox_icon(True)
        self._icon_off = self._checkbox_icon(False)

        self.file_list = ttk.Treeview(
            list_holder, columns=("name", "status"),
            show="tree headings", selectmode="browse", height=8,
            style="Files.Treeview",
        )
        self.file_list.heading("#0", text="", anchor="center")
        self.file_list.heading("name", text="文件", anchor="w",
                               command=self._sort_by_name)
        self.file_list.heading("status", text="状态", anchor="w",
                               command=self._sort_by_name)
        self.file_list.column("#0",     width=self._px(36),  stretch=False, anchor="center")
        self.file_list.column("name",   width=self._px(170), stretch=False, anchor="w")
        self.file_list.column("status", width=self._px(62),  stretch=False, anchor="w")

        # row tags: ticked / unticked / failed — plus a zebra stripe that
        # only sets the background so the two can coexist on one row
        self.file_list.tag_configure("on",     foreground=Theme.TEXT)
        self.file_list.tag_configure("off",    foreground=Theme.TEXT_2)
        self.file_list.tag_configure("failed", foreground=Theme.ERROR)
        self.file_list.tag_configure("odd",    background="#FAFBFC")
        self.file_list.tag_configure("even",   background=Theme.SURFACE)
        self.file_list.tag_configure("hint",   foreground=Theme.TEXT_3)

        self.file_list.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(list_holder, orient="vertical",
                           style="Vertical.TScrollbar", command=self.file_list.yview)
        sb.pack(side="right", fill="y")
        self.file_list.configure(yscrollcommand=sb.set)

        # one press/motion/release trio drives click-to-toggle AND
        # drag-to-sweep; <space> stays as the keyboard path
        self.file_list.bind("<ButtonPress-1>",   self._on_list_press)
        self.file_list.bind("<B1-Motion>",       self._on_list_drag)
        self.file_list.bind("<ButtonRelease-1>", self._on_list_release)
        self.file_list.bind("<space>",           self._on_list_space)
        self.file_list.bind("<<TreeviewSelect>>", self._schedule_preview)
        # keep the name column fitted to the longest file when the panel resizes
        self.file_list.bind("<Configure>", self._schedule_fit_columns)

        # list footer: tick-all / untick-all / clear
        list_footer = ttk.Frame(src_card.body, style="CardInner.TFrame")
        list_footer.pack(fill="x", pady=(4, 0))
        self.list_count_lbl = ttk.Label(list_footer, text="0 files",
                                        style="Hint.TLabel")
        self.list_count_lbl.pack(side="left")
        ttk.Button(list_footer, text="Clear", style="Subtle.TButton",
                   command=self._clear_files).pack(side="right")
        ttk.Button(list_footer, text="全不选", style="Subtle.TButton",
                   command=lambda: self._tick_all_files(False)).pack(side="right")
        ttk.Button(list_footer, text="全选", style="Subtle.TButton",
                   command=lambda: self._tick_all_files(True)).pack(side="right")

        # options row — the same chip component as the field toggles, so the
        # whole window speaks one checkbox language (it used to be a raw
        # ttk.Checkbutton with a system-drawn box)
        opt_row = ttk.Frame(src_card.body, style="CardInner.TFrame")
        opt_row.pack(fill="x", pady=(8, 0))
        ow_chip, ow_render = self._make_toggle_chip(
            opt_row, "Overwrite originals in-place",
            lambda: self.overwrite_var.get(),
            lambda v: self.overwrite_var.set(v))
        ow_chip.pack(side="left")
        ow_render()
        # keep the chip in sync if the flag is ever changed in code
        self.overwrite_var.trace_add("write", lambda *_a: ow_render())

        # --- column 2: EXIF form, inside a scrolling canvas.
        # The form's natural height (~700 px) still exceeds a laptop body, so
        # it keeps its own scroll area. See _on_mousewheel for the routing.
        right_scroll = tk.Canvas(form_holder, bg=Theme.BG, highlightthickness=0, bd=0)
        right_scroll.pack(side="left", fill="both", expand=True)
        right_sb = ttk.Scrollbar(form_holder, orient="vertical",
                                 style="Vertical.TScrollbar",
                                 command=right_scroll.yview)
        right_sb.pack(side="right", fill="y")
        right_scroll.configure(yscrollcommand=right_sb.set)

        right = ttk.Frame(right_scroll, style="App.TFrame")
        right_window = right_scroll.create_window((0, 0), window=right,
                                                  anchor="nw")
        # kept on the instance: the wheel handler and tests both need them
        self._right_scroll, self._right = right_scroll, right

        def _sync_right_scroll(_e=None):
            bbox = right_scroll.bbox("all")
            if bbox:
                right_scroll.configure(scrollregion=bbox)
            # keep the inner frame exactly as wide as the viewport
            right_scroll.itemconfigure(right_window,
                                       width=right_scroll.winfo_width())

        right.bind("<Configure>", _sync_right_scroll)
        right_scroll.bind("<Configure>", _sync_right_scroll)

        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

        # --- activity log: a few lines at the bottom of the middle column.
        # It used to span the whole window width for 4 lines of text.
        log_frame = ttk.Frame(log_holder, style="Log.TFrame", padding=(6, 4))
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=4, wrap="word", state="disabled",
                           background=Theme.SURFACE_2, foreground=Theme.TEXT,
                           insertbackground=Theme.TEXT,
                           relief="flat", highlightthickness=1,
                           highlightbackground=Theme.BORDER,
                           font=Theme.FONT_MONO, bd=0, padx=4, pady=3)
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

        # ---- quick selection sits at the TOP of the column: it decides
        # which modules get written, and it must stay visible even when the
        # preview is dragged taller. Grid (not pack) so the five buttons
        # share the width evenly instead of the last one falling off.
        quick = ttk.Frame(right, style="App.TFrame")
        quick.pack(fill="x", pady=(0, 8))
        ttk.Label(quick, text="快捷选择：", style="Hint.TLabel"
                  ).grid(row=0, column=0, sticky="w", padx=(0, 4))
        for i, (key, label) in enumerate(
                (("camera", "设备"), ("exposure", "曝光"),
                 ("date", "时间"), ("gps", "GPS"), ("all", "全部"))):
            quick.columnconfigure(i + 1, weight=1, uniform="quick")
            ttk.Button(quick, text=label, style="Quick.TButton",
                       command=lambda k=key: self._quick_group(k)
                       ).grid(row=0, column=i + 1, sticky="ew", padx=(3, 0))

        # --- column 3: preview. Its own vertical split (image / meta) so the
        # picture can be made taller by dragging — the same gesture as the
        # column sashes, instead of the little grip handle it used to have.
        prev_card = Card(prev_pane, title="Preview  ·  预览")
        prev_card.pack(fill="both", expand=True)

        prev_split = ttk.Panedwindow(prev_card.body, orient="vertical")
        prev_split.pack(fill="both", expand=True)
        self._prev_split = prev_split

        img_holder  = ttk.Frame(prev_split, style="CardInner.TFrame")
        meta_holder = ttk.Frame(prev_split, style="CardInner.TFrame")
        prev_split.add(img_holder,  weight=8)
        prev_split.add(meta_holder, weight=1)

        prev_box = tk.Frame(img_holder, bg=Theme.SURFACE_2)
        prev_box.pack(fill="both", expand=True)
        self._preview_lbl = tk.Label(
            prev_box, text="选择文件以预览",
            bg=Theme.SURFACE_2, fg=Theme.TEXT_3, font=Theme.FONT,
        )
        self._preview_lbl.pack(fill="both", expand=True)
        self._preview_box = prev_box

        self._preview_meta = ttk.Label(meta_holder, text="", style="Hint.TLabel")
        self._preview_meta.pack(anchor="nw", pady=(4, 0))

        # the decode budget follows whatever size the image area ends up with
        img_holder.bind("<Configure>", self._schedule_preview_refit)

        # Camera card
        cam_card = Card(right, title="Camera  ·  相机")
        cam_card.pack(fill="x")

        cam_grid = ttk.Frame(cam_card.body, style="CardInner.TFrame")
        cam_grid.pack(fill="x")
        cam_grid.columnconfigure(0, weight=1, uniform="cam")
        cam_grid.columnconfigure(1, weight=1, uniform="cam")
        self._labeled_entry(cam_grid, "Make",  self.make_var,  col=0, field="make",
                            hint="e.g. Nikon")
        self._labeled_entry(cam_grid, "Model", self.model_var, col=1, field="model",
                            hint="e.g. Z6 II")

        # Exposure card
        exp_card = Card(right, title="Exposure  ·  曝光参数")
        exp_card.pack(fill="x", pady=(10, 0))

        grid = ttk.Frame(exp_card.body, style="CardInner.TFrame")
        grid.pack(fill="x")
        for c in range(3):
            grid.columnconfigure(c, weight=1, uniform="exp")

        self._labeled_entry(grid, "Focal (mm)",   self.field_vars["focal_length"],
                            col=0, field="focal_length")
        self._labeled_entry(grid, "Aperture (F)", self.field_vars["aperture"],
                            col=1, field="aperture")
        self._labeled_entry(grid, "ISO",          self.field_vars["iso"],
                            col=2, field="iso")

        # Date card
        date_card = Card(right, title="Date  ·  拍摄时间")
        date_card.pack(fill="x", pady=(10, 0))

        row = ttk.Frame(date_card.body, style="CardInner.TFrame")
        row.pack(fill="x")
        # DateEntry has no "empty" state (clearing it snaps back to today), so
        # the whole date row is gated by a toggle chip instead of by its text.
        # Each widget gets its own grid column — label and picker overlapped before.
        date_chip, _render_date_chip = self._make_toggle_chip(
            row, "Date",
            lambda: self.date_flag.get(),
            lambda v: self.date_flag.set(v))
        date_chip.grid(row=0, column=0, sticky="w")

        def _on_date_change(*_):
            _render_date_chip()
        self.date_flag.trace_add("write", _on_date_change)

        date_clear_btn = tk.Button(row, text="✕", width=2, bd=0, relief="flat",
                                   bg=Theme.SURFACE, fg=Theme.TEXT_3, cursor="hand2",
                                   activebackground=Theme.PRIMARY_S,
                                   activeforeground=Theme.PRIMARY_H,
                                   font=(Theme.FONT[0], Theme.FONT[1]),
                                   command=self._clear_date)
        date_clear_btn.grid(row=0, column=5, sticky="e", padx=(4, 0))

        def _render_date_clear(*_):
            date_clear_btn.configure(
                text="↺" if self.date_clear.get() else "✕",
                fg=Theme.ERROR if self.date_clear.get() else Theme.TEXT_3)

        self.date_clear.trace_add("write", _render_date_clear)
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
                            col=0, field="longitude", hint="-180 ~ 180")
        self._labeled_entry(grid, "Latitude",  self.field_vars["latitude"],
                            col=1, field="latitude", hint="-90 ~ 90")

        self._labeled_entry(gps_card.body, "Altitude (m)", self.field_vars["altitude"],
                            field="altitude")

        # GPS actions row: drop the whole location block, or pick one on a map
        gps_actions = ttk.Frame(gps_card.body, style="CardInner.TFrame")
        gps_actions.pack(fill="x", pady=(6, 0))
        self.gps_clear_btn = ttk.Button(gps_actions, text="✕ 清除 GPS 信息",
                                        style="Subtle.TButton",
                                        command=self._toggle_clear_gps)
        self.gps_clear_btn.pack(side="left")
        if HAS_MAP:
            ttk.Button(gps_actions, text="📍 Pick on map",
                       style="Ghost.TButton",
                       command=self._open_map).pack(side="right")

        # ---- bottom action bar + progress + log
        # Packed side="bottom" below: the four bottom rows claim their space
        # first so a short window shrinks the file list instead of pushing
        # these off-screen.
        actions = ttk.Frame(outer, style="App.TFrame")

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
        self.progress = ttk.Progressbar(prog_row, orient="horizontal", mode="determinate",
                                        style="Accent.Horizontal.TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_text = ttk.Label(prog_row, text="", style="Status.TLabel")
        self.progress_text.pack(side="left", padx=(8, 0))

        # status bar
        status_bar = ttk.Frame(outer, style="App.TFrame")
        self.status_lbl = ttk.Label(status_bar, textvariable=self.status_var,
                                    style="Status.TLabel")
        self.status_lbl.pack(side="left")
        self.err_lbl = ttk.Label(status_bar, text="", style="Err.TLabel")
        self.err_lbl.pack(side="right")

        # ---- bottom-up packing: each row takes its space before body,
        # so on a short window the elastic pane area shrinks, not the buttons.
        status_bar.pack(side="bottom", fill="x", pady=(6, 0))
        prog_row.pack(side="bottom", fill="x")
        actions.pack(side="bottom", fill="x", pady=(12, 5))
        body.pack(fill="both", expand=True, pady=(10, 0))

        # initial render: chips are created as "☐" but all fields start checked
        self._refresh_fields_ui()
        # initial render of the file list: with no files picked yet this
        # plants the empty-state hint row (otherwise the list is a blank void)
        self._refresh_list()
        # an empty log pane looks like a broken panel — seed it with a line
        self._log(f"{self.APP_NAME} v{self.APP_VER} 已就绪 · 选择文件后开始。", "muted")

    # ------------------------------------------------------------------
    #  Middle-column (EXIF form) scrolling
    # ------------------------------------------------------------------
    def _wheel_over_form(self, x_root: int, y_root: int) -> bool:
        """True when a screen point lands inside the scrollable form column.

        Walks the widget under the pointer up the parent chain, so a wheel
        event over any card counts — not just the bare canvas.
        """
        w = self._right_scroll.winfo_containing(x_root, y_root)
        while w is not None:
            if w is self._right_scroll or w is self._right:
                return True
            w = w.master
        return False

    def _scroll_form_column(self, steps: int) -> bool:
        """Scroll the form column by *steps* wheel-notches (down if > 0).

        yview_scroll's "units" step depends on the canvas -scrollincrement
        option (0 by default), so the moveto fraction is computed directly.
        Returns False when the content already fits and cannot scroll.
        """
        bbox = self._right_scroll.bbox("all")
        if not bbox:
            return False
        region = bbox[3] - bbox[1]
        view = self._right_scroll.winfo_height()
        if region <= view:
            return False
        top = self._right_scroll.yview()[0]
        top = min(1.0, max(0.0, top + steps * 48.0 / region))
        self._right_scroll.yview_moveto(top)
        return True

    @staticmethod
    def _wheel_steps(delta: int) -> int:
        """Windows wheel convention: forward (+120) scrolls up, back (-120)
        scrolls down. The sign used to be inverted, so the panel moved the
        opposite way to every other Windows app."""
        return 1 if delta < 0 else -1

    def _on_mousewheel(self, event):
        """Route the wheel to the right column only when the pointer is
        over it — the file list on the left keeps its native wheel
        behaviour. Returns "break" when the event was consumed."""
        if not self._wheel_over_form(event.x_root, event.y_root):
            return
        self._scroll_form_column(self._wheel_steps(event.delta))
        return "break"

    def _path_entry(self, parent, var: tk.StringVar) -> ttk.Entry:
        """Single-line path field: right aligned, tail first.

        A media-library path is routinely 100+ characters and a plain Entry
        shows it from the left — exactly the part that never identifies the
        file. This one right-aligns and scrolls to the end after every
        change, so the filename is always visible. To reach the head, drag
        inside the field: selecting text scrolls the view, which is all a
        separate scrollbar would have bought.
        """
        entry = ttk.Entry(parent, textvariable=var, style="Field.TEntry",
                          font=Theme.FONT_MONO, width=1, justify="right")
        entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        var.trace_add("write", lambda *_a: self._scroll_path_to_tail(entry))
        self.root.after_idle(lambda: self._scroll_path_to_tail(entry))
        return entry

    def _scroll_path_to_tail(self, entry: ttk.Entry) -> None:
        """Show the end of the path (the filename), but only when it overflows.

        Deferred: the Entry re-lays out its text on the next idle cycle, and
        xview applied before that is ignored. Calling xview_moveto(1.0) on a
        SHORT string leaves a tiny non-zero 'first' behind, which would then
        make the scrollbar think the text overflows — so reset to the start
        when everything already fits (justify="right" still right-aligns it).
        """
        def _do():
            try:
                _first, last = entry.xview()
                entry.xview_moveto(1.0 if last < 0.999 else 0.0)
            except tk.TclError:
                pass
        self.root.after_idle(_do)

    def _labeled_entry(self, parent, label, var, *, hint=None, col=None, field=None):
        """Build a label + entry (+ optional hint) block. Returns its frame.

        When *field* is given the label becomes a click-to-toggle chip:
        ☑ Field (will be written) / ☐ Field (left untouched), plus a ✕
        button that marks the field for removal instead.
        """
        frame = ttk.Frame(parent, style="CardInner.TFrame")
        if col is not None:
            frame.grid(row=0, column=col, sticky="nsew",
                       padx=(0 if col == 0 else 8, 0), pady=2)
        else:
            frame.pack(fill="x", pady=2)

        head = ttk.Frame(frame, style="CardInner.TFrame")
        head.pack(fill="x")
        if field:
            head.pack_forget()                      # rebuild below with the chip
            head = tk.Frame(frame, bg=Theme.SURFACE)
            head.pack(fill="x")
            chip = self._make_field_chip(head, field, label)
            chip.pack(side="left")
            self._clear_btns[field] = tk.Button(
                head, text="✕", width=2, bd=0, relief="flat",
                bg=Theme.SURFACE, fg=Theme.TEXT_3, cursor="hand2",
                activebackground=Theme.PRIMARY_S, activeforeground=Theme.PRIMARY_H,
                font=(Theme.FONT[0], Theme.FONT[1]),
                command=lambda f=field: self._clear_field(f))
            self._clear_btns[field].pack(side="right")

        entry = ttk.Entry(frame, textvariable=var, style="Field.TEntry",
                          font=Theme.FONT_MONO, width=1)
        entry.pack(fill="x", pady=(3, 0))
        if field:
            self._field_entries[field] = entry
        if hint:
            ttk.Label(frame, text=hint, style="Hint.TLabel").pack(anchor="w", pady=(2, 0))
        return frame

    def _make_toggle_chip(self, parent, label: str, get_state, set_state):
        """Solid-square toggle: ■ label (on) / □ label (off).

        Built from two labels so the square and the text carry different
        colours — state is encoded by shape AND colour AND text weight, never
        by colour alone (accessibility).

        Args:
            parent: container widget.
            label: caption shown next to the square.
            get_state: () -> bool, reads the current state.
            set_state: (bool) -> None, applies a new state.

        Returns:
            (frame, render) — frame is already packed-free; call render()
            whenever the underlying state changes.
        """
        # icons are created by _build; a chip built earlier would need them
        # lazily (guards against ordering changes)
        if self._icon_on is None:
            self._icon_on  = self._checkbox_icon(True)
            self._icon_off = self._checkbox_icon(False)

        chip = tk.Frame(parent, bg=Theme.SURFACE, cursor="hand2",
                        takefocus=True, highlightthickness=1,
                        highlightbackground=Theme.SURFACE,
                        highlightcolor=Theme.PRIMARY)
        mark = tk.Label(chip,
                        image=self._icon_on if get_state() else self._icon_off,
                        bg=Theme.SURFACE, bd=0, highlightthickness=0)
        mark.pack(side="left", padx=(7, 0))       # the icon carries its own gap
        text = tk.Label(chip, text=label, font=Theme.FONT_BOLD,
                        bg=Theme.SURFACE, fg=Theme.TEXT_3)
        text.pack(side="left", padx=(0, 7), pady=5)      # roomy click target

        def render(*_):
            on = bool(get_state())
            mark.configure(image=self._icon_on if on else self._icon_off)
            text.configure(fg=Theme.TEXT if on else Theme.TEXT_3)
            chip.configure(bg=chip._hover_bg if getattr(chip, "_hover", False)
                           else Theme.SURFACE)
            mark.configure(bg=chip.cget("bg"))
            text.configure(bg=chip.cget("bg"))

        def _enter(_e=None):
            chip._hover = True
            chip._hover_bg = Theme.PRIMARY_S if get_state() else Theme.SURFACE_2
            render()

        def _leave(_e=None):
            chip._hover = False
            render()

        def _press(_e=None):
            chip._hover_bg = Theme.BORDER_FOCUS
            chip._hover = True
            render()

        def _release(_e=None):
            set_state(not get_state())
            _enter()                                     # re-evaluate hover colour

        # NB: <Button-1> and <ButtonPress-1> are THE SAME Tk sequence —
        # binding both means the second one replaces the first. The press
        # handler must live on <ButtonRelease-1> so a click ends up as
        # press → flip → release, not press → nothing.
        for w in (chip, mark, text):
            w.bind("<ButtonPress-1>", _press)
            w.bind("<ButtonRelease-1>", _release)
            w.bind("<Enter>", _enter)
            w.bind("<Leave>", _leave)

        # keyboard path: the frame takes focus (highlight ring) and toggles
        # on space/enter, so the chip is not mouse-only
        def _toggle_key(_e=None):
            set_state(not get_state())
            _enter()
            return "break"

        chip.bind("<space>", _toggle_key)
        chip.bind("<Return>", _toggle_key)
        mark.bind("<space>", _toggle_key)
        text.bind("<space>", _toggle_key)

        chip._hover = False
        chip._hover_bg = Theme.SURFACE
        return chip, render

    def _make_field_chip(self, parent, field: str, label: str) -> tk.Label:
        chip, render = self._make_toggle_chip(
            parent, label,
            lambda: self.write_flags[field].get(),
            lambda v: self.write_flags[field].set(v))
        self._toggle_chips[field] = (chip, label, render)
        return chip

    def _refresh_fields_ui(self) -> None:
        """Sync every field's visuals with its three-state flags.

        - chip:   ☑ on light blue = will be written; ☐ grey = untouched
        - entry:  disabled when the field is excluded or pending clearance
        - ✕/↺:   marks (or un-marks) a field for deletion
        """
        for name, entry in self._field_entries.items():
            active = self.write_flags[name].get() and not self.clear_flags[name].get()
            entry.state(["!disabled"] if active else ["disabled"])
        for name, (_chip, _label, render) in self._toggle_chips.items():
            render()
        for name, btn in self._clear_btns.items():
            btn.configure(text="↺" if self.clear_flags[name].get() else "✕",
                          fg=Theme.ERROR if self.clear_flags[name].get()
                          else Theme.TEXT_3)

    # which fields belong to which module button
    FIELD_GROUPS = {
        "camera":   ("make", "model"),
        "exposure": ("focal_length", "aperture", "iso"),
        "gps":      ("longitude", "latitude", "altitude"),
    }
    GROUP_TITLE = {"camera": "相机", "exposure": "曝光参数", "gps": "GPS 坐标"}

    def _reset_clear_flags(self) -> None:
        """Drop pending "delete this tag" marks when a preset is applied —
        a preset says what to write, not what to remove."""
        if self.gps_clear.get():
            self.gps_clear.set(False)
            self.gps_clear_btn.configure(text="✕ 清除 GPS 信息")
        self.date_clear.set(False)

    def _quick_group(self, group: str) -> None:
        """Module presets, each undoable by a second click.

        - camera / exposure / gps: if the whole group is already ticked the
          group is unticked; otherwise ONLY this group is ticked, so one
          click means "write this module and leave the rest untouched".
        - date: flips the date row, nothing else.
        - all:  ticks everything, or clears everything when it already is.
        """
        if group == "all":
            everything = (all(v.get() for v in self.write_flags.values())
                          and self.date_flag.get())
            for v in self.write_flags.values():
                v.set(not everything)
            self.date_flag.set(not everything)
            self._reset_clear_flags()
            self._log("已取消全部字段。" if everything
                      else "已勾选全部字段（含拍摄时间）。",
                      "muted" if everything else "ok")
            return

        if group == "date":
            on = not self.date_flag.get()
            self.date_flag.set(on)
            for v in self.write_flags.values():
                v.set(False)
            self._reset_clear_flags()
            self._log("仅修改拍摄时间：其他字段保持原值。" if on
                      else "已取消拍摄时间。", "ok" if on else "muted")
            return

        names = self.FIELD_GROUPS[group]
        all_on = all(self.write_flags[n].get() for n in names)
        for n in self.field_vars:
            self.write_flags[n].set(n in names and not all_on)
        self.date_flag.set(False)
        self._reset_clear_flags()
        title = self.GROUP_TITLE[group]
        self._log(f"仅修改{title}：其他字段保持原值。" if not all_on
                  else f"已取消{title}。", "ok" if not all_on else "muted")

    def _quick_gps_only(self) -> None:
        """Kept for programmatic callers — same as the GPS preset button."""
        self._quick_group("gps")

    def _quick_select_all(self) -> None:
        """Kept for programmatic callers — same as the 全部 button."""
        self._quick_group("all")

    def _clear_date(self) -> None:
        """Mark the capture time for removal (or undo that)."""
        if self.date_clear.get():
            self.date_clear.set(False)
            self._log("取消清除拍摄时间。", "muted")
            return
        self.date_clear.set(True)
        self.date_flag.set(True)
        self._log("已标记清除拍摄时间。", "muted")

    def _toggle_clear_gps(self) -> None:
        """Mark the whole GPS block for removal (or undo that)."""
        if self.gps_clear.get():
            self.gps_clear.set(False)
            self.gps_clear_btn.configure(text="✕ 清除 GPS 信息")
            self._log("取消清除 GPS。", "muted")
            return
        self.gps_clear.set(True)
        self.gps_clear_btn.configure(text="✓ 将清除 GPS 信息")
        self._log("下次写入将清除 GPS 信息（经纬度、海拔）。", "warn")

    def _clear_field(self, field: str) -> None:
        """Mark a field for removal on the next write (and empty its box)."""
        if self.clear_flags[field].get():          # toggle back off
            self.clear_flags[field].set(False)
            self._log(f"取消清除字段：{field}", "muted")
        else:
            self.field_vars[field].set("")
            self.write_flags[field].set(True)
            self.clear_flags[field].set(True)
            self._log(f"已标记清除字段：{field}", "muted")
        self._refresh_fields_ui()

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
    PREVIEW_MAX   = (640, 480)   # box size; images are contain-fit inside it
    PREVIEW_CACHE = 20           # LRU entries — bounds memory on big folders

    def _preview_budget(self) -> tuple[int, int]:
        """Decode budget = the LIVE size of the preview box.

        Using a hard-coded figure (it used to be 640x480) meant landscape
        shots were decoded wider than the column really is — the label then
        cropped them at both sides. Reading the widget keeps the image
        inside what the user can actually see, at any window size.
        """
        box = self._preview_box
        if box is None:
            return tuple(self.PREVIEW_MAX)
        w = box.winfo_width() or self._px(420)
        h = box.winfo_height() or self._px(320)
        return (max(self._px(120), w), max(self._px(90), h))

    def _load_thumb_image(self, path: str):
        """Decode + downscale *path* into a PIL image ready for PhotoImage.

        Returns (img, (w, h)) where (w, h) is the orientation-corrected
        *source* resolution for the meta line. Raises on undecodable files.

        Performance: draft() lets Pillow decode JPEG at 1/8 scale directly
        (huge on network drives); exif_transpose() fixes portrait shots;
        thumbnail() does the final contain-fit.
        """
        budget = self._preview_budget()
        img = Image.open(path)
        w, h = img.size
        orient = img.getexif().get(0x0112, 1)      # 274 = Orientation
        if orient in (5, 6, 7, 8):                 # 90/270 deg rotations
            w, h = h, w
        img.draft("RGB", budget)
        img = ImageOps.exif_transpose(img)
        img.thumbnail(budget)
        return img, (w, h)

    def _make_thumb(self, path: str):
        """Tk wrapper around _load_thumb_image."""
        img, size = self._load_thumb_image(path)
        return ImageTk.PhotoImage(img), size

    def _schedule_preview_refit(self, _e=None) -> None:
        """Coalesce resize events from the preview column.

        Dragging a sash fires <Configure> dozens of times per second; decoding
        on each one would stutter (a 4K JPEG takes ~20ms to shrink).
        """
        if self._preview_refit_id:
            try:
                self.root.after_cancel(self._preview_refit_id)
            except tk.TclError:
                pass
        try:
            self._preview_refit_id = self.root.after(150, self._refit_preview)
        except tk.TclError:                 # window is closing
            self._preview_refit_id = None

    def _refit_preview(self) -> None:
        self._preview_refit_id = None
        try:
            if not self._preview_box or not self._preview_box.winfo_exists():
                return
        except tk.TclError:
            return
        if self._preview_budget() == tuple(self.PREVIEW_MAX):
            return                       # same size: skip the decode entirely
        self._resize_preview_budget()

    def _resize_preview_budget(self) -> None:
        """Re-fit the preview after the drag handle changed the box size.

        The LRU cache holds PhotoImages decoded for the previous box, so the
        current file's entry is dropped and decoded again at the new size.
        """
        self.PREVIEW_MAX = self._preview_budget()
        idx = self._selected_index()
        if idx is None:
            return
        path = self.input_files[idx]
        self._preview_cache.pop(path, None)
        self._show_preview(path)

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
        idx = self._selected_index()
        if idx is None:
            self._preview_state("empty", "选择文件以预览", "")
            return
        self._show_preview(self.input_files[idx])

    def _selected_index(self) -> int | None:
        """Row index behind the current selection, or None.

        Guards against the empty-state hint row, whose iid is not numeric.
        """
        sel = self.file_list.selection()
        if not sel or not sel[0].isdigit():
            return None
        idx = int(sel[0])
        return idx if 0 <= idx < len(self.input_files) else None

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

    # ------------------------------------------------------------------
    #  File table  ── 5 columns, rendered entirely from memory
    # ------------------------------------------------------------------
    TICK_ON, TICK_OFF = "■", "□"      # text marker (logs / tests)
    HINT_IID = "__hint__"             # empty-state row (never a real file)

    def _px(self, n: float) -> int:
        """Scale a design pixel value for the current DPI."""
        return max(1, int(round(n * self._ui_scale)))

    def _checkbox_icon(self, on: bool) -> ImageTk.PhotoImage:
        """Rounded checkbox in the soft style — used by the table AND chips.

        Selected:   soft blue fill + blue border + blue tick.
        Unselected: white fill + grey border (4.76:1, WCAG 1.4.11 needs 3:1).

        Both states keep the same "fill + border" structure, so toggling
        changes the border colour and reveals a tick — instead of swapping
        a solid saturated block for a one-pixel hairline, which is what made
        the old chips look so harsh.
        """
        s   = self._px(16)
        gap = self._px(5)                    # built-in gap to the label text
        im = Image.new("RGBA", (s + gap, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        bw = max(1, round(s * 0.10))         # ~1.6px border at 16px
        r  = max(2, round(s * 0.22))
        box = (0, 0, s - 1, s - 1)
        if on:
            d.rounded_rectangle(box, radius=r, fill=Theme.CHECK_ON_BG,
                                outline=Theme.PRIMARY, width=bw)
            tw = max(1, round(s * 0.10))
            d.line((s * .28, s * .52, s * .42, s * .67),
                   fill=Theme.PRIMARY, width=tw)
            d.line((s * .42, s * .67, s * .73, s * .34),
                   fill=Theme.PRIMARY, width=tw)
        else:
            d.rounded_rectangle(box, radius=r, fill=Theme.CHECK_BG,
                                outline=Theme.CHECK_BORDER, width=bw)
        return ImageTk.PhotoImage(im)

    def _tick_marker(self, idx: int) -> str:
        """■/□ for logs and tests — independent of what is rendered."""
        return self.TICK_ON if self.file_checked[idx] else self.TICK_OFF

    # ---- write status (from the session journal, zero file IO) ----------
    def _refresh_status_index(self) -> None:
        self._status_cache = (
            {r.src: r.state for r in self.bm.recs} if self.bm else {})

    def _status_of(self, path: str) -> str | None:
        return self._status_cache.get(path)

    def _status_text(self, idx: int) -> str:
        return {"pending": "写入中", "done": "已写入",
                "failed": "失败"}.get(self._status_of(self.input_files[idx]), "就绪")

    def _row_tag(self, idx: int) -> str:
        """Colour tag for one row: failure wins over the tick state."""
        if self._status_of(self.input_files[idx]) == "failed":
            return "failed"
        return "on" if self.file_checked[idx] else "off"

    def _zebra(self, idx: int) -> str:
        return "odd" if idx % 2 else "even"

    def _refresh_status_column(self) -> None:
        """Re-read the journal and repaint the status cells.

        Called after a write batch and after a rollback — never on a timer.
        """
        self._refresh_status_index()
        for i in range(len(self.input_files)):
            iid = str(i)
            if self.file_list.exists(iid):
                self.file_list.set(iid, "status", self._status_text(i))
                self.file_list.item(iid, tags=(self._zebra(i), self._row_tag(i)))

    # ---- row painting ---------------------------------------------------
    def _set_row_state(self, idx: int) -> None:
        """Repaint one row's checkbox + colour. No file access."""
        iid = str(idx)
        if not self.file_list.exists(iid):
            return
        self.file_list.item(
            iid,
            image=self._icon_on if self.file_checked[idx] else self._icon_off,
            tags=(self._zebra(idx), self._row_tag(idx)))

    def _render_file_row(self, idx: int) -> None:
        """Backwards-compatible alias kept for callers and tests."""
        self._set_row_state(idx)

    # ---- list construction ----------------------------------------------
    def _refresh_list(self):
        """Rebuild after a new folder/file selection: everything ticked."""
        self.file_checked = [True] * len(self.input_files)
        self._import_order = list(self.input_files)   # the "no sort" order
        self._sort_col, self._sort_rev = None, False
        self._refresh_list_headers()
        self._rebuild_rows()

    def _rebuild_rows(self, keep_selection: str | None = None) -> None:
        """Insert one row per file. Pure memory — no stat, no open."""
        tv = self.file_list
        kids = tv.get_children()
        if kids:
            tv.delete(*kids)

        if not self.input_files:
            # Empty state: a blank table reads as a broken panel.
            # TEXT_3 (#64748B) = 4.76:1 on white — WCAG AA body text.
            tv.insert("", "end", iid=self.HINT_IID, text="",
                      values=("点击 File / Folder 选择图片", ""),
                      tags=("hint",))
            self._refresh_list_footer()
            self._schedule_fit_columns()
            return

        self._refresh_status_index()
        for i, f in enumerate(self.input_files):
            tv.insert("", "end", iid=str(i),
                      text="",                      # checkbox column
                      image=self._icon_on if self.file_checked[i] else self._icon_off,
                      values=(os.path.basename(f), self._status_text(i)),
                      tags=(self._zebra(i), self._row_tag(i)))

        self._refresh_list_footer()
        target = keep_selection or "0"
        if not tv.exists(target):
            target = "0" if tv.exists("0") else ""
        if target:
            tv.selection_set(target)
            tv.focus(target)
            tv.see(target)
        self._schedule_fit_columns()
        # Tk does not guarantee <<TreeviewSelect>> for a programmatic
        # selection — schedule the preview explicitly.
        self._schedule_preview()

    def _fit_name_column(self) -> None:
        """Size the filename column to the longest name, not to the panel.

        It used to be stretch=True, so it always ate every spare pixel and
        left a wide empty gutter right of short names. The width is now the
        measured width of the longest basename plus cell padding, capped by
        the space the panel really has.
        """
        self._fit_after_id = None
        try:
            if not self.file_list.winfo_exists():
                return
        except tk.TclError:                 # window already torn down
            return
        if not self.input_files:
            self.file_list.column("name", width=self._px(170))
            return
        longest = max(self._name_font.measure(os.path.basename(p))
                      for p in self.input_files)
        pad  = self._px(14)                       # cell padding + breathing room
        avail = (self.file_list.winfo_width()
                 - self._px(36) - self._px(62) - self._px(4))
        if avail < self._px(60):                  # widget not laid out yet
            avail = longest + pad
        width = max(self._px(80), min(longest + pad, max(self._px(80), avail)))
        self.file_list.column("name", width=width)

    def _schedule_fit_columns(self, _e=None) -> None:
        """Coalesce resize events into a single measurement pass."""
        try:
            if self._fit_after_id:
                self.root.after_cancel(self._fit_after_id)
            self._fit_after_id = self.root.after(80, self._fit_name_column)
        except tk.TclError:                 # window is closing
            self._fit_after_id = None

    def _refresh_list_footer(self) -> None:
        n = len(self.input_files)
        picked = sum(1 for c in self.file_checked if c)
        if n == 0:
            text = "尚未选择文件"
        elif picked == n:
            text = f"{n} file{'s' if n != 1 else ''}  ·  全部已勾选"
        else:
            text = f"{n} file{'s' if n != 1 else ''}  ·  已勾选 {picked}"
        self.list_count_lbl.configure(text=text)

    # ---- column sorting (in-memory paths only) --------------------------
    def _sort_by_name(self) -> None:
        """Header click (on 文件 or 状态): name ascending → descending → import.

        Both headers drive the same cycle — the ordering is by filename, so
        the arrow is drawn on the 文件 header either way.
        """
        if not self.input_files:
            return
        if self._sort_col != "name":
            self._sort_col, self._sort_rev = "name", False
        elif not self._sort_rev:
            self._sort_rev = True
        else:
            self._sort_col, self._sort_rev = None, False
        self._apply_sort()

    def _sort_by(self, col: str | None) -> None:
        """Programmatic sort (callers / tests): "name" or None."""
        if not self.input_files:
            return
        self._sort_col = "name" if col == "name" else None
        self._sort_rev = False
        self._apply_sort()

    def _apply_sort(self) -> None:
        keep = self._selected_path()
        pairs = list(zip(self.input_files, self.file_checked))
        if self._sort_col == "name":
            pairs.sort(key=lambda t: os.path.basename(t[0]).lower(),
                       reverse=self._sort_rev)
        else:
            # true import order, not a re-sort of the paths
            rank = {p: i for i, p in enumerate(getattr(self, "_import_order", []))}
            pairs.sort(key=lambda t: rank.get(t[0], len(rank)))

        # tick flags travel with their file, not with the row number
        self.input_files = [p for p, _c in pairs]
        self.file_checked = [c for _p, c in pairs]

        self._refresh_list_headers()
        self._rebuild_rows(keep_selection=self._iid_of_path(keep))

    def _selected_path(self) -> str | None:
        sel = self.file_list.selection()
        if sel and sel[0].isdigit() and int(sel[0]) < len(self.input_files):
            return self.input_files[int(sel[0])]
        return None

    def _iid_of_path(self, path: str | None) -> str | None:
        if path is not None and path in self.input_files:
            return str(self.input_files.index(path))
        return None

    def _refresh_list_headers(self) -> None:
        """Show ▲/▼ on the filename header while a name sort is active.

        Import order shows no arrow — there is nothing to indicate.
        """
        mark = ""
        if self._sort_col == "name":
            mark = " ▼" if self._sort_rev else " ▲"
        self.file_list.heading("name", text="文件" + mark)

    # ---- click / drag-to-sweep ------------------------------------------
    def _on_list_press(self, event):
        """Remember the drag origin. Deliberately does NOT return "break":
        Treeview moves the selection, so the preview follows the cursor.

        The column only matters for a click without movement — a sweep works
        from anywhere, exactly as before.
        """
        iid = self.file_list.identify_row(event.y)
        if not iid or iid == self.HINT_IID:
            self._drag = None
            return
        self._drag = {
            "from": iid,
            "col": self.file_list.identify_column(event.x),
            "value": not self.file_checked[int(iid)],   # fixed target, not a toggle
            # empty on purpose: the first motion repaints the origin row too
            # (a sweep is inclusive of where it started, a plain click is not)
            "seen": set(),
            "moved": False,
        }

    def _on_list_drag(self, event):
        """Sweep: paint every row crossed with the fixed target value."""
        d = self._drag
        if not d:
            return "break"
        iid = self.file_list.identify_row(event.y)
        if iid and iid != self.HINT_IID:
            if iid != d["from"]:
                d["moved"] = True
            for r in self._rows_between(d["from"], iid):
                if r in d["seen"]:
                    continue          # never re-flip: sweeping back and forth
                d["seen"].add(r)      # must leave a uniform block
                self._apply_tick(int(r), d["value"])
        self._autoscroll(event)
        return "break"

    def _on_list_release(self, event):
        d, self._drag = self._drag, None
        if d and not d["moved"]:
            # a click that never moved: only the checkbox cell toggles, while
            # the filename / status cells just select the row (preview follows)
            if d.get("col") == "#0":
                self._toggle_file_tick(int(d["from"]))
        elif d:
            self._refresh_list_footer()                 # one refresh per sweep
        return "break"

    def _rows_between(self, a: str, b: str) -> list[str]:
        """Display-order iids from *a* to *b*, both ends included."""
        rows = [i for i in self.file_list.get_children() if i != self.HINT_IID]
        try:
            i, j = rows.index(a), rows.index(b)
        except ValueError:
            return []
        return rows[min(i, j):max(i, j) + 1]

    def _apply_tick(self, idx: int, value: bool) -> None:
        if 0 <= idx < len(self.input_files) and self.file_checked[idx] != value:
            self.file_checked[idx] = value
            self._set_row_state(idx)

    def _autoscroll(self, event) -> None:
        """Keep sweeping when the pointer is held against an edge."""
        h = self.file_list.winfo_height()
        if event.y < 10:
            self.file_list.yview_scroll(-1, "units")
        elif event.y > h - 10:
            self.file_list.yview_scroll(1, "units")

    def _on_list_space(self, _event=None):
        """Keyboard: tick the highlighted row.

        Reads the selection instead of the event coordinates — a key event
        still carries mouse coordinates, which would tick the wrong row.
        """
        sel = self.file_list.selection()
        if not sel or not sel[0].isdigit():
            return
        self._toggle_file_tick(int(sel[0]))

    def _toggle_file_tick(self, idx: int | None) -> None:
        """Flip the tick box of row *idx* and keep row/preview in sync."""
        if idx is None or not (0 <= idx < len(self.input_files)):
            return
        self.file_checked[idx] = not self.file_checked[idx]
        self._set_row_state(idx)
        self._refresh_list_footer()
        iid = str(idx)
        self.file_list.selection_set(iid)
        self.file_list.focus(iid)
        self._schedule_preview()             # keep the preview in sync

    def _tick_all_files(self, value: bool):
        if not self.input_files:
            return
        self.file_checked = [value] * len(self.input_files)
        for i in range(len(self.input_files)):
            self._set_row_state(i)
        self._refresh_list_footer()
        self._log(f"{'勾选' if value else '取消勾选'}全部文件。", "muted")

    def checked_files(self) -> list[str]:
        """Files the user actually ticked — the only ones a write touches."""
        return [f for f, c in zip(self.input_files, self.file_checked) if c]

    # ------------------------------------------------------------------
    #  Read existing EXIF
    # ------------------------------------------------------------------
    def _fill_form_from_info(self, info: dict) -> None:
        """Put an EXIF dict into the form. Fills values only — never ticks.

        A read (from the selected file or from a template) is informational;
        what gets written stays the user's choice, so this deliberately does
        not touch write_flags / date_flag.
        """
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

        # a read clears any pending "delete this tag" marks
        for name in self.field_vars:
            self.clear_flags[name].set(False)
        self.date_clear.set(False)
        self.gps_clear.set(False)
        self.gps_clear_btn.configure(text="✕ 清除 GPS 信息")
        self._refresh_fields_ui()

    def _read_selected(self):
        # _selected_index() already rejects the empty-state hint row
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo(self.APP_NAME, "Select a file from the list first.")
            return
        path = self.input_files[idx]
        try:
            info = read_exif_info(path)
        except Exception as e:
            messagebox.showerror(self.APP_NAME, f"Failed to read EXIF:\n{e}")
            return

        self._fill_form_from_info(info)
        self._log(f"Loaded EXIF from {os.path.basename(path)}", "ok")

    def _import_template(self) -> None:
        """Pick any photo and adopt its EXIF as the standard for this batch.

        Unlike Read EXIF this file does not have to be in the list — the
        point is to copy the settings from a reference shot.
        """
        p = filedialog.askopenfilename(
            title="选择模板照片（导入它的 EXIF）",
            filetypes=[("Images", " ".join(f"*{e}" for e in self.IMG_EXTS)),
                       ("All files", "*.*")])
        if not p:
            return
        try:
            info = read_exif_info(p)
        except Exception as e:
            messagebox.showerror(self.APP_NAME, f"无法读取模板照片：\n{e}")
            return

        self._fill_form_from_info(info)
        filled = sum(1 for v in self.field_vars.values() if v.get().strip())
        self._log(f"已导入模板 {os.path.basename(p)}：{filled} 个字段已填值"
                  f"（未勾选，请用快捷选择或手动勾选要写入的项）。", "ok")

    # ------------------------------------------------------------------
    #  Map picker
    # ------------------------------------------------------------------
    def _open_map(self):
        if not HAS_MAP:
            messagebox.showinfo(self.APP_NAME,
                                "Install tkintermapview to enable:\n"
                                "  pip install tkintermapview")
            return
        # Refuse while a write is running: the tile threads would compete
        # with the worker for the UI thread and make both feel frozen.
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(self.APP_NAME, "正在写入，完成后再打开地图。")
            return
        # Single window. Each TkinterMapView starts ~13 tile/download threads;
        # letting the user stack several of them is how the app used to end up
        # with a hundred busy threads.
        if self._map_win is not None and self._map_win.winfo_exists():
            self._map_win.deiconify()
            self._map_win.lift()
            self._map_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        win.title(f"{self.APP_NAME}  ·  Map picker")
        win.geometry("640x560")
        win.configure(bg=Theme.BG)
        self._apply_icon(win)
        self._map_win = win

        # tiles persist in one sqlite file next to the app, so a place you
        # already looked at opens instantly the second time
        import map_cache
        if getattr(self, "_tile_cache", None) is None:
            self._tile_cache = map_cache.TileCache(
                os.path.join(APP_DIR, "map_tiles.db"))
        cache = self._tile_cache
        if getattr(self, "_map_cls", None) is None:
            self._map_cls = map_cache.build_widget_class()

        m = self._map_cls(win, cache, width=640, height=440, corner_radius=0)
        m.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        try:
            lat = float(self.field_vars["latitude"].get())
            lon = float(self.field_vars["longitude"].get())
        except ValueError:
            lat, lon = 39.9042, 116.4074  # Beijing fallback
        m.set_position(lat, lon)
        m.set_zoom(8)

        info = ttk.Label(win, text="点击地图选择位置，然后点右下角「确认」",
                         style="Subtitle.TLabel")
        info.pack(pady=(0, 8))

        # A click only previews: the coordinates move into the form when the
        # user confirms, so a stray tap can be undone by closing the window.
        picked = {"coords": None}

        def on_click(coords):
            la, lo = coords
            picked["coords"] = (la, lo)
            info.configure(text=f"Latitude: {la:.6f}   Longitude: {lo:.6f}")
            self._log(f"地图预览：{la:.6f}, {lo:.6f}", "muted")

        m.add_left_click_map_command(on_click)

        def close():
            """Destroy the map widget first, then the window.

            TkinterMapView overrides destroy() to set running=False, which is
            the only thing that stops its worker threads. Destroying the
            Toplevel alone does NOT call that override, so the threads used to
            keep spinning — retrying failed tile requests with no sleep — until
            the CPU was saturated and the whole app stopped responding.
            """
            try:
                # drain the queues first so idle threads see running=False on
                # their very next iteration instead of after a request timeout
                m.image_load_queue_tasks.clear()
                m.image_load_queue_results.clear()
                m.destroy()
            except tk.TclError:
                pass
            finally:
                self._map_win = None
                win.destroy()

        def confirm():
            coords = picked["coords"]
            if coords is not None:
                la, lo = coords
                self.field_vars["latitude"].set(f"{la:.6f}")
                self.field_vars["longitude"].set(f"{lo:.6f}")
                self._log(f"已采用地图坐标：{la:.6f}, {lo:.6f}", "ok")
            close()

        win.protocol("WM_DELETE_WINDOW", close)

        bar = ttk.Frame(win, style="App.TFrame")
        bar.pack(fill="x", padx=10, pady=(0, 10))
        cache_lbl = ttk.Label(bar, text="", style="Hint.TLabel")
        cache_lbl.pack(side="left")

        def refresh_stats():
            try:
                n, size = cache.stats()
                cache_lbl.configure(
                    text=f"本地缓存 {map_cache.format_size(size)} · {n} 张瓦片")
            except Exception:
                pass

        def clear_cache():
            cache.clear()
            refresh_stats()
            self._log("已清理地图瓦片缓存。", "muted")

        refresh_stats()
        win.after(1500, refresh_stats)      # after the first tiles land

        # cache housekeeping sits with the cache readout, well away from the
        # action button
        ttk.Button(bar, text="清理缓存", style="Subtle.TButton",
                   command=clear_cache).pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="确认", style="Ghost.TButton",
                   command=confirm).pack(side="right")

    # ------------------------------------------------------------------
    #  Writing (off the UI thread)
    # ------------------------------------------------------------------
    def _collect_params(self):
        """Build the partial-write field set from the three-state UI.

        Only checked fields appear. A checked-but-cleared field yields None
        (meaning "delete that tag").
        """
        p: dict = {}
        for name, var in self.field_vars.items():
            if not self.write_flags[name].get():
                continue                                  # leave the file's value
            p[name] = None if self.clear_flags[name].get() else var.get()
        if self.date_flag.get():
            p["datetime"] = None if self.date_clear.get() else self._date_string()
        if self.gps_clear.get():
            p["clear_gps"] = True
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

        files = self.checked_files()
        if not files:
            messagebox.showerror(self.APP_NAME,
                                 "没有勾选任何文件。\n请至少勾选一个文件再写入。")
            return

        fields = self._collect_params()
        ok, errs = Validator.check_fields(fields)
        if not ok:
            self._set_error("  ·  ".join(errs))
            messagebox.showerror(self.APP_NAME, "\n".join(errs))
            return
        self._set_error("")

        out_dir = self.out_var.get().strip() or os.path.dirname(files[0])
        os.makedirs(out_dir, exist_ok=True)
        overwrite = self.overwrite_var.get()

        self.progress["value"] = 0
        self.progress["maximum"] = len(files)
        self.progress_text.configure(text=f"0 / {len(files)}")
        self._set_status("Writing…")
        names = ", ".join(k for k in fields if k != "clear_gps")
        skipped = len(self.input_files) - len(files)
        self._log(f"开始写入勾选项 {len(files)}/{len(self.input_files)} 个文件"
                  f"（字段：{names or '仅清除 GPS'}）…"
                  + (f"，跳过未勾选 {skipped} 个" if skipped else ""), "muted")

        self.worker = threading.Thread(
            target=self._write_worker,
            args=(files, out_dir, fields, overwrite),
            daemon=True,
        )
        self.worker.start()

    def _write_worker(self, files, out_dir, fields: dict, overwrite):
        """Write *fields* (a partial field set) into every file in *files*."""
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

        for idx, src in enumerate(files, 1):
            base = os.path.basename(src)
            name, ext = os.path.splitext(base)
            if overwrite:
                dst = src
            else:
                dst = os.path.join(out_dir, f"{name}_exif{ext}")

            # Read the whole file BEFORE any rename: safe_write moves the
            # source away, so reading it afterwards fails. Working on bytes
            # also lets the patcher copy the video trailer verbatim instead
            # of re-encoding the JPEG.
            try:
                with open(src, "rb") as fh:
                    raw_src = fh.read()
            except OSError as e:
                self.root.after(0, lambda b=base, m=str(e):
                                self._log(f"{b} failed: {m}", "err"))
                continue

            # Classify from the bytes, not the extension: a .jpg may be a
            # plain photo or a Live Photo, and only the latter carries a
            # video that must be preserved.
            kind = exif_patch.detect_kind(raw_src)

            def save_to(path, _raw=raw_src):
                # partial write: only the keys in fields are touched
                write_exif_from_bytes(_raw, path, fields)

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
                note = ("  ·  Live Photo（视频已保留）"
                        if kind == "motion-jpeg" else "")
                self.root.after(0, lambda b=base, d=os.path.basename(dst), n=note:
                                self._log(f"{b}  →  {d}{n}", "ok"))
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
        self._refresh_status_column()      # rows fall back to "就绪"
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
        self._refresh_status_column()      # 就绪 → 已写入 / 失败
        self._log(f"Finished. {success}/{total} succeeded.", "ok"
                  if success == total else "warn")
        messagebox.showinfo(self.APP_NAME,
                            f"Processed {total} file(s).\n"
                            f"{success} succeeded, {total - success} failed.")

    # ------------------------------------------------------------------
    #  CSV export
    # ------------------------------------------------------------------
    def _export_csv(self):
        files = self.checked_files()
        if not files:
            messagebox.showinfo(self.APP_NAME,
                                "没有勾选任何文件。\n请先勾选要导出的文件。")
            return
        target = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile="exif_summary.csv",
        )
        if not target:
            return
        rows = []
        for f in files:                     # only what the user ticked
            info = read_exif_info(f)
            rows.append({"file": os.path.basename(f), **{k: v for k, v in info.items()}})
        with open(target, "w", newline="", encoding="utf-8") as fp:
            w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        self._log(f"Exported CSV: {target}（{len(rows)} 个勾选项）", "ok")

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------
    def _on_close(self):
        """Exit silently: closing the window means "keep what I did".

        Every write is already committed to disk, and the Rollback button is
        right there for anyone who wants to undo it. Asking on exit whether to
        roll everything back confused people into undoing work they meant to
        keep — especially in copy mode, where there is nothing to undo anyway.
        """
        self._shutdown()

    def _shutdown(self) -> None:
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
