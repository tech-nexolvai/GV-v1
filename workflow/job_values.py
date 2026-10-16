"""The job values reader in the worker (#1409): read this job's four values once per revision.

**When.** Once per package revision, in the match stage right after the depth reader, and only when
`GV_JOB_VALUES_READER` is on (off by default). Also on request, for a revision already read
(`READ_JOB_VALUES_WORKFLOW`, queued by `scripts/job_values_queue.py`), so a set read before the
switch was on gets its values without being read again.

**What it reads.** The architect's side only (`extraction.job_values.read.prepare_pdf_panels`): on
a combined sheet the drawings GV's heading names the architect's or a brand prototype's; in a
separate architect file every drawing. A vendor drawing is never prepared, so it is never sent. A
file uploaded in both slots is read once.

**The calls.** Every question is recorded in `model_invocations` through the shared call ledger,
under this reader's own extraction run. A stored answer to the identical question is replayed,
never paid twice (`stored_answers`). One spend guard for the reading, capped by what the drawing
set's AI budget has left.

**What it writes**, append-only: one `job_value_readings` row per value, with
`extraction.job_values.decide.form_suggestion`'s shape plus where to ring it on the page. Nothing
here saves a setting: a value reaches the project only when a person presses Save on the form.

Source: issue #1409. Verification: `tests/workflow/test_job_values_wiring.py`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Page
from app.models.job_values import JOB_VALUE_SETTINGS, JobValueReading
from app.models.runs import ExtractionRun, ModelInvocation, ModelInvocationOutcome
from evidence.coordinates import ImagePoint
from extraction.job_values.decide import JobValueResult, Outcome, form_suggestion

__all__ = [
    "JOB_VALUES_EXTRACTOR",
    "JOB_VALUES_READER_VERSION",
    "READ_JOB_VALUES_WORKFLOW",
    "JobValueSuggestion",
    "latest_job_values",
    "persist_job_values",
    "stored_answers",
]

JOB_VALUES_EXTRACTOR: Final = "job-values-reader"
JOB_VALUES_READER_VERSION: Final = "job-values-reader-v1"
#: The outbox work that reads one revision's job values on request (`read_job_values_again`).
READ_JOB_VALUES_WORKFLOW: Final = "read_job_values"

#: What the form says about where a value came from: code's own find on the drawing's text, or the
#: pair of AI readers (with code's checks when it is auto).
_SOURCE_WORDS: Final = {"code": "drawing", "ai-pair": "two AIs", "ai-reader": "two AIs"}


def _stored_polygon(page: Page | None, box: Sequence[str] | None, dpi: int) -> list[list[str]]:
    """The value's box on its page in stored space (0-1 of the visible page), or `[]`.

    The box is in PDF points from the page's top-left (pdfplumber's frame), the frame the page is
    rendered in; it is placed with the page's own published transform, never a guess."""
    from app.evidence.sides import page_transform_at

    if page is None or box is None or len(box) != 4:
        return []
    transform = page_transform_at(page, dpi)
    if transform is None:
        return []
    scale = Decimal(dpi) / Decimal(72)
    try:
        x0, top, x1, bottom = (Decimal(value) * scale for value in box)
        corners = [(x0, top), (x1, top), (x1, bottom), (x0, bottom)]
        stored = [transform.to_stored(ImagePoint(round(x), round(y))) for x, y in corners]
    except (ArithmeticError, TypeError, ValueError):
        return []
    return [[str(point.x), str(point.y)] for point in stored]


def persist_job_values(
    session: Session,
    package_revision_id: UUID,
    run: ExtractionRun,
    results: Mapping[str, JobValueResult],
    *,
    not_read: Sequence[str],
    pages: Mapping[tuple[str, int], Page],
    dpi: int,
) -> dict[str, str]:
    """One append-only row per value; returns each value's outcome."""
    written: dict[str, str] = {}
    for name, result in results.items():
        suggestion = form_suggestion(result)
        if suggestion is not None:
            page = (
                None
                if result.page_index is None
                else pages.get((str(result.document_version_id), result.page_index))
            )
            suggestion["page_id"] = None if page is None else str(page.id)
            box = suggestion.get("box")
            suggestion["polygon"] = _stored_polygon(
                page, box if isinstance(box, list) else None, dpi
            )
        session.add(
            JobValueReading(
                extraction_run_id=run.id,
                package_revision_id=package_revision_id,
                setting=name,
                outcome=result.outcome.value,
                suggestion=suggestion,
                reasons=[str(reason)[:500] for reason in result.reasons],
                not_read=[str(line)[:500] for line in not_read],
            )
        )
        written[name] = result.outcome.value
    session.flush()
    return written


def stored_answers(session: Session) -> Callable[[str, Mapping[str, object]], str | None]:
    """The newest accepted stored answer to the identical job values question, or `None`."""
    from extraction.job_values.read import JOB_VALUES_PROMPT_ID
    from workflow.reader_reuse import question_identity

    def find(model_id: str, packet: Mapping[str, object]) -> str | None:
        identity = question_identity(packet)
        version = packet.get("document_version_id")
        if identity is None or not isinstance(version, str):
            return None
        rows = session.execute(
            select(ModelInvocation.reader_question_packet, ModelInvocation.private_raw_response)
            .where(
                ModelInvocation.outcome == ModelInvocationOutcome.OK.value,
                ModelInvocation.model_id == model_id,
                ModelInvocation.prompt_id == JOB_VALUES_PROMPT_ID,
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


@dataclass(frozen=True, slots=True)
class JobValueSuggestion:
    """What the form shows beside one value's field: from the newest reading of the revision."""

    name: str
    value: str
    page: int
    page_id: str | None
    document_version_id: str | None
    polygon: list[list[str]]
    source: str
    auto: bool
    choices: list[dict[str, object]]
    note: str


def latest_job_values(session: Session, package_revision_id: UUID) -> dict[str, JobValueSuggestion]:
    """Each value the newest job values reading found (auto or a suggestion with a value), by name.

    Read-only. A value the newest reading found nothing for, or refused, is left out: the form then
    shows no suggestion for it."""
    newest = session.scalar(
        select(JobValueReading.extraction_run_id)
        .where(JobValueReading.package_revision_id == package_revision_id)
        .order_by(JobValueReading.created_at.desc(), JobValueReading.id.desc())
        .limit(1)
    )
    if newest is None:
        return {}
    out: dict[str, JobValueSuggestion] = {}
    for row in session.scalars(
        select(JobValueReading).where(
            JobValueReading.extraction_run_id == newest,
            JobValueReading.package_revision_id == package_revision_id,
        )
    ):
        shown = row.suggestion
        if row.outcome not in (Outcome.AUTO.value, Outcome.SUGGESTION.value) or not shown:
            continue
        value = shown.get("value")
        page = shown.get("page")
        if not isinstance(value, str) or not isinstance(page, int):
            continue
        polygon: Any = shown.get("polygon")
        choices: Any = shown.get("choices")
        out[row.setting] = JobValueSuggestion(
            name=row.setting,
            value=value,
            page=page,
            page_id=_text(shown.get("page_id")),
            document_version_id=_text(shown.get("document_version_id")),
            polygon=polygon if isinstance(polygon, list) else [],
            source=_SOURCE_WORDS.get(str(shown.get("source")), "two AIs"),
            auto=row.outcome == Outcome.AUTO.value and shown.get("filled") is True,
            choices=choices if isinstance(choices, list) else [],
            note=str(shown.get("note") or ""),
        )
    order = {name: n for n, name in enumerate(JOB_VALUE_SETTINGS)}
    return dict(sorted(out.items(), key=lambda pair: order.get(pair[0], len(order))))


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def config_hash(reader_ids: Sequence[str], dpi: int, effort: str) -> str:
    """The reading's identity: the question, the pair and the picture resolution."""
    from extraction.job_values.read import JOB_VALUES_PROMPT, JOB_VALUES_PROMPT_ID

    prompt = hashlib.sha256(JOB_VALUES_PROMPT.encode()).hexdigest()[:16]
    return (
        f"prompt={JOB_VALUES_PROMPT_ID}:{prompt};dpi={dpi};readers={'|'.join(reader_ids)};"
        f"effort={effort}"
    )


class _PerThread:
    """A reader client for one model: the shared provider's client for the calling thread."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def converse(self, **request: Any) -> Any:
        return self._provider.for_current_thread().converse(**request)


def run_job_values(
    stages: Any, session: Session, package_revision_id: UUID, *, request: str | None = None
) -> dict[str, object]:
    """Read the revision's job values once (or once for `request`), and record them.

    `stages` is the worker's `DatabaseStages`: its Claude pair, its store, its AI budget, its call
    ledger and spend meter, the same as the depth reader's. **A failure never stops the package**:
    it is returned with why, and the calls already made stay in the call ledger.
    """
    from app.evidence.record import open_extraction_run
    from app.evidence.sides import has_separate_architect_file
    from app.models.document import Document, DocumentKind, DocumentVersion
    from app.models.runs import TaskRun
    from extraction.job_values.read import (
        MEASURED_JOB_VALUES_SETTINGS,
        prepare_pdf_panels,
        read_job_values,
    )
    from extraction.reader import read_pages
    from extraction.slot_reader.anthropic import BatchSpendGuard
    from workflow import stages as stages_module
    from workflow.depth_again import request_ledger_tag
    from workflow.set_spend import depth_cap_usd
    from workflow.slot_row_scope import latest_slot_reader_run

    runtime = stages._slot_reader
    if runtime is None or not runtime.claude_row_reader or stages._form_reader is None:
        return {
            "ran": False,
            "reason": "the job values reader is read by the Claude pair: it needs the Claude slot "
            "reader (GV_CLAUDE_READER_ENABLED)",
        }
    if stages._store is None:
        return {"ran": False, "reason": "no artifact store is configured to read the drawings from"}
    task_run = stages_module._task_run_for(session, package_revision_id, "match")
    if task_run is None:
        newest = latest_slot_reader_run(session, package_revision_id)
        run_row = None if newest is None else session.get(ExtractionRun, newest)
        task_run = None if run_row is None else session.get(TaskRun, run_row.task_run_id)
    if task_run is None:
        return {"ran": False, "reason": "no stage run to record the job values reading under"}
    settings = MEASURED_JOB_VALUES_SETTINGS
    base = config_hash(runtime.form.reader_ids, settings.dpi, str(settings.effort))
    wanted = base if request is None else f"{base};again={request}"
    already = session.scalars(
        select(ExtractionRun.id)
        .join(JobValueReading, JobValueReading.extraction_run_id == ExtractionRun.id)
        .where(
            JobValueReading.package_revision_id == package_revision_id,
            ExtractionRun.extractor == JOB_VALUES_EXTRACTOR,
            ExtractionRun.config_hash == wanted,
        )
        .limit(1)
    ).first()
    if already is not None:
        return {
            "ran": True,
            "run": str(already),
            "reason": "this revision's job values reading is already recorded",
        }

    # The revision's documents, each distinct file once: the architect's own file is read whole,
    # a combined sheet only where GV's heading names an architect or prototype drawing.
    separate = has_separate_architect_file(session, package_revision_id)
    records = stages_module._document_records_for(session, package_revision_id)
    kinds = dict(
        session.execute(
            select(DocumentVersion.id, Document.kind)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentVersion.id.in_([record[0] for record in records]))
        ).all()
    )
    pages_by_key: dict[tuple[str, int], Page] = {}
    prepared = []
    not_prepared: list[str] = []
    seen: set[str] = set()
    for version_id, key, sha, _count in records:
        if sha in seen:
            continue
        seen.add(sha)
        try:
            data = stages_module._fetch(stages._store, key)
        except Exception as error:  # noqa: BLE001 - said, never silent
            not_prepared.append(f"a file could not be read: {type(error).__name__}")
            continue
        if hashlib.sha256(data).hexdigest() != sha:
            not_prepared.append("a stored file does not match its digest")
            continue
        architect_file = separate and kinds.get(version_id) == DocumentKind.ARCHITECTURAL.value
        for page in session.scalars(
            select(Page).where(Page.document_version_id == version_id).order_by(Page.index)
        ):
            pages_by_key[(str(version_id), page.index)] = page
        try:
            count = len(read_pages(data))
        except Exception as error:  # noqa: BLE001 - said, never silent
            not_prepared.append(f"a file could not be opened: {type(error).__name__}")
            continue
        for index in range(count):
            try:
                prepared += prepare_pdf_panels(
                    data,
                    index,
                    document_version_id=str(version_id),
                    architect_file=architect_file,
                    settings=settings,
                )
            except Exception as error:  # noqa: BLE001 - said, never silent
                not_prepared.append(
                    f"p.{index + 1}: code could not prepare this page: {type(error).__name__}"
                )

    cap_usd = depth_cap_usd(
        session,
        package_revision_id,
        budget=stages._ai_budget,
        fixed_usd=stages._ai_budget_fixed_usd(),
        claude_ceiling_usd=runtime.spend_cap_usd,
    )
    recorder = stages._call_ledger(
        session,
        task_run.id,
        reader_version=JOB_VALUES_READER_VERSION,
        key_tag="" if request is None else request_ledger_tag(request),
    )
    reader_ids = tuple(runtime.form.reader_ids)
    clients = {model: _PerThread(runtime.form.clients) for model in reader_ids}
    try:
        reading = read_job_values(
            prepared,
            clients=clients,
            settings=settings,
            record_attempt=recorder.record,
            spend_guard=BatchSpendGuard(cap_usd, runtime.form.rates),
            stored=stored_answers(session) if runtime.reuse_answers else None,
            models=reader_ids,
        )
    except Exception as error:  # noqa: BLE001 - reported, never a value; calls stay ledgered
        return {
            "ran": False,
            "reason": f"the job values reading failed: {type(error).__name__}: {error}"[:500],
        }
    try:
        with session.begin_nested():
            run = open_extraction_run(
                session,
                task_run_id=task_run.id,
                extractor=JOB_VALUES_EXTRACTOR,
                extractor_version=JOB_VALUES_READER_VERSION,
                config_hash=wanted,
                dpi=settings.dpi,
            )
            meter = stages._meter
            stages._meter = stages_module._SpendMeter(cap_micros=stages_module._micros(cap_usd))
            try:
                stages._record_reader_attempts(
                    session, run.id, recorder.snapshot(), ledgered=recorder.ledgered_in(session)
                )
                spent = stages._meter.as_payload()
            finally:
                stages._meter = meter
            written = persist_job_values(
                session,
                package_revision_id,
                run,
                reading.results,
                not_read=[*not_prepared, *reading.not_read],
                pages=pages_by_key,
                dpi=settings.dpi,
            )
    except Exception as error:  # noqa: BLE001 - never stops the package
        return {
            "ran": False,
            "reason": "the job values reading could not be recorded: "
            f"{type(error).__name__}: {error}"[:500],
        }
    return {
        "ran": True,
        "run": str(run.id),
        "drawings": len(prepared),
        "asked": reading.asked,
        "not_read": len(reading.not_read) + len(not_prepared),
        "stopped": reading.stopped,
        "cap_usd": str(cap_usd),
        "spend": spent,
        "values": written,
    }
