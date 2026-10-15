"""What the depth and sink reader read for each countertop, as the screens show it (#1295).

The depth and sink reader (`workflow/depth_reader.py`, behind `GV_DEPTH_READER`) writes each value
it read on a countertop's plan or section view, and ties each such view to one countertop
(`item_view_links`). `workflow/depth_evidence.py` turns those rows into one countertop's operands.
Nothing showed a person what was read, so a check waiting on a depth said only why it waited.

**Three things for the screens, all read-only:**

* `depth_readings_for`: for each depth or sink check waiting on a value (`NOT_FOUND` or
  `REVIEW_REQUIRED`), one line per value per countertop. What was read, on which view, and how code
  checked it ("read", the printed text or a chain of dimensions); a value only its drawn length
  checks, for a person to confirm through the existing confirm path ("to_confirm"); a value a person
  confirmed ("confirmed") or said is something else ("not_this"); or why it was not used ("held",
  "not_read"), in the reader's own plain words.
* `linked_views_by_proposal`: each countertop suggestion's views and their kind (plan, section,
  side, front, detail), from `item_view_links` as they stand now (a person's correction replaces the
  link it corrects).
* `vendor_sink_notes`: the vendor's printed text naming a sink on the pages the reader read, shown on
  the sink card as a hint. **Never filled into a setting** (Anant, 2026-10-14): the reviewer still
  enters the sink's inside size from its spec sheet.

**Only when the reader produced readings.** With `GV_DEPTH_READER` off nothing is written, so every
function here returns nothing and the screens are exactly as before.

**It decides nothing and writes nothing.** No model, no extraction import (the API reads it,
`tests/api/test_no_heavy_work.py`), and a fixed number of queries whatever the number of
countertops. Which reading a check uses is decided only by `workflow/depth_evidence.py`; a line here
says what was read, never that a check passed.

Source: issue #1295; phase 5 readings design §7 PR 5. Verification:
`tests/api/test_depth_readings.py`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final, Literal, Protocol
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.evidence.crop_marks import current_crop_marks
from app.evidence.sides import ReadingSides
from app.models import (
    DrawingView,
    ItemViewLink,
    PackageRevisionDocument,
    PartConfirmation,
    PartDecision,
    PartProposal,
    RuleSnapshot,
)
from app.models.document import Page
from app.models.evidence import (
    CanonicalObservation,
    EvidenceArtifact,
    EvidenceArtifactKind,
    EvidenceSupportingCandidate,
    ObservationCandidate,
)
from app.models.runs import ExtractionRun
from app.schemas.findings import DepthReadingOut
from app.verdicts.rulebook import UnreadableSnapshot, from_row
from evidence.canonical import EvidenceStatus
from rules.semantic_types import DocumentRole
from rules.snapshot import SnapshotIntegrityError
from units.imperial import format_inches
from verdict.outcomes import Outcome
from workflow.depth_evidence import (
    AGREED_PREFIX,
    CONFIRM_ONLY,
    DEPTH_FIELDS,
    DEPTH_READER_FLAG,
    DEPTH_RULE_IDS,
    FIELD_PREFIX,
    HELD,
    ITEM_PREFIX,
    LINK_PREFIX,
    QUALIFIED,
    RUN_FAILED,
    SUPPORT_FLAG,
    UNPLACED_PREFIX,
    VIEW_KIND_PREFIX,
    VIEW_NOTE,
    depth_runs,
)
from workflow.item_plan import live_countertops

__all__ = [
    "VENDOR_TEXT_EXTRACTORS",
    "VIEW_WORDS",
    "LinkedView",
    "VendorSinkNote",
    "depth_readings_for",
    "linked_views_by_proposal",
    "vendor_sink_notes",
]

ViewWord = Literal["plan", "section", "side", "front", "detail"]

#: A view's kind as a person names it: the vendor's "elevation" is the front view.
VIEW_WORDS: Final[Mapping[str, ViewWord]] = {
    "plan": "plan",
    "section": "section",
    "side": "side",
    "elevation": "front",
    "detail": "detail",
}

#: How code checked a value, in the screen's terms (`extraction/depth/read.py` `confirmed_by`).
_CHECKED_BY: Final[Mapping[str, Literal["printed_text", "chain", "drawn_length"]]] = {
    "vector-text": "printed_text",
    "chain": "chain",
    "drawn-length-only": "drawn_length",
}

#: The results a person can act on: still waiting on a value, or waiting for a person.
_WAITING: Final = frozenset({Outcome.NOT_FOUND, Outcome.REVIEW_REQUIRED})

NO_VIEW: Final = "no plan or section view of its drawing was found."
UNPLACED: Final = "the view it was read on could not be tied to one countertop: {reason}"
#: A person confirmed another number for this dimension on an earlier reading
#: (`depth_evidence.PERSON_EARLIER`, said the same way).
PERSON_EARLIER: Final = (
    "a person confirmed {earlier} for this countertop's {what} on an earlier reading of this view, "
    "and the new reading says {now}: held for a person."
)

#: The vendor's own text layers (`workflow/stages._PRINTED_CODE_EXTRACTORS`, held the same by a
#: test): the vector text, the CAD notes, a pasted drawing's font text and the local OCR. Never GV's
#: markup (the answer key) and never an AI reader's text.
VENDOR_TEXT_EXTRACTORS: Final = frozenset(
    {"pdfplumber", "extraction.cad_text", "extraction.stamp_text", "rapidocr"}
)
#: A sink named in words, not a lone "SINK" label.
_SINK_WORD: Final = re.compile(r"\bsinks?\b", re.IGNORECASE)
#: The most notes the sink card shows: a hint, not a list to read through.
SINK_NOTE_LIMIT: Final = 5


class WaitingDepthFinding(Protocol):
    """What `depth_readings_for` reads of a listed result (`FindingOut`)."""

    @property
    def id(self) -> UUID: ...
    @property
    def package_revision_id(self) -> UUID: ...
    @property
    def rule_id(self) -> str: ...
    @property
    def rule_snapshot_id(self) -> UUID: ...
    @property
    def outcome(self) -> Outcome: ...
    @property
    def scope_item_id(self) -> UUID | None: ...
    @property
    def scope_row_candidate_id(self) -> UUID | None: ...


@dataclass(frozen=True, slots=True)
class _Row:
    row: ObservationCandidate
    page_index: int
    flags: frozenset[str]

    def flag(self, prefix: str) -> str | None:
        return next((f.removeprefix(prefix) for f in self.flags if f.startswith(prefix)), None)

    @property
    def field(self) -> str | None:
        return self.flag(FIELD_PREFIX)

    @property
    def view(self) -> ViewWord | None:
        kind = self.flag(VIEW_KIND_PREFIX)
        return None if kind is None else VIEW_WORDS.get(kind)

    @property
    def value(self) -> Fraction | None:
        row = self.row
        if row.value_numerator is None or row.value_denominator is None or row.unit != "in":
            return None
        return Fraction(row.value_numerator, row.value_denominator)

    @property
    def reason(self) -> str:
        return " ".join((self.row.review_reason or "").split()) or "no reason was recorded."

    @property
    def agreed(self) -> Fraction | None:
        """The two readers' agreed number: the value, or on a held value the one they agreed."""
        if self.value is not None:
            return self.value
        raw = self.flag(AGREED_PREFIX)
        try:
            return None if raw is None else Fraction(raw)
        except (ValueError, ZeroDivisionError):
            return None

    @property
    def dimension(self) -> tuple[UUID, frozenset[str]]:
        """Which printed dimension it is: its page, view and span, the same on every reading
        (`depth_evidence._Reading.dimension`)."""
        return self.row.page_id, frozenset(
            f for f in self.flags if f.startswith(("view:", "span:"))
        )


def _inches(value: Fraction) -> str:
    return f'{format_inches(value)}"'


def _good_runs(session: Session, revision_id: UUID) -> list[UUID]:
    """The revision's depth readings that did not fail, newest first: two queries."""
    runs = depth_runs(session, revision_id)
    if not runs:
        return []
    failed = set(
        session.scalars(
            select(ObservationCandidate.extraction_run_id)
            .where(
                ObservationCandidate.extraction_run_id.in_([run.id for run in runs]),
                ObservationCandidate.ambiguity_flags.contains([RUN_FAILED]),
            )
            .distinct()
        )
    )
    return [run.id for run in runs if run.id not in failed]


def _current(links: Iterable[ItemViewLink]) -> dict[UUID, ItemViewLink]:
    """Each link as it stands now: a person's correction (`supersedes_id`) replaces what it
    corrects, followed in memory (as `depth_evidence.current_link` follows it one query a step)."""
    links = list(links)
    later = {link.supersedes_id: link for link in links if link.supersedes_id is not None}
    current: dict[UUID, ItemViewLink] = {}
    for link in links:
        seen = {link.id}
        now = link
        while (nxt := later.get(now.id)) is not None and nxt.id not in seen:
            seen.add(nxt.id)
            now = nxt
        current[link.id] = now
    return current


@dataclass
class _Reading:
    """One revision's newest good depth reading, loaded once for every countertop and check."""

    rows: list[_Row]
    earlier: list[_Row]
    """The rows of the set's earlier good readings: a person's decision on one carries over to the
    same dimension read again (design §9.5, as `depth_evidence._decide` carries it)."""
    supports: dict[UUID, list[UUID]]
    """A reading's two readers' own rows, by the reading."""
    links: dict[UUID, ItemViewLink]
    """Each link on the read pages, as it stands now (by the link first written)."""
    people: dict[UUID, list[CanonicalObservation]]
    """What a person confirmed from a reading (or either reader's own row of it), by the reading."""
    any_evidence: set[UUID]
    """Readings already standing behind an observation: the confirm path refuses them again."""
    crops: dict[UUID, bool | None]
    """Each reading with a picture, and whether it shows GV's own marks (`None`: not checked)."""

    def linked_to(self, row: _Row, proposal: UUID) -> bool:
        """`depth_evidence._linked`: the reading's view is tied to this countertop now."""
        raw = row.flag(LINK_PREFIX)
        try:
            link = None if raw is None else self.links.get(UUID(raw))
        except ValueError:
            return False
        if link is None or link.page_id != row.row.page_id:
            return False
        if link.judged_by != "person" and f"{ITEM_PREFIX}{proposal}" not in row.flags:
            return False
        return link.part_proposal_id == proposal

    def people_for(self, row: _Row, proposal: UUID) -> tuple[list[CanonicalObservation], bool]:
        """What a person confirmed of this value: on this very reading, or on an earlier reading of
        the same dimension tied to the same countertop. True when it was on this very reading."""
        own = self.people.get(row.row.id, [])
        if own:
            return own, True
        earlier = [
            canonical
            for old in self.earlier
            if old.dimension == row.dimension
            and old.field == row.field
            and self.linked_to(old, proposal)
            for canonical in self.people.get(old.row.id, [])
        ]
        return earlier, False

    def mine(self, proposal: UUID) -> list[_Row]:
        return [row for row in self.rows if self.linked_to(row, proposal)]

    def unplaced(self, proposal: UUID, mine: Sequence[_Row]) -> list[_Row]:
        """Readings on its drawing tied to no one countertop, as `depth_operands` counts them."""
        mine_ids = {row.row.id for row in mine}
        return [
            row
            for row in self.rows
            if f"{UNPLACED_PREFIX}{proposal}" in row.flags
            or (
                f"{ITEM_PREFIX}{proposal}" in row.flags
                and row.row.id not in mine_ids
                and VIEW_NOTE in row.flags
            )
        ]

    def proposals(self) -> set[UUID]:
        found: set[UUID] = set()
        for row in self.rows:
            for flag in row.flags:
                for prefix in (ITEM_PREFIX, UNPLACED_PREFIX):
                    if flag.startswith(prefix):
                        try:
                            found.add(UUID(flag.removeprefix(prefix)))
                        except ValueError:
                            continue
        found |= {link.part_proposal_id for link in self.links.values()}
        return found


def _load(session: Session, run_ids: Sequence[UUID]) -> _Reading:
    """Everything a screen shows of the newest good reading (`run_ids[0]`), and the people's
    decisions on the earlier good ones, in a fixed number of queries."""
    loaded = session.execute(
        select(ObservationCandidate, Page.index)
        .join(Page, Page.id == ObservationCandidate.page_id)
        .where(ObservationCandidate.extraction_run_id.in_(run_ids))
        .order_by(Page.index, ObservationCandidate.created_at, ObservationCandidate.id)
    ).all()
    rows: list[_Row] = []
    earlier: list[_Row] = []
    supports: dict[UUID, list[UUID]] = {}
    for candidate, page_index in loaded:
        flags = frozenset(candidate.ambiguity_flags or ())
        if DEPTH_READER_FLAG in flags:
            row = _Row(candidate, page_index, flags)
            (rows if candidate.extraction_run_id == run_ids[0] else earlier).append(row)
        elif SUPPORT_FLAG in flags:
            parent = next(
                (f.removeprefix("supports:") for f in flags if f.startswith("supports:")), ""
            )
            try:
                supports.setdefault(UUID(parent), []).append(candidate.id)
            except ValueError:
                continue
    pages = {row.row.page_id for row in (*rows, *earlier)}
    links = _current(
        session.scalars(select(ItemViewLink).where(ItemViewLink.page_id.in_(pages)))
        if pages
        else ()
    )
    owner = {child: parent for parent, children in supports.items() for child in children}
    ids = [row.row.id for row in (*rows, *earlier)] + list(owner)
    people: dict[UUID, list[CanonicalObservation]] = {}
    any_evidence: set[UUID] = set()
    if ids:
        for candidate_id, canonical in session.execute(
            select(EvidenceSupportingCandidate.candidate_id, CanonicalObservation)
            .join(
                CanonicalObservation,
                CanonicalObservation.id == EvidenceSupportingCandidate.canonical_observation_id,
            )
            .where(EvidenceSupportingCandidate.candidate_id.in_(ids))
            .order_by(CanonicalObservation.created_at, CanonicalObservation.id)
        ).all():
            if candidate_id not in owner:
                any_evidence.add(candidate_id)
            if canonical.status == EvidenceStatus.HUMAN_CONFIRMED.value:
                reading = owner.get(candidate_id, candidate_id)
                found = people.setdefault(reading, [])
                if canonical not in found:
                    found.append(canonical)
    stored: dict[UUID, bool | None] = {}
    crop_of: dict[UUID, UUID] = {}
    if rows:
        for crop_id, cropped, marks in session.execute(
            select(
                EvidenceArtifact.id, EvidenceArtifact.candidate_id, EvidenceArtifact.shows_gv_marks
            )
            .where(
                EvidenceArtifact.candidate_id.in_([row.row.id for row in rows]),
                EvidenceArtifact.kind == EvidenceArtifactKind.CROP.value,
            )
            .order_by(EvidenceArtifact.created_at, EvidenceArtifact.id)
        ).all():
            if cropped is not None and cropped not in crop_of:
                crop_of[cropped] = crop_id
                stored[crop_id] = marks
    marks_now = current_crop_marks(session, stored)
    crops = {candidate: marks_now[crop] for candidate, crop in crop_of.items()}
    return _Reading(rows, earlier, supports, links, people, any_evidence, crops)


def _labels(
    session: Session, revision_id: UUID, proposals: set[UUID]
) -> tuple[dict[UUID, str], dict[UUID, int], dict[UUID, UUID], set[UUID]]:
    """Each countertop suggestion's name on the screens, its page, and each live item's suggestion.

    A suggestion behind a live countertop item takes the item's own name ("Countertop 1 on page 3",
    as on the plan and the results screen); one nobody confirmed yet is "the countertop suggested on
    page 3". Three queries, whatever the number of countertops.
    """
    later = aliased(PartConfirmation)
    live = {
        countertop.item_id: countertop
        for countertop in live_countertops(session, revision_id, follow_rows=False)
    }
    item_proposal: dict[UUID, UUID] = {}
    if live:
        item_proposal = {
            item: proposal
            for item, proposal in session.execute(
                select(PartConfirmation.drawing_item_id, PartConfirmation.part_proposal_id).where(
                    PartConfirmation.drawing_item_id.in_(list(live)),
                    PartConfirmation.decision == PartDecision.CONFIRMED.value,
                    ~exists().where(later.supersedes_id == PartConfirmation.id),
                )
            ).all()
            if item is not None
        }
    pages = dict(
        session.execute(
            select(PartProposal.id, Page.index)
            .join(DrawingView, DrawingView.id == PartProposal.drawing_view_id)
            .join(Page, Page.id == DrawingView.page_id)
            .where(PartProposal.id.in_(proposals | set(item_proposal.values())))
        ).all()
    )
    labels = {
        proposal: f"The countertop suggested on page {index + 1}"
        for proposal, index in pages.items()
    }
    for item, proposal in item_proposal.items():
        labels[proposal] = live[item].label
        pages[proposal] = live[item].page_index
    # The items alone on their drawing: only there is a confirmed reading this countertop's alone.
    on_view: dict[UUID, int] = {}
    for countertop in live.values():
        on_view[countertop.view_id] = on_view.get(countertop.view_id, 0) + 1
    alone = {item for item, countertop in live.items() if on_view[countertop.view_id] == 1}
    return labels, pages, item_proposal, alone


def _rule_fields(session: Session, snapshot_ids: set[UUID]) -> dict[UUID, list[str]]:
    """The drawn depth and sink values each rule reads from the vendor's drawing, in its own order."""
    fields: dict[UUID, list[str]] = {}
    for row in session.scalars(select(RuleSnapshot).where(RuleSnapshot.id.in_(snapshot_ids))):
        try:
            rule = from_row(row).rule
        except (UnreadableSnapshot, SnapshotIntegrityError):
            continue
        wanted: list[str] = []
        for selector in (rule.inputs or {}).values():
            source = getattr(selector.source, "value", str(selector.source))
            semantic = getattr(selector.semantic_type, "value", str(selector.semantic_type))
            if (
                source == DocumentRole.SHOP.value
                and semantic in DEPTH_FIELDS
                and semantic not in wanted
            ):
                wanted.append(semantic)
        fields[row.id] = wanted
    return fields


def _label(field: str) -> str:
    return DEPTH_FIELDS[field][:1].upper() + DEPTH_FIELDS[field][1:]


def _line(
    reading: _Reading,
    row: _Row,
    field: str,
    countertop: str,
    proposal: UUID,
    *,
    tied: bool,
    may_confirm: bool,
) -> DepthReadingOut:
    """One value as read, in the screen's terms. A view not tied to one countertop is held.

    `may_confirm`: only on a countertop's own result, alone on its drawing, is a reading offered
    for confirming: a confirmation is evidence for that countertop's own check only."""
    value = row.value
    line = DepthReadingOut(
        countertop=countertop,
        field=field,
        label=_label(field),
        state="held",
        value=None if value is None else _inches(value),
        view=row.view,
        page_number=row.page_index + 1,
        checked_by=_CHECKED_BY.get(row.flag("confirmed-by:") or ""),
        candidate_id=row.row.id,
        has_picture=row.row.id in reading.crops,
        picture_shows_gv_mark=reading.crops.get(row.row.id),
        can_confirm=False,
    )
    people, here = reading.people_for(row, proposal) if tied else ([], False)
    if people:
        same = [p for p in people if p.semantic_type == field]
        if same:
            confirmed = Fraction(same[0].value_numerator, same[0].value_denominator)
            if here or row.agreed == confirmed:
                return line.model_copy(update={"state": "confirmed", "value": _inches(confirmed)})
            return line.model_copy(
                update={
                    "reason": PERSON_EARLIER.format(
                        earlier=_inches(confirmed),
                        what=DEPTH_FIELDS[field],
                        now="no number" if row.agreed is None else _inches(row.agreed),
                    )
                }
            )
        return line.model_copy(update={"state": "not_this"})
    if not tied:
        return line.model_copy(update={"reason": UNPLACED.format(reason=row.reason)})
    if QUALIFIED in row.flags and value is not None:
        return line.model_copy(update={"state": "read"})
    if CONFIRM_ONLY in row.flags and value is not None:
        return line.model_copy(
            update={
                "state": "to_confirm",
                # The existing confirm path needs the reading's own picture, and refuses a reading
                # already standing behind an observation.
                "can_confirm": may_confirm
                and row.row.id in reading.crops
                and row.row.id not in reading.any_evidence,
            }
        )
    return line.model_copy(
        update={
            "value": None,
            "checked_by": None,
            "reason": row.reason if HELD in row.flags else "no number was used from it.",
        }
    )


def _countertop_lines(
    reading: _Reading,
    proposal: UUID,
    fields: Sequence[str],
    countertop: str,
    *,
    every_field: bool,
    may_confirm: bool,
) -> list[DepthReadingOut]:
    """One countertop's lines for one check: each value read, and with `every_field` a line for a
    value nothing was read for, with why. `may_confirm` as `_line`."""
    mine = reading.mine(proposal)
    unplaced = reading.unplaced(proposal, mine)
    lines: list[DepthReadingOut] = []
    for field in fields:
        tied = [row for row in mine if row.field == field and VIEW_NOTE not in row.flags]
        away = [row for row in unplaced if row.field == field and VIEW_NOTE not in row.flags]
        lines += [
            _line(reading, row, field, countertop, proposal, tied=True, may_confirm=may_confirm)
            for row in tied
        ]
        lines += [
            _line(reading, row, field, countertop, proposal, tied=False, may_confirm=False)
            for row in away
        ]
        if not tied and not away and every_field:
            notes = sorted({row.reason for row in (*mine, *unplaced) if VIEW_NOTE in row.flags})
            lines.append(
                DepthReadingOut(
                    countertop=countertop,
                    field=field,
                    label=_label(field),
                    state="not_read",
                    value=None,
                    view=None,
                    page_number=None,
                    candidate_id=None,
                    has_picture=False,
                    picture_shows_gv_mark=None,
                    checked_by=None,
                    can_confirm=False,
                    reason=" ".join(notes[:2]) if notes else NO_VIEW,
                )
            )
    return lines


def depth_readings_for(
    session: Session, items: Sequence[WaitingDepthFinding]
) -> dict[UUID, list[DepthReadingOut]]:
    """For each depth or sink check in `items` still waiting, what the reader read, per countertop.

    A countertop's own result (`scope_item_id`) gets a line for every value its check reads, read or
    not. A result for the whole package (the five checks run once per revision where no countertop
    is a check scope) gets a line for each value the reader did read, countertop by countertop. A
    result that decided, or any other check, gets nothing; so does every result of a revision the
    reader never read. A fixed number of queries per revision, whatever the number of countertops.
    """
    waiting = [
        item
        for item in items
        if item.rule_id in DEPTH_RULE_IDS
        and item.outcome in _WAITING
        and item.scope_row_candidate_id is None
    ]
    if not waiting:
        return {}
    answers: dict[UUID, list[DepthReadingOut]] = {}
    fields = _rule_fields(session, {item.rule_snapshot_id for item in waiting})
    for revision_id in sorted({item.package_revision_id for item in waiting}, key=str):
        run_ids = _good_runs(session, revision_id)
        if not run_ids:
            continue
        reading = _load(session, run_ids)
        proposals = reading.proposals()
        labels, pages, item_proposal, alone = _labels(session, revision_id, proposals)
        ordered = sorted(
            proposals, key=lambda p: (pages.get(p, 1 << 30), labels.get(p, ""), str(p))
        )
        for item in (i for i in waiting if i.package_revision_id == revision_id):
            wanted = fields.get(item.rule_snapshot_id, [])
            if not wanted:
                continue
            lines: list[DepthReadingOut] = []
            if item.scope_item_id is not None:
                proposal = item_proposal.get(item.scope_item_id)
                if proposal is not None:
                    lines = _countertop_lines(
                        reading,
                        proposal,
                        wanted,
                        labels.get(proposal, "This countertop"),
                        every_field=True,
                        # Re-checked here: the countertop's drawing must hold it alone.
                        may_confirm=item.scope_item_id in alone,
                    )
            else:
                for proposal in ordered:
                    lines += _countertop_lines(
                        reading,
                        proposal,
                        wanted,
                        labels.get(proposal, "A countertop"),
                        every_field=False,
                        # Never on a whole-package result: a person's confirmation is one
                        # countertop's evidence, and the package's check cannot use it (#1296).
                        may_confirm=False,
                    )
            if lines:
                answers[item.id] = lines
    return answers


@dataclass(frozen=True, slots=True)
class LinkedView:
    """One view tied to a countertop suggestion, as it stands now."""

    view: ViewWord
    page_number: int
    heading: str | None
    by_person: bool


def linked_views_by_proposal(session: Session, revision_id: UUID) -> dict[UUID, list[LinkedView]]:
    """Each countertop suggestion's tied views and their kind, from `item_view_links` (two queries).

    Only the link as it stands now counts: a person's correction replaces the link it corrects, so
    a view a person moved to another countertop is listed under that one. Empty when the reader
    never ran.
    """
    rows = session.execute(
        select(ItemViewLink, Page.index)
        .join(Page, Page.id == ItemViewLink.page_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == Page.document_version_id,
        )
        .where(PackageRevisionDocument.package_revision_id == revision_id)
        .order_by(Page.index, ItemViewLink.created_at, ItemViewLink.id)
    ).all()
    if not rows:
        return {}
    pages = {link.id: index for link, index in rows}
    current = _current(link for link, _ in rows)
    found: dict[UUID, list[LinkedView]] = {}
    for link, _ in rows:
        if link.supersedes_id is not None:
            continue
        now = current[link.id]
        word = VIEW_WORDS.get(now.view_kind)
        if word is None:
            continue
        found.setdefault(now.part_proposal_id, []).append(
            LinkedView(
                view=word,
                page_number=pages.get(now.id, pages[link.id]) + 1,
                heading=(now.heading_text or "").strip() or None,
                by_person=now.judged_by == "person",
            )
        )
    return found


@dataclass(frozen=True, slots=True)
class VendorSinkNote:
    """The vendor's printed words naming a sink, and the page they are on."""

    text: str
    page_number: int


def vendor_sink_notes(session: Session, revision_id: UUID) -> list[VendorSinkNote]:
    """The vendor's printed text naming a sink, on the pages the depth reader read (a hint only).

    Only the vendor's own text layers (`VENDOR_TEXT_EXTRACTORS`), only text on the vendor's side of
    the sheet (`ReadingSides`), and only words that name a sink with something more than the word
    itself (a lone "SINK" label names nothing). A note inside another on the same page is left out.
    **It is never filled into a setting**: the sink's inside size is typed from its spec sheet.
    Empty when the reader never read the set.
    """
    good = _good_runs(session, revision_id)
    if not good:
        return []
    run_id = good[0]
    pages = set(
        session.scalars(
            select(ObservationCandidate.page_id)
            .where(
                ObservationCandidate.extraction_run_id == run_id,
                ObservationCandidate.ambiguity_flags.contains([DEPTH_READER_FLAG]),
            )
            .distinct()
        )
    )
    if not pages:
        return []
    rows = session.execute(
        select(ObservationCandidate, Page.index)
        .join(Page, Page.id == ObservationCandidate.page_id)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .where(
            ObservationCandidate.page_id.in_(pages),
            ExtractionRun.extractor.in_(VENDOR_TEXT_EXTRACTORS),
            ObservationCandidate.raw_text.op("~*")(r"\msinks?\M"),
        )
        .order_by(Page.index, ObservationCandidate.created_at, ObservationCandidate.id)
    ).all()
    sides = ReadingSides(session)
    kept: list[VendorSinkNote] = []
    for candidate, index in rows:
        text = " ".join(candidate.raw_text.split())
        if not _SINK_WORD.search(text) or len(_SINK_WORD.sub("", text).strip(" -:.,")) == 0:
            continue
        if sides.of(candidate) != DocumentRole.SHOP:
            continue
        note = VendorSinkNote(text=text, page_number=index + 1)
        same_page = [k for k in kept if k.page_number == note.page_number]
        if any(note.text.casefold() in k.text.casefold() for k in same_page):
            continue
        kept = [
            k
            for k in kept
            if not (k.page_number == note.page_number and k.text.casefold() in note.text.casefold())
        ]
        kept.append(note)
    return kept[:SINK_NOTE_LIMIT]
