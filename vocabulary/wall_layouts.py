"""Plain words for the published wall layouts, said the same on every screen, finding and report.

The rule decides with the layout's code (`rules/rulebook/ct_width_001.yaml`); these words are only
what a reviewer reads. Field cut is 1 inch per wall end (settled with the client lead), so each layout's words
say how many ends stand against a wall.

**Stone between panels.** A row whose stone stops at full-height panels or fillers before its ends
is confirmed with the layout `back_only` because the panels, not the stone, take the field cut. Its
words are therefore "no field cut: the stone stops at panels", never "back wall only" (#1138):
nothing was said about the walls.

**Who set the walls, and the field-cut rule in plain words (#1202).** The automatic layout's
source, the results line ("seen on the drawing at both ends and confirmed by both AIs", "chosen by
<reviewer>", "not needed: wrong for every wall case") and the one-line field-cut rule printed in the
signed PDF and workbook are written here once, so every screen and report says the same.

Source: issues #1138, #1202 · Verification: `tests/workflow/test_wall_layout_words.py`
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from vocabulary.check_holds import STONE_SHORT_OF_ENDS

__all__ = [
    "AUTOMATIC_WALLS_LINE",
    "AUTOMATIC_WALL_SOURCE",
    "BETWEEN_PANELS_WORDS",
    "FIELD_CUT_RULE",
    "NOT_NEEDED_FAIL_LINE",
    "NOT_NEEDED_PASS_LINE",
    "READERS_WALL_SOURCE",
    "WALL_CASE_WORDS",
    "WALL_LAYOUT_WORDS",
    "chosen_by_line",
    "field_cut_footnote",
    "is_between_panels",
    "wall_case_words",
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


#: Who set a row's walls (#1202). The one automatic source: code saw wall ink at both row ends and
#: both AIs said a wall at both ends, and nothing said the stone stops at panels or pockets. Every
#: layout the readers agreed is the `READERS_WALL_SOURCE` proposal, which the reviewer confirms.
AUTOMATIC_WALL_SOURCE: Final = "code-ink-and-both-ais"
READERS_WALL_SOURCE: Final = "readers"

#: Each wall case in the words of the field-cut rule: how many ends meet a wall (#1202).
WALL_CASE_WORDS: Final = {
    "back_left_right": "walls at both ends",
    "back_and_left": "wall at the left end",
    "back_and_right": "wall at the right end",
    "back_only": "back wall only",
    "island": "island",
}

#: The results line: which walls were used, and who decided (#1202).
AUTOMATIC_WALLS_LINE: Final = (
    "walls at both ends — seen on the drawing at both ends and confirmed by both AIs"
)
NOT_NEEDED_FAIL_LINE: Final = "not needed: wrong for every wall case"
NOT_NEEDED_PASS_LINE: Final = "not needed: right for every wall case"

#: The rule itself, in one plain sentence; the company's own rule, set per project.
FIELD_CUT_RULE: Final = (
    "GV's field-cut rule: where the stone meets a wall, it is made longer by the field cut and "
    "trimmed on site to fit."
)


def wall_case_words(value: str, *, between_panels: bool = False) -> str:
    """A wall case in the rule's words; `back_only` between panels is "no field cut" (#1138)."""
    if between_panels and value == "back_only":
        return BETWEEN_PANELS_WORDS
    return WALL_CASE_WORDS.get(value, value.replace("_", " "))


def chosen_by_line(value: str, reviewer: str, *, between_panels: bool = False) -> str:
    """The results line for a layout a reviewer chose for this row."""
    return f"{wall_case_words(value, between_panels=between_panels)} — chosen by {reviewer}"


def field_cut_footnote(per_end: str | None) -> str:
    """The one-line rule under the countertops in the signed PDF and workbook (#1202).

    `per_end` is the project's field cut as the results show it (`1"`), or `None` when no result
    names it; the line then says "the field cut" and no number. No sum is worked out here.
    """
    cut = per_end or "the field cut"
    value = f"{per_end} is this project's field cut. " if per_end else ""
    return (
        f"Field cut: at each end where the stone meets a wall, it is made {cut} longer and trimmed "
        "on site. "
        f"Walls at both ends: {cut} at each end. Wall at one end: {cut}. No end walls (back wall "
        f"only, island, or the stone stops at side panels): no field cut. {value}"
    ).strip()
