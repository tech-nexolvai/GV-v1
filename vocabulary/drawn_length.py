"""When a reading's drawn length could not be checked, and what the reviewer is told (#1107).

The drawn-length check (`extraction/slot_reader/veto.py`) is the non-model witness behind a sealed
vendor width: the value must fit the length actually drawn, at the scale the row's other pieces
give. With fewer than two other pieces to take a scale from, it cannot run. Such a value still
seals on the two readers' identical text (DECIDED 2026-10-09, #1107), but never silently:

* the reader flags the value `no-drawn-length-witness` (`NO_DRAWN_LENGTH_WITNESS`);
* the countertop results and the signed report say "drawn length not checked (no scale)" for the
  row (`not_checked_note`);
* a width PASS resting on such a value waits for one reviewer click (`NO_WITNESS_REASON`), with the
  engine's PASS kept in the notes (`engine_result_note`). A FAIL already waits for one.

Shared by the reader (`workflow/slot_reader.py`), the width check (`workflow/stages.py`) and the
control plane (`app/api/visual_countertops.py`), which may never reach `extraction/`
(`tests/api/test_no_heavy_work.py`); so it lives here and imports nothing from the project.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

#: The flag on a sealed reading the drawn-length check could not reach (no scale in its row).
NO_DRAWN_LENGTH_WITNESS: Final = "no-drawn-length-witness"
#: The words the results and the signed report use for such a reading.
DRAWN_LENGTH_NOT_CHECKED: Final = "drawn length not checked (no scale)"
#: Why a width PASS resting on such a reading waits for the reviewer.
NO_WITNESS_REASON: Final = (
    "The values agree, but their drawn length could not be checked on the drawing; confirm."
)


def not_checked_note(positions: Iterable[int | None]) -> str | None:
    """One line naming the row's readings whose drawn length was not checked; `None` for none.

    `positions` are slots from 0 (`None` for the overall); pieces are named from 1, as the row
    shows them, then the overall.
    """
    chosen = set(positions)
    if not chosen:
        return None
    pieces = sorted(position for position in chosen if position is not None)
    names = [f"piece {position + 1}" for position in pieces]
    if None in chosen:
        names.append("the overall")
    return f"{DRAWN_LENGTH_NOT_CHECKED.capitalize()}: {', '.join(names)}."


def engine_result_note(outcome: str, reason: str) -> str:
    """The engine's own result, kept in the notes while the reviewer confirms the values."""
    return (
        "The engine's result, which counts only once a person confirms the values: "
        f"{outcome} — {reason.strip()}"
    )


__all__ = [
    "DRAWN_LENGTH_NOT_CHECKED",
    "NO_DRAWN_LENGTH_WITNESS",
    "NO_WITNESS_REASON",
    "engine_result_note",
    "not_checked_note",
]
