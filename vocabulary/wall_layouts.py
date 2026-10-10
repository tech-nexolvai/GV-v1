"""Plain words for the published wall layouts, said the same on every screen, finding and report.

The rule decides with the layout's code (`rules/rulebook/ct_width_001.yaml`); these words are only
what a reviewer reads. Field cut is 1 inch per wall end (settled with the client lead), so each layout's words
say how many ends stand against a wall.

**Stone between panels.** A row whose stone stops at full-height panels or fillers before its ends
is confirmed with the layout `back_only` because the panels, not the stone, take the field cut. Its
words are therefore "no field cut: the stone stops at panels", never "back wall only" (#1138):
nothing was said about the walls.

Source: issue #1138 · Verification: `tests/workflow/test_wall_layout_words.py`
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from vocabulary.check_holds import STONE_SHORT_OF_ENDS

__all__ = [
    "BETWEEN_PANELS_WORDS",
    "WALL_LAYOUT_WORDS",
    "is_between_panels",
    "wall_layout_words",
]

#: Every layout CT-WIDTH-001 publishes, in plain words.
WALL_LAYOUT_WORDS: Final = {
    "back_left_right": "back wall and both ends",
    "back_and_left": "back wall and left end",
    "back_and_right": "back wall and right end",
    "back_only": "back wall only; no field cut at the ends",
    "island": "island; no wall ends",
}

BETWEEN_PANELS_WORDS: Final = "no field cut: the stone stops at panels"

_BETWEEN_PANELS_FLAG: Final = f"check-hold:{STONE_SHORT_OF_ENDS[0]}"


def is_between_panels(flags: Iterable[str]) -> bool:
    """Whether a row's candidate flags say its stone stops at panels before its ends."""
    return _BETWEEN_PANELS_FLAG in set(flags)


def wall_layout_words(value: str, *, between_panels: bool = False) -> str:
    """The reviewer's words for a layout; an unknown code is spaced out, never dropped."""
    if between_panels and value == "back_only":
        return BETWEEN_PANELS_WORDS
    return WALL_LAYOUT_WORDS.get(value, value.replace("_", " "))
