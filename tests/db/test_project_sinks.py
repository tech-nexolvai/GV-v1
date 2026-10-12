"""Migration 0086: the project's sink entries, up and down (#1181).

Verification for `alembic/versions/0086_project_sinks.py` and `app.models.project_sinks.ProjectSink`:
the table is the head's, rows are append-only, a blank name is refused, and a downgrade with an
entry stored refuses. Every name and number is synthetic.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError, InternalError

from alembic import command
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

REVISION = "0086_project_sinks"
BEFORE = "0085_document_version_file_name"


def _config(engine: Engine) -> Config:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    return config


def _seed(engine: Engine) -> tuple[str, str]:
    """A project and one project settings version, for a sink row to point at."""
    project, parameter_set = str(uuid4()), str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO projects (id, created_at, name) VALUES (:id, now(), 'sink test')"),
            {"id": project},
        )
        connection.execute(
            text(
                "INSERT INTO parameter_sets (id, created_at, set_id, project_id, layer, version) "
                "VALUES (:id, now(), :hash, :project, 'project', 1)"
            ),
            {"id": parameter_set, "hash": "sha256:" + "a" * 64, "project": project},
        )
    return project, parameter_set


def _insert(engine: Engine, project: str, parameter_set: str, name: str) -> str:
    row = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO project_sinks (id, created_at, project_id, model_name, source_note, "
                "parameter_set_id, set_by) VALUES (:id, now(), :project, :name, NULL, :set, 'a')"
            ),
            {"id": row, "project": project, "name": name, "set": parameter_set},
        )
    return row


def test_0086_follows_0085_and_fits_the_version_column() -> None:
    assert len(REVISION) <= 32
    script = ScriptDirectory.from_config(alembic_config())
    found = script.get_revision(REVISION)
    assert found is not None and found.down_revision == BEFORE
    assert REVISION in {
        rev.revision for rev in script.walk_revisions("base", script.get_current_head())
    }


def test_a_sink_row_is_append_only_and_needs_a_name(postgres_engine: Engine) -> None:
    command.upgrade(_config(postgres_engine), "head")
    project, parameter_set = _seed(postgres_engine)
    row = _insert(postgres_engine, project, parameter_set, "Synthetic Sink")

    with pytest.raises(IntegrityError):
        _insert(postgres_engine, project, parameter_set, "   ")
    with pytest.raises((InternalError, IntegrityError)), postgres_engine.begin() as connection:
        connection.execute(
            text("UPDATE project_sinks SET model_name = 'Other' WHERE id = :id"), {"id": row}
        )


def test_the_migration_goes_down_empty_and_refuses_with_an_entry(postgres_engine: Engine) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    command.downgrade(config, BEFORE)
    assert "project_sinks" not in inspect(postgres_engine).get_table_names()
    command.upgrade(config, "head")
    assert "project_sinks" in inspect(postgres_engine).get_table_names()

    project, parameter_set = _seed(postgres_engine)
    _insert(postgres_engine, project, parameter_set, "Synthetic Sink")
    with pytest.raises(RuntimeError, match="Preserve these records"):
        command.downgrade(config, BEFORE)
