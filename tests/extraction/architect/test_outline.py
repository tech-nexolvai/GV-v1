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
    end_witness,
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
)
VIEW = Box(Decimal(0), Decimal(0), Decimal(400), Decimal(200))
ROW = Decimal(100)
FLOOR = Piece(Decimal(80), Decimal(0), Decimal(400))


def _dashes(x: int, start: int = 10, end: int = 104) -> list[Piece]:
    """A dashed line through the whole drawing, 3 on 3 off, as the client's CAD draws it."""
    return [Piece(Decimal(x), Decimal(y), Decimal(y + 3)) for y in range(start, end, 6)]


def _witness(
    x: int, verticals: list[Piece], marks: tuple[Box, ...] = (), ppi: Decimal | None = None
) -> EndWitness:
    return end_witness(
        Decimal(x),
        ROW,
        view=VIEW,
        work=LineWork(tuple(verticals), (FLOOR,)),
        centre_marks=marks,
        points_per_inch=ppi,
        settings=SETTINGS,
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
