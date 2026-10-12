"""Migration 0088: the outbox records a row it gave up on, and why (#1182)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

REVISION = "0088_outbox_abandoned"
BEFORE = "0087_reading_progress"


def _config(engine: Engine):  # type: ignore[no-untyped-def]
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    return config


def _insert(engine: Engine, *, dispatched: bool, abandoned: bool, reason: str | None) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outbox_entries "
                "(id, created_at, workflow, payload, dispatched_at, attempts, abandoned_at, "
                "abandoned_reason) VALUES (:id, now(), 'run_checks', '{}'::jsonb, "
                + ("now()" if dispatched else "NULL")
                + ", 1, "
                + ("now()" if abandoned else "NULL")
                + ", :reason)"
            ),
            {"id": uuid4(), "reason": reason},
        )


def test_the_revision_follows_the_file_names_and_is_the_head() -> None:
    """`alembic_version.version_num` is 32 characters; a longer id fails CI at upgrade time."""
    assert len(REVISION) <= 32
    script = ScriptDirectory.from_config(alembic_config())
    found = script.get_revision(REVISION)
    assert found is not None and found.down_revision == BEFORE
    # On the one line of history to the single head; later migrations may follow it.
    assert REVISION in {
        rev.revision for rev in script.walk_revisions("base", script.get_current_head())
    }


def test_the_columns_are_nullable_and_set_together(postgres_engine: Engine) -> None:
    command.upgrade(_config(postgres_engine), "head")
    columns = {c["name"]: c for c in inspect(postgres_engine).get_columns("outbox_entries")}
    assert columns["abandoned_at"]["nullable"] and columns["abandoned_reason"]["nullable"]
    assert not columns["refused_attempts"]["nullable"]

    _insert(postgres_engine, dispatched=False, abandoned=True, reason="given up: refused")
    for dispatched, abandoned, reason in (
        (False, True, None),  # given up without a reason
        (False, False, "a reason with nothing given up"),
        (False, True, ""),  # an empty reason
        (True, True, "both dispatched and given up"),
    ):
        with pytest.raises(IntegrityError):
            _insert(postgres_engine, dispatched=dispatched, abandoned=abandoned, reason=reason)


def test_down_and_up_again_with_nothing_given_up(postgres_engine: Engine) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    command.downgrade(config, BEFORE)
    assert "abandoned_at" not in {
        c["name"] for c in inspect(postgres_engine).get_columns("outbox_entries")
    }
    command.upgrade(config, "head")


def test_a_downgrade_with_a_row_given_up_refuses(postgres_engine: Engine) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    _insert(postgres_engine, dispatched=False, abandoned=True, reason="given up: refused")

    with pytest.raises(RuntimeError, match="record why the dispatcher gave up"):
        command.downgrade(config, BEFORE)
