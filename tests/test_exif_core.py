"""Baseline tests for the EXIF core — lock CURRENT behaviour before refactoring.

These tests are the regression net for the EXIF core: full writes must land
the values that read_exif_info() reports back, on every supported file.
"""
from pathlib import Path

import piexif
import pytest
from PIL import Image

import exif_patch as ep
from conftest import ExifCraft


def _write(mod, src, dst, fields):
    """Write through the production entry point (bytes in, bytes out)."""
    mod.write_exif_from_bytes(Path(str(src)).read_bytes(), str(dst), fields)


# ---------------------------------------------------------------- DMS
class TestDms:
    def test_positive(self):
        dms = ep._dms(22.541667)
        assert dms[0] == (22, 1)
        assert dms[1] == (32, 1)
        assert dms[2][0] / dms[2][1] == pytest.approx(30.0, abs=0.01)

    def test_exact_half_degree(self):
        # 22.5 deg == 22 deg 30' 0" exactly — no float drift
        assert ep._dms(22.5) == [(22, 1), (30, 1), (0, 1)]

    def test_zero(self):
        assert ep._dms(0.0) == [(0, 1), (0, 1), (0, 1)]

    def test_negative_uses_positive_parts(self):
        # standard DMS: parts non-negative, sign carried by the Ref tag
        dms = ep._dms(-33.5)
        assert dms[0] == (33, 1)
        assert dms[1] == (30, 1)


# ------------------------------------------------------- set_exif_data (full)
FULL = dict(
    longitude=114.054167, latitude=22.541667, altitude=12.0,
    focal_length=35.0, aperture=2.8, iso=200,
    make="NIKON", model="Z6 II", datetime="2024:05:06 07:08:09",
)


class TestSetExifFullWrite:
    def test_roundtrip_jpeg(self, mod, jpeg_factory, tmp_path):
        src = jpeg_factory("plain.jpg")
        dst = tmp_path / "out.jpg"
        _write(mod, src, dst, FULL)

        info = mod.read_exif_info(dst)
        assert info["make"] == "NIKON"
        assert info["model"] == "Z6 II"
        assert info["datetime"] == "2024:05:06 07:08:09"
        assert info["focal_length"] == "35.0"
        assert info["fnumber"] == "2.8"
        assert info["iso"] == "200"
        assert info["latitude"] == "22.541667"
        assert info["longitude"] == "114.054167"
        assert info["altitude"] == "12.0"

    def test_copy_naming_convention(self, mod, jpeg_factory, tmp_path):
        src = jpeg_factory("holiday.jpg")
        dst = tmp_path / "out.jpg"
        _write(mod, src, dst, FULL)
        # current API takes the dst path verbatim — caller builds the _exif name
        assert dst.exists() and src.exists()

    def test_overwrite_in_place(self, mod, jpeg_factory):
        src = jpeg_factory("inplace.jpg")
        _write(mod, src, src, FULL)
        info = mod.read_exif_info(src)
        assert info["model"] == "Z6 II"

    def test_gps_ref_west_south(self, mod, jpeg_factory, tmp_path):
        src = jpeg_factory("neg.jpg")
        dst = tmp_path / "neg_out.jpg"
        _write(mod, src, dst, {**FULL, "longitude": -46.6, "latitude": -23.5})
        info = mod.read_exif_info(dst)
        assert info["longitude"] == "-46.600000"
        assert info["latitude"] == "-23.500000"

    def test_altitude_zero_writes_zero_tag(self, mod, jpeg_factory, tmp_path):
        src = jpeg_factory("alt0.jpg")
        dst = tmp_path / "alt0_out.jpg"
        _write(mod, src, dst, {**FULL, "altitude": 0})
        info = mod.read_exif_info(dst)
        assert info["altitude"] == "0.0"

    def test_existing_exif_is_merged_not_replaced(self, mod, jpeg_factory, tmp_path):
        # a file that already has GPS keeps unrelated tags; full write still
        # overwrites the fields it owns (current behaviour).
        src = jpeg_factory("hasgps.jpg", gps=True)
        dst = tmp_path / "hasgps_out.jpg"
        _write(mod, src, dst, FULL)
        info = mod.read_exif_info(dst)
        assert info["longitude"] == "114.054167"  # overwritten with new value

    def test_unrecognized_image_raises(self, mod, tmp_path):
        bad = tmp_path / "bad.jpg"
        bad.write_bytes(b"not an image at all")
        with pytest.raises(ValueError):
            mod.write_exif_from_bytes(bad.read_bytes(), tmp_path / "x.jpg", FULL)


# ------------------------------------------------------------- read_exif_info
class TestReadExif:
    def test_missing_exif_returns_blanks(self, mod, jpeg_factory):
        info = mod.read_exif_info(jpeg_factory("bare.jpg"))
        assert set(info.values()) == {""}

    def test_gps_negative_ref_readback(self, mod, jpeg_factory):
        src = jpeg_factory("gpsneg.jpg", gps=False)
        # craft a southern/western GPS exif by hand
        from PIL import Image as _I
        img = _I.new("RGB", (8, 8))
        exif = {"0th": {}, "Exif": {}, "GPS": {
            piexif.GPSIFD.GPSLatitude: [(23, 1), (30, 1), (0, 1)],
            piexif.GPSIFD.GPSLatitudeRef: "S",
            piexif.GPSIFD.GPSLongitude: [(46, 1), (36, 1), (0, 1)],
            piexif.GPSIFD.GPSLongitudeRef: "W",
        }, "1st": {}, "thumbnail": None}
        img.save(src, "jpeg", exif=piexif.dump(exif))
        info = mod.read_exif_info(src)
        assert info["latitude"] == "-23.500000"
        assert info["longitude"] == "-46.600000"

    def test_broken_file_returns_blanks(self, mod, tmp_path):
        junk = tmp_path / "junk.jpg"
        junk.write_bytes(b"\xff\xd8broken")
        info = mod.read_exif_info(junk)
        assert info["make"] == ""
