"""Publication is the last gate before a rule can decide anything about a real drawing (#239).

`AGENTS.md` §9 requires *"rule change → human approval + full gold-set regression"*. Neither was
enforced before this. These tests are mostly about the paths that must be **refused**, because a
publication path that works is easy and a publication path that cannot be talked around is the
point.
"""

from __future__ import annotations

import pytest

from rules.governance.proposal import propose
from rules.governance.publish import (
    Approver,
    NotApprovable,
    NotAuthorised,
    PublicationLog,
    RegressionFailed,
    RegressionOutcome,
    Role,
    SelfApproval,
    publish,
)
from rules.schema import CheckType, GlobalApplicability, InputSelector, OperationRef, Rule
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


def _rule(version: str = "1.0.0", rule_id: str = "CT-WIDTH-001") -> Rule:
    return Rule(
        id=rule_id,
        version=version,
        product_type=ProductType.COUNTERTOP,
        check_type=CheckType.INTERNAL,
        severity=Severity.CRITICAL,
        arithmetic_unit=Unit.MM,
        inputs={
            "width": InputSelector(source=OperandSource.SHOP, semantic_type=SemanticType.CT001)
        },
        applicability=GlobalApplicability(scope="global"),
        operation=OperationRef(type="exists", operands={"value": "width"}),
    )


def _passing(_: object) -> RegressionOutcome:
    return RegressionOutcome(passed=True, summary="12 gold cases, no change in outcomes")


def _failing(_: object) -> RegressionOutcome:
    return RegressionOutcome(passed=False, summary="critical false-PASS rose on 2 cases")


ADMIN = Approver(name="anant", role=Role.RULE_ADMIN)
OTHER_ADMIN = Approver(name="someone-else", role=Role.RULE_ADMIN)
REVIEWER = Approver(name="keyur", role=Role.REVIEWER)


def _proposal(author: str = "keyur", **kw: object) -> object:
    return propose(_rule(**kw), author=author, rationale="tighten the width check")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The happy path, briefly
# ---------------------------------------------------------------------------


def test_an_authorised_approver_publishes() -> None:
    store, log = SnapshotStore(), PublicationLog()
    snapshot = publish(_proposal(), approver=ADMIN, store=store, log=log, regression=_passing)  # type: ignore[arg-type]
    assert store.latest("CT-WIDTH-001") is not None
    assert log.for_snapshot(snapshot.snapshot_id) is not None


def test_the_approval_records_who_proposed_and_who_approved() -> None:
    """The question asked later is 'who approved the thing that produced this finding?'."""
    store, log = SnapshotStore(), PublicationLog()
    snapshot = publish(_proposal(author="keyur"), approver=ADMIN, store=store, log=log, regression=_passing)  # type: ignore[arg-type]
    record = log.for_snapshot(snapshot.snapshot_id)
    assert record is not None
    assert record.author == "keyur"
    assert record.approver == "anant"
    assert "gold cases" in record.regression_summary


# ---------------------------------------------------------------------------
# The refusals — the actual point
# ---------------------------------------------------------------------------


def test_a_reviewer_cannot_publish_a_rule() -> None:
    """Publishing changes what the system decides for every future drawing. Reviewing a package
    does not, and the roles are not interchangeable."""
    store, log = SnapshotStore(), PublicationLog()
    with pytest.raises(NotAuthorised, match="cannot publish a rule"):
        publish(_proposal(), approver=REVIEWER, store=store, log=log, regression=_passing)  # type: ignore[arg-type]
    assert len(store) == 0 and len(log) == 0


def test_the_author_cannot_approve_their_own_proposal() -> None:
    """The value of an approval is that a second person looked."""
    store, log = SnapshotStore(), PublicationLog()
    author_as_admin = Approver(name="keyur", role=Role.RULE_ADMIN)
    with pytest.raises(SelfApproval, match="cannot also approve"):
        publish(_proposal(author="keyur"), approver=author_as_admin, store=store, log=log, regression=_passing)  # type: ignore[arg-type]
    assert len(store) == 0 and len(log) == 0


def test_self_approval_is_not_defeated_by_capitalisation() -> None:
    """The bypass anyone would find first."""
    store, log = SnapshotStore(), PublicationLog()
    with pytest.raises(SelfApproval):
        publish(
            _proposal(author="Keyur"),  # type: ignore[arg-type]
            approver=Approver(name="  keyur ", role=Role.RULE_ADMIN),
            store=store,
            log=log,
            regression=_passing,
        )


def test_an_invalid_proposal_cannot_be_published_by_anyone() -> None:
    """Authority decides whether a coherent change ships, not whether an incoherent one is."""
    store, log = SnapshotStore(), PublicationLog()
    bad = propose(
        Rule(
            id="CT-X",
            version="1.0.0",
            product_type=ProductType.COUNTERTOP,
            check_type=CheckType.INTERNAL,
            severity=Severity.CRITICAL,
            arithmetic_unit=Unit.MM,
            inputs={
                "w": InputSelector(source=OperandSource.SHOP, semantic_type=SemanticType.CT001)
            },
            applicability=GlobalApplicability(scope="global"),
            operation=OperationRef(type="not_registered", operands={"value": "w"}),
        ),
        author="keyur",
        rationale="test",
    )
    with pytest.raises(NotApprovable, match="did not validate"):
        publish(bad, approver=ADMIN, store=store, log=log, regression=_passing)
    assert len(store) == 0


def test_a_failed_regression_blocks_publication_with_no_override() -> None:
    """`AGENTS.md` §9. There is deliberately no parameter that lets this through."""
    store, log = SnapshotStore(), PublicationLog()
    with pytest.raises(RegressionFailed, match="no override"):
        publish(_proposal(), approver=ADMIN, store=store, log=log, regression=_failing)  # type: ignore[arg-type]
    assert len(store) == 0 and len(log) == 0


def test_publish_cannot_be_called_without_deciding_about_regression() -> None:
    """`regression` has no default. A default would mean publication silently works without a
    regression run the moment someone forgets to pass one — the exact failure §9 names."""
    store, log = SnapshotStore(), PublicationLog()
    with pytest.raises(TypeError):
        publish(_proposal(), approver=ADMIN, store=store, log=log)  # type: ignore[call-arg]


def test_a_regression_outcome_must_say_what_it_was_based_on() -> None:
    """A bare pass with no basis is indistinguishable from no regression run at all."""
    with pytest.raises(ValueError, match="must say what it was based on"):
        RegressionOutcome(passed=True, summary="  ")


# ---------------------------------------------------------------------------
# Ordering of refusals
# ---------------------------------------------------------------------------


def test_an_unauthorised_approver_is_told_that_rather_than_a_regression_failure() -> None:
    """Cheap refusals first: being told "regression failed" when the real problem is that you may
    not publish sends someone to fix the wrong thing."""
    store, log = SnapshotStore(), PublicationLog()
    with pytest.raises(NotAuthorised):
        publish(_proposal(), approver=REVIEWER, store=store, log=log, regression=_failing)  # type: ignore[arg-type]


def test_the_regression_check_does_not_run_when_an_earlier_gate_refuses() -> None:
    """Not merely cosmetic — a real gold-set run is expensive."""
    calls: list[object] = []

    def counting(proposal: object) -> RegressionOutcome:
        calls.append(proposal)
        return RegressionOutcome(passed=True, summary="ran")

    store, log = SnapshotStore(), PublicationLog()
    with pytest.raises(NotAuthorised):
        publish(_proposal(), approver=REVIEWER, store=store, log=log, regression=counting)  # type: ignore[arg-type]
    assert calls == []


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_an_approval_is_never_overwritten() -> None:
    """Append-only, for the same reason the correction ledger is: the record of who approved what
    is exactly what someone would tidy after a rule turns out to have been wrong."""
    store, log = SnapshotStore(), PublicationLog()
    publish(_proposal(), approver=ADMIN, store=store, log=log, regression=_passing)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="already recorded as approved"):
        publish(_proposal(), approver=OTHER_ADMIN, store=store, log=log, regression=_passing)  # type: ignore[arg-type]


def test_an_approver_must_be_a_named_person() -> None:
    """'Approved by rule_admin' answers a different question from 'approved by whom'."""
    with pytest.raises(ValueError, match="named person"):
        Approver(name="   ", role=Role.RULE_ADMIN)


# ---------------------------------------------------------------------------
# ADR-0005 — publishing does not disturb an in-flight review
# ---------------------------------------------------------------------------


def test_publishing_a_new_version_leaves_the_old_snapshot_retrievable() -> None:
    """ADR-0005: an old review is reproduced by replaying its recorded snapshots, never by
    re-resolving. Publishing must therefore never make a previously recorded snapshot unavailable —
    that would turn a reproducible finding into an unreproducible one.
    """
    store, log = SnapshotStore(), PublicationLog()
    first = publish(_proposal(), approver=ADMIN, store=store, log=log, regression=_passing)  # type: ignore[arg-type]

    second = publish(
        propose(_rule(version="1.1.0"), author="keyur", rationale="tighter"),
        approver=ADMIN,
        store=store,
        log=log,
        regression=_passing,
    )

    assert store.get(first.snapshot_id).snapshot_id == first.snapshot_id
    assert store.latest("CT-WIDTH-001").snapshot_id == second.snapshot_id  # type: ignore[union-attr]
    assert log.for_snapshot(first.snapshot_id) is not None


def test_the_publication_log_keeps_every_approval_not_only_the_latest() -> None:
    store, log = SnapshotStore(), PublicationLog()
    publish(_proposal(), approver=ADMIN, store=store, log=log, regression=_passing)  # type: ignore[arg-type]
    publish(
        propose(_rule(version="1.1.0"), author="keyur", rationale="tighter"),
        approver=ADMIN,
        store=store,
        log=log,
        regression=_passing,
    )
    assert len(log) == 2
    assert len(log.all()) == 2
