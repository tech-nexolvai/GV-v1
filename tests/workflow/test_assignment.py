"""What a proposed reading-to-field assignment has to survive (#589).

Verification for: `workflow/assignment.py`.

The one to read first is `test_a_run_gathered_from_two_places_is_refused`. It is the check the
dimension geometry was built for: `cabinet_widths` is not three plausible widths off the same sheet,
it is the widths along *one run in order*, because `CT-WIDTH-001` compares two runs position by
position. A proposal that gathered them from three unrelated places would produce a check that
compares the second cabinet against the fifth — and every number in it would be real.

Nothing here inspects a value. These tests are about whether the *shape* of an answer is possible;
what the numbers then say is the deterministic engine's business.
"""

from __future__ import annotations

import pytest

from workflow.assignment import (
    AcceptedAssignment,
    AssignmentContext,
    AssignmentRefused,
    Field,
    ProposedAssignment,
    Reading,
    guard_assignment,
)

DEPTH = Field(key="SHOP:CT010", name="countertop_depth", source="SHOP", many=False)
CABINETS = Field(key="SHOP:cabinet_width", name="cabinet_widths", source="SHOP", many=True)
DESIGN = Field(key="ARCH:CT001", name="design_width", source="ARCH", many=False)


def _reading(
    candidate_id: str,
    value: str,
    *,
    source: str = "SHOP",
    line_key: str | None = "line-1",
    chain_key: str | None = None,
    order: int | None = None,
    geometry_available: bool = True,
) -> Reading:
    return Reading(
        candidate_id=candidate_id,
        value=value,
        source=source,
        page=1,
        line_key=line_key,
        chain_key=chain_key,
        order=order,
        geometry_available=geometry_available,
    )


def _context(
    *readings: Reading, fields: tuple[Field, ...] = (DEPTH, CABINETS, DESIGN)
) -> AssignmentContext:
    return AssignmentContext(fields=fields, readings=readings)


def _guard(context: AssignmentContext, *proposals: ProposedAssignment):
    return guard_assignment(context, proposals)


# ---------------------------------------------------------------------------
# Accepted
# ---------------------------------------------------------------------------


def test_a_well_formed_assignment_is_accepted() -> None:
    """Input: one reading, right sheet, attached, single-valued field. Outcome: accepted."""
    context = _context(_reading("c1", "25 1/2 in"))

    result = _guard(context, ProposedAssignment("SHOP:CT010", ("c1",)))

    assert isinstance(result, AcceptedAssignment)
    assert result.assignments[0].candidate_ids == ("c1",)


def test_a_run_drawn_as_one_chain_in_order_is_accepted() -> None:
    """**The case the geometry exists to permit.** Input: three readings along one chain, in order.

    This is what `cabinet_widths` looks like when it is real: dimensions drawn end to end, which
    `dimension_lines` finds without reading any value, in the order the drawing draws them.
    """
    context = _context(
        _reading("c1", "15 in", chain_key="chain-a", order=0),
        _reading("c2", "36 in", chain_key="chain-a", order=1),
        _reading("c3", "18 in", chain_key="chain-a", order=2),
    )

    result = _guard(context, ProposedAssignment("SHOP:cabinet_width", ("c1", "c2", "c3")))

    assert isinstance(result, AcceptedAssignment)


def test_readings_in_no_chain_are_not_refused_for_that_alone() -> None:
    """Outcome: accepted.

    A drawing that dimensions its parts without chaining them is one this cannot check. That is a
    limit worth stating rather than a reason to manufacture a refusal — the reviewer still sees the
    proposal and still confirms it.
    """
    context = _context(_reading("c1", "15 in"), _reading("c2", "36 in"))

    result = _guard(context, ProposedAssignment("SHOP:cabinet_width", ("c1", "c2")))

    assert isinstance(result, AcceptedAssignment)


# ---------------------------------------------------------------------------
# The seven refusals
# ---------------------------------------------------------------------------


def test_a_run_gathered_from_two_places_is_refused() -> None:
    """**The check the geometry was built for.** Input: two readings from different chains.

    `CT-WIDTH-001` compares two runs position by position. Values gathered from unrelated places
    would produce a check comparing the second cabinet against the fifth, with every number in it
    real and every one in the wrong slot. Nothing downstream could catch that.
    """
    context = _context(
        _reading("c1", "15 in", chain_key="chain-a", order=0),
        _reading("c2", "36 in", chain_key="chain-b", order=0),
    )

    result = _guard(context, ProposedAssignment("SHOP:cabinet_width", ("c1", "c2")))

    assert isinstance(result, AssignmentRefused)
    assert "separate runs" in result.reason


def test_a_run_proposed_out_of_order_is_refused() -> None:
    """Input: a chain's readings in the wrong order. Outcome: refused.

    Position is what the check compares, so the order is a fact about the sheet rather than a
    presentation choice a model may vary.
    """
    context = _context(
        _reading("c1", "15 in", chain_key="chain-a", order=0),
        _reading("c2", "36 in", chain_key="chain-a", order=1),
    )

    result = _guard(context, ProposedAssignment("SHOP:cabinet_width", ("c2", "c1")))

    assert isinstance(result, AssignmentRefused)
    assert "out of the order" in result.reason


def test_a_reading_from_the_wrong_sheet_is_refused() -> None:
    """**Input: a shop reading for an architectural field. Outcome: refused.**

    Which sheet a reading came off is recorded by the reader, never inferred. This is the check most
    likely to fire and the one a model is most likely to get wrong quietly — the numbers on the two
    drawings are similar by construction, because comparing them is the whole job.
    """
    context = _context(_reading("c1", "36 in", source="SHOP"))

    result = _guard(context, ProposedAssignment("ARCH:CT001", ("c1",)))

    assert isinstance(result, AssignmentRefused)
    assert "ARCH drawing" in result.reason


def test_an_unattached_reading_cannot_fill_anything() -> None:
    """**Input: a reading attached to no dimension line. Outcome: refused.**

    `text_association` already declined to say what it annotates — it is a number floating on the
    sheet, a title block or a scale bar. A model may not overrule that refusal by using it.
    """
    context = _context(_reading("c1", "25 1/2 in", line_key=None))

    result = _guard(context, ProposedAssignment("SHOP:CT010", ("c1",)))

    assert isinstance(result, AssignmentRefused)
    assert "not attached to any dimension line" in result.reason


def test_one_reading_cannot_fill_two_fields() -> None:
    """Input: the same reading proposed twice. Outcome: refused. One reading measures one thing."""
    context = _context(_reading("c1", "36 in", chain_key="chain-a", order=0))

    result = _guard(
        context,
        ProposedAssignment("SHOP:CT010", ("c1",)),
        ProposedAssignment("SHOP:cabinet_width", ("c1",)),
    )

    assert isinstance(result, AssignmentRefused)
    assert "One reading measures one thing" in result.reason


def test_a_single_valued_field_given_several_readings_is_refused() -> None:
    """Input: three readings for a field that takes one. Outcome: refused.

    Not a near-miss — a model offering three values for a single quantity has misunderstood what the
    field is, and the rest of its proposal is not more trustworthy for it.
    """
    context = _context(_reading("c1", "25 in"), _reading("c2", "26 in"), _reading("c3", "27 in"))

    result = _guard(context, ProposedAssignment("SHOP:CT010", ("c1", "c2", "c3")))

    assert isinstance(result, AssignmentRefused)
    assert "takes one value" in result.reason


def test_a_field_this_rulebook_does_not_ask_for_is_refused() -> None:
    """Input: an invented field key. Outcome: refused.

    The published rulebook is the list of things that can be filled. A model naming something else
    has produced a value with no check to consume it.
    """
    context = _context(_reading("c1", "36 in"))

    result = _guard(context, ProposedAssignment("SHOP:CT999", ("c1",)))

    assert isinstance(result, AssignmentRefused)
    assert "does not ask for" in result.reason


def test_a_reading_this_run_did_not_produce_is_refused() -> None:
    """**Input: an invented candidate id. Outcome: refused.**

    The one that would otherwise be a hallucinated measurement entering the engine with a real
    field name attached to it.
    """
    context = _context(_reading("c1", "36 in"))

    result = _guard(context, ProposedAssignment("SHOP:CT010", ("ghost",)))

    assert isinstance(result, AssignmentRefused)
    assert "this run did not produce" in result.reason


def test_a_field_proposed_with_no_readings_is_refused() -> None:
    """Input: an empty assignment. Outcome: refused rather than silently accepted.

    An empty proposal and no proposal reach the same screen, so accepting one would put a model in
    the record as having decided something it declined to decide.
    """
    context = _context(_reading("c1", "36 in"))

    result = _guard(context, ProposedAssignment("SHOP:CT010", ()))

    assert isinstance(result, AssignmentRefused)
    assert "no reading" in result.reason


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def test_the_whole_proposal_is_refused_not_the_bad_rows() -> None:
    """**Outcome: one bad assignment discards the good one with it.**

    Keeping the rows that passed would present a partly model-assigned form as though a person had
    checked it, and the reviewer would have no way to tell which fields were which. Narration
    refuses as a batch for the same reason.
    """
    context = _context(
        _reading("c1", "25 1/2 in"),
        _reading("c2", "36 in", source="ARCH"),
    )

    result = _guard(
        context,
        ProposedAssignment("SHOP:CT010", ("c1",)),
        ProposedAssignment("SHOP:cabinet_width", ("c2",)),
    )

    assert isinstance(result, AssignmentRefused)


def test_the_rule_arithmetic_is_not_something_a_field_can_carry() -> None:
    """**The circularity guard.** Outcome: `Field` has nowhere to put a formula.

    `CT-WIDTH-001` checks that a countertop width equals the sum of the cabinets and fillers. A
    model that knew the equation could assign readings so that it balances — and the check would
    then confirm the balance, passing on every drawing including one with a real error in it.

    The field's name and description are safe and useful; its formula is not. Asserted on the type
    rather than on a prompt, because a prompt is not a place a constraint can be kept.
    """
    assert set(Field.__dataclass_fields__) == {
        "key",
        "name",
        "source",
        "many",
        "description",
    }

    with pytest.raises(TypeError):
        Field(  # type: ignore[call-arg]
            key="SHOP:CT010",
            name="countertop_depth",
            source="SHOP",
            many=False,
            operation="sum",
        )


def test_nothing_in_the_guard_reads_a_value() -> None:
    """Outcome: the checks decide whether a shape is possible, never whether a number is right.

    Read off the identifiers the module references rather than its text, because the docstrings say
    "value" repeatedly while explaining exactly this — and a guard a comment can trip is a guard
    somebody deletes.
    """
    import ast
    import inspect
    import textwrap

    from workflow import assignment

    referenced: set[str] = set()
    for function in (assignment.guard_assignment, assignment._run_is_drawn_as_one):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        referenced |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    # `value` is read only to *quote* a reading back in a refusal message. Arithmetic on one — a
    # comparison, a sum, a conversion — would be this module deciding something.
    assert "exact" not in referenced
    assert "value_numerator" not in referenced


# ---------------------------------------------------------------------------
# A check that could not run is not a check that failed
# ---------------------------------------------------------------------------


def test_an_unattached_reading_on_a_page_with_no_geometry_is_not_refused() -> None:
    """**Input: an unattached reading whose page had no line-work. Outcome: accepted, and marked.**

    Measured on a real uploaded pair: `page texts=0, segments=0, annotation strokes=0` on both
    sheets — scanned images in a PDF wrapper. The detector had nothing to detect, so nothing
    attached, so this module refused every proposal and the form stayed empty. The refusal was
    reporting an examination that never happened.

    Where the geometry is absent the attachment check abstains and the field comes back in
    `unverified_placement`. Every other check still applied to get here, and a reviewer still
    confirms the value before anything is saved.
    """
    context = _context(_reading("c1", "25 1/2 in", line_key=None, geometry_available=False))

    result = _guard(context, ProposedAssignment("SHOP:CT010", ("c1",)))

    assert isinstance(result, AcceptedAssignment)
    assert result.unverified_placement == ("SHOP:CT010",)


def test_an_unattached_reading_on_a_page_that_has_dimension_lines_is_still_refused() -> None:
    """**The half that must not move.** Outcome: refused, exactly as before.

    A number unattached on a page with line-work sits near none of it — a title block, a revision
    note, a scale bar. The drawing was examined and did not vouch for it, and that is a finding.
    Widening the abstention to cover this case would be the safety property quietly disappearing.
    """
    context = _context(_reading("c1", "25 1/2 in", line_key=None, geometry_available=True))

    result = _guard(context, ProposedAssignment("SHOP:CT010", ("c1",)))

    assert isinstance(result, AssignmentRefused)
    assert "not attached to any dimension line" in result.reason


def test_an_attached_reading_is_never_marked_unverified() -> None:
    """Outcome: accepted with an empty `unverified_placement`.

    The mark means "nothing confirmed where this sits". A reading the geometry did vouch for must
    not carry it, or the screen would warn about every value and the warning would stop meaning
    anything.
    """
    context = _context(_reading("c1", "25 1/2 in"))

    result = _guard(context, ProposedAssignment("SHOP:CT010", ("c1",)))

    assert isinstance(result, AcceptedAssignment)
    assert result.unverified_placement == ()


def test_only_the_fields_that_could_not_be_checked_are_marked() -> None:
    """**Outcome: one field marked, the other not, in one accepted proposal.**

    A package is two drawings and they need not be the same kind of file: a vector shop drawing
    beside a scanned architectural one is ordinary. The mark is per field, so a reviewer is warned
    about exactly the values nothing vouched for and no others.
    """
    context = _context(
        _reading("c1", "25 1/2 in"),
        _reading("c2", "36 in", source="ARCH", line_key=None, geometry_available=False),
    )

    result = _guard(
        context,
        ProposedAssignment("SHOP:CT010", ("c1",)),
        ProposedAssignment("ARCH:CT001", ("c2",)),
    )

    assert isinstance(result, AcceptedAssignment)
    assert result.unverified_placement == ("ARCH:CT001",)
