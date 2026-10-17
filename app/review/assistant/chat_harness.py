"""Each chat keeps the system prompt and tools it started with (#1577, preserved thinking).

**Why.** Claude Fable 5.1, Opus 5.5 and the other current models bind every thinking block to the
conversation that produced it: the top-level `system` prompt, the set of `tools` and every message
before it. A later request that replays the block after any of those changed is refused with a
400 on accounts Anthropic enforces this for (new accounts since 2026-08-31), or the block is
dropped (with `drop_block`, `chat_request.BINDING_BETA`) and, with it, every later thinking block
of the chat, on every later request. The history itself is append-only and replayed byte for byte
(`chat_store.history`), but the lead's system prompt and tools are module constants
(`chat_tools.LEAD_SYSTEM_PROMPT`, `LEAD_TOOLS`) that change with every new harness id: a chat begun
before a deploy would replay its thinking under a different prefix after it.

**What.** The lead's system prompt and tools are written to the chat's folder (`harness.json`) by
its first answer and read back by every later one, whatever harness the server now runs. Until a
chat has an answer in its history it takes today's (nothing to keep valid yet). A chat that began
before this file existed keeps today's from now on (its older thinking may be dropped once by the
safety net; nothing fails). Helpers, second reads, the critic and the question's task spec are
single-message conversations: they always use today's prompts.

**A newer harness reaches an older chat as an appended note** (`update_note`): today's lead prompt,
once, in the next question's turn, never by editing what was sent before. New tools reach new
chats only (adding a tool to a running chat needs Anthropic's mid-conversation tool changes).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from app.review.assistant.chat_engine import HARNESS_ID
from app.review.assistant.chat_tools import LEAD_SYSTEM_PROMPT, LEAD_TOOLS

__all__ = ["HARNESS_FILE", "ChatHarness", "chat_harness", "current_harness", "update_note"]

_log = logging.getLogger(__name__)

#: In the chat's folder, next to `home/`, `outputs/` and `pictures/`.
HARNESS_FILE: Final = "harness.json"
_NOTE_MARK: Final = "[GV Review update "


@dataclass(frozen=True, slots=True)
class ChatHarness:
    """The lead's system prompt and tools as one chat sends them, and the harness that made them."""

    harness_id: str
    system: str
    tools: tuple[dict[str, Any], ...]


def current_harness() -> ChatHarness:
    """Today's lead prompt and tools (copied through JSON, the same values a stored one reads back)."""
    tools = json.loads(json.dumps(list(LEAD_TOOLS)))
    return ChatHarness(harness_id=HARNESS_ID, system=LEAD_SYSTEM_PROMPT, tools=tuple(tools))


def _read(path: Path) -> ChatHarness | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _log.warning("assistant chat harness file could not be read; today's is used")
        return None
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("harness_id"), str)
        or not isinstance(raw.get("system"), str)
        or not isinstance(raw.get("tools"), list)
        or not all(isinstance(tool, dict) for tool in raw["tools"])
    ):
        _log.warning("assistant chat harness file has an unknown shape; today's is used")
        return None
    return ChatHarness(
        harness_id=raw["harness_id"], system=raw["system"], tools=tuple(raw["tools"])
    )


def _write(path: Path, harness: ChatHarness) -> None:
    """Written whole or not at all (a temporary file moved into place)."""
    data = {
        "harness_id": harness.harness_id,
        "system": harness.system,
        "tools": list(harness.tools),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        _log.warning("assistant chat harness file could not be written")
        temporary.unlink(missing_ok=True)


def chat_harness(folder: Path, *, replayed: bool) -> ChatHarness:
    """The lead's system prompt and tools for the chat whose folder is `folder`. `replayed`: its
    history has at least one answer to send again. Then the kept ones are used; otherwise today's,
    kept from now on."""
    path = folder / HARNESS_FILE
    if replayed:
        kept = _read(path)
        if kept is not None:
            return kept
        _log.info("assistant chat began before its harness was kept; today's is kept from now on")
    harness = current_harness()
    _write(path, harness)
    return harness


def update_note(harness: ChatHarness, history: Sequence[Mapping[str, Any]]) -> str | None:
    """The note that brings today's lead prompt to a chat kept on an older one, or `None`: the
    chat already runs today's prompt, or an earlier turn of its history already carries this note.
    """
    if harness.system == LEAD_SYSTEM_PROMPT:
        return None
    mark = f"{_NOTE_MARK}{HARNESS_ID}]"
    for message in history:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, Mapping) and str(block.get("text") or "").startswith(mark):
                return None
    return (
        f"{mark} GV Review's instructions changed since this chat began. Where they differ from "
        f"the ones at the start of this chat, follow these:\n\n{LEAD_SYSTEM_PROMPT}"
    )
