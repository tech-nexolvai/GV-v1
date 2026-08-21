"""Package-level call and token budget checks.

Source: ``docs/DESIGN_CONTROLS.md`` section 5 and issue #264.
Verification: this file.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from app.budget.ceiling import (
    Ceiling,
    CeilingLayer,
    CeilingState,
    consume,
    resolve_ceiling,
)

PROJECT = UUID("00000000-0000-0000-0000-000000000101")
RUN = UUID("00000000-0000-0000-0000-000000000201")
REVISION = UUID("00000000-0000-0000-0000-000000000301")


class _Result:
    def __init__(self, calls: int, tokens: int) -> None:
        self._row = (calls, tokens)

    def one(self) -> tuple[int, int]:
        return self._row


class _Session:
    """Small query seam: persisted totals are what survive a process restart."""

    def __init__(self, calls: int, tokens: int) -> None:
        self.calls = calls
        self.tokens = tokens
        self.statements: list[object] = []

    def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return _Result(self.calls, self.tokens)


def _consume(session: _Session, **changes: Any):  # type: ignore[no-untyped-def]
    values: dict[str, object] = {
        "ceiling": Ceiling(10, 1_000),
        "calls": 1,
        "tokens": 100,
        "approaching_at": Decimal("0.8"),
    }
    values.update(changes)
    return consume(session, REVISION, **values)  # type: ignore[arg-type]


def test_run_override_wins_over_project_and_global() -> None:
    """Input: all three layers. Output: RUN ceiling. Why: A7 precedence is last-wins."""

    resolved = resolve_ceiling(
        PROJECT,
        RUN,
        global_ceiling=Ceiling(100, 100_000),
        project_ceilings={PROJECT: Ceiling(50, 50_000)},
        run_ceilings={RUN: Ceiling(5, 5_000)},
    )

    assert resolved.ceiling == Ceiling(5, 5_000)
    assert resolved.layer is CeilingLayer.RUN


def test_project_override_wins_when_run_has_none() -> None:
    """Input: GLOBAL and matching PROJECT. Output: PROJECT ceiling."""

    resolved = resolve_ceiling(
        PROJECT,
        RUN,
        global_ceiling=Ceiling(100, 100_000),
        project_ceilings={PROJECT: Ceiling(50, 50_000)},
        run_ceilings={},
    )

    assert resolved.layer is CeilingLayer.PROJECT
    assert resolved.ceiling.max_model_calls == 50


def test_global_is_used_without_matching_override() -> None:
    """Input: no matching override. Output: explicit GLOBAL configuration, never a guessed value."""

    resolved = resolve_ceiling(
        PROJECT,
        RUN,
        global_ceiling=Ceiling(100, 100_000),
        project_ceilings={},
        run_ceilings={},
    )

    assert resolved.layer is CeilingLayer.GLOBAL


def test_usage_is_package_scoped_through_the_full_run_chain() -> None:
    """Input: package revision. Output: query joins every region's invocation to that revision."""

    session = _Session(3, 240)
    result = _consume(session)
    sql = str(session.statements[0])

    assert result.usage.model_calls == 3
    assert result.usage.tokens == 240
    assert "workflow_runs.package_revision_id" in sql
    assert "model_invocations" in sql
    assert "extraction_runs" in sql
    assert "task_runs" in sql


def test_a_new_process_reads_the_same_durable_totals() -> None:
    """Input: two fresh sessions over the same rows. Output: identical spend after restart."""

    before_restart = _consume(_Session(4, 400))
    after_restart = _consume(_Session(4, 400))

    assert after_restart.usage == before_restart.usage
    assert after_restart.calls_remaining == before_restart.calls_remaining


def test_approaching_is_visible_before_exhaustion() -> None:
    """Input: proposed eighth of ten calls. Output: APPROACHING and still permitted."""

    result = _consume(_Session(7, 100))

    assert result.permitted
    assert result.state is CeilingState.APPROACHING
    assert result.calls_remaining == 2


def test_exact_ceiling_is_exhausted_but_the_final_fitting_call_is_permitted() -> None:
    """Input: proposed spend reaches the exact bounds. Output: permitted, with zero remaining."""

    result = _consume(_Session(9, 900))

    assert result.permitted
    assert result.state is CeilingState.EXHAUSTED
    assert result.calls_remaining == 0
    assert result.tokens_remaining == 0


def test_proposed_spend_beyond_either_ceiling_is_refused() -> None:
    """Input: one token beyond the bound. Output: not permitted; #265 decides the outcome."""

    result = _consume(_Session(1, 950), tokens=51)

    assert not result.permitted
    assert result.state is CeilingState.EXHAUSTED


@pytest.mark.parametrize(
    "value",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-0.1"), Decimal(0), Decimal("1.1")],
)
def test_approaching_threshold_refuses_unsafe_values(value: Decimal) -> None:
    """Input: non-finite/out-of-range ratio. Output: loud rejection, never poisoned comparison."""

    with pytest.raises(ValueError, match="finite Decimal"):
        _consume(_Session(0, 0), approaching_at=value)


@pytest.mark.parametrize("field", ["max_model_calls", "max_tokens"])
@pytest.mark.parametrize("value", [True, 0, -1])
def test_ceiling_requires_positive_exact_integers(field: str, value: object) -> None:
    """Input: boolean/zero/negative ceiling. Output: rejected before budget arithmetic."""

    values = {"max_model_calls": 10, "max_tokens": 1_000, field: value}
    with pytest.raises(ValueError, match="positive integer"):
        Ceiling(**values)  # type: ignore[arg-type]
