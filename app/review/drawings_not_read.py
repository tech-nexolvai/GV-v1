"""Drawings on a read page that were not read, listed with the reason (#1193 review).

On a page that prints headings, only the drawing whose own heading says it is the vendor's (or that a
person confirmed) is read for a countertop row (`workflow/stages.py`). A drawing there with no role
of its own — its label is not one we know, it has none, or its heading names another drawing —
is not read. When another drawing on the same page was read, the page is not listed with the pages
not checked, so without this list such a drawing would be silent: AI Set 3's second-set vendor drawing
beside the first-set one, or a second vendor drawing under another.

**A vendor drawing whose row was not read** (#1197 phase 1). V1 reads one countertop row per page.
On a page with two vendor drawings — the first-round and second-round drawings side by side, or a
second drawing a reviewer confirmed as the vendor's — the row read lies in one of them, and the other
is not read. Its role is the vendor's, so the rule above does not list it; without this it would be
silent. It is listed, naming the drawing the row read lies in. When that row lies in no drawing, or
cannot be placed on the sheet, the drawing read cannot be named: on a page with two or more vendor
drawings each is listed then (at least one was not read), and on a page with one, none is. A page
where no row was read lists none of its vendor drawings: it is listed already with the pages with no
countertop. **Which round counts is not decided here** (#1197 phase 2 waits for that decision).

**Listed, never blocking** (the #1108 "second row" pattern): the countertop that was read on the page is
checked as usual; this drawing is named with its place on the page and the reason, on the screen, in
the signed PDF and in the workbook, for the reviewer to look at.

No extraction imports: the API reaches this (`tests/api/test_no_heavy_work.py`); the drawings'
regions and suggestions are read from the database.

Source: #1193 review; #1197 phase 1. Verification: tests/api/test_unchosen_row_pages.py,
tests/review/test_drawings_not_read.py.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.sides import page_transform_at
from app.models import (
    Document,
    DocumentVersion,
    DrawingView,
    ObservationCandidate,
    PackageRevisionDocument,
    Page,
    ViewRole,
    ViewRoleProposal,
)
from app.models.runs import ExtractionRun
from app.schemas.visual_ui import DrawingNotReadOut
from evidence.coordinates import ImagePoint
from vocabulary.panel_headings import NO_HEADINGS_ON_PAGE, Box, place_words
from workflow.slot_row_scope import latest_slot_reader_run

__all__ = ["ONE_ROW_PER_PAGE", "drawings_not_read"]

#: Why a vendor drawing beside the one read was not read (#1197 phase 1), in the reviewer's words.
ONE_ROW_PER_PAGE: Final = "only one countertop is read per page for now"

Point = tuple[Decimal, Decimal]


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


def _covers(box: Box, point: Point) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def _is_vendors(view: DrawingView, suggestion: ViewRoleProposal | None) -> bool:
    """The role a person confirmed, or else the one its heading suggests: the drawings the reader
    reads as the vendor's (`workflow/stages.py` `_view_role`)."""
    role = view.role if view.role is not None else (suggestion and suggestion.proposed_role)
    return role == ViewRole.SHOP.value


def _read_rows(
    session: Session, revision_id: UUID, pages: Iterable[Page]
) -> dict[UUID, tuple[Point, ...]]:
    """For each of `pages` where the newest slot-reader run read a countertop row, the centre of
    each of that row's boxes in the stored space; empty where the row cannot be placed (its run
    recorded no dpi, or the page no transform). A page with no row read is left out."""
    by_id = {page.id: page for page in pages}
    run_id = latest_slot_reader_run(session, revision_id) if by_id else None
    if run_id is None:
        return {}
    run = session.get(ExtractionRun, run_id)
    read: dict[UUID, list[Point]] = {}
    for candidate in session.scalars(
        select(ObservationCandidate)
        .where(
            ObservationCandidate.extraction_run_id == run_id,
            ObservationCandidate.page_id.in_(by_id),
        )
        .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
    ):
        flags = candidate.ambiguity_flags or []
        if "slot-reader" not in flags or not any(flag.startswith("row-rank:") for flag in flags):
            continue
        centres = read.setdefault(candidate.page_id, [])
        transform = (
            None
            if run is None or run.dpi is None
            else page_transform_at(by_id[candidate.page_id], run.dpi)
        )
        if transform is None or not candidate.polygon:
            continue
        try:
            points = [transform.to_stored(ImagePoint(int(x), int(y))) for x, y in candidate.polygon]
        except (ArithmeticError, IndexError, TypeError, ValueError):
            continue
        xs = [point.x for point in points]
        ys = [point.y for point in points]
        centres.append(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))
    return {page_id: tuple(centres) for page_id, centres in read.items()}


def drawings_not_read(
    session: Session, revision_id: UUID, read_pages: Collection[int]
) -> tuple[DrawingNotReadOut, ...]:
    """Each pasted drawing with no role and no role suggested, on a vendor-file page that was read
    (`read_pages`, page numbers from 1), on a page that prints headings; and each vendor drawing
    on such a page whose countertop row was not the one read there (#1197 phase 1); in page order.
    """
    if not read_pages:
        return ()
    rows = session.execute(
        select(Page, DrawingView)
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
        .where(ViewRoleProposal.drawing_view_id.in_([view.id for _page, view in rows]))
        .order_by(ViewRoleProposal.created_at.desc(), ViewRoleProposal.id.desc())
    ):
        latest.setdefault(proposal.drawing_view_id, proposal)
    read_rows = _read_rows(session, revision_id, {page.id: page for page, _view in rows}.values())
    by_page: dict[int, list[tuple[DrawingView, Box]]] = {}
    for page, view in rows:
        box = _box(view.region)
        if box is not None:
            by_page.setdefault(page.index, []).append((view, box))
    listed: list[DrawingNotReadOut] = []
    for index, views in sorted(by_page.items()):
        drawings = [
            (view, box)
            for view, box in views
            if not any(_holds(other, box) for _other_view, other in views)
        ]
        places = place_words([box for _view, box in drawings])
        found = [
            read_rows[page_id]
            for page_id in {view.page_id for view, _box_ in views}
            if page_id in read_rows
        ]
        centres = None if not found else tuple(centre for row in found for centre in row)
        vendors = sum(_is_vendors(view, latest.get(view.id)) for view, _box_ in drawings)
        read_in = next(
            (
                place
                for (_view, box), place in zip(drawings, places, strict=True)
                if centres and any(_covers(box, centre) for centre in centres)
            ),
            None,
        )
        for (view, box), place in zip(drawings, places, strict=True):
            suggestion = latest.get(view.id)
            if (
                view.role is None
                and suggestion is not None
                and suggestion.proposed_role is None
                and suggestion.reason != NO_HEADINGS_ON_PAGE
            ):
                why = f"was not read for a countertop: {suggestion.reason}"
            elif (
                centres is None
                or not _is_vendors(view, suggestion)
                or any(_covers(box, centre) for centre in centres)
            ):
                continue
            elif read_in is not None:
                why = (
                    "is a vendor drawing that was not read for a countertop: "
                    f"{ONE_ROW_PER_PAGE}, and the one read is in {read_in}"
                )
            elif vendors >= 2:
                why = (
                    "is a vendor drawing that may not have been read for a countertop: "
                    f"{ONE_ROW_PER_PAGE}, and the one read could not be placed in any drawing on "
                    "the page"
                )
            else:
                continue
            listed.append(
                DrawingNotReadOut(
                    page_number=index + 1,
                    drawing=place,
                    reason=f"{place[0].upper()}{place[1:]} {why}.",
                )
            )
    return tuple(listed)
