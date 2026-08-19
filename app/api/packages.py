"""Packages: create one, list them, read one (#205, C2.3).

A package is the unit a reviewer signs off, so these three routes are the front door to everything
else. They are short on purpose — backend §4.1 gives the control plane short work only, and there is
nothing here but three small queries and one insert.

**Project scope is an isolation boundary, not a filter.** Every route carries
`require_project_access`, *and* every query filters on the project. The dependency establishes that
the caller may see this project; the `WHERE` clause establishes that these rows are this project's.
A package in another project answers with the same `404` and the same body as a package that does not
exist, because a `403` would confirm it exists and confirming it is what the boundary is for
(`docs/DESIGN_PLATFORM.md` §4.3).

**Creating a package creates its first revision.** A `Document` hangs off a `package_revisions` row,
not off the package, so a package with no revision is a package nothing can be uploaded to. The
revision is created in `CREATED` and this module never moves it: the transition table and the only
function allowed to change state belong to #209 (C3.1). The one state event written here is the
revision's birth — `from_state` null, `to_state` CREATED — which is the single event no transition
function can produce, because there is no prior state to come from.

**Paging is keyset, not offset.** `OFFSET 20` skips the first twenty rows *of the query running now*,
so a package created between two requests shifts every later row down by one and the row that moved
across the boundary is never seen. A cursor names the last row instead, and a row's sort key here
(`created_at`, `id`) never changes.

Source: backend proposal §10.2, §11 · Design: `docs/DESIGN_PLATFORM.md` §4.1, §4.3 ·
Verification: `tests/api/test_packages.py`
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.auth import Action, Principal, require_action, require_project_access
from app.models import Package, PackageRevision, PackageState, PackageStateEvent, Project
from app.schemas.packages import PackageCreate, PackageOut, PackagePage

router = APIRouter(tags=["packages"])

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50

#: What every refusal says. Nothing about the project, the package or the reason — a message that
#: explained itself would give back exactly what the 404 was chosen to hide.
NOT_FOUND_DETAIL = "Not found"

#: The revision number a package starts at, and the sequence number of its first state event. Both are
#: 1 rather than 0: a reviewer reads "revision 1", and `docs/DESIGN_EXTRACTION.md` §3.1's convention is
#: that anything a person sees counts from one.
FIRST_REVISION = 1
FIRST_EVENT_SEQUENCE = 1

ORDERING_DESCRIPTION = (
    "Newest first, then by id descending. The id is what makes the order total: two packages created "
    "in the same microsecond would otherwise tie, and a tie is a page boundary that can fall in two "
    "different places on two different requests."
)


# ---------------------------------------------------------------------------
# The cursor
# ---------------------------------------------------------------------------


def encode_cursor(created_at: datetime, package_id: UUID) -> str:
    """Render a position in the list as one opaque string.

    Opaque on purpose: the components are an implementation detail of the ordering, and a client that
    parsed them would break the day the ordering gains a tie-break.
    """
    payload = json.dumps(
        {"t": created_at.isoformat(), "i": str(package_id)}, separators=(",", ":"), sort_keys=True
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(raw: str) -> tuple[datetime, UUID]:
    """Read a cursor back, refusing anything we did not issue.

    Refuses rather than falling back to the first page. A cursor that silently means "start again"
    turns a client bug into an endless loop over page one, and every request succeeds while it happens.
    """
    try:
        payload = json.loads(base64.urlsafe_b64decode((raw + "=" * (-len(raw) % 4)).encode()))
        return datetime.fromisoformat(payload["t"]), UUID(payload["i"])
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        UnicodeDecodeError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "The cursor is not one this endpoint issued. Use the `next_cursor` from the previous "
                "page verbatim, or omit it to start from the beginning."
            ),
        ) from error


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------


def _current_revision() -> Any:
    """A subquery naming each package's highest revision number.

    The highest number rather than the newest `created_at`: `revision_number` is what
    `docs/DESIGN_PLATFORM.md` §5 orders revisions by, and two revisions written in the same microsecond
    have no order by timestamp at all.
    """
    return (
        select(
            PackageRevision.package_id.label("package_id"),
            func.max(PackageRevision.revision_number).label("revision_number"),
        )
        .group_by(PackageRevision.package_id)
        .subquery()
    )


def _package_query(project_id: UUID) -> Select[Any]:
    """Every package in one project, with the revision documents currently attach to."""
    current = _current_revision()
    return (
        select(
            Package.id,
            Package.project_id,
            Package.vendor,
            Package.created_at,
            PackageRevision.id.label("current_revision_id"),
            PackageRevision.revision_number.label("current_revision_number"),
            PackageRevision.state,
        )
        .join(current, current.c.package_id == Package.id)
        .join(
            PackageRevision,
            and_(
                PackageRevision.package_id == Package.id,
                PackageRevision.revision_number == current.c.revision_number,
            ),
        )
        .where(Package.project_id == project_id)
    )


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/packages",
    response_model=PackageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a package and its first revision",
)
def create_package(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.MANAGE_PROJECT))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    body: PackageCreate,
) -> PackageOut:
    """Create a package in this project, in state `CREATED`, with revision 1.

    Two checks, because they answer different questions: `require_project_access` says the caller may
    see this project at all, and `require_action` says creating a package is something their role may
    do. Collapsing them would make "any member of a project may create packages in it" true by
    accident rather than by decision.

    The package, its first revision and the revision's birth event are one transaction. A package with
    no revision would be a package nothing can be uploaded to, and it would look completely normal in
    a list.

    A project that does not exist answers `404`, in the same words as a project the caller is not in.
    """
    if session.scalar(select(Project.id).where(Project.id == project_id)) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    package = Package(project_id=project_id, vendor=body.vendor)
    session.add(package)
    revision = PackageRevision(
        package_id=package.id, revision_number=FIRST_REVISION, state=PackageState.CREATED
    )
    session.add(revision)
    session.add(
        PackageStateEvent(
            package_revision_id=revision.id,
            sequence=FIRST_EVENT_SEQUENCE,
            from_state=None,
            to_state=PackageState.CREATED,
            actor=principal.id,
            reason="package created",
        )
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise

    return PackageOut(
        id=package.id,
        project_id=package.project_id,
        vendor=package.vendor,
        created_at=package.created_at,
        current_revision_id=revision.id,
        current_revision_number=revision.revision_number,
        state=revision.state,
    )


@router.get(
    "/projects/{project_id}/packages",
    response_model=PackagePage,
    summary="List this project's packages, newest first",
)
def list_packages(
    principal: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    cursor: Annotated[
        str | None, Query(description="The `next_cursor` from the previous page.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> PackagePage:
    """One page of this project's packages, newest first.

    Pass the `next_cursor` from the previous page to continue. `next_cursor` is `null` on the last
    page, and that is the only reliable way to know you have reached it — a short page is not one.
    """
    del principal  # the dependencies are the check; the endpoint needs nothing from the caller

    # The argument before the resource: a cursor we did not issue is a malformed request whatever
    # project it names, and checking it first costs no database round trip.
    position = None if cursor is None else decode_cursor(cursor)

    statement = _package_query(project_id)
    if position is not None:
        created_at, package_id = position
        statement = statement.where(
            or_(
                Package.created_at < created_at,
                and_(Package.created_at == created_at, Package.id < package_id),
            )
        )
    statement = statement.order_by(Package.created_at.desc(), Package.id.desc()).limit(limit + 1)

    # One row more than asked for, discarded before the response. It is the only honest way to say
    # "there is a next page": a full page is not evidence of one, and a cursor leading to an empty page
    # makes a client walk an extra round trip to find the end.
    rows = session.execute(statement).all()
    items = [PackageOut.model_validate(dict(row._mapping)) for row in rows[:limit]]
    next_cursor = (
        encode_cursor(items[-1].created_at, items[-1].id) if len(rows) > limit and items else None
    )
    return PackagePage(
        items=items, next_cursor=next_cursor, limit=limit, ordering=ORDERING_DESCRIPTION
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}",
    response_model=PackageOut,
    summary="Read one package",
)
def get_package(
    principal: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> PackageOut:
    """One package, or a `404` that cannot be told apart from the package not existing."""
    del principal

    row = session.execute(_package_query(project_id).where(Package.id == package_id)).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return PackageOut.model_validate(dict(row._mapping))
