"""Which drawing on a combined sheet is whose, read from the heading GV prints for it (#710, #1193).

**A package always has one architectural drawing and one shop drawing to compare.** They arrive in
one of two ways: two files, one of each, where the upload says which is which; or one combined
sheet carrying both. On the client's combined sheets each drawing is a stamp, and GV types a heading
for each one as a text note — `ID SET ELEVATION`, `VENDOR'S SHOP DRAWING`, `BRAND PROTOTYPE
ELEVATION`… — so the role can be read, not guessed.

**The heading is read by meaning, never by exact words (#1193).** `vocabulary/panel_headings.py`
lists what each wording means: the vendor's drawing (`shop`), the architect's (`arch`), or a
reference drawing (`reference`, a brand prototype: never read for a countertop, never compared).
No model is asked: the text is a dictionary value in the file.

**What is a heading, and what is a note.** A label is a heading when it means one of the listed
headings (whole label), or is shaped like one: capitals, no digits, a few words, printed at heading
size (`looks_like_heading`). A label the file prints smaller than heading size is a note whatever it
says (GV's headings are 24 pt, its notes 12 pt or less). Where it sits does not decide it: GV often
pastes a heading inside the margin of its drawing's stamp. The second kind is an *unknown* heading:
it still heads its drawing, so that drawing gets no role and says why, instead of borrowing a
heading from another drawing.

**A drawing's own heading is the one of its panel.** Headings on one level of the page (their boxes
overlap vertically) form a row, and rows cut the page into panels. GV lays a page out one of two
ways, and the page says which: headings *above* their drawings (AI Set 2: `ID SET ELEVATION` at the
top, the vendor's heading halfway down as a divider) or captions *below* them (several AI Set 3
pages, often side by side). In the first, a drawing's own heading is the nearest row above its
middle; in the second, the nearest row below it. The way that gives every known heading at least
one drawing is the page's way; when both or neither do, the layout is not clear and no drawing gets
a role. In a row of several headings side by side, a drawing takes the one over (or under) it; when
none or several are, it gets none. So a drawing never takes a heading across another heading — an
unknown heading in between stops it — and a drawing with no heading of its own gets no role.

Measured on the 13 combined pages of `AI_Set_2.pdf`: every drawing gets the same role as before
#1193. On `AI_Set_3.pdf` (60 pages) the three new wordings, the brand-prototype pages and the
captions-below pages are read; see the PR for the per-page proof.

**A proposal, never a decision.** What this returns is recorded as a suggestion. Only a person's
confirmation (or code's, when the heading and the drawing's own content agree, #1052) sets a
drawing's role (`workflow/view_roles.py`).

Source: issues #710, #1193. Verification: tests/extraction/test_panels.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from evidence.coordinates import StoredPoint
from extraction.annotations import MarkupNote, VendorStamp
from vocabulary.panel_headings import (
    HEADING_MIN_FONT_PT,
    HeadingMeaning,
    heading_meaning,
    looks_like_heading,
)

__all__ = [
    "PanelLayout",
    "PanelRoleProposal",
    "printed_headings",
    "propose_panel_roles",
    "read_panels",
]

Side = Literal["above", "below"]

#: What each meaning says about the drawing, in a reviewer's words.
_NAMES: Final = {
    HeadingMeaning.ARCH: "the architect's drawing",
    HeadingMeaning.SHOP: "the vendor's shop drawing",
    HeadingMeaning.REFERENCE: "a reference drawing (brand prototype), which is never checked",
}

#: A label quoted in a reason is cut to this many characters, so a reason always fits its column.
_QUOTED: Final = 60


@dataclass(frozen=True, slots=True)
class PanelRoleProposal:
    """What the sheet's headings suggest one drawing is, or why they suggest nothing."""

    annotation_index: int
    role: str | None
    """`arch`, `shop`, `reference`, or `None` when no heading of its own decides it."""
    heading: str | None
    """This drawing's own heading as the sheet prints it — known or not — when it has one."""
    reason: str
    known: bool = False
    """Whether `heading` is one `vocabulary/panel_headings.py` knows."""
    side: Side | None = None
    """Where its own heading sits: `above` it, or `below` it as a caption."""
    place: str = "the drawing"
    """Which drawing on the page this is, in words: "the lower drawing", "the top-left drawing"."""
    nested: bool = False
    """Whether it lies wholly inside another drawing (a mark pasted on a drawing, not a drawing)."""


@dataclass(frozen=True, slots=True)
class PanelLayout:
    """Every drawing's suggestion on one page, and the headings the page prints."""

    proposals: tuple[PanelRoleProposal, ...]
    headings: tuple[MarkupNote, ...]

    @property
    def labelled(self) -> bool:
        """Whether the page prints any heading at all, known or not. On such a page a drawing is
        read as the vendor's only when its own heading says so (`workflow/stages.py`)."""
        return bool(self.headings)


@dataclass(frozen=True, slots=True)
class _Box:
    left: Decimal
    top: Decimal
    right: Decimal
    bottom: Decimal

    @property
    def middle(self) -> Decimal:
        return (self.top + self.bottom) / 2

    @property
    def centre(self) -> Decimal:
        return (self.left + self.right) / 2

    def holds(self, other: _Box) -> bool:
        """Whether `other` lies wholly inside this box, edges included."""
        return (
            self.left <= other.left
            and other.right <= self.right
            and self.top <= other.top
            and other.bottom <= self.bottom
        )

    def overlaps_across(self, other: _Box) -> bool:
        """Whether the two boxes share some width (touching edges do not)."""
        return min(self.right, other.right) > max(self.left, other.left)


@dataclass(frozen=True, slots=True)
class _Row:
    top: Decimal
    bottom: Decimal
    labels: tuple[tuple[MarkupNote, _Box], ...]

    @property
    def known(self) -> bool:
        return any(heading_meaning(note.text) is not None for note, _box in self.labels)


def _box(points: Sequence[StoredPoint]) -> _Box:
    xs = [point.x for point in points]
    ys = [point.y for point in points]
    return _Box(min(xs), min(ys), max(xs), max(ys))


def _is_heading(note: MarkupNote) -> bool:
    """A known wording is a heading unless the file prints it smaller than heading size; an unknown
    one must be shaped like a heading (`looks_like_heading`), size included.

    **Not where it sits.** A heading is often pasted inside the margin of the drawing's own stamp
    (AI Set 3), so lying inside a drawing's box says nothing; GV's notes on a drawing are told apart
    by their size (12 pt or less, against 24 pt headings) and their words."""
    if heading_meaning(note.text) is not None:
        return note.font_size is None or note.font_size >= HEADING_MIN_FONT_PT
    return looks_like_heading(note.text, note.font_size)


def _rows(headings: Sequence[tuple[MarkupNote, _Box]]) -> tuple[_Row, ...]:
    """Headings on one level of the page (vertical extents overlapping) are one row, top first."""
    rows: list[_Row] = []
    for note, box in sorted(headings, key=lambda item: (item[1].top, item[1].left)):
        if rows and box.top < rows[-1].bottom:
            last = rows[-1]
            rows[-1] = _Row(
                last.top,
                max(last.bottom, box.bottom),
                tuple(sorted((*last.labels, (note, box)), key=lambda item: item[1].left)),
            )
        else:
            rows.append(_Row(box.top, box.bottom, ((note, box),)))
    return tuple(rows)


def _row_for(box: _Box, rows: Sequence[_Row], side: Side) -> int | None:
    """The row that heads a drawing: the nearest whose bottom is above its middle (`above`), or the
    nearest whose top is below its middle (`below`)."""
    if side == "above":
        above = [(row.bottom, index) for index, row in enumerate(rows) if row.bottom <= box.middle]
        return max(above)[1] if above else None
    below = [(row.top, index) for index, row in enumerate(rows) if row.top >= box.middle]
    return min(below)[1] if below else None


def _places(boxes: Sequence[_Box]) -> list[str]:
    """Each drawing's place in words, among the page's drawings (nested ones left out)."""
    if len(boxes) == 1:
        return ["the drawing"]
    if len(boxes) == 2:
        first, second = boxes
        shared = min(first.bottom, second.bottom) - max(first.top, second.top)
        smaller = min(first.bottom - first.top, second.bottom - second.top)
        if shared <= smaller / 2:
            upper = first.middle <= second.middle
            return (
                ["the upper drawing", "the lower drawing"]
                if upper
                else ["the lower drawing", "the upper drawing"]
            )
        left = first.centre <= second.centre
        return (
            ["the left drawing", "the right drawing"]
            if left
            else ["the right drawing", "the left drawing"]
        )
    words = []
    for box in boxes:
        vertical = (
            "top"
            if box.middle < Decimal(1) / 3
            else "bottom" if box.middle > Decimal(2) / 3 else ""
        )
        across = (
            "left"
            if box.centre < Decimal(1) / 3
            else "right" if box.centre > Decimal(2) / 3 else ""
        )
        where = "-".join(part for part in (vertical, across) if part) or "middle"
        words.append(f"the {where} drawing")
    return words


def _quoted(text: str) -> str:
    label = " ".join(text.split())
    return repr(label if len(label) <= _QUOTED else label[: _QUOTED - 1] + "…")


def read_panels(stamps: Sequence[VendorStamp], notes: Sequence[MarkupNote]) -> PanelLayout:
    """Every drawing's suggestion, in the order the file lists them, and the page's headings.

    Positions are the stored ones, where `y` runs down the page as a reader sees it, so "above"
    means above on the sheet whatever the page's own rotation.
    """
    boxes = {stamp.annotation_index: _box(stamp.extent.points) for stamp in stamps}
    nested = {
        stamp.annotation_index
        for stamp in stamps
        if any(
            other.annotation_index != stamp.annotation_index
            and boxes[other.annotation_index].holds(boxes[stamp.annotation_index])
            and boxes[other.annotation_index] != boxes[stamp.annotation_index]
            for other in stamps
        )
    }
    drawings = [
        boxes[stamp.annotation_index] for stamp in stamps if stamp.annotation_index not in nested
    ]
    headings = [(note, _box(note.extent.points)) for note in notes if _is_heading(note)]
    printed = tuple(note for note, _box_ in headings)
    places = dict(
        zip(
            (stamp.annotation_index for stamp in stamps if stamp.annotation_index not in nested),
            _places(drawings),
            strict=True,
        )
    )

    def proposal(
        stamp: VendorStamp,
        role: str | None,
        reason: str,
        heading: MarkupNote | None = None,
        side: Side | None = None,
    ) -> PanelRoleProposal:
        index = stamp.annotation_index
        return PanelRoleProposal(
            index,
            role,
            None if heading is None else " ".join(heading.text.split()),
            reason,
            known=heading is not None and heading_meaning(heading.text) is not None,
            side=side,
            place=places.get(index, "a small drawing inside another"),
            nested=index in nested,
        )

    if not headings:
        return PanelLayout(
            tuple(
                proposal(stamp, None, "no label naming the drawings is printed on this page")
                for stamp in stamps
            ),
            printed,
        )

    rows = _rows(headings)
    top_level = [stamp for stamp in stamps if stamp.annotation_index not in nested]
    used: dict[Side, set[int]] = {
        side: {
            row
            for stamp in top_level
            if (row := _row_for(boxes[stamp.annotation_index], rows, side)) is not None
        }
        for side in ("above", "below")
    }
    # A row of known headings must head a drawing; rows of unknown labels alone are weighed only on
    # a page that prints no known heading (an unknown label heading nothing is a title, not a
    # heading).
    weighed = {index for index, row in enumerate(rows) if row.known} or set(range(len(rows)))
    fits = [side for side in ("above", "below") if weighed <= used[side]]
    if len(fits) == 2 and any(
        _row_for(boxes[stamp.annotation_index], rows, "above")
        != _row_for(boxes[stamp.annotation_index], rows, "below")
        for stamp in top_level
    ):
        why = (
            "the labels on this page could head the drawings under them or name the drawings over "
            "them, so it is not clear which label is this drawing's own"
        )
        return PanelLayout(tuple(proposal(stamp, None, why) for stamp in stamps), printed)
    if not fits:
        why = (
            "the labels on this page are neither all above nor all below the drawings they name, "
            "so it is not clear which label is this drawing's own"
        )
        return PanelLayout(tuple(proposal(stamp, None, why) for stamp in stamps), printed)
    side: Side = "above" if fits[0] == "above" else "below"

    proposals: list[PanelRoleProposal] = []
    for stamp in stamps:
        box = boxes[stamp.annotation_index]
        found = _row_for(box, rows, side)
        if found is None:
            proposals.append(
                proposal(stamp, None, f"no label of its own is printed {side} this drawing")
            )
            continue
        labels = rows[found].labels
        if len(labels) > 1:
            over = [(note, label) for note, label in labels if label.overlaps_across(box)]
            meanings = {heading_meaning(note.text) for note, _label in over}
            if not over or len(meanings) != 1 or (None in meanings and len(over) > 1):
                proposals.append(
                    proposal(
                        stamp,
                        None,
                        f"two different labels are equally close {side} this drawing, side by "
                        "side, and no single one lines up with it",
                    )
                )
                continue
            labels = (over[0],)
        note = labels[0][0]
        meaning = heading_meaning(note.text)
        if meaning is None:
            proposals.append(
                proposal(
                    stamp,
                    None,
                    f"the label {side} this drawing ({_quoted(note.text)}) is not one we know",
                    note,
                    side,
                )
            )
            continue
        proposals.append(
            proposal(
                stamp,
                meaning.value,
                f"its own label {_quoted(note.text)}, {side} it, names {_NAMES[meaning]}",
                note,
                side,
            )
        )
    return PanelLayout(tuple(proposals), printed)


def propose_panel_roles(
    stamps: Sequence[VendorStamp], notes: Sequence[MarkupNote]
) -> tuple[PanelRoleProposal, ...]:
    """One suggestion per drawing on the page, in the order the file lists them (`read_panels`)."""
    return read_panels(stamps, notes).proposals


def printed_headings(
    notes: Sequence[MarkupNote], stamps: Sequence[VendorStamp] = ()
) -> tuple[MarkupNote, ...]:
    """The notes on a page that are headings, known or not (`read_panels`): none on a page that
    prints no heading at all."""
    return read_panels(stamps, notes).headings
