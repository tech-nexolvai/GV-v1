"""Writing a verdict's trace into the column a reviewer reads it back from.

`app/api/finding_chain.py` has read `findings.trace` since the chain endpoint was built. Nothing has
ever written one — the shapes it recognises were inferred from the design and from what
`verdict/trace.py` produces, and this is the first code to actually put a trace in the column. That
makes agreeing with the reader the whole job, so `classify_trace` is the specification here rather
than a downstream consumer.

**Two shapes, discriminated by a field only one of them has.** A calculation names an `operation`; an
abstention names a `cause`. The reader dispatches on presence (`finding_chain.py:165`), so writing a
key that belongs to the other shape would not fail — it would silently produce the wrong reading.

**Neither shape carries `kind`.** The reader adds it on the way out. A stored `kind` would survive
into `OpaqueTraceOut` if the other keys ever changed, which is the one outcome that looks like data
and is not.

**Every value is a string.** `4920/127` and `38 3/4` and `0.5` must all round-trip exactly, and JSON
numbers cannot carry the first two — a float would put binary rounding into the record of an exact
comparison, which is the failure ADR-0001 exists to prevent. `tests/api/test_finding_trace_shape.py`
pins this from the reading side.

**Abstentions get a trace even though they have none.** `verdict/finding.py` says an abstention
carries no `CalculationTrace`, because nothing was calculated — and `findings.trace` is NOT NULL. The
honest resolution is not `{}`: an empty object reads as `OpaqueTraceOut` and tells a reviewer nothing
about why the check declined. So an abstention is written in the shape the reader already has for
exactly this, naming the cause and saying it in plain English.
"""

from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from typing import Any

from units.imperial import format_inches
from units.measurement import Measurement
from verdict.outcomes import Outcome
from verdict.trace import CalculationTrace

__all__ = ["abstention_trace", "calculation_trace", "render_value"]


def render_value(value: object) -> str:
    """One operand value, as exact text.

    `OperandValue` is a union of `Measurement`, `Fraction`, `str`, a tuple of measurements and
    `None`, and each has to come out as something a reviewer can compare with the drawing. A
    measurement renders the way a drawing writes it — `38 3/4`, not `155/4` — because the reviewer is
    checking one against the other by eye.

    A tuple renders as its members joined, so a `many` operand shows what it summed rather than a
    length. Seeing `24, 24, 30` is what lets somebody notice a cabinet missing from the run.
    """
    if value is None:
        return ""
    if isinstance(value, Measurement):
        return f"{format_inches(value.exact)} {value.unit.value}"
    if isinstance(value, Fraction):
        return format_inches(value)
    if isinstance(value, tuple):
        return ", ".join(render_value(member) for member in value)
    return str(value)


def calculation_trace(trace: CalculationTrace, *, outcome: Outcome) -> dict[str, Any]:
    """The engine's own record of a decision, as JSON the chain endpoint reads back.

    `outcome` is passed in rather than taken from `trace.outcome`. They are the same value today, and
    the finding is the thing that was persisted — sourcing the stored outcome from the finding keeps
    the column and the trace from being able to disagree about what was decided.
    """
    return {
        "operation": trace.operation,
        "operands": [
            {
                "name": operand.name,
                "value": render_value(operand.value),
                "source": operand.source,
                "evidence_ref": operand.evidence_ref,
            }
            for operand in trace.operands
        ],
        # Pairs, because the reader unpacks them as `(name, value)`. A mapping would read as a pair
        # of one-element lists and lose the order the arithmetic happened in.
        "intermediates": [[name, render_value(value)] for name, value in trace.intermediates],
        "comparison": trace.comparison,
        # Always `None` under exact match (Q2). Kept as a key rather than omitted so a future
        # tolerance has somewhere to go that the reader already understands.
        "tolerance": None if trace.tolerance is None else render_value(trace.tolerance),
        "arithmetic_unit": None if trace.arithmetic_unit is None else trace.arithmetic_unit.value,
        "outcome": outcome.value,
        "engine_version": trace.engine_version,
        "operation_version": trace.operation_version,
    }


def abstention_trace(outcome: Outcome, *, cause: str, reason: str) -> dict[str, Any]:
    """The record of a check that declined to decide.

    An abstention is a result, not a gap — `NOT_FOUND` and `REVIEW_REQUIRED` are outcomes in their own
    right — so it deserves a stored reason a reviewer can act on. "No dimension was read for
    `cutout_width`" sends somebody to the drawing; an empty trace sends them to us.

    `regions_done` and `review_complete` are the fields `app/budget/overflow.py` fills when a run
    stopped early, and they are left absent here rather than set to `False`: this check did not stop
    early, it never had an input, and `False` would be an assertion about a run that did not happen.
    """
    return {"cause": cause, "reason": reason, "outcome": outcome.value}


def missing_operand_reason(missing: Mapping[str, str]) -> str:
    """Plain English for the commonest abstention: nothing was read for these inputs.

    Names the operands, because "an input was missing" is not actionable and "no dimension was read
    for `cutout_width`" is. Sorted so two runs of the same package produce the same sentence — a
    reason that reshuffles between runs looks like a change in the finding.
    """
    if not missing:
        return "No input was available for this check."
    named = ", ".join(f"{name} ({role})" for name, role in sorted(missing.items()))
    return f"No dimension was read for: {named}."
