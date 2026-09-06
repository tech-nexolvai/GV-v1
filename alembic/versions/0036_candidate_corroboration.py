"""Record what a corroboration lane made of a raw reading (#528).

Revision ID: 0036_candidate_corroboration
Revises: 0035_audit_whitespace_regex

A drawing that writes `984 [38 3/4]` states one dimension twice, in two units, in one token. Comparing
the two corroborates that the inch reading was *read* correctly — one of only two ways a single reader
can qualify evidence, and the one `AGENTS.md` means when it says millimetres "may still corroborate
that an inch was read correctly".

Everything that decides agreement was already built and had no caller: `units/dual.py:parse_dual`,
`units/policy.py:check_dual`, `evidence/corroborate.py`. This gives their answer somewhere to live.

**On the candidate, not on a canonical observation.** `evidence_corroboration_lanes` already exists
and is the wrong table for this: it hangs off a canonical observation, which requires a semantic type,
and this lane says nothing about meaning. It compares two readings inside one token and reports
whether the drawing agrees with itself — a fact about a raw reading, available long before anybody
knows what the reading is *of*.

**Both columns or neither.** A lane with no finding, or a finding from no lane, is a row nobody can
interpret. The pairing is a check constraint rather than a convention, for the same reason
`extraction_failures` pairs its reason with its page index.

Nullable, and null is the common case: every candidate whose token states one reading. Nothing is
backfilled — a candidate recorded before this migration was never examined by a lane, and writing
`RAW_CANDIDATE` into those rows would assert that it had been.

Source: issue #528. Verification: tests/db/test_evidence_models.py, tests/evidence/test_dual_lane.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036_candidate_corroboration"
down_revision: str | None = "0035_audit_whitespace_regex"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Written out rather than imported: a migration has to keep saying what it said the day it ran.
STATUSES: tuple[str, ...] = (
    "RAW_CANDIDATE",
    "CORROBORATED",
    "HUMAN_CONFIRMED",
    "CONFLICTING",
    "REJECTED",
)

LANES: tuple[str, ...] = ("SECOND_READER", "DUAL_UNIT", "HUMAN")


def _values(members: tuple[str, ...]) -> str:
    return ", ".join(f"'{member}'" for member in members)


def upgrade() -> None:
    op.add_column(
        "observation_candidates",
        sa.Column("corroboration_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "observation_candidates",
        sa.Column("corroboration_lane", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "candidate_corroboration_status",
        "observation_candidates",
        f"corroboration_status IS NULL OR corroboration_status IN ({_values(STATUSES)})",
    )
    op.create_check_constraint(
        "candidate_corroboration_lane",
        "observation_candidates",
        f"corroboration_lane IS NULL OR corroboration_lane IN ({_values(LANES)})",
    )
    op.create_check_constraint(
        "candidate_corroboration_paired",
        "observation_candidates",
        "(corroboration_status IS NULL AND corroboration_lane IS NULL) OR "
        "(corroboration_status IS NOT NULL AND corroboration_lane IS NOT NULL)",
    )


def downgrade() -> None:
    """Drops the columns, and with them every conflict a lane found."""
    for constraint in (
        "candidate_corroboration_paired",
        "candidate_corroboration_lane",
        "candidate_corroboration_status",
    ):
        op.drop_constraint(constraint, "observation_candidates", type_="check")
    op.drop_column("observation_candidates", "corroboration_lane")
    op.drop_column("observation_candidates", "corroboration_status")
