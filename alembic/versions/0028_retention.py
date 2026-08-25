"""Retention: what expires, what is held, and the record that something went (#258, F1.7).

Revision ID: 0028_retention
Revises: 0027_parameter_sets

Client drawings are proprietary and backend §11 requires retention schedules. Two things are needed
before a policy can run at all: somewhere to record that content is under legal hold, and a way to
audit a deletion.

**`ARTIFACT_DELETION` is a seventh audit category.** Backend §11 lists six. Deletion is the one event
where the thing that would prove it happened is the thing being removed — the bytes — so without a
category for it the trail would be complete about everything except its own gaps. Added by rewriting
the `CHECK`, because the constraint hardcodes its values; a new enum member without a new migration
is how `ModelInvocationOutcome.FAILED` came to be rejected by the database for as long as it was.

**`legal_holds` is a table, not a flag on an artifact.** A hold is placed on a *matter* — a dispute,
an audit, a request from counsel — and it covers whatever content belongs to a project, including
content that does not exist yet. A boolean per artifact could not express that, and would have to be
set on every future upload by whoever remembered.

It carries `released_at` rather than being deleted on release, and `Immutable` is deliberately **not**
applied: a hold is placed and later lifted, and that lifting is an ordinary update. What must not
disappear is the fact that a hold existed during some window, which `released_at` preserves — a
deleted row would leave no way to answer "was this under hold when retention ran?"

`reason` is required. A hold nobody can explain is one nobody dares release, and content held
forever by default is the failure mode a retention policy exists to prevent.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0028_retention"
down_revision: str | None = "0027_parameter_sets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every category after this migration. Written out, because a migration has to keep saying what it
#: said the day it ran — the same reason `0013` lists its tables rather than importing them.
_CATEGORIES = (
    "STATE_CHANGE",
    "RULE_PUBLICATION",
    "FINDING",
    "REVIEW_ACTION",
    "EXCEPTION",
    "ARTIFACT_DOWNLOAD",
    "ARTIFACT_DELETION",
)

_PREVIOUS_CATEGORIES = _CATEGORIES[:-1]


def _in_current_schema(*statements: str) -> str:
    """Wrap statements so `%I` becomes `current_schema()` when they run.

    The same shape 0025 and 0027 use, and for the same reason: the test fixture gives every test its
    own schema, so the schema is resolved at execution time — by PostgreSQL, not by this file
    querying for it, because `tests/app/test_migration_matches_models.py` renders every migration
    offline against no database at all.
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


def _recheck(categories: Sequence[str]) -> None:
    op.drop_constraint("audit_events_category", "audit_events", type_="check")
    op.create_check_constraint(
        "audit_events_category",
        "audit_events",
        "category IN (" + ", ".join(f"'{name}'" for name in categories) + ")",
    )


def upgrade() -> None:
    _recheck(_CATEGORIES)

    op.create_table(
        "legal_holds",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("placed_by", sa.String(length=200), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by", sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("reason !~ '^[[:space:]]*$'", name="legal_hold_reason"),
        sa.CheckConstraint("placed_by !~ '^[[:space:]]*$'", name="legal_hold_placed_by"),
        # A release has to say who lifted it. "The hold came off at some point, by someone" is the
        # answer nobody can act on, and the release is the moment content becomes deletable.
        sa.CheckConstraint(
            "(released_at IS NULL AND released_by IS NULL) OR "
            "(released_at IS NOT NULL AND released_by IS NOT NULL AND released_by !~ '^[[:space:]]*$')",
            name="legal_hold_release_attributed",
        ),
    )
    # Retention asks one question of this table, for one project, on every pass: is anything still
    # holding it? The partial index answers exactly that and stays small, because released holds are
    # history and never queried by the policy.
    op.create_index(
        "ix_legal_holds_active",
        "legal_holds",
        ["project_id"],
        postgresql_where=sa.text("released_at IS NULL"),
    )

    # **Grants for `legal_holds`.** 0025 derives its grant list from `Base.metadata`, so it already
    # names this table — but it runs three migrations earlier, when the table does not exist. #303
    # made that skip rather than fail, which leaves the grant to the migration that creates it.
    #
    # Read from `app.db.roles` rather than written out, for the reason 0025 gives: a grant is the
    # current privilege set, and two copies drift in the direction of more privilege. And
    # `tests/db/test_roles.py` compares the declaration against `information_schema`, so a table
    # left ungranted fails there rather than surfacing as a permission error in production.
    grant_statements = [
        f'GRANT {", ".join(privileges)} ON TABLE %I."{table}" TO {role.value}'
        for role, grants in ROLE_GRANTS.items()
        for table, privileges in grants.privileges.items()
        if table == "legal_holds"
    ]
    if grant_statements:
        op.get_bind().execute(sa.text(_in_current_schema(*grant_statements)))


def downgrade() -> None:
    op.drop_index("ix_legal_holds_active", table_name="legal_holds")
    op.drop_table("legal_holds")
    _recheck(_PREVIOUS_CATEGORIES)
