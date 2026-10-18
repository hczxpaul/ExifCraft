"""Shared fixtures: load ExifCraft.pyw as an importable module, build test images."""
import importlib.util
from pathlib import Path

import piexif
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "ExifCraft.pyw"


def _load_module():
    """Load the .pyw entry file as a module (extension isn't importable directly)."""
    spec = importlib.util.spec_from_file_location("ExifCraft", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# loaded once — module level only defines things, main() stays behind __name__ guard
ExifCraft = _load_module()


@pytest.fixture
def mod():
    return ExifCraft


# ---------------------------------------------------------------------------
#  GUI fixtures — one Tk root for the whole session (Windows cannot create
#  and destroy many roots), plus an app instance that also redirects the
#  backup journal into the test's temp dir.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def tk_root():
    import tkinter as tk
    root = tk.Tk()
    yield root
    root.destroy()


@pytest.fixture
def no_dialogs(mod, monkeypatch):
    """Block modal dialogs — they would wait for a human click in tests."""
    for name in ("showinfo", "showerror", "showwarning",
                 "askyesno", "askokcancel", "askyesnocancel"):
        monkeypatch.setattr(mod.messagebox, name, lambda *a, **k: True)


@pytest.fixture(scope="module")
def _app_instance(tk_root, tmp_path_factory):
    """One ExifCraft window per test module.

    Building the whole UI costs ~0.7s; doing it per test was the single
    biggest cost in the suite (~100s of the runtime). Tests get a shared
    instance with its state reset before each one instead.
    """
    app_dir = tmp_path_factory.mktemp("appdir")
    ExifCraft.APP_DIR = str(app_dir)
    a = ExifCraft.ExifCraft(tk_root)
    yield a
    if a._preview_after_id:
        try:
            tk_root.after_cancel(a._preview_after_id)
        except Exception:
            pass
    for child in tk_root.winfo_children():
        child.destroy()


def _reset_state(app) -> None:
    """Put a shared instance back to "just built" before the next test."""
    app.input_files.clear()
    app.file_checked.clear()
    app._import_order.clear()
    app._sort_col, app._sort_rev = None, False
    app._drag = None
    app._preview_cache.clear()
    app._preview_photo = None
    app.worker = None

    for var in (app.src_var, app.out_var):
        var.set("")
    app.overwrite_var.set(False)
    for v in app.write_flags.values():
        v.set(False)
    for v in app.clear_flags.values():
        v.set(False)
    for v in app.field_vars.values():
        v.set("")
    app.date_flag.set(False)
    app.date_clear.set(False)
    app.gps_clear.set(False)
    app.gps_clear_btn.configure(text="✕ 清除 GPS 信息")

    app.progress["value"] = 0
    app.progress_text.configure(text="")
    app._set_error("")
    app.status_var.set("Ready")
    app.log.configure(state="normal")
    app.log.delete("1.0", "end")
    app.log.configure(state="disabled")

    if app.bm is not None:
        app.bm.cleanup()                    # backups + journal from last test
    app._update_rollback_btn()              # no backups -> button disabled
    if app._map_win is not None and app._map_win.winfo_exists():
        app._map_win.destroy()
    app._map_win = None

    app._refresh_list()                     # empty list, footer back to zero


@pytest.fixture
def app(_app_instance):
    _reset_state(_app_instance)
    return _app_instance


def _base_exif(*, gps: bool, orientation: int | None) -> dict:
    exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    if orientation is not None:
        exif["0th"][piexif.ImageIFD.Orientation] = orientation
    if gps:
        exif["GPS"].update({
            piexif.GPSIFD.GPSLatitude: [(22, 1), (32, 1), (30, 1)],
            piexif.GPSIFD.GPSLatitudeRef: "N",
            piexif.GPSIFD.GPSLongitude: [(114, 1), (3, 1), (15, 1)],
            piexif.GPSIFD.GPSLongitudeRef: "E",
            piexif.GPSIFD.GPSAltitude: (12, 1),
        })
    return exif


@pytest.fixture
def jpeg_factory(tmp_path):
    """Factory: make_jpeg(name, gps=..., orientation=...) -> path (in tmp dir)."""
    counter = {"n": 0}

    def make(name=None, *, gps=False, orientation=None, size=(64, 48)):
        name = name or f"img_{counter['n']:02d}.jpg"
        counter["n"] += 1
        p = tmp_path / name
        img = Image.new("RGB", size, (180, 60, 60))
        exif = _base_exif(gps=gps, orientation=orientation)
        if orientation is not None or gps:
            img.save(p, "jpeg", exif=piexif.dump(exif))
        else:
            img.save(p, "jpeg")
        return p

    return make


@pytest.fixture
def png_factory(tmp_path):
    def make(name=None, *, gps=False, size=(64, 48)):
        name = name or f"img_{abs(hash(name)) % 9999}.png"
        p = tmp_path / name
        img = Image.new("RGB", size, (60, 120, 180))
        exif = _base_exif(gps=gps, orientation=None)
        if gps:
            img.save(p, "png", exif=piexif.dump(exif))
        else:
            img.save(p, "png")
        return p

    return make
