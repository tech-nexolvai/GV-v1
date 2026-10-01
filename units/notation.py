"""How the client's drawings write a dimension, rewritten into the form `normalise_to_inches` parses.

**One module, because four places read these notations** (#733): the vision reader's shape check
(`extraction/models/validation.py`), the value a vision reading is stored with (`workflow/stages.py`),
the value text read from the PDF is stored with (`app/evidence/record.py`), and the answer key and
bake-off scorer (`scripts/author_reading_answer_key.py`, `eval/experiments/model_bakeoff.py`). Before
this each had its own idea of a written dimension, and they disagreed: a model's `8'-6''` passed the
shape check and was then stored with no value, because only the check knew `''` meant inches.

It sits beside `units/dual.py` because that module already owns one notation — the dual token — and
this defers to it rather than growing a second definition. `normalise_to_inches` does not change; it
is the one parser the verdict path shares, and nothing here decides anything. It only lets a written
dimension reach the parser that does.

**Nothing here is arithmetic.** Each rule drops or re-spaces characters the drawing uses and the parser
does not. None computes a value, converts a unit, or adds two numbers together. The moment one does, a
reading stops being a transcription of what was read and becomes a claim of our own.

Measured on the 17-page client set (#733): of the dimensions the PDF's own text carries, the evidence
lane valued 17 before this and 38 after. Twenty of the twenty-one recovered were hyphenated fractions —
the notation GV's reviewers write in their markup.
"""

from __future__ import annotations

import re
from typing import Final

from units.dual import DualDimensionParseError, parse_dual

__all__ = ["canonical_notation", "is_compound"]

#: Marks that mean inches or feet and nothing else, spelled the way the parser knows them.
#:
#: `''` is how the inch double-prime has been typed on typewriters and in plain ASCII for a century;
#: `minicpm-v`, asked for `8' - 6"`, returned `8'-6''`. `″` and `′` are the typographic primes. Each is
#: a transcription equivalence — no number changes.
#:
#: `’` and `”` are the curly quotes a PDF font writes for the straight ones: a Type 1 font's standard
#: encoding maps the ASCII apostrophe to `quoteright`, so a vendor's `2' -5"` reaches the reader as
#: `2’ -5"` (formats phase 1, measured on the client's stamps). `−` (minus) and `–` (en dash) are the
#: hyphen of a feet-and-inches label as a drawing font sets it: `3’ − 6"`. `’` is mapped before `''`
#: is, so two curly quotes written as an inch mark become one inch mark, as two straight ones do.
_MARK_SPELLINGS: Final = (
    ("″", '"'),
    ("′", "'"),
    ("’", "'"),
    ("”", '"'),
    ("−", "-"),
    ("–", "-"),
    ("''", '"'),
)

#: `39 1/4"+6"` — two dimensions and an operator. Not a value, and not illegible either. An inch mark
#: must precede the operator, which is what keeps `2' - 10"` (feet-inches) out of it.
_COMPOUND: Final = re.compile(r'"\s*[+−-]\s*\d')

#: `2" (VIF)`, `181 1/4" (4EQ)` — an exact value followed by a site instruction. The note is not part
#: of the number.
_TRAILING_NOTE: Final = re.compile(r"\s*\([^)]*\)\s*$")

#: `381mm [15"]` — a dual-unit token with its unit word written out, as a model tends to return it.
#: Dropping the word lets `units.dual.parse_dual` read it; that function owns what a dual token *is*.
_UNIT_WORD_BEFORE_BRACKET: Final = re.compile(r"^(\s*\d+)\s*mm\s*(\[)", re.IGNORECASE)

#: `25-1/2"` — a hyphen where the trade writes a space. Not feet-inches: `6'-0"` keeps its hyphen, which
#: is why the pattern refuses to fire when a foot mark precedes the number.
_HYPHENATED_FRACTION: Final = re.compile(r"(?<!')\b(?P<whole>\d+)-(?P<fraction>\d+/\d+)")


def _marks(token: str) -> str:
    for spelling, canonical in _MARK_SPELLINGS:
        token = token.replace(spelling, canonical)
    return token


def is_compound(token: str) -> bool:
    """Whether the token is two dimensions and an operator — `39 1/4"+6"` — rather than one value."""
    return _COMPOUND.search(_marks(token)) is not None


def canonical_notation(token: str) -> tuple[str, str | None]:
    """Rewrite one written dimension into the form `normalise_to_inches` accepts.

    Returns the rewritten token and the millimetre reading a dual-unit token carried, if any, so a
    caller can keep it as corroboration without it ever becoming the value. **The inch half of a dual
    token is the value** (Q12): millimetres on a GV drawing are the vendor's machine reference and
    never a verdict operand.

    A compound is not rewritten — there is no single value to rewrite it into. Callers check
    `is_compound` first and refuse it as not one value.

    The caller keeps the characters it was given. This returns a string to *parse*; a reviewer
    comparing a reading with its crop must still see what was actually written or returned.
    """
    token = _marks(token)
    mm: str | None = None
    try:
        dual = parse_dual(_UNIT_WORD_BEFORE_BRACKET.sub(r"\1 \2", token).strip())
    except DualDimensionParseError:
        dual = None
    if dual is not None and dual.alternate is not None:
        mm = dual.primary.raw_text
        inch = (dual.alternate.raw_text or "").strip()
        token = inch if inch.endswith('"') else f'{inch}"'
    token = _TRAILING_NOTE.sub("", token)
    token = _HYPHENATED_FRACTION.sub(r"\g<whole> \g<fraction>", token)
    return token.strip(), mm
