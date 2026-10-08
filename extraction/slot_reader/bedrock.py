"""The crop readers: one generic question per code-made crop, asked in parallel (#987).

**Generic on purpose.** The prompt names no client value and no client drawing: its examples are
invented. It asks only for the label's text as printed and four flags that can hold a reading back;
it does not ask for whole, numerator and denominator, because those are never used (E2 guard 2).

**Bounded like the form reader.** The same per-model start-rate pacer, the same thread-local Bedrock
clients, the same concurrency cap and throttle back-off, the same one re-ask for a malformed answer
and then an abstention — which sends that crop to the person, never the whole set. Kimi K3 is asked
at low effort, as the form reader asks it (#978).

**The wall question rides in the same batch (#992)**: a job with a second picture asks E3's narrow
yes/no question about the row's walls instead (`walls.WALL_PROMPT`), under the same pacer, so a
model's request rate never doubles. Every attempt's raw text is kept on its usage record, which the
worker stores privately (#985).

Source: issues #987, #992 · Verification: `tests/extraction/slot_reader/test_bedrock.py`
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import Decimal
from time import monotonic
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, ValidationError

from extraction.form_reader.bedrock import (
    KIMI_EFFORT,
    KIMI_K3_MODEL,
    AttemptUsage,
    ConverseClient,
    MalformedFormAnswer,
    _extract_json_object,
    _response_text,
    _response_usage,
)
from extraction.form_reader.pricing import RateLookup, require_priced_readers
from extraction.form_reader.runner import ClientProvider, ModelPacer, _is_throttle
from extraction.product_context import product_context_line, with_product
from extraction.slot_reader.seal import ReaderAnswer
from extraction.slot_reader.walls import WALL_PROMPT, WALL_PROMPT_ID, Side, WallAnswer
from vocabulary.semantic_types import ProductType

__all__ = [
    "CROP_PROMPT",
    "CROP_PROMPT_ID",
    "CropJob",
    "build_crop_request",
    "build_wall_request",
    "crop_prompt_id",
    "read_crop",
    "read_crops_parallel",
    "read_walls",
]

CROP_PROMPT_ID: Final = "slot-crop-v1"

CROP_PROMPT: Final = (
    "This picture is cut from a cabinet shop drawing. It shows ONE dimension label with its "
    "dimension line and tick marks, or something that is not a dimension at all. Copy the label's "
    'characters EXACTLY as printed, including any inch mark ("). Do not convert, correct, complete '
    "or add anything. A label in millimetres with inches in brackets is copied with both, for "
    "example 250 [9 7/8].\n"
    "Return ONLY this JSON object:\n"
    '{"text": "the label exactly as printed, or an empty string if there is none",\n'
    ' "stacked": true if any fraction is drawn stacked (numerator above denominator), else false,\n'
    ' "combined": true if the label is a sum, an expression, a count or has words, such as '
    '4"+1" or (6EQ), else false,\n'
    ' "readable": false if any character is cut off at the picture\'s edge, overlapped, blurred, '
    "or you are not sure of it; else true,\n"
    ' "no_dimension": true if the picture shows no dimension label at all (only a symbol, an '
    "arrow, a letter or a line), else false}"
)


def crop_prompt_id(product: ProductType | None = None) -> str:
    """The id a crop request is recorded under: `CROP_PROMPT_ID`, plus the product when the request
    carried the product line (#994)."""
    return with_product(CROP_PROMPT_ID, product)


class _CropAnswer(BaseModel):
    """The answer's shape, checked strictly. Extra keys are ignored: nothing reads them."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    text: StrictStr
    stacked: StrictBool
    combined: StrictBool
    readable: StrictBool
    no_dimension: StrictBool


def _base_model_id(model_id: str) -> str:
    return model_id.removeprefix("us.").removeprefix("global.")


def build_crop_request(
    *,
    model_id: str,
    crop_png: bytes,
    max_tokens: int,
    product: ProductType | None = None,
    full_view_png: bytes | None = None,
) -> dict[str, Any]:
    """One slot question: whole marked vendor view, close-up, then the question.

    With a product (#994) the drawing set's product is one plain line of its own between the
    picture and the question. Without one the request is exactly as before.
    """
    if not model_id.strip():
        raise ValueError("a crop reader model id must be stated")
    if not crop_png.startswith(b"\x89PNG\r\n\x1a\n") or (
        full_view_png is not None and not full_view_png.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        raise ValueError("a slot reader is shown PNG pictures")
    if isinstance(max_tokens, bool) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    request: dict[str, Any] = {
        "modelId": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    *(
                        []
                        if full_view_png is None
                        else [{"image": {"format": "png", "source": {"bytes": full_view_png}}}]
                    ),
                    {"image": {"format": "png", "source": {"bytes": crop_png}}},
                    *([] if product is None else [{"text": product_context_line(product)}]),
                    {"text": CROP_PROMPT},
                ],
            }
        ],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if _base_model_id(model_id) == KIMI_K3_MODEL:
        request["outputConfig"] = {"effort": KIMI_EFFORT}
    else:
        request["inferenceConfig"]["temperature"] = 0
    return request


def read_crop(
    client: ConverseClient,
    *,
    model_id: str,
    crop_png: bytes,
    page_index: int,
    max_tokens: int,
    record_attempt: Callable[[AttemptUsage], None],
    product: ProductType | None = None,
    full_view_png: bytes | None = None,
    question_packet: Mapping[str, object] | None = None,
) -> ReaderAnswer:
    """Ask one reader about one crop; re-ask once on a malformed answer, then raise."""
    prompt_id = crop_prompt_id(product)
    for attempt in range(2):
        request = build_crop_request(
            model_id=model_id,
            crop_png=crop_png,
            max_tokens=max_tokens,
            product=product,
            full_view_png=full_view_png,
        )
        if attempt:
            request["messages"][0]["content"].append(
                {"text": "Your previous reply was malformed. Return the one JSON object only."}
            )
        started = monotonic()
        try:
            response = client.converse(**request)
        except Exception as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    prompt_id,
                    prompt_id,
                    None,
                    None,
                    int((monotonic() - started) * 1000),
                    False,
                    type(error).__name__,
                    page_index,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            raise
        input_tokens, output_tokens = _response_usage(response)
        elapsed = int((monotonic() - started) * 1000)
        raw: str | None = None
        try:
            raw = _response_text(response)
            parsed = _CropAnswer.model_validate(_extract_json_object(raw))
        except (MalformedFormAnswer, ValidationError) as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    prompt_id,
                    prompt_id,
                    input_tokens,
                    output_tokens,
                    elapsed,
                    True,
                    page_index=page_index,
                    raw_response_text=raw,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            if attempt:
                raise MalformedFormAnswer(
                    "crop answer remained malformed after one re-ask"
                ) from error
            continue
        record_attempt(
            AttemptUsage(
                model_id,
                prompt_id,
                prompt_id,
                input_tokens,
                output_tokens,
                elapsed,
                False,
                page_index=page_index,
                raw_response_text=raw,
                attempt_number=attempt + 1,
                question_packet=question_packet,
            )
        )
        return ReaderAnswer(
            model_id=model_id,
            text=parsed.text,
            readable=parsed.readable,
            no_dimension=parsed.no_dimension,
            stacked=parsed.stacked,
            combined=parsed.combined,
        )
    raise AssertionError("unreachable")


class _WallReply(BaseModel):
    """The wall answer's shape. A side outside yes/no/unsure makes the answer malformed."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    left: StrictStr
    right: StrictStr
    behind: StrictStr
    view: StrictStr = ""
    left_evidence: StrictStr = ""
    right_evidence: StrictStr = ""
    behind_evidence: StrictStr = ""


def _side(value: str) -> Side:
    try:
        return Side(value.strip().lower())
    except ValueError as error:
        raise MalformedFormAnswer(f"a wall side must be yes, no or unsure: {value!r}") from error


def _wall_answer(model_id: str, reply: _WallReply) -> WallAnswer:
    view = reply.view.strip().lower()
    return WallAnswer(
        model_id=model_id,
        left=_side(reply.left),
        right=_side(reply.right),
        behind=_side(reply.behind),
        # Anything but a clear "plan" can never seal a back-only layout, so it is "other".
        view="plan" if view == "plan" else "elevation" if view == "elevation" else "other",
        evidence=(reply.left_evidence, reply.right_evidence, reply.behind_evidence),
    )


_PNG_SIGNATURE: Final = bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A))


def build_wall_request(
    *, model_id: str, row_png: bytes, view_png: bytes, max_tokens: int
) -> dict[str, Any]:
    """One wall question: the row picture, the whole view, then the question (E3's order)."""
    if not model_id.strip():
        raise ValueError("a wall reader model id must be stated")
    if not (row_png.startswith(_PNG_SIGNATURE) and view_png.startswith(_PNG_SIGNATURE)):
        raise ValueError("a wall reader is shown PNGs")
    if isinstance(max_tokens, bool) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    request: dict[str, Any] = {
        "modelId": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": row_png}}},
                    {"image": {"format": "png", "source": {"bytes": view_png}}},
                    {"text": WALL_PROMPT},
                ],
            }
        ],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if _base_model_id(model_id) == KIMI_K3_MODEL:
        request["outputConfig"] = {"effort": KIMI_EFFORT}
    else:
        request["inferenceConfig"]["temperature"] = 0
    return request


def read_walls(
    client: ConverseClient,
    *,
    model_id: str,
    row_png: bytes,
    view_png: bytes,
    page_index: int,
    max_tokens: int,
    record_attempt: Callable[[AttemptUsage], None],
    question_packet: Mapping[str, object] | None = None,
) -> WallAnswer:
    """Ask one reader about one row's walls; re-ask once on a malformed answer, then raise.

    Every attempt's raw text is recorded with its usage (#985), privately, as the form reader's is.
    """
    for attempt in range(2):
        request = build_wall_request(
            model_id=model_id, row_png=row_png, view_png=view_png, max_tokens=max_tokens
        )
        if attempt:
            request["messages"][0]["content"].append(
                {"text": "Your previous reply was malformed. Return the one JSON object only."}
            )
        started = monotonic()
        try:
            response = client.converse(**request)
        except Exception as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    WALL_PROMPT_ID,
                    WALL_PROMPT_ID,
                    None,
                    None,
                    int((monotonic() - started) * 1000),
                    False,
                    type(error).__name__,
                    page_index,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            raise
        input_tokens, output_tokens = _response_usage(response)
        elapsed = int((monotonic() - started) * 1000)
        raw: str | None = None
        try:
            raw = _response_text(response)
            answer = _wall_answer(model_id, _WallReply.model_validate(_extract_json_object(raw)))
        except (MalformedFormAnswer, ValidationError) as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    WALL_PROMPT_ID,
                    WALL_PROMPT_ID,
                    input_tokens,
                    output_tokens,
                    elapsed,
                    True,
                    page_index=page_index,
                    raw_response_text=raw,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            if attempt:
                raise MalformedFormAnswer(
                    "wall answer remained malformed after one re-ask"
                ) from error
            continue
        record_attempt(
            AttemptUsage(
                model_id,
                WALL_PROMPT_ID,
                WALL_PROMPT_ID,
                input_tokens,
                output_tokens,
                elapsed,
                False,
                page_index=page_index,
                raw_response_text=raw,
                attempt_number=attempt + 1,
                question_packet=question_packet,
            )
        )
        return answer
    raise AssertionError("unreachable")


@dataclass(frozen=True, slots=True)
class CropJob:
    """One reader, one crop. `key` is the caller's, to put the answer back where it belongs.

    For a label question, `png` is the close-up and `view_png` the marked full vendor view. A wall
    question sets `wall_question=True` and uses the same two-image shape with its own prompt.
    """

    key: str
    model_id: str
    page_index: int
    png: bytes
    view_png: bytes | None = None
    wall_question: bool = False
    question_packet: Mapping[str, object] | None = None


def read_crops_parallel(
    jobs: Sequence[CropJob],
    *,
    clients: ClientProvider,
    rates: RateLookup | None,
    calls_per_minute: Mapping[str, int],
    max_concurrent_calls: int,
    max_tokens: int,
    max_throttle_retries: int,
    retry_backoff_seconds: float,
    record_attempt: Callable[[AttemptUsage], None],
    product: ProductType | None = None,
    spend_cap_usd: Decimal | None = None,
) -> dict[tuple[str, str], ReaderAnswer | WallAnswer | None]:
    """Every job's answer by `(key, model_id)`; `None` where the reader abstained.

    Label crops and wall questions share one pool, one pacer and one concurrency cap, so asking
    both never doubles a model's request rate.

    Refuses to start when any reader has no stated price or pacing limit, as the form reader does.
    """
    if isinstance(max_concurrent_calls, bool) or max_concurrent_calls <= 0:
        raise ValueError("max_concurrent_calls must be a positive integer")
    if isinstance(max_throttle_retries, bool) or max_throttle_retries < 0:
        raise ValueError("max_throttle_retries must be a non-negative integer")
    if retry_backoff_seconds <= 0:
        raise ValueError("retry_backoff_seconds must be positive")
    keys = [(job.key, job.model_id) for job in jobs]
    if len(set(keys)) != len(keys):
        raise ValueError("each crop is read once by each reader")
    readers = tuple(sorted({job.model_id for job in jobs}))
    if not readers:
        return {}
    missing = set(readers) - set(calls_per_minute)
    if missing:
        raise ValueError(f"missing per-model pacing limits for: {', '.join(sorted(missing))}")
    require_priced_readers(readers, rates)
    pacer = ModelPacer(calls_per_minute)
    spend_guard = None
    if spend_cap_usd is not None:
        from extraction.slot_reader.anthropic import BatchSpendGuard

        spend_guard = BatchSpendGuard(spend_cap_usd, rates)  # type: ignore[arg-type]

    def invoke(job: CropJob) -> tuple[tuple[str, str], ReaderAnswer | WallAnswer | None]:
        for throttle_attempt in range(max_throttle_retries + 1):
            pacer.wait(job.model_id)
            try:
                client = clients.for_current_thread()
                if spend_guard is not None:
                    from extraction.slot_reader.anthropic import (
                        SpendCapExceeded,
                        SpendLimitedClient,
                    )

                    client = SpendLimitedClient(client, spend_guard)
                answer: ReaderAnswer | WallAnswer
                if job.wall_question or (job.view_png is not None and job.question_packet is None):
                    if job.view_png is None:
                        raise ValueError("a wall question packet must include its full vendor view")
                    answer = read_walls(
                        client,
                        model_id=job.model_id,
                        row_png=job.png,
                        view_png=job.view_png,
                        page_index=job.page_index,
                        max_tokens=max_tokens,
                        record_attempt=record_attempt,
                        question_packet=job.question_packet,
                    )
                else:
                    answer = read_crop(
                        client,
                        model_id=job.model_id,
                        crop_png=job.png,
                        page_index=job.page_index,
                        max_tokens=max_tokens,
                        record_attempt=record_attempt,
                        product=product,
                        full_view_png=job.view_png,
                        question_packet=job.question_packet,
                    )
                return (job.key, job.model_id), answer
            except MalformedFormAnswer:
                return (job.key, job.model_id), None
            except Exception as error:
                from extraction.slot_reader.anthropic import SpendCapExceeded

                if isinstance(error, SpendCapExceeded):
                    return (job.key, job.model_id), None
                if not _is_throttle(error) or throttle_attempt >= max_throttle_retries:
                    raise
                time.sleep(retry_backoff_seconds * (2**throttle_attempt))
        raise AssertionError("unreachable")

    answers: dict[tuple[str, str], ReaderAnswer | WallAnswer | None] = {}
    with ThreadPoolExecutor(max_workers=max_concurrent_calls) as executor:
        futures = [executor.submit(invoke, job) for job in jobs]
        for future in as_completed(futures):
            key, answer = future.result()
            answers[key] = answer
    return answers
