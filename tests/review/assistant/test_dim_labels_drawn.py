"""The drawn route's recall step (#1615, #1616, #1617): more of the vendor's pen-stroke dimensions
read, with no guard loosened.

Synthetic CAD stroke lettering (`stroke_text`) and hand-made vector ink. The OCR engines are the
tiny `StrokeReader` (it reads the same stroke font) or scripted readers; nothing here needs an OCR
engine installed or calls a model.
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
from app.review.assistant import ink_labels
from tests.review.assistant.stroke_text import StrokeReader, stroke_ops, stroke_paths
from tests.review.assistant.test_dim_labels import HALVES, _dimension, _pdf

PAGE_H = 400.0


@dataclass(frozen=True, slots=True)
class _Path:
    """An `ink_labels.InkPath` in shown points (origin top-left)."""

    box: tuple[float, float, float, float]
    polylines: tuple[tuple[tuple[float, float], ...], ...]
    filled: bool = False
    width: float = 0.3


def _shown(lines: Sequence[Sequence[tuple[float, float]]]) -> _Path:
    """Lines in PDF space (y up) as a shown path."""
    flipped = tuple(tuple((x, PAGE_H - y) for x, y in line) for line in lines)
    xs = [x for line in flipped for x, _ in line]
    ys = [y for line in flipped for _, y in line]
    return _Path((min(xs), min(ys), max(xs), max(ys)), flipped)


def _ink(text: str, x: float, y: float, height: float = 6.0) -> list[_Path]:
    return [_shown(path.lines) for path in stroke_paths(text, x, y, height)]


def _label(paths: Sequence[_Path]) -> ink_labels.Label:
    xs = [v for p in paths for v in (p.box[0], p.box[2])]
    ys = [v for p in paths for v in (p.box[1], p.box[3])]
    spot = (min(xs), min(ys), max(xs), max(ys))
    (label,) = ink_labels.labels(paths, spot)
    return label


def _engines() -> list[Any]:
    return [StrokeReader("RapidOCR"), StrokeReader("Tesseract")]


def _drawn(path: Path, body: bytes) -> list[dict[str, Any]]:
    path.write_bytes(_pdf([body]))
    found = dl.page_labels(path, 1, HALVES, drawn_budget_s=60, readers=_engines())
    return [label for label in found.labels if label["route"] == "drawn"]


# ---- seeds ---------------------------------------------------------------------------------------


def test_a_lone_digit_with_its_inch_mark_set_wide_is_read(tmp_path: Path) -> None:
    # Large lettering: the inch mark is more than the clusters' gap (1.5 pt) from its digit, so
    # no cluster holds the label; a lone character-sized path seeds it.
    body = _dimension(280, 340, 50) + stroke_ops('2"', 300, 52, height=9)
    assert [label["text"] for label in _drawn(tmp_path / "lone.pdf", body)] == ['2"']


# ---- the vectors' layout -------------------------------------------------------------------------


def test_strokes_of_one_character_count_once() -> None:
    # A `4` drawn as two paths that do not touch, then a `7`: two characters.
    four = [_shown((((0, 6), (0, 3), (4, 3)),)), _shown((((3, 5), (3, 0)),))]
    seven = [_shown((((6, 6), (10, 6), (7, 0)),))]
    glyphs = [ink_labels.Glyph((p,), p.box) for p in (*four, *seven)]
    characters = dl._characters(glyphs, 0)
    assert [len(character) for character in characters] == [2, 1]


@pytest.mark.parametrize(
    ("line", "layout", "expected"),
    [
        ("349", (0, 1, 1), '34"'),  # the inch mark read as a 9
        ("3/499", (0, 1, 1), '3/4"'),
        ('34"', (2, 0, 0), '34"'),  # no more digits than drawn: as read
        ("39", (2, 0, 0), "39"),  # a real 9
        ("549", (0, 1, 1), '54"'),  # still read: the layout then refuses 5/4
        ("3/4", None, "3/4"),
    ],
)
def test_an_inch_mark_read_as_nines_is_put_back(
    line: str, layout: tuple[int, int, int] | None, expected: str
) -> None:
    assert dl._mark_as_nines(line, layout) == expected


class _Reader:
    def __init__(self, name: str, reads_inch_marks: bool) -> None:
        self.name = name
        self.reads_inch_marks = reads_inch_marks


def test_a_stacked_fraction_is_read_for_its_digits_tesseract_without_the_slash() -> None:
    (label,) = ink_labels.labels(_ink('17{3/8}"', 100, 300), (95, 85, 140, 106))
    (row,) = ink_labels.rows(label, 0)
    layout = dl._layout(row, 0)
    assert layout == (2, 1, 1)
    rapid, tesseract = _Reader("RapidOCR", False), _Reader("Tesseract", True)
    # RapidOCR: as `verify_value` reads it (no inch mark), then the digits alone.
    assert dl._passes(rapid, row, layout, True) == [[(False, True, False), (False, False, False)]]
    # Tesseract: the digits alone first, the row as `verify_value` reads it only when they fail.
    assert dl._passes(tesseract, row, layout, True) == [
        [(True, False, False)],
        [(True, True, False)],
    ]
    # A fraction alone: no inch mark, no slash, read as a raw line.
    fraction = ink_labels.Token("fraction", row[2].glyphs, row[2].below)
    assert dl._passes(tesseract, [fraction], (0, 1, 1), True) == [[(False, False, True)]]


# ---- the inch mark -------------------------------------------------------------------------------


def test_an_inch_mark_is_two_twin_ticks() -> None:
    digit = _ink("1", 100, 300)
    twins = [_shown((((104, 306), (104, 304)),)), _shown((((105, 306), (105, 304)),))]
    (row,) = ink_labels.rows(_label(digit + twins), 0)
    assert dl._twin_ticks(row, 0)
    # Two pieces of a broken circle beside a dash: not an inch mark.
    arcs = [
        _shown((((104, 306), (104.6, 305.6), (104.8, 304.8)),)),
        _shown((((105.2, 305.4), (105.6, 304.4), (105.4, 303.8)),)),
    ]
    rows = ink_labels.rows(_label(digit + arcs), 0)
    assert not any(dl._twin_ticks(row, 0) for row in rows)


def test_the_inch_mark_tells_a_lone_digits_reading_direction() -> None:
    # `4"` written up the page: a lone digit has no neighbour to tell its direction by.
    paths = [_shown(p.lines) for p in stroke_paths('4"', 100, 300, 6.0, turn=True)]
    assert dl._marked_turns(_label(paths)) == (90,)


# ---- parts of a label ----------------------------------------------------------------------------


def test_two_dimensions_stacked_into_one_label_are_read_apart() -> None:
    # `4"` stacked right over `1 1/2"` (a chain's labels close together).
    paths = _ink('4"', 100, 309) + _ink('1{1/2}"', 100, 300)
    label = _label(paths)
    parts = dl._parts(label)
    assert len(parts) == 2
    assert all(part.kind == "inches" for part in parts)
    assert sorted(len(part.label.glyphs) for part in parts) == [3, 6]


def test_a_value_with_a_note_after_its_inch_mark_is_the_value() -> None:
    letters = [
        _shown((((x, 300), (x + 2.4, 302), (x, 304), (x + 2.4, 306)),)) for x in (112.0, 115.6)
    ]
    label = _label(_ink('24"', 100, 300) + letters)
    (part,) = dl._parts(label)
    assert len(part.label.glyphs) == 4  # 2, 4 and the inch mark's two ticks
    assert len(part.note) == 2 and part.spot == label.box


class _Scripted:
    def __init__(self, name: str, lines: Sequence[str]) -> None:
        self.name = name
        self.lines = list(lines)
        self.calls = 0

    def __call__(self, picture: np.ndarray, *, single_line: bool) -> list[str]:
        line = self.lines[self.calls % len(self.lines)]
        self.calls += 1
        return [line] if line else []


@pytest.mark.parametrize(("note", "words"), [("(FIELD TRIM)", True), ('+2" FILLER', False)])
def test_a_note_must_read_as_words_only(note: str, words: bool) -> None:
    letters = [
        _shown((((x, 300), (x + 2.4, 302), (x, 304), (x + 2.4, 306)),)) for x in (112.0, 115.6)
    ]
    (part,) = dl._parts(_label(_ink('24"', 100, 300) + letters))
    assert dl._note_is_words(part, [_Scripted("RapidOCR", [note])]) is words


def _bracket(x: float, y: float, opening: bool) -> _Path:
    foot = 0.8 if opening else -0.8
    return _shown((((x + foot, y + 6), (x, y + 6), (x, y - 0.5), (x + foot, y - 0.5)),))


def _mm_over_inches() -> ink_labels.Label:
    paths = _ink("305", 100, 310) + _ink("12", 102, 300)
    paths += [_bracket(100.6, 300, True), _bracket(112.4, 300, False)]
    return _label(paths)


@pytest.mark.parametrize(
    ("inches", "value"),
    [("[12]", (Decimal(12), "305")), ("[13]", None)],  # 13" is 330 mm: they disagree
)
def test_millimetres_over_inches_must_agree(inches: str, value: Any) -> None:
    label = _mm_over_inches()
    (part,) = dl._parts(label)
    assert part.kind == "mm"
    # Each engine reads the millimetre row at two sizes, then the inch row at two sizes.
    engines = [_Scripted(name, ["305", "305", inches, inches]) for name in ("RapidOCR", "Tess")]
    assert dl._mm_value(part, engines) == value


# ---- line work beside a label --------------------------------------------------------------------


def test_a_dimension_lines_pieces_in_a_label_are_line_work_but_a_fraction_bar_is_not() -> None:
    paths = _ink('1{1/2}"', 100, 300)
    piece = _shown((((112.0, 298.0), (112.0, 307.0)),))  # a piece of a line cut by its ticks
    label = _label([*paths, piece])
    # The line runs on above and below the label.
    lines = np.array([[112.0, 80.0, 112.0, 93.0], [112.0, 102.0, 112.0, 120.0]])
    bare = dl._without_line_work(label, lines)
    assert bare is not None and len(bare.glyphs) == len(label.glyphs) - 1
    # A fraction's bar in line with a dimension line keeps its place: glyphs on both sides.
    (bar,) = [g for g in label.glyphs if g.wide > 2 * g.tall and g.tall < 0.2]
    bar_y = bar.box[1]
    along = np.array(
        [[60.0, bar_y, bar.box[0] - 0.2, bar_y], [bar.box[2] + 0.2, bar_y, 160, bar_y]]
    )
    kept = dl._without_line_work(_label(paths), along)
    assert kept is not None and bar in kept.glyphs


# ---- the dimension line --------------------------------------------------------------------------


def test_a_narrow_span_between_ticks_is_a_drawn_labels_line() -> None:
    cuts = [0.0, 10.0, 14.0, 30.0]
    # A 3/4" piece: its ticks closer than the label is wide, its middle over them.
    assert dl._span_at(cuts, 12.5, 9.0, 16.0, 0.0, 30.0, 7.0, True, []) == (10.0, 14.0)
    assert dl._span_at(cuts, 12.5, 9.0, 16.0, 0.0, 30.0, 7.0, False, []) is None
    # Its middle just past a tick, the span there another label's: the span beside it.
    assert dl._span_at(cuts, 14.5, 9.0, 16.0, 0.0, 30.0, 7.0, True, [(14.0, 30.0)]) == (
        10.0,
        14.0,
    )


def test_on_the_drawn_route_a_centre_lines_dash_and_a_letters_stroke_do_not_cut_a_line() -> None:
    y = 100.0
    segments = [
        ((50.0, y + 3), (50.0, y - 20)),  # an extension line
        ((48.5, y + 1.5), (51.5, y - 1.5)),  # its tick
        ((150.0, y + 3), (150.0, y - 20)),
        ((148.5, y + 1.5), (151.5, y - 1.5)),
        ((90.0, y + 1.5), (90.0, y - 1.5)),  # a centre line's dash
        ((120.0, y + 1.2), (122.0, y - 1.0)),  # a character's slanted stroke
    ]
    assert dl._boundaries(y, 40.0, 160.0, segments, 6.0, drawn=True) == [40.0, 50.0, 150.0, 160.0]
    assert 90.0 in dl._boundaries(y, 40.0, 160.0, segments, 6.0)


def test_one_line_one_label(tmp_path: Path) -> None:
    # Two drawn labels over one line: the one centred on it keeps it.
    centred = {"dim_line": [[0, 10], [100, 10]], "orientation": "h", "box": [45, 0, 55, 8]}
    found = {"dim_line": [[0, 10], [100, 10]], "orientation": "h", "box": [5, 0, 15, 8]}
    assert dl._span(centred) is not None and dl._span(found) is not None
    assert dl._span(centred)[4] < dl._span(found)[4]  # type: ignore[index]


# ---- the `1` shape on the vectors ----------------------------------------------------------------


def test_a_ones_shape_is_checked_on_its_vectors() -> None:
    narrow = _label(_ink('17"', 100, 300))
    assert dl._vector_ones(narrow, Decimal(17), 0) is True
    # A `1` drawn as wide as a 7 (a hand-lettered 7): refused.
    wide_one = [_shown((((100, 306), (103, 306), (100.5, 300)),))]
    seven = _ink("7", 104.5, 300)
    ticks = [_shown((((110, 306), (110, 304)),)), _shown((((111, 306), (111, 304)),))]
    assert dl._vector_ones(_label(wide_one + seven + ticks), Decimal(17), 0) is False
    # No row laid out as the value's digits: the engines' check decides.
    assert dl._vector_ones(narrow, Decimal("17.5"), 0) is None
