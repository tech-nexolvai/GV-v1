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

from evidence.coordinates import ImagePoint
from extraction.ocr import OcrItem, RapidOcrEngine, _confidence, read_page
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
