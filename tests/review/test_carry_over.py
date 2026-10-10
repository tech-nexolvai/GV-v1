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
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
from app.models import ApprovalExportSnapshot, ApprovedFinding, PackageRevision, ReviewAction
from app.models.package import PackageState
from app.models.review import ExceptionScope, ReviewActionKind
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.verdicts import CheckRun, Finding
from app.review import approval as approval_module
from app.review.approval import approval_readiness, approval_readiness_many, approve_package
from app.review.session import grant_exception, open_session, record_action
from app.review.signed_exports import load_snapshot
from app.verdicts.record import supersede_runs
from storage.local import LocalStore
from tests.api.test_slot_rows import _package_rows, _run_current_checks, _save_all_row_widths
from tests.app.postgres_fixture import alembic_config
from tests.review.test_session import _revision
from tests.workflow.test_part_operands import Assembly

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
        session, unsealed_all=True, wall_source="vendor-drawing-clues"
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


@pytest.mark.parametrize("change", ["value", "walls"])
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
    else:
        _save_all_row_widths(session, project_id, package_id, anchors[0], wall_config="back_only")

    _run_current_checks(session, package_id, tmp_path)

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
    session: Session, rule_id: str, body: str = "{}", version: str = "1.0.0"
) -> RuleSnapshot:
    definition = session.scalars(
        select(RuleDefinition).where(RuleDefinition.rule_id == rule_id)
    ).one_or_none()
    if definition is None:
        definition = RuleDefinition(rule_id=rule_id)
        session.add(definition)
        session.flush()
    content = f'{{"id":"{rule_id}","body":{body}}}'
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(content.encode()).hexdigest()}",
        version=version,
        canonical_json=content,
        product_type="countertop",
        check_type="internal",
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
) -> Finding:
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version="verdict-test",
    )
    session.add(run)
    session.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=outcome,
        severity="CRITICAL",
        trace={"cause": "needs_review", "reason": reason, "outcome": outcome},
        parameter_set_versions=parameters or {"GLOBAL": "sha256:" + "1" * 64},
        reason=reason,
        notes=[],
        scope_item_id=scope_item_id,
        scope_label=scope_label,
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


@pytest.mark.parametrize("change", ["outcome", "setting", "rule version", "reason"])
def test_each_kind_of_change_drops_the_decision(session: Session, change: str) -> None:
    revision = _revision(session)
    rule_id = f"CT-{uuid4().hex[:6]}"
    snapshot = _snapshot(session, rule_id)
    old = _stored(session, revision, snapshot)
    _dismiss(session, old)
    options: dict[str, object] = {}
    replacement = snapshot
    if change == "outcome":
        options["outcome"] = "NOT_FOUND"
    elif change == "setting":
        options["parameters"] = {"GLOBAL": "sha256:" + "2" * 64}
    elif change == "rule version":
        replacement = _snapshot(session, rule_id, body='{"tolerance":"changed"}', version="1.0.1")
    else:
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
        session, unsealed_all=True, wall_source="vendor-drawing-clues", rows_per_page=10
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
    assert counter[0] <= 14
