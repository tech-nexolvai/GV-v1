"""Sign-off and a save at the same moment, on two real connections (#1182).

A guarded write holds a shared lock on the revision (`app/api/signed_off.py`). Sign-off now locks the
revision for update before it reads anything, and before the review session. Two scenarios:

1. A save that started first records a new input. Sign-off waits for it, then sees it, and refuses
   ("run the checks again") instead of signing off over an input no finding used.
2. A decision recorded at the same moment (revision, then session) and a sign-off (now also revision,
   then session) queue up. Before the fix sign-off locked the session first, and Postgres aborted one
   of the two as a deadlock.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.signed_off import ensure_revision_writable
from app.db.session import session_factory
from app.models.package import PackageRevision, PackageState
from app.models.review import ReviewActionKind
from app.review.approval import INPUTS_CHANGED_NEEDS_RERUN, approve_package
from app.review.session import open_session, record_action
from tests.review.test_approval import reviewer
from tests.review.test_session import _finding, _revision, _upgrade
from workflow.layout_proposals import record_layout_confirmation

pytest_plugins = ("tests.app.postgres_fixture",)

#: Long enough for the sign-off thread to reach its lock and wait on it.
SETTLE_SECONDS = 1.0


def _ready_to_sign(factory: sessionmaker[Session]) -> tuple[UUID, UUID, UUID]:
    """A checked revision whose one finding is decided, and an open review session."""
    with factory.begin() as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        review = open_session(db, package_revision_id=revision.id, reviewer="anant")
        record_action(
            db,
            review_session_id=review.id,
            finding_id=finding.id,
            action=ReviewActionKind.CONFIRM,
            actor="anant",
        )
        return revision.id, review.id, finding.id


def _sign_off_in_background(
    factory: sessionmaker[Session], review_id: UUID
) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            with factory.begin() as db:
                approve_package(db, principal=reviewer(), review_session_id=review_id)
            outcome["approved"] = True
        except Exception as error:  # noqa: BLE001 - the test asserts on it
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(SETTLE_SECONDS)
    return thread, outcome


def _while_a_save_holds_the_revision(
    factory: sessionmaker[Session],
    revision_id: UUID,
    write: Callable[[Session], None],
    review: UUID,
) -> dict[str, Any]:
    with factory() as save:
        ensure_revision_writable(save, revision_id)  # what every guarded route does first
        thread, outcome = _sign_off_in_background(factory, review)
        assert thread.is_alive(), "sign-off must wait for the save's lock"
        write(save)
        save.commit()
    thread.join(timeout=30)
    assert not thread.is_alive()
    return outcome


def test_a_save_that_started_first_is_seen_and_blocks_the_sign_off(
    postgres_engine: Engine,
) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    revision_id, review_id, _ = _ready_to_sign(factory)

    def new_input(db: Session) -> None:
        record_layout_confirmation(
            db,
            package_revision_id=revision_id,
            discriminator_name="wall_config",
            value="island",
            actor="anant",
        )

    outcome = _while_a_save_holds_the_revision(factory, revision_id, new_input, review_id)

    assert "approved" not in outcome
    assert INPUTS_CHANGED_NEEDS_RERUN in str(outcome["error"])
    with factory() as db:
        state = db.scalar(select(PackageRevision.state).where(PackageRevision.id == revision_id))
    assert state == PackageState.AWAITING_REVIEW.value


def test_a_decision_and_a_sign_off_at_the_same_moment_queue_instead_of_deadlocking(
    postgres_engine: Engine,
) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    revision_id, review_id, finding_id = _ready_to_sign(factory)

    def decision(db: Session) -> None:
        record_action(
            db,
            review_session_id=review_id,
            finding_id=finding_id,
            action=ReviewActionKind.CONFIRM,
            actor="anant",
            note="checked again",
        )

    outcome = _while_a_save_holds_the_revision(factory, revision_id, decision, review_id)

    assert "deadlock" not in str(outcome.get("error", "")).lower()
    assert outcome == {"approved": True}, outcome
