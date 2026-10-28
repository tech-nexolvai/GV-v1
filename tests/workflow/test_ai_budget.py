"""What the AI readers may spend on one drawing set (#757; scaled by the pages read since #1206).

Verification for: `workflow/ai_budget.py` (the page-scaled budget), the budget in
`workflow/stages.py` — `ai_budget_from_environment`, `_SpendMeter`, the Claude reader's cap
following the set's budget. The page-level checks before every model call on the vision route and in
the reading agent went with them when the label-first route was retired (2026-10-28).

The money here is made up and stated in a price file the test writes: one call of 1,000 tokens in and
1,000 out at $2 per 1,000 costs $4, which is over a $3 cap by itself.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from workflow.ai_budget import AiBudget, ai_budget_from_settings
from workflow.stages import (
    AI_BUDGET_ENV,
    _remaining_claude_budget,
    _SpendMeter,
    ai_budget_from_environment,
)

# ---------------------------------------------------------------------------
# The page-scaled budget (#1206)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pages", "budget"),
    [
        (0, "3.00"),  # nothing sent yet: the minimum
        (1, "3.00"),  # $1.15 is under the minimum
        (13, "3.00"),  # $2.95: still the minimum
        (14, "3.10"),  # the first page count above it
        (16, "3.40"),  # AI Set 2's 16 pages
        (60, "10.00"),  # AI Set 3's 60 pages, which a fixed $3 / $2.50 could not read
        (160, "25.00"),  # $25.00 exactly: the maximum
        (500, "25.00"),  # never above the admin's maximum
    ],
)
def test_the_budget_is_fifteen_cents_a_page_plus_a_dollar_between_three_and_twenty_five(
    pages: int, budget: str
) -> None:
    """The defaults Claude proposed (decision log 2026-10-13): $0.15 x pages + $1, min $3, max $25."""
    assert AiBudget().for_pages(pages) == Decimal(budget)


def test_each_of_the_four_figures_is_the_admins_to_change() -> None:
    from app.config import Settings

    settings = Settings(
        database_url="postgresql+psycopg://unused@localhost/unused",
        ai_budget_per_page_usd=Decimal("0.20"),
        ai_budget_margin_usd=Decimal("0.50"),
        ai_budget_minimum_usd=Decimal("2.00"),
        ai_budget_maximum_usd=Decimal("40.00"),
    )
    budget = ai_budget_from_settings(settings)

    assert budget == AiBudget(Decimal("0.20"), Decimal("0.50"), Decimal("2.00"), Decimal("40.00"))
    assert budget.for_pages(5) == Decimal("2.00")
    assert budget.for_pages(60) == Decimal("12.50")
    assert budget.for_pages(1000) == Decimal("40.00")


def test_the_settings_defaults_are_the_formulas_defaults() -> None:
    from app.config import Settings

    settings = Settings(database_url="postgresql+psycopg://unused@localhost/unused")

    assert ai_budget_from_settings(settings) == AiBudget()
    assert settings.claude_reader_budget_usd is None


@pytest.mark.parametrize(
    "changes",
    [
        {"ai_budget_per_page_usd": Decimal(0)},
        {"ai_budget_margin_usd": Decimal(-1)},
        {"ai_budget_minimum_usd": Decimal(0)},
        {"ai_budget_minimum_usd": Decimal(30), "ai_budget_maximum_usd": Decimal(25)},
        {"claude_reader_budget_usd": Decimal("25.01")},
    ],
)
def test_a_budget_that_could_not_hold_is_refused_when_the_settings_load(
    changes: dict[str, Decimal],
) -> None:
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+psycopg://unused@localhost/unused", **changes)


def test_the_old_five_dollar_ceiling_is_gone_and_the_maximum_bounds_the_claude_ceiling() -> None:
    from app.config import Settings

    url = "postgresql+psycopg://unused@localhost/unused"
    assert Settings(database_url=url, claude_reader_budget_usd=Decimal(20)).claude_reader_budget_usd
    raised = Settings(
        database_url=url,
        ai_budget_maximum_usd=Decimal(100),
        claude_reader_budget_usd=Decimal(60),
    )
    assert raised.claude_reader_budget_usd == Decimal(60)


def test_the_claude_ceiling_is_one_function_for_the_worker_and_the_screen() -> None:
    """#1317: unstated, the admin's maximum; stated, the lower of it and the maximum. The worker's
    readers (`workflow.slot_reader`) and the "Read depths again" screen use this same function."""
    from app.config import Settings
    from workflow.ai_budget import AiBudget, ai_budget_from_settings, claude_ceiling
    from workflow.slot_reader import _claude_ceiling

    url = "postgresql+psycopg://unused@localhost/unused"
    unstated = Settings(database_url=url, ai_budget_per_set_usd=Decimal(40))
    lower = Settings(database_url=url, claude_reader_budget_usd=Decimal(5))
    for settings, expected in ((unstated, Decimal(25)), (lower, Decimal(5))):
        budget = ai_budget_from_settings(settings)
        assert claude_ceiling(settings, budget) == _claude_ceiling(settings, budget) == expected
    # A stand-in settings object stating more than the maximum is still capped at it.
    over = type("Over", (), {"claude_reader_budget_usd": Decimal(60)})()
    assert claude_ceiling(over, AiBudget()) == Decimal(25)


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


def test_a_fixed_budget_is_stated_only_when_a_deployment_states_one() -> None:
    """Unstated, the budget grows with the pages read (#1206); before, it was a fixed $3."""
    assert ai_budget_from_environment({}) is None
    assert ai_budget_from_environment({AI_BUDGET_ENV: "5.50"}) == Decimal("5.50")


@pytest.mark.parametrize("stated", ["three", "0", "-1", "NaN", "Infinity"])
def test_a_cap_that_is_not_an_amount_is_refused(stated: str) -> None:
    with pytest.raises(ValueError, match=AI_BUDGET_ENV):
        ai_budget_from_environment({AI_BUDGET_ENV: stated})


# ---------------------------------------------------------------------------
# The meter
# ---------------------------------------------------------------------------


def test_the_meter_is_reached_at_the_cap_not_after_it() -> None:
    meter = _SpendMeter(cap_micros=3_000_000)

    meter.add(2_999_999)
    assert not meter.reached
    meter.add(1)
    assert meter.reached
    assert "$3.00" in meter.reason


def test_the_meter_grows_by_each_page_sent_once() -> None:
    """#1206: the set's cap is the page-scaled budget for the pages sent to an AI reader so far."""
    meter = _SpendMeter(cap_micros=3_000_000, budget=AiBudget())

    for page in range(60):
        meter.send(page)
        meter.send(page)  # a page sent twice is one page

    assert meter.cap_micros == 10_000_000
    assert "$10.00" in meter.reason


def test_a_fixed_meter_does_not_grow() -> None:
    meter = _SpendMeter(cap_micros=3_000_000)

    for page in range(60):
        meter.send(page)

    assert meter.cap_micros == 3_000_000


def test_the_claude_cap_is_the_least_of_its_ceiling_its_pages_and_what_is_left() -> None:
    """#1206: the Claude reader's cap follows the set's budget, never above what the set has left."""
    budget = AiBudget()
    sixty_pages = budget.for_pages(60)

    assert _remaining_claude_budget(
        min(Decimal(25), sixty_pages), cap_micros=10_000_000, spent_micros=0, unpriced_calls=0
    ) == Decimal(10)
    # $4 already spent on the set: $6 is left, so the reading may spend $6.
    assert _remaining_claude_budget(
        min(Decimal(25), sixty_pages),
        cap_micros=10_000_000,
        spent_micros=4_000_000,
        unpriced_calls=0,
    ) == Decimal(6)
    # A lower Claude ceiling the admin stated wins.
    assert _remaining_claude_budget(
        min(Decimal(2), sixty_pages), cap_micros=10_000_000, spent_micros=0, unpriced_calls=0
    ) == Decimal(2)


def test_a_call_without_a_price_is_counted_not_hidden() -> None:
    """A model with no stated price has no cost to add (#700), so it is counted by number."""
    meter = _SpendMeter(cap_micros=3_000_000)

    meter.add(None)
    meter.add(None)

    assert meter.unpriced_calls == 2
    assert meter.as_payload() == {
        "cap_usd": "3",
        "spent_usd": "0",
        "calls_without_a_price": 2,
        "reached": False,
    }
