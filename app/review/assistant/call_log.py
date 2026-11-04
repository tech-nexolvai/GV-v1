"""Every model call of an answer, one line each: its tokens, its cache use and its price (rebuild
plan Step 1B).

Before this, an answer's money was known only per phase (`chat_timings`): nobody could say which
calls wrote the prompt cache, which read it, or which host answered. Now every try of every call
the review assistant makes (`ChatEngine._call`, and the simple check's compare calls, which stream
on their own) adds one line to `<chat folder>/calls/<message id>.jsonl` (`CallLog`):

- **Where it ran:** the time it ended (UTC), the chat and message ids, the timings phase
  (`chat_timings.Timings.current`, "other" when none is open) and the helper's number, and the
  tags its caller set (`tagged`): the specialist's `kind` (its lens) and `agent` id, and what it
  was about (`page`: a specialist's page numbers, or a drawing's neutral ref such as `p11.v1`).
  A call with no `kind` tag is of the kind its prompt id names (`purpose_of`: `check` for a
  second read, `page-match` for the page matcher, `lead` for the lead).
- **What it asked for and who answered:** the price key of the model (`model`) and the model id
  that answered (`model_answered`); the provider asked for (`provider_requested`: `anthropic`, or
  the OpenRouter hosts the request allowed, `requested_provider`) and the host that answered
  (`provider_answered`: the `provider` field of the stream's first message when the service sends
  one, else `null`).
- **Tokens, money and time:** input, cache writes (and their 5-minute / 1-hour split when the usage
  carries a `cache_creation` object, else `null`), cache reads, output; the price (`cost_usd`, the
  same rates as `ChatEngine._cost`, as a decimal string; `null` when the price is unknown) and what
  the cache writes added to it (`cache_write_cost_usd`: the try's price less its price without
  them); the milliseconds from opening the stream to its end (`latency_ms`) and, for the engine's
  calls, the wait for a place before it (`queued_ms`).
- **How it ended:** the stop reason, the error's class name, kind and HTTP status (never its
  words), the try (`attempt`: 1, then 2 and on each time it was asked again) and, after a
  refusal fallback, each model's share as its own line (`part` of `parts`, as the Usage page
  records them).
- **Which prompt prefix it sent:** `prefix_hash` (see `prefix_hash`), so calls that could share a
  prompt cache entry are counted together (`scripts/call_log_summary.py`).

**Never the content.** No prompt text, picture bytes or answer reaches a line: only counts, ids,
names and hashes. Writing is best effort: a line that cannot be written is logged and the answer
goes on (as the Usage rows are, `ChatEngine._record`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

__all__ = [
    "CALLS_FOLDER",
    "CallFacts",
    "CallLog",
    "Part",
    "current_tags",
    "prefix_hash",
    "purpose_of",
    "records",
    "requested_provider",
    "tagged",
]

_log = logging.getLogger(__name__)

#: The chat folder's subfolder of call logs, one file per message.
CALLS_FOLDER: Final = "calls"
#: The start every assistant prompt id shares (`chat_engine.prompt_ids`).
_PROMPT_PREFIX: Final = "review-assistant-chat-"
_MICROS: Final = Decimal(1_000_000)
#: The tags `tagged` takes.
_TAG_NAMES: Final = frozenset({"kind", "agent", "page"})

#: The tags open here (`tagged`): an immutable mapping, set and reset per block, so a pool task run
#: in a copy of the submitter's context (`work_pool.ContextPool`) sees its submitter's tags.
_TAGS: ContextVar[Mapping[str, str]] = ContextVar("gv_call_tags", default=MappingProxyType({}))


# ---- tags ----------------------------------------------------------------------------------------


@contextmanager
def tagged(**tags: object) -> Iterator[None]:
    """Tags the calls made in this block (on this thread, and in the pool tasks it starts in a
    copy of its context): `kind` (a specialist's lens), `agent` (its id), `page` (a page or page
    pair id). An inner block's tag wins; a tag given as `None` is left as it was."""
    unknown = set(tags) - _TAG_NAMES
    if unknown:
        raise ValueError(f"unknown call tags: {sorted(unknown)}")
    given = {name: str(value) for name, value in tags.items() if value is not None}
    token = _TAGS.set({**_TAGS.get(), **given})
    try:
        yield
    finally:
        _TAGS.reset(token)


def current_tags() -> Mapping[str, str]:
    """The tags open here."""
    return _TAGS.get()


# ---- what a call asked for -----------------------------------------------------------------------


def purpose_of(prompt_id: str, provider: str) -> str:
    """The call's purpose as its prompt id names it: `review-assistant-chat-{purpose}-{provider}-…`
    gives `purpose`; the lead's id, `review-assistant-chat-{provider}-…`, gives `lead`. An id of
    another shape is returned whole."""
    rest = prompt_id.removeprefix(_PROMPT_PREFIX)
    if rest == prompt_id:
        return prompt_id
    if rest.startswith(f"{provider}-"):
        return "lead"
    cut = rest.find(f"-{provider}-")
    return rest[:cut] if cut > 0 else prompt_id


def requested_provider(provider: str, params: Mapping[str, Any]) -> str:
    """Who the request asked to answer it: `anthropic` for Anthropic's own API; on OpenRouter, the
    hosts its routing block allows (`extra_body.provider.only`, joined with commas, e.g.
    `google-vertex/global`), else the provider's name."""
    if provider == "anthropic":
        return "anthropic"
    extra = params.get("extra_body")
    routing = extra.get("provider") if isinstance(extra, Mapping) else None
    only = routing.get("only") if isinstance(routing, Mapping) else None
    if isinstance(only, list | tuple) and only:
        return ",".join(str(host) for host in only)
    return provider


# ---- the prompt prefix ---------------------------------------------------------------------------


def _blocks(message: Mapping[str, Any]) -> list[Any]:
    content = message.get("content")
    return list(content) if isinstance(content, list) else []


def _cached_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The messages up to and including the last block with a cache breakpoint other than the
    newest block (the last block of the last message, where `chat_request` always puts one):
    whole messages before it, its own message cut after it. None such: no message."""
    newest = None
    if messages and _blocks(messages[-1]):
        newest = (len(messages) - 1, len(_blocks(messages[-1])) - 1)
    last = None
    for at, message in enumerate(messages):
        for index, block in enumerate(_blocks(message)):
            if isinstance(block, Mapping) and "cache_control" in block and (at, index) != newest:
                last = (at, index)
    if last is None:
        return []
    at, index = last
    cut = {**messages[at], "content": _blocks(messages[at])[: index + 1]}
    return [*(dict(message) for message in messages[:at]), cut]


def _data_hash(data: str) -> str:
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raw = data.encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical(value: Any) -> Any:
    """`value` with every `cache_control` left out and every inline file's base64 `data` (a
    picture's, a document's) replaced by the sha256 of its bytes."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key == "cache_control":
                continue
            if key == "data" and value.get("type") == "base64" and isinstance(item, str):
                out[key] = _data_hash(item)
            else:
                out[str(key)] = _canonical(item)
        return out
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    return value


def prefix_hash(params: Mapping[str, Any]) -> str:
    """The sha256 (hex) of the request's cacheable prefix, as sent.

    The prefix is, in the order the API reads it: the `tools` (an empty list when the call sends
    none), the `system` prompt, and the `messages` up to and including the LAST block that carries
    a cache breakpoint other than the newest block (the last block of the last message, where
    `chat_request.call_params` always puts one); with no such block, no message. It is written
    as JSON with sorted keys and no spaces, with every `cache_control` left out (a breakpoint's
    place or lifetime is not part of the content) and every base64 `data` (a picture) replaced by
    the sha256 of its decoded bytes.

    So two calls with the same tools, system prompt and cached blocks have the same hash whatever
    their newest block, and a change to any of those three changes it. The model, the thinking
    settings and `tool_choice` are not in it (the line's `model` says the first)."""
    prefix = {
        "tools": _canonical(params.get("tools") or []),
        "system": _canonical(params.get("system")),
        "messages": _canonical(_cached_messages(params.get("messages") or [])),
    }
    text = json.dumps(prefix, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- one call's lines ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Part:
    """One model's share of a try (a refusal fallback's declined and answering models, else the
    whole try): its price key, its usage, its price and what its cache writes added to it
    (millionths of a dollar; `None`: the price is unknown)."""

    model_key: str
    usage: Mapping[str, Any]
    cost_micros: int | None
    write_micros: int | None


@dataclass(frozen=True, slots=True)
class CallFacts:
    """What one try of a call was, besides its tokens: never its content."""

    prompt_id: str
    purpose: str
    attempt: int
    provider_requested: str | None = None
    provider_answered: str | None = None
    model_answered: str | None = None
    prefix_hash: str | None = None
    latency_ms: int | None = None
    queued_ms: int | None = None
    stop_reason: str | None = None
    error: BaseException | None = None


def _count(usage: Mapping[str, Any], key: str) -> int:
    try:
        return int(usage.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _split(usage: Mapping[str, Any], key: str) -> int | None:
    creation = usage.get("cache_creation")
    return _count(creation, key) if isinstance(creation, Mapping) else None


def _usd(micros: int | None) -> str | None:
    return None if micros is None else f"{Decimal(micros) / _MICROS:f}"


def records(
    call: CallFacts,
    parts: Sequence[Part],
    *,
    chat_id: str | None,
    message_id: str | None,
    phase: str,
    helper: int | None,
    tags: Mapping[str, str],
    ts: datetime,
) -> list[dict[str, Any]]:
    """One try's lines: one per part, in order. The stop reason and the error go on the last line
    (as on the Usage rows); a part's missing token count is 0; the cache-write split is `null`
    when its usage has no `cache_creation` object."""
    lines: list[dict[str, Any]] = []
    error = call.error
    for number, part in enumerate(parts, start=1):
        last = number == len(parts)
        usage = part.usage
        lines.append(
            {
                "ts": ts.isoformat(),
                "chat_id": chat_id,
                "message_id": message_id,
                "phase": phase,
                "helper": helper,
                "kind": tags.get("kind") or call.purpose,
                "agent": tags.get("agent"),
                "page": tags.get("page"),
                "purpose": call.purpose,
                "prompt_id": call.prompt_id,
                "model": part.model_key,
                "model_answered": usage.get("model") if len(parts) > 1 else call.model_answered,
                "provider_requested": call.provider_requested,
                "provider_answered": call.provider_answered,
                "attempt": call.attempt,
                "part": number,
                "parts": len(parts),
                "input_tokens": _count(usage, "input_tokens"),
                "cache_creation_input_tokens": _count(usage, "cache_creation_input_tokens"),
                "ephemeral_5m_input_tokens": _split(usage, "ephemeral_5m_input_tokens"),
                "ephemeral_1h_input_tokens": _split(usage, "ephemeral_1h_input_tokens"),
                "cache_read_input_tokens": _count(usage, "cache_read_input_tokens"),
                "output_tokens": _count(usage, "output_tokens"),
                "cost_usd": _usd(part.cost_micros),
                "cache_write_cost_usd": _usd(part.write_micros),
                "latency_ms": call.latency_ms,
                "queued_ms": call.queued_ms,
                "stop_reason": call.stop_reason if last else None,
                "error": type(error).__name__ if last and error is not None else None,
                "error_kind": getattr(error, "kind", None) if last else None,
                "error_status": getattr(error, "status", None) if last else None,
                "prefix_hash": call.prefix_hash,
            }
        )
    return lines


class CallLog:
    """One message's call log: `<chat folder>/calls/<message id>.jsonl`, appended to only."""

    def __init__(self, path: Path, *, chat_id: str | None, message_id: str | None) -> None:
        self.path = path
        self.chat_id = chat_id
        self.message_id = message_id
        self._lock = threading.Lock()

    @classmethod
    def for_message(cls, chat_folder: Path, chat_id: object, message_id: object) -> CallLog:
        """The log of message `message_id` in the chat whose folder is `chat_folder`."""
        path = chat_folder / CALLS_FOLDER / f"{message_id}.jsonl"
        return cls(path, chat_id=str(chat_id), message_id=str(message_id))

    def append(self, lines: Sequence[Mapping[str, Any]]) -> bool:
        """Appends `lines` (one JSON object each, written together); False, and the failure
        logged, when they could not be written."""
        if not lines:
            return True
        try:
            text = "".join(
                json.dumps(dict(line), separators=(",", ":"), allow_nan=False) + "\n"
                for line in lines
            )
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as out:
                    out.write(text)
        except (OSError, TypeError, ValueError):
            _log.exception("assistant call log could not be written")
            return False
        return True
