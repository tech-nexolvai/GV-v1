"""A reading's live activity, one coded line at a time (#1238), and the `try_again` stage.

Revision ID: 0096_reading_events
Revises: 0094_invocation_failure_reason

**Why.** While a package is read the screen could say only "page k of N": the progress row
(`0087`) is overwritten in place, and the inspectors' results (#1228, `pariksha:` flags) are written
only when the stage commits. So a reviewer could not see, while it happened, that an agent's two
AIs split on a page or that its inspector stopped an item. Anant approved one small append-only
table for this (2026-10-14, phase 3 decision 5).

**`reading_events`.** One row per event, written by the worker in its own short side transaction
(`app.runs.reading_activity.record_events`): the revision, the reading (one id per stage attempt),
the event's place in its batch, the page, the agent, the item (a code question id such as
`p11:row-choice`), the kind (`answered`, `passed`, `stopped`, `trying_again`, `try_again_result`),
a code, what a try-again changed and which try it is (1 to 3, phase 3 decision 4). **Codes only,
never drawing text**: the CHECKs below refuse anything that is not a short lower-case code, so no
printed value or page title can be stored. The words a person reads are made from the codes in
code.

* **Append-only**, with the same trigger as every evidence table; grants are derived from
  `ROLE_GRANTS` (`SELECT`, `INSERT`).
* **No foreign key to `package_revisions`**, for the same reason as `reading_progress`: a running
  stage holds its revision row locked, and a foreign-key check from the side transaction would wait
  for the stage to end.

**`try_again`.** The reading stage list gains `try_again` (phase 3, PR B asks its try-again questions
in a batch of that name), so `reading_progress_stage` is widened to allow it.

**Downgrade.** The events are telemetry, not a record anyone signs (nothing decides anything from
them), so the table is dropped. A progress row that says `try_again` is a reading in progress, and
the narrower check cannot hold it, so such rows are deleted first (the screen then says less until
the next progress line).

**Chaining.** Written on main's head `0094_invocation_failure_reason`. Phase 4's open PR #1236 adds
`0095_part_proposal_rows` on the same head; whichever merges second is re-chained onto the other
(one line, `down_revision`).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0096_reading_events"
down_revision: str | None = "0094_invocation_failure_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "reading_events"
IMMUTABLE_TABLES = (TABLE,)

#: Written out here, not imported from the model: a migration must do the same thing whenever it runs.
_KINDS = "'answered', 'passed', 'stopped', 'trying_again', 'try_again_result'"
_AGENTS = "'Bala', 'Rekha', 'Akshara', 'Vastu', 'Milan', 'Kautilya', 'Nina', 'Vani'"
_ITEM = "^p[0-9]+:[a-z0-9:-]+$"
_CODE = "^[a-z0-9]+(-[a-z0-9]+)*$"

_STAGES_BEFORE = (
    "waiting",
    "ingest",
    "pages",
    "architect",
    "preparing",
    "rows",
    "dimensions",
    "chains",
    "architect_pairing",
    "whole_pages",
    "saving",
    "match",
    "validate",
    "form",
)
_STAGES_AFTER = (*_STAGES_BEFORE, "try_again")
_STAGE_CHECK = "reading_progress_stage"


def _stage_check(stages: Sequence[str]) -> str:
    return "stage IN (" + ", ".join(f"'{stage}'" for stage in stages) + ")"


def _in_current_schema(*statements: str) -> str:
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


def upgrade() -> None:
    """Create the append-only events table and allow the `try_again` reading stage."""
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reading_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("agent", sa.String(length=16), nullable=False),
        sa.Column("item", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("change", sa.String(length=48), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.CheckConstraint(f"kind IN ({_KINDS})", name="reading_event_kind"),
        sa.CheckConstraint(f"agent IN ({_AGENTS})", name="reading_event_agent"),
        sa.CheckConstraint(f"item ~ '{_ITEM}'", name="reading_event_item"),
        sa.CheckConstraint(f"code ~ '{_CODE}'", name="reading_event_code"),
        sa.CheckConstraint(f"change IS NULL OR change ~ '{_CODE}'", name="reading_event_change"),
        sa.CheckConstraint("page_number >= 1", name="reading_event_page_number"),
        sa.CheckConstraint("position >= 0", name="reading_event_position"),
        sa.CheckConstraint(
            "attempt IS NULL OR (attempt >= 1 AND attempt <= 3)", name="reading_event_attempt"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reading_events_reading_id", TABLE, ["reading_id"])
    op.create_index(
        "ix_reading_events_revision_newest", TABLE, ["package_revision_id", "created_at"]
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_append_only "
        f"BEFORE UPDATE OR DELETE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
    )
    grant_statements = [
        f'GRANT {", ".join(privileges)} ON TABLE %I."{table}" TO {role.value}'
        for role, grants in ROLE_GRANTS.items()
        for table, privileges in grants.privileges.items()
        if table in IMMUTABLE_TABLES
    ]
    if grant_statements:
        op.get_bind().execute(sa.text(_in_current_schema(*grant_statements)))
    op.drop_constraint(_STAGE_CHECK, "reading_progress", type_="check")
    op.create_check_constraint(_STAGE_CHECK, "reading_progress", _stage_check(_STAGES_AFTER))


def downgrade() -> None:
    """Drop the events (telemetry) and narrow the stage check again (see the docstring)."""
    op.execute("DELETE FROM reading_progress WHERE stage = 'try_again'")
    op.drop_constraint(_STAGE_CHECK, "reading_progress", type_="check")
    op.create_check_constraint(_STAGE_CHECK, "reading_progress", _stage_check(_STAGES_BEFORE))
    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_append_only ON {TABLE}")
    op.drop_index("ix_reading_events_revision_newest", table_name=TABLE)
    op.drop_index("ix_reading_events_reading_id", table_name=TABLE)
    op.drop_table(TABLE)
