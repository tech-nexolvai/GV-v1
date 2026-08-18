"""Review sessions, actions, corrections, approvals and exceptions (#200).

Revision ID: 0012_review_plane
Revises: 0011_verdict_plane

The human half of the record: who decided, on what, and why — written so those answers cannot later
be tidied.

`review_exceptions.expires_at` is NOT NULL, and that is the whole control. A permanent silent
exception is not representable, so nobody can quietly switch a check off forever. The scope names one
finding, item or package; "this rule, everywhere" is a rule change wearing an exception's clothes.

`correction_ledger` keeps the original beside the correction. Storing only the corrected value would
leave no way to ask what we got wrong, which is the entire purpose and the reason the reviewer
correction rate can be measured at all.

Approvals reference findings through `approved_findings` rather than a list column: each is a
foreign key to a server-side row, which is what "never client-supplied values" means in a schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_review_plane"
down_revision: str | None = "0011_verdict_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIONS = "'confirm', 'correct', 'except', 'dismiss'"
_SCOPES = "'finding', 'item', 'package'"


def _identity_columns() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "review_sessions",
        *_identity_columns(),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("reviewer", sa.String(length=200), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("reviewer <> ''", name="review_session_reviewer_present"),
        sa.ForeignKeyConstraint(
            ["package_revision_id"],
            ["package_revisions.id"],
            name="fk_review_sessions_package_revision_id_package_revisions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_sessions"),
        sa.UniqueConstraint("id", "package_revision_id", name="uq_review_sessions_id_revision"),
    )
    op.create_index(
        "ix_review_sessions_package_revision_id", "review_sessions", ["package_revision_id"]
    )
    op.create_index("ix_review_sessions_reviewer", "review_sessions", ["reviewer"])

    op.create_table(
        "review_actions",
        *_identity_columns(),
        sa.Column("review_session_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("finding_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.CheckConstraint(f"action IN ({_ACTIONS})", name="review_action_kind"),
        sa.CheckConstraint("actor <> ''", name="review_action_actor_present"),
        # Both sides resolved against the same revision: a session reviewing package A cannot
        # carry an action on a finding from package B.
        sa.ForeignKeyConstraint(
            ["review_session_id", "package_revision_id"],
            ["review_sessions.id", "review_sessions.package_revision_id"],
            name="fk_review_actions_session_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id", "package_revision_id"],
            ["findings.id", "findings.package_revision_id"],
            name="fk_review_actions_finding_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_actions"),
        # Lets the ledger and the exception table bind to the *kind* of action.
        sa.UniqueConstraint("id", "action", name="uq_review_actions_id_action"),
    )
    op.create_index("ix_review_actions_action", "review_actions", ["action"])
    op.create_index("ix_review_actions_finding_action", "review_actions", ["finding_id", "action"])
    op.create_index("ix_review_actions_finding_id", "review_actions", ["finding_id"])
    op.create_index(
        "ix_review_actions_package_revision_id", "review_actions", ["package_revision_id"]
    )
    op.create_index("ix_review_actions_review_session_id", "review_actions", ["review_session_id"])

    op.create_table(
        "correction_ledger",
        *_identity_columns(),
        sa.Column("review_action_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("canonical_observation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("original_value", sa.String(length=500), nullable=False),
        sa.Column("corrected_value", sa.String(length=500), nullable=False),
        sa.CheckConstraint("original_value <> ''", name="correction_original_present"),
        sa.CheckConstraint("corrected_value <> ''", name="correction_corrected_present"),
        sa.CheckConstraint(
            "original_value <> corrected_value", name="correction_actually_changes_something"
        ),
        # A ledger entry may only hang off a `correct` action. Without this it could attach to a
        # `confirm` or a `dismiss`, and the correction rate would count events that were not
        # corrections.
        sa.ForeignKeyConstraint(
            ["review_action_id", "action"],
            ["review_actions.id", "review_actions.action"],
            name="fk_correction_action_kind",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("action = 'correct'", name="correction_action_is_a_correction"),
        sa.ForeignKeyConstraint(
            ["canonical_observation_id"],
            ["canonical_observations.id"],
            name="fk_correction_observation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_correction_ledger"),
    )
    op.create_index(
        "ix_correction_ledger_canonical_observation_id",
        "correction_ledger",
        ["canonical_observation_id"],
    )
    op.create_index(
        "ix_correction_ledger_review_action_id",
        "correction_ledger",
        ["review_action_id"],
        unique=True,
    )

    op.create_table(
        "approvals",
        *_identity_columns(),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("approved_by", sa.String(length=200), nullable=False),
        sa.CheckConstraint("approved_by <> ''", name="approval_approved_by_present"),
        sa.ForeignKeyConstraint(
            ["package_revision_id"],
            ["package_revisions.id"],
            name="fk_approvals_package_revision_id_package_revisions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
        sa.UniqueConstraint("id", "package_revision_id", name="uq_approvals_id_revision"),
    )
    op.create_index("ix_approvals_package_revision_id", "approvals", ["package_revision_id"])

    op.create_table(
        "approved_findings",
        *_identity_columns(),
        sa.Column("approval_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("finding_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        # An approval for package A cannot list a finding from package B. An approval that misstates
        # what it covered is worse than no approval: somebody signed it.
        sa.ForeignKeyConstraint(
            ["approval_id", "package_revision_id"],
            ["approvals.id", "approvals.package_revision_id"],
            name="fk_approved_findings_approval_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id", "package_revision_id"],
            ["findings.id", "findings.package_revision_id"],
            name="fk_approved_findings_finding_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approved_findings"),
        sa.UniqueConstraint("approval_id", "finding_id", name="uq_approved_findings_link"),
    )
    op.create_index("ix_approved_findings_approval_id", "approved_findings", ["approval_id"])
    op.create_index("ix_approved_findings_finding_id", "approved_findings", ["finding_id"])
    op.create_index(
        "ix_approved_findings_package_revision_id", "approved_findings", ["package_revision_id"]
    )

    op.create_table(
        "review_exceptions",
        *_identity_columns(),
        sa.Column("review_action_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("approved_by", sa.String(length=200), nullable=False),
        # NOT NULL is the control: a permanent silent exception must not be representable.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"scope IN ({_SCOPES})", name="review_exception_scope"),
        sa.CheckConstraint("reason <> ''", name="review_exception_reason_present"),
        sa.CheckConstraint("approved_by <> ''", name="review_exception_approved_by_present"),
        sa.CheckConstraint(
            "expires_at > created_at", name="review_exception_expires_after_creation"
        ),
        # An exception hanging off a `confirm` would be a check switched off by a record saying the
        # reviewer agreed with it.
        sa.ForeignKeyConstraint(
            ["review_action_id", "action"],
            ["review_actions.id", "review_actions.action"],
            name="fk_exception_action_kind",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("action = 'except'", name="review_exception_action_is_an_exception"),
        sa.PrimaryKeyConstraint("id", name="pk_review_exceptions"),
    )
    op.create_index(
        "ix_review_exceptions_review_action_id",
        "review_exceptions",
        ["review_action_id"],
        unique=True,
    )
    op.create_index("ix_review_exceptions_scope", "review_exceptions", ["scope"])
    op.create_index(
        "ix_review_exceptions_scope_expiry", "review_exceptions", ["scope", "expires_at"]
    )


def downgrade() -> None:
    op.drop_table("review_exceptions")
    op.drop_table("approved_findings")
    op.drop_table("approvals")
    op.drop_table("correction_ledger")
    op.drop_table("review_actions")
    op.drop_table("review_sessions")
