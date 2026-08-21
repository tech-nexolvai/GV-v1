"""Budget exhaustion fails closed and remains understandable to a reviewer.

Source: ``docs/DESIGN_CONTROLS.md`` section 5 and issue #265.
Verification: this file.
"""

from __future__ import annotations

from uuid import UUID

import pytest

import app.budget.overflow as overflow_module
from app.budget.overflow import BUDGET_EXHAUSTED_REASON, on_overflow
from app.models.package import PackageState, PackageStateEvent
from verdict.outcomes import Outcome

REVISION = UUID("00000000-0000-0000-0000-000000000401")
FINDING_A = UUID("00000000-0000-0000-0000-000000000501")
FINDING_B = UUID("00000000-0000-0000-0000-000000000502")


class _Session:
    pass


def _event(reason: str) -> PackageStateEvent:
    return PackageStateEvent(
        package_revision_id=REVISION,
        sequence=1,
        from_state=PackageState.EXTRACTING.value,
        to_state=PackageState.NEEDS_INPUT.value,
        actor="budget-worker",
        reason=reason,
    )


@pytest.fixture
def transition_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_enter(
        session: object,
        package_revision_id: UUID,
        *,
        actor: str,
        needed: str,
    ) -> PackageStateEvent:
        calls.append(
            {
                "session": session,
                "package_revision_id": package_revision_id,
                "actor": actor,
                "needed": needed,
            }
        )
        return _event(needed)

    monkeypatch.setattr(overflow_module, "enter_needs_input", fake_enter)
    return calls


def test_every_affected_finding_is_review_required_and_never_pass(
    transition_calls: list[dict[str, object]],
) -> None:
    """Input: two unfinished checks. Output: two REVIEW REQUIRED dispositions; no PASS path."""

    result = on_overflow(
        _Session(),  # type: ignore[arg-type]
        REVISION,
        regions_done=12,
        affected_finding_ids=(FINDING_A, FINDING_B),
        actor="budget-worker",
    )

    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert {item.finding_id for item in result.affected_findings} == {FINDING_A, FINDING_B}
    assert all(item.outcome is Outcome.REVIEW_REQUIRED for item in result.affected_findings)
    assert all(item.outcome is not Outcome.PASS for item in result.affected_findings)
    assert len(transition_calls) == 1


def test_trace_names_budget_exhaustion_not_drawing_ambiguity(
    transition_calls: list[dict[str, object]],
) -> None:
    """Input: exhaustion after seven regions. Output: exact budget reason in every trace."""

    result = on_overflow(
        _Session(),  # type: ignore[arg-type]
        REVISION,
        regions_done=7,
        affected_finding_ids=(FINDING_A,),
        actor="budget-worker",
    )

    expected = "budget exhausted after 7 regions"
    assert result.reason == expected
    assert result.affected_findings[0].trace["reason"] == expected
    assert result.affected_findings[0].trace["cause"] == "package_model_budget_exhausted"
    assert "ambiguous" not in result.reason
    assert BUDGET_EXHAUSTED_REASON.format(regions_done=7) == expected


def test_partial_evidence_is_retained_but_review_is_not_complete(
    transition_calls: list[dict[str, object]],
) -> None:
    """Input: some regions completed. Output: evidence retained, agent halted, never complete."""

    result = on_overflow(
        _Session(),  # type: ignore[arg-type]
        REVISION,
        regions_done=3,
        affected_finding_ids=(FINDING_A,),
        actor="budget-worker",
    )

    assert result.partial_evidence_retained
    assert result.agent_halted
    assert not result.review_complete
    assert result.affected_findings[0].trace["review_complete"] is False


def test_package_enters_an_actionable_state_with_plain_english_direction(
    transition_calls: list[dict[str, object]],
) -> None:
    """Input: overflow. Output: one NEEDS_INPUT transition explaining reviewer choices."""

    result = on_overflow(
        _Session(),  # type: ignore[arg-type]
        REVISION,
        regions_done=4,
        affected_finding_ids=(),
        actor="budget-worker",
    )

    assert result.state_event.to_state == PackageState.NEEDS_INPUT.value
    assert transition_calls[0]["package_revision_id"] == REVISION
    assert "retained partial evidence" in str(transition_calls[0]["needed"])
    assert "additional model budget" in str(transition_calls[0]["needed"])


@pytest.mark.parametrize("regions_done", [True, -1, 1.5])
def test_invalid_region_counts_are_refused_before_state_changes(
    regions_done: object,
    transition_calls: list[dict[str, object]],
) -> None:
    """Input: unsafe count. Output: rejection before any lifecycle mutation."""

    with pytest.raises(ValueError, match="non-negative integer"):
        on_overflow(
            _Session(),  # type: ignore[arg-type]
            REVISION,
            regions_done=regions_done,  # type: ignore[arg-type]
            affected_finding_ids=(),
            actor="budget-worker",
        )
    assert not transition_calls


def test_duplicate_finding_ids_are_refused(
    transition_calls: list[dict[str, object]],
) -> None:
    """Input: one finding named twice. Output: refusal, not two competing dispositions."""

    with pytest.raises(ValueError, match="duplicates"):
        on_overflow(
            _Session(),  # type: ignore[arg-type]
            REVISION,
            regions_done=1,
            affected_finding_ids=(FINDING_A, FINDING_A),
            actor="budget-worker",
        )
    assert not transition_calls
