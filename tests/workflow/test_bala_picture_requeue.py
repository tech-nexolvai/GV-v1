"""A match while the picture job runs still ends with every suggestion pictured (#1246).

Verification for `workflow/stages.py:_queue_bala_pictures` and `workflow/part_pictures.py:
picture_job_unstarted`.

**The race.** The worker runs a picture job inside the dispatcher's transaction, and stamps it done
(`dispatched_at`) only when that transaction commits, after the cut. So while a cut runs, the job
still looks "waiting" to everyone else. A second match on the revision during that window used to
queue nothing, and the running job had listed its pages before the new suggestions existed: they
were left with "No picture stored yet".

**The fix.** After a match, a job is queued whenever a Bala suggestion on the revision has no
picture, unless a job exists that has **not started**: queued, never tried, and not held by a
worker. Cutting twice is harmless, because a suggestion keeps the first picture cut for it.

**Every value here is synthetic**, built with `tests/workflow/test_bala_items.py`'s revision. The
cut is a stand-in that lists the suggestions when it starts, as the real one lists its pages.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.db.session import session_factory
from app.models import OutboxEntry, PartPicture, PartProposal
from tests.workflow.test_bala_items import _Revision
from tests.workflow.test_part_proposals_route import _upgrade
from workflow.bala_items import BALA_SOURCE
from workflow.outbox import dispatch_committed, enqueue
from workflow.part_pictures import (
    CUT_PART_PICTURES_WORKFLOW,
    PartPictureSettings,
    picture_job_unstarted,
    picture_job_waiting,
    record_part_picture,
)
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

PICTURES = PartPictureSettings(margin_pt=Decimal(36), dpi=150)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _bala(session: Session) -> set[UUID]:
    return set(session.scalars(select(PartProposal.id).where(PartProposal.source == BALA_SOURCE)))


def _pictured(session: Session) -> set[UUID]:
    return set(session.scalars(select(PartPicture.part_proposal_id)))


def _jobs(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(OutboxEntry)
            .where(OutboxEntry.workflow == CUT_PART_PICTURES_WORKFLOW)
        )
        or 0
    )


def _cut(worker: Session, proposals: list[PartProposal]) -> None:
    """What the picture job records for each suggestion it listed: one picture each."""
    for proposal in proposals:
        record_part_picture(
            worker,
            proposal=proposal,
            storage_key=f"synthetic/{uuid4()}.png",
            sha256="c" * 64,
            settings=PICTURES,
            shows_gv_marks=False,
        )


def test_a_second_match_while_the_picture_job_runs_still_ends_with_every_suggestion_pictured(
    session: Session, postgres_engine: Engine
) -> None:
    factory = session_factory(postgres_engine)
    built = _Revision(session)
    built.row(built.run(), pieces=2)
    DatabaseStages().match(session, built.revision.id)
    session.commit()
    first = _bala(session)
    assert len(first) == 3 and _jobs(session) == 1

    runs: list[str] = []

    def start(*, workflow: str, payload: Mapping[str, object], idempotency_key: str) -> None:
        assert workflow == CUT_PART_PICTURES_WORKFLOW
        assert payload == {"package_revision_id": str(built.revision.id)}
        with factory() as worker:
            # The job lists what it will cut when it starts ...
            listed = list(
                worker.scalars(
                    select(PartProposal).where(
                        PartProposal.source == BALA_SOURCE,
                        ~PartProposal.id.in_(select(PartPicture.part_proposal_id)),
                    )
                )
            )
            if not runs:
                # ... and a re-read's match lands while it cuts (about 100 s on a big set).
                built.row(built.run(), pieces=3, shift=60)
                result = DatabaseStages().match(session, built.revision.id)
                session.commit()
                bala = result["bala"]
                assert isinstance(bala, dict) and bala["pictures"] == {"queued": True}
            _cut(worker, listed)
            worker.commit()
        runs.append(idempotency_key)

    while dispatch_committed(factory, start):
        pass

    session.expire_all()
    everyone = _bala(session)
    assert len(everyone) == 3 + 4, "the re-read filed a new countertop and three pieces"
    assert everyone <= _pictured(session), "every suggestion ends with its picture"
    assert len(runs) == 2, "the job ran again for the suggestions filed while it ran"
    assert not picture_job_waiting(session, built.revision.id)


def test_a_match_while_the_job_has_not_started_queues_no_second_one(session: Session) -> None:
    """The job that has not started will list the new suggestions itself: one job, not two."""
    built = _Revision(session)
    built.row(built.run(), pieces=2)
    DatabaseStages().match(session, built.revision.id)
    built.row(built.run(), pieces=3, shift=60)
    DatabaseStages().match(session, built.revision.id)

    assert _jobs(session) == 1


def test_a_match_with_every_suggestion_pictured_queues_nothing(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), pieces=1)
    DatabaseStages().match(session, built.revision.id)
    job = session.scalars(select(OutboxEntry)).one()
    job.attempts, job.dispatched_at = 1, utc_now()
    _cut(session, list(session.scalars(select(PartProposal))))

    result = DatabaseStages().match(session, built.revision.id)

    bala = result["bala"]
    assert isinstance(bala, dict)
    assert bala["pictures"] == {
        "queued": False,
        "reason": "every suggestion already has its picture",
    }
    assert _jobs(session) == 1


def test_an_earlier_suggestion_still_without_a_picture_is_queued_again(session: Session) -> None:
    """A job that ran (or gave up) before every picture was cut: the next match asks again."""
    built = _Revision(session)
    built.row(built.run(), pieces=1)
    DatabaseStages().match(session, built.revision.id)
    job = session.scalars(select(OutboxEntry)).one()
    job.abandoned_at = utc_now()
    job.abandoned_reason = "a synthetic refusal"

    DatabaseStages().match(session, built.revision.id)

    assert _jobs(session) == 2


# -- which job has not started ---------------------------------------------------------------------


def _job(session: Session, revision: UUID) -> OutboxEntry:
    enqueue(
        session,
        workflow=CUT_PART_PICTURES_WORKFLOW,
        payload={"package_revision_id": str(revision)},
    )
    session.commit()
    return session.scalars(select(OutboxEntry)).one()


def test_a_queued_job_never_tried_has_not_started(session: Session) -> None:
    revision = uuid4()
    _job(session, revision)
    assert picture_job_unstarted(session, revision)
    assert not picture_job_unstarted(session, uuid4()), "another revision's job is not this one's"


def test_a_job_a_worker_holds_has_started(session: Session, postgres_engine: Engine) -> None:
    """The dispatcher holds the row (`FOR UPDATE`) for as long as the job runs."""
    revision = uuid4()
    job = _job(session, revision)
    with session_factory(postgres_engine)() as dispatcher:
        dispatcher.scalars(
            select(OutboxEntry).where(OutboxEntry.id == job.id).with_for_update()
        ).one()
        assert not picture_job_unstarted(session, revision)
        assert picture_job_waiting(session, revision), "the screen still says it is being made"
        session.rollback()
        dispatcher.rollback()
    assert picture_job_unstarted(session, revision)


@pytest.mark.parametrize("ended", ["tried", "dispatched", "abandoned"])
def test_a_job_tried_done_or_given_up_has_started(session: Session, ended: str) -> None:
    revision = uuid4()
    job = _job(session, revision)
    if ended == "tried":
        job.attempts = 1
    elif ended == "dispatched":
        job.attempts, job.dispatched_at = 1, utc_now()
    else:
        job.abandoned_at = utc_now()
        job.abandoned_reason = "a synthetic refusal"
    session.commit()

    assert not picture_job_unstarted(session, revision)
