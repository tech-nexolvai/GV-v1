"""Store, once per reading, where each architect dimension was paired with the vendor's line (#1418).

The direct compare (`extraction/compare/pair.py`) pairs every dimension the architect printed on a
combined sheet with the vendor's dimension line for the same span, by geometry only. This writes
its result, one append-only `ArchitectCompareRecord` per architect dimension, under an extraction
run of its own. No decision is written: whether the vendor's number matches is decided from the
stored readings when the review reads the records (`app/review/architect_compare.py`).

**Exactly once.** Every architect dimension `pair_page` returns is written, held or unpaired ones
too, and a run that already has records is never written again (a redelivered stage).

Called from `DatabaseStages.extract_pages` for each shop document. $0: no model call.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.record import open_extraction_run
from app.models import Page
from app.models.architect_compare import ArchitectCompareRecord
from extraction.compare.dimensions import Axis, DrawnDimension
from extraction.compare.page import ComparePage
from extraction.compare.pair import MEASURED_PAIR_SETTINGS, Pairing, PairStatus, compare_pdf
from extraction.geometry.rows import Box

__all__ = [
    "AI_PAIRED",
    "ARCHITECT_COMPARE_EXTRACTOR",
    "ARCHITECT_COMPARE_VERSION",
    "more_pairs",
    "persist_architect_compare",
]

ARCHITECT_COMPARE_EXTRACTOR: Final = "architect-compare"
ARCHITECT_COMPARE_VERSION: Final = "architect-compare-v1"


def _box(box: Box) -> list[str]:
    return [str(box.x0), str(box.top), str(box.x1), str(box.bottom)]


def _union(boxes: Sequence[Box]) -> Box:
    return Box(
        min(box.x0 for box in boxes),
        min(box.top for box in boxes),
        max(box.x1 for box in boxes),
        max(box.bottom for box in boxes),
    )


def _line_box(span: DrawnDimension) -> Box:
    if span.axis is Axis.HORIZONTAL:
        return Box(span.start_pt, span.offset_pt, span.end_pt, span.offset_pt)
    return Box(span.offset_pt, span.start_pt, span.offset_pt, span.end_pt)


#: How a pair made by the AI pair check says so (`Pairing.how`); any other pair is code's alone.
AI_PAIRED: Final = "two AIs"


def _paired_by(pairing: Pairing) -> str:
    return AI_PAIRED if pairing.how == AI_PAIRED else "geometry"


def more_pairs(
    session: Session, page: ComparePage, pairings: tuple[Pairing, ...]
) -> tuple[Pairing, ...]:
    """**The hook for a second pairing pass** over the dimensions geometry left unpaired: the AI
    pair check (`GV_ARCH_COMPARE_AI_PAIR`, off by default, built separately) plugs in here and
    returns the same dimensions, each exactly once, a pair it made marked `how="two AIs"`. Until it
    exists this returns the geometry pass unchanged."""
    return pairings


def persist_architect_compare(
    session: Session,
    *,
    package_revision_id: UUID,
    version_id: UUID,
    data: bytes,
    task_run_id: UUID,
    dpi: int,
) -> int:
    """Pair every architect dimension on the document's sheets and store one record for each.
    Returns how many records were written (0 when this run already wrote them)."""
    run = open_extraction_run(
        session,
        task_run_id=task_run_id,
        extractor=ARCHITECT_COMPARE_EXTRACTOR,
        extractor_version=ARCHITECT_COMPARE_VERSION,
        config_hash=f"{version_id}:{dpi}",
        dpi=dpi,
    )
    if session.scalar(
        select(ArchitectCompareRecord.id)
        .where(ArchitectCompareRecord.extraction_run_id == run.id)
        .limit(1)
    ):
        return 0
    pages = {
        page.index: page
        for page in session.scalars(select(Page).where(Page.document_version_id == version_id))
    }
    titles: dict[tuple[int, int], str | None] = {}
    written = 0
    for page, geometry in compare_pdf(data, dpi=dpi, settings=MEASURED_PAIR_SETTINGS):
        pairings = more_pairs(session, page, geometry)
        stored = pages.get(page.page_index)
        if stored is None:
            continue
        for view in page.architect:
            titles[(page.page_index, view.annotation_index)] = view.title
        for pairing in pairings:
            dim = pairing.architect
            parts = [dim.label_box] + ([] if dim.span is None else [_line_box(dim.span)])
            vendor = pairing.vendor
            paired = pairing.status is PairStatus.PAIRED and vendor is not None
            label = pairing.vendor_label if paired else None
            session.add(
                ArchitectCompareRecord(
                    extraction_run_id=run.id,
                    package_revision_id=package_revision_id,
                    page_id=stored.id,
                    architect_view=pairing.architect_view,
                    view_title=titles.get((page.page_index, pairing.architect_view)),
                    architect_number=dim.number,
                    architect_text=dim.text,
                    architect_numerator=None if dim.inches is None else dim.inches.numerator,
                    architect_denominator=None if dim.inches is None else dim.inches.denominator,
                    axis=None if dim.span is None else dim.span.axis.value,
                    pairing=(
                        PairStatus.PAIRED.value if label is not None else PairStatus.UNPAIRED.value
                    ),
                    reason=(
                        pairing.reason
                        if label is not None or not paired
                        else "the vendor's line carries no label"
                    ),
                    architect_box=_box(_union(parts)),
                    vendor_view=pairing.vendor_view if label is not None else None,
                    vendor_label_box=None if label is None else _box(label),
                    vendor_line_box=(
                        None if label is None or vendor is None else _box(_line_box(vendor))
                    ),
                    paired_by=None if label is None else _paired_by(pairing),
                    vendor_drawn=(
                        None
                        if label is None or pairing.vendor_inches_drawn is None
                        else str(pairing.vendor_inches_drawn)
                    ),
                )
            )
            written += 1
    session.flush()
    return written
