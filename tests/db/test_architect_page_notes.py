"""Migration 0082: the architect reader's page notes table, up and down (#1163).

Verification for: `alembic/versions/0082_architect_page_notes.py` and
`app.models.evidence.ArchitectPageNote`. The rows are written by the stage on an invented sheet
(`tests/extraction/architect/architect_sheet.py`); no client value appears here.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models.evidence import ARCHITECT_PAGE_NOTE_KINDS, ArchitectPageNote
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.architect.architect_sheet import architect_sheet

pytest_plugins = ("tests.app.postgres_fixture",)

_HEAD = "0082_architect_page_notes"
_BEFORE = "0081_evidence_mark_rechecks"


def _config(engine: Engine) -> Config:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    return config


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def test_0082_is_the_head_and_follows_0081() -> None:
    """0082 follows 0081, and the single head is 0082 or a migration built on it (0083 since
    #1166)."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config())
    (head,) = script.get_heads()
    assert _HEAD in {revision.revision for revision in script.walk_revisions("base", head)}
    revision = script.get_revision(_HEAD)
    assert revision is not None and revision.down_revision == _BEFORE


def test_upgrade_creates_the_table_and_an_empty_downgrade_removes_it(
    postgres_engine: Engine,
) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    assert "architect_page_notes" in _tables(postgres_engine)
    try:
        command.downgrade(config, _BEFORE)
        assert "architect_page_notes" not in _tables(postgres_engine)
        command.upgrade(config, "head")
        assert "architect_page_notes" in _tables(postgres_engine)
    finally:
        command.upgrade(config, "head")


def _noted(postgres_engine: Engine, store: LocalStore) -> Session:
    """A package whose architect page gave a note, read by the stage."""
    from tests.workflow.test_architect_file_reader import _extract, _package

    command.upgrade(_config(postgres_engine), "head")
    session = session_factory(postgres_engine)()
    revision, _version = _package(session, store, architect=architect_sheet(titled=False))
    _extract(session, store, revision)
    assert session.query(ArchitectPageNote).count() == 1
    return session


def test_a_downgrade_with_notes_stored_refuses(postgres_engine: Engine, store: LocalStore) -> None:
    session = _noted(postgres_engine, store)
    session.close()
    config = _config(postgres_engine)
    try:
        with pytest.raises(RuntimeError, match="Preserve these records"):
            command.downgrade(config, _BEFORE)
        assert "architect_page_notes" in _tables(postgres_engine)
    finally:
        command.upgrade(config, "head")


def test_the_kinds_are_a_closed_set_held_by_the_database(
    postgres_engine: Engine, store: LocalStore
) -> None:
    session = _noted(postgres_engine, store)
    try:
        note = session.query(ArchitectPageNote).one()
        assert note.kind in ARCHITECT_PAGE_NOTE_KINDS
        with pytest.raises(IntegrityError, match="architect_page_note_kind"):
            session.execute(
                text(
                    "INSERT INTO architect_page_notes "
                    "(id, created_at, extraction_run_id, document_version_id, page_id, kind, text) "
                    "VALUES (gen_random_uuid(), now(), :run, :version, :page, 'guessed', 'x')"
                ),
                {
                    "run": note.extraction_run_id,
                    "version": note.document_version_id,
                    "page": note.page_id,
                },
            )
        session.rollback()
    finally:
        session.close()
