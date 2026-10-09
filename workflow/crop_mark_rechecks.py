"""One-time job: re-check whether evidence crops cut before #1078 show GV's markup (#1141).

**What was wrong.** An evidence crop is cut from the page rendered with both layers, so a person
sees GV's own notes in it. Until #1078 its "shows GV markup" flag
(`EvidenceArtifact.shows_gv_marks`) asked only about GV marks baked into the vendor's drawing,
never about GV's annotation layer, so a crop with GV's yellow box over the vendor's label said "no
GV markup" and the screen did not offer the vendor-only view (#952). New crops are right; the old
ones keep the old answer.

**What this does** (Anant's decision (c), 2026-10-10). For every stored crop of a reading, it cuts
the same pixels again with the crop stage's own code and asks the corrected check (`crop_mark_state`
through `EvidenceMarks`, the very function the crop stage now uses). Where the answer differs from
the flag a reader is shown today, it writes one `EvidenceMarkRecheck` row. Readers use the newest
re-check, else the crop's own flag (`app/evidence/crop_marks.py`).

**What it never does.**

- Change a crop row, its bytes, a reading, a finding or a decision. `evidence_artifacts` is
  append-only (0013) and stays so; this only inserts re-check rows. It stores no picture.
- Lower a warning. A crop already shown as "shows GV markup" is left as it is, whatever a re-check
  says: the flag only offers a second view, and taking it away is the unsafe direction.
- Guess. A crop is re-checked only when the pixels cut again are **byte-identical** to the stored
  crop (same digest). A crop that cannot be cut again identically (a different render, a different
  dpi, a picture that was never a cut of the reading's region) is counted and left alone.
- Call a model. Rendering and the check are deterministic code.

**Idempotent.** One row per crop per `CROP_MARK_CHECK_VERSION`, held by a unique constraint and
skipped up front, so a second run writes nothing. `dry_run` does all the work and writes nothing.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.api.documents import storage_key
from app.evidence.crop_marks import current_crop_marks
from app.models import (
    DocumentVersion,
    EvidenceArtifact,
    EvidenceArtifactKind,
    EvidenceMarkRecheck,
    ObservationCandidate,
    PackageRevisionDocument,
    Page,
)
from evidence.crop import crop_png
from extraction.rasterise import PageTooLarge
from extraction.reader import UnreadablePdf
from storage.store import ArtifactStore
from workflow.stages import DatabaseStages, evidence_crop_spec

#: A stored crop and the reading it was cut for.
_Crop = tuple[EvidenceArtifact, ObservationCandidate]

#: The check the re-check rows record: both layers asked, as #1078 made the crop stage ask (#952).
CROP_MARK_CHECK_VERSION: Final = "952-both-layers"


@dataclass(slots=True)
class CropMarkRecheck:
    """What one run found, as counts a person can read before and after (`dry_run`)."""

    run_id: UUID
    dry_run: bool
    check_version: str = CROP_MARK_CHECK_VERSION
    crops: int = 0
    """Stored crops of readings in scope."""
    already_rechecked: int = 0
    """Crops this check version has already answered for: skipped, nothing written."""
    rechecked: int = 0
    """Crops cut again byte-identically and asked."""
    unchanged: int = 0
    """The answer is the flag already shown."""
    changed: int = 0
    """Rows written (or, in a dry run, that would be)."""
    changes: Counter[str] = field(default_factory=Counter)
    """`changed` by direction, e.g. `False -> True`."""
    kept_marked: int = 0
    """Already shown as "shows GV markup" and the re-check disagrees: left as it is."""
    not_same_pixels: int = 0
    """The pixels cut again are not the stored crop's: left as it is."""
    not_checkable: int = 0
    """The document, page or region could not be read again: left as it is."""
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": str(self.run_id),
            "dry_run": self.dry_run,
            "check_version": self.check_version,
            "crops": self.crops,
            "already_rechecked": self.already_rechecked,
            "rechecked": self.rechecked,
            "unchanged": self.unchanged,
            "changed": self.changed,
            "changes": dict(sorted(self.changes.items())),
            "kept_marked": self.kept_marked,
            "not_same_pixels": self.not_same_pixels,
            "not_checkable": self.not_checkable,
            "reasons": list(self.reasons),
        }


def _flag(value: bool | None) -> str:
    return "None" if value is None else str(value)


def _fetch(store: ArtifactStore, key: str) -> bytes:
    with store.get(key) as stored:
        return stored.read()


def recheck_crop_marks(
    session: Session,
    stages: DatabaseStages,
    *,
    run_by: str,
    dry_run: bool,
    package_revision_id: UUID | None = None,
) -> CropMarkRecheck:
    """Re-check every stored crop of a reading (or one revision's) and record differing answers.

    `stages` must be built as the worker that cut the crops was (same store, dpi and reader
    settings), or nothing re-cuts byte-identically and nothing is written. Flushes; the caller
    commits, or rolls back. In a dry run nothing is added to the session at all.
    """
    if not isinstance(run_by, str) or not run_by.strip():
        raise ValueError("run_by must name who ran the re-check")
    store = stages.artifact_store
    if store is None:
        raise ValueError("these stages have no artifact store, so no document can be read again")
    outcome = CropMarkRecheck(run_id=uuid4(), dry_run=dry_run)

    query = (
        select(
            EvidenceArtifact, ObservationCandidate, Page, DocumentVersion, EvidenceMarkRecheck.id
        )
        .join(ObservationCandidate, ObservationCandidate.id == EvidenceArtifact.candidate_id)
        .join(Page, Page.id == EvidenceArtifact.page_id)
        .join(DocumentVersion, DocumentVersion.id == EvidenceArtifact.document_version_id)
        .outerjoin(
            EvidenceMarkRecheck,
            and_(
                EvidenceMarkRecheck.crop_artifact_id == EvidenceArtifact.id,
                EvidenceMarkRecheck.check_version == CROP_MARK_CHECK_VERSION,
            ),
        )
        .where(
            EvidenceArtifact.kind == EvidenceArtifactKind.CROP.value,
            EvidenceArtifact.coordinate_space == "image",
            # The crop and its reading must agree on where the reading is.
            ObservationCandidate.page_id == EvidenceArtifact.page_id,
            ObservationCandidate.document_version_id == EvidenceArtifact.document_version_id,
            Page.document_version_id == EvidenceArtifact.document_version_id,
        )
        .order_by(
            DocumentVersion.created_at,
            DocumentVersion.id,
            Page.index,
            EvidenceArtifact.created_at,
            EvidenceArtifact.id,
        )
    )
    if package_revision_id is not None:
        query = query.where(
            EvidenceArtifact.document_version_id.in_(
                select(PackageRevisionDocument.document_version_id).where(
                    PackageRevisionDocument.package_revision_id == package_revision_id
                )
            )
        )
    rows = session.execute(query).all()
    outcome.crops = len(rows)

    # Grouped by document and page, so each document is read once and each page rendered once.
    pending: dict[UUID, tuple[DocumentVersion, dict[UUID, tuple[Page, list[_Crop]]]]] = {}
    for artifact, candidate, page, version, recheck_id in rows:
        if recheck_id is not None:
            outcome.already_rechecked += 1
            continue
        _, pages = pending.setdefault(version.id, (version, {}))
        pages.setdefault(page.id, (page, []))[1].append((artifact, candidate))

    for version, pages in pending.values():
        # What a reader is shown today, one document at a time so the lookup stays bounded.
        shown = current_crop_marks(
            session,
            {
                artifact.id: artifact.shows_gv_marks
                for _, crops in pages.values()
                for artifact, _ in crops
            },
        )
        try:
            data = _fetch(store, storage_key(version.document_id, version.sha256))
        # The store is backend-neutral and cannot name every backend's error. A document that
        # cannot be read leaves its crops exactly as they are, and says so.
        except Exception as error:  # noqa: BLE001
            count = sum(len(crops) for _, crops in pages.values())
            outcome.not_checkable += count
            outcome.reasons.append(
                f"document version {version.id}: could not be read ({type(error).__name__})"
            )
            continue
        for page, crops in pages.values():
            _recheck_page(session, stages, outcome, data, page, crops, shown, run_by=run_by)

    if not dry_run:
        session.flush()
    return outcome


def _recheck_page(
    session: Session,
    stages: DatabaseStages,
    outcome: CropMarkRecheck,
    data: bytes,
    page: Page,
    crops: Iterable[_Crop],
    shown: dict[UUID, bool | None],
    *,
    run_by: str,
) -> None:
    crops = list(crops)
    where = f"document version {page.document_version_id} page {page.index}"
    if page.render_failed:
        outcome.not_checkable += len(crops)
        outcome.reasons.append(f"{where}: the manifest recorded a failed render")
        return
    try:
        rendered = stages.evidence_render(data, page)
    except (PageTooLarge, UnreadablePdf, ValueError) as error:
        outcome.not_checkable += len(crops)
        outcome.reasons.append(f"{where}: did not render ({type(error).__name__})")
        return
    marks = stages.evidence_marks(data, page)

    for artifact, candidate in crops:
        spec = evidence_crop_spec(candidate, rendered)
        try:
            png = None if spec is None else crop_png(rendered, spec)
        except ValueError:
            png = None
        if spec is None or png is None:
            outcome.not_checkable += 1
            continue
        if hashlib.sha256(png).hexdigest() != artifact.sha256:
            outcome.not_same_pixels += 1
            continue
        outcome.rechecked += 1
        answer = marks.state(rendered, spec)
        before = shown[artifact.id]
        if answer is before:
            outcome.unchanged += 1
            continue
        if before is True:
            # Never lower a warning: the flag only offers the vendor-only view.
            outcome.kept_marked += 1
            continue
        outcome.changed += 1
        outcome.changes[f"{_flag(before)} -> {_flag(answer)}"] += 1
        if not outcome.dry_run:
            session.add(
                EvidenceMarkRecheck(
                    crop_artifact_id=artifact.id,
                    shows_gv_marks=answer,
                    check_version=CROP_MARK_CHECK_VERSION,
                    run_id=outcome.run_id,
                    run_by=run_by.strip(),
                )
            )
