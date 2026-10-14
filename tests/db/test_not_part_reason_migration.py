"""Migration 0097: why a person said a piece is not a part (#1246).

Checked against what the *migrations* install, not what `create_all` builds from the models: the
column is nullable, its values are `NotPartReason`'s, it is allowed only on a person's withdrawal,
and the migration goes down and up, refusing to go down while a reason is stored.

Every value here is synthetic.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.models import NotPartReason
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

REVISION = "0097_not_part_reason"
BEFORE = "0096_reading_events"


def _config(engine: Engine):  # type: ignore[no-untyped-def]
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    return config


def _columns(engine: Engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("part_confirmations")}


def _proposal(engine: Engine) -> UUID:
    """One suggested part on one drawing, written with SQL so it works on either side."""
    ids = {name: uuid4() for name in ("project", "package", "document", "artifact", "version")}
    ids.update(page=uuid4(), view=uuid4(), proposal=uuid4())
    sha = uuid4().hex * 2
    statements = (
        "INSERT INTO projects (id, name, created_at) VALUES (:project, 'synthetic', now())",
        "INSERT INTO packages (id, project_id, created_at) VALUES (:package, :project, now())",
        (
            "INSERT INTO documents (id, package_id, kind, created_at) "
            "VALUES (:document, :package, 'shop', now())"
        ),
        (
            "INSERT INTO source_artifacts (id, storage_key, sha256, size, created_at) "
            "VALUES (:artifact, 'synthetic/' || :artifact, :sha, 1, now())"
        ),
        (
            "INSERT INTO document_versions (id, document_id, source_artifact_id, sha256, page_count, "
            "created_at) VALUES (:version, :document, :artifact, :sha, 1, now())"
        ),
        (
            "INSERT INTO pages (id, created_at, document_version_id, index, content_hash, width_pt, "
            "height_pt, rotation, has_vector_text, render_failed, media_box, crop_box) VALUES (:page, "
            'now(), :version, 0, :sha, 612, 792, 0, false, false, \'["0","0","612","792"]\', '
            '\'["0","0","612","792"]\')'
        ),
        (
            "INSERT INTO drawing_views (id, created_at, page_id, tag, region) VALUES (:view, now(), "
            ':page, \'panel-1\', \'{"space": "stored", "points": []}\')'
        ),
        (
            "INSERT INTO part_proposals (id, created_at, drawing_view_id, kind, extent, "
            "defining_line, reason, source, source_version) VALUES (:proposal, now(), :view, "
            "'cabinet', '{\"space\": \"stored\"}', NULL, 'a synthetic suggestion', 'synthetic', "
            "'v1')"
        ),
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement), {**ids, "sha": sha})
    return ids["proposal"]


def _withdraw(
    engine: Engine, proposal: UUID, reason: str | None, actor: str = "a@example.com"
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO part_confirmations (id, created_at, part_proposal_id, decision, "
                "confirmed_by, not_part_reason) VALUES (:id, now(), :proposal, 'withdrawn', "
                ":actor, :reason)"
            ),
            {"id": uuid4(), "proposal": proposal, "actor": actor, "reason": reason},
        )


def test_the_revision_follows_the_reading_events_on_the_one_line_of_history() -> None:
    """`alembic_version.version_num` is 32 characters; a longer id fails CI at upgrade time."""
    assert len(REVISION) <= 32
    script = ScriptDirectory.from_config(alembic_config())
    found = script.get_revision(REVISION)
    assert found is not None and found.down_revision == BEFORE
    assert REVISION in {
        rev.revision for rev in script.walk_revisions("base", script.get_current_head() or "")
    }


def test_the_migration_writes_out_exactly_the_models_reasons() -> None:
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    written = {value.strip().strip("'") for value in migration.REASONS.split(",")}
    assert written == {reason.value for reason in NotPartReason}


def test_the_column_is_nullable_and_holds_only_a_persons_reason_on_a_withdrawal(
    postgres_engine: Engine,
) -> None:
    command.upgrade(_config(postgres_engine), "head")
    column = next(
        c
        for c in inspect(postgres_engine).get_columns("part_confirmations")
        if c["name"] == "not_part_reason"
    )
    assert column["nullable"] is True

    for reason in (None, *(reason.value for reason in NotPartReason)):
        _withdraw(postgres_engine, _proposal(postgres_engine), reason)
    for refused, actor in (
        ("a-cupboard", "a@example.com"),
        ("", "a@example.com"),
        ("other", "code:bala-two-judgments/v1"),
    ):
        with pytest.raises(IntegrityError, match="part_confirmation_not_part_reason"):
            _withdraw(postgres_engine, _proposal(postgres_engine), refused, actor)


def test_the_migration_goes_down_and_up_and_an_older_decision_has_no_reason(
    postgres_engine: Engine,
) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    command.downgrade(config, BEFORE)
    try:
        assert "not_part_reason" not in _columns(postgres_engine)
        proposal = _proposal(postgres_engine)
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO part_confirmations (id, created_at, part_proposal_id, decision, "
                    "confirmed_by) VALUES (:id, now(), :proposal, 'withdrawn', 'a@example.com')"
                ),
                {"id": uuid4(), "proposal": proposal},
            )
    finally:
        command.upgrade(config, "head")
    with postgres_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT not_part_reason FROM part_confirmations WHERE part_proposal_id = :p"),
                {"p": proposal},
            ).scalar_one()
            is None
        )


def test_a_downgrade_with_a_reason_stored_refuses(postgres_engine: Engine) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    _withdraw(postgres_engine, _proposal(postgres_engine), "side-panel")

    with pytest.raises(RuntimeError, match="Preserve these records"):
        command.downgrade(config, BEFORE)

    assert "not_part_reason" in _columns(postgres_engine)
