"""A label's value from its printed text, including the two worded forms the admin approved (#992).

**Only text two sources printed identically reaches this module** (`seal.py`), and nothing here is a
model: it is exact arithmetic on characters. What it accepts, and nothing else:

- **A plain dimension** — digits, a fraction, inch and foot marks — parsed by `units/` (unchanged
  from #987).
- **Two numbers, one in brackets** — `648 [25 1/2]` (mm [in], as GV's vendors print) or `25 [635]`
  (in [mm]) — read as the inch number, on either side, only where the two corroborate: one is the
  other times 25.4, for GV's mm-first order within 3 mm (Anant, 2026-10-15) and both printed
  roundings, and for inches first within the mm's own last digit (#1263). Otherwise held. Millimetres only choose which number is the
  inches; they are never the value (CLAUDE.md, the client lead's Q12).
- **A sum, `a"+b"`**, optionally followed by the word `Filler` (the vendor prints `4"+1" Filler` for
  a filler made of two strips). One piece whose width is a + b exactly. Every part must carry its
  own inch mark and parse alone through `units/`; a bare `4+1"` is not read as inches.
- **Equal shares, `N"(K EQ)`** — K equal cabinets that together are N wide. One row entry of width
  N: the countertop's width needs only the total, and each share is never offered as a cabinet.

**Two words hold the whole countertop for the person** (admin, 2026-10-07): `INCLUDING FIELD CUT`
(the printed width already contains the allowance CT-WIDTH-001 would add again) and `VIF` (the
dimension is provisional until verified in the field). They are looked for in *every* text any
source gave for any label of the row — the file's own, either reader's — because holding is always
safe and a word one source missed is still on the drawing.

Any other word, a sum without inch marks, a share count below two: not expanded, so the person
decides, as before.

Source: issues #992, #1263 · Verification: `tests/extraction/slot_reader/test_labels.py`,
`tests/extraction/slot_reader/test_inch_mm_pair.py`
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Final

from units.measurement import Measurement, Unit
from units.normalise import UnitNormalisationError, drawn_fraction_refusal, normalise_to_inches
from units.notation import canonical_notation, is_compound

__all__ = [
    "COUNTER_BREAK",
    "FIELD_CUT_INCLUDED",
    "GV_ORDER_MM_MATCH",
    "PAIR_DISAGREES",
    "VIF",
    "ExpandedLabel",
    "Expansion",
    "RowHold",
    "counter_break_hold",
    "expand_label",
    "is_inch_mm_pair",
    "mm_inch_refusal",
    "odd_fraction",
    "plain_dimension",
    "row_hold",
]

#: A plain dimension of one number: digits, spaces, a fraction slash, inch and foot marks, a dash and
#: a decimal point. Anything else is words, a sum, a count, or a two-number label (`_PAIR`).
_PLAIN: Final = re.compile(r"[0-9 /\"'\-.]+")

#: One number of a two-number label: whole, decimal, a fraction, or a whole and a fraction joined by
#: a space or the trade's hyphen (`24-3/4`).
_PAIR_NUMBER: Final = r"[0-9]+(?:\.[0-9]+)?(?:[ \-][0-9]+/[0-9]+)?|[0-9]+/[0-9]+"
#: A label of two numbers, one outside brackets and one inside, each with an optional inch mark or
#: the word mm: `610 [24]`, `648 [25 1/2]`, `25 [635]`, `25" [635]`, `635 mm [25"]` (#1263).
_PAIR: Final = re.compile(
    rf"\s*(?P<outside>{_PAIR_NUMBER})\s*(?P<outside_mark>\"|mm)?\s*"
    rf"\[\s*(?P<inside>{_PAIR_NUMBER})\s*(?P<inside_mark>\"|mm)?\s*\]\s*",
    re.IGNORECASE,
)
#: 25.4 mm to the inch, exactly.
_MM_PER_INCH: Final = Fraction(254, 10)
#: GV's order, mm first (`648 [25 1/2]`): the printed mm must be within this many mm of 25.4 times
#: the printed inches, inclusive, and within both printed roundings (`_gv_order_allowance`). **Decided by Anant, 2026-10-15** (#1263 review). Wide enough for
#: the vendor printing the inch to its fraction (a whole-inch label is often a millimetre or two
#: off), narrow enough that a lost stacked 1/2" (12.7 mm) is held, not read as the whole inch.
GV_ORDER_MM_MATCH: Final = Fraction(3)
#: Why a two-number label whose numbers do not corroborate gives no value (#1263).
PAIR_DISAGREES: Final = (
    "the millimetres and inches printed together do not match (one is not the other times 25.4), "
    "so neither is used"
)

#: One part of a sum, or the total of equal shares: a number written with its inch mark.
_INCH_PART: Final = r"[0-9][0-9 /.\-]*\""
_SUM: Final = re.compile(
    rf"(?P<parts>{_INCH_PART}(?:\s*\+\s*{_INCH_PART})+)(?:\s*(?P<word>[A-Za-z]+))?"
)
_SHARES: Final = re.compile(
    rf"(?P<total>{_INCH_PART})\s*\(\s*(?P<count>[0-9]+)\s*EQ\s*\)", re.IGNORECASE
)
#: The one word a sum may carry: it names the piece's kind and changes nothing about its width.
_SUM_WORDS: Final = frozenset({"FILLER"})

_FIELD_CUT: Final = re.compile(r"(?<![A-Za-z])INCLUDING\s+FIELD\s+CUT(?![A-Za-z])", re.IGNORECASE)
_VIF: Final = re.compile(r"(?<![A-Za-z])VIF(?![A-Za-z])", re.IGNORECASE)
_COUNTER_BREAK: Final = re.compile(
    r"(?<![A-Za-z0-9])(?:REFRIGERATOR|FRIDGE|WASHER|DRYER|RANGE|STOVE|PANTRY|OVEN|TALL|REF|W/D)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_UNDERCOUNTER: Final = re.compile(
    r"(?<![A-Za-z0-9])(?:UNDER(?:-|\s)?COUNTER|U/C|UC)(?![A-Za-z0-9])", re.IGNORECASE
)
_MICROWAVE_OVEN: Final = re.compile(
    r"(?<![A-Za-z0-9])(?:MICROWAVE|MW)\s+OVEN(?![A-Za-z0-9])", re.IGNORECASE
)

FIELD_CUT_INCLUDED: Final = ("field-cut-included", "width already includes the field cut")
VIF: Final = ("vif", "VIF: provisional, verify in field")
COUNTER_BREAK: Final = (
    "counter-break",
    "this row includes a tall appliance or range bay; it may be the wall-to-wall line, not the countertop",
)


def _printed_step(number: str) -> Fraction:
    """The smallest unit a number was printed in: 1 for `25`, 1/10 for `25.4`, 1/8 for `24 3/8`."""
    if "/" in number:
        return Fraction(1, int(number.rsplit("/", 1)[1]))
    _, point, places = number.partition(".")
    return Fraction(1, 10 ** len(places)) if point else Fraction(1)


def _number(number: str) -> Fraction:
    whole, _, fraction = number.replace("-", " ").partition(" ")
    if "/" in whole:
        whole, fraction = "0", whole
    return Fraction(whole) + (Fraction(fraction) if fraction else 0)


def _both_roundings(millimetres: str, inches: str) -> Fraction:
    """How far apart two honest printings of one length can be: each number is the length rounded
    to the step it is printed in, so half the mm's step plus half the inch's step in mm (the
    allowance `units.policy.check_dual` uses, and the slot reader used before #1263)."""
    return _printed_step(millimetres) / 2 + _MM_PER_INCH * _printed_step(inches) / 2


def _gv_order_allowance(millimetres: str, inches: str) -> Fraction:
    """GV's order, mm first: **the smaller of 3 mm and both printed roundings.**

    3 mm is Anant's decision (2026-10-15): it holds a lost stacked 1/2" that both roundings
    (13.2 mm for a whole inch) let through. Both roundings are the tighter bound for a fraction
    under 1/4" (1/8": 2.09 mm), where 3 mm would let a stacked `7 3/8"` split as `7 [3/8]` read as
    3/8". The smaller of the two is strictly tighter than either, so no label the reader held
    before #1263 for its mm becomes a value (coordinator, 2026-10-15, under Anant's "tight").
    """
    return min(GV_ORDER_MM_MATCH, _both_roundings(millimetres, inches))


def _corroborates(millimetres: str, inches: str, *, gv_order: bool) -> bool:
    """Whether the printed mm is the printed inches times 25.4, within the reading's allowance.

    * **GV's order, mm first** (`648 [25 1/2]`): within the smaller of `GV_ORDER_MM_MATCH` (3 mm,
      Anant's decision of 2026-10-15) and `_both_roundings` (`_gv_order_allowance`).
    * **Inches first** (`25 [635]`): within half the mm's last printed digit, the mm's own rounding
      and nothing more. A GV label whose mm a reader cut short (`1372 [54]` read as `2 [54]`) must
      never pass as two inches beside 54 mm: 54 is not 50.8 to the millimetre.

    Exact fractions; a fraction over 0 is no number.
    """
    try:
        allowance = (
            _gv_order_allowance(millimetres, inches) if gv_order else _printed_step(millimetres) / 2
        )
        return abs(_number(millimetres) - _MM_PER_INCH * _number(inches)) <= allowance
    except ZeroDivisionError:
        return False


def _pair(text: str, allow_explicit_mm: bool) -> re.Match[str] | None:
    """`text` as a two-number label; the word `mm` only where `allow_explicit_mm` (the Claude pair)."""
    pair = _PAIR.fullmatch(text)
    if pair is None:
        return None
    marks = {(pair.group(side) or "").lower() for side in ("outside_mark", "inside_mark")}
    if "mm" in marks and not allow_explicit_mm:
        return None
    return pair


def _pair_inches(pair: re.Match[str]) -> str | None:
    """The inch half of a two-number label as printed, where exactly one reading corroborates.

    The two readings: the number outside is mm and the one inside inches (`648 [25 1/2]`, as GV's
    vendors print), or the other way round (`25 [635]`). A reading is out where the side it calls
    inches says `mm`, or the side it calls mm has an inch mark or a fraction, where the inches are
    not more than 0 (a 0" width is never a value), or where the numbers do not corroborate
    (`_corroborates`, with its own allowance per reading). Neither or both: `None`. Millimetres
    only ever choose which number is the inches; they are never the value.
    """
    found: list[str] = []
    for mm_side, inch_side in (("outside", "inside"), ("inside", "outside")):
        millimetres, inches = pair.group(mm_side), pair.group(inch_side)
        mm_mark = (pair.group(f"{mm_side}_mark") or "").lower()
        inch_mark = (pair.group(f"{inch_side}_mark") or "").lower()
        if mm_mark == '"' or inch_mark == "mm" or "/" in millimetres:
            continue
        try:
            if _number(inches) <= 0:
                continue
        except ZeroDivisionError:
            continue
        if _corroborates(millimetres, inches, gv_order=mm_side == "outside"):
            found.append(inches)
    return found[0] if len(found) == 1 else None


def _as_inches(number: str) -> str:
    """One number of a pair, written as inches for `units/`: `24-3/4` as `24 3/4"`."""
    return canonical_notation(f'{number}"')[0]


def _notation(text: str, allow_explicit_mm: bool) -> str | None:
    """The text as `normalise_to_inches` parses it, or `None` where it is no plain dimension.

    **A two-number label is its inch half, and only where the pair corroborates** (#1263). Before,
    the bracket was always the inches, so `25 [635]` (inches, then mm) was 635 inches, and a pair
    whose numbers disagree (`305 [10]`) was 10. Any other text with a bracket is none.
    """
    if not text or is_compound(text):
        return None
    pair = _pair(text, allow_explicit_mm)
    if pair is not None:
        inches = _pair_inches(pair)
        return None if inches is None else _as_inches(inches)
    if _PLAIN.fullmatch(text) is None:
        return None
    return canonical_notation(text)[0]


def plain_dimension(text: str, *, allow_explicit_mm: bool = False) -> Measurement | None:
    """The exact value of a plain dimension's text, or `None` where it is not one.

    `None` for text with anything but the characters of a dimension, for a compound, for text
    `units/` cannot give a unit — a bare `30` is not assumed to be inches — and for a fraction no
    drawing prints (`163/8"`, `5/4"`, `3/5"`; #1199): read as written, a squeezed `16 3/8"` would be
    twenty and three-eighths.

    **A two-number label** — `648 [25 1/2]`, `25 [635]`, `25" [635]` — is its inch number, on either
    side, only where the two numbers corroborate; otherwise `None`, and `mm_inch_refusal` says why
    (#1263). The word `mm` is read only with `allow_explicit_mm`. Millimetres never give the value.

    `text` is the agreed text after `seal.normalise_text`, which has already folded `½` into `1/2`,
    so the character gate below never sees a fraction character.
    """
    try:
        notation = _notation(text, allow_explicit_mm)
        if notation is None or drawn_fraction_refusal(notation) is not None:
            return None
        value = normalise_to_inches(notation)
    except (UnitNormalisationError, TypeError, ValueError, ArithmeticError):
        return None
    return Measurement(value.exact, value.unit, text)


def mm_inch_refusal(text: str, *, allow_explicit_mm: bool = False) -> str | None:
    """Why a two-number label gives no value: its numbers do not corroborate (`305 [10]`; #1263).

    `None` for any text that is not a two-number label, and for one that corroborates. The word `mm`
    is read only with `allow_explicit_mm`, as `plain_dimension` reads it.
    """
    pair = _pair(text, allow_explicit_mm)
    if pair is None or _pair_inches(pair) is not None:
        return None
    return PAIR_DISAGREES


def is_inch_mm_pair(text: str, *, allow_explicit_mm: bool = False) -> bool:
    """Whether `text` is a two-number label whose numbers corroborate, in either order."""
    pair = _pair(text, allow_explicit_mm)
    return pair is not None and _pair_inches(pair) is not None


def odd_fraction(text: str, *, allow_explicit_mm: bool = False) -> str | None:
    """Why a text that is a plain dimension in every other way has a fraction no drawing prints.

    `163/8"`, `5/4"`, `3/5"`, `16 3/5"`: the reason (`units.normalise.drawn_fraction_refusal`), so
    the person is told what is wrong (#1199). `None` for text that is no plain dimension anyway —
    `457 / 18"`, a bare `24/36`, words — which stays "not plain", and for a fraction a drawing prints.
    In a two-number label, either number's fraction (`78 [31/8"]`), before asking which is the inch.
    """
    if not text or is_compound(text):
        return None
    pair = _pair(text, allow_explicit_mm)
    if pair is not None:
        notations = [_as_inches(pair.group(side)) for side in ("outside", "inside")]
    elif _PLAIN.fullmatch(text) is not None:
        notations = [canonical_notation(text)[0]]
    else:
        return None
    for notation in notations:
        try:
            normalise_to_inches(notation)
        except (UnitNormalisationError, TypeError, ValueError, ArithmeticError):
            continue
        refusal = drawn_fraction_refusal(notation)
        if refusal is not None:
            return refusal
    return None


class Expansion(StrEnum):
    SUM = "sum"
    EQUAL_SHARES = "equal-shares"


@dataclass(frozen=True, slots=True)
class ExpandedLabel:
    """A worded or summed label's one value, and how it was reached."""

    value: Measurement
    how: Expansion
    parts: tuple[Fraction, ...]
    """The sum's parts in inches, left to right; for equal shares, the total alone."""
    shares: int | None
    """K in `(K EQ)`; `None` for a sum."""


def _inches(part: str) -> Fraction | None:
    value = plain_dimension(part.strip())
    if value is None or value.unit is not Unit.INCH:
        return None
    return value.exact


def expand_label(text: str) -> ExpandedLabel | None:
    """The value of an agreed `a"+b"[ Filler]` or `N"(K EQ)`, exactly; `None` for anything else.

    `text` is the agreed text after `seal.normalise_text` (quote glyphs to `"`, whitespace runs to one
    space). A text holding `INCLUDING FIELD CUT` or `VIF` is never expanded: `row_hold` holds it.
    """
    text = text.strip()
    if not text or row_hold([text]) is not None:
        return None
    total = _SUM.fullmatch(text)
    if total is not None:
        word = total.group("word")
        if word is not None and word.upper() not in _SUM_WORDS:
            return None
        parts: list[Fraction] = []
        for part in total.group("parts").split("+"):
            value = _inches(part)
            if value is None or value <= 0:
                return None
            parts.append(value)
        return ExpandedLabel(
            Measurement(sum(parts, Fraction(0)), Unit.INCH, text), Expansion.SUM, tuple(parts), None
        )
    shares = _SHARES.fullmatch(text)
    if shares is not None:
        count = int(shares.group("count"))
        value = _inches(shares.group("total"))
        if value is None or value <= 0 or count < 2:
            return None
        return ExpandedLabel(
            Measurement(value, Unit.INCH, text), Expansion.EQUAL_SHARES, (value,), count
        )
    return None


@dataclass(frozen=True, slots=True)
class RowHold:
    """Why a whole countertop row goes to the person, whatever else sealed."""

    code: str
    reason: str
    text: str
    """The text it was found in, as the source gave it."""


def counter_break_hold(phrases: Iterable[str]) -> RowHold | None:
    """Hold a row for a tall-appliance bay, but not an undercounter or microwave-oven phrase.

    The caller groups vendor words into phrases on one line before calling this. A model's text
    never supplies an unlocated word from another drawing.
    """
    for phrase in phrases:
        text = " ".join(phrase.split())
        if not text or _UNDERCOUNTER.search(text):
            continue
        without_microwave_oven = _MICROWAVE_OVEN.sub(" ", text)
        if _COUNTER_BREAK.search(without_microwave_oven):
            return RowHold(*COUNTER_BREAK, phrase)
    return None


def row_hold(texts: Iterable[str]) -> RowHold | None:
    """`INCLUDING FIELD CUT` first, then `VIF`, in any of the texts; `None` when neither appears."""
    seen = [" ".join(text.split()) for text in texts if text]
    for pattern, (code, reason) in ((_FIELD_CUT, FIELD_CUT_INCLUDED), (_VIF, VIF)):
        for text in seen:
            if pattern.search(text):
                return RowHold(code, reason, text)
    return None
