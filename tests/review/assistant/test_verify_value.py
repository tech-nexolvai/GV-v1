"""`verify_value` (#1531): code reads the number printed at a finding's spot, $0, no AI.

Every PDF here is a small synthetic one built in the test: text inside a pasted `/Stamp` drawing (as
on GV's combined sheets), a millimetre value over its inches, a stacked fraction, a symbol font,
digits drawn as outlined vector glyphs (with GV's yellow box painted over them), sideways text. The
OCR engines in most tests are a tiny digit reader made here (`_GlyphReader`): it really reads the
picture the route draws, by matching each blob of ink against the digits of the same font, so the
tests show what the route hands an engine without installing one. The rules on top (two reads,
no near-miss, big boxes refused) are tested with scripted readers. One test runs the real engines
when they are installed.
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
import reportlab
from PIL import Image, ImageDraw, ImageFont

from app.review.assistant import verify_value as vv
from app.review.assistant.finding_checks import check_finding, drawn_checks, printed_at_spot
from tests.review.assistant.stroke_text import StrokeReader, stroke_ops

PAGE_W, PAGE_H = 600, 400
#: The pasted drawing's place on the page (PDF space): its form's (0, 0) is the page's (300, 50).
STAMP_X, STAMP_Y = 300, 50
_FONT = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"


# ---- a synthetic PDF -----------------------------------------------------------------------------


def _stream(body: bytes, extra: bytes = b"") -> bytes:
    data = zlib.compress(body)
    return (
        b"<< "
        + extra
        + b" /Filter /FlateDecode /Length "
        + str(len(data)).encode()
        + b" >>\nstream\n"
        + data
        + b"\nendstream"
    )


#: A font whose codes 0x20-0x7E read as U+F020-U+F07E: the symbol font that stores `4'-3"` as
#: private-use characters.
_SYMBOL_CMAP = (
    b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /Sym def "
    b"1 begincodespacerange <00> <FF> endcodespacerange 1 beginbfrange <20> <7E> <F020> "
    b"endbfrange endcmap CMapName currentdict /CMap defineresource pop end end"
)


def _pdf(page: bytes = b"", stamp: bytes | None = None) -> bytes:
    """One 600 x 400 page: its own content, and a pasted drawing (`/Stamp`) whose appearance is
    `stamp` (fonts /F1 Helvetica and /F2 the symbol font in both)."""
    fonts = b"<< /F1 4 0 R /F2 5 0 R >>"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 400] /Resources << /Font "
        + fonts
        + b" >> /Contents 6 0 R"
        + (b" /Annots [7 0 R]" if stamp is not None else b"")
        + b" >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 9 0 R >>",
        _stream(page),
        b"<< /Type /Annot /Subtype /Stamp /Rect [300 50 550 250] /AP << /N 8 0 R >> >>",
        _stream(
            stamp or b"",
            b"/Type /XObject /Subtype /Form /FormType 1 /BBox [0 0 250 200] /Resources << /Font "
            + fonts
            + b" >>",
        ),
        _stream(_SYMBOL_CMAP),
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(start).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def _text(
    x: float, y: float, text: str, size: float = 10, font: bytes = b"/F1", turn: bool = False
) -> bytes:
    matrix = f"0 1 -1 0 {x} {y}" if turn else f"1 0 0 1 {x} {y}"
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT {font.decode()} {size} Tf {matrix} Tm ({escaped}) Tj ET ".encode("latin-1")


def _outlines(digits: str, x: float, y: float, height: float = 7.0, turn: bool = False) -> bytes:
    """`digits` drawn as filled outlined glyphs (no text), baseline-left at (x, y) in PDF space:
    each digit traced from the Vera font, one path per digit, as a drawing exported with its
    lettering turned to lines. `turn`: reading up the page."""
    font = ImageFont.truetype(str(_FONT), 200)
    ops: list[str] = []
    advance = 0.0
    scale = height / 150.0
    for digit in digits:
        picture = Image.new("L", (220, 260), 0)
        ImageDraw.Draw(picture).text((10, 10), digit, fill=255, font=font)
        ink = (np.array(picture) > 127).astype(np.uint8)
        ys, xs = np.nonzero(ink)
        bottom, left, right = ys.max(), xs.min(), xs.max()
        contours, _ = cv2.findContours(ink, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        path: list[str] = []
        for contour in contours:
            points = contour.reshape(-1, 2)
            for n, (px, py) in enumerate(points):
                along = advance + (px - left) * scale
                up = (bottom - py) * scale
                gx, gy = (x - up, y + along) if turn else (x + along, y + up)
                path.append(f"{gx:.3f} {gy:.3f} {'m' if n == 0 else 'l'}")
            path.append("h")
        ops.append("0 0 0 rg " + " ".join(path) + " f*")
        advance += (right - left) * scale + height * 0.18
    return (" ".join(ops) + " ").encode()


def _write(tmp_path: Path, data: bytes, name: str = "set.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _box(x0: float, y0: float, x1: float, y1: float) -> dict[str, float]:
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


def _shown(x: float, y: float) -> tuple[float, float]:
    """A stamp-form point as a shown page point (origin top-left)."""
    return STAMP_X + x, PAGE_H - (STAMP_Y + y)


def _around(x: float, y: float, width: float, height: float = 12) -> dict[str, float]:
    """A box round text set at stamp-form point (x, y) (its baseline-left)."""
    left, base = _shown(x, y)
    return _box(left - 2, base - height, left + width, base + 3)


# ---- a tiny digit reader, for the drawn route ----------------------------------------------------


class _GlyphReader:
    """Reads digits of the Vera font in a picture by matching each blob of ink with them. It sees
    only the picture the route hands it, so what it reads is what the route drew."""

    def __init__(self, name: str) -> None:
        self.name = name
        font = ImageFont.truetype(str(_FONT), 120)
        self._templates: list[tuple[str, np.ndarray]] = []
        for digit in "0123456789":
            picture = Image.new("L", (160, 180), 0)
            ImageDraw.Draw(picture).text((10, 10), digit, fill=255, font=font)
            self._templates.append((digit, self._norm(np.array(picture) > 127)))

    @staticmethod
    def _norm(ink: np.ndarray) -> np.ndarray:
        ys, xs = np.nonzero(ink)
        cut = ink[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1].astype(np.uint8) * 255
        return cv2.resize(cut, (24, 32), interpolation=cv2.INTER_AREA).astype(np.float32) / 255

    def __call__(self, picture: np.ndarray, *, single_line: bool) -> list[str]:
        ink = (picture < 128).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
        blobs = [i for i in range(1, count) if stats[i, cv2.CC_STAT_HEIGHT] >= 8]
        if not blobs:
            return []
        tallest = max(stats[i, cv2.CC_STAT_HEIGHT] for i in blobs)
        read = ""
        for i in sorted(blobs, key=lambda i: stats[i, cv2.CC_STAT_LEFT]):
            if stats[i, cv2.CC_STAT_HEIGHT] < 0.6 * tallest:
                continue
            shape = self._norm(labels == i)
            score, digit = min((float(np.abs(shape - t).mean()), d) for d, t in self._templates)
            if score > 0.12:
                return []  # not a digit of this font upright: read nothing rather than guess
            read += digit
        return [read] if read else []


GLYPHS: Sequence[vv.InkReader] = (_GlyphReader("Tesseract"), _GlyphReader("RapidOCR"))


class _Scripted:
    """An engine that reads the same lines in any picture."""

    def __init__(self, name: str, lines: list[str]) -> None:
        self.name, self.lines = name, lines

    def __call__(self, picture: np.ndarray, *, single_line: bool) -> list[str]:
        return list(self.lines)


# ---- route 1: the drawing's text -----------------------------------------------------------------


def test_text_inside_a_stamp_matches_in_any_written_form(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=_text(20, 100, "1168 [46]")))
    box = _around(20, 100, 60)
    for value in ('46"', "3'-10\"", "1168 [46]", "46"):
        found = vv.verify_value(pdf, 1, box, value, drawn=False)
        assert (found.result, found.route) == ("match", "text"), value
    found = vv.verify_value(pdf, 1, box, '46"')
    assert found.words() == "Read from the drawing's text: 1168 [46]"
    assert printed_at_spot(pdf, 1, box, '46"') == "match"


def test_another_value_at_the_spot_is_a_mismatch_with_what_is_printed(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=_text(20, 100, "1168 [46]")))
    found = vv.verify_value(pdf, 1, _around(20, 100, 60), '47"', readers=GLYPHS)
    assert (found.result, found.read) == ("mismatch", "1168 [46]")
    assert found.words() == 'The drawing prints 1168 [46] here, not 47"'
    assert printed_at_spot(pdf, 1, _around(20, 100, 60), '47"') == "not_found"


def test_millimetres_over_inches_on_two_lines_read_as_the_inches(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=_text(20, 100, "610") + _text(20, 88, "[24]")))
    box = _box(*_shown(18, 113)[:1], _shown(18, 113)[1], *_shown(50, 85))
    assert vv.verify_value(pdf, 1, box, '24"', drawn=False).result == "match"
    assert vv.verify_value(pdf, 1, box, "610 [24]", drawn=False).result == "match"


def test_a_stacked_fraction_in_text_is_composed(tmp_path: Path) -> None:
    stamp = (
        _text(20, 140, "71", 10)
        + _text(33, 145, "1", 5)
        + _text(33, 137, "2", 5)
        + _text(37, 140, '"', 10)
    )
    pdf = _write(tmp_path, _pdf(stamp=stamp))
    box = _around(20, 140, 30)
    assert vv.verify_value(pdf, 1, box, '71 1/2"', drawn=False).result == "match"
    assert vv.verify_value(pdf, 1, box, '71-1/2"', drawn=False).result == "match"
    for wrong in ('71"', '1"', '2"', '71 1/4"'):
        assert vv.verify_value(pdf, 1, box, wrong, drawn=False).result != "match", wrong


def test_a_digit_of_a_stacked_fraction_is_never_a_number_of_its_own(tmp_path: Path) -> None:
    # A stacked `3` over `4`, as beside a whole number: the lone `3` is not `3"` (the one false
    # confirmation today's check made).
    stamp = _text(20, 60, "3", 6) + _text(20, 52, "4", 6)
    pdf = _write(tmp_path, _pdf(stamp=stamp))
    box = _around(20, 60, 12, height=20)
    assert vv.verify_value(pdf, 1, box, '3"', drawn=False).result != "match"
    assert vv.verify_value(pdf, 1, box, '4"', drawn=False).result != "match"
    assert vv.verify_value(pdf, 1, box, '3/4"', drawn=False).result == "match"


def test_a_value_ending_a_sentence_is_read(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=_text(20, 100, 'Filler will be 3".')))
    assert vv.verify_value(pdf, 1, _around(20, 100, 90), '3"', drawn=False).result == "match"


def test_a_symbol_font_that_stores_feet_and_inches_as_odd_characters_is_decoded(
    tmp_path: Path,
) -> None:
    pdf = _write(tmp_path, _pdf(stamp=_text(20, 100, "4'-3\"", font=b"/F2")))
    box = _around(20, 100, 40)
    assert vv.verify_value(pdf, 1, box, "4'-3\"", drawn=False).result == "match"
    assert vv.verify_value(pdf, 1, box, '51"', drawn=False).result == "match"
    assert vv.verify_value(pdf, 1, box, '3"', drawn=False).result == "mismatch"


def test_sideways_text_is_read(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=_text(100, 20, '24 3/4"', turn=True)))
    left, bottom = _shown(100, 20)
    box = _box(left - 12, bottom - 50, left + 3, bottom + 2)
    assert vv.verify_value(pdf, 1, box, '24 3/4"', drawn=False).result == "match"
    assert vv.verify_value(pdf, 1, box, '24"', drawn=False).result == "mismatch"


def test_gvs_coloured_text_is_never_read_as_printed(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=b"1 0 0 rg " + _text(20, 100, '38"')))
    found = vv.verify_value(pdf, 1, _around(20, 100, 30), '38"', readers=GLYPHS)
    assert found.result == "cannot_read"
    assert printed_at_spot(pdf, 1, _around(20, 100, 30), '38"') == "no_text"


def test_the_pages_own_text_counts(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(page=_text(50, 350, "3'-1\"", 12)))
    assert vv.verify_value(pdf, 1, _box(40, 30, 110, 60), '37"', drawn=False).result == "match"


@pytest.mark.parametrize(
    ("page", "box", "value", "reason"),
    [
        (1, _box(40, 30, 110, 60), "missing", "not one number"),
        (2, _box(40, 30, 110, 60), '37"', "the page cannot be read"),
        (1, {"x0": 40, "y0": 30}, '37"', "the box cannot be placed"),
        (0, _box(40, 30, 110, 60), '37"', "the box cannot be placed"),
    ],
)
def test_what_cannot_be_checked_says_why(
    tmp_path: Path, page: int, box: dict[str, Any], value: str, reason: str
) -> None:
    pdf = _write(tmp_path, _pdf(page=_text(50, 350, "3'-1\"", 12)))
    found = vv.verify_value(pdf, page, box, value)
    assert (found.result, found.reason) == ("cannot_read", reason)


def test_a_sentence_is_checked_by_its_claimed_value(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=_text(20, 100, '41 1/2"')))
    sentence = '41 1/2" printed overall; parts 32" + 9 1/2"'
    found = vv.verify_value(pdf, 1, _around(20, 100, 40), sentence, drawn=False)
    assert (found.result, found.value) == ("match", '41 1/2"')


# ---- route 2: the vendor's drawn ink -------------------------------------------------------------


def test_drawn_digits_under_gvs_yellow_box_are_read_from_the_vendors_ink(tmp_path: Path) -> None:
    yellow = b"1 1 0 rg 15 95 40 16 re f "
    pdf = _write(tmp_path, _pdf(stamp=_outlines("46", 20, 100) + yellow))
    box = _around(20, 100, 30)
    # The text route finds nothing and leaves the drawn ink for later.
    pending = vv.verify_value(pdf, 1, box, '46"', drawn=False)
    assert (pending.result, pending.pending) == ("cannot_read", True)
    assert printed_at_spot(pdf, 1, box, '46"') == "no_text"
    found = vv.verify_value(pdf, 1, box, '46"', readers=GLYPHS)
    assert (found.result, found.route) == ("match", "drawn")
    assert found.words() == "Read from the vendor's ink by two OCR engines: 46\""
    # The yellow box hides the digits from a picture of the page: only the vector ink shows them.
    assert all(read.startswith("vendor ink") for read in found.reads)
    assert vv.verify_value(pdf, 1, box, '45"', readers=GLYPHS).result == "cannot_read"


def test_drawn_digits_reading_up_the_page_are_read_turned(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=_outlines("72", 100, 20, turn=True)))
    left, bottom = _shown(100, 20)
    box = _box(left - 10, bottom - 22, left + 3, bottom + 2)
    found = vv.verify_value(pdf, 1, box, '72"', readers=GLYPHS)
    assert (found.result, found.route) == ("match", "drawn")


def test_one_read_or_one_engine_alone_never_matches(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=_outlines("46", 20, 100)))
    box = _around(20, 100, 30)
    readers = [_Scripted("Tesseract", []), _Scripted("RapidOCR", [])]
    assert vv.verify_value(pdf, 1, box, '46"', readers=readers).result == "cannot_read"
    # One engine on the vector ink and on the picture: two reads, but not two engines.
    one = [_GlyphReader("Tesseract")]
    found = vv.verify_value(pdf, 1, box, '46"', readers=one)
    assert (found.result, found.reason) == ("cannot_read", "only one OCR engine reads it here")
    lone = [_Scripted("Tesseract", ["46"]), _Scripted("RapidOCR", [])]
    assert vv.verify_value(pdf, 1, box, '46"', readers=lone).result == "cannot_read"
    both = [_Scripted("Tesseract", ["46"]), _Scripted("RapidOCR", ["46"])]
    assert vv.verify_value(pdf, 1, box, '46"', readers=both).result == "match"


class _Shaped(_Scripted):
    """A scripted engine that also says how wide each glyph it read as `1` is (as Tesseract)."""

    def __init__(self, name: str, lines: list[str], ones: list[float]) -> None:
        super().__init__(name, lines)
        self.ones = ones

    def one_shapes(self, picture: np.ndarray, *, single_line: bool) -> list[float]:
        return list(self.ones)


def test_a_drawn_1_must_look_like_a_1(tmp_path: Path) -> None:
    # Both engines read a hand-lettered `7 5/8"` as `1 5/8"`: a glyph read as 1 that is as wide as
    # a 7 refuses the match, and a value with a 1 nobody could show the shape of is not sure.
    pdf = _write(tmp_path, _pdf(stamp=_outlines("15", 20, 100)))
    box = _around(20, 100, 30)
    wide = [_Shaped("Tesseract", ['1 5/8"'], [0.5]), _Scripted("RapidOCR", ['1 5/8"'])]
    found = vv.verify_value(pdf, 1, box, '1 5/8"', readers=wide)
    assert found.result == "cannot_read" and "hand-lettered 7" in found.reason
    narrow = [_Shaped("Tesseract", ['1 5/8"'], [0.3]), _Scripted("RapidOCR", ['1 5/8"'])]
    assert vv.verify_value(pdf, 1, box, '1 5/8"', readers=narrow).result == "match"
    unshown = [_Scripted("Tesseract", ['1 5/8"']), _Scripted("RapidOCR", ['1 5/8"'])]
    found = vv.verify_value(pdf, 1, box, '1 5/8"', readers=unshown)
    assert found.result == "cannot_read" and "no engine could show its shape" in found.reason


def test_a_near_miss_read_refuses_the_match(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=_outlines("46", 20, 100)))
    box = _around(20, 100, 30)
    readers = [_Scripted("Tesseract", ["46"]), _Scripted("RapidOCR", ["45"])]
    found = vv.verify_value(pdf, 1, box, '46"', readers=readers)
    assert found.result == "cannot_read"
    assert "near-miss" in found.reason
    # The expected value as a fragment of a longer number read there is a near-miss too.
    longer = [_Scripted("Tesseract", ["5"]), _Scripted("RapidOCR", ["56"])]
    assert vv.verify_value(pdf, 1, box, '5"', readers=longer).result == "cannot_read"


def test_near_miss() -> None:
    from decimal import Decimal

    assert vv.near_miss(Decimal(46), Decimal(45))
    assert vv.near_miss(Decimal("29.25"), Decimal("29.75"))
    assert vv.near_miss(Decimal(5), Decimal(56))
    assert not vv.near_miss(Decimal(46), Decimal(46))
    assert not vv.near_miss(Decimal(46), Decimal(72))
    assert not vv.near_miss(Decimal("39.5"), Decimal(39))


def test_a_big_box_holding_several_numbers_is_refused(tmp_path: Path) -> None:
    stamp = _outlines("46", 20, 100) + _outlines("72", 120, 30)
    pdf = _write(tmp_path, _pdf(stamp=stamp))
    big = _box(*_shown(10, 120), *_shown(160, 20))  # 150 x 100 pt
    readers = [_Scripted("Tesseract", ["46", "72"]), _Scripted("RapidOCR", ["46", "72"])]
    found = vv.verify_value(pdf, 1, big, '46"', readers=readers)
    assert found.result == "cannot_read"
    assert "too big" in found.reason
    small = [_Scripted("Tesseract", ["46"]), _Scripted("RapidOCR", ["46"])]
    assert vv.verify_value(pdf, 1, big, '46"', readers=small).result == "match"


def test_ocr_alone_never_says_mismatch(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=_outlines("46", 20, 100)))
    readers = [_Scripted("Tesseract", ["72"]), _Scripted("RapidOCR", ["72"])]
    assert (
        vv.verify_value(pdf, 1, _around(20, 100, 30), '46"', readers=readers).result
        == "cannot_read"
    )


def test_no_engine_installed_cannot_read(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=_outlines("46", 20, 100)))
    found = vv.verify_value(pdf, 1, _around(20, 100, 30), '46"', readers=[])
    assert (found.result, found.reason) == ("cannot_read", "no OCR engine is installed here")


def test_the_real_engines_read_drawn_digits(tmp_path: Path) -> None:
    readers = vv.default_readers()
    if not readers:
        pytest.skip("no OCR engine installed here")
    pdf = _write(tmp_path, _pdf(stamp=_outlines("46", 20, 100, height=9)))
    box = _around(20, 100, 36, height=14)
    found = vv.verify_value(pdf, 1, box, '46"', readers=readers)
    # Each installed engine reads the vendor's ink and the picture of the vendor's layer.
    for reader in readers:
        for source in ("vendor ink", "vendor layer picture"):
            line = next(r for r in found.reads if r.startswith(f"{source}, {reader.name}: "))
            assert "46" in line.split(": ", 1)[1].split(" | "), found.reads
    if len(readers) < 2:
        assert found.result == "cannot_read"  # one engine is never two
        return
    assert found.result == "match", found.reads
    assert vv.verify_value(pdf, 1, box, '47"', readers=readers).result != "match"


# ---- route 2b: CAD stroke lettering, label by label (#1561) ---------------------------------------

STROKES: Sequence[vv.InkReader] = (StrokeReader("Tesseract"), StrokeReader("RapidOCR"))


def test_a_stacked_fraction_drawn_as_strokes_is_read_label_by_label(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=stroke_ops('63{1/2}"', 20, 100)))
    box = _around(20, 100, 30)
    found = vv.verify_value(pdf, 1, box, '63 1/2"', readers=STROKES)
    assert (found.result, found.route) == ("match", "drawn")
    assert "label by label" in found.reason
    assert any(read.startswith("vendor ink label 1, ") for read in found.reads)
    for wrong in ('63 1/4"', '63"', '62 1/2"', '1/2"'):
        assert vv.verify_value(pdf, 1, box, wrong, readers=STROKES).result != "match", wrong


def test_a_number_written_up_the_page_in_strokes_is_read(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=stroke_ops('27{1/4}"', 100, 20, turn=True)))
    left, bottom = _shown(100, 20)
    box = _box(left - 9, bottom - 30, left + 3, bottom + 2)
    assert vv.verify_value(pdf, 1, box, '27 1/4"', readers=STROKES).result == "match"
    assert vv.verify_value(pdf, 1, box, '72 1/4"', readers=STROKES).result != "match"


def test_a_clear_label_beside_another_number_is_read_but_a_near_miss_beside_it_refuses(
    tmp_path: Path,
) -> None:
    # A box round two numbers: each label is read alone, so 34" is confirmed beside 72"...
    pdf = _write(tmp_path, _pdf(stamp=stroke_ops('34"', 20, 100) + stroke_ops('72"', 20, 80)))
    box = _box(*_shown(15, 110), *_shown(60, 75))
    assert vv.verify_value(pdf, 1, box, '34"', readers=STROKES).result == "match"
    # ...but never beside 35": the AI may have misread one of the two.
    pdf = _write(tmp_path, _pdf(stamp=stroke_ops('34"', 20, 100) + stroke_ops('35"', 20, 80)))
    found = vv.verify_value(pdf, 1, box, '34"', readers=STROKES)
    assert found.result == "cannot_read" and "near-miss" in found.reason


def test_a_big_box_with_two_numbers_is_read_label_by_label(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=stroke_ops('34"', 20, 100) + stroke_ops('72"', 120, 70)))
    big = _box(*_shown(10, 120), *_shown(160, 60))  # 150 x 60 pt
    found = vv.verify_value(pdf, 1, big, '34"', readers=STROKES)
    assert (found.result, found.route) == ("match", "drawn") and "label by label" in found.reason
    # Scripted whole-box engines that read both numbers on every picture vote for no label.
    both = [_Scripted("Tesseract", ["34", "72"]), _Scripted("RapidOCR", ["34", "72"])]
    assert "too big" in vv.verify_value(pdf, 1, big, '34"', readers=both).reason


class _LosesFractions(StrokeReader):
    """An engine that reads only the whole number of a box with a stacked fraction in it (it
    reads nothing it can use there, and says `63`): the label route reads `63 1/2"`."""

    def __call__(self, picture: np.ndarray, *, single_line: bool) -> list[str]:
        return super().__call__(picture, single_line=single_line) or ["63"]


def test_a_lost_fraction_never_confirms_the_whole_number(tmp_path: Path) -> None:
    pdf = _write(tmp_path, _pdf(stamp=stroke_ops('63{1/2}"', 20, 100)))
    box = _around(20, 100, 30)
    readers = [_LosesFractions("Tesseract"), _LosesFractions("RapidOCR")]
    # Every whole-box read says 63, which alone was a match; the label reads 63 1/2", the same
    # whole number with a fraction, so it is refused.
    found = vv.verify_value(pdf, 1, box, '63"', readers=readers)
    assert found.result == "cannot_read" and "another fraction" in found.reason
    assert vv.verify_value(pdf, 1, box, '63 1/2"', readers=readers).result == "match"


def test_text_printing_a_near_miss_refuses_any_drawn_match(tmp_path: Path) -> None:
    # The box prints 71 1/2" as text (stacked) beside some drawn ink; engines that lose the
    # stacked fraction read 71 on every picture. 71" must not be confirmed.
    stamp = (
        _text(20, 140, "71", 10)
        + _text(33, 145, "1", 5)
        + _text(33, 137, "2", 5)
        + _text(37, 140, '"', 10)
        + _outlines("8", 50, 136)
    )
    pdf = _write(tmp_path, _pdf(stamp=stamp))
    box = _around(20, 140, 40)
    readers = [_Scripted("Tesseract", ["71"]), _Scripted("RapidOCR", ["71"])]
    found = vv.verify_value(pdf, 1, box, '71"', readers=readers)
    assert found.result == "cannot_read" and "near-miss" in found.reason
    assert vv.verify_value(pdf, 1, box, '71 1/2"', readers=readers).result == "match"


def test_a_label_votes_only_when_its_whole_row_is_one_dimension() -> None:
    assert vv._row_value('63 1/2"', inch_mark=True) == Decimal("63.5")
    assert vv._row_value("63 1 1//'", inch_mark=True) is None  # a lost fraction: no 63"
    assert vv._row_value("73 2", inch_mark=True) is None
    assert vv._row_value("[34]", inch_mark=True) == Decimal(34)
    assert vv._row_value("46", inch_mark=False) == Decimal(46)
    assert vv._row_value("46 99", inch_mark=True) is None
    assert vv._row_value("3'", inch_mark=False) is None  # a foot mark is never dropped
    assert vv._row_value('57" (INCLUDING FIELD CUT)', inch_mark=True) == Decimal(57)
    assert vv._row_value('2"+3" FILLER', inch_mark=True) is None


def test_a_box_holding_a_whole_view_is_not_read_label_by_label(tmp_path: Path) -> None:
    labels = b"".join(
        stroke_ops(f'{n}"', 10 + 40 * (n % 6), 20 + 25 * (n // 6), height=5) for n in range(10, 46)
    )
    pdf = _write(tmp_path, _pdf(stamp=labels))
    box = _box(*_shown(5, 200), *_shown(250, 10))
    assert (
        len(
            vv.ink_labels.labels(
                vv._ink_paths_near(vv._read_page(pdf, 0), tuple(box.values())), tuple(box.values())
            )
        )
        > vv.ink_labels.MAX_LABELS
    )
    found = vv.verify_value(pdf, 1, box, '27"', readers=STROKES)
    assert found.result == "cannot_read"
    assert not any(read.startswith("vendor ink label") for read in found.reads)


# ---- a finding's sides ---------------------------------------------------------------------------


def test_check_finding_reads_text_now_and_drawn_ink_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stamp = _text(20, 160, "1168 [46]") + _outlines("46", 20, 100)
    pdf = _write(tmp_path, _pdf(stamp=stamp))
    finding = {
        "architect": '46" overall; parts 30" + 16"',
        "vendor": '46"',
        "arch_box": {**_around(20, 160, 60), "page": 1},
        "vendor_box": {**_around(20, 100, 30), "page": 1},
    }
    checks = check_finding(finding, {"set.pdf": pdf}).as_dict()
    assert checks["printed_architect"] == "match"
    assert checks["verify_architect"]["route"] == "text"
    assert checks["printed_vendor"] == "no_text"
    assert checks["verify_vendor"]["pending"] is True
    monkeypatch.setattr(vv, "default_readers", lambda: list(GLYPHS))
    later = drawn_checks({**finding, "checks": checks}, {"set.pdf": pdf})
    assert set(later) == {"verify_vendor"}
    assert (later["verify_vendor"]["result"], later["verify_vendor"]["route"]) == ("match", "drawn")
