"""Let a finding carry its reason, its delta, its variant and its notes (#521).

Revision ID: 0033_finding_provenance
Revises: 0032_digest_mismatch_failure

`verdict.finding.Finding` has always carried four things this table did not, and `record_finding`
dropped them on the way in. The cost became visible when #519 built a deliverable from stored rows:
four columns of every workbook said `not recorded in the database`, because they could not be filled.

- `reason` — the sentence a reviewer reads. An abstention's was already stored inside its trace; a
  decision's was lost, so the export could explain why a check did not decide and not why it did.
- `delta` — how far out a FAIL was. A reviewer triaging twenty failures needs to know which is a
  sixteenth of an inch and which is two inches.
- `variant` — which applicability branch applied. Two findings from one rule under different variants
  are not comparable, and nothing said which was which.
- `notes` — what did not change the outcome but a reviewer should see. Easy to drop precisely because
  it does not move the verdict.

**The delta is three columns, not one.** Numerator, denominator and unit, the shape `verdict_inputs`
already uses. ADR-0001: never a float in the verdict path, and a decimal column here would answer
"how far out?" with a number the arithmetic never produced.

**Every column is nullable, and `NULL` means "nobody recorded this".** Every finding written before
this migration genuinely has no reason, and backfilling `''` would assert the engine produced an empty
one. For `notes` the distinction is wider still: `NULL` is unknown, `[]` is "the check ran and had
nothing to add".

**`findings` is append-only and stays that way.** This adds columns; it does not touch a row. Old
findings keep their nulls rather than being rewritten, which is what append-only means.

Source: issue #521. Verification: tests/db/test_verdict_models.py, tests/workflow/test_generate_outputs.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0033_finding_provenance"
down_revision: str | None = "0032_digest_mismatch_failure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The units a delta may be expressed in. Written out rather than imported, because a migration has
#: to keep saying what it said the day it ran.
UNITS: tuple[str, ...] = ("in", "mm")


def upgrade() -> None:
    op.add_column("findings", sa.Column("reason", sa.Text(), nullable=True))
    op.add_column("findings", sa.Column("delta_numerator", sa.BigInteger(), nullable=True))
    op.add_column("findings", sa.Column("delta_denominator", sa.BigInteger(), nullable=True))
    op.add_column("findings", sa.Column("delta_unit", sa.String(length=16), nullable=True))
    op.add_column("findings", sa.Column("variant", sa.String(length=100), nullable=True))
    op.add_column("findings", sa.Column("notes", JSONB(), nullable=True))

    op.create_check_constraint(
        "finding_delta_complete",
        "findings",
        "(delta_numerator IS NULL AND delta_denominator IS NULL AND delta_unit IS NULL) OR "
        "(delta_numerator IS NOT NULL AND delta_denominator IS NOT NULL AND delta_unit IS NOT NULL)",
    )
    op.create_check_constraint(
        "finding_delta_denominator",
        "findings",
        "delta_denominator IS NULL OR delta_denominator > 0",
    )
    units = ", ".join(f"'{unit}'" for unit in UNITS)
    op.create_check_constraint(
        "finding_delta_unit", "findings", f"delta_unit IS NULL OR delta_unit IN ({units})"
    )
    op.create_check_constraint("finding_reason", "findings", "reason IS NULL OR reason <> ''")
    op.create_check_constraint("finding_variant", "findings", "variant IS NULL OR variant <> ''")


def downgrade() -> None:
    """Drops the columns, and with them what they held.

    Named rather than left to inference: a downgrade here discards the reasons behind every finding
    written since the upgrade. That is the honest consequence of removing a column, and there is no
    way to preserve it in a table that no longer has anywhere to put it.
    """
    for constraint in (
        "finding_variant",
        "finding_reason",
        "finding_delta_unit",
        "finding_delta_denominator",
        "finding_delta_complete",
    ):
        op.drop_constraint(constraint, "findings", type_="check")
    for column in (
        "notes",
        "variant",
        "delta_unit",
        "delta_denominator",
        "delta_numerator",
        "reason",
    ):
        op.drop_column("findings", column)
