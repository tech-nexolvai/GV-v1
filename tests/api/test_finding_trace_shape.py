"""The typed calculation trace, and the two shapes that actually go into that column.

`FindingChain.trace` was `dict[str, Any]`, which is the one place the frontend's generated types
could not check anything: a change to the trace's shape surfaced as an empty panel rather than a
compile error.

Typing it needed a decision rather than a transcription, because **two unrelated shapes are stored
there**. `verdict/engine.py` builds a `CalculationTrace` — operation, operands, comparison — and
`app/budget/overflow.py` writes an abstention record with a cause and a reason and no arithmetic at
all. Reporting the second as a degenerate version of the first would render an abstention as a check
that ran and found nothing, which is the reading V1 must never invite.

So the wire type is a union discriminated on a `kind` this API adds when it reads the row. The stored
JSON carries no tag; classifying on read means no migration and no rewriting of existing rows.
"""

from __future__ import annotations

import pytest

from app.api.finding_chain import (
    AbstentionTraceOut,
    CalculationTraceOut,
    OpaqueTraceOut,
    classify_trace,
)

CALCULATION = {
    "operation": "equals",
    "operands": [
        {"name": "countertop_width", "value": "96", "source": "SHOP", "evidence_ref": "obs-1"},
        {"name": "cabinet_run_total", "value": "94 1/2", "source": "SHOP", "evidence_ref": None},
    ],
    "intermediates": [["run_plus_fillers", "98 1/2"]],
    "comparison": "96 == 98 1/2",
    "tolerance": None,
    "arithmetic_unit": "inch",
    "outcome": "FAIL",
    "engine_version": "verdict-1.2.3",
    "operation_version": "1.0.0",
}

ABSTENTION = {
    "cause": "package_model_budget_exhausted",
    "reason": "the per-package token ceiling was reached",
    "regions_done": 3,
    "review_complete": False,
}


def test_a_calculation_is_read_as_a_calculation() -> None:
    trace = classify_trace(CALCULATION)

    assert isinstance(trace, CalculationTraceOut)
    assert trace.kind == "calculation"
    assert trace.operation == "equals"
    assert trace.comparison == "96 == 98 1/2"
    assert [operand.name for operand in trace.operands] == [
        "countertop_width",
        "cabinet_run_total",
    ]


def test_operand_values_stay_text() -> None:
    """**The arithmetic rule, carried to the wire.**

    Operand values are exact rationals. A JSON number would be turned into binary floating point by
    most clients, and under exact match that is not a rounding error — with no tolerance band to
    absorb it, a shifted value is a different verdict.
    """
    trace = classify_trace(CALCULATION)
    assert isinstance(trace, CalculationTraceOut)

    for operand in trace.operands:
        assert isinstance(operand.value, str)
    assert trace.operands[1].value == "94 1/2", "a fraction must survive as written"


def test_an_abstention_is_not_reported_as_an_empty_calculation() -> None:
    """**The distinction this union exists for.** An abstention rendered as a calculation with no
    operands reads as "the check ran and found nothing" — which is the false-clean sentence the whole
    posture is built to prevent."""
    trace = classify_trace(ABSTENTION)

    assert isinstance(trace, AbstentionTraceOut)
    assert trace.kind == "abstention"
    assert trace.cause == "package_model_budget_exhausted"
    assert trace.reason
    assert not isinstance(trace, CalculationTraceOut)


def test_an_unrecognised_trace_is_handed_over_intact() -> None:
    """Not dropped and not bent to fit. A recompute is compared against what the engine recorded, so
    losing a shape nobody has taught this endpoint about would lose the only copy — and coercing it
    into whichever model is closest would be read as the engine's own record when it is not."""
    stored = {"engine": "something-new", "steps": [1, 2, 3]}

    trace = classify_trace(stored)

    assert isinstance(trace, OpaqueTraceOut)
    assert trace.kind == "unrecognised"
    assert trace.content == stored, "the content survives so nothing is lost"


def test_a_tolerance_is_absent_in_v1() -> None:
    """Raj settled on exact match with no band, so there is nothing to record. The field stays
    because graded tolerances are deferred past iteration 1 rather than ruled out."""
    trace = classify_trace(CALCULATION)
    assert isinstance(trace, CalculationTraceOut)
    assert trace.tolerance is None


@pytest.mark.parametrize("missing", ["operands", "intermediates", "comparison", "outcome"])
def test_a_partial_calculation_still_reads_as_one(missing: str) -> None:
    """An older row written before a field existed must not fall through to `unrecognised`. The
    discriminator is `operation`, and a trace that names one is a calculation whatever else it
    lacks."""
    stored = {key: value for key, value in CALCULATION.items() if key != missing}

    assert isinstance(classify_trace(stored), CalculationTraceOut)


def test_the_two_shapes_cannot_be_confused() -> None:
    """The tags are what the frontend switches on, so they have to be exclusive."""
    assert classify_trace(CALCULATION).kind != classify_trace(ABSTENTION).kind
