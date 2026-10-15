"""Read a stored run for scoring: each question's identity and every answer to it, read-only (#1219).

**What is read.** For Akshara and Rekha, every `model_invocations` row of one extraction run that
carries a reader question packet under the agent's prompt family: the model, the outcome, the raw
reply, the cost and whether the answer was reused from an earlier call (`reused_from`, #1112). For
a label question, also the span's box from its candidate's `slot-box:` flag. For Vastu, every
architect-reader span (`observation_candidates` flagged `architect-reader`) of one run and one
document, as `eval/architect_reader.ReadSpan`s, the held ones with their printed value read again by
`extraction/architect/labels.read_label`.

**Read-only, enforced.** Every load first makes its transaction read-only, so nothing here can
change the run it measures. The caller rolls back or closes the session.

**Which run.** A database often holds more than one run (a re-run that reused answers, the
countertop and the cabinet package). A load that would mix runs is refused with the list of runs;
the caller names one.

Source: issue #1219 · Verification: `tests/eval/test_pariksha_stored.py`
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Final
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.models.document import DocumentVersion, Page
from app.models.evidence import ObservationCandidate
from app.models.runs import REUSED_FROM_KEY, ExtractionRun, ModelInvocation, TaskRun
from eval.architect_reader import ReadSpan
from eval.pariksha.keys import FAMILIES, QuestionIdentity
from extraction.architect.labels import read_label
from extraction.product_context import PRODUCT_SUFFIX
from extraction.slot_reader.try_again import TRY_KEY_MARK
from workflow.reading_watch import LEDGER_EXTRACTOR, ledger_key_prefix

__all__ = [
    "TRY_FAMILIES",
    "QuestionIdentity",
    "StoredArchitectRun",
    "StoredAttempt",
    "StoredQuestion",
    "StoredRun",
    "StoredRunError",
    "load_architect_run",
    "load_stored_run",
    "prompt_family",
]

_VERSION: Final = re.compile(r"-v\d+$")
#: Each agent's try-again question family (#1309): the number reader's tries are its own question
#: with a new close-up; the line finder's are the tie-break question.
TRY_FAMILIES: Final[Mapping[str, str]] = {
    "akshara": "claude-slot-span",
    "rekha": "slot-row-tiebreak",
}
_FORM_READER: Final = "extraction.form_reader"
#: A ledger run's task key is its stage's key, then this, then a tag (`ledger_key_prefix`).
_LEDGER_KEY: Final = ledger_key_prefix("")
_ARCHITECT_FLAG: Final = "architect-reader"
_OUTLINE: Final = {"yes": True, "no": False}


class StoredRunError(ValueError):
    """The stored run cannot be read as asked: nothing to score, or several runs mixed."""


@dataclass(frozen=True, slots=True)
class StoredAttempt:
    """One recorded answer (or failure) of one reader to one question."""

    model_id: str
    outcome: str
    raw_response: str | None
    cost_micros: int | None
    reused: bool
    """The answer was reused from an earlier identical question (#1112): no call, no cost."""


@dataclass(frozen=True, slots=True)
class StoredQuestion:
    identity: QuestionIdentity
    prompt_id: str
    packet: Mapping[str, object]
    slot_box: str | None
    attempts: tuple[StoredAttempt, ...]

    @property
    def picture_sha256(self) -> tuple[str, ...]:
        images = self.packet.get("images")
        if not isinstance(images, Mapping):
            return ()
        return tuple(
            sorted(
                str(image["sha256"])
                for image in images.values()
                if isinstance(image, Mapping) and isinstance(image.get("sha256"), str)
            )
        )


@dataclass(frozen=True, slots=True)
class StoredRun:
    database: str
    extraction_run_id: str
    extractor: str
    extractor_version: str
    config_hash: str
    questions: tuple[StoredQuestion, ...]
    unidentified: int = 0
    """Answers whose packet names no page hash or question id: counted, never matched."""
    reader_settings: str = ""
    """The form-reader run's settings (readers, prompts, effort, cap): the run's own when it is the
    form-reader run, its stage's form-reader run's when it is the call ledger (#1178); empty when
    neither is found."""


@dataclass(frozen=True, slots=True)
class StoredArchitectRun:
    database: str
    extraction_run_id: str
    extractor: str
    extractor_version: str
    config_hash: str
    document_sha256: str
    pages: Mapping[int, tuple[ReadSpan, ...]]
    """1-based page number → the spans read there, as the architect key counts pages."""


def prompt_family(prompt_id: str) -> str:
    """The prompt id without its product suffix and version: `claude-slot-span-v4` → `claude-slot-span`."""
    return _VERSION.sub("", prompt_id.split(PRODUCT_SUFFIX, 1)[0])


def _read_only(session: Session) -> None:
    session.execute(text("SET TRANSACTION READ ONLY"))


def _run_row(session: Session, run_id: UUID) -> ExtractionRun:
    run = session.get(ExtractionRun, run_id)
    if run is None:
        raise StoredRunError(f"extraction run {run_id} is not in this database")
    return run


def _choose_run(found: Mapping[UUID, int], wanted: UUID | None, what: str) -> UUID:
    if wanted is not None:
        if wanted not in found:
            raise StoredRunError(f"extraction run {wanted} holds no stored {what}")
        return wanted
    if not found:
        raise StoredRunError(f"this database holds no stored {what}")
    if len(found) > 1:
        listed = "; ".join(f"{run} ({count})" for run, count in sorted(found.items(), key=str))
        raise StoredRunError(
            f"{len(found)} extraction runs hold stored {what}: {listed}. Name the one to score; "
            "mixing runs would score one question twice."
        )
    return next(iter(found))


def load_stored_run(
    session: Session,
    *,
    agent: str,
    database: str,
    extraction_run_id: UUID | None = None,
    with_try_again: bool = False,
) -> StoredRun:
    """Every stored question of the agent's family in one run, with every answer to it.

    A try's question (#1309: its id ends `:again<n>`; the line finder's are asked
    `slot-row-tiebreak-v1`) is read only `with_try_again`; without it the run reads exactly as its
    first round, so a scorecard of the first round means what it always did."""
    if agent not in FAMILIES:
        raise StoredRunError(f"{agent!r} has no stored reader questions; use load_architect_run")
    family = FAMILIES[agent]
    families = {family} | ({TRY_FAMILIES[agent]} if with_try_again else set())
    _read_only(session)
    rows = session.execute(
        select(
            ModelInvocation.extraction_run_id,
            ModelInvocation.model_id,
            ModelInvocation.prompt_id,
            ModelInvocation.outcome,
            ModelInvocation.private_raw_response,
            ModelInvocation.cost_micros,
            ModelInvocation.reader_question_packet,
        )
        .where(
            ModelInvocation.extraction_run_id.is_not(None),
            ModelInvocation.reader_question_packet.is_not(None),
            or_(*(ModelInvocation.prompt_id.startswith(f"{name}-v") for name in families)),
        )
        .order_by(ModelInvocation.created_at, ModelInvocation.id)
    ).all()
    rows = [
        row
        for row in rows
        if prompt_family(row.prompt_id) in families
        and (with_try_again or not _is_try(row.reader_question_packet))
    ]
    counts: dict[UUID, int] = defaultdict(int)
    for row in rows:
        assert row.extraction_run_id is not None
        counts[row.extraction_run_id] += 1
    run_id = _choose_run(counts, extraction_run_id, f"{family} questions")
    run = _run_row(session, run_id)

    attempts: dict[QuestionIdentity, list[StoredAttempt]] = defaultdict(list)
    packets: dict[QuestionIdentity, tuple[str, Mapping[str, object]]] = {}
    unidentified = 0
    for row in rows:
        if row.extraction_run_id != run_id:
            continue
        packet = row.reader_question_packet
        page = packet.get("source_page_sha256") if isinstance(packet, Mapping) else None
        question_id = packet.get("question_id") if isinstance(packet, Mapping) else None
        if not isinstance(page, str) or not isinstance(question_id, str):
            unidentified += 1
            continue
        assert isinstance(packet, Mapping)
        identity = QuestionIdentity(
            family=prompt_family(row.prompt_id), page_sha256=page, question_id=question_id
        )
        attempts[identity].append(
            StoredAttempt(
                model_id=row.model_id,
                outcome=row.outcome,
                raw_response=row.private_raw_response,
                cost_micros=row.cost_micros,
                reused=REUSED_FROM_KEY in packet,
            )
        )
        known = packets.setdefault(identity, (row.prompt_id, packet))[1]
        if known.get("packet_sha256") != packet.get("packet_sha256"):
            raise StoredRunError(
                f"run {run_id} asked {identity} with two different question packets "
                f"({str(known.get('packet_sha256'))[:12]}, "
                f"{str(packet.get('packet_sha256'))[:12]}): not one question, so it cannot be "
                "scored as one"
            )

    boxes = _slot_boxes(session, packets.values()) if agent == "akshara" else {}
    questions = []
    for identity, (prompt_id, packet) in packets.items():
        candidates = packet.get("candidate_ids")
        first = (
            str(candidates[0]) if isinstance(candidates, list) and len(candidates) == 1 else None
        )
        questions.append(
            StoredQuestion(
                identity=identity,
                prompt_id=prompt_id,
                packet=packet,
                slot_box=None if first is None else boxes.get(first),
                attempts=tuple(attempts[identity]),
            )
        )
    return StoredRun(
        database=database,
        extraction_run_id=str(run_id),
        extractor=run.extractor,
        extractor_version=run.extractor_version,
        config_hash=run.config_hash,
        questions=tuple(questions),
        unidentified=unidentified,
        reader_settings=_reader_settings(session, run),
    )


def _is_try(packet: object) -> bool:
    """Whether a stored question is a try's (#1309): its id ends `:again<n>`."""
    question_id = packet.get("question_id") if isinstance(packet, Mapping) else None
    return isinstance(question_id, str) and TRY_KEY_MARK in question_id


def _reader_settings(session: Session, run: ExtractionRun) -> str:
    """The settings the readers were asked with, from the form-reader run of the run's stage."""
    if run.extractor == _FORM_READER:
        return run.config_hash
    if run.extractor != LEDGER_EXTRACTOR:
        return ""
    key = session.scalar(select(TaskRun.idempotency_key).where(TaskRun.id == run.task_run_id))
    if not isinstance(key, str) or _LEDGER_KEY not in key:
        return ""
    stage_key = key.split(_LEDGER_KEY, 1)[0]
    settings = session.scalars(
        select(ExtractionRun.config_hash)
        .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
        .where(TaskRun.idempotency_key == stage_key, ExtractionRun.extractor == _FORM_READER)
        .order_by(ExtractionRun.created_at)
    ).all()
    return " | ".join(sorted(set(settings)))


def _slot_boxes(
    session: Session, packets: Iterable[tuple[str, Mapping[str, object]]]
) -> dict[str, str]:
    """The `slot-box:` flag of each label question's one candidate."""
    wanted: set[UUID] = set()
    for _prompt, packet in packets:
        candidates = packet.get("candidate_ids")
        if isinstance(candidates, list) and len(candidates) == 1:
            try:
                wanted.add(UUID(str(candidates[0])))
            except ValueError:
                continue
    if not wanted:
        return {}
    boxes: dict[str, str] = {}
    for candidate_id, flags in session.execute(
        select(ObservationCandidate.id, ObservationCandidate.ambiguity_flags).where(
            ObservationCandidate.id.in_(wanted)
        )
    ):
        for flag in flags or ():
            if isinstance(flag, str) and flag.startswith("slot-box:"):
                boxes[str(candidate_id)] = flag.removeprefix("slot-box:")
                break
    return boxes


def load_architect_run(
    session: Session,
    *,
    database: str,
    extraction_run_id: UUID | None = None,
    document_sha256: str | None = None,
) -> StoredArchitectRun:
    """The architect reader's spans of one run and one document, page by page."""
    _read_only(session)
    rows = session.execute(
        select(
            ObservationCandidate.extraction_run_id,
            DocumentVersion.sha256,
            Page.index.label("page_index"),
            ObservationCandidate.raw_text,
            ObservationCandidate.value_numerator,
            ObservationCandidate.value_denominator,
            ObservationCandidate.review_reason,
            ObservationCandidate.ambiguity_flags,
        )
        .join(Page, Page.id == ObservationCandidate.page_id)
        .join(DocumentVersion, DocumentVersion.id == ObservationCandidate.document_version_id)
        .where(ObservationCandidate.ambiguity_flags.contains([_ARCHITECT_FLAG]))
        .order_by(Page.index, ObservationCandidate.created_at, ObservationCandidate.id)
    ).all()
    if document_sha256 is not None:
        rows = [row for row in rows if row.sha256 == document_sha256]
    counts: dict[UUID, int] = defaultdict(int)
    for row in rows:
        counts[row.extraction_run_id] += 1
    run_id = _choose_run(counts, extraction_run_id, "architect-reader spans")
    rows = [row for row in rows if row.extraction_run_id == run_id]
    documents = sorted({row.sha256 for row in rows})
    if len(documents) != 1:
        raise StoredRunError(
            f"run {run_id} read {len(documents)} documents ({', '.join(d[:12] for d in documents)}); "
            "name the one the key is for"
        )
    run = _run_row(session, run_id)
    pages: dict[int, list[ReadSpan]] = defaultdict(list)
    for row in rows:
        flags = [flag for flag in row.ambiguity_flags or () if isinstance(flag, str)]
        outline = next(
            (
                _OUTLINE.get(flag.removeprefix("arch-ticks-on-outline:"))
                for flag in flags
                if flag.startswith("arch-ticks-on-outline:")
            ),
            None,
        )
        value = (
            None
            if row.value_numerator is None or row.value_denominator is None
            else Fraction(row.value_numerator, row.value_denominator)
        )
        pages[row.page_index + 1].append(
            ReadSpan(
                text=row.raw_text,
                printed_inches=read_label(row.raw_text).printed_inches,
                inches=value,
                held_reason=None if value is not None else (row.review_reason or "held"),
                on_outline=outline,
            )
        )
    return StoredArchitectRun(
        database=database,
        extraction_run_id=str(run_id),
        extractor=run.extractor,
        extractor_version=run.extractor_version,
        config_hash=run.config_hash,
        document_sha256=documents[0],
        pages={page: tuple(spans) for page, spans in sorted(pages.items())},
    )
