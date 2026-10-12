"""Drawings on a read page that were not read, listed with the reason (#1193 review).

On a page that prints headings, only the drawing whose own heading says it is the vendor's (or that a
person confirmed) is read for a countertop row (`workflow/stages.py`). A drawing there with no role
of its own — its label is not one we know, it has none, or its heading names another drawing —
is not read. When another drawing on the same page was read, the page is not listed with the pages
not checked, so without this list such a drawing would be silent: AI Set 3's second-set vendor drawing
beside the first-set one, or a second vendor drawing under another.

**Listed, never blocking** (the #1108 "second row" pattern): the countertop that was read on the page is
checked as usual; this drawing is named with its place on the page and the reason, on the screen, in
the signed PDF and in the workbook, for the reviewer to look at.

No extraction imports: the API reaches this (`tests/api/test_no_heavy_work.py`); the drawings'
regions and suggestions are read from the database.

Source: #1193 review. Verification: tests/api/test_unchosen_row_pages.py.
"""

from __future__ import annotations

from collections.abc import Collection
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Document,
    DocumentVersion,
    DrawingView,
    PackageRevisionDocument,
    Page,
    ViewRoleProposal,
)
from app.schemas.visual_ui import DrawingNotReadOut
from vocabulary.panel_headings import NO_HEADINGS_ON_PAGE, Box, place_words

__all__ = ["drawings_not_read"]


def _box(region: object) -> Box | None:
    if not isinstance(region, dict) or region.get("space") != "stored":
        return None
    points = region.get("points")
    if not isinstance(points, list) or not points:
        return None
    try:
        xs = [Decimal(str(point[0])) for point in points]
        ys = [Decimal(str(point[1])) for point in points]
    except (ArithmeticError, IndexError, TypeError, ValueError):
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _holds(outer: Box, inner: Box) -> bool:
    return (
        outer != inner
        and outer[0] <= inner[0]
        and inner[2] <= outer[2]
        and outer[1] <= inner[1]
        and inner[3] <= outer[3]
    )


def drawings_not_read(
    session: Session, revision_id: UUID, read_pages: Collection[int]
) -> tuple[DrawingNotReadOut, ...]:
    """Each pasted drawing with no role and no role suggested, on a vendor-file page that was read
    (`read_pages`, page numbers from 1), on a page that prints headings; in page order."""
    if not read_pages:
        return ()
    rows = session.execute(
        select(Page.index, DrawingView)
        .join(Page, Page.id == DrawingView.page_id)
        .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == Page.document_version_id,
        )
        .where(
            PackageRevisionDocument.package_revision_id == revision_id,
            Document.kind == "shop",
            Page.index.in_([number - 1 for number in read_pages]),
            DrawingView.tag.like("panel-%"),
        )
        .order_by(Page.index, DrawingView.tag)
    ).all()
    if not rows:
        return ()
    latest: dict[UUID, ViewRoleProposal] = {}
    for proposal in session.scalars(
        select(ViewRoleProposal)
        .where(ViewRoleProposal.drawing_view_id.in_([view.id for _index, view in rows]))
        .order_by(ViewRoleProposal.created_at.desc(), ViewRoleProposal.id.desc())
    ):
        latest.setdefault(proposal.drawing_view_id, proposal)
    by_page: dict[int, list[tuple[DrawingView, Box]]] = {}
    for index, view in rows:
        box = _box(view.region)
        if box is not None:
            by_page.setdefault(index, []).append((view, box))
    listed: list[DrawingNotReadOut] = []
    for index, views in sorted(by_page.items()):
        drawings = [
            (view, box)
            for view, box in views
            if not any(_holds(other, box) for _other_view, other in views)
        ]
        places = place_words([box for _view, box in drawings])
        for (view, _box_), place in zip(drawings, places, strict=True):
            suggestion = latest.get(view.id)
            if (
                view.role is not None
                or suggestion is None
                or suggestion.proposed_role is not None
                or suggestion.reason == NO_HEADINGS_ON_PAGE
            ):
                continue
            listed.append(
                DrawingNotReadOut(
                    page_number=index + 1,
                    drawing=place,
                    reason=(
                        f"{place[0].upper()}{place[1:]} was not read for a countertop: "
                        f"{suggestion.reason}."
                    ),
                )
            )
    return tuple(listed)
