"""Finding a stacked fraction by its bar (#541, #735).

The shapes here are the client's plotter shapes, measured on a real stacked fraction and moved to the
origin: a numerator 3.6 pt wide and 5.5 pt tall, a bar 3.8 pt long drawn as a zero-width stroke, a
denominator drawn as two strokes (a body and a stem), and an inch mark of two short ticks. No client
dimension appears; the digits are synthetic.

The detector's first version passed every test it had and could never fire on a real drawing,
because its tests handed it boxes a real reader never produced (#735). So these hand it what the
stamp actually holds — every path, including the orphaned bar and denominator.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from extraction.glyph_bands import FractionBarGeometry, GlyphBox, stacked_fractions


def _d(*values: str) -> GlyphBox:
    x0, y0, x1, y1 = (Decimal(value) for value in values)
    return (x0, y0, x1, y1)


#: The values `scripts/demo.sh` states, measured on the client set (#735).
GEOMETRY = FractionBarGeometry(
    bar_thickness_max_pt=Decimal("0.3"),
    bar_length_min_pt=Decimal(1),
    reach_pt=Decimal(3),
    glyph_min_pt=Decimal(1),
    glyph_max_pt=Decimal(12),
    proportion_max=Decimal("2.5"),
)

NUMERATOR = _d("0.1", "6.4", "3.7", "11.9")  # one path, as the plotter draws a `3`
BAR = _d("0", "5.5", "3.8", "5.5")  # a zero-width stroke: flat
DENOMINATOR_BODY = _d("0", "1.0", "3.8", "4.6")  # the `4` is two strokes
DENOMINATOR_STEM = _d("2.5", "-0.8", "2.5", "4.6")
INCH_TICKS = [_d("5.6", "7.7", "6.1", "9.3"), _d("7.2", "7.7", "7.7", "9.3")]

FRACTION = [NUMERATOR, BAR, DENOMINATOR_BODY, DENOMINATOR_STEM, *INCH_TICKS]


def _shifted(boxes: list[GlyphBox], dx: str) -> list[GlyphBox]:
    step = Decimal(dx)
    return [(box[0] + step, box[1], box[2] + step, box[3]) for box in boxes]


def test_a_stacked_fraction_is_found_as_the_plotter_draws_it() -> None:
    """**The case #735 exists for.** Bar, numerator above, denominator below in two strokes.

    The box that comes back is the whole fraction — bar, numerator and denominator — so a crop that
    shows any of it can be told apart from one that shows none.
    """
    found = stacked_fractions(FRACTION, geometry=GEOMETRY)

    assert found == (_d("0", "-0.8", "3.8", "11.9"),)


def test_the_whole_number_beside_it_changes_nothing() -> None:
    """`28 3/4"`: two full-height digits to the left. They share no position with the bar, so the
    detection is the same fraction and does not swallow the whole number."""
    whole = [_d("-9", "0", "-5", "11.9"), _d("-4.5", "0", "-0.5", "11.9")]

    found = stacked_fractions([*whole, *FRACTION], geometry=GEOMETRY)

    assert found == (_d("0", "-0.8", "3.8", "11.9"),)


def test_a_numerator_one_drawn_as_a_single_stroke_is_still_found() -> None:
    """**Recall, in the case that matters most.** `1/2`, `1/4`, `1/8` and `1/16` are the commonest
    fractions in the trade, and a stroke-font `1` can be one vertical stroke with no width. Requiring
    a two-dimensional glyph on *both* sides would miss every one of them; the rule asks for one side.
    """
    one = _d("1.9", "6.4", "1.9", "11.9")

    found = stacked_fractions([one, BAR, DENOMINATOR_BODY, DENOMINATOR_STEM], geometry=GEOMETRY)

    assert len(found) == 1


def test_a_hyphen_on_one_line_is_not_a_bar() -> None:
    """`2' - 10"`: the hyphen is a flat stroke, but nothing sits above it or below it."""
    label = [
        _d("0", "0", "3.6", "5.5"),
        _d("4.5", "3.5", "5.0", "5.5"),  # the foot mark
        _d("6.5", "2.7", "8.5", "2.7"),  # the hyphen
        _d("10", "0", "13.6", "5.5"),
        _d("14", "0", "17.6", "5.5"),
    ]

    assert stacked_fractions(label, geometry=GEOMETRY) == ()


def test_two_lines_of_text_are_not_a_fraction() -> None:
    """The false positive `glyph_bands` used to be exposed to: any two-line note has glyphs one above
    another. With no bar between them there is nothing to find."""
    note = [_d("0", "7", "3.6", "12.5"), _d("0", "0", "3.6", "5.5")]

    assert stacked_fractions(note, geometry=GEOMETRY) == ()


def test_one_dash_of_a_dashed_line_is_not_a_bar() -> None:
    """**The false alarm isolation removes.** On the client's page 9, hatch strokes had a shape above
    and below them just as a bar does. A bar stands alone; a dash has a neighbour on its own line a
    short gap away."""
    dashed = [*FRACTION, _d("5.0", "5.5", "8.8", "5.5")]

    assert stacked_fractions(dashed, geometry=GEOMETRY) == ()


def test_a_line_on_the_bar_far_beyond_reach_does_not_matter() -> None:
    far = [*FRACTION, _d("20", "5.5", "40", "5.5")]

    assert len(stacked_fractions(far, geometry=GEOMETRY)) == 1


def test_the_same_stroke_drawn_twice_is_still_one_bar() -> None:
    """PDFs embolden by drawing a path twice. The copy is not a neighbour on the bar's line."""
    doubled = [*FRACTION, BAR]

    assert len(stacked_fractions(doubled, geometry=GEOMETRY)) == 1


def test_a_symbol_of_arc_pieces_is_not_a_fraction() -> None:
    """**The false alarm the one-side shape rule removes.** An electrical-outlet symbol is a flat
    stroke between two circles, and on the client's sheets each circle is drawn as dozens of short
    arc pieces — none with real width *and* height. 28 of them passed every other rule (#735)."""
    arcs_above = [
        _d(str(Decimal("0.4") * index), "6.4", str(Decimal("0.4") * index + Decimal("0.3")), "7.4")
        for index in range(9)
    ]
    arcs_below = [
        _d(str(Decimal("0.4") * index), "3.6", str(Decimal("0.4") * index + Decimal("0.3")), "4.6")
        for index in range(9)
    ]

    assert stacked_fractions([*arcs_above, BAR, *arcs_below], geometry=GEOMETRY) == ()


def test_a_shape_far_larger_than_the_other_is_not_a_digit_over_a_digit() -> None:
    tall = _d("0.1", "6.4", "3.7", "11.9")
    tiny = _d("1.0", "3.0", "3.0", "4.6")  # 1.6 pt tall against 5.5 pt: beyond 2.5x

    assert stacked_fractions([tall, BAR, tiny], geometry=GEOMETRY) == ()


def test_shapes_off_to_one_side_of_the_bar_are_not_its_numerator() -> None:
    """They overlap the bar along the baseline, but their middle is more than a bar length away."""
    offset = _d("3.7", "6.4", "11.7", "11.9")

    assert stacked_fractions([offset, BAR, DENOMINATOR_BODY], geometry=GEOMETRY) == ()


def test_a_path_larger_than_glyph_max_is_not_part_of_a_fraction() -> None:
    """A cabinet edge above a short stroke is line-work, whatever else is near it."""
    edge = _d("-20", "6.4", "20", "18.5")

    assert stacked_fractions([edge, BAR, DENOMINATOR_BODY], geometry=GEOMETRY) == ()


def test_a_quarter_turned_fraction_is_measured_along_its_own_baseline() -> None:
    """Both drawings carry vertical dimension text. Turned a quarter, the bar is vertical on the page
    and only reads as a bar when measured across the baseline the stamp states."""
    turned = [(box[1], box[0], box[3], box[2]) for box in FRACTION]

    assert len(stacked_fractions(turned, geometry=GEOMETRY, rotation_degrees=90)) == 1
    assert stacked_fractions(turned, geometry=GEOMETRY, rotation_degrees=0) == ()


def test_two_fractions_are_found_separately() -> None:
    two = [*FRACTION, *_shifted(FRACTION, "30")]

    assert len(stacked_fractions(two, geometry=GEOMETRY)) == 2


def test_no_paths_no_fractions() -> None:
    assert stacked_fractions([], geometry=GEOMETRY) == ()


@pytest.mark.parametrize(
    "field",
    [
        "bar_thickness_max_pt",
        "bar_length_min_pt",
        "reach_pt",
        "glyph_min_pt",
        "glyph_max_pt",
        "proportion_max",
    ],
)
def test_every_threshold_is_a_positive_exact_decimal(field: str) -> None:
    """ADR-0001: each reaches a comparison, so it is exact or it is refused. None has a default."""
    values = {
        "bar_thickness_max_pt": Decimal("0.3"),
        "bar_length_min_pt": Decimal(1),
        "reach_pt": Decimal(3),
        "glyph_min_pt": Decimal(1),
        "glyph_max_pt": Decimal(12),
        "proportion_max": Decimal("2.5"),
    }
    with pytest.raises(TypeError, match="never a float"):
        FractionBarGeometry(**(values | {field: 0.5}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        FractionBarGeometry(**(values | {field: Decimal(0)}))
    with pytest.raises(TypeError):
        FractionBarGeometry(**{k: v for k, v in values.items() if k != field})  # type: ignore[arg-type]


def test_a_proportion_below_one_would_match_nothing_and_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        FractionBarGeometry(
            bar_thickness_max_pt=Decimal("0.3"),
            bar_length_min_pt=Decimal(1),
            reach_pt=Decimal(3),
            glyph_min_pt=Decimal(1),
            glyph_max_pt=Decimal(12),
            proportion_max=Decimal("0.9"),
        )


def test_every_number_is_in_the_run_identity() -> None:
    """A vision run under other detector numbers is another run, not this one reused."""
    text = GEOMETRY.config_hash

    for value in ("0.3", "1", "3", "12", "2.5"):
        assert value in text
