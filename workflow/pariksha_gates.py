"""Pariksha's gates: a code-only check between agents that passes the work on or stops it (#1228).

**Why.** The pipeline is a line of agents: Bala sorts the pages, Rekha chooses the countertop row
(and its walls and counter breaks), Akshara reads the numbers, Milan pairs them with the
architect's, Nina does the exact arithmetic and Vani words the findings. Before #1228 each handed
its result straight to the next, so one agent's mistake spread and cost money: when both readers
agreed on the outline of the stone top (a line with no numbers) as "the countertop row", ten label
questions were then asked about it. A gate sits at each hand-off and decides:

* **pass**: the work goes on to the next agent;
* **stop**: the item waits for a person, with a plain reason, and no later agent runs on it, so
  nothing more is spent on it.

**The rules every gate keeps.**

* Gates are deterministic code. No model is asked anything.
* A gate may only hold. It never makes a hold into an automatic result, never lets more through and
  never turns a PASS into a FAIL or the other way round.
* Never silent: every gate result names its agent, its gate, the page, row or finding, pass or stop
  and why, and is kept in the run's own records (`GATE_FLAG` on a reading candidate, `NOTE_PREFIX`
  in a finding's notes). `workflow/pariksha_run_check.gate_tally` counts them per agent.
* No information is lost: where stopping would take away something a reviewer gets today, the gate
  records the issue and does not stop (`GateDecision.RECORDED`).

**The gates.**

| Agent | Gate | Where | Stops |
|---|---|---|---|
| Bala | `every-page-accounted` | `workflow/unread_pages.py` (#1193) | a page with no row to read, with code's reason |
| Rekha | `row-has-printed-numbers` | `workflow/slot_reader.read_slot_pages`, right after the row choice | a chosen line with no printed numbers (`workflow/rekha_gate.py`) |
| Akshara | `seal` | `extraction/slot_reader/seal.py` | a label without two agreeing readers (and its drawn length) |
| Milan | `two-judgment-pairing` | `workflow/stages._architect_row_check` | an architect result resting on one judgment of the pairing |
| Nina | `safety-rules` | the end of `workflow/stages.run_checks` (`workflow/nina_gate.py`) | a PASS or FAIL that breaks a safety rule |
| Vani | `wording-guard` | `workflow/findings_composer.compose_findings` | AI wording that changes a number or an outcome |

Only Rekha's and Nina's are new behaviour; the other four were already enforced where they are and
are only counted as gates (`gate_tally`), with no change to what they do.

This module is the record only, and imports nothing heavy, so the API's read paths may use it
(`tests/api/test_no_heavy_work.py`); Rekha's rule, which reads the row builder's geometry, is in
`workflow/rekha_gate.py`.

Source: issue #1228. Verification: `tests/workflow/test_pariksha_gates.py`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "GATE_FLAG",
    "NOTE_PREFIX",
    "REKHA_NO_NUMBERS",
    "REKHA_NO_NUMBERS_REASON",
    "REKHA_OVERALL_KEPT",
    "REKHA_OVERALL_KEPT_REASON",
    "REKHA_PASS",
    "REKHA_PASS_REASON",
    "REKHA_READ_THROUGH_KEPT",
    "REKHA_READ_THROUGH_KEPT_REASON",
    "REKHA_ROW_GATE",
    "REVIEWER_HOLD_SENTENCES",
    "Agent",
    "GateDecision",
    "GateResult",
    "GateScope",
    "gate_results_in",
    "nina_hold_code",
    "reviewer_hold_sentence",
    "stopped_by_rekha",
]


class Agent(StrEnum):
    """The agents of the review line (`06 Plans/Agent stack (proposed 2026-10-13)`)."""

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


class GateDecision(StrEnum):
    """What a gate decided about one item."""

    PASS = "pass"
    """The work goes on to the next agent."""
    STOP = "stop"
    """The item waits for a person; no later agent runs on it."""
    RECORDED = "recorded"
    """The gate found the issue but did not stop, because stopping would take away something the
    reviewer gets today (no information is lost). Counted apart from passes and stops."""


class GateScope(StrEnum):
    """What one gate result is about."""

    PAGE = "page"
    ROW = "row"
    LABEL = "label"
    FINDING = "finding"


#: The flag a gate result is kept as on a reading candidate:
#: `pariksha:<agent>:<gate>:<decision>:<code>` (`GateResult.flag`).
GATE_FLAG: Final = "pariksha:"

#: How a gate result starts in a finding's notes (`GateResult.note`).
NOTE_PREFIX: Final = "Pariksha gate"

#: Rekha's gate (new in #1228).
REKHA_ROW_GATE: Final = "row-has-printed-numbers"

#: The code and plain reason of a Rekha stop.
REKHA_NO_NUMBERS: Final = "no-printed-numbers"
REKHA_NO_NUMBERS_REASON: Final = (
    "The chosen line has no printed numbers; the reviewer picks the row."
)

#: Recorded, not stopped: the chosen line has no printed numbers of its own, but the vendor's
#: equal-shares chain for the same run may give its widths (#1086), which the reviewer gets today.
REKHA_READ_THROUGH_KEPT: Final = "no-printed-numbers-read-through-kept"
REKHA_READ_THROUGH_KEPT_REASON: Final = (
    "The chosen line has no printed numbers of its own, but the vendor's equal-shares line for "
    "the same run may give its widths, so it is read as before."
)
#: Recorded, not stopped: no piece of the chosen line carries a printed number, but its overall
#: does, and the reviewer gets its reading today.
REKHA_OVERALL_KEPT: Final = "no-piece-numbers-overall-kept"
REKHA_OVERALL_KEPT_REASON: Final = (
    "No piece of the chosen line has a printed number, but its overall does, so it is read as "
    "before."
)
REKHA_PASS: Final = "printed-numbers"
REKHA_PASS_REASON: Final = "The chosen line has a printed number on at least one of its pieces."


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's decision about one item, with the reason in plain words."""

    agent: Agent
    gate: str
    scope: GateScope
    decision: GateDecision
    code: str
    """A short stable name for why, such as `no-printed-numbers`."""
    reason: str
    """Code's own sentence: no drawing value is ever put in it."""
    scope_id: str | None = None
    """The page number, row, label or finding the result is about, when known."""

    def __post_init__(self) -> None:
        if not self.gate or not self.code or not self.reason.strip():
            raise ValueError("a gate result needs its gate, its code and its reason")
        if ":" in self.gate or ":" in self.code:
            raise ValueError("a gate or code name may not contain ':'")

    @property
    def stopped(self) -> bool:
        return self.decision is GateDecision.STOP

    @property
    def flag(self) -> str:
        """The result as a reading-candidate flag."""
        return (
            f"{GATE_FLAG}{self.agent.value.lower()}:{self.gate}:{self.decision.value}:{self.code}"
        )

    @property
    def note(self) -> str:
        """The result as a line of a finding's notes."""
        return (
            f"{NOTE_PREFIX} ({self.agent.value}, {self.gate}): {self.decision.value} "
            f"[{self.code}]: {self.reason.strip()}"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "agent": self.agent.value,
            "gate": self.gate,
            "scope": self.scope.value,
            "scope_id": self.scope_id,
            "decision": self.decision.value,
            "code": self.code,
            "reason": self.reason,
        }


#: What a reviewer reads when Nina's gate holds a PASS or FAIL (#1228 review): what the result
#: rests on and what to do, with no agent name and no talk of the pipeline. One sentence per run
#: check item code the gate can hold (`workflow.pariksha_run_check.decision_faults`); each is also
#: the note line that records the hold, so the run check can count it (`nina_hold_code`).
_CONFIRM: Final = "Held for you to confirm: "
REVIEWER_HOLD_SENTENCES: Final[dict[str, str]] = {
    "pass-on-unwitnessed-reading": _CONFIRM
    + "this PASS rests on a reading whose drawn length was not checked. Confirm the reading "
    "or correct it.",
    "pass-on-automatic-walls": _CONFIRM
    + "this PASS rests on walls set automatically from the vendor's drawing. Confirm the wall "
    "layout or correct it.",
    "decision-on-one-judgment-pairing": _CONFIRM
    + "only one judgment paired the architect's dimension with the vendor's. Confirm the "
    "pairing or correct it.",
    "decision-without-pairing-record": _CONFIRM
    + "the pairing this comparison used cannot be shown. Confirm the pairing or correct it.",
    "decision-on-one-route-value": _CONFIRM
    + "a value this result uses has only one reading on record. Confirm the value or correct it.",
    "malformed-supporting-reading": _CONFIRM
    + "the readings behind a value this result uses cannot be shown. Confirm the value or "
    "correct it.",
    "decision-without-inputs": _CONFIRM
    + "the values behind this result were not all kept. Check the values before you rely on it.",
    "decision-without-calculation": _CONFIRM
    + "the calculation behind this result was not kept. Check the result before you rely on it.",
    "decision-on-unqualified-evidence": _CONFIRM
    + "a value this result uses is not confirmed (the readers disagreed, or it was not checked). "
    "Confirm the value or correct it.",
    "decision-in-unknown-unit": _CONFIRM
    + "a value this result uses has a unit the checks do not know. Check the value's unit.",
    "decision-on-value-without-source": _CONFIRM
    + "a value this result uses has nothing on the drawings behind it. Check where it comes "
    "from.",
    "decision-on-advisory-value": _CONFIRM
    + "a value this result uses may not decide a check on its own. Confirm the value or "
    "correct it.",
}
_DEFAULT_HOLD_SENTENCE: Final = (
    _CONFIRM + "this result breaks a safety rule. Confirm it or correct it."
)
_HOLD_CODES: Final = {sentence: code for code, sentence in REVIEWER_HOLD_SENTENCES.items()}


def reviewer_hold_sentence(code: str) -> str:
    """The reviewer's sentence for a held result (`REVIEWER_HOLD_SENTENCES`)."""
    return REVIEWER_HOLD_SENTENCES.get(code, _DEFAULT_HOLD_SENTENCE)


def nina_hold_code(note: str) -> str | None:
    """The run check item code a held result's note line records, or `None` if it records none."""
    if note == _DEFAULT_HOLD_SENTENCE:
        return "safety-rule"
    return _HOLD_CODES.get(note)


_AGENTS: Final = {agent.value.lower(): agent for agent in Agent}
_DECISIONS: Final = {decision.value: decision for decision in GateDecision}


def gate_results_in(
    flags: Iterable[str] | None,
    *,
    scope: GateScope,
    scope_id: str | None = None,
    reasons: dict[str, str] | None = None,
) -> tuple[GateResult, ...]:
    """The gate results kept as flags on one reading candidate; a malformed flag is skipped.

    `reasons` maps a code to its sentence; a code it does not name gets its code as the reason.
    """
    found: list[GateResult] = []
    for flag in flags or ():
        if not flag.startswith(GATE_FLAG):
            continue
        parts = flag.removeprefix(GATE_FLAG).split(":")
        if len(parts) != 4:
            continue
        agent, gate, decision, code = parts
        if agent not in _AGENTS or decision not in _DECISIONS or not gate or not code:
            continue
        found.append(
            GateResult(
                agent=_AGENTS[agent],
                gate=gate,
                scope=scope,
                decision=_DECISIONS[decision],
                code=code,
                reason=(reasons or {}).get(code, _KNOWN_REASONS.get(code, code)),
                scope_id=scope_id,
            )
        )
    return tuple(found)


_KNOWN_REASONS: Final = {
    REKHA_PASS: REKHA_PASS_REASON,
    REKHA_NO_NUMBERS: REKHA_NO_NUMBERS_REASON,
    REKHA_READ_THROUGH_KEPT: REKHA_READ_THROUGH_KEPT_REASON,
    REKHA_OVERALL_KEPT: REKHA_OVERALL_KEPT_REASON,
}


def stopped_by_rekha(flags: Sequence[str] | None) -> bool:
    """Whether a row-choice record was stopped by Rekha's gate (`rekha_row_gate`)."""
    return any(
        result.agent is Agent.REKHA and result.gate == REKHA_ROW_GATE and result.stopped
        for result in gate_results_in(flags, scope=GateScope.ROW)
    )
