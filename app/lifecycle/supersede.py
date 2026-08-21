"""Supersede: a new revision never overwrites the old review (#211, C3.3).

`docs/DESIGN_PLATFORM.md` §5, quoting the backend proposal:

> *"A new document revision never overwrites an old version; it supersedes the prior package revision
> and starts a new workflow run."*

**A revision is not an edit.** That sentence is the whole story, and everything here exists to make it
true of the database rather than of our intentions. When a drawing is re-issued, the prior revision
keeps its findings, its evidence, its state events and its document set exactly as they were, and a new
revision is created beside it. The question a dispute asks — *"what did you tell us in March?"* — has to
keep one answer, and it only does if nothing about March can be rewritten in August.

**Every drawing is carried forward, and that is a safety property rather than a convenience.** A
countertop width check reads the cabinet elevation as well as the countertop sheet. A revision holding
only the changed drawing would run its checks against a partial set, and the drawings that were absent
would produce no failures — which reads as no problems (`AGENTS.md` §2.2). So the new revision includes
the new version of what changed *and* the same version of everything that did not.

Carrying a drawing forward costs one `package_revision_documents` row pointing at the **same**
`document_version`. No new version, no second artifact, the bytes stored once. That was impossible
before ADR-0018 — `uq_document_versions_source_artifact_id` refuses a second version over the same
bytes — and it is why this story was blocked on #366.

**Nothing here commits.** The state change, the membership rows and the outbox row are one transaction
or none of them, exactly as `workflow.outbox.enqueue` requires: a superseded revision with no successor,
or a successor nothing is working on, are both worse than a failure the caller sees.

Source: backend proposal §9.1 · Design: `docs/DESIGN_PLATFORM.md` §5, ADR-0018 ·
Verification: `tests/lifecycle/test_supersede.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.lifecycle.states import begin, transition
from app.models import (
    Document,
    DocumentVersion,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
)
from workflow.outbox import enqueue

__all__ = [
    "PACKAGE_WORKFLOW",
    "NoNewVersions",
    "NothingToSupersede",
    "TwoVersionsOfOneDocument",
    "VersionFromAnotherPackage",
    "revision_chain",
    "supersede",
    "superseded_by",
]

#: The workflow a superseding revision starts. One run for the revision, not one per document.
#:
#: `app/api/documents.py` has `ingest_document_version` for a single upload, and reusing it here would
#: enqueue one workflow per carried-forward drawing — including the ones that did not change. A
#: revision is the unit that gets reviewed, so it is the unit that gets processed.
PACKAGE_WORKFLOW: Final = "process_package_revision"


class NothingToSupersede(Exception):
    """The package has no revision to supersede.

    A package always has one from creation (`app/api/packages.py` creates revision 1 with it), so this
    means the package does not exist or is not readable — reported as its own failure rather than as an
    illegal transition, because there is no state to leave.
    """


class NoNewVersions(Exception):
    """A supersede was asked for with nothing new in it.

    Refused rather than performed. Superseding a revision because nothing changed would close a review
    that was still valid and start another that would reach the same conclusions, and the audit trail
    would show a revision nobody can explain the existence of.
    """


class TwoVersionsOfOneDocument(Exception):
    """The same drawing was offered at two versions in one supersede.

    Refused rather than resolved. A revision holds one version of any document, so one of the two
    would have to be dropped — and picking silently means composing a revision the caller did not ask
    for, with the survivor decided by the order the database happened to return rows in. Undefined,
    and invisible: the wrong drawing would be reviewed and nothing would say so.
    """


class VersionFromAnotherPackage(Exception):
    """One of the versions belongs to a different package.

    The database refuses this too — `package_revision_documents` resolves `package_id` against both the
    revision and the document — but the caller gets a sentence here rather than an `IntegrityError`
    naming a constraint.
    """


def current_revision(session: Session, package_id: UUID) -> PackageRevision:
    """The package's highest-numbered revision — the one a supersede supersedes.

    By `revision_number` and not by `created_at`: §5 orders revisions by number, and two rows written in
    the same microsecond have no order by timestamp.
    """
    highest = (
        select(func.max(PackageRevision.revision_number))
        .where(PackageRevision.package_id == package_id)
        .scalar_subquery()
    )
    revision = session.scalar(
        select(PackageRevision).where(
            PackageRevision.package_id == package_id,
            PackageRevision.revision_number == highest,
        )
    )
    if revision is None:
        raise NothingToSupersede(f"package {package_id} has no revision to supersede")
    return revision


def _project_of(session: Session, package_id: UUID) -> UUID:
    """The package's project, for the outbox payload.

    `app/api/background.py` scopes the polling handle by `payload['project_id']`, so a workflow enqueued
    without one is work nobody can ask about afterwards.
    """
    project_id = session.scalar(select(Package.project_id).where(Package.id == package_id))
    if project_id is None:
        raise NothingToSupersede(f"package {package_id} does not exist")
    return project_id


def _documents_of(session: Session, package_revision_id: UUID) -> dict[UUID, UUID]:
    """`{document_id: document_version_id}` for one revision — what it was composed of."""
    rows = session.execute(
        select(
            PackageRevisionDocument.document_id,
            PackageRevisionDocument.document_version_id,
        ).where(PackageRevisionDocument.package_revision_id == package_revision_id)
    ).all()
    return {document_id: version_id for document_id, version_id in rows}


def _documents_for(session: Session, version_ids: Sequence[UUID]) -> dict[UUID, UUID]:
    """`{document_id: version_id}` for the versions offered, refusing any from another package.

    Checked here as well as by the foreign keys because the caller deserves to know *which* version was
    wrong, and an `IntegrityError` names a constraint rather than a value.
    """
    if not version_ids:
        return {}
    rows = session.execute(
        select(DocumentVersion.id, DocumentVersion.document_id).where(
            DocumentVersion.id.in_(list(version_ids))
        )
    ).all()
    found = {version_id: document_id for version_id, document_id in rows}
    missing = [str(version_id) for version_id in version_ids if version_id not in found]
    if missing:
        raise NoNewVersions(f"no such document version(s): {', '.join(sorted(missing))}")

    # Inverting `found` keys it by document, and two versions of one document would collapse into one
    # entry — dropping whichever the `IN` predicate happened to return second, which is undefined.
    # Found by CodeRabbit on #377, and it is the failure this project cares about most: a revision
    # composed of a version nobody asked for, with nothing reporting it.
    by_document: dict[UUID, UUID] = {}
    duplicated: set[UUID] = set()
    for version_id, document_id in found.items():
        if document_id in by_document:
            duplicated.add(document_id)
        by_document[document_id] = version_id
    if duplicated:
        raise TwoVersionsOfOneDocument(
            f"document(s) {', '.join(sorted(str(d) for d in duplicated))} were offered at more than "
            "one version. A revision holds one version of any drawing, so this has no single answer — "
            "offer the version the revision should include."
        )
    return by_document


def supersede(
    session: Session,
    *,
    package_id: UUID,
    new_document_versions: Sequence[UUID],
    actor: str,
    reason: str | None = None,
) -> PackageRevision:
    """Supersede the package's current revision with a new one, and enqueue its workflow.

    `new_document_versions` are the versions that prompted this — the re-issued drawings. Every other
    drawing the prior revision included is carried forward at the version it already had, so the new
    revision is a complete package rather than a diff.

    Returns the new revision. Commits nothing: the caller owns the transaction, so the prior revision's
    move to `SUPERSEDED`, the new revision, its document set and the outbox row all land together or
    none of them do.

    Raises:
        NothingToSupersede: the package or its current revision does not exist.
        NoNewVersions: nothing new was offered, or a named version does not exist. A supersede that
            changes nothing closes a valid review and starts an identical one.
        VersionFromAnotherPackage: a named version belongs to a different package.
    """
    if not new_document_versions:
        raise NoNewVersions(
            f"superseding package {package_id} was asked for with no new document versions. A "
            "supersede that changes nothing would close a review that is still valid and start "
            "another reaching the same conclusions."
        )

    prior = current_revision(session, package_id)
    project_id = _project_of(session, package_id)
    offered = _documents_for(session, new_document_versions)

    # Every offered version must belong to a document of *this* package. The foreign keys would refuse
    # it, and this says which one and why.
    owners: dict[UUID, UUID] = {
        document_id: owner
        for document_id, owner in session.execute(
            select(Document.id, Document.package_id).where(Document.id.in_(list(offered)))
        ).all()
    }
    strangers = sorted(
        str(document_id) for document_id, owner in owners.items() if owner != package_id
    )
    if strangers:
        raise VersionFromAnotherPackage(
            f"document(s) {', '.join(strangers)} belong to another package, so a revision of "
            f"package {package_id} cannot include them."
        )

    carried = _documents_of(session, prior.id)
    # The offered versions win where a document appears in both; anything only in the prior revision is
    # carried at the version it already had; anything only offered is a drawing new to this package.
    composition = {**carried, **offered}

    new_revision = PackageRevision(
        package_id=package_id,
        revision_number=prior.revision_number + 1,
        state=PackageState.CREATED,
        supersedes_id=prior.id,
    )
    session.add(new_revision)
    # Flushed so the revision exists for `begin` to read and for the membership rows to reference.
    session.flush()
    begin(session, new_revision.id, actor=actor, reason=reason or "superseded the prior revision")

    for document_id, version_id in composition.items():
        session.add(
            PackageRevisionDocument(
                package_revision_id=new_revision.id,
                package_id=package_id,
                document_id=document_id,
                document_version_id=version_id,
            )
        )
    # Flushed before the prior revision moves, so a composition the database refuses fails here rather
    # than after the old revision has already been closed.
    session.flush()

    # The prior revision's set was read above, while it was still in a state that allowed it. Moving it
    # now touches only its own state, never its documents — which is what "never overwrites" means.
    transition(
        session,
        prior.id,
        PackageState.SUPERSEDED,
        actor=actor,
        reason=reason or f"superseded by revision {new_revision.revision_number}",
    )

    # Exactly one workflow, for the revision. See PACKAGE_WORKFLOW.
    enqueue(
        session,
        workflow=PACKAGE_WORKFLOW,
        payload={
            "package_revision_id": str(new_revision.id),
            "package_id": str(package_id),
            "project_id": str(project_id),
            "supersedes_id": str(prior.id),
            "revision_number": new_revision.revision_number,
        },
    )
    return new_revision


def revision_chain(session: Session, package_id: UUID) -> list[PackageRevision]:
    """Every revision of a package, oldest first.

    Ordered by `revision_number`, which is the order §5 gives them and the only total one — two rows
    written in the same microsecond have no order by `created_at`.
    """
    return list(
        session.scalars(
            select(PackageRevision)
            .where(PackageRevision.package_id == package_id)
            .order_by(PackageRevision.revision_number)
        )
    )


def superseded_by(session: Session, package_revision_id: UUID) -> PackageRevision | None:
    """The revision that superseded this one, or `None` if it is still the current one.

    The other half of the link the acceptance asks for. `supersedes_id` answers new→old on the row
    itself; this answers old→new, so a caller reading a superseded revision can find what replaced it
    without writing that query themselves — and without being tempted to infer it from revision
    numbers, which says nothing about *which* revision superseded which.
    """
    return session.scalar(
        select(PackageRevision).where(PackageRevision.supersedes_id == package_revision_id)
    )
