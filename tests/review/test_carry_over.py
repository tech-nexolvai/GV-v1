"""A reviewer's decision carries over a check re-run only when the result is unchanged (#1073).

Anant decided option (b) on 2026-10-09: when "Run checks" replaces a result with one that is exactly
the same (same revision, rule and snapshot, scope, variant, outcome, numbers, evidence, settings and
reviewer inputs), the new result inherits the reviewer's latest valid decision on the one it
replaces. Anything different, and the reviewer decides again. The decision stays the reviewer's own:
no review action is written under anybody's name, and an append-only link records the carry.

Synthetic values only.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.api.slot_rows import SlotRowReviewIn, review_slot_row
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import (
    ApprovalExportSnapshot,
    ApprovedFinding,
    CanonicalObservation,
    ObservationCandidate,
    Package,
    PackageRevision,
    ReviewAction,
)
from app.models.evidence import (
    ArchitectPairingRecord,
    ArchitectViewMatchRecord,
    MeasurementProposal,
    SlotRowReviewDecision,
)
from app.models.package import PackageState
from app.models.parameters import load_parameter_sets, to_rows
from app.models.review import ExceptionScope, FindingDecisionCarryover, ReviewActionKind
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.runs import ExtractionRun, TaskRun
from app.models.verdicts import CheckRun, Finding, VerdictInput
from app.review import approval as approval_module
from app.review.approval import approval_readiness, approval_readiness_many, approve_package
from app.review.session import grant_exception, open_session, record_action
from app.review.signed_exports import load_snapshot
from app.verdicts.record import supersede_runs
from evidence.canonical import Authority, EvidenceStatus
from rules.parameters import ParameterLayer, Provenance
from rules.parameters import ParameterSet as ParameterLayerSet
from rules.parameters import ParameterValue as ParameterLayerValue
from rules.schema import Quantity
from rules.semantic_types import DocumentRole, SemanticType
from storage.local import LocalStore
from tests.api.test_slot_rows import _package_rows, _run_current_checks, _save_all_row_widths
from tests.app.postgres_fixture import alembic_config
from tests.review.test_session import _revision
from tests.workflow.test_part_operands import Assembly
from units.measurement import Unit
from workflow.measurements import run_parameters_for
from workflow.slot_row_scope import latest_slot_reader_run
from workflow.stages import _cited_sets

pytest_plugins = ("tests.app.postgres_fixture",)

NOTE = "TEST ONLY synthetic reviewer note"


def _links() -> type:
    """The carry-over link table, imported late so each test fails on its own assertion on main."""
    from app.models.review import FindingDecisionCarryover

    return FindingDecisionCarryover


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _client(session: Session, project_id: UUID) -> TestClient:
    app = create_app(Settings(database_url="postgresql+psycopg://gv:gv@localhost:5433/gv"))
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({project_id}),
    )
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _principal(project_id: UUID) -> Principal:
    return Principal(
        id="anant (synthetic test)",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({project_id}),
    )


def _revision_of(session: Session, package_id: UUID) -> PackageRevision:
    return session.query(PackageRevision).filter_by(package_id=package_id).one()


def _live(session: Session, revision_id: UUID) -> list[Finding]:
    return list(
        session.scalars(
            select(Finding)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .where(Finding.package_revision_id == revision_id, CheckRun.superseded_at.is_(None))
            .order_by(Finding.created_at, Finding.id)
        )
    )


def _decide_everything(session: Session, revision_id: UUID, reviewer: str = "anant") -> UUID:
    """Confirm every FAIL and dismiss every abstention, each with a TEST ONLY note."""
    review = open_session(session, package_revision_id=revision_id, reviewer=reviewer)
    for finding in _live(session, revision_id):
        if finding.outcome == "FAIL":
            action = ReviewActionKind.CONFIRM
        elif finding.outcome in {"REVIEW_REQUIRED", "NOT_FOUND"}:
            action = ReviewActionKind.DISMISS
        else:
            continue
        record_action(
            session,
            review_session_id=review.id,
            finding_id=finding.id,
            action=action,
            actor=reviewer,
            note=f"{NOTE} for {finding.outcome}",
        )
    session.flush()
    return review.id


def _two_decided_rows(session: Session, tmp_path: Path) -> tuple[UUID, UUID, dict[int, UUID]]:
    project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="code-ink-and-both-ais"
    )
    for anchor in anchors.values():
        _save_all_row_widths(session, project_id, package_id, anchor)
    _run_current_checks(session, package_id, tmp_path)
    revision = _revision_of(session, package_id)
    _decide_everything(session, revision.id)
    assert approval_readiness(session, revision.id).blocking_findings == 0
    return project_id, package_id, anchors


# ---------------------------------------------------------------------------
# End to end through the real check run
# ---------------------------------------------------------------------------


def test_unchanged_rerun_keeps_every_decision_and_sign_off_is_allowed(
    session: Session, tmp_path: Path
) -> None:
    _project_id, package_id, _anchors = _two_decided_rows(session, tmp_path)
    revision = _revision_of(session, package_id)
    before = {finding.id for finding in _live(session, revision.id)}
    actions_before = session.query(ReviewAction).count()

    _run_current_checks(session, package_id, tmp_path)

    after = _live(session, revision.id)
    assert before.isdisjoint({finding.id for finding in after}), "a re-run writes new results"
    readiness = approval_readiness(session, revision.id)
    assert readiness.blocking_findings == 0, readiness.reason
    assert approval_readiness_many(session, [revision.id])[revision.id].blocking_findings == 0
    # The decisions stay the reviewer's own: nothing is written under anybody's name.
    assert session.query(ReviewAction).count() == actions_before
    decided = [
        finding for finding in after if finding.outcome in {"FAIL", "REVIEW_REQUIRED", "NOT_FOUND"}
    ]
    assert decided
    assert session.query(_links()).count() == len(decided)

    session.execute(
        update(PackageRevision)
        .where(PackageRevision.id == revision.id)
        .values(state=PackageState.AWAITING_REVIEW.value)
    )
    review = open_session(session, package_revision_id=revision.id, reviewer="anant")
    decision = approve_package(
        session,
        principal=Principal("anant", frozenset({Role.REVIEWER})),
        review_session_id=review.id,
    )
    approved = set(
        session.scalars(
            select(ApprovedFinding.finding_id).where(
                ApprovedFinding.approval_id == decision.approval.id
            )
        )
    )
    assert approved == {finding.id for finding in after}

    # The signed record freezes the carried decision as the reviewer's own, and re-verifies it.
    snapshot = session.scalars(
        select(ApprovalExportSnapshot).where(
            ApprovalExportSnapshot.approval_id == decision.approval.id
        )
    ).one()
    payload = load_snapshot(session, snapshot)
    for frozen in payload.review.findings:
        if frozen.outcome not in {"FAIL", "REVIEW_REQUIRED", "NOT_FOUND"}:
            continue
        assert frozen.actions, frozen
        last = frozen.actions[-1]
        assert last.carried_from_finding_id is not None
        assert last.carried_from_finding_id in before
        assert last.reviewer == "anant"
        assert last.note is not None and last.note.startswith(NOTE)
        assert "carried over from the previous check run" in frozen.wording.lower()


def test_a_carried_decision_shows_the_original_actor_and_note_marked_carried(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, anchors = _two_decided_rows(session, tmp_path)
    revision = _revision_of(session, package_id)
    old = {finding.scope_row_candidate_id: finding for finding in _live(session, revision.id)}
    old_row = next(
        finding
        for finding in _live(session, revision.id)
        if finding.scope_row_candidate_id == anchors[0] and finding.outcome == "FAIL"
    )
    original = session.scalars(
        select(ReviewAction).where(ReviewAction.finding_id == old_row.id)
    ).one()

    _run_current_checks(session, package_id, tmp_path)
    new_row = next(
        finding
        for finding in _live(session, revision.id)
        if finding.scope_row_candidate_id == anchors[0] and finding.outcome == "FAIL"
    )
    assert old[anchors[0]].id != new_row.id
    client = _client(session, project_id)

    results = client.get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/countertop-results"
    )
    assert results.status_code == 200, results.text
    item = next(row for row in results.json()["items"] if row["row_id"] == str(anchors[0]))
    assert item["finding_id"] == str(new_row.id)
    assert item["needs_decision"] is False
    decision = item["reviewer_decision"]
    assert decision["action"] == "confirm"
    assert decision["actor"] == original.actor
    assert decision["note"] == original.note
    assert decision["carried_over"] is True
    assert decision["carried_from_finding_id"] == str(old_row.id)
    assert datetime.fromisoformat(decision["time"]) == original.created_at

    listed = client.get(f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/findings")
    assert listed.status_code == 200, listed.text
    entry = next(row for row in listed.json()["items"] if row["id"] == str(new_row.id))
    assert entry["reviewer_action"]["actor"] == original.actor
    assert entry["reviewer_action"]["note"] == original.note
    assert entry["reviewer_action"]["carried_over"] is True
    assert entry["reviewer_action"]["carried_from_finding_id"] == str(old_row.id)

    history = client.get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/findings/{new_row.id}/actions"
    )
    assert history.status_code == 200, history.text
    (carried,) = history.json()["items"]
    assert carried["id"] == str(original.id)
    assert carried["finding_id"] == str(old_row.id)
    assert carried["actor"] == original.actor
    assert carried["note"] == original.note
    assert carried["carried_over"] is True
    assert carried["carried_from_finding_id"] == str(old_row.id)

    # A fresh decision on the new result is the reviewer's own again, and wins.
    review = open_session(session, package_revision_id=revision.id, reviewer="second reviewer")
    record_action(
        session,
        review_session_id=review.id,
        finding_id=new_row.id,
        action=ReviewActionKind.DISMISS,
        actor="second reviewer",
        note=f"{NOTE} changed mind",
    )
    session.flush()
    item = next(
        row
        for row in client.get(
            f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/countertop-results"
        ).json()["items"]
        if row["row_id"] == str(anchors[0])
    )
    assert item["reviewer_decision"]["actor"] == "second reviewer"
    assert item["reviewer_decision"]["carried_over"] is False
    assert item["reviewer_decision"]["carried_from_finding_id"] is None
    history = client.get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/findings/{new_row.id}/actions"
    ).json()["items"]
    assert [entry["carried_over"] for entry in history] == [False, True]


@pytest.mark.parametrize("change", ["value", "walls", "the same values again"])
def test_a_change_on_one_row_asks_only_that_row_again(
    session: Session, tmp_path: Path, change: str
) -> None:
    project_id, package_id, anchors = _two_decided_rows(session, tmp_path)
    revision = _revision_of(session, package_id)
    if change == "value":
        review_slot_row(
            _principal(project_id),
            _principal(project_id),
            session,
            project_id,
            package_id,
            anchors[0],
            SlotRowReviewIn(measurements={"countertop_width": "41 in", "piece_widths:0": "20 in"}),
        )
    elif change == "walls":
        _save_all_row_widths(session, project_id, package_id, anchors[0], wall_config="back_only")
    else:
        # A new decision record superseding the last, with the same typed widths (#1177): the
        # widths are compared by value, the record that holds them is not.
        _save_all_row_widths(session, project_id, package_id, anchors[0])

    _run_current_checks(session, package_id, tmp_path)
    if change == "the same values again":
        assert approval_readiness(session, revision.id).blocking_findings == 0
        return

    readiness = approval_readiness(session, revision.id)
    blocked = {
        finding.scope_row_candidate_id
        for finding in _live(session, revision.id)
        if finding.id in readiness.blocking_finding_ids
    }
    assert blocked == {anchors[0]}
    carried_rows = {
        finding.scope_row_candidate_id
        for finding in session.scalars(
            select(Finding).join(_links(), _links().new_finding_id == Finding.id)
        )
    }
    assert anchors[0] not in carried_rows
    assert anchors[1] in carried_rows


# ---------------------------------------------------------------------------
# The rule itself, on hand-built stored results
# ---------------------------------------------------------------------------


def _snapshot(
    session: Session,
    rule_id: str,
    body: str = "{}",
    version: str = "1.0.0",
    *,
    parameters: tuple[str, ...] = (),
    walls: bool = False,
    check_type: str = "internal",
) -> RuleSnapshot:
    """A published rule. `parameters` it declares, `walls` resolves it by the wall layout and
    `check_type="arch_vs_shop"` makes it compare with the architect: what it reads (#1177)."""
    definition = session.scalars(
        select(RuleDefinition).where(RuleDefinition.rule_id == rule_id)
    ).one_or_none()
    if definition is None:
        definition = RuleDefinition(rule_id=rule_id)
        session.add(definition)
        session.flush()
    content = f'{{"id":"{rule_id}","body":{body}}}'
    if parameters or walls:
        declared: dict[str, object] = {"id": rule_id, "body": json.loads(body)}
        if parameters:
            declared["parameters"] = {name: {"scope": "project"} for name in parameters}
        if walls:
            declared["applicability"] = {"discriminator": "wall_config"}
        content = json.dumps(declared, sort_keys=True, separators=(",", ":"))
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(content.encode()).hexdigest()}",
        version=version,
        canonical_json=content,
        product_type="countertop",
        check_type=check_type,
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _stored(
    session: Session,
    revision: PackageRevision,
    snapshot: RuleSnapshot,
    *,
    outcome: str = "REVIEW_REQUIRED",
    parameters: dict[str, str] | None = None,
    reason: str = "Synthetic held result.",
    scope_item_id: UUID | None = None,
    scope_label: str | None = None,
    row: UUID | None = None,
    notes: list[str] | None = None,
    trace: dict[str, object] | None = None,
    engine_version: str = "verdict-test",
    defaults: ParameterLayerSet | None = None,
    inputs: tuple[dict[str, object], ...] = (),
) -> Finding:
    """One stored result. `row` scopes it to a slot-reader row; `defaults` is a rulebook
    defaults set (`rules.parameters.ParameterSet`) the run stored; `inputs` its sealed values."""
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version=engine_version,
        defaults_set_id=None if defaults is None else defaults.set_id,
        defaults_canonical_json=(None if defaults is None else defaults.canonical_json()),
    )
    session.add(run)
    session.flush()
    for operand in inputs:
        session.add(VerdictInput(check_run_id=run.id, **operand))
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=outcome,
        severity="CRITICAL",
        trace=trace or {"cause": "needs_review", "reason": reason, "outcome": outcome},
        parameter_set_versions=parameters or {"global": "sha256:" + "1" * 64},
        reason=reason,
        notes=notes or [],
        scope_item_id=scope_item_id,
        scope_row_candidate_id=row,
        scope_label=scope_label if row is None else (scope_label or "Countertop row on page 1"),
    )
    session.add(finding)
    session.flush()
    return finding


def _rerun(
    session: Session,
    revision: PackageRevision,
    previous: list[Finding],
    *new: tuple[RuleSnapshot, dict[str, object]],
    when: datetime | None = None,
) -> tuple[list[Finding], int]:
    from app.review.carry_over import carry_decisions_over

    supersede_runs(session, revision.id)
    written = [_stored(session, revision, snapshot, **options) for snapshot, options in new]  # type: ignore[arg-type]
    carried = carry_decisions_over(
        session,
        package_revision_id=revision.id,
        previous_finding_ids=[finding.id for finding in previous],
        when=when,
    )
    return written, carried


def _dismiss(session: Session, finding: Finding, note: str | None = NOTE) -> ReviewAction:
    review = open_session(session, package_revision_id=finding.package_revision_id, reviewer="a")
    return record_action(
        session,
        review_session_id=review.id,
        finding_id=finding.id,
        action=ReviewActionKind.DISMISS,
        actor="a",
        note=note,
    )


def test_an_identical_stored_result_carries_the_decision(session: Session) -> None:
    revision = _revision(session)
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}")
    old = _stored(session, revision, snapshot)
    action = _dismiss(session, old)
    (new,), carried = _rerun(session, revision, [old], (snapshot, {}))
    assert carried == 1
    link = session.scalars(select(_links())).one()
    assert (link.new_finding_id, link.from_finding_id, link.review_action_id) == (
        new.id,
        old.id,
        action.id,
    )
    assert approval_readiness(session, revision.id).blocking_findings == 0


@pytest.mark.parametrize(
    "change", ["outcome", "setting", "rule version", "reason", "engine version"]
)
def test_each_kind_of_change_drops_the_decision(session: Session, change: str) -> None:
    revision = _revision(session)
    rule_id = f"CT-{uuid4().hex[:6]}"
    # The rule declares a parameter (#1177): a setting counts only for a rule that reads one. Its
    # value is read back through the real citation: the rulebook default (1 in), then a project
    # value (2 in) for the "setting" case.
    snapshot = _snapshot(session, rule_id, parameters=("field_cut",))
    defaults = _defaults()
    old = _stored(
        session,
        revision,
        snapshot,
        defaults=defaults,
        parameters=_cite(session, revision, defaults),
    )
    _dismiss(session, old)
    later = _defaults()
    if change == "setting":
        project_id = session.get(Package, revision.package_id).project_id  # type: ignore[union-attr]
        _store_layer(
            session, _layer(project_id, ParameterLayer.PROJECT, 1, {"field_cut": Fraction(2)})
        )
    options: dict[str, object] = {
        "defaults": later,
        "parameters": _cite(session, revision, later),
    }
    replacement = snapshot
    if change == "outcome":
        options["outcome"] = "NOT_FOUND"
    elif change == "rule version":
        replacement = _snapshot(
            session,
            rule_id,
            body='{"tolerance":"changed"}',
            version="1.0.1",
            parameters=("field_cut",),
        )
    elif change == "engine version":
        options["engine_version"] = "verdict-test-2"
    elif change == "reason":
        # A held result's reason is the one record of why it held (no hold has a code yet).
        options["reason"] = "A different synthetic reason."
    _, carried = _rerun(session, revision, [old], (replacement, options))
    assert carried == 0
    assert approval_readiness(session, revision.id).blocking_findings == 1


def test_a_correction_never_carries(session: Session) -> None:
    revision = _revision(session)
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}")
    old = _stored(session, revision, snapshot, outcome="FAIL")
    review = open_session(session, package_revision_id=revision.id, reviewer="a")
    record_action(
        session,
        review_session_id=review.id,
        finding_id=old.id,
        action=ReviewActionKind.CORRECT,
        actor="a",
        note=NOTE,
    )
    _, carried = _rerun(session, revision, [old], (snapshot, {"outcome": "FAIL"}))
    assert carried == 0


def test_a_confirm_after_a_correction_never_carries(session: Session) -> None:
    revision = _revision(session)
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}")
    old = _stored(session, revision, snapshot, outcome="FAIL")
    review = open_session(session, package_revision_id=revision.id, reviewer="a")
    for action in (ReviewActionKind.CORRECT, ReviewActionKind.CONFIRM):
        record_action(
            session,
            review_session_id=review.id,
            finding_id=old.id,
            action=action,
            actor="a",
            note=NOTE,
        )
    _, carried = _rerun(session, revision, [old], (snapshot, {"outcome": "FAIL"}))
    assert carried == 0


def test_an_expired_exception_never_carries(session: Session) -> None:
    revision = _revision(session)
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}")
    old = _stored(session, revision, snapshot, outcome="FAIL")
    review = open_session(session, package_revision_id=revision.id, reviewer="a")
    grant_exception(
        session,
        review_session_id=review.id,
        finding_id=old.id,
        actor="a",
        scope=ExceptionScope.FINDING,
        scope_id=old.id,
        reason=NOTE,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    _, carried = _rerun(
        session,
        revision,
        [old],
        (snapshot, {"outcome": "FAIL"}),
        when=datetime.now(UTC) + timedelta(hours=2),
    )
    assert carried == 0


def test_a_carried_exception_blocks_again_once_it_expires(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = _revision(session)
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}")
    old = _stored(session, revision, snapshot, outcome="FAIL")
    review = open_session(session, package_revision_id=revision.id, reviewer="a")
    grant_exception(
        session,
        review_session_id=review.id,
        finding_id=old.id,
        actor="a",
        scope=ExceptionScope.FINDING,
        scope_id=old.id,
        reason=NOTE,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    (new,), carried = _rerun(session, revision, [old], (snapshot, {"outcome": "FAIL"}))
    assert carried == 1
    assert approval_readiness(session, revision.id).blocking_findings == 0
    monkeypatch.setattr(approval_module, "utc_now", lambda: datetime.now(UTC) + timedelta(hours=2))
    assert approval_readiness(session, revision.id).blocking_finding_ids == (new.id,)


def test_a_note_less_dismiss_of_a_fail_never_carries(session: Session) -> None:
    """A legacy action written before notes were enforced; the service refuses one today."""
    revision = _revision(session)
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}")
    old = _stored(session, revision, snapshot, outcome="FAIL")
    review = open_session(session, package_revision_id=revision.id, reviewer="a")
    session.add(
        ReviewAction(
            review_session_id=review.id,
            finding_id=old.id,
            package_revision_id=revision.id,
            action=ReviewActionKind.DISMISS.value,
            actor="a",
            note=None,
        )
    )
    session.flush()
    _, carried = _rerun(session, revision, [old], (snapshot, {"outcome": "FAIL"}))
    assert carried == 0


def test_a_hand_confirmed_countertop_result_never_carries(session: Session, tmp_path: Path) -> None:
    """The legacy manual path keeps its inputs in the countertop-run tables, which this feature
    does not read, so its result always asks again even when nothing changed."""
    from app.review.carry_over import carry_decisions_over, result_fingerprints

    assembly = Assembly(session, LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only"))
    revision = assembly.revision
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}")
    item = {"scope_item_id": assembly.parts[0], "scope_label": "Synthetic countertop"}
    old = _stored(session, revision, snapshot, **item)
    _dismiss(session, old)
    supersede_runs(session, revision.id)
    new = _stored(session, revision, snapshot, **item)
    assert len(set(result_fingerprints(session, [old, new]).values())) == 1
    carried = carry_decisions_over(
        session, package_revision_id=revision.id, previous_finding_ids=[old.id]
    )
    assert carried == 0
    assert new.id in approval_readiness(session, revision.id).blocking_finding_ids


def test_nothing_carries_from_another_revision(session: Session) -> None:
    first = _revision(session)
    second = _revision(session)
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}")
    elsewhere = _stored(session, first, snapshot)
    _dismiss(session, elsewhere)
    _stored(session, second, snapshot)
    from app.review.carry_over import carry_decisions_over

    carried = carry_decisions_over(
        session, package_revision_id=second.id, previous_finding_ids=[elsewhere.id]
    )
    assert carried == 0
    assert approval_readiness(session, second.id).blocking_findings == 1


def test_the_database_refuses_a_cross_revision_link(session: Session) -> None:
    first = _revision(session)
    second = _revision(session)
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}")
    elsewhere = _stored(session, first, snapshot)
    action = _dismiss(session, elsewhere)
    here = _stored(session, second, snapshot)
    # A savepoint, never a commit: the shared test schema must not keep rows between tests.
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            _links()(
                package_revision_id=second.id,
                new_finding_id=here.id,
                from_finding_id=elsewhere.id,
                review_action_id=action.id,
                action=action.action,
                matched_on_hash="0" * 64,
            )
        )
        session.flush()


def test_carry_links_are_append_only(session: Session) -> None:
    revision = _revision(session)
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}")
    old = _stored(session, revision, snapshot)
    _dismiss(session, old)
    _rerun(session, revision, [old], (snapshot, {}))
    assert session.query(_links()).count() == 1
    for statement in (
        "UPDATE finding_decision_carryovers SET matched_on_hash = repeat('1', 64)",
        "DELETE FROM finding_decision_carryovers",
    ):
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(text(statement))
    assert session.query(_links()).count() == 1


def test_another_projects_history_is_not_found(session: Session, tmp_path: Path) -> None:
    project_id, package_id, anchors = _two_decided_rows(session, tmp_path)
    _run_current_checks(session, package_id, tmp_path)
    revision = _revision_of(session, package_id)
    new_row = next(
        finding
        for finding in _live(session, revision.id)
        if finding.scope_row_candidate_id == anchors[0]
    )
    stranger = uuid4()
    response = _client(session, stranger).get(
        f"{API_PREFIX}/projects/{stranger}/packages/{package_id}/findings/{new_row.id}/actions"
    )
    assert response.status_code == 404
    other_package = uuid4()
    response = _client(session, project_id).get(
        f"{API_PREFIX}/projects/{project_id}/packages/{other_package}/findings/{new_row.id}/actions"
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Only the facts the rule used (#1177)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["notes", "decided reason", "trace explanation"])
def test_a_wording_only_change_still_carries(session: Session, change: str) -> None:
    """The audit's page-9 case: #1138 reworded the wall-layout note and the decision was lost."""
    revision = _revision(session)
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}")

    def calculation(explanation: str) -> dict[str, object]:
        return {
            "operation": "equals",
            "operands": [{"name": "countertop_width", "value": "40 in", "source": "SHOP"}],
            "intermediates": [],
            "comparison": "40 != 41",
            "tolerance": None,
            "arithmetic_unit": "in",
            "outcome": "FAIL",
            "engine_version": "verdict-test",
            "operation_version": "1",
            "explanation": [explanation],
        }

    before: dict[str, object] = {}
    after: dict[str, object] = {}
    if change == "notes":
        before["notes"] = [
            "Wall layout back_left_right established from vendor drawing clues for this row."
        ]
        after["notes"] = [
            (
                "Wall layout: back wall and both ends (back_left_right), established from vendor "
                "drawing clues for this row."
            )
        ]
    elif change == "decided reason":
        before |= {"outcome": "FAIL", "reason": "The countertop is 1 in short."}
        after |= {"outcome": "FAIL", "reason": "Synthetic: the row needs 1 in more stone."}
    else:
        before |= {"outcome": "FAIL", "trace": calculation("Synthetic explanation.")}
        after |= {"outcome": "FAIL", "trace": calculation("Synthetic explanation, reworded.")}
    old = _stored(session, revision, snapshot, **before)  # type: ignore[arg-type]
    _dismiss(session, old)
    _, carried = _rerun(session, revision, [old], (snapshot, after))
    assert carried == 1
    assert approval_readiness(session, revision.id).blocking_findings == 0


def _defaults(field_cut: Fraction = Fraction(1)) -> ParameterLayerSet:
    """The rulebook defaults a run stores, stamped with the moment the run read them."""
    return _layer(None, ParameterLayer.GLOBAL, 1, {"field_cut": field_cut}, by="rulebook default")


def _layer(
    project_id: UUID | None,
    layer: ParameterLayer,
    version: int,
    values: dict[str, Fraction],
    *,
    by: str = "anant (synthetic test)",
) -> ParameterLayerSet:
    return ParameterLayerSet(
        project_id=None if project_id is None else str(project_id),
        layer=layer,
        version=version,
        parameters={
            name: ParameterLayerValue(
                value=Quantity(value=value, unit=Unit.INCH),
                provenance=Provenance.MEASURED,
                set_by=by,
                set_at=datetime.now(UTC),
            )
            for name, value in values.items()
        },
    )


def _store_layer(
    session: Session, layer: ParameterLayerSet, revision_id: UUID | None = None
) -> str:
    stored, values = to_rows(layer, package_revision_id=revision_id)
    session.add(stored)
    session.flush()
    for value in values:
        value.parameter_set_id = stored.id
    session.add_all(values)
    session.flush()
    return layer.set_id


def _cite(
    session: Session, revision: PackageRevision, defaults: ParameterLayerSet
) -> dict[str, str]:
    """What a check run cites, the way `run_checks` builds it: the stored GLOBAL and PROJECT sets
    (`load_parameter_sets`), the RUN layer (`run_parameters_for`, measurements left out) and the
    rulebook defaults where no company set was stored (`_cited_sets`)."""
    project_id = session.get(Package, revision.package_id).project_id  # type: ignore[union-attr]
    run_layer = run_parameters_for(session, revision.id)
    return _cited_sets(
        defaults,
        [*load_parameter_sets(session, project_id), *((run_layer,) if run_layer else ())],
    )


type _Save = tuple[ParameterLayer, dict[str, Fraction]]

_TYPED = "CT-WIDTH-001:cabinet_widths#0"


@pytest.mark.parametrize(
    ("before", "after", "later_default", "carries"),
    [
        pytest.param(
            [(ParameterLayer.PROJECT, {"field_cut": 2, "backsplash_thickness": 1})],
            [(ParameterLayer.PROJECT, {"field_cut": 2, "backsplash_thickness": Fraction(3, 2)})],
            1,
            True,
            id="a project value only another rule reads",
        ),
        pytest.param(
            [(ParameterLayer.PROJECT, {"field_cut": 2, "backsplash_thickness": 1})],
            [(ParameterLayer.PROJECT, {"field_cut": 2, "backsplash_thickness": 1})],
            1,
            True,
            id="the same values saved again",
        ),
        pytest.param(
            [(ParameterLayer.PROJECT, {"field_cut": 2, "backsplash_thickness": 1})],
            [(ParameterLayer.PROJECT, {"field_cut": Fraction(5, 2), "backsplash_thickness": 1})],
            1,
            False,
            id="a project value this rule reads",
        ),
        pytest.param(
            [(ParameterLayer.RUN, {"sink_interior_depth": 20, _TYPED: 24})],
            [(ParameterLayer.RUN, {"sink_interior_depth": 20, _TYPED: 24, f"{_TYPED[:-1]}1": 30})],
            1,
            True,
            id="a measurement typed into a run set that also holds a setting",
        ),
        pytest.param(
            [
                (ParameterLayer.PROJECT, {"field_cut": 2}),
                (ParameterLayer.RUN, {"field_cut": 3, _TYPED: 24}),
            ],
            [(ParameterLayer.PROJECT, {"field_cut": 5})],
            1,
            True,
            id="a project value under the run value this rule reads",
        ),
        pytest.param(
            [
                (ParameterLayer.PROJECT, {"field_cut": 2}),
                (ParameterLayer.RUN, {"field_cut": 3, _TYPED: 24}),
            ],
            [(ParameterLayer.RUN, {"field_cut": 4, _TYPED: 24})],
            1,
            False,
            id="the run value this rule reads",
        ),
        pytest.param(
            [(ParameterLayer.GLOBAL, {"backsplash_thickness": 1})],
            [(ParameterLayer.GLOBAL, {"backsplash_thickness": 2})],
            1,
            True,
            id="a company value only another rule reads",
        ),
        pytest.param(
            [(ParameterLayer.GLOBAL, {"backsplash_thickness": 1})],
            [],
            2,
            False,
            id="the rulebook default this rule reads beneath a company set",
        ),
        pytest.param(
            [(ParameterLayer.GLOBAL, {"backsplash_thickness": 1})],
            [(ParameterLayer.GLOBAL, {"backsplash_thickness": 1, "field_cut": 3})],
            1,
            False,
            id="a company value this rule reads",
        ),
    ],
)
def test_only_the_settings_a_rule_reads_ask_it_again(
    session: Session,
    before: list[_Save],
    after: list[_Save],
    later_default: int,
    carries: bool,
) -> None:
    """The audit's second case: saving one project value re-asked every rule's decision. Each run
    cites its sets exactly as `run_checks` does; the rule declares only `field_cut`."""
    revision = _revision(session)
    project_id = session.get(Package, revision.package_id).project_id  # type: ignore[union-attr]
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}", parameters=("field_cut",))
    versions: dict[ParameterLayer, int] = defaultdict(int)

    def save(saves: list[_Save]) -> None:
        for layer, values in saves:
            versions[layer] += 1
            _store_layer(
                session,
                _layer(
                    None if layer is ParameterLayer.GLOBAL else project_id,
                    layer,
                    versions[layer],
                    {name: Fraction(value) for name, value in values.items()},
                ),
                revision.id if layer is ParameterLayer.RUN else None,
            )

    save(before)
    defaults = _defaults()
    old = _stored(
        session,
        revision,
        snapshot,
        defaults=defaults,
        parameters=_cite(session, revision, defaults),
    )
    _dismiss(session, old)
    save(after)
    # A new run reads the rulebook defaults again, at a new moment: a new set id.
    later = _defaults(Fraction(later_default))
    # Beneath a company set the citation stays the same; only the defaults the run stored differ.
    cited = _cite(session, revision, later)
    _, carried = _rerun(
        session, revision, [old], (snapshot, {"defaults": later, "parameters": cited})
    )
    assert carried == (1 if carries else 0)


def test_an_unreadable_cited_set_still_counts_the_defaults_beneath(session: Session) -> None:
    """When a value cannot be read back (here a cited RUN set stored nowhere), the set ids are
    compared, and the rulebook defaults by content with them: a changed default asks again."""
    revision = _revision(session)
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}", parameters=("field_cut",))
    company = _store_layer(
        session, _layer(None, ParameterLayer.GLOBAL, 1, {"backsplash_thickness": Fraction(1)})
    )
    nowhere = {"global": company, "run": "sha256:" + "9" * 64}
    old = _stored(session, revision, snapshot, defaults=_defaults(), parameters=nowhere)
    _dismiss(session, old)
    _, carried = _rerun(
        session,
        revision,
        [old],
        (snapshot, {"defaults": _defaults(Fraction(2)), "parameters": nowhere}),
    )
    assert carried == 0
    # The same defaults (a new stamp only) still carry on the fallback.
    old = _stored(session, revision, snapshot, defaults=_defaults(), parameters=nowhere)
    _dismiss(session, old)
    _, carried = _rerun(
        session, revision, [old], (snapshot, {"defaults": _defaults(), "parameters": nowhere})
    )
    assert carried == 1


def _row_rules(session: Session) -> tuple[RuleSnapshot, RuleSnapshot]:
    """A width rule resolved by the wall layout and an architect comparison, as #1054 has."""
    return (
        _snapshot(session, f"CT-W-{uuid4().hex[:6]}", walls=True),
        _snapshot(session, f"CT-A-{uuid4().hex[:6]}", check_type="arch_vs_shop"),
    )


@pytest.mark.parametrize(
    ("change", "carried_rules"),
    [
        ("architect pairing", {"width"}),
        ("architect view match", {"width"}),
        ("wall layout", {"architect"}),
        ("typed width", set()),
        ("the same wall saved again", {"width", "architect"}),
    ],
)
def test_a_click_on_a_row_asks_again_only_the_rule_that_reads_it(
    session: Session, change: str, carried_rules: set[str]
) -> None:
    """An architect-side click no longer drops the width decision; a wall no longer drops the
    architect's; a typed width is read by both."""
    _project_id, package_id, anchors = _package_rows(session, wall_source="code-ink-and-both-ais")
    revision = _revision_of(session, package_id)
    anchor = session.get(ObservationCandidate, anchors[0])
    assert anchor is not None
    width, architect = _row_rules(session)
    old = [
        _stored(session, revision, width, row=anchor.id),
        _stored(session, revision, architect, row=anchor.id),
    ]
    for finding in old:
        _dismiss(session, finding)
    if change == "architect pairing":
        session.add(
            ArchitectPairingRecord(
                package_revision_id=revision.id,
                page_id=anchor.page_id,
                row_anchor_candidate_id=anchor.id,
                extraction_run_id=None,
                source="reviewer",
                status="reviewer",
                pairs=[],
                details={"reasons": ["TEST ONLY synthetic hand pairing"]},
                decided_by="a",
            )
        )
    elif change == "architect view match":
        # The reviewer's "none of these" for the architect's views: no pairing is written.
        session.add(
            ArchitectViewMatchRecord(
                package_revision_id=revision.id,
                vendor_page_id=anchor.page_id,
                row_anchor_candidate_id=anchor.id,
                extraction_run_id=None,
                source="reviewer",
                status="none_matches",
                matched_view_id=None,
                ai_picks=[],
                candidates=[],
                vendor_references=[],
                reasons=["TEST ONLY synthetic none of these"],
                details={},
                decided_by="a",
            )
        )
    elif change == "wall layout":
        session.add(
            SlotRowReviewDecision(
                row_candidate_id=anchor.id,
                wall_config="back_only",
                measurements={},
                confirmed_by="a",
            )
        )
    elif change == "typed width":
        session.add(
            SlotRowReviewDecision(
                row_candidate_id=anchor.id,
                wall_config=None,
                measurements={
                    "piece_widths:0": {
                        "numerator": 21,
                        "denominator": 1,
                        "unit": "in",
                        "display": "21 in",
                    }
                },
                confirmed_by="a",
            )
        )
    else:
        # The wall the drawing clues already gave, confirmed by a reviewer: the same layout.
        session.add(
            SlotRowReviewDecision(
                row_candidate_id=anchor.id,
                wall_config="back_left_right",
                measurements={},
                confirmed_by="a",
            )
        )
    session.flush()
    (new_width, new_architect), carried = _rerun(
        session, revision, old, (width, {"row": anchor.id}), (architect, {"row": anchor.id})
    )
    names = {new_width.id: "width", new_architect.id: "architect"}
    linked = {
        names[link.new_finding_id] for link in session.scalars(select(FindingDecisionCarryover))
    }
    assert linked == carried_rules
    assert carried == len(carried_rules)


def _observation(anchor: ObservationCandidate, value: int) -> CanonicalObservation:
    return CanonicalObservation(
        document_version_id=anchor.document_version_id,
        page_id=anchor.page_id,
        document_role=DocumentRole.SHOP.value,
        polygon=[["0.1", "0.1"], ["0.2", "0.1"], ["0.2", "0.2"]],
        coordinate_space="stored",
        semantic_type=SemanticType.COUNTERTOP_OVERALL_WIDTH.value,
        value_numerator=value,
        value_denominator=1,
        unit=Unit.INCH.value,
        status=EvidenceStatus.CORROBORATED.value,
        authority=Authority.AUTHORITATIVE.value,
    )


@pytest.mark.parametrize(("value", "carries"), [(40, True), (41, False)])
def test_a_sealed_value_is_compared_by_value_not_by_its_observation_id(
    session: Session, value: int, carries: bool
) -> None:
    """A re-read mints new observation ids: the same value carries, a changed one asks again."""
    _project_id, package_id, anchors = _package_rows(session, wall_source="code-ink-and-both-ais")
    revision = _revision_of(session, package_id)
    anchor = session.get(ObservationCandidate, anchors[0])
    assert anchor is not None
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}")
    observations = [_observation(anchor, 40), _observation(anchor, value)]
    session.add_all(observations)
    session.flush()

    def result(observation: CanonicalObservation) -> dict[str, object]:
        number = observation.value_numerator
        return {
            "row": anchor.id,
            "outcome": "FAIL",
            "trace": {
                "operation": "equals",
                "operands": [
                    {
                        "name": "countertop_width",
                        "value": f"{number} in",
                        "source": "SHOP",
                        "evidence_ref": str(observation.id),
                    }
                ],
                "outcome": "FAIL",
            },
            "inputs": (
                {
                    "operand_name": "countertop_width",
                    "value_numerator": number,
                    "value_denominator": 1,
                    "unit": "in",
                    "evidence_status": "CORROBORATED",
                    "canonical_observation_id": observation.id,
                },
            ),
        }

    old = _stored(session, revision, snapshot, **result(observations[0]))  # type: ignore[arg-type]
    _dismiss(session, old)
    _, carried = _rerun(session, revision, [old], (snapshot, result(observations[1])))
    assert carried == (1 if carries else 0)


_COPIED = (
    "document_version_id",
    "page_id",
    "raw_text",
    "value_numerator",
    "value_denominator",
    "unit",
    "polygon",
    "coordinate_space",
    "review_reason",
    "corroboration_status",
    "corroboration_lane",
)


def _reread(
    session: Session, revision_id: UUID, *, changed: UUID | None = None, moved: int = 0
) -> dict[UUID, UUID]:
    """A new slot-reader run that reads the newest one's rows again with new ids (as a re-read
    with stored answers reused does). `changed` reads one more inch on that one candidate;
    `moved` draws every candidate that many pixels lower (another drawn row, same numbers).
    Returns old candidate id -> new candidate id."""
    previous = session.get(ExtractionRun, latest_slot_reader_run(session, revision_id))
    assert previous is not None
    task = session.get(TaskRun, previous.task_run_id)
    assert task is not None
    again = TaskRun(
        workflow_run_id=task.workflow_run_id,
        idempotency_key=str(uuid4()),
        task_type=task.task_type,
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(again)
    session.flush()
    run = ExtractionRun(
        task_run_id=again.id,
        extractor=previous.extractor,
        extractor_version=previous.extractor_version,
        config_hash=previous.config_hash,
        dpi=previous.dpi,
    )
    session.add(run)
    session.flush()
    copies: dict[UUID, UUID] = {}
    for candidate in session.scalars(
        select(ObservationCandidate)
        .where(ObservationCandidate.extraction_run_id == previous.id)
        .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
    ).all():
        copy = ObservationCandidate(
            extraction_run_id=run.id,
            ambiguity_flags=list(candidate.ambiguity_flags or []),
            **{name: getattr(candidate, name) for name in _COPIED},
        )
        if candidate.id == changed:
            assert copy.value_numerator is not None
            copy.value_numerator += 1
        if moved:
            copy.polygon = [[x, int(y) + moved] for x, y in copy.polygon]
        session.add(copy)
        session.flush()
        copies[candidate.id] = copy.id
    for proposal in session.scalars(
        select(MeasurementProposal).where(MeasurementProposal.candidate_id.in_(copies))
    ).all():
        session.add(
            MeasurementProposal(
                package_revision_id=proposal.package_revision_id,
                page_number=proposal.page_number,
                proposal_id=uuid4(),
                field_key=proposal.field_key,
                position=proposal.position,
                candidate_id=copies[proposal.candidate_id],
                placement_verified=proposal.placement_verified,
                model_id=proposal.model_id,
                prompt_id=proposal.prompt_id,
            )
        )
    session.flush()
    return copies


@pytest.mark.parametrize("changed", [False, True])
def test_a_reread_of_the_same_rows_carries_unless_a_reading_changed(
    session: Session, tmp_path: Path, changed: bool
) -> None:
    """A re-read mints new row and candidate ids; the rows are named by file, page and rank, and
    compared by what was read on them. Held rows seal no operand, so their readings count."""
    _project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="code-ink-and-both-ais"
    )
    _run_current_checks(session, package_id, tmp_path)
    revision = _revision_of(session, package_id)
    _decide_everything(session, revision.id)
    assert approval_readiness(session, revision.id).blocking_findings == 0

    copies = _reread(session, revision.id, changed=anchors[0] if changed else None)
    _run_current_checks(session, package_id, tmp_path)

    live_rows = {
        finding.scope_row_candidate_id
        for finding in _live(session, revision.id)
        if finding.scope_row_candidate_id is not None
    }
    assert live_rows == {copies[anchors[0]], copies[anchors[1]]}, "the re-read's own rows"
    readiness = approval_readiness(session, revision.id)
    blocked = {
        finding.scope_row_candidate_id
        for finding in _live(session, revision.id)
        if finding.id in readiness.blocking_finding_ids
    }
    assert blocked == ({copies[anchors[0]]} if changed else set())


def test_a_reread_finding_another_drawn_row_at_the_same_rank_asks_again(session: Session) -> None:
    """Rank and numbers alone do not name a row: the same rank may be another drawn row after a
    re-read. Where the row is drawn is part of its identity."""
    _project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="code-ink-and-both-ais"
    )
    revision = _revision_of(session, package_id)
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}", walls=True)
    old = _stored(session, revision, snapshot, row=anchors[0])
    _dismiss(session, old)
    copies = _reread(session, revision.id, moved=300)
    _, carried = _rerun(session, revision, [old], (snapshot, {"row": copies[anchors[0]]}))
    assert carried == 0


def test_a_reread_drawing_the_row_in_the_same_place_carries(session: Session) -> None:
    """The control for the test above: same rank, same place, same numbers, new ids."""
    _project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="code-ink-and-both-ais"
    )
    revision = _revision_of(session, package_id)
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}", walls=True)
    old = _stored(session, revision, snapshot, row=anchors[0])
    _dismiss(session, old)
    copies = _reread(session, revision.id)
    _, carried = _rerun(session, revision, [old], (snapshot, {"row": copies[anchors[0]]}))
    assert carried == 1


@pytest.mark.parametrize(("same_dimension", "carries"), [(True, True), (False, False)])
def test_a_pairing_is_compared_by_which_architect_dimension_it_names(
    session: Session, same_dimension: bool, carries: bool
) -> None:
    """Two 30 in architect dimensions on one page are two pairings: a decision on one never
    carries onto a pairing naming the other, which the reviewer never saw."""
    _project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="code-ink-and-both-ais"
    )
    revision = _revision_of(session, package_id)
    anchor = session.get(ObservationCandidate, anchors[0])
    assert anchor is not None

    def dimension(x: int) -> ObservationCandidate:
        span = ObservationCandidate(
            extraction_run_id=anchor.extraction_run_id,
            document_version_id=anchor.document_version_id,
            page_id=anchor.page_id,
            raw_text='30"',
            value_numerator=30,
            value_denominator=1,
            unit="in",
            polygon=[[x, 10], [x + 20, 10], [x + 20, 20], [x, 20]],
            coordinate_space="image",
            ambiguity_flags=["architect-span"],
        )
        session.add(span)
        session.flush()
        return span

    first, second = dimension(100), dimension(400)

    def pairing(anchor_id: UUID, architect_id: UUID, run_id: UUID) -> None:
        session.add(
            ArchitectPairingRecord(
                package_revision_id=revision.id,
                page_id=anchor.page_id,
                row_anchor_candidate_id=anchor_id,
                extraction_run_id=run_id,
                source="code",
                status="paired",
                pairs=[
                    {
                        "kind": "piece",
                        "architect_candidate_id": str(architect_id),
                        "vendor_slot_indices": [0],
                    }
                ],
                details={"reasons": []},
            )
        )
        session.flush()

    architect = _snapshot(session, f"CT-A-{uuid4().hex[:6]}", check_type="arch_vs_shop")
    pairing(anchor.id, first.id, anchor.extraction_run_id)
    reason = "Synthetic: confirm the pairing of piece 1 with the architect's 30 in."
    old = _stored(session, revision, architect, row=anchor.id, reason=reason)
    _dismiss(session, old)

    copies = _reread(session, revision.id)
    copied_anchor = session.get(ObservationCandidate, copies[anchors[0]])
    assert copied_anchor is not None
    # The re-read pairs piece 1 with the same 30 in dimension again, or with the OTHER one.
    named = copies[first.id] if same_dimension else copies[second.id]
    pairing(copied_anchor.id, named, copied_anchor.extraction_run_id)
    _, carried = _rerun(
        session, revision, [old], (architect, {"row": copied_anchor.id, "reason": reason})
    )
    assert carried == (1 if carries else 0)


@pytest.mark.parametrize(("place", "carries"), [(0, True), (1, False)])
def test_a_sealed_value_read_at_another_place_asks_again(
    session: Session, place: int, carries: bool
) -> None:
    """The same 40 in read at another place on the page is another observation."""
    _project_id, package_id, anchors = _package_rows(session, wall_source="code-ink-and-both-ais")
    revision = _revision_of(session, package_id)
    anchor = session.get(ObservationCandidate, anchors[0])
    assert anchor is not None
    snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}")
    here, there = _observation(anchor, 40), _observation(anchor, 40)
    there.polygon = [["0.5", "0.5"], ["0.6", "0.5"], ["0.6", "0.6"]] if place else there.polygon
    session.add_all((here, there))
    session.flush()

    def result(observation: CanonicalObservation) -> dict[str, object]:
        return {
            "row": anchor.id,
            "outcome": "FAIL",
            "inputs": (
                {
                    "operand_name": "countertop_width",
                    "value_numerator": 40,
                    "value_denominator": 1,
                    "unit": "in",
                    "evidence_status": "CORROBORATED",
                    "canonical_observation_id": observation.id,
                },
            ),
        }

    old = _stored(session, revision, snapshot, **result(here))  # type: ignore[arg-type]
    _dismiss(session, old)
    _, carried = _rerun(session, revision, [old], (snapshot, result(there)))
    assert carried == (1 if carries else 0)


def test_the_wall_in_force_follows_the_supersede_chain_not_the_clock(session: Session) -> None:
    """The decision in force is the one nothing supersedes, even when its stored time is older."""
    _project_id, package_id, anchors = _package_rows(session, wall_source="code-ink-and-both-ais")
    revision = _revision_of(session, package_id)
    width = _snapshot(session, f"CT-W-{uuid4().hex[:6]}", walls=True)
    old = _stored(session, revision, width, row=anchors[0])
    _dismiss(session, old)
    now = datetime.now(UTC)
    session.execute(
        update(CheckRun)
        .where(CheckRun.id == old.check_run_id)
        .values(created_at=now - timedelta(hours=1))
    )
    # The drawing clues say back_left_right; the reviewer's latest word (the chain tip) says
    # back_only, although its stored time is earlier than the record it supersedes.
    first = SlotRowReviewDecision(
        row_candidate_id=anchors[0],
        wall_config="back_left_right",
        measurements={},
        confirmed_by="a",
        created_at=now - timedelta(minutes=10),
    )
    session.add(first)
    session.flush()
    session.add(
        SlotRowReviewDecision(
            row_candidate_id=anchors[0],
            supersedes_id=first.id,
            wall_config="back_only",
            measurements={},
            confirmed_by="a",
            created_at=now - timedelta(minutes=20),
        )
    )
    session.flush()
    session.expire_all()
    _, carried = _rerun(session, revision, [old], (width, {"row": anchors[0]}))
    assert carried == 0


def test_an_architect_click_on_a_row_keeps_its_width_decision_end_to_end(
    session: Session, tmp_path: Path
) -> None:
    """The audit's scenario C through the real check run: a hand pairing on the row, re-run."""
    _project_id, package_id, anchors = _two_decided_rows(session, tmp_path)
    revision = _revision_of(session, package_id)
    anchor = session.get(ObservationCandidate, anchors[0])
    assert anchor is not None
    session.add(
        ArchitectPairingRecord(
            package_revision_id=revision.id,
            page_id=anchor.page_id,
            row_anchor_candidate_id=anchor.id,
            extraction_run_id=None,
            source="reviewer",
            status="reviewer",
            pairs=[],
            details={"reasons": ["TEST ONLY synthetic hand pairing"]},
            decided_by="a",
        )
    )
    session.flush()

    _run_current_checks(session, package_id, tmp_path)

    width = next(
        finding
        for finding in _live(session, revision.id)
        if finding.scope_row_candidate_id == anchor.id
        and finding.check_run_id
        in set(
            session.scalars(
                select(CheckRun.id)
                .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
                .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
                .where(RuleDefinition.rule_id == "CT-WIDTH-001")
            )
        )
    )
    assert session.scalars(
        select(FindingDecisionCarryover).where(FindingDecisionCarryover.new_finding_id == width.id)
    ).one()
    assert width.id not in approval_readiness(session, revision.id).blocking_finding_ids


# ---------------------------------------------------------------------------
# Bounded queries
# ---------------------------------------------------------------------------


def _count(session: Session) -> tuple[list[int], object, object]:
    counter = [0]

    def count(*_args: object) -> None:
        counter[0] += 1

    event.listen(session.bind, "before_cursor_execute", count)
    return counter, session.bind, count


def test_reading_effective_decisions_does_not_grow_with_the_results(session: Session) -> None:
    from app.review.carry_over import carry_decisions_over, decision_records

    def decided(size: int) -> list[UUID]:
        revision = _revision(session)
        snapshot = _snapshot(session, f"CT-{uuid4().hex[:6]}")
        previous = [_stored(session, revision, snapshot, reason=f"r{n}") for n in range(size)]
        for finding in previous:
            _dismiss(session, finding)
        supersede_runs(session, revision.id)
        for n in range(size):
            _stored(session, revision, snapshot, reason=f"r{n}")
        assert (
            carry_decisions_over(
                session,
                package_revision_id=revision.id,
                previous_finding_ids=[finding.id for finding in previous],
            )
            == size
        )
        return [finding.id for finding in _live(session, revision.id)]

    counts = []
    for size in (2, 25):
        ids = decided(size)
        session.flush()
        counter, bind, listener = _count(session)
        try:
            records = decision_records(session, ids)
        finally:
            event.remove(bind, "before_cursor_execute", listener)
        assert len(records.decisions) == size
        assert all(decision.carried_over for decision in records.decisions.values())
        counts.append(counter[0])
    assert counts[0] == counts[1]


def test_countertop_results_stay_bounded_with_carried_decisions(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, _anchors = _package_rows(
        session, unsealed_all=True, wall_source="code-ink-and-both-ais", rows_per_page=10
    )
    _run_current_checks(session, package_id, tmp_path)
    revision = _revision_of(session, package_id)
    _decide_everything(session, revision.id)
    _run_current_checks(session, package_id, tmp_path)
    assert session.query(_links()).count() >= 20
    session.flush()
    counter, bind, listener = _count(session)
    try:
        response = _client(session, project_id).get(
            f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/countertop-results"
        )
    finally:
        event.remove(bind, "before_cursor_execute", listener)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 20
    assert all(item["reviewer_decision"]["carried_over"] for item in items)
    # 13: one statement more than before #1088, the same for any number of rows: sign-off
    # readiness asks once whether a reviewer paired architect dimensions after the checks ran.
    # 14 since #1161: the revision is asked once whether its architect drawings are a separate
    # file, for a row the architect check wrote nothing for; still the same for any number of rows.
    # 15 since #1193: the read pages' drawings are asked for once ("drawings not read"; a second
    # statement only when a read page has pasted drawings); still the same for any number of rows.
    # 17 since #1202: the field cut a run would use now is asked once (and the rulebook's default
    # only when no stored setting holds it); still the same for any number of rows.
    assert counter[0] <= 17


def test_an_id_inside_a_flag_is_replaced_not_dropped() -> None:
    """A flag naming a per-run id still counts as a flag; only the id never does (#1177)."""
    from app.review.carry_over import _stable_flags

    assert _stable_flags([f"supports:{uuid4()}", "slot:0", f"supports:{uuid4()}"]) == [
        "slot:0",
        "supports:<id>",
    ]


def test_only_rekhas_row_gate_flag_is_left_out_of_a_readings_fingerprint() -> None:
    """#1228 review: Rekha's row-gate record on a row's anchor says the row was handed on, not what
    was read, so a reading from before the gate keeps matching; any other gate flag still counts."""
    from app.review.carry_over import _stable_flags

    reading = ["slot:0", "row-rank:1"]
    rekha = "pariksha:rekha:row-has-printed-numbers:pass:printed-numbers"
    other = "pariksha:akshara:seal:stop:not-sealed"

    assert _stable_flags([*reading, rekha]) == _stable_flags(reading)
    assert _stable_flags([*reading, other]) != _stable_flags(reading)
