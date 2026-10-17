"""Baseline tests for Validator — lock CURRENT validation matrix."""
import pytest

from conftest import ExifCraft

OK = dict(
    longitude="114.05", latitude="22.54", altitude="12",
    focal_length="35", aperture="2.8", iso="200",
    make="NIKON", model="Z6 II", date="2024:05:06 07:08:09",
)


def check(mod, **over):
    params = {**OK, **over}
    return mod.Validator.check(**params)


class TestValidator:
    def test_all_valid(self, mod):
        ok, errs = check(mod)
        assert ok, errs

    def test_longitude_out_of_range(self, mod):
        ok, errs = check(mod, longitude="181")
        assert not ok and any("180" in e for e in errs)

    def test_latitude_out_of_range(self, mod):
        ok, errs = check(mod, latitude="-91")
        assert not ok

    def test_altitude_may_be_empty(self, mod):
        ok, errs = check(mod, altitude="")
        assert ok, errs

    def test_altitude_non_numeric(self, mod):
        ok, errs = check(mod, altitude="high")
        assert not ok and any("海拔" in e for e in errs)

    def test_focal_length_zero_rejected(self, mod):
        ok, errs = check(mod, focal_length="0")
        assert not ok

    def test_iso_must_be_integer(self, mod):
        ok, errs = check(mod, iso="200.5")
        assert not ok

    def test_iso_below_one_rejected(self, mod):
        ok, errs = check(mod, iso="0")
        assert not ok

    def test_make_empty(self, mod):
        ok, errs = check(mod, make="  ")
        assert not ok and any("制造商" in e for e in errs)

    def test_model_empty(self, mod):
        ok, errs = check(mod, model="")
        assert not ok

    def test_date_wrong_format(self, mod):
        ok, errs = check(mod, date="2024-05-06 07:08:09")
        assert not ok and any("格式" in e for e in errs)

    def test_date_empty(self, mod):
        ok, errs = check(mod, date="")
        assert not ok

    def test_all_errors_collected_at_once(self, mod):
        ok, errs = check(mod, longitude="999", make="", date="")
        assert not ok and len(errs) >= 3
