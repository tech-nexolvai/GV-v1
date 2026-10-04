"""Alembic wiring agrees with SQLAlchemy metadata before the first business model."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.base import Base
from app.db.session import session_factory
from app.models.verdicts import CheckRun, Finding
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_stages import _publish_rulebook, _revision
from workflow.stages import DatabaseStages

pytest_plugins = ("postgres_fixture",)


def test_initial_migration_and_current_metadata_have_no_difference(
    postgres_engine: Engine,
) -> None:
    """Upgrade an empty database to head; autogenerate must propose no operations."""

    config = alembic_config()
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    config.attributes["database_url"] = database_url

    command.upgrade(config, "head")

    with postgres_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, Base.metadata) == []


def test_defaults_citation_migration_does_not_backfill_or_change_old_runs(
    postgres_engine: Engine,
) -> None:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    with session_factory(postgres_engine)() as session:
        assert isinstance(session, Session)
        revision = _revision(session)
        _publish_rulebook(session)
        DatabaseStages().run_checks(session, revision.id)
        before = tuple(session.scalars(select(CheckRun.id)).all())
        findings_before = tuple(session.scalars(select(Finding.id)).all())
        session.commit()

    command.downgrade(config, "0064_countertop_run_wall_layout")
    command.upgrade(config, "head")
    with session_factory(postgres_engine)() as session:
        assert tuple(session.scalars(select(CheckRun.id)).all()) == before
        assert tuple(session.scalars(select(Finding.id)).all()) == findings_before
        assert all(run.defaults_set_id is None for run in session.scalars(select(CheckRun)))
