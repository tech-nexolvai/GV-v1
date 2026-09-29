"""How the client's drawings write a dimension, rewritten into the form `units/` already parses.

**One module, because two places read these notations** — the answer-key tool, from what a person
typed, and the bake-off scorer, from what a model returned. Two canonicalisers are how two readings of
the same printed number come to disagree: the key would record `25-1/2"` as a value and the scorer
would refuse a model that returned exactly that (#730, #732).

**It lives in `eval/`, not `units/`.** `units/` is on the verdict path, and a transcription
convenience does not move it. Nothing here decides anything; it only lets a written dimension reach
the parser that does.

**Nothing here is arithmetic.** Each rule drops or re-spaces characters the drawing uses and the
parser does not. None computes a value, converts a unit, or adds two numbers together. The moment one
does, a reading stops being a transcription of what was read and becomes a claim of our own.
"""

from __future__ import annotations

import re
from typing import Final

from units.dual import DualDimensionParseError, parse_dual

__all__ = ["canonical_notation", "is_compound"]

#: `39 1/4"+6"` — two dimensions and an operator. Not a value, and not illegible either. An inch mark
#: must precede the operator, which is what keeps `2' - 10"` (feet-inches) out of it.
_COMPOUND: Final = re.compile(r'["″]\s*[+−-]\s*\d')

#: `2" (VIF)` — an exact value followed by a site instruction. The note is not part of the number.
_TRAILING_NOTE: Final = re.compile(r"\s*\([^)]*\)\s*$")

#: `381mm [15"]` — a dual-unit token with its unit word written out, as a model tends to return it.
#: Dropping the word lets `units.dual.parse_dual` read it; that function owns what a dual token *is*.
_UNIT_WORD_BEFORE_BRACKET: Final = re.compile(r"^(\s*\d+)\s*mm\s*(\[)", re.IGNORECASE)


#: `25-1/2"` — a hyphen where the trade writes a space. Not feet-inches: `6'-0"` keeps its hyphen,
#: which is why the pattern refuses to fire when a foot mark precedes it.
_HYPHENATED_FRACTION: Final = re.compile(r"(?<![′'])\b(?P<whole>\d+)-(?P<fraction>\d+/\d+)")


def is_compound(token: str) -> bool:
    """Whether the token is two dimensions and an operator — `39 1/4"+6"` — rather than one value."""
    return _COMPOUND.search(token) is not None


def canonical_notation(token: str) -> tuple[str, str | None]:
    """Rewrite one written dimension into the form `units/` accepts.

    Returns the rewritten token and the millimetre reading a dual-unit token carried, if any, so a
    caller can keep it as corroboration without it ever becoming the value.

    A compound is not rewritten — there is no single value to rewrite it into. Callers check
    `is_compound` first and refuse it as not one value.
    """
    mm: str | None = None
    # **A dual token is read by `units.dual.parse_dual`, not by a second regex here** (#732). That
    # module already says why: two definitions of one token shape is how they come to disagree about
    # what a dual dimension is. Its inch half is the value — Q12 makes the inch the governing reading —
    # and the millimetres are handed back as corroboration, never as the value.
    try:
        dual = parse_dual(_UNIT_WORD_BEFORE_BRACKET.sub(r"\1 \2", token).strip())
    except DualDimensionParseError:
        dual = None
    if dual is not None and dual.alternate is not None:
        mm = dual.primary.raw_text
        inch = (dual.alternate.raw_text or "").strip()
        token = inch if inch.endswith(('"', "\u2033")) else f'{inch}"'
    token = _TRAILING_NOTE.sub("", token)
    token = _HYPHENATED_FRACTION.sub(r"\g<whole> \g<fraction>", token)
    return token.strip(), mm
