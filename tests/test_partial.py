"""Tests for feature 2: partial (single-field) EXIF editing."""
import piexif
from PIL import Image

from conftest import ExifCraft


def write_fields(mod, src, dst, fields):
    """apply_exif_fields takes an OPENED image, not a path."""
    with Image.open(str(src)) as img:
        return mod.apply_exif_fields(img, str(dst), fields)

FULL = dict(
    longitude="114.05", latitude="22.54", altitude="12",
    focal_length="35", aperture="2.8", iso="200",
    make="NIKON", model="Z6 II", datetime="2024:05:06 07:08:09",
)


def write_all(mod, src, dst, **over):
    write_fields(mod, src, dst, {**FULL, **over})
    return mod.read_exif_info(dst)


class TestPartialWrite:
    """The core promise: untouched fields survive a partial edit."""

    def test_only_gps_keeps_camera_and_time(self, mod, jpeg_factory, tmp_path):
        src = jpeg_factory("p.jpg")
        # step 1: write everything
        base = tmp_path / "base.jpg"
        write_fields(mod, src, str(base), FULL)
        # step 2: change only the GPS on that file
        out = tmp_path / "out.jpg"
        write_fields(mod, base, str(out), {"latitude": "31.23", "longitude": "121.47"})

        info = mod.read_exif_info(str(out))
        assert info["latitude"] == "31.230000"
        assert info["longitude"] == "121.470000"
        assert info["make"] == "NIKON"                  # untouched
        assert info["model"] == "Z6 II"                 # untouched
        assert info["datetime"] == "2024:05:06 07:08:09"  # untouched
        assert info["focal_length"] == "35.0"
        assert info["iso"] == "200"

    def test_single_field_write_leaves_others(self, mod, jpeg_factory, tmp_path):
        src = jpeg_factory("s.jpg")
        base = tmp_path / "b.jpg"
        write_fields(mod, src, str(base), FULL)
        out = tmp_path / "o.jpg"
        write_fields(mod, base, str(out), {"iso": "1600"})

        info = mod.read_exif_info(str(out))
        assert info["iso"] == "1600"
        assert info["make"] == "NIKON"
        assert info["fnumber"] == "2.8"

    def test_absent_keys_never_touched(self, mod, jpeg_factory, tmp_path):
        src = jpeg_factory("a.jpg")
        out = tmp_path / "a_out.jpg"
        # an empty field set must be a pure no-op round-trip
        write_fields(mod, src, str(out), {})
        info = mod.read_exif_info(str(out))
        assert set(info.values()) == {""}               # still has no exif at all


class TestFieldClearing:
    def test_clear_gps_removes_only_gps(self, mod, jpeg_factory, tmp_path):
        src = jpeg_factory("cg.jpg")
        base = tmp_path / "b.jpg"
        write_fields(mod, src, str(base), FULL)
        out = tmp_path / "o.jpg"
        write_fields(mod, base, str(out), {"clear_gps": True})

        raw = piexif.load(open(out, "rb").read())
        assert raw["GPS"] == {}                         # GPS gone
        info = mod.read_exif_info(str(out))
        assert info["latitude"] == ""
        assert info["make"] == "NIKON"                  # camera data survives
        assert info["datetime"] == "2024:05:06 07:08:09"

    def test_clear_single_field_keeps_its_sibling(self, mod, jpeg_factory, tmp_path):
        src = jpeg_factory("cf.jpg")
        base = tmp_path / "b.jpg"
        write_fields(mod, src, str(base), FULL)
        out = tmp_path / "o.jpg"
        write_fields(mod, base, str(out), {"make": None})

        info = mod.read_exif_info(str(out))
        assert info["make"] == ""
        assert info["model"] == "Z6 II"                 # sibling untouched
        assert info["datetime"] == "2024:05:06 07:08:09"

    def test_clear_latitude_keeps_longitude(self, mod, jpeg_factory, tmp_path):
        src = jpeg_factory("cl.jpg")
        base = tmp_path / "b.jpg"
        write_fields(mod, src, str(base), FULL)
        out = tmp_path / "o.jpg"
        write_fields(mod, base, str(out), {"latitude": None})

        info = mod.read_exif_info(str(out))
        assert info["latitude"] == ""
        assert info["longitude"] == "114.050000"
        assert info["altitude"] == "12.0"

    def test_clear_altitude_keeps_coordinates(self, mod, jpeg_factory, tmp_path):
        src = jpeg_factory("ca.jpg")
        base = tmp_path / "b.jpg"
        write_fields(mod, src, str(base), FULL)
        out = tmp_path / "o.jpg"
        write_fields(mod, base, str(out), {"altitude": None})

        info = mod.read_exif_info(str(out))
        assert info["altitude"] == ""
        assert info["latitude"] == "22.540000"


class TestRegressionFullWrite:
    """Full field set must behave exactly like the pre-refactor writer."""

    def test_all_fields_roundtrip(self, mod, jpeg_factory, tmp_path):
        src = jpeg_factory("full.jpg")
        out = tmp_path / "full_out.jpg"
        info = write_all(mod, src, str(out))
        assert info["make"] == "NIKON"
        assert info["model"] == "Z6 II"
        assert info["datetime"] == "2024:05:06 07:08:09"
        assert info["focal_length"] == "35.0"
        assert info["fnumber"] == "2.8"
        assert info["iso"] == "200"
        assert info["latitude"] == "22.540000"
        assert info["longitude"] == "114.050000"
        assert info["altitude"] == "12.0"

    def test_negative_coordinates_still_roundtrip(self, mod, jpeg_factory, tmp_path):
        src = jpeg_factory("neg.jpg")
        out = tmp_path / "neg_out.jpg"
        info = write_all(mod, src, str(out),
                         latitude="-23.5", longitude="-46.6")
        assert info["latitude"] == "-23.500000"
        assert info["longitude"] == "-46.600000"

    def test_set_exif_data_public_api_unchanged(self, mod, jpeg_factory, tmp_path):
        src = jpeg_factory("pub.jpg")
        out = tmp_path / "pub_out.jpg"
        mod.set_exif_data(src, str(out), longitude=114.05, latitude=22.54,
                          altitude=12.0, focal_length=35.0, aperture=2.8,
                          iso=200, make="NIKON", model="Z6 II",
                          shot_date="2024:05:06 07:08:09")
        info = mod.read_exif_info(str(out))
        assert info["make"] == "NIKON"
        assert info["datetime"] == "2024:05:06 07:08:09"
