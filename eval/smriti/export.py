"""Walk every table where a person decides something, and draw a candidate from each decision.

Read-only, enforced: the first statement makes the transaction read-only, so nothing here can change
the run it reads. The caller rolls back or closes the session. No model is called.

Which table corrects which agent, and how its question identity is found, is in
`eval/smriti/__init__.py`. Every identity is checked twice before it is used: the question's packet
must name the decision's own candidate (the span, the page's candidate rows, the row's wall
record), and the packet's page hash must equal the page row's content hash. Anything that does not
resolve to exactly one question with exactly one picture is listed as not convertible, with the
reason.

**Who decided** is asked of `eval/smriti/actors.py`, once, for every table: a decision by code
(`code:`) or a record whose source is the machine is never a candidate (it is counted in
`skipped`), and a name not on the local real-reviewer list is listed and never convertible. A
person's decision that replaced an automatic one is marked `overturned` (report only).

Source: issues #1232, #1267 · Verification: `tests/eval/test_smriti_export.py`
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.document import Page
from app.models.drawing import (
    CountertopRun,
    CountertopRunDecision,
    DrawingItem,
    DrawingView,
    PartConfirmation,
    PartProposal,
    PartProposalRow,
    ReadingPart,
    ViewRoleConfirmation,
)
from app.models.evidence import (
    ArchitectPairingRecord,
    ArchitectViewMatchRecord,
    EvidenceSupportingCandidate,
    ItemClassification,
    LayoutConfirmation,
    NoCountertopAnswer,
    ObservationCandidate,
    SlotRowReviewDecision,
)
from app.models.matching import ApprovedMatch
from app.models.parameter_value_citations import ParameterValueCitation
from app.models.parameters import ParameterValue
from app.models.review import CorrectionLedgerEntry, ReviewAction
from app.models.runs import ExtractionRun, ModelInvocation
from app.models.verdicts import Finding
from eval.pariksha.keys import FAMILIES
from eval.pariksha.kinds import KINDS, KINDS_FAMILY
from eval.pariksha.stored import prompt_family
from eval.pariksha.walls import WALLS_FAMILY, _polygon_box
from eval.smriti.actors import (
    Reviewers,
    is_code_actor,
    is_machine_source,
    reviewers_from_environment,
)
from eval.smriti.candidates import MISTAKE_UNANSWERED, Candidate, CandidateExport
from vocabulary.semantic_types import SemanticType
from vocabulary.wall_layouts import AUTOMATIC_WALL_SOURCE
from workflow.layout_proposals import WALL_READER_FLAG, WALLS_SEALED_FLAG

__all__ = ["export_candidates"]

LABEL_FAMILY: Final = FAMILIES["akshara"]
ROW_FAMILY: Final = FAMILIES["rekha"]
ROW_CHOICE_FLAG: Final = "slot-reader-row-choice"
NO_ROW_FLAG: Final = "no-row:"
BUDGET_STOPPED: Final = "no-row:budget-reached"
WALL_SOURCE_FLAG: Final = "wall-source:"
#: Why a record is left out: code's own decision, or a record whose source is the machine.
SKIPPED_CODE: Final = "code"
SKIPPED_MACHINE: Final = "machine source"

NO_MILAN_KEY: Final = (
    "pariksha-key/1 has no Milan answer yet (it keys Akshara's labels, Rekha's row choice and "
    "Vastu's architect widths); kept and counted until a Milan key exists"
)
NO_BALA_KEY: Final = (
    "code sorted this page ({code}); pariksha-key/1 has no Bala answer yet, so it is kept and "
    "counted until one exists"
)
NO_VIEW_ROLE_KEY: Final = (
    "no key scores which drawing a view is (the architect's or the vendor's); kept and counted"
)
NO_COUNTERTOP_ITEM_KEY: Final = (
    "no key scores whether a row is a countertop item; kept and counted (a person undoing code's "
    "item is listed as overturned)"
)
NO_RUNS_KEY: Final = "no key scores which parts sit beneath a countertop; kept and counted"
NO_READING_PART_KEY: Final = "no key scores which part a reading measures; kept and counted"
NO_KHOJ_KEY: Final = (
    "no Khoj key exists (Khoj stays off, decided 2026-10-14); a citation shows its pointer was "
    "right, kept and counted"
)
OLDER_KIND_FORM: Final = (
    "an older form's answer about a rule's input, in the rule's own words; it names no piece of a "
    "read row, so it keys no piece kind; kept and counted"
)
OLDER_LAYOUT_FORM: Final = (
    "an older form's layout answer for the whole revision; it names no row, so it keys no walls "
    "case; kept and counted"
)
FINDING_DECISION: Final = (
    "a decision on a check's result (a finding), not on a reading: it keys no reader and feeds "
    "the report only"
)
VASTU_ONE_WIDTH: Final = (
    "a Vastu key lists every architect width on a page; one decided width is not a whole page"
)
SUPERSEDED: Final = "superseded by a later decision on the same {what}; only the newest counts"
CARRIED: Final = (
    "carried from decision {original} on an earlier revision; that decision is its own candidate"
)
LABEL_TEXT_NEEDED: Final = (
    "the printed text of the label: the ledger keeps the corrected value, and Pariksha compares "
    "printed text"
)
TYPED_TEXT_NEEDED: Final = (
    "the printed text of the label: the reviewer typed a value, not necessarily as it is printed"
)
WALLS_NEEDED: Final = (
    "what the vendor drew at each end (drawn: wall, open, not_shown, or key_unsure); the "
    "reviewer's layout is the site answer (site_layout), never what was drawn"
)
BOX_NEEDED: Final = (
    "the box: which numbered row on the stored picture is the countertop ({'box': n}), or "
    "{'kind': 'not_among_boxes'}"
)
BATCH_NEEDED: Final = (
    "its own look: this page was answered with others in one 'confirm these pages' click, so a "
    "person looks at this page alone and states the answer; a batch click is never a ready case"
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _flags(candidate: ObservationCandidate | None) -> list[str]:
    if candidate is None:
        return []
    return [flag for flag in candidate.ambiguity_flags or () if isinstance(flag, str)]


def _flag_value(flags: Iterable[str], prefix: str) -> list[str]:
    return [flag.removeprefix(prefix) for flag in flags if flag.startswith(prefix)]


def _sealed(span: ObservationCandidate | None) -> bool:
    """A slot-reader span both readers agreed on and the form accepted: no reason held it."""
    return (
        span is not None
        and "slot-reader" in _flags(span)
        and span.review_reason is None
        and span.value_numerator is not None
    )


def _sealed_overturn(span: ObservationCandidate | None) -> dict[str, Any] | None:
    if span is None or not _sealed(span):
        return None
    return {
        "what": "a reading both readers agreed on (sealed)",
        "by": "both readers",
        "answer": span.raw_text,
        "record": f"observation_candidates:{span.id}",
    }


@dataclass(frozen=True, slots=True)
class _Asked:
    question_id: str
    page_sha256: str | None
    packet_sha256: str | None
    pictures: tuple[str, ...]


class _Questions:
    """Every stored label, row-choice and walls question packet of the database, for lookup."""

    def __init__(self, session: Session) -> None:
        self.labels: dict[str, list[_Asked]] = defaultdict(list)
        self.rows: dict[tuple[str, int, frozenset[str]], list[_Asked]] = defaultdict(list)
        self.walls: dict[str, list[_Asked]] = defaultdict(list)
        rows = session.execute(
            select(ModelInvocation.prompt_id, ModelInvocation.reader_question_packet).where(
                ModelInvocation.reader_question_packet.is_not(None),
                ModelInvocation.prompt_id.startswith(f"{LABEL_FAMILY}-v")
                | ModelInvocation.prompt_id.startswith(f"{ROW_FAMILY}-v")
                | ModelInvocation.prompt_id.startswith(f"{WALLS_FAMILY}-v"),
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
            elif family == WALLS_FAMILY and len(candidates) == 1:
                self.walls[str(candidates[0])].append(asked)
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
    skipped: dict[str, Counter[str]]

    def skip(self, table: str, why: str) -> None:
        self.skipped.setdefault(table, Counter())[why] += 1

    def page(self, page_id: UUID | None) -> Page | None:
        if page_id is None:
            return None
        if page_id not in self.pages:
            found = self.session.get(Page, page_id)
            if found is None:
                return None
            self.pages[page_id] = found
        return self.pages[page_id]

    def view_page(self, view_id: UUID | None) -> Page | None:
        view = None if view_id is None else self.session.get(DrawingView, view_id)
        return None if view is None else self.page(view.page_id)

    def candidate(self, candidate_id: UUID | None) -> ObservationCandidate | None:
        if candidate_id is None:
            return None
        if candidate_id not in self.candidates:
            self.candidates[candidate_id] = self.session.get(ObservationCandidate, candidate_id)
        return self.candidates[candidate_id]

    def primary(self, observation_id: UUID) -> ObservationCandidate | None:
        """The reading's primary candidate (the span a reader read), if it has one."""
        return self.candidate(
            self.session.scalar(
                select(EvidenceSupportingCandidate.candidate_id).where(
                    EvidenceSupportingCandidate.canonical_observation_id == observation_id,
                    EvidenceSupportingCandidate.role == "primary",
                )
            )
        )

    def label_question(
        self, candidate: ObservationCandidate
    ) -> tuple[dict[str, Any] | None, str | None]:
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
    ) -> tuple[dict[str, Any] | None, str | None]:
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

    def walls_question(
        self, anchor: ObservationCandidate | None
    ) -> tuple[dict[str, Any] | None, str | None, ObservationCandidate | None]:
        """The walls question asked about this row (its wall record and box), or why not.

        The row's wall record is the run's `wall-reader` candidate on the same page with the same
        row rank as the row's anchor; its one `slot-walls` question names it."""
        if anchor is None:
            return None, "the row's anchor reading is missing", None
        ranks = _flag_value(_flags(anchor), "row-rank:")
        if len(ranks) != 1:
            return (
                None,
                "the row's anchor reading has no single row rank to find its walls by",
                None,
            )
        walls = list(
            self.session.scalars(
                select(ObservationCandidate).where(
                    ObservationCandidate.extraction_run_id == anchor.extraction_run_id,
                    ObservationCandidate.page_id == anchor.page_id,
                    ObservationCandidate.ambiguity_flags.contains(
                        [WALL_READER_FLAG, f"row-rank:{ranks[0]}"]
                    ),
                )
            )
        )
        if not walls:
            return (
                None,
                "no wall question was asked about this row (its run has no wall record)",
                None,
            )
        if len(walls) > 1:
            return (
                None,
                f"{len(walls)} wall records stand for this row; which one is not knowable",
                None,
            )
        wall = walls[0]
        question, why = _one_question(
            self.questions.walls.get(str(wall.id), ()), self.page(wall.page_id), "wall"
        )
        if question is None:
            return None, why, wall
        box = _polygon_box(wall.polygon)
        if box is None:
            return None, "the wall record's row box cannot be read", wall
        assert question.page_sha256 is not None
        return (
            {
                "family": WALLS_FAMILY,
                "page_sha256": question.page_sha256,
                "question_id": question.question_id,
                "row_box": ",".join(str(value) for value in box),
            },
            None,
            wall,
        )

    def piece_question(self, link: PartProposalRow) -> tuple[dict[str, Any] | None, str | None]:
        """The piece-kind question of one piece of a read row (`p<page index>:piece<n>`)."""
        anchor = self.candidate(link.row_candidate_id)
        page = None if anchor is None else self.page(anchor.page_id)
        if anchor is None or page is None:
            return None, "the row's anchor reading or its page is missing"
        slots: set[int] = set()
        ranks: set[str] = set()
        for span in self.session.scalars(
            select(ObservationCandidate).where(
                ObservationCandidate.extraction_run_id == link.extraction_run_id,
                ObservationCandidate.page_id == anchor.page_id,
                ObservationCandidate.ambiguity_flags.contains(["slot-reader"]),
            )
        ):
            flags = _flags(span)
            numbers = [value for value in _flag_value(flags, "slot:") if value.isdigit()]
            if len(numbers) == 1:
                slots.add(int(numbers[0]))
                ranks.update(_flag_value(flags, "row-rank:"))
        if len(ranks) > 1:
            return None, (
                "the run read more than one row on this page; a piece-kind case counts the "
                "page's pieces, so which row it means is not knowable"
            )
        pieces = len(slots)
        if link.slot_index is None or link.slot_index not in slots:
            return None, f"the run read no piece {link.slot_index} on this page"
        return {
            "family": KINDS_FAMILY,
            "page_sha256": page.content_hash,
            "question_id": f"p{page.index}:piece{link.slot_index}",
            "pieces": pieces,
        }, None


def _page_facts(reader: _Reader, page_id: UUID | None) -> tuple[str | None, int | None]:
    page = reader.page(page_id)
    return (None, None) if page is None else (page.content_hash, page.index + 1)


def _page_of(page: Page | None) -> tuple[str | None, int | None]:
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


def _reading(
    reader: _Reader, observation_id: UUID
) -> tuple[str, ObservationCandidate | None, dict[str, Any] | None, str | None]:
    """Whose reading this is, its span, and its label question (or why there is none)."""
    span = reader.primary(observation_id)
    if span is None:
        return "akshara", None, None, "the reading has no primary candidate to find its question by"
    if "architect-reader" in _flags(span):
        return "vastu", span, None, VASTU_ONE_WIDTH
    question, why = reader.label_question(span)
    if why is not None and span.extraction_run_id is not None:
        extractor = reader.session.scalar(
            select(ExtractionRun.extractor).where(ExtractionRun.id == span.extraction_run_id)
        )
        why = f"{why} (extractor {extractor})"
    return "akshara", span, question, why


# --- the tables -----------------------------------------------------------------------------------


def _ledger(reader: _Reader) -> list[Candidate]:
    out: list[Candidate] = []
    rows = reader.session.execute(
        select(CorrectionLedgerEntry, ReviewAction.actor)
        .join(ReviewAction, ReviewAction.id == CorrectionLedgerEntry.review_action_id)
        .order_by(CorrectionLedgerEntry.created_at, CorrectionLedgerEntry.id)
    ).all()
    for entry, actor in rows:
        agent, span, question, why = _reading(reader, entry.canonical_observation_id)
        page_sha, page_number = _page_facts(reader, None if span is None else span.page_id)
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
                answer_needed=None if question is None else LABEL_TEXT_NEEDED,
                not_convertible=why,
                overturned=_sealed_overturn(span),
                mistake_needed=MISTAKE_UNANSWERED,
            )
        )
    return out


def _measurement_position(key: str) -> str | None:
    if key == "countertop_width":
        return "overall"
    if key.startswith("piece_widths:") and key.removeprefix("piece_widths:").isdigit():
        return key.removeprefix("piece_widths:")
    return None


def _wall_overturn(wall: ObservationCandidate | None, chosen: str | None) -> dict[str, Any] | None:
    """The automatic wall layout (code's ink and both AIs) a person's different choice replaced."""
    flags = _flags(wall)
    sealed = _flag_value(flags, WALLS_SEALED_FLAG)
    sources = _flag_value(flags, WALL_SOURCE_FLAG)
    if (
        wall is None
        or chosen is None
        or len(sealed) != 1
        or sources != [AUTOMATIC_WALL_SOURCE]
        or sealed[0] == chosen
    ):
        return None
    return {
        "what": "an automatic wall layout",
        "by": f"{WALL_SOURCE_FLAG}{AUTOMATIC_WALL_SOURCE}",
        "answer": sealed[0],
        "record": f"observation_candidates:{wall.id}",
    }


def _walls_part(
    reader: _Reader,
    *,
    candidate_id: str,
    table: str,
    decision_id: str,
    decided_by: str,
    decided_at: datetime,
    anchor: ObservationCandidate | None,
    wall_config: str,
    earlier: tuple[str, str] | None,
) -> Candidate:
    """A person's wall choice for a row: a `rekha-walls` case whose drawn ends the person states."""
    question, why, wall = reader.walls_question(anchor)
    page_sha, page_number = _page_facts(reader, None if anchor is None else anchor.page_id)
    return Candidate(
        candidate_id=candidate_id,
        table=table,
        decision_id=decision_id,
        agent="rekha",
        corrects="the walls at the row's ends",
        kind="decided" if earlier is None else earlier[0],
        decided_by=decided_by,
        decided_at=_iso(decided_at),
        page_sha256=page_sha,
        page_number=page_number,
        recorded={SemanticType.WALL_CONFIG.value: wall_config},
        question=question if earlier is None else None,
        answer=None,
        answer_needed=WALLS_NEEDED if earlier is None and question is not None else None,
        not_convertible=why if earlier is None else earlier[1],
        overturned=None if earlier is not None else _wall_overturn(wall, wall_config),
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
    table = "slot_row_review_decisions"
    for decision in decisions:
        anchor = reader.candidate(decision.row_candidate_id)
        page_sha, page_number = _page_facts(reader, None if anchor is None else anchor.page_id)
        earlier: tuple[str, str] | None = None
        if decision.id in superseded:
            earlier = ("superseded", SUPERSEDED.format(what="row"))
        elif decision.carried_from_id is not None:
            earlier = ("carried", CARRIED.format(original=decision.carried_from_id))

        if decision.wall_config is not None:
            out.append(
                _walls_part(
                    reader,
                    candidate_id=f"{table}:{decision.id}:walls",
                    table=table,
                    decision_id=str(decision.id),
                    decided_by=decision.confirmed_by,
                    decided_at=decision.created_at,
                    anchor=anchor,
                    wall_config=decision.wall_config,
                    earlier=earlier,
                )
            )
        for key, value in sorted((decision.measurements or {}).items()):
            position = _measurement_position(key)
            recorded: dict[str, Any] = {"key": key}
            if isinstance(value, Mapping):
                recorded["inches"] = f"{value.get('numerator')}/{value.get('denominator')}"
                if isinstance(value.get("display"), str):
                    recorded["typed"] = value["display"]
            question: dict[str, Any] | None = None
            span: ObservationCandidate | None = None
            why: str | None
            if anchor is None:
                why = "the row's anchor reading is missing"
            elif position is None:
                why = f"{key!r} names no span of the row"
            else:
                span = _slot_of_row(reader, anchor, position)
                if span is None:
                    why = f"no single span at slot {position} of this row"
                else:
                    question, why = reader.label_question(span)
            out.append(
                Candidate(
                    candidate_id=f"{table}:{decision.id}:{key}",
                    table=table,
                    decision_id=str(decision.id),
                    agent="akshara",
                    corrects="a width the readers held, typed by the reviewer",
                    kind="filled" if earlier is None else earlier[0],
                    decided_by=decision.confirmed_by,
                    decided_at=_iso(decision.created_at),
                    page_sha256=page_sha,
                    page_number=page_number,
                    recorded=recorded,
                    question=question if earlier is None else None,
                    answer=None,
                    answer_needed=(
                        TYPED_TEXT_NEEDED if earlier is None and question is not None else None
                    ),
                    not_convertible=why if earlier is None else earlier[1],
                    overturned=None if earlier is not None else _sealed_overturn(span),
                    mistake_needed=MISTAKE_UNANSWERED if earlier is None else None,
                )
            )
    return out


def _slot_of_row(
    reader: _Reader, anchor: ObservationCandidate, position: str
) -> ObservationCandidate | None:
    """The row's span at `slot:<position>`: same run, same page, same row rank."""
    ranks = _flag_value(_flags(anchor), "row-rank:")
    if len(ranks) != 1:
        return None
    found = list(
        reader.session.scalars(
            select(ObservationCandidate).where(
                ObservationCandidate.extraction_run_id == anchor.extraction_run_id,
                ObservationCandidate.page_id == anchor.page_id,
                ObservationCandidate.ambiguity_flags.contains(
                    ["slot-reader", f"slot:{position}", f"row-rank:{ranks[0]}"]
                ),
            )
        )
    )
    return found[0] if len(found) == 1 else None


def _architect(reader: _Reader) -> list[Candidate]:
    out: list[Candidate] = []
    pairings = list(
        reader.session.scalars(
            select(ArchitectPairingRecord).order_by(
                ArchitectPairingRecord.created_at, ArchitectPairingRecord.id
            )
        )
    )
    by_id = {record.id: record for record in pairings}
    replaced = {record.supersedes_id for record in pairings if record.supersedes_id}
    for record in pairings:
        if is_machine_source(record.source):
            reader.skip("architect_pairing_records", SKIPPED_MACHINE)
            continue
        page_sha, page_number = _page_facts(reader, record.page_id)
        earlier = record.id in replaced
        previous = by_id.get(record.supersedes_id) if record.supersedes_id else None
        overturned = None
        if (
            not earlier
            and previous is not None
            and previous.source == "code+ais"
            and _pair_ids(previous.pairs) != _pair_ids(record.pairs)
        ):
            overturned = {
                "what": "an automatic pairing (code by drawn position, and both AIs)",
                "by": previous.source,
                "answer": {"pairs": list(previous.pairs)},
                "record": f"architect_pairing_records:{previous.id}",
            }
        out.append(
            Candidate(
                candidate_id=f"architect_pairing_records:{record.id}",
                table="architect_pairing_records",
                decision_id=str(record.id),
                agent="milan",
                corrects="which architect dimension pairs with which vendor piece",
                kind="superseded" if earlier else ("corrected" if overturned else "decided"),
                decided_by=record.decided_by or "",
                decided_at=_iso(record.created_at),
                page_sha256=page_sha,
                page_number=page_number,
                recorded={"pairs": list(record.pairs), "nothing_comparable": not record.pairs},
                question=None,
                answer=None,
                answer_needed=None,
                not_convertible=SUPERSEDED.format(what="row") if earlier else NO_MILAN_KEY,
                overturned=overturned,
            )
        )
    matches = list(
        reader.session.scalars(
            select(ArchitectViewMatchRecord).order_by(
                ArchitectViewMatchRecord.created_at, ArchitectViewMatchRecord.id
            )
        )
    )
    matches_by_id = {match.id: match for match in matches}
    replaced = {record.supersedes_id for record in matches if record.supersedes_id}
    for match in matches:
        if is_machine_source(match.source):
            reader.skip("architect_view_matches", SKIPPED_MACHINE)
            continue
        page_sha, page_number = _page_facts(reader, match.vendor_page_id)
        earlier = match.id in replaced
        before = matches_by_id.get(match.supersedes_id) if match.supersedes_id else None
        view_overturned = None
        if (
            not earlier
            and before is not None
            and before.status == "auto_matched"
            and (
                match.status != "reviewer_confirmed"
                or match.matched_view_id != before.matched_view_id
            )
        ):
            view_overturned = {
                "what": "an automatic architect view match",
                "by": before.source,
                "answer": {"matched_view_id": str(before.matched_view_id)},
                "record": f"architect_view_matches:{before.id}",
            }
        out.append(
            Candidate(
                candidate_id=f"architect_view_matches:{match.id}",
                table="architect_view_matches",
                decision_id=str(match.id),
                agent="milan",
                corrects="which architect view shows the vendor row",
                kind="superseded" if earlier else ("corrected" if view_overturned else "decided"),
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
                overturned=view_overturned,
            )
        )
    return out


def _pair_ids(pairs: object) -> frozenset[str]:
    return frozenset(
        str(pair.get("architect_candidate_id"))
        + ":"
        + ",".join(str(index) for index in pair.get("vendor_slot_indices") or ())
        for pair in (pairs if isinstance(pairs, list) else ())
        if isinstance(pair, Mapping)
    )


def _no_countertop_overturn(record: ObservationCandidate | None) -> dict[str, Any] | None:
    """What said "no countertop" by itself on this page: both AIs agreeing, or code listing it."""
    flags = _flags(record)
    if record is None or BUDGET_STOPPED in flags:
        return None
    models = {
        value.rsplit(":", 1)[0]
        for value in _flag_value(flags, "row-kind:")
        if value.endswith(":no_countertop")
    }
    codes = _flag_value(flags, NO_ROW_FLAG)
    if len(models) >= 2:
        by = "both AIs"
    elif codes:
        by = f"code ({NO_ROW_FLAG}{codes[0]})"
    else:
        return None
    return {
        "what": "a page listed as having no countertop",
        "by": by,
        "answer": "no_countertop",
        "record": f"observation_candidates:{record.id}",
    }


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
        said_countertop = answer.answer != "no_countertop"
        kind = "corrected" if said_countertop else "confirmed"
        batch = answer.batch_id is not None
        question: dict[str, Any] | None = None
        key_answer: dict[str, str] | None = None
        needed: str | None = None
        overturned = None
        if newest[answer.row_choice_candidate_id] != answer.id:
            kind, why = "superseded", SUPERSEDED.format(what="page")
        else:
            if said_countertop:
                overturned = _no_countertop_overturn(record)
            if why is None and record is not None:
                question, why = reader.row_question(record)
                if question is not None and question["page_sha256"] != answer.page_content_hash:
                    question, why = None, "the answer's page hash is not the question's"
            if question is not None:
                if batch:
                    needed = BATCH_NEEDED
                elif said_countertop:
                    needed = BOX_NEEDED
                else:
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
                recorded={
                    "answer": answer.answer,
                    "has_note": answer.note is not None,
                    "batch": batch,
                },
                question=question,
                answer=key_answer,
                answer_needed=needed,
                not_convertible=why,
                overturned=overturned,
            )
        )
    return out


def _review_actions(reader: _Reader) -> list[Candidate]:
    """Every reviewer action not already a ledger entry: a confirm of a reading (a positive label
    case), a decision on a split or stopped page's result, or a decision on a finding."""
    ledgered = set(reader.session.scalars(select(CorrectionLedgerEntry.review_action_id)))
    rows = reader.session.execute(
        select(ReviewAction, Finding.scope_row_candidate_id)
        .join(Finding, Finding.id == ReviewAction.finding_id)
        .order_by(ReviewAction.created_at, ReviewAction.id)
    ).all()

    def group(action: ReviewAction) -> tuple[str, UUID]:
        if action.original_observation_id is not None:
            return ("reading", action.original_observation_id)
        return ("finding", action.finding_id)

    newest: dict[tuple[str, UUID], UUID] = {}
    for action, _scope in rows:
        if action.id not in ledgered:
            newest[group(action)] = action.id
    out: list[Candidate] = []
    for action, scope in rows:
        if action.id in ledgered:
            continue  # the ledger entry is this action's candidate
        record = reader.candidate(scope)
        page_sha, page_number = _page_facts(reader, None if record is None else record.page_id)
        superseded = newest[group(action)] != action.id
        recorded: dict[str, Any] = {"action": action.action}
        question: dict[str, Any] | None = None
        key_answer: dict[str, Any] | None = None
        needed: str | None = None
        if record is not None and ROW_CHOICE_FLAG in _flags(record):
            agent, why = _record_agent(record)
            corrects = "the result of a page whose countertop row was not chosen"
            kind = "decided"
            if why is None:
                why = (
                    "a decision on the page's result names no box: the product records no reviewer "
                    "row pick, so a row case cannot be built"
                )
            what = "page's result"
        elif action.action == "confirm" and action.original_observation_id is not None:
            agent, span, question, why = _reading(reader, action.original_observation_id)
            corrects = "a value a reader read (confirmed right)"
            kind = "confirmed"
            what = "reading"
            if span is not None:
                page_sha, page_number = _page_facts(reader, span.page_id)
                recorded["reading"] = span.raw_text
            if question is not None and span is not None and span.raw_text.strip():
                # A positive case: the answer is the stored reading's text, which the person
                # confirming sees against the picture (and may refuse).
                key_answer = {"texts": [span.raw_text]}
            elif question is not None:
                needed = "the printed text of the label: the stored reading has no text"
        else:
            agent, corrects, kind, why, what = (
                "kautilya",
                "a check's result (a finding)",
                "decided",
                FINDING_DECISION,
                "finding",
            )
        if superseded:
            kind, why = "superseded", SUPERSEDED.format(what=what)
            question, key_answer, needed = None, None, None
        out.append(
            Candidate(
                candidate_id=f"review_actions:{action.id}",
                table="review_actions",
                decision_id=str(action.id),
                agent=agent,
                corrects=corrects,
                kind=kind,
                decided_by=action.actor,
                decided_at=_iso(action.created_at),
                page_sha256=page_sha,
                page_number=page_number,
                recorded=recorded,
                question=question,
                answer=key_answer,
                answer_needed=needed,
                not_convertible=why,
            )
        )
    return out


def _part_confirmations(reader: _Reader) -> list[Candidate]:
    """Bala's parts: a person's piece kind becomes a `bala-kinds` case; a countertop yes/no is
    counted, and a person undoing code's own item is listed as overturned."""
    decisions = list(
        reader.session.scalars(
            select(PartConfirmation).order_by(PartConfirmation.created_at, PartConfirmation.id)
        )
    )
    by_id = {decision.id: decision for decision in decisions}
    replaced = {decision.supersedes_id for decision in decisions if decision.supersedes_id}
    out: list[Candidate] = []
    for decision in decisions:
        proposal = reader.session.get(PartProposal, decision.part_proposal_id)
        link = reader.session.scalar(
            select(PartProposalRow).where(
                PartProposalRow.part_proposal_id == decision.part_proposal_id
            )
        )
        page_sha, page_number = _page_of(
            None if proposal is None else reader.view_page(proposal.drawing_view_id)
        )
        previous = by_id.get(decision.supersedes_id) if decision.supersedes_id else None
        overturned = None
        if (
            previous is not None
            and is_code_actor(previous.confirmed_by)
            and (
                previous.decision,
                previous.kind,
            )
            != (decision.decision, decision.kind)
        ):
            overturned = {
                "what": (
                    "an item code made by itself"
                    if previous.decision == "confirmed"
                    else "code's withdrawal of an item"
                ),
                "by": previous.confirmed_by,
                "answer": {"decision": previous.decision, "kind": previous.kind},
                "record": f"part_confirmations:{previous.id}",
            }
        suggested = None if proposal is None else proposal.kind
        agrees = decision.decision == "confirmed" and decision.kind == suggested
        kind = "confirmed" if agrees and overturned is None else "corrected"
        question: dict[str, Any] | None = None
        key_answer: dict[str, Any] | None = None
        why: str | None
        if decision.id in replaced:
            kind, why, overturned = "superseded", SUPERSEDED.format(what="suggestion"), None
        elif link is None:
            why = (
                "not one of Bala's row suggestions (no part_proposal_rows link), so it names no "
                "piece of a read row"
            )
        elif link.slot_index is None:
            why = NO_COUNTERTOP_ITEM_KEY
        elif decision.decision != "confirmed":
            why = "a withdrawn piece names no kind; kept and counted"
        elif decision.kind not in KINDS:
            why = f"a piece confirmed as a {decision.kind} names no piece kind; kept and counted"
        else:
            question, why = reader.piece_question(link)
            if question is not None:
                key_answer = {"kinds": [decision.kind]}
        out.append(
            Candidate(
                candidate_id=f"part_confirmations:{decision.id}",
                table="part_confirmations",
                decision_id=str(decision.id),
                agent="bala",
                corrects="what a suggested part is (a countertop, a cabinet, a filler) or that it "
                "is none",
                kind=kind,
                decided_by=decision.confirmed_by,
                decided_at=_iso(decision.created_at),
                page_sha256=page_sha,
                page_number=page_number,
                recorded={
                    "decision": decision.decision,
                    "kind": decision.kind,
                    "suggested": suggested,
                    "piece": None if link is None else link.slot_index,
                },
                question=question,
                answer=key_answer,
                answer_needed=None,
                not_convertible=why,
                overturned=overturned,
            )
        )
    return out


def _view_roles(reader: _Reader) -> list[Candidate]:
    rows = list(
        reader.session.scalars(
            select(ViewRoleConfirmation).order_by(
                ViewRoleConfirmation.created_at, ViewRoleConfirmation.id
            )
        )
    )
    per_view: dict[UUID, list[ViewRoleConfirmation]] = defaultdict(list)
    for row in rows:
        per_view[row.drawing_view_id].append(row)
    out: list[Candidate] = []
    for row in rows:
        same_view = per_view[row.drawing_view_id]
        place = same_view.index(row)
        previous = same_view[place - 1] if place > 0 else None
        overturned = None
        if row.carried_from_id is not None:
            kind, why = "carried", CARRIED.format(original=row.carried_from_id)
        elif place < len(same_view) - 1:
            kind, why = "superseded", SUPERSEDED.format(what="view")
        else:
            why = NO_VIEW_ROLE_KEY
            if (
                previous is not None
                and is_code_actor(previous.confirmed_by)
                and (previous.role != row.role)
            ):
                kind = "corrected"
                overturned = {
                    "what": "a view role code set by itself",
                    "by": previous.confirmed_by,
                    "answer": previous.role,
                    "record": f"view_role_confirmations:{previous.id}",
                }
            elif previous is not None and previous.role == row.role:
                kind = "confirmed"
            else:
                kind = "decided"
        page_sha, page_number = _page_of(reader.view_page(row.drawing_view_id))
        out.append(
            Candidate(
                candidate_id=f"view_role_confirmations:{row.id}",
                table="view_role_confirmations",
                decision_id=str(row.id),
                agent="bala",
                corrects="which drawing a view is (the architect's or the vendor's)",
                kind=kind,
                decided_by=row.confirmed_by,
                decided_at=_iso(row.created_at),
                page_sha256=page_sha,
                page_number=page_number,
                recorded={"role": row.role},
                question=None,
                answer=None,
                answer_needed=None,
                not_convertible=why,
                overturned=overturned,
            )
        )
    return out


def _run_decisions(reader: _Reader) -> list[Candidate]:
    """A person's run beneath a countertop (Bala, counted) and its wall choice (a walls case)."""
    decisions = list(
        reader.session.scalars(
            select(CountertopRunDecision).order_by(
                CountertopRunDecision.created_at, CountertopRunDecision.id
            )
        )
    )
    replaced = {decision.supersedes_id for decision in decisions if decision.supersedes_id}
    table = "countertop_run_decisions"
    out: list[Candidate] = []
    for decision in decisions:
        item = reader.session.get(DrawingItem, decision.countertop_item_id)
        page_sha, page_number = _page_of(
            None if item is None else reader.view_page(item.drawing_view_id)
        )
        earlier = (
            ("superseded", SUPERSEDED.format(what="countertop"))
            if decision.id in replaced
            else None
        )
        members = (
            0
            if decision.run_id is None
            else len(
                list(
                    reader.session.scalars(
                        select(CountertopRun.id).where(CountertopRun.run_id == decision.run_id)
                    )
                )
            )
        )
        out.append(
            Candidate(
                candidate_id=f"{table}:{decision.id}:run",
                table=table,
                decision_id=str(decision.id),
                agent="bala",
                corrects="which parts sit beneath a countertop (its run)",
                kind="decided" if earlier is None else earlier[0],
                decided_by=decision.confirmed_by,
                decided_at=_iso(decision.created_at),
                page_sha256=page_sha,
                page_number=page_number,
                recorded={"decision": decision.decision, "members": members},
                question=None,
                answer=None,
                answer_needed=None,
                not_convertible=NO_RUNS_KEY if earlier is None else earlier[1],
            )
        )
        if decision.wall_config is not None:
            out.append(
                _walls_part(
                    reader,
                    candidate_id=f"{table}:{decision.id}:walls",
                    table=table,
                    decision_id=str(decision.id),
                    decided_by=decision.confirmed_by,
                    decided_at=decision.created_at,
                    anchor=_countertop_anchor(reader, decision.countertop_item_id),
                    wall_config=decision.wall_config,
                    earlier=earlier,
                )
            )
    return out


def _countertop_anchor(reader: _Reader, item_id: UUID) -> ObservationCandidate | None:
    """The read row a countertop item was made from: item → its confirmation → Bala's link."""
    proposal_id = reader.session.scalar(
        select(PartConfirmation.part_proposal_id).where(PartConfirmation.drawing_item_id == item_id)
    )
    if proposal_id is None:
        return None
    row = reader.session.scalar(
        select(PartProposalRow.row_candidate_id).where(
            PartProposalRow.part_proposal_id == proposal_id, PartProposalRow.slot_index.is_(None)
        )
    )
    return reader.candidate(row)


def _reading_parts(reader: _Reader) -> list[Candidate]:
    links = list(
        reader.session.scalars(select(ReadingPart).order_by(ReadingPart.created_at, ReadingPart.id))
    )
    replaced = {link.supersedes_id for link in links if link.supersedes_id}
    out: list[Candidate] = []
    for link in links:
        earlier = link.id in replaced
        span = reader.primary(link.canonical_observation_id)
        page_sha, page_number = _page_facts(reader, None if span is None else span.page_id)
        out.append(
            Candidate(
                candidate_id=f"reading_parts:{link.id}",
                table="reading_parts",
                decision_id=str(link.id),
                agent="bala",
                corrects="which confirmed part a reading measures",
                kind="superseded" if earlier else "decided",
                decided_by=link.confirmed_by,
                decided_at=_iso(link.created_at),
                page_sha256=page_sha,
                page_number=page_number,
                recorded={"linked": link.drawing_item_id is not None},
                question=None,
                answer=None,
                answer_needed=None,
                not_convertible=(
                    SUPERSEDED.format(what="reading") if earlier else NO_READING_PART_KEY
                ),
            )
        )
    return out


def _item_classifications(reader: _Reader) -> list[Candidate]:
    rows = list(
        reader.session.scalars(
            select(ItemClassification).order_by(
                ItemClassification.created_at, ItemClassification.id
            )
        )
    )
    newest: dict[tuple[UUID, str, str, int], UUID] = {}
    for row in rows:
        newest[(row.package_revision_id, row.rule_id, row.input_name, row.position)] = row.id
    out: list[Candidate] = []
    for row in rows:
        earlier = (
            newest[(row.package_revision_id, row.rule_id, row.input_name, row.position)] != row.id
        )
        out.append(
            Candidate(
                candidate_id=f"item_classifications:{row.id}",
                table="item_classifications",
                decision_id=str(row.id),
                agent="bala",
                corrects="what one item of an ordered run is, in a rule's own words",
                kind="superseded" if earlier else "decided",
                decided_by=row.confirmed_by,
                decided_at=_iso(row.created_at),
                page_sha256=None,
                page_number=None,
                recorded={
                    "input": row.input_name,
                    "position": row.position,
                    "category": row.category,
                },
                question=None,
                answer=None,
                answer_needed=None,
                not_convertible=SUPERSEDED.format(what="input") if earlier else OLDER_KIND_FORM,
            )
        )
    return out


def _layout_confirmations(reader: _Reader) -> list[Candidate]:
    rows = list(
        reader.session.scalars(
            select(LayoutConfirmation).order_by(
                LayoutConfirmation.created_at, LayoutConfirmation.id
            )
        )
    )
    newest: dict[tuple[UUID, str], UUID] = {}
    for row in rows:
        newest[(row.package_revision_id, row.discriminator_name)] = row.id
    out: list[Candidate] = []
    for row in rows:
        earlier = newest[(row.package_revision_id, row.discriminator_name)] != row.id
        out.append(
            Candidate(
                candidate_id=f"layout_confirmations:{row.id}",
                table="layout_confirmations",
                decision_id=str(row.id),
                agent="rekha",
                corrects="the layout a check uses for a whole revision (older form)",
                kind="superseded" if earlier else "decided",
                decided_by=row.confirmed_by,
                decided_at=_iso(row.created_at),
                page_sha256=None,
                page_number=None,
                recorded={"name": row.discriminator_name, "value": row.value},
                question=None,
                answer=None,
                answer_needed=None,
                not_convertible=(
                    SUPERSEDED.format(what="layout") if earlier else OLDER_LAYOUT_FORM
                ),
            )
        )
    return out


def _citations(reader: _Reader) -> list[Candidate]:
    """Khoj: each setting a person typed from Khoj's pointer. A copy made when a later save
    carried the value is listed as carried; the first is the decision."""
    rows = reader.session.execute(
        select(ParameterValueCitation, ParameterValue)
        .join(ParameterValue, ParameterValue.id == ParameterValueCitation.parameter_value_id)
        .order_by(ParameterValueCitation.created_at, ParameterValueCitation.id)
    ).all()
    first: dict[tuple[object, ...], UUID] = {}
    out: list[Candidate] = []
    for citation, value in rows:
        identity = (
            citation.parameter_proposal_id,
            value.name,
            value.numerator,
            value.denominator,
            value.set_by,
            value.set_at,
        )
        original = first.setdefault(identity, citation.id)
        carried = original != citation.id
        page_sha, page_number = _page_facts(reader, citation.page_id)
        out.append(
            Candidate(
                candidate_id=f"parameter_value_citations:{citation.id}",
                table="parameter_value_citations",
                decision_id=str(citation.id),
                agent="khoj",
                corrects="where a setting is stated (Khoj's pointer)",
                kind="carried" if carried else "confirmed",
                decided_by=value.set_by,
                decided_at=_iso(value.set_at),
                page_sha256=page_sha,
                page_number=page_number,
                recorded={"setting": value.name},
                question=None,
                answer=None,
                answer_needed=None,
                not_convertible=CARRIED.format(original=original) if carried else NO_KHOJ_KEY,
            )
        )
    return out


def _approved_matches(reader: _Reader) -> list[Candidate]:
    out: list[Candidate] = []
    for match in reader.session.scalars(
        select(ApprovedMatch).order_by(ApprovedMatch.created_at, ApprovedMatch.id)
    ):
        if is_machine_source(match.approval_source):
            reader.skip("approved_matches", SKIPPED_MACHINE)
            continue
        out.append(
            Candidate(
                candidate_id=f"approved_matches:{match.id}",
                table="approved_matches",
                decision_id=str(match.id),
                agent="milan",
                corrects="that two items correspond (older matcher)",
                kind="decided",
                decided_by=match.approved_by,
                decided_at=_iso(match.created_at),
                page_sha256=None,
                page_number=None,
                recorded={"lane": match.lane, "revoked": match.revoked_at is not None},
                question=None,
                answer=None,
                answer_needed=None,
                not_convertible=NO_MILAN_KEY,
            )
        )
    return out


def _with_actor(reviewers: Reviewers, candidate: Candidate) -> Candidate:
    """The actor rule, applied once to every candidate: a name not on the list is never convertible."""
    status = reviewers.status(candidate.decided_by)
    why = reviewers.why_not(candidate.decided_by)
    if why is None or not candidate.current:
        return replace(candidate, actor_status=status)
    return replace(candidate, actor_status=status, not_convertible=why)


def export_candidates(
    session: Session,
    *,
    database: str,
    code_version: str = "unknown",
    reviewers: Reviewers | None = None,
) -> CandidateExport:
    """Every person's decision in this database as a candidate, read-only.

    `reviewers` is the local real-reviewer list; `None` reads the `GV_SMRITI_REVIEWERS` setting,
    and with that unset nobody is a known reviewer, so nothing is convertible."""
    people = reviewers_from_environment() if reviewers is None else reviewers
    session.execute(text("SET TRANSACTION READ ONLY"))
    reader = _Reader(
        session=session, questions=_Questions(session), pages={}, candidates={}, skipped={}
    )
    drawn = (
        *_ledger(reader),
        *_slot_rows(reader),
        *_architect(reader),
        *_no_countertop(reader),
        *_review_actions(reader),
        *_part_confirmations(reader),
        *_view_roles(reader),
        *_run_decisions(reader),
        *_reading_parts(reader),
        *_item_classifications(reader),
        *_layout_confirmations(reader),
        *_citations(reader),
        *_approved_matches(reader),
    )
    kept: list[Candidate] = []
    counted: set[tuple[str, str]] = set()
    for candidate in drawn:
        if is_code_actor(candidate.decided_by):
            # Code's decision is never a candidate: it is the machine's answer, counted here (once
            # per decision, however many parts it has) and shown only as what a later person's
            # decision overturned.
            if (candidate.table, candidate.decision_id) not in counted:
                counted.add((candidate.table, candidate.decision_id))
                reader.skip(candidate.table, SKIPPED_CODE)
            continue
        kept.append(_with_actor(people, candidate))
    return CandidateExport(
        database=database,
        exported_at=datetime.now(UTC).isoformat(),
        code_version=code_version,
        candidates=tuple(kept),
        skipped={table: dict(why) for table, why in sorted(reader.skipped.items())},
        reviewer_list=people.source,
    )
