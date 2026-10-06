"""A picture of any drawing page, vendor layer only, for the reviewer's left-hand pane.

The Measure screen shows the drawing on the left and the form on the right (admin, 2026-10-06). The
existing vendor page pictures (#948) exist only for a drawing confirmed as the vendor's, which a
combined set does not have before review. This renders the page on request instead: read-only, no
model, no row written. The reviewer's markup is left out (`vendor_only=True`), as everywhere a page
is shown as the vendor's drawing.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.api.drawing_views import NOT_FOUND_DETAIL, _revision
from app.auth import Principal, require_project_access
from app.models import Page
from app.models.document import (
    Document,
    DocumentKind,
    DocumentVersion,
    PackageRevisionDocument,
    SourceArtifact,
)
from evidence.crop import encode_png
from extraction.rasterise import render_page
from storage.hashing import ArtifactCorrupt, IntegrityRecordMissing
from storage.store import ArtifactStore

router = APIRouter(tags=["drawings"])

#: Enough to read a dimension when zoomed in the browser, small enough to send quickly.
DEFAULT_DPI: Final = 110
MAX_DPI: Final = 200
#: A memory budget for one render, as `render_page` requires one.
MAX_PIXELS: Final = 40_000_000


#: Rendered pages by (document digest, page index, dpi). Small and process-local: a page is
#: re-rendered after a restart, which costs well under a second.
_CACHE: dict[tuple[str, int, int], bytes] = {}
_CACHE_LIMIT: Final = 48


def _rendered(
    data: bytes, digest: str, page_index: int, version_id: UUID, content_hash: str, dpi: int
) -> bytes:
    key = (digest, page_index, dpi)
    if key not in _CACHE:
        image = render_page(
            data,
            page_index,
            document_version_id=version_id,
            page_content_hash=content_hash,
            dpi=dpi,
            maximum_pixels=MAX_PIXELS,
            vendor_only=True,
        )
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = encode_png(image.width_px, image.height_px, image.rgb_bytes)
    return _CACHE[key]


@router.get(
    "/projects/{project_id}/packages/{package_id}/pages/{page_number}/picture",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}
        }
    },
    summary="View a drawing page (vendor layer only)",
)
def page_picture(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
    page_number: int,
    dpi: Annotated[int, Query(ge=50, le=MAX_DPI)] = DEFAULT_DPI,
) -> Response:
    """Page `page_number` (1-based) of this package, as a PNG of the vendor's layer.

    When the package holds two drawings with that page, the shop drawing's page is shown: the pane
    is where the reviewer reads the vendor's numbers.
    """
    revision = _revision(session, project_id, package_id)
    row = session.execute(
        select(Page, DocumentVersion, SourceArtifact)
        .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
        .join(SourceArtifact, SourceArtifact.id == DocumentVersion.source_artifact_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == DocumentVersion.id,
        )
        .join(Document, Document.id == PackageRevisionDocument.document_id)
        .where(
            PackageRevisionDocument.package_revision_id == revision.id,
            Page.index == page_number - 1,
        )
        .order_by(case((Document.kind == DocumentKind.SHOP.value, 0), else_=1), Document.id)
        .limit(1)
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    page, version, artifact = row
    try:
        with store.get(artifact.storage_key) as stored:
            data = stored.read()
    except (ArtifactCorrupt, FileNotFoundError, IntegrityRecordMissing) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="the stored drawing is unavailable"
        ) from error
    digest = hashlib.sha256(data).hexdigest()
    if digest != version.sha256:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the stored drawing failed its integrity check, so it cannot be shown",
        )
    png = _rendered(data, digest, page.index, version.id, page.content_hash, dpi)
    return Response(
        content=png, media_type="image/png", headers={"Cache-Control": "private, max-age=600"}
    )
