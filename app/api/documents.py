"""Documents: register one, get an upload ticket, confirm the bytes landed (#205, C2.3).

## The API never receives file bytes

Backend §4.1 and `docs/DESIGN_PLATFORM.md` §4.2 (C2.6) are explicit: the control plane does short work
only, and uploads go straight to storage. So there is no route here that takes a file. Registration
hands back a **ticket** — a URL scoped to one storage key, permitting a write and nothing else, valid
for a bounded time — and the client writes the drawing to storage itself. Confirmation is a small JSON
request saying "it landed, and this is its hash".

That is not a convention to be remembered. `tests/api/test_packages.py` walks every route in the whole
application, descends into every request model, and fails if any of them declares a file or a bytes
body. A new route that took an upload would fail that test rather than quietly ship.

## What confirmation guarantees, layer by layer

This is the part that is easy to over-claim, so it is stated before anything else.

* **The `DocumentVersion` and the outbox row commit together, or neither does.** Both are written into
  one `Session` and one transaction; `workflow.outbox.enqueue()` never opens a transaction of its own.
  A failure anywhere between the version and the commit takes both down. That is the whole of C4.1, and
  it works for exactly one reason: they are the same transaction. If they can ever end up in two, this
  guarantees nothing.
* **A re-upload becomes a new version, and the *database* is what refuses an edit.**
  `document_versions` is `Immutable`, and #202 installed a trigger that rejects an `UPDATE` on it at
  the database, against every writer — this module, another service, a hand-typed `UPDATE`. What *this
  module* does is never attempt one: a new upload inserts a new row. Those are two different
  guarantees and only the first one is enforcement.
* **A hash mismatch writes nothing.** The stored object is read and hashed, and the answer is compared
  with what the client declared. The check happens before the first `INSERT`, so a rejection leaves no
  artifact row, no version and no outbox row.
* **Re-confirming the same bytes is idempotent** — the same version comes back and no second outbox row
  is written, so ingestion does not run twice. Sequentially this is a lookup; concurrently it is
  `document_versions`' unique constraint on `(document_id, sha256)`, and the loser of that race is
  handed the winner's row instead of an error. The constraint is the mechanism; the lookup is a
  convenience over it.
* **Not guaranteed: that the ticket was the only way the bytes got there.** On the local backend
  nothing enforces the ticket — a filesystem has no gatekeeper to present one to (`storage/local.py`
  says so plainly). What confirmation checks is the bytes, by hashing them, which is the check that
  actually matters: whatever route they arrived by, they are the bytes the client declared or the
  request is refused.
* **Not guaranteed: that the page count is right.** It is the client's declaration; the API never
  opens the file. Ingestion builds the real page manifest (#160) from the PDF.

Source: backend proposal §10.2, §11; §4.1 · Design: `docs/DESIGN_PLATFORM.md` §4.1, §4.2, §6.1 ·
Verification: `tests/api/test_packages.py`
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, Final
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import Select, and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.auth import Action, Principal, require_action, require_project_access
from app.db.base import utc_now
from app.db.session import is_unique_violation
from app.models import (
    Document,
    DocumentVersion,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    SourceArtifact,
)
from app.schemas.packages import (
    DocumentRegistration,
    DocumentVersionOut,
    PresignedUpload,
    UploadConfirmation,
    UploadRequest,
)
from storage.hashing import content_key, sha256_stream
from storage.store import ArtifactStore
from workflow.outbox import enqueue

router = APIRouter(tags=["documents"])

#: What every refusal says. Nothing about the project, the document or the reason.
NOT_FOUND_DETAIL = "Not found"

#: How long an upload ticket lasts.
#:
#: An operational default, not a domain tolerance — `AGENTS.md` §2.4's "never invent a value" is about
#: measurements and tolerances that decide a PASS, and no arithmetic in this project depends on this
#: number. It is long enough for a large drawing on a poor connection and short enough that a leaked
#: URL is not a standing capability. It lives in one place so changing it is one edit, and the client
#: is told the resulting deadline rather than the duration.
UPLOAD_TICKET_LIFETIME: Final = timedelta(minutes=15)

#: V1 ingests PDFs — `docs/DESIGN_EXTRACTION.md` §3.1 reads PDF pages, and `pdfplumber`/`pypdfium2`
#: read nothing else. Fixed here rather than taken from the request: a client-chosen media type would
#: need an allowlist to be worth anything, and inventing one would be deciding something this story was
#: not asked to decide. The ticket already carries the type as a required header, so accepting another
#: one later is a change to this constant and a request field, not a redesign.
CONTENT_TYPE: Final = "application/pdf"
KEY_SUFFIX: Final = ".pdf"

#: The workflow confirmation asks for. A name, resolved by whoever runs the dispatcher — nothing here
#: imports the workflow engine, which is what keeps the outbox a seam (`workflow/outbox.py`).
INGEST_WORKFLOW: Final = "ingest_document_version"

#: Constraints whose violation means "these exact bytes are already confirmed for this document".
#: Both spellings of the same fact: the version is unique on `(document_id, sha256)`, and the artifact
#: is unique on `storage_key`, which is derived from those two. Matched by the name the *database*
#: reports — see `app/db/session.py`.
_ALREADY_CONFIRMED: Final = ("document_id_sha256", "storage_key", "source_artifact_id")


def storage_key(document_id: UUID, sha256: str) -> str:
    """Where a document's bytes live: derived from the document and the hash, never chosen.

    Content-addressed, so it is stable — asking for a second ticket for the same bytes yields the same
    key, and the store's own rule that an existing key may never be rewritten with different bytes
    (`docs/DESIGN_PLATFORM.md` §7) then reinforces the immutability rather than sitting beside it.

    The document id is in the key on purpose. Without it two documents that happen to be byte-identical
    would map to one key, and `source_artifacts.storage_key` is unique while
    `document_versions.source_artifact_id` is unique too — so the second document could never be
    confirmed at all. Per-document keys cost some duplicate bytes and keep each document's history its
    own.
    """

    return content_key(f"documents/{document_id}", sha256, suffix=KEY_SUFFIX)


# ---------------------------------------------------------------------------
# Resolving a resource inside its project — the isolation boundary in SQL
# ---------------------------------------------------------------------------


def _package_revision(session: Session, project_id: UUID, package_id: UUID) -> PackageRevision:
    """The revision a document registered now attaches to, or a 404.

    The highest `revision_number`, which is the order `docs/DESIGN_PLATFORM.md` §5 gives revisions.
    The project is in the `WHERE` clause as well as in the dependency: the dependency establishes that
    the caller may see this project, and the clause establishes that this row is this project's.
    """

    latest = (
        select(func.max(PackageRevision.revision_number))
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .scalar_subquery()
    )
    revision = session.scalar(
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(
            Package.id == package_id,
            Package.project_id == project_id,
            PackageRevision.revision_number == latest,
        )
    )
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return revision


def _document(session: Session, project_id: UUID, document_id: UUID) -> Document:
    """One document, reached only through the project that owns it, or a 404.

    A document in another project is reported exactly as one that does not exist. Answering the two
    differently is the 403-shaped leak `docs/DESIGN_PLATFORM.md` §4.3 names.
    """

    # One join fewer than before ADR-0018: a document names its package directly, so the revision is
    # no longer in the path between them. Which revisions *include* a version of it is
    # `package_revision_documents`, and it is not what identifies the document.
    document = session.scalar(
        select(Document)
        .join(Package, Package.id == Document.package_id)
        .where(Document.id == document_id, Package.project_id == project_id)
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return document


def _version_query(document_id: UUID, sha256: str) -> Select[Any]:
    """The existing version for these exact bytes, with the artifact it pins."""

    return (
        select(
            DocumentVersion.id,
            DocumentVersion.document_id,
            DocumentVersion.source_artifact_id,
            DocumentVersion.sha256,
            DocumentVersion.page_count,
            DocumentVersion.created_at,
            SourceArtifact.storage_key,
            SourceArtifact.size.label("size_bytes"),
        )
        .join(
            SourceArtifact,
            and_(
                SourceArtifact.id == DocumentVersion.source_artifact_id,
                SourceArtifact.sha256 == DocumentVersion.sha256,
            ),
        )
        .where(DocumentVersion.document_id == document_id, DocumentVersion.sha256 == sha256)
    )


def _existing_version(
    session: Session, document_id: UUID, sha256: str
) -> DocumentVersionOut | None:
    row = session.execute(_version_query(document_id, sha256)).first()
    return None if row is None else DocumentVersionOut.model_validate(dict(row._mapping))


def _ticket(store: ArtifactStore, document_id: UUID, sha256: str) -> PresignedUpload:
    """Issue the ticket for one document's declared bytes. Writes nothing."""

    key = storage_key(document_id, sha256)
    ticket = store.upload_ticket(key, content_type=CONTENT_TYPE, expires_in=UPLOAD_TICKET_LIFETIME)
    return PresignedUpload(
        document_id=document_id,
        storage_key=ticket.key,
        upload_url=ticket.url,
        method=ticket.method,
        expires_at=ticket.expires_at,
        required_headers=dict(ticket.required_headers),
    )


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/packages/{package_id}/documents",
    response_model=PresignedUpload,
    status_code=status.HTTP_201_CREATED,
    summary="Register a document and get an upload ticket",
)
def register_document(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.MANAGE_PROJECT))],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
    body: DocumentRegistration,
) -> PresignedUpload:
    """Create the document's identity and hand back where to send its bytes.

    **No file in this request.** Hash your file locally, send the hash, and you get back a URL scoped
    to one storage key, permitting a write and nothing else, expiring at `expires_at`. Send the bytes
    there with the `method` and `required_headers` exactly as returned, then call `/confirm`.

    The ticket is issued *before* the document row is committed, so a store that cannot issue one
    leaves no half-registered document behind. Nothing is written to storage either — a ticket is an
    intention, and an intention that is never used leaves nothing to clean up.
    """
    del principal

    # Still resolved, and still a 404 if it is not this project's: registering a document against a
    # package with no revision would create an identity nothing can ever be uploaded into.
    _package_revision(session, project_id, package_id)
    # Package-scoped since ADR-0018. A document is one drawing for the life of the package; which
    # revisions include which version of it is recorded when a version is confirmed, below.
    document = Document(package_id=package_id, kind=body.kind)
    # The id is assigned at construction (`app/db/base.py` assigns it on `init`, not at INSERT), so the
    # key and the ticket can be built before anything is committed.
    ticket = _ticket(store, document.id, body.sha256)

    session.add(document)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return ticket


@router.post(
    "/projects/{project_id}/documents/{document_id}/uploads",
    response_model=PresignedUpload,
    status_code=status.HTTP_201_CREATED,
    summary="Get an upload ticket for a new version of an existing document",
)
def request_upload(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.MANAGE_PROJECT))],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    document_id: UUID,
    body: UploadRequest,
) -> PresignedUpload:
    """A ticket for re-uploading this document — new bytes, same logical document.

    This is the route that makes "a re-upload is a new version, never an edit" reachable through the
    API. The existing versions are untouched, and confirming these bytes inserts another
    `document_versions` row beside them.

    Reads nothing and writes nothing: the document already exists, and the ticket is derived from its
    id and the hash you declared.
    """
    del principal

    document = _document(session, project_id, document_id)
    return _ticket(store, document.id, body.sha256)


@router.post(
    "/projects/{project_id}/documents/{document_id}/confirm",
    response_model=DocumentVersionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Confirm an upload landed, and start ingestion",
)
def confirm_upload(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.MANAGE_PROJECT))],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    response: Response,
    project_id: UUID,
    document_id: UUID,
    body: UploadConfirmation,
) -> DocumentVersionOut:
    """Check the bytes, then write the version and the ingestion request in one transaction.

    In order:

    1. The stored object for your declared hash must exist. Nothing there is `409` — there is no upload
       to confirm, which is different from a bad request.
    2. It is read and hashed. If it does not hash to what you declared, the request is refused with
       `422` and **nothing at all is written** — no artifact row, no version, no outbox row.
    3. The `SourceArtifact`, the `DocumentVersion` and the outbox row are written in one transaction.
       Either all three land or none do.

    Returns `201` when a version was created and `200` when these exact bytes had already been
    confirmed. The repeat is a genuine no-op: the same version comes back and no second outbox row is
    written, so ingestion does not run twice.

    Reading the object to hash it is the one piece of real work here, and it is bounded by the size of
    one drawing. It is also the only honest way to verify: `AGENTS.md` §2.7 pins a document version to
    exact bytes, and taking the client's word for the hash would make that pin a restatement of their
    claim. A backend that computes the checksum itself can answer this without a read; the local
    filesystem cannot, and #221 is where that becomes worth optimising.
    """
    del principal

    document = _document(session, project_id, document_id)
    if (existing := _existing_version(session, document.id, body.sha256)) is not None:
        response.status_code = status.HTTP_200_OK
        return existing

    key = storage_key(document.id, body.sha256)
    if not store.exists(key):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Nothing has been uploaded for that hash yet. Send the bytes to the upload URL from "
                "registration first, then confirm."
            ),
        )
    with store.get(key) as stored:
        digest, size = sha256_stream(stored)
    if digest != body.sha256:
        # Refused before the first INSERT, so this path writes nothing. The declared hash is what the
        # storage key was built from, so a mismatch means the bytes at that key are not the bytes the
        # client says they are — the version would pin the wrong content.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "The uploaded bytes do not match the SHA-256 you declared. Nothing has been recorded. "
                "Upload again and confirm with the hash of what you actually sent."
            ),
        )

    artifact = SourceArtifact(storage_key=key, sha256=digest, size=size, backend_version_id=None)
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=artifact.id,
        sha256=digest,
        page_count=body.page_count,
    )
    try:
        session.add(artifact)
        # Flushed on its own, and the order matters. `DocumentVersion.source_artifact_id` is a plain
        # `ForeignKey` with no `relationship()` between the two models, so SQLAlchemy has no way to
        # know the artifact must be inserted first — adding both and flushing once let it choose, and
        # it chose the version, which the foreign key then refused. Two flushes in one transaction
        # cost nothing; guessing costs a 500 on the happy path.
        session.flush()

        session.add(version)
        # Still flushed, still not committed. This is the point the induced-failure test targets: the
        # version is in front of the database, the outbox row is not yet written, and a failure here
        # must leave neither. It also brings a unique violation forward to where it can be recognised.
        session.flush()

        # The version joins the package's current revision (ADR-0018). Before that record, the
        # document itself named a revision and this step did not exist; now membership is explicit,
        # which is what lets a later revision include this same version without copying it.
        #
        # Resolved here rather than at registration because the current revision is a fact about *now*:
        # a document registered against revision 1 and confirmed after a supersede belongs in the
        # revision being assembled, not the one that has closed.
        revision = _package_revision(session, project_id, document.package_id)
        # Upserted, not inserted. A revision holds one version of any drawing
        # (`uq_revision_documents_one_version_per_document`), so re-uploading corrected bytes while the
        # revision is still being assembled *replaces* which version it includes rather than adding a
        # second. The database refuses this once the revision has left assembly — see
        # `gv_reject_frozen_revision_documents` in `0017` — which is the case handled below.
        membership = pg_insert(PackageRevisionDocument).values(
            id=uuid4(),
            created_at=utc_now(),
            package_revision_id=revision.id,
            package_id=document.package_id,
            document_id=document.id,
            document_version_id=version.id,
        )
        session.execute(
            membership.on_conflict_do_update(
                constraint="uq_revision_documents_one_version_per_document",
                set_={"document_version_id": version.id},
            )
        )
        # Flushed here rather than at commit so a frozen revision is refused before the outbox row is
        # written — the caller gets a conflict they can act on instead of an opaque failure after the
        # ingestion request had already been recorded.
        session.flush()

        enqueue(
            session,
            workflow=INGEST_WORKFLOW,
            payload={
                "document_version_id": str(version.id),
                "document_id": str(document.id),
                "package_revision_id": str(revision.id),
                "project_id": str(project_id),
                "storage_key": key,
                "sha256": digest,
            },
        )
        session.commit()
    except IntegrityError as error:
        session.rollback()
        if not is_unique_violation(error, *_ALREADY_CONFIRMED):
            raise
        # Somebody else confirmed the same bytes while this request was in flight. The constraint is
        # what decided it; this hands the loser the winner's row instead of a 500. If the row is not
        # readable the error is re-raised rather than reported as "already done" — a wrong answer here
        # would be a version this caller believes exists.
        concurrent = _existing_version(session, document.id, body.sha256)
        if concurrent is None:
            raise
        response.status_code = status.HTTP_200_OK
        return concurrent
    except Exception:
        session.rollback()
        raise

    return DocumentVersionOut(
        id=version.id,
        document_id=version.document_id,
        source_artifact_id=version.source_artifact_id,
        sha256=version.sha256,
        page_count=version.page_count,
        storage_key=artifact.storage_key,
        size_bytes=artifact.size,
        created_at=version.created_at,
    )
