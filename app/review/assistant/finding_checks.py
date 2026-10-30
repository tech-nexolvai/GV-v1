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
  the inches count, also when the millimetres say something else (`660 [27"]` is 27": settled
  with GV, "inches count; ignore millimetres"). Such a value is never a doubt, never red and
  never a question because of its millimetres (`units_check` still says they disagree, as a
  fact; a check of a side printed in the text layer at its spot ignores it).
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
    "PARTS_READ",
    "VERIFY_MODULE",
    "WRONG_PART",
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
    "inch_first_mm",
    "parts_read",
    "printed_at_spot",
    "printed_from",
    "printed_mm",
    "read_result",
    "to_inches",
    "trust_level",
    "units_check",
    "value_flags",
    "verify_at_spot",
    "wrong_part",
]

type PrintedResult = Literal["match", "not_found", "no_text", "no_box", "not_a_number"]
type ArithmeticResult = Literal["adds_up", "does_not_add_up", "not_a_number"]
type TrustLevel = Literal["confirmed", "likely", "to_check"]
type UnitsResult = Literal["agrees", "disagrees"]
type PartsRead = Literal["code", "ai"]

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

#: A reference dimension, written in round brackets with its mark: `(24")`, `(2'-6")` (#1778).
_REFERENCE: Final = re.compile(r"""^\(\s*(?P<value>[^()]*\d[^()]*['"][^()]*)\s*\)$""")
#: Words and signs written before a value that are not part of it: `VIF 24"`, `±24"`, `+/- 24"`,
#: `APPROX. 24"` (#1778). The value is read; the note is kept by `value_flags`.
_LEADING_NOTE: Final = re.compile(
    r"^(?:V\.?I\.?F\.?|APPROX\.?|REF\.?|\+\s*/\s*-|\+-|±)\s*", re.IGNORECASE
)
#: A plus-or-minus written after a value: `24"+/-`, `24" ±` (#1778).
_TRAILING_PLUS_MINUS: Final = re.compile(r"\s*(?:\+\s*/\s*-|\+-|±)\s*$")
#: Inches first, millimetres in brackets: `31 1/4" [794]`, `2'-0" [610]`, `25 [635]` (#1778).
_INCH_FIRST: Final = re.compile(
    r"^(?P<inches>[^\[\]]*\d[^\[\]]*?)\s*\[\s*(?P<mm>\d+(?:\.\d+)?)\s*(?:mm\.?)?\s*\]$",
    re.IGNORECASE,
)
_INCH_MARKS: Final = re.compile(r"""["']|\bin\b|\binch""", re.IGNORECASE)
_FLAG_PATTERNS: Final = (
    ("vif", re.compile(r"\bV\.?I\.?F\b|verify in field|field verify", re.IGNORECASE)),
    ("approx", re.compile(r"\+\s*/\s*-|\+-|±|\bAPPROX", re.IGNORECASE)),
)


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
    reference = _REFERENCE.match(token)
    if reference is not None:
        token = reference.group("value").strip()
    noted = _LEADING_NOTE.match(token) is not None or _TRAILING_PLUS_MINUS.search(token)
    if noted and not re.search(r"""['"]""", token):
        return None  # `approx 36`: a noted value with no mark is not one sure value
    while True:
        bare = _LEADING_NOTE.sub("", token, count=1)
        bare = _TRAILING_PLUS_MINUS.sub("", bare)
        if (trailing := _TRAILING_WORD.match(bare)) and (
            trailing.group("word").lower() not in _UNIT_WORDS
        ):
            bare = trailing.group("value")
        if bare == token or not bare:
            break
        token = bare
    inch_first = _inch_first(token)
    if inch_first is not None:
        return inch_first or None
    dual = _DUAL_UNITS.match(token)
    if dual is not None and "'" in dual.group("inches"):
        # Millimetres with feet and inches in brackets: `610 [2'-0"]` is 24" (#1778).
        return to_inches(dual.group("inches"))
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


def _mm_agrees(millimetres: str, inches: Decimal) -> bool:
    """Whether printed millimetres are the printed inches times 25.4, within the millimetres' own
    rounding (half their last printed digit): `794` for 31 1/4" (793.75), `635` for 25"."""
    places = len(millimetres.partition(".")[2])
    allowance = Decimal("0.5") / (Decimal(10) ** places)
    return abs(Decimal(millimetres) - inches * _MM_PER_INCH) <= allowance


#: Printed millimetres beside printed inches agree within this (the inches' own 1/16" step, which
#: covers the whole millimetre a drawing rounds to): 4" [101mm] (101.6) and 3/4" [20mm] (19.05) are
#: plain rounding, never "millimetres that do not match" (the pre-flight of a live run, 2026-10-24).
_MM_NEAR: Final = Decimal("25.4") / 16


def _mm_near(millimetres: str, inches: Decimal) -> bool:
    """Whether printed millimetres are the printed inches times 25.4 within a drawing's rounding:
    their own last digit (`_mm_agrees`) or the 1/16" step (`_MM_NEAR`, about 1.6 mm), whichever is
    more. Only for the units check of a pair already read as inches first: which number is the
    inches (`_inch_first`) is still decided by `_mm_agrees` alone."""
    if _mm_agrees(millimetres, inches):
        return True
    return abs(Decimal(millimetres) - inches * _MM_PER_INCH) <= _MM_NEAR


def mm_off(label: Mapping[str, Any]) -> bool:
    """A set pack label whose printed millimetres really do not match its inches: marked
    `mm_ignored` when the pack was made, and still not within a drawing's rounding by today's
    check (`_mm_near`), so a pack made before 2026-10-24 never says 4" [101mm] does not match."""
    return bool(label.get("mm_ignored")) and units_check(label.get("text")) != "agrees"


def _inch_first(token: str) -> Decimal | Literal[False] | None:
    """A pair written inches first with the millimetres in brackets (#1778): `31 1/4" [794]` is
    31 1/4", `25 [635]` is 25". A marked value (`31 1/4"`, `2'-0"`) is the inches whatever the
    bracket says: inches count, millimetres are ignored (settled with GV), so `31 1/4" [800]` is
    31 1/4" too. Two bare numbers are inches first only when the bracket is the first number
    times 25.4 and the first is not the bracket's millimetres (GV's usual `794 [31 1/4]` keeps its
    reading). `None`: not this form (read as before); `False`: this form, refused (a marked value
    that is not one number)."""
    found = _INCH_FIRST.match(token)
    if found is None:
        return None
    outside, millimetres = found.group("inches").strip(), found.group("mm")
    if outside.lower().endswith("mm") or "[" in outside:
        return None
    marked = _INCH_MARKS.search(outside) is not None
    value = to_inches(outside if marked else f'{outside}"')
    if value is None or value <= 0:
        return False if marked else None
    if _mm_agrees(millimetres, value):
        if marked:
            return value
        # Two bare numbers: GV's order (mm first) wins whenever it agrees too.
        backwards = to_inches(f'{millimetres}"')
        plain = re.fullmatch(r"\d+(?:\.\d+)?", outside) is not None
        if plain and backwards is not None and _mm_agrees(outside, backwards):
            return None
        return value
    # A marked value is the inches: the millimetres beside it are ignored (settled with GV).
    return value if marked else None


def inch_first_mm(text: object) -> str | None:
    """The millimetres of a value written inches first with them in brackets (`31 1/4" [794]`
    gives `794`), when `to_inches` reads it so (the 25.4 cross-check agrees); else `None`."""
    if not isinstance(text, str):
        return None
    token = " ".join(plain_marks(text).split())
    found = _INCH_FIRST.match(token)
    if found is None or not _inch_first(token):
        return None
    return found.group("mm")


def printed_mm(text: object) -> str | None:
    """The millimetres of a value printed in both units, either way round: `660 [27"]` and
    `660 (27")` give `660` (millimetres first, the inches in brackets), `31 1/4" [794]` gives
    `794` (`inch_first_mm`). `None` for a value in one unit, or when the inches are not read
    (`to_inches`). Only says what is printed: the inches count (settled with GV)."""
    if not isinstance(text, str):
        return None
    first = inch_first_mm(text)
    if first is not None:
        return first
    token = " ".join(plain_marks(text).split())
    if to_inches(token) is None:
        return None
    round_dual = _ROUND_DUAL.match(token)
    if round_dual is not None:
        return round_dual.group("mm")
    if _INCH_FIRST.match(token) is not None and _inch_first(token) is not None:
        return None
    dual = _DUAL_UNITS.match(token)
    return dual.group("mm") if dual is not None else None


def value_flags(text: object) -> tuple[str, ...]:
    """The notes written with a value that `to_inches` reads past (#1778), kept beside it:
    `reference` for a reference dimension in round brackets (`(24")`), `vif` for verify in field
    (`VIF 24"`, `24" (VIF)`), `approx` for plus-or-minus (`24"+/-`, `±24"`). Empty for a plain
    value."""
    if not isinstance(text, str):
        return ()
    token = " ".join(plain_marks(text).split())
    flags: list[str] = []
    if _REFERENCE.match(token) or re.search(r"\bREF\b", token, re.IGNORECASE):
        flags.append("reference")
    flags += [name for name, pattern in _FLAG_PATTERNS if pattern.search(token)]
    return tuple(flags)


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


#: A vendor string's part that code's geometry names as the wrong number (`vendor_consistency`):
#: `{text, should_be, label}`, `should_be` in inches (the overall less the other parts).
WRONG_PART: Final = "wrong_part"


def wrong_part(finding: Mapping[str, Any]) -> tuple[str, Decimal] | None:
    """A vendor string's wrong part as printed and what it should be (inches), when code's
    geometry named one: the cloud and the "should be" then follow that part, not the printed
    overall against the parts' sum (the overall is right). None otherwise."""
    if finding.get("source") != "vendor_arithmetic":
        return None
    part = finding.get(WRONG_PART)
    if not isinstance(part, Mapping) or not part.get("text"):
        return None
    try:
        should = Decimal(str(part.get("should_be")))
    except ArithmeticError:
        return None
    return (str(part["text"]), should) if should.is_finite() and should > 0 else None


#: A vendor string's parts and overall are code's own labels of one whole row (the finding's field,
#: and its checks' key: `witness.PARTS_READ`, 2026-10-31).
PARTS_READ: Final = witness.PARTS_READ
#: The code checks that make vendor strings from code's own labels: the consistency check
#: (`vendor_consistency`) and GV's site-fit rule (`run_fit`, through the same check).
_CODE_STRING_MAKERS: Final = frozenset({"consistency", "run_fit"})


def parts_read(finding: Mapping[str, Any]) -> PartsRead | None:
    """Who read a vendor string's parts and overall: `code` when they are code's own labels of one
    whole row (a code check made the string, code's geometry verified a reading's string, or code
    found the reading's parts as its own row: `PARTS_READ`), else `ai` (the AI typed
    them: code's sum is then no witness, `witness.py`). `None` for any other finding."""
    if finding.get("source") != "vendor_arithmetic":
        return None
    if (
        finding.get("created_by") in _CODE_STRING_MAKERS
        or finding.get("string_geometry") == "verified"
        or finding.get(PARTS_READ) == "code"
    ):
        return "code"
    return "ai"


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
    parts_read: PartsRead | None = None
    """Who read a vendor string's parts (`parts_read`); `None` for any other finding."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "printed_architect": self.printed_architect,
            "printed_vendor": self.printed_vendor,
            "arithmetic": self.arithmetic,
            PARTS_READ: self.parts_read,
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
        units=_both_units(finding, {"architect": architect, "vendor": vendor}),
        verify_architect=verify_architect,
        verify_vendor=verify_vendor,
        parts_read=parts_read(finding),
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


def _both_units(
    finding: Mapping[str, Any], printed: Mapping[str, PrintedResult] | None = None
) -> UnitsResult | None:
    """Whether the millimetres and inches a side prints together agree. A side that the text
    layer prints at its spot (`printed` "match") is read exactly, so millimetres that disagree
    there are the vendor's (or architect's) own slip: inches count and the millimetres are
    ignored (settled with GV), never a doubt. Elsewhere (drawn ink, a picture) a disagreement
    still means the reading may be wrong."""
    printed = printed or {}
    sides = [
        (
            None
            if (result := units_check(finding.get(side))) == "disagrees"
            and printed.get(side) == "match"
            else result
        )
        for side in ("architect", "vendor")
    ]
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
    token = " ".join(plain_marks(text).split())
    first = _INCH_FIRST.match(token)
    if first is not None and _inch_first(token) is not None:
        # Inches first (#1778): read only when the millimetres agree; a marked pair that does not.
        outside = first.group("inches").strip()
        value = to_inches(outside if _INCH_MARKS.search(outside) else f'{outside}"')
        if value is None:
            return None
        return "agrees" if _mm_near(first.group("mm"), value) else "disagrees"
    dual = _DUAL_UNITS.match(token)
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
