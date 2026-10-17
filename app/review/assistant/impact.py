"""How much each Wrong item matters, by code: the biggest issues first (#1569).

A full check of a 60-page set can list 40 Wrong items; the reviewer needs the few that change what
the shop builds first. Code scores each Wrong (and Your-decision) finding from:

- **the size of the difference**: code's inches (never the AI's arithmetic), and how big it is
  against the architect's value;
- **what it touches**: an overall size, a cut-out or appliance opening, a height, an outlet, a
  missing or extra part changes fabrication; a tag, a label or a finish note is cosmetic (and a
  quarter inch on a label even more so);
- **how sure it is**: a checked item (Confirmed, Likely) always comes before one that is an AI
  reading only (the DECIDED order of the top issues), then the score.

It is ORDERING ONLY: no new status, no new colour (Wrong stays Wrong everywhere). The screen, the
summary's top issues, the cloud drawing's numbers and the check report all use `by_impact`, so
"AI 1" is the biggest item everywhere. Pure code: no I/O, no AI call.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Final

__all__ = [
    "BIGGEST_FIRST",
    "TOP_IMPACT",
    "biggest_line",
    "blocks_with_impacts",
    "by_impact",
    "impact_of",
    "with_impacts",
]

#: How many of the biggest items the screen and the notes show first.
TOP_IMPACT: Final = 5
#: The quiet note above them.
BIGGEST_FIRST: Final = "Biggest first"

_TRUSTED: Final = frozenset({"confirmed", "likely"})
_TRUST_POINTS: Final = {"confirmed": 20, "likely": 14, "to_check": 4}
_TRUST_WORDS: Final = {"confirmed": "confirmed", "likely": "likely", "to_check": "AI reading only"}

#: What an item touches, in the order they are tried: (pattern, points, words).
_TOUCHES: Final[tuple[tuple[re.Pattern[str], int, str], ...]] = tuple(
    (re.compile(pattern, re.IGNORECASE), points, words)
    for pattern, points, words in (
        (
            r"\b(missing|omitted|not shown|left out|extra|added)\b",
            35,
            "changes fabrication (a missing or extra part)",
        ),
        (
            (
                r"\b(cut-?outs?|sinks?|cooktops?|ranges?|faucets?|appliances?|openings?|fridges?|"
                r"refrigerators?|dishwashers?|microwaves?|ovens?)\b"
            ),
            32,
            "changes fabrication (a cut-out or appliance opening)",
        ),
        (
            r"\b(overall|total|wall[- ]to[- ]wall|run length)\b",
            32,
            "changes fabrication (overall size)",
        ),
        (r"\b(ada|heights?|toe ?kick)\b", 28, "changes fabrication (a height)"),
        (
            r"\b(doors?|drawers?|bins?|shelf|shelves|fronts?|bays?|sections?|counts?|pull-?outs?)\b",
            28,
            "changes fabrication (parts or their arrangement)",
        ),
        (
            r"\b(outlets?|j-?box|power|data|grommets?|receptacles?)\b",
            26,
            "changes fabrication (an outlet or power unit)",
        ),
        (r"\b(widths?|depths?|lengths?|thickness|overhang|reveal)\b", 24, "changes a size"),
        (
            r"\b(pulls?|handles?|locks?|legs?|hinges?|hardware|supports?)\b",
            18,
            "changes hardware",
        ),
    )
)
_COSMETIC: Final = re.compile(
    r"\b(tags?|labels?|finish(es)?|materials?|notes?|text|spelling|colou?rs?|titles?|sheet|"
    r"callouts?|keynotes?)\b",
    re.IGNORECASE,
)
_MISSING_VALUE: Final = re.compile(
    r"^\s*(not shown|missing|none|omitted|not drawn|no\b)", re.IGNORECASE
)


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _inches_words(value: Decimal) -> str:
    """9 → "9 in", 0.25 → "1/4 in", 1.5 → "1 1/2 in" (to the nearest 1/16)."""
    sixteenths = Fraction(int((value * 16).to_integral_value()), 16)
    whole = int(sixteenths)
    part = sixteenths - whole
    if not part:
        return f"{whole} in"
    if not whole:
        return f"{part.numerator}/{part.denominator} in"
    return f"{whole} {part.numerator}/{part.denominator} in"


def _size_points(difference: Decimal | None, architect: Decimal | None) -> int:
    if difference is None:
        return 12
    if difference >= 6:
        points = 40
    elif difference >= 3:
        points = 34
    elif difference >= 1:
        points = 28
    elif difference >= Decimal("0.5"):
        points = 20
    elif difference > Decimal("0.25"):
        points = 14
    elif difference > 0:
        points = 8
    else:
        points = 4
    if architect is not None and architect > 0 and difference > 0:
        share = difference / architect
        points += 6 if share >= Decimal("0.1") else 3 if share >= Decimal("0.03") else 0
    return points


def _touches(finding: Mapping[str, Any], difference: Decimal | None) -> tuple[int, str]:
    item = str(finding.get("item") or "")
    vendor = str(finding.get("vendor") or "")
    if finding.get("source") == "completeness" or _MISSING_VALUE.match(vendor):
        return 35, "changes fabrication (a missing or extra part)"
    touched = next(
        ((points, words) for pattern, points, words in _TOUCHES if pattern.search(item)), None
    )
    cosmetic = _COSMETIC.search(item) is not None
    if cosmetic and (touched is None or (difference is not None and difference <= Decimal("0.25"))):
        return 6, "cosmetic (a tag, label or note)"
    if touched is not None:
        return touched
    return 18, "a dimension"


def impact_of(finding: Mapping[str, Any]) -> dict[str, Any] | None:
    """A Wrong or Your-decision finding's score (0-100) and why, in plain words; `None` for a
    Right one."""
    if finding.get("status") not in ("different", "check"):
        return None
    given = finding.get("inches")
    inches: Mapping[str, Any] = given if isinstance(given, Mapping) else {}
    raw = _decimal(inches.get("difference"))
    difference = None if raw is None else abs(raw)
    architect = _decimal(inches.get("architect"))
    points, touches = _touches(finding, difference)
    level = str(finding.get("trust_level") or "")
    score = _size_points(difference, architect) + points + _TRUST_POINTS.get(level, 8)
    words = [touches]
    if difference is not None and difference > 0:
        words.append(f"{_inches_words(difference)} off")
    if level in _TRUST_WORDS:
        words.append(_TRUST_WORDS[level])
    return {"score": max(0, min(100, score)), "why": "; ".join(words)}


def _key(finding: Mapping[str, Any], index: int) -> tuple[int, int, int, int, int]:
    status = {"different": 0, "check": 1}.get(str(finding.get("status")), 2)
    found = impact_of(finding)
    trusted = 0 if finding.get("trust_level") in _TRUSTED else 1
    score = 0 if found is None else int(found["score"])
    if status == 2:
        return (status, 0, 0, 0, index)
    return (status, trusted, -score, int(finding.get("page") or 0), index)


def by_impact(findings: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Wrong first, then Your decision, each with the checked items before the AI-reading-only
    ones and then the biggest score first; Right items last, in their own order."""
    order = sorted(range(len(findings)), key=lambda i: _key(findings[i], i))
    return [findings[i] for i in order]


def biggest_line(numbered: Sequence[tuple[int, Mapping[str, Any]]]) -> str | None:
    """The notes pages' line naming the biggest Wrong items by their numbers:
    "Biggest first: AI 1 p.4 Buffet outlet height · AI 2 …" (`None`: nothing wrong)."""
    wrong = sorted(
        ((n, f) for n, f in numbered if f.get("status") == "different"), key=lambda pair: pair[0]
    )[:TOP_IMPACT]
    if not wrong:
        return None
    parts = [f"AI {n} p.{f.get('page')} {str(f.get('item') or 'item').strip()}" for n, f in wrong]
    return f"{BIGGEST_FIRST}: " + " · ".join(parts)


def blocks_with_impacts(blocks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """An answer's blocks with each finding's `impact` (worked out on read; nothing stored)."""
    positions = [
        n
        for n, block in enumerate(blocks)
        if block.get("type") == "finding" and isinstance(block.get("finding"), Mapping)
    ]
    out = [dict(block) for block in blocks]
    scored = with_impacts([blocks[n]["finding"] for n in positions])
    for n, finding in zip(positions, scored, strict=True):
        out[n]["finding"] = finding
    return out


def with_impacts(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each finding (copied, in its own order) with `impact`: its score, why, and its rank among
    the items of its status (1 = the biggest). Right items carry none."""
    ranks: dict[int, int] = {}
    seen: dict[str, int] = {}
    for finding in by_impact(findings):
        status = str(finding.get("status"))
        if status in ("different", "check"):
            seen[status] = seen.get(status, 0) + 1
            ranks[id(finding)] = seen[status]
    out: list[dict[str, Any]] = []
    for finding in findings:
        copy = dict(finding)
        found = impact_of(finding)
        if found is None:
            copy.pop("impact", None)
        else:
            copy["impact"] = {**found, "rank": ranks.get(id(finding))}
        out.append(copy)
    return out
