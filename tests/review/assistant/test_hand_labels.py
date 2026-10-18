"""Hand-lettered dimensions on one line (#1662): feet and inches, inline fractions.

A synthetic hand-lettering stroke font like an architect's CAD set: a `1` is one bare upright
stroke, a zero carries a slash, a fraction is written inline with a slanted `/`, the foot mark and
a doubled dash stand a letter space apart, and a `7` has an upright stem under a flat bar (both OCR
engines read it as `1`). The OCR engines are scripted; nothing here needs an engine installed or
calls a model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.review.assistant import dim_labels as dl
from app.review.assistant import hand_labels as hl
from app.review.assistant import ink_labels
from tests.review.assistant.test_dim_labels import HALVES, _dimension, _pdf

PAGE_H = 400.0
#: Strokes on a 4 wide x 6 tall grid, y up; one tuple per path.
_OVAL = tuple(
    (2 + 2 * float(np.cos(a)), 3 + 3 * float(np.sin(a))) for a in np.linspace(0, 2 * np.pi, 25)
)
HAND: dict[str, tuple[tuple[tuple[float, float], ...], ...]] = {
    "0": (_OVAL, ((0.6, 0.4), (3.4, 5.6))),
    "1": (((1, 6), (1, 3), (1, 0)),),  # one stroke, drawn as three points
    "2": (((0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (0, 0)), ((0, 0), (4, 0))),
    "3": (((0, 6), (4, 6), (2, 3.4), (4, 2), (4, 1), (3, 0), (0, 0)),),
    "4": (((3, 0), (3, 6), (0, 2), (4, 2)),),
    "5": (((4, 6), (0, 6), (0, 3.4), (3, 3.4), (4, 2.4), (4, 1), (3, 0), (0, 0)),),
    "6": (((3.5, 6), (1, 6), (0, 4), (0, 1), (1, 0), (3, 0), (4, 1), (4, 2.5), (0, 3)),),
    "7": (((0.6, 6), (2.4, 6)), ((2.4, 6), (2.4, 0))),  # a flat bar over an upright stem
    "8": (((2, 3), (0, 4.5), (2, 6), (4, 4.5), (2, 3), (0, 1.5), (2, 0), (4, 1.5), (2, 3)),),
    "9": (((4, 3), (0, 3.5), (0, 5), (1, 6), (3, 6), (4, 5), (4, 0)),),
}
_SCALE = 5.0 / 6  # a 5-point letter height


@dataclass(frozen=True, slots=True)
class _Path:
    """An `ink_labels.InkPath` in shown points (origin top-left)."""

    box: tuple[float, float, float, float]
    polylines: tuple[tuple[tuple[float, float], ...], ...]
    filled: bool = False
    width: float = 0.57


def _hand(text: str, x: float, y: float, up: bool = False) -> list[list[tuple[float, float]]]:
    """`text` hand-lettered as PDF-space strokes (y up), baseline-left at `(x, y)`; `up` reads up
    the page. Returns one list of points per path."""
    out: list[list[tuple[float, float]]] = []
    advance = 0.0

    def place(along: float, rise: float) -> tuple[float, float]:
        return (x - rise, y + along) if up else (x + along, y + rise)

    def stroke(points: Sequence[tuple[float, float]]) -> None:
        out.append([place(advance + gx * _SCALE, gy * _SCALE) for gx, gy in points])

    for char in text:
        if char == " ":
            advance += 1.6  # a word space: about 0.7 of the letter height with the gaps round it
        elif char == "'":
            stroke(((0, 6), (0, 4)))
            advance += 1.5
        elif char == '"':
            stroke(((0, 6), (0, 3.6)))
            advance += 1.1
            stroke(((0, 6), (0, 3.6)))
            advance += 1.5
        elif char == "-":
            stroke(((0, 2.6), (1.9, 2.6)))
            stroke(((0, 2.9), (1.9, 2.9)))
            advance += 1.6 + 1.3
        elif char == "/":
            stroke(((0, 0), (2, 6)))
            advance += 1.7 + 0.6
        else:
            for line in HAND[char]:
                stroke(line)
            right = max(gx for line in HAND[char] for gx, _gy in line)
            advance += right * _SCALE + 1.3
    return out


def _shown(lines: Sequence[Sequence[tuple[float, float]]]) -> list[_Path]:
    paths = []
    for line in lines:
        flipped = tuple((px, PAGE_H - py) for px, py in line)
        xs, ys = [p[0] for p in flipped], [p[1] for p in flipped]
        paths.append(_Path((min(xs), min(ys), max(xs), max(ys)), (flipped,)))
    return paths


def _one(text: str, up: bool = False) -> hl.Lettered:
    (item,) = hl.lettered(_shown(_hand(text, 100, 300, up)))
    return item


# ---- the vectors ---------------------------------------------------------------------------------


def test_feet_inches_and_an_inline_fraction_are_one_dimension() -> None:
    item = _one("2'-6 1/2\"")
    assert item.turn == 0
    assert item.layout == (1, 1, 1, 1)  # feet, inches, numerator, denominator
    assert item.known == (None, None, "1", None)  # the bare stroke is a 1
    assert len(item.label.glyphs) == 9  # 2 ' - 6 1 / 2 and the inch mark's two ticks


def test_a_slashed_zero_and_bare_ones_are_known_by_their_vectors() -> None:
    item = _one("1'-10 1/4\"")
    assert item.layout == (1, 2, 1, 1)
    assert item.known == ("1", "1", "0", "1", None)


def test_the_shapes_a_dimension_takes() -> None:
    assert _one('5 3/8"').layout == (0, 1, 1, 1)
    assert _one('9"').layout == (0, 1, 0, 0)
    assert _one('3/4"').layout == (0, 0, 1, 1)
    assert _one("3'-10\"").layout == (1, 2, 0, 0)


@pytest.mark.parametrize(
    "text",
    [
        "2'-6",  # no inch mark
        '1-1/2"',  # a dash with no foot mark before it
        "2'6'",  # a foot mark where the inch mark goes
        '2"6',  # an inch mark inside
        "46",  # no mark at all
    ],
)
def test_anything_else_is_no_dimension(text: str) -> None:
    assert hl.lettered(_shown(_hand(text, 100, 300))) == []


def test_a_label_reading_up_the_page() -> None:
    item = _one("2'-6 1/2\"", up=True)
    assert item.turn == 90
    assert item.layout == (1, 1, 1, 1)


# ---- the value from an OCR line ------------------------------------------------------------------


def test_a_reading_is_laid_out_as_the_vectors_say() -> None:
    item = _one("2'-6 1/2\"")
    for line in ("2'-6 1/2\"", "2-6 1/2", "2-61/2", "2' 6 1 2"):
        assert hl.value(item, line) == Decimal("30.5"), line
    assert hl.text_of(item, Decimal("30.5")) == "2'-6 1/2\""
    # Not this label's count of digits: no reading.
    assert hl.value(item, "2-6 /2") is None
    assert hl.value(item, "2-6 1/2 9") is None
    # A digit the vectors know read otherwise: no reading.
    assert hl.value(item, "2-6 7/2") is None


def test_a_slashed_zero_must_read_as_zero() -> None:
    item = _one("3'-0 1/2\"")
    assert hl.value(item, "3'-0 1/2\"") == Decimal("36.5")
    assert hl.text_of(item, Decimal("36.5")) == "3'-0 1/2\""
    assert hl.value(item, "3'-8 1/2\"") is None
    assert hl.value(item, "3'-6 1/2\"") is None


def test_a_hand_lettered_7_read_as_1_is_no_reading() -> None:
    # Both engines read this `7` (upright stem, flat bar) as `1`; its vectors say it is no 1.
    item = _one('7 5/8"')
    assert item.known == (None, None, None) and item.ones[0] is False
    assert hl.value(item, '1 5/8"') is None
    assert hl.value(item, '7 5/8"') == Decimal("7.625")


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("3'-14\"", "3'-14\""),  # inches after feet must be under 12
        ('3/9"', '3/9"'),  # a fraction no drawing prints
        ('5/4"', '5/4"'),  # a numerator not below its denominator
        ("2'-06\"", "2'-06\""),  # a leading zero
    ],
)
def test_impossible_values_are_none(text: str, line: str) -> None:
    item = _one(text)
    assert hl.value(item, line) is None


def test_text_as_the_drawing_letters_it() -> None:
    assert hl.text_of(_one('5 3/8"'), Decimal("5.375")) == '5 3/8"'
    assert hl.text_of(_one('3/4"'), Decimal("0.75")) == '3/4"'
    assert hl.text_of(_one("3'-10\""), Decimal(46)) == "3'-10\""
    assert hl.text_of(_one("2'-0 1/2\""), Decimal("24.5")) == "2'-0 1/2\""


def test_the_picture_draws_a_known_1_flagged_and_a_zero_without_its_slash() -> None:
    item = _one("1'-0\"")
    tokens = hl.tokens(item)
    assert [t.kind for t in tokens] == ["glyph", "mark", "mark", "glyph", "mark", "mark"]
    one, zero = tokens[0].glyphs[0], tokens[3].glyphs[0]
    assert len(one.paths[0].polylines) == 2  # the stem and its flag
    assert len(zero.paths) == 1  # the oval alone
    picture = ink_labels.row_picture(tokens, 0, 40)
    assert picture is not None and picture.min() == 0


# ---- through `dim_labels` ------------------------------------------------------------------------


class _Scripted:
    """An OCR engine that reads the same line in every picture."""

    def __init__(self, name: str, line: str, *, marks: bool = True) -> None:
        self.name, self.line, self.reads_inch_marks = name, line, marks

    def __call__(self, picture: Any, *, single_line: bool) -> list[str]:
        return [self.line]


def _ops(lines: Sequence[Sequence[tuple[float, float]]]) -> bytes:
    out = []
    for line in lines:
        parts = [f"{x:.3f} {y:.3f} {'m' if n == 0 else 'l'}" for n, (x, y) in enumerate(line)]
        out.append("0 0 0 RG 0.57 w " + " ".join(parts) + " S")
    return (" ".join(out) + " ").encode()


def _page(path: Path, x: float, text: str = "2'-6 1/2\"") -> Path:
    # The label over a dimension line at y = 300 (PDF space), its extension lines below.
    body = _dimension(x - 20, x + 50, 300) + _ops(_hand(text, x, 302))
    path.write_bytes(_pdf([body]))
    return path


def _lettered_labels(path: Path, readers: Sequence[Any]) -> list[dict[str, Any]]:
    found = dl.page_labels(path, 1, HALVES, drawn_budget_s=60, readers=readers)
    return [label for label in found.labels if label["route"] == "drawn"]


def test_an_architects_hand_lettered_dimension_becomes_a_label(tmp_path: Path) -> None:
    both = [_Scripted("RapidOCR", "2-6 1/2", marks=False), _Scripted("Tesseract", "2'-6 1/2\"")]
    (label,) = _lettered_labels(_page(tmp_path / "a.pdf", 100), both)
    assert label["side"] == "architect"
    assert (label["text"], label["inches"], label["orientation"]) == ("2'-6 1/2\"", "30.5", "h")
    assert label["dim_line"] is not None


def test_two_engines_must_read_the_same_value(tmp_path: Path) -> None:
    pdf = _page(tmp_path / "a.pdf", 100)
    other = [_Scripted("RapidOCR", "2-6 1/2", marks=False), _Scripted("Tesseract", "2'-8 1/2\"")]
    assert _lettered_labels(pdf, other) == []
    alone = [_Scripted("RapidOCR", "2-6 1/2", marks=False), _Scripted("Tesseract", "")]
    assert _lettered_labels(pdf, alone) == []


def test_the_vendors_drawing_is_left_to_its_own_route(tmp_path: Path) -> None:
    # The same lettering on the vendor's half: this reader does not run there.
    both = [_Scripted("RapidOCR", "2-6 1/2", marks=False), _Scripted("Tesseract", "2'-6 1/2\"")]
    labels = _lettered_labels(_page(tmp_path / "v.pdf", 400), both)
    assert all(label["side"] != "architect" for label in labels)
    assert all(label["text"] != "2'-6 1/2\"" for label in labels)


def test_a_dimension_with_no_line_of_its_own_is_no_label(tmp_path: Path) -> None:
    path = tmp_path / "n.pdf"
    path.write_bytes(_pdf([_ops(_hand("2'-6 1/2\"", 100, 302))]))
    both = [_Scripted("RapidOCR", "2-6 1/2", marks=False), _Scripted("Tesseract", "2'-6 1/2\"")]
    assert _lettered_labels(path, both) == []


def test_a_row_of_flat_strokes_is_no_lettering() -> None:
    # Short flat strokes end to end (a line drawn in pieces, no letter height at all): nothing,
    # and no error.
    flat = [[(100 + 1.5 * k, 300.0), (101.5 + 1.5 * k, 300.0)] for k in range(6)]
    assert hl.lettered(_shown(flat)) == []
