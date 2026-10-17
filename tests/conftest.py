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
