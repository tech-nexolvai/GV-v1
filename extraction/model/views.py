"""What a drawing view is, and how a tag identifies it (#164, B7.1).

The real drawings carry circled view tags — `D`, `E`, `F`, `G`. Those tags are how a detail relates to the
plan it came from, and they are the backbone of item grouping.

**Identity is `(page, tag)`, never the tag alone.** `docs/DESIGN_EXTRACTION.md` §4.1 states the reason in
five words: *"tags repeat across pages; tag alone merges two views."* A vendor numbering details `D` on
every sheet is completely ordinary, and a model keyed on the letter would silently fuse a kitchen detail
with a bathroom one — after which every item in both belongs to the same view and no later guard notices.

**An untagged region is a first-class view, and it needs its own identity.** Most drawings have them, so
this cannot be the awkward case. But `(page, None)` would make every untagged region on a sheet one view,
which is the same merging failure by a different route. So an untagged view is identified by its region:
two untagged regions on one page are two views, because they are in two places.

**A tag is kept exactly as printed.** `D`, `D1`, `4/A-101` — a report cites what the drawing shows, and
normalisation is a separate value for comparison, exactly as with a revision label (#183) or a sheet
number (#162).

**Which view an observation sits in is answered geometrically, and refused when ambiguous.** A detail
region nested inside a plan region is normal, and two views both containing an observation is therefore
normal too — so `view_containing` returns no view rather than picking. Choosing the smaller one is a
plausible rule and it is not mine to invent: it needs real drawings to confirm that innermost always wins,
and guessing puts an observation in the wrong view, which §3.2 already establishes no tolerance check can
catch.

Source: backend proposal §10.1 `drawing_views` · Design: `docs/DESIGN_EXTRACTION.md` §4.1 ·
Verification: `tests/extraction/model/test_views.py`
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from evidence.polygon import Polygon

__all__ = [
    "DrawingView",
    "TagStyle",
    "ViewIdentity",
    "ViewMatch",
    "ViewTag",
    "normalise_tag",
    "view_containing",
]


class TagStyle(StrEnum):
    """How a view tag is drawn, where that is known.

    Only `CIRCLE` so far, and deliberately: the issue's own context says *"the real drawing carries
    circled view tags"*, so that is the one style this project has evidence for. `None` means the style
    was not recorded — which is different from "not circled" and must stay different, because a reader
    comparing styles needs to know when we simply did not look.

    More members belong here when real drawings show other shapes (#274). Inventing `HEXAGON` now would
    put a name in a closed vocabulary that nothing has ever produced.
    """

    CIRCLE = "circle"


@dataclass(frozen=True, slots=True)
class ViewTag:
    """A view tag as the drawing prints it."""

    as_printed: str
    style: TagStyle | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.as_printed, str) or not self.as_printed.strip():
            raise ValueError(
                "as_printed must be the tag exactly as the drawing shows it; an empty tag would make "
                "every untagged region look tagged"
            )

    @property
    def normalised(self) -> str:
        """The comparable form. Separate from `as_printed`, never replacing it."""
        return normalise_tag(self.as_printed)


@dataclass(frozen=True, slots=True)
class ViewIdentity:
    """What makes two views the same view.

    A type rather than a tuple, so the untagged case cannot be flattened into the tagged one by accident.
    `tag` is the normalised tag for a tagged view; `region_key` is set instead for an untagged one, and
    exactly one of the two is ever present.
    """

    document_version_id: UUID
    page: int
    tag: str | None = None
    region_key: str | None = None

    def __post_init__(self) -> None:
        if (self.tag is None) == (self.region_key is None):
            raise ValueError(
                "a view is identified by its tag or, when untagged, by its region — exactly one. "
                "Neither would make all views equal; both would make the tag redundant."
            )


@dataclass(frozen=True, slots=True)
class DrawingView:
    """One view on one page: its tag if it has one, its region, and what it references.

    `region` carries the document version and page, so identity does not need them passed separately and
    cannot disagree with the geometry.
    """

    region: Polygon
    tag: ViewTag | None = None
    references: ViewTag | None = None
    """The view this one points at, where the drawing states one — a detail saying which plan it came
    from. Recorded rather than resolved: turning it into a link between views is grouping, which is
    B7.3's."""

    @property
    def page(self) -> int:
        return self.region.page

    @property
    def document_version_id(self) -> UUID:
        return self.region.document_version_id

    @property
    def is_tagged(self) -> bool:
        return self.tag is not None

    @property
    def identity(self) -> ViewIdentity:
        """`(page, tag)` for a tagged view; `(page, region)` for an untagged one.

        The untagged branch is what stops every unlabelled region on a sheet collapsing into one view.
        The region key is derived from the points, so it is stable across runs and two views with the same
        outline in the same place really are the same view.
        """
        if self.tag is not None:
            return ViewIdentity(self.document_version_id, self.page, tag=self.tag.normalised)
        return ViewIdentity(
            self.document_version_id,
            self.page,
            region_key=_region_key(self.region),
        )

    def contains(self, other: Polygon) -> bool:
        """Whether `other` lies inside this view's region.

        Delegates to `evidence/polygon.py`, which raises on a cross-page or cross-space comparison rather
        than returning a misleading geometric answer — so this cannot quietly compare two pages.
        """
        return self.region.contains(other)


@dataclass(frozen=True, slots=True)
class ViewMatch:
    """Which view an observation is in, or why that could not be said."""

    view: DrawingView | None
    reason: str

    @property
    def is_resolved(self) -> bool:
        return self.view is not None


def normalise_tag(as_printed: str) -> str:
    """A comparable view tag: uppercase, without surrounding whitespace or decoration.

    `(D)`, ` D `, `D` all compare equal — a circled tag read with its bubble should not be a different
    view from the same tag read without it. Inner structure is kept: `4/A-101` stays `4/A-101`, because the
    sheet reference is part of which view is meant.
    """
    return re.sub(r"^[\s(\[]+|[\s)\]]+$", "", as_printed).upper()


def _region_key(region: Polygon) -> str:
    """A stable key for an untagged view's region.

    Built from the exact `Decimal` coordinates rather than a hash of a float, so it is reproducible and
    two identical regions produce one key. Sorted, because a polygon written clockwise and the same
    polygon written anticlockwise are the same region.
    """
    return ";".join(sorted(f"{point.x}:{point.y}" for point in region.points))


def view_containing(target: Polygon, views: list[DrawingView]) -> ViewMatch:
    """Which of `views` contains `target`, or a refusal saying why not.

    Refuses when:

    * **No view contains it.** An observation outside every view is not in the nearest one.
    * **More than one does.** Nested views are normal — a detail inside a plan — so this is a real case
      rather than a corner one, and picking the smaller is a rule that needs real drawings to justify.
      Putting an observation in the wrong view is exactly the failure §3.2 says no tolerance check
      catches.

    Views on other pages are skipped rather than compared: `Polygon.contains` raises across pages, and a
    caller passing a whole package's views should get an answer rather than an exception.
    """
    same_page = [
        view
        for view in views
        if view.page == target.page and view.document_version_id == target.document_version_id
    ]

    containing = [view for view in same_page if view.contains(target)]

    if not containing:
        return ViewMatch(
            None,
            f"no view on page {target.page} contains this region. It is not placed in the nearest view: "
            "an observation outside every view has no view.",
        )

    if len(containing) > 1:
        described = ", ".join(
            sorted(
                view.tag.as_printed if view.tag is not None else "untagged" for view in containing
            )
        )
        return ViewMatch(
            None,
            f"{len(containing)} views on page {target.page} contain this region ({described}). Nested "
            "views are normal, so this is not resolved by preferring the smaller one — which view an "
            "observation belongs to decides which item it belongs to, and no later check catches that "
            "being wrong.",
        )

    only = containing[0]
    named = only.tag.as_printed if only.tag is not None else "the untagged region"
    return ViewMatch(only, f"contained by {named} on page {target.page}")
