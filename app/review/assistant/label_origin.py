"""Which dimension labels are OCR readings with no line of their own (#1830, #1848).

Most labels are read from a drawing's text or proved by its vectors (`dim_labels`). Two kinds are
read by two OCR engines from pixels only: a number inside a pasted picture (`origin: "picture"`,
`picture_labels`) and a number on a vector page that no label reader read but both engines read
the same at its spot (`origin: "ocr"`, `ocr_labels`). Both are the drawn-ink witness and never
more: they have no dimension line, so they never pass a geometry, anchor, pairing or same-ends
check on a line, and their own ink is looked at for GV's colour. This module has no imports, so
every check that treats them alike can ask it.

Verification: `tests/review/assistant/test_ocr_labels.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = ["OCR", "OCR_ORIGINS", "PICTURE", "ocr_read"]

#: Read inside a pasted picture (`picture_labels.ORIGIN`).
PICTURE: Final = "picture"
#: Read at its spot on a vector page by two OCR engines (`ocr_labels.ORIGIN`).
OCR: Final = "ocr"
OCR_ORIGINS: Final = frozenset({PICTURE, OCR})


def ocr_read(label: Mapping[str, Any]) -> bool:
    """Whether a label is an OCR reading of pixels (a picture's or a vector page's), with no line."""
    return label.get("origin") in OCR_ORIGINS
