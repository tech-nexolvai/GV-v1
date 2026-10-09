"""One package's review records as a compact, id-keyed snapshot for the assistant (#1128).

**Built from what the screens already show, never re-derived.** The countertop part is the
`countertop-results` projection; sign-off readiness and the standing reviewer decisions are the ones
`app.review.approval` computes; the other checks are the reviewer chat's own finding facts. This
module only selects, labels and trims them, so the assistant cannot see an outcome or a number the
reviewer's screen does not.

Every number is kept as the API's display text (`84 1/2"`) and, where the API has it, as the exact
value (`169/2`), so the guard can compare an answer's numbers by value and the model can copy them by
text.

**Small enough for one prompt.** Long lists are capped and the remainder counted (`omitted`), in a
fixed order (records that need the reviewer first, then by page), so the same records always give the
same snapshot. Free text is cut at a fixed length.

Each record has a short id for the model (`C1` countertop, `F1` other check) and its real id for the
screen (`record_id`: the countertop row id, or the finding id).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Final, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.visual_ui import CountertopResultOut, CountertopResultsOut, ExactValueOut
from workflow.findings_composer import ComposerFinding, reviewer_outcome

__all__ = [
    "ArchitectPairRecord",
    "ArchitectRecord",
    "CountertopRecord",
    "DecisionRecord",
    "FindingRecord",
    "PageNoteRecord",
    "PieceRecord",
    "ReadinessRecord",
    "ReviewSnapshot",
    "RuleRecord",
    "ValueRecord",
    "build_snapshot",
    "outcome_label",
    "prompt_records",
]

#: Caps that keep one package's records inside one prompt. Records past a cap are counted.
MAX_COUNTERTOPS: Final = 60
MAX_FINDINGS: Final = 40
MAX_PIECES: Final = 16
MAX_PAGE_NOTES: Final = 40
MAX_ARCHITECT_PAIRS: Final = 12
MAX_TEXT: Final = 320
#: The prompt JSON is rebuilt without per-piece detail when it is longer than this many characters.
MAX_PROMPT_CHARS: Final = 40_000

#: Shown for a countertop no check has produced a result for yet.
NOT_CHECKED_LABEL: Final = "Not checked yet"

#: What each reviewer action is called on the screen, in plain words.
_ACTION_WORDS: Final[Mapping[str, str]] = {
    "confirm": "confirmed by the reviewer",
    "correct": "corrected by the reviewer (waiting for the checks to run again)",
    "except": "accepted as an exception by the reviewer",
    "dismiss": "dismissed by the reviewer",
}


def outcome_label(outcome: str | None) -> str:
    """The reviewer-facing word for an outcome, the same one the badge shows."""
    return NOT_CHECKED_LABEL if outcome is None else reviewer_outcome(outcome)


#: Characters that hide or reorder text: controls, zero-width and bidirectional marks.
_HIDDEN: Final = re.compile(
    "[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u00ad\u061c\u200b-\u200f\u2028-\u202e"
    "\u2060-\u2064\u2066-\u2069\ufeff]"
)
_MARKDOWN_LINK: Final = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HTML_TAG: Final = re.compile(r"<[^>]*>")
_URL: Final = re.compile(
    r"(?i)\b(?:https?|ftp|javascript|data):\S*|\bwww\.\S+|\b[a-z][a-z0-9-]*(?:\.[a-z0-9-]+)*"
    r"\.[a-z]{2,}(?:/\S*)?"
)


def sanitise(text: str) -> str:
    """Record text from readers and vendors, made inert before it is shown or put in a prompt.

    Hidden characters are removed; markdown links keep only their words; HTML tags, links,
    citation markers, placeholder braces and markdown emphasis are removed. The words stay.
    """
    text = _HIDDEN.sub("", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _HTML_TAG.sub(" ", text)
    text = _URL.sub("(link removed)", text)
    text = re.sub(r"[\[\]{}*_`#~|<>]+", " ", text)
    return text


def _cut(text: str | None) -> str | None:
    if text is None:
        return None
    collapsed = " ".join(sanitise(text).split())
    if not collapsed:
        return None
    return collapsed if len(collapsed) <= MAX_TEXT else collapsed[: MAX_TEXT - 1].rstrip() + "…"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ValueRecord(_Frozen):
    """A number as the API shows it, with its exact value (`numerator/denominator`) when known."""

    display: str
    exact: str | None = None


class PieceRecord(_Frozen):
    number: int
    """1-based, the way a reviewer counts pieces along the row."""
    value: ValueRecord | None
    kind: str | None
    source: Literal["sealed", "typed", "missing"]


class DecisionRecord(_Frozen):
    action: str
    words: str
    note: str | None
    carried_over: bool


class RuleRecord(_Frozen):
    check: str
    name: str
    tolerance: str | None
    comparison: str | None
    reason: str | None


class ArchitectPairRecord(_Frozen):
    kind: Literal["overall", "piece"]
    vendor_piece: int | None
    vendor: str | None
    architect: str | None
    difference: str | None
    outcome: str | None


class ArchitectRecord(_Frozen):
    outcome: str | None
    outcome_label: str | None
    reason: str | None
    not_compared_reason: str | None
    needs_you: bool
    pairs: tuple[ArchitectPairRecord, ...]


class CountertopRecord(_Frozen):
    id: str
    record_id: str
    finding_id: str | None
    page_number: int
    label: str
    outcome: str | None
    outcome_label: str
    needs_you: bool
    printed: ValueRecord | None
    needed: ValueRecord | None
    difference: ValueRecord | None
    field_cut_per_end: ValueRecord | None
    field_cut_count: int | None
    pieces: tuple[PieceRecord, ...]
    pieces_omitted: int
    wall: str | None
    wall_source: str
    hold_code: str | None
    hold_reason: str | None
    drawn_length_note: str | None
    decision: DecisionRecord | None
    rule: RuleRecord | None
    architect: ArchitectRecord | None


class FindingRecord(_Frozen):
    id: str
    record_id: str
    check: str
    check_name: str
    outcome: str
    outcome_label: str
    needs_you: bool
    reason: str | None
    comparison: str | None
    tolerance: str | None
    values: tuple[str, ...]
    pages: tuple[int, ...]
    decision: DecisionRecord | None


class PageNoteRecord(_Frozen):
    page_number: int
    reason: str


class ReadinessRecord(_Frozen):
    can_sign_off: bool
    reason: str | None
    blocking_findings: int
    """Sign-off's own count of findings still without a valid decision."""
    needing_you: tuple[str, ...]
    """Short ids of the records that still need the reviewer, in queue order."""


class ReviewSnapshot(_Frozen):
    package_id: str
    revision_id: str
    checks_have_run: bool
    readiness: ReadinessRecord
    countertops: tuple[CountertopRecord, ...]
    other_checks: tuple[FindingRecord, ...]
    pages_without_countertop: tuple[PageNoteRecord, ...]
    rows_not_checked: tuple[PageNoteRecord, ...]
    omitted: Mapping[str, int]
    totals: Mapping[str, int]
    """Counts over every record, before any cap: what `{count.*}` says."""

    def records(self) -> tuple[CountertopRecord | FindingRecord, ...]:
        """Every countertop, then every other check."""
        items: list[CountertopRecord | FindingRecord] = [*self.countertops, *self.other_checks]
        return tuple(items)

    def record(self, short_id: str) -> CountertopRecord | FindingRecord | None:
        return next((item for item in self.records() if item.id == short_id), None)

    def by_record_id(self, record_id: str) -> CountertopRecord | FindingRecord | None:
        """A record by the screen's id: a countertop's row id or finding id, or a finding's id."""
        for item in self.records():
            finding_id = item.finding_id if isinstance(item, CountertopRecord) else None
            if record_id in (item.record_id, finding_id):
                return item
        return None

    def on_page(self, page_number: int) -> tuple[CountertopRecord | FindingRecord, ...]:
        countertops = tuple(item for item in self.countertops if item.page_number == page_number)
        findings = tuple(item for item in self.other_checks if page_number in item.pages)
        return (*countertops, *findings)

    def pages(self) -> frozenset[int]:
        """Every page some record names: the pages the assistant may point to."""
        return frozenset(
            {
                *(item.page_number for item in self.countertops),
                *(page for item in self.other_checks for page in item.pages),
                *(item.page_number for item in self.pages_without_countertop),
                *(item.page_number for item in self.rows_not_checked),
            }
        )


class _Decision(Protocol):
    """The part of `app.review.carry_over.EffectiveDecision` this module reads."""

    @property
    def carried_over(self) -> bool: ...

    @property
    def action(self) -> _Action: ...


class _Action(Protocol):
    @property
    def action(self) -> str: ...

    @property
    def note(self) -> str | None: ...


class _Readiness(Protocol):
    """The part of `app.review.approval.ApprovalReadiness` this module reads."""

    @property
    def can_approve(self) -> bool: ...

    @property
    def blocking_findings(self) -> int: ...

    @property
    def blocking_finding_ids(self) -> tuple[UUID, ...]: ...

    @property
    def reason(self) -> str | None: ...


def _value(value: ExactValueOut | None) -> ValueRecord | None:
    if value is None:
        return None
    return ValueRecord(display=value.display, exact=f"{value.numerator}/{value.denominator}")


def _decision(action: str, note: str | None, carried_over: bool) -> DecisionRecord:
    return DecisionRecord(
        action=action,
        words=_ACTION_WORDS.get(action, f"{action} by the reviewer"),
        note=_cut(note),
        carried_over=carried_over,
    )


def _from_effective(decision: _Decision | None) -> DecisionRecord | None:
    if decision is None:
        return None
    return _decision(decision.action.action, decision.action.note, decision.carried_over)


def _rule(finding: ComposerFinding | None) -> RuleRecord | None:
    if finding is None:
        return None
    return RuleRecord(
        check=finding.check,
        name=finding.check_name,
        tolerance=_cut(finding.tolerance),
        comparison=_cut(finding.comparison),
        reason=_cut(finding.reason),
    )


def _architect(item: CountertopResultOut) -> ArchitectRecord | None:
    block = item.architect
    if block.outcome is None and block.not_compared_reason is None:
        return None
    return ArchitectRecord(
        outcome=None if block.outcome is None else block.outcome.value,
        outcome_label=None if block.outcome is None else outcome_label(block.outcome.value),
        reason=_cut(block.reason),
        not_compared_reason=_cut(block.not_compared_reason),
        needs_you=block.needs_decision,
        pairs=tuple(
            ArchitectPairRecord(
                kind=pair.kind,
                vendor_piece=pair.vendor_piece,
                vendor=pair.vendor_display,
                architect=pair.architect_display,
                difference=pair.delta_display,
                outcome=None if pair.outcome is None else outcome_label(pair.outcome.value),
            )
            for pair in block.compared[:MAX_ARCHITECT_PAIRS]
        ),
    )


def _countertop(
    short_id: str,
    item: CountertopResultOut,
    facts: Mapping[str, ComposerFinding],
) -> CountertopRecord:
    outcome = None if item.outcome is None else item.outcome.value
    decision = item.reviewer_decision
    return CountertopRecord(
        id=short_id,
        record_id=str(item.row_id),
        finding_id=None if item.finding_id is None else str(item.finding_id),
        page_number=item.page_number,
        label=_cut(item.label) or "Countertop",
        outcome=outcome,
        outcome_label=outcome_label(outcome),
        needs_you=item.needs_decision or item.architect.needs_decision,
        printed=_value(item.printed_overall),
        needed=_value(item.expected_total),
        difference=_value(item.delta),
        field_cut_per_end=_value(item.field_cut_per_end),
        field_cut_count=item.field_cut_count,
        pieces=tuple(
            PieceRecord(
                number=piece.index + 1,
                value=_value(piece.value),
                kind=_cut(piece.kind),
                source=piece.source,
            )
            for piece in item.pieces[:MAX_PIECES]
        ),
        pieces_omitted=max(0, len(item.pieces) - MAX_PIECES),
        wall=_cut(item.wall_layout.label),
        wall_source=item.wall_layout.source,
        hold_code=None if item.hold is None else item.hold.code,
        hold_reason=None if item.hold is None else _cut(item.hold.reason),
        drawn_length_note=_cut(item.drawn_length_note),
        decision=(
            None
            if decision is None
            else _decision(decision.action, decision.note, decision.carried_over)
        ),
        rule=_rule(None if item.finding_id is None else facts.get(str(item.finding_id))),
        architect=_architect(item),
    )


def _pages(finding: ComposerFinding) -> tuple[int, ...]:
    return tuple(
        sorted({int(page) for page in finding.evidence_pages if page.isdigit() and int(page) > 0})
    )


def build_snapshot(
    countertops: CountertopResultsOut,
    readiness: _Readiness,
    findings: Sequence[ComposerFinding],
    decisions: Mapping[UUID, _Decision],
    *,
    checks_have_run: bool,
) -> ReviewSnapshot:
    """Select, label and cap one package's records. No outcome or number is computed here."""
    facts = {finding.key: finding for finding in findings}
    blocking = {str(identity) for identity in readiness.blocking_finding_ids}

    # Records that need the reviewer first, then page order: the cap never hides a blocker first.
    ordered = sorted(
        countertops.items,
        key=lambda item: (
            not (item.needs_decision or item.architect.needs_decision),
            item.page_number,
        ),
    )
    kept = sorted(ordered[:MAX_COUNTERTOPS], key=lambda item: item.page_number)
    countertop_records = tuple(
        _countertop(f"C{index}", item, facts) for index, item in enumerate(kept, start=1)
    )

    # Findings already shown on a countertop (its width check and its architect check).
    shown = {
        str(identity)
        for item in countertops.items
        for identity in (item.finding_id, item.architect.finding_id)
        if identity is not None
    }
    others = [finding for finding in findings if finding.key not in shown]
    others.sort(
        key=lambda finding: (
            finding.key not in blocking,
            (_pages(finding) or (10**6,))[0],
            finding.check,
        )
    )
    other_records = tuple(
        FindingRecord(
            id=f"F{index}",
            record_id=finding.key,
            check=finding.check,
            check_name=_cut(finding.check_name) or finding.check,
            outcome=finding.outcome,
            outcome_label=outcome_label(finding.outcome),
            needs_you=finding.key in blocking,
            reason=_cut(finding.reason),
            comparison=_cut(finding.comparison),
            tolerance=_cut(finding.tolerance),
            values=tuple(
                text
                for operand in finding.operands[:8]
                if (text := _cut(f"{operand.name}: {operand.value}")) is not None
            ),
            pages=_pages(finding),
            decision=_from_effective(decisions.get(UUID(finding.key))),
        )
        for index, finding in enumerate(others[:MAX_FINDINGS], start=1)
    )

    queue: list[CountertopRecord | FindingRecord] = [*countertop_records, *other_records]
    needing_you = tuple(item.id for item in queue if item.needs_you)
    omitted = {
        name: count
        for name, count in (
            ("countertops", len(countertops.items) - len(kept)),
            ("other_checks", len(others) - len(other_records)),
            (
                "pages_without_countertop",
                len(countertops.pages_without_countertop) - MAX_PAGE_NOTES,
            ),
            ("rows_not_checked", len(countertops.rows_not_checked) - MAX_PAGE_NOTES),
        )
        if count > 0
    }
    return ReviewSnapshot(
        package_id=str(countertops.package_id),
        revision_id=str(countertops.revision_id),
        checks_have_run=checks_have_run,
        readiness=ReadinessRecord(
            can_sign_off=readiness.can_approve,
            reason=_cut(readiness.reason),
            blocking_findings=readiness.blocking_findings,
            needing_you=needing_you,
        ),
        countertops=countertop_records,
        other_checks=other_records,
        pages_without_countertop=tuple(
            PageNoteRecord(page_number=page.page_number, reason=_cut(page.reason) or "")
            for page in countertops.pages_without_countertop[:MAX_PAGE_NOTES]
        ),
        rows_not_checked=tuple(
            PageNoteRecord(page_number=page.page_number, reason=_cut(page.reason) or "")
            for page in countertops.rows_not_checked[:MAX_PAGE_NOTES]
        ),
        omitted=omitted,
        totals=_totals(countertops, others, readiness, decisions),
    )


#: Decisions after which a result no longer needs correction from the reviewer's side.
_SETTLED: Final = frozenset({"except", "dismiss"})


def _totals(
    countertops: CountertopResultsOut,
    others: Sequence[ComposerFinding],
    readiness: _Readiness,
    decisions: Mapping[UUID, _Decision],
) -> dict[str, int]:
    """Counts over the uncapped lists, decision-aware: a decided result is not counted as open."""
    states: list[tuple[str | None, str | None]] = [
        (
            None if item.outcome is None else item.outcome.value,
            None if item.reviewer_decision is None else item.reviewer_decision.action,
        )
        for item in countertops.items
    ]
    for finding in others:
        decision = decisions.get(UUID(finding.key))
        states.append((finding.outcome, None if decision is None else decision.action.action))
    return {
        "countertops": len(countertops.items),
        "checks": len(others),
        "pass": sum(outcome == "PASS" for outcome, _ in states),
        "fail": sum(outcome == "FAIL" and action not in _SETTLED for outcome, action in states),
        "review": sum(
            outcome == "REVIEW_REQUIRED" and action is None for outcome, action in states
        ),
        "waiting": sum(outcome == "NOT_FOUND" and action is None for outcome, action in states),
        "not_checked": sum(item.outcome is None for item in countertops.items),
        "no_countertop_pages": len(countertops.pages_without_countertop),
        "rows_not_checked": len(countertops.rows_not_checked),
        "needs_you": readiness.blocking_findings,
    }


def _drop_none(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _drop_none(item)
            for key, item in value.items()
            if item is not None and item != () and item != [] and item != {}
        }
    if isinstance(value, list | tuple):
        return [_drop_none(item) for item in value]
    return value


def _prompt_value(value: ValueRecord | None) -> str | None:
    return None if value is None else value.display


def _prompt_countertop(item: CountertopRecord, *, detail: bool) -> dict[str, object]:
    return {
        "id": item.id,
        "page": item.page_number,
        "label": item.label,
        "outcome": item.outcome_label,
        "needs_reviewer": item.needs_you,
        "printed_overall": _prompt_value(item.printed),
        "needed_overall": _prompt_value(item.needed),
        "difference": _prompt_value(item.difference),
        "field_cut_per_end": _prompt_value(item.field_cut_per_end),
        "field_cut_ends": item.field_cut_count,
        "pieces": (
            [
                {
                    "piece": piece.number,
                    "width": _prompt_value(piece.value),
                    "kind": piece.kind,
                    "source": piece.source,
                }
                for piece in item.pieces
            ]
            if detail
            else None
        ),
        "pieces_not_listed": (item.pieces_omitted if detail else len(item.pieces)) or None,
        "walls": item.wall,
        "walls_from": item.wall_source,
        "held_because": item.hold_reason,
        "drawn_length_note": item.drawn_length_note,
        "reviewer_decision": (
            None
            if item.decision is None
            else {
                "decision": item.decision.words,
                "note": item.decision.note,
                "carried_over_from_earlier_run": item.decision.carried_over or None,
            }
        ),
        "rule": (
            None
            if item.rule is None
            else {
                "check": item.rule.name,
                "tolerance": item.rule.tolerance,
                "comparison": item.rule.comparison,
                "reason": item.rule.reason,
            }
        ),
        "architect_check": (
            None
            if item.architect is None
            else {
                "outcome": item.architect.outcome_label,
                "reason": item.architect.reason,
                "not_compared_because": item.architect.not_compared_reason,
                "needs_reviewer": item.architect.needs_you or None,
                "compared": (
                    [pair.model_dump() for pair in item.architect.pairs] if detail else None
                ),
            }
        ),
    }


def _prompt_finding(item: FindingRecord) -> dict[str, object]:
    return {
        "id": item.id,
        "check": item.check_name,
        "outcome": item.outcome_label,
        "needs_reviewer": item.needs_you,
        "pages": list(item.pages),
        "reason": item.reason,
        "comparison": item.comparison,
        "tolerance": item.tolerance,
        "values": list(item.values),
        "reviewer_decision": (
            None
            if item.decision is None
            else {
                "decision": item.decision.words,
                "note": item.decision.note,
                "carried_over_from_earlier_run": item.decision.carried_over or None,
            }
        ),
    }


def prompt_records(snapshot: ReviewSnapshot) -> str:
    """The snapshot as compact JSON for the model: short ids and display text only.

    Rebuilt without per-piece and per-pair detail when the full form is too long, so one package
    always fits one prompt; the guard reads the full snapshot either way.
    """
    for detail in (True, False):
        document = {
            "checks_have_run": snapshot.checks_have_run,
            "sign_off": {
                "can_sign_off": snapshot.readiness.can_sign_off,
                "reason": snapshot.readiness.reason,
                "findings_without_a_decision": snapshot.readiness.blocking_findings,
                "records_needing_reviewer": list(snapshot.readiness.needing_you),
            },
            "countertops": [
                _prompt_countertop(item, detail=detail) for item in snapshot.countertops
            ],
            "other_checks": [_prompt_finding(item) for item in snapshot.other_checks],
            "pages_without_countertop": [
                {"page": page.page_number, "reason": page.reason}
                for page in snapshot.pages_without_countertop
            ],
            "countertop_rows_not_checked": [
                {"page": page.page_number, "reason": page.reason}
                for page in snapshot.rows_not_checked
            ],
            "records_left_out_for_length": dict(snapshot.omitted),
        }
        text = json.dumps(_drop_none(document), ensure_ascii=False, separators=(",", ":"))
        if len(text) <= MAX_PROMPT_CHARS:
            return text
    return text
