"""What an exception may be, and when it stops counting (#234, D5.2).

An exception is the one deliberate way a failed check gets accepted, so the tests that matter here
are the refusals. Four of them, one per acceptance criterion:

* an exception with no expiry cannot be built at all;
* a scope of "this rule, everywhere" is not a value;
* an expired exception stops applying by itself, and the report says so;
* an excepted finding is still in the report, never removed from it.

Several negative tests assert *which* error was raised and what it said, because the near misses in
this module all fail in ways that look alike: an out-of-scope exception and an expired one both come
back "not excepted", and a test that cannot tell them apart reads as coverage without being any.

Source: backend proposal §10.1; `AGENTS.md` §2.6 · Design: `docs/DESIGN_PRODUCT.md` §4.1
"""

from __future__ import annotations

import dataclasses
import inspect
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.models.review import ExceptionScope, ReviewException
from app.review.exceptions import (
    ExceptionGrant,
    FindingRef,
    apply_exceptions,
    decide,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
NEXT_MONTH = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
LAST_MONTH = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def _grant(
    scope: ExceptionScope = ExceptionScope.FINDING,
    scope_id: UUID | None = None,
    *,
    expires_at: datetime = NEXT_MONTH,
    approved_by: str = "anant",
    reason: str = "client accepted the 3mm overhang on this run",
) -> ExceptionGrant:
    return ExceptionGrant(
        scope=scope,
        scope_id=scope_id if scope_id is not None else uuid4(),
        reason=reason,
        approved_by=approved_by,
        expires_at=expires_at,
    )


def _finding(*, item: UUID | None = None) -> FindingRef:
    return FindingRef(finding_id=uuid4(), package_revision_id=uuid4(), item_id=item)


def _naive(moment: datetime) -> datetime:
    """The same instant with its timezone taken off — the input several tests are about.

    Built by stripping the zone rather than by writing a bare `datetime(...)`, so the lint rule that
    stops naive datetimes appearing by accident anywhere else stays switched on rather than being
    silenced here.
    """
    return moment.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# An exception without an expiry is not representable
# ---------------------------------------------------------------------------


def test_an_exception_cannot_be_built_without_an_expiry() -> None:
    """The control this story exists for. Not a validation message afterwards — the constructor
    cannot be called without the date, so an unbounded exception has no way into the system."""
    with pytest.raises(TypeError, match="expires_at"):
        ExceptionGrant(  # type: ignore[call-arg]
            scope=ExceptionScope.FINDING,
            scope_id=uuid4(),
            reason="looks fine",
            approved_by="anant",
        )


def test_the_expiry_has_no_default_to_fall_back_on() -> None:
    """A default would put the required argument back: `ExceptionGrant(...)` would succeed and the
    system would have an exception nobody chose an end date for."""
    field = {f.name: f for f in dataclasses.fields(ExceptionGrant)}["expires_at"]
    assert field.default is dataclasses.MISSING
    assert field.default_factory is dataclasses.MISSING
    assert (
        inspect.signature(ExceptionGrant).parameters["expires_at"].default
        is inspect.Parameter.empty
    )


def test_an_expiry_of_none_is_refused() -> None:
    """The other way somebody reaches for "no expiry" — passing it explicitly."""
    with pytest.raises(TypeError, match="expires_at must be a datetime"):
        _grant(expires_at=None)  # type: ignore[arg-type]


def test_a_naive_expiry_is_refused() -> None:
    """A naive datetime is a time plus a missing assumption. Assuming UTC would move the expiry by
    the reader's offset, which surfaces as an exception outliving its own end date."""
    with pytest.raises(ValueError, match="expires_at must be timezone-aware"):
        _grant(expires_at=_naive(NEXT_MONTH))


# ---------------------------------------------------------------------------
# Scope is explicit, and never "this rule, everywhere"
# ---------------------------------------------------------------------------


def test_scope_is_one_of_exactly_three_things() -> None:
    """One finding, one item, one package revision. The same three the database CHECK allows."""
    assert {member.value for member in ExceptionScope} == {"finding", "item", "package"}


@pytest.mark.parametrize("rule_wide", ["rule", "all", "everywhere", "RULE", "*"])
def test_a_rule_wide_scope_is_not_a_value(rule_wide: str) -> None:
    """A rule that should stop firing everywhere is a rule change and goes through the rulebook.
    The error has to say so, not merely say "invalid", or somebody will look for the right spelling.
    """
    with pytest.raises(ValueError, match="is not an exception scope"):
        _grant(scope=rule_wide)  # type: ignore[arg-type]


def test_a_scope_given_as_its_stored_string_is_accepted_and_typed() -> None:
    """Rows come back from the database as strings; they must land as the enum, not stay loose."""
    grant = _grant(scope="package")  # type: ignore[arg-type]
    assert grant.scope is ExceptionScope.PACKAGE


def test_a_scope_id_must_be_a_uuid() -> None:
    """Matching is exact, so a string id would silently match nothing at all — an exception that
    covers nothing looks identical to one that was never granted."""
    with pytest.raises(TypeError, match="scope_id must be a UUID"):
        ExceptionGrant(
            scope=ExceptionScope.FINDING,
            scope_id=str(uuid4()),  # type: ignore[arg-type]
            reason="looks fine",
            approved_by="anant",
            expires_at=NEXT_MONTH,
        )


def test_an_exception_must_say_why() -> None:
    with pytest.raises(ValueError, match="stated reason"):
        _grant(reason="   ")


def test_an_exception_must_name_its_approver() -> None:
    """`AGENTS.md` §2.6 — accepting a failed check is a person's decision, not the system's."""
    with pytest.raises(ValueError, match="named approver"):
        _grant(approved_by="")


# ---------------------------------------------------------------------------
# Scope matching is exact, and the near misses
# ---------------------------------------------------------------------------


def test_an_exception_covers_the_thing_it_names() -> None:
    finding = _finding()
    grant = _grant(ExceptionScope.FINDING, finding.finding_id)
    assert decide(finding, [grant], when=NOW).is_excepted


def test_an_exception_for_another_finding_does_not_cover_this_one() -> None:
    decision = decide(_finding(), [_grant(ExceptionScope.FINDING, uuid4())], when=NOW)
    assert not decision.is_excepted
    # Not merely "not excepted": it must not be reported as a lapsed one either, because that
    # sentence tells a reviewer somebody once accepted this finding, and nobody did.
    assert decision.expired == ()
    assert "no exception names this finding" in decision.explain()


def test_an_id_one_digit_out_does_not_cover() -> None:
    """The near miss that a prefix or substring match would wave through."""
    finding = _finding()
    digits = finding.finding_id.hex
    neighbour = UUID(hex=digits[:-1] + ("0" if digits[-1] != "0" else "1"))
    assert not decide(finding, [_grant(ExceptionScope.FINDING, neighbour)], when=NOW).is_excepted


def test_the_right_id_at_the_wrong_scope_does_not_cover() -> None:
    """The nastiest near miss: an item-scoped exception carrying this finding's id. Ids are opaque
    and could collide across tables, so the scope kind has to be compared as well as the id."""
    finding = _finding(item=uuid4())
    assert not decide(
        finding, [_grant(ExceptionScope.ITEM, finding.finding_id)], when=NOW
    ).is_excepted
    assert not decide(
        finding, [_grant(ExceptionScope.FINDING, finding.item_id)], when=NOW
    ).is_excepted


def test_matching_compares_the_scope_kind_as_well_as_the_id() -> None:
    """The same check at the level below, so it is tested rather than merely reached. One id, three
    scopes, and only the scope it was granted at matches."""
    shared_id = uuid4()
    grant = _grant(ExceptionScope.ITEM, shared_id)
    assert grant.covers(ExceptionScope.ITEM, shared_id)
    assert not grant.covers(ExceptionScope.FINDING, shared_id)
    assert not grant.covers(ExceptionScope.PACKAGE, shared_id)


def test_an_item_exception_does_not_cover_a_finding_that_has_no_item() -> None:
    """No falling back to something broader when the narrow scope has nothing to match against."""
    finding = _finding(item=None)
    assert not decide(finding, [_grant(ExceptionScope.ITEM, uuid4())], when=NOW).is_excepted


def test_a_package_exception_covers_a_finding_in_that_package() -> None:
    """The widest scope the design allows, and it still names one package revision by id."""
    finding = _finding()
    grant = _grant(ExceptionScope.PACKAGE, finding.package_revision_id)
    assert decide(finding, [grant], when=NOW).is_excepted


def test_a_package_exception_does_not_reach_into_another_package() -> None:
    assert not decide(_finding(), [_grant(ExceptionScope.PACKAGE, uuid4())], when=NOW).is_excepted


# ---------------------------------------------------------------------------
# Expiry, enforced where the exception is read
# ---------------------------------------------------------------------------


def test_an_exception_applies_up_to_the_instant_before_it_expires() -> None:
    grant = _grant(expires_at=NEXT_MONTH)
    assert grant.applies_at(NEXT_MONTH - timedelta(microseconds=1))


def test_an_exception_has_expired_at_the_instant_of_its_expiry() -> None:
    """Exclusive at the boundary, and deliberately the strict reading: where the two readings differ
    the safe one turns the check back on. A wrong FAIL costs review time; a wrong PASS gets built.
    """
    grant = _grant(expires_at=NEXT_MONTH)
    assert not grant.applies_at(NEXT_MONTH)
    assert not grant.applies_at(NEXT_MONTH + timedelta(microseconds=1))


def test_expiry_is_compared_across_timezones_exactly() -> None:
    """The same instant written two ways must give the same answer. A comparison that ignored the
    offset would extend or cut short every exception granted outside UTC."""
    grant = _grant(expires_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC))
    just_before = datetime(2026, 8, 31, 18, 59, 59, tzinfo=timezone(timedelta(hours=-5)))
    exactly = datetime(2026, 8, 31, 19, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert grant.applies_at(just_before)
    assert not grant.applies_at(exactly)


def test_asking_with_a_naive_now_is_refused_rather_than_answered() -> None:
    """It must raise, not guess. Guessing here is how an exception ends up living for ever, and
    `applies_at` returning a plausible boolean is exactly how that goes unnoticed."""
    with pytest.raises(ValueError, match="when must be timezone-aware"):
        _grant().applies_at(_naive(NOW))
    with pytest.raises(ValueError, match="when must be timezone-aware"):
        decide(_finding(), [], when=_naive(NOW))


def test_an_expired_exception_stops_applying_and_says_so() -> None:
    """ "Says so" is half the requirement. A finding that quietly stopped being excepted, with no
    record that it once was, gives a reviewer nothing to act on."""
    finding = _finding()
    grant = _grant(ExceptionScope.FINDING, finding.finding_id, expires_at=LAST_MONTH)
    decision = decide(finding, [grant], when=NOW)

    assert not decision.is_excepted
    assert decision.applied is None
    assert decision.expired == (grant,)
    explanation = decision.explain()
    assert "expired" in explanation
    assert LAST_MONTH.isoformat() in explanation
    assert grant.approved_by in explanation


def test_the_same_exception_applied_before_its_expiry_and_not_after() -> None:
    """One grant, two moments — the clock is the only thing that changed, and no writer was
    involved. This is what "enforced where it is read" means."""
    finding = _finding()
    grant = _grant(ExceptionScope.FINDING, finding.finding_id, expires_at=NEXT_MONTH)
    assert decide(finding, [grant], when=NOW).is_excepted
    assert not decide(finding, [grant], when=NEXT_MONTH + timedelta(days=1)).is_excepted


def test_status_reads_as_plain_english_on_both_sides() -> None:
    live = _grant(expires_at=NEXT_MONTH)
    lapsed = _grant(expires_at=LAST_MONTH)
    assert live.status_at(NOW).startswith("in force until")
    assert lapsed.status_at(NOW).startswith("expired on")


# ---------------------------------------------------------------------------
# Reading a stored row
# ---------------------------------------------------------------------------


def test_a_stored_row_is_read_by_its_real_column_names() -> None:
    """Built from the real model, not a stand-in, so a column rename breaks this test rather than
    quietly producing a grant with a missing field."""
    finding_id = uuid4()
    row = ReviewException(
        review_action_id=uuid4(),
        action="except",
        scope=ExceptionScope.FINDING.value,
        scope_id=finding_id,
        reason="client accepted the 3mm overhang on this run",
        approved_by="anant",
        expires_at=NEXT_MONTH,
    )
    grant = ExceptionGrant.from_stored(row)
    assert grant.scope is ExceptionScope.FINDING
    assert grant.scope_id == finding_id
    assert grant.approved_by == "anant"
    assert grant.expires_at == NEXT_MONTH


def test_an_exception_that_expired_after_it_was_stored_can_still_be_read() -> None:
    """Loading it has to succeed so the report can say it expired. Refusing to build it would make
    the lapsed exception vanish, which is the silent suppression this module exists to prevent —
    and it is the whole reason expiry is enforced at read time and not at write time."""
    finding = _finding()
    row = ReviewException(
        review_action_id=uuid4(),
        action="except",
        scope=ExceptionScope.FINDING.value,
        scope_id=finding.finding_id,
        reason="site condition accepted for one production run",
        approved_by="anant",
        expires_at=LAST_MONTH,
    )
    grant = ExceptionGrant.from_stored(row)
    decision = decide(finding, [grant], when=NOW)
    assert not decision.is_excepted
    assert decision.expired == (grant,)


# ---------------------------------------------------------------------------
# A suppressed finding is never invisible
# ---------------------------------------------------------------------------


def test_every_finding_comes_back_including_the_excepted_one() -> None:
    """The acceptance criterion, stated as an assertion: same count, same order, same ids. Nothing
    is filtered. A finding that disappeared because somebody excepted it would be indistinguishable
    from a check that never ran."""
    findings = [_finding(), _finding(), _finding()]
    grant = _grant(ExceptionScope.FINDING, findings[1].finding_id)

    decisions = apply_exceptions(findings, [grant], when=NOW)

    assert [d.finding.finding_id for d in decisions] == [f.finding_id for f in findings]
    assert [d.is_excepted for d in decisions] == [False, True, False]


def test_an_applied_exception_is_reported_with_who_why_and_until_when() -> None:
    """Appearing in the report is not enough on its own — the reviewer reading it has to be able to
    challenge the decision, which means seeing the name, the reason and the end date."""
    finding = _finding()
    grant = _grant(
        ExceptionScope.FINDING,
        finding.finding_id,
        approved_by="raj",
        reason="client accepted the 3mm overhang on this run",
    )
    explanation = decide(finding, [grant], when=NOW).explain()

    assert "raj" in explanation
    assert "3mm overhang" in explanation
    assert NEXT_MONTH.isoformat() in explanation
    assert "stands and is reported unchanged" in explanation


def test_the_narrowest_exception_leads_and_the_others_are_still_listed() -> None:
    """A package-wide exception must not be the sentence the report shows when somebody wrote one
    about this exact finding — and it must not be dropped either.

    The broader exceptions deliberately run *longer* here. Give all three the same expiry and the
    ordering passes whichever way round its key is written, which is how a report that leads with
    "excepted for the whole package" would slip through.
    """
    finding = _finding(item=uuid4())
    on_finding = _grant(ExceptionScope.FINDING, finding.finding_id, expires_at=NEXT_MONTH)
    on_item = _grant(
        ExceptionScope.ITEM, finding.item_id, expires_at=NEXT_MONTH + timedelta(days=30)
    )
    on_package = _grant(
        ExceptionScope.PACKAGE,
        finding.package_revision_id,
        expires_at=NEXT_MONTH + timedelta(days=60),
    )

    decision = decide(finding, [on_package, on_item, on_finding], when=NOW)

    assert decision.applied is on_finding
    assert set(decision.in_force) == {on_finding, on_item, on_package}
    assert "2 further exception(s)" in decision.explain()


def test_the_reported_exception_does_not_depend_on_the_order_they_arrived_in() -> None:
    """Two reviewers' exceptions arriving in a different order must not change what the report says
    happened. An unstable answer here is one nobody can reproduce from the stored records."""
    finding = _finding()
    early = _grant(ExceptionScope.FINDING, finding.finding_id, expires_at=NEXT_MONTH)
    late = _grant(
        ExceptionScope.FINDING, finding.finding_id, expires_at=NEXT_MONTH + timedelta(days=30)
    )

    forwards = decide(finding, [early, late], when=NOW)
    backwards = decide(finding, [late, early], when=NOW)

    assert forwards.in_force == backwards.in_force
    # Ties at the same scope break towards the later expiry, because that is when cover at this
    # scope actually ends — reporting the earlier one would understate how long this is suppressed.
    assert forwards.applied is late


def test_a_live_and_a_lapsed_exception_are_both_reported() -> None:
    """The expired one is not noise: it is the record that this finding was accepted once already."""
    finding = _finding()
    live = _grant(ExceptionScope.FINDING, finding.finding_id, expires_at=NEXT_MONTH)
    lapsed = _grant(ExceptionScope.PACKAGE, finding.package_revision_id, expires_at=LAST_MONTH)

    decision = decide(finding, [live, lapsed], when=NOW)

    assert decision.in_force == (live,)
    assert decision.expired == (lapsed,)


def test_an_exception_cannot_be_widened_after_the_fact() -> None:
    """Frozen. Editing the scope or the expiry in place would leave the original approver's name on
    a decision they did not make."""
    grant = _grant()
    with pytest.raises(dataclasses.FrozenInstanceError):
        grant.scope_id = uuid4()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        grant.expires_at = NEXT_MONTH + timedelta(days=3650)  # type: ignore[misc]
