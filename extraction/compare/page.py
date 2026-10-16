"""One combined sheet, read for the direct compare: every architect dimension per architect view, and the vendor's drawn dimensions per vendor view.

The direct compare asks, for every dimension the architect printed, whether the vendor's drawing
shows the same span with the same number. This module gathers, by code and for free, what that
question is asked about:

* **the architect's views**: only a pasted drawing whose role code decided is the architect's
  (`extraction/architect/reader.py`: the printed heading and the drawing's own content agreeing,
  decision D2). Each gives every dimension printed in it, exactly once (`dimensions.py`), with its
  scale as the architect reader witnessed it;
* **the vendor's views**: every view on a pasted drawing GV's heading names the vendor's
  (`extraction/panels.py`), found by its printed title and scale (`depth/view_kind.py`), or the
  whole drawing when no view is found. On a sheet with no heading, a drawing the architect reader
  decided is the vendor's from the content of both drawings counts too. Each gives its labelled
  dimension lines on both axes and its scale, from its own printed scale note through the factor
  it was pasted at — the only scale code can know there without reading a number (the vendor's
  numbers are pen strokes the AIs read).

**Never silent.** A view that is not read, a vendor drawing with no view, a scale that cannot be
known: each is a note with its reason. **Never** coloured ink (GV's markup): the page is read
through `stamp_text.stamps_only` and the black-and-grey filter every reader uses.

Pure: PDF bytes in, plain values out. Coordinates are pdfplumber's (page points, `top` down).
Source: the direct-compare issue · Verification: `tests/extraction/compare/test_page.py`
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
from typing import Final
from uuid import UUID

from extraction.annotations import read_markup_layer
from extraction.architect.reader import (
    MEASURED_ARCHITECT_SETTINGS,
    ArchitectSettings,
    open_page,
    page_stamps,
    read_architect_page,
)
from extraction.architect.text import TextChar, find_printed
from extraction.architect.views import Role
from extraction.compare.dimensions import (
    ArchitectDimension,
    Axis,
    DimensionSettings,
    DrawnDimension,
    architect_dimensions,
    drawn_dimensions,
    labelled_lines,
)
from extraction.depth.view_kind import (
    MEASURED_VIEW_SETTINGS,
    ViewFinderSettings,
    find_vendor_views,
    kind_from_heading,
)
from extraction.geometry.rows import MEASURED_SETTINGS, Box, RowSettings
from extraction.panels import propose_panel_roles
from extraction.stamp_text import stamps_only

__all__ = [
    "MEASURED_COMPARE_SETTINGS",
    "ArchitectViewDimensions",
    "ComparePage",
    "CompareSettings",
    "VendorViewDimensions",
    "read_compare_page",
]

_UNSTORED: Final = UUID(int=0)
_POINTS_PER_INCH: Final = Fraction(72)


@dataclass(frozen=True, slots=True)
class CompareSettings:
    """Every setting the page reading uses (no defaults)."""

    architect: ArchitectSettings
    architect_rows: RowSettings
    """The architect's dimension lines: its slashes are drawn larger than the vendor's."""
    vendor_rows: RowSettings
    dimensions: DimensionSettings
    views: ViewFinderSettings


MEASURED_COMPARE_SETTINGS: Final = CompareSettings(
    architect=MEASURED_ARCHITECT_SETTINGS,
    # The architect's own page-content row settings (slashes up to 8 pt, about 45 degrees only):
    # on the client's sets the pasted drawings' height ticks are drawn larger than their width
    # ticks, and the smaller limit finds no height at all (measured on all three sets, $0).
    architect_rows=MEASURED_ARCHITECT_SETTINGS.content_rows,
    vendor_rows=MEASURED_SETTINGS,
    dimensions=DimensionSettings(
        tick_pair_pt=MEASURED_ARCHITECT_SETTINGS.tick_pair_pt,
        label_reach_pt=MEASURED_ARCHITECT_SETTINGS.label_reach_pt,
        label_slack_pt=MEASURED_ARCHITECT_SETTINGS.label_slack_pt,
        witness_band=MEASURED_ARCHITECT_SETTINGS.witness_band,
        witness_floor_in=MEASURED_ARCHITECT_SETTINGS.witness_floor_in,
    ),
    views=MEASURED_VIEW_SETTINGS,
)
"""The architect reader's own measured thresholds, reused: nothing here is tuned anew."""


@dataclass(frozen=True, slots=True)
class ArchitectViewDimensions:
    annotation_index: int
    box: Box
    title: str | None
    scale_note: str | None
    points_per_inch: Decimal | None
    """The view's scale as the architect reader witnessed it; `None` when it could not be."""
    scale_reason: str
    dimensions: tuple[ArchitectDimension, ...]
    """Every dimension printed in the view, exactly once."""
    lines: tuple[DrawnDimension, ...]
    """The spans of every dimension line a label was placed on: what is laid over the vendor's."""
    kind: str | None = None
    """What kind of view the sheet's heading over it names (`elevation`, `plan`, ...), or `None`."""


@dataclass(frozen=True, slots=True)
class VendorViewDimensions:
    number: int
    """Page-local, from 1."""
    panel_box: Box
    heading: str | None
    extent: Box
    points_per_inch: Fraction | None
    """Page points per real inch from the view's printed scale note and the paste factor."""
    scale_reason: str
    lines: tuple[DrawnDimension, ...]
    """Every labelled dimension span in the view, both axes."""
    kind: str | None = None
    """What kind of view its own title names, or (when it names none and it is the drawing's only
    view) the sheet's heading over the drawing; `None` when neither names one."""


@dataclass(frozen=True, slots=True)
class ComparePage:
    page_index: int
    architect: tuple[ArchitectViewDimensions, ...]
    vendor: tuple[VendorViewDimensions, ...]
    notes: tuple[str, ...]


def _chars_in(chars: tuple[TextChar, ...], box: Box) -> list[TextChar]:
    return [
        char
        for char in chars
        if box.x0 <= char.box.centre_x <= box.x1 and box.top <= char.box.centre_y <= box.bottom
    ]


def _moved_box(box: Box, dx: Decimal, dtop: Decimal) -> Box:
    return Box(box.x0 + dx, box.top + dtop, box.x1 + dx, box.bottom + dtop)


def _moved_span(span: DrawnDimension, dx: Decimal, dtop: Decimal) -> DrawnDimension:
    across, along = (dtop, dx) if span.axis is Axis.HORIZONTAL else (dx, dtop)
    return replace(
        span,
        offset_pt=span.offset_pt + across,
        start_pt=span.start_pt + along,
        end_pt=span.end_pt + along,
        label_boxes=tuple(_moved_box(box, dx, dtop) for box in span.label_boxes),
    )


def _moved_dimension(dim: ArchitectDimension, dx: Decimal, dtop: Decimal) -> ArchitectDimension:
    return replace(
        dim,
        label_box=_moved_box(dim.label_box, dx, dtop),
        span=None if dim.span is None else _moved_span(dim.span, dx, dtop),
    )


def _vendor_scale(
    chars: tuple[TextChar, ...], where: Box, paste: Decimal | None, settings: CompareSettings
) -> tuple[Fraction | None, str]:
    notes = find_printed(_chars_in(chars, where), settings.architect.text).scales
    if not notes:
        return None, "the view prints no scale code can read"
    if len(notes) > 1:
        return None, "the view prints more than one scale"
    if paste is None:
        return None, "the factor the drawing was pasted at is not known"
    note = notes[0]
    return (
        _POINTS_PER_INCH * note.paper_per_real * Fraction(paste),
        f"its printed scale {note.text} at the paste factor",
    )


def _kind(heading: str | None) -> str | None:
    kind = kind_from_heading(heading).kind
    return None if kind is None else kind.value


def read_compare_page(
    data: bytes, page_index: int, *, settings: CompareSettings, dpi: int
) -> ComparePage:
    """The architect's views with every printed dimension, and the vendor's views with their drawn
    dimensions, on one combined sheet. A page with no architect view returns none, with a note."""
    arch = read_architect_page(data, page_index, settings=settings.architect, dpi=dpi)
    stamps, _pictures = page_stamps(data, page_index)
    page = open_page(
        stamps_only(data, page_index),
        page_index,
        drawing_boxes=tuple(box for box, _factor in stamps.values()),
        rows=settings.architect.rows,
        settings=settings.architect,
        dpi=dpi,
    )
    dx, dtop = page.dx, page.dtop
    notes: list[str] = []
    views: list[ArchitectViewDimensions] = []
    for view in arch.views:
        if not view.read or view.judgment.agreed is not Role.ARCH:
            if view.judgment.agreed is not Role.SHOP:
                notes.append(
                    f"drawing {view.annotation_index} is not read as the architect's: "
                    f"{view.judgment.reason}"
                )
            continue
        box = _moved_box(view.box, -dx, -dtop)
        inside = _chars_in(page.chars, box)
        printed = find_printed(inside, settings.architect.text).dimensions
        spans = drawn_dimensions(
            page.ink,
            box,
            rows=settings.architect_rows,
            settings=settings.dimensions,
            place=page.place,
        )
        dims = architect_dimensions(
            printed, spans, points_per_inch=view.points_per_inch, settings=settings.dimensions
        )
        lines = labelled_lines(spans, [dim.span for dim in dims if dim.span is not None])
        views.append(
            ArchitectViewDimensions(
                annotation_index=view.annotation_index,
                box=view.box,
                title=view.title,
                scale_note=view.scale_note,
                points_per_inch=view.points_per_inch,
                scale_reason=view.scale_reason,
                dimensions=tuple(_moved_dimension(dim, dx, dtop) for dim in dims),
                lines=tuple(_moved_span(span, dx, dtop) for span in lines),
                kind=_kind(view.judgment.heading),
            )
        )
    if not views:
        notes.append("no architect's drawing on this page is decided by code")
    layers = read_markup_layer(data, page_index, document_version_id=_UNSTORED, dpi=dpi)
    proposals = propose_panel_roles(layers.vendor_stamps, layers.markup)
    sheet_headings = {proposal.annotation_index: proposal.heading for proposal in proposals}
    named = [
        proposal.annotation_index
        for proposal in proposals
        if proposal.role == "shop" and proposal.annotation_index in stamps
    ]
    # A sheet with no heading: a drawing the architect reader decided is the vendor's, by the
    # content of both drawings agreeing (its decision D2), is the vendor's here too.
    named += [
        view.annotation_index
        for view in arch.views
        if view.judgment.agreed is Role.SHOP
        and view.annotation_index in stamps
        and view.annotation_index not in named
    ]
    panels = [(*stamps[index], sheet_headings.get(index)) for index in named]
    vendor: list[VendorViewDimensions] = []
    for panel_box, paste, sheet_heading in panels:
        found = [
            view
            for view in find_vendor_views(page.chars, page.ink, panel_box, settings.views).views
            if view.cut_apart
        ]
        places = [(view.heading, view.extent, view.heading_box or view.extent) for view in found]
        if not places:
            notes.append("a vendor drawing shows no view code can find: read as one view")
            places = [(None, panel_box, panel_box)]
        for heading, extent, scale_at in places:
            ppi, reason = _vendor_scale(page.chars, scale_at, paste, settings)
            spans = drawn_dimensions(
                page.ink,
                extent,
                rows=settings.vendor_rows,
                settings=settings.dimensions,
                place=page.place,
            )
            vendor.append(
                VendorViewDimensions(
                    number=len(vendor) + 1,
                    panel_box=_moved_box(panel_box, dx, dtop),
                    heading=heading,
                    extent=_moved_box(extent, dx, dtop),
                    points_per_inch=ppi,
                    scale_reason=reason,
                    lines=tuple(_moved_span(span, dx, dtop) for span in labelled_lines(spans)),
                    kind=_kind(heading) or (_kind(sheet_heading) if len(places) == 1 else None),
                )
            )
    if not panels:
        notes.append("no drawing on this page is named the vendor's by its heading")
    return ComparePage(page_index, tuple(views), tuple(vendor), tuple(notes))
