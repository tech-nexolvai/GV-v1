"""Thinking on for every judgement call; its tokens recorded (#1775, C4). $0, no model call.

A judgement ("do these two dimensions measure the same points?") is never made with thinking off:
the resolve loop, the critic and the advocate run with adaptive thinking, and the effort is lowered
instead to keep their cost. Copying a printed value (the second read) may stay without thinking.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.review.assistant import chat_read_review, finding_critic
from app.review.assistant.chat_request import MODELS, thinking_config, thinking_tokens
from app.review.assistant.simple_check import compare, resolve


def test_thinking_tokens_are_read_from_the_usage() -> None:
    assert thinking_tokens({"output_tokens_details": {"thinking_tokens": 321}}) == 321
    assert thinking_tokens({"thinking_tokens": 12}) == 12
    assert thinking_tokens({"output_tokens_details": {"thinking_tokens": 0}}) == 0
    # Not reported is not "did not think": None, so a red whose checker did not think is found
    # only where the provider said so.
    assert thinking_tokens({"output_tokens": 5}) is None
    assert thinking_tokens({"output_tokens_details": {}}) is None
    assert thinking_tokens(None) is None


def test_a_compare_usage_without_thinking_details_records_none_not_zero() -> None:
    usage: dict[str, int] = {}
    compare._merge(usage, {"output_tokens": 50, "output_tokens_details": {}})
    assert "thinking_tokens" not in usage
    compare._merge(usage, {"output_tokens": 50})
    assert "thinking_tokens" not in usage


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


def test_the_resolve_call_thinks_and_saves_its_thinking_tokens() -> None:
    from tests.review.assistant.test_simple_loop_wall import FIXTURES, PACK, _verified
    from tests.review.assistant.test_simple_resolve import BLACK, ON, _Engine, _proposer, _Reply

    fixture = next(f for f in FIXTURES if f.id == "F29-unsure-worst")
    v, item, raw = _verified(fixture)
    answer = {
        "kind": "none",
        "vendor_label": None,
        "architect_label": None,
        "labels": [],
        "vendor_from": "",
        "vendor_to": "",
        "architect_from": "",
        "architect_to": "",
        "conventions": [],
        "verdict": "unsure",
        "want": "none",
        "note": "",
    }
    for usage, saved in (
        ({"input_tokens": 9000, "output_tokens_details": {"thinking_tokens": 777}}, 777),
        ({"input_tokens": 9000}, None),
    ):
        engine = _Engine([_Reply(answer, usage=usage), _Reply(answer, usage=usage)])
        done = resolve.resolve_one(
            v,
            raw,
            item,
            PACK,
            proposer=_proposer(engine, v, raw),
            settings=ON,
            ink=lambda _l: BLACK,
        )
        call = engine.calls[0]
        assert call["thinking"]["type"] == "adaptive" and call["effort"] == "medium"
        assert done.rounds[0].usage["thinking_tokens"] == saved


def test_the_critic_is_asked_with_thinking_on(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, object]] = []

    def record(*args: object, **kwargs: object) -> object:
        seen.append(kwargs)
        return lambda content: "{}"

    monkeypatch.setattr(chat_read_review, "_model_call", record)
    engine = SimpleNamespace(
        book=SimpleNamespace(coverage_used=True),
        _whole_set_work=lambda: True,
        critic_off=False,
        provider=SimpleNamespace(name="anthropic"),
        settings=SimpleNamespace(mode="full_check"),
        check_prompt_id="review-assistant-chat-check-x",
    )
    chat_read_review.critic_jobs(engine, [], object(), lambda *a: None)  # type: ignore[arg-type]
    assert seen and seen[0]["thinking"] is True
