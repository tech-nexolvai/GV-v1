"""A second, independent read of one chat finding (chat v3, trust).

A fresh, cheap model sees ONLY the two crops (the architect's drawing and the vendor's drawing at the
finding's spot) and one neutral question: what value is printed for this item on each drawing? It
is never shown the first answer, so it cannot simply agree with it. Code then compares its numbers
with the finding's, in inches (`finding_checks.to_inches`): `agrees`, `disagrees` or `unsure`.

The engine owns the model, the money (counted in the message's cost, within its caps) and the usage
rows: it passes `model_call`, which sends one user message and returns the reply's text, and raises
when the call fails (the engine then records the check as `not_run`). Nothing here touches the
network.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final, Literal

from app.review.assistant.finding_checks import to_inches

__all__ = ["SIDES", "SecondRead", "compare_read", "parse_reply", "question", "second_read"]

type ReadResult = Literal["agrees", "disagrees", "unsure"]

#: The two drawings, in the order they are shown and named.
SIDES: Final = ("architect", "vendor")
_NAMES: Final = {"architect": "the architect's drawing", "vendor": "the vendor's shop drawing"}

#: A reply wrapped in a code fence, as models sometimes write it. Only the fence is removed.
_FENCED: Final = re.compile(r"^```(?:json)?\s*(?P<body>.*?)\s*```$", re.DOTALL)

#: An item name longer than this is cut: it is a label for the question, not a description.
_LONGEST_ITEM: Final = 120


@dataclass(frozen=True, slots=True)
class SecondRead:
    """What the fresh model read on each crop, as printed, and code's comparison with the finding."""

    architect: str | None
    vendor: str | None
    result: ReadResult = "unsure"
    sure: bool = False
    """Whether the model said it was sure. A read it is not sure of never agrees."""
    asked: tuple[str, ...] = ()
    """The sides it was shown a crop of."""


def question(item: str, sides: tuple[str, ...]) -> str:
    """The neutral question: the item and the drawings, never the first answer's values."""
    label = " ".join(str(item).split())[:_LONGEST_ITEM] or "this item"
    shown = " and ".join(f"image {index} is {_NAMES[side]}" for index, side in enumerate(sides, 1))
    return (
        f"These are crops of construction drawings: {shown}. "
        f'What value is printed for "{label}" on each drawing? Copy it exactly as printed, with its '
        'marks (for example 7\'-2" or 12 3/4"). Use null for a drawing you were not shown or where '
        "no value for it is printed. Answer with JSON only: "
        '{"architect": string or null, "vendor": string or null, "sure": true or false}'
    )


def second_read(
    crops: Mapping[str, bytes],
    item: str,
    model_call: Callable[[list[dict[str, Any]]], str],
) -> SecondRead:
    """Ask the fresh model what is printed on the crops. `result` stays `unsure` until
    `compare_read`. A reply that is not the asked-for JSON is read as nothing (`unsure`)."""
    sides = tuple(side for side in SIDES if crops.get(side))
    if not sides:
        return SecondRead(architect=None, vendor=None, asked=())
    content: list[dict[str, Any]] = []
    for side in sides:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(crops[side]).decode("ascii"),
                },
            }
        )
    content.append({"type": "text", "text": question(item, sides)})
    reply = model_call(content)
    return replace(parse_reply(reply), asked=sides)


def parse_reply(reply: str) -> SecondRead:
    """The reply read strictly: one JSON object with `architect` and `vendor` (text or null) and
    `sure` (true/false), nothing else. Anything else is a read of nothing."""
    nothing = SecondRead(architect=None, vendor=None)
    if not isinstance(reply, str):
        return nothing
    text = reply.strip()
    fenced = _FENCED.match(text)
    if fenced is not None:
        text = fenced.group("body")
    try:
        answer = json.loads(text)
    except ValueError:
        return nothing
    if not isinstance(answer, dict) or set(answer) != {"architect", "vendor", "sure"}:
        return nothing
    architect, vendor, sure = answer["architect"], answer["vendor"], answer["sure"]
    if not isinstance(sure, bool):
        return nothing
    if not all(value is None or isinstance(value, str) for value in (architect, vendor)):
        return nothing
    return SecondRead(
        architect=architect.strip() or None if isinstance(architect, str) else None,
        vendor=vendor.strip() or None if isinstance(vendor, str) else None,
        sure=sure,
    )


def compare_read(read: SecondRead, finding: Mapping[str, Any]) -> SecondRead:
    """Code compares the read with `finding["architect"]` / `finding["vendor"]`, in inches.

    Only a read the model was sure of counts. Then `disagrees` when a side it was shown gives a
    different number from the finding's, `agrees` when every side it was shown gives the same
    number; `unsure` otherwise (not sure, nothing read, or a value that is not a number).
    """
    if not read.sure:
        return replace(read, result="unsure")
    compared: list[bool | None] = []
    for side in read.asked:
        stated = finding.get(side)
        seen = read.architect if side == "architect" else read.vendor
        expected = to_inches(stated) if isinstance(stated, str) else None
        found = to_inches(seen) if seen is not None else None
        compared.append(None if expected is None or found is None else expected == found)
    if False in compared:
        return replace(read, result="disagrees")
    if not compared or None in compared:
        return replace(read, result="unsure")
    return replace(read, result="agrees")
