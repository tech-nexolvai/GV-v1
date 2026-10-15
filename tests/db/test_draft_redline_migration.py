"""Migration 0099: the draft cloud PDF's kind, `draft_redline` (#1340).

Checked against what the *migrations* install: the CHECK admits a draft after the upgrade and
refuses it before, the migration goes down and up, and it refuses to go down while a draft is
stored.

Every value here is synthetic.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.models.verdicts import OutputArtifactKind
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

REVISION = "0099_draft_redline_output"
BEFORE = "0098_item_view_links"


def _config(engine: Engine):  # type: ignore[no-untyped-def]
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    return config


def _revision(engine: Engine) -> UUID:
    ids = {"project": uuid4(), "package": uuid4(), "revision": uuid4()}
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (id, name, created_at) VALUES (:project, 'synthetic', now())"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO packages (id, project_id, created_at) VALUES (:package, :project, now())"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO package_revisions (id, package_id, revision_number, state, created_at) "
                "VALUES (:revision, :package, 1, 'UPLOADED', now())"
            ),
            ids,
        )
    return ids["revision"]


def _artifact(engine: Engine, revision: UUID, kind: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO output_artifacts (id, created_at, package_revision_id, kind, "
                "storage_key, sha256, media_type, findings) VALUES (:id, now(), :revision, :kind, "
                ":key, :sha, 'application/pdf', 1)"
            ),
            {
                "id": uuid4(),
                "revision": revision,
                "kind": kind,
                "key": f"synthetic/{uuid4()}",
                "sha": uuid4().hex * 2,
            },
        )


def test_the_revision_follows_the_item_view_links_on_the_one_line_of_history() -> None:
    assert len(REVISION) <= 32
    script = ScriptDirectory.from_config(alembic_config())
    found = script.get_revision(REVISION)
    assert found is not None and found.down_revision == BEFORE
    assert REVISION in {
        rev.revision for rev in script.walk_revisions("base", script.get_current_head() or "")
    }


def test_the_migration_writes_out_exactly_the_models_kinds() -> None:
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    written = {value.strip().strip("'") for value in migration.WITH_DRAFT.split(",")}
    assert written == {kind.value for kind in OutputArtifactKind}


def test_a_draft_is_admitted_after_the_upgrade_and_refused_before_it(
    postgres_engine: Engine,
) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    _artifact(postgres_engine, _revision(postgres_engine), "draft_redline")
    with postgres_engine.begin() as connection:
        connection.execute(text("TRUNCATE output_artifacts CASCADE"))

    command.downgrade(config, BEFORE)
    try:
        revision = _revision(postgres_engine)
        _artifact(postgres_engine, revision, "redline")
        with pytest.raises(IntegrityError, match="output_artifact_kind"):
            _artifact(postgres_engine, revision, "draft_redline")
    finally:
        command.upgrade(config, "head")
    _artifact(postgres_engine, revision, "draft_redline")


def test_a_downgrade_with_a_draft_stored_refuses(postgres_engine: Engine) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    _artifact(postgres_engine, _revision(postgres_engine), "draft_redline")

    with pytest.raises(RuntimeError, match="Preserve these records"):
        command.downgrade(config, BEFORE)

    with postgres_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM output_artifacts WHERE kind = 'draft_redline'")
            ).scalar_one()
            == 1
        )
