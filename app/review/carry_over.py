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
rows: the revision; rule id, snapshot row and content hash; engine version; the rulebook defaults by
content (values, units, provenance, author; only the run-time stamp `declared_defaults` puts on them
is left out, see `_defaults_content`); the scope (item, slot-reader row, label); variant, outcome,
severity, reason, exact delta and notes; the whole stored trace; `parameter_set_versions`; every
sealed `verdict_inputs` row (exact numerator and denominator, unit, status, observation and
row-review ids); every `finding_evidence` link; and the reviewer inputs on a slot-reader row that
were in force when the run was written (its wall and width decisions and its architect pairing
records). That last part matters for a held row, whose stored result records no operand: changing
its walls still changes its fingerprint, so it asks again.

**Never carried:** a `correct` (the re-run is what consumes it), or anything from a finding whose
history has one; an exception whose grant has run out; a confirm or dismiss without the note
`needs_note` requires; anything from another revision (and so another project). If the latest
decision is not valid, nothing is carried; an older one is never dug out instead. Two old or two new
results sharing one fingerprint are ambiguous, and nothing is carried for them either. A
hand-confirmed countertop's result (the legacy manual path) never carries: its inputs live in the
countertop-run tables, which only the confirmed-structure resolver reads.

Nothing here imports `verdict/`, `rules/`, `extraction/` or `retrieval/`.

Verification: `tests/review/test_carry_over.py`
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.models.evidence import ArchitectPairingRecord, SlotRowReviewDecision
from app.models.review import FindingDecisionCarryover, ReviewAction, ReviewException
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.verdicts import CheckRun, Finding, FindingEvidence, VerdictInput
from app.review.exceptions import ExceptionGrant, FindingRef, decide
from app.review.requirements import needs_note

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
FINGERPRINT_VERSION = 1

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


def _inputs_in_force(
    rows: Iterable[tuple[UUID, UUID, datetime]],
) -> dict[UUID, list[tuple[UUID, datetime]]]:
    grouped: dict[UUID, list[tuple[UUID, datetime]]] = defaultdict(list)
    for identity, scope, created in rows:
        grouped[scope].append((identity, created))
    return grouped


def result_fingerprints(db: Session, findings: Collection[Finding]) -> dict[UUID, str]:
    """The "same result" hash of each finding, from stored rows only. Five statements in all."""
    return {
        identity: hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
        for identity, body in fingerprint_bodies(db, findings).items()
    }


def _defaults_content(canonical: str | None) -> str | None:
    """The rulebook defaults a run used, by content: every value, unit, provenance and author.

    `declared_defaults` stamps the set with the moment the run read it (`set_at`), so its id changes
    on every run although no value did. That stamp is the only field dropped here; anything a rule
    author or a setting could change still changes the hash. `None` for a run that stored none.
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


def fingerprint_bodies(db: Session, findings: Collection[Finding]) -> dict[UUID, dict[str, Any]]:
    """What `result_fingerprints` hashes, per finding (canonical-JSON-ready, exact text only)."""
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
    inputs: dict[UUID, list[list[object]]] = defaultdict(list)
    for row in db.scalars(select(VerdictInput).where(VerdictInput.check_run_id.in_(runs))).all():
        inputs[row.check_run_id].append(
            [
                row.operand_name,
                str(row.value_numerator),
                str(row.value_denominator),
                row.unit,
                row.evidence_status,
                _text(row.canonical_observation_id),
                _text(row.slot_row_review_decision_id),
            ]
        )
    evidence: dict[UUID, list[list[object]]] = defaultdict(list)
    for link in db.scalars(
        select(FindingEvidence).where(FindingEvidence.finding_id.in_(by_id))
    ).all():
        evidence[link.finding_id].append([str(link.canonical_observation_id), link.role])

    rows = {f.scope_row_candidate_id for f in findings if f.scope_row_candidate_id is not None}
    row_decisions = _inputs_in_force(
        db.execute(
            select(
                SlotRowReviewDecision.id,
                SlotRowReviewDecision.row_candidate_id,
                SlotRowReviewDecision.created_at,
            ).where(SlotRowReviewDecision.row_candidate_id.in_(rows))
        ).all()
        if rows
        else ()
    )
    pairings = _inputs_in_force(
        db.execute(
            select(
                ArchitectPairingRecord.id,
                ArchitectPairingRecord.row_anchor_candidate_id,
                ArchitectPairingRecord.created_at,
            ).where(ArchitectPairingRecord.row_anchor_candidate_id.in_(rows))
        ).all()
        if rows
        else ()
    )

    def in_force(
        grouped: Mapping[UUID, list[tuple[UUID, datetime]]], scope: UUID | None, at: datetime
    ) -> list[str]:
        if scope is None:
            return []
        return sorted(
            str(identity) for identity, created in grouped.get(scope, ()) if created <= at
        )

    result: dict[UUID, dict[str, Any]] = {}
    for finding in findings:
        run, snapshot, rule_id = runs[finding.check_run_id]
        defaults = _defaults_content(run.defaults_canonical_json)
        body: dict[str, Any] = {
            "fingerprint_version": FINGERPRINT_VERSION,
            "package_revision_id": str(finding.package_revision_id),
            "rule_id": rule_id,
            "rule_snapshot_row": str(snapshot.id),
            "rule_snapshot_hash": snapshot.snapshot_id,
            "engine_version": run.engine_version,
            "defaults_content": defaults,
            "scope_item_id": _text(finding.scope_item_id),
            "scope_row_candidate_id": _text(finding.scope_row_candidate_id),
            "scope_label": finding.scope_label,
            "variant": finding.variant,
            "outcome": finding.outcome,
            "severity": finding.severity,
            "reason": finding.reason,
            "delta": (
                None
                if finding.delta_numerator is None
                else [
                    str(finding.delta_numerator),
                    str(finding.delta_denominator),
                    finding.delta_unit,
                ]
            ),
            "notes": finding.notes,
            "trace": finding.trace,
            "parameter_set_versions": {
                layer: (
                    f"defaults:{defaults}"
                    if run.defaults_set_id is not None and set_id == run.defaults_set_id
                    else set_id
                )
                for layer, set_id in (finding.parameter_set_versions or {}).items()
            },
            "verdict_inputs": sorted(inputs.get(run.id, []), key=_canonical),
            "finding_evidence": sorted(evidence.get(finding.id, []), key=_canonical),
            "row_review_decisions": in_force(
                row_decisions, finding.scope_row_candidate_id, run.created_at
            ),
            "architect_pairing_records": in_force(
                pairings, finding.scope_row_candidate_id, run.created_at
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
