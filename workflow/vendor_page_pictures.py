"""Stored vendor-only page pictures used by the reviewer's click-to-place flow (#948).

This module owns only the immutable artifact record and geometry-only snap suggestions. It does not
read values, assign meaning, or create/confirm parts. The person remains the sole decision-maker.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Page, VendorPagePicture
from evidence.coordinates import StoredPoint

__all__ = [
    "PNG",
    "RENDER_VENDOR_PAGE_PICTURES_WORKFLOW",
    "SnapPoint",
    "nearest_snap",
    "page_picture",
    "pages_without_pictures",
    "record_page_picture",
]

PNG: Final = "image/png"
RENDER_VENDOR_PAGE_PICTURES_WORKFLOW: Final = "render_vendor_page_pictures"


@dataclass(frozen=True, slots=True)
class SnapPoint:
    """A detected dimension-line or extension-line endpoint in stored page space."""

    point: StoredPoint
    source: str


def page_picture(session: Session, page_id: UUID) -> VendorPagePicture | None:
    return session.execute(
        select(VendorPagePicture).where(VendorPagePicture.page_id == page_id)
    ).scalar_one_or_none()


def pages_without_pictures(session: Session, page_ids: Sequence[UUID]) -> list[UUID]:
    if not page_ids:
        return []
    stored = set(
        session.scalars(
            select(VendorPagePicture.page_id).where(VendorPagePicture.page_id.in_(page_ids))
        )
    )
    return [
        page_id
        for page_id in session.scalars(
            select(Page.id).where(Page.id.in_(page_ids)).order_by(Page.index)
        )
        if page_id not in stored
    ]


def record_page_picture(
    session: Session,
    *,
    page_id: UUID,
    storage_key: str,
    sha256: str,
    media_type: str,
    dpi: int,
    width_px: int,
    height_px: int,
    snap_points: list[dict[str, str]],
    snap_tolerance: str | None,
) -> VendorPagePicture:
    existing = page_picture(session, page_id)
    if existing is not None:
        return existing
    row = VendorPagePicture(
        page_id=page_id,
        storage_key=storage_key,
        sha256=sha256,
        media_type=media_type,
        dpi=dpi,
        width_px=width_px,
        height_px=height_px,
        snap_points=snap_points,
        snap_tolerance=snap_tolerance,
    )
    session.add(row)
    session.flush()
    return row


def nearest_snap(
    point: StoredPoint, candidates: Sequence[SnapPoint], *, tolerance: Decimal
) -> tuple[SnapPoint, Decimal] | None:
    """Return the nearest endpoint at or inside the stated Euclidean stored-space tolerance.

    Equal-distance alternatives are ambiguous and therefore not suggested.
    """
    if not tolerance.is_finite() or tolerance < 0:
        raise ValueError("tolerance must be finite and zero or greater")
    ranked = sorted(
        (
            ((candidate.point.x - point.x) ** 2 + (candidate.point.y - point.y) ** 2, candidate)
            for candidate in candidates
        ),
        key=lambda entry: (entry[0], entry[1].point.x, entry[1].point.y, entry[1].source),
    )
    if not ranked:
        return None
    squared, candidate = ranked[0]
    if squared > tolerance * tolerance:
        return None
    if len(ranked) > 1 and ranked[1][0] == squared:
        return None
    return candidate, squared.sqrt()
