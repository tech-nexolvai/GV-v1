"""The OCR reading route: what it records, and what it refuses to decide.

Two kinds of test here. The seam tests use a stub engine, because they are about the contract — what a
reading must carry to be storable. The adapter test runs the real engine on a page rendered from a PDF
whose text this file wrote, which is the only way to know the adapter reads a real result correctly
rather than a shape somebody imagined.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from evidence.coordinates import ImagePoint, StoredPoint
from extraction.ocr import (
    OcrItem,
    RapidOcrEngine,
    _confidence,
    combine_dual_notation,
    read_page,
)
from extraction.rasterise import render_page
from tests.extraction.test_reader import _pdf

SCAN = _pdf(
    b'BT /F1 14 Tf 1 0 0 1 40 300 Tm (38 3/4") Tj ET\n'
    b"BT /F1 14 Tf 1 0 0 1 40 250 Tm (984 mm) Tj ET\n"
    b"BT /F1 14 Tf 1 0 0 1 40 200 Tm (TITLE BLOCK) Tj ET\n",
    box=b"[0 0 400 400]",
)

CORNERS = (
    ImagePoint(10, 10),
    ImagePoint(50, 10),
    ImagePoint(50, 30),
    ImagePoint(10, 30),
)


class _StubEngine:
    """An engine that returns exactly what a test tells it to."""

    name = "stub"
    version = "test/1"

    def __init__(self, items: tuple[OcrItem, ...] = ()) -> None:
        self._items = items
        self.calls: list[tuple[int, int]] = []

    def read(self, rgb: bytes, *, width: int, height: int) -> tuple[OcrItem, ...]:
        self.calls.append((width, height))
        return self._items


def _rendered(dpi: int = 150):
    return render_page(
        SCAN,
        0,
        document_version_id=uuid4(),
        page_content_hash="0" * 64,
        dpi=dpi,
        maximum_pixels=40_000_000,
    )


def _item(text: str, box: tuple[int, int, int, int], confidence: str = "0.9") -> OcrItem:
    left, top, right, bottom = box
    return OcrItem(
        text=text,
        confidence=Decimal(confidence),
        image_extent=(
            ImagePoint(left, top),
            ImagePoint(right, top),
            ImagePoint(right, bottom),
            ImagePoint(left, bottom),
        ),
    )


def _quadrilateral(text: str, points: tuple[ImagePoint, ...]) -> OcrItem:
    return OcrItem(text=text, confidence=Decimal("0.9"), image_extent=points)


def test_a_reading_names_the_engine_that_produced_it() -> None:
    """A candidate points at a run to say what read it, and the run needs a name and a version.

    Without both, a re-read by a newer engine is indistinguishable from the first — the same reason
    `ExtractionRun` carries `extractor_version`.
    """
    page = read_page(_rendered(), engine=_StubEngine())

    assert page.engine == "stub"
    assert page.engine_version == "test/1"
    assert page.page_index == 0


def test_a_page_the_engine_reads_nothing_on_is_an_empty_result_not_an_error() -> None:
    """A blank sheet and an illegible one are different, and neither is an exception.

    Telling them apart is a job for a second route or a person. Raising here would make an ordinary
    blank page fail a stage.
    """
    page = read_page(_rendered(), engine=_StubEngine())

    assert page.items == ()


def test_the_engine_is_handed_the_rendered_page_at_its_real_size() -> None:
    """The extent an engine returns is in pixels, so it has to be told the pixel dimensions.

    A mismatch here would put every polygon in the wrong frame, and nothing downstream could detect
    it: the numbers would be plausible and wrong.
    """
    rendered = _rendered()
    engine = _StubEngine()

    read_page(rendered, engine=engine)

    assert engine.calls == [(rendered.width_px, rendered.height_px)]


def test_split_vendor_dual_notation_is_one_reading_with_the_conservative_confidence() -> None:
    """The two OCR boxes state one dimension; inches govern only after the exact pair is present."""
    items = (
        _item("76", (100, 100, 140, 120), "0.81"),
        _item("381", (200, 100, 250, 120), "0.92"),
        # Engine order is not reading order on a full page; geometry, not list adjacency, pairs.
        _item("[15]", (198, 118, 252, 142), "0.73"),
        _item("[3]", (98, 118, 142, 142), "0.77"),
    )

    combined = combine_dual_notation(items)

    assert [item.text for item in combined] == ["76 [3]", "381 [15]"]
    assert [item.confidence for item in combined] == [Decimal("0.77"), Decimal("0.73")]
    assert {item.rotation_degrees for item in combined} == {0}
    assert combined[0].image_extent == (
        ImagePoint(98, 100),
        ImagePoint(142, 100),
        ImagePoint(142, 142),
        ImagePoint(98, 142),
    )


def test_split_tokens_that_are_not_spatially_adjacent_still_abstain() -> None:
    """Matching strings elsewhere on a page must not be fuzzy-merged into a fabricated dimension."""
    items = (
        _item("76", (100, 100, 140, 120)),
        _item("[3]", (100, 180, 140, 205)),
        _item("TITLE", (200, 100, 250, 120)),
    )

    assert combine_dual_notation(items) == items


def test_an_ambiguous_split_pair_still_abstains() -> None:
    """Two plausible inch alternates are not resolved by nearest, confidence, or input order."""
    items = (
        _item("76", (100, 100, 140, 120)),
        _item("[3]", (98, 118, 125, 142)),
        _item("[4]", (115, 118, 142, 142)),
    )

    assert combine_dual_notation(items) == items


def test_reverse_ambiguity_still_abstains() -> None:
    items = (
        _item("76", (100, 100, 140, 120)),
        _item("77", (105, 100, 145, 120)),
        _item("[3]", (98, 118, 142, 142)),
    )
    assert combine_dual_notation(items) == items


def test_whitespace_limit_is_inclusive_and_one_pixel_past_abstains() -> None:
    at_limit = (_item("76", (100, 100, 140, 120)), _item("[3]", (100, 140, 140, 160)))
    past = (_item("76", (100, 100, 140, 120)), _item("[3]", (100, 141, 140, 161)))
    assert [item.text for item in combine_dual_notation(at_limit)] == ["76 [3]"]
    assert combine_dual_notation(past) == past


def test_invalid_bracketed_text_and_empty_input_abstain() -> None:
    invalid = (
        _item("76", (100, 100, 140, 120)),
        _item("[DETAIL]", (98, 118, 142, 142)),
    )
    assert combine_dual_notation(invalid) == invalid
    assert combine_dual_notation(()) == ()


def test_a_rotated_stacked_pair_is_not_declared_horizontal() -> None:
    items = (
        _quadrilateral(
            "76",
            (ImagePoint(100, 100), ImagePoint(140, 110), ImagePoint(135, 130), ImagePoint(95, 120)),
        ),
        _quadrilateral(
            "[3]",
            (ImagePoint(95, 120), ImagePoint(140, 130), ImagePoint(135, 155), ImagePoint(90, 145)),
        ),
    )
    assert combine_dual_notation(items) == items


def test_read_page_locates_a_combined_reading_in_stored_space() -> None:
    rendered = _rendered()
    engine = _StubEngine(
        (
            _item("76", (100, 100, 140, 120)),
            _item("[3]", (98, 118, 142, 142)),
        )
    )

    page = read_page(rendered, engine=engine)

    assert [item.text for item in page.items] == ["76 [3]"]
    reading = page.items[0]
    assert reading.extent is not None
    assert reading.extent.document_version_id == rendered.document_version_id
    assert reading.extent.page == rendered.page_index
    assert reading.extent.points == tuple(
        StoredPoint(
            Decimal(point.x) / Decimal(rendered.width_px),
            Decimal(point.y) / Decimal(rendered.height_px),
        )
        for point in reading.image_extent
    )


def test_a_blank_reading_is_refused_rather_than_stored() -> None:
    """A row saying a reading happened that cannot say what it was is worse than no row."""
    with pytest.raises(ValueError, match="blank reading"):
        OcrItem(text="   ", confidence=Decimal("0.9"), image_extent=CORNERS)


@pytest.mark.parametrize("confidence", ["-0.1", "1.1"])
def test_a_confidence_outside_zero_to_one_is_refused(confidence: str) -> None:
    """The column has a `0 <= confidence <= 1` check, so a value outside it is an adapter bug.

    Refused here with a message that says so, rather than reaching the database and arriving as a
    constraint violation that names no cause.
    """
    with pytest.raises(ValueError, match="outside 0..1"):
        OcrItem(text="984 mm", confidence=Decimal(confidence), image_extent=CORNERS)


def test_an_extent_that_is_not_four_corners_is_refused() -> None:
    """Four points, because that is what the engine reports and what the polygon column stores."""
    with pytest.raises(ValueError, match="four corner points"):
        OcrItem(text="984 mm", confidence=Decimal("0.9"), image_extent=CORNERS[:3])


def test_boolean_rotation_is_refused() -> None:
    with pytest.raises(ValueError, match="rotation_degrees"):
        OcrItem(
            text="76 [3]",
            confidence=Decimal("0.9"),
            image_extent=CORNERS,
            rotation_degrees=True,  # type: ignore[arg-type]
        )


def test_out_of_page_combined_geometry_is_kept_but_left_unlocated() -> None:
    rendered = _rendered()
    engine = _StubEngine(
        (
            _item("76", (rendered.width_px - 20, 100, rendered.width_px + 20, 120)),
            _item("[3]", (rendered.width_px - 22, 118, rendered.width_px + 22, 142)),
        )
    )
    reading = read_page(rendered, engine=engine).items[0]
    assert reading.text == "76 [3]"
    assert reading.extent is None
    assert reading.rotation_degrees is None


# --------------------------------------------------------------------------------------
# The real adapter. CI installs the `ocr` extra so these run there.
# --------------------------------------------------------------------------------------


def _engine() -> RapidOcrEngine:
    pytest.importorskip(
        "rapidocr_onnxruntime", reason='needs the ocr extra: pip install -e ".[ocr]"'
    )
    return RapidOcrEngine()


def test_the_real_engine_reads_a_rendered_page() -> None:
    """**The adapter against the actual library, not a shape somebody imagined.**

    Everything above uses a stub, so without this the conversion from the engine's result — its
    corner floats, its confidence, its occasional string score — would be exercised nowhere.
    """
    page = read_page(_rendered(), engine=_engine())
    texts = {item.text for item in page.items}

    assert "984 mm" in texts, f"the engine read {texts}"
    assert "TITLE BLOCK" in texts


def test_the_engine_keeps_a_number_and_its_unit_together() -> None:
    """**The one thing this route does better than the vector one.**

    `pdfplumber.extract_words` splits at the space, which is how `984 mm` came to be recorded as 984
    inches (#483). The engine returns the line whole. Asserted because it is a real difference in what
    the two routes can parse — not because the parsing rule softens for it: a token with no unit is
    still recorded with no value either way.
    """
    page = read_page(_rendered(), engine=_engine())

    assert any(item.text == "984 mm" for item in page.items), (
        "the engine split the number from its unit, so this route now has the same weakness the "
        "vector route does and the docstring in extraction/ocr.py is wrong"
    )


def test_a_real_confidence_is_an_exact_decimal_in_range() -> None:
    """Exact, because the engine reports it as a string and `Decimal(float)` would add rounding.

    Nothing decides anything from this number. It is stored so a reviewer can see it.
    """
    page = read_page(_rendered(), engine=_engine())

    assert page.items, "nothing was read, so there is no confidence to check"
    for item in page.items:
        assert isinstance(item.confidence, Decimal)
        assert Decimal(0) <= item.confidence <= Decimal(1)


def test_every_real_extent_is_four_integer_points_inside_the_page() -> None:
    """Integer pixels in the rendered page's own frame — the space the polygon column requires.

    A point outside the page would mean the adapter is reading the engine's coordinates in the wrong
    order or the wrong frame, which no downstream check could catch.
    """
    rendered = _rendered()
    page = read_page(rendered, engine=_engine())

    assert page.items
    for item in page.items:
        assert len(item.image_extent) == 4
        for x, y in item.image_extent:
            assert isinstance(x, int) and isinstance(y, int)
            assert 0 <= x <= rendered.width_px
            assert 0 <= y <= rendered.height_px


def test_a_byte_count_that_is_not_the_image_is_refused() -> None:
    """Reshaping the wrong buffer would read whatever followed it in memory as pixels."""
    engine = _engine()

    with pytest.raises(ValueError, match="not a 10x10 RGB image"):
        engine.read(b"\x00" * 12, width=10, height=10)


def test_a_string_score_is_kept_exactly_rather_than_routed_through_a_float() -> None:
    """**The docstring claims exactness; without this nothing checked it.**

    RapidOCR reports confidence as a string. `Decimal(score)` keeps every digit the engine wrote;
    `Decimal(float(score))` would round to the nearest binary double first and then record that as if
    it were the reported value. Both land in a `Numeric` column and both look plausible.

    Caught by mutation: replacing the conversion with `Decimal(float(score))` passed every other test
    in this file.
    """
    reported = "0.6284352689981461"

    assert _confidence(reported) == Decimal(reported)
    assert str(_confidence(reported)) == reported, (
        "the score was routed through a float: the stored value is no longer the digits the engine "
        "reported"
    )


def test_a_float_score_is_converted_through_its_string_not_its_binary_value() -> None:
    """The other branch, for an engine version that returns a float.

    `Decimal(0.1)` is `0.1000000000000000055511151231257827...`; `Decimal(str(0.1))` is `0.1`.
    ADR-0001's rule is the same here as everywhere: never build a Decimal from a float directly.
    """
    assert _confidence(0.1) == Decimal("0.1")


def test_a_score_of_an_unreadable_type_is_refused() -> None:
    """An engine that returned something else has changed its contract, and that must be loud."""
    with pytest.raises(TypeError, match="cannot read an OCR confidence"):
        _confidence(object())
