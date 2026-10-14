"""What kind of drawing a vendor view is, as code sees it: one judgment, never a decision (#1256).

The depth and sink reader reads a countertop's depth on a **plan** (top view), or across the stone
on a **section or side view**, and the sink cut-out on a plan. Which kind a view is therefore
decides which way a depth runs and which spans can be a cut-out. Phase 5 makes that kind need **two
independent judgments** (design §4): two AI readers name it from a picture of the view with its
heading hidden, and code judges it from the heading words and the drawing. This module is the code
half. Its answer is **one judgment**: a caller that acted on it alone would be breaking the rule
"an automatic result needs two independent judgments", so the type says `judged_by="code"` and no
function here returns a decision.

**Why code cannot decide alone.** The vendor's heading can be wrong. On one real sheet the heading
"A PLAN VIEW" sits under the front elevation and "B FRONT VIEW" under the plan; a heading-only rule
reads the elevation as a plan. So the heading is checked against the drawing where the drawing can
say something: a plan looks down on the stone, and the wall behind it is a band (hatched or
filled) along one long edge of the drawing, with nothing of the drawing beyond it. A heading that
says plan with no such band, or says elevation with one, is a conflict, and the conflict makes the
code judgment **unknown** (`kind=None`) with the reason. Unknown is safe: the view is not read.

**What the drawing is not asked.** A section's stone slab is a thin band too, and a hatched floor
can run along an elevation's bottom edge, so the band test is used only where it separates the
two kinds that get confused: plan and elevation. A section, side, detail or other heading is taken
from its words alone and says so in its reason.

**Finding the views on a vendor panel.** Where each view prints a title over a scale note
(`A PLAN VIEW` over `1:10`), `extraction/architect/page_views.py` already finds the views and
their extents, so it is reused unchanged. Some vendors print only a caption under each drawing
(`ELEVATION`, `SECTION AT ...`, `PLAN`) with no scale note; then the captions are found by their
words and size, and each caption owns the part of the panel above it, columns split halfway
between captions on one row. A stroke that crosses from one view's part into another's marks both
views not separated, as `page_views` does. Nothing is silent: ink below every caption, a caption
with no drawing and a panel with no view each leave a note.

Pure: plain values in, plain values out, page points with `top` downward (pdfplumber's frame).

Source: issue #1256 (phase 5 design §4, §7 PR 1) · Verification:
`tests/extraction/depth/test_view_kind.py`
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal

from extraction.architect.page_views import (
    INK_OUTSIDE_VIEWS,
    TITLE_NOT_VIEW,
    PageViewSettings,
    survey_page_views,
)
from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS
from extraction.architect.text import (
    Orientation,
    TextChar,
    TextSettings,
    find_phrases,
    find_printed,
)
from extraction.depth.spans import WallBand
from extraction.geometry.dimension_lines import Axis
from extraction.geometry.rows import Box, InkLine, PageInk
from extraction.page_type import TYPE_WORDS
from vocabulary.page_types import PageType

__all__ = [
    "HEADING_WORDS",
    "MEASURED_VIEW_SETTINGS",
    "CodeViewKind",
    "HeadingKind",
    "VendorView",
    "ViewFinderSettings",
    "ViewKind",
    "ViewSurvey",
    "code_view_kind",
    "find_vendor_views",
    "kind_from_heading",
    "panel_ink",
]

_TWO: Final = Decimal(2)


class ViewKind(StrEnum):
    """The kinds the depth reader tells apart (design §4)."""

    PLAN = "plan"
    """Looking down on the stone: a depth runs top to bottom; a sink is a rectangle."""
    SECTION = "section"
    """A section or a side view: a cut through the counter, where a depth runs left to right."""
    ELEVATION = "elevation"
    """A front view: doors standing up. Widths and heights, never a depth."""
    DETAIL = "detail"
    OTHER = "other"

    @property
    def page_type(self) -> PageType | None:
        """The shared page vocabulary's word for this kind (`vocabulary/page_types.py`)."""
        return _PAGE_TYPE[self]


_PAGE_TYPE: Final[dict[ViewKind, PageType | None]] = {
    ViewKind.PLAN: PageType.PLAN,
    ViewKind.SECTION: PageType.SECTION,
    ViewKind.ELEVATION: PageType.ELEVATION,
    ViewKind.DETAIL: PageType.DETAIL,
    ViewKind.OTHER: None,
}

#: The page vocabulary's words (`extraction/page_type.TYPE_WORDS`, built on `PageType`), each as the
#: view kind it names. A schedule or a title sheet is a view of no kind the reader uses.
_FROM_PAGE_TYPE: Final[dict[PageType, ViewKind]] = {
    PageType.PLAN: ViewKind.PLAN,
    PageType.SECTION: ViewKind.SECTION,
    PageType.ELEVATION: ViewKind.ELEVATION,
    PageType.DETAIL: ViewKind.DETAIL,
    PageType.SCHEDULE: ViewKind.OTHER,
    PageType.TITLE: ViewKind.OTHER,
}

#: Every heading word and the kind it names: the page vocabulary's words, plus the vendors' own
#: phrases for a view (design §4). Matched longest first on word boundaries, so `SIDE ELEVATION`
#: is a side view and not an elevation, and `PLANNING` is not `PLAN`.
HEADING_WORDS: Final[dict[str, ViewKind]] = {
    **{word: _FROM_PAGE_TYPE[page_type] for word, page_type in TYPE_WORDS.items()},
    "TOP PLAN": ViewKind.PLAN,
    "FLOOR PLAN": ViewKind.PLAN,
    "TOP VIEW": ViewKind.PLAN,
    "SECTION VIEW": ViewKind.SECTION,
    "SECTION AT": ViewKind.SECTION,
    "SIDE VIEW": ViewKind.SECTION,
    "SIDE ELEVATION": ViewKind.SECTION,
    "FRONT VIEW": ViewKind.ELEVATION,
    "FRONT ELEVATION": ViewKind.ELEVATION,
    "DETAIL VIEW": ViewKind.DETAIL,
}

_WORD_PATTERN: Final = re.compile(
    r"\b("
    + "|".join(
        re.escape(word).replace(r"\ ", r"\s+")
        for word in sorted(HEADING_WORDS, key=len, reverse=True)
    )
    + r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class HeadingKind:
    """What a view's printed heading says its kind is."""

    heading: str | None
    kind: ViewKind | None
    """`None` when the heading names no kind, names two, or there is no heading."""
    words: tuple[str, ...]
    """The heading words found, as printed (upper-cased, spaces folded)."""
    reason: str


def kind_from_heading(heading: str | None) -> HeadingKind:
    """The kind a heading's words name, or `None` with the reason when they name none or two.

    Two kinds in one heading (`PLAN AND ELEVATION`) is unknown, never a choice between them, as
    `extraction/page_type.classify` rules for a title block.
    """
    if heading is None or not heading.strip():
        return HeadingKind(None, None, (), "the view has no printed heading")
    words = tuple(
        " ".join(match.group(1).upper().split()) for match in _WORD_PATTERN.finditer(heading)
    )
    kinds = {HEADING_WORDS[word] for word in words}
    if not kinds:
        return HeadingKind(
            heading, None, (), f"the heading {heading!r} names no kind of view the reader knows"
        )
    if len(kinds) > 1:
        named = ", ".join(sorted(kind.value for kind in kinds))
        return HeadingKind(
            heading, None, words, f"the heading {heading!r} names more than one kind ({named})"
        )
    (kind,) = kinds
    return HeadingKind(
        heading, kind, words, f"the heading {heading!r} says {kind.value} ({', '.join(words)})"
    )


@dataclass(frozen=True, slots=True)
class CodeViewKind:
    """Code's judgment of one view's kind. **One judgment, never a decision**: the reader acts on a
    kind only when the two AI readers, shown the view with its heading hidden, name the same one."""

    kind: ViewKind | None
    """`None` is "code cannot say": no heading kind, or the heading and the drawing disagree."""
    heading: HeadingKind
    plan_band: bool
    """Whether a wall band runs along a long edge of the drawing, with nothing beyond it."""
    reason: str
    judged_by: Literal["code"] = "code"


def code_view_kind(heading: HeadingKind, bands: Sequence[WallBand]) -> CodeViewKind:
    """Code's judgment of a view's kind from its heading words and its wall bands.

    `bands` are the view's wall bands as `spans.wall_bands` finds them. A **plan band** is one that
    runs along the drawing's width (a horizontal band) at the drawing's edge: the wall behind a
    stone seen from above. Only plan and elevation headings are checked against it; see the module
    docstring for why.
    """
    plan_band = any(band.axis is Axis.HORIZONTAL and band.at_edge for band in bands)
    kind = heading.kind
    if kind is None:
        return CodeViewKind(None, heading, plan_band, heading.reason)
    if kind is ViewKind.PLAN:
        if plan_band:
            return CodeViewKind(
                ViewKind.PLAN,
                heading,
                plan_band,
                f"{heading.reason}, and a wall band runs along a long edge of the drawing",
            )
        return CodeViewKind(
            None,
            heading,
            plan_band,
            f"{heading.reason}, but no wall band runs along a long edge of the drawing, as it "
            "does on a plan: the heading may be wrong (a front view headed plan), so code cannot "
            "say",
        )
    if kind is ViewKind.ELEVATION:
        if plan_band:
            return CodeViewKind(
                None,
                heading,
                plan_band,
                f"{heading.reason}, but a wall band runs along a long edge of the drawing, as it "
                "does on a plan: the heading may be wrong (a plan headed front view), so code "
                "cannot say",
            )
        return CodeViewKind(
            ViewKind.ELEVATION,
            heading,
            plan_band,
            f"{heading.reason}, and no wall band runs along a long edge of the drawing",
        )
    return CodeViewKind(
        kind,
        heading,
        plan_band,
        f"{heading.reason} (taken from the words alone: the wall-band test separates only plan "
        "from elevation)",
    )


# ---------------------------------------------------------------------------
# Finding the views on a vendor panel
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ViewFinderSettings:
    """Every length the view finder uses. No defaults; `MEASURED_VIEW_SETTINGS` is the measured
    set."""

    text: TextSettings
    views: PageViewSettings
    """`page_views`' own settings, for panels whose views print a title over a scale note."""
    heading_height_ratio: Decimal
    """A caption is printed at least this many times as tall as the panel's median phrase."""
    heading_line_em: Decimal
    """A second caption line starts within this many caption heights below the first."""


@dataclass(frozen=True, slots=True)
class VendorView:
    """One drawing view on a vendor panel."""

    number: int
    """Panel-local: top to bottom, then left to right, from 1."""
    heading: str | None
    heading_box: Box | None
    """Everything printed as the view's label (bubble, title, scale): what its picture hides."""
    extent: Box
    separated: bool
    reason: str
    found_by: Literal["title over scale", "heading words"]


@dataclass(frozen=True, slots=True)
class ViewSurvey:
    """The views on one panel, and a note for everything left out."""

    views: tuple[VendorView, ...]
    notes: tuple[str, ...]


def _inside(x: Decimal, y: Decimal, box: Box) -> bool:
    return box.x0 <= x <= box.x1 and box.top <= y <= box.bottom


def panel_ink(ink: PageInk, panel: Box) -> PageInk:
    """The part of a page's ink that lies in `panel`: lines with both ends in it, curves and
    characters with their middle in it."""
    return PageInk(
        width=ink.width,
        height=ink.height,
        lines=tuple(
            line
            for line in ink.lines
            if _inside(line.x0, line.y0, panel) and _inside(line.x1, line.y1, panel)
        ),
        curves=tuple(
            curve for curve in ink.curves if _inside(curve.box.centre_x, curve.box.centre_y, panel)
        ),
        characters=tuple(
            char for char in ink.characters if _inside(char.box.centre_x, char.box.centre_y, panel)
        ),
        drawing_boxes=ink.drawing_boxes,
    )


def find_vendor_views(
    chars: Sequence[TextChar], ink: PageInk, panel: Box, settings: ViewFinderSettings
) -> ViewSurvey:
    """The drawing views on one vendor panel, each with its heading and extent.

    `chars` and `ink` are the page's black and grey text and ink (pdfplumber's frame); only what lies
    in `panel` (the vendor's pasted drawing) is looked at.
    """
    inside_chars = [char for char in chars if _inside(char.box.centre_x, char.box.centre_y, panel)]
    inside_ink = panel_ink(ink, panel)
    survey = survey_page_views(
        inside_chars, inside_ink, text=settings.text, settings=settings.views
    )
    trouble = [
        note
        for note in survey.notes
        if note.kind == TITLE_NOT_VIEW
        or (note.kind == INK_OUTSIDE_VIEWS and note.startswith("left out as the sheet's border"))
    ]
    if survey.views and not trouble:
        views = tuple(
            VendorView(
                number=view.number,
                heading=" ".join(part for part in (view.bubble, view.title) if part),
                heading_box=view.label_box,
                extent=view.extent,
                separated=view.separated,
                reason=view.reason,
                found_by="title over scale",
            )
            for view in survey.views
        )
        return ViewSurvey(views, tuple(str(note) for note in survey.notes))
    fallback = _views_by_heading_words(inside_chars, inside_ink, panel, settings)
    if not trouble:
        return fallback
    return ViewSurvey(
        fallback.views,
        (
            "the title-over-scale finder left drawing out ("
            + "; ".join(str(note) for note in trouble)
            + "), so the panel was cut by its captions instead",
            *fallback.notes,
        ),
    )


@dataclass(frozen=True, slots=True)
class _Caption:
    text: str
    box: Box


def _captions(chars: Sequence[TextChar], settings: ViewFinderSettings) -> list[_Caption]:
    """Each view's caption: a phrase naming a kind of view that is printed tall (at least
    `heading_height_ratio` times the panel's median phrase), with any second line right under it;
    or one printed right over a scale note, as `page_views` reads a title, with the note."""
    phrases = find_phrases(chars, settings.text)
    if not phrases:
        return []
    median = Decimal(statistics.median(phrase.height for phrase in phrases))
    tall = [
        phrase
        for phrase in phrases
        if phrase.orientation is Orientation.UPRIGHT
        and phrase.height >= settings.heading_height_ratio * median
    ]
    used: set[int] = set()
    captions: list[_Caption] = []
    for index, phrase in sorted(enumerate(tall), key=lambda pair: pair[1].box.top):
        if index in used or not kind_from_heading(phrase.text).words:
            continue
        used.add(index)
        text, box = phrase.text, phrase.box
        grew = True
        while grew:
            grew = False
            for other_index, other in enumerate(tall):
                if other_index in used:
                    continue
                gap = other.box.top - box.bottom
                if Decimal(
                    0
                ) <= gap <= settings.heading_line_em * phrase.height and other.box.overlaps_x(box):
                    used.add(other_index)
                    text, box = f"{text} {other.text}", box.union(other.box)
                    grew = True
        captions.append(_Caption(text, box))
    views = settings.views
    for scale in find_printed(chars, settings.text).scales:
        em = scale.box.height
        reach = Box(
            scale.box.x0 - views.title_reach_em * em,
            scale.box.top,
            scale.box.x1 + views.title_reach_em * em,
            scale.box.bottom,
        )
        above = [
            phrase
            for phrase in phrases
            if phrase.orientation is Orientation.UPRIGHT
            and kind_from_heading(phrase.text).words
            and Decimal(0) <= scale.box.top - phrase.box.bottom <= views.title_gap_em * em
            and phrase.box.overlaps_x(reach)
        ]
        if not above:
            continue
        title = max(above, key=lambda phrase: phrase.box.bottom)
        if any(caption.box.meets(title.box) for caption in captions):
            continue
        captions.append(_Caption(title.text, title.box.union(scale.box)))
    return captions


def _rows(captions: Sequence[_Caption]) -> list[list[_Caption]]:
    rows: list[list[_Caption]] = []
    for caption in sorted(captions, key=lambda item: item.box.top):
        for row in rows:
            if any(
                caption.box.top <= other.box.bottom and caption.box.bottom >= other.box.top
                for other in row
            ):
                row.append(caption)
                break
        else:
            rows.append([caption])
    return [sorted(row, key=lambda item: item.box.centre_x) for row in rows]


def _views_by_heading_words(
    chars: Sequence[TextChar], ink: PageInk, panel: Box, settings: ViewFinderSettings
) -> ViewSurvey:
    captions = _captions(chars, settings)
    if not captions:
        return ViewSurvey(
            (),
            (
                (
                    "no view found: no title over a scale note, and no caption naming a kind of "
                    "view is printed on this panel"
                ),
            ),
        )
    rows = _rows(captions)
    regions: list[tuple[_Caption, Box]] = []
    previous_bottom = panel.top
    for row in rows:
        row_bottom = max(caption.box.bottom for caption in row)
        for position, caption in enumerate(row):
            left = (
                panel.x0
                if position == 0
                else (row[position - 1].box.centre_x + caption.box.centre_x) / _TWO
            )
            right = (
                panel.x1
                if position == len(row) - 1
                else (caption.box.centre_x + row[position + 1].box.centre_x) / _TWO
            )
            regions.append((caption, Box(left, previous_bottom, right, row_bottom)))
        previous_bottom = row_bottom

    def region_of(x: Decimal, y: Decimal) -> int | None:
        for index, (_caption, region) in enumerate(regions):
            if _inside(x, y, region):
                return index
        return None

    found: dict[int, Box] = {}
    crossing: dict[int, set[int]] = {index: set() for index in range(len(regions))}
    below = 0

    def take(index: int | None, box: Box) -> None:
        nonlocal below
        if index is None:
            below += 1
            return
        found[index] = found[index].union(box) if index in found else box

    caption_boxes = [caption.box for caption, _region in regions]

    def in_caption(x: Decimal, y: Decimal) -> bool:
        return any(_inside(x, y, box) for box in caption_boxes)

    for line in ink.lines:
        ends = (region_of(line.x0, line.y0), region_of(line.x1, line.y1))
        if ends[0] is not None and ends[1] is not None and ends[0] != ends[1]:
            crossing[ends[0]].add(ends[1])
            crossing[ends[1]].add(ends[0])
            continue
        box = _line_box(line)
        take(ends[0] if ends[0] is not None else ends[1], box)
    for curve in ink.curves:
        if in_caption(curve.box.centre_x, curve.box.centre_y):
            continue
        corners = {
            region_of(curve.box.x0, curve.box.top),
            region_of(curve.box.x1, curve.box.bottom),
        } - {None}
        if len(corners) > 1:
            first, second = sorted(index for index in corners if index is not None)[:2]
            crossing[first].add(second)
            crossing[second].add(first)
            continue
        take(region_of(curve.box.centre_x, curve.box.centre_y), curve.box)
    for char in ink.characters:
        if in_caption(char.box.centre_x, char.box.centre_y):
            continue
        take(region_of(char.box.centre_x, char.box.centre_y), char.box)

    notes: list[str] = []
    if below:
        notes.append(
            f"left out: {below} piece(s) of ink lie below every caption, so belong to no view"
        )
    ordered = sorted(
        range(len(regions)),
        key=lambda index: (regions[index][1].top, regions[index][1].x0),
    )
    views: list[VendorView] = []
    for index in ordered:
        caption, _region = regions[index]
        if index not in found:
            notes.append(f"not a view: the caption {caption.text!r} has no drawing above it")
            continue
        others = sorted(regions[other][0].text for other in crossing[index])
        separated = not others
        reason = (
            f"view {caption.text!r} found by its caption's words (no scale note is printed); "
            f"one of {len(captions)} caption(s) on the panel"
            + (
                ""
                if separated
                else "; not clearly separated: strokes cross into "
                + ", ".join(repr(text) for text in others)
            )
        )
        views.append(
            VendorView(
                number=len(views) + 1,
                heading=caption.text,
                heading_box=caption.box,
                extent=found[index].union(caption.box),
                separated=separated,
                reason=reason,
                found_by="heading words",
            )
        )
    if not views:
        notes.append("no view found: no caption on this panel has a drawing above it")
    return ViewSurvey(tuple(views), tuple(notes))


def _line_box(line: InkLine) -> Box:
    return Box(
        min(line.x0, line.x1), min(line.y0, line.y1), max(line.x0, line.x1), max(line.y0, line.y1)
    )


#: The architect reader's measured text and view settings (a vendor's view prints its title over
#: its scale note as an architect's does); captions are measured on AI Set 3's caption-only vendor
#: panels (2026-10-14): view captions are about three times the dimension text's height, and a
#: two-line caption's second line starts within a point of the first's bottom.
MEASURED_VIEW_SETTINGS: Final = ViewFinderSettings(
    text=MEASURED_ARCHITECT_SETTINGS.text,
    views=MEASURED_ARCHITECT_SETTINGS.views,
    heading_height_ratio=Decimal("1.5"),
    heading_line_em=Decimal("0.5"),
)
