"""Where each architect dimension was paired with the vendor's line, by code and geometry only.

One append-only row per dimension the architect printed, per compare reading of a package revision
(`workflow/architect_compare.py`, from `extraction/compare/pair.py`). It records **where things are
drawn**, never a decision: the architect's printed value (from the PDF text), its label and span,
and, when exactly one vendor dimension line draws the same span, that line and its label. Whether
the vendor's number matches is decided from the stored readings when the review reads the records
(`app/review/architect_compare.py`), so a later reading or confirmation is never missed.

Boxes are in page points in pdfplumber's frame (`top` down), as four decimal strings
`[x0, top, x1, bottom]`: placed on the page for "Look" with the page's recorded transform.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID

#: Whether exactly one vendor line draws the same span (`extraction.compare.pair.PairStatus`).
ARCHITECT_COMPARE_PAIRINGS: tuple[str, ...] = ("paired", "unpaired")


class ArchitectCompareRecord(Base, TimestampedUUID, Immutable):
    """One architect dimension and the vendor line drawn for the same span, or why there is none."""

    __tablename__ = "architect_compare_records"

    extraction_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"), index=True
    )
    package_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT"), index=True
    )
    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="RESTRICT"), index=True)
    architect_view: Mapped[int] = mapped_column()
    """The architect drawing's annotation index on the sheet."""
    view_title: Mapped[str | None] = mapped_column(Text, default=None)
    architect_number: Mapped[int] = mapped_column()
    """View-local, from 1, in reading order."""
    architect_text: Mapped[str] = mapped_column(Text)
    architect_numerator: Mapped[int | None] = mapped_column(BigInteger, default=None)
    architect_denominator: Mapped[int | None] = mapped_column(BigInteger, default=None)
    """The architect's value in inches, exactly; both null when the label is held."""
    axis: Mapped[str | None] = mapped_column(String(16), default=None)
    pairing: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    architect_box: Mapped[list[str]] = mapped_column(JSONB)
    """The label and its span, together."""
    vendor_view: Mapped[int | None] = mapped_column(default=None)
    vendor_label_box: Mapped[list[str] | None] = mapped_column(
        JSONB(none_as_null=True), default=None
    )
    """The paired line's nearest label: where the vendor's number is printed."""
    vendor_line_box: Mapped[list[str] | None] = mapped_column(
        JSONB(none_as_null=True), default=None
    )
    vendor_drawn: Mapped[str | None] = mapped_column(String(64), default=None)
    """The paired line's drawn length in inches, `n/d` (geometry only, never a printed number)."""
    paired_by: Mapped[str | None] = mapped_column(String(16), default=None)
    """How the pair was made: `geometry` (code alone) or `two AIs` (the AI pair check, #1418's
    follow-up); null when unpaired."""

    __table_args__ = (
        CheckConstraint("pairing IN ('paired', 'unpaired')", name="architect_compare_pairing"),
        CheckConstraint(
            "(architect_numerator IS NULL) = (architect_denominator IS NULL) "
            "AND (architect_denominator IS NULL OR architect_denominator > 0)",
            name="architect_compare_value",
        ),
        CheckConstraint(
            "pairing = 'unpaired' OR (vendor_label_box IS NOT NULL AND vendor_line_box IS NOT NULL "
            "AND architect_numerator IS NOT NULL)",
            name="architect_compare_paired_has_vendor",
        ),
        CheckConstraint("reason <> ''", name="architect_compare_reason"),
        CheckConstraint(
            "(pairing = 'paired') = (paired_by IS NOT NULL) "
            "AND (paired_by IS NULL OR paired_by IN ('geometry', 'two AIs'))",
            name="architect_compare_paired_by",
        ),
    )


#: A person's answers on a compared dimension (`app/review/architect_compare.py`).
ARCHITECT_COMPARE_ANSWERS: tuple[str, ...] = (
    "vendor_wrong",
    "not_a_difference",
    "checked_right",
    "checked_wrong",
    "not_compared",
)


class ArchitectCompareAnswer(Base, TimestampedUUID, Immutable):
    """A person's answer on one compared dimension. Never edits the record: a later answer is a
    new row, and the newest one counts."""

    __tablename__ = "architect_compare_answers"

    record_id: Mapped[UUID] = mapped_column(
        ForeignKey("architect_compare_records.id", ondelete="RESTRICT"), index=True
    )
    package_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT"), index=True
    )
    answer: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text)
    answered_by: Mapped[str] = mapped_column(String(200))

    __table_args__ = (
        CheckConstraint(
            "answer IN ('vendor_wrong', 'not_a_difference', 'checked_right', 'checked_wrong', "
            "'not_compared')",
            name="architect_compare_answer_kind",
        ),
        CheckConstraint("note <> '' AND answered_by <> ''", name="architect_compare_answer_who"),
    )
