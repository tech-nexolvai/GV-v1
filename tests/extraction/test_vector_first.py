"""Choosing which regions a model is asked to read, and cutting those crops.

Verification for: `extraction/vector_first.py` (#539).

The PDFs are the hand-built ones from `test_annotations.py`, reused rather than re-invented: one
`/FreeText` whose text is exact, one `/Stamp` whose appearance draws a long line and a five-stroke
cluster. What is under test here is not the reading — no model is involved anywhere in this file —
but the two decisions in front of it: which regions get a call, and what the crop of one contains.

`test_a_region_with_no_line_work_near_it_is_kept_not_dropped` is the one to read first. A dimension
whose line was never detected must show up as something a reviewer can see, and the only way that
happens is if it is still in the result.
"""

from __future__ import annotations

import struct
import zlib
from decimal import Decimal

import pytest

from extraction.annotations import read_annotation_layers
from extraction.rasterise import VISION_CROP_DPI
from extraction.reader import UnreadablePdf
from extraction.vector_first import plan_reads, region_crop
from tests.extraction.test_annotations import (
    BOTH_LAYERS,
    DOCUMENT,
    DPI,
    KNOWN_MARKUP,
    _appearance,
    _free_text,
    _pdf,
    _stamp,
)

#: A glyph cluster far from any line-work: the same five strokes, moved away from the line.
FAR_FROM_ANY_LINE = _pdf(
    annotations=[_stamp(appearance_object=6)],
    extra_objects=[
        _appearance(
            b"1 w 100 500 m 200 500 l S\n"
            b"390 690 m 392 694 l S\n"
            b"393 690 m 395 694 l S\n"
            b"396 690 m 398 694 l S\n"
        )
    ],
)


def _layers(data: bytes = BOTH_LAYERS, *, glyph_gap_pt: Decimal = Decimal(4)):
    return read_annotation_layers(
        data,
        0,
        document_version_id=DOCUMENT,
        dpi=DPI,
        line_minimum_pt=Decimal(50),
        glyph_maximum_pt=Decimal(10),
        glyph_gap_pt=glyph_gap_pt,
    )


def _plan(
    data: bytes = BOTH_LAYERS,
    *,
    proximity_limit: Decimal = Decimal("0.2"),
    minimum_paths: int = 1,
    maximum_span: Decimal = Decimal("0.5"),
    glyph_gap_pt: Decimal = Decimal(4),
):
    return plan_reads(
        _layers(data, glyph_gap_pt=glyph_gap_pt),
        proximity_limit=proximity_limit,
        minimum_paths=minimum_paths,
        maximum_span=maximum_span,
    )


# ---------------------------------------------------------------------------
# What gets read, and what does not
# ---------------------------------------------------------------------------


def test_a_region_sitting_on_line_work_is_planned_for_reading() -> None:
    """Input: a glyph cluster just above a dimension line. Outcome: one region to read.

    Geometry chooses, not the model. Asked to enumerate a real elevation, `minicpm-v` returned 429
    lines containing one distinct token — so what gets read is decided by the sheet's own line-work
    and each call is answerable in isolation.
    """
    plan = _plan()

    assert len(plan.to_read) == 1
    assert plan.to_read[0].region.path_count == 5
    assert plan.to_read[0].lines_near, "the region was planned without recording what selected it"


def test_a_region_with_no_line_work_near_it_is_kept_not_dropped() -> None:
    """Input: a cluster in the corner, far from the line. Outcome: set aside with a reason.

    **The failure this prevents is silence.** Either it is not a dimension label, or it labels a line
    this run did not detect — and the second is a missed dimension. A dropped region would make that
    indistinguishable from a sheet that never had one.
    """
    plan = _plan(FAR_FROM_ANY_LINE, proximity_limit=Decimal("0.01"))

    assert plan.to_read == ()
    assert len(plan.set_aside) == 1
    assert "no line-work within the proximity limit" in plan.set_aside[0].reason


def test_the_proximity_limit_is_what_decides_it() -> None:
    """Input: the same file at a limit wide enough to reach. Outcome: the region is read.

    The limit belongs to the caller (#179), so this asserts it changes the answer rather than
    sitting in the signature unexercised.
    """
    near = _plan(FAR_FROM_ANY_LINE, proximity_limit=Decimal("0.9"))

    assert len(near.to_read) == 1
    assert near.set_aside == ()


def test_a_cluster_too_small_to_be_a_label_is_set_aside_with_its_count() -> None:
    """Input: single-path clusters against a minimum of two. Outcome: set aside, counted.

    A lone path is a dot in a hatch pattern far more often than a digit. The reason names the count
    so a reviewer can see how close it was to being read.
    """
    plan = _plan(glyph_gap_pt=Decimal("0.5"), minimum_paths=2)

    assert plan.to_read == ()
    assert len(plan.set_aside) == 5
    assert all("below the 2" in entry.reason for entry in plan.set_aside)


def test_every_region_is_either_read_or_set_aside_exactly_once() -> None:
    """Outcome: the two lists account for every region the layers held.

    Asserted on the plan rather than trusted: `VectorFirstPage` refuses to be built if a region
    appears twice, and this is the case that would have found it.
    """
    layers = _layers()
    plan = plan_reads(
        layers,
        proximity_limit=Decimal("0.2"),
        minimum_paths=1,
        maximum_span=Decimal("0.5"),
    )

    assert len(plan.to_read) + len(plan.set_aside) == len(layers.outlined_regions)


def test_the_markup_passes_through_untouched_and_is_never_planned_for_a_model() -> None:
    """Outcome: the exact text is on the plan; no region was made from it.

    There is nothing a model could add to a string that is already exact, and asking would introduce
    an error rate where the file has none.
    """
    plan = _plan()

    assert [note.text for note in plan.markup] == [KNOWN_MARKUP]
    assert len(plan.to_read) == 1
    # The note's own rectangle never became a region to read: the two layers are different kinds of
    # thing, and the markup's box is not a crop anybody needs.
    note_extent = plan.markup[0].extent.points
    assert all(entry.region.extent.points != note_extent for entry in plan.to_read)


def test_nothing_in_the_plan_carries_a_meaning() -> None:
    """Outcome: no semantic type anywhere on the plan.

    The auto-typing hard stop, asserted at this seam too: a region is a rectangle and a note is a
    string, and neither has a field that could hold a claim about what it measures.
    """
    plan = _plan()

    for entry in plan.to_read:
        assert not hasattr(entry.region, "semantic_type")
        assert not hasattr(entry, "semantic_type")
    for note in plan.markup:
        assert not hasattr(note, "semantic_type")


@pytest.mark.parametrize("minimum_paths", [0, -1, True])
def test_a_minimum_path_count_that_means_nothing_is_refused(minimum_paths: object) -> None:
    """Input: zero, negative or boolean. Outcome: `ValueError`."""
    with pytest.raises(ValueError, match="minimum_paths"):
        _plan(minimum_paths=minimum_paths)  # type: ignore[arg-type]


def test_a_float_proximity_limit_is_refused() -> None:
    """Input: a float limit. Outcome: `TypeError`, from the shared measure check."""
    with pytest.raises(TypeError, match="Decimal"):
        _plan(proximity_limit=0.2)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The crop itself
# ---------------------------------------------------------------------------


def _png_size(data: bytes) -> tuple[int, int]:
    """Width and height out of a PNG's IHDR, so the test reads the bytes rather than trusting them."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_a_region_crop_is_a_png_of_that_region_at_the_vision_resolution() -> None:
    """Outcome: PNG bytes whose pixel size matches the region's box at 600 dpi.

    The size is the assertion that matters. The cluster here is about 14 points wide, and at 600 dpi
    with a 2-point margin either side that is roughly 150 pixels — a crop of a label, not of a page.
    """
    plan = _plan()
    crop = region_crop(
        BOTH_LAYERS, 0, plan.to_read[0].region, dpi=VISION_CROP_DPI, margin_pt=Decimal(2)
    )
    width, height = _png_size(crop)

    assert 100 < width < 400, width
    assert 50 < height < 400, height
    assert zlib.decompress(crop[41 : crop.rindex(b"IEND") - 8]), "the PNG has no image data"


def test_the_resolution_is_what_makes_the_crop_bigger() -> None:
    """Input: the same region at 150 and at 600 dpi. Outcome: four times the pixels each way.

    Free detail, because the page is vector: there is no scan resolution to be limited by, and the
    600 dpi render is what turned a rotated feet-and-inches label from `10.8` into `8'-6''`.
    """
    region = _plan().to_read[0].region
    coarse = _png_size(region_crop(BOTH_LAYERS, 0, region, dpi=150, margin_pt=Decimal(2)))
    fine = _png_size(region_crop(BOTH_LAYERS, 0, region, dpi=600, margin_pt=Decimal(2)))

    assert fine[0] == pytest.approx(coarse[0] * 4, abs=2)
    assert fine[1] == pytest.approx(coarse[1] * 4, abs=2)


def test_the_margin_adds_page_around_the_region() -> None:
    """Input: the same region with no margin and with eight points. Outcome: a wider crop.

    A crop cut tight to the glyph outlines can clip the inch mark, which is the difference between a
    dimension and a bare number — so how much context a model gets is the caller's decision.
    """
    region = _plan().to_read[0].region
    tight = _png_size(region_crop(BOTH_LAYERS, 0, region, margin_pt=Decimal(0)))
    padded = _png_size(region_crop(BOTH_LAYERS, 0, region, margin_pt=Decimal(8)))

    assert padded[0] > tight[0]
    assert padded[1] > tight[1]


def test_the_crop_is_deterministic() -> None:
    """Outcome: the same region twice produces identical bytes.

    Two readings of the same region are only comparable if the crop is. `encode_png` is the same
    encoder `evidence/crop.py` stores evidence with, for the same reason.
    """
    region = _plan().to_read[0].region

    assert region_crop(BOTH_LAYERS, 0, region, margin_pt=Decimal(2)) == region_crop(
        BOTH_LAYERS, 0, region, margin_pt=Decimal(2)
    )


def test_a_float_margin_is_refused() -> None:
    """Input: a float margin. Outcome: `TypeError`."""
    region = _plan().to_read[0].region

    with pytest.raises(TypeError, match="never a float"):
        region_crop(BOTH_LAYERS, 0, region, margin_pt=2.0)  # type: ignore[arg-type]


def test_a_page_that_is_not_there_is_refused() -> None:
    """Input: page 9 of a one-page file. Outcome: `UnreadablePdf`."""
    region = _plan().to_read[0].region

    with pytest.raises(UnreadablePdf, match="not in this document"):
        region_crop(BOTH_LAYERS, 9, region, margin_pt=Decimal(2))


def test_a_markup_only_page_plans_no_reading_at_all() -> None:
    """Input: a sheet with a reviewer note and no vendor stamp. Outcome: text, and no calls.

    The cheapest possible page: everything on it is already exact, so the model is not involved.
    """
    plan = plan_reads(
        read_annotation_layers(
            _pdf(annotations=[_free_text()]),
            0,
            document_version_id=DOCUMENT,
            dpi=DPI,
            line_minimum_pt=Decimal(50),
            glyph_maximum_pt=Decimal(10),
            glyph_gap_pt=Decimal(4),
        ),
        proximity_limit=Decimal("0.2"),
        minimum_paths=1,
        maximum_span=Decimal("0.5"),
    )

    assert [note.text for note in plan.markup] == [KNOWN_MARKUP]
    assert plan.to_read == ()
    assert plan.set_aside == ()


def test_a_cluster_too_wide_to_be_one_label_is_set_aside() -> None:
    """**The false accept this exists to prevent.** Input: a cluster wider than one label.

    On the first real sheet, the clustering gap that merges `120"` into one region also merges that
    sheet's whole dimension chain — 4572 paths, two thirds of the page wide. `minicpm-v` answered
    that region with `3' - 3`, the first label in the chain, and **every guard passed it**: it is a
    well-formed feet-and-inches dimension, so the seam recorded a partial reading of a chain as
    though it were a label. No validator can catch that, because there is nothing wrong with the
    string. Refusing to ask is the fix.
    """
    plan = _plan(maximum_span=Decimal("0.01"))

    assert plan.to_read == ()
    assert len(plan.set_aside) == 1
    assert "largest a single label can be" in plan.set_aside[0].reason
    assert "several labels clustered together" in plan.set_aside[0].reason


def test_the_span_bound_lets_a_label_sized_region_through() -> None:
    """Input: the same region against a generous bound. Outcome: it is read.

    A bound that refused everything would be safe and useless, so both directions are asserted.
    """
    assert len(_plan(maximum_span=Decimal("0.5")).to_read) == 1


@pytest.mark.parametrize("maximum_span", [Decimal(0), Decimal(-1)])
def test_a_span_bound_that_admits_nothing_is_refused(maximum_span: Decimal) -> None:
    """Input: zero or negative. Outcome: `ValueError` rather than a plan that reads nothing."""
    with pytest.raises(ValueError, match="maximum_span"):
        _plan(maximum_span=maximum_span)


def test_a_float_span_bound_is_refused() -> None:
    """Input: a float. Outcome: `TypeError`, for the reason every other length here has one."""
    with pytest.raises(TypeError, match="never a float"):
        _plan(maximum_span=0.5)  # type: ignore[arg-type]
