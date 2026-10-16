"""After sign-off nothing about a review changes (#1182).

**Why.** A signed-off review is the record GV hands over: its findings, its decisions and the signed
PDF, workbook and redline. Before #1182 the write endpoints only checked that the revision existed,
so after approval a reviewer could still save a value, a wall choice, a pairing or a decision, and
even ask for a check run. The run could never finish (the lifecycle table has no way from
`APPROVED` back to `RUNNING_CHECKS`), so the worker retried it for ever, and the screen showed
inputs that no signed finding had used.

**What.** Every write that changes a revision's inputs or decisions takes one of the dependencies
below, on its route decorator (`dependencies=[Depends(open_revision)]`), so the endpoint's own
signature is unchanged. Each first requires project membership itself, because route dependencies
run before the endpoint's: someone outside the project still gets the same 404 as for a package
that does not exist, never a 409 that would confirm it does. They answer 409 with a plain sentence when the revision is closed: signed off (`APPROVED`),
sent back (`CHANGES_REQUESTED`), replaced (`SUPERSEDED`) or `CANCELLED`. The way to change a closed
review is a new revision. Reads are untouched.

**The lock.** The open check takes a shared row lock on the revision (`FOR SHARE`) in the request's
own transaction. Sign-off and "request changes" lock the same row for update as their very first
step, before they read readiness (`app/review/approval.py:_lock_revision`). So a write that saw an
open review commits before the sign-off reads anything (and readiness then sees the new input: an
input newer than the last check run blocks sign-off), and a write that starts later finds the review
closed. One lock order everywhere, revision first and then the review session, so a sign-off and a
decision at the same moment wait for each other instead of deadlocking. The documents routes lock
the revision for update themselves and so call `ensure_revision_writable` after their own lock
instead (two shared locks upgraded at once would deadlock).

**Reuse.** A new write that changes one revision takes `Depends(open_revision)` on its decorator, or
calls `ensure_revision_writable(session, revision_id)` itself. A project-level value (a project
setting, a sink size) is not one revision's and is not refused here: it can never change a signed
review either, because the signed files print only what the approval froze
(`app/review/signed_exports.py`, #1182).

**Kept honest by a test.** `tests/api/test_signed_off_writes.py` walks every write route under a
package or a review session and fails on any that neither carries one of these dependencies nor is
named in `EXEMPT_WRITES` with its reason, and posts to every guarded one on a signed-off package.
"""

from __future__ import annotations

from typing import Annotated, Final
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.auth import Principal, require_project_access
from app.lifecycle.closed import closed_reason
from app.models import Package, PackageRevision
from app.models.review import ReviewSession

#: Stamped on the dependencies below so the route audit recognises one by fact, not by name (the
#: same reason `app/auth/dependencies.py` stamps its own).
REFUSES_CLOSED_MARKER: Final = "_gv_refuses_closed_revision"

NOT_FOUND_DETAIL: Final = "Not found"


def ensure_revision_writable(session: Session, revision_id: UUID, *, lock: bool = True) -> None:
    """409 with the plain sentence when the revision is closed; nothing otherwise. The one check.

    The state is read again from the database (under a shared lock unless `lock=False`) rather than
    taken from the loaded object, which may be older than the approval.
    """
    statement = select(PackageRevision.state).where(PackageRevision.id == revision_id)
    if lock:
        statement = statement.with_for_update(read=True)
    state = session.execute(statement).scalar_one()
    reason = closed_reason(state)
    if reason is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)


def _latest_revision(session: Session, project_id: UUID, package_id: UUID) -> PackageRevision:
    """This package's newest revision, or 404 in the same words as everywhere else."""
    revision = session.execute(
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return revision


def open_revision(
    _member: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> PackageRevision:
    """The package's current revision, refused with 409 once it is closed (locked; see above)."""
    revision = _latest_revision(session, project_id, package_id)
    ensure_revision_writable(session, revision.id)
    return revision


def open_revision_unlocked(
    _member: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> PackageRevision:
    """The same refusal without the lock, for a request that runs for a long time.

    Only for a route that records nothing a check reads: asking the AI to propose values writes
    proposals, which count only once a reviewer saves them through a locked route. Holding a lock for
    a model call would stall the worker's state changes for as long as the model takes.
    """
    revision = _latest_revision(session, project_id, package_id)
    ensure_revision_writable(session, revision.id, lock=False)
    return revision


def open_session_revision(
    _member: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    review_session_id: UUID,
) -> PackageRevision:
    """The revision a review session belongs to, refused with 409 once it is closed."""
    revision = session.execute(
        select(PackageRevision)
        .join(ReviewSession, ReviewSession.package_revision_id == PackageRevision.id)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(ReviewSession.id == review_session_id, Package.project_id == project_id)
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    ensure_revision_writable(session, revision.id)
    return revision


for _dependency in (open_revision, open_revision_unlocked, open_session_revision):
    setattr(_dependency, REFUSES_CLOSED_MARKER, True)

#: Write routes under a package or a review session that deliberately take no open-revision
#: dependency, each with its reason. A route here must still not change what a signed review says.
EXEMPT_WRITES: Final[dict[tuple[str, str], str]] = {
    (
        "POST",
        "/api/v1/projects/{project_id}/packages/{package_id}/revisions",
    ): "starting a new revision is the way to change a closed review (#1180)",
    (
        "POST",
        "/api/v1/projects/{project_id}/review-sessions/{review_session_id}/approve",
    ): "approval is the sign-off itself; it requires AWAITING_REVIEW already",
    (
        "POST",
        "/api/v1/projects/{project_id}/packages/{package_id}/signed-exports",
    ): "publishes the frozen record of a signed-off review; it requires APPROVED",
    (
        "POST",
        "/api/v1/projects/{project_id}/review-sessions/{review_session_id}/complete",
    ): "ends a sitting; records no input or decision",
    (
        "POST",
        "/api/v1/projects/{project_id}/packages/{package_id}/documents",
    ): "assembly only; refuses a closed revision after its own update lock (ensure_revision_writable)",
    (
        "POST",
        "/api/v1/projects/{project_id}/packages/{package_id}/extract",
    ): "assembly only; refuses a closed revision after its own update lock (ensure_revision_writable)",
    (
        "POST",
        "/api/v1/projects/{project_id}/packages/{package_id}/pages/pictures",
    ): "renders page pictures from immutable drawings; a signed export may need them",
    (
        "POST",
        "/api/v1/projects/{project_id}/packages/{package_id}/parts/page-pictures",
    ): "cuts part pictures from immutable drawings; records no input or decision",
    (
        "POST",
        "/api/v1/projects/{project_id}/packages/{package_id}/chat",
    ): "answers a question from the records; writes no input or decision",
    (
        "POST",
        "/api/v1/projects/{project_id}/packages/{package_id}/chat/stream",
    ): "answers a question from the records; writes no input or decision",
    (
        "POST",
        "/api/v1/projects/{project_id}/packages/{package_id}/assistant/stream",
    ): "answers a question from the records; writes no input or decision",
    (
        "POST",
        "/api/v1/packages/{package_id}/chats",
    ): "starts a saved assistant chat (#1463); the chat reads the drawings and writes no input or "
    "decision on the review",
}
