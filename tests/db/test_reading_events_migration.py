"""Migration 0096: the reading's live activity table and the `try_again` stage (#1238).

Checked against what the *migrations* install, not what `create_all` builds from the models.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

REVISION = "0096_reading_events"
BEFORE = "0094_invocation_failure_reason"


def _config(engine: Engine):  # type: ignore[no-untyped-def]
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    return config


def test_the_revision_follows_main_and_is_on_the_one_line_of_history() -> None:
    assert len(REVISION) <= 32
    script = ScriptDirectory.from_config(alembic_config())
    found = script.get_revision(REVISION)
    assert found is not None and found.down_revision == BEFORE
    assert REVISION in {
        rev.revision for rev in script.walk_revisions("base", script.get_current_head() or "")
    }


def test_the_table_has_no_foreign_key_and_the_progress_row_may_say_try_again(
    postgres_engine: Engine,
) -> None:
    command.upgrade(_config(postgres_engine), "head")
    columns = {c["name"] for c in inspect(postgres_engine).get_columns("reading_events")}
    assert {
        "package_revision_id",
        "reading_id",
        "position",
        "page_number",
        "agent",
        "item",
        "kind",
        "code",
        "change",
    } <= columns
    assert inspect(postgres_engine).get_foreign_keys("reading_events") == []
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reading_progress (id, created_at, package_revision_id, stage, "
                "updated_at) VALUES (:id, now(), :revision, 'try_again', now())"
            ),
            {"id": uuid4(), "revision": uuid4()},
        )


def test_the_downgrade_narrows_the_stage_and_drops_the_events_then_upgrades_again(
    postgres_engine: Engine,
) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reading_progress (id, created_at, package_revision_id, stage, "
                "updated_at) VALUES (:id, now(), :revision, 'try_again', now())"
            ),
            {"id": uuid4(), "revision": uuid4()},
        )
        connection.execute(
            text(
                "INSERT INTO reading_events (id, created_at, package_revision_id, reading_id, "
                "position, page_number, agent, item, kind, code) VALUES (:id, now(), :revision, "
                ":reading, 0, 1, 'Rekha', 'p0:row-choice', 'stopped', 'readers-differ')"
            ),
            {"id": uuid4(), "revision": uuid4(), "reading": uuid4()},
        )
    command.downgrade(config, BEFORE)
    try:
        assert "reading_events" not in inspect(postgres_engine).get_table_names()
        with pytest.raises(IntegrityError), postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO reading_progress (id, created_at, package_revision_id, stage, "
                    "updated_at) VALUES (:id, now(), :revision, 'try_again', now())"
                ),
                {"id": uuid4(), "revision": uuid4()},
            )
    finally:
        command.upgrade(config, "head")
    assert "reading_events" in inspect(postgres_engine).get_table_names()
