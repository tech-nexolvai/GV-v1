"""Where a call's prompt-cache breakpoints go, and the `shared` layout (rebuild plan Step 5).

No network, no paid call: every test builds the request the code would send and reads it.

- The `legacy` layout (the default) sends byte for byte today's request: pinned against a frozen
  copy of the old request builder (`legacy_call_params.py`) over a grid of calls.
- A caller says where the breakpoints go (`CachePlan`, `cache_mark`), each 5-minute or 1-hour;
  more than 4, or a 1-hour one after a 5-minute one, is refused before anything is sent.
- A wrap-up may keep `tool_choice` as it was (`final_choice="keep"`).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.review.assistant import chat_request
from app.review.assistant.chat_request import (
    BY_SHORT,
    LEGACY_CACHE,
    SINGLE_SHOT,
    CachePlan,
    CacheRefused,
    breakpoints,
    cache_mark,
    call_params,
    schema_note,
    thinking_config,
)
from tests.review.assistant.legacy_call_params import legacy_call_params

_SHAPE = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
_TOOLS = (
    {"name": "a", "description": "A.", "input_schema": {"type": "object"}},
    {"name": "b", "description": "B.", "input_schema": {"type": "object"}, "defer_loading": True},
    {"name": "c", "description": "C.", "input_schema": {"type": "object"}},
)
_PICTURE = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
}


def _messages(turns: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [_PICTURE, {"type": "text", "text": "Compare the pages."}]}
    ]
    for turn in range(turns):
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"t{turn}", "name": "a", "input": {}}],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"t{turn}", "content": "ok"}],
            }
        )
    return messages


@pytest.fixture(autouse=True)
def _routing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        chat_request,
        "_openrouter_routing",
        lambda: {"only": ["google-vertex/global"], "allow_fallbacks": False},
    )


@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
@pytest.mark.parametrize("short", ["opus", "sonnet", "haiku"])
@pytest.mark.parametrize("final", [False, True])
@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("tools", [(), _TOOLS])
@pytest.mark.parametrize("turns", [0, 2])
def test_the_legacy_layout_sends_byte_for_byte_the_old_request(
    provider: str, short: str, final: bool, shaped: bool, tools: Any, turns: int
) -> None:
    model = BY_SHORT[short]
    given: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "effort": "medium",
        "thinking": thinking_config(model, True),
        "system": "SYSTEM",
        "tools": tools,
        "messages": _messages(turns),
        "final": final,
        "max_tokens": 4000,
        "context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]} if turns else None,
        "output_format": _SHAPE if shaped else None,
    }
    new = call_params(**given)
    old = legacy_call_params(**given)
    assert json.dumps(new, sort_keys=False) == json.dumps(old, sort_keys=False)
    # The default plan is the legacy one, said explicitly too.
    assert call_params(**given, cache=LEGACY_CACHE, final_choice="none") == old


def _call(**changes: Any) -> dict[str, Any]:
    model = BY_SHORT["sonnet"]
    given: dict[str, Any] = {
        "provider": "anthropic",
        "model": model,
        "effort": "low",
        "thinking": thinking_config(model, True),
        "system": "SYSTEM",
        "tools": _TOOLS[:1],
        "messages": _messages(1),
        "final": False,
        "max_tokens": 1000,
    }
    return call_params(**{**given, **changes})


def test_a_single_shot_has_no_breakpoint_on_its_newest_block() -> None:
    params = _call(messages=_messages(0), tools=(), cache=SINGLE_SHOT)
    last = params["messages"][-1]["content"][-1]
    assert "cache_control" not in last
    assert breakpoints(params) == ["5m"]  # the system prompt's only
    assert "cache_control" in params["system"][0]


def test_a_caller_puts_breakpoints_where_it_says_and_none_elsewhere() -> None:
    marked = _messages(1)
    marked[0]["content"][0] = cache_mark(marked[0]["content"][0])
    params = _call(messages=marked, cache=CachePlan(tools=None, system="5m", newest="5m"))
    assert "cache_control" not in params["tools"][0]
    assert params["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert breakpoints(params) == ["5m", "5m", "5m"]


def test_a_one_hour_breakpoint_says_its_lifetime_and_must_come_first() -> None:
    marked = _messages(0)
    marked[0]["content"][0] = cache_mark(marked[0]["content"][0], "1h")
    params = _call(messages=marked, cache=CachePlan(tools=None, system="1h", newest="5m"))
    assert params["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert breakpoints(params) == ["1h", "1h", "5m"]
    late = _messages(0)
    late[0]["content"][0] = cache_mark(late[0]["content"][0], "1h")
    with pytest.raises(CacheRefused, match="before every 5-minute"):
        _call(messages=late, cache=CachePlan(tools=None, system="5m", newest=None))


def test_more_than_four_breakpoints_are_refused_before_anything_is_sent() -> None:
    marked = _messages(1)
    marked[0]["content"] = [cache_mark(block) for block in marked[0]["content"]]
    marked[1]["content"] = [cache_mark(block) for block in marked[1]["content"]]
    with pytest.raises(CacheRefused, match="at most 4"):
        _call(messages=marked, cache=CachePlan())


def test_an_unknown_lifetime_is_refused() -> None:
    with pytest.raises(CacheRefused):
        cache_mark({"type": "text", "text": "x"}, "2h")  # type: ignore[arg-type]


def test_a_wrap_up_may_keep_its_tool_choice() -> None:
    assert _call(final=True)["tool_choice"] == {"type": "none"}
    assert _call(final=True, final_choice="keep")["tool_choice"] == {"type": "auto"}
    assert _call(final=False, final_choice="keep")["tool_choice"] == {"type": "auto"}


def test_on_openrouter_the_reply_shape_may_go_in_the_first_message_not_the_system() -> None:
    plain = _call(provider="openrouter", cache=SINGLE_SHOT, schema_in="first_message")
    shaped = _call(
        provider="openrouter", output_format=_SHAPE, cache=SINGLE_SHOT, schema_in="first_message"
    )
    assert shaped["system"] == plain["system"]
    first = shaped["messages"][0]["content"]
    assert first[-1] == {"type": "text", "text": schema_note(_SHAPE)}
    # The same block on every turn: the first message never changes as the turns grow.
    later = _call(
        provider="openrouter",
        output_format=_SHAPE,
        messages=_messages(3),
        cache=SINGLE_SHOT,
        schema_in="first_message",
    )
    assert later["messages"][0] == shaped["messages"][0]


def test_the_layout_is_a_setting_and_legacy_by_default() -> None:
    from app.config import Settings, unknown_settings
    from app.review.assistant.chat_engine import EngineSettings
    from app.review.assistant.chat_service import MessageJob

    assert Settings.model_fields["assistant_cache_layout"].default == "legacy"
    assert unknown_settings({"GV_ASSISTANT_CACHE_LAYOUT": "shared"}) == []
    assert EngineSettings.__dataclass_fields__["cache_layout"].default == "legacy"
    assert MessageJob.__dataclass_fields__["cache_layout"].default == "legacy"
