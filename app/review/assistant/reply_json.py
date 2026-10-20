"""The JSON answer in a model's reply, read the same way at every step that asks for one.

On Anthropic's own API a structured-output reply is bare JSON. On a route without structured
outputs (OpenRouter puts the schema in the prompt) the model often wraps the JSON in a ```json
block or writes a sentence first; a strict `json.loads` then threw the answer away and the item
quietly stayed grey (log forensics, 2026-10-20). This reader takes the answer out of those
wrappings and nothing more:

1. the whole reply, when it is bare JSON;
2. else the one fenced block that is JSON, when no brace is left outside it (two different
   fenced answers, or a fenced one and a bare one: refused, never picked);
3. else the text from its first `{` to its last `}`, when that whole span is one JSON object.

An inch mark written inside a string without its backslash (`"why": "prints 5'-2" and 4'-0""`)
is the one slip it mends (#1817: a prover's whole answer was lost to it): a `"` inside a string
that is not followed by `,` `:` `}` `]` or the end can never end that string in JSON, so it is read
as the inch mark it is. Nothing else is mended, and valid JSON is never changed.

It never takes a piece from inside a broken or cut answer, and never chooses between two answers:
any doubt raises `ValueError`, exactly like the strict read did. What the answer must contain is
still checked by each caller, as strictly as before (a wrong shape is refused, never guessed).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Final

__all__ = ["excerpt", "inch_marks_escaped", "json_of_reply", "log_unreadable", "object_of_reply"]

_FENCED: Final = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n?(.*?)```", re.DOTALL)
_EXCERPT_CHARS: Final = 300


#: What may follow a string's closing quote in JSON (after spaces).
_AFTER_STRING: Final = frozenset(",:}]")


def inch_marks_escaped(text: str) -> str:
    """`text` with each `"` inside a JSON string that cannot end it (not followed by `,` `:` `}`
    `]` or the end, spaces skipped) written as `\\"`. Valid JSON comes back unchanged."""
    out: list[str] = []
    inside = escaped = False
    size = len(text)
    for at, char in enumerate(text):
        if inside and escaped:
            escaped = False
        elif inside and char == "\\":
            escaped = True
        elif char == '"' and inside:
            ahead = at + 1
            while ahead < size and text[ahead] in " \t\r\n":
                ahead += 1
            if ahead < size and text[ahead] not in _AFTER_STRING:
                out.append("\\")  # an inch mark inside the string, not its end
            else:
                inside = False
        elif char == '"':
            inside = True
        out.append(char)
    return "".join(out)


def _loads(text: str) -> object:
    """`json.loads`, else the same text with its unescaped inch marks mended (`inch_marks_escaped`);
    `ValueError` when neither reads."""
    try:
        return json.loads(text)
    except ValueError:
        mended = inch_marks_escaped(text)
        if mended == text:
            raise
        return json.loads(mended)


def json_of_reply(text: str) -> object:
    """The reply's one JSON answer (see the module); `ValueError` when there is not exactly one."""
    plain = (text or "").strip()
    if not plain:
        raise ValueError("the reply is empty")
    try:
        return _loads(plain)
    except ValueError:
        pass
    fenced: list[object] = []
    for block in _FENCED.findall(plain):
        try:
            value = _loads(block.strip())
        except ValueError:
            continue
        if isinstance(value, (dict, list)) and value not in fenced:
            fenced.append(value)
    if len(fenced) > 1:
        raise ValueError("the reply has more than one JSON answer")
    if fenced:
        # A brace outside the fenced answer may be a second answer (a draft and a correction):
        # never pick one of them.
        if "{" in _FENCED.sub("", plain):
            raise ValueError("the reply may hold a second JSON answer outside its code block")
        return fenced[0]
    start, end = plain.find("{"), plain.rfind("}")
    if 0 <= start < end:
        value = _loads(plain[start : end + 1])
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
