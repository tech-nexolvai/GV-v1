"""The dashes a drawing prints inside a code (`XX-213`), and one way to read them all as a hyphen.

A drawing's text layer does not always carry the ASCII hyphen-minus the drafter typed. A CAD font,
a PDF writer or a word processor may set the same stroke as a minus sign (U+2212), a hyphen
(U+2010), a non-breaking hyphen (U+2011), a figure dash (U+2012), an en dash (U+2013), an em dash
(U+2014), a horizontal bar (U+2015), or the small or full-width hyphen-minus of an East Asian font
(U+FE58, U+FE63, U+FF0D). On paper they look the same; to a pattern that spells `-` they are other
characters. 2026-11-03: an architect's file printed its item tags with the minus sign (`XX−213`)
while the vendor printed the hyphen (`XX-213`), so code read no tag on the architect's pages and
paired none of the vendor's pages of that kind with the architect's, although the architect printed
exactly the vendor's tags.

Every reader of a code printed on a drawing (an item or finish tag, a sheet number, a view
reference, a cabinet code) reads the text through `dashes_to_hyphen`, or matches a dash with
`DASH`, so every look-alike is the hyphen. The map is one character for one character: a position
in the normalised text is the same position in the printed text.

**Codes only, never sizes.** A feet-and-inches size (`3’ − 6"`) is read by `units/notation.py` and
`units/normalise.py` with their own marks; nothing here changes how a size is read.

Like the rest of `vocabulary/`, this module imports nothing from the project.
"""

from __future__ import annotations

from typing import Final

__all__ = ["DASH", "LOOKALIKE_DASHES", "dashes_to_hyphen"]

#: Every character a drawing may print for the hyphen of a code, the ASCII hyphen-minus left out.
LOOKALIKE_DASHES: Final = (
    "‐"  # hyphen
    "‑"  # non-breaking hyphen
    "‒"  # figure dash
    "–"  # en dash
    "—"  # em dash
    "―"  # horizontal bar
    "−"  # minus sign
    "﹘"  # small em dash
    "﹣"  # small hyphen-minus
    "－"  # full-width hyphen-minus
)
#: A regular-expression class matching the hyphen or any look-alike (`rf"[A-Z]{{2}}{DASH}\d+"`).
DASH: Final = f"[-{LOOKALIKE_DASHES}]"

_TO_HYPHEN: Final = str.maketrans(dict.fromkeys(LOOKALIKE_DASHES, "-"))


def dashes_to_hyphen(text: str) -> str:
    """`text` with every look-alike dash written as the ASCII hyphen (`XX−213` is `XX-213`); the
    same length, so a match's position is the same in both."""
    return text.translate(_TO_HYPHEN)
