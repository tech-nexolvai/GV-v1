"""Bedrock Converse request/response boundary for the form-first readers.

The functions are side-effect free except for the injected Converse callable. They do not create
AWS clients or make calls at import time, which keeps tests and disabled deployments credential-free.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol

from extraction.form_reader.parser import validate_page_answer
from extraction.form_reader.prompt_v5 import PROMPT_ID, SYSTEM_PROMPT_V5, TEMPLATE_ID, page_prompt
from extraction.form_reader.schema import PageFormAnswer

KIMI_K3_MODEL = "moonshotai.kimi-k3"


class ConverseClient(Protocol):
    def converse(self, **kwargs: Any) -> Mapping[str, Any]: ...


class MalformedFormAnswer(ValueError):
    """The provider replied, but its content could not be parsed against the strict local schema."""


@dataclass(frozen=True, slots=True)
class AttemptUsage:
    model_id: str
    prompt_id: str
    template_id: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    malformed: bool
    failure_kind: str | None = None
    page_index: int = 0


UsageRecorder = Callable[[AttemptUsage], None]


def form_json_schema() -> dict[str, object]:
    """JSON Schema subset compatible with Bedrock structured outputs.

    The local Pydantic validator remains authoritative for coordinate bounds and exact field shape.
    """
    box = {
        "anyOf": [
            {"type": "array", "items": {"type": "integer"}},
            {"type": "null"},
        ]
    }
    dimension_properties = {
        "position": {"type": "integer"},
        "text": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "whole": {"type": "string"},
        "numerator": {"type": "string"},
        "denominator": {"type": "string"},
        "stacked": {"type": "boolean"},
        "kind": {
            "type": "string",
            "enum": [
                "filler",
                "cabinet",
                "cabinets_equal",
                "appliance_space",
                "end_panel",
                "unknown",
            ],
        },
        "combined": {"type": "boolean"},
        "readable": {"type": "boolean"},
        "box": box,
    }
    dimension_required = list(dimension_properties)
    dimension = {
        "type": "object",
        "properties": dimension_properties,
        "required": dimension_required,
        "additionalProperties": False,
    }
    overall = {"anyOf": [dimension, {"type": "null"}]}
    overall_scope = {
        "anyOf": [
            {"type": "string", "enum": ["run", "wall", "unknown"]},
            {"type": "null"},
        ]
    }
    countertop = {
        "type": "object",
        "properties": {
            "view_title": {"type": "string"},
            "overall_scope": overall_scope,
            "overall": overall,
            "chain": {"type": "array", "items": dimension},
        },
        "required": ["view_title", "overall_scope", "overall", "chain"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "countertops": {"type": "array", "items": countertop},
            "notes": {"type": "string"},
        },
        "required": ["countertops", "notes"],
        "additionalProperties": False,
    }


def _base_model_id(model_id: str) -> str:
    return model_id.removeprefix("us.").removeprefix("global.")


def build_converse_request(
    *, model_id: str, page_png: bytes, page_index: int, max_tokens: int
) -> dict[str, Any]:
    """Build one page request; attach the image before text as recommended for Kimi K3."""
    if not model_id.strip():
        raise ValueError("a form reader model id must be stated")
    if not page_png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("the form reader requires a rendered PNG page")
    if isinstance(max_tokens, bool) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    message = {
        "role": "user",
        "content": [
            {"image": {"format": "png", "source": {"bytes": page_png}}},
            {"text": page_prompt(page_index)},
        ],
    }
    request: dict[str, Any] = {
        "modelId": model_id,
        "system": [{"text": SYSTEM_PROMPT_V5}],
        "messages": [message],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if _base_model_id(model_id) == KIMI_K3_MODEL:
        request["outputConfig"] = {
            "textFormat": {
                "type": "json_schema",
                "structure": {
                    "jsonSchema": {
                        "name": "countertop_page_reading_v5",
                        "description": "Copied dimension labels and display boxes for one page.",
                        "schema": json.dumps(form_json_schema(), separators=(",", ":")),
                    }
                },
            }
        }
    return request


def _extract_json_object(text: str) -> Mapping[str, Any]:
    """Forgivingly extract one JSON object from a plain or fenced response, then parse strictly."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    start = cleaned.find("{")
    if start < 0:
        raise MalformedFormAnswer("response contains no JSON object")
    depth = 0
    in_string = False
    escaped = False
    end = -1
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end < 0:
        raise MalformedFormAnswer("response contains an incomplete JSON object")
    try:
        parsed = json.loads(cleaned[start:end])
    except json.JSONDecodeError as error:
        raise MalformedFormAnswer("response JSON is malformed") from error
    if not isinstance(parsed, dict):
        raise MalformedFormAnswer("response JSON root must be an object")
    return parsed


def _response_text(response: Mapping[str, Any]) -> str:
    try:
        message = response["output"]["message"]
        content = message["content"]
        texts = [item["text"] for item in content if isinstance(item, Mapping) and "text" in item]
    except (KeyError, TypeError, AttributeError) as error:
        raise MalformedFormAnswer("Bedrock response does not contain a message") from error
    if not texts or not all(isinstance(text, str) for text in texts):
        raise MalformedFormAnswer("Bedrock response contains no text block")
    return "\n".join(texts)


def _response_usage(response: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """Return exact provider token counts; missing/invalid usage stays unknown, never zero."""
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return None, None
    values = (usage.get("inputTokens"), usage.get("outputTokens"))
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        return None, None
    return values[0], values[1]


def read_page(
    client: ConverseClient,
    *,
    model_id: str,
    page_png: bytes,
    page_index: int,
    max_tokens: int,
    record_attempt: UsageRecorder,
) -> PageFormAnswer:
    """Call a configured reader; retry exactly once for malformed output, never for rule outcomes."""
    for attempt in range(2):
        request = build_converse_request(
            model_id=model_id,
            page_png=page_png,
            page_index=page_index,
            max_tokens=max_tokens,
        )
        if attempt:
            request["messages"][0]["content"].append(
                {
                    "text": "Your previous reply was malformed. Return one corrected JSON object only; do not reconsider or change any legible label."
                }
            )
        started = monotonic()
        response: Mapping[str, Any] | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        answer: PageFormAnswer | None = None
        try:
            response = client.converse(**request)
        except Exception as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    PROMPT_ID,
                    TEMPLATE_ID,
                    None,
                    None,
                    int((monotonic() - started) * 1000),
                    False,
                    type(error).__name__,
                    page_index,
                )
            )
            raise
        input_tokens, output_tokens = _response_usage(response)
        try:
            text = _response_text(response)
            payload = _extract_json_object(text)
            answer = validate_page_answer(payload, page_index=page_index)
        except (MalformedFormAnswer, ValueError) as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    PROMPT_ID,
                    TEMPLATE_ID,
                    input_tokens,
                    output_tokens,
                    int((monotonic() - started) * 1000),
                    True,
                    page_index=page_index,
                )
            )
            if attempt:
                raise MalformedFormAnswer(
                    "Bedrock answer remained malformed after one retry"
                ) from error
            continue
        except Exception as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    PROMPT_ID,
                    TEMPLATE_ID,
                    input_tokens,
                    output_tokens,
                    int((monotonic() - started) * 1000),
                    False,
                    type(error).__name__,
                    page_index,
                )
            )
            raise
        record_attempt(
            AttemptUsage(
                model_id,
                PROMPT_ID,
                TEMPLATE_ID,
                input_tokens,
                output_tokens,
                int((monotonic() - started) * 1000),
                False,
                page_index=page_index,
            )
        )
        if answer is None:
            raise AssertionError("validated answer was not produced")
        return answer
    raise AssertionError("unreachable")
