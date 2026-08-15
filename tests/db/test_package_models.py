"""Database contract for the package aggregate introduced by issue #192."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import Engine, UniqueConstraint, delete, inspect, select
from sqlalchemy.exc import IntegrityError, StatementError

from app.db.base import Base, Immutable
from app.db.session import session_factory, unit_of_work
from app.models import Package, PackageRevision, PackageState, PackageStateEvent, Project

pytest_plugins = ("tests.app.postgres_fixture",)

PACKAGE_TABLES = ("projects", "packages", "package_revisions", "package_state_events")


@pytest.mark.parametrize("table", PACKAGE_TABLES)
def test_every_package_table_is_registered(table: str) -> None:
    """Input: imported models. Outcome: table registered. Why: Alembic must see it."""

    assert table in Base.metadata.tables


def test_project_scope_is_structural_through_restricting_foreign_keys() -> None:
    """Input: descendant metadata. Outcome: one FK path to project with no cascade delete."""

    packages = Base.metadata.tables["packages"]
    revisions = Base.metadata.tables["package_revisions"]
    events = Base.metadata.tables["package_state_events"]

    assert packages.c.project_id.references(Base.metadata.tables["projects"].c.id)
    assert revisions.c.package_id.references(packages.c.id)
    assert events.c.package_revision_id.references(revisions.c.id)
    for table in (packages, revisions, events):
        assert all(foreign_key.ondelete == "RESTRICT" for foreign_key in table.foreign_keys)


def test_revision_and_event_uniqueness_are_declared_in_metadata() -> None:
    """Input: duplicate logical positions. Outcome: schema uniqueness prevents ambiguity."""

    revision_uniques = {
        tuple(constraint.columns.keys())
        for constraint in Base.metadata.tables["package_revisions"].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    event_uniques = {
        tuple(constraint.columns.keys())
        for constraint in Base.metadata.tables["package_state_events"].constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("package_id", "revision_number") in revision_uniques
    assert ("package_revision_id", "sequence") in event_uniques


def test_state_events_are_marked_for_append_only_database_permissions() -> None:
    """Input: state-event model. Outcome: Immutable marker. Why: C1.12 can revoke writes."""

    assert issubclass(PackageStateEvent, Immutable)


def _create_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def _aggregate() -> tuple[Project, Package, PackageRevision]:
    project = Project(name="GV Test Project")
    package = Package(project_id=project.id, vendor="Vendor metadata only")
    revision = PackageRevision(
        package_id=package.id,
        revision_number=1,
        state=PackageState.CREATED,
    )
    return project, package, revision


def test_revision_supersedes_predecessor_without_updating_it(postgres_engine: Engine) -> None:
    """Input: revision 2 superseding revision 1. Outcome: both rows retain separate identity."""

    _create_schema(postgres_engine)
    factory = session_factory(postgres_engine)
    project, package, first = _aggregate()
    first_id = first.id
    with unit_of_work(factory) as session:
        session.add_all((project, package, first))
    with unit_of_work(factory) as session:
        second = PackageRevision(
            package_id=package.id,
            revision_number=2,
            state=PackageState.CREATED,
            supersedes_id=first_id,
        )
        session.add(second)
        second_id = second.id
    with unit_of_work(factory) as session:
        stored_first = session.get(PackageRevision, first_id)
        stored_second = session.get(PackageRevision, second_id)
        assert stored_first is not None and stored_second is not None
        assert stored_first.supersedes_id is None
        assert stored_second.supersedes_id == stored_first.id


def test_duplicate_event_sequence_is_rejected(postgres_engine: Engine) -> None:
    """Input: two sequence-1 events. Outcome: IntegrityError. Why: audit order is singular."""

    _create_schema(postgres_engine)
    factory = session_factory(postgres_engine)
    project, package, revision = _aggregate()
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.add_all((project, package, revision))
        session.add_all(
            (
                PackageStateEvent(
                    package_revision_id=revision.id,
                    sequence=1,
                    from_state=None,
                    to_state=PackageState.CREATED,
                    actor="system",
                ),
                PackageStateEvent(
                    package_revision_id=revision.id,
                    sequence=1,
                    from_state=PackageState.CREATED,
                    to_state=PackageState.UPLOADING,
                    actor="reviewer@example.test",
                ),
            )
        )
        session.flush()


def test_unknown_state_is_rejected_before_insert(postgres_engine: Engine) -> None:
    """Input: invented state. Outcome: StatementError. Why: lifecycle states are closed."""

    _create_schema(postgres_engine)
    factory = session_factory(postgres_engine)
    project, package, revision = _aggregate()
    revision.state = "AUTOMATICALLY_APPROVED"  # type: ignore[assignment]
    with pytest.raises(StatementError), unit_of_work(factory) as session:
        session.add_all((project, package, revision))
        session.flush()


def test_deleting_project_with_package_is_restricted(postgres_engine: Engine) -> None:
    """Input: delete parent project. Outcome: IntegrityError. Why: history never cascades away."""

    _create_schema(postgres_engine)
    factory = session_factory(postgres_engine)
    project, package, revision = _aggregate()
    project_id: UUID = project.id
    with unit_of_work(factory) as session:
        session.add_all((project, package, revision))
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.execute(delete(Project).where(Project.id == project_id))


def test_state_events_are_read_in_explicit_sequence_order(postgres_engine: Engine) -> None:
    """Input: events inserted out of order. Outcome: query by sequence reconstructs history."""

    _create_schema(postgres_engine)
    factory = session_factory(postgres_engine)
    project, package, revision = _aggregate()
    with unit_of_work(factory) as session:
        session.add_all((project, package, revision))
        session.add_all(
            (
                PackageStateEvent(
                    package_revision_id=revision.id,
                    sequence=2,
                    from_state=PackageState.CREATED,
                    to_state=PackageState.UPLOADING,
                    actor="system",
                ),
                PackageStateEvent(
                    package_revision_id=revision.id,
                    sequence=1,
                    from_state=None,
                    to_state=PackageState.CREATED,
                    actor="system",
                ),
            )
        )
    with unit_of_work(factory) as session:
        events = session.scalars(
            select(PackageStateEvent)
            .where(PackageStateEvent.package_revision_id == revision.id)
            .order_by(PackageStateEvent.sequence)
        ).all()
        assert [event.sequence for event in events] == [1, 2]


def test_mapped_tables_have_named_constraints() -> None:
    """Input: package schema. Outcome: no anonymous constraint before migration ships."""

    for table_name in PACKAGE_TABLES:
        table = Base.metadata.tables[table_name]
        assert inspect(table).name == table_name
        for constraint in table.constraints:
            assert constraint.name is not None
