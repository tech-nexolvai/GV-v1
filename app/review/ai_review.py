"""The AI page review as the review shows it, and a person's answers on it (decision 2026-10-16).

The worker stores, once per reading, one AI page review per combined sheet and one row per
architect dimension code read on it, already decided by code (`workflow/ai_page_review.py`,
`extraction/ai_review/checks.py`). This reads the newest review of each page, with each row's
newest answer:

* **Nothing becomes PASS, and no row needs a person** (decision 2026-10-16: "we don't want
  anything from the reviewer"). A `different` row is clouded on the draft and final cloud PDF by
  itself and never holds sign-off; `same`, `not_shown`, `unsure` and `unreadable` rows are shown
  on the screen only. The answers table and `record_ai_review_answer` stay (append-only, no longer
  offered by the screen); a stored answer is still shown but changes nothing.
* **Where:** the architect's dimension (from code's own boxes) and, when the AI gave a plausible
  box, the vendor's printed number; `None` otherwise (the cloud builder lists unplaced ones).

Read-only but for `record_ai_review_answer`, which appends one answer row (kept, unused).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.evidence.sides import page_transform_at
from app.models import Page
from app.models.ai_review import AiPageReview, AiPageReviewAnswer, AiPageReviewRow
from app.review.row_location import RowLocation
from evidence.coordinates import ImagePoint, PageTransform, PdfPoint

__all__ = [
    "AI_KEY_PREFIX",
    "AI_REVIEW_ANSWER_NOTES",
    "AI_REVIEW_SENT_AS",
    "AiReviewCountsOut",
    "AiReviewOut",
    "AiReviewPageOut",
    "AiReviewRefused",
    "AiReviewRowOut",
    "ai_review_for_revision",
    "ai_review_open_many",
    "record_ai_review_answer",
]

AI_KEY_PREFIX: Final = "air:"

AiResult = Literal["same", "different", "not_shown", "unsure", "unreadable"]

AI_REVIEW_ANSWER_NOTES: Final[Mapping[str, str]] = {
    "vendor_wrong": "Agreed: the vendor's number is not the architect's. It stays clouded.",
    "not_a_difference": "Not a difference: the person checked these two numbers.",
}
#: The names the screen sends, as the answers stored.
AI_REVIEW_SENT_AS: Final[Mapping[str, str]] = {
    "agree": "vendor_wrong",
    "agree_vendor_wrong": "vendor_wrong",
    "vendor_wrong": "vendor_wrong",
    "not_this": "not_a_difference",
    "not_a_difference": "not_a_difference",
}
#: Each stored answer as the screen and the cloud PDF name it on a row.
_SHOWN_AS: Final[Mapping[str, str]] = {"vendor_wrong": "agree", "not_a_difference": "not_this"}


class AiReviewRowOut(BaseModel):
    key: str = Field(description="`air:<row id>`: stable for this reading.")
    architect_text: str = Field(description="As printed, read by code.")
    architect_inches: str | None = Field(description="Exact inches `n/d`; null when held.")
    measures: str = Field(description="What the AI says the dimension measures.")
    vendor_text: str | None = Field(description="The vendor's number as the AI read it.")
    vendor_inches: str | None = Field(description="Exact inches `n/d`, parsed by code.")
    result: AiResult
    why: str
    vendor_box: RowLocation | None = Field(
        description="Where the vendor's number is printed (the AI's box); null when not placed."
    )
    architect_ref: RowLocation | None
    actions: list[str] = Field(
        default_factory=list,
        description="Always empty: no row asks a person anything (decision 2026-10-16).",
    )
    status: Literal["open", "done"] = Field(
        default="done", description="Always `done`: no row waits for a person."
    )
    answer: str | None = Field(
        default=None,
        description="An older stored answer, as the action is named (`agree` | `not_this`); "
        "it changes nothing.",
    )
    answered_by: str | None = None
    clouded: bool = Field(
        default=False,
        description="True on every `different` row: it is clouded by itself.",
    )


class AiReviewPageOut(BaseModel):
    page: int = Field(description="The sheet, from 1.")
    status: Literal["done", "running", "failed", "not_run"]
    reason: str | None = Field(default=None, description="Why the page failed or was not run.")
    cost_usd: str | None = Field(description="What its AI call cost; null when unpriced.")
    rows: list[AiReviewRowOut] = Field(default_factory=list)


class AiReviewCountsOut(BaseModel):
    same: int = 0
    different: int = 0
    not_shown: int = 0
    unsure: int = 0
    unreadable: int = 0
    needs_person: int = Field(
        default=0, description="Always 0: no row waits for a person, sign-off never waits on it."
    )


class AiReviewOut(BaseModel):
    pages: list[AiReviewPageOut] = Field(default_factory=list)
    counts: AiReviewCountsOut = Field(default_factory=AiReviewCountsOut)


def _fraction(numerator: int | None, denominator: int | None) -> Fraction | None:
    if numerator is None or denominator is None:
        return None
    return Fraction(numerator, denominator)


def _text(value: Fraction | None) -> str | None:
    return None if value is None else str(value)


def _stored(
    transform: PageTransform, page: Page, corners: Sequence[ImagePoint]
) -> RowLocation | None:
    try:
        points = [transform.to_stored(corner) for corner in corners]
    except (ArithmeticError, TypeError, ValueError):
        return None
    left, right = min(p.x for p in points), max(p.x for p in points)
    upper, lower = min(p.y for p in points), max(p.y for p in points)
    if any(not v.is_finite() or v < 0 or v > 1 for v in (left, right, upper, lower)):
        return None
    return RowLocation(
        page_id=page.id,
        document_version_id=page.document_version_id,
        page_number=page.index + 1,
        polygon=[
            [str(x), str(y)]
            for x, y in ((left, upper), (right, upper), (right, lower), (left, lower))
        ],
    )


def _architect_ref(page: Page, dpi: int | None, box: object) -> RowLocation | None:
    height = page.height_pt
    if dpi is None or not isinstance(height, Decimal) or not isinstance(box, list) or len(box) != 4:
        return None
    transform = page_transform_at(page, dpi)
    if transform is None:
        return None
    try:
        x0, top, x1, bottom = (Decimal(str(v)) for v in box)
        corners = [
            transform.to_image(PdfPoint(x, height - y)) for x, y in ((x0, top), (x1, bottom))
        ]
    except (InvalidOperation, ArithmeticError, TypeError, ValueError):
        return None
    return _stored(transform, page, corners)


def _vendor_ref(page: Page, dpi: int | None, box: object) -> RowLocation | None:
    if dpi is None or not isinstance(box, list) or len(box) != 4:
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in box):
        return None
    transform = page_transform_at(page, dpi)
    if transform is None:
        return None
    return _stored(transform, page, [ImagePoint(box[0], box[1]), ImagePoint(box[2], box[3])])


def _latest_reviews(
    session: Session, revision_ids: Sequence[UUID]
) -> dict[UUID, list[AiPageReview]]:
    """Each revision's newest review of each page: one statement."""
    rank = (
        func.row_number()
        .over(
            partition_by=(AiPageReview.package_revision_id, AiPageReview.page_id),
            order_by=(AiPageReview.created_at.desc(), AiPageReview.id.desc()),
        )
        .label("rank")
    )
    ranked = (
        select(AiPageReview.id.label("review_id"), rank)
        .where(AiPageReview.package_revision_id.in_(list(revision_ids)))
        .subquery()
    )
    found: dict[UUID, list[AiPageReview]] = {}
    for review in session.scalars(
        select(AiPageReview)
        .join(ranked, ranked.c.review_id == AiPageReview.id)
        .where(ranked.c.rank == 1)
    ):
        found.setdefault(review.package_revision_id, []).append(review)
    return found


def ai_review_many(session: Session, revision_ids: Sequence[UUID]) -> dict[UUID, AiReviewOut]:
    """`ai_review_for_revision` for several revisions in a fixed handful of statements; a revision
    with no review is left out."""
    ids = list(dict.fromkeys(revision_ids))
    if not ids:
        return {}
    by_revision = _latest_reviews(session, ids)
    if not by_revision:
        return {}
    reviews = [review for listed in by_revision.values() for review in listed]
    pages = {
        page.id: page
        for page in session.scalars(select(Page).where(Page.id.in_({r.page_id for r in reviews})))
    }
    rows: dict[UUID, list[AiPageReviewRow]] = {}
    for row in session.scalars(
        select(AiPageReviewRow)
        .where(AiPageReviewRow.review_id.in_([r.id for r in reviews]))
        .order_by(AiPageReviewRow.position, AiPageReviewRow.id)
    ):
        rows.setdefault(row.review_id, []).append(row)
    every_row = [row.id for listed in rows.values() for row in listed]
    answers: dict[UUID, AiPageReviewAnswer] = {}
    if every_row:
        for given_answer in session.scalars(
            select(AiPageReviewAnswer)
            .where(AiPageReviewAnswer.row_id.in_(every_row))
            .order_by(AiPageReviewAnswer.created_at, AiPageReviewAnswer.id)
        ):
            answers[given_answer.row_id] = given_answer
    out: dict[UUID, AiReviewOut] = {}
    for revision_id, listed in by_revision.items():
        review_out = AiReviewOut()
        counts = review_out.counts
        for review in sorted(listed, key=lambda r: pages[r.page_id].index):
            page = pages[review.page_id]
            page_out = AiReviewPageOut(
                page=page.index + 1,
                status=review.status,  # type: ignore[arg-type]
                reason=review.reason,
                cost_usd=(
                    None
                    if review.cost_micros is None
                    else str(Decimal(review.cost_micros) / Decimal(1_000_000))
                ),
            )
            for row in rows.get(review.id, []):
                given = answers.get(row.id)
                stored = given.answer if given is not None and row.result == "different" else None
                answer = None if stored is None else _SHOWN_AS[stored]
                page_out.rows.append(
                    AiReviewRowOut(
                        key=f"{AI_KEY_PREFIX}{row.id}",
                        architect_text=row.architect_text,
                        architect_inches=_text(
                            _fraction(row.architect_numerator, row.architect_denominator)
                        ),
                        measures=row.measures,
                        vendor_text=row.vendor_text,
                        vendor_inches=_text(
                            _fraction(row.vendor_numerator, row.vendor_denominator)
                        ),
                        result=row.result,  # type: ignore[arg-type]
                        why=row.why,
                        vendor_box=_vendor_ref(page, review.vendor_dpi, row.vendor_box_px),
                        architect_ref=_architect_ref(page, review.vendor_dpi, row.architect_box),
                        actions=[],
                        status="done",
                        answer=answer,
                        answered_by=None if answer is None or given is None else given.answered_by,
                        clouded=row.result == "different",
                    )
                )
                setattr(counts, row.result, getattr(counts, row.result) + 1)
            review_out.pages.append(page_out)
        out[revision_id] = review_out
    return out


def ai_review_for_revision(session: Session, revision_id: UUID) -> AiReviewOut:
    """The revision's AI page review: the newest reading of each page, its rows decided by code
    and each row's newest answer, with the counts. Empty (no pages) when it has not run."""
    return ai_review_many(session, [revision_id]).get(revision_id, AiReviewOut())


def ai_review_open_many(session: Session, revision_ids: Sequence[UUID]) -> dict[UUID, int]:
    """For each revision, its rows waiting for a person; a revision with none is left out. Always
    empty since no row waits (decision 2026-10-16); kept for callers."""
    return {
        revision_id: review.counts.needs_person
        for revision_id, review in ai_review_many(session, revision_ids).items()
        if review.counts.needs_person
    }


class AiReviewRefused(ValueError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def record_ai_review_answer(
    session: Session, *, revision_id: UUID, key: str, answer: str, actor: str
) -> int:
    """Append a person's answer on one `different` row (`air:<id>`): `agree` or `not_this`.
    Returns 0 when it is already the newest answer. Raises `AiReviewRefused`."""
    stored = AI_REVIEW_SENT_AS.get(answer.strip().lower().replace("-", "_"))
    if stored is None:
        raise AiReviewRefused(422, f"An AI review row does not take '{answer}'.")
    items = [
        row for page in ai_review_for_revision(session, revision_id).pages for row in page.rows
    ]
    item = next((row for row in items if row.key == key), None)
    if item is None:
        raise AiReviewRefused(404, "No such AI review row on this revision's newest reading.")
    if item.answer == _SHOWN_AS[stored]:
        return 0
    if item.result != "different":
        raise AiReviewRefused(422, f"A '{item.result}' row takes no answer: only a different one.")
    session.add(
        AiPageReviewAnswer(
            row_id=UUID(key.removeprefix(AI_KEY_PREFIX)),
            package_revision_id=revision_id,
            answer=stored,
            note=AI_REVIEW_ANSWER_NOTES[stored],
            answered_by=actor,
        )
    )
    session.flush()
    return 1
