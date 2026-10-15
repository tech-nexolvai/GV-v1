"""Every depth reading has its own picture, cut the same way as every other reading (#1389).

**What was wrong.** The crop stage (`DatabaseStages.validate_evidence`) cuts the picture of every
reading, and runs once, after the match stage that holds the depth and sink reader. "Read depths
again" (#1308) reads the depths a second time on a set already past that stage, and never cut a
picture; nor did a run that stopped after matching. Their depth readings have no picture, and the
simple screen cannot offer Confirm on a reading without its own picture (`app/review/
depth_readings.py`).

**What this does.** For every depth reading (`DEPTH_EXTRACTOR`) with no crop, it cuts one with the
crop stage's own code (`DatabaseStages.cut_page_crops`: both layers rendered at the crop dpi, the
reading's own region, `generate_crop`), and records it as the crop stage would. "Read depths again"
runs it at its end (`DatabaseStages.read_depths_again`); `scripts/backfill_depth_pictures.py` runs
it once over the readings made before, with a dry run first.

**What it never does.**

- Cut a picture at screen time, change a reading, a crop or a decision. `evidence_artifacts` is
  append-only (0013): this only adds the crop rows that are missing.
- Guess where a reading is. A reading's region is pixels at the dpi it was read at
  (`ExtractionRun.dpi`); it is cut only where that is the crop dpi, so the region names the same
  pixels. A reading made at another dpi, a stored file that is not its digest, a page that will
  not render: no picture, and a plain sentence saying why. Never silent.
- Call a model. Rendering and cutting are deterministic code: a picture cut again is byte for byte
  the one the crop stage stored.

**Idempotent.** A reading with a crop is skipped up front, so a second run cuts nothing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from sqlalchemy import Select, exists, select
from sqlalchemy.orm import Session

from app.api.documents import storage_key
from app.models import (
    DocumentVersion,
    EvidenceArtifact,
    EvidenceArtifactKind,
    ObservationCandidate,
    PackageRevisionDocument,
    Page,
)
from app.models.runs import ExtractionRun
from workflow.depth_evidence import DEPTH_EXTRACTOR
from workflow.stages import REPORTED_REFUSALS, DatabaseStages

__all__ = ["DepthPictures", "cut_depth_pictures"]


@dataclass(slots=True)
class DepthPictures:
    """What one run did, as counts a person can read before and after (`dry_run`)."""

    run_id: UUID
    dry_run: bool
    readings: int = 0
    """Depth readings in scope with no picture."""
    cut: int = 0
    """Pictures cut and recorded (in a dry run, that would be)."""
    not_cut: int = 0
    """Readings left without a picture, each with a reason below."""
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": str(self.run_id),
            "dry_run": self.dry_run,
            "readings": self.readings,
            "cut": self.cut,
            "not_cut": self.not_cut,
            # Capped: this payload is stored as JSON, and a page that will not render says so once
            # per reading. The counts are exact.
            "reasons": self.reasons[:REPORTED_REFUSALS],
        }


def _missing(
    package_revision_id: UUID | None,
) -> Select[ObservationCandidate, Page, DocumentVersion, ExtractionRun]:
    """Every depth reading with no crop, with its page, document and run, page by page."""
    query = (
        select(ObservationCandidate, Page, DocumentVersion, ExtractionRun)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .join(Page, Page.id == ObservationCandidate.page_id)
        .join(DocumentVersion, DocumentVersion.id == ObservationCandidate.document_version_id)
        .where(
            ExtractionRun.extractor == DEPTH_EXTRACTOR,
            Page.document_version_id == ObservationCandidate.document_version_id,
            ~exists().where(
                EvidenceArtifact.candidate_id == ObservationCandidate.id,
                EvidenceArtifact.kind == EvidenceArtifactKind.CROP.value,
            ),
        )
        .order_by(
            DocumentVersion.created_at,
            DocumentVersion.id,
            Page.index,
            ObservationCandidate.created_at,
            ObservationCandidate.id,
        )
    )
    if package_revision_id is not None:
        query = query.where(
            ObservationCandidate.document_version_id.in_(
                select(PackageRevisionDocument.document_version_id).where(
                    PackageRevisionDocument.package_revision_id == package_revision_id
                )
            )
        )
    return query


def cut_depth_pictures(
    session: Session,
    stages: DatabaseStages,
    *,
    dry_run: bool = False,
    package_revision_id: UUID | None = None,
) -> DepthPictures:
    """Cut the picture of every depth reading without one (or one revision's), as the crop stage.

    `stages` must be built as the worker that cuts pictures is (same store, dpi and settings).
    Flushes; the caller commits, or rolls back. In a dry run nothing is stored or added.
    """
    outcome = DepthPictures(run_id=uuid4(), dry_run=dry_run)
    rows = session.execute(_missing(package_revision_id)).all()
    outcome.readings = len(rows)
    if not rows:
        return outcome
    store = stages.artifact_store
    if store is None:
        outcome.not_cut = len(rows)
        outcome.reasons.append("no artifact store is configured to read the drawings from")
        return outcome

    # Grouped by document and page, so each document is read once and each page rendered once.
    pending: dict[UUID, tuple[DocumentVersion, dict[UUID, tuple[Page, list[ObservationCandidate]]]]]
    pending = {}
    for candidate, page, version, run in rows:
        if run.dpi != stages.evidence_dpi:
            # The region is pixels at the dpi it was read at: at another dpi it names other pixels.
            outcome.not_cut += 1
            outcome.reasons.append(
                f"page {page.index + 1}: it was read at "
                f"{'an unrecorded' if run.dpi is None else run.dpi} dpi and pictures are cut at "
                f"{stages.evidence_dpi} dpi"
            )
            continue
        _, pages = pending.setdefault(version.id, (version, {}))
        pages.setdefault(page.id, (page, []))[1].append(candidate)

    for version, pages in pending.values():
        count = sum(len(candidates) for _, candidates in pages.values())
        try:
            with store.get(storage_key(version.document_id, version.sha256)) as stored:
                data = stored.read()
        # The store is backend-neutral and cannot name every backend's error.
        except Exception as error:  # noqa: BLE001 - every reading on it is told why
            outcome.not_cut += count
            outcome.reasons.append(f"the drawing's file could not be read ({type(error).__name__})")
            continue
        if hashlib.sha256(data).hexdigest() != version.sha256:
            outcome.not_cut += count
            outcome.reasons.append("the drawing's stored file does not match its digest")
            continue
        for page, candidates in pages.values():
            cut = stages.cut_page_crops(session, data, page, candidates, set(), dry_run=dry_run)
            outcome.cut += len(cut.cut)
            outcome.not_cut += len(candidates) - len(cut.cut)
            outcome.reasons.extend(
                # The crop stage counts pages from 0; a person counts them from 1.
                f"page {page.index + 1}: {why.split(': ', 1)[-1]}"
                for why in cut.refusals
            )

    if not dry_run:
        session.flush()
    return outcome
