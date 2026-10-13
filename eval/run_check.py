"""Pariksha's per-run check: every place a finished run's safety rules did not hold (#1220).

**Why.** Every safety rule is enforced where it applies: the seal wants two readers, the width check
holds a PASS on an unwitnessed reading, an abstention is written as a result with its reason, a page
nobody read is recorded with why. Nothing confirms afterwards that all of them held for one run. This
does, over the rows a run stored, and lists each place one did not hold with the agent it points to
(`06 Plans/Agent stack`) and what to do: hold the run for a person, or re-run that agent.

**A report only (phase 1).** It reads; it never writes, so it changes no result, hold, verdict or
screen. Nothing in the pipeline calls it yet: `scripts/run_check.py` prints it. It is deterministic
and makes no model call. Its sentences are code's own, with ids and page numbers only, so a report
can be pasted without carrying a drawing's values.

**What it checks, on the revision's live results** (the check runs not superseded):

* (a) coverage: every published rule for the package's product has a result; every abstention says
  why; every countertop row (and every confirmed countertop) has its width result.
* (b) two judgments: no PASS or FAIL rests on a one-judgment architect pairing (the pairing the
  result recorded in its own note, checked against the record it names; `AUTOMATIC_SOURCES`) or
  on a value with only one reading on record; no PASS rests on a reading flagged
  `no-drawn-length-witness` (`confirm_unwitnessed_pass`) or on walls nobody chose
  (`confirm_vendor_walls_pass`).
* (c) inputs: every PASS or FAIL has each single-valued input of its rule stored in
  `verdict_inputs` (a list input is never stored as a list, so it must show in the calculation),
  and its calculation on record.
* (d) unread pages: every vendor page of the newest reading has a row, a recorded reason or a
  reviewer's answer, and every page that blocks sign-off has its result.
* (e) wording: not computable from stored rows yet (see `WORDING_NOT_STORED`). Two limits of (b)
  and (f) are listed with every report too (`UNWITNESSED_FLAG_ONLY`, `LIST_MEMBERS_NOT_STORED`).
* (f) the four contract gates of `eval/release_gates.py`, on the run's own operands: no decision
  on conflicting or unqualified evidence, in an unknown unit, on a value with no source, or on
  advisory evidence.

**Why `eval/`.** Pariksha is the evaluator, and `eval/` already holds the release gates this applies.
`eval/` may read `app.models` and `workflow`'s read helpers (as `eval/form_first_product.py` does),
while nothing in `app/`, `workflow/` or `verdict/` imports `eval/`: no production path can reach it
by accident, which is what "report only" needs. `verdict/` stays unable to import it
(`tests/test_verdict_isolation.py`).

Source: issue #1220. Verification: `tests/eval/test_run_check.py`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ArchitectPairingRecord,
    CanonicalObservation,
    Document,
    DocumentVersion,
    EvidenceCorroborationLane,
    EvidenceSupportingCandidate,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    Page,
)
from app.models.package import package_product
from app.models.rules import RuleDefinition
from app.models.rules import RuleSnapshot as RuleSnapshotRow
from app.models.verdicts import CheckRun, Finding, VerdictInput
from app.review.no_countertop import answered_pages
from app.verdicts.rulebook import from_row, snapshot_store
from eval.release_gates import ADVISORY_GATE, MISSING_SOURCE_GATE, OCR_GATE, UNKNOWN_UNIT_GATE
from evidence.canonical import Authority, CorroborationLane
from extraction.slot_reader.seal import TEXT_LAYER
from rules.semantic_types import ProductType
from units.measurement import Unit
from verdict.operands import QUALIFIED_STATUSES, EvidenceStatus
from verdict.outcomes import DECISIVE_OUTCOMES, Outcome
from vocabulary.drawn_length import NO_DRAWN_LENGTH_WITNESS
from vocabulary.semantic_types import DocumentRole, OperandSource
from vocabulary.wall_layouts import AUTOMATIC_WALLS_LINE, NOT_NEEDED_PASS_LINE
from workflow.architect_row_plan import (
    ARCHITECT_CHECK_RULE_ID,
    AUTOMATIC_SOURCES,
    pairing_source_from_notes,
)
from workflow.part_operands import CountertopScope, countertop_scopes, manual_run_pages
from workflow.slot_row_scope import (
    SlotRow,
    UnchosenRowPage,
    candidate_is_sealed,
    latest_slot_reader_run,
    slot_rows_and_unchosen_pages,
)

__all__ = [
    "LIST_MEMBERS_NOT_STORED",
    "UNWITNESSED_FLAG_ONLY",
    "WORDING_NOT_STORED",
    "Action",
    "Agent",
    "Check",
    "NotComputable",
    "RunCheckItem",
    "RunCheckReport",
    "UnknownRevision",
    "run_check",
]

#: The countertop width check: one result per countertop row, confirmed countertop or split page.
WIDTH_RULE_ID: Final = "CT-WIDTH-001"

#: Why check (e) cannot be computed from stored rows yet.
WORDING_NOT_STORED: Final = (
    "Whether the AI's wording of the findings passed its fact check, or the plain template was "
    "used instead, is not kept anywhere. The step that writes the reports works it out but does "
    "not save it, and the record of each AI call keeps only whether the call itself worked, not "
    "whether its text passed the check. Keeping it needs a database change (a migration), which "
    "phase 1 does not add."
)

#: Why part of checks (b) and (f) cannot be computed (#1222 review): a list input's members.
LIST_MEMBERS_NOT_STORED: Final = (
    "{count} decision(s) in this run used a list of values (several cabinets or fillers, say) "
    "outside a countertop row. The results keep each single value with where it came from, but a "
    "list only as text in the calculation, so where each member of the list came from, and whether "
    "it had two judgments, cannot be checked from what is stored."
)

#: The limit of the unwitnessed-reading check (#1222 review).
UNWITNESSED_FLAG_ONLY: Final = (
    "The drawn-length check reads the same 'drawn length not checked' mark the reader sets on a "
    "reading. A reading that should carry the mark but does not cannot be caught: the drawn "
    "lengths and the scale they were checked against are not stored."
)

_UNITS: Final = frozenset(unit.value for unit in Unit)
_SOURCES: Final = frozenset(source.value for source in OperandSource)
_QUALIFIED: Final = frozenset(status.value for status in QUALIFIED_STATUSES)
_DECIDED: Final = frozenset(outcome.value for outcome in DECISIVE_OUTCOMES)
_READER_ID: Final = "reader-id:"
_SUPPORTS: Final = "supports:"


class UnknownRevision(LookupError):
    """There is no package revision with this id."""


class Check(StrEnum):
    """The six checks, in the order the report lists them."""

    COVERAGE = "coverage"
    TWO_JUDGMENTS = "two judgments"
    INPUTS = "inputs"
    UNREAD_PAGES = "unread pages"
    WORDING = "wording"
    CONTRACT_GATES = "contract gates"


_CHECK_ORDER: Final = {check: index for index, check in enumerate(Check)}


class Agent(StrEnum):
    """The agent an item points to (`06 Plans/Agent stack (proposed 2026-10-13)`)."""

    BALA = "Bala"
    """Collector: pages, products, items."""
    REKHA = "Rekha"
    """Row and layout: the countertop row on a page, its walls."""
    AKSHARA = "Akshara"
    """Number reader: each vendor dimension as printed."""
    VASTU = "Vastu"
    """Architect reader."""
    MILAN = "Milan"
    """Matcher: which architect dimension pairs with which vendor piece."""
    KAUTILYA = "Kautilya"
    """Planner: which checks apply."""
    NINA = "Nina"
    """Executor: the checks' exact arithmetic and the results it records."""
    VANI = "Vani"
    """Writer: the findings wording."""


class Action(StrEnum):
    """What the run check suggests."""

    READY = "ready"
    """Nothing found: the run can go to the reviewer as it is. Only ever the whole report's."""
    HOLD = "hold for a person"
    """A person looks before anything on this item counts."""
    RERUN = "re-run"
    """Re-run the item's agent: its output is missing or stale."""


@dataclass(frozen=True, slots=True)
class RunCheckItem:
    """One place a safety rule did not hold."""

    check: Check
    code: str
    """A short stable name for what was found, such as `silent-page`."""
    agent: Agent
    action: Action
    found: str
    """What was found, in plain words of code's own."""
    ids: tuple[tuple[str, str], ...] = ()
    """`(kind, id)` pairs: the finding, rule, row, page or value concerned."""

    def __post_init__(self) -> None:
        if self.action is Action.READY:
            raise ValueError("an item is something to act on; only a whole report is ready")
        if not self.code or not self.found.strip():
            raise ValueError("an item needs its code and what was found")

    @property
    def suggested(self) -> str:
        """`hold for a person` or `re-run <agent>`."""
        if self.action is Action.RERUN:
            return f"{Action.RERUN.value} {self.agent.value}"
        return self.action.value

    def to_dict(self) -> dict[str, object]:
        return {
            "check": self.check.value,
            "code": self.code,
            "agent": self.agent.value,
            "action": self.suggested,
            "found": self.found,
            "ids": [{"kind": kind, "id": value} for kind, value in self.ids],
        }


@dataclass(frozen=True, slots=True)
class NotComputable:
    """A check, or part of one, that stored rows cannot answer yet, and why."""

    check: Check
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {"check": self.check.value, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class RunCheckReport:
    """The run check of one package revision. Empty `items` means every check that could be
    computed held."""

    package_revision_id: str
    product_type: str | None
    """The package's product, `None` when it was never stated (every product's rules run)."""
    items: tuple[RunCheckItem, ...]
    not_computable: tuple[NotComputable, ...]
    examined: tuple[tuple[str, int], ...] = field(default=())
    """What was looked at, as `(what, how many)`: counts only."""

    @property
    def action(self) -> str:
        """`ready` when nothing was found; else `hold for a person` when any item needs one, and
        `re-run` with the agents to re-run, in the order the items name them, joined by `; `."""
        if not self.items:
            return Action.READY.value
        parts: list[str] = []
        if any(item.action is Action.HOLD for item in self.items):
            parts.append(Action.HOLD.value)
        agents = dict.fromkeys(
            item.agent.value for item in self.items if item.action is Action.RERUN
        )
        if agents:
            parts.append(f"{Action.RERUN.value} {', '.join(agents)}")
        return "; ".join(parts)

    def to_dict(self) -> dict[str, object]:
        return {
            "package_revision_id": self.package_revision_id,
            "product_type": self.product_type,
            "action": self.action,
            "items": [item.to_dict() for item in self.items],
            "not_computable": [entry.to_dict() for entry in self.not_computable],
            "examined": {what: count for what, count in self.examined},
        }


# --- the stored rows ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Live:
    finding: Finding
    run: CheckRun
    rule_id: str
    snapshot: RuleSnapshotRow

    @property
    def decided(self) -> bool:
        return self.finding.outcome in _DECIDED

    @property
    def ids(self) -> tuple[tuple[str, str], ...]:
        scope: tuple[tuple[str, str], ...] = ()
        if self.finding.scope_row_candidate_id is not None:
            scope = (("row", str(self.finding.scope_row_candidate_id)),)
        elif self.finding.scope_item_id is not None:
            scope = (("item", str(self.finding.scope_item_id)),)
        return (("finding", str(self.finding.id)), ("rule", self.rule_id), *scope)


@dataclass(frozen=True, slots=True)
class _Value:
    """A stored canonical observation a decision used, with what is on record about it."""

    row: CanonicalObservation
    lanes: frozenset[str]
    supporters: tuple[ObservationCandidate, ...]
    parents: tuple[ObservationCandidate, ...]
    """The readings the supporters support (`supports:<id>`), or the supporter itself when it
    supports nothing else (a reviewer-confirmed reading)."""
    malformed: tuple[str, ...] = ()
    """Supporting readings whose `supports:` mark names no reading id: what they support is
    unknown."""

    @property
    def reader_ids(self) -> frozenset[str]:
        return frozenset(
            flag.removeprefix(_READER_ID)
            for supporter in self.supporters
            for flag in supporter.ambiguity_flags or ()
            if flag.startswith(_READER_ID)
        )

    @property
    def agent(self) -> Agent:
        if self.row.document_role == DocumentRole.SHOP.value:
            return Agent.AKSHARA
        if self.row.document_role == DocumentRole.ARCH.value:
            return Agent.VASTU
        return Agent.NINA


def _live_results(session: Session, revision_id: UUID) -> list[_Live]:
    return [
        _Live(finding, run, rule_id, snapshot)
        for finding, run, rule_id, snapshot in session.execute(
            select(Finding, CheckRun, RuleDefinition.rule_id, RuleSnapshotRow)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .join(RuleSnapshotRow, RuleSnapshotRow.id == CheckRun.rule_snapshot_id)
            .join(RuleDefinition, RuleDefinition.id == RuleSnapshotRow.rule_definition_id)
            .where(Finding.package_revision_id == revision_id, CheckRun.superseded_at.is_(None))
            .order_by(Finding.created_at, Finding.id)
        ).all()
    ]


def _inputs_by_run(session: Session, run_ids: Sequence[UUID]) -> dict[UUID, list[VerdictInput]]:
    found: dict[UUID, list[VerdictInput]] = defaultdict(list)
    if run_ids:
        for item in session.scalars(
            select(VerdictInput)
            .where(VerdictInput.check_run_id.in_(run_ids))
            .order_by(VerdictInput.operand_name)
        ):
            found[item.check_run_id].append(item)
    return found


def _supported(candidate: ObservationCandidate) -> list[UUID | None]:
    """The reading ids a supporting reading's `supports:` marks name; `None` for a mark that names
    no valid id (reported, never a crash)."""
    found: list[UUID | None] = []
    for flag in candidate.ambiguity_flags or ():
        if not flag.startswith(_SUPPORTS):
            continue
        try:
            found.append(UUID(flag.removeprefix(_SUPPORTS)))
        except ValueError:
            found.append(None)
    return found


def _values(session: Session, observation_ids: Iterable[UUID]) -> dict[UUID, _Value]:
    ids = sorted(set(observation_ids), key=str)
    if not ids:
        return {}
    rows = {
        row.id: row
        for row in session.scalars(
            select(CanonicalObservation).where(CanonicalObservation.id.in_(ids))
        )
    }
    lanes: dict[UUID, set[str]] = defaultdict(set)
    for observation_id, lane in session.execute(
        select(
            EvidenceCorroborationLane.canonical_observation_id, EvidenceCorroborationLane.lane
        ).where(EvidenceCorroborationLane.canonical_observation_id.in_(ids))
    ):
        lanes[observation_id].add(lane)
    supporters: dict[UUID, list[ObservationCandidate]] = defaultdict(list)
    for observation_id, candidate in session.execute(
        select(EvidenceSupportingCandidate.canonical_observation_id, ObservationCandidate)
        .join(
            ObservationCandidate,
            ObservationCandidate.id == EvidenceSupportingCandidate.candidate_id,
        )
        .where(EvidenceSupportingCandidate.canonical_observation_id.in_(ids))
        .order_by(ObservationCandidate.id)
    ):
        supporters[observation_id].append(candidate)
    parent_ids = {
        parent_id
        for candidates in supporters.values()
        for candidate in candidates
        for parent_id in _supported(candidate)
        if parent_id is not None
    }
    parents = (
        {
            candidate.id: candidate
            for candidate in session.scalars(
                select(ObservationCandidate).where(ObservationCandidate.id.in_(parent_ids))
            )
        }
        if parent_ids
        else {}
    )
    values: dict[UUID, _Value] = {}
    for observation_id, row in rows.items():
        mine = tuple(supporters.get(observation_id, ()))
        readings: dict[UUID, ObservationCandidate] = {}
        malformed: list[str] = []
        for supporter in mine:
            supported = _supported(supporter)
            if not supported:
                readings[supporter.id] = supporter
            for parent_id in supported:
                if parent_id is None:
                    malformed.append(str(supporter.id))
                elif (parent := parents.get(parent_id)) is not None:
                    readings[parent.id] = parent
        values[observation_id] = _Value(
            row=row,
            lanes=frozenset(lanes.get(observation_id, ())),
            supporters=mine,
            parents=tuple(readings[key] for key in sorted(readings, key=str)),
            malformed=tuple(sorted(set(malformed))),
        )
    return values


_PAIRING_RECORD: Final = re.compile(r"\(pairing record ([0-9a-fA-F-]{36})\)")


def _noted_pairing(
    session: Session, finding: Finding, run: CheckRun
) -> tuple[str | None, str | None]:
    """The pairing source the architect result recorded in its own notes
    (`workflow.architect_row_plan.pairing_source_note`), and why it cannot be trusted, if so.

    The finding's note is what the check used: it names the effective pairing, after the #1167
    rule that a pairing counts only while its view is the row's match. The record it names must
    exist, belong to the same row, have been written before the result, and say the same source.
    """
    source = pairing_source_from_notes(finding.notes)
    if source is None:
        return None, "it records no pairing source"
    record_id = next(
        (
            match.group(1)
            for note in finding.notes or ()
            if isinstance(note, str) and note.startswith("Pairing source: ")
            for match in (_PAIRING_RECORD.search(note),)
            if match is not None
        ),
        None,
    )
    if record_id is None:
        return source, "its pairing note names no pairing record"
    try:
        record = session.get(ArchitectPairingRecord, UUID(record_id))
    except ValueError:
        record = None
    if record is None:
        return source, "the pairing record its note names does not exist"
    if record.row_anchor_candidate_id != finding.scope_row_candidate_id:
        return source, "the pairing record its note names belongs to another row"
    if record.created_at > run.created_at:
        return source, "the pairing record its note names was written after the result"
    if record.source != source:
        return source, f"its note says '{source}' but the record it names says '{record.source}'"
    return source, None


def _shop_pages(session: Session, revision_id: UUID) -> list[Page]:
    return list(
        session.scalars(
            select(Page)
            .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.document_version_id == Page.document_version_id,
            )
            .where(
                PackageRevisionDocument.package_revision_id == revision_id, Document.kind == "shop"
            )
            .order_by(Page.document_version_id, Page.index)
        ).all()
    )


# --- the checks -----------------------------------------------------------------------------------


def _abstention_reason(finding: Finding) -> str:
    if finding.reason and finding.reason.strip():
        return finding.reason
    trace = finding.trace if isinstance(finding.trace, Mapping) else {}
    reason = trace.get("reason")
    return reason.strip() if isinstance(reason, str) else ""


def _coverage(
    live: Sequence[_Live],
    rules_in_scope: Sequence[str],
) -> list[RunCheckItem]:
    items: list[RunCheckItem] = []
    with_result = {entry.rule_id for entry in live}
    for rule_id in rules_in_scope:
        if rule_id not in with_result:
            items.append(
                RunCheckItem(
                    Check.COVERAGE,
                    "rule-without-result",
                    Agent.KAUTILYA,
                    Action.RERUN,
                    "A published rule for this product has no live result: it was neither run "
                    "nor recorded as not applicable, so it is missing from the review.",
                    (("rule", rule_id),),
                )
            )
    for entry in live:
        if not entry.decided and not _abstention_reason(entry.finding):
            items.append(
                RunCheckItem(
                    Check.COVERAGE,
                    "abstention-without-reason",
                    Agent.NINA,
                    Action.HOLD,
                    f"A {entry.finding.outcome} result records no reason, so the reviewer cannot "
                    "tell why the check did not decide.",
                    entry.ids,
                )
            )
    return items


def _width_coverage(
    rows: Sequence[SlotRow],
    manual: Sequence[CountertopScope] | None,
    *,
    shop_versions: set[UUID],
    manual_pages: set[int],
    width_rows: set[UUID],
    width_items: set[UUID],
) -> list[RunCheckItem]:
    items: list[RunCheckItem] = []
    for row in rows:
        if row.anchor.document_version_id not in shop_versions:
            continue
        if row.page_number - 1 in manual_pages:
            # The reviewer-owned run is the source on its page (`run_checks`).
            continue
        if row.anchor.id not in width_rows:
            items.append(
                RunCheckItem(
                    Check.COVERAGE,
                    "row-without-width-result",
                    Agent.NINA,
                    Action.RERUN,
                    f"The countertop row on page {row.page_number} has no live width result.",
                    (("row", str(row.anchor.id)), ("page", str(row.page_number))),
                )
            )
    for subject in manual or ():
        if subject.item_id not in width_items:
            items.append(
                RunCheckItem(
                    Check.COVERAGE,
                    "countertop-without-width-result",
                    Agent.NINA,
                    Action.RERUN,
                    f"The confirmed countertop on page {subject.page_index + 1} has no live "
                    "width result.",
                    (("item", str(subject.item_id)), ("page", str(subject.page_index + 1))),
                )
            )
    return items


_ONE_JUDGMENT_WORDS: Final = {"code": "code alone", "both-ais": "the two AIs alone"}


def _walls_automatic(entry: _Live, rows: Mapping[UUID, SlotRow]) -> bool:
    """Whether a width PASS rests on walls nobody chose (#1202 review): its own note says the
    walls were set automatically, or its row has no reviewer's wall choice and the result is not
    one that is right for every wall case."""
    notes = entry.finding.notes or ()
    if any(AUTOMATIC_WALLS_LINE in note for note in notes):
        return True
    row = (
        None
        if entry.finding.scope_row_candidate_id is None
        else rows.get(entry.finding.scope_row_candidate_id)
    )
    if row is None or any(NOT_NEEDED_PASS_LINE in note for note in notes):
        return False
    return row.decision is None or row.decision.wall_config is None


def _two_judgments(
    session: Session,
    live: Sequence[_Live],
    inputs: Mapping[UUID, Sequence[VerdictInput]],
    values: Mapping[UUID, _Value],
    rows: Mapping[UUID, SlotRow],
) -> list[RunCheckItem]:
    items: list[RunCheckItem] = []
    for entry in live:
        if not entry.decided:
            continue
        finding = entry.finding
        if entry.rule_id == ARCHITECT_CHECK_RULE_ID:
            source, untrusted = _noted_pairing(session, finding, entry.run)
            if untrusted is not None:
                items.append(
                    RunCheckItem(
                        Check.TWO_JUDGMENTS,
                        "decision-without-pairing-record",
                        Agent.MILAN,
                        Action.HOLD,
                        f"A {finding.outcome} compared with the architect cannot show which "
                        f"pairing it rests on: {untrusted}.",
                        entry.ids,
                    )
                )
            elif source is not None and source not in AUTOMATIC_SOURCES:
                who = _ONE_JUDGMENT_WORDS.get(source)
                items.append(
                    RunCheckItem(
                        Check.TWO_JUDGMENTS,
                        "decision-on-one-judgment-pairing",
                        Agent.MILAN,
                        Action.HOLD,
                        f"A {finding.outcome} compared with the architect rests on "
                        + (f"a pairing made by {who}" if who else "no pairing at all")
                        + "; only code and both AIs together, or a reviewer, may make it "
                        "automatic. It should have waited for the reviewer and did not.",
                        (*entry.ids, ("pairing source", source)),
                    )
                )
        used = [
            values[item.canonical_observation_id]
            for item in inputs.get(entry.run.id, ())
            if item.canonical_observation_id is not None and item.canonical_observation_id in values
        ]
        for value in used:
            if value.malformed:
                items.append(
                    RunCheckItem(
                        Check.TWO_JUDGMENTS,
                        "malformed-supporting-reading",
                        value.agent,
                        Action.HOLD,
                        f"A {finding.outcome} uses a value whose supporting reading names no "
                        "valid reading it supports, so its readings cannot be checked.",
                        (
                            *entry.ids,
                            ("value", str(value.row.id)),
                            *(("reading", reading) for reading in value.malformed),
                        ),
                    )
                )
            if value.row.status != EvidenceStatus.CORROBORATED.value:
                # A reviewer-confirmed value needs no second reading; an unqualified one is the
                # contract gate's (below).
                continue
            two_readers = len(value.reader_ids) >= 2
            if (
                CorroborationLane.SECOND_READER.value in value.lanes
                or (
                    CorroborationLane.DRAWN_LENGTH.value in value.lanes
                    and value.row.document_role == DocumentRole.ARCH.value
                )
                or two_readers
            ):
                continue
            items.append(
                RunCheckItem(
                    Check.TWO_JUDGMENTS,
                    "decision-on-one-route-value",
                    value.agent,
                    Action.HOLD,
                    f"A {finding.outcome} uses a value with only one reading on record: no "
                    "second reader, and no drawn-length witness on the architect's side.",
                    (*entry.ids, ("value", str(value.row.id))),
                )
            )
        if finding.outcome != Outcome.PASS.value:
            continue
        unwitnessed = sorted(
            {
                str(reading.id)
                for value in used
                # A value the reviewer confirmed is theirs, not the readers' text alone.
                if value.row.document_role == DocumentRole.SHOP.value
                and value.row.status == EvidenceStatus.CORROBORATED.value
                for reading in value.parents
                if NO_DRAWN_LENGTH_WITNESS in (reading.ambiguity_flags or ())
                and f"{_READER_ID}{TEXT_LAYER}" not in (reading.ambiguity_flags or ())
                and candidate_is_sealed(reading)
            }
        )
        if unwitnessed:
            items.append(
                RunCheckItem(
                    Check.TWO_JUDGMENTS,
                    "pass-on-unwitnessed-reading",
                    Agent.AKSHARA,
                    Action.HOLD,
                    f"A PASS rests on {len(unwitnessed)} reading(s) whose drawn length was not "
                    "checked (no scale): the two readers' text alone. It should have waited for "
                    "one click and did not.",
                    (*entry.ids, *(("reading", reading) for reading in unwitnessed)),
                )
            )
        if entry.rule_id == WIDTH_RULE_ID and _walls_automatic(entry, rows):
            items.append(
                RunCheckItem(
                    Check.TWO_JUDGMENTS,
                    "pass-on-automatic-walls",
                    Agent.REKHA,
                    Action.HOLD,
                    "A width PASS rests on walls set automatically from the vendor's drawing; "
                    "no reviewer chose them. It should have waited for one click and did not.",
                    entry.ids,
                )
            )
    return items


def _trace(finding: Finding) -> tuple[Mapping[str, object], list[Mapping[str, object]]]:
    trace = finding.trace if isinstance(finding.trace, Mapping) else {}
    stored = trace.get("operands")
    operands = [
        operand
        for operand in (stored if isinstance(stored, list) else ())
        if isinstance(operand, Mapping)
    ]
    return trace, operands


def _cardinalities(
    snapshot: RuleSnapshotRow, cache: dict[UUID, dict[str, str] | None]
) -> dict[str, str] | None:
    """Each declared input of the result's own rule snapshot and how many values it takes
    (`one` or `many`); `None` when the snapshot cannot be read as a rule."""
    if snapshot.id not in cache:
        try:
            rule = from_row(snapshot).rule
        except Exception:  # noqa: BLE001 - an unreadable snapshot is reported, never a crash
            cache[snapshot.id] = None
        else:
            cache[snapshot.id] = {
                name: selector.cardinality.value for name, selector in rule.inputs.items()
            }
    return cache[snapshot.id]


@dataclass(frozen=True, slots=True)
class _InputsSeen:
    items: list[RunCheckItem]
    list_decisions: int
    """Decisions with a list input whose members are not stored one by one."""


def _inputs(live: Sequence[_Live], inputs: Mapping[UUID, Sequence[VerdictInput]]) -> _InputsSeen:
    """A decision's single values must each be stored (`verdict_inputs`, which the database
    admits only qualified); a list input is never stored as a list (`record_finding` keeps one
    exact number per input), so it must show in the calculation instead."""
    items: list[RunCheckItem] = []
    cache: dict[UUID, dict[str, str] | None] = {}
    list_decisions = 0
    for entry in live:
        if not entry.decided:
            continue
        recorded = {item.operand_name for item in inputs.get(entry.run.id, ())}
        trace, operands = _trace(entry.finding)
        shown = {
            str(operand.get("name"))
            for operand in operands
            if str(operand.get("value") or "").strip()
        }
        declared = _cardinalities(entry.snapshot, cache)
        if declared is None:
            missing = [] if recorded else ["(the rule could not be read)"]
            lists: list[str] = []
        else:
            missing = sorted(
                name for name, many in declared.items() if many == "one" and name not in recorded
            )
            lists = [name for name, many in declared.items() if many == "many" and name in shown]
            if not recorded and not lists:
                missing = missing or ["(no input at all)"]
        if any(not any(stored.startswith(f"{name}[") for stored in recorded) for name in lists):
            list_decisions += 1
        if missing:
            items.append(
                RunCheckItem(
                    Check.INPUTS,
                    "decision-without-inputs",
                    Agent.NINA,
                    Action.HOLD,
                    f"A {entry.finding.outcome} has no stored value for {len(missing)} of its "
                    "inputs: it cannot be recomputed from what was stored.",
                    (*entry.ids, *(("input", name) for name in missing)),
                )
            )
        if not isinstance(trace.get("operation"), str) or not isinstance(
            trace.get("operands"), list
        ):
            items.append(
                RunCheckItem(
                    Check.INPUTS,
                    "decision-without-calculation",
                    Agent.NINA,
                    Action.HOLD,
                    f"A {entry.finding.outcome} has no calculation on record: nothing shows the "
                    "arithmetic that decided it.",
                    entry.ids,
                )
            )
    return _InputsSeen(items, list_decisions)


def _unread_pages(
    session: Session,
    revision: PackageRevision,
    *,
    rows: Sequence[SlotRow],
    unchosen: Sequence[UnchosenRowPage],
    shop_pages: Sequence[Page],
    manual_pages: set[int],
    width_rows: set[UUID],
) -> list[RunCheckItem]:
    accounted = {row.anchor.page_id for row in rows}
    accounted |= {page.record.page_id for page in unchosen}
    items: list[RunCheckItem] = []
    for page in shop_pages:
        if page.id in accounted or page.index in manual_pages:
            continue
        items.append(
            RunCheckItem(
                Check.UNREAD_PAGES,
                "silent-page",
                Agent.REKHA,
                Action.RERUN,
                f"Vendor page {page.index + 1} has no countertop row read and no recorded reason "
                "or answer: it vanished from the review.",
                (("page", str(page.index + 1)), ("page id", str(page.id))),
            )
        )
    kept = [entry for entry in unchosen if entry.page_number - 1 not in manual_pages]
    answered, answers = answered_pages(session, revision, kept)
    for listed in answered:
        ids = (("record", str(listed.record.id)), ("page", str(listed.page_number)))
        if listed.split and listed.record.id not in width_rows:
            items.append(
                RunCheckItem(
                    Check.UNREAD_PAGES,
                    "blocking-page-without-result",
                    Agent.NINA,
                    Action.RERUN,
                    f"Page {listed.page_number} needs the reviewer (the readers did not agree on its "
                    "row, or a reviewer said it has a countertop) but has no live result.",
                    ids,
                )
            )
        elif (
            listed.listed
            and not (listed.stored_reason or "").strip()
            and listed.record.id not in answers
        ):
            items.append(
                RunCheckItem(
                    Check.UNREAD_PAGES,
                    "unread-page-without-reason",
                    Agent.REKHA,
                    Action.HOLD,
                    f"Page {listed.page_number} was not read and its record keeps no reason, and "
                    "no reviewer has answered it.",
                    ids,
                )
            )
    return items


def _contract_gates(
    live: Sequence[_Live],
    inputs: Mapping[UUID, Sequence[VerdictInput]],
    values: Mapping[UUID, _Value],
) -> list[RunCheckItem]:
    items: list[RunCheckItem] = []
    for entry in live:
        if not entry.decided:
            continue
        finding = entry.finding
        recorded = inputs.get(entry.run.id, ())
        used = [
            values[item.canonical_observation_id]
            for item in recorded
            if item.canonical_observation_id is not None and item.canonical_observation_id in values
        ]
        trace, operands = _trace(finding)

        unqualified = [value for value in used if value.row.status not in _QUALIFIED]
        if unqualified:
            items.append(
                RunCheckItem(
                    Check.CONTRACT_GATES,
                    "decision-on-unqualified-evidence",
                    unqualified[0].agent,
                    Action.HOLD,
                    f"A {finding.outcome} uses evidence stored as "
                    f"{', '.join(sorted({value.row.status for value in unqualified}))}: a "
                    "disagreement or an unconfirmed reading reached the arithmetic.",
                    (
                        *entry.ids,
                        ("gate", OCR_GATE),
                        *(("value", str(value.row.id)) for value in unqualified),
                    ),
                )
            )

        units = {
            *(item.unit for item in recorded),
            *(value.row.unit for value in used),
            *((finding.delta_unit,) if finding.delta_unit is not None else ()),
            *((trace["arithmetic_unit"],) if trace.get("arithmetic_unit") is not None else ()),
        }
        unknown = sorted(str(unit) for unit in units if unit not in _UNITS)
        if unknown:
            items.append(
                RunCheckItem(
                    Check.CONTRACT_GATES,
                    "decision-in-unknown-unit",
                    Agent.NINA,
                    Action.HOLD,
                    f"A {finding.outcome} was decided with a unit this code does not know "
                    f"({', '.join(repr(unit) for unit in unknown)}).",
                    (*entry.ids, ("gate", UNKNOWN_UNIT_GATE)),
                )
            )

        sourceless = sorted(
            {
                str(operand.get("name") or "?")
                for operand in operands
                if not str(operand.get("value") or "").strip()
                or not str(operand.get("source") or "").strip()
            }
            | {
                item.operand_name
                for item in recorded
                if item.evidence_status == EvidenceStatus.CORROBORATED.value
                and item.canonical_observation_id is None
            }
        )
        if sourceless:
            items.append(
                RunCheckItem(
                    Check.CONTRACT_GATES,
                    "decision-on-value-without-source",
                    Agent.NINA,
                    Action.HOLD,
                    f"A {finding.outcome} used {len(sourceless)} input(s) with no value or no "
                    "drawing behind them on record; a missing value must give NOT_FOUND.",
                    (*entry.ids, ("gate", MISSING_SOURCE_GATE)),
                )
            )

        advisory = [value for value in used if value.row.authority == Authority.ADVISORY.value]
        foreign = sorted(
            {
                str(operand.get("source"))
                for operand in operands
                if str(operand.get("source") or "").strip()
                and str(operand.get("source")) not in _SOURCES
            }
        )
        if advisory or foreign:
            items.append(
                RunCheckItem(
                    Check.CONTRACT_GATES,
                    "decision-on-advisory-value",
                    Agent.NINA,
                    Action.HOLD,
                    f"A {finding.outcome} used advisory evidence or an input from a source that "
                    "may not enter a verdict"
                    + (f" ({', '.join(foreign)})" if foreign else "")
                    + ".",
                    (
                        *entry.ids,
                        ("gate", ADVISORY_GATE),
                        *(("value", str(value.row.id)) for value in advisory),
                    ),
                )
            )
    return items


# --- the report -----------------------------------------------------------------------------------


def run_check(session: Session, package_revision_id: UUID) -> RunCheckReport:
    """The run check of one package revision's live results. Reads only.

    Raises:
        UnknownRevision: when there is no such revision.
    """
    revision = session.get(PackageRevision, package_revision_id)
    if revision is None:
        raise UnknownRevision(f"no package revision {package_revision_id}")
    package = session.get(Package, revision.package_id)
    if package is None:
        raise UnknownRevision(f"package revision {package_revision_id} has no package")
    product = package_product(package)
    in_scope = {item.value for item in ProductType} if product is None else {product.value}
    countertops = ProductType.COUNTERTOP.value in in_scope
    not_computable: list[NotComputable] = [NotComputable(Check.WORDING, WORDING_NOT_STORED)]

    store = snapshot_store(session)
    rules_in_scope = sorted(
        rule_id
        for rule_id in store.rule_ids()
        if (latest := store.latest(rule_id)) is not None
        and latest.rule.product_type.value in in_scope
    )
    live = _live_results(session, package_revision_id)

    def report(items: list[RunCheckItem], examined: dict[str, int]) -> RunCheckReport:
        # Stable: within one code, items keep the order they were found in (pages in page order).
        ordered = sorted(items, key=lambda item: (_CHECK_ORDER[item.check], item.code))
        return RunCheckReport(
            package_revision_id=str(package_revision_id),
            product_type=None if product is None else product.value,
            items=tuple(ordered),
            not_computable=tuple(
                sorted(not_computable, key=lambda entry: _CHECK_ORDER[entry.check])
            ),
            examined=tuple(examined.items()),
        )

    if not rules_in_scope:
        return report(
            [
                RunCheckItem(
                    Check.COVERAGE,
                    "no-rules-in-scope",
                    Agent.KAUTILYA,
                    Action.HOLD,
                    "No published rule applies to this package's product, so nothing was checked.",
                )
            ],
            {"rules in scope": 0, "live results": len(live)},
        )
    if not live:
        return report(
            [
                RunCheckItem(
                    Check.COVERAGE,
                    "no-live-results",
                    Agent.NINA,
                    Action.RERUN,
                    "This revision has no live check results: the checks have not run on it, or "
                    "their results were replaced and nothing new was written.",
                )
            ],
            {"rules in scope": len(rules_in_scope), "live results": 0},
        )

    inputs = _inputs_by_run(session, [entry.run.id for entry in live])
    values = _values(
        session,
        (
            item.canonical_observation_id
            for entry in live
            if entry.decided
            for item in inputs.get(entry.run.id, ())
            if item.canonical_observation_id is not None
        ),
    )
    reading = (
        slot_rows_and_unchosen_pages(session, package_revision_id)
        if countertops and WIDTH_RULE_ID in rules_in_scope
        else None
    )
    rows: tuple[SlotRow, ...] = () if reading is None else reading[0]

    seen = _inputs(live, inputs)
    items = _coverage(live, rules_in_scope)
    items += _two_judgments(session, live, inputs, values, {row.anchor.id: row for row in rows})
    items += seen.items
    items += _contract_gates(live, inputs, values)
    not_computable.append(NotComputable(Check.TWO_JUDGMENTS, UNWITNESSED_FLAG_ONLY))
    if seen.list_decisions:
        words = LIST_MEMBERS_NOT_STORED.format(count=seen.list_decisions)
        not_computable.append(NotComputable(Check.TWO_JUDGMENTS, words))
        not_computable.append(NotComputable(Check.CONTRACT_GATES, words))

    examined = {
        "rules in scope": len(rules_in_scope),
        "live results": len(live),
        "decided findings": sum(entry.decided for entry in live),
        "recorded inputs": sum(len(inputs.get(entry.run.id, ())) for entry in live),
        "values behind decisions": len(values),
    }
    if reading is not None:
        unchosen = reading[1]
        manual = countertop_scopes(session, package_revision_id)
        manual_pages = {subject.page_index for subject in manual or ()} | manual_run_pages(
            session, package_revision_id
        )
        shop_pages = _shop_pages(session, package_revision_id)
        widths = [entry.finding for entry in live if entry.rule_id == WIDTH_RULE_ID]
        width_rows = {
            finding.scope_row_candidate_id
            for finding in widths
            if finding.scope_row_candidate_id is not None
        }
        items += _width_coverage(
            rows,
            manual,
            shop_versions={page.document_version_id for page in shop_pages},
            manual_pages=manual_pages,
            width_rows=width_rows,
            width_items={
                finding.scope_item_id for finding in widths if finding.scope_item_id is not None
            },
        )
        if latest_slot_reader_run(session, package_revision_id) is not None:
            items += _unread_pages(
                session,
                revision,
                rows=rows,
                unchosen=unchosen,
                shop_pages=shop_pages,
                manual_pages=manual_pages,
                width_rows=width_rows,
            )
        examined.update(
            {
                "countertop rows": len(rows),
                "confirmed countertops": len(manual or ()),
                "vendor pages": len(shop_pages),
                "pages not read": len(unchosen),
            }
        )
    return report(items, examined)
