"""Revisions of a review: start the next one, list them, and say what changed (#1180).

Vendors always resubmit. Before this, a revised shop drawing meant a brand-new review, and the link
to revision 1 — its decisions, its answers, its signed record — was lost. The backend could already
supersede a revision (`app/lifecycle/supersede.py`, ADR-0018); nothing called it.

**Starting a revision reuses the upload flow, it does not replace it.** `POST .../revisions` opens
revision N+1 holding every drawing of revision N at the version it had, closes revision N
(`SUPERSEDED`, read-only, kept as it was), and hands back upload tickets for the re-issued drawings
— each a new version of the *same* drawing. The client then does exactly what it does for a first
upload: PUT the bytes to the ticket, `POST /documents/{id}/confirm` (which puts the new version into
the revision being assembled, replacing the carried one), then `POST .../extract`.

**Same auth as uploading.** `require_project_access` and the upload routes' own action
(`Action.MANAGE_PROJECT`, as `register_document`), so whoever may upload drawings may start a
revision and nobody else. Another project's package answers `404` in the same words as a missing one.

**Read-only means read-only.** Every package-scoped write resolves the newest revision, so none can
reach an older one; a review sitting opened on the older revision is refused by
`app/review/session.py` (`RevisionSuperseded`, a plain 409).

**"What changed"** compares two revisions page by page by each page's content hash, and countertop
row by countertop row by the stored result of each revision's last check run — the same projection
the Results screen and the signed PDF read, never recomputed.

Source: issue #1180 · Design: ADR-0018 · Verification: `tests/api/test_revisions.py`
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Annotated, Final
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.api.documents import _package_revision, _ticket
from app.api.visual_countertops import _countertop_results_for_revision
from app.auth import Action, Principal, require_action, require_project_access
from app.db.base import utc_now
from app.lifecycle.carry_forward import carry_run_values, known_content
from app.lifecycle.states import ASSEMBLY_STATES, PROCESSING_STATES, TERMINAL_STATES
from app.lifecycle.supersede import start_revision
from app.models import (
    Approval,
    Document,
    DocumentKind,
    DocumentVersion,
    OutboxEntry,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Page,
)
from app.schemas.revisions import (
    CountertopChangeOut,
    CountertopSideOut,
    DrawingChangeOut,
    PageChangeOut,
    RevisionComparisonOut,
    RevisionDrawing,
    RevisionOut,
    RevisionsOut,
    RevisionStart,
    RevisionStarted,
    RevisionUpload,
)
from app.schemas.visual_ui import CountertopResultOut
from storage.store import ArtifactStore
from workflow.slot_row_scope import slot_rows

router = APIRouter(tags=["revisions"])

NOT_FOUND_DETAIL: Final = "Not found"

#: The action that may start a revision: the one that may upload drawings (`register_document`).
UPLOAD_ACTION: Final = Action.START_REVIEW

#: Plain refusals, one per reason a revision cannot start now.
STILL_ASSEMBLING: Final = (
    "This revision's drawings are still being uploaded. Upload them and start reading them first; "
    "a new revision replaces a review that has been read."
)
BEING_READ: Final = (
    "The drawings are being read or checked right now. Wait until that finishes, then start a new "
    "revision."
)
CLOSED: Final = "This review was closed, so it cannot have a new revision."
SAME_SHOP_FILE: Final = (
    "This is the same shop drawing file as revision {number}. A new revision needs the vendor's "
    "re-issued drawing; nothing would change."
)
ARCHITECT_ONLY: Final = (
    "Only the architect's drawings changed. A new revision is for the vendor's re-issued shop "
    "drawing; changing just the architect's drawings is not supported yet. Start a new review if "
    "the architect's drawings must be checked again."
)
JUST_STARTED: Final = (
    "A new revision was just started by someone else: the newest is now revision {number}. Reload "
    "the review to see it."
)
CHECKS_RUNNING: Final = (
    "Checks are running on revision {number}; wait for them to finish, then start the new revision."
)
#: The workflow `POST .../measurements` queues to run the checks (`app/api/measurements.py`).
RUN_CHECKS_WORKFLOW: Final = "run_checks"
ONE_FILE_TWICE: Final = (
    "The same file would be both the architect's and the vendor's drawings in this revision. A file "
    "that holds both is one combined set: give it once, as the shop drawing."
)
SEVERAL_OF_A_KIND: Final = (
    "This review holds more than one {kind} drawing, so the new file cannot be matched to the one it "
    "replaces. Start a new review for it instead."
)
READ_ONLY_NOTE: Final = (
    "Upload the re-issued drawing to the ticket, confirm it, then start reading. Revision {previous} "
    "is now read-only and stays visible as it was."
)
RESUME_NOTE: Final = (
    "Revision {number} was started but not read yet: upload the re-issued drawing to the ticket, "
    "confirm it, then start reading."
)

#: The states a started revision is still waiting for its re-issued drawing in.
WAITING_FOR_UPLOAD: Final = frozenset({PackageState.CREATED, PackageState.UPLOADING})


# --- resolving ------------------------------------------------------------------------------------


def _package(session: Session, project_id: UUID, package_id: UUID) -> Package:
    package = session.scalar(
        select(Package).where(Package.id == package_id, Package.project_id == project_id)
    )
    if package is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return package


def _revisions(session: Session, package_id: UUID) -> list[PackageRevision]:
    return list(
        session.scalars(
            select(PackageRevision)
            .where(PackageRevision.package_id == package_id)
            .order_by(PackageRevision.revision_number)
        )
    )


def _members(
    session: Session, revision_ids: list[UUID]
) -> dict[UUID, list[tuple[Document, DocumentVersion]]]:
    """Each revision's drawings, with the version it holds."""
    found: dict[UUID, list[tuple[Document, DocumentVersion]]] = defaultdict(list)
    if not revision_ids:
        return found
    for revision_id, document, version in session.execute(
        select(PackageRevisionDocument.package_revision_id, Document, DocumentVersion)
        .join(Document, Document.id == PackageRevisionDocument.document_id)
        .join(DocumentVersion, DocumentVersion.id == PackageRevisionDocument.document_version_id)
        .where(PackageRevisionDocument.package_revision_id.in_(revision_ids))
        .order_by(Document.kind, Document.created_at, Document.id)
    ):
        found[revision_id].append((document, version))
    return found


def _revision_out(
    revision: PackageRevision,
    *,
    revisions: list[PackageRevision],
    members: dict[UUID, list[tuple[Document, DocumentVersion]]],
    approvals: dict[UUID, Approval],
) -> RevisionOut:
    numbers = {item.id: item.revision_number for item in revisions}
    newest = max(item.revision_number for item in revisions)
    successor = next((item for item in revisions if item.supersedes_id == revision.id), None)
    # The earliest revision before this one holding the same version of a drawing: "same file as".
    first_holder: dict[UUID, int] = {}
    for item in revisions:
        for _document, version in members.get(item.id, ()):
            first_holder.setdefault(version.id, item.revision_number)
    approval = approvals.get(revision.id)
    return RevisionOut(
        revision_id=revision.id,
        revision_number=revision.revision_number,
        state=PackageState(revision.state),
        created_at=revision.created_at,
        current=revision.revision_number == newest,
        read_only=revision.revision_number != newest,
        supersedes_revision_number=(
            None if revision.supersedes_id is None else numbers.get(revision.supersedes_id)
        ),
        superseded_by_revision_number=None if successor is None else successor.revision_number,
        signed_off_at=None if approval is None else approval.created_at,
        signed_off_by=None if approval is None else approval.approved_by,
        drawings=tuple(
            RevisionDrawing(
                kind=document.kind,
                document_id=document.id,
                document_version_id=version.id,
                sha256=version.sha256,
                file_name=version.file_name,
                page_count=version.page_count,
                same_as_revision=(
                    first_holder[version.id]
                    if first_holder.get(version.id, revision.revision_number)
                    < revision.revision_number
                    else None
                ),
            )
            for document, version in members.get(revision.id, ())
        ),
    )


def _approvals(session: Session, revision_ids: list[UUID]) -> dict[UUID, Approval]:
    """The newest approval of each revision."""
    latest: dict[UUID, Approval] = {}
    if not revision_ids:
        return latest
    for approval in session.scalars(
        select(Approval)
        .where(Approval.package_revision_id.in_(revision_ids))
        .order_by(Approval.created_at.desc(), Approval.id.desc())
    ):
        latest.setdefault(approval.package_revision_id, approval)
    return latest


def _all_revisions_out(session: Session, package_id: UUID) -> tuple[RevisionOut, ...]:
    revisions = _revisions(session, package_id)
    ids = [revision.id for revision in revisions]
    members = _members(session, ids)
    approvals = _approvals(session, ids)
    return tuple(
        _revision_out(revision, revisions=revisions, members=members, approvals=approvals)
        for revision in revisions
    )


def _checks_queued(session: Session, revision_id: UUID) -> bool:
    """Whether a check run for this revision is queued or running: its outbox row is undelivered.

    The local worker stamps `dispatched_at` only once the run has finished, so an undelivered row
    is a run that has not finished yet.
    """
    return (
        session.scalar(
            select(OutboxEntry.id)
            .where(
                OutboxEntry.workflow == RUN_CHECKS_WORKFLOW,
                OutboxEntry.dispatched_at.is_(None),
                OutboxEntry.payload["package_revision_id"].astext == str(revision_id),
            )
            .limit(1)
        )
        is not None
    )


def _refusal(state: PackageState) -> str | None:
    """Why a revision cannot start from this state now, or `None` when it can."""
    if state is PackageState.UPLOADED:
        return BEING_READ
    if state in ASSEMBLY_STATES:
        return STILL_ASSEMBLING
    if state in PROCESSING_STATES or state is PackageState.FAILED_RETRYABLE:
        return BEING_READ
    if state in TERMINAL_STATES:
        return CLOSED
    return None


# --- the routes -----------------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/packages/{package_id}/revisions",
    response_model=RevisionStarted,
    status_code=status.HTTP_201_CREATED,
    summary="Start the next revision of a review with the vendor's re-issued drawing",
)
def start_next_revision(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(UPLOAD_ACTION))],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
    body: RevisionStart,
) -> RevisionStarted:
    """Open revision N+1 and get upload tickets for the re-issued drawing(s).

    Every drawing of revision N is carried into N+1 at the version it had; a drawing you send
    replaces its carried version once confirmed. Revision N becomes read-only. Then: PUT each
    `upload` ticket, confirm it at `/documents/{document_id}/confirm`, and `POST .../extract`.

    `409` while the current revision is still being uploaded, read or checked, or is closed; when one
    file would be both drawings; or when the review holds several drawings of one kind. `422` when
    the shop file is the same bytes as the current one.
    """
    current = _package_revision(session, project_id, package_id, lock=True)
    state = PackageState(current.state)
    if body.expected_revision != current.revision_number or state is PackageState.SUPERSEDED:
        # Someone else's start landed first (or this screen is behind): never start a second one.
        newest = max(
            current.revision_number,
            session.scalar(
                select(PackageRevision.revision_number)
                .where(PackageRevision.package_id == package_id)
                .order_by(PackageRevision.revision_number.desc())
                .limit(1)
            )
            or 0,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=JUST_STARTED.format(number=newest)
        )
    # **A start that was never finished is resumed, not refused** (#1180 review): the newest
    # revision was opened by a start, still waits for its drawing and was never read. Its tickets
    # are handed out again; nothing new is opened and nothing more is closed.
    resuming = current.supersedes_id is not None and state in WAITING_FOR_UPLOAD
    if resuming:
        base = session.get(PackageRevision, current.supersedes_id)
        assert base is not None
    else:
        refusal = _refusal(state)
        if refusal is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=refusal)
        if _checks_queued(session, current.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=CHECKS_RUNNING.format(number=current.revision_number),
            )
        base = current

    held: dict[str, list[tuple[Document, DocumentVersion]]] = defaultdict(list)
    for document, version in _members(session, [current.id]).get(current.id, ()):
        held[document.kind].append((document, version))
    before: dict[str, list[tuple[Document, DocumentVersion]]] = defaultdict(list)
    for document, version in _members(session, [base.id]).get(base.id, ()):
        before[document.kind].append((document, version))
    offered: dict[str, str] = {DocumentKind.SHOP.value: body.shop.sha256}
    if body.architectural is not None:
        offered[DocumentKind.ARCHITECTURAL.value] = body.architectural.sha256
    for kind in offered:
        if len(held.get(kind, ())) > 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=SEVERAL_OF_A_KIND.format(
                    kind="architect's" if kind == DocumentKind.ARCHITECTURAL.value else "shop"
                ),
            )
    shop_before = before.get(DocumentKind.SHOP.value)
    if shop_before and shop_before[0][1].sha256 == body.shop.sha256:
        arch_before = before.get(DocumentKind.ARCHITECTURAL.value)
        architect_changed = body.architectural is not None and (
            not arch_before or arch_before[0][1].sha256 != body.architectural.sha256
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                ARCHITECT_ONLY
                if architect_changed
                else SAME_SHOP_FILE.format(number=base.revision_number)
            ),
        )
    # The revision's drawings once the offered files are in: no file may be two of them (#963).
    final: dict[UUID | str, str] = {
        document.id: version.sha256
        for kind, pairs in held.items()
        if kind not in offered
        for document, version in pairs
    }
    final.update(offered)
    if len(set(final.values())) != len(final):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ONE_FILE_TWICE)

    if resuming:
        revision = current
    else:
        try:
            revision = start_revision(
                session, package_id=package_id, actor=principal.id, reason=body.note or None
            )
        except IntegrityError as error:
            # Two starts racing past the lock: the database's unique revision number decides.
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=JUST_STARTED.format(number=current.revision_number + 1),
            ) from error

    uploads: list[RevisionUpload] = []
    for kind, sha in offered.items():
        pairs = held.get(kind)
        if pairs:
            document, carried = pairs[0]
        else:
            # A drawing the review did not hold yet: a new logical drawing of this package.
            document = Document(package_id=package_id, kind=kind)
            session.add(document)
            session.flush()
            carried = None
        if carried is not None and carried.sha256 == sha:
            uploads.append(RevisionUpload(kind=kind, document_id=document.id, status="unchanged"))  # type: ignore[arg-type]
            continue
        stored = session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id, DocumentVersion.sha256 == sha
            )
        )
        if stored is not None:
            # These exact bytes were confirmed for this drawing before: the revision uses that
            # version directly, with nothing to send and no second copy (ADR-0018).
            membership = pg_insert(PackageRevisionDocument).values(
                id=uuid4(),
                created_at=utc_now(),
                package_revision_id=revision.id,
                package_id=package_id,
                document_id=document.id,
                document_version_id=stored.id,
            )
            session.execute(
                membership.on_conflict_do_update(
                    constraint="uq_revision_documents_one_version_per_document",
                    set_={"document_version_id": stored.id},
                )
            )
            uploads.append(
                RevisionUpload(kind=kind, document_id=document.id, status="already_stored")  # type: ignore[arg-type]
            )
            continue
        uploads.append(
            RevisionUpload(
                kind=kind,  # type: ignore[arg-type]
                document_id=document.id,
                status="upload",
                ticket=_ticket(store, document.id, sha),
            )
        )

    carried_values = carry_run_values(
        session,
        project_id=project_id,
        prior_revision_id=base.id,
        new_revision_id=revision.id,
    )
    session.flush()
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise

    items = _all_revisions_out(session, package_id)
    return RevisionStarted(
        revision=next(item for item in items if item.revision_id == revision.id),
        uploads=tuple(uploads),
        carried_values=carried_values,
        resumed=resuming,
        next_step=(
            RESUME_NOTE.format(number=revision.revision_number)
            if resuming
            else READ_ONLY_NOTE.format(previous=current.revision_number)
        ),
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/revisions",
    response_model=RevisionsOut,
    summary="Every revision of a review, oldest first",
)
def list_revisions(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.READ_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> RevisionsOut:
    """Each revision's number, state, when it was started, whether it was signed off and by whom,
    and the drawings it holds ("same file as revision N" when one was carried forward)."""
    _package(session, project_id, package_id)
    items = _all_revisions_out(session, package_id)
    if not items:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    newest = items[-1]
    unfinished = (
        newest.revision_number
        if newest.supersedes_revision_number is not None and newest.state in WAITING_FOR_UPLOAD
        else None
    )
    return RevisionsOut(
        package_id=package_id,
        current_revision_number=newest.revision_number,
        can_start_revision=principal.may(UPLOAD_ACTION),
        unfinished_revision=unfinished,
        items=items,
    )


@dataclass(frozen=True, slots=True)
class _PageFacts:
    index: int
    content_hash: str
    sheet_number: str | None


def _pages(session: Session, version_ids: set[UUID]) -> dict[UUID, list[_PageFacts]]:
    found: dict[UUID, list[_PageFacts]] = defaultdict(list)
    if not version_ids:
        return found
    for version_id, index, content_hash, sheet in session.execute(
        select(Page.document_version_id, Page.index, Page.content_hash, Page.sheet_number).where(
            Page.document_version_id.in_(version_ids)
        )
    ):
        found[version_id].append(_PageFacts(index, content_hash, sheet))
    for pages in found.values():
        pages.sort(key=lambda page: page.index)
    return found


def pair_pages(
    old: list[_PageFacts], new: list[_PageFacts]
) -> list[tuple[_PageFacts | None, _PageFacts | None, str]]:
    """Pair two issues of one drawing's pages: `(earlier, later, change)`.

    First by content (the same page bytes, wherever they moved: `unchanged` or `moved`), then by
    printed sheet number (`changed`), then by position (`changed`). What is left over was `added`
    or `removed`. A page whose content is unknown is never paired by content (#1180 review).
    """
    pairs: list[tuple[_PageFacts | None, _PageFacts | None, str]] = []
    left = list(old)
    right = list(new)
    for page in list(right):
        if not known_content(page.content_hash):
            continue
        same = [earlier for earlier in left if earlier.content_hash == page.content_hash]
        if not same:
            continue
        earlier = min(same, key=lambda candidate: abs(candidate.index - page.index))
        left.remove(earlier)
        right.remove(page)
        pairs.append((earlier, page, "unchanged" if earlier.index == page.index else "moved"))
    for page in list(right):
        if not page.sheet_number:
            continue
        same_sheet = [earlier for earlier in left if earlier.sheet_number == page.sheet_number]
        if len(same_sheet) != 1:
            continue
        left.remove(same_sheet[0])
        right.remove(page)
        pairs.append((same_sheet[0], page, "changed"))
    for page in list(right):
        same_place = [earlier for earlier in left if earlier.index == page.index]
        if not same_place:
            continue
        left.remove(same_place[0])
        right.remove(page)
        pairs.append((same_place[0], page, "changed"))
    pairs.extend((None, page, "added") for page in right)
    pairs.extend((page, None, "removed") for page in left)
    return sorted(
        pairs,
        key=lambda pair: (pair[1] or pair[0]).index if (pair[1] or pair[0]) else 0,  # type: ignore[union-attr]
    )


def _drawing_changes(
    session: Session,
    before: list[tuple[Document, DocumentVersion]],
    after: list[tuple[Document, DocumentVersion]],
) -> tuple[DrawingChangeOut, ...]:
    earlier = {document.id: (document, version) for document, version in before}
    later = {document.id: (document, version) for document, version in after}
    pages = _pages(session, {version.id for _, version in (*earlier.values(), *later.values())})
    order = [*later, *(document_id for document_id in earlier if document_id not in later)]
    changes: list[DrawingChangeOut] = []
    for document_id in order:
        document = (later.get(document_id) or earlier[document_id])[0]
        old = earlier.get(document_id, (None, None))[1]
        new = later.get(document_id, (None, None))[1]
        shown = new or old
        name = None if shown is None else shown.file_name
        same = old is not None and new is not None and old.sha256 == new.sha256
        old_pages = pages.get(old.id, []) if old is not None else []
        new_pages = pages.get(new.id, []) if new is not None else []
        unread = (old is not None and not old_pages) or (new is not None and not new_pages)
        if unread and not same:
            changes.append(
                DrawingChangeOut(
                    kind=document.kind,
                    document_id=document.id,
                    file_name=name,
                    same_file=False,
                    compared=False,
                    reason="Not read yet, so its pages cannot be compared.",
                    pages=(),
                )
            )
            continue
        listed: list[PageChangeOut] = []
        paired = (
            [(page, page, "unchanged") for page in new_pages]
            if same
            else pair_pages(old_pages, new_pages)
        )
        for before_page, after_page, change in paired:
            shown_page = after_page or before_page
            assert shown_page is not None
            listed.append(
                PageChangeOut(
                    kind=document.kind,
                    document_id=document.id,
                    file_name=name,
                    page_number=shown_page.index + 1,
                    before_page_number=(
                        None if before_page is None or after_page is None else before_page.index + 1
                    ),
                    change=change,  # type: ignore[arg-type]
                )
            )
        changes.append(
            DrawingChangeOut(
                kind=document.kind,
                document_id=document.id,
                file_name=name,
                same_file=same,
                compared=True,
                pages=tuple(listed),
            )
        )
    return tuple(changes)


def _side(item: CountertopResultOut | None) -> CountertopSideOut | None:
    if item is None:
        return None
    return CountertopSideOut(
        outcome=item.outcome,
        needs_decision=item.needs_decision,
        printed_overall=item.printed_overall,
        expected_total=item.expected_total,
        delta=item.delta,
    )


def _same_side(left: CountertopSideOut, right: CountertopSideOut) -> bool:
    def text(value: object) -> str | None:
        return None if value is None else getattr(value, "display", None)

    return (
        left.outcome == right.outcome
        and text(left.printed_overall) == text(right.printed_overall)
        and text(left.expected_total) == text(right.expected_total)
        and text(left.delta) == text(right.delta)
    )


def _row_keys(session: Session, revision_id: UUID) -> dict[UUID, tuple[str, str]]:
    """`{row id: (page content hash, row rank)}` for a revision's countertop rows on pages whose
    content is known: the carry's own row identity, without the values that may have changed."""
    rows = slot_rows(session, revision_id)
    if not rows:
        return {}
    hashes = {
        page_id: content_hash
        for page_id, content_hash in session.execute(
            select(Page.id, Page.content_hash).where(
                Page.id.in_({row.anchor.page_id for row in rows})
            )
        )
    }
    keys: dict[UUID, tuple[str, str]] = {}
    for row in rows:
        rank = next(
            (
                flag.removeprefix("row-rank:")
                for flag in row.anchor.ambiguity_flags or ()
                if flag.startswith("row-rank:")
            ),
            None,
        )
        content = hashes.get(row.anchor.page_id)
        if rank is not None and known_content(content):
            assert content is not None
            keys[row.anchor.id] = (content, rank)
    return keys


def _countertop_changes(
    before: tuple[CountertopResultOut, ...],
    after: tuple[CountertopResultOut, ...],
    before_keys: dict[UUID, tuple[str, str]],
    after_keys: dict[UUID, tuple[str, str]],
) -> tuple[CountertopChangeOut, ...]:
    """Pair rows by the row on the same page bytes and rank first, then by page and label."""
    remaining = list(before)
    by_identity: dict[tuple[str, str], list[CountertopResultOut]] = defaultdict(list)
    for item in remaining:
        if item.row_id in before_keys:
            by_identity[before_keys[item.row_id]].append(item)
    pairs: list[tuple[CountertopResultOut | None, CountertopResultOut | None]] = []
    unpaired: list[CountertopResultOut] = []
    for item in after:
        key = after_keys.get(item.row_id)
        candidates = [c for c in by_identity.get(key, []) if c in remaining] if key else []
        if len(candidates) == 1:
            remaining.remove(candidates[0])
            pairs.append((candidates[0], item))
        else:
            unpaired.append(item)
    for item in unpaired:
        match = next(
            (
                earlier
                for earlier in remaining
                if (earlier.page_number, earlier.label) == (item.page_number, item.label)
            ),
            None,
        )
        if match is not None:
            remaining.remove(match)
        pairs.append((match, item))
    pairs.extend((item, None) for item in remaining)

    changes: list[CountertopChangeOut] = []
    for old_item, new_item in pairs:
        shown = new_item or old_item
        assert shown is not None
        old, new = _side(old_item), _side(new_item)
        if old is None:
            change = "new"
        elif new is None:
            change = "gone"
        elif _same_side(old, new):
            change = "same"
        else:
            change = "changed"
        changes.append(
            CountertopChangeOut(
                page_number=shown.page_number,
                label=shown.label,
                change=change,  # type: ignore[arg-type]
                before=old,
                after=new,
            )
        )
    return tuple(sorted(changes, key=lambda change: (change.page_number, change.label)))


@router.get(
    "/projects/{project_id}/packages/{package_id}/revisions/compare",
    response_model=RevisionComparisonOut,
    summary="What changed between two revisions of a review",
)
def compare_revisions(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.READ_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    before: Annotated[
        int | None, Query(ge=1, description="The earlier revision; default the one before `after`.")
    ] = None,
    after: Annotated[
        int | None, Query(ge=1, description="The later revision; default the newest.")
    ] = None,
) -> RevisionComparisonOut:
    """Pages added, removed and changed (by each page's content hash), and every countertop row's
    stored result before and after. Read only; nothing is recomputed."""
    del principal
    _package(session, project_id, package_id)
    revisions = {revision.revision_number: revision for revision in _revisions(session, package_id)}
    if not revisions:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    later_number = after if after is not None else max(revisions)
    earlier_number = before if before is not None else later_number - 1
    if earlier_number not in revisions or later_number not in revisions:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    if earlier_number >= later_number:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Compare an earlier revision with a later one.",
        )
    earlier, later = revisions[earlier_number], revisions[later_number]
    members = _members(session, [earlier.id, later.id])
    drawings = _drawing_changes(session, members.get(earlier.id, []), members.get(later.id, []))
    counts: dict[str, int] = defaultdict(int)
    for drawing in drawings:
        for page in drawing.pages:
            counts[page.change] += 1
    before_results = _countertop_results_for_revision(session, package_id, earlier).items
    after_results = _countertop_results_for_revision(session, package_id, later).items
    # A newer revision not read yet has no rows: say so, rather than every earlier row "gone".
    read = bool(after_results) or PackageState(later.state) not in (
        ASSEMBLY_STATES | {PackageState.INGESTING, PackageState.EXTRACTING}
    )
    return RevisionComparisonOut(
        package_id=package_id,
        before_revision_number=earlier_number,
        after_revision_number=later_number,
        drawings=drawings,
        pages_added=counts["added"],
        pages_removed=counts["removed"],
        pages_changed=counts["changed"],
        pages_unchanged=counts["unchanged"],
        countertops=(
            _countertop_changes(
                before_results,
                after_results,
                _row_keys(session, earlier.id),
                _row_keys(session, later.id),
            )
            if read
            else ()
        ),
        countertops_read=read,
        countertops_checked=any(item.finding_id is not None for item in after_results),
    )
