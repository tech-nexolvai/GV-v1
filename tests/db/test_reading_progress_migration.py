"""Migration 0087: the reading-progress table (#1178)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.models.runs import READING_STAGES
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

REVISION = "0087_reading_progress"
BEFORE = "0086_project_sinks"


def _config(engine: Engine):  # type: ignore[no-untyped-def]
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    return config


def test_the_revision_follows_the_file_names_and_is_the_head() -> None:
    """`alembic_version.version_num` is 32 characters; a longer id fails CI at upgrade time."""
    assert len(REVISION) <= 32
    script = ScriptDirectory.from_config(alembic_config())
    found = script.get_revision(REVISION)
    assert found is not None and found.down_revision == BEFORE
    assert script.get_current_head() == REVISION


def test_the_table_holds_one_known_stage_per_revision_and_honest_page_counts(
    postgres_engine: Engine,
) -> None:
    command.upgrade(_config(postgres_engine), "head")
    columns = {c["name"] for c in inspect(postgres_engine).get_columns("reading_progress")}
    assert {"package_revision_id", "stage", "pages_done", "pages_total", "updated_at"} <= columns
    # No foreign key on purpose: a running stage holds its revision row locked (see the model).
    assert inspect(postgres_engine).get_foreign_keys("reading_progress") == []

    insert = text(
        "INSERT INTO reading_progress (id, created_at, package_revision_id, stage, pages_done, "
        "pages_total, updated_at) VALUES (:id, now(), :revision, :stage, :done, :total, now())"
    )
    revision = uuid4()
    with postgres_engine.begin() as connection:
        connection.execute(
            insert,
            {"id": uuid4(), "revision": revision, "stage": "pages", "done": 2, "total": 5},
        )
    for stage, done, total in (
        ("guessing", None, None),  # not a stage
        ("pages", 6, 5),  # more done than there are
        ("pages", 1, None),  # done without a total
        ("pages", -1, 5),
    ):
        with pytest.raises(IntegrityError), postgres_engine.begin() as connection:
            connection.execute(
                insert,
                {"id": uuid4(), "revision": uuid4(), "stage": stage, "done": done, "total": total},
            )
    with pytest.raises(IntegrityError), postgres_engine.begin() as connection:
        connection.execute(
            insert,
            {"id": uuid4(), "revision": revision, "stage": "match", "done": None, "total": None},
        )


def test_the_migration_lists_exactly_the_models_stages_and_downgrades(
    postgres_engine: Engine,
) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    with postgres_engine.connect() as connection:
        check = connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'reading_progress'::regclass AND conname LIKE '%reading_progress_stage'"
            )
        ).scalar_one()
    assert all(f"'{stage}'" in check for stage in READING_STAGES)
    command.downgrade(config, BEFORE)
    try:
        assert "reading_progress" not in inspect(postgres_engine).get_table_names()
    finally:
        command.upgrade(config, "head")
