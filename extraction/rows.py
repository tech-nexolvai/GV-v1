"""The dimension rows of one page, from the flattened stamp layer, placed in stored coordinates.

This is the function the worker will call in Phase 4: `countertop_row_candidates`. It owns the I/O
and the coordinate frame — flattening the page's pasted drawings with `stamp_text.stamps_only`,
reading the result with pdfplumber, keeping only the drawing's black and grey ink, turning
pdfplumber's floats into exact `Decimal`s, and placing every box through the same `page_frame`
placement every other consumer of stored coordinates uses — and hands the geometry to
`extraction/geometry/rows.py`, which has no I/O at all.

**Why the flattened layer and pdfplumber.** The client's drawings are `/Stamp` annotations whose
appearance streams nest Form XObjects; the pypdfium2 reader in `extraction/annotations.py` stops at
the first form (#981). pdfplumber walks into nested forms, and flattening first means the reviewer's
notes — a separate layer — are not on the page at all. Colour is the second guard: a mark the
reviewer drew in red or yellow that survived flattening is dropped by `drawing_ink`'s rule before
the builder sees it, so GV's ink can never become a tick, a row or a label.

**No AI here, and no value.** Nothing in this module or the builder reads a number. Phase 4 adds the
slot crops and the two readers; this phase gives it the slots.

Source: issue #980 · Verification: `tests/extraction/test_rows.py`
"""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pdfplumber

from evidence.coordinates import PdfPoint, StoredPoint
from extraction.geometry.rows import (
    Box,
    InkCharacter,
    InkCurve,
    InkLine,
    PageInk,
    PageRows,
    RowSettings,
    build_rows,
)
from extraction.reader import UnreadablePdf, page_frame, pixel_placement
from extraction.stamp_text import drawing_ink, path_ink, stamps_only

__all__ = [
    "PageRowCandidates",
    "RowsAndInk",
    "countertop_row_candidates",
    "ink_from_page",
    "page_rows_and_ink",
]


@dataclass(frozen=True, slots=True)
class PageRowCandidates:
    """One page's rows, and what they were built from."""

    page_index: int
    rows: PageRows
    drawings: int
    """How many pasted drawings the page has. Zero means no stamp was found: the rows, if any, come
    from a page whose line-work is in its own content — not the client's shape, worth knowing."""


def _decimal(value: object) -> Decimal:
    """A pdfplumber number as an exact Decimal, through `str` so a float's binary tail is not kept."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("a coordinate cannot be a bool")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    raise TypeError(f"not a pdfplumber number: {value!r}")


def _box(obj: dict[str, Any]) -> Box:
    x0, x1 = _decimal(obj["x0"]), _decimal(obj["x1"])
    top, bottom = _decimal(obj["top"]), _decimal(obj["bottom"])
    return Box(min(x0, x1), min(top, bottom), max(x0, x1), max(top, bottom))


def _points(obj: dict[str, Any]) -> tuple[tuple[Decimal, Decimal], ...]:
    """A path's points in pdfplumber's frame — the `pts` it gives, already measured from the top."""
    pts = obj.get("pts") or ()
    return tuple((_decimal(x), _decimal(top)) for x, top in pts)


def _is_ink(obj: dict[str, Any]) -> bool:
    """`path_ink`'s rule, with a path that states neither stroke nor fill taken as drawn."""
    if obj.get("stroke") or obj.get("fill"):
        return path_ink(obj)
    return drawing_ink({"non_stroking_color": obj.get("stroking_color")})


def ink_from_page(page: Any, *, drawing_boxes: Sequence[Box]) -> PageInk:
    """A pdfplumber page's black and grey ink, as the builder takes it.

    Lines, rectangles and curves become `InkLine`s and `InkCurve`s; a rectangle is a closed
    four-point curve. Characters keep their text and box. Anything coloured — by `drawing_ink`'s
    rule, which is the GV-mark rule the rest of the product uses — is left out here.
    """
    lines: list[InkLine] = []
    curves: list[InkCurve] = []
    for line in page.lines:
        if not _is_ink(line):
            continue
        pts = _points(line)
        if len(pts) == 2:
            (x0, y0), (x1, y1) = pts
        else:
            box = _box(line)
            x0, y0, x1, y1 = box.x0, box.top, box.x1, box.bottom
        lines.append(InkLine(x0=x0, y0=y0, x1=x1, y1=y1))
    for rect in page.rects:
        if not _is_ink(rect):
            continue
        box = _box(rect)
        curves.append(
            InkCurve(
                box=box,
                points=(
                    (box.x0, box.top),
                    (box.x1, box.top),
                    (box.x1, box.bottom),
                    (box.x0, box.bottom),
                ),
                closed=True,
            )
        )
    for curve in page.curves:
        if not _is_ink(curve):
            continue
        pts = _points(curve)
        if not pts:
            continue
        curves.append(InkCurve(box=_box(curve), points=pts, closed=False))
    characters = tuple(
        InkCharacter(text=str(char.get("text", "")), box=_box(char))
        for char in page.chars
        if drawing_ink(char)
    )
    return PageInk(
        width=_decimal(page.width),
        height=_decimal(page.height),
        lines=tuple(lines),
        curves=tuple(curves),
        characters=characters,
        drawing_boxes=tuple(drawing_boxes),
    )


def _stamp_boxes(data: bytes, page_index: int) -> tuple[Box, ...]:
    """The pasted drawings' rectangles on the original page, in pdfplumber's frame."""
    with pdfplumber.open(io.BytesIO(data)) as document:
        try:
            page = document.pages[page_index]
        except IndexError as error:
            raise UnreadablePdf(
                f"page {page_index} is beyond the {len(document.pages)} pages in this document"
            ) from error
        boxes: list[Box] = []
        for annotation in page.annots:
            raw = annotation.get("data") or {}
            subtype = raw.get("Subtype")
            if getattr(subtype, "name", str(subtype)) != "Stamp":
                continue
            boxes.append(_box(annotation))
        return tuple(boxes)


@dataclass(frozen=True, slots=True)
class RowsAndInk:
    """One page's rows, with the ink they were built from and the page's pixel placement.

    For a caller that needs to look again at the same strokes the rows came from — Phase 4's slot
    reader assembles each label's whole run of strokes and cuts its crop (#987) — without
    flattening and reading the page a second time, and so without a second copy that could
    disagree with the first.
    """

    candidates: PageRowCandidates
    ink: PageInk
    """The page's black and grey ink, in pdfplumber's frame (page points, `top` downward)."""
    to_pixels: Callable[[Decimal, Decimal], tuple[int, int]]
    """Where a pdfplumber `(x, top)` lands in the page's pixels at the dpi asked for: the placement
    `extraction/ink.py` places every word by, so a crop and the ink check share one frame."""


def page_rows_and_ink(
    data: bytes, page_index: int, *, dpi: int, settings: RowSettings
) -> RowsAndInk:
    """`countertop_row_candidates`, also handing back the ink and the pixel placement it used.

    Raises `UnreadablePdf` exactly where `countertop_row_candidates` does.
    """
    drawings = _stamp_boxes(data, page_index)
    flattened = stamps_only(data, page_index)
    try:
        with pdfplumber.open(io.BytesIO(flattened)) as document:
            page = document.pages[page_index]
            transform, height = page_frame(page, dpi)
            ink = ink_from_page(page, drawing_boxes=drawings)
            pixel = pixel_placement(page, dpi)
    except UnreadablePdf:
        raise
    except Exception as error:
        raise UnreadablePdf(
            f"page {page_index}'s pasted drawings could not be read for their rows: {error}"
        ) from error

    def place(x: Decimal, top: Decimal) -> StoredPoint:
        return transform.to_stored(transform.to_image(PdfPoint(x=x, y=height - top)))

    def to_pixels(x: Decimal, top: Decimal) -> tuple[int, int]:
        point = pixel(x, top)
        return (point.x, point.y)

    return RowsAndInk(
        candidates=PageRowCandidates(
            page_index=page_index,
            rows=build_rows(ink, settings, place=place),
            drawings=len(drawings),
        ),
        ink=ink,
        to_pixels=to_pixels,
    )


def countertop_row_candidates(
    data: bytes, page_index: int, *, dpi: int, settings: RowSettings
) -> PageRowCandidates:
    """Every dimension row on `page_index` of `data`, built from its pasted drawings' own lines.

    Args:
        data: the whole PDF, as uploaded. Never modified.
        page_index: zero-based.
        dpi: the image grid stored coordinates are placed through — the same `dpi` the page was
            rendered at, so a slot box lands where the render shows the slot.
        settings: every threshold, written down. `MEASURED_SETTINGS` is E1's.

    Raises:
        UnreadablePdf: when the page cannot be flattened or read. Never returns an empty result in
            place of a refusal: a page with no rows and a page that could not be read are different
            answers.
    """
    return page_rows_and_ink(data, page_index, dpi=dpi, settings=settings).candidates
