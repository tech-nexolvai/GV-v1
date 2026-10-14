"""Walk the reviewer-decision tables of one database and draw a candidate from each decision (#1232).

Read-only, enforced: the first statement makes the transaction read-only, so nothing here can change
the run it reads. The caller rolls back or closes the session. No model is called.

Which table corrects which agent, and how its question identity is found, is in
`eval/smriti/__init__.py`. Every identity is checked twice before it is used: the question's packet
must name the decision's own candidate (the span, or the page's candidate rows), and the packet's
page hash must equal the page row's content hash. Anything that does not resolve to exactly one
question with exactly one picture is listed as not convertible, with the reason.

Source: issue #1232 · Verification: `tests/eval/test_smriti_export.py`
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.document import Page
from app.models.evidence import (
    ArchitectPairingRecord,
    ArchitectViewMatchRecord,
    EvidenceSupportingCandidate,
    NoCountertopAnswer,
    ObservationCandidate,
    SlotRowReviewDecision,
)
from app.models.review import CorrectionLedgerEntry, ReviewAction
from app.models.runs import ExtractionRun, ModelInvocation
from app.models.verdicts import Finding
from eval.pariksha.keys import FAMILIES
from eval.pariksha.stored import prompt_family
from eval.smriti.candidates import Candidate, CandidateExport
from vocabulary.semantic_types import SemanticType

__all__ = ["export_candidates"]

LABEL_FAMILY: Final = FAMILIES["akshara"]
ROW_FAMILY: Final = FAMILIES["rekha"]
ROW_CHOICE_FLAG: Final = "slot-reader-row-choice"
NO_ROW_FLAG: Final = "no-row:"
BUDGET_STOPPED: Final = "no-row:budget-reached"

NO_MILAN_KEY: Final = (
    "pariksha-key/1 has no Milan answer yet (it keys Akshara's labels, Rekha's row choice and "
    "Vastu's architect widths); kept and counted until a Milan key exists"
)
NO_WALLS_KEY: Final = (
    "pariksha-key/1 has no walls answer yet (it keys Rekha's row choice only); kept and counted "
    "until a walls key exists"
)
NO_BALA_KEY: Final = (
    "code sorted this page ({code}); pariksha-key/1 has no Bala answer yet, so it is kept and "
    "counted until one exists"
)
SUPERSEDED: Final = "superseded by a later decision on the same {what}; only the newest counts"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _flags(candidate: ObservationCandidate | None) -> list[str]:
    if candidate is None:
        return []
    return [flag for flag in candidate.ambiguity_flags or () if isinstance(flag, str)]


def _flag_value(flags: Iterable[str], prefix: str) -> list[str]:
    return [flag.removeprefix(prefix) for flag in flags if flag.startswith(prefix)]


@dataclass(frozen=True, slots=True)
class _Asked:
    question_id: str
    page_sha256: str | None
    packet_sha256: str | None
    pictures: tuple[str, ...]


class _Questions:
    """Every stored label and row-choice question packet of the database, indexed for lookup."""

    def __init__(self, session: Session) -> None:
        self.labels: dict[str, list[_Asked]] = defaultdict(list)
        self.rows: dict[tuple[str, int, frozenset[str]], list[_Asked]] = defaultdict(list)
        rows = session.execute(
            select(ModelInvocation.prompt_id, ModelInvocation.reader_question_packet).where(
                ModelInvocation.reader_question_packet.is_not(None),
                ModelInvocation.prompt_id.startswith(f"{LABEL_FAMILY}-v")
                | ModelInvocation.prompt_id.startswith(f"{ROW_FAMILY}-v"),
            )
        ).all()
        for prompt_id, packet in rows:
            if not isinstance(packet, Mapping):
                continue
            family = prompt_family(prompt_id)
            question_id = packet.get("question_id")
            candidates = packet.get("candidate_ids")
            if not isinstance(question_id, str) or not isinstance(candidates, list):
                continue
            images = packet.get("images")
            asked = _Asked(
                question_id=question_id,
                page_sha256=_text_or_none(packet.get("source_page_sha256")),
                packet_sha256=_text_or_none(packet.get("packet_sha256")),
                pictures=tuple(
                    sorted(
                        str(image["sha256"])
                        for image in (images.values() if isinstance(images, Mapping) else ())
                        if isinstance(image, Mapping) and isinstance(image.get("sha256"), str)
                    )
                ),
            )
            if family == LABEL_FAMILY and len(candidates) == 1:
                self.labels[str(candidates[0])].append(asked)
            elif family == ROW_FAMILY:
                version = packet.get("document_version_id")
                page_index = packet.get("page_index")
                if isinstance(version, str) and isinstance(page_index, int):
                    self.rows[(version, page_index, frozenset(map(str, candidates)))].append(asked)


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _one_question(
    asked: Sequence[_Asked], page: Page | None, what: str
) -> tuple[_Asked | None, str | None]:
    """The one question these packets ask, or why there is not exactly one."""
    if not asked:
        return None, f"no {what} question in this database names it"
    question_ids = sorted({entry.question_id for entry in asked})
    if len(question_ids) > 1:
        return None, (
            f"{len(question_ids)} {what} questions name it ({', '.join(question_ids)}); which one "
            "the decision answers is not knowable"
        )
    packets = {(entry.packet_sha256, entry.pictures) for entry in asked}
    if len(packets) > 1:
        return None, (
            f"its {what} question was asked with {len(packets)} different packets or pictures; "
            "which one the decision answers is not knowable"
        )
    question = asked[0]
    if page is None or question.page_sha256 is None or question.page_sha256 != page.content_hash:
        return None, "the question's page hash does not match the page it was asked on"
    return question, None


@dataclass(slots=True)
class _Reader:
    session: Session
    questions: _Questions
    pages: dict[UUID, Page]
    candidates: dict[UUID, ObservationCandidate | None]

    def page(self, page_id: UUID | None) -> Page | None:
        if page_id is None:
            return None
        if page_id not in self.pages:
            found = self.session.get(Page, page_id)
            if found is None:
                return None
            self.pages[page_id] = found
        return self.pages[page_id]

    def candidate(self, candidate_id: UUID | None) -> ObservationCandidate | None:
        if candidate_id is None:
            return None
        if candidate_id not in self.candidates:
            self.candidates[candidate_id] = self.session.get(ObservationCandidate, candidate_id)
        return self.candidates[candidate_id]

    def label_question(
        self, candidate: ObservationCandidate
    ) -> tuple[dict[str, str] | None, str | None]:
        """The label question asked about this span, with its slot box, or why not."""
        flags = _flags(candidate)
        if "slot-reader" not in flags:
            return None, "the reading was not read by the slot reader, so no label question asks it"
        boxes = _flag_value(flags, "slot-box:")
        if len(boxes) != 1:
            return None, "the reading has no single recorded slot box to name its span by"
        question, why = _one_question(
            self.questions.labels.get(str(candidate.id), ()),
            self.page(candidate.page_id),
            "label",
        )
        if question is None:
            return None, why
        assert question.page_sha256 is not None
        return {
            "family": LABEL_FAMILY,
            "page_sha256": question.page_sha256,
            "question_id": question.question_id,
            "slot_box": boxes[0],
        }, None

    def row_question(
        self, record: ObservationCandidate
    ) -> tuple[dict[str, str] | None, str | None]:
        """The row-choice question of the page this record answers, with its picture, or why not."""
        rows = frozenset(
            value.split(":", 1)[1]
            for value in _flag_value(_flags(record), "row-candidate:")
            if ":" in value
        )
        if not rows:
            return None, "the row-choice record names no candidate rows, so its question is unknown"
        page = self.page(record.page_id)
        if page is None:
            return None, "the record's page row is missing"
        question, why = _one_question(
            self.questions.rows.get((str(record.document_version_id), page.index, rows), ()),
            page,
            "row-choice",
        )
        if question is None:
            return None, why
        if len(question.pictures) != 1:
            return None, (
                f"the row-choice question carries {len(question.pictures)} pictures; a row case "
                "needs the one numbered picture its box numbers belong to"
            )
        assert question.page_sha256 is not None
        return {
            "family": ROW_FAMILY,
            "page_sha256": question.page_sha256,
            "question_id": question.question_id,
            "picture_sha256": question.pictures[0],
        }, None

    def slot_of_row(
        self, anchor: ObservationCandidate, position: str
    ) -> ObservationCandidate | None:
        """The row's span at `slot:<position>`: same run, same page, same row rank."""
        ranks = _flag_value(_flags(anchor), "row-rank:")
        if len(ranks) != 1:
            return None
        found = [
            candidate
            for candidate in self.session.scalars(
                select(ObservationCandidate).where(
                    ObservationCandidate.extraction_run_id == anchor.extraction_run_id,
                    ObservationCandidate.page_id == anchor.page_id,
                    ObservationCandidate.ambiguity_flags.contains(
                        ["slot-reader", f"slot:{position}", f"row-rank:{ranks[0]}"]
                    ),
                )
            )
        ]
        return found[0] if len(found) == 1 else None


def _page_facts(reader: _Reader, page_id: UUID | None) -> tuple[str | None, int | None]:
    page = reader.page(page_id)
    return (None, None) if page is None else (page.content_hash, page.index + 1)


def _record_agent(record: ObservationCandidate | None) -> tuple[str, str | None]:
    """Whose judgment a listed or split page's record holds, and why it cannot be keyed if not Rekha's."""
    flags = _flags(record)
    if BUDGET_STOPPED in flags:
        return "none", (
            "the AI budget stopped this page before any agent judged it; the answer corrects no "
            "agent"
        )
    codes = _flag_value(flags, NO_ROW_FLAG)
    if codes:
        return "bala", NO_BALA_KEY.format(code=f"no-row:{codes[0]}")
    return "rekha", None


# --- the tables -----------------------------------------------------------------------------------


def _ledger(reader: _Reader) -> list[Candidate]:
    out: list[Candidate] = []
    rows = reader.session.execute(
        select(CorrectionLedgerEntry, ReviewAction.actor)
        .join(ReviewAction, ReviewAction.id == CorrectionLedgerEntry.review_action_id)
        .order_by(CorrectionLedgerEntry.created_at, CorrectionLedgerEntry.id)
    ).all()
    for entry, actor in rows:
        primary_id = reader.session.scalar(
            select(EvidenceSupportingCandidate.candidate_id).where(
                EvidenceSupportingCandidate.canonical_observation_id
                == entry.canonical_observation_id,
                EvidenceSupportingCandidate.role == "primary",
            )
        )
        candidate = reader.candidate(primary_id)
        agent = "akshara"
        question: dict[str, str] | None = None
        why: str | None
        if candidate is None:
            why = "the corrected reading has no primary candidate to find its question by"
        elif "architect-reader" in _flags(candidate):
            agent = "vastu"
            why = (
                "a Vastu key lists every architect width on a page; one corrected width is not a "
                "whole page"
            )
        else:
            question, why = reader.label_question(candidate)
            if why is not None and candidate.extraction_run_id is not None:
                extractor = reader.session.scalar(
                    select(ExtractionRun.extractor).where(
                        ExtractionRun.id == candidate.extraction_run_id
                    )
                )
                why = f"{why} (extractor {extractor})"
        page_sha, page_number = _page_facts(
            reader, None if candidate is None else candidate.page_id
        )
        out.append(
            Candidate(
                candidate_id=f"correction_ledger:{entry.id}",
                table="correction_ledger",
                decision_id=str(entry.id),
                agent=agent,
                corrects="a value a reader read",
                kind="corrected",
                decided_by=actor,
                decided_at=_iso(entry.created_at),
                page_sha256=page_sha,
                page_number=page_number,
                recorded={"original": entry.original_value, "corrected": entry.corrected_value},
                question=question,
                answer=None,
                answer_needed=(
                    None
                    if question is None
                    else "the printed text of the label: the ledger keeps the corrected value, "
                    "and Pariksha compares printed text"
                ),
                not_convertible=why,
            )
        )
    return out


def _measurement_position(key: str) -> str | None:
    if key == "countertop_width":
        return "overall"
    if key.startswith("piece_widths:") and key.removeprefix("piece_widths:").isdigit():
        return key.removeprefix("piece_widths:")
    return None


def _row_part(
    decision: SlotRowReviewDecision,
    page_sha: str | None,
    page_number: int | None,
    earlier: tuple[str, str] | None,
    suffix: str,
    *,
    agent: str,
    corrects: str,
    kind: str,
    recorded: Mapping[str, Any],
    question: dict[str, str] | None,
    answer_needed: str | None,
    why: str | None,
) -> Candidate:
    """One part of a row decision (its walls, or one typed width); an earlier record keys nothing."""
    return Candidate(
        candidate_id=f"slot_row_review_decisions:{decision.id}:{suffix}",
        table="slot_row_review_decisions",
        decision_id=str(decision.id),
        agent=agent,
        corrects=corrects,
        kind=kind if earlier is None else earlier[0],
        decided_by=decision.confirmed_by,
        decided_at=_iso(decision.created_at),
        page_sha256=page_sha,
        page_number=page_number,
        recorded=recorded,
        question=question if earlier is None else None,
        answer=None,
        answer_needed=answer_needed if earlier is None and question is not None else None,
        not_convertible=why if earlier is None else earlier[1],
    )


def _slot_rows(reader: _Reader) -> list[Candidate]:
    decisions = list(
        reader.session.scalars(
            select(SlotRowReviewDecision).order_by(
                SlotRowReviewDecision.created_at, SlotRowReviewDecision.id
            )
        )
    )
    superseded = {decision.supersedes_id for decision in decisions if decision.supersedes_id}
    out: list[Candidate] = []
    for decision in decisions:
        anchor = reader.candidate(decision.row_candidate_id)
        page_sha, page_number = _page_facts(reader, None if anchor is None else anchor.page_id)
        earlier: tuple[str, str] | None = None
        if decision.id in superseded:
            earlier = ("superseded", SUPERSEDED.format(what="row"))
        elif decision.carried_from_id is not None:
            earlier = (
                "carried",
                (
                    f"carried from decision {decision.carried_from_id} on an earlier revision; "
                    "that decision is its own candidate"
                ),
            )

        if decision.wall_config is not None:
            out.append(
                _row_part(
                    decision,
                    page_sha,
                    page_number,
                    earlier,
                    "walls",
                    agent="rekha",
                    corrects="the walls at the row's ends",
                    kind="decided",
                    recorded={SemanticType.WALL_CONFIG.value: decision.wall_config},
                    question=None,
                    answer_needed=None,
                    why=NO_WALLS_KEY,
                )
            )
        for key, value in sorted((decision.measurements or {}).items()):
            position = _measurement_position(key)
            recorded: dict[str, Any] = {"key": key}
            if isinstance(value, Mapping):
                recorded["inches"] = f"{value.get('numerator')}/{value.get('denominator')}"
                if isinstance(value.get("display"), str):
                    recorded["typed"] = value["display"]
            question: dict[str, str] | None = None
            why: str | None
            if anchor is None:
                why = "the row's anchor reading is missing"
            elif position is None:
                why = f"{key!r} names no span of the row"
            else:
                span = reader.slot_of_row(anchor, position)
                if span is None:
                    why = f"no single span at slot {position} of this row"
                else:
                    question, why = reader.label_question(span)
            out.append(
                _row_part(
                    decision,
                    page_sha,
                    page_number,
                    earlier,
                    key,
                    agent="akshara",
                    corrects="a width the readers held, typed by the reviewer",
                    kind="filled",
                    recorded=recorded,
                    question=question,
                    answer_needed=(
                        "the printed text of the label: the reviewer typed a value, not "
                        "necessarily as it is printed"
                    ),
                    why=why,
                )
            )
    return out


def _architect(reader: _Reader) -> list[Candidate]:
    out: list[Candidate] = []
    pairings = list(
        reader.session.scalars(
            select(ArchitectPairingRecord).order_by(
                ArchitectPairingRecord.created_at, ArchitectPairingRecord.id
            )
        )
    )
    replaced = {record.supersedes_id for record in pairings if record.supersedes_id}
    for record in pairings:
        if record.source != "reviewer":
            continue
        page_sha, page_number = _page_facts(reader, record.page_id)
        earlier = record.id in replaced
        out.append(
            Candidate(
                candidate_id=f"architect_pairing_records:{record.id}",
                table="architect_pairing_records",
                decision_id=str(record.id),
                agent="milan",
                corrects="which architect dimension pairs with which vendor piece",
                kind="superseded" if earlier else "decided",
                decided_by=record.decided_by or "",
                decided_at=_iso(record.created_at),
                page_sha256=page_sha,
                page_number=page_number,
                recorded={"pairs": list(record.pairs), "nothing_comparable": not record.pairs},
                question=None,
                answer=None,
                answer_needed=None,
                not_convertible=SUPERSEDED.format(what="row") if earlier else NO_MILAN_KEY,
            )
        )
    matches = list(
        reader.session.scalars(
            select(ArchitectViewMatchRecord).order_by(
                ArchitectViewMatchRecord.created_at, ArchitectViewMatchRecord.id
            )
        )
    )
    replaced = {record.supersedes_id for record in matches if record.supersedes_id}
    for match in matches:
        if match.source != "reviewer":
            continue
        page_sha, page_number = _page_facts(reader, match.vendor_page_id)
        earlier = match.id in replaced
        out.append(
            Candidate(
                candidate_id=f"architect_view_matches:{match.id}",
                table="architect_view_matches",
                decision_id=str(match.id),
                agent="milan",
                corrects="which architect view shows the vendor row",
                kind="superseded" if earlier else "decided",
                decided_by=match.decided_by or "",
                decided_at=_iso(match.created_at),
                page_sha256=page_sha,
                page_number=page_number,
                recorded={
                    "status": match.status,
                    "matched_view_id": (
                        None if match.matched_view_id is None else str(match.matched_view_id)
                    ),
                    "code_pick_view_id": (
                        None if match.code_pick_view_id is None else str(match.code_pick_view_id)
                    ),
                },
                question=None,
                answer=None,
                answer_needed=None,
                not_convertible=SUPERSEDED.format(what="row") if earlier else NO_MILAN_KEY,
            )
        )
    return out


def _no_countertop(reader: _Reader) -> list[Candidate]:
    answers = list(
        reader.session.scalars(
            select(NoCountertopAnswer).order_by(
                NoCountertopAnswer.created_at, NoCountertopAnswer.id
            )
        )
    )
    newest: dict[UUID, UUID] = {}
    for answer in answers:
        newest[answer.row_choice_candidate_id] = answer.id
    out: list[Candidate] = []
    for answer in answers:
        record = reader.candidate(answer.row_choice_candidate_id)
        agent, why = _record_agent(record)
        kind = "confirmed" if answer.answer == "no_countertop" else "corrected"
        question: dict[str, str] | None = None
        key_answer: dict[str, str] | None = None
        if newest[answer.row_choice_candidate_id] != answer.id:
            kind, why = "superseded", SUPERSEDED.format(what="page")
        elif why is None and answer.answer != "no_countertop":
            why = (
                "the reviewer said there is a countertop but named no box; a row case needs "
                "the box"
            )
        elif why is None and record is not None:
            question, why = reader.row_question(record)
            if question is not None and question["page_sha256"] != answer.page_content_hash:
                question, why = None, "the answer's page hash is not the question's"
            if question is not None:
                key_answer = {"kind": "no_countertop"}
        out.append(
            Candidate(
                candidate_id=f"no_countertop_answers:{answer.id}",
                table="no_countertop_answers",
                decision_id=str(answer.id),
                agent=agent,
                corrects="a page listed as having no countertop",
                kind=kind,
                decided_by=answer.answered_by,
                decided_at=_iso(answer.created_at),
                page_sha256=answer.page_content_hash,
                page_number=answer.page_number,
                recorded={"answer": answer.answer, "has_note": answer.note is not None},
                question=question,
                answer=key_answer,
                answer_needed=None,
                not_convertible=why,
            )
        )
    return out


def _unchosen_page_results(reader: _Reader) -> list[Candidate]:
    """The reviewer's decisions on the result of a page whose row was not chosen (split or stopped)."""
    rows = reader.session.execute(
        select(ReviewAction, Finding.scope_row_candidate_id)
        .join(Finding, Finding.id == ReviewAction.finding_id)
        .join(ObservationCandidate, ObservationCandidate.id == Finding.scope_row_candidate_id)
        .where(ObservationCandidate.ambiguity_flags.contains([ROW_CHOICE_FLAG]))
        .order_by(ReviewAction.created_at, ReviewAction.id)
    ).all()
    newest: dict[UUID, UUID] = {}
    for action, _record in rows:
        newest[action.finding_id] = action.id
    out: list[Candidate] = []
    for action, record_id in rows:
        record = reader.candidate(record_id)
        agent, why = _record_agent(record)
        kind = "decided"
        if newest[action.finding_id] != action.id:
            kind, why = "superseded", SUPERSEDED.format(what="page's result")
        elif why is None:
            why = (
                "a decision on the page's result names no box: the product records no reviewer "
                "row pick, so a row case cannot be built"
            )
        page_sha, page_number = _page_facts(reader, None if record is None else record.page_id)
        out.append(
            Candidate(
                candidate_id=f"review_actions:{action.id}",
                table="review_actions",
                decision_id=str(action.id),
                agent=agent,
                corrects="the result of a page whose countertop row was not chosen",
                kind=kind,
                decided_by=action.actor,
                decided_at=_iso(action.created_at),
                page_sha256=page_sha,
                page_number=page_number,
                recorded={"action": action.action},
                question=None,
                answer=None,
                answer_needed=None,
                not_convertible=why,
            )
        )
    return out


def export_candidates(
    session: Session, *, database: str, code_version: str = "unknown"
) -> CandidateExport:
    """Every reviewer decision in this database as a candidate, read-only."""
    session.execute(text("SET TRANSACTION READ ONLY"))
    reader = _Reader(session=session, questions=_Questions(session), pages={}, candidates={})
    candidates = (
        *_ledger(reader),
        *_slot_rows(reader),
        *_architect(reader),
        *_no_countertop(reader),
        *_unchosen_page_results(reader),
    )
    return CandidateExport(
        database=database,
        exported_at=datetime.now(UTC).isoformat(),
        code_version=code_version,
        candidates=tuple(candidates),
    )
