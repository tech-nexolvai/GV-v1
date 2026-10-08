"""Display-only outline of an immutable slot row, in the published stored space."""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.sides import reading_transform
from app.models import ObservationCandidate, Page
from app.models.runs import ExtractionRun
from evidence.coordinates import ImagePoint, PdfPoint, StoredPoint


class RowLocation(BaseModel):
    page_id: UUID
    document_version_id: UUID
    page_number: int
    coordinate_space: str = "stored"
    polygon: list[list[str]]


def row_location(db: Session, row_id: UUID | None) -> RowLocation | None:
    """Never borrow another page/run's boxes or invent a missing transform."""
    anchor = None if row_id is None else db.get(ObservationCandidate, row_id)
    if anchor is None:
        return None
    rank = next(
        (flag for flag in anchor.ambiguity_flags or () if flag.startswith("row-rank:")), None
    )
    if rank is None:
        return None
    page = db.get(Page, anchor.page_id)
    run = db.get(ExtractionRun, anchor.extraction_run_id)
    if page is None or run is None:
        return None
    transform = reading_transform(page, run)
    if transform is None:
        return None
    points: list[StoredPoint] = []
    for candidate in db.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.page_id == anchor.page_id,
            ObservationCandidate.document_version_id == anchor.document_version_id,
            ObservationCandidate.extraction_run_id == anchor.extraction_run_id,
        )
    ):
        flags = candidate.ambiguity_flags or ()
        if "slot-reader" not in flags or rank not in flags:
            continue
        slot_box = next(
            (flag.split(":", 1)[1] for flag in flags if flag.startswith("slot-box:")), None
        )
        try:
            if slot_box is not None:
                x0, y0, x1, y1 = [int(value) for value in slot_box.split(",")]
                points.extend(
                    transform.to_stored(ImagePoint(x, y)) for x, y in ((x0, y0), (x1, y1))
                )
            else:
                for x, y in candidate.polygon:
                    if candidate.coordinate_space == "image":
                        point = transform.to_stored(ImagePoint(int(x), int(y)))
                    elif candidate.coordinate_space == "stored":
                        point = StoredPoint(Decimal(str(x)), Decimal(str(y)))
                    elif candidate.coordinate_space == "pdf":
                        point = transform.to_stored(
                            transform.to_image(PdfPoint(Decimal(str(x)), Decimal(str(y))))
                        )
                    else:
                        return None
                    points.append(point)
        except (ValueError, TypeError, ArithmeticError):
            return None
    if not points or any(not v.is_finite() or v < 0 or v > 1 for p in points for v in p):
        return None
    left, top = min(p.x for p in points), min(p.y for p in points)
    right, bottom = max(p.x for p in points), max(p.y for p in points)
    if left == right or top == bottom:
        return None
    return RowLocation(
        page_id=page.id,
        document_version_id=page.document_version_id,
        page_number=page.index + 1,
        polygon=[
            [str(x), str(y)]
            for x, y in ((left, top), (right, top), (right, bottom), (left, bottom))
        ],
    )
