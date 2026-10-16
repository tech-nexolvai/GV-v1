"""What a finished reading asks for next, on its own (Set 3 QA).

A new upload used to stop at `NEEDS_INPUT` after reading with nothing else queued: no page pictures
(only the old Measure screen asked for them, so "Look" showed nothing on a new set), and no check
run (the review list then said "Run the checks first" until someone asked). Now the reading's last
stage queues both, in its own transaction, so they land with the hand-over or not at all:

- **Page pictures:** the worker's free render of every page still without a picture, once (never
  a second job while one waits), exactly what `POST .../pages/pictures` queues.
- **The check run:** queued when nothing a run needs is missing, with the discriminators a person
  confirmed (as `POST .../checks` sends them; code's clues never settle the walls alone, #1202).
  It is the same rule the screen used: a run is not asked for while one already waits, or while
  pages wait for the AI budget (the screen asks to read those first), or when a confirmed setting
  is one the rulebook does not offer (`POST .../checks` refuses that with 422). In those cases the
  package stays in `NEEDS_INPUT`, and `needed` says what is missing in plain words.

Nothing here decides anything: the checks are code's, and every reading a person must confirm is
still theirs to confirm (a held number stays held; its answer re-runs the checks).

Source: the Set 3 QA. Verification: `tests/workflow/test_after_reading.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OutboxEntry, Page
from app.models.document import PackageRevisionDocument
from workflow.outbox import enqueue
from workflow.vendor_page_pictures import (
    RENDER_VENDOR_PAGE_PICTURES_WORKFLOW,
    pages_without_pictures,
)

__all__ = ["RUN_CHECKS_WORKFLOW", "AfterReading", "queue_after_reading"]

#: The outbox name `app/api/measurements.py:RUN_CHECKS_WORKFLOW` enqueues.
RUN_CHECKS_WORKFLOW: Final = "run_checks"

#: What the reviewer is told when the checks were queued: nothing is missing to run them.
CHECKS_QUEUED_NEED: Final = (
    "The checks were started when the reading finished. Confirm the readings the checks hold "
    "back; each answer runs the checks again."
)


@dataclass(frozen=True, slots=True)
class AfterReading:
    """What the reading's end queued, and in plain words what still stops the checks."""

    pictures_queued: bool
    checks_queued: bool
    needed: str


def _waiting(session: Session, workflow: str, revision_id: UUID) -> bool:
    return (
        session.scalar(
            select(OutboxEntry.id)
            .where(
                OutboxEntry.workflow == workflow,
                OutboxEntry.dispatched_at.is_(None),
                OutboxEntry.abandoned_at.is_(None),
                OutboxEntry.payload["package_revision_id"].astext == str(revision_id),
            )
            .limit(1)
        )
        is not None
    )


def _queue_pictures(session: Session, revision_id: UUID) -> bool:
    page_ids = list(
        session.scalars(
            select(Page.id)
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.document_version_id == Page.document_version_id,
            )
            .where(PackageRevisionDocument.package_revision_id == revision_id)
        )
    )
    if not pages_without_pictures(session, page_ids):
        return False
    if _waiting(session, RENDER_VENDOR_PAGE_PICTURES_WORKFLOW, revision_id):
        return False
    enqueue(
        session,
        workflow=RENDER_VENDOR_PAGE_PICTURES_WORKFLOW,
        payload={"package_revision_id": str(revision_id)},
    )
    return True


def _refused_settings(session: Session, confirmed: dict[str, str]) -> list[str]:
    """The confirmed settings the published rulebook does not offer, in plain words."""
    if not confirmed:
        return []
    from app.verdicts.rulebook import snapshot_store
    from rules.required_inputs import required_inputs

    store = snapshot_store(session)
    rules = [
        snapshot.rule
        for snapshot in (store.latest(rule_id) for rule_id in store.rule_ids())
        if snapshot is not None
    ]
    declared = {
        discriminator.name: tuple(discriminator.choices)
        for discriminator in required_inputs(rules).discriminators
    }
    return [
        (
            f'"{name}" is set to "{value}", which the rulebook does not offer'
            if name in declared
            else f'"{name}" is set, but no published check uses it'
        )
        for name, value in sorted(confirmed.items())
        if value not in declared.get(name, ())
    ]


def queue_after_reading(session: Session, revision_id: UUID) -> AfterReading:
    """Queue the page pictures and, when nothing it needs is missing, the check run. Flushes only:
    the caller's commit lands these with the reading's hand-over."""
    from workflow.layout_proposals import DISCRIMINATOR_FROM_REVIEWER, confirmed_discriminators
    from workflow.slot_row_scope import budget_stopped_pages

    pictures = _queue_pictures(session, revision_id)
    waiting_pages = budget_stopped_pages(session, revision_id)
    if waiting_pages:
        listed = ", ".join(str(page) for page in waiting_pages)
        noun = "Page" if len(waiting_pages) == 1 else "Pages"
        verb = "waits" if len(waiting_pages) == 1 else "wait"
        return AfterReading(
            pictures,
            False,
            f"{noun} {listed} {verb} for the AI budget. Read them (or answer them) first; then run "
            "the checks.",
        )
    confirmed = dict(confirmed_discriminators(session, revision_id))
    refused = _refused_settings(session, confirmed)
    if refused:
        return AfterReading(
            pictures,
            False,
            "The checks did not start: " + "; ".join(refused) + ". Change it, then run the checks.",
        )
    if _waiting(session, RUN_CHECKS_WORKFLOW, revision_id):
        return AfterReading(pictures, False, CHECKS_QUEUED_NEED)
    enqueue(
        session,
        workflow=RUN_CHECKS_WORKFLOW,
        payload={
            "package_revision_id": str(revision_id),
            "discriminators": confirmed,
            "discriminator_sources": {name: DISCRIMINATOR_FROM_REVIEWER for name in confirmed},
        },
    )
    return AfterReading(pictures, True, CHECKS_QUEUED_NEED)
