"""Migration 0098: which countertop a plan or section view shows (#1290).

Checked against what the *migrations* install, not what `create_all` builds from the models: the
table, its CHECKs and its unique index, that it is append-only, and that the migration goes down and
up, refusing to go down while a link is stored.

Every value here is synthetic.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from app.models.drawing import ITEM_VIEW_JUDGES, ITEM_VIEW_KINDS
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

REVISION = "0098_item_view_links"
BEFORE = "0097_not_part_reason"
REGION = {"space": "stored", "points": [["0.1", "0.2"], ["0.5", "0.2"], ["0.5", "0.6"]]}


def _config(engine: Engine):  # type: ignore[no-untyped-def]
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    return config


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _countertop(engine: Engine) -> dict[str, UUID]:
    """One suggested countertop on one drawing, and a reader's run, written with SQL."""
    ids = {
        name: uuid4()
        for name in (
            "project",
            "package",
            "document",
            "artifact",
            "version",
            "page",
            "view",
            "proposal",
            "revision",
            "workflow",
            "task",
            "run",
        )
    }
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
            "'countertop', '{\"space\": \"stored\"}', NULL, 'a synthetic suggestion', 'synthetic', "
            "'v1')"
        ),
        (
            "INSERT INTO package_revisions (id, package_id, revision_number, created_at, state) "
            "VALUES (:revision, :package, 1, now(), 'MATCHING')"
        ),
        (
            "INSERT INTO workflow_runs (id, package_revision_id, engine_run_id, created_at) "
            "VALUES (:workflow, :revision, 'synthetic', now())"
        ),
        (
            "INSERT INTO task_runs (id, workflow_run_id, idempotency_key, task_type, attempt, "
            "outcome, created_at) VALUES (:task, :workflow, 'synthetic-key', 'match', 1, "
            "'SUCCEEDED', now())"
        ),
        (
            "INSERT INTO extraction_runs (id, task_run_id, extractor, extractor_version, "
            "config_hash, dpi, created_at) VALUES (:run, :task, 'extraction.depth_reader', "
            "'depth-reader-v1', 'synthetic', 300, now())"
        ),
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement), {**ids, "sha": sha})
    return ids


def _link(
    engine: Engine,
    ids: dict[str, UUID],
    *,
    kind: str = "plan",
    judged_by: str = "code+ais",
    run: bool = True,
    region: dict[str, object] | None = None,
    heading: str | None = "PLAN VIEW",
) -> UUID:
    link = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO item_view_links (id, created_at, part_proposal_id, page_id, region, "
                "view_kind, heading_text, judged_by, extraction_run_id) VALUES (:id, now(), "
                ":proposal, :page, CAST(:region AS jsonb), :kind, :heading, :judge, :run)"
            ),
            {
                "id": link,
                "proposal": ids["proposal"],
                "page": ids["page"],
                "region": json.dumps(REGION if region is None else region),
                "kind": kind,
                "heading": heading,
                "judge": judged_by,
                "run": ids["run"] if run else None,
            },
        )
    return link


def test_the_revision_follows_the_not_part_reason_on_the_one_line_of_history() -> None:
    """`alembic_version.version_num` is 32 characters; a longer id fails CI at upgrade time."""
    assert len(REVISION) <= 32
    script = ScriptDirectory.from_config(alembic_config())
    found = script.get_revision(REVISION)
    assert found is not None and found.down_revision == BEFORE
    assert REVISION in {
        rev.revision for rev in script.walk_revisions("base", script.get_current_head() or "")
    }


def test_the_migration_writes_out_exactly_the_models_kinds_and_judges() -> None:
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    kinds = {value.strip().strip("'") for value in migration.KINDS.split(",")}
    judges = {value.strip().strip("'") for value in migration.JUDGES.split(",")}
    assert kinds == set(ITEM_VIEW_KINDS)
    assert judges == set(ITEM_VIEW_JUDGES)


def test_a_link_is_kept_and_its_checks_refuse_what_it_cannot_say(
    postgres_engine: Engine,
) -> None:
    command.upgrade(_config(postgres_engine), "head")
    ids = _countertop(postgres_engine)
    _link(postgres_engine, ids)
    _link(postgres_engine, ids, kind="section", region={**REGION, "points": [["0", "0"]]})
    _link(
        postgres_engine,
        ids,
        judged_by="person",
        run=False,
        heading=None,
        region={**REGION, "points": [["1", "1"]]},
    )

    refused = (
        ("item_view_link_kind", {"kind": "front"}),
        ("item_view_link_judge", {"judged_by": "code"}),
        ("item_view_link_run", {"run": False}),
        ("item_view_link_region_stored", {"region": {"space": "image", "points": []}}),
        ("item_view_link_heading_not_blank", {"heading": "  "}),
    )
    for number, (name, change) in enumerate(refused, start=2):
        arguments: dict[str, object] = {"region": {**REGION, "points": [[str(number), "0"]]}}
        arguments.update(change)
        with pytest.raises(IntegrityError, match=name):
            _link(postgres_engine, ids, **arguments)  # type: ignore[arg-type]


def test_one_countertop_page_and_region_is_linked_once(postgres_engine: Engine) -> None:
    command.upgrade(_config(postgres_engine), "head")
    ids = _countertop(postgres_engine)
    _link(postgres_engine, ids)
    with pytest.raises(IntegrityError, match="uq_item_view_links_item_page_region"):
        _link(postgres_engine, ids, kind="section")


def test_a_link_is_never_edited_or_deleted(postgres_engine: Engine) -> None:
    command.upgrade(_config(postgres_engine), "head")
    link = _link(postgres_engine, _countertop(postgres_engine))
    for statement in (
        "UPDATE item_view_links SET view_kind = 'section' WHERE id = :id",
        "DELETE FROM item_view_links WHERE id = :id",
    ):
        with pytest.raises(DBAPIError), postgres_engine.begin() as connection:
            connection.execute(text(statement), {"id": link})


def test_the_migration_goes_down_and_up(postgres_engine: Engine) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    command.downgrade(config, BEFORE)
    try:
        assert "item_view_links" not in _tables(postgres_engine)
    finally:
        command.upgrade(config, "head")
    assert "item_view_links" in _tables(postgres_engine)
    indexes = {index["name"] for index in inspect(postgres_engine).get_indexes("item_view_links")}
    assert {
        "ix_item_view_links_part_proposal_id",
        "ix_item_view_links_extraction_run_id",
        "uq_item_view_links_item_page_region",
    } <= indexes


def test_a_downgrade_with_a_link_stored_refuses(postgres_engine: Engine) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    _link(postgres_engine, _countertop(postgres_engine))

    with pytest.raises(RuntimeError, match="Preserve these records"):
        command.downgrade(config, BEFORE)

    assert "item_view_links" in _tables(postgres_engine)
