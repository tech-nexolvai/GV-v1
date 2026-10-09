"""Which drawing on a combined sheet is the ID set's and which is the vendor's, read from the sheet's
own labels (#710).

**A package always has one architectural drawing and one shop drawing to compare.** They arrive in
one of two ways: two files, one of each, where the upload says which is which; or one combined
sheet carrying both. On the client's combined sheets each drawing is a stamp, and the sheet labels
each one in plain text — `ID SET ELEVATION` above the architect's drawing, `VENDOR'S SHOP DRAWING
ELEVATION` above the vendor's — so the role can be read, not guessed.

**The label is exact text, so no model is asked.** ADR-0020 expected the headings to be drawn as
outlines a model would have to read. On `AI_Set_2.pdf` they are text notes, the same two on every
content page, so reading them costs nothing and cannot misread.

**A label heads everything below it, down to the next label.** On the client's layout
`ID SET ELEVATION` sits at the top of the page and `VENDOR'S SHOP DRAWING ELEVATION` halfway down,
as a divider, with the vendor's drawing under it. So a drawing belongs to the nearest label above
its middle. Measured on the 13 combined pages of `AI_Set_2.pdf`: every page's two drawings get one
role each. This reads the sheet's own labelling; it is not a rule that the top drawing is the
architect's, and a sheet laid out the other way round is read the other way round.

**A proposal, never a decision.** What this returns is recorded as a suggestion. Only a person's
confirmation sets a drawing's role (`workflow/view_roles.py`). A drawing with no label above it, or
two different labels equally near, gets no suggestion and says why.

Source: issue #710. Verification: tests/extraction/test_panels.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from evidence.coordinates import StoredPoint
from extraction.annotations import MarkupNote, VendorStamp

__all__ = ["PANEL_HEADINGS", "PanelRoleProposal", "printed_headings", "propose_panel_roles"]

#: The labels a combined sheet prints above each drawing, and the role each one names. Matched on
#: the whole label, ignoring case and spacing: `ID SET ELEVATION NOTES` is not this label.
PANEL_HEADINGS: Final = {
    "ID SET ELEVATION": "arch",
    "VENDOR'S SHOP DRAWING ELEVATION": "shop",
}


@dataclass(frozen=True, slots=True)
class PanelRoleProposal:
    """What the sheet's labels suggest one drawing is, or why they suggest nothing."""

    annotation_index: int
    role: str | None
    """`arch`, `shop`, or `None` when no label decides it."""
    heading: str | None
    """The label as the sheet prints it, when one was used."""
    reason: str


def _label(text: str) -> str:
    return " ".join(text.replace("’", "'").upper().split())


def printed_headings(notes: Sequence[MarkupNote]) -> tuple[MarkupNote, ...]:
    """The notes on a page that are one of `PANEL_HEADINGS`, exactly: none on a page that prints
    no heading at all."""
    return tuple(note for note in notes if _label(note.text) in PANEL_HEADINGS)


def _vertical(points: Sequence[StoredPoint]) -> tuple[Decimal, Decimal]:
    ys = [point.y for point in points]
    return min(ys), max(ys)


def propose_panel_roles(
    stamps: Sequence[VendorStamp], notes: Sequence[MarkupNote]
) -> tuple[PanelRoleProposal, ...]:
    """One suggestion per drawing on the page, in the order the file lists them.

    Positions are the stored ones, where `y` runs down the page as a reader sees it, so "above" means
    above on the sheet whatever the page's own rotation.
    """
    headings = [
        (note, PANEL_HEADINGS[_label(note.text)])
        for note in notes
        if _label(note.text) in PANEL_HEADINGS
    ]
    proposals: list[PanelRoleProposal] = []
    for stamp in stamps:
        top, bottom = _vertical(stamp.extent.points)
        middle = (top + bottom) / 2
        above = [
            (_vertical(note.extent.points)[1], note, role)
            for note, role in headings
            if _vertical(note.extent.points)[1] <= middle
        ]
        if not above:
            proposals.append(
                PanelRoleProposal(
                    stamp.annotation_index,
                    None,
                    None,
                    "no ID SET or VENDOR'S SHOP DRAWING label is printed above this drawing",
                )
            )
            continue
        nearest = max(position for position, _note, _role in above)
        closest = [(note, role) for position, note, role in above if position == nearest]
        if len({role for _note, role in closest}) > 1:
            proposals.append(
                PanelRoleProposal(
                    stamp.annotation_index,
                    None,
                    None,
                    "two different labels are equally close above this drawing",
                )
            )
            continue
        note, role = closest[0]
        proposals.append(
            PanelRoleProposal(
                stamp.annotation_index,
                role,
                note.text.strip(),
                f"the label {note.text.strip()!r} is the nearest one above this drawing",
            )
        )
    return tuple(proposals)
