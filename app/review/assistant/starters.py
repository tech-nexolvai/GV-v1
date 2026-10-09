"""Up to four starter questions, chosen from the review's state (#1128). Deterministic.

Each starter is a question the records-only answers in `records_only` can answer without a model,
so the starters work when the assistant is off as well.
"""

from __future__ import annotations

from typing import Final

from app.review.assistant.records import ReviewSnapshot

__all__ = ["MAX_STARTERS", "starters"]

MAX_STARTERS: Final = 4

LEFT_BEFORE_SIGN_OFF: Final = "What is left before sign-off?"
NO_COUNTERTOP_PAGES: Final = "Which pages have no countertop?"
ROWS_NOT_CHECKED: Final = "Which countertops were not checked?"


def _first_fail_page(snapshot: ReviewSnapshot) -> int | None:
    for countertop in snapshot.countertops:
        if countertop.outcome == "FAIL":
            return countertop.page_number
    for finding in snapshot.other_checks:
        if finding.outcome == "FAIL" and finding.pages:
            return finding.pages[0]
    return None


def _first_held_page(snapshot: ReviewSnapshot) -> int | None:
    for countertop in snapshot.countertops:
        if countertop.needs_you and countertop.outcome != "FAIL":
            return countertop.page_number
    for finding in snapshot.other_checks:
        if finding.needs_you and finding.outcome != "FAIL" and finding.pages:
            return finding.pages[0]
    return None


def starters(snapshot: ReviewSnapshot) -> tuple[str, ...]:
    """The first fail, what blocks sign-off, the pages with no countertop, the first hold."""
    chosen: list[str] = []
    failed = _first_fail_page(snapshot)
    if failed is not None:
        chosen.append(f"Why did page {failed} fail?")
    if snapshot.readiness.needing_you or not snapshot.readiness.can_sign_off:
        chosen.append(LEFT_BEFORE_SIGN_OFF)
    if snapshot.pages_without_countertop:
        chosen.append(NO_COUNTERTOP_PAGES)
    held = _first_held_page(snapshot)
    if held is not None:
        chosen.append(f"Why does page {held} need me?")
    if snapshot.rows_not_checked:
        chosen.append(ROWS_NOT_CHECKED)
    return tuple(chosen[:MAX_STARTERS])
