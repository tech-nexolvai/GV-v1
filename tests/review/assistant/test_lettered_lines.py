"""A hand-lettered reference dimension's own line when it stands off the text.

CAD sets plotted in an SHX stroke font letter a size a letter height or more off its dimension
line (above a level line, beside an upright line it reads up), with small slash ticks at the
line's ends. The label reader looked for a line through the text or right by it only, so these
sizes were found but never kept. Made-up line work and lettering (`test_outlined_labels`); the OCR
engines are scripted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.review.assistant import dim_labels as dl
from app.review.assistant import hand_labels as hl
from tests.review.assistant.test_dim_labels import _line, _pdf
from tests.review.assistant.test_hand_labels import PAGE_H
from tests.review.assistant.test_outlined_labels import (
    _labels,
    _ops,
    _outlined,
    _Scripted,
    _Shape,
    _shown,
)

_BOTH = [_Scripted("RapidOCR", "2-10", marks=False), _Scripted("Tesseract", "2'-10\"")]


def _upright(x: float, y0: float, y1: float, *, ticks: tuple[bool, bool] = (True, True)) -> bytes:
    """An upright dimension line at `x` (PDF space) from `y0` to `y1`, extension lines reaching
    right of it, and a slash tick across each end that `ticks` asks for."""
    out = (
        _line(x, y0 - 1.5, x, y1 + 1.5)
        + _line(x - 3, y0, x + 40, y0)
        + _line(x - 3, y1, x + 40, y1)
    )
    for y, tick in zip((y0, y1), ticks, strict=True):
        if tick:
            out += _line(x - 1.8, y - 1.8, x + 1.8, y + 1.8)
    return out


def _page(tmp_path: Path, body: bytes) -> Path:
    path = tmp_path / "beside.pdf"
    path.write_bytes(_pdf([body]))
    return path


def _up(text: str = "2'-10\"") -> bytes:
    # Written up the page, its letters from x 94 to 100 (PDF space), from y 295 up.
    return _ops(_outlined(text, 100, 295, up=True))


def test_a_line_a_letter_beside_the_text_with_ticks_at_both_ends_is_its_own(
    tmp_path: Path,
) -> None:
    (label,) = _labels(_page(tmp_path, _upright(106.5, 280, 340) + _up()), _BOTH)
    assert (label["text"], label["inches"], label["orientation"]) == ("2'-10\"", "34", "v")
    (x0, _y0), (x1, _y1) = label["dim_line"]
    assert x0 == x1 == 106.5
    assert label["span_pt"] == 60.0
    assert "_beside" not in label


def test_the_line_right_by_the_text_is_still_found_first(tmp_path: Path) -> None:
    (label,) = _labels(_page(tmp_path, _upright(102, 280, 340) + _up()), _BOTH)
    assert label["dim_line"][0][0] == 102


def test_a_line_with_a_tick_at_one_end_only_is_no_dimension_line(tmp_path: Path) -> None:
    body = _upright(106.5, 280, 340, ticks=(True, False)) + _up()
    assert _labels(_page(tmp_path, body), _BOTH) == []


def test_a_line_too_far_from_the_text_is_not_its_own(tmp_path: Path) -> None:
    assert _labels(_page(tmp_path, _upright(115, 280, 340) + _up()), _BOTH) == []


def test_two_lines_one_each_side_of_the_text_link_neither(tmp_path: Path) -> None:
    body = _upright(106.5, 280, 340) + _upright(88, 280, 340) + _up()
    assert _labels(_page(tmp_path, body), _BOTH) == []


def test_another_line_between_the_text_and_the_line_takes_it_away(tmp_path: Path) -> None:
    body = _upright(106.5, 280, 340) + _line(103.5, 290, 103.5, 330) + _up()
    assert _labels(_page(tmp_path, body), _BOTH) == []


def test_a_level_dimension_a_letter_above_its_line(tmp_path: Path) -> None:
    # `2'-10"` lettered level from x 100, its baseline at y 300; the line 6 points below it.
    line = (
        _line(80, 294, 160, 294)
        + _line(80, 290, 80, 330)
        + _line(160, 290, 160, 330)
        + _line(78.2, 292.2, 81.8, 295.8)
        + _line(158.2, 292.2, 161.8, 295.8)
    )
    (label,) = _labels(_page(tmp_path, line + _ops(_outlined("2'-10\"", 100, 300))), _BOTH)
    assert (label["inches"], label["orientation"]) == ("34", "h")
    assert label["span_pt"] == 80.0


# ---- the end of a fraction is no dimension -------------------------------------------------------


def _stacked() -> list[_Shape]:
    """`3"` raised over a `4` a hair below it: the stacked `3/4"` of a CAD font, whose `3"` the
    vectors spell as a dimension of its own."""
    return _outlined('3"', 100, 306) + _outlined("4", 100, 299.5)


def test_a_stacked_fractions_numerator_is_the_end_of_a_fraction() -> None:
    shapes = _stacked()
    (item,) = hl.lettered(_shown(_outlined('3"', 100, 306)))
    assert hl.in_fraction(item, _shown(shapes)) is True


def test_a_digit_after_a_slash_is_the_end_of_a_fraction() -> None:
    # `3/4"` whose slash is plotted as a plain line (no character-sized path): the vectors spell
    # its `4"` alone.
    (item,) = hl.lettered(_shown(_outlined('4"', 100.5, 300)))
    slash = ((97.0, PAGE_H - 299.0), (99.5, PAGE_H - 306.5))
    assert hl.in_fraction(item, [], [slash]) is True
    assert hl.in_fraction(item, [], [((90.0, PAGE_H - 299.0), (92.5, PAGE_H - 306.5))]) is False


def test_a_dimensions_own_ticks_under_it_are_no_fraction() -> None:
    # A `6"` over a short span: the line's slash ticks stand right under its digit.
    ticks: list[_Shape] = [
        ([(99.0, 295.0), (102.0, 298.6)], False),
        ([(99.3, 295.0), (102.3, 298.6)], False),
    ]
    shapes = [*_outlined('6"', 100, 300), *ticks]
    (item,) = hl.lettered(_shown(_outlined('6"', 100, 300)))
    assert hl.in_fraction(item, _shown(shapes)) is False


def test_a_tag_further_off_is_no_fraction() -> None:
    shapes = _outlined('3"', 100, 306) + _outlined("2", 100, 297.5)
    (item,) = hl.lettered(_shown(_outlined('3"', 100, 306)))
    assert hl.in_fraction(item, _shown(shapes)) is False


def test_the_end_of_a_fraction_beside_a_line_is_never_a_label(tmp_path: Path) -> None:
    line = (
        _line(80, 293, 160, 293)
        + _line(80, 290, 80, 330)
        + _line(160, 290, 160, 330)
        + _line(78.2, 291.2, 81.8, 294.8)
        + _line(158.2, 291.2, 161.8, 294.8)
    )
    both = [_Scripted("RapidOCR", "3", marks=False), _Scripted("Tesseract", '3"')]
    assert _labels(_page(tmp_path, line + _ops(_stacked())), both) == []


# ---- the view's scale ----------------------------------------------------------------------------


def _level(value: str, x0: float, x1: float, y: float, **more: Any) -> dict[str, Any]:
    return {
        "inches": value,
        "box": [x0, y - 8, x0 + 10, y - 2],
        "side": "architect",
        "route": "text",
        "dim_line": [[x0, y], [x1, y]],
        "span_pt": x1 - x0,
        **more,
    }


def test_a_line_beside_drawn_to_another_length_at_its_views_scale_is_dropped() -> None:
    # One view at 1 point per inch (three labels agree), and two labels linked beside their text:
    # one drawn 24 long for 24", kept; one drawn 40 long for 24", dropped.
    others = [_level("20", 0, 20, 10), _level("30", 0, 30, 30), _level("40", 0, 40, 50)]
    good = _level("24", 0, 24, 70, route="drawn", _beside=True)
    bad = _level("24", 0, 40, 90, route="drawn", _beside=True)
    frame = [((0.0, 0.0), (0.0, 100.0)), ((0.0, 100.0), (60.0, 100.0))]
    lines = [
        ((float(a[0]), float(a[1])), (float(b[0]), float(b[1])))
        for a, b in (label["dim_line"] for label in [*others, good, bad])
    ]
    kept = dl._beside_on_scale([good, bad], others, frame + lines)
    assert kept == [good]
    assert "_beside" not in good


def test_with_no_scale_to_tell_a_line_beside_is_kept() -> None:
    lonely = _level("24", 0, 40, 90, route="drawn", _beside=True)
    assert dl._beside_on_scale([lonely], [], [((0.0, 90.0), (40.0, 90.0))]) == [lonely]
