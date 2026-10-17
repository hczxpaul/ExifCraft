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


class TestValidatorPartial:
    def test_empty_selection_is_an_error(self, mod):
        ok, errs = mod.Validator.check_fields({})
        assert not ok and any("至少" in e for e in errs)

    def test_only_checked_fields_are_validated(self, mod):
        ok, errs = mod.Validator.check_fields({"iso": "400"})   # no others required
        assert ok, errs

    def test_out_of_range_still_caught(self, mod):
        ok, errs = mod.Validator.check_fields({"longitude": "999"})
        assert not ok and any("180" in e for e in errs)

    def test_clear_marker_skips_validation(self, mod):
        ok, errs = mod.Validator.check_fields({"make": None})
        assert ok, errs

    def test_bad_date_format_caught(self, mod):
        ok, errs = mod.Validator.check_fields({"datetime": "2024-05-06 07:08:09"})
        assert not ok and any("格式" in e for e in errs)

    def test_clear_gps_only_is_valid(self, mod):
        ok, errs = mod.Validator.check_fields({"clear_gps": True})
        assert ok, errs


class TestThreeStateUI:
    def test_collect_only_checked_fields(self, app):
        app.field_vars["make"].set("NIKON")
        app.write_flags["make"].set(False)          # user unchecked Make
        app.write_flags["model"].set(True)
        app.field_vars["model"].set("Z6 II")
        p = app._collect_params()
        assert "make" not in p
        assert p["model"] == "Z6 II"

    def test_clear_flag_yields_none(self, app):
        app.field_vars["make"].set("NIKON")
        app._clear_field("make")
        assert app._collect_params()["make"] is None

    def test_typing_after_clear_restores_value(self, app):
        app._clear_field("make")
        app.field_vars["make"].set("SONY")          # trace cancels the clear
        assert app._collect_params()["make"] == "SONY"

    def test_date_flag_off_excludes_datetime(self, app):
        app.date_flag.set(False)
        assert "datetime" not in app._collect_params()

    def test_clear_date_yields_none(self, app):
        app._clear_date()
        assert app._collect_params()["datetime"] is None

    def test_gps_clear_flag(self, app):
        app._toggle_clear_gps()
        assert app._collect_params()["clear_gps"] is True
        app._toggle_clear_gps()
        assert "clear_gps" not in app._collect_params()

    def test_unchanged_entry_is_disabled(self, app):
        app.write_flags["iso"].set(False)
        assert "disabled" in app._field_entries["iso"].state()
        app.write_flags["iso"].set(True)
        assert "disabled" not in app._field_entries["iso"].state()


class TestReadBackFillsEverything:
    """Regression red line: read a file, write it back → same as a full write."""

    def test_read_then_write_preserves_all_fields(self, app, mod, jpeg_factory, tmp_path):
        src = str(jpeg_factory("rt.jpg"))
        seeded = tmp_path / "seeded.jpg"
        write_fields(mod, src, str(seeded), FULL)

        app.input_files = [str(seeded)]
        app._refresh_list()
        app._read_selected()

        # after a read every non-empty field must be selected, none cleared
        for name, var in app.field_vars.items():
            if var.get().strip():
                assert app.write_flags[name].get(), name
            assert not app.clear_flags[name].get(), name
        assert app.date_flag.get()

        out = tmp_path / "seeded_exif.jpg"
        app._write_worker([str(seeded)], str(tmp_path), app._collect_params(),
                          overwrite=False)
        app.root.update()
        result = mod.read_exif_info(str(out))
        assert result["make"] == "NIKON"
        assert result["model"] == "Z6 II"
        assert result["datetime"] == "2024:05:06 07:08:09"
        assert result["latitude"] == "22.540000"
        assert result["longitude"] == "114.050000"


class TestBatchPartialEdit:
    """The actual user story: change one field across a whole folder."""

    def test_only_gps_changes_across_batch(self, app, mod, no_dialogs,
                                           jpeg_factory, tmp_path):
        paths = []
        for i in range(4):
            p = jpeg_factory(f"batch{i}.jpg")
            write_fields(mod, p, str(tmp_path / f"seed{i}.jpg"), FULL)
            paths.append(str(tmp_path / f"seed{i}.jpg"))

        app._write_worker(paths, "", {"latitude": "31.23", "longitude": "121.47"},
                          overwrite=True)
        app.root.update()

        for p in paths:
            info = mod.read_exif_info(p)
            assert info["latitude"] == "31.230000"      # changed
            assert info["make"] == "NIKON"              # preserved
            assert info["datetime"] == "2024:05:06 07:08:09"   # preserved
            assert info["iso"] == "200"                 # preserved

    def test_clear_gps_across_batch(self, app, mod, no_dialogs,
                                    jpeg_factory, tmp_path):
        paths = []
        for i in range(3):
            p = jpeg_factory(f"cb{i}.jpg")
            write_fields(mod, p, str(tmp_path / f"s{i}.jpg"), FULL)
            paths.append(str(tmp_path / f"s{i}.jpg"))

        app._write_worker(paths, "", {"clear_gps": True}, overwrite=True)
        app.root.update()

        for p in paths:
            info = mod.read_exif_info(p)
            assert info["latitude"] == ""               # GPS gone
            assert info["make"] == "NIKON"              # everything else intact
            assert info["datetime"] == "2024:05:06 07:08:09"
