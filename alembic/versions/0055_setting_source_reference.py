"""A setting records where it came from: one new source word, and a short reference (#827).

Revision ID: 0055_setting_source_reference
Revises: 0054_run_set_names_its_revision

A reviewer's overhang was stored as `Measured`, the only word the form could record, though it came
from the architect's drawings or the project spec. The form now asks where each setting came from, and
this stores the answer:

* **`Fabricator` joins the source vocabulary.** The check constraint is replaced by one that also
  admits it. Every existing row already satisfies the new constraint, which only widens the old one.
* **`source_reference`**: where in that source the number is, in the reviewer's words. Nullable, so
  every row written before this reads exactly as it did, and blank is refused — a citation that
  points nowhere reads as one.

Both are additive. Nothing stored changes meaning, and no set's content hash moves: the reference
enters the hash only when one is given (`rules/parameters.py:ParameterValue.canonical_form`).

Source: issue #827. Verification: tests/db/test_parameter_models.py, tests/api/test_measurements.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0055_setting_source_reference"
down_revision: str | None = "0054_run_set_names_its_revision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Written out rather than imported: a migration records the vocabulary as it stood when it ran, and
#: an import would silently rewrite this one the next time the enum changed.
_BEFORE = ("Company standard", "G.C / Client", "Measured")
_AFTER = ("Company standard", "Fabricator", "G.C / Client", "Measured")

_CONSTRAINT = "ck_parameter_values_provenance_in_vocabulary"


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    # Raw SQL, as in 0054: `op.drop_constraint` applies the naming convention to the name it is given,
    # and the full name would be prefixed a second time.
    op.execute(f"ALTER TABLE parameter_values DROP CONSTRAINT {_CONSTRAINT}")
    op.create_check_constraint(
        "provenance_in_vocabulary", "parameter_values", f"provenance IN ({_in_list(_AFTER)})"
    )
    op.add_column(
        "parameter_values",
        sa.Column("source_reference", sa.String(200), nullable=True),
    )
    op.create_check_constraint(
        "source_reference_not_blank",
        "parameter_values",
        "source_reference IS NULL OR source_reference !~ '^[[:space:]]*$'",
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE parameter_values DROP CONSTRAINT ck_parameter_values_source_reference_not_blank"
    )
    op.drop_column("parameter_values", "source_reference")
    op.execute(f"ALTER TABLE parameter_values DROP CONSTRAINT {_CONSTRAINT}")
    op.create_check_constraint(
        "provenance_in_vocabulary", "parameter_values", f"provenance IN ({_in_list(_BEFORE)})"
    )
