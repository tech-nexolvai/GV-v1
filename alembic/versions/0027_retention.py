"""Retention: what expires, what is held, and the record that something went (#258, F1.7).

Revision ID: 0027_retention
Revises: 0026_outbox_trace_context

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
from app.db.roles import grant_body

revision: str = "0027_retention"
down_revision: str | None = "0026_outbox_trace_context"
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

    # Re-applied because `legal_holds` did not exist when `0025` ran, and a grant is current state
    # rather than a historical fact. Without this the newest table would be held by nobody — and
    # `tests/db/test_roles.py` compares the declaration against `information_schema`, so a missing
    # grant fails there rather than surfacing as a permission error in production.
    op.execute(grant_body())


def downgrade() -> None:
    """Drops the table; refuses to narrow the category set once it has been used.

    `audit_events` is append-only — a trigger from `0024` refuses `DELETE`, and no role holds the
    privilege either. So once a single `ARTIFACT_DELETION` row exists, narrowing the `CHECK` back to
    six categories cannot succeed: the constraint would be violated by rows nothing is permitted to
    remove, and the migration would fail partway with the table already dropped.

    Refusing up front, with the count and an explanation, is the honest behaviour. The alternative —
    deleting the offending rows — would have this migration destroy audit records to make its own
    downgrade tidy, which is the exact thing the append-only trigger exists to prevent.
    """
    connection = op.get_bind()
    recorded = connection.execute(
        sa.text("SELECT count(*) FROM audit_events WHERE category = 'ARTIFACT_DELETION'")
    ).scalar_one()
    if recorded:
        raise RuntimeError(
            f"{recorded} ARTIFACT_DELETION audit row(s) exist, and audit_events is append-only, so "
            "this downgrade cannot narrow the category constraint back to six. Those rows are the "
            "record that content was deleted; removing them to tidy a downgrade is what the "
            "append-only trigger exists to prevent."
        )

    op.drop_index("ix_legal_holds_active", table_name="legal_holds")
    op.drop_table("legal_holds")
    _recheck(_PREVIOUS_CATEGORIES)
