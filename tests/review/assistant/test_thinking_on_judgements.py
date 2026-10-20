"""Thinking on for every judgement call; its tokens recorded (#1775, C4). $0, no model call.

A judgement ("do these two dimensions measure the same points?") is never made with thinking off:
the resolve loop, the critic and the advocate run with adaptive thinking, and the effort is lowered
instead to keep their cost. Copying a printed value (the second read) may stay without thinking.
"""

from __future__ import annotations

from app.review.assistant import chat_read_review, finding_critic
from app.review.assistant.chat_request import MODELS, thinking_config, thinking_tokens
from app.review.assistant.simple_check import compare, resolve


def test_thinking_tokens_are_read_from_the_usage() -> None:
    assert thinking_tokens({"output_tokens_details": {"thinking_tokens": 321}}) == 321
    assert thinking_tokens({"thinking_tokens": 12}) == 12
    assert thinking_tokens({"output_tokens": 5}) == 0
    assert thinking_tokens(None) == 0


def test_the_compare_usage_keeps_the_thinking_tokens_of_every_call() -> None:
    usage: dict[str, int] = {}
    compare._merge(usage, {"output_tokens": 50, "output_tokens_details": {"thinking_tokens": 40}})
    total: dict[str, int] = {}
    compare._add(total, usage)
    compare._add(total, usage)
    assert total["thinking_tokens"] == 80


def test_the_resolve_loop_thinks_at_a_lower_effort() -> None:
    settings = resolve.ResolveSettings()
    assert settings.effort == "medium"
    assert resolve.ResolveSettings.of({"mode": "on"}).effort == "medium"
    # Sonnet 5.5 off would be `between_tools` (no thinking on a one-shot call): never used here.
    sonnet = MODELS["claude-sonnet-5-5"]
    assert thinking_config(sonnet, True)["type"] == "adaptive"


def test_the_critic_has_room_to_think() -> None:
    assert finding_critic.CRITIC_MAX_TOKENS >= 12_000


def test_the_read_review_call_passes_thinking_through() -> None:
    calls: list[dict[str, object]] = []

    class _Budget:
        def hold(self, amount: object) -> int:
            return 0

        def release(self, amount: object) -> None:
            return None

    class _Engine:
        provider = type("P", (), {"name": "anthropic"})()

        def _price(self, *args: object) -> int:
            return 0

        def _call(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return type("R", (), {"text": lambda self: "{}", "stop_reason": "end_turn"})()

    for thinking in (True, False):
        call = chat_read_review._model_call(
            _Engine(),
            _Budget(),  # type: ignore[arg-type]
            short=finding_critic.CRITIC_MODEL,
            effort="low",
            system="s",
            max_tokens=100,
            prompt_tokens=10,
            prompt_id="p",
            thinking=thinking,
        )
        call([{"type": "text", "text": "q"}])
    assert calls[0]["thinking"]["type"] == "adaptive"  # type: ignore[index]
    assert calls[1]["thinking"]["type"] != "adaptive"  # type: ignore[index]
