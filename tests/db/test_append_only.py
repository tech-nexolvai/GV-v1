"""Immutable tables are immutable in the database, not by convention (#202, C1.12).

Twenty-eight tables carry the `Immutable` marker. Until this story the marker was enforced by
nothing: an `UPDATE` against any of them succeeded, and every claim made in six migrations about
records that "cannot be edited" was a claim about discipline rather than about the schema.

The isolation guard and the licence guard both work because they fail rather than asking people to
remember. This is the same idea applied to the audit trail.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from alembic import command
from app.db.base import Base, Immutable, immutable_table_names
from app.db.session import session_factory, unit_of_work
from app.models import Project, SourceArtifact
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _migration_tables() -> tuple[str, ...]:
    """Every table any migration has made append-only, unioned across all of them.

    Read from **every** migration declaring `IMMUTABLE_TABLES`, not only 0013. That migration says
    why in its own docstring: its list is written out because *"a migration has to keep saying what
    it said the day it ran"*. So a table added later is protected by a later migration, and appending
    to 0013 would make an applied migration claim it had protected something that did not yet exist.

    Reading only the first migration made this guard fail the moment a new `Immutable` model landed
    — which it did, correctly, for `audit_events`: the marker was there and the trigger was not.
    """
    names: set[str] = set()
    for path in sorted(VERSIONS.glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"append_only_{path.stem}", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        names.update(getattr(module, "IMMUTABLE_TABLES", ()))
    return tuple(sorted(names))


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


def _artifact(session: Session) -> SourceArtifact:
    artifact = SourceArtifact(
        storage_key=f"originals/{uuid4()}.pdf", sha256="e" * 64, size=1, backend_version_id=None
    )
    session.add(artifact)
    session.flush()
    return artifact


# ---------------------------------------------------------------------------
# The list is derived, and the two copies agree
# ---------------------------------------------------------------------------


def test_the_protected_list_is_explicit_and_matches_the_marker() -> None:
    """The acceptance asks for a list that is explicit and tested rather than inferred.

    The migration writes it out — a migration has to keep saying what it said the day it ran, and a
    list computed from live metadata would silently change meaning as tables are added. This is what
    stops the two copies drifting: add an `Immutable` table without a migration and this fails.
    """
    assert _migration_tables() == immutable_table_names()


def test_every_marked_table_is_covered() -> None:
    """Stated the other way round, because the failure that matters is a table nobody protected —
    and it would be the one somebody most wanted to edit."""
    marked = {
        mapper.local_table.name
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, Immutable) and mapper.local_table is not None
    }
    assert marked <= set(_migration_tables())


def test_the_list_is_not_empty() -> None:
    """A passing comparison of two empty lists would be the quietest possible failure."""
    assert len(_migration_tables()) >= 28


# ---------------------------------------------------------------------------
# Against a real database, through the ORM
# ---------------------------------------------------------------------------


def test_an_update_through_the_orm_is_rejected(postgres_engine: Engine) -> None:
    """Through the ORM, not a mock. The acceptance says the database must refuse it, and a mock
    would only prove that the test author believed it would."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        _artifact(session)

    with pytest.raises(DBAPIError, match="append-only"), unit_of_work(factory) as session:
        stored = session.scalars(select(SourceArtifact)).first()
        assert stored is not None
        stored.size = 999
        session.flush()


def test_a_delete_through_the_orm_is_rejected(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        _artifact(session)

    with pytest.raises(DBAPIError, match="append-only"), unit_of_work(factory) as session:
        stored = session.scalars(select(SourceArtifact)).first()
        assert stored is not None
        session.delete(stored)
        session.flush()


def test_a_raw_sql_update_is_rejected_too(postgres_engine: Engine) -> None:
    """The ORM is not the only way in. A trigger refuses whoever is connected, which is why this is a
    trigger rather than a `REVOKE`: CI connects as the database owner, and `REVOKE` does not restrict
    an owner or a superuser — the guard would have run and permitted the update."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        _artifact(session)

    with pytest.raises(DBAPIError, match="append-only"), unit_of_work(factory) as session:
        session.execute(text("UPDATE source_artifacts SET size = 42"))


def test_inserting_still_works(postgres_engine: Engine) -> None:
    """Append-only, not read-only. The check has to be able to say yes."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        _artifact(session)
        _artifact(session)
    with unit_of_work(factory) as session:
        assert len(session.scalars(select(SourceArtifact)).all()) == 2


def test_a_mutable_table_is_untouched(postgres_engine: Engine) -> None:
    """`projects` carries no marker and must stay editable. A guard that froze everything would be
    indistinguishable from a broken database."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        session.add(Project(name="before"))
    with unit_of_work(factory) as session:
        project = session.scalars(select(Project)).one()
        project.name = "after"
    with unit_of_work(factory) as session:
        assert session.scalars(select(Project)).one().name == "after"


def test_the_refusal_says_what_to_do_instead(postgres_engine: Engine) -> None:
    """ "A correction is a new row" is the whole policy, and the error message is where somebody hits
    it — long after anybody reads this file."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        _artifact(session)
    with pytest.raises(DBAPIError) as raised, unit_of_work(factory) as session:
        session.execute(text("DELETE FROM source_artifacts"))
    assert "a correction is a new row" in str(raised.value).lower()


def test_schema_changes_still_work(postgres_engine: Engine) -> None:
    """The acceptance asks for it explicitly. A row trigger fires on UPDATE and DELETE of *rows*;
    DDL is untouched, so a migration that legitimately alters one of these tables is unaffected."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        session.execute(text("ALTER TABLE source_artifacts ADD COLUMN scratch_check integer"))
        session.execute(text("ALTER TABLE source_artifacts DROP COLUMN scratch_check"))


def test_a_second_upgrade_is_idempotent(postgres_engine: Engine) -> None:
    """`_upgrade` runs per test against a shared database in some configurations, and a trigger
    created twice would error rather than being a no-op."""
    _upgrade(postgres_engine)
    _upgrade(postgres_engine)


def test_every_protected_table_actually_has_its_trigger(postgres_engine: Engine) -> None:
    """The enumeration test the acceptance asks for, asked of the database rather than of the
    migration source: what matters is what is installed, not what was written."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        installed = {
            row[0]
            for row in session.execute(
                text(
                    "SELECT event_object_table FROM information_schema.triggers "
                    "WHERE trigger_name LIKE '%_append_only'"
                )
            )
        }
    missing = sorted(set(immutable_table_names()) - installed)
    assert not missing, f"marked immutable but unprotected in the database: {missing}"


def test_an_owner_can_disable_the_trigger_and_that_is_the_boundary(postgres_engine: Engine) -> None:
    """The limit, demonstrated rather than claimed away.

    The migration says the trigger refuses ordinary mutation, and it does. It does **not** make these
    tables tamper-proof: a table owner can disable or drop the trigger, and a superuser can bypass
    every user trigger with `session_replication_role = replica`. Both need deliberate action from a
    role that can already rewrite the schema.

    This test exists so that "append-only" is not read as more than it is. Closing the gap needs the
    application to connect as a role owning nothing and holding only INSERT and SELECT — the grant
    half of C1.12, deferred because no such role exists and nothing connects as one. Asserting the
    bypass were impossible would be a comfortable test and a false one.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        _artifact(session)

    with unit_of_work(factory) as session:
        session.execute(
            text("ALTER TABLE source_artifacts DISABLE TRIGGER source_artifacts_append_only")
        )
        session.execute(text("UPDATE source_artifacts SET size = 7"))
        session.execute(
            text("ALTER TABLE source_artifacts ENABLE TRIGGER source_artifacts_append_only")
        )

    # And it protects again the moment it is re-enabled, so the guard is not left off by the test.
    with pytest.raises(DBAPIError, match="append-only"), unit_of_work(factory) as session:
        session.execute(text("UPDATE source_artifacts SET size = 8"))
