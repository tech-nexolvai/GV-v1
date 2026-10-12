"""Refuse a write sent from a screen that shows an older revision of the review (#1180).

Package-scoped writes always land on the newest revision (`…/packages/{package_id}/…` resolves the
highest revision number). A screen opened on revision 1 before someone started revision 2 would
therefore save a wall answer, a value or a pairing onto revision 2 — a revision the person never
looked at. So the screen says which revision it shows, in the `X-GV-Revision` header, on every
write; when that is not the newest, the write is refused with a plain 409 instead.

**Opt-in by the header, on writes only.** A request without the header (an older client, a script)
is unchanged, and reads are never refused: an earlier revision stays readable. Applied to every
route through one application-wide dependency, so a new package write is covered without anyone
remembering to add it.

Verification: `tests/api/test_revisions.py`
"""

from __future__ import annotations

from typing import Annotated, Final
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.models import Package, PackageRevision

__all__ = ["REVISION_HEADER", "refuse_stale_revision"]

#: The header a screen sends with each write: the revision number it shows.
REVISION_HEADER: Final = "X-GV-Revision"

_WRITES: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})

STALE: Final = (
    "You are looking at revision {shown}, which is read-only: revision {newest} replaced it. Switch "
    "to revision {newest} to make changes."
)


def refuse_stale_revision(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> None:
    """409 when a write names a revision that is not the package's newest. Otherwise nothing."""
    if request.method not in _WRITES:
        return
    shown_text = request.headers.get(REVISION_HEADER)
    if shown_text is None or not shown_text.strip().isdigit():
        return
    raw_package = request.path_params.get("package_id")
    raw_project = request.path_params.get("project_id")
    if raw_package is None or raw_project is None:
        return
    try:
        package_id, project_id = UUID(str(raw_package)), UUID(str(raw_project))
    except ValueError:
        return  # the route's own validation answers a malformed id
    newest = session.scalar(
        select(PackageRevision.revision_number)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    )
    shown = int(shown_text)
    if newest is not None and shown != newest:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=STALE.format(shown=shown, newest=newest),
        )
