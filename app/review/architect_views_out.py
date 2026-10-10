"""One view of the architect's own file, as the screen and the reports name it (#1166, #1168).

Shared by the reviewer picker (`app/api/architect_matches.py`) and the countertop results
(`app/review/architect_view_match.py`), so both describe a view the same way: its page, sheet,
title, stored frame and picture link.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DocumentVersion, Page
from app.models.evidence import ArchitectViewIndexEntry
from app.review.row_location import RowLocation
from app.schemas.architect_matches import ArchitectViewRefOut
from workflow.architect_match_records import ARCHITECT_FILE_NAME

__all__ = ["API_PREFIX", "PICTURE_PATH", "view_out", "views_by_id"]

#: The prefix `app/main.py` mounts this router under (`API_PREFIX`), for the picture links.
API_PREFIX: Final = "/api/v1"
PICTURE_PATH: Final = (
    "/projects/{project_id}/packages/{package_id}/architect-views/{view_id}/picture"
)


def _label(entry: ArchitectViewIndexEntry, page_number: int) -> str:
    words = f"Page {page_number}, view {entry.view_number}"
    if entry.title:
        words += f": {entry.title}"
    if entry.sheet_number:
        words += f" (sheet {entry.sheet_number})"
    return words


def _region(entry: ArchitectViewIndexEntry, page_number: int) -> RowLocation | None:
    points = entry.extent.get("stored_points") if isinstance(entry.extent, dict) else None
    if not isinstance(points, list) or len(points) < 3:
        return None
    try:
        polygon = [[str(point[0]), str(point[1])] for point in points]
    except (IndexError, TypeError):
        return None
    return RowLocation(
        page_id=entry.page_id,
        document_version_id=entry.document_version_id,
        page_number=page_number,
        polygon=polygon,
    )


def views_by_id(
    session: Session, view_ids: set[UUID]
) -> dict[UUID, tuple[ArchitectViewIndexEntry, int, UUID]]:
    if not view_ids:
        return {}
    return {
        entry.id: (entry, page_index + 1, document_id)
        for entry, page_index, document_id in session.execute(
            select(ArchitectViewIndexEntry, Page.index, DocumentVersion.document_id)
            .join(Page, Page.id == ArchitectViewIndexEntry.page_id)
            .join(
                DocumentVersion, DocumentVersion.id == ArchitectViewIndexEntry.document_version_id
            )
            .where(ArchitectViewIndexEntry.id.in_(tuple(view_ids)))
        )
    }


def view_out(
    entry: ArchitectViewIndexEntry,
    page_number: int,
    document_id: UUID,
    *,
    project_id: UUID,
    package_id: UUID,
) -> ArchitectViewRefOut:
    return ArchitectViewRefOut(
        view_id=entry.id,
        document_id=document_id,
        document_version_id=entry.document_version_id,
        file_name=ARCHITECT_FILE_NAME,
        page_number=page_number,
        sheet_number=entry.sheet_number,
        bubble=entry.bubble,
        title=entry.title,
        scale_note=entry.scale_note,
        label=_label(entry, page_number),
        region=_region(entry, page_number),
        picture_url=(
            None
            if entry.picture_storage_key is None
            else API_PREFIX
            + PICTURE_PATH.format(project_id=project_id, package_id=package_id, view_id=entry.id)
        ),
        separated=entry.separated,
    )
