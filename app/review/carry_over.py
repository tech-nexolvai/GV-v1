"""Which reviewer decision stands on a finding, and carrying it over an unchanged re-run (#1073).

**The problem.** Every "Run checks" writes new findings and supersedes the previous run
(`app/verdicts/record.py`), and a `ReviewAction` belongs to the one finding it was recorded on. So
after any re-run every held result asked again, even when nothing it rests on had changed. Anant
decided option (b) on 2026-10-09: a new finding inherits the reviewer's latest *valid* decision on
the finding it replaces, **only when the stored result is the same**.

**Honest record.** Nothing here writes a `ReviewAction`, and findings are never touched. The new
finding gets one append-only `FindingDecisionCarryover` link naming the reviewer's own action (its
original actor, time and note) and the hash both results share. Every reader asks
`decision_records` which decision stands, so approval, readiness, the countertop results, the
findings list, the history and the signed record cannot disagree.

**"The same result"** (`result_fingerprints`) is a sha256 over canonical JSON built only from stored
rows, and only from the facts the finding's own rule used (#1177, `FINGERPRINT_VERSION` 2):

- the revision; the rule id and its snapshot's content hash; the engine version (an engine maths
  change still asks again);
- variant, outcome, severity and the exact delta;
- the stored trace without its prose: operation, operand names, exact values and sources,
  intermediates, comparison, tolerance, unit, versions;
- every sealed `verdict_inputs` value by operand name (which carries the piece position), exact
  value, unit and status, plus where its reading is (file sha256, page index, role, semantic type,
  value, status and where on the page it is drawn) or that a reviewer typed it for the row;
- every `finding_evidence` link the same way, by role and content;
- the parameters the rule declares, each by the value the run resolved for it (RUN over PROJECT
  over the company set over the rulebook defaults the run stored). A RUN set is found under the id
  the run cites for it, its measurements left out (`workflow/measurements.run_layer`). When a value
  cannot be read from stored rows (a cited set that is not stored, an unknown layer, a run with no
  defaults) the cited set ids and the rulebook defaults by content are hashed instead, which asks
  again on any change; a run with no readable defaults never matches;
- for a slot-reader row: the row by file sha256, page index, the reader's row rank, the reader
  and its version, and where the row's first piece is drawn (rounded the same way every time:
  the same rank may be another drawn row after a re-read); every slot reading on it (slot, value,
  unit, status, lane, flags with any id replaced by `<id>`, where it is drawn); the widths typed
  for the row by the decision in force when the run was written (the tip of its supersede chain);
  the effective wall layout, only for a rule resolved by the wall layout (`wall_config`); and the
  row's architect pairing and view-match records in force, by content, only for a rule comparing
  the vendor with the architect (`arch_vs_shop`), each paired architect dimension by file, page,
  value, printed text, where it is drawn and its `arch-` flags (two 30 in dimensions on one page
  are two pairings);
- for a result that did not decide (not PASS or FAIL), its reason: no hold has a stable code yet,
  so the sentence is the only record of *why* it held.

**What moved out, and why (#1177).** `notes`: commentary whose wording changes between releases
(#1138 reworded the wall-layout note and dropped a held page's decision); the facts it states are
hashed above. A decided result's `reason`, the trace's `explanation` and an abstention trace's
copy of the reason: prose about values already hashed. The whole `parameter_set_versions` map and
the content of every rulebook default: one saved project value changed every rule's fingerprint,
although each rule reads only its own parameters. `scope_row_candidate_id`, observation ids,
row-review ids, the trace's `evidence_ref`, the snapshot's row id and `scope_label`: ids minted per
reading run (so after any re-read nothing carried) or text derived from the row identity. Every
architect pairing record id on the row: the width check never reads a pairing, so an
architect-side click dropped the width decision. No timestamp is hashed; one only decides which
reviewer records were in force when a run was written.

**Never carried:** a `correct` (the re-run is what consumes it), or anything from a finding whose
history has one; an exception whose grant has run out; a confirm or dismiss without the note
`needs_note` requires; anything from another revision (and so another project). If the latest
decision is not valid, nothing is carried; an older one is never dug out instead. Two old or two new
results sharing one fingerprint are ambiguous, and nothing is carried for them either. A
hand-confirmed countertop's result (the legacy manual path) never carries: its inputs live in the
countertop-run tables, which only the confirmed-structure resolver reads.

Nothing here imports `verdict/`, `rules/`, `extraction/` or `retrieval/`; the wall layout is decided
by `workflow/slot_row_scope.effective_row_wall`, the one rule the width check itself uses.

Verification: `tests/review/test_carry_over.py`
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.models.document import DocumentVersion, Page
from app.models.evidence import (
    ArchitectPairingRecord,
    ArchitectViewIndexEntry,
    ArchitectViewMatchRecord,
    CanonicalObservation,
    ObservationCandidate,
    SlotRowReviewDecision,
)
from app.models.parameters import ParameterSet, ParameterValue, from_rows
from app.models.review import FindingDecisionCarryover, ReviewAction, ReviewException
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.runs import ExtractionRun
from app.models.verdicts import CheckRun, Finding, FindingEvidence, VerdictInput
from app.review.exceptions import ExceptionGrant, FindingRef, decide
from app.review.requirements import needs_note
from vocabulary.semantic_types import SemanticType
from workflow.measurements import run_layer
from workflow.pariksha_gates import GATE_FLAG, REKHA_ROW_GATE
from workflow.slot_row_scope import effective_row_wall

__all__ = [
    "CARRIABLE_ACTIONS",
    "FINGERPRINT_VERSION",
    "DecisionRecords",
    "EffectiveDecision",
    "carry_decisions_over",
    "decision_holds",
    "decision_records",
    "live_finding_ids",
    "result_fingerprints",
]

#: Bumped whenever the fingerprint's contents change, so two versions can never match each other.
#: 2 (#1177): only the facts the finding's rule used; no prose, no per-run ids.
FINGERPRINT_VERSION = 2

#: The outcomes that decide. Any other result held, and its reason is the record of why.
_DECIDED = frozenset({"PASS", "FAIL"})

#: Parameter layers, highest precedence first (`rules/parameters.py`: GLOBAL -> PROJECT -> RUN).
_LAYER_PRECEDENCE = ("run", "project", "global")

#: A rule resolved by this discriminator reads the row's wall layout (CT-WIDTH-001).
_WALL_DISCRIMINATOR = SemanticType.WALL_CONFIG.value

#: A rule of this check type compares the vendor with the architect, and so reads the row's
#: architect pairing and matched view (CT-ARCH-WIDTH-001, #1054/#1167).
_ARCHITECT_CHECK_TYPE = "arch_vs_shop"

#: An id inside a stored flag (`supports:<uuid>`): minted per reading run, never content.
_ID_IN_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

CARRIABLE_ACTIONS = frozenset({"confirm", "dismiss", "except"})


@dataclass(frozen=True, slots=True)
class EffectiveDecision:
    """The decision that stands on one finding: the reviewer's own row, never a copy.

    `carried_from_finding_id` is the finding the reviewer actually decided on, when the decision was
    carried over an unchanged re-run; `None` when it was recorded on this finding itself.
    """

    action: ReviewAction
    carried_from_finding_id: UUID | None

    @property
    def carried_over(self) -> bool:
        return self.carried_from_finding_id is not None


@dataclass(frozen=True, slots=True)
class DecisionRecords:
    """Everything a reader needs to say which decision stands, read in a fixed number of queries.

    - `decisions`: per finding, its own latest action, else the decision it carried;
    - `carried`: per finding, the carried decision (even when a later own action replaced it), so a
      history can show it;
    - `corrected`: findings with a `correct` of their own (blocked until a re-run);
    - `own_actions`: every action recorded on each finding, oldest first;
    - `grants`: the exception grant behind each `except` in `decisions`.
    """

    decisions: Mapping[UUID, EffectiveDecision]
    carried: Mapping[UUID, EffectiveDecision]
    corrected: frozenset[UUID]
    own_actions: Mapping[UUID, tuple[ReviewAction, ...]]
    grants: Mapping[UUID, ExceptionGrant]


def decision_records(db: Session, finding_ids: Collection[UUID]) -> DecisionRecords:
    """Which decision stands on each finding. The one function every reader uses.

    Three statements whatever the number of findings: the carry links (with their actions), every
    action on these findings and on the findings the carried actions were recorded on, and the
    exception grants. A carried decision only stands while it is still the latest action on the
    finding it was recorded on, that finding has no `correct`, and it belongs to the same revision.
    """
    ids = set(finding_ids)
    if not ids:
        return DecisionRecords({}, {}, frozenset(), {}, {})
    links = db.execute(
        select(FindingDecisionCarryover, ReviewAction)
        .join(ReviewAction, ReviewAction.id == FindingDecisionCarryover.review_action_id)
        .where(
            FindingDecisionCarryover.new_finding_id.in_(ids),
            ReviewAction.package_revision_id == FindingDecisionCarryover.package_revision_id,
        )
    ).all()
    sources = {action.finding_id for _, action in links}
    actions = db.scalars(
        select(ReviewAction)
        .where(ReviewAction.finding_id.in_(ids | sources))
        .order_by(ReviewAction.created_at, ReviewAction.id)
    ).all()
    by_finding: dict[UUID, list[ReviewAction]] = defaultdict(list)
    for action in actions:
        by_finding[action.finding_id].append(action)
    corrected_anywhere = {
        identity
        for identity, history in by_finding.items()
        if any(action.action == "correct" for action in history)
    }

    carried: dict[UUID, EffectiveDecision] = {}
    for link, action in links:
        source_history = by_finding.get(action.finding_id, [])
        if (
            action.action in CARRIABLE_ACTIONS
            and source_history
            and source_history[-1].id == action.id
            and action.finding_id not in corrected_anywhere
        ):
            carried[link.new_finding_id] = EffectiveDecision(action, action.finding_id)

    decisions: dict[UUID, EffectiveDecision] = {}
    for identity in ids:
        own = by_finding.get(identity)
        if own:
            decisions[identity] = EffectiveDecision(own[-1], None)
        elif identity in carried:
            decisions[identity] = carried[identity]

    excepted = [d.action.id for d in decisions.values() if d.action.action == "except"]
    grants: dict[UUID, ExceptionGrant] = {}
    if excepted:
        grants = {
            grant.review_action_id: ExceptionGrant.from_stored(grant)
            for grant in db.scalars(
                select(ReviewException).where(ReviewException.review_action_id.in_(excepted))
            ).all()
        }
    return DecisionRecords(
        decisions=decisions,
        carried=carried,
        corrected=frozenset(ids & corrected_anywhere),
        own_actions={identity: tuple(by_finding.get(identity, ())) for identity in ids},
        grants=grants,
    )


def decision_holds(finding: Finding, records: DecisionRecords, *, when: datetime) -> bool:
    """Whether the decision standing on this finding is a real, still-valid one.

    The rule sign-off has always applied (`app/review/approval.py`), now for a carried decision as
    well: a correction never decides (the re-run consumes it); a confirm or dismiss needs the note
    the rule requires; an exception counts only while its grant is in force at `when`. A carried
    exception is asked about the finding it was granted on, because that is what its scope names.
    """
    if finding.id in records.corrected:
        return False
    decision = records.decisions.get(finding.id)
    if decision is None:
        return False
    action = decision.action
    if action.package_revision_id != finding.package_revision_id:
        return False
    if action.action in {"confirm", "dismiss"}:
        return not needs_note(finding.outcome, action.action) or bool(
            action.note and action.note.strip()
        )
    if action.action == "except" and action.id in records.grants:
        return decide(
            FindingRef(
                finding_id=action.finding_id,
                package_revision_id=finding.package_revision_id,
                item_id=finding.scope_item_id,
            ),
            (records.grants[action.id],),
            when=when,
        ).is_excepted
    return False


def live_finding_ids(db: Session, package_revision_id: UUID) -> tuple[UUID, ...]:
    """The findings of the revision's live runs: what a re-run is about to supersede."""
    return tuple(
        db.scalars(
            select(Finding.id)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .where(
                Finding.package_revision_id == package_revision_id,
                CheckRun.superseded_at.is_(None),
            )
        ).all()
    )


def _text(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _sorted(rows: Iterable[object]) -> list[object]:
    return sorted(rows, key=_canonical)


def result_fingerprints(db: Session, findings: Collection[Finding]) -> dict[UUID, str]:
    """The "same result" hash of each finding, from stored rows only (a bounded number of reads)."""
    return {
        identity: hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
        for identity, body in fingerprint_bodies(db, findings).items()
    }


def _defaults_content(canonical: str | None) -> str | None:
    """The rulebook defaults a run used, by content, without the run-time `set_at` stamp.

    Used only when a rule's parameters cannot be read by value (`_ParameterLayers`), so the
    fallback still does not change on every run. `None` for a run that stored none.
    """
    if canonical is None:
        return None
    payload = json.loads(canonical)
    parameters = payload.get("parameters", {})
    payload["parameters"] = {
        name: {key: value for key, value in form.items() if key != "set_at"}
        for name, form in parameters.items()
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _cited_fallback(finding: Finding, run: CheckRun) -> dict[str, object]:
    """The settings of a rule whose values cannot be read back: the cited set ids, and the
    rulebook defaults by content (they lie beneath a company set too). A run that stored no
    readable defaults can never match another, so its result always asks again."""
    try:
        defaults = _defaults_content(run.defaults_canonical_json)
    except (TypeError, ValueError, AttributeError):
        defaults = None
    return {
        "cited_sets": {
            layer: (
                "rulebook defaults"
                if run.defaults_set_id is not None and set_id == run.defaults_set_id
                else set_id
            )
            for layer, set_id in (finding.parameter_set_versions or {}).items()
        },
        "defaults": defaults if defaults is not None else f"none stored: {finding.id}",
    }


def _exact_text(numerator: object, denominator: object) -> str:
    """`n/d` reduced, so `2/16` and `1/8` are one value; raises on anything that is not exact."""
    return str(Fraction(int(str(numerator)), int(str(denominator))))


@dataclass(frozen=True, slots=True)
class _RuleReads:
    """What one published rule reads besides its sealed operands, from its snapshot alone."""

    parameters: tuple[str, ...]
    """The parameters the rule declares: the only names the engine resolves for it."""
    walls: bool
    """Whether it is resolved by the wall layout (`applicability.discriminator: wall_config`)."""
    architect: bool
    """Whether it compares the vendor with the architect (`check_type: arch_vs_shop`): only such
    a rule reads a row's architect pairing and its matched architect view."""


def _rule_reads(snapshot: RuleSnapshot) -> _RuleReads:
    try:
        payload = json.loads(snapshot.canonical_json)
    except (TypeError, ValueError):
        payload = None
    payload = payload if isinstance(payload, dict) else {}
    parameters = payload.get("parameters")
    applicability = payload.get("applicability")
    discriminator = applicability.get("discriminator") if isinstance(applicability, dict) else None
    return _RuleReads(
        parameters=tuple(sorted(parameters)) if isinstance(parameters, dict) else (),
        walls=discriminator == _WALL_DISCRIMINATOR,
        architect=snapshot.check_type == _ARCHITECT_CHECK_TYPE,
    )


type _Value = tuple[str, str]
"""An exact parameter value as `(n/d, unit)`."""


@dataclass(frozen=True, slots=True)
class _ParameterLayers:
    """The stored parameter sets a batch of findings cites, read by value (one statement)."""

    stored: Mapping[str, Mapping[str, _Value]]
    """`set_id` -> name -> value, for every cited set found in `parameter_sets`."""

    def used(
        self, finding: Finding, run: CheckRun, names: Sequence[str]
    ) -> dict[str, object] | None:
        """Each parameter `names` resolved exactly as the run resolved it, by value.

        Precedence is the resolver's (`rules/parameters.py`): RUN over PROJECT over the company
        (GLOBAL) set, and the rulebook defaults the run stored beneath the company set by name
        (`workflow/stages.py:_layered`). `None` when a value cannot be established from stored
        rows: a cited set that is not stored, a layer this code does not know, or a run that kept
        no defaults. The caller then falls back to the cited set ids, which re-asks on any change.
        """
        cited = finding.parameter_set_versions or {}
        if not isinstance(cited, dict) or set(cited) - set(_LAYER_PRECEDENCE):
            return None
        layers: list[Mapping[str, _Value]] = []
        for layer in _LAYER_PRECEDENCE:
            set_id = cited.get(layer)
            if set_id is None:
                continue
            if (
                layer == "global"
                and run.defaults_set_id is not None
                and set_id == run.defaults_set_id
            ):
                continue  # the rulebook defaults themselves: read from the run below
            values = self.stored.get(str(set_id))
            if values is None:
                return None
            layers.append(values)
        defaults = _stored_defaults(run.defaults_canonical_json)
        used: dict[str, object] = {}
        for name in names:
            found = next((layer[name] for layer in layers if name in layer), None)
            if found is None and defaults is None:
                return None
            if found is None and defaults is not None:
                found = defaults.get(name)
            used[name] = None if found is None else list(found)
        return used


def _stored_defaults(canonical: str | None) -> dict[str, _Value] | None:
    """The rulebook defaults a run stored, by value; `None` when there are none or they are
    unreadable (the fallback then applies)."""
    if canonical is None:
        return None
    try:
        payload = json.loads(canonical)
        return {
            str(name): (str(Fraction(str(form["value"]))), str(form["unit"]))
            for name, form in payload["parameters"].items()
        }
    except (KeyError, TypeError, ValueError, AttributeError, ZeroDivisionError):
        return None


def _parameter_layers(db: Session, findings: Collection[Finding]) -> _ParameterLayers:
    set_ids = {
        str(set_id)
        for finding in findings
        if isinstance(finding.parameter_set_versions, dict)
        for set_id in finding.parameter_set_versions.values()
    }
    stored: dict[str, dict[str, _Value]] = {}
    if set_ids:
        found = db.execute(
            select(ParameterSet.set_id, ParameterValue)
            .outerjoin(ParameterValue, ParameterValue.parameter_set_id == ParameterSet.id)
            .where(ParameterSet.set_id.in_(set_ids))
        ).all()
        for set_id, value in found:
            values = stored.setdefault(set_id, {})
            if value is not None:
                values[value.name] = (
                    _exact_text(value.numerator, value.denominator),
                    value.unit,
                )
    if set_ids - set(stored):
        stored |= _run_layers(db, {finding.package_revision_id for finding in findings})
    return _ParameterLayers(stored)


def _run_layers(db: Session, revision_ids: Collection[UUID]) -> dict[str, dict[str, _Value]]:
    """Each stored RUN set of these revisions under the id a check run cites for it (one statement).

    A RUN set also keeps the measurements typed for the review; the run resolves and cites it with
    those left out (`workflow/measurements.run_layer`), and that id is stored nowhere. So typing a
    measurement, which writes a new RUN set version, is matched by value like any other setting.
    """
    rows: dict[UUID, tuple[ParameterSet, list[ParameterValue]]] = {}
    for stored, value in db.execute(
        select(ParameterSet, ParameterValue)
        .outerjoin(ParameterValue, ParameterValue.parameter_set_id == ParameterSet.id)
        .where(
            ParameterSet.package_revision_id.in_(set(revision_ids)),
            ParameterSet.layer == "run",
        )
    ).all():
        entry = rows.setdefault(stored.id, (stored, []))
        if value is not None:
            entry[1].append(value)
    layers: dict[str, dict[str, _Value]] = {}
    for stored, values in rows.values():
        try:
            layer = run_layer(from_rows(stored, values))
        except (TypeError, ValueError):
            continue  # unreadable: the cited id then stays unresolved, and asks again
        if layer is None:
            continue
        layers[layer.set_id] = {
            name: (str(value.value.exact_value), str(value.value.unit))
            for name, value in layer.parameters.items()
        }
    return layers


def _stable_trace(trace: object) -> object:
    """The stored trace without its prose and its per-run references.

    Kept: the operation, every operand's name, exact value and source, the intermediates, the
    comparison, tolerance, unit, outcome and versions. Left out: `explanation` (a sentence written
    from the intermediates, which stay), each operand's `evidence_ref` (an id minted per reading;
    the evidence is hashed by content instead) and an abstention's `reason` (hashed once, as
    `hold_reason`).
    """
    if not isinstance(trace, dict):
        return trace
    stable = {key: value for key, value in trace.items() if key not in {"explanation"}}
    if "cause" in stable:
        stable.pop("reason", None)
    operands = stable.get("operands")
    if isinstance(operands, list):
        stable["operands"] = [
            (
                {key: value for key, value in operand.items() if key != "evidence_ref"}
                if isinstance(operand, dict)
                else operand
            )
            for operand in operands
        ]
    return stable


#: The prefix of Rekha's row-gate flags (`workflow.pariksha_gates.GateResult.flag`).
REKHA_ROW_FLAG: Final = f"{GATE_FLAG}rekha:{REKHA_ROW_GATE}:"


def _stable_flags(flags: Iterable[str] | None) -> list[str]:
    """A reading's flags with every id inside one (`supports:<uuid>`, minted per reading run)
    replaced by `<id>`: the flag still counts, the id never does. Rekha's row-gate record on a
    row's anchor (#1228, `REKHA_ROW_FLAG`) is left out: it says the row was handed on, not what was
    read, and a reading from before the gate must keep matching the same reading after it. Any
    other gate flag still counts."""
    return sorted(
        {
            _ID_IN_TEXT.sub("<id>", flag)
            for flag in flags or ()
            if not flag.startswith(REKHA_ROW_FLAG)
        }
    )


def _polygon(points: object, space: str | None) -> object:
    """Where something is drawn, rounded the same way every time: to the pixel in image space,
    to 1/10000 of the page in stored (normalised) space. Anything unreadable is kept as stored."""
    step = Decimal(1) if space == "image" else Decimal("0.0001")
    try:
        return [
            [str(Decimal(str(value)).quantize(step, rounding=ROUND_HALF_UP) + 0) for value in point]
            for point in points  # type: ignore[attr-defined]
        ]
    except (TypeError, ValueError, ArithmeticError):
        return points


def _flag(flags: Iterable[str] | None, prefix: str) -> str | None:
    return next(
        (flag.removeprefix(prefix) for flag in flags or () if flag.startswith(prefix)), None
    )


def _typed_values(measurements: object) -> object:
    """A row decision's typed widths by value: `{key: [n/d, unit]}`; anything unreadable as is."""
    if not isinstance(measurements, dict):
        return measurements
    typed: dict[str, object] = {}
    for key, saved in measurements.items():
        try:
            typed[str(key)] = [
                _exact_text(saved["numerator"], saved["denominator"]),
                str(saved["unit"]),
            ]
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            typed[str(key)] = saved
    return typed


@dataclass(frozen=True, slots=True)
class _Row:
    """One slot-reader row as stored: who it is, and what was read on it."""

    identity: list[object]
    anchor: ObservationCandidate | None
    readings: list[object]
    wall: ObservationCandidate | None


def _rows(db: Session, anchor_ids: Collection[UUID]) -> dict[UUID, _Row]:
    """Each row by a stable identity (two statements for any number of rows).

    The anchor candidate's id is minted per reading run, so the row is named by what does not
    change on a re-read: the vendor file's sha256, the page index and the reader's row rank
    (`row-rank:<n>`, or `row-choice` for a page whose row was not chosen, #1093). Its readings are
    every slot candidate of that rank on that page of the same run, by slot, value, unit, status,
    lane and flags (ids left out), so a held row, which seals no operand, still asks again when
    what was read on it changes.
    """
    if not anchor_ids:
        return {}
    anchors = {
        candidate.id: (candidate, page_index, sha256, f"{extractor} {extractor_version}")
        for candidate, page_index, sha256, extractor, extractor_version in db.execute(
            select(
                ObservationCandidate,
                Page.index,
                DocumentVersion.sha256,
                ExtractionRun.extractor,
                ExtractionRun.extractor_version,
            )
            .join(Page, Page.id == ObservationCandidate.page_id)
            .join(DocumentVersion, DocumentVersion.id == ObservationCandidate.document_version_id)
            .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
            .where(ObservationCandidate.id.in_(set(anchor_ids)))
        ).all()
    }
    runs = {candidate.extraction_run_id for candidate, *_ in anchors.values()}
    pages = {candidate.page_id for candidate, *_ in anchors.values()}
    neighbours: dict[tuple[UUID, UUID], list[ObservationCandidate]] = defaultdict(list)
    if anchors:
        for candidate in db.scalars(
            select(ObservationCandidate).where(
                ObservationCandidate.extraction_run_id.in_(runs),
                ObservationCandidate.page_id.in_(pages),
            )
            # Oldest first, as `slot_rows` reads them: the newest wall record of a rank counts.
            .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
        ).all():
            neighbours[(candidate.extraction_run_id, candidate.page_id)].append(candidate)
    rows: dict[UUID, _Row] = {}
    for anchor_id in anchor_ids:
        if anchor_id not in anchors:
            rows[anchor_id] = _Row(["unknown row", str(anchor_id)], None, [], None)
            continue
        anchor, page_index, sha256, reader = anchors[anchor_id]
        flags = anchor.ambiguity_flags or []
        rank = _flag(flags, "row-rank:")
        # Where the row is drawn, and which reader version found it: the same rank on the same
        # page may be another drawn row after a re-read.
        where = [reader, _polygon(anchor.polygon, anchor.coordinate_space)]
        if rank is None:
            marker = "row-choice" if "slot-reader-row-choice" in flags else f"row:{anchor_id}"
            rows[anchor_id] = _Row([sha256, page_index, marker, *where], anchor, [], None)
            continue
        readings: list[object] = []
        wall: ObservationCandidate | None = None
        for candidate in neighbours[(anchor.extraction_run_id, anchor.page_id)]:
            other = candidate.ambiguity_flags or []
            if _flag(other, "row-rank:") != rank:
                continue
            if candidate.raw_text.startswith("walls: "):
                wall = candidate
                continue
            slot = _flag(other, "slot:")
            if "slot-reader" not in other or slot is None:
                continue
            readings.append(
                [
                    slot,
                    None if candidate.value_numerator is None else str(candidate.value_numerator),
                    (
                        None
                        if candidate.value_denominator is None
                        else str(candidate.value_denominator)
                    ),
                    candidate.unit,
                    candidate.corroboration_status,
                    candidate.corroboration_lane,
                    _stable_flags(other),
                    _polygon(candidate.polygon, candidate.coordinate_space),
                ]
            )
        rows[anchor_id] = _Row(
            [sha256, page_index, f"rank:{rank}", *where], anchor, _sorted(readings), wall
        )
    return rows


def _row_decisions(
    db: Session, anchor_ids: Collection[UUID]
) -> dict[UUID, list[SlotRowReviewDecision]]:
    """Every wall and width decision on these rows, oldest first (one statement)."""
    grouped: dict[UUID, list[SlotRowReviewDecision]] = defaultdict(list)
    if anchor_ids:
        for decision in db.scalars(
            select(SlotRowReviewDecision)
            .where(SlotRowReviewDecision.row_candidate_id.in_(set(anchor_ids)))
            .order_by(SlotRowReviewDecision.created_at, SlotRowReviewDecision.id)
        ).all():
            grouped[decision.row_candidate_id].append(decision)
    return grouped


def _in_force[T: (SlotRowReviewDecision, ArchitectPairingRecord, ArchitectViewMatchRecord)](
    records: Sequence[T], at: datetime
) -> list[T]:
    return [record for record in records if record.created_at <= at]


def _chain_tip(decisions: Sequence[SlotRowReviewDecision]) -> SlotRowReviewDecision | None:
    """The decision no other one (of those given) supersedes: the one in force. Followed by
    `supersedes_id`, not by time; two tips (never written by the API) fall back to the newest."""
    superseded = {decision.supersedes_id for decision in decisions}
    tips = [decision for decision in decisions if decision.id not in superseded]
    return tips[-1] if tips else None


def _effective_wall(row: _Row, decision: SlotRowReviewDecision | None) -> str | None:
    """The wall layout the width check used on this row, the way `slot_row_check` decides it."""
    flags = [] if row.wall is None else (row.wall.ambiguity_flags or [])
    value, _why = effective_row_wall(
        layout=_flag(flags, "walls-sealed:"),
        source=_flag(flags, "wall-source:"),
        held=any(flag.startswith("walls-held:") for flag in flags),
        reviewer_confirmed=None if decision is None else decision.wall_config,
    )
    return value


@dataclass(frozen=True, slots=True)
class _ArchitectSide:
    """A row's architect pairing and view-match records, by content (at most five statements)."""

    pairings: Mapping[UUID, list[ArchitectPairingRecord]]
    matches: Mapping[UUID, list[ArchitectViewMatchRecord]]
    candidates: Mapping[UUID, list[object]]
    views: Mapping[UUID, list[object]]
    versions: Mapping[str, str]

    def content(self, anchor_id: UUID, at: datetime) -> dict[str, object]:
        """What the architect check could read on this row at `at`, oldest first, ids left out.

        Every record in force is listed (not only the one that counted), so a reviewer's pick,
        "none of these" or hand pairing on the row always asks the architect result again; the
        width result never reads any of it.
        """
        return {
            "pairings": [
                [
                    record.source,
                    record.status,
                    _sorted(self._pair(raw) for raw in record.pairs or ()),
                    self._view_named(record.details or {}),
                ]
                for record in _in_force(self.pairings.get(anchor_id, ()), at)
            ],
            "view_matches": [
                [
                    record.source,
                    record.status,
                    (
                        None
                        if record.matched_view_id is None
                        else self.views.get(
                            record.matched_view_id, ["unknown view", str(record.matched_view_id)]
                        )
                    ),
                ]
                for record in _in_force(self.matches.get(anchor_id, ()), at)
            ],
        }

    def _pair(self, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        candidate = _uuid(raw.get("architect_candidate_id"))
        indices = raw.get("vendor_slot_indices")
        return [
            raw.get("kind"),
            sorted(indices, key=str) if isinstance(indices, list) else indices,
            (
                ["unreadable", raw.get("architect_candidate_id")]
                if candidate is None
                else self.candidates.get(candidate, ["unknown candidate", str(candidate)])
            ),
        ]

    def _view_named(self, details: Mapping[str, object]) -> object:
        if details.get("architect_view_id") is None:
            return None
        version = str(details.get("architect_document_version_id"))
        return [
            self.versions.get(version, version),
            details.get("architect_page_index"),
            details.get("architect_view_number"),
            details.get("architect_view_tag"),
        ]


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _architect_side(db: Session, anchor_ids: Collection[UUID]) -> _ArchitectSide:
    if not anchor_ids:
        return _ArchitectSide({}, {}, {}, {}, {})
    pairings: dict[UUID, list[ArchitectPairingRecord]] = defaultdict(list)
    for record in db.scalars(
        select(ArchitectPairingRecord)
        .where(ArchitectPairingRecord.row_anchor_candidate_id.in_(set(anchor_ids)))
        .order_by(ArchitectPairingRecord.created_at, ArchitectPairingRecord.id)
    ).all():
        pairings[record.row_anchor_candidate_id].append(record)
    matches: dict[UUID, list[ArchitectViewMatchRecord]] = defaultdict(list)
    for match in db.scalars(
        select(ArchitectViewMatchRecord)
        .where(ArchitectViewMatchRecord.row_anchor_candidate_id.in_(set(anchor_ids)))
        .order_by(ArchitectViewMatchRecord.created_at, ArchitectViewMatchRecord.id)
    ).all():
        matches[match.row_anchor_candidate_id].append(match)
    named = {
        identity
        for records in pairings.values()
        for record in records
        for raw in record.pairs or ()
        if isinstance(raw, dict)
        and (identity := _uuid(raw.get("architect_candidate_id"))) is not None
    }
    candidates: dict[UUID, list[object]] = {}
    if named:
        for (
            identity,
            numerator,
            denominator,
            unit,
            raw_text,
            polygon,
            space,
            flags,
            page_index,
            sha256,
        ) in db.execute(
            select(
                ObservationCandidate.id,
                ObservationCandidate.value_numerator,
                ObservationCandidate.value_denominator,
                ObservationCandidate.unit,
                ObservationCandidate.raw_text,
                ObservationCandidate.polygon,
                ObservationCandidate.coordinate_space,
                ObservationCandidate.ambiguity_flags,
                Page.index,
                DocumentVersion.sha256,
            )
            .join(Page, Page.id == ObservationCandidate.page_id)
            .join(DocumentVersion, DocumentVersion.id == ObservationCandidate.document_version_id)
            .where(ObservationCandidate.id.in_(named))
        ).all():
            # Which dimension, not only its value: two 30" spans on one page are two pairings.
            candidates[identity] = [
                sha256,
                page_index,
                None if numerator is None else str(numerator),
                None if denominator is None else str(denominator),
                unit,
                raw_text,
                _polygon(polygon, space),
                [flag for flag in _stable_flags(flags) if flag.startswith("arch-")],
            ]
    view_ids = {
        match.matched_view_id
        for records in matches.values()
        for match in records
        if match.matched_view_id is not None
    }
    views: dict[UUID, list[object]] = {}
    if view_ids:
        for identity, number, tag, page_index, sha256 in db.execute(
            select(
                ArchitectViewIndexEntry.id,
                ArchitectViewIndexEntry.view_number,
                ArchitectViewIndexEntry.view_tag,
                Page.index,
                DocumentVersion.sha256,
            )
            .join(Page, Page.id == ArchitectViewIndexEntry.page_id)
            .join(
                DocumentVersion, DocumentVersion.id == ArchitectViewIndexEntry.document_version_id
            )
            .where(ArchitectViewIndexEntry.id.in_(view_ids))
        ).all():
            views[identity] = [sha256, page_index, number, tag]
    version_ids = {
        identity
        for records in pairings.values()
        for record in records
        if (identity := _uuid((record.details or {}).get("architect_document_version_id")))
        is not None
    }
    versions: dict[str, str] = {}
    if version_ids:
        versions = {
            str(identity): sha256
            for identity, sha256 in db.execute(
                select(DocumentVersion.id, DocumentVersion.sha256).where(
                    DocumentVersion.id.in_(version_ids)
                )
            ).all()
        }
    return _ArchitectSide(pairings, matches, candidates, views, versions)


def _observations(db: Session, identities: Collection[UUID]) -> dict[UUID, list[object]]:
    """Each canonical observation by what it says and where it is drawn, never by its id: the
    same value read at another place on the page is another observation."""
    if not identities:
        return {}
    return {
        identity: [
            sha256,
            page_index,
            document_role,
            semantic_type,
            _exact_text(numerator, denominator),
            unit,
            status,
            _polygon(polygon, space),
        ]
        for (
            identity,
            sha256,
            page_index,
            document_role,
            semantic_type,
            numerator,
            denominator,
            unit,
            status,
            polygon,
            space,
        ) in db.execute(
            select(
                CanonicalObservation.id,
                DocumentVersion.sha256,
                Page.index,
                CanonicalObservation.document_role,
                CanonicalObservation.semantic_type,
                CanonicalObservation.value_numerator,
                CanonicalObservation.value_denominator,
                CanonicalObservation.unit,
                CanonicalObservation.status,
                CanonicalObservation.polygon,
                CanonicalObservation.coordinate_space,
            )
            .join(Page, Page.id == CanonicalObservation.page_id)
            .join(DocumentVersion, DocumentVersion.id == CanonicalObservation.document_version_id)
            .where(CanonicalObservation.id.in_(set(identities)))
        ).all()
    }


def fingerprint_bodies(db: Session, findings: Collection[Finding]) -> dict[UUID, dict[str, Any]]:
    """What `result_fingerprints` hashes, per finding (canonical-JSON-ready, exact text only).

    See the module docstring for what is in it and what is left out, and why.
    """
    if not findings:
        return {}
    by_id = {finding.id: finding for finding in findings}
    runs = {
        run.id: (run, snapshot, rule_id)
        for run, snapshot, rule_id in db.execute(
            select(CheckRun, RuleSnapshot, RuleDefinition.rule_id)
            .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
            .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
            .where(CheckRun.id.in_({finding.check_run_id for finding in findings}))
        ).all()
    }
    reads = {run_id: _rule_reads(snapshot) for run_id, (_, snapshot, _) in runs.items()}
    inputs: dict[UUID, list[VerdictInput]] = defaultdict(list)
    for row in db.scalars(select(VerdictInput).where(VerdictInput.check_run_id.in_(runs))).all():
        inputs[row.check_run_id].append(row)
    evidence: dict[UUID, list[FindingEvidence]] = defaultdict(list)
    for link in db.scalars(
        select(FindingEvidence).where(FindingEvidence.finding_id.in_(by_id))
    ).all():
        evidence[link.finding_id].append(link)
    observations = _observations(
        db,
        {
            row.canonical_observation_id
            for rows in inputs.values()
            for row in rows
            if row.canonical_observation_id is not None
        }
        | {link.canonical_observation_id for links in evidence.values() for link in links},
    )
    parameters = (
        _parameter_layers(db, findings)
        if any(reads[finding.check_run_id].parameters for finding in findings)
        else _ParameterLayers({})
    )
    anchor_ids = {
        f.scope_row_candidate_id for f in findings if f.scope_row_candidate_id is not None
    }
    rows = _rows(db, anchor_ids)
    decisions = _row_decisions(db, anchor_ids)
    architect = _architect_side(
        db,
        {
            f.scope_row_candidate_id
            for f in findings
            if f.scope_row_candidate_id is not None and reads[f.check_run_id].architect
        },
    )

    def observed(identity: UUID) -> list[object]:
        return observations.get(identity, ["unknown observation", str(identity)])

    result: dict[UUID, dict[str, Any]] = {}
    for finding in findings:
        run, snapshot, rule_id = runs[finding.check_run_id]
        rule = reads[run.id]
        used = parameters.used(finding, run, rule.parameters) if rule.parameters else {}
        body: dict[str, Any] = {
            "fingerprint_version": FINGERPRINT_VERSION,
            "package_revision_id": str(finding.package_revision_id),
            "rule_id": rule_id,
            "rule_snapshot_hash": snapshot.snapshot_id,
            "engine_version": run.engine_version,
            "scope_item_id": _text(finding.scope_item_id),
            "variant": finding.variant,
            "outcome": finding.outcome,
            "severity": finding.severity,
            # Prose only for a result that did not decide: there it is the one record of why the
            # check held (no hold has a code yet), so a different hold asks again. A decided
            # result's reason is a sentence about values hashed below, and is left out.
            "hold_reason": None if finding.outcome in _DECIDED else finding.reason,
            "delta": (
                None
                if finding.delta_numerator is None
                else [
                    str(finding.delta_numerator),
                    str(finding.delta_denominator),
                    finding.delta_unit,
                ]
            ),
            "trace": _stable_trace(finding.trace),
            "parameters": used if used is not None else _cited_fallback(finding, run),
            "verdict_inputs": _sorted(
                [
                    row.operand_name,
                    _exact_text(row.value_numerator, row.value_denominator),
                    row.unit,
                    row.evidence_status,
                    (
                        observed(row.canonical_observation_id)
                        if row.canonical_observation_id is not None
                        else (
                            ["typed for this row"]
                            if row.slot_row_review_decision_id is not None
                            else ["value"]
                        )
                    ),
                ]
                for row in inputs.get(run.id, [])
            ),
            "finding_evidence": _sorted(
                [link.role, *observed(link.canonical_observation_id)]
                for link in evidence.get(finding.id, [])
            ),
            "row": None,
        }
        anchor_id = finding.scope_row_candidate_id
        if anchor_id is not None:
            stored_row = rows[anchor_id]
            decision = _chain_tip(_in_force(decisions.get(anchor_id, ()), run.created_at))
            body["row"] = {
                "identity": stored_row.identity,
                "readings": stored_row.readings,
                # No decision and a decision that typed nothing (a wall-only save) read alike.
                "typed_values": {} if decision is None else _typed_values(decision.measurements),
                "wall_layout": _effective_wall(stored_row, decision) if rule.walls else None,
                "architect": (
                    architect.content(anchor_id, run.created_at) if rule.architect else None
                ),
            }
        result[finding.id] = body
    return result


def carry_decisions_over(
    db: Session,
    *,
    package_revision_id: UUID,
    previous_finding_ids: Collection[UUID],
    when: datetime | None = None,
) -> int:
    """Link each new live finding to the still-valid decision on the identical one it replaced.

    Called by `run_checks` after the last new finding is written, in the same transaction, with the
    findings that were live just before `supersede_runs`. Returns how many decisions were carried.
    Previous findings from another revision are ignored. Commits nothing.
    """
    if not previous_finding_ids:
        return 0
    at = when if when is not None else utc_now()
    previous = list(
        db.scalars(
            select(Finding).where(
                Finding.id.in_(set(previous_finding_ids)),
                Finding.package_revision_id == package_revision_id,
            )
        ).all()
    )
    if not previous:
        return 0
    previous_ids = {finding.id for finding in previous}
    # A hand-confirmed countertop's result (the legacy manual path, `scope_item_id`) never carries:
    # its reviewer inputs live in the countertop-run tables this module does not read, so it always
    # asks again, which is the safe direction.
    current = [
        finding
        for finding in db.scalars(
            select(Finding)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .where(
                Finding.package_revision_id == package_revision_id,
                CheckRun.superseded_at.is_(None),
            )
        ).all()
        if finding.id not in previous_ids and finding.scope_item_id is None
    ]
    if not current:
        return 0
    prints = result_fingerprints(db, [*previous, *current])
    old_by_print: dict[str, list[Finding]] = defaultdict(list)
    for finding in previous:
        old_by_print[prints[finding.id]].append(finding)
    new_by_print: dict[str, list[Finding]] = defaultdict(list)
    for finding in current:
        new_by_print[prints[finding.id]].append(finding)
    records = decision_records(db, previous_ids)

    carried = 0
    for fingerprint, new_findings in new_by_print.items():
        old_findings = old_by_print.get(fingerprint, [])
        if len(new_findings) != 1 or len(old_findings) != 1:
            continue
        (new,), (old,) = new_findings, old_findings
        decision = records.decisions.get(old.id)
        if (
            decision is None
            or decision.action.action not in CARRIABLE_ACTIONS
            or decision.action.package_revision_id != package_revision_id
            or not decision_holds(old, records, when=at)
        ):
            continue
        db.add(
            FindingDecisionCarryover(
                package_revision_id=package_revision_id,
                new_finding_id=new.id,
                from_finding_id=old.id,
                review_action_id=decision.action.id,
                action=decision.action.action,
                matched_on_hash=fingerprint,
            )
        )
        carried += 1
    if carried:
        db.flush()
    return carried
