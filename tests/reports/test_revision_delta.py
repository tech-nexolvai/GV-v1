"""What changed between two revisions of a package (#186, B11.4).

Three properties matter more than the bucketing: a delta is never presented for two things that are not
comparable, a change always says whether the drawing or the rule moved, and nothing is silently dropped.
The buckets are easy to get right and easy to test; those three are where a delta misleads a reviewer.

Source: `docs/DESIGN_EXTRACTION.md` §7 · Verification: this file
"""

from __future__ import annotations

import pytest

from reports.revision_delta import (
    DIFFERENT_SHEET_SETS,
    DUPLICATE_KEY,
    SAME_REVISION,
    ChangeCause,
    CheckKey,
    KeyedFinding,
    NotComparable,
    RevisionDelta,
    RevisionSide,
    compare_revisions,
)
from verdict.finding import Finding
from verdict.outcomes import Outcome, Severity, is_decision
from verdict.trace import CalculationTrace


def _trace(outcome: Outcome) -> CalculationTrace:
    """The minimum arithmetic a decision must carry.

    `Finding` refuses a PASS or FAIL with no trace — *"a decision a reviewer cannot check by hand is not
    defensible; only an abstention may lack a trace"*. My first version of this helper omitted it and 18
    tests failed at construction, which is the invariant doing exactly its job.
    """
    return CalculationTrace(
        operation="compare_exact",
        operands=(),
        intermediates=(),
        comparison="expected == observed",
        tolerance=None,
        arithmetic_unit=None,
        outcome=outcome,
        engine_version="1.0.0",
        operation_version="1",
    )


def _finding(outcome: Outcome, snapshot: str = "snap-1", rule: str = "CT-1") -> Finding:
    return Finding(
        rule_id=rule,
        outcome=outcome,
        severity=Severity.MAJOR,
        reason="checked",
        snapshot_id=snapshot,
        engine_version="1.0.0",
        # Only a decision needs one, and supplying one for an abstention would be claiming arithmetic
        # that never ran.
        trace=_trace(outcome) if is_decision(outcome) else None,
    )


def _side(
    revision: int,
    *entries: tuple[str, Outcome, str],
    sheets: dict[str, str] | None = None,
) -> RevisionSide:
    """One revision. Entries are `(item, outcome, snapshot)`; the rule is CT-1 unless stated."""
    return RevisionSide(
        revision_number=revision,
        governing_revisions=sheets if sheets is not None else {"A-101": f"R{revision}"},
        findings=tuple(
            KeyedFinding(CheckKey(item, "CT-1"), _finding(outcome, snapshot))
            for item, outcome, snapshot in entries
        ),
    )


# ---------------------------------------------------------------------------
# A delta is refused when the two sides are not comparable
# ---------------------------------------------------------------------------


def test_different_sheet_sets_are_not_comparable() -> None:
    """The third acceptance criterion.

    Every finding on an added sheet would be reported as new, which reads as a package that got worse
    when in fact it got bigger. Refusing is the only honest answer.
    """
    before = _side(1, ("top", Outcome.PASS, "snap-1"), sheets={"A-101": "A"})
    after = _side(2, ("top", Outcome.FAIL, "snap-1"), sheets={"A-101": "B", "A-201": "A"})

    outcome = compare_revisions(before, after)

    assert isinstance(outcome, NotComparable)
    assert outcome.cause == DIFFERENT_SHEET_SETS
    assert not outcome.is_comparable
    assert "A-201" in outcome.detail, "the refusal names the sheet that differs"


def test_a_removed_sheet_is_also_not_comparable() -> None:
    """Both directions. A sheet that vanished is as much a difference as one that appeared."""
    before = _side(1, sheets={"A-101": "A", "A-201": "A"})
    after = _side(2, sheets={"A-101": "B"})

    outcome = compare_revisions(before, after)
    assert isinstance(outcome, NotComparable)
    assert outcome.cause == DIFFERENT_SHEET_SETS


def test_comparing_a_revision_with_itself_is_refused() -> None:
    """An all-unchanged delta reads like a reviewed re-submission. Nothing was submitted."""
    side = _side(1, ("top", Outcome.PASS, "snap-1"))

    outcome = compare_revisions(side, side)

    assert isinstance(outcome, NotComparable)
    assert outcome.cause == SAME_REVISION


def test_two_findings_under_one_key_are_refused() -> None:
    """One would silently win the comparison, and nothing would say which.

    This is the failure mode that keying by position produces on purpose, so it must be impossible to
    reach by accident either.
    """
    duplicated = RevisionSide(
        revision_number=2,
        governing_revisions={"A-101": "B"},
        findings=(
            KeyedFinding(CheckKey("top", "CT-1"), _finding(Outcome.PASS)),
            KeyedFinding(CheckKey("top", "CT-1"), _finding(Outcome.FAIL)),
        ),
    )

    outcome = compare_revisions(_side(1, ("top", Outcome.PASS, "snap-1")), duplicated)

    assert isinstance(outcome, NotComparable)
    assert outcome.cause == DUPLICATE_KEY
    assert "silently win" in outcome.detail


# ---------------------------------------------------------------------------
# Keyed by item and check, never by position
# ---------------------------------------------------------------------------


def test_findings_are_paired_by_key_not_by_list_position() -> None:
    """The first acceptance criterion, tested where position would give the wrong answer.

    The two sides list the same two items in opposite orders. Pairing by position would compare the
    countertop against the cabinet run and report two changes that did not happen.
    """
    before = RevisionSide(
        1,
        {"A-101": "A"},
        (
            KeyedFinding(CheckKey("countertop", "CT-1"), _finding(Outcome.PASS)),
            KeyedFinding(CheckKey("cabinet-run", "CT-1"), _finding(Outcome.FAIL)),
        ),
    )
    after = RevisionSide(
        2,
        {"A-101": "B"},
        (
            KeyedFinding(CheckKey("cabinet-run", "CT-1"), _finding(Outcome.FAIL)),
            KeyedFinding(CheckKey("countertop", "CT-1"), _finding(Outcome.PASS)),
        ),
    )

    delta = compare_revisions(before, after)

    assert isinstance(delta, RevisionDelta)
    assert len(delta.unchanged) == 2, "reordering the list is not a change"
    assert delta.resolved == () and delta.newly_failing == ()


def test_the_variant_is_part_of_the_identity() -> None:
    """The same rule under two variants is two checks.

    `back_left_right` and `island` compare different numbers, so pairing them would report a change
    between two unrelated results.
    """
    before = RevisionSide(
        1,
        {"A-101": "A"},
        (KeyedFinding(CheckKey("top", "CT-1", "back_left_right"), _finding(Outcome.PASS)),),
    )
    after = RevisionSide(
        2,
        {"A-101": "B"},
        (KeyedFinding(CheckKey("top", "CT-1", "island"), _finding(Outcome.FAIL)),),
    )

    delta = compare_revisions(before, after)

    assert isinstance(delta, RevisionDelta)
    assert len(delta.appeared) == 1 and len(delta.disappeared) == 1
    assert delta.newly_failing == (), "two different checks are not one check that changed"


def test_a_blank_key_is_refused() -> None:
    """A blank item pairs everything with everything."""
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="identify something"):
            CheckKey(blank, "CT-1")
        with pytest.raises(ValueError, match="identify something"):
            CheckKey("top", blank)


# ---------------------------------------------------------------------------
# Resolved, newly failing, unchanged
# ---------------------------------------------------------------------------


def test_a_fail_that_stopped_failing_is_resolved() -> None:
    before = _side(1, ("top", Outcome.FAIL, "snap-1"))
    after = _side(2, ("top", Outcome.PASS, "snap-1"))

    delta = compare_revisions(before, after)

    assert isinstance(delta, RevisionDelta)
    assert len(delta.resolved) == 1
    assert delta.resolved[0].before is Outcome.FAIL
    assert delta.resolved[0].after is Outcome.PASS


def test_a_pass_that_started_failing_is_newly_failing() -> None:
    delta = compare_revisions(
        _side(1, ("top", Outcome.PASS, "snap-1")), _side(2, ("top", Outcome.FAIL, "snap-1"))
    )

    assert isinstance(delta, RevisionDelta)
    assert len(delta.newly_failing) == 1


def test_an_abstention_becoming_a_fail_is_newly_failing() -> None:
    """A new problem, whatever it was before. `REVIEW_REQUIRED` → `FAIL` is not "still unresolved"."""
    delta = compare_revisions(
        _side(1, ("top", Outcome.REVIEW_REQUIRED, "snap-1")),
        _side(2, ("top", Outcome.FAIL, "snap-1")),
    )

    assert isinstance(delta, RevisionDelta)
    assert len(delta.newly_failing) == 1


def test_a_fail_becoming_an_abstention_is_not_reported_as_resolved() -> None:
    """**The bucketing decision worth arguing about.**

    A FAIL that became REVIEW_REQUIRED is not a fix — it is the system declining to say. Putting it in
    `resolved` would let a reviewer scanning the fixed list conclude the problem went away, when in fact
    we stopped being able to judge it. It lands in `other_changes`, where it has to be read.
    """
    delta = compare_revisions(
        _side(1, ("top", Outcome.FAIL, "snap-1")),
        _side(2, ("top", Outcome.REVIEW_REQUIRED, "snap-1")),
    )

    assert isinstance(delta, RevisionDelta)
    assert delta.resolved == (), "an abstention is not a fix"
    assert len(delta.other_changes) == 1


def test_the_same_outcome_is_unchanged() -> None:
    delta = compare_revisions(
        _side(1, ("top", Outcome.PASS, "snap-1")), _side(2, ("top", Outcome.PASS, "snap-1"))
    )

    assert isinstance(delta, RevisionDelta)
    assert len(delta.unchanged) == 1
    assert delta.resolved == () and delta.newly_failing == ()


def test_nothing_is_silently_dropped() -> None:
    """Every key on either side lands in exactly one bucket.

    A check that quietly vanished from the report is a finding the reviewer never saw — and "it was in
    neither revision's list" is not something they can discover.
    """
    before = _side(
        1,
        ("a", Outcome.FAIL, "snap-1"),
        ("b", Outcome.PASS, "snap-1"),
        ("gone", Outcome.PASS, "snap-1"),
    )
    after = _side(
        2,
        ("a", Outcome.PASS, "snap-1"),
        ("b", Outcome.PASS, "snap-1"),
        ("new", Outcome.FAIL, "snap-1"),
    )

    delta = compare_revisions(before, after)
    assert isinstance(delta, RevisionDelta)

    accounted = {
        *(c.key.item for c in (*delta.resolved, *delta.newly_failing, *delta.other_changes)),
        *(k.item for k in delta.unchanged),
        *(e.key.item for e in delta.appeared),
        *(e.key.item for e in delta.disappeared),
    }
    assert accounted == {"a", "b", "gone", "new"}


# ---------------------------------------------------------------------------
# Why it changed: the rule, or the drawing
# ---------------------------------------------------------------------------


def test_a_change_with_a_new_rule_snapshot_is_attributed_to_the_rule() -> None:
    """The second acceptance criterion — "why did this pass last month and fail now?"

    Same drawing revision, different snapshot: this is our regression, not the vendor's mistake. Getting
    this backwards sends a vendor a redline for a rule we changed.
    """
    before = _side(1, ("top", Outcome.PASS, "snap-1"), sheets={"A-101": "B"})
    after = _side(2, ("top", Outcome.FAIL, "snap-2"), sheets={"A-101": "B"})

    delta = compare_revisions(before, after)
    assert isinstance(delta, RevisionDelta)

    change = delta.newly_failing[0]
    assert change.cause is ChangeCause.RULE
    assert change.snapshot_changed and not change.revision_changed
    assert change.snapshot_before == "snap-1" and change.snapshot_after == "snap-2"


def test_a_change_with_a_new_governing_revision_is_attributed_to_the_drawing() -> None:
    """Same rule, new sheet revision: a vendor conversation."""
    before = _side(1, ("top", Outcome.PASS, "snap-1"), sheets={"A-101": "A"})
    after = _side(2, ("top", Outcome.FAIL, "snap-1"), sheets={"A-101": "B"})

    delta = compare_revisions(before, after)
    assert isinstance(delta, RevisionDelta)

    change = delta.newly_failing[0]
    assert change.cause is ChangeCause.DRAWING
    assert change.revision_before == "A" and change.revision_after == "B"


def test_both_changing_is_reported_as_both() -> None:
    """Not collapsed to one. A reviewer needs to know the answer is entangled."""
    before = _side(1, ("top", Outcome.PASS, "snap-1"), sheets={"A-101": "A"})
    after = _side(2, ("top", Outcome.FAIL, "snap-2"), sheets={"A-101": "B"})

    delta = compare_revisions(before, after)
    assert isinstance(delta, RevisionDelta)
    assert delta.newly_failing[0].cause is ChangeCause.BOTH


def test_an_outcome_that_moved_with_nothing_else_changing_is_unexplained() -> None:
    """This should be impossible, which is exactly why it is surfaced.

    Same rule snapshot, same governing revision, different outcome: a deterministic engine cannot do
    that. Folding it in with the ordinary regressions would hide the only evidence that determinism had
    broken.
    """
    before = _side(1, ("top", Outcome.PASS, "snap-1"), sheets={"A-101": "B"})
    after = _side(2, ("top", Outcome.FAIL, "snap-1"), sheets={"A-101": "B"})

    delta = compare_revisions(before, after)
    assert isinstance(delta, RevisionDelta)

    assert delta.newly_failing[0].cause is ChangeCause.UNEXPLAINED
    assert len(delta.unexplained) == 1, "it is reachable without scanning every bucket"


def test_unexplained_collects_from_every_bucket() -> None:
    """A resolved finding with no explanation is as alarming as a newly failing one."""
    before = _side(
        1, ("a", Outcome.FAIL, "snap-1"), ("b", Outcome.PASS, "snap-1"), sheets={"A-101": "B"}
    )
    after = _side(
        2, ("a", Outcome.PASS, "snap-1"), ("b", Outcome.FAIL, "snap-1"), sheets={"A-101": "B"}
    )

    delta = compare_revisions(before, after)
    assert isinstance(delta, RevisionDelta)

    assert len(delta.resolved) == 1 and len(delta.newly_failing) == 1
    assert len(delta.unexplained) == 2


def test_a_multi_sheet_package_does_not_claim_the_drawing_changed() -> None:
    """The stated limit of this module, asserted so it cannot drift into a false claim.

    `Finding` does not record which sheet it came from, so on a package governing several sheets this
    cannot attribute a change to the drawing. It answers `RULE` or `UNEXPLAINED` — both of which send a
    reviewer looking — rather than guessing a sheet and saying `DRAWING`.
    """
    before = _side(1, ("top", Outcome.PASS, "snap-1"), sheets={"A-101": "A", "A-201": "A"})
    after = _side(2, ("top", Outcome.FAIL, "snap-1"), sheets={"A-101": "B", "A-201": "A"})

    delta = compare_revisions(before, after)
    assert isinstance(delta, RevisionDelta)

    change = delta.newly_failing[0]
    assert (
        change.cause is not ChangeCause.DRAWING
    ), "with no per-finding sheet attribution this must not claim the drawing changed"
    assert change.revision_before is None and change.revision_after is None


# ---------------------------------------------------------------------------
# Determinism of the report itself
# ---------------------------------------------------------------------------


def test_the_delta_is_ordered_deterministically() -> None:
    """Two runs over the same data must produce the same report, whatever order the input arrived in."""
    entries = [
        ("c", Outcome.FAIL, "snap-1"),
        ("a", Outcome.FAIL, "snap-1"),
        ("b", Outcome.FAIL, "snap-1"),
    ]
    fixed = [
        ("a", Outcome.PASS, "snap-1"),
        ("b", Outcome.PASS, "snap-1"),
        ("c", Outcome.PASS, "snap-1"),
    ]

    first = compare_revisions(_side(1, *entries), _side(2, *fixed))
    second = compare_revisions(_side(1, *reversed(entries)), _side(2, *reversed(fixed)))

    assert isinstance(first, RevisionDelta) and isinstance(second, RevisionDelta)
    assert [c.key.item for c in first.resolved] == ["a", "b", "c"]
    assert [c.key.item for c in first.resolved] == [c.key.item for c in second.resolved]


def test_an_empty_pair_of_revisions_is_a_delta_with_nothing_in_it() -> None:
    """Not a refusal: two revisions that legitimately produced no findings are comparable."""
    delta = compare_revisions(_side(1), _side(2))

    assert isinstance(delta, RevisionDelta)
    assert delta.unchanged == () and delta.resolved == () and delta.newly_failing == ()
    assert delta.is_comparable
