"""Code decides the AI page review's rows: the AI only reads (decision 2026-10-16).

For each architect dimension code read from the PDF text, exactly once, in the order listed:

1. **The architect row must be one code read.** An AI row whose `architect_id` is not listed, or
   whose `architect_text` does not read as the same exact inches as the listed one, is dropped and
   the drop is recorded with its reason. A listed dimension the AI gave no row for comes out
   `unsure` ("the AI gave no answer"), so nothing listed vanishes. A second row for the same id is
   dropped.
2. **The vendor's number must parse exactly** from the text as printed: millimetres with inches in
   brackets (`610 [24]`) give the bracketed inches; feet-inches (`2'-10 1/2"`) and inches (`31"`,
   `25 1/2"`) are read as written. A bare number with no inch mark and no brackets is not inches.
3. **SAME only if equal, DIFFERENT only if both parse and differ.** When the AI says same or
   different and the exact arithmetic agrees, that is the result. When they disagree, or the
   vendor's number does not parse, the row is `unsure`. `not_shown` and `unreadable` are kept as
   the AI said them (no number to check). **Nothing becomes PASS**: a different row needs a person.
4. **The vendor box** (where the vendor's number is printed, on the vendor image, 0..1000) is kept
   only when it is a plausible small box inside the image; otherwise `None`.

Pure: plain values in, plain values out.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final, Literal

__all__ = [
    "AI_RESULTS",
    "ArchitectListed",
    "CheckedRow",
    "Dropped",
    "check_rows",
    "parse_inches",
    "plausible_box",
]

Result = Literal["same", "different", "not_shown", "unsure", "unreadable"]
AI_RESULTS: Final[tuple[str, ...]] = ("SAME", "DIFFERENT", "NOT_SHOWN", "UNSURE", "UNREADABLE")

#: A vendor box wider or taller than this share of the vendor image is not one printed number.
_MAX_BOX_SIDE: Final = 400
_BOX_SCALE: Final = 1000

_QUOTES: Final = str.maketrans(
    {"’": "'", "‘": "'", "′": "'", "”": '"', "“": '"', "″": '"', "−": "-", "–": "-", "—": "-"}
)
_FRACTION: Final = r"(?:(?P<{0}w>\d+)\s+)?(?P<{0}n>\d+)\s*/\s*(?P<{0}d>\d+)|(?P<{0}i>\d+(?:\.\d+)?)"
_INCHES: Final = re.compile(rf"^\s*(?:{_FRACTION.format('a')})\s*(?:\"|in\.?|inches?)?\s*$")
_FEET: Final = re.compile(rf"^\s*(?P<ft>\d+)\s*'\s*-?\s*(?:(?:{_FRACTION.format('b')})\s*\"?)?\s*$")
_BRACKET: Final = re.compile(r"\[([^\[\]]+)\]")


def _number(match: re.Match[str], tag: str) -> Fraction | None:
    whole, num, den, plain = (match.group(f"{tag}{k}") for k in ("w", "n", "d", "i"))
    if plain is not None:
        return Fraction(plain)
    if num is None or den is None or int(den) == 0:
        return None
    return Fraction(int(whole or 0)) + Fraction(int(num), int(den))


def _plain_inches(text: str, *, need_mark: bool) -> Fraction | None:
    feet = _FEET.match(text)
    if feet is not None:
        rest = (
            _number(feet, "b")
            if feet.group("bn") is not None or feet.group("bi") is not None
            else Fraction(0)
        )
        return None if rest is None else Fraction(int(feet.group("ft"))) * 12 + rest
    inches = _INCHES.match(text)
    if inches is None:
        return None
    if need_mark and not re.search(r"\"|in\.?\s*$|inch", text):
        return None
    return _number(inches, "a")


def parse_inches(printed: str | None) -> Fraction | None:
    """The exact inches a printed dimension states, or `None` when it states none code can read.

    `610 [24]` and `1067 [42]` give the bracketed inches; `31"`, `25 1/2"`, `5'-0"`, `2'-10 1/2"`
    read as written. A bare `787` (no inch mark, no brackets) is not inches; two bracketed values
    are refused."""
    if printed is None:
        return None
    text = printed.translate(_QUOTES).strip()
    if not text:
        return None
    brackets = _BRACKET.findall(text)
    if len(brackets) > 1:
        return None
    if brackets:
        return _plain_inches(brackets[0], need_mark=False)
    return _plain_inches(text, need_mark=True)


@dataclass(frozen=True, slots=True)
class ArchitectListed:
    """One architect dimension code read and listed in the question."""

    ref: str
    """`A1`, `A2`, ... as listed."""
    text: str
    inches: Fraction | None
    """Exact; `None` when code holds the label (it is listed as not read)."""


@dataclass(frozen=True, slots=True)
class CheckedRow:
    ref: str
    architect_text: str
    architect_inches: Fraction | None
    measures: str
    vendor_text: str | None
    vendor_inches: Fraction | None
    result: Result
    ai_result: str | None
    """What the AI said (`SAME` ...), or `None` when it gave no row."""
    why: str
    vendor_box: tuple[int, int, int, int] | None
    """On the vendor image, 0..1000, when plausible."""


@dataclass(frozen=True, slots=True)
class Dropped:
    architect_id: str
    reason: str


def plausible_box(box: object) -> tuple[int, int, int, int] | None:
    """A vendor box kept only when it is four whole numbers, a real rectangle inside 0..1000, and
    small enough to be one printed number."""
    if not isinstance(box, Sequence) or isinstance(box, str) or len(box) != 4:
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in box):
        return None
    x0, y0, x1, y1 = (int(v) for v in box)
    if not (0 <= x0 < x1 <= _BOX_SCALE and 0 <= y0 < y1 <= _BOX_SCALE):
        return None
    if x1 - x0 > _MAX_BOX_SIDE or y1 - y0 > _MAX_BOX_SIDE:
        return None
    return x0, y0, x1, y1


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _decide(
    listed: ArchitectListed, row: Mapping[str, object]
) -> tuple[Result, str, str | None, Fraction | None]:
    said = _text(row.get("result")).upper()
    vendor_text = _text(row.get("vendor_text")) or None
    why = _text(row.get("why"))
    vendor = parse_inches(vendor_text)
    if said == "NOT_SHOWN":
        return "not_shown", why or "The vendor does not show this span.", vendor_text, None
    if said == "UNREADABLE":
        return "unreadable", why or "The vendor's number here cannot be read.", vendor_text, None
    if said not in ("SAME", "DIFFERENT"):
        return (
            "unsure",
            why or "The AI is not sure which vendor number this is.",
            vendor_text,
            vendor,
        )
    if listed.inches is None:
        return "unsure", "The architect's label is not read exactly by code.", vendor_text, vendor
    if vendor is None:
        return (
            "unsure",
            "The vendor's number as the AI read it does not state exact inches.",
            vendor_text,
            None,
        )
    code: Result = "same" if vendor == listed.inches else "different"
    if code.upper() != said:
        return (
            "unsure",
            f"The AI said {said.lower()}, but the numbers as read say {code}: check by eye.",
            vendor_text,
            vendor,
        )
    return (
        code,
        why or ("Same number." if code == "same" else "Different number."),
        vendor_text,
        vendor,
    )


def check_rows(
    listed: Sequence[ArchitectListed], rows: Sequence[Mapping[str, object]]
) -> tuple[list[CheckedRow], list[Dropped]]:
    """Every listed architect dimension exactly once, decided by code, and the AI rows dropped."""
    by_ref = {item.ref: item for item in listed}
    chosen: dict[str, Mapping[str, object]] = {}
    dropped: list[Dropped] = []
    for row in rows:
        ref = _text(row.get("architect_id")).upper()
        item = by_ref.get(ref)
        if item is None:
            dropped.append(Dropped(ref or "?", "not an architect dimension code read"))
            continue
        if ref in chosen:
            dropped.append(Dropped(ref, "a second answer for the same architect dimension"))
            continue
        echoed = parse_inches(_text(row.get("architect_text")))
        if item.inches is not None and echoed != item.inches:
            dropped.append(Dropped(ref, "its architect number is not the one code read"))
            continue
        chosen[ref] = row
    out: list[CheckedRow] = []
    for item in listed:
        found = chosen.get(item.ref)
        if found is None:
            out.append(
                CheckedRow(
                    item.ref,
                    item.text,
                    item.inches,
                    "",
                    None,
                    None,
                    "unsure",
                    None,
                    "The AI gave no usable answer for this architect dimension.",
                    None,
                )
            )
            continue
        row = found
        result, why, vendor_text, vendor = _decide(item, row)
        out.append(
            CheckedRow(
                item.ref,
                item.text,
                item.inches,
                _text(row.get("measures")),
                vendor_text,
                vendor if result in ("same", "different", "unsure") else None,
                result,
                _text(row.get("result")).upper() or None,
                why,
                plausible_box(row.get("vendor_box")) if vendor_text else None,
            )
        )
    return out, dropped
