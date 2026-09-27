"""Let a review-time model call carry its own origin (ADR-0019, #694).

Revision ID: 0049_review_invocation_origin
Revises: 0048_item_classifications

`model_invocations.extraction_run_id` was `NOT NULL`, so a call that is not an extraction had
nowhere to go. Reviewer chat and findings narration were anchored to the package's *latest*
extraction run — which attributed a narration to work that did not make it, and raised outright on a
package that had never been extracted. A reviewer chat on a typed package therefore failed after the
model had already been paid.

So the column becomes nullable, `package_revision_id` joins it, and a CHECK asserts exactly one is
set: never both, which would count one call against two packages, and never neither, which would
leave a paid call attributable to nothing.

**Append-only is preserved.** This adds columns and a constraint; it mutates no row and does not
touch the table's `gv_reject_mutation` trigger.

Source: docs/adr/0019-review-model-invocation-provenance.md, issue #694.
Verification: tests/app/test_review_invocations.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# 29 characters: `alembic_version.version_num` is varchar(32), and the first spelling of this
# (`0049_review_model_invocation_origin`, 35) failed the upgrade on the length rather than on
# anything to do with the migration.
revision: str = "0049_review_invocation_origin"
down_revision: str | None = "0048_item_classifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than held in a constant, deliberately. `tests/app/test_migration_matches_models`
# reads these files as text, and its docstring says why: "a migration name hidden behind a module
# constant is invisible to it… a migration is one fixed historical state and spelling it out is no
# hardship." It caught this file doing exactly that.


def upgrade() -> None:
    op.alter_column(
        "model_invocations", "extraction_run_id", existing_type=sa.Uuid(), nullable=True
    )
    op.add_column(
        "model_invocations", sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_model_invocations_package_revision_id",
        "model_invocations",
        "package_revisions",
        ["package_revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_model_invocations_package_revision_id",
        "model_invocations",
        ["package_revision_id"],
    )
    # Added last, so it is applied to a table whose existing rows already satisfy it: every row
    # written before this migration has an extraction run and no package revision.
    op.create_check_constraint(
        "model_invocation_one_origin",
        "model_invocations",
        "(extraction_run_id IS NULL) <> (package_revision_id IS NULL)",
    )


def downgrade() -> None:
    # **Refuses rather than destroys.** Going back means `extraction_run_id` is NOT NULL again, and
    # any review-time row has none — so the column cannot be restored without either inventing an
    # extraction run for it or deleting a record of a call that really happened and really cost
    # money. `model_invocations` is append-only; a downgrade that silently dropped rows would be the
    # one thing the table exists to prevent.
    bind = op.get_bind()
    orphaned = bind.execute(
        sa.text("SELECT count(*) FROM model_invocations WHERE extraction_run_id IS NULL")
    ).scalar_one()
    if orphaned:
        raise RuntimeError(
            f"{orphaned} model_invocations row(s) belong to a package revision rather than an "
            "extraction run. Downgrading would require deleting them, and this table is "
            "append-only. Decide what to do with them first."
        )
    op.drop_constraint("model_invocation_one_origin", "model_invocations", type_="check")
    op.drop_index("ix_model_invocations_package_revision_id", table_name="model_invocations")
    op.drop_constraint(
        "fk_model_invocations_package_revision_id", "model_invocations", type_="foreignkey"
    )
    op.drop_column("model_invocations", "package_revision_id")
    op.alter_column(
        "model_invocations", "extraction_run_id", existing_type=sa.Uuid(), nullable=False
    )
