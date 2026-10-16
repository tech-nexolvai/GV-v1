"""Code's own checks on one chat finding (chat v3, trust): $0, no network, the same answer every time.

A finding is the lead model's claim: "on this page the architect prints X and the vendor prints Y".
Before a reviewer is asked to believe it, code checks what it can without any AI:

- **Printed at this spot.** Is X really printed inside the finding's architect box, and Y inside its
  vendor box? Every printed form of the same number counts (`3'-1"`, `37"`, `37 in`, `940 [37]`),
  because code compares inches, not spellings. The words are read with the repo's own page reader
  (`extraction.reader.read_page_contents`) from the page's content AND from the drawings pasted on it
  as `/Stamp` annotations (`extraction.stamp_text.read_stamp_text`): on GV's combined sheets the
  drawings are stamps, and pdfium's page text does not see inside them. Only black or grey text is
  read, so GV's coloured markup is never taken as what the drawing prints (the vendor-layer rule).
  `no_text` means the box holds no words at all: the numbers there are pen strokes, which this check
  cannot read.
- **Inches by code.** `to_inches` turns a written value into exact inches with the repo's one parser
  (`units.notation` + `units.normalise`), so the screen shows code's numbers and code's difference,
  never the model's arithmetic. Millimetres alone are not read (GV ignores metric); in `940 [37]`
  the inches count.
- **Vendor arithmetic.** For a finding about the vendor's own sums, code adds the parts up.

`trust_level` turns the full set of checks on a finding into Confirmed / Likely / To check
(CONTRACT3). The second independent read lives in `second_read.py`.

The PDF reader is loaded by name at the first page read: `app/api/` must not reach `extraction/` by
import (`tests/api/test_no_heavy_work.py`).
"""

from __future__ import annotations

import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Literal

from units.measurement import Unit
from units.normalise import (
    UnitNormalisationError,
    drawn_fraction_refusal,
    normalise_to_inches,
    plain_marks,
)
from units.notation import canonical_notation, is_compound

__all__ = [
    "Checks",
    "PrintedResult",
    "TrustLevel",
    "arithmetic_check",
    "arithmetic_off_by",
    "check_finding",
    "printed_at_spot",
    "to_inches",
    "trust_level",
]

type PrintedResult = Literal["match", "not_found", "no_text", "no_box", "not_a_number"]
type ArithmeticResult = Literal["adds_up", "does_not_add_up", "not_a_number"]
type TrustLevel = Literal["confirmed", "likely", "to_check"]

# ---- inches by code ------------------------------------------------------------------------------

#: Words a drawing writes after a dimension that are not part of the number: `2'-9" AFF`, `36" TYP.`.
#: Any trailing word is dropped unless it is a unit word, which the parser itself reads.
_TRAILING_WORD: Final = re.compile(r"^(?P<value>.*?\S)\s+(?P<word>[A-Za-z][A-Za-z.]*)$")
_UNIT_WORDS: Final = frozenset(
    {
        "in",
        "in.",
        "inch",
        "inches",
        "ft",
        "ft.",
        "feet",
        "mm",
        "cm",
        "m",
        "yd",
        "yds",
        "yard",
        "yards",
    }
)

#: `940 (37)`: millimetres with the inches in round brackets, written as the dual token `940 [37]`.
_ROUND_DUAL: Final = re.compile(r'^(?P<mm>\d+(?:\.\d+)?)\s*\(\s*(?P<inches>[\d\s/.]+"?)\s*\)$')

#: Metric with no inches beside it. Not read: GV ignores metric.
_METRIC_ONLY: Final = re.compile(r"^\d+(?:\.\d+)?\s*(?:mm|cm|m)\.?$", re.IGNORECASE)

#: The longest text read as one value. A sentence is not a dimension.
_LONGEST_VALUE: Final = 40


def to_inches(text: str) -> Decimal | None:
    """A written value as exact inches, or `None` when it is not one value code can be sure of.

    `3'-1"` is 37, `3' - 1 1/2"` 37.5, `37½"` 37.5, `37"`, `37 in` and a bare `37` are 37, `36.25`
    is 36.25, `940 [37]` and `940 (37)` are 37 (the inches count), `2'-9" AFF` is 33. `None` for
    millimetres alone, two values (`36 x 24`, `39 1/4"+6"`), a misprinted fraction (`163/8"`,
    `1/3"`, `3'-14"`) and anything else that is not plainly one dimension.

    A bare number is read as inches: on a GV drawing and in a finding written from one, a number
    without a unit is inches (metric is ignored, and a millimetre value always says `mm` or sits
    before its inches in brackets).
    """
    if not isinstance(text, str):
        return None
    token = " ".join(plain_marks(text).split())
    if not token or len(token) > _LONGEST_VALUE:
        return None
    while (trailing := _TRAILING_WORD.match(token)) and (
        trailing.group("word").lower() not in _UNIT_WORDS
    ):
        token = trailing.group("value")
    round_dual = _ROUND_DUAL.match(token)
    if round_dual is not None:
        token = f"{round_dual.group('mm')} [{round_dual.group('inches').strip()}]"
    if _METRIC_ONLY.match(token) or is_compound(token):
        return None
    canonical, _millimetres = canonical_notation(token)
    if drawn_fraction_refusal(canonical) is not None:
        return None
    try:
        exact = normalise_to_inches(canonical, unmarked_unit=Unit.INCH).exact
    except (UnitNormalisationError, ArithmeticError):
        return None
    return _exact_decimal(exact)


def _exact_decimal(value: Fraction) -> Decimal | None:
    """`value` as a Decimal with no rounding, or `None` when no finite decimal is exactly it."""
    if value < 0:
        return None
    denominator = value.denominator
    for prime in (2, 5):
        while denominator % prime == 0:
            denominator //= prime
    if denominator != 1:
        return None
    exact = Decimal(value.numerator) / Decimal(value.denominator)
    return exact.normalize() if exact != exact.to_integral_value() else exact.quantize(Decimal(1))


# ---- printed at this spot ------------------------------------------------------------------------

#: How far outside a finding's box a word may start and still count as at the spot, in PDF points.
#: A box drawn tight round a dimension often clips its inch mark or its last digit.
_NEAR_PT: Final = Decimal(4)

#: The most words read together as one value: `3'` `-` `1` `1/2"` is four.
_LONGEST_RUN: Final = 5

#: Points: the reader places words on a 72 dpi page, so a pixel is a PDF point of the page as shown.
_POINTS: Final = 72

_READ_LOCK: Final = threading.Lock()
_PAGE_CACHE: OrderedDict[tuple[str, int, int, int], _PageWords] = OrderedDict()
_PAGE_CACHE_SIZE: Final = 24


@dataclass(frozen=True, slots=True)
class _Word:
    """One run of text the reader found, in PDF points of the page as shown (origin top-left)."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    sideways: bool


@dataclass(frozen=True, slots=True)
class _PageWords:
    words: tuple[_Word, ...]
    unread: tuple[tuple[float, float, float, float], ...]
    """Where the reader saw text it set aside unread: text is there, just not a value."""


def _box_of(points: Iterable[Any]) -> tuple[float, float, float, float]:
    xs = [float(point.x) for point in points]
    ys = [float(point.y) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _read_page(path: Path, page_index: int) -> _PageWords:
    """Every black or grey word on one page: its own content and its pasted drawings (cached)."""
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size, page_index)
    with _READ_LOCK:
        cached = _PAGE_CACHE.get(key)
        if cached is not None:
            _PAGE_CACHE.move_to_end(key)
            return cached
        reader = import_module("extraction.reader")
        stamps = import_module("extraction.stamp_text")
        stages = import_module("workflow.stages")
        missing_space = stages.missing_space_from_environment()
        data = path.read_bytes()
        document = uuid.uuid4()
        own = reader.read_page_contents(
            data,
            page_index,
            document_version_id=document,
            dpi=_POINTS,
            missing_space=missing_space,
            keep_char=stamps.drawing_ink,
            keep_path=stamps.path_ink,
        )
        pasted = stamps.read_stamp_text(
            data, page_index, document_version_id=document, dpi=_POINTS, missing_space=missing_space
        ).contents
        words: list[_Word] = []
        unread: list[tuple[float, float, float, float]] = []
        for contents in (own, pasted):
            for item in contents.texts:
                x0, y0, x1, y1 = _box_of(item.image_extent)
                sideways = int(item.rotation_degrees) % 180 == 90
                words.append(_Word(item.text, x0, y0, x1, y1, sideways))
            unread.extend(_box_of(label.image_extent) for label in contents.set_aside)
        page = _PageWords(tuple(words), tuple(unread))
        _PAGE_CACHE[key] = page
        while len(_PAGE_CACHE) > _PAGE_CACHE_SIZE:
            _PAGE_CACHE.popitem(last=False)
        return page


def _overlaps(
    box: tuple[float, float, float, float], spot: tuple[float, float, float, float]
) -> bool:
    near = float(_NEAR_PT)
    return not (
        box[2] < spot[0] - near
        or box[0] > spot[2] + near
        or box[3] < spot[1] - near
        or box[1] > spot[3] + near
    )


def _across(word: _Word) -> float:
    """Where a word sits across its line: its middle up the page, or across it when sideways."""
    return (word.x0 + word.x1) / 2 if word.sideways else (word.y0 + word.y1) / 2


def _along(word: _Word) -> float:
    return word.y0 if word.sideways else word.x0


def _height(word: _Word) -> float:
    return (word.x1 - word.x0) if word.sideways else (word.y1 - word.y0)


def _lines(words: Sequence[_Word]) -> list[list[_Word]]:
    """The words grouped into lines of one reading direction, each in reading order."""
    lines: list[list[_Word]] = []
    for word in sorted(words, key=lambda word: (word.sideways, _across(word))):
        for line in lines:
            first = line[0]
            tolerance = max(_height(first), _height(word), 1.0) * 0.6
            if first.sideways is word.sideways and abs(_across(first) - _across(word)) <= tolerance:
                line.append(word)
                break
        else:
            lines.append([word])
    return [sorted(line, key=_along) for line in lines]


def _printed_values(words: Sequence[_Word]) -> set[Decimal]:
    """Every value printed in these words, read the longest way first along each line (so `38`
    `3/4"` is 38.75 and never 38), in both directions because sideways text may read either way."""
    values: set[Decimal] = set()
    for line in _lines(words):
        for ordered in (line, line[::-1]):
            start = 0
            while start < len(ordered):
                for size in range(min(_LONGEST_RUN, len(ordered) - start), 0, -1):
                    value = to_inches(" ".join(word.text for word in ordered[start : start + size]))
                    if value is not None:
                        values.add(value)
                        start += size
                        break
                else:
                    start += 1
    return values


def printed_at_spot(path: Path, page: int, box: Mapping[str, Any], value: str) -> PrintedResult:
    """Whether `value` is printed inside `box` on `page` (1-based) of the PDF at `path`.

    `box` is `{x0, y0, x1, y1}` in PDF points of the page as shown, origin top-left. `match`: the
    same inches are printed there, in any form; `not_found`: words are there but not this value;
    `no_text`: no words at all (pen strokes, which code cannot read); `no_box`: the box or page
    cannot be placed; `not_a_number`: `value` is not one dimension.
    """
    wanted = to_inches(value) if isinstance(value, str) else None
    if wanted is None:
        return "not_a_number"
    spot = _spot(box)
    if spot is None or isinstance(page, bool) or not isinstance(page, int) or page < 1:
        return "no_box"
    reader = import_module("extraction.reader")
    try:
        words = _read_page(path, page - 1)
    except (OSError, reader.UnreadablePdf, IndexError):
        return "no_box"
    inside = [word for word in words.words if _overlaps((word.x0, word.y0, word.x1, word.y1), spot)]
    if wanted in _printed_values(inside):
        return "match"
    if inside or any(_overlaps(label, spot) for label in words.unread):
        return "not_found"
    return "no_text"


def _spot(box: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    try:
        x0, y0, x1, y1 = (float(box[key]) for key in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return None
    if not x1 > x0 or not y1 > y0:
        return None
    return x0, y0, x1, y1


# ---- vendor arithmetic ---------------------------------------------------------------------------


def arithmetic_off_by(parts: Sequence[str], printed_total: str) -> Decimal | None:
    """The printed total minus the sum of the parts, in inches; `None` when any is not a number."""
    if not parts or isinstance(parts, str):
        return None
    values = [to_inches(part) if isinstance(part, str) else None for part in parts]
    total = to_inches(printed_total) if isinstance(printed_total, str) else None
    if total is None or any(value is None for value in values):
        return None
    return total - sum((value for value in values if value is not None), Decimal(0))


def arithmetic_check(parts: Sequence[str], printed_total: str) -> ArithmeticResult:
    """`adds_up` when the parts sum exactly to the printed total, else `does_not_add_up`."""
    off_by = arithmetic_off_by(parts, printed_total)
    if off_by is None:
        return "not_a_number"
    return "adds_up" if off_by == 0 else "does_not_add_up"


# ---- one finding ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Checks:
    """Code's checks on one finding."""

    printed_architect: PrintedResult
    printed_vendor: PrintedResult
    arithmetic: ArithmeticResult | None
    """`None` unless the finding's source is `vendor_arithmetic`."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "printed_architect": self.printed_architect,
            "printed_vendor": self.printed_vendor,
            "arithmetic": self.arithmetic,
        }


def check_finding(finding: Mapping[str, Any], pdf_paths: Mapping[str, Path]) -> Checks:
    """Code's checks on a stored finding. `pdf_paths` maps an upload's file name to its PDF; a box's
    `file` names one (with one upload, a box without `file` means it). Thread-safe.

    Raises `ValueError` only when the deployment does not state the reader's missing-space setting
    (`GV_READER_MISSING_SPACE_HEIGHTS`), without which no page's text is read anywhere in GV.
    """
    arithmetic: ArithmeticResult | None = None
    if finding.get("source") == "vendor_arithmetic":
        arithmetic = arithmetic_check(
            finding.get("parts") or [], finding.get("printed_total") or ""
        )
    return Checks(
        printed_architect=_side(finding.get("architect"), finding.get("arch_box"), pdf_paths),
        printed_vendor=_side(finding.get("vendor"), finding.get("vendor_box"), pdf_paths),
        arithmetic=arithmetic,
    )


def _side(value: object, box: object, pdf_paths: Mapping[str, Path]) -> PrintedResult:
    if not isinstance(value, str) or to_inches(value) is None:
        return "not_a_number"
    if not isinstance(box, Mapping):
        return "no_box"
    name = box.get("file")
    if isinstance(name, str) and name in pdf_paths:
        path = pdf_paths[name]
    elif name is None and len(pdf_paths) == 1:
        path = next(iter(pdf_paths.values()))
    else:
        return "no_box"
    return printed_at_spot(Path(path), box.get("page"), box, value)  # type: ignore[arg-type]


# ---- trust level ---------------------------------------------------------------------------------


def trust_level(checks: Mapping[str, Any], *, status: str | None = None) -> TrustLevel:
    """Confirmed / Likely / To check from a finding's full checks (CONTRACT3).

    `checks` holds `printed_architect`, `printed_vendor`, `second_read`, `rule`, `arithmetic` and
    `lead_checked`. Code backs the finding when both values are printed at their spots, or GV's
    rule check agrees, or code's arithmetic does. Then:

    - **confirmed**: code backs it AND the second read agrees;
    - **likely**: two of code backs it, the second read agrees, the lead checked it on the drawing;
    - **to_check**: anything else, and always when a check disagrees: the second read, the rule
      check, or (given the finding's `status`) code's arithmetic (`adds_up` on a "different"
      finding, `does_not_add_up` on a "same" one).
    """
    second = checks.get("second_read")
    rule = checks.get("rule")
    arithmetic = checks.get("arithmetic")
    if second == "disagrees" or rule == "disagrees":
        return "to_check"
    if (arithmetic == "adds_up" and status == "different") or (
        arithmetic == "does_not_add_up" and status == "same"
    ):
        return "to_check"
    printed = checks.get("printed_architect") == "match" and checks.get("printed_vendor") == "match"
    code_backs = printed or rule == "agrees" or arithmetic in ("adds_up", "does_not_add_up")
    agrees = second == "agrees"
    if code_backs and agrees:
        return "confirmed"
    if sum((code_backs, agrees, checks.get("lead_checked") is True)) >= 2:
        return "likely"
    return "to_check"
