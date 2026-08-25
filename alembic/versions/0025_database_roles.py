"""Four least-privilege database roles (#253, F1.2).

Revision ID: 0025_database_roles
Revises: 0024_audit_events_append_only

Backend §11 names four roles — application, worker, read-only reporting, and verdict, the last with
no model or retrieval credentials. `tests/test_verdict_isolation.py` enforces the static half: the
verdict service cannot import retrieval or model code. This migration is the runtime half, so that
handing the verdict a session does not hand it the tables its import graph refuses to name.

**Roles are cluster-wide; grants are not.** `CREATE ROLE` affects the whole PostgreSQL cluster, while
`GRANT ... ON TABLE` affects one schema's tables. The test fixture runs every test in its own
temporary schema, so this migration creates the roles idempotently — `IF NOT EXISTS`, effectively —
and grants against `current_schema()`. A migration that hardcoded `public` would grant nothing in a
test schema and the tests would pass by proving nothing, which is the failure mode `0013` warned
about in its own docstring.

**`NOLOGIN`, and no passwords.** These are permission sets, not accounts. Deployment creates login
users and grants them membership. Nothing here needs a credential, so nothing here can leak one.

**`REVOKE ... FROM PUBLIC` first.** Every role is a member of `PUBLIC`, and on PostgreSQL before 15
`PUBLIC` holds `CREATE` on the `public` schema. Granting a narrow allowlist to `gv_verdict` while
leaving `PUBLIC` able to create a table — or a view over a retrieval table — would be a lock with the
window open.

**What this does not do.** Nothing connects as these roles yet. The application runs as the database
owner, and an owner is restricted by neither `REVOKE` nor a missing `GRANT`. These grants are the
declaration a deployment binds to; `tests/db/test_roles.py` proves the declaration is real by
assuming each role with `SET ROLE` and attempting what it should not be able to do. Saying so
plainly here rather than letting "least privilege" be read as already in force.

The grant table is imported from `app.db.roles` rather than written out, which is the opposite of
what `0013` does with its table list — deliberately, and for the same reason. `0013`'s list records
which tables were immutable *on the day it ran*, a historical fact that must not change. A grant is
not historical: it is the current privilege set, and this migration is the only thing that applies
it. Two copies would drift, and the drift would be silent in the direction of more privilege.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op
from app.db.roles import ROLE_GRANTS, Role

revision: str = "0025_database_roles"
down_revision: str | None = "0024_audit_events_append_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: `CREATE ROLE` has no `IF NOT EXISTS`, and roles are cluster-wide: every test schema in a shared
#: database runs this, and only the first can create them.
#:
#: The `EXISTS` check alone is not enough. Two connections can both pass it and then one fails on the
#: `CREATE`, so `duplicate_object` is caught as well — the check keeps the common path quiet and the
#: handler makes it correct. Belt and braces here rather than a comment promising nothing runs
#: concurrently, which is the sort of promise a later `pytest -n auto` breaks silently.
CREATE_ROLE = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
        CREATE ROLE {role} NOLOGIN;
    END IF;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END
$$;
"""


def _in_current_schema(*statements: str) -> str:
    """Wrap statements so `%I` is filled with `current_schema()` when they run.

    The schema has to be resolved at execution time — the test fixture gives every test its own —
    but it must **not** be resolved by this file querying for it. `tests/app/test_migration_matches
    _models.py` renders every migration with `alembic upgrade head --sql`, against no database at
    all, because a migration nobody has tried to execute is a migration that may not execute. A
    `SELECT current_schema()` in Python breaks that check, and the fix for the check would have been
    to weaken it.

    So the resolution happens inside PostgreSQL: the emitted SQL is a static `DO` block, which
    renders offline exactly as it runs. `%I` quotes the identifier, so a schema name needing quotes
    is handled by the database rather than by string concatenation here.
    """
    body = "\n".join(f"    EXECUTE format({statement!r}, gv_schema);" for statement in statements)
    return f"""
DO $$
DECLARE
    gv_schema text := current_schema();
BEGIN
{body}
END
$$;
"""


def upgrade() -> None:
    connection = op.get_bind()

    for role in Role:
        connection.execute(text(CREATE_ROLE.format(role=role.value)))

    statements: list[str] = [
        # USAGE only. CREATE would let a role add a table — or a view over a table it was not
        # granted — inside the schema, and a view is a perfectly good way to read what you cannot.
        *(f"GRANT USAGE ON SCHEMA %I TO {role.value}" for role in Role),
        # PUBLIC is an implicit member of every role, so a privilege left here is granted to all
        # four and would make each allowlist below meaningless without any of them changing.
        "REVOKE ALL ON SCHEMA %I FROM PUBLIC",
        "REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM PUBLIC",
    ]

    for role, grants in ROLE_GRANTS.items():
        # Cleared first, so re-running this migration cannot leave behind a privilege the
        # declaration has since dropped. A grant removed from `app/db/roles.py` has to disappear
        # from the database, or the declaration describes what used to be true.
        statements.append(f"REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM {role.value}")
        statements.append(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM {role.value}")
        for table, privileges in grants.privileges.items():
            granted = ", ".join(privileges)
            statements.append(f'GRANT {granted} ON TABLE %I."{table}" TO {role.value}')

    connection.execute(text(_in_current_schema(*statements)))


def downgrade() -> None:
    """Revoke the grants; leave the roles.

    A role is cluster-wide and may own objects or be a member of something outside this schema, so
    `DROP ROLE` from a per-schema downgrade could fail, or take something with it that this migration
    never created. Revoking is the reversal of what was *granted* here.

    **Deliberately not symmetric.** `upgrade()` also revokes everything from `PUBLIC`, and this does
    not put it back. Restoring a privilege to `PUBLIC` on the way down would hand it to all four
    roles — a downgrade that widened access is not a downgrade anybody wants, and nothing in this
    system depended on `PUBLIC` holding those privileges in the first place. Said here rather than
    left for the next reader to assume the migration reverses cleanly.
    """
    statements = [
        statement
        for role in Role
        for statement in (
            f"REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM {role.value}",
            f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM {role.value}",
            f"REVOKE USAGE ON SCHEMA %I FROM {role.value}",
        )
    ]
    op.get_bind().execute(text(_in_current_schema(*statements)))
