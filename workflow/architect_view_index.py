"""Index every view of the architect's own file, with a picture of each (#1166).

When the architect's drawings are uploaded as their own PDF, each vendor countertop row must be
matched with one of the architect's views (`workflow/architect_matching.py`). This module writes
down what the matcher, the AIs and the reviewer choose from, right after the architect reader has
read the file (`workflow/stages.py`, after `persist_architect_pages`):

* one `architect_view_index` row per view per architect reader run, on an architect-kind file only:
  its page, number and tag, the sheet number from the title block (`sheet_index.read_sheet_labels`),
  the bubble, title and scale note as printed, the view's scale as exact text (the printed scale
  and paste factor when the reader gave the view a scale at all, as the pairing uses it), its
  extent, whether it stands clearly apart from its neighbours, whether its role is confirmed, how
  many dimension rows were read in it, and why;
* a picture of each view's extent, rendered vendor-layer only (no reviewer markup) and stored at
  `architect-views/{version}/pages/{page index}/view-{n}-{sha256}.png`.

Every view is indexed, whether or not its role was confirmed or it stands apart: the reviewer must
be able to choose any view of the file, and to be told why one was not read. Nothing here decides a
match or reads a value.

Source: issue #1166 · Verification: `tests/workflow/test_architect_view_index.py`
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Final
from uuid import UUID

import pdfplumber
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DocumentVersion, DrawingView, Page
from app.models.evidence import ArchitectViewIndexEntry
from extraction.architect.pairing import DrawnRow, DrawnSpan
from extraction.architect.reader import ArchitectPage, ArchitectSettings, ArchitectView
from extraction.architect.sheet_index import SheetLabels, read_sheet_labels
from extraction.architect.view_matching import ArchitectViewFacts
from extraction.geometry.rows import Box
from extraction.rasterise import PageTooLarge, render_region
from extraction.reader import UnreadablePdf, pixel_placement
from workflow.architect_match_contract import MatchedView
from workflow.architect_match_records import ARCHITECT_FILE_NAME
from workflow.architect_pairing_records import architect_views
from workflow.view_roles import content_view_tag, panel_tag

if TYPE_CHECKING:
    from storage.store import ArtifactStore

__all__ = [
    "VIEW_PICTURE_MARGIN_PT",
    "ArchitectViewCrop",
    "IndexedView",
    "record_architect_view_index",
    "view_scale",
]

#: The picture of a view reaches this far past its extent, in page points, so a line drawn on the
#: extent's edge is never clipped.
VIEW_PICTURE_MARGIN_PT: Final = Decimal(12)
#: The most pixels one view's picture may hold (the stage's whole-page budget).
_MAXIMUM_PIXELS: Final = 40_000_000
_POINTS_PER_INCH: Final = Fraction(72)
_REASON_LIMIT: Final = 500


@dataclass(frozen=True, slots=True)
class IndexedView:
    """One indexed view, as the matcher takes it."""

    view: MatchedView
    facts: ArchitectViewFacts
    """`facts.key` is `str(view.view_id)`."""
    points_per_inch: Fraction | None
    """Page points per real inch of the view; `None` when unknown."""
    carry_key: tuple[str, str, str]
    """`(document sha256, page content hash, view tag)`: the same view on another revision."""
    view_number: int
    extent: Box
    """The view's extent in page points (pdfplumber's frame)."""


@dataclass(frozen=True, slots=True)
class ArchitectViewCrop:
    """The picture of one view and the view it shows."""

    view_id: UUID
    png: bytes | None
    """`None` when it could not be rendered or there is no store: then no AI is shown the view."""
    sha256: str | None
    storage_key: str | None
    px_per_inch: Fraction | None
    """Picture pixels per real inch, when the view's scale is known."""
    indexed: IndexedView


def view_scale(view: ArchitectView) -> Fraction | None:
    """The view's scale for drawn positions, as the pairing takes it: the printed scale times the
    paste factor when the reader gave the view a scale at all, else the reader's own; `None` when
    the reader gave it none (its labels contradicted its note, or neither exists)."""
    if view.points_per_inch is None:
        return None
    if view.absolute_points_per_inch is not None:
        return Fraction(view.absolute_points_per_inch)
    return Fraction(view.points_per_inch)


def _tag(view: ArchitectView) -> str:
    if view.source == "content":
        return content_view_tag(view.annotation_index)
    return panel_tag(view.annotation_index)


def _drawn_rows(
    reading: ArchitectPage, view: ArchitectView, scale: Fraction | None
) -> tuple[DrawnRow, ...]:
    rows: list[DrawnRow] = []
    for row in reading.rows:
        if row.view_annotation_index != view.annotation_index or not row.spans:
            continue
        try:
            rows.append(
                DrawnRow(
                    key=f"arch-row:{row.rank}",
                    spans=tuple(
                        DrawnSpan(span.x0_pt, span.x1_pt, span.on_outline, printed=span.text)
                        for span in row.spans
                    ),
                    overall=None,
                    pt_per_inch=scale,
                )
            )
        except (TypeError, ValueError):
            continue
    return tuple(rows)


def _box_json(box: Box) -> dict[str, object]:
    return {
        "frame": "pdfplumber-points",
        "x0": str(box.x0),
        "top": str(box.top),
        "x1": str(box.x1),
        "bottom": str(box.bottom),
    }


def _picture(
    data: bytes,
    page: Page,
    view: ArchitectView,
    *,
    place: object,
    dpi: int,
    version_id: UUID,
    store: ArtifactStore | None,
) -> tuple[bytes | None, str | None, str | None]:
    """The view's extent rendered vendor-layer only, stored; `(None, None, None)` without a store
    or when it cannot be rendered."""
    if store is None or not callable(place):
        return None, None, None
    box = view.box
    first = place(box.x0 - VIEW_PICTURE_MARGIN_PT, box.top - VIEW_PICTURE_MARGIN_PT)
    second = place(box.x1 + VIEW_PICTURE_MARGIN_PT, box.bottom + VIEW_PICTURE_MARGIN_PT)
    box_px = (
        max(0, min(first.x, second.x)),
        max(0, min(first.y, second.y)),
        max(first.x, second.x),
        max(first.y, second.y),
    )
    if box_px[2] <= box_px[0] or box_px[3] <= box_px[1]:
        return None, None, None
    try:
        png = render_region(
            data,
            page.index,
            box_px=box_px,
            dpi=dpi,
            maximum_pixels=_MAXIMUM_PIXELS,
            vendor_only=True,
        )
    except (PageTooLarge, UnreadablePdf, ValueError):
        return None, None, None
    digest = hashlib.sha256(png).hexdigest()
    key = (
        f"architect-views/{version_id}/pages/{page.index}/view-{view.annotation_index}-{digest}.png"
    )
    saved = store.put(key, io.BytesIO(png), content_type="image/png")
    if saved.sha256 != digest:
        raise ValueError("stored architect view picture hash does not match its bytes")
    return png, digest, key


def _reason(view: ArchitectView, labels: SheetLabels, rows: int) -> str:
    parts = [view.judgment.reason]
    if not view.separated:
        parts.append("not clearly apart from another view, so none of its values are read")
    parts.append(f"scale: {view.scale_reason}")
    parts.append(f"sheet number: {labels.reason}")
    parts.append(f"{rows} dimension row(s) read")
    return "; ".join(part for part in parts if part)[:_REASON_LIMIT]


def record_architect_view_index(
    session: Session,
    *,
    run_id: UUID,
    version_id: UUID,
    data: bytes,
    pages: Sequence[tuple[Page, ArchitectPage]],
    store: ArtifactStore | None,
    dpi: int,
    settings: ArchitectSettings,
) -> dict[UUID, ArchitectViewCrop]:
    """Index every view the architect reader found on the architect's own file, with its picture.

    One row per view per run; a redelivered stage reuses the rows it already wrote for this run.
    Returns every view's picture (and the view itself) by index row id.
    """
    version = session.get_one(DocumentVersion, version_id)
    existing: Mapping[tuple[UUID, int], ArchitectViewIndexEntry] = {
        (entry.page_id, entry.view_number): entry
        for entry in session.scalars(
            select(ArchitectViewIndexEntry).where(
                ArchitectViewIndexEntry.extraction_run_id == run_id,
                ArchitectViewIndexEntry.document_version_id == version_id,
            )
        )
    }
    crops: dict[UUID, ArchitectViewCrop] = {}
    # An unreadable file still gets its index rows, without pictures.
    try:
        document = pdfplumber.open(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        document = None
    try:
        for page, reading in pages:
            if not reading.views:
                continue
            try:
                labels = read_sheet_labels(data, page.index, text=settings.text)
            except UnreadablePdf as error:
                labels = SheetLabels(None, f"the sheet's text could not be read: {error}")
            confirmed = architect_views(session, page.id)
            drawing_views = {
                tag: view_id
                for view_id, tag in session.execute(
                    select(DrawingView.id, DrawingView.tag).where(DrawingView.page_id == page.id)
                )
            }
            place = (
                None
                if document is None or page.index >= len(document.pages)
                else pixel_placement(document.pages[page.index], dpi)
            )
            for view in reading.views:
                tag = _tag(view)
                scale = view_scale(view)
                rows = _drawn_rows(reading, view, scale)
                entry = existing.get((page.id, view.annotation_index))
                png, digest, key = _picture(
                    data,
                    page,
                    view,
                    place=place,
                    dpi=dpi,
                    version_id=version_id,
                    store=store,
                )
                if entry is None:
                    entry = ArchitectViewIndexEntry(
                        extraction_run_id=run_id,
                        document_version_id=version_id,
                        page_id=page.id,
                        view_number=view.annotation_index,
                        view_tag=tag,
                        drawing_view_id=drawing_views.get(tag),
                        sheet_number=(
                            None if labels.sheet_number is None else labels.sheet_number[:64]
                        ),
                        bubble=None if view.bubble is None else view.bubble[:200],
                        title=None if view.title is None else view.title[:300],
                        scale_note=None if view.scale_note is None else view.scale_note[:100],
                        points_per_inch=None if scale is None else _exact(scale),
                        extent={
                            **_box_json(view.box),
                            "stored_points": [[str(x), str(y)] for x, y in view.stored_points],
                        },
                        label_box=None,
                        separated=view.separated,
                        role_confirmed=view.annotation_index in confirmed,
                        row_count=len(rows),
                        reason=_reason(view, labels, len(rows)),
                        picture_sha256=digest,
                        picture_storage_key=key,
                    )
                    session.add(entry)
                    session.flush()
                elif entry.picture_sha256 != digest:
                    # The stored row names the picture first rendered; a re-render that differs is
                    # not shown in its place.
                    png, digest, key = None, entry.picture_sha256, entry.picture_storage_key
                matched = MatchedView(
                    view_id=entry.id,
                    document_version_id=version_id,
                    page_id=page.id,
                    page_number=page.index + 1,
                    view_number=entry.view_number,
                    view_tag=entry.view_tag,
                    title=entry.title,
                    bubble=entry.bubble,
                    sheet_number=entry.sheet_number,
                    scale_note=entry.scale_note,
                    file_name=ARCHITECT_FILE_NAME,
                    separated=entry.separated,
                )
                indexed = IndexedView(
                    view=matched,
                    facts=ArchitectViewFacts(
                        key=str(entry.id),
                        sheet_number=entry.sheet_number,
                        bubble=entry.bubble,
                        rows=rows,
                        separated=entry.separated,
                    ),
                    points_per_inch=scale,
                    carry_key=(version.sha256, page.content_hash, entry.view_tag),
                    view_number=entry.view_number,
                    extent=view.box,
                )
                crops[entry.id] = ArchitectViewCrop(
                    view_id=entry.id,
                    png=png,
                    sha256=digest if png is not None else None,
                    storage_key=key if png is not None else None,
                    px_per_inch=None if scale is None else scale * dpi / _POINTS_PER_INCH,
                    indexed=indexed,
                )
    finally:
        if document is not None:
            document.close()
    session.flush()
    return crops


def _exact(value: Fraction) -> str:
    """A scale as exact text: a terminating decimal when it is one, else `numerator/denominator`."""
    decimal = Decimal(value.numerator) / Decimal(value.denominator)
    if Fraction(decimal) == value:
        return str(decimal.normalize())
    return f"{value.numerator}/{value.denominator}"
