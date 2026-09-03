"""Mark a check run superseded when the checks are run again (#477).

Revision ID: 0028_check_run_supersession
Revises: 0027_parameter_sets

Findings are immutable and a re-run writes new ones — `#199` settled that: *"a re-run produces a new
finding linked to a new check run"*. What nothing settled is which set a reviewer is looking at. The
findings query filters by package revision, so a second run of the same rules would put two copies of
every finding in front of the reviewer with nothing to say which was current.

That is worse than it sounds. The pair would usually agree, so it would read as duplication rather
than as ambiguity — right up to the run where a rulebook fix changed a verdict, and the screen then
shows a PASS and a FAIL for the same check with equal authority.

**Annotating the run rather than the findings.** `check_runs` is deliberately not `Immutable`
(`app/models/verdicts.py`): *"a run is a process record and may be annotated as it completes"*. The
findings stay untouched and every one ever written is still there — this only records which run has
been replaced, which is a fact about the process and not about the verdict.

**Nullable, and `NULL` is the live state.** Existing rows are live and need no backfill. A boolean
would have needed one, and would have said less: the timestamp answers *when* a run stopped being
current, which is what somebody reconstructing a review months later has to know.

The partial index covers the only query anyone runs — the live findings for a revision. Indexing the
superseded rows too would be paying for a lookup nothing performs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_check_run_supersession"
down_revision: str | None = "0027_parameter_sets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "check_runs",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_check_runs_live_by_revision",
        "check_runs",
        ["package_revision_id"],
        unique=False,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_check_runs_live_by_revision", table_name="check_runs")
    op.drop_column("check_runs", "superseded_at")
