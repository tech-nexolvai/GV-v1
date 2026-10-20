"""Size callouts a product pack teaches the label reader: a diameter and a "W x H" pair (#1776).

Light fixtures, mirrors and artwork print sizes the kitchen's reader leaves out: `Ø24"` (a round
fixture or mirror) and `24"x36"` / `24"Wx36"H` written as one word (a framed piece). `to_inches`
rightly refuses both as one value; this module splits them, so a check of a product whose pack lists
the format (`size_formats: [diameter, w_x_h]`) gets one label per size:

- `diameter`: `Ø24"`, `⌀ 24"`, `24"Ø`, `DIA24"` -> 24 (the label keeps its printed text);
- `w_x_h`: `24"x36"`, `24x36"`, `24"Wx36"H` -> 24 and 36, each its own characters (an inch mark
  is needed: `2x4` is lumber).

Only the formats of the ACTIVE products are read (`formats()`), so a kitchen-only registry reads its
drawings exactly as before. Pure code.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Final

from app.review.assistant.finding_checks import to_inches
from vocabulary.product_registry import registry

__all__ = ["diameter", "formats", "split_w_x_h"]

_DIAMETER: Final = re.compile(r"^(?:[Ø⌀∅]|DIA\.?)\s*(?P<a>.+)$|^(?P<b>.+?)\s*[Ø⌀∅]$", re.IGNORECASE)
#: The times sign between two sizes, with an optional letter after each size (W, H, L, D).
_TIMES: Final = re.compile(r"^(?P<one>.*?\d.*?)\s*[WLHD]?\s*[xX×]\s*(?P<two>\d.*?)\s*[WLHD]?$")
_LETTER: Final = re.compile(r"\s*[WLHDwlhd]\s*$")


def formats() -> frozenset[str]:
    """The size formats every active product's pack asks the reader for."""
    return frozenset(f for p in registry().active() for f in p.size_formats)


def _inches(text: str) -> Decimal | None:
    text = _LETTER.sub("", text.strip())
    value = to_inches(text)
    return value if value is not None and value > 0 else None


def diameter(text: str) -> Decimal | None:
    """The size a diameter callout prints (`Ø24"` -> 24), or None."""
    found = _DIAMETER.match(" ".join(text.split()))
    if found is None:
        return None
    return _inches(found.group("a") or found.group("b") or "")


def split_w_x_h(text: str, chars: Sequence[Any]) -> list[tuple[str, Decimal, list[Any]]]:
    """A "W x H" callout written as one word, as two sizes with their own characters: (text,
    inches, chars) each; [] when it is not one. `chars` are the word's characters in reading
    order (each with `.text`)."""
    plain = "".join(str(c.text) for c in chars)
    if _TIMES.match(plain) is None or '"' not in plain:
        return []  # "2x4 BLOCKING" is lumber, not a size: a callout prints its inch marks
    at = next(
        (
            n
            for n, c in enumerate(chars)
            if str(c.text) and str(c.text) in "xX×" and any(ch.isdigit() for ch in plain[:n])
        ),
        None,
    )
    if at is None:
        return []
    out: list[tuple[str, Decimal, list[Any]]] = []
    for part in (list(chars[:at]), list(chars[at + 1 :])):
        while part and _LETTER.fullmatch(str(part[-1].text)):
            part.pop()
        words = "".join(str(c.text) for c in part).strip()
        value = _inches(words) if words else None
        if value is None:
            return []
        out.append((words, value, part))
    return out
