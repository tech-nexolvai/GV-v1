"""A frozen copy of `chat_request.call_params` as it was before the cache layouts (rebuild plan
Step 5; main at d5cfeb8b, with #2093), kept only so a test can prove that the `legacy` layout still
sends byte for byte the same request (`test_cache_layout.py`). Never imported by the app; never
edit it to make a test pass."""

# ruff: noqa
# mypy: ignore-errors

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.review.assistant import chat_request as _chat_request
from app.review.assistant.chat_context import CONTEXT_BETA
from app.review.assistant.chat_request import (
    BINDING_BETA,
    FALLBACK_BETA,
    TOOL_SEARCH,
    ChatModel,
    schema_note,
)

_EPHEMERAL = {"type": "ephemeral"}
_BLOCK_BINDING = {"prefix_mismatch_behavior": "drop_block"}


def _with_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages with a cache breakpoint on the newest block, copied only where it changes."""
    if not messages:
        return messages
    last = dict(messages[-1])
    content = last.get("content")
    if isinstance(content, list) and content and isinstance(content[-1], Mapping):
        blocks = list(content)
        blocks[-1] = {**blocks[-1], "cache_control": dict(_EPHEMERAL)}
        last["content"] = blocks
    return [*messages[:-1], last]


def legacy_call_params(
    *,
    provider: Provider,
    model: ChatModel,
    effort: str,
    thinking: Mapping[str, str],
    system: str,
    tools: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]],
    final: bool,
    max_tokens: int,
    context_management: Mapping[str, Any] | None = None,
    output_format: Mapping[str, Any] | None = None,
    enforce_format: bool = False,
) -> dict[str, Any]:
    """The keyword arguments of one streamed call (`client.beta.messages.create(stream=True)` on
    Anthropic's API with a beta, else `client.messages.create(stream=True)`): everything that
    leaves the process apart from the key. `messages` already carry their pictures' bytes.

    `output_format`: a JSON schema the reply must follow (structured outputs). On Anthropic's own
    API it goes in `output_config.format`, so the reply is that JSON and nothing else. On
    OpenRouter the schema is added to the system prompt; with `enforce_format` it is also sent in
    `output_config.format` (OpenRouter's Messages API takes it, and with `require_parameters` the
    route only runs on a host that enforces it). Only the readings ask for that (2026-10-28: their
    third reader wrapped its JSON in words on 30 of 48 calls of a real upload); every other call
    with a schema is unchanged."""
    listed = [dict(tool) for tool in tools]
    if provider != "anthropic":
        # Tool search is Anthropic's: every tool goes loaded elsewhere.
        for tool in listed:
            tool.pop("defer_loading", None)
    elif any(tool.get("defer_loading") for tool in listed):
        listed.insert(0, dict(TOOL_SEARCH))
    # The tools' cache breakpoint: on the last tool always loaded (a deferred tool takes none).
    loaded = [
        n
        for n, tool in enumerate(listed)
        if "input_schema" in tool and not tool.get("defer_loading")
    ]
    if loaded:
        listed[loaded[-1]]["cache_control"] = dict(_EPHEMERAL)
    if output_format is not None and provider != "anthropic":
        system = f"{system}\n\n{schema_note(output_format)}"
    params: dict[str, Any] = {
        "model": model.id if provider == "anthropic" else model.openrouter,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": dict(_EPHEMERAL)}],
        "messages": _with_breakpoint([dict(message) for message in messages]),
        "tools": listed,
        # Never `any` or `tool`: forced tool use is a 400 on Fable 5.1 and the 5.5 models.
        "tool_choice": {"type": "none"} if final else {"type": "auto"},
        "thinking": dict(thinking),
        "output_config": {"effort": effort},
    }
    if not listed:
        # A call without tools (a finding's second read) sends neither field.
        del params["tools"], params["tool_choice"]
    if output_format is not None and (provider == "anthropic" or enforce_format):
        params["output_config"]["format"] = {
            "type": "json_schema",
            "schema": json.loads(json.dumps(output_format)),
        }
    if provider == "anthropic":
        betas: list[str] = []
        if thinking.get("type") == "adaptive":
            # The safety net for preserved thinking (`between_tools` and `disabled` take no other
            # field, and carry no thinking to bind).
            params["thinking"]["block_binding"] = dict(_BLOCK_BINDING)
            betas.append(BINDING_BETA)
        if model.fallbacks:
            betas.append(FALLBACK_BETA)
            params["fallbacks"] = "default"
        if context_management:
            # Old tool results and thinking cleared server-side (`chat_context`, #1548).
            betas.append(CONTEXT_BETA)
            params["context_management"] = json.loads(json.dumps(context_management))
        if betas:
            params["betas"] = betas
    else:
        if model.extra:
            # OpenRouter turns adaptive thinking and `output_config.effort` into the model's own
            # reasoning effort; `display` (and every other thinking form) is Claude's.
            params["thinking"] = {"type": "adaptive"}
            params["output_config"]["effort"] = model.effort_for(effort)
        elif context_management:
            # OpenRouter passes Claude's context editing through (checked 2026-10-29: the
            # answer carries `applied_edits`): old zoom pictures cleared there too.
            params["betas"] = [CONTEXT_BETA]
            params["context_management"] = json.loads(json.dumps(context_management))
        routing = _chat_request._openrouter_routing()
        if model.hosts:
            # Only the model's own keep-no-data hosts (the rest of the block unchanged).
            routing["only"] = list(model.hosts)
        params["extra_body"] = {"provider": routing}
    return params
