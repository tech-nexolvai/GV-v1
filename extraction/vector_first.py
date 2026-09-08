"""Deciding which regions of a vector sheet a model is asked to read, and cutting those crops.

The vector-first path, in order: `extraction/annotations.py` separates the layers, this chooses what
the vision seam gets asked, and `extraction/models/` asks it. The reviewer's markup never reaches a
model at all — it is already exact text — so what is left is the vendor's outlined numbers, and the
question this module answers is *which* of them.

**Geometry chooses, not the model.** Asked to list every dimension on a real elevation, `minicpm-v`
produced 429 lines containing one distinct token; on half the same region, six plausible readings
and then the same token 604 times. Enumeration does not merely lose recall on a dense sheet, it
degenerates — so nothing here asks a model what it can see. The sheet's own line-work says where the
labels are, one crop is cut per candidate region, and each call is answerable in isolation.

**Sitting on line-work is what makes a cluster worth reading.** A stamp's small paths include
hatching, arrowheads and dots as well as glyph outlines: the first real sheet yields 571 candidate
clusters, and reading all of them would be 571 calls to have most of them refused. A dimension label
is drawn next to the line it dimensions, so proximity to line-work is the cheapest honest filter —
and choosing to *read* a region assigns it no meaning, which is why proximity is enough here and is
not enough for an association (`text_association.lines_within` says so at length).

**Nothing set aside is discarded.** Regions with no line-work near them come back in their own list
with the reason. A dimension whose line was never detected would otherwise disappear silently, and
silence is the one failure this project treats as worse than a refusal.

**This is not wired into the pipeline.** Nothing in `workflow/` imports it, deliberately: the model
lane produces untyped readings, and until semantic typing exists (#274, Q20) those cannot become
operands. It is the reader mechanics, built and testable, waiting above the gate.

Source: issue #539. Verification: `tests/extraction/test_vector_first.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pypdfium2 as pdfium  # type: ignore[import-untyped]

from evidence.crop import encode_png
from extraction.annotations import MarkupNote, OutlinedTextRegion, PageLayers
from extraction.geometry.containment import DimensionExtent
from extraction.geometry.text_association import lines_within
from extraction.rasterise import VISION_CROP_DPI
from extraction.reader import UnreadablePdf

__all__ = [
    "RegionToRead",
    "SetAsideRegion",
    "VectorFirstPage",
    "plan_reads",
    "region_crop",
]

#: PDF user space is 72 units to the inch. The same definition `extraction/rasterise.py` states.
_POINTS_PER_INCH = Decimal(72)


@dataclass(frozen=True, slots=True)
class RegionToRead:
    """One region the vision seam will be asked about, and the line-work that selected it."""

    region: OutlinedTextRegion
    lines_near: tuple[DimensionExtent, ...]
    """Nearest first. Carried so a reviewer looking at a reading can see what it sits on, and so a
    later association step does not have to recompute what selection already measured."""


@dataclass(frozen=True, slots=True)
class SetAsideRegion:
    """One region not being read, and why. Retained rather than dropped."""

    region: OutlinedTextRegion
    reason: str


@dataclass(frozen=True, slots=True)
class VectorFirstPage:
    """One page, split into what needs no model, what gets one call each, and what was set aside."""

    page_index: int
    markup: tuple[MarkupNote, ...]
    """Exact text from the file. **Never sent to a model** — there is nothing a model could add to a
    string that is already exact, and asking would introduce an error rate where there is none."""

    to_read: tuple[RegionToRead, ...]
    set_aside: tuple[SetAsideRegion, ...]
    drawing_segments: tuple[DimensionExtent, ...]

    def __post_init__(self) -> None:
        planned = len(self.to_read) + len(self.set_aside)
        seen = {id(entry.region) for entry in self.to_read} | {
            id(entry.region) for entry in self.set_aside
        }
        if len(seen) != planned:
            raise ValueError(
                "a region appears twice in the plan. Every region is read or set aside, exactly "
                "once, so that a count of regions matches a count of decisions about them."
            )


def plan_reads(
    layers: PageLayers,
    *,
    proximity_limit: Decimal,
    minimum_paths: int,
) -> VectorFirstPage:
    """Choose which of a page's outlined regions to read, and keep the rest with a reason.

    `proximity_limit` is in **stored units** — normalised `0..1` against the visible page, the same
    space `text_association.associate` measures in, so the two agree about what "near" means. It has
    no default for the reason that module gives: the number is empirical, one sheet cannot fix it,
    and a default would ship today's guess as ground truth.

    `minimum_paths` drops clusters too small to be a legible label. A single path is a dot in a
    hatch pattern far more often than it is a digit — but not always, so this is the caller's number
    too, and every region it removes is named in `set_aside` rather than vanishing.
    """
    if not isinstance(layers, PageLayers):
        raise TypeError("layers must be a PageLayers")
    if isinstance(minimum_paths, bool) or not isinstance(minimum_paths, int) or minimum_paths < 1:
        raise ValueError("minimum_paths must be a positive integer")

    to_read: list[RegionToRead] = []
    set_aside: list[SetAsideRegion] = []
    for region in layers.outlined_regions:
        if region.path_count < minimum_paths:
            set_aside.append(
                SetAsideRegion(
                    region,
                    f"{region.path_count} path(s), below the {minimum_paths} this run treats as "
                    "the smallest legible label",
                )
            )
            continue
        near = lines_within(region.extent, layers.drawing_segments, proximity_limit=proximity_limit)
        if not near:
            set_aside.append(
                SetAsideRegion(
                    region,
                    "no line-work within the proximity limit. Either it is not a dimension label, "
                    "or it labels a line this run did not detect — both are for a reviewer to see, "
                    "which is why it is here and not dropped.",
                )
            )
            continue
        to_read.append(RegionToRead(region=region, lines_near=near))

    return VectorFirstPage(
        page_index=layers.page_index,
        markup=layers.markup,
        to_read=tuple(to_read),
        set_aside=tuple(set_aside),
        drawing_segments=layers.drawing_segments,
    )


def _region_box_pt(
    region: OutlinedTextRegion,
    page_width_pt: Decimal,
    page_height_pt: Decimal,
    margin_pt: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """The region's stored extent as `(left, bottom, right, top)` in PDF points, plus a margin.

    Stored coordinates measure y **downward** from the top of the visible page while PDF user space
    measures it upward from the bottom, so the two y values swap as well as flip. Getting that wrong
    produces a crop of the mirror-image position on the sheet — a real region, the wrong one, and a
    perfectly plausible reading of it.
    """
    xs = [Decimal(point.x) for point in region.extent.points]
    ys = [Decimal(point.y) for point in region.extent.points]
    left = min(xs) * page_width_pt - margin_pt
    right = max(xs) * page_width_pt + margin_pt
    top = page_height_pt - min(ys) * page_height_pt + margin_pt
    bottom = page_height_pt - max(ys) * page_height_pt - margin_pt
    return (
        max(Decimal(0), left),
        max(Decimal(0), bottom),
        min(page_width_pt, right),
        min(page_height_pt, top),
    )


def region_crop(
    data: bytes,
    page_index: int,
    region: OutlinedTextRegion,
    *,
    dpi: int = VISION_CROP_DPI,
    margin_pt: Decimal,
) -> bytes:
    """PNG bytes for one region, rendered from the vector page at `dpi`.

    **Only the region is rendered, which is what makes 600 dpi affordable.** A whole A3 sheet at 600
    dpi is 9922×7016 pixels — over 200 MB of RGB — and `extraction/rasterise.render_page` refuses
    that against any budget this system has, correctly. A dimension label is a fraction of an inch
    across, so the same resolution over its own box is a few hundred kilobytes.

    `dpi` is the one default in this module, and it is `VISION_CROP_DPI` — measured rather than
    chosen, and recorded there with the readings that produced it. `margin_pt` has none: how much
    surrounding page a model needs in order to read a label is exactly the kind of number this
    project does not guess, and a crop cut tight to the glyph outlines may clip the marks that say
    what unit it is in.

    PNG through `evidence.crop.encode_png`, so a crop shown to a model is byte-identical to a crop
    stored as evidence for the same pixels.
    """
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise ValueError("dpi must be a positive integer")
    if isinstance(margin_pt, float):
        raise TypeError("margin_pt must be a Decimal, never a float")
    if not isinstance(margin_pt, Decimal) or not margin_pt.is_finite() or margin_pt < 0:
        raise ValueError("margin_pt must be a finite, non-negative Decimal")
    if not isinstance(region, OutlinedTextRegion):
        raise TypeError("region must be an OutlinedTextRegion")

    document = pdfium.PdfDocument(data)
    try:
        try:
            page = document[page_index]
        except Exception as error:
            # pypdfium2 raises its own `PdfiumError` for a page that is not there, not `IndexError`,
            # so this catches by behaviour rather than by the type one library happens to use.
            raise UnreadablePdf(f"page {page_index} is not in this document: {error}") from error
        width_pt, height_pt = (Decimal(str(value)) for value in page.get_size())
        left, bottom, right, top = _region_box_pt(region, width_pt, height_pt, margin_pt)
        if right <= left or top <= bottom:
            raise UnreadablePdf("a region with no area on the page cannot be cropped")
        bitmap = page.render(
            scale=float(Decimal(dpi) / _POINTS_PER_INCH),
            # pypdfium2 states a crop as how much to take off each edge, anticlockwise from the
            # left, rather than as a box.
            crop=(
                float(left),
                float(bottom),
                float(width_pt - right),
                float(height_pt - top),
            ),
            rev_byteorder=True,
        )
        return _png_from(bitmap)
    finally:
        document.close()


def _png_from(bitmap: Any) -> bytes:
    """A pdfium bitmap as PNG bytes, without Pillow.

    Two details `extraction/rasterise.py` documents for the same reason. The buffer may be **padded**
    — a row's stride can exceed its pixel width — so rows are copied out individually rather than
    handed over whole; and pdfium renders BGR unless asked otherwise, which is why the caller passes
    `rev_byteorder=True`. A crop with its red and blue channels swapped looks like a colour-management
    problem and gets diagnosed nowhere near here.

    Any alpha channel is dropped: `encode_png` writes eight-bit RGB, and a model reading a dimension
    has no use for transparency.
    """
    width = int(bitmap.width)
    height = int(bitmap.height)
    channels = int(bitmap.n_channels)
    stride = int(bitmap.stride)
    buffer = bytes(bitmap.buffer)
    rows: list[bytes] = []
    for row in range(height):
        line = buffer[row * stride : row * stride + width * channels]
        if channels == 3:
            rows.append(line)
        else:
            rows.append(
                b"".join(line[index : index + 3] for index in range(0, len(line), channels))
            )
    return encode_png(width, height, b"".join(rows))
