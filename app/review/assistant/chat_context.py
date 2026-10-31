"""Room in a long chat (#1548): old tool results and old thinking cleared, history kept append-only.

A chat replays every earlier turn byte for byte (prompt caching and Claude's preserved thinking need
an append-only history). A long chat on a 60-page set fills the context with old pictures and command
output it no longer needs. claude.ai and Claude Code clear them; so does this:

- **Anthropic's API: context editing** (beta `context-management-2025-06-27`). Each lead call says
  `clear_tool_uses_20250919`: once the prompt passes `GV_ASSISTANT_CONTEXT_TOKENS` (default 400,000),
  the API clears the oldest tool results (pictures included: they sit inside the tool results) from
  the conversation it reads, never from ours, keeping the last `GV_ASSISTANT_CONTEXT_KEEP_TURNS`
  turns (default 2: this question and the one before) whole. Near the threshold it also says
  `clear_thinking_20251015` with the same boundary. The boundary is a turn's start, so it does not
  move while one answer works: each call clears the same blocks and the cached prefix holds. The
  check on replayed thinking compares the conversation as we send it, so server-side clearing is no
  edit of history (the claude-api skill: "server-side compaction and context editing don't count as
  edits").
- **Where the beta is not available** (the OpenRouter route): when the replayed history alone would
  pass the threshold, code writes a summary of the earlier turns (each question, its answer's words,
  the findings reported and the files handed over) and the answer starts from that summary instead
  of the history: simple compaction, which replays nothing older, so no earlier thinking is sent
  out of its conversation.
- **One agent's long run of tool calls** (the page reviewer, `tool_result_edits`): a reviewer has
  only two or three "turns" (its first message and code's notes), so keeping turns cleared nothing
  (2026-10-31: every call re-read every zoom picture). There the boundary is counted in tool uses
  and moves in batches: the oldest tool results are cleared only once a whole batch of at least
  `clear_at_least` tokens lies before the last `keep` tool uses, so a clear is rare and large and
  the cached prefix holds between clears; the tools named in `exclude` are never cleared.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = [
    "CONTEXT_BETA",
    "DEFAULT_CONTEXT_TOKENS",
    "DEFAULT_KEEP_TURNS",
    "context_edits",
    "context_keep_turns",
    "context_tokens",
    "history_summary",
    "tool_result_edits",
    "turn_starts",
]

CONTEXT_BETA: Final = "context-management-2025-06-27"
DEFAULT_CONTEXT_TOKENS: Final = 400_000
DEFAULT_KEEP_TURNS: Final = 2
#: Clearing also thinking starts this close to the threshold (a share of it).
NEAR_SHARE: Final = 0.9
#: A clear is skipped unless it frees at least this many tokens (each clear costs a cache write).
_CLEAR_AT_LEAST: Final = 50_000
#: The fallback summary: at most this many characters of each answer's words, and findings listed.
_ANSWER_CHARS: Final = 1_500
_QUESTION_CHARS: Final = 600
_FINDINGS: Final = 40
#: The app's scope note, which follows the question in its turn (`chat_scope.scope_note`).
_SCOPE_NOTE: Final = "The app scoped this question to "


def _env_int(name: str, default: int, low: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return max(low, value)


def context_tokens() -> int:
    """The prompt size at which old tool results start being cleared (`GV_ASSISTANT_CONTEXT_TOKENS`)."""
    return _env_int("GV_ASSISTANT_CONTEXT_TOKENS", DEFAULT_CONTEXT_TOKENS, 50_000)


def context_keep_turns() -> int:
    """Turns kept whole (`GV_ASSISTANT_CONTEXT_KEEP_TURNS`): this question and the ones before it."""
    return _env_int("GV_ASSISTANT_CONTEXT_KEEP_TURNS", DEFAULT_KEEP_TURNS, 1)


def _is_question(message: Mapping[str, Any]) -> bool:
    """A user message that opens a turn (a question), not one carrying tool results."""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    return not any(
        isinstance(block, Mapping) and block.get("type") == "tool_result" for block in content
    )


def turn_starts(messages: Sequence[Mapping[str, Any]]) -> list[int]:
    """Where each turn (a reviewer's question and its answer) starts in the API messages."""
    return [n for n, message in enumerate(messages) if _is_question(message)]


def _count(messages: Sequence[Mapping[str, Any]], kind: str) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            continue
        if kind == "assistant":
            total += 1
            continue
        total += sum(
            1 for block in content if isinstance(block, Mapping) and block.get("type") == kind
        )
    return total


def context_edits(
    messages: Sequence[Mapping[str, Any]],
    *,
    threshold: int,
    keep_turns: int,
    near: bool,
) -> dict[str, Any]:
    """The `context_management` of one lead call: clear tool results (and, when `near`, thinking)
    older than the last `keep_turns` turns, once the prompt passes `threshold` tokens."""
    starts = turn_starts(messages)
    boundary = starts[-keep_turns] if len(starts) >= keep_turns else 0
    kept = messages[boundary:]
    edits: list[dict[str, Any]] = []
    if near:
        # Listed first, as the API asks.
        edits.append(
            {
                "type": "clear_thinking_20251015",
                "keep": {"type": "thinking_turns", "value": max(1, _count(kept, "assistant"))},
            }
        )
    edits.append(
        {
            "type": "clear_tool_uses_20250919",
            "trigger": {"type": "input_tokens", "value": threshold},
            "keep": {"type": "tool_uses", "value": max(1, _count(kept, "tool_use"))},
            "clear_at_least": {
                "type": "input_tokens",
                "value": min(_CLEAR_AT_LEAST, max(1_000, threshold // 8)),
            },
        }
    )
    return {"edits": edits}


#: A picture's tokens (`tool_result_edits`): one per 28 x 28 pixel patch, and when its size is not
#: known, a full picture of the page reviewer's (1600 x 1132 px).
_PATCH_PX: Final = 28
_UNKNOWN_PICTURE_TOKENS: Final = 2_400
#: Characters of text per token, about (a tool result's words).
_CHARS_PER_TOKEN: Final = 4


def _picture_tokens(block: Mapping[str, Any]) -> int:
    source = block.get("source")
    if isinstance(source, Mapping):
        try:
            width, height = int(source["width"]), int(source["height"])
        except (KeyError, TypeError, ValueError):
            return _UNKNOWN_PICTURE_TOKENS
        if width > 0 and height > 0:
            return math.ceil(width / _PATCH_PX) * math.ceil(height / _PATCH_PX)
    return _UNKNOWN_PICTURE_TOKENS


def _result_tokens(block: Mapping[str, Any]) -> int:
    """A tool result's tokens, about: its pictures by their size, its words by their length."""
    content = block.get("content")
    if isinstance(content, str):
        return len(content) // _CHARS_PER_TOKEN
    if not isinstance(content, list):
        return 0
    total = 0
    for part in content:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") == "image":
            total += _picture_tokens(part)
        elif part.get("type") == "text":
            total += len(str(part.get("text") or "")) // _CHARS_PER_TOKEN
    return total


def _tool_uses(messages: Sequence[Mapping[str, Any]]) -> list[tuple[str, int]]:
    """Every tool use in the conversation, in order: its tool's name and its result's tokens."""
    sizes: dict[str, int] = {}
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_result":
                sizes[str(block.get("tool_use_id"))] = _result_tokens(block)
    uses: list[tuple[str, int]] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                uses.append((str(block.get("name")), sizes.get(str(block.get("id")), 0)))
    return uses


def tool_result_edits(
    messages: Sequence[Mapping[str, Any]],
    *,
    trigger: int,
    keep: int,
    clear_at_least: int,
    exclude: Sequence[str] = (),
) -> dict[str, Any]:
    """The `context_management` of one agent's call in a long run of tool calls: old tool results
    cleared once the prompt passes `trigger` tokens, in batches of at least `clear_at_least`
    tokens, always keeping the last `keep` tool uses and never clearing the tools in `exclude`.

    The batches are counted from the conversation's start (its history is append-only), so the
    boundary moves only when a whole new batch lies before the last `keep` uses: between clears
    every call clears exactly the same blocks and the prompt cache holds. The API is told to keep
    every tool use after that boundary (at least `keep`); counted with or without the excluded
    tools, that never clears past it."""
    uses = _tool_uses(messages)
    excluded = set(exclude)
    boundary = 0
    gathered = 0
    for index, (name, tokens) in enumerate(uses):
        if index >= len(uses) - keep:
            break
        if name not in excluded:
            gathered += tokens
        if gathered >= clear_at_least:
            boundary, gathered = index + 1, 0
    edit: dict[str, Any] = {
        "type": "clear_tool_uses_20250919",
        "trigger": {"type": "input_tokens", "value": trigger},
        "keep": {"type": "tool_uses", "value": max(1, keep, len(uses) - boundary)},
        "clear_at_least": {"type": "input_tokens", "value": clear_at_least},
    }
    if excluded:
        edit["exclude_tools"] = sorted(excluded)
    return {"edits": [edit]}


# ---- the fallback: a code-written summary of the earlier turns -----------------------------------


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def history_summary(messages: Sequence[Mapping[str, Any]]) -> str:
    """The earlier turns in a few lines each, written by code from the API messages: the question,
    the answer's last words, the findings it reported and the files it handed over."""
    starts = turn_starts(messages)
    lines = [
        "<earlier_turns_summary>",
        (
            "This chat is too long to replay whole, so GV Review wrote this summary of its "
            "earlier turns (code, not the model). Your working files are still in /home/claude "
            "and /mnt/user-data/outputs."
        ),
    ]
    for number, start in enumerate(starts, start=1):
        end = starts[number] if number < len(starts) else len(messages)
        turn = messages[start:end]
        # The question is the turn's last text block: the app's own blocks (the files, the focus
        # line, set notes, an earlier summary) come before it.
        asked = [
            t for t in _texts(turn[0].get("content")) if t.strip() and not t.startswith(_SCOPE_NOTE)
        ]
        question = asked[-1] if asked else ""
        lines.append(f"\nTurn {number}. Question: {_short(question, _QUESTION_CHARS)}")
        findings: list[str] = []
        files: list[str] = []
        answer = ""
        for message in turn[1:]:
            content = message.get("content")
            if message.get("role") != "assistant" or not isinstance(content, list):
                continue
            words = [t for t in _texts(content) if t.strip()]
            if words:
                answer = "\n".join(words)
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                    continue
                given = block.get("input") if isinstance(block.get("input"), Mapping) else {}
                assert isinstance(given, Mapping)
                if block.get("name") == "report_finding":
                    findings.append(
                        f"p.{given.get('page')} {given.get('item')}: architect "
                        f"{given.get('architect')}, vendor {given.get('vendor')} "
                        f"({given.get('status')})"
                    )
                elif block.get("name") == "present_files":
                    files += [str(path) for path in given.get("filepaths") or []]
        if answer:
            lines.append(f"Answer: {_short(answer, _ANSWER_CHARS)}")
        if findings:
            lines.append(
                "Findings it reported: "
                + "; ".join(findings[:_FINDINGS])
                + (f"; and {len(findings) - _FINDINGS} more" if len(findings) > _FINDINGS else "")
            )
        if files:
            lines.append("Files handed over: " + ", ".join(files[:10]))
    lines.append("</earlier_turns_summary>")
    return "\n".join(lines)
