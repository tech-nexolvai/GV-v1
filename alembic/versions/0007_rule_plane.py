"""Rule definitions, content-addressed snapshots and applicability scopes (#198).

Revision ID: 0007_rule_plane
Revises: 0006_evidence_plane

Applicability is a child table rather than a column per discriminator. ADR-0007 rejected the column
form, and in a database it is worse than in a signature: `AGENTS.md` forbids editing a shipped
migration, so every new discriminator would need a fresh one before a rule keyed on it could be
stored at all.

The reserved-discriminator CHECK is written out literally here rather than generated. A migration has
to keep saying what it said the day it ran — importing the live `RESERVED_DISCRIMINATORS` would make
this file's meaning change when that set changes, which is the opposite of what a migration is for.
`tests/db/test_rule_models.py` asserts the model and the set still agree, so drift surfaces there.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_rule_plane"
down_revision: str | None = "0006_evidence_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESERVED = (
    "'fabricator', 'submitter', 'supplier', 'supplier_id', 'vendor', 'vendor_id', 'vendor_name'"
)


def _identity_columns() -> tuple[sa.Column[object], sa.Column[object]]:
    """The `TimestampedUUID` pair, in one place. Written out by hand on the first attempt at this
    migration, which is how it grew an `updated_at` the mixin does not supply — a NOT NULL column
    nothing ever wrote, so every insert failed."""
    return (
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "rule_definitions",
        *_identity_columns(),
        sa.Column("rule_id", sa.String(length=200), nullable=False),
        sa.CheckConstraint("rule_id <> ''", name="definition_rule_id_present"),
        sa.PrimaryKeyConstraint("id", name="pk_rule_definitions"),
    )
    op.create_index("ix_rule_definitions_rule_id", "rule_definitions", ["rule_id"], unique=True)

    op.create_table(
        "rule_snapshots",
        *_identity_columns(),
        sa.Column("rule_definition_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("canonical_json", sa.String(), nullable=False),
        sa.Column("product_type", sa.String(length=100), nullable=False),
        sa.Column("check_type", sa.String(length=100), nullable=False),
        sa.Column("unconfirmed_tolerance_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("snapshot_id LIKE 'sha256:%'", name="snapshot_id_is_prefixed"),
        sa.CheckConstraint("canonical_json <> ''", name="snapshot_canonical_json_present"),
        sa.CheckConstraint("version <> ''", name="snapshot_version_present"),
        sa.CheckConstraint("product_type <> ''", name="snapshot_product_type_present"),
        sa.CheckConstraint(
            "unconfirmed_tolerance_count >= 0",
            name="snapshot_unconfirmed_count_not_negative",
        ),
        sa.ForeignKeyConstraint(
            ["rule_definition_id"],
            ["rule_definitions.id"],
            name="fk_rule_snapshots_rule_definition_id_rule_definitions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_rule_snapshots"),
        sa.UniqueConstraint(
            "rule_definition_id", "version", name="uq_rule_snapshots_definition_version"
        ),
    )
    op.create_index("ix_rule_snapshots_product_type", "rule_snapshots", ["product_type"])
    op.create_index(
        "ix_rule_snapshots_rule_definition_id", "rule_snapshots", ["rule_definition_id"]
    )
    op.create_index("ix_rule_snapshots_snapshot_id", "rule_snapshots", ["snapshot_id"], unique=True)

    op.create_table(
        "rule_applicability_scopes",
        *_identity_columns(),
        sa.Column("rule_snapshot_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("discriminator", sa.String(length=100), nullable=False),
        sa.Column("value", sa.String(length=200), nullable=False),
        sa.CheckConstraint(
            "discriminator <> ''",
            name="scope_discriminator_present",
        ),
        sa.CheckConstraint(
            f"discriminator NOT IN ({_RESERVED})",
            name="scope_discriminator_not_reserved",
        ),
        sa.CheckConstraint("value <> ''", name="scope_value_present"),
        sa.ForeignKeyConstraint(
            ["rule_snapshot_id"],
            ["rule_snapshots.id"],
            name="fk_rule_applicability_scopes_rule_snapshot_id_rule_snapshots",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_rule_applicability_scopes"),
        sa.UniqueConstraint(
            "rule_snapshot_id",
            "discriminator",
            "value",
            name="uq_rule_applicability_scopes_snapshot_discriminator_value",
        ),
    )
    op.create_index(
        "ix_rule_applicability_scopes_discriminator", "rule_applicability_scopes", ["discriminator"]
    )
    op.create_index(
        "ix_rule_applicability_scopes_lookup",
        "rule_applicability_scopes",
        ["discriminator", "value"],
    )
    op.create_index(
        "ix_rule_applicability_scopes_rule_snapshot_id",
        "rule_applicability_scopes",
        ["rule_snapshot_id"],
    )
    op.create_index("ix_rule_applicability_scopes_value", "rule_applicability_scopes", ["value"])


def downgrade() -> None:
    op.drop_table("rule_applicability_scopes")
    op.drop_table("rule_snapshots")
    op.drop_table("rule_definitions")
