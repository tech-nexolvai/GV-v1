"""A RUN parameter set belongs to one package revision, not to the whole project (#801).

Revision ID: 0054_run_set_names_its_revision
Revises: 0053_invocation_cost_unknown

A RUN set holds what was true for one review: the sink's cut-sheet size and the measurements typed
for one package. It was keyed by project alone, so every package in a project shared one set — the
newest — and package A's checks ran on package B's numbers. This adds `package_revision_id` and a
check that every **new** RUN set names one and no other layer does.

**`NOT VALID`, and why.** RUN rows written before this cannot be given a revision: nothing records
which package they were typed for, and guessing would attach one package's numbers to another, which
is the defect being fixed. `parameter_sets` is append-only besides. So the check binds every new row
and exempts the old ones, and the readers filter by revision, so an old RUN row is read by nothing.
Its values have to be typed again, which on the databases that exist (local and demo) is the honest
cost of not guessing.

Source: issue #801. Verification: tests/api/test_measurements.py, tests/db/test_parameter_models.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0054_run_set_names_its_revision"
down_revision: str | None = "0053_invocation_cost_unknown"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "parameter_sets",
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_parameter_sets_package_revision_id_package_revisions",
        "parameter_sets",
        "package_revisions",
        ["package_revision_id"],
        ["id"],
        # RESTRICT, as for `project_id`: a finding cites the set that judged it, and the set must not
        # disappear with the revision record.
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_parameter_sets_package_revision_id", "parameter_sets", ["package_revision_id"]
    )
    op.execute(
        "ALTER TABLE parameter_sets ADD CONSTRAINT ck_parameter_sets_run_names_its_revision "
        "CHECK ((layer = 'run') = (package_revision_id IS NOT NULL)) NOT VALID"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE parameter_sets DROP CONSTRAINT ck_parameter_sets_run_names_its_revision"
    )
    op.drop_index("ix_parameter_sets_package_revision_id", table_name="parameter_sets")
    op.drop_constraint(
        "fk_parameter_sets_package_revision_id_package_revisions",
        "parameter_sets",
        type_="foreignkey",
    )
    op.drop_column("parameter_sets", "package_revision_id")
