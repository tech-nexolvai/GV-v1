"""The JSON answer in a model's reply, read the same way at every step that asks for one.

On Anthropic's own API a structured-output reply is bare JSON. On a route without structured
outputs (OpenRouter puts the schema in the prompt) the model often wraps the JSON in a ```json
block or writes a sentence first; a strict `json.loads` then threw the answer away and the item
quietly stayed grey (log forensics, 2026-10-20). This reader takes the answer out of those
wrappings and nothing more:

1. the whole reply, when it is bare JSON;
2. else the one fenced block that is JSON (two different fenced answers: refused, never picked);
3. else the text from its first `{` to its last `}`, when that whole span is one JSON object.

It never takes a piece from inside a broken or cut answer, and never chooses between two answers:
any doubt raises `ValueError`, exactly like the strict read did. What the answer must contain is
still checked by each caller, as strictly as before (a wrong shape is refused, never guessed).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Final

_FENCED: Final = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n?(.*?)```", re.DOTALL)
_EXCERPT_CHARS: Final = 300


def json_of_reply(text: str) -> object:
    """The reply's one JSON answer (see the module); `ValueError` when there is not exactly one."""
    plain = (text or "").strip()
    if not plain:
        raise ValueError("the reply is empty")
    try:
        return json.loads(plain)
    except ValueError:
        pass
    fenced: list[object] = []
    for block in _FENCED.findall(plain):
        try:
            value = json.loads(block.strip())
        except ValueError:
            continue
        if isinstance(value, (dict, list)) and value not in fenced:
            fenced.append(value)
    if len(fenced) > 1:
        raise ValueError("the reply has more than one JSON answer")
    if fenced:
        return fenced[0]
    start, end = plain.find("{"), plain.rfind("}")
    if 0 <= start < end:
        value = json.loads(plain[start : end + 1])
        if isinstance(value, dict):
            return value
    raise ValueError("the reply holds no JSON answer")


def object_of_reply(text: str) -> dict[str, Any]:
    """`json_of_reply` when the answer must be one JSON object; `ValueError` otherwise."""
    value = json_of_reply(text)
    if not isinstance(value, dict):
        raise ValueError("the reply's answer is not a JSON object")  # noqa: TRY004 - one error kind
    return value


def excerpt(text: str, limit: int = _EXCERPT_CHARS) -> str:
    """The reply's start on one line, for a log line (never a key: replies hold drawing text)."""
    return " ".join((text or "").split())[:limit]


def log_unreadable(log: logging.Logger, step: str, item: str, text: str) -> None:
    """One warning when a reply still holds no readable answer: the step, the item, the start."""
    log.warning(
        "assistant reply not readable: step=%s item=%s chars=%d start=%r",
        step,
        item,
        len(text or ""),
        excerpt(text),
    )
