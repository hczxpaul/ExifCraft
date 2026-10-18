"""Tests for feature 1: thumbnail preview."""
import tkinter as tk

import pytest
from PIL import Image

from conftest import ExifCraft


# tk_root / app fixtures live in conftest.py (shared with the backup tests)


class TestThumbGeneration:
    def test_landscape_fits_box(self, app, jpeg_factory):
        p = jpeg_factory("land.jpg", size=(800, 600))
        img, size = app._load_thumb_image(str(p))
        assert size == (800, 600)
        assert img.width <= app.PREVIEW_MAX[0]
        assert img.height <= app.PREVIEW_MAX[1]
        assert img.width / img.height == pytest.approx(800 / 600, rel=0.01)

    def test_portrait_fits_box(self, app, jpeg_factory):
        p = jpeg_factory("port.jpg", size=(600, 800))
        img, size = app._load_thumb_image(str(p))
        assert size == (600, 800)
        assert img.width <= app.PREVIEW_MAX[0]
        assert img.height <= app.PREVIEW_MAX[1]

    def test_orientation_corrected_size(self, app, jpeg_factory):
        # orientation=6 (rotate 90 CW): a 64x48 shot displays as 48x64
        p = jpeg_factory("rot.jpg", size=(64, 48), orientation=6)
        img, size = app._load_thumb_image(str(p))
        assert size == (48, 64)
        assert img.height >= img.width          # portrait after transpose

    def test_large_image_downsamples(self, app, jpeg_factory):
        p = jpeg_factory("big.jpg", size=(4000, 3000))
        img, size = app._load_thumb_image(str(p))
        assert size == (4000, 3000)              # meta reports the true size
        assert img.width <= app.PREVIEW_MAX[0]
        assert img.height <= app.PREVIEW_MAX[1]

    def test_bad_file_raises(self, app, tmp_path):
        bad = tmp_path / "corrupt.jpg"
        bad.write_bytes(b"\xff\xd8 not really a jpeg")
        with pytest.raises(Exception):
            app._load_thumb_image(str(bad))

    def test_meta_text(self, app, jpeg_factory):
        p = jpeg_factory("meta.jpg", size=(800, 600))
        text = app._meta_text(str(p), 800, 600)
        assert "meta.jpg" in text
        assert "800×600" in text
        assert "KB" in text


class TestPreviewCache:
    def test_lru_evicts_oldest(self, app, jpeg_factory):
        paths = [str(jpeg_factory(f"lru_{i}.jpg")) for i in range(25)]
        for p in paths:
            app._cache_put(p, "photo")
        assert len(app._preview_cache) == app.PREVIEW_CACHE
        assert paths[0] not in app._preview_cache          # oldest evicted
        assert paths[-1] in app._preview_cache             # newest kept

    def test_move_to_end_refreshes_recency(self, app, jpeg_factory):
        p0, p1 = str(jpeg_factory("a.jpg")), str(jpeg_factory("b.jpg"))
        app._cache_put(p0, "x")
        app._cache_put(p1, "y")
        app._cache_put(p0, "x2")                           # p0 touched again
        app._cache_put(str(jpeg_factory("c.jpg")), "z")    # fill beyond... no, cap 20
        # p1 should now be the least-recent entry
        assert list(app._preview_cache.keys())[0] == p1


class TestDebounce:
    def test_rapid_selection_fires_once(self, app, jpeg_factory):
        calls = []
        app._preview_for_selection = lambda: calls.append(1)
        app._schedule_preview()
        app._schedule_preview()
        app._schedule_preview()
        # let the single pending after() fire
        app.root.after(150, app.root.quit)
        app.root.mainloop()
        assert len(calls) == 1
