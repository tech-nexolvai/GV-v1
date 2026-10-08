"""Display-only outline of an immutable slot row, in the published stored space."""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.orm import Session, aliased

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
    return None if row_id is None else row_locations(db, (row_id,)).get(row_id)


def row_locations(db: Session, row_ids: tuple[UUID, ...]) -> dict[UUID, RowLocation]:
    """Resolve many row outlines with one bounded query plan, rather than four queries per row."""
    if not row_ids:
        return {}
    anchor = aliased(ObservationCandidate)
    requested_pairs = (
        select(
            anchor.page_id.label("page_id"),
            anchor.document_version_id.label("document_version_id"),
            anchor.extraction_run_id.label("extraction_run_id"),
        )
        .where(anchor.id.in_(row_ids))
        .distinct()
        .subquery()
    )
    records = db.execute(
        select(ObservationCandidate, Page, ExtractionRun)
        .join(
            requested_pairs,
            and_(
                ObservationCandidate.page_id == requested_pairs.c.page_id,
                ObservationCandidate.document_version_id == requested_pairs.c.document_version_id,
                ObservationCandidate.extraction_run_id == requested_pairs.c.extraction_run_id,
            ),
        )
        .join(Page, Page.id == ObservationCandidate.page_id)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
    ).all()
    by_row: dict[UUID, tuple[ObservationCandidate, Page, ExtractionRun]] = {}
    by_pair: dict[tuple[UUID, UUID, UUID], list[ObservationCandidate]] = {}
    requested = set(row_ids)
    for candidate, page, run in records:
        key = (candidate.page_id, candidate.document_version_id, candidate.extraction_run_id)
        by_pair.setdefault(key, []).append(candidate)
        if candidate.id in requested:
            by_row[candidate.id] = (candidate, page, run)

    output: dict[UUID, RowLocation] = {}
    for row_id, (anchor_candidate, page, run) in by_row.items():
        rank = next(
            (
                flag
                for flag in anchor_candidate.ambiguity_flags or ()
                if flag.startswith("row-rank:")
            ),
            None,
        )
        if rank is None:
            continue
        key = (anchor_candidate.page_id, anchor_candidate.document_version_id, run.id)
        location = _location_from_candidates(
            anchor_candidate,
            page,
            run,
            by_pair[key],
            rank,
        )
        if location is not None:
            output[row_id] = location
    return output


def _location_from_candidates(
    anchor: ObservationCandidate,
    page: Page,
    run: ExtractionRun,
    candidates: list[ObservationCandidate],
    rank: str,
) -> RowLocation | None:
    rank = (
        next((flag for flag in anchor.ambiguity_flags or () if flag.startswith("row-rank:")), None)
        or rank
    )
    transform = reading_transform(page, run)
    if transform is None:
        return None
    points: list[StoredPoint] = []
    for candidate in candidates:
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
