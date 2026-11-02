"""Which dimension labels are OCR readings with no line of their own (#1830, #1848, #1750).

Most labels are read from a drawing's text or proved by its vectors (`dim_labels`). Two kinds are
read by two OCR engines from pixels only: a number inside a pasted picture (`origin: "picture"`,
`picture_labels`) and a number on a vector page that no label reader read but both engines read
the same at its spot (`origin: "ocr"`, `ocr_labels`). A third is lower still: a number in a scan's
own OCR text, which the scanner's software laid over the picture (`origin: "scan"`,
`visible_text`, #1750): one unknown program's reading, never typed text, and a doubt for any red
(`reading_routes`). All are the drawn-ink witness at most: they have no dimension line, so they
never pass a geometry, anchor, pairing or same-ends check on a line, and their own ink is looked
at for GV's colour. This module has no imports, so every check that treats them alike can ask it.

Verification: `tests/review/assistant/test_ocr_labels.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = ["OCR", "OCR_ORIGINS", "PICTURE", "SCAN", "ocr_read"]

#: Read inside a pasted picture (`picture_labels.ORIGIN`).
PICTURE: Final = "picture"
#: Read at its spot on a vector page by two OCR engines (`ocr_labels.ORIGIN`).
OCR: Final = "ocr"
#: Read from a scan's own OCR text, the scanner's software's reading (`dim_labels._scan_labels`).
SCAN: Final = "scan"
OCR_ORIGINS: Final = frozenset({PICTURE, OCR, SCAN})


def ocr_read(label: Mapping[str, Any]) -> bool:
    """Whether a label is an OCR reading of pixels (a picture's or a vector page's), with no line."""
    return label.get("origin") in OCR_ORIGINS
