"""Accepting long work without doing it: enqueue, hand back a handle, and answer honestly (#208, C2.6).

Backend §4.1, via `docs/DESIGN_PLATFORM.md` §4.2: anything CPU-heavy is a background task. On one 8 GB
VM, rendering inside a request competes with PostgreSQL and OCR for the same memory, so the endpoint
that *accepts* a drawing must not be the code that reads it. `tests/api/test_no_heavy_work.py` is what
keeps that true as the API grows — it walks every module under `app/api/` and fails if any of them can
reach OCR, rendering or extraction, transitively.

This module is the other half: the sanctioned way to accept such work.

**The handle names the work, not a run.** `enqueue_and_respond` returns `accepted_work_id` — the
outbox entry's id. It is deliberately not called a workflow run id, because at this moment no run
exists: `workflow.outbox.enqueue` writes a row and starts nothing, and `WorkflowRun.engine_run_id` is
assigned by the engine when a dispatcher picks the row up, after the caller's transaction commits.
Returning a run id here would mean inventing one that will not match the run that eventually happens,
and a client correlating logs by it would find nothing.

**`started` does not mean finished, and the response says so.** The outbox knows one thing: whether
the workflow was handed to the engine. It knows nothing about whether the work succeeded. A status
field that blurred those would be the "fake completion" this story exists to avoid — a caller polling
until `started` and then reporting the drawing as reviewed. Every state carries its own sentence.

**Scope is enforced, not assumed.** The polling route sits under `/projects/{project_id}/…` with
`require_project_access`, like every other project route. `OutboxEntry` has no `project_id` column, so
the boundary is applied by matching `payload->>'project_id'`, which is what the existing caller in
`app/api/documents.py` already writes. That makes `project_id` in the payload a *requirement* for a
pollable handle, and `enqueue_and_respond` refuses a payload without one rather than returning a URL
guaranteed to 404. The alternative — an unscoped status route — would let any authenticated caller
poll any project's work, which ADR-0006 and §4.3 rule out.

Source: backend proposal §4.1, §9.2 · Design: `docs/DESIGN_PLATFORM.md` §4.2, §6.1 ·
Verification: `tests/api/test_no_heavy_work.py`
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.auth import Principal, require_project_access
from app.models import OutboxEntry
from workflow.outbox import enqueue

router = APIRouter(tags=["operations"])

__all__ = [
    "ACCEPTED_WORK_PATH",
    "STATUS_DESCRIPTIONS",
    "AcceptedOut",
    "PayloadMissingProject",
    "WorkStatus",
    "enqueue_and_respond",
    "router",
    "scoped_query",
    "status_url_for",
]

#: The polling route, as one template. The route decorator and `status_url_for` both read it, so the
#: URL handed to a client cannot drift from the URL the app actually serves — a test asserts they agree.
ACCEPTED_WORK_PATH: Final = "/projects/{project_id}/accepted-work/{accepted_work_id}"

#: The key the boundary is enforced on. Named once so the writer, the reader and the refusal agree.
PROJECT_KEY: Final = "project_id"

#: What a refusal says. Nothing about the project or the reason — a message that explained itself
#: would hand back exactly what the 404 is chosen to hide (`DESIGN_PLATFORM.md` §4.3).
NOT_FOUND_DETAIL: Final = "Not found"


class WorkStatus:
    """The three things the outbox can honestly say about a row.

    Not a `StrEnum` in `vocabulary/`: these are not a domain vocabulary anything else names or
    persists — they are this endpoint's reading of two columns, and putting them in the shared
    vocabulary would imply a stored value somewhere that agrees with them.
    """

    QUEUED: Final = "queued"
    RETRYING: Final = "retrying"
    STARTED: Final = "started"


#: One sentence per state, returned with the status. **This is the acceptance criterion about honesty.**
#: `started` is the one that matters: it means the workflow engine accepted the start, and says nothing
#: at all about whether the work is done. A client that read it as completion would report a drawing as
#: reviewed on the strength of the request having been accepted.
STATUS_DESCRIPTIONS: Final[Mapping[str, str]] = {
    WorkStatus.QUEUED: (
        "Recorded and waiting. Nothing has started yet — the work is committed to happen, and a "
        "dispatcher has not picked it up."
    ),
    WorkStatus.RETRYING: (
        "A hand-off to the workflow engine was attempted and has not succeeded yet. It will be "
        "tried again; the work has still not started."
    ),
    WorkStatus.STARTED: (
        "Handed to the workflow engine. This says the work was started, and nothing about whether "
        "it finished or succeeded — ask the package or the findings for that."
    ),
}


class PayloadMissingProject(ValueError):
    """The payload names no project, so no handle could be scoped to one.

    Raised rather than returning an unscoped URL. A status route that any authenticated caller could
    read would leak the existence of another project's work, and a handle that is simply never
    pollable would look like a working API returning 404 for ever.
    """


class AcceptedOut(BaseModel):
    """What an endpoint returns when it has accepted work rather than done it.

    Carries no result and no estimate. There is nothing truthful to say about either at this point,
    and a hopeful one would be read as a promise.
    """

    accepted_work_id: UUID
    """The outbox entry — the work that was accepted.

    **Not a workflow run id.** No run exists yet; see the module docstring. Named for what it is so a
    client does not correlate it against engine logs that will never mention it.
    """

    status: str = Field(default=WorkStatus.QUEUED)
    what_it_means: str = Field(default=STATUS_DESCRIPTIONS[WorkStatus.QUEUED])
    """The status in plain English, because `started` is the one a client will misread."""

    status_url: str
    """Where to poll. A path rather than an absolute URL: the host a client should use is decided by
    whatever is in front of this service, and guessing it from request headers is how a link ends up
    pointing at an internal address."""


def status_url_for(project_id: UUID | str, accepted_work_id: UUID | str) -> str:
    """The polling path for one piece of accepted work, built from the route's own template."""
    return ACCEPTED_WORK_PATH.format(project_id=project_id, accepted_work_id=accepted_work_id)


def enqueue_and_respond(
    session: Session, *, workflow: str, payload: Mapping[str, object], prefix: str = ""
) -> AcceptedOut:
    """Record durable work in the caller's transaction and describe it. Starts nothing, commits nothing.

    Call this beside the business write, inside the same transaction, exactly as `enqueue` is called —
    the point of the outbox is that the change and the intent commit together, and this adds no
    boundary of its own. The caller still owns the commit.

    `prefix` is the mount prefix of the polling route (`API_PREFIX` in the application factory), so the
    returned path is the one a client can actually request rather than the router-relative one.

    Raises `PayloadMissingProject` if the payload names no project. That is a programming error rather
    than a client error — the endpoint author chose the payload — so it is not an `HTTPException`.
    """
    # `str(...)` rather than the raw value: a payload is `Mapping[str, object]`, and the caller in
    # `documents.py` already stores the project as a string because JSONB has no UUID type. Formatting
    # whatever arrived would put `UUID('…')` in the URL if somebody passed the object.
    raw_project = payload.get(PROJECT_KEY)
    project = "" if raw_project is None else str(raw_project).strip()
    if not project:
        raise PayloadMissingProject(
            f"the payload for workflow {workflow!r} has no {PROJECT_KEY!r}, so the handle could not "
            "be scoped to a project. The polling route applies project scope by matching this key, "
            "and an unscoped one would let any authenticated caller read another project's work. "
            f"Add {PROJECT_KEY!r} to the payload."
        )

    accepted_work_id = enqueue(session, workflow=workflow, payload=payload)
    return AcceptedOut(
        accepted_work_id=accepted_work_id,
        status=WorkStatus.QUEUED,
        what_it_means=STATUS_DESCRIPTIONS[WorkStatus.QUEUED],
        status_url=prefix + status_url_for(project, accepted_work_id),
    )


def scoped_query(project_id: UUID, accepted_work_id: UUID) -> Select[tuple[OutboxEntry]]:
    """One row, and only if it belongs to this project.

    Both clauses matter, and they answer different questions. The `id` pins the resource; the payload
    match is the isolation boundary. Leaving the second to `require_project_access` would mean any
    work id reached the database with nothing but a membership claim between them — the dependency
    establishes that the caller may see *this project*, not that *this row* is this project's.

    A separate function so its SQL can be asserted without a database
    (`tests/api/test_no_heavy_work.py`): the boundary is a `->>` comparison, and a mistyped key would
    match nothing while looking exactly right.
    """
    return select(OutboxEntry).where(
        OutboxEntry.id == accepted_work_id,
        OutboxEntry.payload[PROJECT_KEY].astext == str(project_id),
    )


def _status_of(entry: OutboxEntry) -> str:
    """Read two columns as one honest word.

    `dispatched_at` is stamped only after the engine accepted the start, so it is the only evidence
    that anything began. `attempts` above zero without it means a hand-off was tried and did not take
    — worth saying, because it is the difference between "waiting its turn" and "wedged".
    """
    if entry.dispatched_at is not None:
        return WorkStatus.STARTED
    return WorkStatus.RETRYING if entry.attempts > 0 else WorkStatus.QUEUED


@router.get(
    ACCEPTED_WORK_PATH,
    response_model=AcceptedOut,
    summary="What happened to work this API accepted",
)
def read_accepted_work(
    principal: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    accepted_work_id: UUID,
) -> AcceptedOut:
    """Whether accepted work has started, in plain English.

    **`started` is not `finished`.** This reports what the outbox knows — that the workflow was handed
    to the engine — and nothing about the outcome. For the result, read the package or its findings.

    Work belonging to another project is reported as not found, in the same words as work that does
    not exist. A 403 would confirm it exists, which is what the boundary is for.
    """
    del principal  # the dependency is the check; nothing here needs the caller's identity

    entry = session.execute(scoped_query(project_id, accepted_work_id)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    state = _status_of(entry)
    return AcceptedOut(
        accepted_work_id=entry.id,
        status=state,
        what_it_means=STATUS_DESCRIPTIONS[state],
        status_url=status_url_for(project_id, entry.id),
    )
