"""What the AI readers may spend on one drawing set: a budget that grows with the pages read (#1206).

**Why it scales.** The old limits were fixed amounts: $3 for a drawing set (`#757`, when the readers
cost about $1.60 per 1,000 labels) and $2.50 for the Claude readers (at most $5). The Claude pair
costs about $0.088 a page on countertops, so a 60-page set needs about $5.25 and was refused part
way through. A limit that does not grow with the set either stops big sets or lets small ones run
away. So the budget is:

    $0.15 for every page sent to the AI readers, plus $1 for the calls still in flight,
    never below $3 and never above the admin's maximum ($25 unless the admin says otherwise).

$0.15 a page is about 1.7 times the measured cost, so ordinary work never reaches it; it still
stops a runaway (a bad loop, a huge file, a mispriced model). A 16-page set gets $3.40, a 60-page
set $10. These four figures are the defaults Claude proposed (decision log, 2026-10-13); every one
is a setting the admin can change (`GV_AI_BUDGET_PER_PAGE_USD`, `GV_AI_BUDGET_MARGIN_USD`,
`GV_AI_BUDGET_MINIMUM_USD`, `GV_AI_BUDGET_MAXIMUM_USD`, see `.env.example`).

**Where it applies.** The drawing set's meter (`workflow/stages._SpendMeter`) grows by the per-page
amount for each page an AI reader is sent, and the Claude readers' cap for one reading is this
budget for the pages that reading sends, never more than what the set has left.

Pure: no I/O. Verification: `tests/workflow/test_ai_budget.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

__all__ = [
    "DEFAULT_MARGIN_USD",
    "DEFAULT_MAXIMUM_USD",
    "DEFAULT_MINIMUM_USD",
    "DEFAULT_PER_PAGE_USD",
    "AiBudget",
    "ai_budget_from_settings",
]

#: For every page sent to the AI readers (#1206): about 1.7 times the measured $0.088 a page.
DEFAULT_PER_PAGE_USD: Final = Decimal("0.15")
#: Room for the calls in flight at the moment the budget is reached: eight at once, each reserved
#: at its worst case before it is sent (`extraction/slot_reader/anthropic.BatchSpendGuard`).
DEFAULT_MARGIN_USD: Final = Decimal("1.00")
#: The smallest budget, so a short set is never stopped by a handful of retried questions.
DEFAULT_MINIMUM_USD: Final = Decimal("3.00")
#: The admin's maximum for one drawing set, however many pages it has.
DEFAULT_MAXIMUM_USD: Final = Decimal("25.00")


@dataclass(frozen=True, slots=True)
class AiBudget:
    """The four figures of the page-scaled budget, all in US dollars."""

    per_page_usd: Decimal = DEFAULT_PER_PAGE_USD
    margin_usd: Decimal = DEFAULT_MARGIN_USD
    minimum_usd: Decimal = DEFAULT_MINIMUM_USD
    maximum_usd: Decimal = DEFAULT_MAXIMUM_USD

    def __post_init__(self) -> None:
        for name in ("per_page_usd", "margin_usd", "minimum_usd", "maximum_usd"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"the AI budget's {name} must be an amount in dollars")
        if self.per_page_usd <= 0:
            raise ValueError("the AI budget per page must be more than zero dollars")
        if self.margin_usd < 0:
            raise ValueError("the AI budget's margin for calls in flight cannot be negative")
        if self.minimum_usd <= 0:
            raise ValueError("the AI budget's minimum must be more than zero dollars")
        if self.maximum_usd < self.minimum_usd:
            raise ValueError("the AI budget's maximum must not be below its minimum")

    def for_pages(self, pages: int) -> Decimal:
        """The budget for `pages` pages sent to the AI readers: per page plus the margin, kept
        between the minimum and the maximum."""
        if isinstance(pages, bool) or not isinstance(pages, int) or pages < 0:
            raise ValueError("a page count is a whole number, zero or more")
        scaled = self.per_page_usd * pages + self.margin_usd
        return min(max(scaled, self.minimum_usd), self.maximum_usd)


def ai_budget_from_settings(settings: object) -> AiBudget:
    """The budget as `app.config.Settings` states it; the defaults where a field is absent (a
    test's stand-in settings object)."""
    return AiBudget(
        per_page_usd=Decimal(getattr(settings, "ai_budget_per_page_usd", DEFAULT_PER_PAGE_USD)),
        margin_usd=Decimal(getattr(settings, "ai_budget_margin_usd", DEFAULT_MARGIN_USD)),
        minimum_usd=Decimal(getattr(settings, "ai_budget_minimum_usd", DEFAULT_MINIMUM_USD)),
        maximum_usd=Decimal(getattr(settings, "ai_budget_maximum_usd", DEFAULT_MAXIMUM_USD)),
    )
