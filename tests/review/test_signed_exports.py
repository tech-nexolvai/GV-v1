"""Approval-bound publication refuses old files and changing inputs."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select

from app.db.session import session_factory
from app.models.outbox import OutboxEntry
from app.models.review import ReviewActionKind
from app.models.signed_exports import ApprovalExportAction, ApprovalExportSnapshot
from app.review.approval import approve_package
from app.review.session import open_session, record_action
from app.review.signed_exports import (
    SignedExportRefused,
    digest,
    load_snapshot,
    request_signed_exports,
)
from tests.review.test_approval import reviewer
from tests.review.test_session import _finding, _revision, _upgrade


def test_approval_freezes_actions_and_enqueues_once(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    with session_factory(postgres_engine).begin() as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        review = open_session(db, package_revision_id=revision.id, reviewer="reviewer")
        action = record_action(
            db,
            review_session_id=review.id,
            finding_id=finding.id,
            action=ReviewActionKind.CONFIRM,
            actor="reviewer",
            note="Checked source",
        )
        decision = approve_package(db, principal=reviewer("reviewer"), review_session_id=review.id)
        snapshot = db.scalar(
            select(ApprovalExportSnapshot).where(
                ApprovalExportSnapshot.approval_id == decision.approval.id
            )
        )
        assert snapshot is not None
        linked = db.scalars(
            select(ApprovalExportAction.review_action_id).where(
                ApprovalExportAction.snapshot_id == snapshot.id
            )
        ).all()
        assert linked == [action.id]
        frozen = load_snapshot(db, snapshot)
        assert frozen.review.findings[0].outcome == finding.outcome == "FAIL"
        assert frozen.review.findings[0].actions[0].action_id == action.id
        request_signed_exports(db, decision.approval.id)
        jobs = db.scalars(
            select(OutboxEntry).where(OutboxEntry.workflow == "generate_signed_exports")
        ).all()
        assert len(jobs) == 1
        assert jobs[0].payload["approval_id"] == str(decision.approval.id)


def test_snapshot_hash_is_verified(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    with session_factory(postgres_engine).begin() as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        review = open_session(db, package_revision_id=revision.id, reviewer="reviewer")
        record_action(
            db,
            review_session_id=review.id,
            finding_id=finding.id,
            action=ReviewActionKind.CONFIRM,
            actor="reviewer",
            note="Checked source",
        )
        approval = approve_package(
            db, principal=reviewer("reviewer"), review_session_id=review.id
        ).approval
        snapshot = db.scalar(
            select(ApprovalExportSnapshot).where(ApprovalExportSnapshot.approval_id == approval.id)
        )
        assert snapshot is not None
        db.expunge(snapshot)
        data = json.loads(snapshot.canonical_json)
        data["review"]["findings"][0]["outcome"] = "PASS"
        snapshot.canonical_json = json.dumps(data)
        with pytest.raises(SignedExportRefused, match="hash"):
            load_snapshot(db, snapshot)


@pytest.mark.parametrize(
    "field,value", [("outcome", "PASS"), ("rule_id", "OTHER"), ("scope_label", "other part")]
)
def test_resealed_review_cannot_rejudge_or_relabel_a_finding(
    postgres_engine: Engine, field: str, value: str
) -> None:
    _upgrade(postgres_engine)
    with session_factory(postgres_engine).begin() as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        review = open_session(db, package_revision_id=revision.id, reviewer="reviewer")
        record_action(
            db,
            review_session_id=review.id,
            finding_id=finding.id,
            action=ReviewActionKind.CONFIRM,
            actor="reviewer",
            note="Checked source",
        )
        approval = approve_package(
            db, principal=reviewer("reviewer"), review_session_id=review.id
        ).approval
        snapshot = request_signed_exports(db, approval.id)
        db.expunge(snapshot)
        data = json.loads(snapshot.canonical_json)
        data["review"]["findings"][0][field] = value
        snapshot.canonical_json = json.dumps(data)
        snapshot.sha256 = digest(snapshot.canonical_json)
        with pytest.raises(SignedExportRefused, match="finding"):
            load_snapshot(db, snapshot)


@pytest.mark.parametrize(
    "field,value", [("reviewer", "somebody else"), ("action", "dismiss"), ("note", "invented note")]
)
def test_resealed_action_must_match_its_stored_row(
    postgres_engine: Engine, field: str, value: str
) -> None:
    _upgrade(postgres_engine)
    with session_factory(postgres_engine).begin() as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        review = open_session(db, package_revision_id=revision.id, reviewer="reviewer")
        record_action(
            db,
            review_session_id=review.id,
            finding_id=finding.id,
            action=ReviewActionKind.CONFIRM,
            actor="reviewer",
            note="Checked source",
        )
        approval = approve_package(
            db, principal=reviewer("reviewer"), review_session_id=review.id
        ).approval
        snapshot = request_signed_exports(db, approval.id)
        db.expunge(snapshot)
        data = json.loads(snapshot.canonical_json)
        data["review"]["findings"][0]["actions"][0][field] = value
        snapshot.canonical_json = json.dumps(data)
        snapshot.sha256 = digest(snapshot.canonical_json)
        with pytest.raises(SignedExportRefused, match="action"):
            load_snapshot(db, snapshot)


@pytest.mark.parametrize("change", ["finding_set", "revision", "approval", "facts"])
def test_snapshot_binding_refuses_substitution(postgres_engine: Engine, change: str) -> None:
    _upgrade(postgres_engine)
    with session_factory(postgres_engine).begin() as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        review = open_session(db, package_revision_id=revision.id, reviewer="reviewer")
        record_action(
            db,
            review_session_id=review.id,
            finding_id=finding.id,
            action=ReviewActionKind.CONFIRM,
            actor="reviewer",
            note="Checked source",
        )
        approval = approve_package(
            db, principal=reviewer("reviewer"), review_session_id=review.id
        ).approval
        snapshot = request_signed_exports(db, approval.id)
        db.expunge(snapshot)
        data = json.loads(snapshot.canonical_json)
        if change == "finding_set":
            data["review"]["findings"][0]["finding_id"] = str(uuid4())
        elif change == "revision":
            data["package_revision_id"] = str(uuid4())
        elif change == "approval":
            data["review"]["approval_id"] = str(uuid4())
        else:
            data["facts"][0]["finding"]["outcome"] = "PASS"
        snapshot.canonical_json = json.dumps(data)
        snapshot.sha256 = digest(snapshot.canonical_json)
        with pytest.raises(SignedExportRefused):
            load_snapshot(db, snapshot)


def test_downgrade_empty_and_refusal_preserve_old_rows(postgres_engine: Engine) -> None:
    from alembic import command
    from app.models.review import Approval, ApprovedFinding
    from tests.app.postgres_fixture import alembic_config

    _upgrade(postgres_engine)
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    with session_factory(postgres_engine).begin() as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        approval = Approval(package_revision_id=revision.id, approved_by="reviewer")
        db.add(approval)
        db.flush()
        approval_id = approval.id
        db.add(
            ApprovedFinding(
                approval_id=approval.id, package_revision_id=revision.id, finding_id=finding.id
            )
        )
    try:
        command.downgrade(config, "0068_page_measurements")
        command.upgrade(config, "head")
        with session_factory(postgres_engine).begin() as db:
            assert db.get(Approval, approval_id).approved_by == "reviewer"
            snapshot_id = request_signed_exports(db, approval_id).id
        with pytest.raises(RuntimeError, match="must not be deleted"):
            command.downgrade(config, "0068_page_measurements")
        with session_factory(postgres_engine).begin() as db:
            assert db.get(Approval, approval_id).approved_by == "reviewer"
            assert db.get(ApprovalExportSnapshot, snapshot_id) is not None
    finally:
        command.upgrade(config, "head")


def test_review_action_waits_for_the_approval_session_lock(postgres_engine: Engine) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.models.review import ReviewSession

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with factory.begin() as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        review = open_session(db, package_revision_id=revision.id, reviewer="reviewer")
        review_id, finding_id = review.id, finding.id
    with factory.begin() as locking:
        # NO KEY UPDATE permits the INSERT's FK key-share lock. This tests the explicit
        # review-session lock rather than an incidental foreign-key wait.
        locking.scalar(
            select(ReviewSession)
            .where(ReviewSession.id == review_id)
            .with_for_update(key_share=True)
        )
        with pytest.raises(DBAPIError, match="lock timeout"), factory.begin() as concurrent:
            concurrent.execute(text("SET LOCAL lock_timeout = '100ms'"))
            record_action(
                concurrent,
                review_session_id=review_id,
                finding_id=finding_id,
                action=ReviewActionKind.CONFIRM,
                actor="reviewer",
            )
