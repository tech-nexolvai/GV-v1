"""A rule that cannot decide anything must not reach production (#240, ADR-0011).

The failure is specific and has already happened once outside the code. A tolerance of `±1/8″`
circulated for weeks as though the client had supplied it. It was our own placeholder from
`Countertop_Checks_SAMPLE_Nexolv.xlsx`, labelled *"PLACEHOLDER — please confirm your acceptable
deviation"*, and it reached `docs/RULE_ENGINE_SPEC.md` §4 and started being quoted as fact.

What made that possible was not the placeholder. It was that nothing counted placeholders, so
nothing contradicted the impression that the rulebook was finished.

Two things are therefore under test: that an unconfirmed rule cannot be *released*, and that it can
still be *authored* — because a gate at the authoring boundary would make the sentinel useless and
push people to invent a number instead.
"""

from __future__ import annotations

import pytest

from rules.governance import readiness
from rules.governance.proposal import propose
from rules.governance.publish import (
    Approver,
    PublicationLog,
    PublicationTarget,
    RegressionOutcome,
    Role,
    publish,
)
from rules.publication import NotProductionReadyError
from rules.schema import (
    TOLERANCE_UNCONFIRMED,
    CheckType,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Rule,
    Tolerance,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from rules.snapshot import SnapshotStore
from units.measurement import Unit
from verdict.operations.aggregate import AGGREGATE_SPECS
from verdict.operations.alignment import ALIGNMENT_SPECS
from verdict.operations.pairwise import PAIRWISE_SPECS
from verdict.operations.scalar import SCALAR_SPECS
from verdict.outcomes import Severity
from verdict.registry import REGISTRY, register


@pytest.fixture(autouse=True)
def _registry() -> None:
    for spec in (*SCALAR_SPECS, *AGGREGATE_SPECS, *PAIRWISE_SPECS, *ALIGNMENT_SPECS):
        if spec.name not in REGISTRY:
            register(spec)


ADMIN = Approver(name="anant", role=Role.RULE_ADMIN)


def _passing(_: object) -> RegressionOutcome:
    return RegressionOutcome(passed=True, summary="12 gold cases, no change")


def _rule(*, confirmed: bool, rule_id: str = "CT-WIDTH-001", version: str = "1.0.0") -> Rule:
    tolerance = (
        Tolerance(value="1/8", unit=Unit.INCH)
        if confirmed
        else Tolerance(value=TOLERANCE_UNCONFIRMED)
    )
    return Rule(
        id=rule_id,
        version=version,
        product_type=ProductType.COUNTERTOP,
        check_type=CheckType.INTERNAL,
        severity=Severity.CRITICAL,
        arithmetic_unit=Unit.MM,
        inputs={
            "width": InputSelector(source=OperandSource.SHOP, semantic_type=SemanticType.CT001),
            "expected": InputSelector(source=OperandSource.ARCH, semantic_type=SemanticType.CT001),
            "tol": InputSelector(source=OperandSource.LITERAL, semantic_type=SemanticType.CT001),
        },
        applicability=GlobalApplicability(scope="global"),
        operation=OperationRef(
            type="within_tolerance",
            operands={"actual": "width", "expected": "expected", "tolerance": "tol"},
            tolerance=tolerance,
        ),
    )


def _publish(rule: Rule, target: PublicationTarget, store: SnapshotStore, log: PublicationLog):
    return publish(
        propose(rule, author="keyur", rationale="test"),
        approver=ADMIN,
        store=store,
        log=log,
        regression=_passing,
        target=target,
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_an_unconfirmed_tolerance_cannot_reach_production() -> None:
    store, log = SnapshotStore(), PublicationLog()
    with pytest.raises(NotProductionReadyError):
        _publish(_rule(confirmed=False), PublicationTarget.PRODUCTION, store, log)
    assert len(store) == 0 and len(log) == 0


def test_the_refusal_distinguishes_the_two_messages_a_reviewer_would_confuse() -> None:
    """'A reviewer should look at this' and 'nobody has told us the limit' read identically in a
    report, and only one of them gets acted on."""
    store, log = SnapshotStore(), PublicationLog()
    with pytest.raises(NotProductionReadyError) as err:
        _publish(_rule(confirmed=False), PublicationTarget.PRODUCTION, store, log)
    assert "nobody has told us the limit" in str(err.value)


def test_a_confirmed_rule_reaches_production() -> None:
    store, log = SnapshotStore(), PublicationLog()
    snapshot = _publish(_rule(confirmed=True), PublicationTarget.PRODUCTION, store, log)
    assert store.get(snapshot.snapshot_id) is not None


# ---------------------------------------------------------------------------
# Authoring must still work
# ---------------------------------------------------------------------------


def test_an_unconfirmed_rule_can_still_be_published_to_development() -> None:
    """The gate is at release, not at authoring. Blocking it here would make the UNCONFIRMED
    sentinel useless and push an author to invent a number instead — which is precisely how the
    ±1/8" placeholder acquired the appearance of authority."""
    store, log = SnapshotStore(), PublicationLog()
    snapshot = _publish(_rule(confirmed=False), PublicationTarget.DEVELOPMENT, store, log)
    assert store.get(snapshot.snapshot_id) is not None


def test_the_approval_records_which_boundary_it_was_for() -> None:
    """'Was this approved for production?' is a different question from 'was this approved?', and
    only the first matters when a finding is disputed."""
    store, log = SnapshotStore(), PublicationLog()
    snapshot = _publish(_rule(confirmed=False), PublicationTarget.DEVELOPMENT, store, log)
    record = log.for_snapshot(snapshot.snapshot_id)
    assert record is not None
    assert record.target is PublicationTarget.DEVELOPMENT


def test_publish_cannot_be_called_without_stating_the_target() -> None:
    """No default. A default of production would block authoring; a default of development would
    let a production release skip the readiness gate by omission."""
    store, log = SnapshotStore(), PublicationLog()
    with pytest.raises(TypeError):
        publish(
            propose(_rule(confirmed=True), author="keyur", rationale="t"),
            approver=ADMIN,
            store=store,
            log=log,
            regression=_passing,
        )  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# The readiness report
# ---------------------------------------------------------------------------


def test_readiness_counts_what_is_still_waiting_on_the_client() -> None:
    store, log = SnapshotStore(), PublicationLog()
    _publish(_rule(confirmed=True, rule_id="CT-A"), PublicationTarget.PRODUCTION, store, log)
    _publish(_rule(confirmed=False, rule_id="CT-B"), PublicationTarget.DEVELOPMENT, store, log)

    state = readiness.assess(store)
    assert state.total_rules == 2
    assert state.releasable == 1
    assert state.awaiting_tolerance == ("CT-B",)


def test_a_rulebook_where_nothing_can_decide_says_so_plainly() -> None:
    """The state the project is actually in today. It must not read as 'some rules are pending'."""
    store, log = SnapshotStore(), PublicationLog()
    _publish(_rule(confirmed=False, rule_id="CT-A"), PublicationTarget.DEVELOPMENT, store, log)

    state = readiness.assess(store)
    assert not state.can_release_anything
    assert "No rule in the rulebook can currently produce a PASS or a FAIL" in readiness.report(
        store
    )


def test_an_empty_rulebook_does_not_claim_a_problem() -> None:
    assert "empty" in str(readiness.assess(SnapshotStore()))


def test_the_report_is_readable_by_someone_outside_the_codebase() -> None:
    """Written to be pasted into a client email unedited."""
    store, log = SnapshotStore(), PublicationLog()
    _publish(_rule(confirmed=False, rule_id="CT-A"), PublicationTarget.DEVELOPMENT, store, log)
    text = readiness.report(store)
    assert "await" in text.lower()
    assert "CT-A" in text


def test_the_report_does_not_re_render_what_publication_already_says() -> None:
    """Delegates per-rule detail to `rules/publication.py`, so the two cannot drift into saying
    different things about the same rulebook."""
    store, log = SnapshotStore(), PublicationLog()
    _publish(_rule(confirmed=False, rule_id="CT-A"), PublicationTarget.DEVELOPMENT, store, log)
    from rules.publication import tolerance_report

    assert tolerance_report(store) in readiness.report(store)
