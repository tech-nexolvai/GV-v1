"""The vendor's field cut, recognised by code: never a design difference. Code, $0, no AI.

Settled with GV (Raj): a countertop gets a **field cut**, about 1" of extra stone at each end that
meets a wall, added at the factory and trimmed on site (GV's company standard is 1" per wall end; a
project value replaces it, `rules.md`'s `field_cut`). GV's own countertop drawings print it: a 1"
dimension at the top's wall end with the words "FIELD CUT" on the drawing, and an overall that
includes it (a 48" top + 1" field cut = 49").

The 2026-10-23 client meeting showed what goes wrong when code does not know this: the whole-drawing
look paired the vendor's 1" field cut with the reference's 1" overhang past the last cabinet at the
other end and raised a Likely problem, and a helper matched the same two 1" values as Right. They
measure different things. So:

- **A field-cut dimension** (`page_field_cuts`): a vendor label printing the field cut per wall end
  (exactly), on a drawing that prints the words FIELD CUT, and either those words sit near the
  label (`NEAR_PT`) or the label goes with a stacked pair of the run's sizes that differ by exactly
  one field cut (the 49" overall over the 48" top). Nothing else counts: a 1" reveal on a drawing
  with no FIELD CUT words is an ordinary dimension.
- **Never compared with the reference** (`vet`): an item that sets a field-cut label against an
  architect's (or the brand prototype's) value is refused; the whole-drawing look drops such a
  claim. The run's log says why; nobody sees it.
- **The top's length allows it** (`vet`): an overall that includes field cuts (the stacked pair
  above) is the same as the architect's when it is the architect's value plus one field cut per
  wall end the drawing shows. That is GV's settled rule, not a difference.

Pure: plain values in, plain values out. Verification: `tests/review/assistant/test_field_cut.py`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Final, Literal

__all__ = [
    "DEFAULT_PER_END",
    "FIELD_CUT_WORDS",
    "NEAR_PT",
    "PageFieldCuts",
    "Vetted",
    "field_cut_label_words",
    "page_field_cuts",
    "per_end_from",
    "vet",
]

#: GV's company standard: 1" per wall end (Raj; adopted 2026-10-07), until a project value is set.
DEFAULT_PER_END: Final = Fraction(1)
#: The words a field cut is printed with ("FIELD CUT", "FIELD-CUT", "field cut").
FIELD_CUT_WORDS: Final = re.compile(r"\bFIELD[\s\-]*CUT\b", re.IGNORECASE)
#: Words a field-cut label may carry beside FIELD CUT ("1" FIELD CUT AT WALL END, TYP.").
_LABEL_WORDS: Final = frozenset(
    {"incl", "including", "typ", "typical", "at", "wall", "end", "ends", "each", "on", "site"}
)
#: What is left of a phrase once FIELD CUT and its sizes are taken out.
_WORD: Final = re.compile(r"[A-Za-z]+")
#: The FIELD CUT words this close to a label (points, box to box) name it.
NEAR_PT: Final = 90.0
#: An overall and the part under it are printed this close (points, centre to centre).
STACK_PT: Final = 60.0
#: A field-cut label this close (points, centre to centre) to the run's stacked pair is its end.
RUN_PT: Final = 170.0
#: Sides a label of the vendor's own drawing may carry.
_NOT_VENDOR: Final = frozenset({"architect", "brand_prototype"})
#: The sources of an item that compares the vendor with another drawing.
_DRAWING_SOURCES: Final = frozenset({"architect", "brand_prototype", "gv_review"})

type Box = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class PageFieldCuts:
    """One vendor page's field-cut labels and the overalls that include field cuts."""

    per_end: Fraction = DEFAULT_PER_END
    labels: frozenset[str] = frozenset()
    """The ids of the labels that print the field cut itself."""
    overalls: Mapping[str, int] = field(default_factory=dict)
    """An overall's label id -> how many field cuts it includes (1 or 2)."""

    def __bool__(self) -> bool:
        return bool(self.labels or self.overalls)


@dataclass(frozen=True, slots=True)
class Vetted:
    """What the field-cut rule does with one item: refuse it, or allow it (the same)."""

    outcome: Literal["refuse", "allowed"]
    words: str
    field_cuts: int = 0
    per_end: Fraction = DEFAULT_PER_END

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "words": self.words,
            "field_cuts": self.field_cuts,
            "per_end": _inches_text(self.per_end),
        }


def _inches_text(value: Fraction) -> str:
    whole, rest = divmod(value, 1)
    if not rest:
        return f'{whole}"'
    return f'{whole} {rest.numerator}/{rest.denominator}"' if whole else f'{rest}"'


def per_end_from(rules_md: str | None) -> Fraction:
    """The field cut per wall end in force: `rules.md`'s `field_cut` (a project value replaces GV's
    1"), else GV's company standard."""
    from app.review.assistant.sink_offset import setting_from

    found = setting_from(rules_md, "field_cut", "field cut")
    return found.value if found is not None else DEFAULT_PER_END


def field_cut_label_words(text: str) -> bool:
    """Whether a printed phrase is a field-cut label ("FIELD CUT", "1" FIELD CUT AT WALL", "FIELD
    CUT ON SITE"), not a sentence that only uses the words: a filler "to be field cut as required"
    is a filler, and a note "dimensions including field cut: ..." names no dimension of its own."""
    if not FIELD_CUT_WORDS.search(text):
        return False
    rest = FIELD_CUT_WORDS.sub(" ", text)
    words = [w.lower() for w in _WORD.findall(rest) if w.lower() not in ("mm", "in")]
    return all(word in _LABEL_WORDS for word in words)


def _fraction(value: object) -> Fraction | None:
    try:
        found = Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    return found


def _box(raw: object) -> Box | None:
    if not isinstance(raw, Sequence) or isinstance(raw, str) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _gap(a: Box, b: Box) -> float:
    dx = max(0.0, b[0] - a[2], a[0] - b[2])
    dy = max(0.0, b[1] - a[3], a[1] - b[3])
    return math.hypot(dx, dy)


def _centre_distance(a: Box, b: Box) -> float:
    return math.hypot((a[0] + a[2] - b[0] - b[2]) / 2, (a[1] + a[3] - b[1] - b[3]) / 2)


@dataclass(frozen=True, slots=True)
class _Printed:
    id: str
    inches: Fraction
    box: Box
    orientation: str | None


def _vendor_labels(labels: Sequence[Mapping[str, Any]]) -> list[_Printed]:
    out: list[_Printed] = []
    for label in labels:
        if not isinstance(label, Mapping) or str(label.get("side") or "") in _NOT_VENDOR:
            continue
        inches, box = _fraction(label.get("inches")), _box(label.get("box"))
        if inches is None or inches <= 0 or box is None or not label.get("id"):
            continue
        orientation = label.get("orientation")
        out.append(
            _Printed(
                str(label["id"]),
                inches,
                box,
                str(orientation) if orientation in ("h", "v") else None,
            )
        )
    return out


def _same_way(a: _Printed, b: _Printed) -> bool:
    return a.orientation is None or b.orientation is None or a.orientation == b.orientation


def _stacked(
    printed: Sequence[_Printed], per_end: Fraction, cuts: int
) -> list[tuple[_Printed, _Printed]]:
    """(overall, part) pairs printed close together whose values differ by `cuts` field cuts."""
    out: list[tuple[_Printed, _Printed]] = []
    for overall in printed:
        for part in printed:
            if (
                part is not overall
                and _same_way(overall, part)
                and overall.inches - part.inches == cuts * per_end
                and part.inches > per_end
                and _centre_distance(overall.box, part.box) <= STACK_PT
            ):
                out.append((overall, part))
    return out


def page_field_cuts(
    labels: Sequence[Mapping[str, Any]],
    phrases: Sequence[Mapping[str, Any]],
    per_end: Fraction = DEFAULT_PER_END,
) -> PageFieldCuts:
    """One vendor page's field cuts (see the module): `labels` and `phrases` as `dims.json` holds
    them for the page."""
    words = [
        box
        for phrase in phrases
        if isinstance(phrase, Mapping)
        and str(phrase.get("side") or "") not in _NOT_VENDOR
        and field_cut_label_words(str(phrase.get("text") or ""))
        and (box := _box(phrase.get("box"))) is not None
    ]
    if not words or per_end <= 0:
        return PageFieldCuts(per_end)
    printed = _vendor_labels(labels)
    one_cut = _stacked(printed, per_end, 1)
    found: set[str] = set()
    for label in printed:
        if label.inches != per_end:
            continue
        near = any(_gap(label.box, box) <= NEAR_PT for box in words)
        run = any(
            label is not overall
            and label is not part
            and _same_way(label, part)
            and min(_centre_distance(label.box, overall.box), _centre_distance(label.box, part.box))
            <= RUN_PT
            for overall, part in one_cut
        )
        if near or run:
            found.add(label.id)
    cut_labels = [label for label in printed if label.id in found]
    counts: dict[str, set[int]] = {}
    for cuts in (1, 2):
        for overall, part in _stacked(printed, per_end, cuts):
            if overall.id in found or part.id in found:
                continue
            close = sum(
                1
                for label in cut_labels
                if _same_way(label, part)
                and min(
                    _centre_distance(label.box, overall.box),
                    _centre_distance(label.box, part.box),
                )
                <= RUN_PT
            )
            if close >= cuts:
                counts.setdefault(overall.id, set()).add(cuts)
    # An overall two readings give different field cuts gets no allowance at all.
    overalls = {
        key: next(iter(found_counts))
        for key, found_counts in counts.items()
        if len(found_counts) == 1
    }
    return PageFieldCuts(per_end, frozenset(found), overalls)


def _architect_inches(dims: Mapping[str, Any], architect: object) -> Fraction | None:
    side = dims.get("architect")
    if isinstance(side, Mapping):
        return _fraction(side.get("inches"))
    from app.review.assistant.finding_checks import claim_value, to_inches

    claim = claim_value(" ".join(str(architect or "").split()))
    value = None if claim is None else to_inches(claim)
    return None if value is None else _fraction(value)


def vet(
    dims: Mapping[str, Any] | None,
    *,
    status: object,
    source: object,
    architect: object,
    cuts: PageFieldCuts,
) -> Vetted | None:
    """The field-cut rule on one item (see the module): `refuse` when it sets the vendor's field
    cut against another drawing's value; `allowed` when its vendor overall is the architect's value
    plus the field cuts the drawing shows; else `None` (the rule says nothing)."""
    if not cuts or not isinstance(dims, Mapping):
        return None
    vendor = dims.get("vendor")
    if not isinstance(vendor, Mapping) or not vendor.get("id"):
        return None
    own = str(vendor["id"])
    compares = isinstance(dims.get("architect"), Mapping) or (
        str(source or "") in _DRAWING_SOURCES and _architect_inches(dims, architect) is not None
    )
    per_end = _inches_text(cuts.per_end)
    if own in cuts.labels and compares:
        return Vetted(
            "refuse",
            f"the vendor's {per_end} here is the field cut (the drawing prints FIELD CUT): extra "
            "stone GV adds at the factory for a wall end and trims on site (GV's settled rule). "
            "It is never compared with the architect's drawing: compare the top without it.",
            per_end=cuts.per_end,
        )
    count = cuts.overalls.get(own)
    if count is None or status != "different" or str(source or "") not in _DRAWING_SOURCES:
        return None
    vendor_in, architect_in = _fraction(vendor.get("inches")), _architect_inches(dims, architect)
    if vendor_in is None or architect_in is None:
        return None
    if vendor_in - count * cuts.per_end != architect_in:
        return None
    ends = "a wall end" if count == 1 else f"{count} wall ends"
    return Vetted(
        "allowed",
        f"The vendor's {_inches_text(vendor_in)} includes the {per_end} field cut at {ends} "
        "(GV's settled rule: added at the factory, trimmed on site); without it the top is "
        f"{_inches_text(architect_in)}, the same as the architect's.",
        field_cuts=count,
        per_end=cuts.per_end,
    )
