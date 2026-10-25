"""The reference's lettering exported as outlines (final-plan change #5).

Some reference drawings (a casework maker's set, an architect's set plotted from CAD) print every
character as a filled shape, not as text: the foot and inch marks are small filled wedges, many
dimensions are written up the page beside a vertical dimension line, and a fraction is printed
diagonally (a numerator raised before a long slash, a denominator lowered after it). This file
draws made-up numbers that way: digits in the hand-lettering stroke font of `test_hand_labels`,
marks as filled wedges, fractions diagonal. The OCR engines are scripted; nothing here needs an
engine installed or calls a model.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from app.review.assistant import dim_labels as dl
from app.review.assistant import hand_labels as hl
from app.review.assistant import ink_labels
from tests.review.assistant.test_dim_labels import HALVES, _line, _pdf
from tests.review.assistant.test_hand_labels import HAND, PAGE_H, _Path

_SCALE = 5.0 / 6  # a 5-point letter height (6 grid units)

type _Shape = tuple[list[tuple[float, float]], bool]
"""One path's points (PDF space, y up) and whether it is filled."""


def _outlined(
    text: str, x: float, y: float, up: bool = False, *, bent: bool = False
) -> list[_Shape]:
    """`text` lettered as a CAD font plotted as outlines, baseline-left at `(x, y)` (PDF space);
    `up` reads up the page. `{N/D}` is a diagonal fraction, `=` an equals sign. `bent`: the marks
    are an SHX stroke font's instead (short bent strokes standing a little above the digits)."""
    out: list[_Shape] = []
    advance = 0.0

    def place(along: float, rise: float) -> tuple[float, float]:
        return (x - rise, y + along) if up else (x + along, y + rise)

    def shape(points: Sequence[tuple[float, float]], filled: bool, size: float = 1.0) -> None:
        out.append(
            ([place(advance + gx * _SCALE, gy * _SCALE * size) for gx, gy in points], filled)
        )

    def wedge() -> None:  # a tick: a small filled wedge at the top, or a bent stroke above it
        if bent:
            shape(((0.4, 7.2), (0.5, 7.0), (0.45, 6.6), (0.3, 6.1), (0.1, 5.7)), False)
        else:
            shape(((0, 6), (0.5, 6), (0.32, 4), (0.18, 4)), True)

    def digit(char: str, dx: float, dy: float, size: float) -> None:
        for line in HAND[char]:
            out.append(
                (
                    [
                        place(advance + (dx + gx * size) * _SCALE, (dy + gy * size) * _SCALE)
                        for gx, gy in line
                    ],
                    False,
                )
            )

    index = 0
    while index < len(text):
        char = text[index]
        if char == "{":
            end = text.index("}", index)
            top, bottom = text[index + 1 : end].split("/")
            # Small digits: the numerator raised to the letter's top, the denominator lowered
            # below the baseline; one straight slash rising across both.
            for k, d in enumerate(top):
                digit(d, k * 2.8, 3.2, 0.6)
            low = len(top) * 2.8 + 0.2
            for k, d in enumerate(bottom):
                digit(d, low + k * 2.8, -0.8, 0.6)
            span = low + len(bottom) * 2.8
            shape(((0.1, -0.8), (span - 0.1, 6.8)), False)
            advance += span * _SCALE + 1.3
            index = end + 1
            continue
        if char == " ":
            advance += 1.6
        elif char == "'":
            wedge()
            advance += 1.5
        elif char == '"':
            wedge()
            advance += 1.1
            wedge()
            advance += 1.5
        elif char == "-":
            shape(((0, 2.6), (1.9, 2.6), (1.9, 2.95), (0, 2.95)), True)
            advance += 1.6 + 1.3
        elif char == "=":
            shape(((0, 1.8), (3, 1.8), (3, 2.2), (0, 2.2)), True)
            shape(((0, 3.6), (3, 3.6), (3, 4.0), (0, 4.0)), True)
            advance += 2.5 + 1.3
        else:
            digit(char, 0, 0, 1.0)
            right = max(gx for line in HAND[char] for gx, _gy in line)
            advance += right * _SCALE + 1.3
        index += 1
    return out


def _shown(shapes: Sequence[_Shape]) -> list[_Path]:
    paths = []
    for points, filled in shapes:
        flipped = tuple((px, PAGE_H - py) for px, py in points)
        xs, ys = [p[0] for p in flipped], [p[1] for p in flipped]
        paths.append(_Path((min(xs), min(ys), max(xs), max(ys)), (flipped,), filled))
    return paths


def _found(text: str, up: bool = False) -> list[hl.Lettered]:
    return hl.lettered(_shown(_outlined(text, 100, 300, up)))


def _one(text: str, up: bool = False) -> hl.Lettered:
    (item,) = _found(text, up)
    return item


# ---- the vectors ---------------------------------------------------------------------------------


def test_marks_drawn_as_filled_wedges_are_foot_and_inch_marks() -> None:
    item = _one("2'-10\"")
    assert item.turn == 0
    assert item.layout == (1, 2, 0, 0)
    assert hl.value(item, "2'-10\"") == Decimal(34)


def test_an_shx_fonts_bent_marks_above_the_digits_are_foot_and_inch_marks() -> None:
    (item,) = hl.lettered(_shown(_outlined("3'-0\"", 100, 300, bent=True)))
    assert item.layout == (1, 1, 0, 0)
    (item,) = hl.lettered(_shown(_outlined('24"', 100, 300, up=True, bent=True)))
    assert (item.turn, item.layout) == (90, (0, 2, 0, 0))


def test_a_dimension_written_up_the_page() -> None:
    item = _one("2'-10\"", up=True)
    assert item.turn == 90
    assert item.layout == (1, 2, 0, 0)
    # Read the other way round it is no dimension: the inch mark would come first.
    assert all(found.turn == 90 for found in _found("2'-10\"", up=True))


def test_two_dimensions_of_a_chain_on_one_line_are_each_read() -> None:
    found = _found('6" 1\'-4"', up=True)
    assert sorted(item.layout for item in found) == [(0, 1, 0, 0), (1, 1, 0, 0)]


def test_a_diagonal_fraction_is_read_as_its_parts() -> None:
    assert _one("1'-10 {1/8}\"").layout == (1, 2, 1, 1)
    assert _one('9{7/8}"').layout == (0, 1, 1, 1)
    assert _one('{1/8}"').layout == (0, 0, 1, 1)
    item = _one("1'-10 {1/8}\"", up=True)
    assert item.turn == 90
    assert hl.value(item, "1'-10 1/8\"") == Decimal("22.125")
    assert hl.value(item, "1-101/8") == Decimal("22.125")  # the digits laid out by the vectors
    assert hl.value(item, "1'-10 1/3\"") is None  # a fraction no drawing prints
    assert hl.value(item, "1'-10\"") is None  # the fraction's digits missing: no reading
    assert hl.text_of(item, Decimal("22.125")) == "1'-10 1/8\""


def test_the_picture_moves_a_diagonal_fraction_onto_the_line() -> None:
    item = _one('9{7/8}"')
    tokens = hl.tokens(item)
    assert [t.kind for t in tokens] == ["glyph", "fraction", "mark", "mark"]
    fraction = tokens[1]
    assert len(fraction.glyphs) == 1 and len(fraction.below) == 1  # the slash is drawn anew
    picture = ink_labels.row_picture(tokens, 0, 40)
    assert picture is not None and picture.min() == 0


def test_a_fraction_whose_part_reaches_across_the_slash_is_none() -> None:
    # The `7` lowered to the baseline: it no longer keeps above the slash's middle, so the
    # vectors do not show a fraction, and the row is no dimension.
    moved = []
    for points, filled in _outlined('9{7/8}"', 100, 300):
        # the numerator's strokes are the ones starting 3.2 grid units up (2.67 points)
        if not filled and min(py for _px, py in points) >= 300 + 2.66:
            points = [(px, py - 2.5) for px, py in points]
        moved.append((points, filled))
    assert hl.lettered(_shown(moved)) == []


def test_two_fractions_of_a_chain_on_one_line_are_each_read() -> None:
    found = _found('{1/8}" 1{1/2}"', up=True)
    assert sorted(item.layout for item in found) == [(0, 0, 1, 1), (0, 1, 1, 1)]


# ---- through `dim_labels` ------------------------------------------------------------------------


class _Scripted:
    """An OCR engine that reads the same line in every picture."""

    def __init__(self, name: str, line: str, *, marks: bool = True) -> None:
        self.name, self.line, self.reads_inch_marks = name, line, marks

    def __call__(self, picture: Any, *, single_line: bool) -> list[str]:
        return [self.line]


def _ops(shapes: Sequence[_Shape]) -> bytes:
    out = []
    for points, filled in shapes:
        parts = [f"{x:.3f} {y:.3f} {'m' if n == 0 else 'l'}" for n, (x, y) in enumerate(points)]
        if filled:
            out.append("0 0 0 rg " + " ".join(parts) + " h f")
        else:
            out.append("0 0 0 RG 0.3 w " + " ".join(parts) + " S")
    return (" ".join(out) + " ").encode()


def _vertical(x: float, y0: float, y1: float, reach: float = 50) -> bytes:
    """A vertical dimension line at `x` (PDF space) with extension lines left to `x - reach`."""
    return (
        _line(x, y0 - 1.5, x, y1 + 1.5)
        + _line(x - reach, y0, x + 3, y0)
        + _line(x - reach, y1, x + 3, y1)
    )


def _labels(path: Path, readers: Sequence[Any]) -> list[dict[str, Any]]:
    found = dl.page_labels(path, 1, HALVES, drawn_budget_s=60, readers=readers)
    return [label for label in found.labels if label["route"] == "drawn"]


def test_a_reference_dimension_written_up_the_page_becomes_a_label(tmp_path: Path) -> None:
    path = tmp_path / "up.pdf"
    path.write_bytes(
        _pdf([_vertical(102, 280, 340) + _ops(_outlined("2'-10\"", 100, 295, up=True))])
    )
    both = [_Scripted("RapidOCR", "2-10", marks=False), _Scripted("Tesseract", "2'-10\"")]
    (label,) = _labels(path, both)
    assert label["side"] == "architect"
    assert (label["text"], label["inches"], label["orientation"]) == ("2'-10\"", "34", "v")


def test_the_two_engines_must_still_agree(tmp_path: Path) -> None:
    path = tmp_path / "up.pdf"
    path.write_bytes(
        _pdf([_vertical(102, 280, 340) + _ops(_outlined("2'-10\"", 100, 295, up=True))])
    )
    other = [_Scripted("RapidOCR", "2-10", marks=False), _Scripted("Tesseract", "2'-11\"")]
    assert _labels(path, other) == []


def test_a_scale_notes_number_is_no_dimension(tmp_path: Path) -> None:
    # `1" = 1'-0"` over a line: the `1"` has a line and room, but an `=` stands beside it.
    path = tmp_path / "scale.pdf"
    body = _line(80, 298, 160, 298) + _ops(_outlined('1" = 1\'-0"', 100, 300))
    path.write_bytes(_pdf([body]))
    both = [_Scripted("RapidOCR", "1", marks=False), _Scripted("Tesseract", '1"')]
    assert all(label["text"] != '1"' for label in _labels(path, both))


def test_a_bare_stroke_with_two_dashes_is_no_dimension() -> None:
    # A piece of line work (one straight stroke) with two short dashes at its top: shaped like a
    # bare-stroke `1"`, but nothing in it is a digit only a letter has; never a label.
    shapes: list[_Shape] = [
        ([(100.0, 300.0), (100.0, 305.0)], False),
        ([(101.0, 305.0), (101.0, 303.4)], False),
        ([(102.0, 305.0), (102.0, 303.4)], False),
    ]
    assert hl.lettered(_shown(shapes)) == []
    assert hl.lettered(_shown(_outlined("1'-1\"", 100, 300))) == []
    assert _one("1'-4\"").layout == (1, 1, 0, 0)  # a digit with a shape of its own


def test_hatching_with_two_lines_at_its_edge_is_no_dimension() -> None:
    # A detail's hatched block (its outline and many short lines across it) with two short lines
    # beside its top: shaped like a digit and an inch mark, never a label.
    outline = [(100.0, 300.0), (104.0, 300.0), (104.0, 306.0), (100.0, 306.0), (100.0, 300.0)]
    block: list[_Shape] = [([a, b], False) for a, b in pairwise(outline)]
    block += [([(100.0 + k * 0.4, 300.0), (100.0 + k * 0.4, 306.0)], False) for k in range(1, 10)]
    ticks: list[_Shape] = [
        ([(105.0, 306.0), (105.0, 303.5)], False),
        ([(105.3, 306.0), (105.3, 303.5)], False),
    ]
    assert hl.lettered(_shown(block + ticks)) == []


def test_a_reference_fraction_written_up_the_page_becomes_a_label(tmp_path: Path) -> None:
    path = tmp_path / "up.pdf"
    lettering = _ops(_outlined("1'-10 {1/8}\"", 100, 295, up=True))
    path.write_bytes(_pdf([_vertical(102, 280, 340) + lettering]))
    both = [_Scripted("RapidOCR", "1-10 1/8", marks=False), _Scripted("Tesseract", "1'-10 1/8\"")]
    (label,) = _labels(path, both)
    assert label["side"] == "architect"
    assert (label["text"], label["inches"], label["orientation"]) == ("1'-10 1/8\"", "22.125", "v")
