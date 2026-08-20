"""Let a state event name the run that caused it, and refuse a nameless actor (#210).

Revision ID: 0016_state_event_workflow_run
Revises: 0015_model_invocation_failed

`package_state_events` answers *"what happened to this package, and when?"* — the question asked when a
review is disputed months later. Two things stopped it answering fully.

**No link to the workflow run.** C3.2's scope says record the run that caused the transition, and there
was nowhere to put it. `workflow_run_id` is nullable because the null means something: nothing ran. A
reviewer approving a package, and a revision's own genesis event, are transitions with no workflow
behind them, so a non-nullable column would have to be filled with something untrue. A foreign key
rather than an id inside the `reason` prose, because an id in prose cannot be joined or constrained and
a dispute would come down to a `LIKE` query.

**A nameless actor was storable.** `actor` was `NOT NULL` with no non-empty check, so `''` satisfied the
schema while naming nobody — and every event naming an actor is the acceptance criterion. The check
brings this table in line with `model_invocation_model_id`, `outbox_entry_workflow` and the others.

**Adding the check re-validates the table**, so this migration will refuse if any event already carries
a blank actor. That is the right failure: the alternative is editing audit rows so a schema change can
succeed, and `package_state_events` is append-only precisely so that cannot happen. An operator meeting
that refusal has rows to explain, not a migration to force.

Source: backend proposal §9.1 · Design: `docs/DESIGN_PLATFORM.md` §5.
Verification: `tests/lifecycle/test_events.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_state_event_workflow_run"
down_revision: str | None = "0015_model_invocation_failed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Written as literals rather than module constants, and that is not just style. Two of this repo's
#: checks read migrations as text — `tests/app/test_migration_matches_models.py` walks the AST to
#: compare declared columns against the models — and a name behind a constant is a name a static
#: reader cannot resolve. It also suits what a migration is: one fixed historical state, spelled out.


def upgrade() -> None:
    """Add the nullable run link and refuse a blank actor."""
    op.add_column(
        "package_state_events", sa.Column("workflow_run_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_package_state_events_workflow_run_id_workflow_runs",
        "package_state_events",
        "workflow_runs",
        ["workflow_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # Indexed because the question it answers is "what did this run do?", which reads by run rather
    # than by revision — the direction the existing indexes do not serve.
    op.create_index(
        "ix_package_state_events_workflow_run_id",
        "package_state_events",
        ["workflow_run_id"],
    )
    op.create_check_constraint("package_event_actor", "package_state_events", "actor <> ''")


def downgrade() -> None:
    """Drop both. Losing the link loses which run caused which transition, permanently."""
    op.drop_constraint("package_event_actor", "package_state_events", type_="check")
    op.drop_index("ix_package_state_events_workflow_run_id", table_name="package_state_events")
    op.drop_constraint(
        "fk_package_state_events_workflow_run_id_workflow_runs",
        "package_state_events",
        type_="foreignkey",
    )
    op.drop_column("package_state_events", "workflow_run_id")
