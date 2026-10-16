"""The AI page review's reader: one sharper AI reading per combined sheet (decision 2026-10-16).

The recipe measured in the read tests (two sharp halves, the architect's exact numbers from code
in the prompt, "write down every vendor number first", strict JSON):

* **Two pictures**, cut from one combined sheet and each rendered sharp on its own: the
  architect's views (image 1) and the vendor's views (image 2), each as large as the model reads
  without resizing. Rendered **vendor-only**: GV's markup annotations are not painted, so the AI
  never sees them (GV's markup is the answer key, never a reading source).
* **The architect's dimensions, read by code** from the PDF text (`extraction.compare`), listed
  `A1..An` with where each sits on image 1. The AI does not re-read them.
* **The question**: (1) write down every printed vendor dimension; (2) for each listed architect
  dimension, what it measures and the vendor's dimension of the same span, SAME / DIFFERENT /
  NOT_SHOWN / UNSURE / UNREADABLE, with an approximate box (0..1000 of image 2) around the
  vendor's printed number; (3) never use reviewer markup, a hidden number is UNREADABLE; (4) one
  JSON object.

**The AI only reads.** `checks.py` decides every row. One call per sheet (Opus 5.5), re-asked
once when the reply is malformed; a stored answer to the identical question is reused, never paid
twice; the caller's spend guard reserves each call's worst case first.

Pure but for the call: PDF bytes in, plain values out. Coordinates are pdfplumber's (page points,
`top` down) or the page's pixels at the half's own dpi.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from time import monotonic
from typing import Any, Final

from extraction.ai_review.checks import AI_RESULTS, ArchitectListed
from extraction.compare.page import ComparePage
from extraction.form_reader.bedrock import (
    AttemptUsage,
    MalformedFormAnswer,
    _response_text,
    _response_usage,
)
from extraction.geometry.rows import Box
from extraction.slot_reader.claude_output import (
    OUTPUT_CONFIG_KEY,
    _conforms,
    claude_answer,
    output_config,
    picture_fits,
)

__all__ = [
    "AI_REVIEW_MODEL",
    "AI_REVIEW_PROMPT",
    "AI_REVIEW_PROMPT_ID",
    "MEASURED_AI_REVIEW_SETTINGS",
    "AiReviewSettings",
    "Half",
    "PageQuestion",
    "PageRead",
    "ai_review_schema",
    "ask_page",
    "page_question",
    "question_packet",
]

#: The one reader: Opus 5.5 (the slot reader's id; OpenRouter maps it to its own name).
AI_REVIEW_MODEL: Final = "anthropic.claude-opus-5-5"
AI_REVIEW_PROMPT_ID: Final = "ai-page-review-v1"
_READER: Final = "ai-page-review"


@dataclass(frozen=True, slots=True)
class AiReviewSettings:
    margin_pt: Decimal
    """Each half is cut with this much room around its views."""
    long_edge_px: int
    max_tiles: int
    """Visual tokens (28 px tiles) per picture; the model reads at most 4784 unresized."""
    max_dpi: int
    max_tokens: int
    effort: str


MEASURED_AI_REVIEW_SETTINGS: Final = AiReviewSettings(
    margin_pt=Decimal(12),
    long_edge_px=2576,
    max_tiles=4700,
    max_dpi=600,
    max_tokens=16000,
    effort="high",
)


AI_REVIEW_PROMPT: Final = (
    "You get two pictures cut from the same combined sheet, each at high resolution: image 1 is "
    "the ARCHITECT's drawing, image 2 is the millwork VENDOR's shop drawing of the same thing. "
    "Act like an experienced shop-drawing reviewer.\n\n"
    "A computer already read the ARCHITECT's dimensions exactly from the PDF's text; they are "
    "listed below with where each sits on image 1 (percent across and down image 1). These values "
    "are certain: do not re-read them.\n\n"
    "Step 1. Write down EVERY printed dimension on the vendor's drawing (image 2), each with its "
    "value exactly as printed (vendor labels are usually millimetres with inches in brackets, "
    'e.g. 610 [24]; some are inches like 25 1/2"), what it runs from and to, horizontal or '
    "vertical, and roughly where. Include every piece of a dimension chain and every overall.\n"
    "Step 2. For EACH listed architect dimension, say what it measures (from what to what) and "
    "find the vendor's dimension of the SAME span (the same two endpoints). Give the vendor's "
    "value exactly as printed and one result: SAME (same span, same number), DIFFERENT (same "
    "span, different number; only when you are confident both measure the same two endpoints), "
    "NOT_SHOWN (the vendor does not dimension this span), UNSURE (you cannot tell which vendor "
    "dimension is this span), UNREADABLE (the vendor's number for this span cannot be read). "
    "Give vendor_box: an approximate box around the vendor's printed number on image 2, as four "
    "whole numbers [left, top, right, bottom] from 0 to 1000 of image 2's width and height; an "
    "empty list when there is no vendor number.\n"
    "Step 3. Never use any red, blue, yellow or other coloured reviewer markup, clouds, notes or "
    "highlights; only the drawings' own black ink. A number hidden under markup is UNREADABLE, "
    "never guessed.\n"
    "Step 4. Answer with the one JSON object only. Copy each architect_id and architect_text "
    "exactly as listed.\n\n"
    "ARCHITECT DIMENSIONS (read by code):\n"
)


def ai_review_schema() -> dict[str, object]:
    """The answer's strict shape (every key required, nothing else)."""
    string = {"type": "string"}

    def obj(properties: Mapping[str, object]) -> dict[str, object]:
        return {
            "type": "object",
            "properties": dict(properties),
            "required": list(properties),
            "additionalProperties": False,
        }

    return obj(
        {
            "vendor_dimensions": {
                "type": "array",
                "items": obj(
                    {
                        "printed": string,
                        "from_to": string,
                        "direction": {
                            "type": "string",
                            "enum": ["horizontal", "vertical", "other"],
                        },
                        "where": string,
                    }
                ),
            },
            "rows": {
                "type": "array",
                "items": obj(
                    {
                        "architect_id": string,
                        "architect_text": string,
                        "measures": string,
                        "vendor_text": string,
                        "result": {"type": "string", "enum": list(AI_RESULTS)},
                        "why": string,
                        "vendor_box": {"type": "array", "items": {"type": "integer"}},
                    }
                ),
            },
        }
    )


@dataclass(frozen=True, slots=True)
class Half:
    """One half of the sheet as the AI is shown it."""

    picture: bytes
    dpi: int
    box_px: tuple[int, int, int, int]
    """Where the picture sits on the page's pixels at `dpi` (`render_page`'s frame)."""
    box_pt: Box


@dataclass(frozen=True, slots=True)
class ListedDimension:
    listed: ArchitectListed
    label_box: Box
    """The label and its span together, page points."""


@dataclass(frozen=True, slots=True)
class PageQuestion:
    page_index: int
    page_sha256: str
    architect: Half
    vendor: Half
    dimensions: tuple[ListedDimension, ...]
    prompt: str


def _union(boxes: Sequence[Box]) -> Box:
    return Box(
        min(b.x0 for b in boxes),
        min(b.top for b in boxes),
        max(b.x1 for b in boxes),
        max(b.bottom for b in boxes),
    )


def _grow(box: Box, margin: Decimal, width: Decimal, height: Decimal) -> Box:
    return Box(
        max(Decimal(0), box.x0 - margin),
        max(Decimal(0), box.top - margin),
        min(width, box.x1 + margin),
        min(height, box.bottom + margin),
    )


def _render_half(
    data: bytes, page_index: int, box: Box, place: Callable[[int], Any], settings: AiReviewSettings
) -> Half:
    """`box` rendered vendor-only, as sharp as the model reads it unresized."""
    from PIL import Image

    from extraction.rasterise import render_region

    w, h = float(box.x1 - box.x0), float(box.bottom - box.top)
    if w <= 0 or h <= 0:
        raise ValueError("an empty half")
    scale = min(settings.long_edge_px / max(w, h), (settings.max_tiles * 784 / (w * h)) ** 0.5)
    dpi = max(36, min(settings.max_dpi, int(scale * 72 * 0.98)))
    placement = place(dpi)
    first, second = placement(box.x0, box.top), placement(box.x1, box.bottom)
    box_px = (
        max(0, min(first.x, second.x)),
        max(0, min(first.y, second.y)),
        max(first.x, second.x),
        max(first.y, second.y),
    )
    png = render_region(
        data, page_index, box_px=box_px, dpi=dpi, maximum_pixels=40_000_000, vendor_only=True
    )
    image = Image.open(io.BytesIO(png))
    width, height = image.size
    while not picture_fits(width, height, AI_REVIEW_MODEL):
        width, height = max(1, int(width * 0.97)), max(1, int(height * 0.97))
    if (width, height) != image.size:
        out = io.BytesIO()
        image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS).save(
            out, format="PNG", optimize=True
        )
        png = out.getvalue()
    return Half(png, dpi, box_px, box)


def _where(dim_box: Box, half: Box) -> str:
    w, h = half.x1 - half.x0, half.bottom - half.top
    cx = ((dim_box.x0 + dim_box.x1) / 2 - half.x0) / w * 100
    cy = ((dim_box.top + dim_box.bottom) / 2 - half.top) / h * 100
    return f"about {cx:.0f}% across and {cy:.0f}% down image 1"


def page_question(
    data: bytes, page: ComparePage, settings: AiReviewSettings = MEASURED_AI_REVIEW_SETTINGS
) -> PageQuestion | str:
    """The question for one combined sheet, or the reason none is asked (in plain words)."""
    import pdfplumber

    from extraction.reader import pixel_placement, read_pages

    if not page.architect:
        return "no architect drawing was found on this sheet"
    if not page.vendor:
        return "no vendor drawing was found on this sheet"
    listed: list[ListedDimension] = []
    for view in page.architect:
        for dim in view.dimensions:
            parts = [dim.label_box] + ([] if dim.span is None else [dim.span.box])
            listed.append(
                ListedDimension(
                    ArchitectListed(f"A{len(listed) + 1}", dim.text, dim.inches), _union(parts)
                )
            )
    if not listed:
        return "code read no architect dimension on this sheet"
    sha = hashlib.sha256(read_pages(data)[page.page_index].content).hexdigest()
    with pdfplumber.open(io.BytesIO(data)) as document:
        plumber = document.pages[page.page_index]
        width, height = Decimal(str(plumber.width)), Decimal(str(plumber.height))
        arch_box = _grow(_union([v.box for v in page.architect]), settings.margin_pt, width, height)
        vendor_box = _grow(
            _union([v.panel_box for v in page.vendor]), settings.margin_pt, width, height
        )

        def place(dpi: int) -> Any:
            return pixel_placement(plumber, dpi)

        architect = _render_half(data, page.page_index, arch_box, place, settings)
        vendor = _render_half(data, page.page_index, vendor_box, place, settings)
    lines = []
    for item in listed:
        value = (
            f"= {_inch_text(item.listed.inches)} in"
            if item.listed.inches is not None
            else "not read exactly by code"
        )
        lines.append(
            f'{item.listed.ref}: "{item.listed.text}" ({value}); {_where(item.label_box, arch_box)}'
        )
    return PageQuestion(
        page.page_index, sha, architect, vendor, tuple(listed), AI_REVIEW_PROMPT + "\n".join(lines)
    )


def _inch_text(value: Fraction) -> str:
    whole, rest = divmod(value, 1)
    if rest == 0:
        return str(whole)
    return f"{whole} {rest}" if whole else str(rest)


def question_packet(
    question: PageQuestion, document_version_id: str, settings: AiReviewSettings
) -> dict[str, object]:
    """What the question shows and asks, signed: the identity a stored answer is reused by."""
    body: dict[str, object] = {
        "reader": _READER,
        "document_version_id": document_version_id,
        "page_index": question.page_index,
        "source_page_sha256": question.page_sha256,
        "prompt_id": AI_REVIEW_PROMPT_ID,
        "prompt_sha256": hashlib.sha256(question.prompt.encode()).hexdigest(),
        "schema_sha256": hashlib.sha256(
            json.dumps(ai_review_schema(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "effort": settings.effort,
        "images": {
            "architect": {"sha256": hashlib.sha256(question.architect.picture).hexdigest()},
            "vendor": {"sha256": hashlib.sha256(question.vendor.picture).hexdigest()},
        },
    }
    body["packet_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


@dataclass(frozen=True, slots=True)
class PageRead:
    """What one sheet's question came back with."""

    answer: Mapping[str, Any] | None
    """The parsed JSON; `None` when there is no usable answer (`reason` says why)."""
    raw: str | None
    reason: str | None
    attempts: tuple[AttemptUsage, ...] = field(default=())


def _request(question: PageQuestion, settings: AiReviewSettings, retry: bool) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {"image": {"format": "png", "source": {"bytes": question.architect.picture}}},
        {"image": {"format": "png", "source": {"bytes": question.vendor.picture}}},
        {"text": question.prompt},
    ]
    if retry:
        content.append(
            {"text": "Your previous reply was malformed. Return the one JSON object only."}
        )
    return {
        "modelId": AI_REVIEW_MODEL,
        "messages": [{"role": "user", "content": content}],
        "inferenceConfig": {"maxTokens": settings.max_tokens},
        OUTPUT_CONFIG_KEY: output_config(ai_review_schema(), settings.effort),
    }


def _estimated_input_tokens(question: PageQuestion) -> int:
    from extraction.slot_reader.claude_output import png_size, visual_tokens

    pictures = sum(
        visual_tokens(*png_size(half.picture)) for half in (question.architect, question.vendor)
    )
    return pictures + len(question.prompt.encode()) + 2048


def ask_page(
    question: PageQuestion,
    *,
    client: Any,
    document_version_id: str,
    spend_guard: Any,
    stored: Callable[[Mapping[str, object]], str | None] | None,
    settings: AiReviewSettings = MEASURED_AI_REVIEW_SETTINGS,
) -> PageRead:
    """Ask the sheet's question once (re-asked once when malformed), or reuse a stored answer.

    `client` has `converse(**request)` (the OpenRouter client); `spend_guard` is the package's
    `BatchSpendGuard` (a refused reservation asks nothing). Never raises for a failed call: the
    reason is in the result, and every attempt is in `attempts` for the call record."""
    packet = question_packet(question, document_version_id, settings)
    schema = ai_review_schema()
    attempts: list[AttemptUsage] = []
    index = question.page_index
    if stored is not None:
        raw = stored(packet)
        if raw is not None:
            try:
                parsed = json.loads(raw)
                if _conforms(parsed, schema):
                    attempts.append(
                        AttemptUsage(
                            AI_REVIEW_MODEL,
                            AI_REVIEW_PROMPT_ID,
                            AI_REVIEW_PROMPT_ID,
                            0,
                            0,
                            0,
                            False,
                            page_index=index,
                            raw_response_text=raw,
                            question_packet=packet,
                            reused_from="stored",
                        )
                    )
                    return PageRead(parsed, raw, None, tuple(attempts))
            except json.JSONDecodeError:
                pass
    if client is None:
        return PageRead(None, None, "no AI client is configured (no stored answer either)")
    last_reason = "its answer was malformed twice"
    for attempt in range(2):
        reservation = None
        if spend_guard is not None:
            from extraction.slot_reader.anthropic import SpendCapExceeded

            try:
                reservation = spend_guard.reserve(
                    AI_REVIEW_MODEL, _estimated_input_tokens(question), settings.max_tokens
                )
            except SpendCapExceeded:
                return PageRead(None, None, "the package's AI budget was reached", tuple(attempts))
        started = monotonic()
        try:
            response = client.converse(**_request(question, settings, bool(attempt)))
        except Exception as error:  # noqa: BLE001 - recorded; the page is marked failed
            attempts.append(
                AttemptUsage(
                    AI_REVIEW_MODEL,
                    AI_REVIEW_PROMPT_ID,
                    AI_REVIEW_PROMPT_ID,
                    None,
                    None,
                    int((monotonic() - started) * 1000),
                    False,
                    type(error).__name__,
                    index,
                    attempt_number=attempt + 1,
                    question_packet=packet,
                )
            )
            return PageRead(
                None, None, f"the AI call failed ({type(error).__name__})", tuple(attempts)
            )
        if reservation is not None:
            spend_guard.settle(reservation, response)
        input_tokens, output_tokens = _response_usage(response)
        elapsed = int((monotonic() - started) * 1000)
        raw_text: str | None = None
        try:
            raw_text = _response_text(response)
        except MalformedFormAnswer:
            raw_text = None
        try:
            parsed = claude_answer(response, schema)
        except MalformedFormAnswer as error:
            last_reason = f"its answer was malformed twice ({error})"
            attempts.append(
                AttemptUsage(
                    AI_REVIEW_MODEL,
                    AI_REVIEW_PROMPT_ID,
                    AI_REVIEW_PROMPT_ID,
                    input_tokens,
                    output_tokens,
                    elapsed,
                    True,
                    page_index=index,
                    raw_response_text=raw_text,
                    attempt_number=attempt + 1,
                    question_packet=packet,
                )
            )
            continue
        attempts.append(
            AttemptUsage(
                AI_REVIEW_MODEL,
                AI_REVIEW_PROMPT_ID,
                AI_REVIEW_PROMPT_ID,
                input_tokens,
                output_tokens,
                elapsed,
                False,
                page_index=index,
                raw_response_text=raw_text,
                attempt_number=attempt + 1,
                question_packet=packet,
            )
        )
        return PageRead(parsed, raw_text, None, tuple(attempts))
    return PageRead(None, None, last_reason[:480], tuple(attempts))
