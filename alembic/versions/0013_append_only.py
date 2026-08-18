"""Make append-only actually append-only (#202).

Revision ID: 0013_append_only
Revises: 0012_review_plane

Twenty-eight tables carry the `Immutable` marker, and until this migration the marker was enforced by
nothing at all: an `UPDATE` against any of them succeeded. The isolation guard and the licence guard
both work because they fail rather than asking people to remember, and immutability deserves the
same.

**A trigger, not `REVOKE`.** The story's plan proposed revoking `UPDATE, DELETE` from `gv_app` and
`gv_worker`. Neither role exists, and CI and local development connect as the database owner —
`REVOKE` does not restrict an owner or a superuser, so the revoke would have run, the test would have
attempted an update, and it would have **succeeded**. The guard would have been decoration.

**What this does and does not stop, precisely.** The trigger refuses every ordinary `UPDATE` and
`DELETE`: application code, a psql session, an ORM flush, a well-meant data fix. It does **not** make
the tables tamper-proof, and it is worth being exact rather than letting "append-only" be read as
more than it is:

* a table **owner** can `ALTER TABLE ... DISABLE TRIGGER` or drop it outright;
* a **superuser** can set `session_replication_role = replica` and bypass user triggers entirely.

Both require deliberate action by a role that can already rewrite the schema, so this is the boundary
of what a trigger can offer — not a hole in this migration. Closing it needs the application to
connect as a role that owns nothing and holds only `INSERT` and `SELECT`, which is the grant half of
`C1.12` and is deferred because no such role exists yet and nothing connects as one. Inventing roles
nothing uses would be the same mistake as the `REVOKE` above: a control that looks enforced and is
not.

`tests/db/test_append_only.py` demonstrates the owner bypass rather than asserting it is impossible,
so the limit is recorded where somebody will meet it.

**Schema changes still work.** A row trigger fires on `UPDATE` and `DELETE` of *rows*. `ALTER TABLE`,
`DROP TABLE` and every other DDL are untouched, so migrations that legitimately change these tables
are unaffected.

The table list is written out rather than imported from `app.db.base`. A migration has to keep saying
what it said the day it ran, and a list computed from live metadata would silently change meaning as
tables are added. `tests/db/test_append_only.py` asserts this list still equals the live one.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_append_only"
down_revision: str | None = "0012_review_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table carrying `Immutable` when this migration was written.
IMMUTABLE_TABLES: tuple[str, ...] = (
    "aliases",
    "approvals",
    "approved_findings",
    "approved_matches",
    "canonical_observations",
    "case_results",
    "correction_ledger",
    "document_versions",
    "evaluation_runs",
    "evidence_artifacts",
    "evidence_corroboration_lanes",
    "evidence_supporting_candidates",
    "finding_evidence",
    "findings",
    "gold_cases",
    "match_candidates",
    "match_review_events",
    "metric_results",
    "model_invocations",
    "observation_candidates",
    "package_state_events",
    "pages",
    "review_actions",
    "review_exceptions",
    "rule_applicability_scopes",
    "rule_snapshots",
    "source_artifacts",
    "verdict_inputs",
)

REJECT_FUNCTION = """
CREATE OR REPLACE FUNCTION gv_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'table % is append-only: % is not permitted. A correction is a new row.',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(REJECT_FUNCTION)
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
        )


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS gv_reject_mutation()")
