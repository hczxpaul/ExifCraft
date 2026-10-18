"""Byte-level EXIF editing for JPEG — never re-encodes pixels.

Why this exists
---------------
A Live Photo is *one* file: a JPEG, an XMP segment declaring the motion
video, sometimes an MPF second image, and the video appended after EOI.
Re-saving the JPEG through Pillow (``img.save(..., exif=...)``) writes only
the segments Pillow knows, so the XMP segment and everything past EOI — the
entire video — are dropped. Measured on real samples: a 7.1 MB photo became
4.6 MB and stopped being a Live Photo.

Three offset schemes coexist in such a file, and each is translation-safe
as long as only the EXIF segment's *length* changes:

  * ``XMP Item:Length``      video start = file size - length  (not an offset)
  * ``MPF MP Entry`` offset  relative to the MPF header
  * ``MP4 stco``             relative to the video block

The one value that does not follow automatically is MPF ``entry[0].size``
(the primary image length); it is shifted by the delta when the EXIF segment
grows or shrinks.

Strategy
--------
1. In-place path: locate the tag, overwrite its value bytes.
   Fixed-size values (RATIONAL/SHORT/20-byte date) and shorter ASCII keep
   the segment length identical -> every offset in the file stays valid.
2. Growing ASCII (Make/Model): append the new string at the end of the TIFF
   block (4-byte aligned) and repoint that single tag.
3. Anything needing a structural change (missing tag, new GPS IFD, delete a
   whole IFD) falls back to rebuilding the EXIF segment with piexif and
   splicing it in — still never touching any other byte of the file.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# TIFF field type -> bytes per component
TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}

TAG_MAKE, TAG_MODEL, TAG_DATETIME = 0x010F, 0x0110, 0x0132
TAG_EXIF_IFD, TAG_GPS_IFD = 0x8769, 0x8825
TAG_DT_ORIGINAL = 0x9003
TAG_FOCAL, TAG_FNUM, TAG_ISO = 0x920A, 0x829D, 0x8827

GPS_LAT, GPS_LAT_REF = 0x0002, 0x0001
GPS_LON, GPS_LON_REF = 0x0004, 0x0003
GPS_ALT, GPS_ALT_REF = 0x0006, 0x0005

# field name -> (ifd key, tag, tiff type)
SIMPLE_SPECS = {
    "make":         ("ifd0", TAG_MAKE, 2),
    "model":        ("ifd0", TAG_MODEL, 2),
    "datetime":     ("exif", TAG_DT_ORIGINAL, 2),
    "focal_length": ("exif", TAG_FOCAL, 5),
    "aperture":     ("exif", TAG_FNUM, 5),
    "iso":          ("exif", TAG_ISO, 3),
}


@dataclass
class Segment:
    marker: int
    start: int          # offset of the 0xFF
    end: int            # one past the last byte of the segment
    data_start: int     # first payload byte

    @property
    def length(self) -> int:
        return self.end - self.start


# ----------------------------------------------------------------------
#  JPEG structure
# ----------------------------------------------------------------------
def scan_segments(raw: bytes) -> list[Segment]:
    """Split a JPEG into segments, stopping after EOI.

    Anything past EOI (a Live Photo's video) is intentionally not a segment:
    callers treat it as opaque tail bytes.
    """
    segs: list[Segment] = []
    i, n = 2, len(raw)
    while i + 1 < n:
        if raw[i] != 0xFF:
            i += 1
            continue
        m = raw[i + 1]
        if m == 0x00:                       # stuffed byte inside entropy data
            i += 2
            continue
        if m == 0xD8:                       # SOI
            i += 2
            continue
        if m == 0xD9:                       # EOI
            segs.append(Segment(0xD9, i, i + 2, i + 2))
            return segs
        if m == 0xDA:                       # SOS: header then entropy data
            ln = int.from_bytes(raw[i + 2:i + 4], "big")
            segs.append(Segment(0xDA, i, i + 2 + ln, i + 4))
            eoi = raw.find(b"\xff\xd9", i + 2 + ln)
            if eoi >= 0:
                segs.append(Segment(0xD9, eoi, eoi + 2, eoi + 2))
            return segs
        if m == 0x01 or 0xD0 <= m <= 0xD7:  # standalone markers
            segs.append(Segment(m, i, i + 2, i + 2))
            i += 2
            continue
        ln = int.from_bytes(raw[i + 2:i + 4], "big")
        segs.append(Segment(m, i, i + 2 + ln, i + 4))
        i += 2 + ln
    return segs


def find_exif_segment(raw: bytes, segs: list[Segment]) -> Segment | None:
    for s in segs:
        if s.marker == 0xE1 and raw[s.data_start:s.data_start + 6] == b"Exif\x00\x00":
            return s
    return None


def find_mpf_segment(raw: bytes, segs: list[Segment]) -> Segment | None:
    for s in segs:
        if s.marker == 0xE2 and raw[s.data_start:s.data_start + 4] == b"MPF\x00":
            return s
    return None


# ----------------------------------------------------------------------
#  TIFF
# ----------------------------------------------------------------------
class Tiff:
    """Minimal mutable TIFF view over the EXIF payload."""

    def __init__(self, buf: bytes):
        self.buf = bytearray(buf)
        self.bo = "little" if buf[:2] == b"II" else "big"
        if self.u16(2) != 42:
            raise ValueError("not a TIFF payload")
        self.ifd0 = self.u32(4)
        self._ifds: dict[str, int] = {"ifd0": self.ifd0}
        self._discover_sub_ifds()

    # -- primitives
    def u16(self, o: int) -> int:
        return int.from_bytes(self.buf[o:o + 2], self.bo)

    def u32(self, o: int) -> int:
        return int.from_bytes(self.buf[o:o + 4], self.bo)

    def put16(self, o: int, v: int) -> None:
        self.buf[o:o + 2] = v.to_bytes(2, self.bo)

    def put32(self, o: int, v: int) -> None:
        self.buf[o:o + 4] = v.to_bytes(4, self.bo)

    # -- structure
    def _discover_sub_ifds(self) -> None:
        for e, tag, _t, _c in self.entries(self.ifd0):
            if tag == TAG_EXIF_IFD:
                self._ifds["exif"] = self.u32(e + 8)
            elif tag == TAG_GPS_IFD:
                self._ifds["gps"] = self.u32(e + 8)

    def entries(self, ifd_off: int) -> list[tuple[int, int, int, int]]:
        """(entry_offset, tag, type, count) for one IFD."""
        out = []
        n = self.u16(ifd_off)
        for k in range(n):
            e = ifd_off + 2 + k * 12
            out.append((e, self.u16(e), self.u16(e + 2), self.u32(e + 4)))
        return out

    def find(self, ifd: str, tag: int) -> int | None:
        off = self._ifds.get(ifd)
        if off is None:
            return None
        for e, t, _typ, _cnt in self.entries(off):
            if t == tag:
                return e
        return None

    def has_ifd(self, ifd: str) -> bool:
        return ifd in self._ifds

    # -- value access
    def value_range(self, entry: int) -> tuple[int, int]:
        """(offset, byte_size) of an entry's data, inline or pointed-to."""
        typ, cnt = self.u16(entry + 2), self.u32(entry + 4)
        size = TYPE_SIZE.get(typ, 1) * cnt
        if size <= 4:
            return entry + 8, size
        return self.u32(entry + 8), size

    def write_in_place(self, entry: int, typ: int, data: bytes) -> bool:
        """Overwrite an entry's data without moving anything.

        Returns False when the new data does not fit the old slot.
        """
        old_typ, old_cnt = self.u16(entry + 2), self.u32(entry + 4)
        old_size = TYPE_SIZE.get(old_typ, 1) * old_cnt
        new_cnt = len(data) // TYPE_SIZE[typ]

        if old_size <= 4:                       # value lives inside the entry
            if len(data) > 4:
                return False
            self.buf[entry + 8:entry + 12] = data.ljust(4, b"\x00")
        else:
            if len(data) > old_size:
                return False
            off = self.u32(entry + 8)
            self.buf[off:off + len(data)] = data
            self.buf[off + len(data):off + old_size] = b"\x00" * (old_size - len(data))
        self.put16(entry + 2, typ)
        self.put32(entry + 4, new_cnt)
        return True

    def append_data(self, entry: int, typ: int, data: bytes) -> None:
        """Store data at the end of the TIFF block and repoint the entry."""
        while len(self.buf) % 4:
            self.buf.append(0)
        off = len(self.buf)
        self.buf.extend(data)
        self.put16(entry + 2, typ)
        self.put32(entry + 4, len(data) // TYPE_SIZE[typ])
        self.put32(entry + 8, off)


# ----------------------------------------------------------------------
#  Value encoding
# ----------------------------------------------------------------------
def _ascii(value: str) -> bytes:
    return str(value).encode("utf-8") + b"\x00"


def _rational_pairs(pairs: list[tuple[int, int]], bo: str) -> bytes:
    fmt = "<" if bo == "little" else ">"
    return b"".join(struct.pack(fmt + "II", n, d) for n, d in pairs)


def _dms(value: float) -> list[tuple[int, int]]:
    v = abs(value)
    d = int(v)
    m = int((v - d) * 60)
    s = (v - d - m / 60) * 3600
    from fractions import Fraction
    f = Fraction(s).limit_denominator(1_000_000)
    return [(d, 1), (m, 1), (f.numerator, f.denominator)]


# ----------------------------------------------------------------------
#  Public entry point
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
#  Format detection
# ----------------------------------------------------------------------
class UnsupportedFormat(ValueError):
    """Not a JPEG — the byte patcher only handles JPEG."""


def detect_kind(raw: bytes) -> str:
    """Classify by the actual bytes, never by the file extension.

    The distinction that matters: both a plain JPG and a Live Photo use the
    ``.jpg`` extension, but a Live Photo carries a video (and usually an XMP
    declaration and/or an MPF second image) that must survive the edit.
    """
    if raw.startswith(b"\xff\xd8"):
        return "motion-jpeg" if looks_like_motion(raw) else "jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    return "unknown"


def looks_like_motion(raw: bytes) -> bool:
    """True when the JPEG declares something beyond the still image.

    Three independent signals, any one is enough:
      * bytes appended after EOI that start like an MP4 box chain
      * an XMP motion declaration (Container or legacy MicroVideo)
      * an MPF segment (a second embedded image, e.g. an HDR gain map)
    """
    segs = scan_segments(raw)
    eoi = next((s.start for s in segs if s.marker == 0xD9), None)
    if eoi is not None and len(raw) > eoi + 2:
        tail = raw[eoi + 2:eoi + 12]
        if b"ftyp" in tail or b"moov" in tail or b"mdat" in tail:
            return True
    if b'MotionPhoto="1"' in raw or b"MicroVideo=" in raw:
        return True
    return find_mpf_segment(raw, segs) is not None


def describe(raw: bytes) -> str:
    """Short human label for logs."""
    return {
        "jpeg": "静态 JPG",
        "motion-jpeg": "Live Photo（实况照片）",
        "png": "PNG",
        "tiff": "TIFF",
    }.get(detect_kind(raw), "未知格式")


def patch_exif_bytes(raw: bytes, fields: dict) -> bytes:
    """Return *raw* with only the EXIF tags named in *fields* changed.

    Bytes outside the EXIF segment are copied verbatim, including the XMP
    segment, any MPF second image and the video appended after EOI.

    Raises:
        ValueError: when the file has no EXIF segment or a value is invalid.
    """
    if not raw.startswith(b"\xff\xd8"):
        raise UnsupportedFormat(f"{describe(raw)} 不是 JPEG，不能用字节补丁")
    segs = scan_segments(raw)
    if not any(s.marker == 0xD9 for s in segs):
        raise ValueError("JPEG 文件不完整（找不到 EOI）")
    exif = find_exif_segment(raw, segs)
    if exif is None:
        return _create_exif_segment(raw, segs, fields)

    tiff = Tiff(raw[exif.data_start + 6:exif.end])
    structural = False

    for name, value in fields.items():
        if name in ("clear_gps",):
            structural = True
            continue
        if value is None:
            structural = True           # deleting a tag needs a rebuild
            continue
        if name in SIMPLE_SPECS:
            if not _apply_simple(tiff, name, value):
                structural = True
        elif name in ("latitude", "longitude", "altitude"):
            if not _apply_gps(tiff, name, value):
                structural = True

    if structural:
        return _rebuild_and_splice(raw, segs, exif, fields)

    new_tiff = bytes(tiff.buf)
    delta = len(new_tiff) - (exif.end - exif.data_start - 6)
    new_app1 = _app1(new_tiff)
    out = bytearray(raw[:exif.start] + new_app1 + raw[exif.end:])
    if delta:
        _adjust_mpf_primary_size(out, delta)
    return bytes(out)


def _apply_simple(tiff: Tiff, name: str, value) -> bool:
    ifd, tag, typ = SIMPLE_SPECS[name]
    entry = tiff.find(ifd, tag)
    if entry is None:
        return False                    # tag absent -> structural rebuild
    if typ == 2:
        data = _ascii(value)
    elif typ == 5:
        from fractions import Fraction
        f = Fraction(float(value)).limit_denominator(1_000_000)
        data = _rational_pairs([(f.numerator, f.denominator)], tiff.bo)
    else:                               # SHORT
        data = int(value).to_bytes(2, tiff.bo)
    if tiff.write_in_place(entry, typ, data):
        return True
    if typ == 2:                        # longer string: append and repoint
        tiff.append_data(entry, typ, data)
        return True
    return False


def _apply_gps(tiff: Tiff, name: str, value) -> bool:
    if not tiff.has_ifd("gps"):
        return False
    if name == "altitude":
        entry = tiff.find("gps", GPS_ALT)
        ref = tiff.find("gps", GPS_ALT_REF)
        if entry is None or ref is None:
            return False
        alt = float(value)
        from fractions import Fraction
        f = Fraction(abs(alt)).limit_denominator(1_000_000)
        ok = tiff.write_in_place(entry, 5, _rational_pairs([(f.numerator, f.denominator)], tiff.bo))
        if ok:
            tiff.write_in_place(ref, 1, bytes([0 if alt >= 0 else 1]))
        return ok

    tag, ref_tag, refs = (GPS_LAT, GPS_LAT_REF, ("N", "S")) if name == "latitude" \
        else (GPS_LON, GPS_LON_REF, ("E", "W"))
    entry, ref = tiff.find("gps", tag), tiff.find("gps", ref_tag)
    if entry is None or ref is None:
        return False
    v = float(value)
    data = _rational_pairs(_dms(v), tiff.bo)
    ok = tiff.write_in_place(entry, 5, data)
    if ok:
        tiff.write_in_place(ref, 2, _ascii(refs[0] if v >= 0 else refs[1])[:2])
    return ok


def _app1(tiff_bytes: bytes) -> bytes:
    """Build the APP1 payload.

    ``piexif.dump()`` already prepends ``b"Exif\\0\\0"``; the in-place path
    hands over a bare TIFF block. Normalise both here so callers cannot get
    a doubled prefix (which produced an unparsable segment).
    """
    if tiff_bytes.startswith(b"Exif\x00\x00"):
        tiff_bytes = tiff_bytes[6:]
    payload = b"Exif\x00\x00" + tiff_bytes
    return b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload


def _create_exif_segment(raw: bytes, segs: list[Segment], fields: dict) -> bytes:
    """Insert a fresh EXIF segment when the file has none.

    Placed after SOI and any APP0 (JFIF) segment, which is where readers
    expect it. Everything else in the file is still copied verbatim.
    """
    import piexif

    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {},
                 "1st": {}, "thumbnail": None}
    _apply_to_dict(exif_dict, fields)
    rebuilt = piexif.dump(exif_dict)
    if rebuilt.startswith(b"Exif\x00\x00"):
        rebuilt = rebuilt[6:]
    app1 = _app1(rebuilt)

    pos = 2                                    # right after SOI
    for s in segs:
        if s.marker == 0xE0:                   # keep JFIF first
            pos = s.end
        else:
            break
    out = bytearray(raw[:pos] + app1 + raw[pos:])
    _adjust_mpf_primary_size(out, len(app1))
    return bytes(out)


def _rebuild_and_splice(raw: bytes, segs, exif: Segment, fields: dict) -> bytes:
    """Fallback: rebuild the EXIF segment with piexif, splice it in place.

    Every other byte of the file — XMP, MPF, ICC, pixels, video — is copied
    untouched. This is the path for adding a GPS block, deleting tags, etc.
    """
    import piexif

    # piexif.load() accepts JPEG/TIFF bytes or a path; the APP1 payload
    # (starting with b"Exif\0\0") is the form it recognises directly.
    original = raw[exif.data_start:exif.end]
    exif_dict = piexif.load(original)
    _apply_to_dict(exif_dict, fields)
    rebuilt = piexif.dump(exif_dict)
    if rebuilt.startswith(b"Exif\x00\x00"):
        rebuilt = rebuilt[6:]
    delta = len(rebuilt) - (exif.end - exif.data_start - 6)
    out = bytearray(raw[:exif.start] + _app1(rebuilt) + raw[exif.end:])
    if delta:
        _adjust_mpf_primary_size(out, delta)
    return bytes(out)


def _apply_to_dict(exif_dict: dict, fields: dict) -> None:
    """Mirror of the piexif path in the app, minus any image handling."""
    import piexif

    if fields.get("clear_gps"):
        exif_dict["GPS"] = {}
    for name, (_ifd, tag, kind) in SIMPLE_SPECS.items():
        if name not in fields:
            continue
        value = fields[name]
        if value is None:
            exif_dict["0th"].pop(tag, None)
            exif_dict["Exif"].pop(tag, None)
            continue
        if kind == 2:
            exif_dict["0th" if _ifd == "ifd0" else "Exif"][tag] = str(value).encode("utf-8")
        elif kind == 5:
            from fractions import Fraction
            f = Fraction(float(value)).limit_denominator(1_000_000)
            exif_dict["Exif"][tag] = (f.numerator, f.denominator)
        else:
            exif_dict["Exif"][tag] = int(value)

    gps = exif_dict.setdefault("GPS", {})
    for name, tag, ref_tag, pos, neg in (
            ("latitude", GPS_LAT, GPS_LAT_REF, b"N", b"S"),
            ("longitude", GPS_LON, GPS_LON_REF, b"E", b"W")):
        if name not in fields:
            continue
        value = fields[name]
        if value is None:
            gps.pop(tag, None)
            gps.pop(ref_tag, None)
        else:
            v = float(value)
            gps[tag] = [(n, d) for n, d in _dms(v)]
            gps[ref_tag] = pos if v >= 0 else neg
    if "altitude" in fields:
        value = fields["altitude"]
        if value is None:
            gps.pop(GPS_ALT, None)
            gps.pop(GPS_ALT_REF, None)
        else:
            alt = float(value)
            from fractions import Fraction
            f = Fraction(abs(alt)).limit_denominator(1_000_000)
            gps[GPS_ALT] = (f.numerator, f.denominator)
            gps[GPS_ALT_REF] = 0 if alt >= 0 else 1


def _adjust_mpf_primary_size(buf: bytearray, delta: int) -> None:
    """MP entry[0].size is the primary image length; shift it by *delta*.

    It is the only offset-like value that does not follow automatically when
    the EXIF segment changes size (the MPF offsets are relative to the MPF
    header, which moves together with its targets).
    """
    segs = scan_segments(bytes(buf))
    mpf = find_mpf_segment(bytes(buf), segs)
    if mpf is None:
        return
    tiff_start = mpf.data_start + 4
    bo = "little" if buf[tiff_start:tiff_start + 2] == b"II" else "big"
    u16 = lambda o: int.from_bytes(buf[o:o + 2], bo)
    u32 = lambda o: int.from_bytes(buf[o:o + 4], bo)
    ifd = u32(tiff_start + 4)
    n = u16(tiff_start + ifd)
    for k in range(n):
        e = tiff_start + ifd + 2 + k * 12
        if u16(e) == 0xB002 and u32(e + 4) >= 16:
            base = tiff_start + u32(e + 8)
            size = u32(base + 4)
            buf[base + 4:base + 8] = (size + delta).to_bytes(4, bo)
            return


# ----------------------------------------------------------------------
#  Post-write self check
# ----------------------------------------------------------------------
def video_length_from_xmp(raw: bytes) -> int | None:
    """Motion video byte count declared in XMP, or None.

    Both container styles list the primary image first with
    ``Item:Length="0"``; the motion item is the one with a real length.
    """
    import re
    i = raw.find(b"ns.adobe.com/xap")
    if i < 0:
        return None
    chunk = raw[i:i + 8192].decode("utf-8", "replace")
    for m in re.finditer(r'Item:Length="(\d+)"', chunk):
        value = int(m.group(1))
        if value:
            return value
    m = re.search(r'MicroVideoOffset="(\d+)"', chunk)
    return int(m.group(1)) if m else None


def verify_patch(old: bytes, new: bytes) -> list[str]:
    """Return a list of problems; empty means the rewrite looks safe."""
    problems: list[str] = []

    old_segs, new_segs = scan_segments(old), scan_segments(new)
    old_eoi = next((s.start for s in old_segs if s.marker == 0xD9), len(old))
    new_eoi = next((s.start for s in new_segs if s.marker == 0xD9), len(new))

    old_tail, new_tail = old[old_eoi:], new[new_eoi:]
    if old_tail != new_tail:
        problems.append("bytes after EOI changed (video would be damaged)")

    # the declared video length must still land on ftyp
    length = video_length_from_xmp(new)
    if length:
        vstart = len(new) - length
        if not new[vstart + 4:vstart + 8] == b"ftyp":
            problems.append(f"declared video length does not land on ftyp ({vstart})")

    # MPF second image must still point at a JPEG
    mpf = find_mpf_segment(new, new_segs)
    if mpf is not None:
        tiff_start = mpf.data_start + 4
        bo = "little" if new[tiff_start:tiff_start + 2] == b"II" else "big"
        u16 = lambda o: int.from_bytes(new[o:o + 2], bo)
        u32 = lambda o: int.from_bytes(new[o:o + 4], bo)
        ifd = u32(tiff_start + 4)
        for k in range(u16(tiff_start + ifd)):
            e = tiff_start + ifd + 2 + k * 12
            if u16(e) == 0xB002:
                base = tiff_start + u32(e + 8)
                off = int.from_bytes(new[base + 24:base + 28], bo)
                if off and new[tiff_start + off:tiff_start + off + 2] != b"\xff\xd8":
                    problems.append("MPF entry[1] no longer points at a JPEG")

    return problems
