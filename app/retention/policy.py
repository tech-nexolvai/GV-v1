"""What expires, what is held, and the record that something went (#258, F1.7).

Client drawings are proprietary, and backend §11 requires retention schedules. Holding a customer's
drawing set forever because nobody wrote the deletion code is not a neutral default — it is an
undertaking nobody made, and it grows.

**The bytes expire; the record does not.** `SourceArtifact` and `EvidenceArtifact` carry `Immutable`,
and this module never deletes a row. It removes content from storage and leaves behind the key, the
sha256, the size and the time — so "we held this drawing, this was its hash, it was deleted on this
date under this policy" stays answerable after the drawing itself is gone. A retention policy that
erased the record along with the bytes would destroy the audit trail it operates under, and the
question retention exists to answer is precisely *what did you have, and what happened to it?*

**Nothing disappears without a record.** Every deletion emits an `ARTIFACT_DELETION` audit event, and
the audit trail is committed *before* any byte is touched — storage cannot join a database
transaction, so one of the two has to go first, and a record of a deletion that has not happened yet
is recoverable where a deletion with no record is not. Backend §11 lists six audit categories; this is
a seventh, because deletion is the one event whose own evidence is the thing being removed.

**Legal hold wins, always.** A project under hold has nothing deleted, however old. The check is a
positive one — *is there an unreleased hold?* — rather than an exclusion applied afterwards, so a
join that silently returns no rows keeps content rather than deleting it. The direction matters: a
bug here deletes a customer's drawings during a dispute.

**A dry run is the default posture, not a courtesy.** `apply_retention` takes `commit=False` and
reports what it would do. The first thing anybody sensible does with a deletion job is ask what it is
about to remove, and a policy that could only be run for real makes that impossible to ask.

**What this does not do.** It does not delete logs or traces. Those expire in the systems that hold
them — a log sink's own retention setting — and a Python function claiming to delete them would be a
control that looked implemented and enforced nothing. `RETENTION` declares their periods
anyway, as the documented figures an operator configures those systems to — a schedule that exists
only in somebody's memory of a conversation is not a schedule. And `tests/test_retention.py` asserts
the far more useful property: that drawing content never reaches a log in the first place, so what
expires there is only ever references and hashes.

Source: backend proposal §11; `AGENTS.md` §6 · Verification: ``tests/test_retention.py``
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.events import SYSTEM_ACTOR, AuditCategory, emit
from app.models.document import Document, DocumentVersion, SourceArtifact
from app.models.evidence import EvidenceArtifact
from app.models.package import Package
from app.models.retention import LegalHold
from storage.store import ArtifactStore

__all__ = [
    "RETENTION",
    "ArtifactClass",
    "Expired",
    "RetentionReport",
    "apply_retention",
    "held_projects",
]


class ArtifactClass(StrEnum):
    """The classes of content this project retains, each with its own schedule.

    Separate schedules because the classes carry different risk and different value. An original is
    the customer's own document and the thing a dispute is about; a crop is a few hundred pixels this
    system produced, useful while a review is live and of little value afterwards.
    """

    ORIGINAL = "original"
    """The uploaded drawing. The longest schedule: it is the evidence in any argument about what was
    submitted, and it is the one artifact this system did not create and cannot recreate."""

    RENDER = "render"
    """A rasterised page this system produced. Reproducible from the original, so it is cache rather
    than evidence — and the shortest useful schedule is the right one for a derivative."""

    CROP = "crop"
    """The region behind one reading. Kept as long as a finding is likely to be disputed, then
    dropped: it is the most sensitive content here and the easiest to regenerate."""

    LOG = "log"
    """Application logs. Expired by the log system's own retention, not by this module — see the
    module docstring."""

    TRACE = "trace"
    """Distributed traces. As `LOG`: expired where they are stored."""


#: How long each class is kept, from creation.
#:
#: Defaults, and deliberately conservative ones — a period too long is a storage bill, a period too
#: short is a customer's drawing gone before a dispute is heard. An operator overrides them per
#: deployment; `apply_retention` takes the schedule as an argument for exactly that reason, so a
#: shorter period is a call site's decision rather than an edit to this constant.
RETENTION: Final[Mapping[ArtifactClass, timedelta]] = MappingProxyType(
    {
        ArtifactClass.ORIGINAL: timedelta(days=7 * 365),
        ArtifactClass.RENDER: timedelta(days=180),
        ArtifactClass.CROP: timedelta(days=365),
        ArtifactClass.LOG: timedelta(days=90),
        ArtifactClass.TRACE: timedelta(days=30),
    }
)

#: The classes this module actually deletes. `LOG` and `TRACE` expire where they are stored.
DELETABLE: Final = (ArtifactClass.ORIGINAL, ArtifactClass.RENDER, ArtifactClass.CROP)

#: Which `EvidenceArtifact.kind` belongs to which class.
_EVIDENCE_CLASS: Final[Mapping[str, ArtifactClass]] = MappingProxyType(
    {"crop": ArtifactClass.CROP, "render": ArtifactClass.RENDER}
)


@dataclass(frozen=True, slots=True)
class Expired:
    """One artifact past its schedule, and enough to delete it and say what was deleted."""

    artifact_id: UUID
    artifact_class: ArtifactClass
    storage_key: str
    sha256: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RetentionReport:
    """What a pass did, or would have done.

    `held` is reported rather than merely skipped. A retention pass that quietly deletes less than
    expected looks identical to one that is working, and "why is this project still full?" should
    have an answer that does not require reading the code.
    """

    deleted: tuple[Expired, ...]
    held: tuple[UUID, ...]
    """Projects skipped because a legal hold is in force."""

    already_gone: tuple[Expired, ...]
    """Past their schedule and not in storage. Ordinary on a second pass — retention is re-run on a
    schedule and interrupted halfway more often than anyone plans for — and worth reporting rather
    than counting as a deletion that did not happen."""

    committed: bool

    @property
    def total_considered(self) -> int:
        return len(self.deleted) + len(self.already_gone)


def held_projects(session: Session) -> frozenset[UUID]:
    """Projects with a legal hold in force.

    Asked as a positive question — *which projects are held?* — and the deletion path then requires a
    project to be absent from this set. The inverse (*is this one held?*, asked per artifact) would
    delete content whenever the query failed to find the hold, and the failure modes of a join are
    all silent. Here a broken query returns fewer held projects, which is still wrong, but the shape
    of the code makes the risky direction the one you have to write deliberately.
    """
    return frozenset(
        session.scalars(select(LegalHold.project_id).where(LegalHold.released_at.is_(None))).all()
    )


def _expired_originals(session: Session, cutoff: datetime, held: frozenset[UUID]) -> list[Expired]:
    """Uploaded drawings past their schedule, excluding held projects.

    Joined all the way to `Package` so the project is known: a hold is placed on a matter, and the
    matter is a project. An artifact whose join to a project is broken is not deleted, because it
    cannot be shown to be unheld.
    """
    rows = session.execute(
        select(
            SourceArtifact.id,
            SourceArtifact.storage_key,
            SourceArtifact.sha256,
            SourceArtifact.created_at,
            Package.project_id,
        )
        .join(DocumentVersion, DocumentVersion.source_artifact_id == SourceArtifact.id)
        .join(Document, DocumentVersion.document_id == Document.id)
        .join(Package, Document.package_id == Package.id)
        .where(SourceArtifact.created_at < cutoff)
    ).all()

    return [
        Expired(
            artifact_id=artifact_id,
            artifact_class=ArtifactClass.ORIGINAL,
            storage_key=key,
            sha256=digest,
            created_at=created,
        )
        for artifact_id, key, digest, created, project_id in rows
        if project_id not in held
    ]


def _expired_evidence(
    session: Session,
    schedule: Mapping[ArtifactClass, timedelta],
    now: datetime,
    held: frozenset[UUID],
) -> list[Expired]:
    """Crops and renders past their own schedules.

    Two classes in one query because they share a table and differ only by `kind`, and each is
    compared against its own cutoff — a crop and a render created in the same second do not expire
    on the same day.
    """
    rows = session.execute(
        select(
            EvidenceArtifact.id,
            EvidenceArtifact.kind,
            EvidenceArtifact.storage_key,
            EvidenceArtifact.sha256,
            EvidenceArtifact.created_at,
            Package.project_id,
        )
        .join(DocumentVersion, EvidenceArtifact.document_version_id == DocumentVersion.id)
        .join(Document, DocumentVersion.document_id == Document.id)
        .join(Package, Document.package_id == Package.id)
    ).all()

    expired: list[Expired] = []
    for artifact_id, kind, key, digest, created, project_id in rows:
        if project_id in held:
            continue
        artifact_class = _EVIDENCE_CLASS.get(str(kind))
        if artifact_class is None:
            # An unrecognised kind is kept. A new artifact kind with no schedule yet must not fall
            # into whichever cutoff happens to be nearest.
            continue
        if created >= now - schedule[artifact_class]:
            continue
        expired.append(
            Expired(
                artifact_id=artifact_id,
                artifact_class=artifact_class,
                storage_key=key,
                sha256=digest,
                created_at=created,
            )
        )
    return expired


def apply_retention(
    session: Session,
    store: ArtifactStore,
    *,
    now: datetime | None = None,
    schedule: Mapping[ArtifactClass, timedelta] | None = None,
    commit: bool = False,
) -> RetentionReport:
    """Delete the bytes of everything past its schedule, and audit each deletion.

    `commit=False` by default, and that default is the point: the first thing anybody sensible does
    with a deletion job is ask what it is about to remove. A dry run touches no storage, writes no
    audit event and returns the same report a real pass would.

    `now` is injectable so a pass is reproducible and its boundary testable. A function that read the
    clock could not be tested for the day an artifact expires, and the boundary is where a schedule
    is most often wrong by one.

    **`commit=True` commits the session, and that is a deliberate departure.** Everything else in
    this codebase writes in the caller's transaction and leaves the commit to them. Here it cannot:
    storage is a second system with no shared transaction, so the audit row and the byte removal
    cannot land together whatever order they are written in. One of two things has to be possible.

    Either the bytes go first — and a rollback afterwards discards the audit rows, leaving content
    deleted with no record of it, which is the state this module calls undetectable by construction.
    Or the audit rows are committed first — and a crash before the storage call leaves a row saying
    an artifact was deleted while its bytes are still there.

    The second is recoverable and the first is not. A row claiming a deletion that has not happened
    yet is corrected by the next pass, which deletes the bytes and appends a second row; a deletion
    with no row is invisible forever. So the audit trail is committed **before** any byte is touched,
    and this function owns that commit. The cost is an occasional duplicate audit row after a crash,
    which over-records rather than under-records — the direction to fail in.

    A caller that wants the audit rows in its own transaction should run a dry pass, do its own work,
    and call again; it must not wrap this in a `unit_of_work` it intends to roll back.
    """
    if not isinstance(commit, bool):
        raise TypeError("commit must be a bool")

    moment = now if now is not None else datetime.now(UTC)
    periods = schedule if schedule is not None else RETENTION
    for artifact_class in DELETABLE:
        if artifact_class not in periods:
            raise ValueError(
                f"the schedule does not say how long to keep {artifact_class.value}. A missing "
                "period is not a licence to delete now, and it is not a licence to keep forever "
                "either — it has to be stated."
            )
        if periods[artifact_class] <= timedelta(0):
            raise ValueError(
                f"the retention period for {artifact_class.value} is not positive, which would "
                "delete content the moment it was written."
            )

    held = held_projects(session)
    expired: list[Expired] = [
        *_expired_originals(session, moment - periods[ArtifactClass.ORIGINAL], held),
        *_expired_evidence(session, periods, moment, held),
    ]

    if not commit:
        return RetentionReport(
            deleted=tuple(expired),
            held=tuple(sorted(held, key=str)),
            already_gone=(),
            committed=False,
        )

    for item in expired:
        emit(
            session,
            category=AuditCategory.ARTIFACT_DELETION,
            actor=SYSTEM_ACTOR,
            target_id=item.artifact_id,
            target_type=(
                "source_artifacts"
                if item.artifact_class is ArtifactClass.ORIGINAL
                else "evidence_artifacts"
            ),
        )

    # Committed before a single byte is removed, and this is the whole reason the function owns its
    # commit. Storage cannot join this transaction, so the choice is between a record of a deletion
    # that has not happened yet and a deletion with no record. The first is corrected by the next
    # pass; the second is invisible forever.
    session.commit()

    deleted: list[Expired] = []
    already_gone: list[Expired] = []
    for item in expired:
        if store.delete(item.storage_key):
            deleted.append(item)
        else:
            already_gone.append(item)

    return RetentionReport(
        deleted=tuple(deleted),
        held=tuple(sorted(held, key=str)),
        already_gone=tuple(already_gone),
        committed=True,
    )
