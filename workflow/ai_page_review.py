"""Run and store the AI page review for a package's combined sheets (decision 2026-10-16).

For each shop document: every sheet with an architect drawing (`extraction.compare.pair.compare_pdf`)
gets one AI page review (`extraction/ai_review/reader.py`), decided by code
(`extraction/ai_review/checks.py`) and stored append-only (`app/models/ai_review.py`) under an
extraction run of its own. Every call is recorded in `model_invocations` with its cost; a stored
answer to the identical question is reused, never paid twice.

**Switch:** `GV_AI_PAGE_REVIEW` (off unless a yes word; anything unrecognised is refused).
**Budget:** `GV_AI_PAGE_REVIEW_BUDGET_USD` per package (default $6): each call's worst case is
reserved first, and a page the budget refuses is stored `failed` with that reason.
**Never stops the package:** a page that fails is stored `failed` with its reason; the worker
stage runs this inside a savepoint and logs anything unexpected (`run_in_stage`).

Called from `DatabaseStages.extract_pages` after the reading, and by
`scripts/ai_page_review_backfill.py` for revisions read before it existed.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.record import open_extraction_run
from app.models import Page
from app.models.ai_review import AiPageReview, AiPageReviewRow
from extraction.ai_review.checks import check_rows
from extraction.ai_review.reader import (
    AI_REVIEW_MODEL,
    AI_REVIEW_PROMPT_ID,
    MEASURED_AI_REVIEW_SETTINGS,
    AiReviewSettings,
    PageQuestion,
    PageRead,
    ask_page,
    page_question,
)
from extraction.form_reader.bedrock import AttemptUsage

__all__ = [
    "AI_PAGE_REVIEW_BUDGET_ENV",
    "AI_PAGE_REVIEW_ENV",
    "AI_PAGE_REVIEW_EXTRACTOR",
    "AI_PAGE_REVIEW_VERSION",
    "ai_page_review_budget",
    "ai_page_review_enabled",
    "configured_client",
    "persist_ai_page_review",
    "run_in_stage",
    "spend_guard",
    "stored_answers",
]

logger = logging.getLogger("gv.workflow.ai_page_review")

AI_PAGE_REVIEW_ENV: Final = "GV_AI_PAGE_REVIEW"
AI_PAGE_REVIEW_BUDGET_ENV: Final = "GV_AI_PAGE_REVIEW_BUDGET_USD"
DEFAULT_BUDGET_USD: Final = Decimal(6)
AI_PAGE_REVIEW_EXTRACTOR: Final = "ai-page-review"
AI_PAGE_REVIEW_VERSION: Final = "ai-page-review-v1"
_PARALLEL: Final = 4


def ai_page_review_enabled(environ: Mapping[str, str]) -> bool:
    raw = environ.get(AI_PAGE_REVIEW_ENV, "").strip().lower()
    if raw in ("", "0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    raise ValueError(f"{AI_PAGE_REVIEW_ENV} must be true or false, not {raw!r}")


def ai_page_review_budget(environ: Mapping[str, str]) -> Decimal:
    raw = environ.get(AI_PAGE_REVIEW_BUDGET_ENV, "").strip()
    if not raw:
        return DEFAULT_BUDGET_USD
    value = Decimal(raw)
    if not value.is_finite() or value < 0:
        raise ValueError(f"{AI_PAGE_REVIEW_BUDGET_ENV} must be a non-negative amount")
    return value


def configured_client(environ: Mapping[str, str]) -> Any | None:
    """The OpenRouter client (Opus 5.5 on the decided zero-retention host), or `None` with no key."""
    key = environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        return None
    from extraction.slot_reader.openrouter import OpenRouterMessagesClient

    return OpenRouterMessagesClient(key, timeout_seconds=600)


def spend_guard(rates: object | None, budget: Decimal) -> Any | None:
    """The package's spend guard, or `None` when there is no price file (then nothing is paid)."""
    if rates is None:
        return None
    from extraction.slot_reader.anthropic import BatchSpendGuard

    return BatchSpendGuard(budget, rates)


def stored_answers(session: Session) -> Callable[[Mapping[str, object]], str | None]:
    """The newest accepted stored answer to the identical question, or `None`."""
    from app.models.runs import ModelInvocation, ModelInvocationOutcome
    from workflow.reader_reuse import question_identity

    def find(packet: Mapping[str, object]) -> str | None:
        identity = question_identity(packet)
        version = packet.get("document_version_id")
        if identity is None or not isinstance(version, str):
            return None
        rows = session.execute(
            select(ModelInvocation.reader_question_packet, ModelInvocation.private_raw_response)
            .where(
                ModelInvocation.outcome == ModelInvocationOutcome.OK.value,
                ModelInvocation.model_id == AI_REVIEW_MODEL,
                ModelInvocation.prompt_id == AI_REVIEW_PROMPT_ID,
                ModelInvocation.private_raw_response.is_not(None),
                ModelInvocation.reader_question_packet["document_version_id"].astext == version,
            )
            .order_by(ModelInvocation.created_at.desc(), ModelInvocation.id.desc())
        ).all()
        for stored_packet, raw in rows:
            if isinstance(stored_packet, Mapping) and question_identity(stored_packet) == identity:
                return str(raw)
        return None

    return find


def _record_attempts(
    session: Session, run_id: UUID, attempts: tuple[AttemptUsage, ...], rates: object | None
) -> int | None:
    """One `model_invocations` row per attempt; returns the page's cost in micros (`None` when a
    paid attempt is unpriced)."""
    from app.models.runs import REUSED_FROM_KEY, ModelInvocationOutcome
    from app.runs.invocations import record
    from app.runs.rates import call_cost_micros
    from extraction.models.invocations import InvocationRecord, failure_kind_reason

    total: int | None = 0
    for attempt in attempts:
        packet = None if attempt.question_packet is None else dict(attempt.question_packet)
        tokens_in, tokens_out = attempt.input_tokens or 0, attempt.output_tokens or 0
        if attempt.reused_from is not None:
            packet = packet or {}
            packet[REUSED_FROM_KEY] = attempt.reused_from
            cost: int | None = 0
        else:
            cost = call_cost_micros(rates, attempt.model_id, tokens_in, tokens_out)  # type: ignore[arg-type]
        if attempt.malformed:
            outcome, reason = ModelInvocationOutcome.REJECTED.value, "malformed_form_answer"
        elif attempt.failure_kind:
            outcome = {
                "SpendCapExceeded": ModelInvocationOutcome.BUDGET_REFUSED.value,
                "ProviderAccountRefused": ModelInvocationOutcome.ACCOUNT_REFUSED.value,
                "ProviderInFlightLimited": ModelInvocationOutcome.ACCOUNT_REFUSED.value,
            }.get(attempt.failure_kind, ModelInvocationOutcome.FAILED.value)
            reason = failure_kind_reason(attempt.failure_kind)
        else:
            outcome, reason = ModelInvocationOutcome.OK.value, None
        record(
            session,
            InvocationRecord(
                extraction_run_id=run_id,
                model_id=attempt.model_id,
                prompt_id=attempt.prompt_id,
                template_id=attempt.template_id,
                crop_artifact_id=None,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cost_micros=cost,
                latency_ms=attempt.latency_ms,
                outcome=outcome,
                rejection_reason=reason,
                private_raw_response=attempt.raw_response_text,
                reader_page_index=attempt.page_index,
                reader_attempt_number=attempt.attempt_number,
                reader_question_packet=packet,
            ),
            flush=False,
        )
        total = None if total is None or cost is None else total + cost
    return total


def _vendor_px(question: PageQuestion, box: tuple[int, int, int, int] | None) -> list[int] | None:
    if box is None:
        return None
    x0, y0, x1, y1 = question.vendor.box_px
    w, h = x1 - x0, y1 - y0
    return [
        x0 + box[0] * w // 1000,
        y0 + box[1] * h // 1000,
        x0 + box[2] * w // 1000,
        y0 + box[3] * h // 1000,
    ]


def _box(box: Any) -> list[str]:
    return [str(box.x0), str(box.top), str(box.x1), str(box.bottom)]


def persist_ai_page_review(
    session: Session,
    *,
    package_revision_id: UUID,
    version_id: UUID,
    data: bytes,
    task_run_id: UUID,
    dpi: int,
    client: Any | None,
    rates: object | None,
    guard: Any | None,
    settings: AiReviewSettings = MEASURED_AI_REVIEW_SETTINGS,
    pages_wanted: set[int] | None = None,
) -> dict[str, int]:
    """Review every combined sheet of one shop document and store it. Returns counts by status
    (empty when this run already stored its reviews: a redelivered stage)."""
    from extraction.compare.pair import compare_pdf

    run = open_extraction_run(
        session,
        task_run_id=task_run_id,
        extractor=AI_PAGE_REVIEW_EXTRACTOR,
        extractor_version=AI_PAGE_REVIEW_VERSION,
        config_hash=f"{version_id}:{AI_REVIEW_PROMPT_ID}",
        dpi=dpi,
    )
    if session.scalar(
        select(AiPageReview.id).where(AiPageReview.extraction_run_id == run.id).limit(1)
    ):
        return {}
    pages = {
        page.index: page
        for page in session.scalars(select(Page).where(Page.document_version_id == version_id))
    }
    stored = stored_answers(session)
    questions: list[tuple[Page, PageQuestion | str]] = []
    for compare_page, _pairings in compare_pdf(data, dpi=dpi):
        stored_page = pages.get(compare_page.page_index)
        if stored_page is None:
            continue
        if pages_wanted is not None and compare_page.page_index + 1 not in pages_wanted:
            continue
        try:
            question: PageQuestion | str = page_question(data, compare_page, settings)
        except Exception as error:  # noqa: BLE001 - recorded as the page's reason
            question = f"the sheet's pictures could not be made ({type(error).__name__})"
            logger.warning("ai page review: page %s not asked", compare_page.page_index + 1)
        questions.append((stored_page, question))

    # Stored answers are looked up on this thread (the session is not shared across threads).
    lookups: dict[int, str | None] = {}
    for _page, question in questions:
        if isinstance(question, PageQuestion):
            from extraction.ai_review.reader import question_packet

            lookups[question.page_index] = stored(
                question_packet(question, str(version_id), settings)
            )

    def ask(question: PageQuestion) -> PageRead:
        known = lookups.get(question.page_index)
        return ask_page(
            question,
            client=client,
            document_version_id=str(version_id),
            spend_guard=guard,
            stored=lambda _packet: known,
            settings=settings,
        )

    asked = [q for _p, q in questions if isinstance(q, PageQuestion)]
    with ThreadPoolExecutor(max_workers=_PARALLEL) as pool:
        reads = dict(zip([q.page_index for q in asked], pool.map(ask, asked), strict=True))

    counts: dict[str, int] = {}
    for page, question in questions:
        if isinstance(question, str):
            review = AiPageReview(
                extraction_run_id=run.id,
                package_revision_id=package_revision_id,
                page_id=page.id,
                status="not_run",
                reason=question,
                model_id=AI_REVIEW_MODEL,
                prompt_id=AI_REVIEW_PROMPT_ID,
                # Not asked: no call, so no cost is recorded for it (null, never a stand-in zero).
                cost_micros=None,
                dropped=[],
            )
            session.add(review)
            counts["not_run"] = counts.get("not_run", 0) + 1
            continue
        read = reads[question.page_index]
        cost = _record_attempts(session, run.id, read.attempts, rates)
        rows = read.answer.get("rows") if read.answer is not None else None
        status = "done" if isinstance(rows, list) else "failed"
        checked, dropped = (
            check_rows([d.listed for d in question.dimensions], rows)
            if isinstance(rows, list)
            else ([], [])
        )
        review = AiPageReview(
            extraction_run_id=run.id,
            package_revision_id=package_revision_id,
            page_id=page.id,
            status=status,
            reason=None if status == "done" else (read.reason or "no usable answer"),
            model_id=AI_REVIEW_MODEL,
            prompt_id=AI_REVIEW_PROMPT_ID,
            cost_micros=cost,
            vendor_dpi=question.vendor.dpi,
            vendor_crop_px=list(question.vendor.box_px),
            raw_answer=read.raw,
            dropped=[{"architect_id": d.architect_id, "reason": d.reason} for d in dropped],
        )
        session.add(review)
        session.flush()
        # A failed page stores no rows: its reason says why (never silent).
        for position, (row, listed) in enumerate(
            zip(checked, question.dimensions if checked else (), strict=True), start=1
        ):
            architect, vendor = row.architect_inches, row.vendor_inches
            session.add(
                AiPageReviewRow(
                    review_id=review.id,
                    package_revision_id=package_revision_id,
                    position=position,
                    architect_text=row.architect_text,
                    architect_numerator=None if architect is None else architect.numerator,
                    architect_denominator=None if architect is None else architect.denominator,
                    architect_box=_box(listed.label_box),
                    measures=row.measures,
                    vendor_text=row.vendor_text,
                    vendor_numerator=None if vendor is None else vendor.numerator,
                    vendor_denominator=None if vendor is None else vendor.denominator,
                    result=row.result,
                    ai_result=row.ai_result,
                    why=row.why,
                    vendor_box_px=_vendor_px(question, row.vendor_box),
                )
            )
        counts[status] = counts.get(status, 0) + 1
    session.flush()
    return counts


def run_in_stage(
    session: Session,
    *,
    package_revision_id: UUID,
    documents: list[tuple[UUID, bytes]],
    task_run_id: UUID,
    dpi: int,
    environ: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> dict[str, int]:
    """The worker's call after the reading: off unless `GV_AI_PAGE_REVIEW`; never raises.

    Each document is reviewed inside a savepoint: anything unexpected rolls that document's review
    back and is logged, and the package carries on."""
    env = os.environ if environ is None else environ
    try:
        if not ai_page_review_enabled(env):
            return {}
        budget = ai_page_review_budget(env)
    except ValueError:
        logger.warning("ai page review: its settings are not valid; not run")
        return {}
    from app.runs.rates import rates_from_environment

    rates = rates_from_environment()
    if client is None:
        client = configured_client(env) if rates is not None else None
    guard = spend_guard(rates, budget)
    totals: dict[str, int] = {}
    for version_id, data in documents:
        try:
            with session.begin_nested():
                counts = persist_ai_page_review(
                    session,
                    package_revision_id=package_revision_id,
                    version_id=version_id,
                    data=data,
                    task_run_id=task_run_id,
                    dpi=dpi,
                    client=client,
                    rates=rates,
                    guard=guard,
                )
        except Exception:
            logger.exception("ai page review: document %s not reviewed", version_id)
            continue
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
    return totals
