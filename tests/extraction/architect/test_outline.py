"""Whether a span's ends sit on casework edges or on centre lines (D4, #1052).

Verification for: `extraction/architect/outline.py`. Line-work written by hand, page points,
`top` downward. The dimension line is at y = 100; the drawing is above it.
"""

from __future__ import annotations

from decimal import Decimal

from extraction.architect.outline import (
    EndWitness,
    LineWork,
    OutlineSettings,
    Piece,
    Stroke,
    end_witness,
    hatched_regions,
    one_object,
    span_on_outline,
)
from extraction.geometry.rows import Box

SETTINGS = OutlineSettings(
    straightness_pt=Decimal("0.3"),
    tolerance_pt=Decimal(1),
    row_clearance_pt=Decimal(1),
    join_gap_pt=Decimal("0.05"),
    solid_minimum_pt=Decimal(12),
    dash_maximum_pt=Decimal(6),
    dash_minimum_pieces=3,
    mark_reach_pt=Decimal(4),
    link_pt=Decimal(4),
    toe_kick_in=Decimal(6),
    hatch_parallel_sine=Decimal("0.02"),
    hatch_spacing_pt=Decimal(6),
    hatch_minimum_lines=5,
)
VIEW = Box(Decimal(0), Decimal(0), Decimal(400), Decimal(200))
ROW = Decimal(100)
FLOOR = Piece(Decimal(80), Decimal(0), Decimal(400))


def _dashes(x: int, start: int = 10, end: int = 104) -> list[Piece]:
    """A dashed line through the whole drawing, 3 on 3 off, as the client's CAD draws it."""
    return [Piece(Decimal(x), Decimal(y), Decimal(y + 3)) for y in range(start, end, 6)]


def _witness(
    x: int,
    verticals: list[Piece],
    marks: tuple[Box, ...] = (),
    ppi: Decimal | None = None,
    hatched: tuple[Box, ...] = (),
) -> EndWitness:
    return end_witness(
        Decimal(x),
        ROW,
        view=VIEW,
        work=LineWork(tuple(verticals), (FLOOR,)),
        centre_marks=marks,
        points_per_inch=ppi,
        settings=SETTINGS,
        hatched=hatched,
    )


def test_a_cabinet_side_starting_at_the_floor_is_an_edge() -> None:
    side = Piece(Decimal(50), Decimal(20), Decimal(80))

    assert _witness(50, [side, *_dashes(50)]).on_outline is True


def test_a_side_starting_at_the_toe_kick_is_an_edge_when_the_scale_is_known() -> None:
    side = Piece(
        Decimal(50), Decimal(20), Decimal(72)
    )  # 8 pt above the floor: a 4" toe kick at 2 pt/in

    assert _witness(50, [side], ppi=Decimal(2)).on_outline is True
    assert _witness(50, [side, *_dashes(50)]).on_outline is False


def test_a_dashed_centre_line_alone_is_not_an_edge() -> None:
    witness = _witness(120, _dashes(120))

    assert witness.on_outline is False
    assert "dashed" in witness.reason


def test_a_solid_line_sharing_the_centre_lines_x_further_in_is_not_an_edge() -> None:
    """An upper cabinet's joint high above the outlet the dimension locates."""
    joint = Piece(Decimal(120), Decimal(10), Decimal(40))

    assert _witness(120, [joint, *_dashes(120)]).on_outline is False


def test_a_centre_line_mark_on_the_line_is_never_an_edge() -> None:
    side = Piece(Decimal(50), Decimal(20), Decimal(80))
    mark = Box(Decimal(47), Decimal(5), Decimal(54), Decimal(10))

    witness = _witness(50, [side], marks=(mark,))
    assert witness.on_outline is False
    assert "centre-line mark" in witness.reason


def test_two_dashed_lines_out_of_step_are_not_one_solid_line() -> None:
    first = _dashes(120)
    second = [Piece(p.at, p.start + 2, p.end + 2) for p in first]

    assert _witness(120, first + second).on_outline is False


def test_nothing_drawn_is_unclear() -> None:
    assert _witness(300, []).on_outline is None


def test_a_span_is_on_the_outline_only_when_both_ends_are() -> None:
    yes, no, unclear = EndWitness(True, ""), EndWitness(False, ""), EndWitness(None, "")

    assert span_on_outline(yes, yes) is True
    assert span_on_outline(yes, no) is False
    assert span_on_outline(unclear, no) is False
    assert span_on_outline(yes, unclear) is None


# --- Hatched material is never casework (#1052 fix) ------------------------------------------------


def _hatch(x0: int, x1: int, top: int, bottom: int, spacing: int = 4) -> list[Stroke]:
    """Parallel 45-degree strokes filling a box, as a CAD program draws wood blocking or a wall's
    cut section: each stroke runs from the box's bottom edge up and to the right."""
    height = bottom - top
    return [
        Stroke(Decimal(x), Decimal(bottom), Decimal(x + height), Decimal(top))
        for x in range(x0, x1 - height + 1, spacing)
    ]


def test_a_dense_family_of_parallel_strokes_is_a_hatched_region() -> None:
    (region,) = hatched_regions(_hatch(50, 150, 60, 80), (), view=VIEW, settings=SETTINGS)

    assert region.x0 == Decimal(50) and region.top == Decimal(60)
    assert region.bottom == Decimal(80)


def test_a_few_diagonals_or_one_dashed_diagonal_are_not_hatching() -> None:
    """A door's swing is two lines; a dashed diagonal is many pieces of one line."""
    swing = [
        Stroke(Decimal(200), Decimal(80), Decimal(260), Decimal(20)),
        Stroke(Decimal(210), Decimal(80), Decimal(270), Decimal(20)),
    ]
    dashed = [
        Stroke(Decimal(300 + 6 * k), Decimal(80 - 6 * k), Decimal(303 + 6 * k), Decimal(77 - 6 * k))
        for k in range(10)
    ]

    assert hatched_regions(swing + dashed, (), view=VIEW, settings=SETTINGS) == ()


def test_a_pattern_fill_is_a_hatched_region() -> None:
    fill = Box(Decimal(10), Decimal(60), Decimal(40), Decimal(80))

    assert hatched_regions((), (fill,), view=VIEW, settings=SETTINGS) == (fill,)


def test_an_edge_bounding_hatched_material_is_not_casework() -> None:
    """Wood blocking drawn as a hatched strip on the floor line: its sides start at the first
    outline crossed, exactly like a cabinet's, but the strip is not casework."""
    side = Piece(Decimal(50), Decimal(60), Decimal(80))
    region = hatched_regions(_hatch(50, 150, 60, 80), (), view=VIEW, settings=SETTINGS)

    assert _witness(50, [side]).on_outline is True
    witness = _witness(50, [side], hatched=region)
    assert witness.on_outline is False
    assert "hatched material" in witness.reason


# --- Both ends must be edges of one object (#1052 fix) ---------------------------------------------


def _ended(x: int, top: int, bottom: int) -> EndWitness:
    return EndWitness(True, "edge", edge_top=Decimal(top), edge_bottom=Decimal(bottom))


def test_a_cabinet_with_its_top_and_bottom_between_both_ends_is_one_object() -> None:
    top = Piece(Decimal(20), Decimal(50), Decimal(150))
    work = LineWork((), (FLOOR, top))

    assert one_object(
        _ended(50, 20, 80),
        _ended(150, 20, 80),
        Decimal(50),
        Decimal(150),
        work=work,
        view=VIEW,
        dimension_lines=(),
        settings=SETTINGS,
    )


def test_a_cabinet_in_a_run_under_one_continuous_top_is_one_object() -> None:
    """The top line runs on past the joint: the cabinet is the cell between its two sides."""
    top = Piece(Decimal(20), Decimal(0), Decimal(300))
    work = LineWork((), (FLOOR, top))

    assert one_object(
        _ended(50, 20, 80),
        _ended(150, 20, 80),
        Decimal(50),
        Decimal(150),
        work=work,
        view=VIEW,
        dimension_lines=(),
        settings=SETTINGS,
    )


def test_a_wall_to_an_objects_side_is_a_clearance_not_an_object() -> None:
    """From the wall to a credenza standing apart: only the floor line runs between the two ends,
    and the credenza's top stops at its own side."""
    credenza_top = Piece(Decimal(60), Decimal(150), Decimal(250))
    work = LineWork((), (FLOOR, credenza_top))

    assert not one_object(
        _ended(50, 0, 80),
        _ended(150, 60, 80),
        Decimal(50),
        Decimal(150),
        work=work,
        view=VIEW,
        dimension_lines=(),
        settings=SETTINGS,
    )


def test_an_end_without_an_edge_is_never_one_object() -> None:
    work = LineWork((), (FLOOR, Piece(Decimal(20), Decimal(50), Decimal(150))))

    assert not one_object(
        EndWitness(True, "edge"),
        _ended(150, 20, 80),
        Decimal(50),
        Decimal(150),
        work=work,
        view=VIEW,
        dimension_lines=(),
        settings=SETTINGS,
    )
