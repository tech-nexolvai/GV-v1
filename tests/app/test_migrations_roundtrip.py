"""Alembic wiring agrees with SQLAlchemy metadata before the first business model."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine

from alembic import command
from app.db.base import Base
from tests.app.postgres_fixture import alembic_config

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
