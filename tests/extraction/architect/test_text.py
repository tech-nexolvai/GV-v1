"""Putting the architect's printed text back together from single characters (#1052).

Verification for: `extraction/architect/text.py`. Synthetic characters only.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from extraction.architect.text import (
    Orientation,
    ScaleKind,
    TextChar,
    TextSettings,
    find_printed,
    orientation_of,
)
from extraction.geometry.rows import Box

SETTINGS = TextSettings(
    same_line_em=Decimal("0.3"),
    space_em=Decimal("0.15"),
    phrase_em=Decimal("1.0"),
    duplicate_em=Decimal("0.2"),
    same_size_fraction=Decimal("0.1"),
)


def _line(
    text: str, x: float, top: float = 100, *, width: float = 4, height: float = 6
) -> list[TextChar]:
    """Upright characters, `width` apart; a space in `text` is a gap of one character."""
    chars: list[TextChar] = []
    for index, character in enumerate(text):
        if character == " ":
            continue
        left = Decimal(str(x + index * width))
        chars.append(
            TextChar(
                character,
                Box(
                    left,
                    Decimal(str(top)),
                    left + Decimal(str(width - 0.5)),
                    Decimal(str(top + height)),
                ),
                Orientation.UPRIGHT,
            )
        )
    return chars


def test_a_label_set_with_spaces_and_typographic_marks_is_one_dimension() -> None:
    printed = find_printed(_line('3’ − 6"', 10), SETTINGS)

    (dimension,) = printed.dimensions
    assert dimension.reading.inches == Fraction(42)
    assert dimension.is_feet_and_inches


def test_two_labels_that_touch_are_two_dimensions() -> None:
    printed = find_printed(_line("1' -0\"1' -0\"", 10), SETTINGS)

    assert [d.reading.inches for d in printed.dimensions] == [Fraction(12), Fraction(12)]
    assert printed.dimensions[0].box.x1 <= printed.dimensions[1].box.x0


def test_a_character_printed_twice_on_itself_is_read_once() -> None:
    chars = _line("2' - 6\"", 10)
    doubled = chars + [
        TextChar(
            c.text, Box(c.box.x0 + Decimal("0.2"), c.box.top, c.box.x1, c.box.bottom), c.orientation
        )
        for c in chars
    ]

    (dimension,) = find_printed(doubled, SETTINGS).dimensions
    assert dimension.reading.inches == Fraction(30)


def test_characters_of_two_sizes_are_held_as_a_stacked_fraction() -> None:
    big = _line("21", 10)
    small = _line('8"', 18, top=100, width=3, height=3)

    (dimension,) = find_printed(big + small, SETTINGS).dimensions
    assert dimension.reading.inches is None
    assert "size" in (dimension.reading.held_reason or "")


def test_a_note_with_a_number_in_it_is_held() -> None:
    (dimension,) = find_printed(_line('6" WOODEN', 10), SETTINGS).dimensions

    assert dimension.reading.inches is None


def test_scale_notes_are_found_with_their_wide_gaps_and_not_read_as_dimensions() -> None:
    chars = _line("ID 8.3", 10) + _line('1/2"', 60) + _line("=", 90) + _line("1'-0\"", 110)

    printed = find_printed(chars, SETTINGS)

    (note,) = printed.scales
    assert note.kind is ScaleKind.ARCHITECTURAL
    assert note.paper_per_real == Fraction(1, 24)
    assert printed.dimensions == ()


def test_a_ratio_scale_is_found() -> None:
    (note,) = find_printed(_line("1:10", 10), SETTINGS).scales

    assert note.kind is ScaleKind.RATIO
    assert note.paper_per_real == Fraction(1, 10)


def test_a_centre_line_mark_is_found_where_it_is() -> None:
    printed = find_printed(_line("C L", 40) + _line("CLEAT", 10, top=200), SETTINGS)

    (mark,) = printed.centre_marks
    assert mark.x0 == Decimal(40)


def test_sideways_text_reads_in_its_own_order() -> None:
    """Turned a quarter anticlockwise, a label reads from the bottom of the page up."""
    chars = []
    for index, character in enumerate("2'-5\""):
        bottom = Decimal(200 - index * 4)
        chars.append(
            TextChar(character, Box(Decimal(50), bottom - 4, Decimal(56), bottom), Orientation.UP)
        )

    (dimension,) = find_printed(chars, SETTINGS).dimensions
    assert dimension.reading.inches == Fraction(29)
    assert dimension.orientation is Orientation.UP


def test_orientation_comes_from_the_text_matrix() -> None:
    assert orientation_of((1, 0, 0, 1, 0, 0), True) is Orientation.UPRIGHT
    assert orientation_of((0, 1, -1, 0, 0, 0), False) is Orientation.UP
    assert orientation_of((0, -1, 1, 0, 0, 0), False) is Orientation.DOWN
    assert orientation_of((0.7, 0.7, -0.7, 0.7, 0, 0), False) is None
