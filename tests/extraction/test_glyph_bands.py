"""Finding a stacked fraction by its bar (#541, #735), and where each part of its label was drawn (#834).

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

from extraction.glyph_bands import FractionBarGeometry, FractionLayout, GlyphBox, stacked_fractions


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
    character_gap_pt=Decimal(4),
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


def _found(
    boxes: list[GlyphBox], *, ink: list[bool] | None = None, rotation_degrees: int = 0
) -> tuple[FractionLayout, ...]:
    """Every path drawn in ink unless a test says otherwise, which is what the plotter draws."""
    return stacked_fractions(
        boxes,
        geometry=GEOMETRY,
        ink=[True] * len(boxes) if ink is None else ink,
        rotation_degrees=rotation_degrees,
    )


def _boxes(found: tuple[FractionLayout, ...]) -> tuple[GlyphBox, ...]:
    return tuple(layout.box for layout in found)


def _counts(layout: FractionLayout) -> tuple[int, int, int, int]:
    """Whole-number characters, numerator, denominator, and inch-mark paths."""
    return (
        len(layout.whole),
        len(layout.numerator),
        len(layout.denominator),
        len(layout.inch_mark),
    )


def test_a_stacked_fraction_is_found_as_the_plotter_draws_it() -> None:
    """**The case #735 exists for.** Bar, numerator above, denominator below in two strokes.

    The box that comes back is the whole fraction — bar, numerator and denominator — so a crop that
    shows any of it can be told apart from one that shows none.
    """
    found = _found(FRACTION)

    assert _boxes(found) == (_d("0", "-0.8", "3.8", "11.9"),)


def test_the_whole_number_beside_it_changes_nothing() -> None:
    """`28 3/4"`: two full-height digits to the left. They share no position with the bar, so the
    detection is the same fraction and does not swallow the whole number."""
    whole = [_d("-9", "0", "-5", "11.9"), _d("-4.5", "0", "-0.5", "11.9")]

    found = _found([*whole, *FRACTION])

    assert _boxes(found) == (_d("0", "-0.8", "3.8", "11.9"),)
    assert len(found[0].whole) == 2


def test_a_numerator_one_drawn_as_a_single_stroke_is_still_found() -> None:
    """**Recall, in the case that matters most.** `1/2`, `1/4`, `1/8` and `1/16` are the commonest
    fractions in the trade, and a stroke-font `1` can be one vertical stroke with no width. Requiring
    a two-dimensional glyph on *both* sides would miss every one of them; the rule asks for one side.
    """
    one = _d("1.9", "6.4", "1.9", "11.9")

    found = _found([one, BAR, DENOMINATOR_BODY, DENOMINATOR_STEM])

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

    assert _found(label) == ()


def test_two_lines_of_text_are_not_a_fraction() -> None:
    """The false positive `glyph_bands` used to be exposed to: any two-line note has glyphs one above
    another. With no bar between them there is nothing to find."""
    note = [_d("0", "7", "3.6", "12.5"), _d("0", "0", "3.6", "5.5")]

    assert _found(note) == ()


def test_one_dash_of_a_dashed_line_is_not_a_bar() -> None:
    """**The false alarm isolation removes.** On the client's page 9, hatch strokes had a shape above
    and below them just as a bar does. A bar stands alone; a dash has a neighbour on its own line a
    short gap away."""
    dashed = [*FRACTION, _d("5.0", "5.5", "8.8", "5.5")]

    assert _found(dashed) == ()


def test_a_line_on_the_bar_far_beyond_reach_does_not_matter() -> None:
    far = [*FRACTION, _d("20", "5.5", "40", "5.5")]

    assert len(_found(far)) == 1


def test_the_same_stroke_drawn_twice_is_still_one_bar() -> None:
    """PDFs embolden by drawing a path twice. The copy is not a neighbour on the bar's line."""
    doubled = [*FRACTION, BAR]

    assert len(_found(doubled)) == 1


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

    assert _found([*arcs_above, BAR, *arcs_below]) == ()


def test_a_shape_far_larger_than_the_other_is_not_a_digit_over_a_digit() -> None:
    tall = _d("0.1", "6.4", "3.7", "11.9")
    tiny = _d("1.0", "3.0", "3.0", "4.6")  # 1.6 pt tall against 5.5 pt: beyond 2.5x

    assert _found([tall, BAR, tiny]) == ()


def test_shapes_off_to_one_side_of_the_bar_are_not_its_numerator() -> None:
    """They overlap the bar along the baseline, but their middle is more than a bar length away."""
    offset = _d("3.7", "6.4", "11.7", "11.9")

    assert _found([offset, BAR, DENOMINATOR_BODY]) == ()


def test_a_path_larger_than_glyph_max_is_not_part_of_a_fraction() -> None:
    """A cabinet edge above a short stroke is line-work, whatever else is near it."""
    edge = _d("-20", "6.4", "20", "18.5")

    assert _found([edge, BAR, DENOMINATOR_BODY]) == ()


def test_a_quarter_turned_fraction_is_measured_along_its_own_baseline() -> None:
    """Both drawings carry vertical dimension text. Turned a quarter, the bar is vertical on the page
    and only reads as a bar when measured across the baseline the stamp states."""
    turned = [(box[1], box[0], box[3], box[2]) for box in FRACTION]

    assert len(_found(turned, rotation_degrees=90)) == 1
    assert _found(turned, rotation_degrees=0) == ()


def test_two_fractions_are_found_separately() -> None:
    two = [*FRACTION, *_shifted(FRACTION, "30")]

    assert len(_found(two)) == 2


def test_no_paths_no_fractions() -> None:
    assert _found([]) == ()


@pytest.mark.parametrize(
    "field",
    [
        "bar_thickness_max_pt",
        "bar_length_min_pt",
        "reach_pt",
        "glyph_min_pt",
        "glyph_max_pt",
        "proportion_max",
        "character_gap_pt",
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
        "character_gap_pt": Decimal(4),
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
            character_gap_pt=Decimal(4),
        )


def test_every_number_is_in_the_run_identity() -> None:
    """A vision run under other detector numbers is another run, not this one reused."""
    text = GEOMETRY.config_hash

    for value in ("0.3", "1", "3", "12", "2.5"):
        assert value in text
    assert "character_gap<=4" in text


# ---------------------------------------------------------------------------
# Where each part of the label was drawn (#834)
# ---------------------------------------------------------------------------

#: `39 1/2"` in the plotter's proportions (measured on a real label and moved to the origin; the
#: digits are synthetic): 5.4 pt digits, the whole number centred on the bar, a numerator `1` with
#: a little width, a gap of 1.56 pt between the whole number's digits and 0.96 pt before the bar.
WHOLE_THREE = _d("-9.48", "2.86", "-5.88", "8.26")
WHOLE_NINE = _d("-4.32", "2.86", "-0.96", "8.26")
LONG_BAR = _d("0", "5.5", "3.72", "5.5")
ONE = _d("1.2", "6.46", "2.52", "11.86")
TWO = _d("0", "-0.74", "3.72", "4.66")
TICKS = [_d("5.52", "7.78", "6.0", "9.34"), _d("7.08", "7.78", "7.56", "9.34")]

THIRTY_NINE_AND_A_HALF = [WHOLE_THREE, WHOLE_NINE, LONG_BAR, ONE, TWO, *TICKS]


def _layout(boxes: list[GlyphBox], **arguments: object) -> FractionLayout:
    found = _found(boxes, **arguments)  # type: ignore[arg-type]
    assert len(found) == 1, found
    return found[0]


def test_a_whole_number_and_a_fraction_are_laid_out_part_by_part() -> None:
    """**`39 1/2"`: two whole-number characters, one over one, and an inch mark.** Each part names
    the paths that draw it, in reading order, so a reading can be checked against it and a later
    step can cut each part out to read it alone."""
    layout = _layout(THIRTY_NINE_AND_A_HALF)

    assert _counts(layout) == (2, 1, 1, 2)
    assert layout.whole == ((WHOLE_THREE,), (WHOLE_NINE,))
    assert layout.numerator == ((ONE,),)
    assert layout.denominator == ((TWO,),)
    assert layout.bar == LONG_BAR
    assert layout.inch_mark == tuple(TICKS)
    assert layout.box == _d("0", "-0.74", "3.72", "11.86"), "the detection's box is unchanged"
    assert layout.rotation_degrees == 0, "read upright"


def test_a_bare_fraction_has_no_whole_number_and_a_two_stroke_four_counts_once() -> None:
    """**`3/4"`: no whole number, one over one.** The `4` is drawn as a body and a stem, which overlap
    side to side and so are one character. Counted as two, a `3/4"` would have a two-digit
    denominator and every right reading of it would be refused."""
    layout = _layout(FRACTION)

    assert _counts(layout) == (0, 1, 1, 2)
    assert layout.denominator == ((DENOMINATOR_BODY, DENOMINATOR_STEM),)


def test_a_dimension_tick_below_the_label_is_not_a_whole_number() -> None:
    """**The miscount that would let `3 3/4"` through.** A dimension's tick or arrowhead sits just
    before a label and below its digits. Taken for a character, it would give the `3/4"` a whole
    number, and a reading that promoted the numerator into one would match. A path wholly below the
    digit band is left out, and so is one that only reaches up into it."""
    below = _d("-3.0", "-4.0", "-0.2", "-1.0")
    reaching_in = _d("-3.0", "-2.0", "-0.2", "3.0")

    for tick in (below, reaching_in):
        assert _counts(_layout([tick, *FRACTION])) == (0, 1, 1, 2)


def test_a_whole_number_runs_on_digit_by_digit_and_stops_at_the_gap() -> None:
    """Each character is reached from the one after it, so a whole number of any length is taken
    whole; a character further than `character_gap_pt` from the label's start is another label's."""
    three_digits = [_shifted([WHOLE_THREE], "-5.04")[0], *THIRTY_NINE_AND_A_HALF]
    neighbour = [_shifted([WHOLE_THREE], "-9.6")[0], *THIRTY_NINE_AND_A_HALF]

    assert len(_layout(three_digits).whole) == 3  # 1.44 pt before the `3`: the same label
    assert len(_layout(neighbour).whole) == 2  # 6 pt before it: beyond the 4 pt gap


def test_two_digits_that_only_touch_stay_two() -> None:
    """**An undercount is the dangerous miscount**: it lets a reading that dropped a digit match. So
    digits that share an edge, set tight, are not merged — only paths that overlap are."""
    tight = [_d("-8.0", "2.86", "-4.4", "8.26"), _d("-4.4", "2.86", "-0.8", "8.26")]
    stroke_on_the_edge = [_d("-4.4", "2.0", "-4.4", "8.26"), _d("-4.4", "2.86", "-0.8", "8.26")]

    for boxes in (tight, stroke_on_the_edge):
        for drawn in (boxes, boxes[::-1]):
            assert len(_layout([*drawn, *FRACTION]).whole) == 2, drawn


def test_a_character_is_the_same_whatever_order_its_paths_were_drawn_in() -> None:
    reversed_four = [NUMERATOR, BAR, DENOMINATOR_STEM, DENOMINATOR_BODY, *INCH_TICKS]

    assert _layout(reversed_four).denominator == ((DENOMINATOR_BODY, DENOMINATOR_STEM),)


def test_the_same_stroke_drawn_twice_is_one_character() -> None:
    """PDFs embolden by drawing a path twice, and a `1` can be one stroke with no width."""
    one = _d("-2.0", "2.86", "-2.0", "8.26")

    layout = _layout([one, one, *FRACTION])

    assert layout.whole == ((one, one),)


def test_a_path_not_drawn_in_ink_is_no_character_but_the_fraction_is_still_found() -> None:
    """**The reviewer's colour baked into a snapshot** (#834). A red stroke before a `3/4"` is
    somebody's markup, not the vendor's digit, so it is not counted. Colour decides only the layout:
    a fraction whose bar happens to be coloured is found exactly as before, because missing one
    costs more than a reviewer's look."""
    red = _d("-2.0", "2.86", "-1.0", "8.26")
    boxes = [red, *FRACTION]

    assert len(_layout(boxes, ink=[False] + [True] * len(FRACTION)).whole) == 0
    assert len(_layout(boxes).whole) == 1, "the same stroke in ink is a character"
    coloured_bar = [box != BAR for box in boxes]
    assert _boxes(_found(boxes, ink=coloured_bar)) == _boxes(_found(boxes))


def test_only_what_sits_wholly_above_the_bar_is_the_inch_mark() -> None:
    """An inch mark sits high; a digit after the fraction is centred on the bar, so it is not one."""
    digit_after = _d("5.52", "2.86", "9.12", "8.26")
    unmarked = [NUMERATOR, BAR, DENOMINATOR_BODY, DENOMINATOR_STEM]

    assert _layout([*unmarked, digit_after]).inch_mark == ()
    assert _layout([*unmarked, *INCH_TICKS]).inch_mark == tuple(INCH_TICKS)


def _turned(box: GlyphBox, degrees: int) -> GlyphBox:
    """A box turned anticlockwise about the origin, as a stamp turned by its placement draws it."""
    x0, y0, x1, y1 = box
    if degrees == 90:
        return (-y1, x0, -y0, x1)
    if degrees == 180:
        return (-x1, -y1, -x0, -y0)
    return (y0, -x1, y1, -x0)


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_a_turned_label_is_laid_out_the_way_it_reads(degrees: int) -> None:
    """**Turned, not only swapped.** Which side of the bar is the numerator, and which end the whole
    number is at, depend on which way the label reads. Turned any quarter, `39 1/2"` still has its
    `1` as the numerator and its two digits before the fraction."""
    turned = [_turned(box, degrees) for box in THIRTY_NINE_AND_A_HALF]

    layout = _layout(turned, rotation_degrees=degrees)

    assert _counts(layout) == (2, 1, 1, 2)
    assert layout.rotation_degrees == degrees, "and it says which way it is turned (#848)"
    assert layout.numerator == ((_turned(ONE, degrees),),)
    assert layout.whole == ((_turned(WHOLE_THREE, degrees),), (_turned(WHOLE_NINE, degrees),))


def test_a_rotation_that_is_not_a_quarter_turn_is_refused() -> None:
    with pytest.raises(ValueError, match="quarter turns"):
        _found(FRACTION, rotation_degrees=45)


def test_ink_must_be_said_for_every_box() -> None:
    """`ink` has no default and must cover every box: a list one short would shift every colour."""
    with pytest.raises(ValueError, match="ink must say"):
        stacked_fractions(FRACTION, geometry=GEOMETRY, ink=[True])
    with pytest.raises(TypeError, match="ink"):
        stacked_fractions(FRACTION, geometry=GEOMETRY)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# What lies beside a label and is counted as none of it (#848)
# ---------------------------------------------------------------------------

#: A `+` after the inch mark, centred on the bar as a character beside a fraction is: the start of
#: more label, as in `39 1/2"+6"`.
PLUS_AFTER = _d("8.5", "3.0", "11.5", "6.0")


def test_a_label_with_nothing_beside_it_has_no_neighbours() -> None:
    assert _layout(THIRTY_NINE_AND_A_HALF).neighbours == ()
    assert _layout(FRACTION).neighbours == ()


def test_a_character_after_the_inch_mark_within_the_gap_is_a_neighbour() -> None:
    """**More label than the layout counts.** The `+` is not wholly above the bar, so it is not the
    inch mark, and it is after the fraction, so it is not the whole number: counted as nothing, it
    is named, so a reading put together from the parts is not taken for the whole label."""
    layout = _layout([*FRACTION, PLUS_AFTER])

    assert _counts(layout) == (0, 1, 1, 2), "the parts are counted as before"
    assert layout.neighbours == (PLUS_AFTER,)


def test_a_first_digit_drawn_taller_than_the_band_is_a_neighbour() -> None:
    """**The miscount that would make `39 1/2"` read as `9 1/2"`.** A `3` reaching below the
    denominator's foot is not wholly in the digit band, so the whole number is taken as the `9`
    alone; it is centred in the band, so it is a neighbour, and the label is not read in parts."""
    tall_three = _d("-9.48", "-1.0", "-5.88", "8.26")
    layout = _layout([tall_three, WHOLE_NINE, LONG_BAR, ONE, TWO, *TICKS])

    assert layout.whole == ((WHOLE_NINE,),)
    assert layout.neighbours == (tall_three,)


def test_a_tick_centred_below_the_band_is_not_a_neighbour_though_it_reaches_in() -> None:
    """A dimension's tick sits below the label. Measured on the client's drawing, ticks reach a
    quarter of a point into the band from below; centred below it, they are no character."""
    tick = _d("-3.0", "-3.8", "-0.2", "-0.56")

    assert _layout([tick, *FRACTION]).neighbours == ()


def test_beyond_the_gap_or_out_of_ink_a_path_is_not_a_neighbour() -> None:
    """Further than `character_gap_pt` from the label it is another label's; in a reviewer's colour it
    is nobody's character. Neither stops the label being read."""
    beyond = _shifted([PLUS_AFTER], "3.5")[0]  # starts 4.3 pt after the mark ends, the gap is 4

    assert _layout([*FRACTION, beyond]).neighbours == ()
    coloured = _layout([*FRACTION, PLUS_AFTER], ink=[True] * len(FRACTION) + [False])
    assert coloured.neighbours == ()


def test_the_bar_drawn_twice_is_not_its_own_neighbour() -> None:
    """PDFs embolden by drawing a stroke twice. The second bar lies on the first, in the band and
    inside the label, and is the same stroke, not a character beside it."""
    assert _layout([BAR, *FRACTION]).neighbours == ()


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_a_turned_label_names_its_neighbour_in_page_space(degrees: int) -> None:
    turned = [_turned(box, degrees) for box in [*FRACTION, PLUS_AFTER]]

    assert _layout(turned, rotation_degrees=degrees).neighbours == (_turned(PLUS_AFTER, degrees),)
