"""Reading a page that has no vector text, and being honest about how it was read.

**The gap this closes.** `extraction/reader.py` reads text objects out of a PDF. A scanned page has
none — `has_vector_text` is false and the pipeline stops, so a scanned drawing produces no candidates
at all and looks exactly like a drawing with nothing on it. Scanned pages are one of the six things
`#274` asks the client for, so this is not a hypothetical page.

**A different route, not a fallback, and the difference matters.** `docs/DESIGN.md` wants *two
independent reading routes* to agree before a candidate is `CORROBORATED`. That is why the engine is
injected behind `OcrEngine` rather than imported here: a second reader is the point of the seam, and
a module that hard-wired one would have to be rewritten to get one. Nothing in this file decides
anything about agreement — `evidence/corroborate.py` owns that.

**Confidence is recorded and never consulted.** The engine returns a score per line and it is stored,
because a reviewer deciding whether to look at a page should be able to see it. Nothing here filters,
ranks or gates on it. A threshold standing in for a decision is exactly what `AGENTS.md` forbids, and
a low-confidence reading that is quietly dropped is indistinguishable from a page with nothing on it —
which is the failure this module exists to fix.

**Every reading is a candidate.** OCR output is never a fact, never authoritative, and never a verdict
operand. It is `ObservationCandidate` rows like any other reading, distinguished only by the extractor
that produced them, so a reviewer can tell a scanned reading from a vector one.

**One thing the vector route does worse.** `pdfplumber.extract_words` splits `984 mm` into `984` and
`mm`, which is how a millimetre dimension came to be recorded as 984 inches (#483). The OCR engine
returns the line whole, so a token here usually still carries its unit. That is a property of this
route, not a promise about the engine, and the parsing rule is unchanged: a token with no unit of its
own is recorded with no value.

Source: `docs/DESIGN.md` §B2.4, `docs/DESIGN_AI.md` §3.2 (OCR retries) · Verification:
`tests/extraction/test_ocr.py`
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final, Protocol
from uuid import UUID

from evidence.coordinates import ImagePoint, StoredPoint
from evidence.crop import RenderedPage
from evidence.polygon import Polygon

__all__ = [
    "OcrEngine",
    "OcrItem",
    "OcrPage",
    "OcrUnavailable",
    "RapidOcrEngine",
    "combine_dual_notation",
    "read_page",
]


_MILLIMETRE_TOKEN: Final = re.compile(r"\d+")
_BRACKETED_INCH_TOKEN: Final = re.compile(r"\[\s*[^\[\]]+\s*\]")
_AXIS_ALIGNMENT_TOLERANCE_PX: Final = 3


class OcrUnavailable(RuntimeError):
    """The OCR extra is not installed, said plainly rather than as an ImportError.

    The dependency is optional because most of this project does not need it, and a bare
    `ModuleNotFoundError: rapidocr_onnxruntime` sends the reader looking for a bug rather than for
    `pip install -e ".[ocr]"`.
    """


@dataclass(frozen=True, slots=True)
class OcrItem:
    """One line the engine read, where it was, and how sure it says it is."""

    text: str
    #: `Decimal`, not `float`, and built from the engine's own string where it gives one. The column
    #: is `Numeric` with a `0 <= confidence <= 1` check, and `Decimal(float)` would carry binary
    #: rounding into a stored number for no reason (ADR-0001).
    confidence: Decimal
    #: Integer pixels from the top-left, in the rendered image's own space — the space
    #: `observation_candidates.polygon` is constrained to.
    image_extent: tuple[ImagePoint, ...]
    #: Present only when the OCR output itself establishes how the text reads. A dual-unit pair
    #: stacked as millimetres over bracketed inches establishes a horizontal reading; arbitrary OCR
    #: quadrilaterals do not, and are deliberately left `None` rather than snapped to an axis.
    rotation_degrees: int | None = None
    #: The same extent in stored page space. `read_page` fills this for layout-established readings;
    #: callers constructing raw engine results and unoriented readings leave it `None`.
    extent: Polygon | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("an OCR item must carry text; a blank reading is not a reading")
        if not Decimal(0) <= self.confidence <= Decimal(1):
            raise ValueError(
                f"confidence {self.confidence} is outside 0..1, so the adapter has misread what the "
                "engine returned rather than the engine being unsure"
            )
        if len(self.image_extent) != 4:
            raise ValueError("an OCR item's extent must be the engine's four corner points")
        if isinstance(self.rotation_degrees, bool) or self.rotation_degrees not in (
            None,
            0,
            90,
            180,
            270,
        ):
            raise ValueError("rotation_degrees must be None or one of 0, 90, 180 or 270")


@dataclass(frozen=True, slots=True)
class OcrPage:
    """Everything one OCR pass over one page produced, and which engine produced it."""

    document_version_id: UUID
    page_index: int
    engine: str
    engine_version: str
    items: tuple[OcrItem, ...]


class OcrEngine(Protocol):
    """The narrow thing this module needs from a reader.

    Deliberately not the shape of any particular library: it takes the bytes the rasteriser already
    produced, so an adapter owns its own image handling and this module never grows a numpy import.
    A second engine for corroboration implements this and nothing else changes.
    """

    @property
    def name(self) -> str:
        """The extractor identity recorded against every candidate this engine produces."""

    @property
    def version(self) -> str:
        """Pinned, because a re-read by a newer engine must be distinguishable from the first."""

    def read(self, rgb: bytes, *, width: int, height: int) -> tuple[OcrItem, ...]:
        """Read one RGB image. Returns nothing for a page with no legible text — never raises for it."""


def read_page(rendered: RenderedPage, *, engine: OcrEngine) -> OcrPage:
    """Run one OCR pass over one rendered page.

    A page the engine finds nothing on returns an empty `items`, which is a real answer and not an
    error: a blank sheet and an illegible one are different, and telling them apart is a job for a
    second route or a person, not for a threshold here.
    """
    raw = engine.read(rendered.rgb_bytes, width=rendered.width_px, height=rendered.height_px)
    items = tuple(
        _located(item, rendered) if item.rotation_degrees is not None else item
        for item in combine_dual_notation(raw)
    )
    return OcrPage(
        document_version_id=rendered.document_version_id,
        page_index=rendered.page_index,
        engine=engine.name,
        engine_version=engine.version,
        items=tuple(items),
    )


def combine_dual_notation(items: tuple[OcrItem, ...]) -> tuple[OcrItem, ...]:
    """Join an unambiguous stacked ``millimetres`` + ``[inches]`` OCR pair.

    Vendor drawings state one dimension twice, with the unitless millimetre token immediately above
    its bracketed imperial alternate. RapidOCR can detect both lines while returning them as two
    boxes. Neither fragment has a safe value on its own, while their exact pair is already supported
    by `units.dual.parse_dual`.

    This is deliberately a layout recogniser, not fuzzy text assembly. A pair must overlap
    horizontally, each rounded quadrilateral must be axis-aligned within three pixels, the bracketed
    token's centre must sit below the millimetre token's centre, and any whitespace between rows must
    be no taller than the shorter row. The relationship must also be one-to-one in both directions.
    Any ambiguity leaves every original token untouched, preserving the existing abstention.
    """
    from units.dual import DualDimensionParseError, parse_dual

    millimetres = [
        index for index, item in enumerate(items) if _MILLIMETRE_TOKEN.fullmatch(item.text.strip())
    ]
    alternates = [
        index
        for index, item in enumerate(items)
        if _BRACKETED_INCH_TOKEN.fullmatch(item.text.strip())
    ]

    compatible: dict[int, list[int]] = {index: [] for index in millimetres}
    reverse: dict[int, list[int]] = {index: [] for index in alternates}
    for primary_index in millimetres:
        for alternate_index in alternates:
            primary = items[primary_index]
            alternate = items[alternate_index]
            if not _is_stacked_pair(primary, alternate):
                continue
            try:
                parsed = parse_dual(f"{primary.text.strip()} {alternate.text.strip()}")
            except DualDimensionParseError:
                continue
            if parsed.alternate is None:
                continue
            compatible[primary_index].append(alternate_index)
            reverse[alternate_index].append(primary_index)

    pairs = {
        primary_index: matches[0]
        for primary_index, matches in compatible.items()
        if len(matches) == 1 and len(reverse[matches[0]]) == 1
    }
    consumed = set(pairs.values())
    combined: list[OcrItem] = []
    for index, item in enumerate(items):
        if index in consumed:
            continue
        paired_alternate_index = pairs.get(index)
        if paired_alternate_index is None:
            combined.append(item)
            continue
        alternate = items[paired_alternate_index]
        combined.append(
            OcrItem(
                text=f"{item.text.strip()} {alternate.text.strip()}",
                confidence=min(item.confidence, alternate.confidence),
                image_extent=_union_extent(item.image_extent, alternate.image_extent),
                # The recognised layout is two horizontal text rows stacked vertically. Rotated or
                # diagonal arrangements do not satisfy `_is_stacked_pair` and remain uncombined.
                rotation_degrees=0,
            )
        )
    return tuple(combined)


def _bounds(points: tuple[ImagePoint, ...]) -> tuple[int, int, int, int]:
    xs = [point.x for point in points]
    ys = [point.y for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _is_stacked_pair(primary: OcrItem, alternate: OcrItem) -> bool:
    if not _is_axis_aligned(primary.image_extent) or not _is_axis_aligned(alternate.image_extent):
        return False
    primary_left, primary_top, primary_right, primary_bottom = _bounds(primary.image_extent)
    alternate_left, alternate_top, alternate_right, alternate_bottom = _bounds(
        alternate.image_extent
    )
    primary_height = primary_bottom - primary_top
    alternate_height = alternate_bottom - alternate_top
    if primary_height <= 0 or alternate_height <= 0:
        return False
    horizontal_overlap = min(primary_right, alternate_right) - max(primary_left, alternate_left)
    primary_middle = primary_top + primary_bottom
    alternate_middle = alternate_top + alternate_bottom
    vertical_gap = max(0, alternate_top - primary_bottom)
    return (
        horizontal_overlap > 0
        and alternate_middle > primary_middle
        and vertical_gap <= min(primary_height, alternate_height)
    )


def _is_axis_aligned(points: tuple[ImagePoint, ...]) -> bool:
    first, second, third, fourth = points
    tolerance = _AXIS_ALIGNMENT_TOLERANCE_PX
    return (
        abs(first.y - second.y) <= tolerance
        and abs(second.x - third.x) <= tolerance
        and abs(third.y - fourth.y) <= tolerance
        and abs(fourth.x - first.x) <= tolerance
    )


def _union_extent(
    first: tuple[ImagePoint, ...], second: tuple[ImagePoint, ...]
) -> tuple[ImagePoint, ...]:
    left, top, right, bottom = _bounds(first + second)
    return (
        ImagePoint(left, top),
        ImagePoint(right, top),
        ImagePoint(right, bottom),
        ImagePoint(left, bottom),
    )


def _in_stored_space(item: OcrItem, rendered: RenderedPage) -> OcrItem:
    """Attach the page-normalised extent that production association consumes."""
    extent = Polygon(
        points=tuple(
            StoredPoint(
                Decimal(point.x) / Decimal(rendered.width_px),
                Decimal(point.y) / Decimal(rendered.height_px),
            )
            for point in item.image_extent
        ),
        space="stored",
        document_version_id=rendered.document_version_id,
        page=rendered.page_index,
    )
    return OcrItem(
        text=item.text,
        confidence=item.confidence,
        image_extent=item.image_extent,
        rotation_degrees=item.rotation_degrees,
        extent=extent,
    )


def _located(item: OcrItem, rendered: RenderedPage) -> OcrItem:
    """Locate valid geometry; preserve an invalid reading but abstain from associating it."""
    try:
        return _in_stored_space(item, rendered)
    except (ArithmeticError, TypeError, ValueError):
        return replace(item, extent=None, rotation_degrees=None)


#: The version recorded against candidates this adapter produces.
#:
#: Ours, not the library's: it names the contract between the engine and the rows, so a change to how
#: this adapter reads a result — the confidence conversion, the corner rounding — is a new version
#: even when the library is unchanged.
RAPIDOCR_ADAPTER_VERSION: Final = "extraction.ocr.rapidocr/2"


class RapidOcrEngine:
    """RapidOCR (Apache-2.0, ONNX) behind the protocol.

    Chosen over the alternatives on two grounds that are about this repository rather than about
    accuracy: it installs from PyPI with no system binary, so CI needs no extra step and
    `tests/test_licences.py` can see it; and it reuses `opencv-python-headless`, which the extraction
    extra already carries. Tesseract would have needed a package installed in the runner, which is a
    licence-clean dependency the licence test cannot inspect.

    The model is loaded once per instance and the instance is reusable. Loading it per page would pay
    the start-up cost on every page of every document.
    """

    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-untyped]
        except ModuleNotFoundError as missing:  # pragma: no cover - depends on the install
            raise OcrUnavailable(
                'the OCR extra is not installed: pip install -e ".[ocr]"'
            ) from missing

        self._engine = RapidOCR()

    @property
    def name(self) -> str:
        return "rapidocr"

    @property
    def version(self) -> str:
        return RAPIDOCR_ADAPTER_VERSION

    def read(self, rgb: bytes, *, width: int, height: int) -> tuple[OcrItem, ...]:
        import numpy as np

        expected = width * height * 3
        if len(rgb) != expected:
            raise ValueError(
                f"{len(rgb)} bytes is not a {width}x{height} RGB image, which needs {expected}. "
                "Reshaping it anyway would read whatever followed in memory as pixels."
            )
        image = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3)

        result, _elapsed = self._engine(image)
        items: list[OcrItem] = []
        for box, text, score in result or ():
            if not str(text).strip():
                # Nothing was read here. Recording an empty candidate would add a row that says a
                # reading happened and cannot say what it was.
                continue
            items.append(
                OcrItem(
                    text=str(text),
                    confidence=_confidence(score),
                    image_extent=tuple(ImagePoint(round(x), round(y)) for x, y in box),
                )
            )
        return tuple(items)


def _confidence(score: object) -> Decimal:
    """The engine's score as an exact decimal.

    RapidOCR returns this as a **string** in some versions and a float in others. The string is taken
    as written — that is the exact value the engine reported — and a float is converted through `str`,
    never `Decimal(float)`, which would carry binary rounding into a stored number (ADR-0001).
    """
    if isinstance(score, Decimal):
        return score
    if isinstance(score, str):
        return Decimal(score)
    if isinstance(score, float | int):
        return Decimal(str(score))
    raise TypeError(f"cannot read an OCR confidence from {type(score).__name__}")
