"""Starter questions (#1128): chosen from the review's state, at most four, deterministic."""

from __future__ import annotations

from app.review.assistant.starters import starters
from tests.review.assistant.synthetic import (
    Inputs,
    Readiness,
    countertop_results,
    empty_snapshot,
    snapshot,
)


def test_a_review_with_everything_gets_the_four_starters_in_order() -> None:
    assert starters(snapshot()) == (
        "Why did page 4 fail?",
        "What is left before sign-off?",
        "Which pages have no countertop?",
        "Why does page 7 need me?",
    )


def test_a_review_nothing_has_checked_asks_what_is_left() -> None:
    assert starters(empty_snapshot()) == ("What is left before sign-off?",)


def test_a_review_ready_to_sign_off_offers_only_what_its_records_hold() -> None:
    base = countertop_results()
    passed_only = base.model_copy(update={"items": (base.items[2],)})
    ready = snapshot(
        Inputs(
            countertops=passed_only,
            readiness=Readiness(
                can_approve=True, blocking_findings=0, blocking_finding_ids=(), reason=None
            ),
            findings=(),
        )
    )
    assert starters(ready) == (
        "Which pages have no countertop?",
        "Which countertops were not checked?",
    )


def test_starters_are_the_same_for_the_same_records() -> None:
    assert starters(snapshot()) == starters(snapshot())
