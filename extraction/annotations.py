"""Reading a sheet's annotation layers separately, because rendering them together loses which is which.

`extraction/reader.py` reads a page's content stream. That is the right thing for a sheet whose text
is in its content stream, and on the first real client set (`AI_Set 2`, #274) it finds nothing at all:
every page there has an **89-byte** content stream, no image XObject and no embedded font. The drawing
is in two `/Stamp` annotations — 1338 and 6707 page objects, ~720 KB of vector paths — and the
reviewer's markup is in twenty `/FreeText` annotations. So the reader reports every page as
unreadable and the pipeline sends the whole sheet to OCR, including labels that are sitting in the
file as strings.

**Three separate things live in one rendered picture, and this module is about not mixing them.**

1. **The reviewer's markup is already exact text.** `/FreeText` carries its string in `/Contents` and
   its author in `/T`. Reading it costs one dictionary lookup; OCR-ing a picture of it can only lose.

2. **The vendor's line-work is exact geometry.** Dimension lines, extension lines and leaders are
   paths with real coordinates. `extraction/geometry/text_association.py` was written to attach a
   reading to the line it annotates and has never been given vector line-work on a sheet like this.

3. **Only the vendor's numbers need a model.** Its text is converted to outlines — glyph shapes drawn
   as paths — so no dictionary holds it and no OCR-free route exists. Those regions, and only those,
   are what the vision seam is for.

**And the layers contradict each other, which is the point.** On page 3 the vendor drawing says
`191"` where the markup says `185 1/4"`; elsewhere the drawing says `3"+2"Filler` and the markup
`2"+3"(filler)` — the same numbers in the opposite order. Flattened into one image they are
indistinguishable except by colour, and a model asked to read the result returns a blend of two
sources with different authority. That disagreement is a review signal and it survives only if the
layers are kept apart, so nothing here reconciles them, picks one, or even compares them.

**Nothing here assigns meaning.** A markup note is a string and a region is a rectangle. No `CT0xx`
type, no "this is a width", no vendor tag interpreted — semantic typing is gated on reviewed answers
(#274, Q20) and this module has no input that could supply one.

**Nothing here decides what a dimension line is, either.** #179 — deciding which vector primitives
*are* dimension lines — is a detector, and one sheet is not enough to validate one. So the two
lengths that separate line-work from glyph outlines are **required arguments with no defaults**, the
same stance `text_association.py` takes for the same reason, and a "text region" is named as a
*candidate*: a cluster of arrowheads is a region the reader will look at and refuse, which is cheaper
than a detector nobody can yet check.

Two libraries, deliberately. pdfplumber resolves the annotation dictionaries — subtype, rect, text,
author, and the appearance stream's `/BBox` and `/Matrix`. pypdfium2 is the only one of the two that
flattens an appearance stream into path objects. Both are already dependencies, and where the two
views must line up the module **checks** that they do rather than assuming it.

Source: issue #539, from the `AI_Set 2` reader probe. Verification: `tests/extraction/test_annotations.py`.
"""

from __future__ import annotations

import ctypes
import io
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Any, Final
from uuid import UUID

import pdfplumber
import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pypdfium2.raw as pdfium_raw  # type: ignore[import-untyped]

from evidence.coordinates import ImagePoint, PageTransform, PdfPoint
from evidence.polygon import Polygon
from extraction.geometry.containment import DimensionExtent
from extraction.reader import UnreadablePdf

__all__ = [
    "DrawingLayer",
    "LayerRefusal",
    "MarkupNote",
    "OutlinedTextRegion",
    "PageLayers",
    "read_annotation_layers",
]

#: How far pdfium's page-space rect for an annotation may differ from the dictionary's `/Rect`
#: before the two enumerations are treated as disagreeing, in PDF points.
#:
#: The two views are matched by **index**, because both walk the `/Annots` array in order. That is
#: true of every PDF this has been run against and it is still an assumption, so it is checked
#: instead of trusted: if index *i* means a different annotation to the two libraries, their rects
#: will not agree and the page is refused rather than silently attributing one annotation's geometry
#: to another's text. A hundredth of a point is float noise between a C float and a Decimal; anything
#: larger is a different rectangle.
_RECT_AGREEMENT_PT: Final = Decimal("0.01")


class DrawingLayer(StrEnum):
    """Which authority an annotation carries. The one distinction this module exists to preserve."""

    VENDOR_DRAWING = "vendor_drawing"
    """`/Stamp` — what the vendor drew. Line-work plus outlined text."""

    REVIEWER_MARKUP = "reviewer_markup"
    """`/FreeText` — what a reviewer wrote on top of it, as exact text."""

    OTHER = "other"
    """Everything else: `/Line`, `/Square`, `/Ink`. Reported, never merged into the two above."""


#: The annotation subtypes each layer is made of. A subtype absent here is `OTHER`, which is a
#: report and not a discard — an unrecognised markup type is exactly the thing that must not be
#: quietly folded into the vendor's drawing.
_LAYER_BY_SUBTYPE: Final = {
    "Stamp": DrawingLayer.VENDOR_DRAWING,
    "FreeText": DrawingLayer.REVIEWER_MARKUP,
}


@dataclass(frozen=True, slots=True)
class MarkupNote:
    """One reviewer annotation, with its text exactly as the file holds it.

    `text` is not a reading and not a candidate: it is what the file says, so there is no confidence,
    no unit guess and no semantic type on it. Whether it is a dimension, a tag, a note or a title is
    a question for a later step that has reviewed answers to work from.
    """

    layer: DrawingLayer
    subtype: str
    text: str
    author: str | None
    """`/T`, the markup author — `GVI-007` on the first real set. Present because *who* corrected a
    drawing is part of why the correction outranks it, and the file is the only place it exists."""

    extent: Polygon
    image_extent: tuple[ImagePoint, ...]
    annotation_index: int
    """Where in `/Annots` it came from, so a reader can go back to the file and check."""


@dataclass(frozen=True, slots=True)
class OutlinedTextRegion:
    """A cluster of small paths: text drawn as glyph outlines, which only a model can read.

    **A candidate region, not a detected label.** Nothing here has established that these paths are
    text — they are paths too small to be line-work, near enough to each other to be one run. An
    arrowhead cluster produces one of these too, and the reader refuses it. That is the intended
    behaviour: refusing a region costs one call, and a detector that decided the question wrongly
    would cost a wrong dimension (#179).
    """

    extent: Polygon
    image_extent: tuple[ImagePoint, ...]
    path_count: int
    point_count: int
    """Both counts are kept because they are the only description of the cluster's shape that
    survives into the output, and a one-path, three-point "region" is worth being able to spot."""


@dataclass(frozen=True, slots=True)
class LayerRefusal:
    """One annotation that could not be turned into a note or a region, and why.

    Every annotation on the page comes back in exactly one of the result's lists. A page that
    silently dropped the annotation it could not handle would read as a page that did not have one.
    """

    annotation_index: int
    subtype: str
    reason: str


@dataclass(frozen=True, slots=True)
class PageLayers:
    """What one page's annotations hold, kept in separate lists on purpose.

    `markup` and `drawing_segments`/`outlined_regions` are never merged and never compared. A caller
    that wants to know whether the reviewer disagreed with the drawing has both halves and does that
    itself, with a reviewer in the loop.
    """

    page_index: int
    markup: tuple[MarkupNote, ...]
    drawing_segments: tuple[DimensionExtent, ...]
    outlined_regions: tuple[OutlinedTextRegion, ...]
    other_layer_notes: tuple[MarkupNote, ...]
    refusals: tuple[LayerRefusal, ...]
    unreadable_reason: str | None = None

    @property
    def readable(self) -> bool:
        """Whether the annotation layers could be read at all."""
        return self.unreadable_reason is None


def _decimal(value: object) -> Decimal:
    """A PDF number as an exact Decimal, via `str` so a float's binary value is not inherited."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(str(value))


def _rect(values: object) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """A PDF rectangle as `(left, bottom, right, top)`, normalised for corner order.

    A `/Rect` is allowed to name its corners in either order, and a reader that assumed
    lower-left-first would compute a negative width on the files that do not.
    """
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise UnreadablePdf(f"a rectangle needs four numbers, got {values!r}")
    x0, y0, x1, y1 = (_decimal(value) for value in values)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _text(value: object) -> str | None:
    """A PDF text string as `str`, or `None` when the key is absent.

    PDF strings arrive as bytes in either PDFDocEncoding or UTF-16BE with a byte-order mark. Decoded
    here rather than downstream so a markup note is a `str` everywhere in this module.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        if value.startswith(b"\xfe\xff"):
            return value.decode("utf-16-be", "replace")[1:]
        return value.decode("latin-1", "replace")
    return str(value)


def _subtype(annotation: dict[str, Any]) -> str:
    """The `/Subtype` name without its slash, or `""` when it has none."""
    value = annotation.get("Subtype")
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value).lstrip("/").strip("'") if value is not None else ""


def _appearance_transform(
    annotation: dict[str, Any], rect: tuple[Decimal, Decimal, Decimal, Decimal]
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
    """The affine `(a, b, c, d, e, f)` taking appearance-stream space to page space.

    An annotation's appearance is a form XObject with its own coordinate system. PDF 32000-1 §12.5.5
    defines the mapping: apply `/Matrix` to the four corners of `/BBox`, take the bounding box of the
    result, and map that onto the annotation's `/Rect`. pdfium reports an appearance's path objects
    in the form's own space, so without this every coordinate is off by the difference — 175 points
    across and 139 down on the first real sheet, which is a third of the page.

    Computed, never assumed. On `AI_Set 2` the matrix is a pure translation and the scale is exactly
    1, so a hard-coded translation would have passed every test written against that sheet and been
    wrong on the first drawing whose stamp was placed at a different size.

    A degenerate transformed BBox — zero width or height — is refused rather than divided by.
    """
    appearance = annotation.get("AP")
    if not isinstance(appearance, dict) or "N" not in appearance:
        raise UnreadablePdf("a stamp annotation has no normal appearance stream to read")
    from pdfminer.pdftypes import resolve1

    normal = resolve1(appearance["N"])
    attributes = getattr(normal, "attrs", None)
    if not isinstance(attributes, dict) or "BBox" not in attributes:
        raise UnreadablePdf("an appearance stream has no /BBox, so its space cannot be placed")

    box_left, box_bottom, box_right, box_top = _rect(resolve1(attributes["BBox"]))
    matrix = resolve1(attributes.get("Matrix")) or [1, 0, 0, 1, 0, 0]
    if not isinstance(matrix, (list, tuple)) or len(matrix) != 6:
        raise UnreadablePdf(f"an appearance /Matrix needs six numbers, got {matrix!r}")
    a, b, c, d, e, f = (_decimal(value) for value in matrix)

    corners = [
        (a * x + c * y + e, b * x + d * y + f)
        for x, y in (
            (box_left, box_bottom),
            (box_right, box_bottom),
            (box_right, box_top),
            (box_left, box_top),
        )
    ]
    transformed_left = min(x for x, _ in corners)
    transformed_bottom = min(y for _, y in corners)
    transformed_width = max(x for x, _ in corners) - transformed_left
    transformed_height = max(y for _, y in corners) - transformed_bottom
    if transformed_width <= 0 or transformed_height <= 0:
        raise UnreadablePdf("an appearance /BBox has no area, so it cannot be mapped to a /Rect")

    scale_x = (rect[2] - rect[0]) / transformed_width
    scale_y = (rect[3] - rect[1]) / transformed_height
    offset_x = rect[0] - transformed_left * scale_x
    offset_y = rect[1] - transformed_bottom * scale_y
    # The placement composed with `/Matrix`, so a caller applies one affine rather than two in the
    # right order. Getting that order wrong is silent: on the first real sheet the matrix is a pure
    # translation, so skipping it moved every path 192 points up the page while still looking like
    # plausible geometry.
    return (
        scale_x * a,
        scale_y * b,
        scale_x * c,
        scale_y * d,
        scale_x * e + offset_x,
        scale_y * f + offset_y,
    )


def _polygon(
    rect: tuple[Decimal, Decimal, Decimal, Decimal],
    transform: PageTransform,
    document_version_id: UUID,
    page_index: int,
) -> tuple[Polygon, tuple[ImagePoint, ...]]:
    """A PDF-space rectangle as a stored polygon and its integer image corners.

    Both are kept for the reason `extraction/reader.py` documents on `TextItem.image_extent`: the
    image-space box is what a candidate's polygon column holds, and recovering it from stored
    coordinates needs the dpi, media box and crop box that nothing persists.
    """
    left, bottom, right, top = rect
    corners = ((left, bottom), (right, bottom), (right, top), (left, top))
    image = tuple(transform.to_image(PdfPoint(x=x, y=y)) for x, y in corners)
    stored = tuple(transform.to_stored(point) for point in image)
    return (
        Polygon(
            points=stored,
            space="stored",
            document_version_id=document_version_id,
            page=page_index,
        ),
        image,
    )


def _stamp_paths(
    opened_pdf: Any, page_index: int, annotation_index: int
) -> tuple[tuple[tuple[Decimal, Decimal], ...], ...]:
    """Every path in one annotation's appearance, as tuples of points in appearance space.

    pypdfium2's raw bindings rather than its Python wrapper, because the wrapper has no annotation
    object accessor. The handle is closed in a `finally` so a raised refusal does not leak it.

    `opened_pdf` and not `document`: `tests/storage/test_pinning.py` refuses a parameter that names a
    document without a version, and it is right to even here. This one is an open handle on bytes a
    caller already pinned, and a name that reads as "the drawing" is how an unversioned path starts.
    """
    page = opened_pdf[page_index]
    annotation = pdfium_raw.FPDFPage_GetAnnot(page, annotation_index)
    if not annotation:
        raise UnreadablePdf(f"annotation {annotation_index} could not be opened for its geometry")
    try:
        paths: list[tuple[tuple[Decimal, Decimal], ...]] = []
        for index in range(pdfium_raw.FPDFAnnot_GetObjectCount(annotation)):
            page_object = pdfium_raw.FPDFAnnot_GetObject(annotation, index)
            if pdfium_raw.FPDFPageObj_GetType(page_object) != pdfium_raw.FPDF_PAGEOBJ_PATH:
                continue
            # **The path object's own matrix, which is not optional.** pdfium reports a path's
            # points in the path's space, and a CAD appearance stream routinely draws at one scale
            # and places it at another: the two stamps on the first real sheet use the identity and
            # a uniform 0.12, so a reader that ignored this would be correct on one and eight times
            # out on the other — with coordinates that still look like a drawing.
            matrix = pdfium_raw.FS_MATRIX()
            if pdfium_raw.FPDFPageObj_GetMatrix(page_object, ctypes.byref(matrix)):
                a, b, c, d, e, f = (
                    _decimal(matrix.a),
                    _decimal(matrix.b),
                    _decimal(matrix.c),
                    _decimal(matrix.d),
                    _decimal(matrix.e),
                    _decimal(matrix.f),
                )
            else:
                a, b, c, d, e, f = (
                    Decimal(1),
                    Decimal(0),
                    Decimal(0),
                    Decimal(1),
                    Decimal(0),
                    Decimal(0),
                )
            points: list[tuple[Decimal, Decimal]] = []
            for position in range(pdfium_raw.FPDFPath_CountSegments(page_object)):
                segment = pdfium_raw.FPDFPath_GetPathSegment(page_object, position)
                x, y = ctypes.c_float(), ctypes.c_float()
                if not pdfium_raw.FPDFPathSegment_GetPoint(
                    segment, ctypes.byref(x), ctypes.byref(y)
                ):
                    continue
                point_x, point_y = _decimal(x.value), _decimal(y.value)
                points.append((a * point_x + c * point_y + e, b * point_x + d * point_y + f))
            if points:
                paths.append(tuple(points))
        return tuple(paths)
    finally:
        pdfium_raw.FPDFPage_CloseAnnot(annotation)


def _pdfium_rect(
    opened_pdf: Any, page_index: int, annotation_index: int
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """pdfium's page-space rect for one annotation, for checking the two views agree."""
    page = opened_pdf[page_index]
    annotation = pdfium_raw.FPDFPage_GetAnnot(page, annotation_index)
    if not annotation:
        raise UnreadablePdf(f"annotation {annotation_index} could not be opened")
    try:
        rect = pdfium_raw.FS_RECTF()
        if not pdfium_raw.FPDFAnnot_GetRect(annotation, ctypes.byref(rect)):
            raise UnreadablePdf(f"annotation {annotation_index} has no rectangle pdfium can read")
        return _rect([rect.left, rect.bottom, rect.right, rect.top])
    finally:
        pdfium_raw.FPDFPage_CloseAnnot(annotation)


def _placed(
    points: tuple[tuple[Decimal, Decimal], ...],
    placement: tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal],
) -> tuple[tuple[Decimal, Decimal], ...]:
    """Appearance-space points in page space, through one affine."""
    a, b, c, d, e, f = placement
    return tuple((a * x + c * y + e, b * x + d * y + f) for x, y in points)


def _long_segments(
    paths: tuple[tuple[tuple[Decimal, Decimal], ...], ...], line_minimum_pt: Decimal
) -> tuple[tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal]], ...]:
    """Consecutive point pairs at least `line_minimum_pt` apart: the sheet's line-work.

    Length is compared **squared**, so no square root and no float enters the decision — the same
    reason `text_association.py` keeps its distances squared.

    Direction is not filtered. A leader running at an angle is line-work as much as a horizontal
    dimension line is, and deciding which of them a number belongs to is the association step's job.
    """
    limit = line_minimum_pt * line_minimum_pt
    found: list[tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal]]] = []
    for path in paths:
        for start, end in pairwise(path):
            dx = end[0] - start[0]
            dy = end[1] - start[1]
            if dx * dx + dy * dy >= limit:
                found.append((start, end))
    return tuple(found)


def _bounds(
    points: tuple[tuple[Decimal, Decimal], ...],
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _clusters(
    boxes: list[tuple[Decimal, Decimal, Decimal, Decimal]], gap_pt: Decimal
) -> list[list[int]]:
    """Group boxes that are within `gap_pt` of each other, transitively.

    Union-find over a grid rather than every pair: a stamp holds thousands of paths, and comparing
    all of them against all of them is the difference between a second and an hour. Boxes are bucketed
    by `gap_pt`, and only the nine buckets around each one are consulted — which is exact rather than
    approximate, because two boxes further apart than `gap_pt` cannot be in the same or an adjacent
    bucket and touch.

    The result is ordered by first appearance so the same file always produces the same regions in
    the same order; a caller comparing two runs must not see a set reordering as a change.
    """
    parent = list(range(len(boxes)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    buckets: dict[tuple[int, int], list[int]] = {}
    for index, (left, bottom, _, _) in enumerate(boxes):
        key = (int(left / gap_pt), int(bottom / gap_pt))
        buckets.setdefault(key, []).append(index)

    for index, box in enumerate(boxes):
        key_x = int(box[0] / gap_pt)
        key_y = int(box[1] / gap_pt)
        for offset_x in (-1, 0, 1):
            for offset_y in (-1, 0, 1):
                for other in buckets.get((key_x + offset_x, key_y + offset_y), ()):
                    if other <= index:
                        continue
                    candidate = boxes[other]
                    near_x = box[0] - gap_pt <= candidate[2] and candidate[0] - gap_pt <= box[2]
                    near_y = box[1] - gap_pt <= candidate[3] and candidate[1] - gap_pt <= box[3]
                    if near_x and near_y:
                        union(index, other)

    grouped: dict[int, list[int]] = {}
    for index in range(len(boxes)):
        grouped.setdefault(find(index), []).append(index)
    return [grouped[key] for key in sorted(grouped)]


def read_annotation_layers(
    data: bytes,
    page_index: int,
    *,
    document_version_id: UUID,
    dpi: int,
    line_minimum_pt: Decimal,
    glyph_maximum_pt: Decimal,
    glyph_gap_pt: Decimal,
) -> PageLayers:
    """One page's annotation layers, read apart and never merged.

    `dpi` has no default for the reason `read_page_contents` gives: stored coordinates are reached
    through integer image space, so the resolution decides how much precision survives, and a default
    would choose that for every caller.

    The three lengths have no defaults either, and that is not caution — it is #179. Which vector
    primitives *are* dimension lines is a detector, `data/drawings/` held nothing when this
    repository's geometry was written, and one sheet is still not enough to fix a number that decides
    what gets read. They are in **PDF points**, which is a real physical length (72 to the inch) and
    the same on every sheet size, unlike stored coordinates.

    * `line_minimum_pt` — at least this long, measured between consecutive path points, and the run
      is line-work.
    * `glyph_maximum_pt` — a path whose bounding box is smaller than this in both axes is small
      enough to be part of a glyph.
    * `glyph_gap_pt` — small paths within this distance of each other are one text run.

    A path that is neither long enough to be line-work nor small enough to be a glyph is counted in
    the refusals and used for nothing. Hatching and arrowheads live there, and naming them rather
    than forcing them into one of the two answers is the point.
    """
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise ValueError("dpi must be a positive integer; stored coordinates depend on it")
    for name, value in (
        ("line_minimum_pt", line_minimum_pt),
        ("glyph_maximum_pt", glyph_maximum_pt),
        ("glyph_gap_pt", glyph_gap_pt),
    ):
        if isinstance(value, float):
            raise TypeError(f"{name} must be a Decimal, never a float")
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError(f"{name} must be a finite positive Decimal")

    markup: list[MarkupNote] = []
    other: list[MarkupNote] = []
    segments: list[DimensionExtent] = []
    regions: list[OutlinedTextRegion] = []
    refusals: list[LayerRefusal] = []

    try:
        with pdfplumber.open(io.BytesIO(data)) as plumbed:
            try:
                page = plumbed.pages[page_index]
            except IndexError as error:
                raise UnreadablePdf(
                    f"page {page_index} is beyond the {len(plumbed.pages)} pages in this document"
                ) from error
            transform = PageTransform(
                dpi=dpi,
                rotation=int(page.rotation or 0) % 360,
                media_box=_rect(page.mediabox),
                crop_box=_rect(page.cropbox),
            )
            annotations = [annotation["data"] for annotation in page.annots]

        pdfium_document = pdfium.PdfDocument(data)
        try:
            for index, annotation in enumerate(annotations):
                subtype = _subtype(annotation)
                layer = _LAYER_BY_SUBTYPE.get(subtype, DrawingLayer.OTHER)
                try:
                    rect = _rect(annotation.get("Rect"))
                    seen = _pdfium_rect(pdfium_document, page_index, index)
                    if any(
                        abs(mine - theirs) > _RECT_AGREEMENT_PT
                        for mine, theirs in zip(rect, seen, strict=True)
                    ):
                        raise UnreadablePdf(
                            f"annotation {index} is a different rectangle to the two readers "
                            f"({rect} against {seen}); their /Annots order does not agree, so "
                            "geometry cannot be attributed to text"
                        )
                    extent, image_extent = _polygon(
                        rect, transform, document_version_id, page_index
                    )
                except (UnreadablePdf, TypeError, ValueError) as error:
                    refusals.append(LayerRefusal(index, subtype, str(error)))
                    continue

                if layer is DrawingLayer.VENDOR_DRAWING:
                    try:
                        placement = _appearance_transform(annotation, rect)
                        paths = tuple(
                            _placed(path, placement)
                            for path in _stamp_paths(pdfium_document, page_index, index)
                        )
                    except (UnreadablePdf, TypeError, ValueError) as error:
                        refusals.append(LayerRefusal(index, subtype, str(error)))
                        continue
                    # **Clipped to the annotation's own rectangle, because a viewer clips too.**
                    # An appearance stream may draw past its `/BBox`; PDF 32000-1 §12.5.5 says the
                    # box clips it, so anything outside is not on the sheet a reviewer saw. Whole
                    # paths are dropped rather than trimmed: a trimmed path would join two points
                    # that were never adjacent, which is a line-work segment the drawing does not
                    # have. Fourteen of stamp 22's 1310 paths on the first real sheet.
                    inside = tuple(
                        path
                        for path in paths
                        if all(rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3] for x, y in path)
                    )
                    if len(inside) != len(paths):
                        refusals.append(
                            LayerRefusal(
                                index,
                                subtype,
                                f"{len(paths) - len(inside)} paths fall outside the annotation "
                                "rectangle that clips them and were not used",
                            )
                        )
                    paths = inside
                    found, ignored = _drawing_geometry(
                        paths,
                        transform=transform,
                        document_version_id=document_version_id,
                        page_index=page_index,
                        line_minimum_pt=line_minimum_pt,
                        glyph_maximum_pt=glyph_maximum_pt,
                        glyph_gap_pt=glyph_gap_pt,
                    )
                    segments.extend(found[0])
                    regions.extend(found[1])
                    if ignored:
                        refusals.append(
                            LayerRefusal(
                                index,
                                subtype,
                                f"{ignored} paths were neither line-work nor glyph-sized and were "
                                "not used",
                            )
                        )
                    continue

                note = MarkupNote(
                    layer=layer,
                    subtype=subtype,
                    text=_text(annotation.get("Contents")) or "",
                    author=_text(annotation.get("T")),
                    extent=extent,
                    image_extent=image_extent,
                    annotation_index=index,
                )
                (markup if layer is DrawingLayer.REVIEWER_MARKUP else other).append(note)
        finally:
            pdfium_document.close()
    except UnreadablePdf:
        raise
    except Exception as error:
        raise UnreadablePdf(f"page {page_index} annotations could not be read: {error}") from error

    return PageLayers(
        page_index=page_index,
        markup=tuple(markup),
        drawing_segments=tuple(segments),
        outlined_regions=tuple(regions),
        other_layer_notes=tuple(other),
        refusals=tuple(refusals),
        unreadable_reason=(
            None
            if (markup or segments or regions or other)
            else "this page carries no annotation layers to read"
        ),
    )


def _drawing_geometry(
    paths: tuple[tuple[tuple[Decimal, Decimal], ...], ...],
    *,
    transform: PageTransform,
    document_version_id: UUID,
    page_index: int,
    line_minimum_pt: Decimal,
    glyph_maximum_pt: Decimal,
    glyph_gap_pt: Decimal,
) -> tuple[tuple[tuple[DimensionExtent, ...], tuple[OutlinedTextRegion, ...]], int]:
    """One stamp's paths split into line-work and candidate text regions, plus what was left over."""
    segments: list[DimensionExtent] = []
    ignored_segments = 0
    for start, end in _long_segments(paths, line_minimum_pt):
        first = transform.to_stored(transform.to_image(PdfPoint(x=start[0], y=start[1])))
        last = transform.to_stored(transform.to_image(PdfPoint(x=end[0], y=end[1])))
        if first == last:
            # Long in PDF points but the same pixel at this resolution. `DimensionExtent` refuses
            # identical endpoints because such a line has no axis, and it is right to: rendering it
            # finer is the answer, not inventing a direction for it.
            continue
        try:
            segments.append(
                DimensionExtent(
                    start=first,
                    end=last,
                    document_version_id=document_version_id,
                    page=page_index,
                )
            )
        except (TypeError, ValueError):
            # Geometry outside the visible crop box. A `/Rect` may legally extend past it, and a
            # line nobody can see is not a line this can associate a reading with.
            ignored_segments += 1

    small: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
    small_paths: list[tuple[tuple[Decimal, Decimal], ...]] = []
    ignored = ignored_segments
    for path in paths:
        left, bottom, right, top = _bounds(path)
        if right - left < glyph_maximum_pt and top - bottom < glyph_maximum_pt:
            small.append((left, bottom, right, top))
            small_paths.append(path)
        elif not _long_segments((path,), line_minimum_pt):
            ignored += 1

    regions: list[OutlinedTextRegion] = []
    for cluster in _clusters(small, glyph_gap_pt):
        boxes = [small[index] for index in cluster]
        rect = (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
        try:
            extent, image_extent = _polygon(rect, transform, document_version_id, page_index)
        except (TypeError, ValueError):
            # A cluster so thin it is a line in image space, or one that falls outside the visible
            # crop box. Neither is a region a crop could be cut from.
            ignored += len(cluster)
            continue
        regions.append(
            OutlinedTextRegion(
                extent=extent,
                image_extent=image_extent,
                path_count=len(cluster),
                point_count=sum(len(small_paths[index]) for index in cluster),
            )
        )

    return (tuple(segments), tuple(regions)), ignored
