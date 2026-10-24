"""PDF text with a backslash before an ordinary character: the character is kept.

**Why.** A PDF writes each piece of text as a string, and inside it a backslash starts an escape
(`\\n`, `\\(`, `\\\\`, an octal code like `\\053`). For any other character the PDF reference says
the backslash is ignored and the character kept (PDF 1.7, 7.3.4.2): `(15 1\\/2")` is `15 1/2"`.
pdfminer.six (the parser under pdfplumber, every release up to and including 20260107) drops the
character as well, so that label came out as `15 12"` and its `(\\[394mm\\])` as `394mm`. Some CAD
programs escape every `/`, `[` and `]` this way: on one real set of countertop shop drawings
nearly every fraction lost its slash (`5/32"` came out as `532"`, `7 1/4"` as `7 14"`), the
millimetres beside it lost their brackets, and the label reader listed the wrong inches.

This is a reading fix, not a guess: the character is in the file. Nothing is inferred from the
millimetres or the drawing.

**How.** Importing this module replaces pdfminer's `PSBaseParser._parse_string_1` (the step after
a backslash inside a string) with one that keeps an ordinary character and hands every other case
(the named escapes, octal codes, a backslash before a line end, which joins two lines) to
pdfminer's own. It is installed only when a probe shows pdfminer drops the character, so a fixed
pdfminer release is left alone, and installing twice does nothing. `extraction`, `app`,
`workflow` and `reports` import it in their `__init__`, so every reader that opens a PDF with
pdfplumber in this repo gets it (`tests/extraction/test_pdf_strings.py` checks they do).

pdfium (`pypdfium2`) and pikepdf parse strings themselves and were never affected.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any, Final

try:
    from pdfminer import psparser
except ImportError:  # the `extraction` extra is not installed: nothing reads PDFs here
    psparser = None  # type: ignore[assignment]

__all__ = ["drops_escaped", "install", "installed"]

#: Marks the replacement this module installed.
_MARK: Final = "__gv_keeps_escaped__"
#: After a backslash, a line end joins two lines: it is dropped (pdfminer's own handles it).
_LINE_ENDS: Final = (b"\r", b"\n")


def drops_escaped() -> bool:
    """Whether pdfminer, as it is now, reads `(1\\/2)` without its slash."""
    if psparser is None:
        return False
    _, token = psparser.PSBaseParser(BytesIO(b"(1\\/2)")).nexttoken()
    return token != b"1/2"


def installed() -> bool:
    """Whether this module's replacement is in place."""
    return psparser is not None and bool(
        getattr(psparser.PSBaseParser._parse_string_1, _MARK, False)
    )


def install() -> None:
    """Put the replacement in place when pdfminer drops an escaped ordinary character."""
    if psparser is None or installed() or not drops_escaped():
        return
    original = psparser.PSBaseParser._parse_string_1

    def _parse_string_1(self: Any, s: bytes, i: int) -> int:
        c = s[i : i + 1]
        if (
            c
            and not self.oct
            and c not in psparser.ESC_STRING
            and c not in _LINE_ENDS
            and not psparser.OCT_STRING.match(c)
        ):
            self._curtoken += c  # the backslash is ignored, the character kept
            self._parse1 = self._parse_string
            return i + 1
        return original(self, s, i)

    setattr(_parse_string_1, _MARK, True)
    psparser.PSBaseParser._parse_string_1 = _parse_string_1  # type: ignore[method-assign]


install()
