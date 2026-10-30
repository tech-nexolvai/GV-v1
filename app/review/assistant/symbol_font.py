"""The symbol font some offices print a drawing's words in (2026-10-31).

A drawing's text layer can store each character of a word as a private-use code point, U+F020 to
U+F07E: the font draws code `0xF000 + c` as the ASCII character `c` (a "symbol" font, as some CAD
programs write their text). The page shows `LIBRARY`; its text layer gives seven private-use
characters, and code reading the words (an item tag, a view's title, a room's name, the set
index's printed text) sees none of them. On one real design set most of the words were printed so:
every room name of its furniture plans, and many of its sizes.

`decoded` gives back the ASCII each such character stands for, and touches nothing else (another
private-use character stays as it is). Every place that reads a PDF's words for tags, titles,
pairing or the set index reads them through it (`under_markup`, `set_pack`, `verify_value`).
"""

from __future__ import annotations

from typing import Final

__all__ = ["FIRST", "LAST", "OFFSET", "decoded"]

#: The private-use characters of the symbol font: `0xF000 + c` for each printable ASCII `c`
#: (space to `~`).
FIRST: Final = 0xF020
LAST: Final = 0xF07E
OFFSET: Final = 0xF000
_TABLE: Final = {code: code - OFFSET for code in range(FIRST, LAST + 1)}


def decoded(text: str) -> str:
    """`text` with each symbol-font character (U+F020 to U+F07E) as the ASCII it stands for;
    every other character as it is."""
    return text.translate(_TABLE)
