from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from workflow.stages import _remaining_claude_budget, _whole_page_fallback_enabled


def test_grounded_claude_route_does_not_fall_back_to_whole_page_reader() -> None:
    assert _whole_page_fallback_enabled(SimpleNamespace(question_packets=True)) is False


def test_existing_slot_reader_and_disabled_route_keep_the_existing_fallback() -> None:
    assert _whole_page_fallback_enabled(SimpleNamespace(question_packets=False)) is True
    assert _whole_page_fallback_enabled(None) is True


def test_claude_budget_uses_remaining_set_spend_and_refuses_unknown_prior_cost() -> None:
    assert _remaining_claude_budget(
        Decimal(2), cap_micros=3_000_000, spent_micros=1_250_000, unpriced_calls=0
    ) == Decimal("1.75")
    assert _remaining_claude_budget(
        Decimal(2), cap_micros=3_000_000, spent_micros=0, unpriced_calls=1
    ) == Decimal(0)
