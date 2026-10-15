"""The pages of both drawings, for the simple review's side-by-side viewer (#1342, W7).

`GET .../pages` lists each document of the package's latest revision (the vendor's shop drawing
first, then the architect's drawing), with its pages and, per page, whether its picture is ready, its
size and where to fetch it. It reads rows only: no rendering, no store access, no model call.

**Combined sheets.** When the architect's drawing is pasted onto the vendor's sheet, the architect
side of the viewer is the same page zoomed to the architect's drawing. Each page therefore lists its
architect regions: drawings a person (or code's two judgments, #1052) confirmed as the architect's,
and drawings whose sheet heading only *suggests* the architect's, marked `role_confirmed: false`. A
suggestion is a place to look, never a role: nothing here sets or decides one.

**Big pictures.** Page pictures are the worker's 300 dpi full-page PNGs (`vendor_page_pictures`); a
large sheet is tens of megapixels. Shrinking one here would mean decoding it in the API process,
which `tests/api/test_no_heavy_work.py` keeps out, and storing a smaller copy needs a worker change.
So each page reports its pixel size and dpi, and the viewer decides how to show it (for example,
decode once with `createImageBitmap` at the size it draws, and never upscale past the source).

Coordinates are the stored space (`evidence/coordinates.py`): fractions of the visible page, top-left
origin, as exact decimal text. Multiply by the picture's `width_px`/`height_px` to get pixels.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.drawing_views import _revision
from app.auth import Principal, require_project_access
from app.evidence.sides import has_separate_architect_file
from app.models import Page, VendorPagePicture, ViewRole
from app.models.document import Document, DocumentKind, DocumentVersion, PackageRevisionDocument
from app.review.architect_views_out import API_PREFIX
from workflow.view_roles import revision_views

router = APIRouter(tags=["drawings"])

#: Where one page's picture is served (`app/api/page_pictures.py`), pinned to its own document so a
#: two-file package never shows the shop drawing's page 1 for the architect's page 1.
PICTURE_PATH: Final = (
    "/projects/{project_id}/packages/{package_id}/pages/{page_number}/picture"
    "?document_version_id={document_version_id}"
)

PictureStatus = Literal["ready", "not_ready", "render_failed"]


class BoundsOut(BaseModel):
    """The smallest box around a region, in stored space (exact decimal text)."""

    x0: str
    y0: str
    x1: str
    y1: str


class ArchitectRegionOut(BaseModel):
    """One architect's drawing on a page: where the viewer zooms the architect side to."""

    view_id: UUID
    tag: str
    role_confirmed: bool
    """True once the drawing's role is confirmed as the architect's. False when only the sheet's
    heading suggests it: shown as a place to look, never treated as the architect's."""
    heading: str | None
    """The label the sheet prints above the drawing, when one suggested its role."""
    polygon: list[list[str]]
    bbox: BoundsOut


class PagePictureOut(BaseModel):
    url: str
    media_type: str
    dpi: int
    width_px: int
    height_px: int


class ReviewPageOut(BaseModel):
    page_id: UUID
    page_number: int
    """1-based, within its own document."""
    sheet_number: str | None
    picture_status: PictureStatus
    """`ready`; `not_ready` until the worker renders it (ask with `POST .../pages/pictures`); or
    `render_failed` when the page could not be read, so no picture will come."""
    picture: PagePictureOut | None
    architect_regions: list[ArchitectRegionOut]


class ReviewDocumentOut(BaseModel):
    document_id: UUID
    document_version_id: UUID
    kind: str
    """The upload's kind: `shop`, `architectural`, or another accepted kind."""
    side: Literal["vendor", "architect", "other"]
    file_name: str | None
    page_count: int
    """Pages in the file. `pages` may hold fewer while the pages are still being read."""
    pages: list[ReviewPageOut]


class ReviewPagesOut(BaseModel):
    package_revision_id: UUID
    revision_number: int
    combined_set: bool
    """True when the architect's drawings are not a separate file: the architect side of the viewer
    is the vendor's page, zoomed to its architect regions."""
    pages_without_pictures: int
    documents: list[ReviewDocumentOut]


_SIDE: Final[dict[str, Literal["vendor", "architect"]]] = {
    DocumentKind.SHOP.value: "vendor",
    DocumentKind.ARCHITECTURAL.value: "architect",
}


def _region(points: object) -> tuple[list[list[str]], BoundsOut] | None:
    """The polygon and its box, or None for a region that is not a readable polygon."""
    if not isinstance(points, list) or len(points) < 3:
        return None
    try:
        exact = [(Decimal(str(point[0])), Decimal(str(point[1]))) for point in points]
    except (IndexError, TypeError, InvalidOperation):
        return None
    xs, ys = [x for x, _ in exact], [y for _, y in exact]
    return [[str(x), str(y)] for x, y in exact], BoundsOut(
        x0=str(min(xs)), y0=str(min(ys)), x1=str(max(xs)), y1=str(max(ys))
    )


def _architect_regions(session: Session, revision_id: UUID) -> dict[UUID, list[ArchitectRegionOut]]:
    by_page: dict[UUID, list[ArchitectRegionOut]] = {}
    for entry in revision_views(session, revision_id):
        view, proposal = entry.view, entry.proposal
        suggested = (
            view.role is None
            and proposal is not None
            and proposal.proposed_role == ViewRole.ARCH.value
        )
        if view.role != ViewRole.ARCH.value and not suggested:
            continue
        region = view.region if isinstance(view.region, dict) else {}
        shape = _region(region.get("points"))
        if shape is None:
            continue
        polygon, bbox = shape
        by_page.setdefault(view.page_id, []).append(
            ArchitectRegionOut(
                view_id=view.id,
                tag=view.tag,
                role_confirmed=view.role == ViewRole.ARCH.value,
                heading=None if proposal is None else proposal.heading,
                polygon=polygon,
                bbox=bbox,
            )
        )
    return by_page


@router.get(
    "/projects/{project_id}/packages/{package_id}/pages",
    response_model=ReviewPagesOut,
    summary="Both drawings' pages and their pictures, for the side-by-side viewer",
)
def review_pages(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> ReviewPagesOut:
    """Every document of the latest revision, vendor first, with each page's picture readiness."""
    revision = _revision(session, project_id, package_id)
    documents = session.execute(
        select(Document, DocumentVersion)
        .join(PackageRevisionDocument, PackageRevisionDocument.document_id == Document.id)
        .join(DocumentVersion, DocumentVersion.id == PackageRevisionDocument.document_version_id)
        .where(PackageRevisionDocument.package_revision_id == revision.id)
        .order_by(
            case(
                (Document.kind == DocumentKind.SHOP.value, 0),
                (Document.kind == DocumentKind.ARCHITECTURAL.value, 1),
                else_=2,
            ),
            Document.created_at,
            Document.id,
        )
    ).all()
    version_ids = [version.id for _, version in documents]
    pages_by_version: dict[UUID, list[tuple[Page, VendorPagePicture | None]]] = {}
    for page, stored in session.execute(
        select(Page, VendorPagePicture)
        .outerjoin(VendorPagePicture, VendorPagePicture.page_id == Page.id)
        .where(Page.document_version_id.in_(version_ids))
        .order_by(Page.index, Page.id)
    ).all():
        pages_by_version.setdefault(page.document_version_id, []).append((page, stored))
    regions = _architect_regions(session, revision.id)

    missing = 0
    out: list[ReviewDocumentOut] = []
    for document, version in documents:
        pages: list[ReviewPageOut] = []
        for page, picture in pages_by_version.get(version.id, []):
            status: PictureStatus
            if picture is not None:
                status = "ready"
            elif page.render_failed:
                status = "render_failed"
            else:
                status = "not_ready"
            missing += picture is None
            pages.append(
                ReviewPageOut(
                    page_id=page.id,
                    page_number=page.index + 1,
                    sheet_number=page.sheet_number,
                    picture_status=status,
                    picture=(
                        None
                        if picture is None
                        else PagePictureOut(
                            url=API_PREFIX
                            + PICTURE_PATH.format(
                                project_id=project_id,
                                package_id=package_id,
                                page_number=page.index + 1,
                                document_version_id=version.id,
                            ),
                            media_type=picture.media_type,
                            dpi=picture.dpi,
                            width_px=picture.width_px,
                            height_px=picture.height_px,
                        )
                    ),
                    architect_regions=regions.get(page.id, []),
                )
            )
        out.append(
            ReviewDocumentOut(
                document_id=document.id,
                document_version_id=version.id,
                kind=str(document.kind),
                side=_SIDE.get(str(document.kind), "other"),
                file_name=version.file_name,
                page_count=version.page_count,
                pages=pages,
            )
        )
    return ReviewPagesOut(
        package_revision_id=revision.id,
        revision_number=revision.revision_number,
        combined_set=not has_separate_architect_file(session, revision.id),
        pages_without_pictures=missing,
        documents=out,
    )
