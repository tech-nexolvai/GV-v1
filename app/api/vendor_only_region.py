"""The same region as an evidence crop, from the vendor-only page picture (#952).

An evidence crop is cut from the page rendered with both layers, so where GV's reviewer wrote over
the vendor's label the crop shows GV's note and hides the vendor's number. The worker already stores
a picture of every page with GV's markup left out (#948, `VendorPagePicture`). This shows the crop's
own region from that picture, labelled **"vendor's drawing without GV markup"**, beside the original.

**A visual aid, never a reading.** Nothing here reads a value, names a type, writes a row or feeds a
check: it serves pixels the worker already stored and says plainly when it has none. The original
crop and its provenance are untouched and stay the evidence.

**The same paper, by the published transform, or nothing.** The region is the reading's stored
polygon on that exact document version and page: a confirmed reading's stored polygon as it is, an
unconfirmed reading's pixel polygon carried to stored space by `PageTransform` at the dpi its run
recorded. The picture must be the one recorded for that page id, and its size must be the one the
page's transform gives at the picture's dpi. The pixel box is cut by `evidence.crop.pixel_box`, the
computation the original crop was cut by, with the same context margin. When any of that is missing
the answer is "not available" with the reason, never another page or a guessed region. Stored rows
that disagree about which page they are on are refused.

**Never starts work.** These GETs queue no job and call no model. Rendering missing page pictures is
the existing `POST .../pages/pictures`, a free render that the screen may ask for.

Verification: ``tests/api/test_vendor_only_region.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.confirmations import _candidate_crop_artifact
from app.api.confirmations import _revision as _current_revision
from app.api.dependencies import get_artifact_store, get_session
from app.api.drawing_parts import _verified_page_picture
from app.api.finding_chain import NOT_FOUND_DETAIL, evidence_crop_artifacts, revision_observation
from app.auth import Principal, require_project_access
from app.evidence.sides import page_transform_at, reading_transform
from app.models import Page, VendorPagePicture
from app.models.document import DocumentVersion, PackageRevisionDocument
from app.models.evidence import EvidenceArtifact, ObservationCandidate
from app.models.runs import ExtractionRun
from evidence.coordinates import ImagePoint, StoredPoint
from evidence.crop import (
    EVIDENCE_CONTEXT_MARGIN_PT,
    BoxCropSpec,
    cut_png_region,
    pixel_box,
    png_size,
)
from storage.store import ArtifactStore
from workflow.vendor_page_pictures import page_picture as recorded_page_picture

router = APIRouter(tags=["evidence"])

#: The words the screen shows over the second view. Fixed here so every screen says the same.
VENDOR_ONLY_LABEL: Final = "vendor's drawing without GV markup"

NO_PICTURE: Final = "the vendor-only picture of this page has not been made yet"
NO_TRANSFORM: Final = (
    "this page's transform was not recorded, so the region cannot be placed on the vendor-only "
    "picture"
)
NO_READING_DPI: Final = (
    "the resolution this reading was made at was not recorded, so its region cannot be placed"
)
WRONG_SIZE: Final = (
    "the vendor-only picture is not the size this page's transform gives, so the region cannot be "
    "placed on it"
)
MISMATCH: Final = (
    "the stored evidence and the page it points at do not agree, so no vendor-only view is shown"
)


class StoredRegionOut(BaseModel):
    """The reading's region in stored page space (0..1 across and down), as exact decimal text."""

    left: str
    top: str
    right: str
    bottom: str


class VendorOnlyRegionOut(BaseModel):
    """What the second, vendor-only view of one evidence region is, or why there is none."""

    label: str = Field(
        default=VENDOR_ONLY_LABEL, description="Show this over the vendor-only picture."
    )
    available: bool = Field(
        description=(
            "Whether the vendor-only picture of this exact region can be shown. False is a plain "
            "'not available yet'; never show another page or region instead."
        )
    )
    unavailable_reason: str | None = Field(
        default=None, description="Why it is not available, for the reviewer. Null when available."
    )
    crop_shows_gv_mark: bool | None = Field(
        default=None,
        description=(
            "Whether the original crop was found to show GV's markup. Null: no crop, or not checked "
            "(crops cut before #952 were checked for marks inside the vendor's drawing only)."
        ),
    )
    document_version_id: UUID
    page_id: UUID
    page_number: int = Field(description="1-based, as the screens number pages.")
    region: StoredRegionOut | None = Field(
        default=None, description="Null only when the reading's region could not be placed."
    )
    context_margin_pt: str = Field(description="Page context kept around the region, in points.")
    picture_dpi: int | None = Field(default=None, description="The vendor-only picture's dpi.")
    pixel_box: tuple[int, int, int, int] | None = Field(
        default=None,
        description="`(left, top, right, bottom)` cut from the vendor-only picture, when available.",
    )


@dataclass(frozen=True, slots=True)
class _Region:
    """One reading's region, resolved and checked against the page it points at."""

    page: Page
    points: tuple[StoredPoint, ...]
    crop: EvidenceArtifact | None
    unavailable: str | None = None


@dataclass(frozen=True, slots=True)
class _View:
    out: VendorOnlyRegionOut
    picture: VendorPagePicture | None


def _refuse_mismatch() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=MISMATCH)


def _checked_page(
    session: Session, *, document_version_id: UUID, page_id: UUID, crop: EvidenceArtifact | None
) -> Page:
    """The page a reading points at, only while the reading, its page and its crop agree on it."""
    page = session.get(Page, page_id)
    if page is None or page.document_version_id != document_version_id:
        raise _refuse_mismatch()
    if crop is not None and (
        crop.document_version_id != document_version_id or crop.page_id != page_id
    ):
        raise _refuse_mismatch()
    return page


def _observation_region(
    session: Session, project_id: UUID, package_id: UUID, canonical_observation_id: UUID
) -> _Region:
    observation = revision_observation(session, project_id, package_id, canonical_observation_id)
    crop = evidence_crop_artifacts(session, [observation.id]).get(observation.id)
    page = _checked_page(
        session,
        document_version_id=observation.document_version_id,
        page_id=observation.page_id,
        crop=crop,
    )
    if observation.coordinate_space != "stored":
        raise _refuse_mismatch()
    try:
        points = tuple(StoredPoint(Decimal(x), Decimal(y)) for x, y in observation.polygon)
    except (ArithmeticError, TypeError, ValueError) as error:
        raise _refuse_mismatch() from error
    return _Region(page=page, points=points, crop=crop)


def _candidate_region(
    session: Session, project_id: UUID, package_id: UUID, candidate_id: UUID
) -> _Region:
    revision = _current_revision(session, project_id, package_id)
    candidate = session.execute(
        select(ObservationCandidate)
        .join(DocumentVersion, DocumentVersion.id == ObservationCandidate.document_version_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == DocumentVersion.id,
        )
        .where(
            ObservationCandidate.id == candidate_id,
            PackageRevisionDocument.package_revision_id == revision.id,
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    crop = _candidate_crop_artifact(session, revision, candidate.id)
    page = _checked_page(
        session,
        document_version_id=candidate.document_version_id,
        page_id=candidate.page_id,
        crop=crop,
    )
    if candidate.coordinate_space != "image":
        raise _refuse_mismatch()
    run = session.get_one(ExtractionRun, candidate.extraction_run_id)
    transform = reading_transform(page, run)
    if transform is None:
        return _Region(
            page=page,
            points=(),
            crop=crop,
            unavailable=NO_TRANSFORM if run.dpi is not None else NO_READING_DPI,
        )
    try:
        points = tuple(
            transform.to_stored(ImagePoint(int(x), int(y))) for x, y in candidate.polygon
        )
    except (TypeError, ValueError) as error:
        raise _refuse_mismatch() from error
    return _Region(page=page, points=points, crop=crop)


def _view(session: Session, region: _Region) -> _View:
    """Where the region lies on the page's vendor-only picture, or why it cannot be shown."""
    page = region.page
    xs = [point.x for point in region.points]
    ys = [point.y for point in region.points]
    stored = (
        StoredRegionOut(
            left=str(min(xs)), top=str(min(ys)), right=str(max(xs)), bottom=str(max(ys))
        )
        if region.points
        else None
    )

    def out(**fields: object) -> VendorOnlyRegionOut:
        return VendorOnlyRegionOut(
            crop_shows_gv_mark=None if region.crop is None else region.crop.shows_gv_marks,
            document_version_id=page.document_version_id,
            page_id=page.id,
            page_number=page.index + 1,
            region=stored,
            context_margin_pt=str(EVIDENCE_CONTEXT_MARGIN_PT),
            **fields,  # type: ignore[arg-type]
        )

    if region.unavailable is not None:
        return _View(out(available=False, unavailable_reason=region.unavailable), None)

    picture = recorded_page_picture(session, page.id)
    if picture is None:
        return _View(out(available=False, unavailable_reason=NO_PICTURE), None)
    transform = page_transform_at(page, picture.dpi)
    if transform is None:
        return _View(
            out(available=False, unavailable_reason=NO_TRANSFORM, picture_dpi=picture.dpi), None
        )
    corner = transform.from_stored(StoredPoint(Decimal(1), Decimal(1)))
    if (corner.x, corner.y) != (picture.width_px, picture.height_px):
        return _View(
            out(available=False, unavailable_reason=WRONG_SIZE, picture_dpi=picture.dpi), None
        )
    try:
        spec = BoxCropSpec(
            document_version_id=page.document_version_id,
            page=page.index,
            left=min(xs),
            top=min(ys),
            right=max(xs),
            bottom=max(ys),
            context_margin_pt=EVIDENCE_CONTEXT_MARGIN_PT,
            dpi=picture.dpi,
        )
        box = pixel_box(picture.width_px, picture.height_px, spec)
    except (TypeError, ValueError) as error:
        raise _refuse_mismatch() from error
    return _View(out(available=True, picture_dpi=picture.dpi, pixel_box=box), picture)


def _picture(store: ArtifactStore, view: _View) -> Response:
    """The region's pixels from the stored, digest-checked picture, or 404 with the reason."""
    if view.picture is None or view.out.pixel_box is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=view.out.unavailable_reason or NO_PICTURE,
        )
    content = _verified_page_picture(store, view.picture)
    try:
        if png_size(content) != (view.picture.width_px, view.picture.height_px):
            raise ValueError("the stored picture's size is not its recorded size")
        region = cut_png_region(content, view.out.pixel_box)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the stored vendor page picture cannot be cut, so it cannot be shown",
        ) from error
    return Response(content=region, media_type="image/png", headers={"Cache-Control": "no-store"})


_PNG_RESPONSE: Final[dict[int | str, dict[str, Any]]] = {
    status.HTTP_200_OK: {
        "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
        "description": "The same region, cut from the vendor-only page picture.",
    }
}


@router.get(
    "/projects/{project_id}/packages/{package_id}/evidence/{canonical_observation_id}/vendor-only",
    response_model=VendorOnlyRegionOut,
    summary="The vendor-only view of a confirmed reading's evidence region, or why there is none",
)
def evidence_vendor_only(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    canonical_observation_id: UUID,
) -> VendorOnlyRegionOut:
    region = _observation_region(session, project_id, package_id, canonical_observation_id)
    return _view(session, region).out


@router.get(
    "/projects/{project_id}/packages/{package_id}/evidence/{canonical_observation_id}"
    "/vendor-only/picture",
    response_class=Response,
    responses=_PNG_RESPONSE,
    summary="The vendor-only picture of a confirmed reading's evidence region",
)
def evidence_vendor_only_picture(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
    canonical_observation_id: UUID,
) -> Response:
    region = _observation_region(session, project_id, package_id, canonical_observation_id)
    return _picture(store, _view(session, region))


@router.get(
    "/projects/{project_id}/packages/{package_id}/candidates/{candidate_id}/vendor-only",
    response_model=VendorOnlyRegionOut,
    summary="The vendor-only view of an unconfirmed reading's crop region, or why there is none",
)
def candidate_vendor_only(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    candidate_id: UUID,
) -> VendorOnlyRegionOut:
    return _view(session, _candidate_region(session, project_id, package_id, candidate_id)).out


@router.get(
    "/projects/{project_id}/packages/{package_id}/candidates/{candidate_id}/vendor-only/picture",
    response_class=Response,
    responses=_PNG_RESPONSE,
    summary="The vendor-only picture of an unconfirmed reading's crop region",
)
def candidate_vendor_only_picture(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
    candidate_id: UUID,
) -> Response:
    region = _candidate_region(session, project_id, package_id, candidate_id)
    return _picture(store, _view(session, region))


__all__ = ["VENDOR_ONLY_LABEL", "VendorOnlyRegionOut", "router"]
