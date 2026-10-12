"""`start_revision`: open revision N+1 for an upload, and close revision N (#1180).

The lifecycle half of `POST .../revisions`. Unlike `supersede`, it needs no new versions up front
(the upload has not happened yet) and enqueues nothing: reading starts only when the reviewer asks.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.lifecycle.events import history
from app.lifecycle.states import transition
from app.lifecycle.supersede import NothingToSupersede, start_revision
from app.models import OutboxEntry, PackageRevision, PackageState
from tests.lifecycle.test_supersede import ACTOR, _assembled_package, _composition
from tests.lifecycle.test_supersede import factory as factory_fixture

pytest_plugins = ("tests.app.postgres_fixture",)

factory = factory_fixture


def _read(session: Session, revision: PackageRevision) -> None:
    for state in (PackageState.UPLOADING, PackageState.UPLOADED, PackageState.INGESTING):
        transition(session, revision.id, state, actor=ACTOR)


def test_it_opens_the_next_revision_holding_the_same_drawings(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        package, one, composition = _assembled_package(session)
        _read(session, one)
        outbox_before = session.scalar(select(func.count()).select_from(OutboxEntry))

        two = start_revision(session, package_id=package.id, actor=ACTOR, reason="re-issue")
        session.commit()

        assert two.revision_number == 2 and two.supersedes_id == one.id
        assert two.state == PackageState.UPLOADING
        session.refresh(one)
        assert one.state == PackageState.SUPERSEDED
        # The same versions, no new bytes; revision 1's own set untouched.
        assert _composition(session, two.id) == composition
        assert _composition(session, one.id) == composition
        # Nothing is queued: reading starts when the reviewer asks for it.
        assert session.scalar(select(func.count()).select_from(OutboxEntry)) == outbox_before
        assert [event.to_state for event in history(session, two.id)] == [
            PackageState.CREATED,
            PackageState.UPLOADING,
        ]


def test_a_package_that_does_not_exist_has_nothing_to_supersede(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session, pytest.raises(NothingToSupersede):
        start_revision(session, package_id=uuid4(), actor=ACTOR)


def test_each_start_numbers_the_next_revision(factory: sessionmaker[Session]) -> None:
    """The lifecycle numbers revisions one after another. Whether a second start should open
    revision 3 or resume revision 2 is the API's decision (`tests/api/test_revisions.py`)."""
    with factory() as session:
        package, one, _composition_one = _assembled_package(session)
        _read(session, one)
        start_revision(session, package_id=package.id, actor=ACTOR)
        three = start_revision(session, package_id=package.id, actor=ACTOR)
        session.commit()

        assert three.revision_number == 3
        numbers = session.scalars(
            select(PackageRevision.revision_number)
            .where(PackageRevision.package_id == package.id)
            .order_by(PackageRevision.revision_number)
        ).all()
        assert numbers == [1, 2, 3]
