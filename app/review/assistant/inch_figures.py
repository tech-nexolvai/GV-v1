"""A vendor string whose millimetres add up but whose inch figures do not (option A, 2026-11-03).

A metric drawing prints each size in millimetres with its inches in brackets: `36 [1 1/2]`. The
vendor's CAD rounds each bracket on its own, so a string can add up in millimetres
(`36 + 436 + 36 = 508`) while its inch figures do not (`1 1/2 + 17 1/4 + 1 1/2 = 20 1/4` against
the printed `[20]`). Such a string stays red (settled with GV: inches count, millimetres are
ignored, exact match; GV's own reviewer corrected exactly this on a keyed sheet, writing the middle
figure 78 5/8 so the inches add up). Only its words change: they say plainly that the millimetres
add up and point at the inch figures, never at the overall.

`of_finding` says when a finding is such a string, from code's own labels of the row when the
finding carries them (`PRINTED_STRING`, set where code reads the row), else from its own parts.
It applies only when every value is millimetres first with its inches in brackets, the inch
figures do not add up, and the millimetres add up exactly, or within the vendor's own millimetre
rounding (each printed value is off its true size by at most half its last digit, so the parts'
sum may miss the overall by (parts + 1) halves; never 3 mm or more, under 1/8") on a drawing that
works in millimetres (at least two values whose millimetres are not just their inches converted).
An inch-only string, or one whose millimetres do not add up, keeps today's words.

A figure to change is named only when code can derive it from the millimetres alone: exactly one
part whose bracket, moved by the miss, makes the figures add up and is still a rounding of its own
millimetres at the drawing's step. It is a suggestion ("e.g. 6 3/4 to 6 7/8"), never proof, and
never one of two parts printed alike.

Pure code: no I/O, no AI call.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final

from app.review.assistant.finding_checks import inch_first_mm, to_inches, wrong_part

__all__ = [
    "PRINTED_STRING",
    "InchFigures",
    "figures",
    "of_finding",
    "printed_string",
]

#: A finding's field: code's own labels of a vendor string's row, as printed
#: (`{"parts": ["36 [1 1/2]", ...], "total": "508 [20]"}`), set where code reads the row.
PRINTED_STRING: Final = "printed_string"

#: Millimetres first with the inches in brackets: `508 [20]`, `398.5 [15 3/4]`, `940 mm (37")`.
_MM_FIRST: Final = re.compile(
    r"^(?P<mm>\d+(?:\.\d+)?)\s*(?:mm\.?)?\s*[\[(]\s*(?P<inches>[^\])]+?)\s*[\])]$", re.IGNORECASE
)
_MARKED: Final = re.compile(r"""["']""")
_MM_PER_INCH: Final = Decimal("25.4")
#: A millimetre miss this big is never called rounding: just under 1/8" (3.175 mm), the smallest
#: inch miss GV marks (exact match, flag everything).
_MM_CAP: Final = Decimal(3)
#: The brackets' step is never taken coarser than this when a drawing prints whole inches only.
_COARSEST: Final = Fraction(1, 4)


@dataclass(frozen=True, slots=True)
class _Value:
    mm_text: str
    mm: Decimal
    inches: Fraction


def _value(text: object) -> _Value | None:
    """A value printed millimetres first with its inches in brackets, else None (inches first,
    one unit, or not a number)."""
    if not isinstance(text, str):
        return None
    token = " ".join(text.replace("″", '"').replace("”", '"').split())
    if inch_first_mm(token) is not None:
        return None  # `31 1/4" [794]`: the inches are the drawing's own numbers
    found = _MM_FIRST.match(token)
    if found is None:
        return None
    inside = found.group("inches").strip()
    inches = to_inches(inside if _MARKED.search(inside) else f'{inside}"')
    if inches is None or inches <= 0:
        return None
    return _Value(found.group("mm"), Decimal(found.group("mm")), Fraction(inches))


def _bare(value: Fraction) -> str:
    """Inches as a bracket prints them: `20 1/4`, `20`, `3/4`."""
    whole, rest = divmod(value.numerator, value.denominator)
    if rest == 0:
        return str(whole)
    fraction = f"{rest}/{value.denominator}"
    return f"{whole} {fraction}" if whole else fraction


def _mm_words(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True, slots=True)
class InchFigures:
    """A vendor string whose millimetres add up while its inch figures do not."""

    mm_parts: tuple[str, ...]
    """Each part's millimetres as printed."""
    mm_total: str
    """The overall's millimetres as printed."""
    mm_sum: Decimal
    inch_parts: tuple[Fraction, ...]
    inch_total: Fraction
    """The printed overall's inch figure: what the inch figures must add up to."""
    suggestion: tuple[Fraction, Fraction] | None = None
    """One figure to change and to what, when the millimetres alone say so (`figures`)."""

    @property
    def exact(self) -> bool:
        return self.mm_sum == Decimal(self.mm_total)

    @property
    def inch_sum(self) -> Fraction:
        return sum(self.inch_parts, Fraction(0))

    @property
    def target(self) -> str:
        """The printed overall's inch figure as printed: `[20]`."""
        return f"[{_bare(self.inch_total)}]"

    @property
    def inch_sum_words(self) -> str:
        """`20 1/4`."""
        return _bare(self.inch_sum)

    def mm_line(self) -> str:
        """`36 + 436 + 36 = 508 mm`, or, within rounding, `30 + 969 + 30 = 1029 mm, printed
        1031 mm`."""
        summed = f"{' + '.join(self.mm_parts)} = {_mm_words(self.mm_sum)} mm"
        return summed if self.exact else f"{summed}, printed {_mm_words(Decimal(self.mm_total))} mm"

    def mm_note(self) -> str:
        """`36 + 436 + 36 = 508 mm (adds up)`, or `... (adds up within rounding)`."""
        return f"{self.mm_line()} ({'adds up' if self.exact else 'adds up within rounding'})"

    def mm_words(self) -> str:
        """`The millimetres add up (36 + 436 + 36 = 508 mm)`."""
        how = "add up" if self.exact else "add up within their own rounding"
        return f"The millimetres {how} ({self.mm_line()})"

    def inch_parts_words(self) -> str:
        """`1 1/2 + 17 1/4 + 1 1/2`."""
        return " + ".join(_bare(part) for part in self.inch_parts)

    def inch_line(self) -> str:
        """`1 1/2 + 17 1/4 + 1 1/2 = 20 1/4 vs [20]`."""
        return f"{self.inch_parts_words()} = {self.inch_sum_words} vs {self.target}"

    def suggestion_words(self) -> str:
        """`e.g. 6 3/4 to 6 7/8`, or "" when the millimetres name no one figure."""
        if self.suggestion is None:
            return ""
        was, now = self.suggestion
        return f"e.g. {_bare(was)} to {_bare(now)}"

    def correct_words(self) -> str:
        """`Correct the inch figures so they add up to the printed [20] (e.g. 6 3/4 to 6 7/8).`"""
        hint = self.suggestion_words()
        return (
            f"Correct the inch figures so they add up to the printed {self.target}"
            + (f" ({hint})" if hint else "")
            + "."
        )

    def sentence(self) -> str:
        """The whole plain sentence (`proof_words.sentence_runs` prints it with its colours)."""
        return (
            f"{self.mm_words()}, but the inch figures do not ({self.inch_line()}). "
            f"{self.correct_words()}"
        )

    def should_words(self) -> str:
        """The green should-be: `inch figures adding up to [20]`, with the suggestion."""
        hint = self.suggestion_words()
        return f"inch figures adding up to {self.target}" + (f" ({hint})" if hint else "")


def _converted(inches: Fraction) -> Decimal:
    """Inches in millimetres, unrounded."""
    return Decimal(inches.numerator) / Decimal(inches.denominator) * _MM_PER_INCH


def _step(values: Sequence[_Value], miss: Fraction) -> Fraction:
    """The brackets' rounding step: the finest the string prints (or its miss), never coarser
    than `_COARSEST`."""
    finest = max([_COARSEST.denominator, miss.denominator, *(v.inches.denominator for v in values)])
    return Fraction(1, finest)


def _suggestion(
    parts: Sequence[_Value], total: _Value, mm_half: Decimal
) -> tuple[Fraction, Fraction] | None:
    """The one part whose bracket, moved by the miss, makes the figures add up and is still a
    rounding of its own millimetres at the drawing's step (its millimetres may be off their true
    size by `mm_half`); None when no part or more than one part does, or the one is printed alike
    with another part."""
    miss = total.inches - sum((p.inches for p in parts), Fraction(0))
    step = _step([*parts, total], abs(miss))
    allowance = Fraction(step) / 2 + Fraction(mm_half) / Fraction(_MM_PER_INCH)
    fits = [
        index
        for index, part in enumerate(parts)
        if part.inches + miss > 0
        and abs(part.inches + miss - Fraction(part.mm) / Fraction(_MM_PER_INCH)) <= allowance
    ]
    if len(fits) != 1:
        return None
    chosen = parts[fits[0]]
    if sum(1 for p in parts if (p.mm, p.inches) == (chosen.mm, chosen.inches)) > 1:
        return None  # two parts printed alike: code cannot say which one
    return chosen.inches, chosen.inches + miss


def figures(parts: Sequence[object], total: object) -> InchFigures | None:
    """The string as `InchFigures` when its millimetres add up and its inch figures do not (see
    the module), else None."""
    if isinstance(parts, str) or len(parts) < 2:
        return None
    values = [_value(part) for part in parts]
    overall = _value(total)
    if overall is None or any(v is None for v in values):
        return None
    read = [v for v in values if v is not None]
    if sum((v.inches for v in read), Fraction(0)) == overall.inches:
        return None  # the inch figures add up: nothing to say
    places = max(len(v.mm_text.partition(".")[2]) for v in [*read, overall])
    mm_half = Decimal("0.5") / (Decimal(10) ** places)
    mm_sum = sum((v.mm for v in read), Decimal(0))
    miss = abs(overall.mm - mm_sum)
    if miss:
        allowance = min((len(read) + 1) * mm_half, _MM_CAP)
        # Millimetres that are just their inches converted say nothing more than the inches.
        own = sum(1 for v in [*read, overall] if abs(v.mm - _converted(v.inches)) > mm_half)
        if miss > allowance or own < 2:
            return None
    return InchFigures(
        mm_parts=tuple(v.mm_text for v in read),
        mm_total=overall.mm_text,
        mm_sum=mm_sum,
        inch_parts=tuple(v.inches for v in read),
        inch_total=overall.inches,
        suggestion=_suggestion(read, overall, mm_half),
    )


def printed_string(finding: Mapping[str, Any]) -> tuple[list[str], str]:
    """A vendor string's parts and overall as printed: code's own labels of its row when the
    finding carries them (`PRINTED_STRING`), else its own parts and printed total."""
    printed = finding.get(PRINTED_STRING)
    if isinstance(printed, Mapping):
        parts = printed.get("parts")
        total = printed.get("total")
        if isinstance(parts, list) and parts and isinstance(total, str) and total:
            return [str(p) for p in parts], total
    raw = finding.get("parts")
    parts = [str(p) for p in raw] if isinstance(raw, list) else []
    return parts, str(finding.get("printed_total") or "")


def of_finding(finding: Mapping[str, Any]) -> InchFigures | None:
    """A vendor string that does not add up in inches while its millimetres do (see the module);
    None for any other finding, and for a string whose drawing names the wrong part (that part's
    own words stand)."""
    if finding.get("source") != "vendor_arithmetic" or wrong_part(finding) is not None:
        return None
    parts, total = printed_string(finding)
    return figures(parts, total)
