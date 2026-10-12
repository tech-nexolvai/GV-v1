"""A countertop width checked for every wall case, when its walls are not established (#1202).

**The rule (decided 2026-10-13).** GV's field cut is added once at each end where the stone meets a
wall: walls at both ends, two field cuts; a wall at one end, one; no end walls (back wall only, an
island, or stone that stops at side panels), none. When a row's numbers are all sealed but its
walls are not established, the width check is run once for **every** wall case the rulebook
publishes (`verdict.every_variant`):

- wrong in every case → **FAIL**, "Wrong whatever the walls: it should be 94", 95" or 96" depending
  on the walls; the vendor wrote 99"." The walls are not needed;
- right in every case → **PASS** (only possible when the project's field cut is nothing);
- otherwise the row still needs its walls, and each case's result is kept for the reviewer.

Every number is the verdict engine's: the expected widths here are read back from its own trace,
never worked out again. Every hold still applies before this runs (`slot_row_check` reaches it only
for a qualified row whose numbers all sealed), and a PASS still meets the drawn-length witness rule.

`stored_wall_cases` reads the cases back from a stored finding's trace, for the reviewer's one-click
wall answer (each choice with its result) and the "i" explanation's worked example.

Source: issue #1202 · Verification: `tests/workflow/test_wall_cases.py`
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Final

from rules.parameters import ResolvedParameter
from rules.schema import Applicability
from rules.semantic_types import SemanticType
from rules.snapshot import RuleSnapshot
from units.imperial import format_inches, parse_imperial
from units.measurement import Measurement
from verdict.every_variant import (
    VariantOutcome,
    combined_finding,
    execute_every_variant,
    trace_result,
    unanimous_outcome,
)
from verdict.finding import Finding
from verdict.operands import VerdictOperand
from verdict.outcomes import Outcome
from vocabulary.wall_layouts import NOT_NEEDED_FAIL_LINE, NOT_NEEDED_PASS_LINE

__all__ = [
    "EVERY_WALL_CASE_OPERATION_SUFFIX",
    "StoredWallCase",
    "is_every_wall_case",
    "scale_inputs",
    "stored_wall_cases",
    "wall_free_finding",
]

#: How the combined trace names its operation: `"<operation> for every wall_config"`.
EVERY_WALL_CASE_OPERATION_SUFFIX: Final = f" for every {SemanticType.WALL_CONFIG.value}"

#: The field-cut count's words, as the rule says them.
_COUNT_WORDS: Final = {2: "walls at both ends", 1: "a wall at one end", 0: "no end walls"}


def _inches(value: Fraction) -> str:
    return f'{format_inches(value)}"'


def _listed(items: Sequence[str], joiner: str = "or") -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} {joiner} {items[-1]}"


def _counts(snapshot: RuleSnapshot) -> dict[str, int | None]:
    """Each published variant's field-cut count, from the rulebook's own extras."""
    applicability = snapshot.rule.applicability
    if not isinstance(applicability, Applicability):
        return {}
    counts: dict[str, int | None] = {}
    for variant in applicability.variants:
        raw = variant.extras.get("field_cut_count")
        counts[variant.when] = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
    return counts


def _expected(item: VariantOutcome) -> Fraction | None:
    value = trace_result(item.finding, "expected_width")
    return value.exact if isinstance(value, Measurement) else None


def _by_count(
    snapshot: RuleSnapshot, outcomes: Sequence[VariantOutcome]
) -> list[tuple[int, Fraction, Outcome]]:
    """`(field-cut count, expected width, outcome)` once per count, fewest cuts first."""
    counts = _counts(snapshot)
    seen: dict[int, tuple[Fraction, Outcome]] = {}
    for item in outcomes:
        count = counts.get(item.variant)
        expected = _expected(item)
        if count is None or expected is None or count in seen:
            continue
        seen[count] = (expected, item.finding.outcome)
    return [(count, *seen[count]) for count in sorted(seen)]


def wall_free_finding(
    snapshot: RuleSnapshot,
    operands: Mapping[str, VerdictOperand],
    parameters: Mapping[str, ResolvedParameter] | None,
    *,
    discriminators: Mapping[str, str],
    waiting: str,
) -> Finding:
    """The width check for every wall case, combined: FAIL or PASS only when every case agrees.

    `waiting` is the row's own wall question ("Choose the wall layout for this countertop row."),
    kept as the reason when the walls are still needed.
    """
    outcomes = execute_every_variant(snapshot, operands, parameters, discriminators=discriminators)
    outcome = unanimous_outcome(outcomes)
    vendor_operand = operands.get("countertop_width")
    vendor = (
        vendor_operand.value.exact
        if vendor_operand is not None and isinstance(vendor_operand.value, Measurement)
        else None
    )
    cases = _by_count(snapshot, outcomes)
    wrote = "" if vendor is None else f"; the vendor wrote {_inches(vendor)}"
    if outcome is Outcome.FAIL and cases:
        expected = list(dict.fromkeys(_inches(value) for _count, value, _ in cases))
        reason = (
            f"Wrong whatever the walls: it should be {_listed(expected)} depending on the walls"
            f"{wrote}."
        )
        return combined_finding(
            snapshot, outcomes, reason=reason, notes=(f"Wall layout: {NOT_NEEDED_FAIL_LINE}.",)
        )
    if outcome is Outcome.PASS and cases:
        expected = list(dict.fromkeys(_inches(value) for _count, value, _ in cases))
        reason = (
            f"Right whatever the walls: it should be {_listed(expected)} in every wall case"
            f"{wrote}."
        )
        return combined_finding(
            snapshot, outcomes, reason=reason, notes=(f"Wall layout: {NOT_NEEDED_PASS_LINE}.",)
        )
    right = [
        f"{_COUNT_WORDS.get(count, f'{count} wall ends')} ({_inches(value)})"
        for count, value, result in cases
        if result is Outcome.PASS
    ]
    wrong = [
        f"{_COUNT_WORDS.get(count, f'{count} wall ends')} ({_inches(value)})"
        for count, value, result in cases
        if result is Outcome.FAIL
    ]
    summary = ""
    if right and wrong and vendor is not None:
        summary = (
            f" The vendor wrote {_inches(vendor)}: right with {_listed(right)}; wrong with "
            f"{_listed(wrong)}."
        )
    return combined_finding(
        snapshot, outcomes, reason=f"{waiting.rstrip()}{summary}".strip() or waiting
    )


# ---------------------------------------------------------------------------------------------
# Reading the cases back from a stored finding
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredWallCase:
    """One wall case of a stored every-case finding, exactly as the engine recorded it."""

    layout: str
    outcome: Outcome
    expected: Fraction | None
    field_cut_count: int | None
    field_cut_per_end: Fraction | None
    field_cut_total: Fraction | None
    row_width: Fraction | None


_RESULT: Final = re.compile(r"(?:^|, )result, (.*?)(?:, rendering,|$)")
_INPUTS: Final = re.compile(r"(?:^|, )inputs, (.*?), result, ")


def _parsed(text: str | None) -> Fraction | None:
    if text is None:
        return None
    try:
        return parse_imperial(text.removesuffix(" in").removesuffix('"'))
    except (ValueError, TypeError):
        return None


def scale_inputs(raw: object) -> tuple[Fraction | None, int | None]:
    """The field cut per end and the count from a stored `scale` derivation, whichever order its
    inputs were written in (`value, 1 in, multiplier, 2`)."""
    if not isinstance(raw, str):
        return None, None
    match = _INPUTS.search(raw)
    if match is None:
        return None, None
    parts = match.group(1).split(", ")
    named = dict(zip(parts[0::2], parts[1::2], strict=False))
    per_end = _parsed(named.get("value"))
    count = _parsed(named.get("multiplier"))
    return per_end, (count.numerator if count is not None and count.denominator == 1 else None)


def _stored_result(raw: object) -> Fraction | None:
    if not isinstance(raw, str):
        return None
    match = _RESULT.search(raw)
    return _parsed(match.group(1) if match else raw)


def is_every_wall_case(trace: Mapping[str, Any] | None) -> bool:
    """Whether a stored trace is the width check run for every wall case (#1202)."""
    operation = None if trace is None else trace.get("operation")
    return isinstance(operation, str) and operation.endswith(EVERY_WALL_CASE_OPERATION_SUFFIX)


def stored_wall_cases(trace: Mapping[str, Any] | None) -> tuple[StoredWallCase, ...]:
    """Every case of a stored every-case trace, in the rule's order; empty for any other trace."""
    if trace is None or not is_every_wall_case(trace):
        return ()
    entries: dict[str, object] = {}
    order: list[str] = []
    for pair in trace.get("intermediates", ()) or ():
        if not isinstance(pair, (list, tuple)) or len(pair) != 2 or not isinstance(pair[0], str):
            continue
        name, value = pair
        entries[name] = value
        if name.endswith(".outcome"):
            order.append(name.removesuffix(".outcome"))
    cases: list[StoredWallCase] = []
    for layout in order:
        try:
            outcome = Outcome(str(entries[f"{layout}.outcome"]))
        except ValueError:
            continue
        total_raw = entries.get(f"{layout}.field_cut_total")
        per_end, count = scale_inputs(total_raw)
        cases.append(
            StoredWallCase(
                layout=layout,
                outcome=outcome,
                expected=_stored_result(entries.get(f"{layout}.expected_width")),
                field_cut_count=count,
                field_cut_per_end=per_end,
                field_cut_total=_stored_result(total_raw),
                row_width=_stored_result(entries.get(f"{layout}.row_width")),
            )
        )
    return tuple(cases)
