"""The AI page review's evaluator: a second, independent look at every "different" row (decision 2026-10-16).

Differences are clouded automatically, with no reviewer clicks, so each row code calls
`different` gets a second judgment before it is clouded (the two-independent-judgments rule):

* **A different model** (Sonnet 5.5; the first reading is Opus 5.5), in its own call.
* **Two close-ups**, each with the spot outlined in magenta: the architect's dimension (code's own
  box around its label and span, padded) and the vendor's printed number (the first reading's
  box, padded so the dimension line's ends show).
* **The claim stated neutrally**: "Architect: <text> measuring <what>. Vendor label in the box: ?".
  It never sees the first AI's vendor number, reasoning or verdict.
* **Strict JSON**: where each line runs from and to (first, so it traces the ends before it
  judges), `same_thing` (yes / no / unsure: do both run between the same two features?),
  `vendor_value` as printed, and a short `reason`. It is told a different number is never
  evidence of a different span: it decides from the drawn ends only.

**Code decides** (`decide_recheck`): the row stays `different` only when the evaluator says
`yes`, its vendor value parses to the same exact inches the first reading had, and that still
differs from the architect's. Anything else, including an evaluator that could not be asked,
makes the row `unsure` with the reason in plain words. Nothing becomes `same` here.

A stored answer to the identical question is reused, never paid twice; the caller's spend guard
reserves each call's worst case first. Pure but for the call and the rendering.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from time import monotonic
from typing import Any, Final, Literal

from extraction.ai_review.checks import parse_inches
from extraction.form_reader.bedrock import (
    AttemptUsage,
    MalformedFormAnswer,
    _response_text,
    _response_usage,
)
from extraction.slot_reader.claude_output import (
    OUTPUT_CONFIG_KEY,
    _conforms,
    claude_answer,
    output_config,
    picture_fits,
)

__all__ = [
    "EVALUATOR_MODEL",
    "EVALUATOR_PROMPT",
    "EVALUATOR_PROMPT_ID",
    "EvaluatorQuestion",
    "EvaluatorRead",
    "Recheck",
    "ask_evaluator",
    "decide_recheck",
    "evaluator_packet",
    "evaluator_pictures",
    "evaluator_question",
    "evaluator_schema",
]

#: A different model from the first reading (Opus 5.5); OpenRouter maps it to its own name.
EVALUATOR_MODEL: Final = "anthropic.claude-sonnet-5-5"
EVALUATOR_PROMPT_ID: Final = "ai-page-review-evaluator-v1"
_READER: Final = "ai-page-review-evaluator"
_EFFORT: Final = "high"
_MAX_TOKENS: Final = 8000
_LONG_EDGE_PX: Final = 1600
_RENDER_DPI: Final = 300
_ARCHITECT_PAD_PT: Final = 150
_VENDOR_PAD_PT: Final = 200
_OUTLINE: Final = (255, 0, 255)

EVALUATOR_PROMPT: Final = (
    "You check one dimension on two drawings of the same millwork. Image 1 is a close-up of the "
    "ARCHITECT's drawing: the architect's dimension is outlined in magenta. Image 2 is a close-up "
    "of the millwork VENDOR's shop drawing: one vendor dimension label is outlined in magenta. "
    "The magenta outlines were added by us; they are not part of either drawing.\n\n"
    "The question is ONLY whether both dimension lines run between the same two features. The "
    "two numbers may well differ: a vendor number that differs from the architect's is exactly "
    "what is being looked for, so a different number is never evidence of a different span. "
    "Never decide from the numbers; decide only from where each line's ends are drawn.\n\n"
    "1. Read the vendor's dimension label inside the magenta box on image 2 exactly as printed "
    "(vendor labels are usually millimetres with inches in brackets, e.g. 610 [24]; some are "
    'inches like 25 1/2"). If no one dimension label is inside the box, give vendor_value "".\n'
    "2. On each image, follow the outlined dimension's line to its two ends (arrowheads or ticks) "
    "and the extension lines there, and name the drawn feature each end is level with (for "
    "example the floor, the top of the counter slab, the top of a backsplash standing on the "
    "counter, a wall, an outlet's centre). When a chain of smaller dimensions runs beside the "
    "line, use where its ticks fall to see which parts the line covers (for example toe kick + "
    "cabinet + counter, with or without the backsplash above the counter).\n"
    '3. same_thing: "yes" only when both lines start at the same feature AND end at the same '
    'feature; "no" when either end is at a different feature (for example one ends at the top '
    'of the counter slab and the other at the top of the backsplash); "unsure" when you cannot '
    "tell from the drawings.\n"
    "Use only the drawings' own black ink; ignore any other coloured markup, clouds or notes. "
    "Answer with the one JSON object only: where each line runs from and to, same_thing, "
    "vendor_value (exactly as printed) and reason (one or two sentences on the ends).\n\n"
)


def evaluator_schema() -> dict[str, object]:
    """The answer's strict shape (every key required, nothing else)."""
    return {
        "type": "object",
        "properties": {
            "architect_from": {"type": "string"},
            "architect_to": {"type": "string"},
            "vendor_from": {"type": "string"},
            "vendor_to": {"type": "string"},
            "same_thing": {"type": "string", "enum": ["yes", "no", "unsure"]},
            "vendor_value": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": [
            "architect_from",
            "architect_to",
            "vendor_from",
            "vendor_to",
            "same_thing",
            "vendor_value",
            "reason",
        ],
        "additionalProperties": False,
    }


def _claim(architect_text: str, measures: str) -> str:
    what = measures.strip() or "the span shown"
    return f"Architect: {architect_text.strip()} measuring {what}. Vendor label in the box: ?"


@dataclass(frozen=True, slots=True)
class EvaluatorQuestion:
    page_index: int
    architect_picture: bytes
    vendor_picture: bytes
    prompt: str
    """The instructions and the neutral claim: never the first AI's number, reasoning or verdict."""


def evaluator_question(
    page_index: int,
    architect_picture: bytes,
    vendor_picture: bytes,
    *,
    architect_text: str,
    measures: str,
) -> EvaluatorQuestion:
    return EvaluatorQuestion(
        page_index,
        architect_picture,
        vendor_picture,
        EVALUATOR_PROMPT + "CLAIM TO CHECK: " + _claim(architect_text, measures),
    )


def _fit(png: bytes) -> bytes:
    """Shrunk (never enlarged) to the long edge and to what the evaluator reads unresized."""
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    width, height = image.size
    scale = min(1.0, _LONG_EDGE_PX / max(width, height))
    width, height = max(1, int(width * scale)), max(1, int(height * scale))
    while not picture_fits(width, height, EVALUATOR_MODEL):
        width, height = max(1, int(width * 0.97)), max(1, int(height * 0.97))
    if (width, height) != image.size:
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _outlined(
    data: bytes, page_index: int, crop: tuple[int, int, int, int], mark: tuple[int, ...], dpi: int
) -> bytes:
    from PIL import Image, ImageDraw

    from extraction.rasterise import render_region

    png = render_region(
        data, page_index, box_px=crop, dpi=dpi, maximum_pixels=40_000_000, vendor_only=True
    )
    image = Image.open(io.BytesIO(png)).convert("RGB")
    width = max(2, dpi // 100)
    ImageDraw.Draw(image).rectangle(
        (mark[0] - crop[0] - width, mark[1] - crop[1] - width)
        + (mark[2] - crop[0] + width, mark[3] - crop[1] + width),
        outline=_OUTLINE,
        width=width,
    )
    out = io.BytesIO()
    image.save(out, format="PNG")
    return _fit(out.getvalue())


def _clip(box: Sequence[int], within: Sequence[int]) -> tuple[int, int, int, int]:
    return (
        max(box[0], within[0]),
        max(box[1], within[1]),
        min(box[2], within[2]),
        min(box[3], within[3]),
    )


def evaluator_pictures(
    data: bytes,
    page_index: int,
    *,
    architect_box_pt: Sequence[object],
    vendor_box_px: Sequence[int],
    vendor_dpi: int,
    vendor_crop_px: Sequence[int] | None,
) -> tuple[bytes, bytes]:
    """The two close-ups (architect, vendor), each with its spot outlined, vendor-only rendered.

    `architect_box_pt` is code's box around the architect's label and span (page points, `top`
    down); `vendor_box_px` the first reading's box around the vendor's number, in the page's
    pixels at `vendor_dpi`, kept inside the vendor's views (`vendor_crop_px`)."""
    import pdfplumber

    from extraction.reader import pixel_placement

    with pdfplumber.open(io.BytesIO(data)) as document:
        page = document.pages[page_index]
        width, height = float(page.width), float(page.height)
        place = pixel_placement(page, _RENDER_DPI)
        x0, top, x1, bottom = (float(str(v)) for v in architect_box_pt)
        corners = [place(x, y) for x, y in ((x0, top), (x1, bottom))]
        mark = (
            min(c.x for c in corners),
            min(c.y for c in corners),
            max(c.x for c in corners),
            max(c.y for c in corners),
        )
        pad = _ARCHITECT_PAD_PT * _RENDER_DPI // 72
        page_px = (0, 0, int(width * _RENDER_DPI / 72), int(height * _RENDER_DPI / 72))
        crop = _clip((mark[0] - pad, mark[1] - pad, mark[2] + pad, mark[3] + pad), page_px)
    architect = _outlined(data, page_index, crop, mark, _RENDER_DPI)
    vpad = _VENDOR_PAD_PT * vendor_dpi // 72
    vbox = tuple(int(v) for v in vendor_box_px)
    vcrop = (vbox[0] - vpad, vbox[1] - vpad, vbox[2] + vpad, vbox[3] + vpad)
    if vendor_crop_px is not None and len(vendor_crop_px) == 4:
        vcrop = _clip(vcrop, vendor_crop_px)
    vcrop = _clip(vcrop, (0, 0, int(width * vendor_dpi / 72), int(height * vendor_dpi / 72)))
    vendor = _outlined(data, page_index, vcrop, vbox, vendor_dpi)
    return architect, vendor


def evaluator_packet(question: EvaluatorQuestion, document_version_id: str) -> dict[str, object]:
    """What the question shows and asks, signed: the identity a stored answer is reused by."""
    body: dict[str, object] = {
        "reader": _READER,
        "document_version_id": document_version_id,
        "page_index": question.page_index,
        "prompt_id": EVALUATOR_PROMPT_ID,
        "prompt_sha256": hashlib.sha256(question.prompt.encode()).hexdigest(),
        "schema_sha256": hashlib.sha256(
            json.dumps(evaluator_schema(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "effort": _EFFORT,
        "images": {
            "architect": {"sha256": hashlib.sha256(question.architect_picture).hexdigest()},
            "vendor": {"sha256": hashlib.sha256(question.vendor_picture).hexdigest()},
        },
    }
    body["packet_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


@dataclass(frozen=True, slots=True)
class EvaluatorRead:
    answer: Mapping[str, Any] | None
    raw: str | None
    reason: str | None
    attempts: tuple[AttemptUsage, ...] = field(default=())


def _request(question: EvaluatorQuestion, retry: bool) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {"image": {"format": "png", "source": {"bytes": question.architect_picture}}},
        {"image": {"format": "png", "source": {"bytes": question.vendor_picture}}},
        {"text": question.prompt},
    ]
    if retry:
        content.append(
            {"text": "Your previous reply was malformed. Return the one JSON object only."}
        )
    return {
        "modelId": EVALUATOR_MODEL,
        "messages": [{"role": "user", "content": content}],
        "inferenceConfig": {"maxTokens": _MAX_TOKENS},
        OUTPUT_CONFIG_KEY: output_config(evaluator_schema(), _EFFORT),
    }


def _estimated_input_tokens(question: EvaluatorQuestion) -> int:
    from extraction.slot_reader.claude_output import png_size, visual_tokens

    pictures = sum(
        visual_tokens(*png_size(p)) for p in (question.architect_picture, question.vendor_picture)
    )
    return pictures + len(question.prompt.encode()) + 1024


def _attempt(
    question: EvaluatorQuestion,
    packet: Mapping[str, object],
    tokens: tuple[int | None, int | None],
    elapsed: int,
    malformed: bool,
    *,
    raw: str | None = None,
    failure: str | None = None,
    number: int = 1,
    reused: str | None = None,
) -> AttemptUsage:
    return AttemptUsage(
        EVALUATOR_MODEL,
        EVALUATOR_PROMPT_ID,
        EVALUATOR_PROMPT_ID,
        tokens[0],
        tokens[1],
        elapsed,
        malformed,
        failure,
        question.page_index,
        raw_response_text=raw,
        attempt_number=number,
        question_packet=packet,
        reused_from=reused,
    )


def ask_evaluator(
    question: EvaluatorQuestion,
    *,
    client: Any,
    document_version_id: str,
    spend_guard: Any,
    stored: str | None,
) -> EvaluatorRead:
    """Ask once (re-asked once when malformed), or reuse `stored` (the identical question's stored
    answer). Never raises for a failed call: the reason is in the result."""
    packet = evaluator_packet(question, document_version_id)
    schema = evaluator_schema()
    attempts: list[AttemptUsage] = []
    if stored is not None:
        try:
            parsed = json.loads(stored)
        except json.JSONDecodeError:
            parsed = None
        if _conforms(parsed, schema):
            attempts.append(
                _attempt(question, packet, (0, 0), 0, False, raw=stored, reused="stored")
            )
            return EvaluatorRead(parsed, stored, None, tuple(attempts))
    if client is None:
        return EvaluatorRead(None, None, "no AI client is configured")
    last = "its answer was malformed twice"
    for number in (1, 2):
        reservation = None
        if spend_guard is not None:
            from extraction.slot_reader.anthropic import SpendCapExceeded

            try:
                reservation = spend_guard.reserve(
                    EVALUATOR_MODEL, _estimated_input_tokens(question), _MAX_TOKENS
                )
            except SpendCapExceeded:
                return EvaluatorRead(
                    None, None, "the package's AI budget was reached", tuple(attempts)
                )
        started = monotonic()
        try:
            response = client.converse(**_request(question, number > 1))
        except Exception as error:  # noqa: BLE001 - recorded; the row is made unsure
            elapsed = int((monotonic() - started) * 1000)
            attempts.append(
                _attempt(
                    question,
                    packet,
                    (None, None),
                    elapsed,
                    False,
                    failure=type(error).__name__,
                    number=number,
                )
            )
            return EvaluatorRead(
                None, None, f"the call failed ({type(error).__name__})", tuple(attempts)
            )
        if reservation is not None:
            spend_guard.settle(reservation, response)
        tokens = _response_usage(response)
        elapsed = int((monotonic() - started) * 1000)
        try:
            raw: str | None = _response_text(response)
        except MalformedFormAnswer:
            raw = None
        try:
            parsed = claude_answer(response, schema)
        except MalformedFormAnswer as error:
            last = f"its answer was malformed twice ({error})"
            attempts.append(
                _attempt(question, packet, tokens, elapsed, True, raw=raw, number=number)
            )
            continue
        attempts.append(_attempt(question, packet, tokens, elapsed, False, raw=raw, number=number))
        return EvaluatorRead(parsed, raw, None, tuple(attempts))
    return EvaluatorRead(None, None, last[:480], tuple(attempts))


@dataclass(frozen=True, slots=True)
class Recheck:
    """What code decided for one `different` row from the evaluator's answer."""

    result: Literal["different", "unsure"]
    why: str
    verified_by: Literal["evaluator"] | None
    """`evaluator` when the evaluator confirmed the difference; `None` when it did not."""
    evaluator_reason: str
    """The evaluator's own reason, or why it could not be asked."""
    answer: Mapping[str, str] | None
    """The evaluator's answer as given (`None` when there is none)."""


def _inch(value: Fraction) -> str:
    whole, rest = divmod(value, 1)
    if rest == 0:
        return f'{whole}"'
    return f'{whole} {rest}"' if whole else f'{rest}"'


def decide_recheck(
    *,
    architect_inches: Fraction | None,
    first_vendor_inches: Fraction | None,
    first_why: str,
    answer: Mapping[str, Any] | None,
    failure: str | None,
) -> Recheck:
    """Keep `different` only when the evaluator says the same span, reads the same vendor inches
    the first reading had, and that still differs from the architect's. Otherwise `unsure`."""
    if answer is None:
        reason = failure or "no usable answer"
        return Recheck(
            "unsure",
            f"Not clouded: the evaluator could not check this difference ({reason}).",
            None,
            reason,
            None,
        )
    said = str(answer.get("same_thing", "")).strip().lower()
    printed = str(answer.get("vendor_value", "")).strip()
    reason = str(answer.get("reason", "")).strip() or "no reason given"
    kept: Mapping[str, str] = {
        **{
            key: str(answer.get(key, "")).strip()
            for key in ("architect_from", "architect_to", "vendor_from", "vendor_to")
        },
        "same_thing": said,
        "vendor_value": printed,
        "reason": reason,
    }
    value = parse_inches(printed)

    def unsure(why: str) -> Recheck:
        return Recheck("unsure", why, None, reason, kept)

    if said == "no":
        return unsure(f"The evaluator says the vendor line runs to a different point: {reason}")
    if said != "yes":
        return unsure(f"The evaluator cannot tell whether both measure the same span: {reason}")
    if value is None or first_vendor_inches is None:
        return unsure(
            f'The evaluator read the vendor\'s number as "{printed}", which does not state exact '
            f"inches: {reason}"
        )
    if value != first_vendor_inches:
        return unsure(
            f"The evaluator read the vendor's number as {_inch(value)}, not "
            f"{_inch(first_vendor_inches)}: {reason}"
        )
    if architect_inches is None or value == architect_inches:
        return unsure(
            f"The evaluator's reading does not differ from the architect's number: {reason}"
        )
    return Recheck(
        "different",
        f"{first_why} Checked again by the evaluator: {reason}",
        "evaluator",
        reason,
        kept,
    )
