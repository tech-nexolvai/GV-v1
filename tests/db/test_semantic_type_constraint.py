"""The database accepts every semantic type the vocabulary declares — piece widths first (#991).

Verification for: `alembic/versions/0072_countertop_piece_width.py`.

`CT-WIDTH-001` 1.1.0 reads `countertop_piece_width`. `0006` fixed the allowed types in two CHECK
constraints, and nothing compared them with the enum: `compare_metadata` ignores check constraints, and
every test that builds tables with `create_all` builds them from the enum, so it agrees with itself.
`cabinet_category` had been missing from the migrated database since #681 for exactly that reason.
These tests read what the *migrations* install, as `tests/db/test_run_models.py` does for outcomes.
"""

from __future__ import annotations

import re

from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text

from alembic import command
from tests.app.postgres_fixture import alembic_config
from vocabulary.semantic_types import SemanticType

pytest_plugins = ("tests.app.postgres_fixture",)

#: Every value, aliases collapsed — what `app/models/evidence.py` builds its constraint from.
DECLARED = frozenset(member.value for member in SemanticType)

TABLES = {
    "canonical_observations": "ck_canonical_observations_canonical_observation_semantic_type",
    "observation_candidates": "ck_observation_candidates_observation_candidate_semantic_guess",
}


def _from_the_newest_migration() -> tuple[str, frozenset[str]]:
    """The list the newest migration naming `SEMANTIC_TYPES` installs, and which revision it is."""
    script = ScriptDirectory.from_config(alembic_config())
    for revision in script.walk_revisions():
        values = getattr(revision.module, "SEMANTIC_TYPES", None)
        if values is not None:
            return revision.revision, frozenset(re.findall(r"'([^']+)'", values))
    raise AssertionError("no migration defines SEMANTIC_TYPES")


def test_the_newest_migration_lists_every_declared_semantic_type() -> None:
    """Runs without a database. A type added to the vocabulary without a migration fails here."""
    revision, migrated = _from_the_newest_migration()

    assert migrated == DECLARED, (
        f"SemanticType and migration {revision} disagree.\n"
        f"  only in the enum:      {sorted(DECLARED - migrated)}\n"
        f"  only in the migration: {sorted(migrated - DECLARED)}\n"
        "Add a migration widening both semantic-type CHECK constraints; never edit a shipped one."
    )


def test_the_piece_width_is_among_them() -> None:
    """Two lists agreeing is not the fix: the one this story needs must be in both."""
    _, migrated = _from_the_newest_migration()

    assert SemanticType.COUNTERTOP_PIECE_WIDTH.value == "countertop_piece_width"
    assert "countertop_piece_width" in migrated


def test_a_migrated_database_enforces_the_declared_types(postgres_engine: Engine) -> None:
    """The authoritative version: read both constraints back out of `alembic upgrade head`."""
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")

    with postgres_engine.connect() as connection:
        for table, name in TABLES.items():
            definition = connection.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = :name AND conrelid = CAST(:table AS regclass)"
                ),
                {"name": name, "table": table},
            ).scalar_one()
            enforced = frozenset(re.findall(r"'([^']+)'", definition))

            assert enforced == DECLARED, f"{table}: {definition}"
