"""Route and purpose of a recorded call, from its model id and prompt id only (#1165)."""

from __future__ import annotations

import pytest

from app.usage_history import charged_cost_micros, infer_purpose, infer_route


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
    ("model", "prompt", "purpose"),
    [
        ("anthropic.claude-opus-5-5", "claude-slot-span-v2", "reading"),
        ("anthropic.claude-opus-5-5", "claude-counter-break-v2", "reading"),
        ("anthropic.claude-opus-5-5", "slot-walls-v1", "reading"),
        ("anthropic.claude-opus-5-5", "arch-pair-v1", "reading"),
        ("qwen.qwen3-vl-235b-a22b", "form-reader-private-abc+product=countertop", "reading"),
        ("anthropic.claude-opus-5-5", "slot-row-choice-v2", "row-choice"),
        ("anthropic.claude-sonnet-5-5", "review-assistant-v2", "assistant"),
        ("us.amazon.nova-pro-v1:0", "reviewer-chat-v2", "chat"),
        ("us.amazon.nova-2-lite-v1:0", "dimension-reader-v1", "reading"),
        ("mistral.mistral-large-3-675b-instruct", "dimension-reader-v1", "bake-off"),
        ("us.amazon.nova-2-lite-v1:0", "dimension-reader-teaching-v2+abc", "bake-off"),
        ("us.amazon.nova-pro-v1:0", "findings-composer-v3", "other"),
    ],
)
def test_purpose(model: str, prompt: str, purpose: str) -> None:
    assert infer_purpose(model, prompt) == purpose


def test_only_a_failed_call_with_no_tokens_is_free() -> None:
    assert charged_cost_micros(None, "failed", 0, 0) == 0
    assert charged_cost_micros(None, "failed", 1, 0) is None
    assert charged_cost_micros(None, "timeout", 0, 0) is None
    assert charged_cost_micros(None, "ok", 0, 0) is None
    assert charged_cost_micros(12, "failed", 0, 0) == 12
