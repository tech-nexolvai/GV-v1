"""The reviewer's Agree can say what the value should be (#1803).

Verification for: `alembic/versions/0112_finding_decision_should_be.py` and
`AssistantFindingDecision.should_be`, against what the migrations install: a nullable text column
with a CHECK that keeps it to an Agree and never empty, and a clean way down and up again.
"""

from __future__ import annotations

from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text

from alembic import command
from app.models import AssistantFindingDecision
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

REVISION = "0112_finding_decision_should_be"
BEFORE = "0111_package_product_registry"
TABLE = "assistant_finding_decisions"


def _config(engine: Engine):  # type: ignore[no-untyped-def]
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    return config


def test_the_revision_follows_0111_and_fits_the_version_column() -> None:
    assert len(REVISION) <= 32
    found = ScriptDirectory.from_config(alembic_config()).get_revision(REVISION)
    assert found is not None and found.down_revision == BEFORE


def test_the_model_has_the_column() -> None:
    column = AssistantFindingDecision.__table__.c["should_be"]
    assert column.nullable


def _check(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return connection.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = "
                "CAST(:table AS regclass) AND contype = 'c' "
                "AND pg_get_constraintdef(oid) LIKE '%should_be%'"
            ),
            {"table": TABLE},
        )


def test_the_migration_adds_a_nullable_column_kept_to_an_agree_and_goes_down_and_up(
    postgres_engine: Engine,
) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    columns = {c["name"]: c for c in inspect(postgres_engine).get_columns(TABLE)}
    assert columns["should_be"]["nullable"]
    check = _check(postgres_engine) or ""
    assert "should_be IS NULL" in check and "'agree'" in check

    with postgres_engine.connect() as connection:
        values = connection.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = "
                "CAST(:table AS regclass) AND contype = 'c' "
                "AND pg_get_constraintdef(oid) LIKE '%not_a_problem%'"
            ),
            {"table": TABLE},
        )
    assert "'undo'" in (values or "")  # an Undo is one more row (#1803)

    command.downgrade(config, BEFORE)
    assert "should_be" not in {c["name"] for c in inspect(postgres_engine).get_columns(TABLE)}
    assert _check(postgres_engine) is None
    command.upgrade(config, "head")
    assert "should_be" in {c["name"] for c in inspect(postgres_engine).get_columns(TABLE)}
