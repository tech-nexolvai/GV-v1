"""Match candidates, approved matches and the review trail (#197).

Revision ID: 0010_matching_plane
Revises: 0009_drawing_model

Candidates and approvals are separate tables so that promotion is an insert naming its source, not a
column update. A single table with `approved BOOLEAN` is one careless `UPDATE ... SET` away from
turning every similarity guess in the database into a fact.

The constraint worth reading is `match_approval_deterministic_lane_only`.
`docs/DESIGN_EXTRACTION.md` §8 permits auto-approval on the exact and alias lanes only; the other six
are candidate-only. Without it a dense-vector proposal could be written as `deterministic` and would
be indistinguishable from an exact-identifier match thereafter.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_matching_plane"
down_revision: str | None = "0009_drawing_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported. A migration has to keep saying what it said the day it ran; the
# live enums may gain members, and `tests/db/test_matching_models.py` asserts the two still agree.
_LANES = "'exact', 'alias', 'metadata', 'geometry', 'trigram', 'lexical', 'dense', 'fusion'"
_DETERMINISTIC_LANES = "'alias', 'exact'"
_SOURCES = "'deterministic', 'human'"


def _identity_columns() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "match_candidates",
        *_identity_columns(),
        sa.Column("left_item_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("right_item_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("lane", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Numeric(precision=18, scale=9), nullable=True),
        sa.CheckConstraint(f"lane IN ({_LANES})", name="match_candidate_lane"),
        sa.CheckConstraint("left_item_id <> right_item_id", name="match_candidate_distinct_items"),
        sa.ForeignKeyConstraint(
            ["left_item_id"],
            ["drawing_items.id"],
            name="fk_match_candidates_left_item_id_drawing_items",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["right_item_id"],
            ["drawing_items.id"],
            name="fk_match_candidates_right_item_id_drawing_items",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_match_candidates"),
        sa.UniqueConstraint(
            "left_item_id", "right_item_id", "lane", name="uq_match_candidates_pair_lane"
        ),
    )
    op.create_index("ix_match_candidates_lane", "match_candidates", ["lane"])
    op.create_index("ix_match_candidates_left_item_id", "match_candidates", ["left_item_id"])
    op.create_index(
        "ix_match_candidates_pair", "match_candidates", ["left_item_id", "right_item_id"]
    )
    op.create_index("ix_match_candidates_right_item_id", "match_candidates", ["right_item_id"])

    op.create_table(
        "approved_matches",
        *_identity_columns(),
        sa.Column("match_candidate_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("lane", sa.String(length=32), nullable=False),
        sa.Column("approval_source", sa.String(length=32), nullable=False),
        sa.Column("approved_by", sa.String(length=200), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=1000), nullable=True),
        sa.CheckConstraint(f"approval_source IN ({_SOURCES})", name="match_approval_source"),
        sa.CheckConstraint(f"lane IN ({_LANES})", name="match_approval_lane"),
        sa.CheckConstraint(
            f"approval_source <> 'deterministic' OR lane IN ({_DETERMINISTIC_LANES})",
            name="match_approval_deterministic_lane_only",
        ),
        sa.CheckConstraint("approved_by <> ''", name="match_approval_approved_by_present"),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoked_reason IS NULL)"
            " OR (revoked_at IS NOT NULL AND revoked_reason IS NOT NULL AND revoked_reason <> '')",
            name="match_approval_revocation_is_explained",
        ),
        sa.ForeignKeyConstraint(
            ["match_candidate_id"],
            ["match_candidates.id"],
            name="fk_approved_matches_match_candidate_id_match_candidates",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approved_matches"),
    )
    op.create_index("ix_approved_matches_approval_source", "approved_matches", ["approval_source"])
    op.create_index(
        "ix_approved_matches_match_candidate_id",
        "approved_matches",
        ["match_candidate_id"],
        unique=True,
    )

    op.create_table(
        "match_review_events",
        *_identity_columns(),
        sa.Column("match_candidate_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("reviewer", sa.String(length=200), nullable=False),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.CheckConstraint("action <> ''", name="match_review_event_action_present"),
        sa.CheckConstraint("reviewer <> ''", name="match_review_event_reviewer_present"),
        sa.ForeignKeyConstraint(
            ["match_candidate_id"],
            ["match_candidates.id"],
            name="fk_match_review_events_match_candidate_id_match_candidates",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_match_review_events"),
    )
    op.create_index(
        "ix_match_review_events_candidate_created",
        "match_review_events",
        ["match_candidate_id", "created_at"],
    )
    op.create_index(
        "ix_match_review_events_match_candidate_id", "match_review_events", ["match_candidate_id"]
    )


def downgrade() -> None:
    op.drop_table("match_review_events")
    op.drop_table("approved_matches")
    op.drop_table("match_candidates")
