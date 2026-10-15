"""The depth reader's readings, as one countertop's operands for the five depth and sink checks (#1290).

**What it serves.** CT-DEPTH-001, CT-SINK-CUTOUT-WIDTH-001, CT-SINK-CUTOUT-DEPTH-001,
CT-SINK-OFFSET-FRONT-001 and CT-BACK-OFFSET-MIN-001 read the countertop's depth (CT010), the sink
cut-out's width (CT012) and front-to-back size (CT008), and the strip in front of the cut-out
(CT007). The depth reader (`workflow/depth_reader.py`) writes each value it read as an
`observation_candidates` row on the view it was read on, and ties each plan or section view to one
of Bala's countertops (`item_view_links`). This module turns those rows into one countertop's
operands, and only that countertop's (`part_operands` asks it for a scoped check).

**Only qualified readings fill a check.** A reading is qualified when both AI readers agreed **and**
code confirmed the number by the exact printed text or by a chain of dimensions that sums to it
(`state:qualified`, lane `SECOND_READER`). Its two answers must still be on record in the reader's
run. A reading confirmed by its drawn length alone (`state:confirm-only`) never fills a check: the
check waits for a person to confirm it (REVIEW_REQUIRED, "Confirm this reading"), the 2026-10-12
confirm-only rule.

**Every hold has a plain reason on the countertop, never silence:**

* the countertop's drawing now holds two or more countertops (as #1247): withheld;
* a plan or section on its drawing could not be tied to one countertop (`unplaced-among:`);
* two different values were read for one countertop (two depths in one plan, a plan and a
  section that disagree): withheld;
* a reading code held, with code's or the readers' reason;
* nothing was read, and why (no view found, the view's kind not settled).

**A person always wins.** A reading a person confirmed (a `HUMAN_CONFIRMED` observation it
supports) is used as the person confirmed it, before any code+AI value; one a person confirmed as
another kind of value no longer counts as this one.

**This only ever withholds what it does not qualify**, and reads nothing but the reader's rows: no
model, no extraction import (the API reaches `part_operands`, `tests/api/test_no_heavy_work.py`).

Source: issue #1290; phase 5 readings design §7 PR 4, §9. Verification:
`tests/workflow/test_depth_evidence.py`.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from typing import Final
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.audit.events import SYSTEM_ACTOR, AuditCategory, emit
from app.evidence.sides import reading_transform
from app.models import (
    DrawingItem,
    ItemViewLink,
    PartConfirmation,
    PartDecision,
)
from app.models.document import Page
from app.models.evidence import (
    CanonicalObservation,
    EvidenceCorroborationLane,
    EvidenceSupportingCandidate,
    ObservationCandidate,
)
from app.models.runs import ExtractionRun, ModelInvocation, TaskRun, WorkflowRun
from evidence.canonical import Authority, CorroborationLane, EvidenceStatus
from evidence.canonical import CanonicalObservation as DomainObservation
from evidence.coordinates import ImagePoint, StoredPoint
from evidence.gate import GateRefusal, seal
from evidence.polygon import Polygon
from rules.schema import Rule
from rules.semantic_types import DocumentRole, SemanticType
from units.measurement import Measurement, Unit
from units.normalise import fold_fractions
from verdict.operands import VerdictOperand
from workflow.item_plan import countertops_by_view

__all__ = [
    "CLAUDE_PAIR",
    "DEPTH_EXTRACTOR",
    "DEPTH_FIELDS",
    "DEPTH_READER_FLAG",
    "DEPTH_READER_VERSION",
    "DEPTH_RULE_IDS",
    "DepthItemOperands",
    "DepthReading",
    "depth_operands",
    "item_proposals",
    "latest_depth_run",
    "person_types",
    "reading_linked",
]

#: The five checks this module fills, per countertop.
DEPTH_RULE_IDS: Final = frozenset(
    {
        "CT-DEPTH-001",
        "CT-SINK-CUTOUT-WIDTH-001",
        "CT-SINK-CUTOUT-DEPTH-001",
        "CT-SINK-OFFSET-FRONT-001",
        "CT-BACK-OFFSET-MIN-001",
    }
)

#: The values the depth reader reads, in plain words for the reviewer.
DEPTH_FIELDS: Final[Mapping[str, str]] = {
    SemanticType.CT010.value: "countertop depth",
    SemanticType.CT012.value: "sink cut-out width",
    SemanticType.CT008.value: "sink cut-out front-to-back size",
    SemanticType.CT007.value: "front offset (cabinet front to the sink cut-out)",
}

#: The reader's run, as `open_extraction_run` records it (`workflow/depth_reader.py` writes it).
DEPTH_EXTRACTOR: Final = "extraction.depth_reader"
DEPTH_READER_VERSION: Final = "depth-reader-v1"
#: The number question's pinned id (`extraction/depth/prompts.DEPTH_SPAN_PROMPT_ID`; a test holds
#: the two the same, since this module may not import extraction code).
DEPTH_SPAN_PROMPT_ID: Final = "claude-depth-span-v1"
#: The decided Claude pair (`extraction/depth/read.CLAUDE_PAIR`; held the same by a test).
CLAUDE_PAIR: Final = ("anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5")

#: The flags the reader writes on each row (`workflow/depth_reader.py`).
DEPTH_READER_FLAG: Final = "depth-reader"
QUALIFIED: Final = "state:qualified"
CONFIRM_ONLY: Final = "state:confirm-only"
HELD: Final = "state:held"
VIEW_NOTE: Final = "state:view-note"
#: A reading that failed as a whole (`workflow/depth_reader.py`): its rows say why, and an earlier
#: good reading is used instead.
RUN_FAILED: Final = "state:run-failed"
#: The two readers' agreed number on a value code held, so a disagreement can still be seen.
AGREED_PREFIX: Final = "agreed:"
#: A reader's own row behind a qualified reading (`supports:<id>`, `reader-id:<model>`).
SUPPORT_FLAG: Final = "depth-reader-support"
FIELD_PREFIX: Final = "field:"
ITEM_PREFIX: Final = "item:"
LINK_PREFIX: Final = "link:"
UNPLACED_PREFIX: Final = "unplaced-among:"
QUESTION_PREFIX: Final = "question:"
VIEW_KIND_PREFIX: Final = "view-kind:"

NOT_READ: Final = (
    "The depth and sink reader has not read this drawing set, so no {what} was read for this "
    "countertop."
)
NOT_BALAS: Final = (
    "This countertop was not confirmed from one of Bala's countertop suggestions, so no plan or "
    "section view is tied to it and no {what} was read for it."
)
SHARED_DRAWING: Final = (
    "This countertop's drawing holds {count} countertops, and a plan or section view cannot be "
    "tied to one of them, so its {what} reading is not used."
)
TWO_VALUES: Final = (
    "Two different values were read for this countertop's {what} ({values}), so which one is "
    "right is not known: held for a person."
)
PEOPLE_DIFFER: Final = (
    "People confirmed two different values for this countertop's {what} ({values}): held."
)
CONFIRM_THIS: Final = (
    "Confirm this reading: {what} {text}, read on the {kind} view. Both AI readers agree, but code "
    "checked it only by its drawn length, which is not enough on its own."
)
NOT_ON_RECORD: Final = (
    "The {what} reading {text} cannot be used: its two readers' answers are not on record in the "
    "reader's run."
)
UNPLACED: Final = (
    "A plan or section view on this countertop's drawing could not be tied to one countertop: "
    "{reason} Its {what} reading is not used."
)
CODE_HELD: Final = "The {what} was not read automatically: {reason}"
NOTHING: Final = "No {what} was read for this countertop: {reason}"
NO_VIEW: Final = "no plan or section view of its drawing was found."
PERSON_EARLIER: Final = (
    "A person confirmed {earlier} for this countertop's {what} on an earlier reading of this view, "
    "and the new reading says {now}: held for a person."
)
RETYPED_EARLIER: Final = (
    "A person said earlier that the {earlier} on this view is not the {what}, and the new reading "
    "of it says {now}: held for a person."
)
NOT_AGAIN: Final = (
    "A person confirmed {earlier} for this countertop's {what} on an earlier reading, and the new "
    "reading does not read that dimension for this countertop again: held for a person."
)
UNAGREED: Final = (
    "Another reading of this countertop's {what} was held without a number both readers agreed "
    "on ({reason}), so {value} is not used: held for a person."
)
READER_FAILED: Final = "The depth and sink reader failed on this drawing set: {reason}"

_QUOTES: Final = {"“": '"', "”": '"', "„": '"', "″": '"', "ʺ": '"', "‘": "'", "’": "'", "′": "'"}


def _plain(text: str) -> str:
    """The width reader's own text fold (`extraction/slot_reader/seal.normalise_text`)."""
    text = fold_fractions(unicodedata.normalize("NFC", text))
    for glyph, plain in _QUOTES.items():
        text = text.replace(glyph, plain)
    text = text.replace("''", '"')
    return " ".join(text.split())


@dataclass(frozen=True)
class DepthItemOperands:
    """One countertop's operands for one check, and what was withheld and why."""

    operands: dict[str, VerdictOperand] = field(default_factory=dict)
    ambiguous: dict[str, str] = field(default_factory=dict)
    """Inputs read and withheld (the check waits for a person), with why."""
    missing: str | None = None
    """Why the check has nothing to go on, when an input has no reading at all."""
    notes: tuple[str, ...] = ()


def depth_runs(session: Session, revision_id: UUID) -> list[ExtractionRun]:
    """The revision's depth-reader runs, newest first."""
    return list(
        session.scalars(
            select(ExtractionRun)
            .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
            .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
            .where(
                WorkflowRun.package_revision_id == revision_id,
                ExtractionRun.extractor == DEPTH_EXTRACTOR,
            )
            .order_by(ExtractionRun.created_at.desc(), ExtractionRun.id.desc())
        )
    )


def _failed(session: Session, run: ExtractionRun) -> bool:
    return (
        session.scalar(
            select(ObservationCandidate.id)
            .where(
                ObservationCandidate.extraction_run_id == run.id,
                ObservationCandidate.ambiguity_flags.contains([RUN_FAILED]),
            )
            .limit(1)
        )
        is not None
    )


def latest_depth_run(session: Session, revision_id: UUID) -> ExtractionRun | None:
    """The revision's newest depth-reader run that did not fail, or `None`."""
    return next(
        (run for run in depth_runs(session, revision_id) if not _failed(session, run)), None
    )


def item_proposals(session: Session, item_id: UUID) -> tuple[UUID | None, UUID | None]:
    """The suggestion behind the item's current confirmation, and the item's drawing view."""
    later = aliased(PartConfirmation)
    row = session.execute(
        select(PartConfirmation.part_proposal_id, DrawingItem.drawing_view_id)
        .join(DrawingItem, DrawingItem.id == PartConfirmation.drawing_item_id)
        .where(
            PartConfirmation.drawing_item_id == item_id,
            PartConfirmation.decision == PartDecision.CONFIRMED.value,
            ~exists().where(later.supersedes_id == PartConfirmation.id),
        )
        .limit(1)
    ).first()
    if row is None:
        return None, None
    return row[0], row[1]


@dataclass(frozen=True)
class DepthReading:
    row: ObservationCandidate
    flags: frozenset[str]

    @property
    def field(self) -> str | None:
        return next(
            (f.removeprefix(FIELD_PREFIX) for f in self.flags if f.startswith(FIELD_PREFIX)), None
        )

    @property
    def kind(self) -> str:
        return next(
            (
                f.removeprefix(VIEW_KIND_PREFIX)
                for f in self.flags
                if f.startswith(VIEW_KIND_PREFIX)
            ),
            "plan or section",
        )

    @property
    def value(self) -> Fraction | None:
        row = self.row
        if row.value_numerator is None or row.value_denominator is None or row.unit != "in":
            return None
        return Fraction(row.value_numerator, row.value_denominator)

    @property
    def reason(self) -> str:
        return (self.row.review_reason or "").strip() or "no reason was recorded"

    @property
    def agreed(self) -> Fraction | None:
        """The two readers' agreed number: the value, or on a held value the one they agreed."""
        if self.value is not None:
            return self.value
        raw = next(
            (f.removeprefix(AGREED_PREFIX) for f in self.flags if f.startswith(AGREED_PREFIX)),
            None,
        )
        try:
            return None if raw is None else Fraction(raw)
        except (ValueError, ZeroDivisionError):
            return None

    @property
    def dimension(self) -> tuple[UUID, frozenset[str]]:
        """Which printed dimension it is: its page, view and span, the same on every reading."""
        return self.row.page_id, frozenset(
            f for f in self.flags if f.startswith(("view:", "span:"))
        )


def _inches(value: Fraction) -> str:
    whole, part = divmod(value, 1)
    if not part:
        return f'{whole}"'
    return f'{whole} {part}"' if whole else f'{part}"'


def _values(values: Sequence[Fraction]) -> str:
    return " and ".join(_inches(value) for value in sorted(set(values)))


def current_link(session: Session, link: ItemViewLink) -> ItemViewLink:
    """The link that stands for `link`'s view now: a person's correction replaces it."""
    seen = {link.id}
    while True:
        later = session.scalar(select(ItemViewLink).where(ItemViewLink.supersedes_id == link.id))
        if later is None or later.id in seen:
            return link
        seen.add(later.id)
        link = later


def reading_linked(session: Session, reading: DepthReading, proposal_id: UUID) -> bool:
    """Whether the reading's view is tied to this countertop by its link as it stands now."""
    link_id = next(
        (f.removeprefix(LINK_PREFIX) for f in reading.flags if f.startswith(LINK_PREFIX)), None
    )
    if link_id is None:
        return False
    try:
        link = session.get(ItemViewLink, UUID(link_id))
    except ValueError:
        return False
    if link is None or link.page_id != reading.row.page_id:
        return False
    link = current_link(session, link)
    if link.judged_by != "person" and f"{ITEM_PREFIX}{proposal_id}" not in reading.flags:
        return False
    return link.part_proposal_id == proposal_id and link.page_id == reading.row.page_id


def person_types(session: Session, row: ObservationCandidate) -> list[CanonicalObservation]:
    """Every observation a person confirmed from this reading, or from either reader's own row of
    it: a person's confirmation is never ignored."""
    ids = [
        row.id,
        *session.scalars(
            select(ObservationCandidate.id).where(
                ObservationCandidate.extraction_run_id == row.extraction_run_id,
                ObservationCandidate.ambiguity_flags.contains([SUPPORT_FLAG, f"supports:{row.id}"]),
            )
        ),
    ]
    return list(
        session.scalars(
            select(CanonicalObservation)
            .join(
                EvidenceSupportingCandidate,
                EvidenceSupportingCandidate.canonical_observation_id == CanonicalObservation.id,
            )
            .where(
                EvidenceSupportingCandidate.candidate_id.in_(ids),
                CanonicalObservation.status == EvidenceStatus.HUMAN_CONFIRMED.value,
            )
            .distinct()
            .order_by(CanonicalObservation.created_at, CanonicalObservation.id)
        )
    )


def _domain(session: Session, row: CanonicalObservation) -> DomainObservation | None:
    """The stored observation as the gate takes it, with the lanes on record."""
    page = session.get(Page, row.page_id)
    if page is None:
        return None
    try:
        lanes = tuple(
            CorroborationLane(value)
            for value in session.scalars(
                select(EvidenceCorroborationLane.lane)
                .where(EvidenceCorroborationLane.canonical_observation_id == row.id)
                .order_by(EvidenceCorroborationLane.lane)
            )
        )
        supporting = tuple(
            str(value)
            for value in session.scalars(
                select(EvidenceSupportingCandidate.candidate_id).where(
                    EvidenceSupportingCandidate.canonical_observation_id == row.id
                )
            )
        )
        return DomainObservation(
            document_version_id=row.document_version_id,
            document_role=DocumentRole(row.document_role),
            page=page.index,
            polygon=Polygon(
                points=tuple(
                    StoredPoint(x=Decimal(str(x)), y=Decimal(str(y))) for x, y in row.polygon
                ),
                space="stored",
                document_version_id=row.document_version_id,
                page=page.index,
            ),
            semantic_type=SemanticType(row.semantic_type),
            value=Measurement(
                Fraction(row.value_numerator, row.value_denominator), Unit(row.unit), None
            ),
            status=EvidenceStatus(row.status),
            authority=Authority(row.authority),
            supported_by=supporting,
            corroborated_by=lanes,
            conflicts_with=(),
            evidence_crop_uri=row.evidence_crop_uri,
        )
    except (ArithmeticError, TypeError, ValueError):
        return None


def _signed(packet: Mapping[str, object]) -> bool:
    body = {
        key: value for key, value in packet.items() if key not in ("packet_sha256", "reused_from")
    }
    actual = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    return packet.get("packet_sha256") == actual.hexdigest()


def answers_on_record(session: Session, reading: DepthReading) -> bool:
    """Whether both readers of the Claude pair answered this very number in the reader's run.

    Each answer must be an `ok` call to the number question under the reading's question id, on its
    document version and page, with its packet's hash intact, saying the label belongs, is readable,
    and reads exactly the reading's text (the width reader's text fold). Two records never stand for
    one reader: both models of the pair must answer.
    """
    row = reading.row
    question = next(
        (f.removeprefix(QUESTION_PREFIX) for f in reading.flags if f.startswith(QUESTION_PREFIX)),
        None,
    )
    page = session.get(Page, row.page_id)
    if question is None or page is None:
        return False
    expected = _plain(row.raw_text)
    if not expected:
        return False
    agreed: set[str] = set()
    for invocation in session.scalars(
        select(ModelInvocation).where(
            ModelInvocation.extraction_run_id == row.extraction_run_id,
            ModelInvocation.prompt_id == DEPTH_SPAN_PROMPT_ID,
            ModelInvocation.outcome == "ok",
            ModelInvocation.private_raw_response.is_not(None),
            ModelInvocation.reader_question_packet["question_id"].astext == question,
        )
    ):
        packet = invocation.reader_question_packet
        if (
            not isinstance(packet, dict)
            or invocation.model_id not in CLAUDE_PAIR
            or packet.get("document_version_id") != str(row.document_version_id)
            or packet.get("page_index") != page.index
            or not _signed(packet)
        ):
            continue
        try:
            answer = json.loads(invocation.private_raw_response or "")
        except ValueError:
            continue
        if (
            isinstance(answer, dict)
            and answer.get("belongs") == "yes"
            and answer.get("readable") is True
            and answer.get("no_dimension") is False
            and isinstance(answer.get("text"), str)
            and _plain(answer["text"]) == expected
        ):
            agreed.add(invocation.model_id)
    return agreed == set(CLAUDE_PAIR)


def reader_supports(session: Session, row: ObservationCandidate) -> list[ObservationCandidate]:
    """The two readers' own rows behind a qualified reading (`depth-reader-support`), or none.

    One per reader of the Claude pair, each with the reading's value, page, run and place: what a
    corroborated observation stands on (two candidates, as the width reader's).
    """
    children = list(
        session.scalars(
            select(ObservationCandidate)
            .where(
                ObservationCandidate.extraction_run_id == row.extraction_run_id,
                ObservationCandidate.ambiguity_flags.contains([SUPPORT_FLAG, f"supports:{row.id}"]),
            )
            .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
        )
    )
    readers = [
        next(
            (f.removeprefix("reader-id:") for f in c.ambiguity_flags if f.startswith("reader-id:")),
            "",
        )
        for c in children
    ]
    if (
        len(children) != 2
        or sorted(readers) != sorted(CLAUDE_PAIR)
        or any(
            (c.value_numerator, c.value_denominator, c.unit, c.page_id, c.polygon)
            != (row.value_numerator, row.value_denominator, row.unit, row.page_id, row.polygon)
            for c in children
        )
    ):
        return []
    return children


def _qualified_canonical(
    session: Session, reading: DepthReading, semantic: str
) -> CanonicalObservation | None:
    """The qualified observation of a code+AI reading, made once and found again after."""
    row = reading.row
    value = reading.value
    if value is None or row.corroboration_lane != CorroborationLane.SECOND_READER.value:
        return None
    children = reader_supports(session, row)
    if not children:
        return None
    existing = list(
        session.scalars(
            select(CanonicalObservation)
            .join(
                EvidenceSupportingCandidate,
                EvidenceSupportingCandidate.canonical_observation_id == CanonicalObservation.id,
            )
            .where(EvidenceSupportingCandidate.candidate_id.in_([c.id for c in children]))
            .distinct()
        )
    )
    if existing:
        if len(existing) != 1:
            return None
        found = existing[0]
        if (
            found.semantic_type != semantic
            or Fraction(found.value_numerator, found.value_denominator) != value
            or found.unit != Unit.INCH.value
            or found.status != EvidenceStatus.CORROBORATED.value
        ):
            return None
        return found
    page = session.get(Page, row.page_id)
    run = session.get(ExtractionRun, row.extraction_run_id)
    if page is None or run is None:
        return None
    transform = reading_transform(page, run)
    if transform is None:
        return None
    try:
        points = tuple(
            transform.to_stored(ImagePoint(int(point[0]), int(point[1]))) for point in row.polygon
        )
    except (TypeError, ValueError):
        return None
    canonical = CanonicalObservation(
        document_version_id=row.document_version_id,
        page_id=row.page_id,
        document_role=DocumentRole.SHOP.value,
        polygon=[[str(point.x), str(point.y)] for point in points],
        coordinate_space="stored",
        semantic_type=semantic,
        value_numerator=value.numerator,
        value_denominator=value.denominator,
        unit=Unit.INCH.value,
        status=EvidenceStatus.CORROBORATED.value,
        authority=Authority.AUTHORITATIVE.value,
        evidence_crop_uri=None,
    )
    session.add(canonical)
    session.flush()
    session.add_all(
        [
            EvidenceSupportingCandidate(
                canonical_observation_id=canonical.id,
                candidate_id=child.id,
                role="primary" if index == 0 else "corroborating",
            )
            for index, child in enumerate(children)
        ]
    )
    session.add(
        EvidenceCorroborationLane(
            canonical_observation_id=canonical.id, lane=CorroborationLane.SECOND_READER.value
        )
    )
    emit(
        session,
        category=AuditCategory.EVIDENCE_QUALIFICATION,
        actor=SYSTEM_ACTOR,
        target_id=canonical.id,
        target_type="canonical_observation",
    )
    session.flush()
    return canonical


def _sealed(session: Session, canonical: CanonicalObservation, name: str) -> VerdictOperand | None:
    domain = _domain(session, canonical)
    if domain is None:
        return None
    operand = seal(domain, name)
    if isinstance(operand, GateRefusal):
        return None
    return replace(operand, evidence_observation_id=str(canonical.id))


type _Decision = VerdictOperand | tuple[str, str]
"""An operand, or `("ambiguous" | "missing", why)`."""


@dataclass(frozen=True)
class _Person:
    """A person's confirmation of one reading of this countertop, on any reading of the set."""

    canonical: CanonicalObservation
    reading: DepthReading

    @property
    def value(self) -> Fraction:
        return Fraction(self.canonical.value_numerator, self.canonical.value_denominator)


def _text(value: Fraction | None) -> str:
    return "no number" if value is None else _inches(value)


def _decide(
    session: Session,
    name: str,
    semantic: str,
    mine: Sequence[DepthReading],
    unplaced: Sequence[DepthReading],
    people: Sequence[_Person],
    notes: list[str],
    run_id: UUID,
) -> _Decision:
    what = DEPTH_FIELDS[semantic]
    for_field = [reading for reading in mine if reading.field == semantic]

    # **A person first, on any reading of this set (design §9.5).** A person's decision on an
    # earlier reading carries over to the same dimension read again identically; a new reading
    # that contradicts it holds for a person.
    retyped = [
        p for p in people if p.reading.field == semantic and p.canonical.semantic_type != semantic
    ]
    for decision in retyped:
        for reading in for_field:
            if reading.dimension == decision.reading.dimension and reading.agreed != decision.value:
                return (
                    "ambiguous",
                    RETYPED_EARLIER.format(
                        earlier=_inches(decision.value), what=what, now=_text(reading.agreed)
                    ),
                )
    retyped_dimensions = {(p.reading.dimension, p.value) for p in retyped}
    for_field = [
        reading
        for reading in for_field
        if (reading.dimension, reading.agreed) not in retyped_dimensions
        and not any(p.reading.row.id == reading.row.id for p in retyped)
    ]
    # Every reading of this field that may be this countertop's, on the newest reading: tied to it,
    # or on its drawing and tied to no one countertop.
    field_rows = [*for_field, *(r for r in unplaced if r.field == semantic)]
    numbers = [r.agreed for r in field_rows if r.agreed is not None]
    unagreed = [r for r in field_rows if r.agreed is None and VIEW_NOTE not in r.flags]

    def conflict(value: Fraction) -> tuple[str, str] | None:
        """**Another number holds both**, and so does a reading held with no agreed number."""
        if len({value, *numbers}) > 1:
            return ("ambiguous", TWO_VALUES.format(what=what, values=_values([value, *numbers])))
        if unagreed:
            return (
                "ambiguous",
                UNAGREED.format(
                    what=what, reason=min(r.reason for r in unagreed), value=_inches(value)
                ),
            )
        return None

    confirmed = [p for p in people if p.canonical.semantic_type == semantic]
    if confirmed:
        values = [p.value for p in confirmed]
        if len(set(values)) > 1:
            return ("ambiguous", PEOPLE_DIFFER.format(what=what, values=_values(values)))
        value = values[0]
        for decision in confirmed:
            if decision.reading.row.extraction_run_id == run_id:
                continue
            # Carried over only on an identical reading (design §9.5): the same dimension, read
            # again for this countertop with the same number.
            again = [r for r in for_field if r.dimension == decision.reading.dimension]
            if not again:
                return ("ambiguous", NOT_AGAIN.format(earlier=_inches(value), what=what))
            for reading in again:
                if reading.agreed != value:
                    return (
                        "ambiguous",
                        PERSON_EARLIER.format(
                            earlier=_inches(value), what=what, now=_text(reading.agreed)
                        ),
                    )
        clash = conflict(value)
        if clash is not None:
            return clash
        operand = _sealed(session, confirmed[0].canonical, name)
        if operand is not None:
            notes.append(
                f"{what.capitalize()}: {_inches(value)}, confirmed by a person "
                f"(observation {confirmed[0].canonical.id})."
            )
            return operand

    qualified = [r for r in for_field if QUALIFIED in r.flags and r.value is not None]
    confirm = [r for r in for_field if CONFIRM_ONLY in r.flags and r.value is not None]
    chosen = next((r.value for r in (*qualified, *confirm) if r.value is not None), None)
    if chosen is not None:
        clash = conflict(chosen)
        if clash is not None:
            return clash
    if qualified:
        reading = qualified[0]
        if not answers_on_record(session, reading):
            return ("missing", NOT_ON_RECORD.format(what=what, text=reading.row.raw_text))
        qualified_row = _qualified_canonical(session, reading, semantic)
        operand = None if qualified_row is None else _sealed(session, qualified_row, name)
        if operand is None:
            return (
                "missing",
                CODE_HELD.format(
                    what=what, reason="its reading could not be placed on the page as evidence."
                ),
            )
        checks = sorted(
            f.removeprefix("confirmed-by:") for f in reading.flags if f.startswith("confirmed-by:")
        )
        notes.append(
            f"{what.capitalize()}: {reading.row.raw_text}, read on the {reading.kind} view by both "
            f"AI readers and confirmed by code ({', '.join(checks) or 'code'}); "
            f"reading {reading.row.id}."
        )
        return operand
    if confirm:
        reading = confirm[0]
        return (
            "ambiguous",
            CONFIRM_THIS.format(what=what, text=reading.row.raw_text, kind=reading.kind),
        )
    held = [r for r in for_field if HELD in r.flags]
    if held:
        reasons = sorted({r.reason for r in held})
        return ("missing", CODE_HELD.format(what=what, reason=" ".join(reasons[:2])))
    away = [r for r in unplaced if r.field == semantic]
    if away:
        valued = [r for r in away if r.value is not None]
        reason = min({r.reason for r in (valued or away)})
        return ("ambiguous" if valued else "missing", UNPLACED.format(reason=reason, what=what))
    views = sorted({r.reason for r in (*mine, *unplaced) if VIEW_NOTE in r.flags})
    return ("missing", NOTHING.format(what=what, reason=" ".join(views[:2]) if views else NO_VIEW))


def _readings(session: Session, run: ExtractionRun) -> list[DepthReading]:
    return [
        reading
        for reading in (
            DepthReading(row, frozenset(row.ambiguity_flags or ()))
            for row in session.scalars(
                select(ObservationCandidate)
                .where(ObservationCandidate.extraction_run_id == run.id)
                .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
            )
        )
        if DEPTH_READER_FLAG in reading.flags
    ]


def depth_operands(
    session: Session, revision_id: UUID, rule: Rule, item_id: UUID
) -> DepthItemOperands:
    """One countertop's operands for one of the five checks, or why each input is withheld."""
    inputs = {
        name: getattr(selector.semantic_type, "value", str(selector.semantic_type))
        for name, selector in (rule.inputs or {}).items()
        if getattr(selector.source, "value", str(selector.source)) == DocumentRole.SHOP.value
        and getattr(selector.semantic_type, "value", str(selector.semantic_type)) in DEPTH_FIELDS
    }
    if not inputs:
        return DepthItemOperands()
    first_what = DEPTH_FIELDS[next(iter(inputs.values()))]
    runs = depth_runs(session, revision_id)
    if not runs:
        return DepthItemOperands(missing=NOT_READ.format(what=first_what))
    proposal_id, view_id = item_proposals(session, item_id)
    if proposal_id is None or view_id is None:
        return DepthItemOperands(missing=NOT_BALAS.format(what=first_what))
    failed = [run for run in runs if _failed(session, run)]
    good = [run for run in runs if run not in failed]
    if not good:
        # Every reading failed: each countertop says so, with the reader's own reason.
        reasons = sorted(
            {
                r.reason
                for r in _readings(session, runs[0])
                if RUN_FAILED in r.flags
                and (
                    f"{UNPLACED_PREFIX}{proposal_id}" in r.flags
                    or not any(f.startswith(UNPLACED_PREFIX) for f in r.flags)
                )
            }
        )
        return DepthItemOperands(
            missing=READER_FAILED.format(reason=" ".join(reasons[:1]) or "no reason was recorded")
        )
    run = good[0]
    readings = _readings(session, run)
    mine = [r for r in readings if reading_linked(session, r, proposal_id)]
    unplaced = [r for r in readings if f"{UNPLACED_PREFIX}{proposal_id}" in r.flags]
    # Notes on a view that names this countertop but whose link does not hold count as unplaced.
    mine_ids = {r.row.id for r in mine}
    unplaced += [
        r
        for r in readings
        if f"{ITEM_PREFIX}{proposal_id}" in r.flags
        and r.row.id not in mine_ids
        and VIEW_NOTE in r.flags
    ]
    # A person's decisions on this countertop's readings, on every good reading of the set.
    people = [
        _Person(canonical, reading)
        for earlier in good
        for reading in (readings if earlier is run else _readings(session, earlier))
        if reading_linked(session, reading, proposal_id)
        for canonical in person_types(session, reading.row)
    ]

    shared = countertops_by_view(session, revision_id).get(view_id, ())
    notes: list[str] = [f"Depth reader run {run.id}; countertop suggestion {proposal_id}."]
    if runs[0] is not run:
        failures = sorted({r.reason for r in _readings(session, runs[0]) if RUN_FAILED in r.flags})
        notes.append(
            f"A later depth reading (run {runs[0].id}) failed "
            f"({' '.join(failures[:1]) or 'no reason'}); "
            "this earlier one is used."
        )
    operands: dict[str, VerdictOperand] = {}
    ambiguous: dict[str, str] = {}
    missing: list[str] = []
    for name, semantic in inputs.items():
        what = DEPTH_FIELDS[semantic]
        if len(shared) > 1:
            read = any(r.field == semantic and r.value is not None for r in (*mine, *unplaced))
            why = SHARED_DRAWING.format(count=len(shared), what=what)
            if read:
                ambiguous[name] = why
            else:
                missing.append(why)
            continue
        decided = _decide(session, name, semantic, mine, unplaced, people, notes, run.id)
        if isinstance(decided, VerdictOperand):
            operands[name] = decided
        elif decided[0] == "ambiguous":
            ambiguous[name] = decided[1]
        else:
            missing.append(decided[1])
    if missing and not ambiguous:
        return DepthItemOperands(
            operands={}, missing=" ".join(dict.fromkeys(missing)), notes=tuple(notes)
        )
    notes.extend(dict.fromkeys(missing))
    return DepthItemOperands(operands=operands, ambiguous=ambiguous, notes=tuple(notes))
