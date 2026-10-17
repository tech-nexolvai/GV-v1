"""`ink_labels` (#1561): the vendor's drawn numbers split into labels and re-typeset on one line.

The ink here is synthetic CAD stroke lettering (`stroke_text`): every character its own stroked
path, inch marks as two ticks, stacked fractions as numerator, bar and denominator. No OCR engine
is needed: the tiny `StrokeReader` reads the re-typeset pictures by matching the same font.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.review.assistant import ink_labels
from tests.review.assistant.stroke_text import StrokeReader, stroke_paths

PAGE_H = 400.0


@dataclass(frozen=True, slots=True)
class _Path:
    """An `ink_labels.InkPath` in shown points (origin top-left)."""

    box: tuple[float, float, float, float]
    polylines: tuple[tuple[tuple[float, float], ...], ...]
    filled: bool = False
    width: float = 0.3


def _shown(lines: tuple[tuple[tuple[float, float], ...], ...], filled: bool = False) -> _Path:
    flipped = tuple(tuple((x, PAGE_H - y) for x, y in line) for line in lines)
    xs = [x for line in flipped for x, _ in line]
    ys = [y for line in flipped for _, y in line]
    return _Path((min(xs), min(ys), max(xs), max(ys)), flipped, filled)


def _ink(text: str, x: float, y: float, turn: bool = False) -> list[_Path]:
    return [_shown(path.lines) for path in stroke_paths(text, x, y, 6.0, turn)]


def _dashed_line(x: float, y0: float, y1: float) -> list[_Path]:
    """A dashed extension line: straight strokes 1.2 pt long, 0.8 pt apart, going up the page."""
    out, y = [], y0
    while y + 1.2 <= y1:
        out.append(_shown((((x, y), (x, y + 1.2)),)))
        y += 2.0
    return out


def _arrowhead(x: float, y: float) -> _Path:
    return _shown((((x, y), (x + 2.5, y + 0.8), (x + 2.5, y - 0.8), (x, y)),), filled=True)


READ = StrokeReader("test")


def _read(label: ink_labels.Label) -> list[str]:
    out = []
    for turn in ink_labels.turns(label):
        for row in ink_labels.rows(label, turn):
            picture = ink_labels.row_picture(row, turn, 48)
            assert picture is not None
            out += READ(picture, single_line=True)
    return out


def test_labels_split_the_ink_and_leave_out_dashed_lines_and_arrowheads() -> None:
    paths = (
        _ink('63{1/2}"', 100, 300)
        + _ink('72"', 160, 300)
        + _dashed_line(140, 290, 310)
        + [_arrowhead(150, 305)]
    )
    found = ink_labels.labels(paths, (95, 80, 185, 106))
    assert [_read(label) for label in found] == [['63 1/2"'], ['72"']]


def test_a_stacked_fraction_is_one_token_with_its_numerator_and_denominator() -> None:
    (label,) = ink_labels.labels(_ink('63{1/2}"', 100, 300), (95, 90, 130, 106))
    (row,) = ink_labels.rows(label, 0)
    assert [token.kind for token in row] == ["glyph", "glyph", "fraction", "mark", "mark"]
    fraction = row[2]
    assert (len(fraction.glyphs), len(fraction.below)) == (1, 1)


def test_the_re_typeset_row_is_one_line_of_text() -> None:
    (label,) = ink_labels.labels(_ink('81{3/4}"', 100, 300), (95, 90, 130, 106))
    (row,) = ink_labels.rows(label, 0)
    picture = ink_labels.row_picture(row, 0, 48)
    assert picture is not None
    ink_rows = np.nonzero((picture < 128).any(axis=1))[0]
    # Everything sits on one line, no taller than the row and its marks.
    assert ink_rows.max() - ink_rows.min() <= 48 + 6
    assert READ(picture, single_line=True) == ['81 3/4"']


def test_a_number_written_up_the_page_reads_turned() -> None:
    (label,) = ink_labels.labels(_ink('27{1/4}"', 200, 100, turn=True), (190, 260, 206, 305))
    assert ink_labels.turns(label) == (90,)
    assert _read(label) == ['27 1/4"']


def test_an_upright_label_reads_upright() -> None:
    (label,) = ink_labels.labels(_ink('46"', 100, 300), (95, 90, 120, 106))
    assert ink_labels.turns(label) == (0,)


def test_an_inch_mark_can_be_left_out_but_a_foot_mark_never() -> None:
    (inches,) = ink_labels.labels(_ink('34"', 100, 300), (95, 90, 120, 106))
    (row,) = ink_labels.rows(inches, 0)
    assert ink_labels.inch_mark_at_end(row, 0) == 2
    bare = ink_labels.row_picture(row, 0, 48, inch_mark=False)
    assert bare is not None and READ(bare, single_line=True) == ["34"]
    (feet,) = ink_labels.labels(_ink("3'", 100, 300), (95, 90, 120, 106))
    (row,) = ink_labels.rows(feet, 0)
    assert ink_labels.inch_mark_at_end(row, 0) == 0


def test_a_label_the_box_clips_is_read_whole() -> None:
    (label,) = ink_labels.labels(_ink('864"', 100, 300), (112, 92, 140, 106))
    assert _read(label) == ['864"']


def test_ticks_alone_are_not_a_label() -> None:
    ticks = [
        _shown((((100.0, 300.0), (102.0, 302.0)),)),
        _shown((((110.0, 300.0), (112.0, 302.0)),)),
    ]
    assert ink_labels.labels(ticks, (95, 90, 120, 106)) == []
