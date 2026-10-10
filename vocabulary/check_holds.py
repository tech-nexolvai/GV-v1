"""Why a countertop row is read but not checked: its stone does not end at the walls.

Shared by the reader (`extraction/slot_reader/`), which flags the row `check-hold:<code>`, and by the
row-scoped check (`workflow/slot_row_scope.py`), which shows the reason and abstains. Kept here,
outside `extraction/`, because the control plane may reach the second and never the first
(`tests/api/test_no_heavy_work.py`).

The client lead's field cut is added to wall-to-wall where the stone meets a wall. Where the stone stops at
full-height fillers or panels, they take it; where it runs into wall pockets, the pocket detail
decides (GV-Brain "Field cut - when it applies", 2026-10-08). The readings stand either way; only
the width check's arithmetic does not apply.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final, overload

STONE_SHORT_OF_ENDS: Final = (
    "stone-short-of-ends",
    (
        "the stone stops at fillers or panels before the row's ends; the field cut belongs to "
        "them, so the reviewer checks the stone width"
    ),
)
STONE_INTO_WALLS: Final = (
    "stone-into-walls",
    (
        "the stone runs into the walls (pockets); the check needs the pocket detail, so the "
        "reviewer checks it"
    ),
)

#: Code → the reviewer's words, for every check hold.
CHECK_HOLD_REASONS: Final = {
    code: reason for code, reason in (STONE_SHORT_OF_ENDS, STONE_INTO_WALLS)
}

#: A counter-break reader that said no stone top is drawn over the row (`no_stone`, #1111) is
#: recorded on the row's candidates as `no-stone:<model>`. It is a note, never a hold: it neither
#: holds a row nor clears one, and it is named only in a reason the row already has.
NO_STONE_FLAG: Final = "no-stone:"
NO_STONE_NOTE: Final = "A reader also said no stone top is drawn over this row."


@overload
def with_no_stone_note(reason: str, flags: Iterable[str]) -> str: ...
@overload
def with_no_stone_note(reason: None, flags: Iterable[str]) -> None: ...
@overload
def with_no_stone_note(reason: str | None, flags: Iterable[str]) -> str | None: ...
def with_no_stone_note(reason: str | None, flags: Iterable[str]) -> str | None:
    """The row's reason with the no-stone note added; `None` stays `None` (a note adds no hold)."""
    if reason is None or NO_STONE_NOTE in reason:
        return reason
    if not any(flag.startswith(NO_STONE_FLAG) for flag in flags):
        return reason
    return f"{reason} {NO_STONE_NOTE}"
