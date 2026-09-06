"""Put every extension in one named schema, so any session can find them (#513).

Revision ID: 0034_pg_trgm_in_public
Revises: 0033_finding_provenance

Migration 0020 runs `CREATE EXTENSION IF NOT EXISTS pg_trgm` with no schema, so the extension lands
in whichever schema happens to be first on the session's `search_path`. An extension is a
database-wide object living in exactly one schema, and `gin_trgm_ops` is resolved by name through
`search_path` like anything else. Put together, those two facts make the unqualified form a race:

- The first session to run 0020 owns the extension, wherever it was pointing.
- Every later session gets `IF NOT EXISTS` doing nothing, and then cannot create the GIN index,
  because the operator class is in a schema it cannot see:
  `operator class "gin_trgm_ops" does not exist for access method "gin"`.
- When the first session's schema is dropped, the extension goes with it, and the next session
  silently becomes the owner.

The test fixture gives every test its own schema, so this is not hypothetical: it is why database
tests fail in batches whenever two pytest processes share a database, and why a long-lived migrated
schema — which any attempt to stop re-migrating per test needs — breaks immediately (#522).

**Moved rather than recreated.** `ALTER EXTENSION ... SET SCHEMA` relocates it and leaves every index
that depends on it valid, because those dependencies are by object id and not by name. Dropping and
recreating would invalidate `ix_item_identifiers_value_trigram` and lose it.

**The index is rebuilt anyway**, with the operator class qualified. An index created before this
migration names `gin_trgm_ops` as resolved at creation time, which is correct but leaves the schema's
own definition depending on a search path — and `tests/app/test_migration_matches_models.py` compares
what the migrations build against what the models declare, so the two have to agree on the qualified
form.

Idempotent in both directions: it checks where the extension actually is before moving it, because
a database that never had the problem is the common case and must not fail here.

Source: issue #513. Verification: tests/db/test_drawing_models.py, tests/retrieval/lanes/test_trigram.py.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0034_pg_trgm_in_public"
down_revision: str | None = "0033_finding_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Where the extension belongs. Matches `app.models.drawing.EXTENSION_SCHEMA`, written out here
#: because a migration has to keep saying what it said the day it ran.
EXTENSION_SCHEMA = "public"

#: Every extension this database installs. Both are database-wide, and both were unqualified.
EXTENSIONS = ("pg_trgm", "vector")

INDEX = "ix_item_identifiers_value_trigram"


def _relocate(extension: str) -> None:
    """Create the extension in the named schema, or move it there if it is somewhere else.

    Two statements rather than one: `CREATE EXTENSION IF NOT EXISTS ... SCHEMA` is an error when the
    extension already exists in a *different* schema, which is precisely the case being repaired.
    Both are guarded on the catalog, so a database that never had the problem is left untouched.
    """
    op.execute(f"CREATE EXTENSION IF NOT EXISTS {extension} SCHEMA {EXTENSION_SCHEMA}")
    op.execute(
        f"DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM pg_extension e "
        f"JOIN pg_namespace n ON n.oid = e.extnamespace "
        f"WHERE e.extname = '{extension}' AND n.nspname <> '{EXTENSION_SCHEMA}') THEN "
        f"ALTER EXTENSION {extension} SET SCHEMA {EXTENSION_SCHEMA}; "
        f"END IF; END $$;"
    )


def upgrade() -> None:
    # **Both extensions, not only the one whose error message we had.** `vector` has the identical
    # bug and fails differently — `type "vector" does not exist` — because it supplies a type rather
    # than an operator class. Found by keeping a migrated schema alive and watching the next one
    # fail; fixing only pg_trgm would have left this to be rediscovered later.
    for extension in EXTENSIONS:
        _relocate(extension)
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(
        f"CREATE INDEX {INDEX} ON item_identifiers "
        f"USING gin (value_as_printed {EXTENSION_SCHEMA}.gin_trgm_ops)"
    )


def downgrade() -> None:
    """Returns the index to the unqualified spelling; leaves the extension where it is.

    The extension is not moved back. Nothing knows where it was — the whole problem being fixed is
    that it went wherever a search path pointed — and putting it somewhere arbitrary would recreate
    the bug rather than undo it.
    """
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(
        f"CREATE INDEX {INDEX} ON item_identifiers USING gin (value_as_printed gin_trgm_ops)"
    )
