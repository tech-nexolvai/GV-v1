"""What the job values reader found for one package revision (#1407, wired by #1409).

One append-only row per value per reading: cabinet depth, countertop overhang, backsplash thickness
and cabinet side thickness, under the extraction run that read them. `suggestion` is
`extraction.job_values.decide.form_suggestion`'s shape (plus where to ring it on the page), shown
beside the value's field in the simple review's "Enter this job's values" form.

**Nothing here is a setting or a reading of a dimension.** A value is saved for the project only
when a person presses the form's Save (`app.api.company_settings.store_project_values`); these rows
are never read by a check, an operand or the evidence gate. They live in their own table, not in
`observation_candidates`, so no reader of dimension readings can ever pick one up.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID

#: The four per-job values, as `extraction.job_values.decide.SETTINGS` names them.
JOB_VALUE_SETTINGS: tuple[str, ...] = (
    "cabinet_depth",
    "countertop_overhang",
    "backsplash_thickness",
    "cabinet_side_thickness",
)
#: What code decided for a value (`extraction.job_values.decide.Outcome`).
JOB_VALUE_OUTCOMES: tuple[str, ...] = ("auto", "suggestion", "refused", "nothing")


def _listed(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class JobValueReading(Base, TimestampedUUID, Immutable):
    """One value as one job values reading found it: auto, a suggestion, refused or nothing."""

    __tablename__ = "job_value_readings"

    extraction_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"), index=True
    )
    package_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT"), index=True
    )
    setting: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16))
    suggestion: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), default=None
    )
    """`form_suggestion()`'s object, plus `page_id` and `polygon` (stored space) for "Look"."""
    reasons: Mapped[list[str]] = mapped_column(JSONB)
    """Why, in plain words: every check that held it, or why nothing was found."""
    not_read: Mapped[list[str]] = mapped_column(JSONB)
    """Every drawing this reading prepared and did not ask about, with why (never silent)."""

    __table_args__ = (
        CheckConstraint(
            f"setting IN ({_listed(JOB_VALUE_SETTINGS)})", name="job_value_reading_setting"
        ),
        CheckConstraint(
            f"outcome IN ({_listed(JOB_VALUE_OUTCOMES)})", name="job_value_reading_outcome"
        ),
        CheckConstraint(
            "suggestion IS NULL OR (jsonb_typeof(suggestion) = 'object' "
            "AND outcome IN ('auto', 'suggestion'))",
            name="job_value_reading_suggestion",
        ),
        CheckConstraint("jsonb_typeof(reasons) = 'array'", name="job_value_reading_reasons"),
        CheckConstraint("jsonb_typeof(not_read) = 'array'", name="job_value_reading_not_read"),
    )
