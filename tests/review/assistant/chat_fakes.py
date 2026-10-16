"""Fakes for the chat-first assistant's tests (#1463): no network, no paid call, no Docker.

- `reply(...)` builds one streamed reply as the API's raw events (`message_start`,
  `content_block_*`, `message_delta`, `message_stop`), with thinking blocks signed like the real
  ones, so the tests check what the engine stores and sends back.
- `ScriptedProvider` serves scripted replies (or failures) and records every call's parameters as
  JSON text, exactly as they would leave the process. The lead's calls and each helper's are
  scripted apart (helpers run in threads), keyed by the helper's task.
- `FakeSandbox` runs a command through a Python callback on the chat's real folders.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.review.assistant.chat_provider import ProviderFailure
from app.review.assistant.chat_tools import LEAD_SYSTEM_PROMPT
from app.review.assistant.sandbox import ChatWorkspace, SandboxResult

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"


def thinking(text: str, signature: str = "sig-abc") -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": signature}


def text(words: str) -> dict[str, Any]:
    return {"type": "text", "text": words}


def tool(call_id: str, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": dict(arguments)}


def reply(
    *blocks: Mapping[str, Any],
    stop: str = "end_turn",
    model: str = OPUS,
    usage: Mapping[str, int] | None = None,
    stop_details: Mapping[str, Any] | None = None,
    cancel_after: int | None = None,
) -> list[dict[str, Any]]:
    """One reply's raw stream events. Text and thinking stream in two pieces each."""
    counts = {"input_tokens": 1000, "output_tokens": 0, **(usage or {})}
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "usage": {
                    "input_tokens": counts["input_tokens"],
                    "output_tokens": 1,
                    "cache_creation_input_tokens": counts.get("cache_creation_input_tokens", 0),
                    "cache_read_input_tokens": counts.get("cache_read_input_tokens", 0),
                },
            },
        }
    ]
    for index, block in enumerate(blocks):
        kind = block["type"]
        if kind == "thinking":
            events.append(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                }
            )
            half = len(block["thinking"]) // 2
            for piece in (block["thinking"][:half], block["thinking"][half:]):
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "thinking_delta", "thinking": piece},
                    }
                )
            events.append(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "signature_delta", "signature": block["signature"]},
                }
            )
        elif kind == "text":
            events.append(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                }
            )
            half = len(block["text"]) // 2
            for piece in (block["text"][:half], block["text"][half:]):
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": piece},
                    }
                )
        elif kind == "tool_use":
            events.append(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": {},
                    },
                }
            )
            raw = json.dumps(block["input"])
            half = len(raw) // 2
            for piece in (raw[:half], raw[half:]):
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": piece},
                    }
                )
        else:
            events.append({"type": "content_block_start", "index": index, "content_block": block})
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None, "stop_details": stop_details},
            "usage": {"output_tokens": counts["output_tokens"] or 500},
        }
    )
    events.append({"type": "message_stop"})
    if cancel_after is not None:
        events.insert(cancel_after, {"type": "__cancel__"})
    return events


type Script = list[dict[str, Any]] | BaseException


class _Stream:
    def __init__(self, events: list[dict[str, Any]], cancel: threading.Event | None) -> None:
        self._events = events
        self._cancel = cancel
        self.closed = False

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for event in self._events:
            if event.get("type") == "__cancel__":
                if self._cancel is not None:
                    self._cancel.set()
                continue
            if event.get("type") == "__wait_for_cancel__":
                # Holds the stream open until the message is cancelled (at most 10 s).
                if self._cancel is not None:
                    self._cancel.wait(10)
                continue
            if isinstance(event.get("__raise__"), BaseException):
                raise event["__raise__"]
            yield event

    def close(self) -> None:
        self.closed = True


@dataclass
class ScriptedProvider:
    """Scripted replies: `lead` in order; `helpers[task]` in order for the helper given `task`."""

    lead: list[Script] = field(default_factory=list)
    helpers: dict[str, list[Script]] = field(default_factory=dict)
    name: str = "anthropic"
    cancel: threading.Event | None = None
    calls: list[str] = field(default_factory=list)
    """Every call's parameters as JSON text, in the order they were made."""
    lead_calls: list[str] = field(default_factory=list)
    helper_calls: dict[str, list[str]] = field(default_factory=dict)
    helper_answer: Callable[[Mapping[str, Any]], Script] | None = None
    """Answers every helper call from its parameters instead of `helpers` (for helpers whose
    number of calls the engine decides)."""

    def __post_init__(self) -> None:
        self._lead = deque(self.lead)
        self._helpers = {task: deque(scripts) for task, scripts in self.helpers.items()}
        self._lock = threading.Lock()

    def open_stream(self, params: Mapping[str, Any]) -> _Stream:
        sent = json.dumps(params)
        system = params["system"][0]["text"]
        with self._lock:
            self.calls.append(sent)
            if system == LEAD_SYSTEM_PROMPT:
                self.lead_calls.append(sent)
                script = self._lead.popleft()
            else:
                task = params["messages"][0]["content"][0]["text"]
                self.helper_calls.setdefault(task, []).append(sent)
                if self.helper_answer is not None:
                    script = self.helper_answer(params)
                else:
                    script = self._helpers[task].popleft()
        if isinstance(script, BaseException):
            raise script
        return _Stream(script, self.cancel)


def server_error() -> ProviderFailure:
    return ProviderFailure(
        "The AI service had a problem (error 529). Please ask again.", status=529, retryable=True
    )


@dataclass
class FakeSandbox:
    """Runs a command with `handler(command, workspace)` -> (output, exit code)."""

    workspace: ChatWorkspace
    handler: Callable[[str, ChatWorkspace], tuple[str, int]] = lambda command, _w: ("ok", 0)
    kind: str = "fake"
    commands: list[str] = field(default_factory=list)
    killed: int = 0

    def run(self, command: str) -> SandboxResult:
        self.commands.append(command)
        output, code = self.handler(command, self.workspace)
        return SandboxResult(output=output, exit_code=code)

    def kill_all(self) -> None:
        self.killed += 1


@dataclass
class FakeLedger:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def record(self, **row: Any) -> None:
        self.rows.append(row)


def strip_cache_control(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: strip_cache_control(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [strip_cache_control(item) for item in value]
    return value


def events_named(events: Sequence[tuple[str, Any]], name: str) -> list[Any]:
    return [data for event, data in events if event == name]
