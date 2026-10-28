"""A finished reading asks for the check run on its own (Set 3 QA), and no page pictures.

Synthetic values only. Before, a new upload stopped at NEEDS_INPUT with nothing queued: no check run
(the list said "Run the checks first"). The page pictures it also queued were for the old review
screens only, which were removed (#1991); since 2026-10-28 the reading no longer asks for them.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OutboxEntry, PackageRevision, PackageState, PackageStateEvent
from storage.local import LocalStore
from tests.evidence.test_sides import _read, session, store  # noqa: F401
from tests.scripts.test_drain_outbox import _NoCandidatesStages, _uploaded_revision
from workflow import after_reading
from workflow.vendor_page_pictures import RENDER_VENDOR_PAGE_PICTURES_WORKFLOW

pytest_plugins = ("tests.app.postgres_fixture",)


def _queued(
    session: Session, workflow: str, revision_id: object  # noqa: F811
) -> list[OutboxEntry]:
    return list(
        session.scalars(
            select(OutboxEntry).where(
                OutboxEntry.workflow == workflow,
                OutboxEntry.dispatched_at.is_(None),
                OutboxEntry.payload["package_revision_id"].astext == str(revision_id),
            )
        )
    )


def _needed(session: Session, revision_id: object) -> str:  # noqa: F811
    reason = session.scalars(
        select(PackageStateEvent.reason)
        .where(
            PackageStateEvent.package_revision_id == revision_id,
            PackageStateEvent.to_state == PackageState.NEEDS_INPUT.value,
        )
        .order_by(PackageStateEvent.sequence.desc())
        .limit(1)
    ).one()
    return str(reason)


def test_a_read_set_queues_its_checks_once_and_no_page_pictures(
    session: Session, store: LocalStore  # noqa: F811
) -> None:
    revision = _read(session, store, kind="shop")
    first = after_reading.queue_after_reading(session, revision.id)
    assert first.checks_queued is True
    (checks,) = _queued(session, after_reading.RUN_CHECKS_WORKFLOW, revision.id)
    assert checks.payload["discriminators"] == {}
    assert _queued(session, RENDER_VENDOR_PAGE_PICTURES_WORKFLOW, revision.id) == []

    # Asked again (a redelivered reading): nothing is queued twice.
    again = after_reading.queue_after_reading(session, revision.id)
    assert again.checks_queued is False
    assert _queued(session, RENDER_VENDOR_PAGE_PICTURES_WORKFLOW, revision.id) == []
    assert len(_queued(session, after_reading.RUN_CHECKS_WORKFLOW, revision.id)) == 1


def test_the_reading_end_queues_the_checks_and_says_so(
    session: Session, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    import scripts.drain_outbox as worker

    revision = _uploaded_revision(session, "reading end queues the checks")
    monkeypatch.setattr(worker, "_stages", lambda **_: _NoCandidatesStages())
    results = worker._extract_package(session, revision.id, str(uuid4()))
    session.commit()

    assert results["after_reading"] == {"checks_queued": True}
    assert len(_queued(session, after_reading.RUN_CHECKS_WORKFLOW, revision.id)) == 1
    state = session.scalar(select(PackageRevision.state).where(PackageRevision.id == revision.id))
    assert state == PackageState.NEEDS_INPUT.value
    assert "checks were started" in _needed(session, revision.id)


def test_pages_waiting_for_the_ai_budget_keep_the_checks_and_say_why(
    session: Session, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    import scripts.drain_outbox as worker
    import workflow.slot_row_scope as scope

    revision = _uploaded_revision(session, "budget pages wait")
    monkeypatch.setattr(worker, "_stages", lambda **_: _NoCandidatesStages())
    monkeypatch.setattr(scope, "budget_stopped_pages", lambda _session, _revision: (3, 7))
    worker._extract_package(session, revision.id, str(uuid4()))
    session.commit()

    assert _queued(session, after_reading.RUN_CHECKS_WORKFLOW, revision.id) == []
    assert "Pages 3, 7 wait for the AI budget" in _needed(session, revision.id)


def test_a_confirmed_setting_the_rulebook_does_not_offer_keeps_the_checks_and_says_which(
    session: Session, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    import workflow.layout_proposals as layouts

    revision = _uploaded_revision(session, "unknown setting")
    monkeypatch.setattr(
        layouts, "confirmed_discriminators", lambda _session, _revision: {"made_up": "x"}
    )
    after = after_reading.queue_after_reading(session, revision.id)
    assert after.checks_queued is False
    assert '"made_up" is set, but no published check uses it' in after.needed
    assert _queued(session, after_reading.RUN_CHECKS_WORKFLOW, revision.id) == []
