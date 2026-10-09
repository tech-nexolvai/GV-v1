"""Display-only outlines in the published stored space: a slot row's, and an architect dimension's.

`architect_location` (#1066) outlines one architect span the architect reader stored
(`workflow/architect_reader.py`): its ticks (`arch-ticks:<x0>:<x1>`, page points in pdfplumber's
frame) and its printed label's box (the candidate's `image` polygon). Read-only; `None` whenever
that geometry or the page's recorded transform is missing or malformed, never a guess.
"""

from collections.abc import Collection, Sequence
from decimal import Decimal, InvalidOperation
from typing import Final
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.orm import Session, aliased

from app.evidence.sides import reading_transform
from app.models import ObservationCandidate, Page
from app.models.runs import ExtractionRun
from evidence.coordinates import POINTS_PER_INCH, ImagePoint, PdfPoint, StoredPoint
from workflow.architect_pairing_records import ARCHITECT_EXTRACTOR

#: How far from its dimension line the architect reader accepts a printed label, in page points
#: (`MEASURED_ARCHITECT_SETTINGS.label_reach_pt` in `extraction/architect/reader.py`, which the
#: control plane may not import; `tests/api/test_architect_locations.py` keeps the two equal). The
#: reader does not store the line's height, so this band around the label is where it lies.
ARCHITECT_LABEL_REACH_PT: Final = Decimal(12)


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


# --- the architect's dimension (#1066) ------------------------------------------------------------


def _ticks(flags: Sequence[str]) -> tuple[Decimal, Decimal] | None:
    """The span's two ticks, exactly as stored; `None` unless there is one well-formed record."""
    found = [flag.removeprefix("arch-ticks:") for flag in flags if flag.startswith("arch-ticks:")]
    if len(found) != 1:
        return None
    parts = found[0].split(":")
    if len(parts) != 2:
        return None
    try:
        left, right = Decimal(parts[0]), Decimal(parts[1])
    except InvalidOperation:
        return None
    if not (left.is_finite() and right.is_finite()) or left >= right:
        return None
    return left, right


def _label_pixels(candidate: ObservationCandidate) -> list[ImagePoint] | None:
    """The label's four stored corners, integer pixels; `None` for anything else."""
    polygon = candidate.polygon
    if candidate.coordinate_space != "image" or not isinstance(polygon, list) or len(polygon) != 4:
        return None
    corners: list[ImagePoint] = []
    for corner in polygon:
        if not isinstance(corner, list | tuple) or len(corner) != 2:
            return None
        if any(isinstance(value, bool) or not isinstance(value, int) for value in corner):
            return None
        corners.append(ImagePoint(corner[0], corner[1]))
    if len({point.x for point in corners}) < 2 or len({point.y for point in corners}) < 2:
        return None
    return corners


def architect_location(
    candidate: ObservationCandidate, page: Page, run: ExtractionRun
) -> RowLocation | None:
    """Where one stored architect span is on its page: tick to tick, with its label.

    Built in PDF space with the transform the reading was made under (`reading_transform`), the
    way the reader placed the label (`extraction/reader.pixel_placement`): across, from the left
    tick (or the label, if it overhangs) to the right one; up and down, the label's box and the band
    of `ARCHITECT_LABEL_REACH_PT` around its centre, in which the reader found the line, widened by
    one pixel for the label's rounding to whole pixels. `None` when the candidate is not the
    architect reader's, is not on `page`, or any geometry is missing or malformed.
    """
    if (
        run.extractor != ARCHITECT_EXTRACTOR
        or candidate.extraction_run_id != run.id
        or candidate.page_id != page.id
    ):
        return None
    ticks = _ticks(candidate.ambiguity_flags or ())
    label = _label_pixels(candidate)
    transform = reading_transform(page, run)
    if ticks is None or label is None or transform is None or run.dpi is None:
        return None
    try:
        corners = [transform.to_pdf(point) for point in label]
        label_xs = [corner.x for corner in corners]
        label_ys = [corner.y for corner in corners]
        middle = (min(label_ys) + max(label_ys)) / 2
        reach = ARCHITECT_LABEL_REACH_PT + POINTS_PER_INCH / Decimal(run.dpi)
        left, right = min(ticks[0], *label_xs), max(ticks[1], *label_xs)
        low, high = min(middle - reach, *label_ys), max(middle + reach, *label_ys)
        points = [
            transform.to_stored(transform.to_image(PdfPoint(x, y)))
            for x, y in ((left, low), (right, low), (right, high), (left, high))
        ]
    except (ArithmeticError, TypeError, ValueError):
        return None
    if any(not value.is_finite() or value < 0 or value > 1 for point in points for value in point):
        return None
    top_left = StoredPoint(min(p.x for p in points), min(p.y for p in points))
    bottom_right = StoredPoint(max(p.x for p in points), max(p.y for p in points))
    if top_left.x == bottom_right.x or top_left.y == bottom_right.y:
        return None
    return RowLocation(
        page_id=page.id,
        document_version_id=page.document_version_id,
        page_number=page.index + 1,
        polygon=[
            [str(x), str(y)]
            for x, y in (
                (top_left.x, top_left.y),
                (bottom_right.x, top_left.y),
                (bottom_right.x, bottom_right.y),
                (top_left.x, bottom_right.y),
            )
        ],
    )


def architect_locations(db: Session, candidate_ids: Collection[UUID]) -> dict[UUID, RowLocation]:
    """`architect_location` for many candidates in one statement; a candidate it cannot place is
    left out."""
    if not candidate_ids:
        return {}
    output: dict[UUID, RowLocation] = {}
    for candidate, page, run in db.execute(
        select(ObservationCandidate, Page, ExtractionRun)
        .join(Page, Page.id == ObservationCandidate.page_id)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .where(ObservationCandidate.id.in_(tuple(candidate_ids)))
    ).all():
        location = architect_location(candidate, page, run)
        if location is not None:
            output[candidate.id] = location
    return output
