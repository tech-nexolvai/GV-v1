"""The AI page review's records (decision 2026-10-16): one reading per sheet, its rows, and a person's answers.

* `AiPageReview`: one per package revision, page and reading run: whether the sheet was read
  (`done`), could not be (`failed`, with the reason) or was not asked (`not_run`, with the reason),
  the model, the cost and the AI's raw answer.
* `AiPageReviewRow`: one per architect dimension code read on that sheet, exactly once, with what
  code decided from the AI's reading (`extraction/ai_review/checks.py`).
* `AiPageReviewAnswer`: a person's answer on a `different` row ("the vendor is wrong" / "not a
  difference"); a later answer is a new row and the newest counts.

All append-only. Boxes: `architect_box` in page points in pdfplumber's frame (`top` down);
`vendor_box_px` in the page's pixels at the review's `vendor_dpi` (`render_page`'s frame).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID

AI_PAGE_REVIEW_STATUSES: tuple[str, ...] = ("done", "failed", "not_run")
AI_PAGE_REVIEW_RESULTS: tuple[str, ...] = ("same", "different", "not_shown", "unsure", "unreadable")
AI_PAGE_REVIEW_ANSWERS: tuple[str, ...] = ("vendor_wrong", "not_a_difference")


class AiPageReview(Base, TimestampedUUID, Immutable):
    """One sheet's AI page review reading."""

    __tablename__ = "ai_page_reviews"

    extraction_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"), index=True
    )
    package_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT"), index=True
    )
    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    model_id: Mapped[str] = mapped_column(String(300))
    prompt_id: Mapped[str] = mapped_column(String(300))
    cost_micros: Mapped[int | None] = mapped_column(BigInteger, default=None)
    """What this sheet's calls cost (0 for a reused stored answer); `None` when unpriced."""
    vendor_dpi: Mapped[int | None] = mapped_column(default=None)
    vendor_crop_px: Mapped[list[int] | None] = mapped_column(JSONB(none_as_null=True), default=None)
    raw_answer: Mapped[str | None] = mapped_column(Text, default=None)
    dropped: Mapped[list[dict[str, str]]] = mapped_column(JSONB, default=list)
    """AI rows code dropped, each with its reason (never silent)."""

    __table_args__ = (
        CheckConstraint("status IN ('done', 'failed', 'not_run')", name="ai_page_review_status"),
        CheckConstraint(
            "status = 'done' OR (reason IS NOT NULL AND reason <> '')",
            name="ai_page_review_reason",
        ),
    )


class AiPageReviewRow(Base, TimestampedUUID, Immutable):
    """One architect dimension on a reviewed sheet and what code decided from the AI's reading."""

    __tablename__ = "ai_page_review_rows"

    review_id: Mapped[UUID] = mapped_column(
        ForeignKey("ai_page_reviews.id", ondelete="RESTRICT"), index=True
    )
    package_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT"), index=True
    )
    position: Mapped[int] = mapped_column()
    """`A<position>` as listed to the AI, from 1."""
    architect_text: Mapped[str] = mapped_column(Text)
    architect_numerator: Mapped[int | None] = mapped_column(BigInteger, default=None)
    architect_denominator: Mapped[int | None] = mapped_column(BigInteger, default=None)
    architect_box: Mapped[list[str]] = mapped_column(JSONB)
    measures: Mapped[str] = mapped_column(Text)
    vendor_text: Mapped[str | None] = mapped_column(Text, default=None)
    vendor_numerator: Mapped[int | None] = mapped_column(BigInteger, default=None)
    vendor_denominator: Mapped[int | None] = mapped_column(BigInteger, default=None)
    result: Mapped[str] = mapped_column(String(16))
    ai_result: Mapped[str | None] = mapped_column(String(16), default=None)
    why: Mapped[str] = mapped_column(Text)
    vendor_box_px: Mapped[list[int] | None] = mapped_column(JSONB(none_as_null=True), default=None)
    verified_by: Mapped[str | None] = mapped_column(String(16), default=None)
    """`evaluator` when the independent evaluator confirmed this difference (migration 0104)."""
    evaluator_reason: Mapped[str | None] = mapped_column(Text, default=None)
    """The evaluator's reason, or why it could not be asked; `None` when not re-checked."""
    evaluator_answer: Mapped[dict[str, str] | None] = mapped_column(
        JSONB(none_as_null=True), default=None
    )

    __table_args__ = (
        CheckConstraint(
            "verified_by IS NULL OR (verified_by = 'evaluator' AND result = 'different' "
            "AND evaluator_reason IS NOT NULL AND evaluator_reason <> '')",
            name="ai_page_review_row_verified",
        ),
        CheckConstraint(
            "result IN ('same', 'different', 'not_shown', 'unsure', 'unreadable')",
            name="ai_page_review_row_result",
        ),
        CheckConstraint(
            "(architect_numerator IS NULL) = (architect_denominator IS NULL) "
            "AND (vendor_numerator IS NULL) = (vendor_denominator IS NULL) "
            "AND (architect_denominator IS NULL OR architect_denominator > 0) "
            "AND (vendor_denominator IS NULL OR vendor_denominator > 0)",
            name="ai_page_review_row_values",
        ),
        CheckConstraint(
            "result NOT IN ('same', 'different') OR "
            "(architect_numerator IS NOT NULL AND vendor_numerator IS NOT NULL)",
            name="ai_page_review_row_decided",
        ),
        CheckConstraint("why <> ''", name="ai_page_review_row_why"),
    )


class AiPageReviewAnswer(Base, TimestampedUUID, Immutable):
    """A person's answer on one `different` row. Never edits the row."""

    __tablename__ = "ai_page_review_answers"

    row_id: Mapped[UUID] = mapped_column(
        ForeignKey("ai_page_review_rows.id", ondelete="RESTRICT"), index=True
    )
    package_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT"), index=True
    )
    answer: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text)
    answered_by: Mapped[str] = mapped_column(String(200))

    __table_args__ = (
        CheckConstraint(
            "answer IN ('vendor_wrong', 'not_a_difference')", name="ai_page_review_answer_kind"
        ),
        CheckConstraint("note <> '' AND answered_by <> ''", name="ai_page_review_answer_who"),
    )
