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

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Protocol
from uuid import UUID

from evidence.coordinates import ImagePoint
from evidence.crop import RenderedPage

__all__ = [
    "OcrEngine",
    "OcrItem",
    "OcrPage",
    "OcrUnavailable",
    "RapidOcrEngine",
    "read_page",
]


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
    items = engine.read(rendered.rgb_bytes, width=rendered.width_px, height=rendered.height_px)
    return OcrPage(
        document_version_id=rendered.document_version_id,
        page_index=rendered.page_index,
        engine=engine.name,
        engine_version=engine.version,
        items=tuple(items),
    )


#: The version recorded against candidates this adapter produces.
#:
#: Ours, not the library's: it names the contract between the engine and the rows, so a change to how
#: this adapter reads a result — the confidence conversion, the corner rounding — is a new version
#: even when the library is unchanged.
RAPIDOCR_ADAPTER_VERSION: Final = "extraction.ocr.rapidocr/1"


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
