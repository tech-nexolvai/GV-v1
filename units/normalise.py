"""Read a dimension written in another unit into inches, at read time only.

`CALL_2026_08_25_INPUTS` N3: some drawings label only in millimetres or only in feet, and without
conversion those read as `NOT_FOUND` even though the value is plainly on the page. This turns such a
token into an inch `Measurement` so it can be *read*.

**It does not change the verdict model.** Inches remain authoritative and millimetres are never a
verdict operand (Q12, `ADR-0001`). This is normalisation upstream of the comparison — a different
thing from comparing units against each other, which nothing here does. `units/policy.py` still
refuses to combine measurements authored in different units, and this module never touches that path.

**Nothing is ever rounded.** Every conversion is exact `Fraction` arithmetic: 25.4 mm to the inch as
`5/127`, twelve inches to the foot. Rounding a converted value to the nearest sixteenth would make
most millimetre drawings compare equal, and it would do so by inventing precision the drawing does
not carry — a guess, on the dimension that decides the verdict. See `read_the_docstring_on`
`normalise_to_inches` for what that costs and why it is still the right trade.

Yards are out of scope by decision, not by omission, and are refused explicitly (N3).
"""

from __future__ import annotations

import re
import unicodedata
from fractions import Fraction

from units.imperial import ImperialParseError, parse_imperial
from units.measurement import Measurement, Unit


class UnitNormalisationError(ValueError):
    """Raised when a token is not a dimension this module can read into inches.

    A `ValueError` rather than a sentinel return: a caller that forgets to handle it gets a loud
    failure, where a `None` would flow onward and be read as "no dimension here" — the silent
    NOT_FOUND this module exists to remove.
    """


#: Exactly twelve inches. Not a conversion factor so much as the definition of the unit.
INCHES_PER_FOOT: Fraction = Fraction(12)

#: 25.4 mm to the inch, as an exact ratio. `Measurement.to` uses the same number the other way.
INCHES_PER_MM: Fraction = Fraction(5, 127)

#: Refused on sight, so the reason appears in the error rather than as an unsupported-token shrug.
_YARD_RE = re.compile(r"\b(yd|yds|yard|yards)\b", re.IGNORECASE)

#: `984 mm`, `984mm`, `984 MM`.
_MM_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)\s*mm$", re.IGNORECASE)

#: `3 ft`, `3ft`, `3'`, and the feet-and-inches forms `3'-6`, `3'-6"`, `3' 6 1/2"`, `3'-6 1/2`,
#: `3' - 6"` with spaces round the dash, and `3'6"` with no separator at all (#1052). The inch part
#: is matched loosely here and checked strictly by `_inch_part`, so a malformed one is refused with a
#: reason rather than half-read.
_FEET_RE = re.compile(
    r"^(?P<feet>\d+)\s*(?:'|ft|feet)"
    r"(?:\s*-?\s*(?P<inches>\d[\d\s/\.\-]*?)\s*(?:\"|in|inches)?)?$",
    re.IGNORECASE,
)

#: The inch part of a feet-and-inches dimension, as a drawing writes it: a whole number under a foot,
#: a proper fraction, or both — `6`, `1/2`, `6 1/2`, `6-1/2` — or a decimal (`6.5`), which the parser
#: has always read. Anything else is malformed.
_INCH_PART_RE = re.compile(
    r"^(?:(?P<whole>\d+)(?:(?:\s+|\s*-\s*)(?P<num>\d+)/(?P<den>\d+))?"
    r"|(?P<alone_num>\d+)/(?P<alone_den>\d+)"
    r"|(?P<decimal>\d+\.\d+))$"
)

#: The largest denominator a drawn fraction may have. Tapes and drawings stop at sixty-fourths.
MAXIMUM_DENOMINATOR = 64

#: How a drawing font may write the marks the parser knows (#1052): typographic primes, curly quotes
#: (a Type 1 font's encoding of the straight ones) and every dash a CAD font sets for the hyphen.
#: Each is a transcription equivalence; no number changes.
_WRITTEN_MARKS = (
    ("″", '"'),
    ("′", "'"),
    ("’", "'"),
    ("‘", "'"),
    ("”", '"'),
    ("“", '"'),
    ("‐", "-"),
    ("‑", "-"),
    ("‒", "-"),
    ("–", "-"),
    ("—", "-"),
    ("−", "-"),
)

#: An inch token, with or without its marker — `38 3/4"`, `4 in`, `2.375 inches`, and the bare
#: `38 3/4` which is matched here so it can be *refused with a reason* rather than falling through to
#: the generic unknown-unit branch. Whether the marker was present is decided separately, below.
_INCH_RE = re.compile(r'^(?P<value>[\d\s/\.]+?)\s*(?P<marker>"|in|inch|inches)?$', re.IGNORECASE)


def normalise_to_inches(text: str, *, unmarked_unit: Unit | None = None) -> Measurement:
    """Read a dimension token as inches, exactly.

    Accepts millimetres (`984 mm`), feet (`3'`, `3 ft`), feet and inches (`3'-6 1/2"`) and marked
    inches (`38 3/4"`). The returned `Measurement` keeps the authored token as `raw_text`, so a
    reviewer is shown what the drawing actually said and not only what it became.

    **A bare number is refused unless the caller says what the sheet is drawn in.** `38 3/4` carries
    no unit at all, and the first version of this function read it as inches — which is the same
    assumption it refuses for `38 3/4 cm`, made silently because inches happened to be the answer we
    wanted. A bare token is exactly the ambiguity `AGENTS.md` §2 says must abstain.

    `unmarked_unit` is how a caller that genuinely knows — a sheet whose title block declares its
    units, a reviewer who has said so — passes that knowledge in. It has no default, so the
    assumption is always somebody's explicit act and appears in the calling code where it can be
    reviewed, rather than being buried here.

    **What conversion costs under exact match, and why it is still correct.** 984 mm is exactly
    `4920/127` inches — about 38.7402 — and the drawing that means it almost certainly writes
    `38 3/4`, which is `155/4`. Those are not equal, and `Q2` settled that V1 compares exactly with
    no tolerance band, so a millimetre-sourced value checked against an inch one will normally FAIL.

    That is the honest result and not a defect to paper over. The alternative is to round the
    converted value to some denominator, and a rounded value would be the system deciding what the
    drawing *meant* — the guess `AGENTS.md` §2 forbids on precisely the number that gets cut. Under
    `Q4` every finding is flagged for a reviewer anyway, and a flag saying "these two dimensions do
    not agree exactly" is true. What the reviewer must not be shown is a PASS that arithmetic did not
    earn.

    Feet are the clean case: twelve to the inch exactly, so `3'-6"` is `42` and nothing is lost.

    Raises `UnitNormalisationError` for yards (out of scope by decision, N3) and for anything else it
    cannot read. It never returns an approximation and never falls back to a bare number when the
    unit is unrecognised — a token whose unit cannot be established is exactly the ambiguity the
    caller must turn into an abstention.
    """
    if not isinstance(text, str):
        raise UnitNormalisationError("dimension must be text")

    token = plain_marks(text.strip())
    if not token:
        raise UnitNormalisationError("dimension is empty")

    if _YARD_RE.search(token):
        raise UnitNormalisationError(
            f"yards are out of scope for V1 and are not converted: {text!r}. "
            "Refused rather than read, so a yard cannot enter a comparison unnoticed."
        )

    millimetres = _MM_RE.match(token)
    if millimetres is not None:
        exact = Fraction(millimetres.group("value")) * INCHES_PER_MM
        return Measurement(exact=exact, unit=Unit.INCH, raw_text=text)

    feet = _FEET_RE.match(token)
    if feet is not None:
        exact = Fraction(int(feet.group("feet"))) * INCHES_PER_FOOT
        remainder = feet.group("inches")
        if remainder is not None and remainder.strip():
            exact += _inch_part(remainder.strip(), text)
        return Measurement(exact=exact, unit=Unit.INCH, raw_text=text)

    inches = _INCH_RE.match(token)
    if inches is not None:
        if inches.group("marker") is None and unmarked_unit is not Unit.INCH:
            raise UnitNormalisationError(
                f"{text!r} carries no unit. Reading it as inches would be the same assumption this "
                "refuses for '38 3/4 cm', made silently because inches is the answer we wanted. "
                "Pass unmarked_unit=Unit.INCH if the sheet's units are actually known."
            )
        try:
            exact = parse_imperial(inches.group("value"))
        except ImperialParseError as error:
            raise UnitNormalisationError(f"unreadable inch dimension: {text!r}") from error
        return Measurement(exact=exact, unit=Unit.INCH, raw_text=text)

    raise UnitNormalisationError(
        f"no unit could be established for {text!r}. A dimension whose unit is unknown is an "
        "ambiguity, and the caller turns it into REVIEW_REQUIRED rather than assuming inches."
    )


def plain_marks(token: str) -> str:
    """The token with the marks a drawing font writes rewritten into the ones the parser knows.

    A vulgar fraction (`½`) becomes its own digits — taken from Unicode's decomposition of it, never
    typed out here — with a space in front where a digit precedes it, so `1½` is one and a half and
    never fifteen over two (the character's NFKC form, read exactly). Then primes, curly quotes and
    dashes become their plain spellings, the fraction slash becomes `/`, and two primes become one
    inch mark. Nothing else is folded: superscript and subscript digits are not digits the parser
    reads (`units/notation.py`). Nothing here is arithmetic: no number changes, only how it is
    spelled.
    """
    token = fold_fractions(token)
    for spelling, plain in _WRITTEN_MARKS:
        token = token.replace(spelling, plain)
    return token.replace("''", '"')


#: The fraction slash, `\u2044` (U+2044): the slash Unicode writes between a fraction's numerator and its
#: denominator. It means `/` and nothing else.
FRACTION_SLASH = "\u2044"


def fold_fractions(token: str) -> str:
    """Unicode's fraction characters and the fraction slash in their plain spelling (#907, #1199).

    `88\u00bd"` is `88 1/2"`, `\u215e` is `7/8`, `3\u20448` is `3/8`. A vulgar fraction is the character whose
    decomposition Unicode tags `<fraction>`, and that decomposition *is* its numerator, the fraction
    slash and its denominator, so the digits written in its place are the standard's own and no
    table here can hold a wrong one. Only the spelling changes, never the value.

    A space goes in front where a digit comes before it (`1\u00bd` is one and a half, never fifteen over
    two) and after it where a digit follows (`\u00bd2` is never one over twenty-two: it stays two
    numbers, which no parser reads as one). Nothing else is folded: superscript and subscript digits,
    LaTeX `\\frac`, a squeezed `163/8` and a fraction split over a line break stay as written.
    """
    if not any(
        character == FRACTION_SLASH or unicodedata.decomposition(character).startswith("<fraction>")
        for character in token
    ):
        return token
    written: list[str] = []
    for index, character in enumerate(token):
        if unicodedata.decomposition(character).startswith("<fraction>"):
            parts = unicodedata.normalize("NFKD", character).replace(FRACTION_SLASH, "/")
            if written and written[-1][-1:].isdigit():
                parts = f" {parts}"
            if token[index + 1 : index + 2].isdigit():
                parts = f"{parts} "
            written.append(parts)
        elif character == FRACTION_SLASH:
            written.append("/")
        else:
            written.append(character)
    return "".join(written)


#: A written fraction in a dimension, with the whole number written before it if there is one:
#: `16 3/8`, `16-3/8`, `163/8`, `3/8`. ASCII digits only: the parsers read nothing else, so a
#: full-width `１６３/８` is never a fraction of ours, only text that is not a dimension.
_WRITTEN_FRACTION_RE = re.compile(
    r"(?<![0-9/])(?:(?P<whole>[0-9]+)(?:\s+|\s*-\s*))?(?P<top>[0-9]+)\s*/\s*(?P<bottom>[0-9]+)"
    r"(?![0-9/])",
    re.ASCII,
)


def _squeezed_split(top: str, bottom: int) -> tuple[str, str] | None:
    """The first place `top` splits into a whole number and a proper fraction over `bottom`.

    `163` over 8 splits as `16` and `3`; `1615` over 16 as `16` and `15` (161 and 5 is possible
    too, which is why this is only ever a suggestion to a person, never a value). A part never starts
    with a zero, and the new top is never zero. `None` where no split gives a proper fraction.
    """
    for cut in range(1, len(top)):
        whole, rest = top[:cut], top[cut:]
        if rest.startswith("0") or whole.startswith("0"):
            continue
        if 0 < int(rest) < bottom:
            return whole, rest
    return None


def drawn_fraction_refusal(token: str) -> str | None:
    """Why a fraction written in a drawn dimension is not one a drawing prints, or `None`.

    **The rule the inch part of feet-and-inches already keeps** (`_inch_part`), for every fraction
    a reader or a drawing's text wrote: the bottom is 2, 4, 8, 16, 32 or 64, and the top is smaller
    than the bottom. A drawing writes nine eighths as `1 1/8`, never `9/8`.

    **Why it matters on the reading path (#1199).** `parse_imperial` reads any `a/b`, as it must for
    the values arithmetic produces (`format_inches` writes `38 94/127` for 984 mm and reads it back).
    But a reader that writes `163/8"` has almost certainly lost the space in `16 3/8"`, and read as
    written it is twenty and three-eighths. Splitting the digits would be a guess (`1615/16` is
    161 5/16 or 16 15/16), so the label goes to a person, even when both readers wrote it. The reason
    mentions a missing space only when the text looks like a squeezed mixed number: no whole number
    before the fraction, and a top that splits into one.

    For text a reader or a drawing printed only. Values the system wrote itself are never checked
    here; a value a person typed is checked by `typed_fraction_refusal`.
    """
    for found in _WRITTEN_FRACTION_RE.finditer(token):
        top, bottom = int(found.group("top")), int(found.group("bottom"))
        if not _drawn_denominator(bottom):
            return (
                f"the fraction {top}/{bottom} is over {bottom}; a drawing prints fractions over "
                "2, 4, 8, 16, 32 or 64 only, so a person reads it"
            )
        if top >= bottom:
            squeezed = found.group("whole") is None and _squeezed_split(found.group("top"), bottom)
            if squeezed:
                return (
                    f"the fraction {top}/{bottom} has a top that is not smaller than its bottom, "
                    "which a drawing never prints; the space between the whole number and the "
                    "fraction may be missing, so a person reads it"
                )
            return (
                f"the fraction {top}/{bottom} has a top that is not smaller than its bottom, which "
                "a drawing never prints, so a person reads it"
            )
    return None


def typed_fraction_refusal(text: str) -> str | None:
    """Why a value a person typed has a fraction no one writes, or `None` (#1199).

    Only a top that is not smaller than its bottom is refused: `163/8"` typed was stored as 20 3/8.
    Where the digits split into a whole number and a proper fraction, the refusal suggests the first
    split (`did you mean 16 3/8"?`); it is only a suggestion, and the person types the value again.

    **Never the powers-of-two rule.** The system writes its own values back into the form, exactly:
    984 mm is `38 94/127"` (`format_inches`), and that text must read back unchanged.
    """
    for found in _WRITTEN_FRACTION_RE.finditer(plain_marks(text)):
        top_text, bottom = found.group("top"), int(found.group("bottom"))
        if bottom == 0 or int(top_text) < bottom:
            continue
        refusal = (
            f"the fraction {int(top_text)}/{bottom} has a top that is not smaller than its bottom, "
            "which nobody writes"
        )
        split = None if found.group("whole") is not None else _squeezed_split(top_text, bottom)
        if split is not None:
            whole, rest = split
            return f'{refusal}; did you mean {whole} {rest}/{bottom}"?'
        return f'{refusal}; type the whole number and the fraction apart, as 1 1/4"'
    return None


def _inch_part(remainder: str, text: str) -> Fraction:
    """The inch part of a feet-and-inches dimension, exactly, or a refusal saying why.

    Stricter than an inch token on its own, because the form is: the whole inches are under a foot
    (`3'-14"` is not how anyone writes 4'-2"), a fraction is proper (`3'-61/2"` is a misprint, not
    thirty and a half inches), and its denominator is a power of two no larger than sixty-four (a
    drawing never prints thirds). A misprint read as something near would be a wrong number on the
    side of the comparison that decides; refused, it goes to a person.
    """
    part = _INCH_PART_RE.fullmatch(remainder)
    if part is None:
        raise UnitNormalisationError(
            f"unreadable inch part of a feet-and-inches dimension: {text!r}"
        )
    if part.group("decimal") is not None:
        exact = Fraction(part.group("decimal"))
    else:
        whole = Fraction(int(part.group("whole") or 0))
        numerator = part.group("num") or part.group("alone_num")
        denominator = part.group("den") or part.group("alone_den")
        exact = whole
        if numerator is not None and denominator is not None:
            top, bottom = int(numerator), int(denominator)
            if not _drawn_denominator(bottom):
                raise UnitNormalisationError(
                    f"{text!r} has a fraction over {bottom}; a drawn fraction is over a power of two "
                    f"no larger than {MAXIMUM_DENOMINATOR}"
                )
            if top >= bottom:
                raise UnitNormalisationError(
                    f"{text!r} has an improper fraction {top}/{bottom} in its inch part"
                )
            exact += Fraction(top, bottom)
    if exact >= INCHES_PER_FOOT:
        raise UnitNormalisationError(
            f"{text!r} has {exact} inches after its feet; the inch part of a feet-and-inches "
            "dimension is under a foot"
        )
    return exact


def _drawn_denominator(denominator: int) -> bool:
    """Whether a fraction's denominator is one a drawing prints: 2, 4, 8, 16, 32 or 64."""
    return 2 <= denominator <= MAXIMUM_DENOMINATOR and denominator & (denominator - 1) == 0


def inches_from_mm(millimetres: Fraction | int | str) -> Fraction:
    """Convert millimetres to inches exactly.

    Separate from `normalise_to_inches` for callers that already hold a number and its unit and have
    no token to parse. Same ratio, same refusal to round.
    """
    return Fraction(millimetres) * INCHES_PER_MM


def inches_from_feet(feet: Fraction | int | str) -> Fraction:
    """Convert feet to inches exactly."""
    return Fraction(feet) * INCHES_PER_FOOT
