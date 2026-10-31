"""One page split into its views, one way for every check that asks "which drawing is this in?"
(2026-11-01): code, $0, no AI.

`view_questions` (code's questions comparing a page's views) and `dim_parts` (which part each size
measures) each split a page into views their own way, so the same page could have five views for
one and twenty-five drawings for the other, and a sink drawn inside a plan was one view for the
questions and a separate drawing for the size links. Both now use this module.

**1. The drawings** (`drawings`). The page's line work joins into connected drawings: pieces that
meet or pass within `JOIN_PT`. A drawing of fewer than `MIN_PIECES` pieces (a bubble, a picture
frame, a logo letter) or covering more than `PAGE_SHARE` of the page (the border and title block)
is no view of its own. A drawing lying inside another's box (a sink in a plan, the bubbles of an
elevation marker, a dimension chain inside the view) is part of that view; so is a thin one
(thinner than `THIN_PT`) within `THIN_GAP_PT` of a thicker one (a dimension chain, whose extension
lines stop short of the drawing by design, or a top drawn apart from its cabinets); a scrap thinner
than `MIN_SIDE_PT` joins the view without growing its box (a view's box places titles and text). A
drawing that is none of these is a view on its own when it is at least `MIN_SIDE_PT` both ways. Each piece of
line knows its view (`Drawings.view_of`); a piece in no view keeps its own drawing (`group_of`),
so two pieces of one small drawing are still in one drawing.

**2. The titles** (`titles`). A phrase naming a view (VIEW, ELEVATION, SECTION, PLAN, DETAIL), at
most five words, not a note (`/`, SEE, SCALE, TYP), not title-block words, not a part callout or a
finish tag (the caller's `names_part`), with the bubble printed beside it ("S1", "A") or inside
it ("SECTION VIEW S1"); titles on one line next to each other are one title, named by the one
with a bubble.

**3. Which view a title names** (`owners`). Each view's box is grown by its own text (the sizes,
tags and part words nearest it, `texts`, within `ASSIGN_PT`), so a title under that text is still
under the view. A title inside a grown box names the smallest such view; else the view it sits
under, at most `TITLE_BELOW_PT` below and over its width (20 points either side).

Which of the two former splitters was kept: `view_questions`' (the drawings, the titles and the
owners above). It joined drawings inside a view's box to that view and grew a view by its text
before looking for its title, and read a bubble on either side of a title or inside it; the size
links' own splitter did neither (a sink in a plan and the plan were two drawings, so a size from the
counter edge to the sink was refused as "edges in different drawings"). From the size links' side it
keeps: a piece in no view still belongs to its own drawing, a thin drawing beside a view joins it
whatever its size, and title-block words never name a view.

Both callers go through `view_questions.page_split`, which reads the titles and the views' text
from the page's phrases the same way for both. Pure: line work as `(point, point)` pieces in page
points, phrases as `{text, box}`. Verification:
`tests/review/assistant/test_page_views.py`, and both callers' tests (made-up drawings only).
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "ASSIGN_PT",
    "JOIN_PT",
    "MIN_PIECES",
    "MIN_SIDE_PT",
    "PAGE_SHARE",
    "THIN_GAP_PT",
    "THIN_PT",
    "TITLE_BELOW_PT",
    "Drawings",
    "drawings",
    "is_title_block",
    "owners",
    "titles",
]

type Box = tuple[float, float, float, float]

#: The line work's pieces join into one drawing when they pass within about this (points).
JOIN_PT: Final = 3.0
#: A drawing of fewer pieces than this is a picture frame or a bubble, not a view; one covering
#: more than `PAGE_SHARE` of the page is the border and title block.
MIN_PIECES: Final = 12
PAGE_SHARE: Final = 0.5
#: A drawing that is a view on its own is at least this big both ways (points).
MIN_SIDE_PT: Final = 20.0
#: A drawing thinner than this (points) within `THIN_GAP_PT` of a thicker one is part of it.
THIN_PT: Final = 40.0
THIN_GAP_PT: Final = 20.0
#: A drawing inside another's box, to within this (points), is part of it.
INSIDE_PT: Final = 2.0
#: A view's text lies in it or within this of it (points); it grows the view's box.
ASSIGN_PT: Final = 40.0
#: A view title sits under its drawing (or in it), at most this far below (points).
TITLE_BELOW_PT: Final = 60.0
#: A title's bubble ("A", "S1", "01") sits beside it, within this (points).
MARKER_PT: Final = 30.0
#: Two titles on one line closer than this are one title ("TOP PLAN (B) FRONT VIEW").
TITLE_JOIN_PT: Final = 60.0

_TITLE: Final = re.compile(r"\b(VIEW|ELEVATION|SECTION|PLAN|DETAIL)\b", re.IGNORECASE)
_MARKER: Final = re.compile(r"^(?:[A-Z]|[A-Z]?\d{1,2})$")
_MARKER_IN: Final = re.compile(r"(?:^|\s)([A-Z]|[A-Z]?\d{1,2})$|^([A-Z]|[A-Z]?\d{1,2})(?:\s)")
_NOT_TITLE: Final = re.compile(r"/|\bSEE\b|\bSCALE\b|\bTYP", re.IGNORECASE)
_TITLE_BLOCK: Final = re.compile(
    r"\b(?:SCALE|SHEET|DATE|DRAWN|CHECKED|APPROVED|PROJECT|PHONE|FAX|PAGE|DWG|REVISIONS?|REV|"
    r"COPYRIGHT|DESCRIPTION|ITEM\s*NO|OVERALL\s*SIZE|MATERIAL\s*SPEC|QTY)\b",
    re.IGNORECASE,
)


def is_title_block(text: str) -> bool:
    """Title-block words (SHEET, DATE, DRAWN, PROJECT, QTY...): never a view's title or text."""
    return _TITLE_BLOCK.search(text) is not None


def _centre(b: Sequence[float]) -> tuple[float, float]:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def _gap(a: Sequence[float], b: Sequence[float]) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _inside(point: tuple[float, float], b: Sequence[float]) -> bool:
    return b[0] <= point[0] <= b[2] and b[1] <= point[1] <= b[3]


def _area(b: Sequence[float]) -> float:
    return (b[2] - b[0]) * (b[3] - b[1])


def _union(a: Sequence[float], b: Sequence[float]) -> Box:
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


# ---- 1. the drawings ------------------------------------------------------------------------------


@dataclass(slots=True)
class Drawings:
    """The page's views (`boxes`, each with its small and thin drawings) and each piece of line's
    view (`views`, `None` when its drawing is no view) and own connected drawing (`groups`)."""

    boxes: list[Box] = field(default_factory=list)
    views: list[int | None] = field(default_factory=list)
    groups: list[int] = field(default_factory=list)

    def view_of(self, index: int) -> int | None:
        """The view of piece `index` (`None`: in no view, or no such piece)."""
        if not 0 <= index < len(self.views):
            return None
        return self.views[index]

    def group_of(self, index: int) -> tuple[str, int] | None:
        """Piece `index`'s drawing: its view (`("view", n)`), else its own connected drawing
        (`("group", root)`), so two pieces of one small drawing are in one drawing."""
        if not 0 <= index < len(self.views):
            return None
        view = self.views[index]
        return ("view", view) if view is not None else ("group", self.groups[index])


def _pieces(segments: Sequence[Any]) -> list[tuple[float, float, float, float] | None]:
    out: list[tuple[float, float, float, float] | None] = []
    for seg in segments:
        try:
            (ax, ay), (bx, by) = seg
            out.append((float(ax), float(ay), float(bx), float(by)))
        except (TypeError, ValueError):
            out.append(None)
    return out


def drawings(segments: Sequence[Any], size: tuple[float, float] | None = None) -> Drawings:
    """The page's views from its line work (see the module, step 1). `size`: the page (points),
    for the border; without it, the line work's extent."""
    pieces = _pieces(segments)
    parent = list(range(len(pieces)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cells: dict[tuple[int, int], int] = {}
    for i, piece in enumerate(pieces):
        if piece is None:
            continue
        ax, ay, bx, by = piece
        steps = max(1, int(math.hypot(bx - ax, by - ay) / JOIN_PT) + 1)
        seen: set[tuple[int, int]] = set()
        for k in range(steps + 1):
            key = (
                int((ax + (bx - ax) * k / steps) // JOIN_PT),
                int((ay + (by - ay) * k / steps) // JOIN_PT),
            )
            if key in seen:
                continue
            seen.add(key)
            other = cells.setdefault(key, i)
            a, b = root(i), root(other)
            if a != b:
                parent[a] = b
    roots = [root(i) for i in range(len(pieces))]
    boxes: dict[int, list[float]] = {}
    counts: dict[int, int] = defaultdict(int)
    for i, piece in enumerate(pieces):
        if piece is None:
            continue
        ax, ay, bx, by = piece
        r = roots[i]
        counts[r] += 1
        extent = boxes.setdefault(r, [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)])
        extent[0], extent[1] = min(extent[0], ax, bx), min(extent[1], ay, by)
        extent[2], extent[3] = max(extent[2], ax, bx), max(extent[3], ay, by)
    out = Drawings(groups=roots, views=[None] * len(pieces))
    if not boxes:
        return out
    if size is None or size[0] <= 0 or size[1] <= 0:
        every = list(boxes.values())
        size = (
            max(b[2] for b in every) - min(b[0] for b in every),
            max(b[3] for b in every) - min(b[1] for b in every),
        )
    page_area = max(1.0, size[0] * size[1])
    kept: dict[int, Box] = {
        r: (b[0], b[1], b[2], b[3])
        for r, b in boxes.items()
        if counts[r] >= MIN_PIECES and _area(b) <= PAGE_SHARE * page_area
    }
    # A drawing inside another's box is part of the smallest that holds it.
    owner: dict[int, int] = {}
    for r, box in kept.items():
        holding = [
            other
            for other, ob in kept.items()
            if other != r
            and ob != box
            and (_area(ob), -other) > (_area(box), -r)
            and ob[0] - INSIDE_PT <= box[0]
            and ob[1] - INSIDE_PT <= box[1]
            and box[2] <= ob[2] + INSIDE_PT
            and box[3] <= ob[3] + INSIDE_PT
        ]
        if holding:
            owner[r] = min(holding, key=lambda o: _area(kept[o]))
    top = {r: b for r, b in kept.items() if r not in owner}
    thick = {r: b for r, b in top.items() if min(b[2] - b[0], b[3] - b[1]) >= THIN_PT}
    for r, box in top.items():
        if r in thick:
            continue
        near = [(_gap(box, b), t) for t, b in thick.items() if _gap(box, b) <= THIN_GAP_PT]
        if near:
            owner[r] = min(near)[1]
    views: dict[int, int] = {}
    grown: list[Box] = []
    for r, box in top.items():
        if r in owner:
            continue
        if r not in thick and min(box[2] - box[0], box[3] - box[1]) < MIN_SIDE_PT:
            continue  # a thin scrap on its own: no view
        views[r] = len(grown)
        grown.append(box)

    def view_root(r: int) -> int | None:
        seen: set[int] = set()
        while r in owner and r not in seen:
            seen.add(r)
            r = owner[r]
        return views.get(r)

    for r, box in kept.items():
        n = view_root(r)
        if n is None or r in views or min(box[2] - box[0], box[3] - box[1]) < MIN_SIDE_PT:
            # A scrap thinner than `MIN_SIDE_PT` (a leader, a chain's line) is part of the view
            # but does not move its edge towards the next view's title.
            continue
        grown[n] = _union(grown[n], box)
    for i, r in enumerate(roots):
        if pieces[i] is not None and r in kept:
            out.views[i] = view_root(r)
    out.boxes = grown
    return out


# ---- 2. the titles --------------------------------------------------------------------------------


def titles(
    items: Sequence[Mapping[str, Any]], names_part: Callable[[str], bool] = lambda _t: False
) -> tuple[list[tuple[str, Box]], set[int]]:
    """The page's view titles with their bubbles ("SECTION VIEW" + "S1"), titles on one line next
    to each other joined; and the phrases they are made of (`id`s). `items`: `{text, box}` with the
    box a 4-tuple; `names_part(text)`: the text is a part callout or a finish tag (with "TOP" taken
    out: "TOP PLAN" is a title), never a title."""
    found: list[tuple[str, Box]] = []
    members: set[int] = set()
    for item in items:
        text, box = str(item["text"]), item["box"]
        if (
            not _TITLE.search(text)
            or _NOT_TITLE.search(text)
            or is_title_block(text)
            or len(text.split()) > 5
            or names_part(re.sub(r"\bTOP\b", " ", text, flags=re.IGNORECASE))
        ):
            continue
        marker = None
        members.add(id(item))
        inner = _MARKER_IN.search(text)
        if inner is None:
            for other in items:
                ob = other["box"]
                if (
                    other is not item
                    and _MARKER.match(str(other["text"]))
                    and abs(_centre(ob)[1] - _centre(box)[1]) <= 8
                    and min(abs(box[0] - ob[2]), abs(ob[0] - box[2])) <= MARKER_PT
                ):
                    marker = str(other["text"])
                    members.add(id(other))
                    box = (min(box[0], ob[0]), min(box[1], ob[1]), max(box[2], ob[2]), box[3])
                    break
        found.append((f"{text} {marker}" if marker else text, box))
    found.sort(key=lambda t: (round(_centre(t[1])[1] / 8), t[1][0]))
    joined: list[tuple[str, Box]] = []
    for name, box in found:
        if joined:
            last_name, last = joined[-1]
            if (
                abs(_centre(last)[1] - _centre(box)[1]) <= 8
                and box[0] - last[2] <= TITLE_JOIN_PT
                and last[0] - box[2] <= TITLE_JOIN_PT
            ):
                # The one with a bubble names the view.
                best = name if re.search(r"\s\S{1,3}$", name) else last_name
                joined[-1] = (best, _union(last, box))
                continue
        joined.append((name, box))
    return joined, members


# ---- 3. which view a title names ------------------------------------------------------------------


def nearest(box: Sequence[float], boxes: Sequence[Box]) -> int | None:
    """The view a phrase lies in (the smallest that holds its centre), else the nearest one within
    `ASSIGN_PT`."""
    centre = _centre(box)
    holding = [i for i, b in enumerate(boxes) if _inside(centre, b)]
    if holding:
        return min(holding, key=lambda i: _area(boxes[i]))
    near = [(_gap(box, b), i) for i, b in enumerate(boxes)]
    near = [n for n in near if n[0] <= ASSIGN_PT]
    return min(near)[1] if near else None


def owners(
    boxes: Sequence[Box], found_titles: Sequence[tuple[str, Box]], texts: Sequence[Sequence[float]]
) -> dict[int, list[tuple[str, Box]]]:
    """Each view's titles (view index -> `(name, box)`s): see the module, step 3. `texts`: the
    boxes of the page's sizes, tags and part words (they grow the view nearest them)."""
    grown = [list(b) for b in boxes]
    for box in texts:
        n = nearest(box, boxes)
        if n is not None:
            grown[n] = list(_union(grown[n], box))
    taken: dict[int, list[tuple[str, Box]]] = {}
    for name, tbox in found_titles:
        centre = _centre(tbox)
        owner = [i for i, g in enumerate(grown) if _inside(centre, g)]
        if not owner:
            under = [
                (tbox[1] - g[3], i)
                for i, g in enumerate(grown)
                if -10 <= tbox[1] - g[3] <= TITLE_BELOW_PT and g[0] - 20 <= centre[0] <= g[2] + 20
            ]
            owner = [min(under)[1]] if under else []
        if owner:
            taken.setdefault(min(owner, key=lambda i: _area(boxes[i])), []).append((name, tbox))
    return taken
