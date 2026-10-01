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

3. **Only the vendor's numbers need reading from shapes.** Its text is converted to outlines — glyph
   shapes drawn as paths — so no dictionary holds it. Those regions, and only those, are what the
   vision seam is for, and since #756 what `extraction/glyph_reader.py` reads from the paths
   themselves once a person has labelled each character's shape.

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
from dataclasses import dataclass, field
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
from extraction.glyph_bands import FractionBarGeometry, GlyphBox, stacked_fractions
from extraction.reader import UnreadablePdf, page_boxes_in_pdf_space

__all__ = [
    "DrawingLayer",
    "LayerRefusal",
    "MarkupNote",
    "OutlinedTextRegion",
    "PageLayers",
    "PathSegment",
    "SegmentKind",
    "StackedFraction",
    "VectorPath",
    "VendorStamp",
    "glyph_runs",
    "page_box_polygon",
    "read_annotation_layers",
    "read_markup_layer",
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

    VENDOR_TEXT = "vendor_text"
    """The vendor's own text, kept by its CAD program as exact strings: the invisible `/Square`
    annotations AutoCAD writes for every string drawn in an SHX font, titled `AutoCAD SHX Text`.

    AutoCAD's SHX fonts cannot be embedded in a PDF, so its export draws each string as strokes —
    the same unreadable shapes #756 exists to read — and, with `PDFSHX=1` (its default), also writes
    one of these notes per string holding the text and its rectangle. The strokes and the note say
    the same thing; the note says it exactly. It is the vendor's authority, never the reviewer's,
    and it is never folded into `markup`."""

    OTHER = "other"
    """Everything else: `/Line`, `/Square`, `/Ink`. Reported, never merged into the two above."""


class SegmentKind(StrEnum):
    """What one step of a vector path does, as the file states it (#756)."""

    MOVE = "move"
    """Lift the pen and start a new sub-path here. The two ticks of an inch mark are two of these."""

    LINE = "line"
    """A straight line from the previous point to this one."""

    BEZIER = "bezier"
    """One of the three points of a cubic Bézier: two control points, then the end point, in order.
    pdfium reports each as its own segment. A control point is not on the curve, so drawing
    through it would put a corner where the file has a curve."""

    UNKNOWN = "unknown"
    """A step pdfium could not name. Kept rather than dropped, so a shape with one is known to be
    incomplete and can be refused rather than drawn wrongly."""


@dataclass(frozen=True, slots=True)
class PathSegment:
    """One step of a path: what it does, where it ends, and whether it closes its sub-path."""

    kind: SegmentKind
    point: tuple[Decimal, Decimal]
    closes: bool


@dataclass(frozen=True, slots=True)
class VectorPath:
    """One path object from the vendor's drawing, with what it takes to draw it again (#756).

    **Why points alone were not enough.** A path used to be kept as its points, in order. That joins
    an inch mark's two ticks into one stroke, draws a Bézier through its control points, and cannot
    tell a filled outline from a stroked line — and on the client's drawing each character is one
    path object, so the path *is* the character's shape. A shape reader needs the shape.

    `points` is exactly the sequence the path used to be kept as, so everything that ran on points
    runs on the same numbers.
    """

    segments: tuple[PathSegment, ...]
    stroked: bool | None
    """Whether the path is drawn as a line. `None` where pdfium could not say, which a shape reader
    must treat as unknown rather than as either answer."""
    filled: bool | None
    """Whether the path is filled in. `None` as for `stroked`."""

    @property
    def points(self) -> tuple[tuple[Decimal, Decimal], ...]:
        return tuple(segment.point for segment in self.segments)

    def placed(
        self, placement: tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]
    ) -> VectorPath:
        """The same path in page space, through the stamp's placement affine."""
        a, b, c, d, e, f = placement
        return VectorPath(
            segments=tuple(
                PathSegment(
                    kind=segment.kind,
                    point=(
                        a * segment.point[0] + c * segment.point[1] + e,
                        b * segment.point[0] + d * segment.point[1] + f,
                    ),
                    closes=segment.closes,
                )
                for segment in self.segments
            ),
            stroked=self.stroked,
            filled=self.filled,
        )


#: pdfium's segment types, by the raw constant.
_SEGMENT_KINDS: Final = {
    pdfium_raw.FPDF_SEGMENT_MOVETO: SegmentKind.MOVE,
    pdfium_raw.FPDF_SEGMENT_LINETO: SegmentKind.LINE,
    pdfium_raw.FPDF_SEGMENT_BEZIERTO: SegmentKind.BEZIER,
}


#: The annotation subtypes each layer is made of. A subtype absent here is `OTHER`, which is a
#: report and not a discard — an unrecognised markup type is exactly the thing that must not be
#: quietly folded into the vendor's drawing.
_LAYER_BY_SUBTYPE: Final = {
    "Stamp": DrawingLayer.VENDOR_DRAWING,
    "FreeText": DrawingLayer.REVIEWER_MARKUP,
}

#: The title AutoCAD gives the note it writes for each SHX-font string. Matched exactly (ignoring
#: case and surrounding space) on `/T` or `/Subj`, never by resemblance: a reviewer's own `/Square`
#: box is a markup, and treating it as the vendor's text would give the reviewer's hand the vendor's
#: authority.
AUTOCAD_SHX_TEXT: Final = "autocad shx text"


def _layer_of(annotation: dict[str, Any], subtype: str) -> DrawingLayer:
    """Which authority an annotation carries: its subtype, except AutoCAD's own text notes."""
    if subtype == "Square" and any(
        (_text(annotation.get(key)) or "").strip().lower() == AUTOCAD_SHX_TEXT
        for key in ("T", "Subj")
    ):
        return DrawingLayer.VENDOR_TEXT
    return _LAYER_BY_SUBTYPE.get(subtype, DrawingLayer.OTHER)


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
    rotation_degrees: int
    """Which way the note reads, from the annotation's own `/Rotation`. **Read, never inferred.**

    `text_association.associate` filters candidate lines by whether they run the way the text does,
    so this decides which line a note can be attached to. On the first real sheet exactly two notes
    carry `270` and the rest carry nothing — and inferring it from the box would be worse than
    useless: a box around `102"` is taller than it is wide, and so is a box around `2"` that is not
    rotated at all. `DimensionText` refuses an inferred rotation for the same reason.

    Zero when the file does not say, which is the ordinary case and is what a viewer draws."""

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

    stacked_glyphs: bool = False
    """Whether this cluster touches a stacked fraction the bar detector found (#541, #735).

    Touches, not contains: the run a reader forms at a fraction usually holds only the numerator,
    because the bar and denominator sit below it and are orphaned by `_glyph_runs`. A crop cut round
    this region still shows the rest, and that is what a model reads.

    **`False` says nothing unless `PageLayers.fractions_read`.** A read that supplied no
    `FractionBarGeometry` never looked, and its regions are all `False` for that reason alone.
    """

    baseline_rotation_degrees: int = 0
    """How the appearance transform turns the text baseline on the rendered page.

    This is read from the stamp's placement matrix, not from the crop pixels. ``region_crop`` uses
    it to turn vertical labels upright before a reader sees them, and to invert reader rectangles
    back to the unrotated page.
    """

    glyph_paths: tuple[VectorPath, ...] = field(default=(), compare=False)
    """The paths this region was formed from, in page space (PDF points), in drawing order (#756).

    On the client's drawing one path object is one character, so these are the label's characters
    as the file draws them — what a shape reader reads, where a model reads the pixels. Not part of
    the region's equality: a region is the same region whatever its paths are compared by.
    """


@dataclass(frozen=True, slots=True)
class VendorStamp:
    """One vendor-drawing stamp on the page, and where it sits (#710).

    On the client's combined sheets each drawing — the ID set's elevation and the vendor's — is one
    stamp, so this is where a drawing panel is on the page. Only its place, read from the file's own
    rectangle: which of the two it is comes from the label the sheet prints above it
    (`extraction/panels.py`), and a person confirms that.
    """

    annotation_index: int
    extent: Polygon
    image_extent: tuple[ImagePoint, ...]


@dataclass(frozen=True, slots=True)
class StackedFraction:
    """Where the vendor drew a stacked fraction: bar, numerator and denominator together (#735).

    Kept on the page rather than only on a region because the regions do not hold it — the bar and
    the denominator were orphaned before any region was formed — and because what matters is whether
    a *crop* shows one, and crops are cut round other readers' boxes as well as these regions.
    """

    extent: Polygon
    image_extent: tuple[ImagePoint, ...]


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
    geometry_read: bool = True
    """Whether the vendor's path geometry was looked at at all.

    `read_markup_layer` leaves it `False`, and the distinction is not cosmetic: empty
    `drawing_segments` from a markup-only read means *nobody looked*, while empty segments from
    `read_annotation_layers` means *there is no line-work on this sheet*. Those are opposite facts
    and a caller that could not tell them apart would report a drawing as having no dimensions."""

    stacked_fractions: tuple[StackedFraction, ...] = ()
    """Every stacked fraction the bar detector found on the vendor's layer (#735)."""

    vendor_stamps: tuple[VendorStamp, ...] = ()
    """Every vendor-drawing stamp on the page, in `/Annots` order (#710). Filled by both reads."""

    vendor_text: tuple[MarkupNote, ...] = ()
    """The vendor's exact text as its CAD program kept it (`DrawingLayer.VENDOR_TEXT`). Filled by
    both reads, because reading it needs no threshold: the string and its rectangle are dictionary
    values. Empty on every page without an AutoCAD SHX export, which is most drawings today."""

    fractions_read: bool = False
    """Whether the bar detector ran. `False` makes an empty `stacked_fractions` mean *nobody looked*,
    the same distinction `geometry_read` draws for line-work."""

    glyph_paths: tuple[VectorPath, ...] = field(default=(), compare=False)
    """Every glyph-sized path on the vendor's layer, in page space, whether or not a region holds it
    (#756 phase C).

    A region holds only the paths `_glyph_runs` grouped, and that grouping advances along the
    stamp's baseline: a sideways label's characters and a stacked fraction's bar and denominator
    are left out of every region. A shape reader forms its own labels from all of them.
    """

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


#: The four rotations text can be set at. Anything else has no representation here — the same closed
#: set `text_association.TEXT_ROTATIONS` holds, and for the same reason: rounding an angle to the
#: nearest axis would decide which line a number belongs to.
_ROTATIONS: Final = (0, 90, 180, 270)


def _rotation_degrees(annotation: dict[str, Any]) -> int:
    """The annotation's stated rotation, normalised to 0, 90, 180 or 270.

    `/Rotation` is what Acrobat and its imitators write on a rotated `/FreeText`; `/Rotate` is the
    page-level spelling and is accepted here because some tools use it on annotations too. A negative
    or over-turn value is brought into range — `-90` and `270` are the same quarter turn — because
    that is arithmetic on a stated value, not a guess about an unstated one.

    **An angle that is not a quarter turn is refused, not rounded.** A note set at 45 degrees reads
    along no axis, and rounding it to the nearest one would pick which dimension line it belongs to.
    """
    stated = annotation.get("Rotation")
    if stated is None:
        stated = annotation.get("Rotate")
    if stated is None:
        return 0
    try:
        degrees = int(_decimal(stated))
    except (TypeError, ValueError, ArithmeticError) as error:
        raise UnreadablePdf(
            f"an annotation states a rotation that is not a number: {stated!r}"
        ) from error
    degrees %= 360
    if degrees not in _ROTATIONS:
        raise UnreadablePdf(
            f"an annotation is rotated {degrees} degrees, which reads along no axis. Rounding it to "
            "the nearest one would decide which dimension line its text belongs to."
        )
    return degrees


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


def _baseline_rotation_degrees(
    placement: tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal],
) -> int:
    """Return the quarter-turn of the appearance baseline from the placement matrix.

    The baseline is the appearance stream's positive x-axis after placement. Only axis-aligned
    quarter turns are representable in the crop mapper; a skewed appearance is refused rather than
    rounded into a direction it did not state.
    """
    a, b, _, _, _, _ = placement
    if a > 0 and b == 0:
        return 0
    if a == 0 and b > 0:
        return 90
    if a < 0 and b == 0:
        return 180
    if a == 0 and b < 0:
        return 270
    raise UnreadablePdf(
        "an appearance baseline is not an axis-aligned quarter turn, so a crop could not be "
        "rotated upright and mapped back exactly"
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


def _visible_annotation_rect(
    rect: tuple[Decimal, Decimal, Decimal, Decimal], transform: PageTransform
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return the part of an annotation rectangle that is visible on this page.

    A PDF page may use a non-zero, tight ``/CropBox`` without rewriting the page-space
    rectangle of a stamp it clips. That is still a real drawing region: a viewer clips
    the stamp at the page edge. Rejecting the entire annotation because one corner is
    outside the visible box turns a valid cropped upload into a silent zero-reading
    result.

    This is deliberately an intersection, not a coordinate translation. The annotation
    appearance still maps through its original ``/Rect``; only the part the reviewer can
    see is admitted to the stored visible-page coordinate frame. A stamp with no visible
    area remains unreadable rather than being moved onto the page.
    """
    left, bottom, right, top = rect
    crop_left, crop_bottom, crop_right, crop_top = transform.crop_box
    visible = (
        max(left, crop_left),
        max(bottom, crop_bottom),
        min(right, crop_right),
        min(top, crop_top),
    )
    if visible[2] <= visible[0] or visible[3] <= visible[1]:
        raise UnreadablePdf("annotation rectangle does not intersect the visible crop box")
    return visible


def _stamp_paths(opened_pdf: Any, page_index: int, annotation_index: int) -> tuple[VectorPath, ...]:
    """Every path in one annotation's appearance, in appearance space, as the file draws it.

    Each keeps its segment kinds, close flags and draw mode (#756). A segment whose point pdfium
    cannot read is skipped, exactly as before, so `VectorPath.points` is the sequence this used to
    return.

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
        paths: list[VectorPath] = []
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
            segments: list[PathSegment] = []
            for position in range(pdfium_raw.FPDFPath_CountSegments(page_object)):
                segment = pdfium_raw.FPDFPath_GetPathSegment(page_object, position)
                x, y = ctypes.c_float(), ctypes.c_float()
                if not pdfium_raw.FPDFPathSegment_GetPoint(
                    segment, ctypes.byref(x), ctypes.byref(y)
                ):
                    continue
                point_x, point_y = _decimal(x.value), _decimal(y.value)
                segments.append(
                    PathSegment(
                        kind=_SEGMENT_KINDS.get(
                            pdfium_raw.FPDFPathSegment_GetType(segment), SegmentKind.UNKNOWN
                        ),
                        point=(a * point_x + c * point_y + e, b * point_x + d * point_y + f),
                        closes=bool(pdfium_raw.FPDFPathSegment_GetClose(segment)),
                    )
                )
            if segments:
                # **Stroked or filled, from the file.** 97.8% of the client's glyph-sized paths are
                # stroked; the rest are outlines filled in. The two draw the same character
                # differently, so a shape reader has to know which it is looking at.
                fill_mode, stroke = ctypes.c_int(), ctypes.c_int()
                known = bool(
                    pdfium_raw.FPDFPath_GetDrawMode(
                        page_object, ctypes.byref(fill_mode), ctypes.byref(stroke)
                    )
                )
                paths.append(
                    VectorPath(
                        segments=tuple(segments),
                        stroked=bool(stroke.value) if known else None,
                        filled=(
                            (fill_mode.value != pdfium_raw.FPDF_FILLMODE_NONE) if known else None
                        ),
                    )
                )
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


def _advance_interval(
    box: tuple[Decimal, Decimal, Decimal, Decimal], baseline_rotation_degrees: int
) -> tuple[Decimal, Decimal]:
    """The box extent along the text baseline, in reading-independent order."""
    left, bottom, right, top = box
    if baseline_rotation_degrees in (0, 180):
        return (left, right)
    return (bottom, top)


def _baseline_interval(
    box: tuple[Decimal, Decimal, Decimal, Decimal], baseline_rotation_degrees: int
) -> tuple[Decimal, Decimal]:
    """The box extent perpendicular to the text baseline."""
    left, bottom, right, top = box
    if baseline_rotation_degrees in (0, 180):
        return (bottom, top)
    return (left, right)


def _intervals_overlap(left: tuple[Decimal, Decimal], right: tuple[Decimal, Decimal]) -> bool:
    """Whether two closed intervals share any stated span, without inventing a tolerance."""
    return left[0] <= right[1] and right[0] <= left[1]


def _glyph_runs(
    boxes: list[tuple[Decimal, Decimal, Decimal, Decimal]],
    gap_pt: Decimal,
    baseline_rotation_degrees: int,
) -> tuple[list[list[int]], int]:
    """Group glyph-sized boxes into ordered text runs.

    The old grouping answered only "are these boxes close in two dimensions?" and did that
    transitively, so two dimension labels on nearby baselines could become one crop. A run is
    stricter: consecutive glyph boxes must share the same baseline interval, and advance along that
    baseline with no gap larger than the caller's stated glyph gap. Single glyphs are reported
    separately rather than promoted into model crops; they are the exact fragments this issue is
    removing from the reader input.
    """
    ordered = sorted(
        range(len(boxes)),
        key=lambda index: (
            _baseline_interval(boxes[index], baseline_rotation_degrees)[0],
            _advance_interval(boxes[index], baseline_rotation_degrees)[0],
            index,
        ),
    )
    runs: list[list[int]] = []
    orphaned = 0
    while ordered:
        seed = ordered.pop(0)
        run = [seed]
        baseline = _baseline_interval(boxes[seed], baseline_rotation_degrees)
        last_advance = _advance_interval(boxes[seed], baseline_rotation_degrees)

        changed = True
        while changed:
            changed = False
            best_position: int | None = None
            best_index: int | None = None
            best_advance: tuple[Decimal, Decimal] | None = None
            for position, candidate in enumerate(ordered):
                candidate_baseline = _baseline_interval(boxes[candidate], baseline_rotation_degrees)
                if not _intervals_overlap(baseline, candidate_baseline):
                    continue
                candidate_advance = _advance_interval(boxes[candidate], baseline_rotation_degrees)
                gap = candidate_advance[0] - last_advance[1]
                if gap < 0 or gap > gap_pt:
                    continue
                if best_advance is None or candidate_advance[0] < best_advance[0]:
                    best_position = position
                    best_index = candidate
                    best_advance = candidate_advance
            if best_position is not None and best_index is not None and best_advance is not None:
                ordered.pop(best_position)
                run.append(best_index)
                baseline = (
                    max(
                        baseline[0],
                        _baseline_interval(boxes[best_index], baseline_rotation_degrees)[0],
                    ),
                    min(
                        baseline[1],
                        _baseline_interval(boxes[best_index], baseline_rotation_degrees)[1],
                    ),
                )
                last_advance = (last_advance[0], max(last_advance[1], best_advance[1]))
                changed = True

        if len(run) == 1:
            orphaned += 1
        else:
            runs.append(sorted(run))

    runs.sort(key=lambda run: min(run))
    return runs, orphaned


def page_box_polygon(
    rect: tuple[Decimal, Decimal, Decimal, Decimal],
    transform: PageTransform,
    document_version_id: UUID,
    page_index: int,
) -> tuple[Polygon, tuple[ImagePoint, ...]]:
    """A PDF-point box as the stored polygon and image corners every candidate row is placed by.

    The same computation regions and markup are placed with, for a reader outside this module that
    places a label it read from these paths (#756 phase E).
    """
    return _polygon(rect, transform, document_version_id, page_index)


def glyph_runs(
    boxes: list[tuple[Decimal, Decimal, Decimal, Decimal]],
    gap_pt: Decimal,
    baseline_rotation_degrees: int,
) -> tuple[list[list[int]], int]:
    """The run grouping regions are formed by, for a reader that must size characters the same way.

    `scripts/glyph_inventory.py` sizes every character against the region it came from, and
    `extraction/glyph_reader.py` has to size a character it reads by the same rule or a shape a
    person labelled would not match itself (#756 phase C). One grouping, not two copies of it.
    """
    return _glyph_runs(boxes, gap_pt, baseline_rotation_degrees)


def read_markup_layer(
    data: bytes,
    page_index: int,
    *,
    document_version_id: UUID,
    dpi: int,
) -> PageLayers:
    """One page's reviewer markup, and nothing else. The half that needs no empirical numbers.

    **This is the entry point the pipeline uses**, and the reason it exists is that
    `read_annotation_layers` cannot be called without the three lengths that classify the vendor's
    path geometry — and those are open questions (#179). Reading `/FreeText` is not: the text is a
    dictionary value, the rectangle is a dictionary value, and nothing about either is a threshold.
    Requiring the pipeline to supply numbers it has no basis for would make it the place they got
    invented.

    The result is a `PageLayers` with `drawing_segments` and `outlined_regions` empty, because
    nothing looked at the stamps. That is a report, not a claim that the sheet has no line-work:
    `read_annotation_layers` is what looks.
    """
    return _read_layers(
        data,
        page_index,
        document_version_id=document_version_id,
        dpi=dpi,
        geometry=None,
    )


def read_annotation_layers(
    data: bytes,
    page_index: int,
    *,
    document_version_id: UUID,
    dpi: int,
    line_minimum_pt: Decimal,
    glyph_maximum_pt: Decimal,
    glyph_gap_pt: Decimal,
    fraction_bar: FractionBarGeometry | None = None,
) -> PageLayers:
    """One page's annotation layers, read apart and never merged.

    `fraction_bar` turns on the stacked-fraction detector (#735). It is optional here because a
    script exploring line-work has no use for it, and the result says whether it ran
    (`PageLayers.fractions_read`) so an empty list from a read that never looked cannot pass for a
    sheet with no fractions on it. **Where a reading can be accepted it is not optional:**
    `workflow.association.AssociationSettings` requires it, because without it the rule that a
    stacked fraction always goes to a reviewer (#726) cannot run.

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
    for name, value in (
        ("line_minimum_pt", line_minimum_pt),
        ("glyph_maximum_pt", glyph_maximum_pt),
        ("glyph_gap_pt", glyph_gap_pt),
    ):
        if isinstance(value, float):
            raise TypeError(f"{name} must be a Decimal, never a float")
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError(f"{name} must be a finite positive Decimal")
    if fraction_bar is not None and not isinstance(fraction_bar, FractionBarGeometry):
        raise TypeError("fraction_bar must be a FractionBarGeometry")

    return _read_layers(
        data,
        page_index,
        document_version_id=document_version_id,
        dpi=dpi,
        geometry=(line_minimum_pt, glyph_maximum_pt, glyph_gap_pt, fraction_bar),
    )


def _read_layers(
    data: bytes,
    page_index: int,
    *,
    document_version_id: UUID,
    dpi: int,
    geometry: tuple[Decimal, Decimal, Decimal, FractionBarGeometry | None] | None,
) -> PageLayers:
    """The annotation walk both entry points share.

    `geometry` carries the three lengths and the optional fraction-bar detector, or `None` to skip
    the vendor's path geometry entirely. One
    walk rather than two, because the markup half and the geometry half read the same `/Annots` array
    and must agree about which annotation is which — two walks could drift apart, and a drift here
    attributes one annotation's geometry to another's text.
    """
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise ValueError("dpi must be a positive integer; stored coordinates depend on it")

    markup: list[MarkupNote] = []
    vendor_text: list[MarkupNote] = []
    other: list[MarkupNote] = []
    segments: list[DimensionExtent] = []
    regions: list[OutlinedTextRegion] = []
    fractions: list[StackedFraction] = []
    stamps: list[VendorStamp] = []
    glyph_paths: list[VectorPath] = []
    refusals: list[LayerRefusal] = []

    try:
        with pdfplumber.open(io.BytesIO(data)) as plumbed:
            try:
                page = plumbed.pages[page_index]
            except IndexError as error:
                raise UnreadablePdf(
                    f"page {page_index} is beyond the {len(plumbed.pages)} pages in this document"
                ) from error
            # **PDF space, not pdfplumber's.** `page_boxes_in_pdf_space` explains at length why
            # these cannot be `page.mediabox` / `page.cropbox`: those are inverted about the media
            # box height, and every other coordinate in this function — the annotation `/Rect`, the
            # appearance matrix, the paths pypdfium2 returns — is bottom-up PDF space. Mixing them
            # threw away an entire drawing.
            media_box, crop_box = page_boxes_in_pdf_space(page)
            transform = PageTransform(
                dpi=dpi,
                rotation=int(page.rotation or 0) % 360,
                media_box=media_box,
                crop_box=crop_box,
            )
            annotations = [annotation["data"] for annotation in page.annots]

        pdfium_document = pdfium.PdfDocument(data)
        try:
            for index, annotation in enumerate(annotations):
                subtype = _subtype(annotation)
                layer = _layer_of(annotation, subtype)
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
                    visible_rect = _visible_annotation_rect(rect, transform)
                    extent, image_extent = _polygon(
                        visible_rect, transform, document_version_id, page_index
                    )
                except (UnreadablePdf, TypeError, ValueError) as error:
                    refusals.append(LayerRefusal(index, subtype, str(error)))
                    continue

                if layer is DrawingLayer.VENDOR_DRAWING:
                    # Listed whether or not the geometry is read: where a drawing sits on the page is
                    # a dictionary value, and the panel roles (#710) need it on every read.
                    stamps.append(
                        VendorStamp(
                            annotation_index=index, extent=extent, image_extent=image_extent
                        )
                    )
                    if geometry is None:
                        continue
                    line_minimum_pt, glyph_maximum_pt, glyph_gap_pt, fraction_bar = geometry
                    try:
                        placement = _appearance_transform(annotation, rect)
                        baseline_rotation_degrees = _baseline_rotation_degrees(placement)
                        paths = tuple(
                            path.placed(placement)
                            for path in _stamp_paths(pdfium_document, page_index, index)
                        )
                    except (UnreadablePdf, TypeError, ValueError) as error:
                        refusals.append(LayerRefusal(index, subtype, str(error)))
                        continue
                    # **Clipped to the visible part of the annotation, because a viewer clips too.**
                    # An appearance stream may draw past its `/BBox`; PDF 32000-1 §12.5.5 says the
                    # annotation box clips it, and a page's `/CropBox` can clip that box again.
                    # Anything outside the visible intersection is not on the sheet a reviewer
                    # saw. Whole paths are dropped rather than trimmed: a trimmed path would join
                    # two points that were never adjacent, which is a line-work segment the drawing
                    # does not have. Fourteen of stamp 22's 1310 paths on the first real sheet.
                    inside = tuple(
                        path
                        for path in paths
                        if all(
                            visible_rect[0] <= x <= visible_rect[2]
                            and visible_rect[1] <= y <= visible_rect[3]
                            for x, y in path.points
                        )
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
                    found, ignored, orphaned_glyphs = _drawing_geometry(
                        paths,
                        transform=transform,
                        document_version_id=document_version_id,
                        page_index=page_index,
                        line_minimum_pt=line_minimum_pt,
                        glyph_maximum_pt=glyph_maximum_pt,
                        glyph_gap_pt=glyph_gap_pt,
                        fraction_bar=fraction_bar,
                        baseline_rotation_degrees=baseline_rotation_degrees,
                    )
                    segments.extend(found[0])
                    regions.extend(found[1])
                    fractions.extend(found[2])
                    glyph_paths.extend(found[3])
                    if ignored:
                        refusals.append(
                            LayerRefusal(
                                index,
                                subtype,
                                f"{ignored} paths were neither line-work nor glyph-sized and were "
                                "not used",
                            )
                        )
                    if orphaned_glyphs:
                        refusals.append(
                            LayerRefusal(
                                index,
                                subtype,
                                f"{orphaned_glyphs} glyph-sized paths did not confidently belong "
                                "to a glyph run and were not used",
                            )
                        )
                    continue

                try:
                    rotation = _rotation_degrees(annotation)
                except UnreadablePdf as error:
                    refusals.append(LayerRefusal(index, subtype, str(error)))
                    continue
                note = MarkupNote(
                    layer=layer,
                    subtype=subtype,
                    text=_text(annotation.get("Contents")) or "",
                    # AutoCAD's `/T` names its own export, not a person; the vendor's text has no
                    # author the way a reviewer's correction does.
                    author=(
                        None if layer is DrawingLayer.VENDOR_TEXT else _text(annotation.get("T"))
                    ),
                    extent=extent,
                    image_extent=image_extent,
                    rotation_degrees=rotation,
                    annotation_index=index,
                )
                if layer is DrawingLayer.REVIEWER_MARKUP:
                    markup.append(note)
                elif layer is DrawingLayer.VENDOR_TEXT:
                    vendor_text.append(note)
                else:
                    other.append(note)
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
            if (markup or segments or regions or other or vendor_text)
            else "this page carries no annotation layers to read"
        ),
        geometry_read=geometry is not None,
        stacked_fractions=tuple(fractions),
        fractions_read=geometry is not None and geometry[3] is not None,
        vendor_stamps=tuple(stamps),
        glyph_paths=tuple(glyph_paths),
        vendor_text=tuple(vendor_text),
    )


def _drawing_geometry(
    vector_paths: tuple[VectorPath, ...],
    *,
    transform: PageTransform,
    document_version_id: UUID,
    page_index: int,
    line_minimum_pt: Decimal,
    glyph_maximum_pt: Decimal,
    glyph_gap_pt: Decimal,
    fraction_bar: FractionBarGeometry | None,
    baseline_rotation_degrees: int,
) -> tuple[
    tuple[
        tuple[DimensionExtent, ...],
        tuple[OutlinedTextRegion, ...],
        tuple[StackedFraction, ...],
        tuple[VectorPath, ...],
    ],
    int,
    int,
]:
    """One stamp's paths split into line-work, candidate text regions and stacked fractions, plus
    what was left over.

    Everything is decided on each path's points, as it always was. The full paths ride along only
    so a region can carry its members (#756).
    """
    paths = tuple(path.points for path in vector_paths)
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
    small_vector_paths: list[VectorPath] = []
    ignored = ignored_segments
    for path, vector_path in zip(paths, vector_paths, strict=True):
        left, bottom, right, top = _bounds(path)
        if right - left < glyph_maximum_pt and top - bottom < glyph_maximum_pt:
            small.append((left, bottom, right, top))
            small_paths.append(path)
            small_vector_paths.append(vector_path)
        elif not _long_segments((path,), line_minimum_pt):
            ignored += 1

    # **Every path, not `small`.** `small` is bounded by `glyph_maximum_pt`, which is tuned for the
    # dimension-line detector and excludes a full-height numerator, and the runs below have already
    # orphaned the bar and denominator. The detector brings its own size bound.
    fraction_boxes: tuple[GlyphBox, ...] = (
        ()
        if fraction_bar is None
        else stacked_fractions(
            [_bounds(path) for path in paths],
            geometry=fraction_bar,
            rotation_degrees=baseline_rotation_degrees,
        )
    )
    fractions: list[StackedFraction] = []
    for box in fraction_boxes:
        try:
            extent, image_extent = _polygon(box, transform, document_version_id, page_index)
        except (TypeError, ValueError):
            # Outside the visible crop box, or a line in image space. Neither can be in a crop.
            continue
        fractions.append(StackedFraction(extent=extent, image_extent=image_extent))

    regions: list[OutlinedTextRegion] = []
    runs, orphaned_glyphs = _glyph_runs(small, glyph_gap_pt, baseline_rotation_degrees)
    for cluster in runs:
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
                stacked_glyphs=any(_touches(rect, box) for box in fraction_boxes),
                baseline_rotation_degrees=baseline_rotation_degrees,
                glyph_paths=tuple(small_vector_paths[index] for index in cluster),
            )
        )

    return (
        (tuple(segments), tuple(regions), tuple(fractions), tuple(small_vector_paths)),
        ignored,
        orphaned_glyphs,
    )


def _touches(first: GlyphBox, second: GlyphBox) -> bool:
    """Whether two boxes share any point, edges included."""
    return (
        first[0] <= second[2]
        and second[0] <= first[2]
        and first[1] <= second[3]
        and second[1] <= first[3]
    )
