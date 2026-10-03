"""Suggesting the parts of a vendor's drawing, and the code shapes a suggestion may carry (#868).

Every suggestion here is only a suggestion: a person confirms each one before it becomes a part, and
`tests/db/test_drawing_models.py` fails if anything but that confirmation writes a drawing item. So
these tests are about what is suggested and, mostly, about what is held back: a code two cabinets
could claim, a drawing nested inside another, a run the chain only partly saw, a row above the base
cabinets, a dimension drawn twice, and a countertop that must keep its own ends rather than borrow
its cabinets'.

The geometry is built from strokes and run through the real `detect()`, so a chain here is a chain
because the detector made it one. **Every code is invented.** None is a string from a client drawing.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise
from uuid import UUID, uuid4

import pytest

from evidence.coordinates import StoredPoint
from evidence.polygon import Polygon, PolygonSpaceMismatchError
from extraction.geometry.containment import DimensionExtent
from extraction.geometry.dimension_lines import DetectedDimensions, detect
from extraction.model.part_proposals import (
    PageParts,
    PrintedText,
    ProposedPart,
    ViewOutline,
    propose_parts,
)
from vocabulary import cabinet_codes
from vocabulary.cabinet_codes import (
    CABINET_CODE_SHAPES,
    FINISH_CODE_SHAPES,
    CodeShape,
    is_cabinet_code,
    is_finish_code,
)
from vocabulary.part_kinds import PartKind

DOCUMENT = uuid4()
PAGE = 2
TOLERANCE = Decimal("0.002")

#: How far a witness line reaches above and below its dimension unless a test says otherwise.
ABOVE = Decimal("0.05")
BELOW = Decimal("0.01")

#: A short reach above, for a row or an overall whose witness lines must not cross another row.
SHORT = Decimal("0.01")


def _d(value: str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(value)


def _stroke(
    x0: str | Decimal, y0: str | Decimal, x1: str | Decimal, y1: str | Decimal
) -> DimensionExtent:
    return DimensionExtent(
        start=StoredPoint(_d(x0), _d(y0)),
        end=StoredPoint(_d(x1), _d(y1)),
        document_version_id=DOCUMENT,
        page=PAGE,
    )


def _dimension(
    x0: str, x1: str, y: str, *, above: Decimal = ABOVE, below: Decimal = BELOW
) -> list[DimensionExtent]:
    """A horizontal dimension at `y` from `x0` to `x1`, its two ends crossed by witness lines."""
    at = _d(y)
    return [
        _stroke(x0, y, x1, y),
        _stroke(x0, at - above, x0, at + below),
        _stroke(x1, at - above, x1, at + below),
    ]


def _chain(
    y: str, *edges: str, above: Decimal = ABOVE, below: Decimal = BELOW
) -> list[DimensionExtent]:
    """Dimensions drawn end to end at `y`, one between each pair of neighbouring `edges`."""
    strokes: list[DimensionExtent] = []
    for left, right in pairwise(edges):
        strokes.extend(_dimension(left, right, y, above=above, below=below))
    return strokes


def _detect(*groups: list[DimensionExtent]) -> DetectedDimensions:
    return detect(
        [stroke for group in groups for stroke in group],
        witness_tolerance=TOLERANCE,
        minimum_span=Decimal("0.01"),
        straightness=Decimal("0.0005"),
        crossing_margin=Decimal("0.001"),
    )


def _box(x0: str, y0: str, x1: str, y1: str, *, page: int = PAGE) -> Polygon:
    return Polygon(
        points=(
            StoredPoint(_d(x0), _d(y0)),
            StoredPoint(_d(x1), _d(y0)),
            StoredPoint(_d(x1), _d(y1)),
            StoredPoint(_d(x0), _d(y1)),
        ),
        space="stored",
        document_version_id=DOCUMENT,
        page=page,
    )


def _view(
    *, vendor: bool = True, box: tuple[str, str, str, str] = ("0.05", "0.05", "0.95", "0.95")
) -> ViewOutline:
    return ViewOutline(view_id=uuid4(), region=_box(*box), vendor=vendor)


def _code(text: str, x0: str, x1: str, *, y: str = "0.70") -> PrintedText:
    """A reading of `text` across `x0..x1`, a hundredth of the page tall, its top at `y`."""
    top = _d(y)
    return PrintedText(
        candidate_id=uuid4(),
        text=text,
        extent=_box(x0, str(top), x1, str(top + Decimal("0.01"))),
    )


def _propose(
    views: list[ViewOutline],
    detected: DetectedDimensions,
    texts: list[PrintedText] | None = None,
) -> PageParts:
    return propose_parts(views, detected, texts or [], edge_tolerance=TOLERANCE)


def _across(part: ProposedPart) -> tuple[Decimal, Decimal]:
    """A part's extent across the page, measured here rather than with the module's own helper."""
    xs = [point.x for point in part.extent.points]
    return min(xs), max(xs)


def _down(part: ProposedPart) -> tuple[Decimal, Decimal]:
    ys = [point.y for point in part.extent.points]
    return min(ys), max(ys)


def _kinds(result: PageParts) -> list[PartKind]:
    return [part.kind for part in result.parts]


# ---------------------------------------------------------------------------
# The code shapes
# ---------------------------------------------------------------------------

#: Invented codes, one or more of each cabinet shape.
INVENTED_CABINET_CODES = (
    "XQ24",
    "XQ30L",
    "XQ3036",
    "X48R",
    "XQ22P5R",
    "XQ40P250",
    "XQ16-4",
    "XQ16−4",
)

#: Invented finish codes, of the shape a drawing prints for a laminate, a stone or a metal.
INVENTED_FINISH_CODES = ("QV-07", "QV-118", "QV-118-ZZZ", "Q-204-ZZ", "QVX-009A-B2", "QV−07")


@pytest.mark.parametrize("code", INVENTED_CABINET_CODES)
def test_each_cabinet_shape_is_recognised(code: str) -> None:
    assert is_cabinet_code(code)


@pytest.mark.parametrize("code", INVENTED_FINISH_CODES)
def test_a_finish_code_is_never_a_cabinet_code(code: str) -> None:
    """**Done when, 1.** A finish code taken for a cabinet code would name a finish as a cabinet
    model."""
    assert is_finish_code(code)
    assert not is_cabinet_code(code)


@pytest.mark.parametrize("code", INVENTED_FINISH_CODES)
def test_a_finish_code_is_refused_even_when_a_cabinet_shape_would_take_it(
    code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exclusion is enforced, not an accident of today's shapes being disjoint. A cabinet shape
    widened to take anything still cannot let a finish code through."""
    monkeypatch.setattr(
        cabinet_codes,
        "CABINET_CODE_SHAPES",
        (*CABINET_CODE_SHAPES, CodeShape(name="anything", description="anything", pattern=".+")),
    )

    assert not cabinet_codes.is_cabinet_code(code)


@pytest.mark.parametrize(
    "text",
    [
        "A1",  # a view or detail mark: one letter, one digit
        "XQ7",  # one digit only
        "XQ04",  # a leading zero
        "XQZ3011",  # three letters: an appliance model number's shape
        "xq24",  # lower case is not what the vendor printed
        " XQ24",  # nor is a space before it
        "XQ24 ",
        "XQ24LL",
        "24XQ",
        "XQ-24",
        "XQ16-0",
        "XQ16-42",
        "",
    ],
)
def test_text_of_no_cabinet_shape_is_not_a_cabinet_code(text: str) -> None:
    assert not is_cabinet_code(text)


def test_every_shape_says_in_words_what_it_matches() -> None:
    """The admin approves the list by reading it, so every shape carries a plain description."""
    shapes = (*CABINET_CODE_SHAPES, *FINISH_CODE_SHAPES)
    assert all(shape.name.strip() and shape.description.strip() for shape in shapes)
    assert len({shape.name for shape in shapes}) == len(shapes)


def test_the_shapes_are_matched_against_the_whole_text() -> None:
    """The whole reading must have the shape: a code inside a longer string is not a code."""
    assert not is_cabinet_code("see XQ24")
    assert not is_cabinet_code("XQ24/XQ30")


# ---------------------------------------------------------------------------
# Cabinets from a chain
# ---------------------------------------------------------------------------


def test_each_dimension_in_a_chain_is_suggested_as_one_cabinet() -> None:
    """Left and right from the dimension, height from how far its witness lines reach."""
    view = _view()
    result = _propose([view], _detect(_chain("0.80", "0.20", "0.35", "0.50")))

    assert _kinds(result) == [PartKind.CABINET, PartKind.CABINET]
    assert [_across(part) for part in result.parts] == [
        (Decimal("0.20"), Decimal("0.35")),
        (Decimal("0.35"), Decimal("0.50")),
    ]
    assert {_down(part) for part in result.parts} == {(Decimal("0.75"), Decimal("0.81"))}
    assert all(part.view_id == view.view_id for part in result.parts)


def test_a_filler_is_suggested_as_a_cabinet_and_the_reason_says_a_person_decides() -> None:
    """Telling a filler from a cabinet by how narrow it is would be reading a width off the
    geometry. So nothing is suggested as a filler, and the person is told to say which."""
    result = _propose([_view()], _detect(_chain("0.80", "0.20", "0.22", "0.50", "0.52")))

    assert PartKind.FILLER not in _kinds(result)
    assert all("filler" in part.reason for part in result.parts)


def test_a_partial_chain_suggests_only_what_it_saw() -> None:
    """**Done when, 4.** Two cabinets of a longer run are dimensioned in a chain, and an overall
    reaches well past them. Outcome: the two, as drawn, and nothing filled in or stretched to the
    overall, which is not a countertop either because it does not end where they end."""
    detected = _detect(
        _chain("0.80", "0.20", "0.30", "0.40"),
        _dimension("0.20", "0.70", "0.88"),
    )

    result = _propose([_view()], detected)

    assert _kinds(result) == [PartKind.CABINET, PartKind.CABINET]
    assert [_across(part) for part in result.parts] == [
        (Decimal("0.20"), Decimal("0.30")),
        (Decimal("0.30"), Decimal("0.40")),
    ]


def test_a_dimension_drawn_on_its_own_is_not_suggested_as_a_cabinet() -> None:
    """A lone dimension could be an overall or a box edge the detector took for one (#748)."""
    result = _propose([_view()], _detect(_dimension("0.20", "0.40", "0.80")))

    assert result.parts == ()


def test_a_vertical_chain_suggests_nothing() -> None:
    """Cabinets run across the page. A chain of heights is not a run of parts."""
    strokes: list[DimensionExtent] = []
    for top, bottom in (("0.20", "0.40"), ("0.40", "0.60")):
        strokes += [
            _stroke("0.80", top, "0.80", bottom),
            _stroke("0.75", top, "0.81", top),
            _stroke("0.75", bottom, "0.81", bottom),
        ]
    detected = _detect(strokes)
    assert detected.chains  # the detector did chain them, so the refusal is this module's

    assert _propose([_view()], detected).parts == ()


# ---------------------------------------------------------------------------
# Which drawings
# ---------------------------------------------------------------------------


def test_only_the_vendors_drawing_is_given_suggestions() -> None:
    detected = _detect(_chain("0.80", "0.20", "0.35", "0.50"))

    assert _propose([_view(vendor=False)], detected).parts == ()
    assert len(_propose([_view(vendor=True)], detected).parts) == 2


def test_a_nested_view_gets_nothing() -> None:
    """**Done when, 3.** A small vendor stamp pasted inside another drawing holds a full chain, an
    overall over it and a code. Outcome: nothing for it, and the reason it was left out. The same
    stamp standing alone gets all of it, so the refusal is about the nesting and not the geometry.
    """
    small = _view(box=("0.15", "0.60", "0.60", "0.95"))
    detected = _detect(
        _chain("0.80", "0.20", "0.35", "0.50"),
        _dimension("0.20", "0.50", "0.88", above=Decimal("0.01")),
    )
    texts = [_code("XQ24", "0.25", "0.30")]

    nested = _propose([_view(vendor=False), small], detected, texts)
    alone = _propose([small], detected, texts)

    assert nested.parts == ()
    assert set(nested.nested) == {small.view_id}
    assert "inside" in nested.nested[small.view_id]
    assert _kinds(alone) == [PartKind.CABINET, PartKind.CABINET, PartKind.COUNTERTOP]
    assert [part.code for part in alone.parts] == [texts[0], None, None]
    assert alone.nested == {}


def test_the_drawing_around_a_nested_one_keeps_what_lies_inside_it() -> None:
    """Whatever lies inside the small stamp lies inside the drawing around it too."""
    outer = _view()
    small = _view(box=("0.15", "0.60", "0.60", "0.95"))

    result = _propose([outer, small], _detect(_chain("0.80", "0.20", "0.35", "0.50")))

    assert [part.view_id for part in result.parts] == [outer.view_id, outer.view_id]
    assert set(result.nested) == {small.view_id}


def test_a_dimension_that_leaves_a_drawing_is_not_its_part() -> None:
    """Only what lies wholly inside the drawing is its own: the second dimension runs out of it."""
    view = _view(box=("0.05", "0.05", "0.45", "0.95"))

    result = _propose([view], _detect(_chain("0.80", "0.20", "0.35", "0.50")))

    assert [_across(part) for part in result.parts] == [(Decimal("0.20"), Decimal("0.35"))]


# ---------------------------------------------------------------------------
# Codes
# ---------------------------------------------------------------------------


def test_a_code_inside_exactly_one_span_is_attached_with_its_reading() -> None:
    code = _code("XQ24", "0.38", "0.42")

    result = _propose([_view()], _detect(_chain("0.80", "0.20", "0.35", "0.50")), [code])

    assert [part.code for part in result.parts] == [None, code]
    assert "XQ24" in result.parts[1].reason


def test_a_code_inside_two_spans_gets_no_code() -> None:
    """**Done when, 2.** Two chains in the lowest row measure overlapping stretches, neither a copy
    of the other: a code over the overlap sits inside a span of each. Outcome: neither cabinet takes
    it, and both say why. A code over a stretch only one of them measures still attaches, so the
    refusal is about the two spans."""
    shared = _code("XQ24", "0.22", "0.28")
    single = _code("XQ30L", "0.44", "0.48")
    detected = _detect(
        _chain("0.80", "0.20", "0.35", "0.50"),
        _chain("0.8015", "0.20", "0.30", "0.42"),
    )

    result = _propose([_view()], detected, [shared, single])

    holders = [part for part in result.parts if _across(part)[0] == Decimal("0.20")]
    assert len(holders) == 2
    assert all(part.code is None for part in holders)
    assert all("one other suggested cabinet" in part.reason for part in holders)
    assert [part.code for part in result.parts if part.code is not None] == [single]


def test_two_codes_inside_one_span_give_it_no_code() -> None:
    """Which of two codes over one cabinet is its own is not something geometry can say."""
    first, second = _code("XQ24", "0.22", "0.26"), _code("XQ30L", "0.28", "0.32")

    result = _propose([_view()], _detect(_chain("0.80", "0.20", "0.35", "0.50")), [first, second])

    assert result.parts[0].code is None
    assert "'XQ24'" in result.parts[0].reason and "'XQ30L'" in result.parts[0].reason


def test_a_code_across_a_joint_is_inside_no_span() -> None:
    """Straddling two cabinets, it sits wholly inside neither, and neither takes it."""
    result = _propose(
        [_view()],
        _detect(_chain("0.80", "0.20", "0.35", "0.50")),
        [_code("XQ24", "0.33", "0.37")],
    )

    assert [part.code for part in result.parts] == [None, None]


def test_a_finish_code_over_a_cabinet_is_not_its_code() -> None:
    result = _propose(
        [_view()],
        _detect(_chain("0.80", "0.20", "0.35", "0.50")),
        [_code("QV-118-ZZZ", "0.22", "0.30")],
    )

    assert [part.code for part in result.parts] == [None, None]


def test_a_code_outside_the_drawing_is_not_attached() -> None:
    view = _view(box=("0.05", "0.75", "0.95", "0.95"))

    result = _propose(
        [view],
        _detect(_chain("0.80", "0.20", "0.35", "0.50", above=Decimal("0.01"))),
        [_code("XQ24", "0.22", "0.30", y="0.50")],
    )

    assert [part.code for part in result.parts] == [None, None]


def test_a_code_never_changes_a_parts_extent() -> None:
    """No width is read from a code: two codes whose digits differ leave the same boxes."""
    detected = _detect(_chain("0.80", "0.20", "0.35", "0.50"))

    narrow = _propose([_view()], detected, [_code("XQ12", "0.22", "0.30")])
    wide = _propose([_view()], detected, [_code("XQ96", "0.22", "0.30")])

    assert [part.extent for part in narrow.parts] == [part.extent for part in wide.parts]


def test_a_reason_names_at_most_three_codes() -> None:
    """The reason is stored in a 500-character column; the fourth code onward is counted."""
    codes = [
        _code(
            f"XQ{20 + index}",
            str(Decimal("0.21") + Decimal("0.03") * index),
            str(Decimal("0.22") + Decimal("0.03") * index),
        )
        for index in range(4)
    ]

    result = _propose([_view()], _detect(_chain("0.80", "0.20", "0.35", "0.50")), codes)

    assert "and 1 more" in result.parts[0].reason
    assert "'XQ23'" not in result.parts[0].reason


# ---------------------------------------------------------------------------
# Countertops
# ---------------------------------------------------------------------------


def test_a_dimension_over_two_cabinets_end_to_end_is_suggested_as_a_countertop() -> None:
    result = _propose(
        [_view()],
        _detect(
            _chain("0.80", "0.20", "0.35", "0.50"),
            _dimension("0.20", "0.50", "0.88", above=Decimal("0.01")),
        ),
    )

    assert _kinds(result) == [PartKind.CABINET, PartKind.CABINET, PartKind.COUNTERTOP]
    assert result.parts[2].code is None


def test_a_countertop_is_never_the_union_of_its_members() -> None:
    """**Done when, 5.** The overall stops a hair inside its cabinets' outer ends, within the
    tolerance, and its own witness lines are short. Outcome: the countertop's box is its own
    dimension's ends and its own witness lines' reach, never the cabinets' outline. Taken from them,
    "the run reaches both ends of the countertop" would be true by construction."""
    detected = _detect(
        _chain("0.80", "0.20", "0.35", "0.50", above=Decimal("0.30")),
        _dimension("0.2015", "0.4985", "0.88", above=Decimal("0.01"), below=Decimal("0.01")),
    )

    result = _propose([_view()], detected)

    (top,) = [part for part in result.parts if part.kind is PartKind.COUNTERTOP]
    cabinets = [part for part in result.parts if part.kind is PartKind.CABINET]
    union_across = (min(_across(p)[0] for p in cabinets), max(_across(p)[1] for p in cabinets))
    union_down = (min(_down(p)[0] for p in cabinets), max(_down(p)[1] for p in cabinets))
    assert _across(top) == (Decimal("0.2015"), Decimal("0.4985"))
    assert _down(top) == (Decimal("0.87"), Decimal("0.89"))
    assert _across(top) != union_across
    assert _down(top) != union_down
    assert top.defining_line.start.y == Decimal("0.88")


def test_one_cabinet_under_an_overall_is_not_a_countertop() -> None:
    """A dimension that repeats one cabinet's is the same dimension drawn twice, not a run."""
    detected = _detect(
        _chain("0.80", "0.20", "0.35", "0.50"),
        _dimension("0.20", "0.35", "0.88", above=Decimal("0.01")),
    )

    assert PartKind.COUNTERTOP not in _kinds(_propose([_view()], detected))


def test_an_overall_over_a_gap_is_not_a_countertop() -> None:
    """Two chains with a hole between them: the overall's ends match, but the cabinets do not meet."""
    detected = _detect(
        _chain("0.80", "0.20", "0.30", "0.40"),
        _chain("0.80", "0.45", "0.55", "0.60"),
        _dimension("0.20", "0.60", "0.88", above=Decimal("0.01")),
    )

    assert PartKind.COUNTERTOP not in _kinds(_propose([_view()], detected))


def test_a_countertop_is_suggested_only_over_the_lowest_row() -> None:
    """The countertop rule is applied to the base cabinets' row. An overall over the row above it is
    not a countertop, even though that row's dimensions are drawn end to end beneath it."""
    detected = _detect(
        _chain("0.60", "0.20", "0.35", "0.50", above=SHORT),
        _dimension("0.20", "0.50", "0.52", above=SHORT),
        _chain("0.80", "0.20", "0.30", "0.40"),
        _dimension("0.20", "0.40", "0.88", above=SHORT),
    )

    tops = [
        part for part in _propose([_view()], detected).parts if part.kind is PartKind.COUNTERTOP
    ]

    assert [_across(top) for top in tops] == [(Decimal("0.20"), Decimal("0.40"))]


def test_a_dimension_in_a_chain_is_never_also_a_countertop() -> None:
    """One suggestion per dimension: a chained one is a cabinet, even over two smaller ones in the
    same row."""
    detected = _detect(
        _chain("0.80", "0.20", "0.30", "0.40"),
        _chain("0.8015", "0.10", "0.20", "0.40"),
    )

    result = _propose([_view()], detected)

    assert (Decimal("0.20"), Decimal("0.40")) in [_across(part) for part in result.parts]
    assert _kinds(result) == [PartKind.CABINET] * 4


# ---------------------------------------------------------------------------
# The lowest row only, and a dimension drawn twice counted once (#868, admin, 2026-10-03)
# ---------------------------------------------------------------------------


def test_an_upper_row_gets_nothing() -> None:
    """Wall cabinets, doors and drawers are dimensioned in rows above the base cabinets, and a wall
    cabinet often lines up with the base cabinet below it. Outcome: only the lowest chained row is
    suggested, the base cabinet is not mistaken for a copy of the wall cabinet above it, and a code
    over an upper cabinet alone attaches to nothing, because no suggested cabinet lies beneath it.
    """
    upper_code = _code("XQ30L", "0.42", "0.48", y="0.55")
    detected = _detect(
        _chain("0.60", "0.20", "0.30", "0.50", above=SHORT),
        _chain("0.80", "0.20", "0.30", "0.40"),
    )

    result = _propose([_view()], detected, [upper_code])

    assert [_across(part) for part in result.parts] == [
        (Decimal("0.20"), Decimal("0.30")),
        (Decimal("0.30"), Decimal("0.40")),
    ]
    assert {part.defining_line.start.y for part in result.parts} == {Decimal("0.80")}
    assert all(part.code is None for part in result.parts)


def test_chains_level_with_the_lowest_one_share_its_row() -> None:
    """Two chains a hair apart down the page, within the tolerance, are one row: both are base
    cabinets. A chain further up than the tolerance is not."""
    detected = _detect(
        _chain("0.80", "0.20", "0.30", "0.40"),
        _chain("0.8015", "0.50", "0.60", "0.70"),
        _chain("0.7975", "0.75", "0.80", "0.85"),
    )

    result = _propose([_view()], detected)

    assert [_across(part)[0] for part in result.parts] == [
        Decimal("0.20"),
        Decimal("0.30"),
        Decimal("0.50"),
        Decimal("0.60"),
    ]


def test_a_dimension_drawn_twice_is_suggested_once() -> None:
    """The row is drawn as two strokes a hair apart, each end within the tolerance of its copy.
    Outcome: one cabinet per dimension, standing on the first stroke down the page, and the code
    over the first cabinet attaches. Counted twice, that code would sit inside two spans."""
    code = _code("XQ24", "0.22", "0.28")
    detected = _detect(
        _chain("0.80", "0.20", "0.35", "0.50"),
        _chain("0.8014", "0.2015", "0.3515", "0.5015"),
    )

    result = _propose([_view()], detected, [code])

    assert [_across(part) for part in result.parts] == [
        (Decimal("0.20"), Decimal("0.35")),
        (Decimal("0.35"), Decimal("0.50")),
    ]
    assert {part.defining_line.start.y for part in result.parts} == {Decimal("0.80")}
    assert [part.code for part in result.parts] == [code, None]
    assert all("Drawn as 2 strokes" in part.reason for part in result.parts)


def test_a_dimension_is_in_a_chain_if_either_of_its_strokes_is() -> None:
    """The copy drawn first down the page touches nothing end to end, so the detector leaves it out
    of every chain, while the copy below is in the row's chain. Outcome: still a cabinet, standing
    on the first copy."""
    detected = _detect(
        _chain("0.80", "0.20", "0.35", "0.50"),
        [_stroke("0.35", "0.7986", "0.50", "0.7986")],
    )
    lone = [
        line
        for line in detected.lines
        if line.extent.start.y == Decimal("0.7986")
        and all(line not in chain.lines for chain in detected.chains)
    ]
    assert lone  # the premise: the first copy is in no chain

    result = _propose([_view()], detected)

    assert [_across(part) for part in result.parts] == [
        (Decimal("0.20"), Decimal("0.35")),
        (Decimal("0.35"), Decimal("0.50")),
    ]
    assert result.parts[1].defining_line.start.y == Decimal("0.7986")


def test_a_countertop_drawn_twice_is_suggested_once() -> None:
    detected = _detect(
        _chain("0.80", "0.20", "0.35", "0.50"),
        _dimension("0.20", "0.50", "0.88", above=SHORT),
        _dimension("0.2012", "0.4988", "0.8814", above=SHORT),
    )

    tops = [
        part for part in _propose([_view()], detected).parts if part.kind is PartKind.COUNTERTOP
    ]

    assert [_across(top) for top in tops] == [(Decimal("0.20"), Decimal("0.50"))]


def test_strokes_further_apart_than_the_tolerance_stay_two_dimensions() -> None:
    """The tolerance is the boundary: ends three thousandths apart are two dimensions."""
    detected = _detect(
        _chain("0.80", "0.20", "0.35", "0.50"),
        _chain("0.8005", "0.203", "0.353", "0.503"),
    )

    assert len(_propose([_view()], detected).parts) == 4


# ---------------------------------------------------------------------------
# Every reason fits, and the caller's mistakes raise
# ---------------------------------------------------------------------------


def test_the_longest_reasons_still_fit_their_column() -> None:
    """Every reason is stored in a 500-character column, so each long form is built here: five of
    the longest code there is over one cabinet, one code over two, and a row and an overall each
    drawn twice."""
    longest = "XQ123P4567L"
    assert is_cabinet_code(longest)
    many = [_code(longest, f"0.2{index}", f"0.2{index}5") for index in range(1, 6)]
    shared = _code(longest, "0.37", "0.39")
    detected = _detect(
        _chain("0.80", "0.20", "0.35", "0.50"),
        _chain("0.8014", "0.2015", "0.3515", "0.5015"),
        _chain("0.8018", "0.36", "0.40", "0.42", above=SHORT),
        _dimension("0.20", "0.50", "0.88", above=SHORT),
        _dimension("0.2012", "0.4988", "0.8814", above=SHORT),
    )

    reasons = [part.reason for part in _propose([_view()], detected, [*many, shared]).parts]

    assert any("and 2 more" in reason and "Drawn as 2 strokes" in reason for reason in reasons)
    assert any("one other suggested cabinet" in reason for reason in reasons)
    assert any("countertop" in reason and "Drawn as 2 strokes" in reason for reason in reasons)
    assert all(0 < len(reason) <= 500 for reason in reasons), max(map(len, reasons))


@pytest.mark.parametrize(
    "tolerance", [0.002, Decimal("NaN"), Decimal("Infinity"), Decimal("-0.001")]
)
def test_a_tolerance_that_is_not_a_finite_decimal_raises(tolerance: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        propose_parts([_view()], _detect(), [], edge_tolerance=tolerance)  # type: ignore[arg-type]


def test_a_drawing_listed_twice_raises() -> None:
    view = _view()

    with pytest.raises(ValueError, match="twice"):
        _propose([view, view], _detect())


def test_geometry_from_another_page_raises() -> None:
    other = ViewOutline(
        view_id=uuid4(), region=_box("0.05", "0.05", "0.95", "0.95", page=PAGE + 1), vendor=True
    )

    with pytest.raises(PolygonSpaceMismatchError):
        _propose([_view(), other], _detect())


def test_the_result_carries_the_line_that_defined_each_part() -> None:
    result = _propose([_view()], _detect(_chain("0.80", "0.20", "0.35", "0.50")))

    assert [(part.defining_line.start.x, part.defining_line.end.x) for part in result.parts] == [
        (Decimal("0.20"), Decimal("0.35")),
        (Decimal("0.35"), Decimal("0.50")),
    ]
    assert all(isinstance(part.view_id, UUID) for part in result.parts)
