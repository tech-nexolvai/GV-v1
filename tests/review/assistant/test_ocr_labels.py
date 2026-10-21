"""Numbers the label readers missed, read at their spot by two OCR engines (#1848).

Every PDF here is made in the test: made-up dimensions drawn as pen strokes (one stroked path per
stroke, a CAD "simplex" look) over made-up dimension lines, on a page whose left part is the
architect's drawing and right part the vendor's. Most tests use scripted OCR engines that read a
spot only the right way round (its heavy digits first, its light inch mark last), so they need no
engine installed; the last ones read the drawn strokes with the two local engines (RapidOCR and
Tesseract) and are skipped where either is missing. No model is ever called. The values are
invented.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from app.review.assistant import dim_labels as dl
from app.review.assistant import dim_pairs, ocr_labels, same_ends
from app.review.assistant import full_check_advocate as advocate
from app.review.assistant.dim_findings import label_checks
from app.review.assistant.dim_index import label_line
from app.review.assistant.label_origin import OCR_ORIGINS, ocr_read
from app.review.assistant.simple_check import verify as simple_verify
from tests.review.assistant.test_dim_labels import _dimension, _glyphs, _line, _pdf, _text

HALVES = [
    {"box": [0, 0, 250, 400], "role": "architect"},
    {"box": [250, 0, 600, 400], "role": "vendor"},
]

# ---- a pen-stroke font ---------------------------------------------------------------------------

#: Each character as strokes (polylines) in a 6 x 9 cell, y up: CAD's simplex lettering, roughly.
_STROKES: dict[str, list[list[tuple[float, float]]]] = {
    "0": [[(2, 9), (1, 8), (0, 6), (0, 3), (1, 1), (2, 0), (4, 0), (5, 1), (6, 3), (6, 6)]
          + [(5, 8), (4, 9), (2, 9)]],
    "1": [[(1.5, 7), (3, 9), (3, 0)]],
    "2": [[(0.5, 7), (1, 8.2), (2, 9), (4, 9), (5, 8.2), (5.5, 7), (5.5, 6), (5, 5), (0, 0)]
          + [(6, 0)]],
    "3": [[(0.5, 9), (5.5, 9), (2.5, 5.5), (4, 5.5), (5, 5), (5.7, 4), (6, 3), (5.5, 1.2)]
          + [(4.5, 0.2), (3, 0), (1.5, 0.2), (0.5, 1.2)]],
    "4": [[(4.5, 0), (4.5, 9), (0, 2.5), (6.5, 2.5)]],
    "6": [[(5.5, 8), (4.5, 9), (3, 9), (1.5, 8.4), (0.5, 6.5), (0, 4), (0.3, 1.6), (1.3, 0.3)]
          + [(3, 0), (4.5, 0.3), (5.6, 1.5), (6, 3), (5.6, 4.5), (4.5, 5.5), (3, 5.8)]
          + [(1.5, 5.4), (0.3, 4.2)]],
    "7": [[(0, 9), (6, 9), (2, 0)]],
    "8": [[(3, 9), (1, 8.6), (0.5, 7), (1, 5.6), (3, 5), (5, 4.4), (6, 2.5), (5.6, 1)]
          + [(4.5, 0.1), (3, 0), (1.5, 0.1), (0.4, 1), (0, 2.5), (1, 4.4), (3, 5), (5, 5.6)]
          + [(5.5, 7), (5, 8.6), (3, 9)]],
    '"': [[(0.5, 9), (0.5, 6.5)], [(2, 9), (2, 6.5)]],
    "'": [[(0.5, 9), (0.5, 6.5)]],
    "-": [[(0.5, 4.5), (4, 4.5)]],
}  # fmt: skip
_ADVANCE = {'"': 3.5, "'": 2.0, "-": 5.0}


def _pen(text: str, x: float, y: float, *, size: float = 8.0, up: bool = False) -> bytes:
    """`text` drawn as pen strokes from (x, y) in PDF space (`up`: reading up the page)."""
    unit = size / 9.0
    out = b""
    pen = 0.0
    for char in text:
        for stroke in _STROKES[char]:
            points = []
            for gx, gy in stroke:
                px, py = (pen + gx) * unit, gy * unit
                if up:
                    px, py = -py, px
                points.append((x + px, y + py))
            moves = " ".join(f"{a:.2f} {b:.2f} l" for a, b in points[1:])
            out += f"0 0 0 RG 0.5 w {points[0][0]:.2f} {points[0][1]:.2f} m {moves} S ".encode()
        pen += _ADVANCE.get(char, 7.5)
    return out


def _page(extra: bytes = b"", *, number: bytes | None = None) -> bytes:
    """A printed architect dimension (text), and the vendor's number drawn as lines over a
    dimension line of its own: by default two filled bars (its digits, the heavy end) and an inch
    mark of two small ticks after them (`test_dim_labels._glyphs`), as the scripted engines read."""
    body = _dimension(80, 200, 300) + _text(130, 303, '36"')
    body += _dimension(300, 360, 150) + (_glyphs(320.0, 323.5, y=154) if number is None else number)
    return body + extra


def _bars_up(x: float, ys: Sequence[float]) -> bytes:
    """A number drawn reading up the page: one bar per digit at each `y`, then the inch mark."""
    body = b"".join(f"0 0 0 rg {x} {y} 6 2.4 re f ".encode() for y in ys)
    end = max(ys) + 3.2
    return body + f"0 0 0 rg {x + 4} {end} 2 0.4 re f {x + 4} {end + 1} 2 0.4 re f ".encode()


def _feet(x: float, y: float) -> bytes:
    """A `7'-3"`-shaped row: a bar, a foot mark (one tick), a dash, a bar and the inch mark."""
    return (
        f"0 0 0 rg {x} {y} 2.4 6 re f {x + 3} {y + 4} 0.4 2 re f {x + 4} {y + 2.8} 1.6 0.4 re f "
        f"{x + 6.4} {y} 2.4 6 re f {x + 9.6} {y + 4} 0.4 2 re f {x + 10.6} {y + 4} 0.4 2 re f "
    ).encode()


def _write(tmp_path: Path, body: bytes) -> Path:
    path = tmp_path / "set.pdf"
    path.write_bytes(_pdf([body]))
    return path


# ---- scripted engines ----------------------------------------------------------------------------


def _forwards(picture: np.ndarray) -> bool:
    """Whether a spot is shown the right way round: its digits (most of the ink) before its light
    inch mark, so the first half of the ink holds more of it than the last."""
    ys, xs = np.nonzero(picture < 128)
    if len(xs) == 0:
        return False
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    if (x1 - x0) * 2 < int(ys.max()) - int(ys.min()):
        return False  # standing: shown sideways
    middle = (x0 + x1) // 2
    dark = picture < 128
    return int(dark[:, x0:middle].sum()) > int(dark[:, middle:x1].sum())


class _Reader:
    """Reads a spot shown the right way round as `text`, any other way as `other`."""

    def __init__(self, name: str, text: str, other: str = "-") -> None:
        self.name = name
        self.text = text
        self.other = other
        self.calls = 0

    def __call__(self, picture: np.ndarray, *, single_line: bool) -> list[str]:
        self.calls += 1
        return [self.text if _forwards(picture) else self.other]


def _engines(rapid: str, tesseract: str, other: str = "-") -> list[_Reader]:
    return [_Reader("RapidOCR", rapid, other), _Reader("Tesseract", tesseract, other)]


def _labels(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    first = dl.page_labels(path, 1, HALVES)
    return first.labels, first.phrases


def _ocr(path: Path, readers: Sequence[Any]) -> dl.PageLabels:
    labels, phrases = _labels(path)
    return dl.ocr_pass(path, 1, HALVES, labels, phrases, budget_s=60, readers=readers)


def _added(found: dl.PageLabels) -> list[dict[str, Any]]:
    return [label for label in found.labels if label.get("origin") == ocr_labels.ORIGIN]


# ---- two agreeing reads --------------------------------------------------------------------------


def test_two_engines_agreeing_at_a_missed_spot_make_a_label_after_the_others(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, _page())
    before, _phrases = _labels(path)

    found = _ocr(path, _engines('18"', '18"'))

    (label,) = _added(found)
    assert (label["text"], label["inches"], label["side"], label["orientation"]) == (
        '18"',
        "18",
        "vendor",
        "h",
    )
    # The OCR trust class: drawn ink, read by OCR, no dimension line of its own.
    assert (label["route"], label["dim_line"], label["extension"]) == ("drawn", None, None)
    # Its box is its own ink in page points (origin top-left): bars from x 320, y 154 to 160 up.
    assert label["box"] == pytest.approx([320.0, 240.0, 328.1, 246.0], abs=0.6)
    # Every older label keeps its id; the new one is numbered after them.
    assert [(lab["id"], lab["text"]) for lab in found.labels[: len(before)]] == [
        (lab["id"], lab["text"]) for lab in before
    ]
    assert label["id"] == f"D{len(before) + 1}"
    assert found.ocr is not None and found.ocr["status"] == "done"


def test_a_sideways_number_is_read_up_the_page(tmp_path: Path) -> None:
    body = _dimension(80, 200, 300) + _text(130, 303, '36"')
    path = _write(tmp_path, body + _line(500, 100, 500, 220) + _bars_up(492.0, (140.0, 143.5)))

    (label,) = _added(_ocr(path, _engines('22"', '22"')))

    assert (label["text"], label["orientation"]) == ('22"', "v")


def test_an_inch_mark_an_engine_drops_is_put_back_only_where_the_vectors_draw_it(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, _page())

    # `18` read with no mark: the vectors draw the two ticks after the digits, so it is 18".
    (label,) = _added(_ocr(path, _engines("18", '18"')))
    assert label["inches"] == "18"
    # A foot mark or a dash in the row: its digits alone are never its value.
    feet = _write(tmp_path, _page(number=_feet(320.0, 154.0)))
    assert not _added(_ocr(feet, _engines("73", "73")))


# ---- anything less is never a label --------------------------------------------------------------


@pytest.mark.parametrize(
    ("rapid", "tesseract", "why"),
    [
        ('18"', '16"', "the readings disagree"),
        ('18"', "-", "only one engine read it"),
    ],
)
def test_less_than_two_agreeing_reads_is_a_suggestion_never_a_label(
    tmp_path: Path, rapid: str, tesseract: str, why: str
) -> None:
    path = _write(tmp_path, _page())

    found = _ocr(path, _engines(rapid, tesseract))

    assert not _added(found)
    assert found.ocr is not None
    (item,) = found.ocr["unconfirmed"]
    assert item["why"] == f"{ocr_labels.UNCONFIRMED} ({why})"
    assert item["side"] == "vendor"


def test_a_number_read_both_ways_round_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, _page())

    # Upside down it reads as another whole dimension: code cannot tell which way it reads.
    found = _ocr(path, _engines('18"', '18"', other='81"'))

    assert not _added(found)
    assert "both ways round" in found.ocr["unconfirmed"][0]["why"]  # type: ignore[index]


class _Row(_Reader):
    """Reads the spot as `text`, but a picture of the row round it (much wider than tall) as
    `row`: what a scale note's `1"` looks like with its `= 1'-0"` beside it."""

    def __init__(self, name: str, text: str, row: str) -> None:
        super().__init__(name, text)
        self.row = row

    def __call__(self, picture: np.ndarray, *, single_line: bool) -> list[str]:
        if picture.shape[1] > 2.5 * picture.shape[0]:
            return [self.row]
        return super().__call__(picture, single_line=single_line)


@pytest.mark.parametrize("row", ['SCALE 18" = 1\'-0"', '18" = 1\'-0"', 'TYP 18"'])
def test_a_number_with_words_or_signs_beside_it_is_refused(tmp_path: Path, row: str) -> None:
    path = _write(tmp_path, _page())
    readers = [_Row("RapidOCR", '18"', row), _Reader("Tesseract", '18"')]

    found = _ocr(path, readers)

    assert not _added(found)
    assert found.ocr is not None
    assert found.ocr["unconfirmed"][0]["why"].endswith("(words or signs are written beside it)")


def test_digits_or_ticks_beside_a_number_do_not_refuse_it(tmp_path: Path) -> None:
    path = _write(tmp_path, _page())
    readers = [_Row("RapidOCR", '18"', '| 18" | 1/2"'), _Reader("Tesseract", '18"')]

    assert [label["text"] for label in _added(_ocr(path, readers))] == ['18"']


def test_one_engine_reads_nothing(tmp_path: Path) -> None:
    path = _write(tmp_path, _page())
    one = [_Reader("Tesseract", '18"')]

    found = _ocr(path, one)

    assert not _added(found)
    assert found.ocr is not None and found.ocr["status"] == "no_engine"


def test_a_spot_no_engine_reads_is_not_shown_to_the_next_engine(tmp_path: Path) -> None:
    path = _write(tmp_path, _page())
    blank, second = _Reader("RapidOCR", "-"), _Reader("Tesseract", '18"')

    assert not _added(_ocr(path, [blank, second]))
    assert blank.calls > 0 and second.calls == 0


def test_a_number_a_label_already_covers_is_not_read_again(tmp_path: Path) -> None:
    path = _write(tmp_path, _page())
    labels, phrases = _labels(path)
    drawn = {"id": "", "text": '18"', "box": [319.0, 237.0, 336.0, 247.0], "route": "drawn"}
    readers = _engines('18"', '18"')

    found = dl.ocr_pass(path, 1, HALVES, [*labels, drawn], phrases, budget_s=60, readers=readers)

    assert not _added(found)
    assert readers[0].calls == 0


def test_a_number_on_no_drawing_is_not_read(tmp_path: Path) -> None:
    path = _write(tmp_path, _page())
    labels, phrases = _labels(path)
    readers = _engines('18"', '18"')

    found = dl.ocr_pass(path, 1, HALVES[:1], labels, phrases, budget_s=60, readers=readers)

    assert not _added(found) and readers[0].calls == 0


def test_a_number_in_gv_colour_is_never_read(tmp_path: Path) -> None:
    # Every coloured pixel is whitened before reading: GV's notes are red, blue or yellow.
    red = _glyphs(320.0, 323.5, y=154).replace(b"0 0 0 rg", b"1 0 0 rg")
    path = _write(tmp_path, _page(number=red))

    assert not _added(_ocr(path, _engines('18"', '18"')))


def test_the_route_failing_keeps_the_pages_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, _page())
    labels, phrases = _labels(path)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("engine crashed")

    monkeypatch.setattr(ocr_labels, "read_spots", broken)
    found = dl.ocr_pass(path, 1, HALVES, labels, phrases, budget_s=60, readers=_engines("1", "1"))

    assert [label["text"] for label in found.labels] == [label["text"] for label in labels]
    assert found.ocr is not None and found.ocr["status"] == "failed"


# ---- the pack ------------------------------------------------------------------------------------


def test_the_pack_reads_missed_numbers_last_and_moves_no_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, _page())
    page_map = {"pages": [{"file": "set.pdf", "page": 1, "halves": HALVES}]}
    # The drawn route's engines read nothing here; the OCR route's two agree.
    monkeypatch.setattr(dl, "drawn_readers", lambda: _engines('18"', '18"'))
    monkeypatch.setattr(dl, "_drawn_label", lambda *_args: None)

    document = dl.build_dims({"set.pdf": path}, page_map, drawn_budget_s=60)
    off = dl.build_dims({"set.pdf": path}, page_map, drawn_budget_s=60, ocr_budget_s=0)

    (entry,) = document["pages"]
    labels = entry["labels"]
    assert [label["id"] for label in labels] == [f"D{n}" for n in range(1, len(labels) + 1)]
    assert labels[-1]["origin"] == "ocr" and labels[-1]["text"] == '18"'
    assert [lab["text"] for lab in labels[:-1]] == [
        lab["text"] for lab in off["pages"][0]["labels"]
    ]
    assert entry["ocr"]["status"] == "done" and entry["ocr"]["seconds"] >= 0
    assert "ocr" not in off["pages"][0]
    assert document["version"] == dl.LABELS_VERSION


def test_a_page_the_drawn_route_did_not_reach_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, _page())
    page_map = {"pages": [{"file": "set.pdf", "page": 1, "halves": HALVES}]}
    monkeypatch.setattr(dl, "drawn_readers", lambda: _engines('18"', '18"'))

    document = dl.build_dims({"set.pdf": path}, page_map, drawn_budget_s=0)

    assert "ocr" not in document["pages"][0]


# ---- the trust class: an OCR reading, never more -------------------------------------------------


_OCR_LABEL: dict[str, Any] = {
    "id": "D9",
    "page": 1,
    "file": "set.pdf",
    "text": '18"',
    "inches": "18",
    "route": "drawn",
    "origin": "ocr",
    "side": "vendor",
    "orientation": "h",
    "box": [320.0, 238.0, 335.0, 246.0],
    # Even a line (never written by the route) is never used.
    "dim_line": [[300.0, 250.0], [360.0, 250.0]],
    "scale_ok": True,
}


def test_an_ocr_label_is_read_by_pixels_like_a_picture_label() -> None:
    assert OCR_ORIGINS == {"picture", "ocr"}
    assert ocr_read(_OCR_LABEL) and ocr_read({"origin": "picture"})
    assert not ocr_read({"route": "drawn"}) and not ocr_read({"route": "text"})


def test_an_ocr_label_never_passes_a_check_on_a_line() -> None:
    assert advocate._axis(_OCR_LABEL) is None
    assert advocate._line(_OCR_LABEL) is None
    assert dim_pairs._along(_OCR_LABEL) is None
    segments = [((300.0, 250.0), (360.0, 250.0)), ((300.0, 240.0), (300.0, 260.0))]
    drawing = same_ends.side("vendor", [_OCR_LABEL], segments, (250.0, 0.0, 600.0, 400.0))
    assert drawing is None or "D9" not in drawing.lines


def test_an_ocr_label_has_its_own_ink_looked_at_for_gv_colour() -> None:
    assert simple_verify._ink_check(_OCR_LABEL, None).outcome == "unknown"
    assert simple_verify._ink_check(_OCR_LABEL, lambda _label: (1, 0.0)).outcome == "pass"
    assert simple_verify._ink_check(_OCR_LABEL, lambda _label: (1, 0.9)).outcome == "fail"


def test_an_ocr_label_says_how_it_was_read() -> None:
    assert "read at its spot, two OCR engines" in label_line(_OCR_LABEL)
    finding = {
        "vendor": '18"',
        "vendor_box": {"page": 1, "x0": 320.0, "y0": 238.0, "x1": 335.0, "y1": 246.0},
        "dims": {"vendor": dict(_OCR_LABEL)},
    }
    checks = label_checks(finding, {})
    assert "read at its spot (two OCR engines)" in checks["verify_vendor"]["reason"]


# ---- real engines on drawn strokes (skipped where an engine is missing) ---------------------------


def _real() -> list[Any]:
    readers = dl.drawn_readers()
    if {reader.name for reader in readers} != {"RapidOCR", "Tesseract"}:
        pytest.skip("RapidOCR and Tesseract are both needed")
    return readers


class _Blurred:
    """A real engine shown each spot blurred (a picture too soft to read a digit by)."""

    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self.name = reader.name

    def __call__(self, picture: np.ndarray, *, single_line: bool) -> list[str]:
        soft = cv2.GaussianBlur(picture, (0, 0), 6)
        return list(self.reader(soft, single_line=single_line))


def test_real_engines_read_a_drawn_number_the_readers_missed(tmp_path: Path) -> None:
    readers = _real()
    pen = _pen('18"', 320, 154) + _line(500, 100, 500, 220) + _pen('22"', 497, 140, up=True)
    path = _write(tmp_path, _page(number=pen))

    found = _ocr(path, readers)

    assert sorted((lab["text"], lab["orientation"]) for lab in _added(found)) == [
        ('18"', "h"),
        ('22"', "v"),
    ]
    assert {Decimal(lab["inches"]) for lab in _added(found)} == {Decimal(18), Decimal(22)}


def test_real_engines_never_confirm_a_blurry_number(tmp_path: Path) -> None:
    readers = [_Blurred(reader) for reader in _real()]
    path = _write(tmp_path, _page(number=_pen('18"', 320, 154)))

    assert not _added(_ocr(path, readers))
