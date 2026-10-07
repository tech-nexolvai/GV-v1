"""Let a reading be a countertop piece's width, whatever kind the piece is (#991).

Revision ID: 0072_countertop_piece_width
Revises: 0071_invocation_raw_response

`CT-WIDTH-001` 1.1.0 adds up every piece in a countertop's row (admin, 2026-10-07), read as the new
semantic type `countertop_piece_width`. `0006_evidence_plane` fixed the types a reading may carry in
two CHECK constraints, so without this a sealed piece reading could not be stored at all: PostgreSQL
would refuse the insert and the width check would wait for ever on a reading that was taken.

`cabinet_category` is added too. The ORM has allowed it since #681 (`app/models/evidence.py` builds
its constraint from the enum), but no migration ever did, so a database built by migrations and one
built from the models disagreed about it. Widening only — no stored row can violate the new list.

**The values are written out, not imported**, for the reason `0015_model_invocation_failed` gives: a
migration describes one fixed state, and reading the live enum would make this file describe a
different one whenever the enum changed. `tests/db/test_semantic_type_constraint.py` reads the
constraint back out of a migrated database and compares it with the enum, which is what catches drift.

Source: #991. Verification: `tests/db/test_semantic_type_constraint.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0072_countertop_piece_width"
down_revision: str | None = "0071_invocation_raw_response"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Exactly what `0006` installed, so `downgrade` restores it rather than something resembling it.
SEMANTIC_TYPES_BEFORE = (
    "'CT001', 'CT002', 'CT003', 'CT004', 'CT005', 'CT006', 'CT007', 'CT008', "
    "'CT009', 'CT010', 'CT011', 'CT012', 'CT013', 'B.S_THK', 'C.T_OH', "
    "'CAB_SIDE_THK', 'cabinet_width', 'filler_width', 'countertop_overall_width', "
    "'wall_config', 'field_dimension', 'material'"
)

#: The types after this migration.
SEMANTIC_TYPES = SEMANTIC_TYPES_BEFORE + ", 'countertop_piece_width', 'cabinet_category'"

#: Each constraint, its table, and the condition with a placeholder for the list.
CONSTRAINTS = (
    (
        "canonical_observation_semantic_type",
        "canonical_observations",
        "semantic_type IN ({})",
    ),
    (
        "observation_candidate_semantic_guess",
        "observation_candidates",
        "semantic_guess IS NULL OR semantic_guess IN ({})",
    ),
)


def upgrade() -> None:
    """Widen both constraints. Dropped and recreated: PostgreSQL cannot alter a CHECK in place."""
    for name, table, condition in CONSTRAINTS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, condition.format(SEMANTIC_TYPES))


def downgrade() -> None:
    """Narrow both back to `0006`'s list.

    **Fails while a piece or category reading exists, deliberately.** Recreating the narrower check
    re-validates the table, and the alternative — deleting readings so a schema change is tidy —
    would destroy append-only evidence a finding may cite.
    """
    for name, table, condition in CONSTRAINTS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, condition.format(SEMANTIC_TYPES_BEFORE))
