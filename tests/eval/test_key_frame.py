"""The dimension-first frame, its quotas, and the person's two checks (#867).

Pure tests on hand-built places. That the script builds those places from where things are and never
from what they say is tested on synthetic PDFs in `test_author_reading_answer_key.py`.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest

from eval.experiments.key_frame import (
    Differs,
    Geometry,
    Glyph,
    Kind,
    LabelShape,
    Layout,
    LookAgain,
    Place,
    Site,
    Source,
    Typed,
    Witnessed,
    frame,
    kind_of,
    label_shaped,
    look_again,
    sample,
    triage,
    two_rows,
)

#: Stated for these hand-built places, as a caller states them; measured on no drawing.
SHAPE = LabelShape(minimum_glyphs=2, maximum_glyphs=12, height_ratio=Decimal("1.5"))
SMALL_PX = 60


def _glyph(along: str, across_low: str = "0", height: str = "5") -> Glyph:
    start = Decimal(along)
    low = Decimal(across_low)
    return (start, start + Decimal(3), low, low + Decimal(height))


#: `18"`, as a vendor draws it: two digits five points tall and the inch mark's two short ticks.
LABEL = (_glyph("0"), _glyph("4"), _glyph("8", "3.4", "1.6"), _glyph("9", "3.4", "1.6"))


def _geometry(**changes: object) -> Geometry:
    plain = Geometry(
        glyphs=LABEL,
        closed=True,
        one_nearest_line=True,
        sideways=False,
        stacked=False,
        cut=False,
        gv_mark=False,
    )
    return dataclasses.replace(plain, **changes)  # type: ignore[arg-type]


def _place(
    left: int = 0,
    *,
    page: int = 0,
    width: int = 100,
    source: Source = Source.PATH_LABEL,
    **changes: Any,
) -> Place:
    """A place at `left`, whose label — `LABEL` unless given — is drawn where the place is, so two
    places at different spots never gather one label by accident."""
    at = Decimal(left)
    changes.setdefault("glyphs", tuple((a + at, b + at, c, d) for a, b, c, d in LABEL))
    return Place(
        page_index=page,
        box=(left, 0, left + width, 40),
        sources=frozenset({source}),
        geometry=_geometry(**changes),
    )


def _quotas(**given: int) -> dict[Kind, int]:
    return {kind: given.get(kind.value, 0) for kind in Kind}


# --- the label-shape test ----------------------------------------------------------------------


def test_a_closed_label_of_like_glyphs_nearest_one_line_is_label_shaped() -> None:
    assert label_shaped(_geometry(), SHAPE)


@pytest.mark.parametrize(
    ("why", "changes"),
    [
        ("a lone mark", {"glyphs": (_glyph("0"),)}),
        ("a run of hatching", {"glyphs": tuple(_glyph(str(n * 4)) for n in range(13))}),
        ("one tall glyph among small marks", {"glyphs": (_glyph("0"), _glyph("4", "0", "1"))}),
        ("a label whose end is not settled", {"closed": False}),
        ("a cluster between several lines", {"one_nearest_line": False}),
    ],
)
def test_what_is_not_shaped_like_a_label(why: str, changes: dict[str, object]) -> None:
    assert not label_shaped(_geometry(**changes), SHAPE), why


def test_an_inch_marks_ticks_neither_count_nor_disqualify() -> None:
    """`18"`'s ticks are a third of a digit's height. They are not characters, and the label is
    still two glyphs of one height."""
    digits_only = _geometry(glyphs=LABEL[:2])

    assert label_shaped(_geometry(), SHAPE) == label_shaped(digits_only, SHAPE) is True


def test_millimetres_over_inches_are_two_rows_and_a_stacked_fraction_is_not() -> None:
    dual = (_glyph("0", "10"), _glyph("4", "10"), _glyph("0", "0"), _glyph("4", "0"))
    fraction = (_glyph("0", "10"), _glyph("0", "0"))

    assert two_rows(dual, SHAPE)
    assert not two_rows(fraction, SHAPE)
    assert not two_rows(LABEL, SHAPE)


def test_every_shape_setting_is_stated_and_checked() -> None:
    with pytest.raises(ValueError, match="maximum_glyphs"):
        LabelShape(minimum_glyphs=3, maximum_glyphs=2, height_ratio=Decimal(2))
    with pytest.raises(ValueError, match="at least 1"):
        LabelShape(minimum_glyphs=2, maximum_glyphs=12, height_ratio=Decimal("0.5"))
    with pytest.raises(TypeError, match="never a float"):
        LabelShape(minimum_glyphs=2, maximum_glyphs=12, height_ratio=1.5)  # type: ignore[arg-type]


# --- kinds, from geometry only -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "width", "kind"),
    [
        ({"stacked": True, "sideways": True, "cut": True}, 100, Kind.STACKED),
        ({"layout": Layout.STACKED_FRACTION}, 100, Kind.STACKED),
        ({"layout": Layout.TWO_LINES, "sideways": True}, 100, Kind.DUAL),
        ({"sideways": True, "cut": True}, 100, Kind.SIDEWAYS),
        ({"cut": True, "gv_mark": True}, 100, Kind.CUT),
        ({"gv_mark": True}, 30, Kind.GV_MARK),
        ({}, 30, Kind.SMALL),
        ({}, 100, Kind.PLAIN),
    ],
)
def test_a_place_is_the_first_kind_its_geometry_shows(
    changes: dict[str, Any], width: int, kind: Kind
) -> None:
    assert kind_of(_place(width=width, **changes), shape=SHAPE, small_px=SMALL_PX) is kind


# --- the frame ---------------------------------------------------------------------------------


@pytest.mark.parametrize("source", [Source.PATH_LABEL, Source.PRINTED_TEXT])
def test_a_place_not_shaped_like_a_label_is_dropped_and_counted(source: Source) -> None:
    """A printed run of letters is shaped like a run of digits, so printed text is held to the test
    a path region is: most of what a drawing prints is not a dimension."""
    built = frame(
        [_place(0, source=source), _place(500, source=source, closed=False)],
        [],
        shape=SHAPE,
        site_reach_px=0,
    )

    assert [place.box[0] for place in built.places] == [0]
    assert built.not_label_shaped == 1


@pytest.mark.parametrize(
    ("source", "layout"),
    [
        (Source.AGREED, None),
        (Source.PRINTED_TEXT, Layout.TWO_LINES),
        (Source.PATH_LABEL, Layout.FRAGMENT),
    ],
)
def test_a_place_the_file_already_shows_is_a_label_needs_no_shape_test(
    source: Source, layout: Layout | None
) -> None:
    unshaped = _place(source=source, closed=False, one_nearest_line=False, layout=layout)

    assert frame([unshaped], [], shape=SHAPE, site_reach_px=0).places == (unshaped,)


def test_a_place_at_a_gv_site_is_kept_and_marked_and_a_site_with_none_is_counted() -> None:
    under = _place(0, closed=False)
    beside = _place(500, width=50, closed=False)
    far = _place(900, closed=False)
    sites = [Site(0, (20, 10, 60, 30)), Site(0, (560, 0, 580, 40)), Site(1, (0, 0, 10, 10))]

    built = frame([under, beside, far], sites, shape=SHAPE, site_reach_px=10)

    assert [place.box[0] for place in built.places] == [0, 500]
    assert all(Source.GV_SITE in place.sources for place in built.places)
    assert built.sites_without_a_place == 1


def test_a_site_vouches_for_the_one_place_nearest_its_centre() -> None:
    """GV writes over the label it corrects; the line-work round it lies off to the side. So of two
    unshaped places under one box, only the one under its middle is kept as the vendor's label."""
    label = _place(40, width=60, closed=False)
    tick = _place(0, width=30, closed=False)

    built = frame([tick, label], [Site(0, (30, 0, 110, 40))], shape=SHAPE, site_reach_px=0)

    assert [place.box for place in built.places] == [label.box]
    assert built.not_label_shaped == 1


def test_two_regions_that_gathered_one_label_are_one_place() -> None:
    """Two planned regions over the two halves of one label lie side by side, neither's centre in
    the other's box, and each gathers the whole label: one place, drawn at most once."""
    left_half = _place(0, width=40, glyphs=LABEL)
    right_half = _place(60, width=40, glyphs=LABEL)
    elsewhere = _place(500, width=40)

    built = frame([left_half, right_half, elsewhere], [], shape=SHAPE, site_reach_px=0)

    assert [place.box[0] for place in built.places] == [0, 500]


def test_one_spot_found_three_ways_is_one_place_with_all_three_sources() -> None:
    """A label found as a path region, as printed text and by a run's agreement is drawn at most
    once; the file's own text run stands for it."""
    path = _place(0, width=100)
    text = _place(10, width=80, source=Source.PRINTED_TEXT)
    agreed = _place(5, width=90, source=Source.AGREED)

    (place,) = frame([path, agreed, text], [], shape=SHAPE, site_reach_px=0).places

    assert place.box == text.box
    assert place.sources == {Source.PATH_LABEL, Source.PRINTED_TEXT, Source.AGREED}


# --- the sample --------------------------------------------------------------------------------


def _mixed_frame() -> list[Place]:
    places = [_place(n * 200, stacked=True) for n in range(5)]
    places += [_place(1000 + n * 200, sideways=True) for n in range(5)]
    places += [_place(2000 + n * 200) for n in range(5)]
    return places


def test_each_kind_is_drawn_to_its_quota() -> None:
    drawn = sample(
        _mixed_frame(),
        quotas=_quotas(stacked=2, sideways=3, plain=1),
        shape=SHAPE,
        small_px=SMALL_PX,
        seed=0,
    )

    assert drawn.counts == Counter({Kind.STACKED: 2, Kind.SIDEWAYS: 3, Kind.PLAIN: 1})
    assert drawn.available[Kind.STACKED] == drawn.available[Kind.PLAIN] == 5


def test_a_short_kind_is_drawn_short_never_topped_up_from_another() -> None:
    quotas = _quotas(stacked=8, plain=1)

    drawn = sample(_mixed_frame(), quotas=quotas, shape=SHAPE, small_px=SMALL_PX, seed=0)

    assert drawn.counts == Counter({Kind.STACKED: 5, Kind.PLAIN: 1})
    assert drawn.short(quotas) == {Kind.STACKED: 3}


def test_the_same_frame_and_seed_draw_the_same_sample() -> None:
    quotas = _quotas(stacked=2, sideways=2, plain=2)
    first = sample(_mixed_frame(), quotas=quotas, shape=SHAPE, small_px=SMALL_PX, seed=4)
    again = sample(
        list(reversed(_mixed_frame())), quotas=quotas, shape=SHAPE, small_px=SMALL_PX, seed=4
    )

    assert first == again


def test_every_kind_needs_a_stated_quota() -> None:
    with pytest.raises(ValueError, match="every kind needs a stated quota"):
        sample(
            _mixed_frame(),
            quotas={Kind.STACKED: 2},
            shape=SHAPE,
            small_px=SMALL_PX,
            seed=0,
        )


def test_a_place_holds_no_value_and_no_text_for_a_sample_to_select_on() -> None:
    """**The sample never selects on a reading**, and the first guard is that there is none to
    select on: a place is a page, a box, where it was found and the geometry there."""
    assert {field.name for field in dataclasses.fields(Place)} == {
        "page_index",
        "box",
        "sources",
        "geometry",
    }
    assert {field.name for field in dataclasses.fields(Geometry)} == {
        "glyphs",
        "closed",
        "one_nearest_line",
        "sideways",
        "stacked",
        "cut",
        "gv_mark",
        "layout",
    }


# --- triage ------------------------------------------------------------------------------------


def test_triage_counts_the_crops_that_are_dimensions() -> None:
    counted = triage("drawing-a", [False, True, False, False, True])

    assert (counted.crops, counted.not_a_dimension, counted.dimensions) == (5, 2, 3)
    assert counted.share == Fraction(3, 5)
    assert counted.meets(Fraction(3, 5))
    assert not counted.meets(Fraction(61, 100))


def test_a_drawing_with_no_crops_never_meets_the_minimum() -> None:
    counted = triage("empty", [])

    assert counted.share is None
    assert not counted.meets(Fraction(0))


# --- the typist's look-again check -------------------------------------------------------------


def test_a_typed_value_the_drawing_does_not_print_there_is_listed() -> None:
    typed = [Typed("c1", Fraction(69, 4), None), Typed("c2", Fraction(24), None)]
    seen = {"c1": Witnessed("c1", printed=frozenset({Fraction(71, 4)})), "c2": Witnessed("c2")}

    assert look_again(typed, seen) == (LookAgain("c1", Differs.FILE_TEXT),)


def test_gv_missed_and_gv_taken_for_the_vendor_are_both_listed() -> None:
    gv = frozenset({Fraction(61, 2)})
    typed = [
        Typed("missed", None, None),
        Typed("taken", Fraction(61, 2), Fraction(61, 2)),
        Typed("right", None, Fraction(61, 2)),
    ]
    seen = {crop: Witnessed(crop, gv=gv) for crop in ("missed", "taken", "right")}

    assert look_again(typed, seen) == (
        LookAgain("missed", Differs.GV_TEXT),
        LookAgain("taken", Differs.GV_TEXT),
    )


def test_where_the_file_prints_nothing_nothing_is_compared() -> None:
    """A crop drawn in paths has no printed text, and its typed value is the only one there is."""
    assert look_again([Typed("paths", Fraction(18), Fraction(20))], {}) == ()


def test_the_look_again_list_never_carries_a_value() -> None:
    """**Blind.** The list must not tell the typist the answer, so an entry holds the crop and which
    text differs, and nothing else."""
    assert {field.name for field in dataclasses.fields(LookAgain)} == {"crop_id", "differs"}
