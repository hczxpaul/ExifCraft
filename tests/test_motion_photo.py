"""Live Photo / animated-container protection.

The writer must never re-encode a JPEG: bytes after EOI hold the motion
video and the XMP segment declares it. It must also refuse containers where
a re-save would silently keep only the first frame or page.
"""
import io
import os
import struct

import piexif
import pytest
from PIL import Image

import exif_patch as ep


# ----------------------------------------------------------------------
#  Sample builders
# ----------------------------------------------------------------------
def _base_jpeg(size=(320, 240), exif=True) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", size, (110, 140, 170))
    kw = {}
    if exif:
        d = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        d["0th"][piexif.ImageIFD.Make] = "vivo"
        d["0th"][piexif.ImageIFD.Model] = "V2309A"
        kw["exif"] = piexif.dump(d)
    img.save(buf, "jpeg", quality=90, **kw)
    return buf.getvalue()


def _with_xmp(raw: bytes, video_len: int) -> bytes:
    xmp = (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" '
        'xmlns:GCamera="http://ns.google.com/photos/1.0/camera/" '
        'xmlns:Container="http://ns.google.com/photos/1.0/container/" '
        'GCamera:MotionPhoto="1" GCamera:MotionPhotoVersion="1">'
        f'<Container:Directory><rdf:li><Container:Item Item:Mime="image/jpeg" '
        'Item:Semantic="Primary" Item:Length="0" Item:Padding="0"/></rdf:li>'
        f'<rdf:li><Container:Item Item:Mime="video/mp4" '
        f'Item:Semantic="MotionPhoto" Item:Length="{video_len}" '
        'Item:Padding="0"/></rdf:li></Container:Directory>'
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    ).encode("utf-8")
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + xmp
    seg = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    return raw[:2] + seg + raw[2:]


def build_motion(video_bytes: bytes = b"") -> tuple[bytes, bytes]:
    """Return (file bytes, video block)."""
    if not video_bytes:
        video_bytes = (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42mp41"
                       + b"\x00\x00\x00\x08mdat" + os.urandom(24_000))
    raw = _with_xmp(_base_jpeg(), len(video_bytes))
    return raw + video_bytes, video_bytes


# ----------------------------------------------------------------------
#  Detection: plain JPG vs Live Photo
# ----------------------------------------------------------------------
class TestDetectKind:
    def test_plain_jpeg(self):
        assert ep.detect_kind(_base_jpeg()) == "jpeg"

    def test_motion_jpeg(self):
        raw, _v = build_motion()
        assert ep.detect_kind(raw) == "motion-jpeg"

    def test_motion_jpeg_without_xmp_detected_by_tail(self):
        """Some devices ship the video with no XMP declaration."""
        raw = _base_jpeg() + (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"
                              b"mp42mp41" + b"\x00\x00\x00\x08mdat" + b"\x00" * 64)
        assert ep.detect_kind(raw) == "motion-jpeg"

    def test_png(self):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "png")
        assert ep.detect_kind(buf.getvalue()) == "png"

    def test_tiff(self):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "tiff")
        assert ep.detect_kind(buf.getvalue()) == "tiff"

    def test_unknown(self):
        assert ep.detect_kind(b"not an image at all") == "unknown"

    def test_describe_is_human_readable(self):
        assert "Live Photo" in ep.describe(build_motion()[0])
        assert "JPG" in ep.describe(_base_jpeg())


# ----------------------------------------------------------------------
#  Motion photo safety
# ----------------------------------------------------------------------
def _eoi(raw: bytes) -> int:
    return next(s.start for s in ep.scan_segments(raw) if s.marker == 0xD9)


class TestMotionPhotoSurvives:
    def test_video_bytes_untouched(self):
        raw, video = build_motion()
        out = ep.patch_exif_bytes(raw, {"make": "NIKON"})
        assert out[_eoi(out) + 2:] == video

    def test_xmp_segment_kept(self):
        raw, _v = build_motion()
        out = ep.patch_exif_bytes(raw, {"make": "NIKON"})
        assert b"ns.adobe.com/xap" in out
        assert b'MotionPhoto="1"' in out

    def test_declared_length_still_lands_on_ftyp(self):
        raw, video = build_motion()
        out = ep.patch_exif_bytes(raw, {"make": "A_MUCH_LONGER_NAME"})
        length = ep.video_length_from_xmp(out)
        assert length == len(video)
        start = len(out) - length
        assert out[start + 4:start + 8] == b"ftyp"

    def test_self_check_is_clean(self):
        raw, _v = build_motion()
        for fields in ({"make": "NIKON"}, {"make": "LONGLONGLONG"},
                       {"datetime": "2024:05:06 07:08:09"},
                       {"latitude": 22.5, "longitude": 114.0}):
            out = ep.patch_exif_bytes(raw, fields)
            assert ep.verify_patch(raw, out) == [], fields

    def test_unrelated_bytes_identical(self):
        """Everything except the EXIF segment must be byte-identical."""
        raw, _v = build_motion()
        out = ep.patch_exif_bytes(raw, {"make": "NIKON"})
        seg_o = ep.find_exif_segment(raw, ep.scan_segments(raw))
        seg_n = ep.find_exif_segment(out, ep.scan_segments(out))
        assert out[:seg_n.start] == raw[:seg_o.start]
        # after the segment: EOI + video, shifted by the same delta
        assert out[seg_n.end:] == raw[seg_o.end:]

    def test_idempotent(self):
        raw, _v = build_motion()
        once = ep.patch_exif_bytes(raw, {"make": "NIKON"})
        twice = ep.patch_exif_bytes(once, {"make": "NIKON"})
        assert once == twice


# ----------------------------------------------------------------------
#  Values
# ----------------------------------------------------------------------
class TestValues:
    def _tags(self, raw):
        return piexif.load(Image.open(io.BytesIO(raw)).info["exif"])

    def test_make_model(self):
        raw, _v = build_motion()
        out = ep.patch_exif_bytes(raw, {"make": "NIKON", "model": "Z6 II"})
        ex = self._tags(out)
        assert ex["0th"][piexif.ImageIFD.Make] == b"NIKON"
        assert ex["0th"][piexif.ImageIFD.Model] == b"Z6 II"

    def test_iso_aperture_focal(self):
        raw, _v = build_motion()
        out = ep.patch_exif_bytes(raw, {"iso": 800, "aperture": 2.8,
                                        "focal_length": 50.0})
        ex = self._tags(out)
        assert ex["Exif"][piexif.ExifIFD.ISOSpeedRatings] == 800
        assert ex["Exif"][piexif.ExifIFD.FNumber] == (14, 5)
        assert ex["Exif"][piexif.ExifIFD.FocalLength] == (50, 1)

    def test_datetime(self):
        raw, _v = build_motion()
        out = ep.patch_exif_bytes(raw, {"datetime": "2024:05:06 07:08:09"})
        ex = self._tags(out)
        assert ex["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2024:05:06 07:08:09"


# ----------------------------------------------------------------------
#  Refusals
# ----------------------------------------------------------------------
class TestRefusesNonJpeg:
    """The byte patcher is JPEG-only; other containers go through Pillow."""

    def _png(self):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "png")
        return buf.getvalue()

    def test_png_rejected_by_patcher(self):
        with pytest.raises(ep.UnsupportedFormat):
            ep.patch_exif_bytes(self._png(), {"make": "X"})

    def test_broken_jpeg_rejected(self):
        with pytest.raises(ValueError):
            ep.patch_exif_bytes(b"not an image", {"make": "X"})
