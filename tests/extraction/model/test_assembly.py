"""Which cabinets sit beneath a countertop — and, mostly, when the resolver refuses to say.

§9 of `docs/DESIGN_EXTRACTION.md` requires every resolver that can fail to have its *cannot-resolve*
path asserted, not only its success path. Here that is not a formality: a run one cabinet short is
summed exactly, traced faithfully and reported with confidence, and nothing downstream can catch it.
So most of this file is about refusals, and the partial-run refusal is the test the story exists for.

The fixtures are deliberately geometric rather than realistic. `data/drawings/` is empty, and a
fixture shaped like a real elevation would be encoding today's guess about real elevations as ground
truth — §9 again. These check the logic; the characterisation tests arrive with the drawings.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from evidence.coordinates import StoredPoint
from evidence.polygon import Polygon, PolygonSpaceMismatchError
from extraction.model.assembly import (
    Assembly,
    AssemblyMember,
    CannotResolve,
    DrawingContext,
    resolve_assembly,
)
from extraction.model.items import DrawingItem, IdentifierKind, PrintedIdentifier, ViewIdentity
from vocabulary.semantic_types import SemanticType

DOCUMENT = uuid4()
PAGE = 3
TOLERANCE = Decimal("0.001")

CABINET = SemanticType.CT003
FILLER = SemanticType.CT002
COUNTERTOP = SemanticType.COUNTERTOP_OVERALL_WIDTH


def _d(value: str) -> Decimal:
    return Decimal(value)


def _item(
    x0: str,
    x1: str,
    kind: SemanticType = CABINET,
    *,
    y0: str = "0.20",
    y1: str = "0.30",
    tag: str = "D",
    page: int = PAGE,
    document: UUID | None = None,
    identifiers: tuple[PrintedIdentifier, ...] = (),
) -> DrawingItem:
    """A rectangle in stored space. Only the x extent and the view carry meaning here."""
    resolved = document or DOCUMENT
    return DrawingItem(
        view=ViewIdentity(document_version_id=resolved, page=page, tag=tag),
        item_type=kind,
        extent=Polygon(
            points=(
                StoredPoint(_d(x0), _d(y0)),
                StoredPoint(_d(x1), _d(y0)),
                StoredPoint(_d(x1), _d(y1)),
                StoredPoint(_d(x0), _d(y1)),
            ),
            space="stored",
            document_version_id=resolved,
            page=page,
        ),
        identifiers=identifiers,
    )


def _countertop(x0: str = "0.10", x1: str = "0.40", **kwargs: object) -> DrawingItem:
    return _item(x0, x1, COUNTERTOP, y0="0.30", y1="0.34", **kwargs)  # type: ignore[arg-type]


def _ctx(*items: DrawingItem) -> DrawingContext:
    return DrawingContext(document_version_id=DOCUMENT, items=items)


def _three_cabinets() -> tuple[DrawingItem, DrawingItem, DrawingItem]:
    return _item("0.10", "0.20"), _item("0.20", "0.30"), _item("0.30", "0.40")


# ---------------------------------------------------------------------------
# The run it does resolve
# ---------------------------------------------------------------------------


def test_the_run_beneath_a_countertop_resolves_in_left_to_right_order() -> None:
    """A6.4 distributes fillers positionally, so the order is part of the answer rather than a
    presentation detail. Passed in back to front to prove it is sorted rather than echoed."""
    top = _countertop()
    left, middle, right = _three_cabinets()

    result = resolve_assembly(top, _ctx(top, right, middle, left), edge_tolerance=TOLERANCE)

    assert isinstance(result, Assembly)
    assert [member.item for member in result.run] == [left, middle, right]
    assert [member.position for member in result.run] == [0, 1, 2]


def test_the_order_is_stable_across_the_order_the_items_arrive_in() -> None:
    """Two contexts holding the same drawing must produce the same run, or a rule's expected value
    would depend on which order extraction happened to emit."""
    top = _countertop()
    left, middle, right = _three_cabinets()

    first = resolve_assembly(top, _ctx(top, left, middle, right), edge_tolerance=TOLERANCE)
    second = resolve_assembly(top, _ctx(top, right, left, middle), edge_tolerance=TOLERANCE)

    assert isinstance(first, Assembly)
    assert isinstance(second, Assembly)
    assert [m.item.id for m in first.run] == [m.item.id for m in second.run]


def test_fillers_and_cabinets_are_both_members() -> None:
    """CT-1 sums fillers alongside cabinets. A resolver that returned only the cabinets would
    produce a run that is short by both fillers and looks complete."""
    top = _countertop()
    left_filler = _item("0.10", "0.12", FILLER)
    cabinet = _item("0.12", "0.38")
    right_filler = _item("0.38", "0.40", FILLER)

    result = resolve_assembly(
        top, _ctx(top, left_filler, cabinet, right_filler), edge_tolerance=TOLERANCE
    )

    assert isinstance(result, Assembly)
    assert [member.item.item_type for member in result.run] == [FILLER, CABINET, FILLER]


def test_every_member_records_the_signal_that_included_it() -> None:
    """The acceptance criterion, and §2.4. A reviewer shown a finding has to be able to check why a
    cabinet is in the sum, and "the geometry matched" is not something anybody can check."""
    top = _countertop()
    left, middle, right = _three_cabinets()

    result = resolve_assembly(top, _ctx(top, left, middle, right), edge_tolerance=TOLERANCE)

    assert isinstance(result, Assembly)
    assert set(result.signals) == {left.id, middle.id, right.id}
    for signal in result.signals.values():
        assert len(signal.split()) > 8, "a signal has to be a sentence, not a label"
        assert "view D" in signal, "which view grouped it is half the reason"


def test_a_printed_identifier_appears_in_the_signal() -> None:
    """Identifiers cannot establish membership here, but they are how a reviewer finds the cabinet
    on the sheet, so they belong in the reason."""
    top = _countertop()
    left = _item(
        "0.10",
        "0.25",
        identifiers=(PrintedIdentifier(IdentifierKind.MARK, "B-12"),),
    )
    right = _item("0.25", "0.40")

    result = resolve_assembly(top, _ctx(top, left, right), edge_tolerance=TOLERANCE)

    assert isinstance(result, Assembly)
    assert "B-12" in result.signals[left.id]
    assert "nothing printed" in result.signals[right.id]


def test_items_outside_the_countertop_span_are_not_members() -> None:
    """A cabinet on the other side of the same elevation belongs to another countertop. Sweeping it
    in would inflate the expected width by a whole cabinet."""
    top = _countertop("0.10", "0.40")
    left, middle, right = _three_cabinets()
    elsewhere = _item("0.60", "0.70")

    result = resolve_assembly(
        top, _ctx(top, left, middle, right, elsewhere), edge_tolerance=TOLERANCE
    )

    assert isinstance(result, Assembly)
    assert elsewhere.id not in result.signals


def test_an_item_on_another_page_is_not_a_member() -> None:
    """Cross-view identity is B7.3's problem. Merging a plan and an elevation here would build one
    run out of two drawings of the same kitchen and double it."""
    top = _countertop()
    left, middle, right = _three_cabinets()
    other_sheet = _item("0.10", "0.40", page=PAGE + 1, tag="E")

    result = resolve_assembly(
        top, _ctx(top, left, middle, right, other_sheet), edge_tolerance=TOLERANCE
    )

    assert isinstance(result, Assembly)
    assert other_sheet.id not in result.signals


# ---------------------------------------------------------------------------
# Refusals — the half that matters
# ---------------------------------------------------------------------------


def test_a_run_with_one_member_missing_refuses_rather_than_returning_a_shorter_run() -> None:
    """**The test this story exists for.**

    The right-hand cabinet was never read. The two that were found are a perfectly good run of two
    — contiguous, ordered, each with a reason — and summing them reports a countertop 300 units
    narrower than it is. The arithmetic would be exact and the answer would be wrong, so the only
    safe result is no result.
    """
    top = _countertop("0.10", "0.40")
    left = _item("0.10", "0.20")
    middle = _item("0.20", "0.30")

    result = resolve_assembly(top, _ctx(top, left, middle), edge_tolerance=TOLERANCE)

    assert isinstance(result, CannotResolve)
    assert set(result.candidates) == {left, middle}, "a reviewer sees what was found"


def test_a_member_missing_from_the_left_end_also_refuses() -> None:
    """Both ends, not just the far one. An implementation checking only the right-hand end passes
    every test written left to right and drops the left filler on every real drawing."""
    top = _countertop("0.10", "0.40")
    middle = _item("0.20", "0.30")
    right = _item("0.30", "0.40")

    result = resolve_assembly(top, _ctx(top, middle, right), edge_tolerance=TOLERANCE)

    assert isinstance(result, CannotResolve)


def test_a_gap_inside_the_run_refuses_rather_than_summing_across_it() -> None:
    """Something between them is missing or was not read. Summing the two either side reports the
    width of three cabinets as the width of two."""
    top = _countertop("0.10", "0.40")
    left = _item("0.10", "0.20")
    right = _item("0.30", "0.40")

    result = resolve_assembly(top, _ctx(top, left, right), edge_tolerance=TOLERANCE)

    assert isinstance(result, CannotResolve)
    assert "gap" in result.reason


def test_overlapping_members_refuse_rather_than_counting_the_shared_part_twice() -> None:
    top = _countertop("0.10", "0.40")
    left = _item("0.10", "0.30")
    right = _item("0.20", "0.40")

    result = resolve_assembly(top, _ctx(top, left, right), edge_tolerance=TOLERANCE)

    assert isinstance(result, CannotResolve)
    assert "twice" in result.reason


def test_a_second_row_of_cabinets_under_the_same_span_refuses() -> None:
    """The module never asks whether a member is vertically below the countertop — stored space
    does not promise which way y points, and a plan view has no "below" at all. This is the case
    that would exploit the gap, and it is caught because two rows overlap along the run."""
    top = _countertop("0.10", "0.40")
    upper = _three_cabinets()
    lower = (
        _item("0.10", "0.20", y0="0.05", y1="0.15"),
        _item("0.20", "0.30", y0="0.05", y1="0.15"),
        _item("0.30", "0.40", y0="0.05", y1="0.15"),
    )

    result = resolve_assembly(top, _ctx(top, *upper, *lower), edge_tolerance=TOLERANCE)

    assert isinstance(result, CannotResolve)


def test_a_cabinet_from_another_view_on_the_same_sheet_refuses() -> None:
    """The disagreement §4.2 leaves open. Two elevations share a sheet, so an item can be inside
    the countertop's span on the page and belong to a different drawing. Which signal wins is
    empirical, so until there are drawings to answer it the resolver abstains."""
    top = _countertop("0.10", "0.40")
    left, middle, right = _three_cabinets()
    other_view = _item("0.15", "0.25", y0="0.60", y1="0.70", tag="E")

    result = resolve_assembly(
        top, _ctx(top, left, middle, right, other_view), edge_tolerance=TOLERANCE
    )

    assert isinstance(result, CannotResolve)
    assert "view E" in result.reason
    assert other_view in result.candidates


def test_two_members_sharing_a_unique_identifier_refuse() -> None:
    """One mark names one physical cabinet. Two of them is the same cabinet read twice or one of
    them misread, and either way summing both counts a cabinet that is not there."""
    top = _countertop("0.10", "0.40")
    mark = PrintedIdentifier(IdentifierKind.MARK, "B-12")
    left = _item("0.10", "0.25", identifiers=(mark,))
    right = _item("0.25", "0.40", identifiers=(mark,))

    result = resolve_assembly(top, _ctx(top, left, right), edge_tolerance=TOLERANCE)

    assert isinstance(result, CannotResolve)
    assert "B-12" in result.reason


def test_a_shared_catalogue_number_is_not_a_contradiction() -> None:
    """Three identical cabinets in a row share a catalogue number by definition. Refusing on that
    would abstain on the most ordinary drawing there is."""
    top = _countertop("0.10", "0.40")
    catalogue = PrintedIdentifier(IdentifierKind.CATALOGUE, "W2430")
    left = _item("0.10", "0.25", identifiers=(catalogue,))
    right = _item("0.25", "0.40", identifiers=(catalogue,))

    result = resolve_assembly(top, _ctx(top, left, right), edge_tolerance=TOLERANCE)

    assert isinstance(result, Assembly)


def test_an_unrecognised_item_inside_the_run_leaves_a_gap_and_refuses() -> None:
    """Whatever sits between the two cabinets is not a type this resolver knows. Skipping it
    silently would sum a run with a hole in it; the hole is a gap, and a gap refuses."""
    top = _countertop("0.10", "0.40")
    left = _item("0.10", "0.20")
    unknown = _item("0.20", "0.30", SemanticType.MATERIAL)
    right = _item("0.30", "0.40")

    result = resolve_assembly(top, _ctx(top, left, unknown, right), edge_tolerance=TOLERANCE)

    assert isinstance(result, CannotResolve)


def test_a_countertop_taller_than_it_is_wide_refuses_rather_than_ordering_up_the_page() -> None:
    """ "Left to right" only means something across the page. Ordering a run up the sheet and calling
    it left-to-right would hand A6.4 the fillers the wrong way round — a wrong answer that looks
    entirely ordinary."""
    top = _item("0.10", "0.20", COUNTERTOP, y0="0.10", y1="0.80")
    cabinet = _item("0.10", "0.20", y0="0.20", y1="0.30")

    result = resolve_assembly(top, _ctx(top, cabinet), edge_tolerance=TOLERANCE)

    assert isinstance(result, CannotResolve)
    assert "left-to-right" in result.reason


def test_a_refusal_carries_the_candidates_so_a_reviewer_sees_the_choice() -> None:
    """A refusal that says only "could not resolve" moves the problem to a human without giving
    them anything to work with. §6: the refusal marks it for confirmation and shows the candidates.
    """
    top = _countertop("0.10", "0.40")
    left = _item("0.10", "0.20")
    right = _item("0.30", "0.40")

    result = resolve_assembly(top, _ctx(top, left, right), edge_tolerance=TOLERANCE)

    assert isinstance(result, CannotResolve)
    assert result.candidates
    assert result.reason.strip()


def test_the_refusal_is_the_marker_for_human_confirmation() -> None:
    """There is one mechanism, not two. `CannotResolve` is what marks an assembly for a person, and
    a second flag alongside it would be one more thing that could be read and ignored."""
    top = _countertop("0.10", "0.40")

    result = resolve_assembly(top, _ctx(top, _item("0.10", "0.20")), edge_tolerance=TOLERANCE)

    assert isinstance(result, CannotResolve)
    assert not isinstance(result, Assembly)


# ---------------------------------------------------------------------------
# Nothing found is not the same as cannot tell
# ---------------------------------------------------------------------------


def test_a_countertop_with_no_cabinets_returns_an_empty_run_not_a_refusal() -> None:
    """Two different facts. "We found nothing" sends a reviewer to the extraction; "we found
    something that does not add up" sends them to the drawing. An empty run also sums to zero,
    which fails a width check loudly rather than passing it quietly."""
    top = _countertop()

    result = resolve_assembly(top, _ctx(top), edge_tolerance=TOLERANCE)

    assert isinstance(result, Assembly)
    assert result.run == ()
    assert dict(result.signals) == {}


# ---------------------------------------------------------------------------
# The tolerance is a stated parameter
# ---------------------------------------------------------------------------


def test_the_edge_tolerance_is_a_required_keyword_argument() -> None:
    """What gap is a joint and what gap is a missing cabinet is empirical, and `data/drawings/` is
    empty. A default would ship today's guess as ground truth; a positional one could be passed by
    accident."""
    parameter = inspect.signature(resolve_assembly).parameters["edge_tolerance"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_the_tolerance_actually_changes_the_answer() -> None:
    """Otherwise it is a parameter in name only. The same drawing resolves or refuses depending on
    what the caller states, which is the point of it being theirs to state."""
    top = _countertop("0.10", "0.40")
    items = (_item("0.10", "0.20"), _item("0.2004", "0.40"))

    assert isinstance(
        resolve_assembly(top, _ctx(top, *items), edge_tolerance=Decimal("0.0001")), CannotResolve
    )
    assert isinstance(
        resolve_assembly(top, _ctx(top, *items), edge_tolerance=Decimal("0.001")), Assembly
    )


def test_a_float_tolerance_is_refused() -> None:
    """Binary rounding deciding whether a run is complete is exactly the class of failure ADR-0001
    exists to prevent."""
    top = _countertop()
    with pytest.raises(TypeError, match="Decimal"):
        resolve_assembly(top, _ctx(top), edge_tolerance=0.001)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_a_tolerance_that_is_not_a_finite_number_is_refused(value: str) -> None:
    """`Decimal("NaN")` is a `Decimal` and `Decimal("NaN") < 0` is `False`, so it would pass both
    other checks — and then every `... > NaN` below is `False` too. Gaps, overlaps and a run that
    falls short of the countertop would all answer the same way, and a run with a cabinet missing
    would be returned as a complete `Assembly`.

    That is this module's whole failure mode wearing the costume of a clean answer, so it is a hard
    refusal rather than a comparison that happens to be safe today. Infinity is the mirror image.
    """
    top = _countertop("0.10", "0.40")
    incomplete = _ctx(top, _item("0.10", "0.20"))

    assert isinstance(resolve_assembly(top, incomplete, edge_tolerance=TOLERANCE), CannotResolve)
    with pytest.raises(ValueError, match="finite"):
        resolve_assembly(top, incomplete, edge_tolerance=Decimal(value))


def test_a_negative_tolerance_is_refused() -> None:
    top = _countertop()
    with pytest.raises(ValueError, match="negative"):
        resolve_assembly(top, _ctx(top), edge_tolerance=Decimal("-0.001"))


# ---------------------------------------------------------------------------
# Caller mistakes raise rather than resolving
# ---------------------------------------------------------------------------


def test_a_countertop_from_another_document_version_raises() -> None:
    """Not ambiguity to mark for review. The run would be assembled out of two documents, one of
    which may well be superseded."""
    top = _countertop(document=uuid4())
    with pytest.raises(PolygonSpaceMismatchError):
        resolve_assembly(top, _ctx(), edge_tolerance=TOLERANCE)


def test_a_context_mixing_two_document_versions_is_refused_at_construction() -> None:
    """Caught where it is introduced rather than where it does damage. A context spanning versions
    would let a cabinet from a superseded sheet join a run drawn on the current one."""
    with pytest.raises(PolygonSpaceMismatchError):
        DrawingContext(
            document_version_id=DOCUMENT, items=(_item("0.10", "0.20", document=uuid4()),)
        )


def test_a_context_listing_one_item_twice_is_refused() -> None:
    """One item listed twice is one cabinet summed twice, and the arithmetic would be exactly
    wrong."""
    cabinet = _item("0.10", "0.20")
    with pytest.raises(ValueError, match="twice"):
        DrawingContext(document_version_id=DOCUMENT, items=(cabinet, cabinet))


def test_asking_for_the_assembly_beneath_a_cabinet_raises() -> None:
    """Nothing sits beneath a cabinet. Answering would produce a run for something that is not a
    countertop, and it would look like every other run."""
    cabinet = _item("0.10", "0.40")
    with pytest.raises(ValueError, match="cabinet or filler"):
        resolve_assembly(cabinet, _ctx(cabinet), edge_tolerance=TOLERANCE)


# ---------------------------------------------------------------------------
# The types refuse to hold a run that would be summed wrongly
# ---------------------------------------------------------------------------


def test_an_assembly_cannot_be_built_with_a_member_that_has_no_signal() -> None:
    """A caller assembling one by hand cannot skip the reason. An inclusion with no reason cannot be
    checked by the person whose signature the finding carries."""
    top = _countertop()
    cabinet = _item("0.10", "0.40")
    with pytest.raises(ValueError, match="recorded signal"):
        Assembly(countertop=top, run=(AssemblyMember(item=cabinet, position=0),), signals={})


def test_an_assembly_cannot_be_built_with_positions_out_of_order() -> None:
    """Positions are what A6.4 distributes fillers by. A run numbered 0, 2 has lost a member and
    still looks well formed."""
    top = _countertop()
    left = _item("0.10", "0.25")
    right = _item("0.25", "0.40")
    with pytest.raises(ValueError, match="ordered left to right"):
        Assembly(
            countertop=top,
            run=(AssemblyMember(item=left, position=0), AssemblyMember(item=right, position=2)),
            signals={left.id: "because", right.id: "because"},
        )


def test_an_assembly_cannot_hold_the_same_item_twice() -> None:
    top = _countertop()
    cabinet = _item("0.10", "0.40")
    with pytest.raises(ValueError, match="twice"):
        Assembly(
            countertop=top,
            run=(
                AssemblyMember(item=cabinet, position=0),
                AssemblyMember(item=cabinet, position=1),
            ),
            signals={cabinet.id: "because"},
        )


def test_an_assembly_cannot_hold_a_member_from_another_view() -> None:
    """The type refuses what the resolver refuses, so a caller building one by hand cannot assemble
    a run out of two drawings."""
    top = _countertop()
    elsewhere = _item("0.10", "0.40", tag="E")
    with pytest.raises(ValueError, match="different view"):
        Assembly(
            countertop=top,
            run=(AssemblyMember(item=elsewhere, position=0),),
            signals={elsewhere.id: "because"},
        )


def test_the_signals_map_cannot_be_changed_after_the_assembly_is_built() -> None:
    """The trace is part of the answer. A map a caller could edit afterwards would let the recorded
    reason drift from the run it describes, and the drift would be invisible."""
    top = _countertop()
    left, middle, right = _three_cabinets()
    result = resolve_assembly(top, _ctx(top, left, middle, right), edge_tolerance=TOLERANCE)

    assert isinstance(result, Assembly)
    with pytest.raises(TypeError):
        result.signals[left.id] = "something else"  # type: ignore[index]


def test_a_member_position_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="counts from zero"):
        AssemblyMember(item=_item("0.10", "0.20"), position=-1)
