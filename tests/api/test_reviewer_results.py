"""Reviewer results preserve outcomes, row identity and explicit sign-off decisions."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CheckRun, Finding, ReviewAction
from app.models.review import ReviewActionKind
from app.review.approval import approval_readiness, approve_package
from app.review.session import open_session, record_action
from tests.api.test_review_api import _finding, _revision, session
from tests.review.test_approval import reviewer

pytest_plugins = ("tests.app.postgres_fixture",)
__all__ = ["session"]


def result(db: Session, outcome: str) -> Finding:
    revision = _revision(db, uuid4())
    first = _finding(db, revision)
    parent = db.get(CheckRun, first.check_run_id)
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=parent.rule_snapshot_id,
        engine_version="synthetic",
    )
    db.add(run)
    db.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=outcome,
        severity="FLAG",
        trace={},
        parameter_set_versions={},
    )
    db.add(finding)
    db.flush()
    return finding


@pytest.mark.parametrize("outcome", ["FAIL", "REVIEW_REQUIRED", "NOT_FOUND"])
def test_each_unaddressed_outcome_is_signed_as_not_confirmed(
    session: Session, outcome: str
) -> None:
    """Every question is optional (owner decision 2026-10-16): an unanswered result no longer
    refuses sign-off. It is signed with no answer recorded for it, never as answered."""
    finding = result(session, outcome)
    review = open_session(
        session, package_revision_id=finding.package_revision_id, reviewer="reviewer"
    )
    assert (
        finding.id in approval_readiness(session, finding.package_revision_id).blocking_finding_ids
    )
    decision = approve_package(session, principal=reviewer(), review_session_id=review.id)
    assert finding.id in decision.finding_ids
    assert not session.scalars(
        select(ReviewAction).where(ReviewAction.finding_id == finding.id)
    ).all()


@pytest.mark.parametrize("outcome", ["REVIEW_REQUIRED", "NOT_FOUND"])
@pytest.mark.parametrize("action", [ReviewActionKind.CONFIRM, ReviewActionKind.DISMISS])
def test_review_note_preserves_the_recorded_outcome(
    session: Session, outcome: str, action: ReviewActionKind
) -> None:
    finding = result(session, outcome)
    review = open_session(
        session, package_revision_id=finding.package_revision_id, reviewer="reviewer"
    )
    saved = record_action(
        session,
        review_session_id=review.id,
        finding_id=finding.id,
        action=action,
        actor="reviewer",
        note="Checked the supporting detail",
    )
    session.refresh(finding)
    assert finding.outcome == outcome
    assert saved.note == "Checked the supporting detail"
    assert saved.actor == "reviewer"
