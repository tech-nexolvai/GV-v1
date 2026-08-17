"""Views, items, printed identifiers and the alias table (#196).

Revision ID: 0009_drawing_model
Revises: 0008_case_results

A view is identified by `(page_id, tag)`, never the tag alone — sheets reuse `D`, `E`, `F` page after
page, and a schema trusting the tag would merge two elevations from different sheets into one view.

`item_identifiers.value_as_printed` is deliberately **not** unique. Real packages reuse marks, and a
unique index would refuse the drawing rather than the ambiguity. The drawing is the fact;
`duplicate_identifiers()` reports them so a reviewer decides.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_drawing_model"
down_revision: str | None = "0008_case_results"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _identity_columns() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "drawing_views",
        *_identity_columns(),
        sa.Column("page_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tag", sa.String(length=50), nullable=False),
        sa.Column("region", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("tag <> ''", name="drawing_view_tag_present"),
        sa.ForeignKeyConstraint(
            ["page_id"], ["pages.id"], name="fk_drawing_views_page_id_pages", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_drawing_views"),
        sa.UniqueConstraint("page_id", "tag", name="uq_drawing_views_page_tag"),
    )
    op.create_index("ix_drawing_views_page_id", "drawing_views", ["page_id"])

    op.create_table(
        "drawing_items",
        *_identity_columns(),
        sa.Column("drawing_view_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("item_type", sa.String(length=100), nullable=False),
        sa.Column("extent", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("corroborated", sa.Boolean(), nullable=False),
        sa.CheckConstraint("item_type <> ''", name="drawing_item_type_present"),
        sa.ForeignKeyConstraint(
            ["drawing_view_id"],
            ["drawing_views.id"],
            name="fk_drawing_items_drawing_view_id_drawing_views",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_drawing_items"),
    )
    op.create_index("ix_drawing_items_drawing_view_id", "drawing_items", ["drawing_view_id"])
    op.create_index("ix_drawing_items_item_type", "drawing_items", ["item_type"])
    op.create_index("ix_drawing_items_view_type", "drawing_items", ["drawing_view_id", "item_type"])

    op.create_table(
        "item_identifiers",
        *_identity_columns(),
        sa.Column("drawing_item_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("value_as_printed", sa.String(length=200), nullable=False),
        sa.CheckConstraint("kind <> ''", name="item_identifier_kind_present"),
        sa.CheckConstraint("value_as_printed <> ''", name="item_identifier_value_present"),
        sa.ForeignKeyConstraint(
            ["drawing_item_id"],
            ["drawing_items.id"],
            name="fk_item_identifiers_drawing_item_id_drawing_items",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_item_identifiers"),
    )
    op.create_index("ix_item_identifiers_drawing_item_id", "item_identifiers", ["drawing_item_id"])
    op.create_index(
        "ix_item_identifiers_kind_value", "item_identifiers", ["kind", "value_as_printed"]
    )
    op.create_index(
        "ix_item_identifiers_value_as_printed", "item_identifiers", ["value_as_printed"]
    )

    op.create_table(
        "aliases",
        *_identity_columns(),
        sa.Column("spelling", sa.String(length=200), nullable=False),
        sa.Column("canonical_term", sa.String(length=200), nullable=False),
        sa.Column("added_by", sa.String(length=200), nullable=False),
        sa.Column("rationale", sa.String(length=1000), nullable=False),
        sa.Column("rulebook_version", sa.String(length=50), nullable=False),
        sa.CheckConstraint("spelling <> ''", name="alias_spelling_present"),
        sa.CheckConstraint("canonical_term <> ''", name="alias_canonical_term_present"),
        sa.CheckConstraint("added_by <> ''", name="alias_added_by_present"),
        sa.CheckConstraint("rationale <> ''", name="alias_rationale_present"),
        sa.PrimaryKeyConstraint("id", name="pk_aliases"),
        sa.UniqueConstraint(
            "spelling",
            "canonical_term",
            "rulebook_version",
            name="uq_aliases_spelling_term_version",
        ),
    )
    op.create_index("ix_aliases_canonical_term", "aliases", ["canonical_term"])
    op.create_index("ix_aliases_rulebook_version", "aliases", ["rulebook_version"])
    op.create_index("ix_aliases_spelling", "aliases", ["spelling"])


def downgrade() -> None:
    op.drop_table("aliases")
    op.drop_table("item_identifiers")
    op.drop_table("drawing_items")
    op.drop_table("drawing_views")
