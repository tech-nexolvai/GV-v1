"""Route, purpose and charged cost of a recorded call (#1165)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.runs.rates import ModelRate, ModelRates
from app.usage_history import ChargedCost, charged_cost, infer_purpose, infer_route


@pytest.mark.parametrize(
    ("model", "prompt", "route"),
    [
        ("us.amazon.nova-pro-v1:0", "findings-composer-v3", "bedrock"),
        ("amazon.nova-lite-v1:0", "findings-composer-v3", "bedrock"),
        ("qwen.qwen3-vl-235b-a22b", "slot-crop-v1", "bedrock"),
        ("us.moonshotai.kimi-k3", "form-reader-v5", "bedrock"),
        ("mistral.ministral-3-3b-instruct", "dimension-reader-v1", "bedrock"),
        ("anthropic.claude-haiku-4-5-20251001-v1:0", "dimension-reader-v1", "bedrock"),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "dimension-reader-v1", "bedrock"),
        ("anthropic.claude-sonnet-5-5", "review-assistant-v3", "openrouter"),
        # The readers went through Anthropic's API, later OpenRouter; the row does not say which.
        ("anthropic.claude-opus-5-5", "claude-slot-span-v2", "unknown"),
        ("synthetic-model", "synthetic-prompt", "unknown"),
    ],
)
def test_route(model: str, prompt: str, route: str) -> None:
    assert infer_route(model, prompt) == route


@pytest.mark.parametrize(
    ("prompt", "purpose"),
    [
        ("claude-slot-span-v2", "reading"),
        ("claude-counter-break-v2", "reading"),
        ("slot-walls-v1", "reading"),
        ("arch-pair-v1", "reading"),
        ("form-reader-private-abc+product=countertop", "reading"),
        ("dimension-reader-v1", "reading"),
        # The production prompt of the Qwen3-VL + Nova 2 Lite pair (#907), not a comparison.
        ("dimension-reader-teaching-v2+49f87a3cc496", "reading"),
        ("slot-row-choice-v2", "row-choice"),
        ("review-assistant-v2", "assistant"),
        ("reviewer-chat-v2", "chat"),
        ("findings-composer-v3", "findings"),
        ("synthetic-prompt", "other"),
    ],
)
def test_purpose(prompt: str, purpose: str) -> None:
    assert infer_purpose(prompt) == purpose


RATES = ModelRates(
    source="synthetic",
    retrieved=date(2026, 1, 1),
    currency="USD",
    rates={"amazon.nova-pro-v1:0": ModelRate(Decimal("0.0008"), Decimal("0.0032"))},
)


def test_only_a_failed_call_with_no_tokens_is_free() -> None:
    assert charged_cost("m", None, "failed", 0, 0) == ChargedCost(0)
    assert charged_cost("m", None, "failed", 1, 0) == ChargedCost(None)
    assert charged_cost("m", None, "timeout", 0, 0) == ChargedCost(None)
    assert charged_cost("m", None, "ok", 0, 0) == ChargedCost(None)
    assert charged_cost("m", 12, "failed", 0, 0) == ChargedCost(12)
    assert charged_cost("m", 0, "failed", 0, 0) == ChargedCost(0)


def test_a_zero_with_tokens_is_priced_later_from_the_published_rates() -> None:
    # A pre-#754 row: stored as 0 whatever it used. Profile ids price as their model.
    assert charged_cost("us.amazon.nova-pro-v1:0", 0, "ok", 1000, 100, rates=RATES) == (
        ChargedCost(1120, priced_later=True)
    )
    assert charged_cost("amazon.nova-pro-v1:0", 0, "failed", 10, 0, rates=RATES) == (
        ChargedCost(8, priced_later=True)
    )


def test_a_zero_with_tokens_and_no_published_rate_is_unpriced_never_free() -> None:
    assert charged_cost("mistral.unknown", 0, "ok", 1000, 100, rates=RATES) == ChargedCost(None)


def test_the_published_file_prices_the_old_rows() -> None:
    # The repository's own price file, through the recorder's own code.
    assert charged_cost("mistral.ministral-3-3b-instruct", 0, "ok", 10_000, 1_000) == (
        ChargedCost(1100, priced_later=True)
    )
    assert charged_cost("anthropic.claude-haiku-4-5-20251001-v1:0", 0, "ok", 10, 1).micros is None
