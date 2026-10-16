"""A synthetic vendor drawing with one stored slot-reader row, for the cabinet run proposal tests.

Every number, name and hash here is made up.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models import (
    Document,
    DocumentKind,
    DocumentVersion,
    ObservationCandidate,
    PackageRevisionDocument,
    SourceArtifact,
)
from app.models.document import Page
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun

READERS = ("reader-a", "reader-b")


@dataclass
class Piece:
    """One stored piece: its printed text, its exact inches, and anything that should spoil it."""

    text: str
    inches: Fraction | None
    sealed: bool = True
    flags: list[str] = field(default_factory=list)
    ink: str = "vendor"


def add_reading(
    session: Session,
    *,
    package_id: UUID,
    revision_id: UUID,
    pieces: Sequence[Piece],
    overall: Piece | None = None,
    kind: str = DocumentKind.SHOP.value,
    row_flags: Sequence[str] = (),
    page_index: int = 12,
) -> UUID:
    """Store one read row on a new page of a new document; return the row's anchor (`slot:0`)."""
    source = SourceArtifact(storage_key=f"synthetic/{uuid4()}", sha256="c" * 64, size=1)
    document = Document(package_id=package_id, kind=kind)
    session.add_all((source, document))
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=source.id, sha256="c" * 64, page_count=20
    )
    session.add(version)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision_id,
            package_id=package_id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    page = Page(
        document_version_id=version.id,
        index=page_index,
        content_hash=f"{uuid4().int:064x}"[-64:],
        width_pt=Decimal(612),
        height_pt=Decimal(792),
        rotation=0,
        has_vector_text=True,
        media_box=["0", "0", "612", "792"],
        crop_box=["0", "0", "612", "792"],
    )
    session.add(page)
    workflow_run = WorkflowRun(package_revision_id=revision_id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow_run.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task)
    session.flush()
    run = ExtractionRun(
        task_run_id=task.id,
        extractor="extraction.form_reader",
        extractor_version="slot-reader-v1-test",
        config_hash="synthetic-test",
        dpi=150,
    )
    session.add(run)
    session.flush()
    anchor: UUID | None = None
    owners: list[tuple[str, Piece]] = [(str(index), piece) for index, piece in enumerate(pieces)]
    if overall is not None:
        owners.append(("overall", overall))
    for at, (slot, piece) in enumerate(owners):
        x = 100 + at * 60
        sealed = piece.sealed and piece.inches is not None
        candidate = ObservationCandidate(
            document_version_id=version.id,
            page_id=page.id,
            extraction_run_id=run.id,
            raw_text=piece.text,
            value_numerator=None if piece.inches is None else piece.inches.numerator,
            value_denominator=None if piece.inches is None else piece.inches.denominator,
            unit=None if piece.inches is None else "in",
            polygon=[[x, 800], [x + 40, 800], [x + 40, 820], [x, 820]],
            coordinate_space="image",
            ambiguity_flags=[
                "slot-reader",
                f"slot:{slot}",
                f"slot-box:{x},790,{x + 40},830",
                "row-rank:1",
                f"row-slot-count:{len(pieces)}",
                *row_flags,
                *piece.flags,
                *(f"reader-id:{reader}" for reader in READERS),
                f"ink:{piece.ink}",
            ],
            corroboration_status="CORROBORATED" if sealed else None,
            corroboration_lane="SECOND_READER" if sealed else None,
        )
        session.add(candidate)
        session.flush()
        if slot == "0":
            anchor = candidate.id
    session.commit()
    assert anchor is not None
    return anchor


SAMPLE_RUN = (
    Piece('2"', Fraction(2)),
    Piece('21"', Fraction(21)),
    Piece('30"', Fraction(30)),
    Piece('24"', Fraction(24)),
    Piece('1 1/2"', Fraction(3, 2)),
)
