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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Literal

from app.review.assistant import witness
from units.measurement import Unit
from units.normalise import (
    UnitNormalisationError,
    drawn_fraction_refusal,
    normalise_to_inches,
    plain_marks,
)
from units.notation import canonical_notation, is_compound

__all__ = [
    "VERIFY_MODULE",
    "Checks",
    "PrintedResult",
    "TrustLevel",
    "arithmetic_check",
    "arithmetic_off_by",
    "check_finding",
    "claim_value",
    "critic_passed",
    "dimension_tokens",
    "drawn_checks",
    "printed_at_spot",
    "printed_from",
    "read_result",
    "to_inches",
    "trust_level",
    "units_check",
    "verify_at_spot",
]

type PrintedResult = Literal["match", "not_found", "no_text", "no_box", "not_a_number"]
type ArithmeticResult = Literal["adds_up", "does_not_add_up", "not_a_number"]
type TrustLevel = Literal["confirmed", "likely", "to_check"]
type UnitsResult = Literal["agrees", "disagrees"]

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


#: One dimension inside a sentence: `940 [37]`, `3'-1 1/2"`, `41 1/2"`, `3/4"`.
_DIMENSION: Final = re.compile(
    r"""\d{2,5}\s*\[\s*\d+(?:\s+\d+/\d+|-\d+/\d+|\.\d+)?\s*"?\s*\]
     |\d+\s*'\s*-?\s*\d+(?:\s+\d+/\d+|-\d+/\d+)?\s*"?
     |\d+(?:\s+\d+/\d+|-\d+/\d+|\.\d+)?\s*(?:"|\bin\b)
     |\d+/\d+\s*"
    """,
    re.VERBOSE,
)


def dimension_tokens(text: object) -> list[str]:
    """Every dimension written in `text`, in order, each one `to_inches` reads: the sentence
    `41 1/2" printed overall; parts 32" + 9 1/2"` gives three."""
    if not isinstance(text, str):
        return []
    found: list[str] = []
    for match in _DIMENSION.finditer(plain_marks(text)):
        token = match.group(0).strip()
        if to_inches(token) is not None:
            found.append(token)
    return found


def claim_value(text: object) -> str | None:
    """The one value a finding's side claims: the whole text when it is one dimension, else the
    first dimension in its sentence (the value the claim is about). `None` when there is none."""
    if not isinstance(text, str):
        return None
    if to_inches(text) is not None:
        return text
    tokens = dimension_tokens(text)
    return tokens[0] if tokens else None


# ---- printed at this spot ------------------------------------------------------------------------

#: The module that reads the drawing at a spot (`verify_value`): loaded by name, because it renders
#: pages and runs OCR, which `app/api/` must not reach by import (`tests/api/test_no_heavy_work.py`).
VERIFY_MODULE: Final = "app.review.assistant.verify_value"


def verify_at_spot(
    path: Path, page: int, box: Mapping[str, Any], value: str, *, drawn: bool = False
) -> dict[str, Any]:
    """Code's reading of one value at its spot (`verify_value.verify_value(...).as_dict()`):
    `result` match / mismatch / cannot_read, `route` text / drawn / none, `read`, `reads`,
    `reason`, `value`, `pending`, `words`. `drawn=False` reads the drawing's text only (ms); a box
    that also holds drawn ink then comes back `pending` for the drawn-ink pass."""
    module = import_module(VERIFY_MODULE)
    found = module.verify_value(Path(path), page, box, value, drawn=drawn)
    return dict(found.as_dict())


def printed_at_spot(path: Path, page: int, box: Mapping[str, Any], value: str) -> PrintedResult:
    """Whether `value` is printed as text inside `box` on `page` (1-based) of the PDF at `path`.

    `box` is `{x0, y0, x1, y1}` in PDF points of the page as shown, origin top-left. `match`: the
    same inches are printed there, in any form (read by `verify_value`'s text route: two-line
    mm-over-inches, stacked fractions, the symbol font); `not_found`: text is there but not this
    value; `no_text`: no text at all (drawn lines or a picture, which only the drawn-ink route
    reads); `no_box`: the box or page cannot be placed; `not_a_number`: `value` is not one
    dimension.
    """
    wanted = to_inches(value) if isinstance(value, str) else None
    if wanted is None:
        return "not_a_number"
    spot = _spot(box)
    if spot is None or isinstance(page, bool) or not isinstance(page, int) or page < 1:
        return "no_box"
    return printed_from(verify_at_spot(path, page, box, value))


def printed_from(verified: Mapping[str, Any]) -> PrintedResult:
    """The printed-at-spot word for a `verify_at_spot` answer (its text route)."""
    if verified.get("route") == "text" and verified.get("result") == "match":
        return "match"
    if verified.get("reason") in ("the box cannot be placed", "the page cannot be read"):
        return "no_box"
    if verified.get("reason") == "not one number":
        return "not_a_number"
    has_text = any(str(read).startswith("text: ") for read in verified.get("reads") or [])
    return "not_found" if has_text else "no_text"


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
    units: UnitsResult | None = None
    """Millimetres and inches written together (`940 [37]`): do they agree? `None`: no side has
    both."""
    verify_architect: Mapping[str, Any] | None = None
    """Code's reading of the architect's value at its spot (`verify_at_spot`); `None` when the
    side has no number or no box."""
    verify_vendor: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "printed_architect": self.printed_architect,
            "printed_vendor": self.printed_vendor,
            "arithmetic": self.arithmetic,
            "units": self.units,
            "verify_architect": (
                None if self.verify_architect is None else dict(self.verify_architect)
            ),
            "verify_vendor": None if self.verify_vendor is None else dict(self.verify_vendor),
        }


#: Each side of a finding: its value, its box and its checks' keys.
_SIDES: Final = (
    ("architect", "arch_box", "verify_architect"),
    ("vendor", "vendor_box", "verify_vendor"),
)


def check_finding(finding: Mapping[str, Any], pdf_paths: Mapping[str, Path]) -> Checks:
    """Code's checks on a stored finding. `pdf_paths` maps an upload's file name to its PDF; a box's
    `file` names one (with one upload, a box without `file` means it). Thread-safe.

    Each side's value is read at its box by the text route (ms). A side whose box also holds drawn
    ink comes back `pending`; `drawn_checks` reads it (seconds, OCR) after the lead's answer.
    """
    arithmetic: ArithmeticResult | None = None
    if finding.get("source") == "vendor_arithmetic":
        arithmetic = arithmetic_check(
            finding.get("parts") or [], finding.get("printed_total") or ""
        )
    architect, verify_architect = _side(
        finding.get("architect"), finding.get("arch_box"), pdf_paths
    )
    vendor, verify_vendor = _side(finding.get("vendor"), finding.get("vendor_box"), pdf_paths)
    return Checks(
        printed_architect=architect,
        printed_vendor=vendor,
        arithmetic=arithmetic,
        units=_both_units(finding),
        verify_architect=verify_architect,
        verify_vendor=verify_vendor,
    )


def drawn_checks(finding: Mapping[str, Any], pdf_paths: Mapping[str, Path]) -> dict[str, Any]:
    """The drawn-ink route for each side its text route left `pending`: `{verify_<side>: answer}`
    for the sides read now (none: `{}`). Seconds per side (two OCR engines); thread-safe."""
    checks = finding.get("checks") or {}
    out: dict[str, Any] = {}
    for side, box_key, key in _SIDES:
        verified = checks.get(key)
        if not isinstance(verified, Mapping) or not verified.get("pending"):
            continue
        box = finding.get(box_key)
        path = _path_of(box, pdf_paths)
        claim = claim_value(finding.get(side))
        if path is None or claim is None or not isinstance(box, Mapping):
            continue
        out[key] = verify_at_spot(path, box.get("page"), box, claim, drawn=True)  # type: ignore[arg-type]
    return out


def _both_units(finding: Mapping[str, Any]) -> UnitsResult | None:
    sides = [units_check(finding.get(side)) for side in ("architect", "vendor")]
    if "disagrees" in sides:
        return "disagrees"
    return "agrees" if "agrees" in sides else None


#: Millimetres with the inches beside them in brackets: `940 [37]`, `940 mm (37 1/2")`.
_DUAL_UNITS: Final = re.compile(
    r"^(?P<mm>\d+(?:\.\d+)?)\s*(?:mm\.?)?\s*[\[(]\s*(?P<inches>[^\])]+?)\s*[\])]$", re.IGNORECASE
)
_MM_PER_INCH: Final = Decimal("25.4")


def units_check(text: object) -> UnitsResult | None:
    """Code's mm-to-inch check on one value written in both units (`940 [37]`): `agrees` when the
    millimetres are the inches within the inches' own rounding (half their finest step: a whole
    inch rounds to 1/2", 37 1/2" to 1/4"), `disagrees` when not, `None` when the value is not
    written in both units or either part is not a number. $0, no PDF."""
    if not isinstance(text, str):
        return None
    dual = _DUAL_UNITS.match(" ".join(plain_marks(text).split()))
    if dual is None:
        return None
    inches = to_inches(dual.group("inches"))
    if inches is None:
        return None
    millimetres = Decimal(dual.group("mm"))
    step = Fraction(inches).limit_denominator(64).denominator
    tolerance = Decimal(1) / Decimal(2 * step) + Decimal("0.5") / _MM_PER_INCH
    return "agrees" if abs(millimetres / _MM_PER_INCH - inches) <= tolerance else "disagrees"


def _path_of(box: object, pdf_paths: Mapping[str, Path]) -> Path | None:
    if not isinstance(box, Mapping):
        return None
    name = box.get("file")
    if isinstance(name, str) and name in pdf_paths:
        return Path(pdf_paths[name])
    if name is None and len(pdf_paths) == 1:
        return Path(next(iter(pdf_paths.values())))
    return None


def _side(
    value: object, box: object, pdf_paths: Mapping[str, Path]
) -> tuple[PrintedResult, dict[str, Any] | None]:
    """One side's printed-at-spot word and code's reading of its claimed value (a sentence's first
    dimension: `41 1/2" printed overall; parts ...` is checked as `41 1/2"`)."""
    claim = claim_value(value)
    if claim is None:
        return "not_a_number", None
    path = _path_of(box, pdf_paths)
    if path is None or not isinstance(box, Mapping):
        return "no_box", None
    page = box.get("page")
    if _spot(box) is None or isinstance(page, bool) or not isinstance(page, int) or page < 1:
        return "no_box", None
    verified = verify_at_spot(path, page, box, claim)
    return printed_from(verified), verified


# ---- trust level ---------------------------------------------------------------------------------


#: The read's result (the third read's majority when one was made, #1498) and the critic: the
#: trust rule's own helpers (`witness.py`).
read_result = witness.read_result
critic_passed = witness.critic_passed


def trust_level(checks: Mapping[str, Any], *, status: str | None = None) -> TrustLevel:
    """Confirmed / Likely / To check from a finding's full checks (CONTRACT3, as changed by the
    accuracy rules in `witness.py`).

    `checks` holds `printed_architect`, `printed_vendor`, `second_read`, `rule`, `arithmetic`,
    `lead_checked` and, when present, `adjudication` (the third read, #1498), `critic`, `ledger`
    and `units`. Confirmed needs a non-AI witness (both values printed at their spots, GV's rule
    check, code's arithmetic, the reading ledger, or the millimetres and inches agreeing) AND an AI
    witness that agrees (the read, the lead's check, or the critic passing it). Two AI witnesses
    alone are Likely at best; a non-AI witness alone is Likely. Any check that disagrees (the read,
    the critic objecting, the rule, the ledger, mm/inch, or given the finding's `status` code's
    arithmetic) makes it To check.
    """
    level = witness.trust_level(checks, status)
    return "confirmed" if level == "confirmed" else "likely" if level == "likely" else "to_check"
