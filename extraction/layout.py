"""Closed-question layout classification for rulebook discriminators.

This module asks one narrow question of a rendered page: which one of the rulebook's permitted
layout answers is visible here, or can no reader safely say?  It does not persist the answer, does
not read dimensions, and does not turn model confidence into authority.

Source: issue #654. Verification: ``tests/extraction/test_layout.py``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol, cast

from evidence.coordinates import StoredPoint
from evidence.crop import RenderedPage, encode_png
from evidence.polygon import Polygon

TOOL_NAME = "answer_closed_layout_question"


class LayoutStatus(StrEnum):
    """The closed set of outcomes for a page-layout question."""

    ANSWERED = "answered"
    ABSTAINED = "abstained"
    DISAGREEMENT = "disagreement"


@dataclass(frozen=True, slots=True)
class ClosedQuestion:
    """A question whose answer space is fixed by the published rulebook."""

    name: str
    prompt: str
    choices: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in ("name", "prompt"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not isinstance(self.choices, tuple):
            raise TypeError("choices must be a tuple")
        if not self.choices:
            raise ValueError("a closed question must declare at least one permitted answer")
        if any(not isinstance(choice, str) or not choice.strip() for choice in self.choices):
            raise ValueError("each permitted answer must be a non-empty string")
        duplicates = sorted({choice for choice in self.choices if self.choices.count(choice) > 1})
        if duplicates:
            raise ValueError(f"duplicate permitted answer(s): {duplicates}")


@dataclass(frozen=True, slots=True)
class LayoutReaderResult:
    """One reader's structured answer or abstention, always with the region it inspected."""

    reader: str
    answer: str | None
    region: Polygon
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reader, str) or not self.reader.strip():
            raise ValueError("reader must be a non-empty string")
        if self.answer is not None and (
            not isinstance(self.answer, str) or not self.answer.strip()
        ):
            raise ValueError("answer must be a non-empty string or None")
        if not isinstance(self.region, Polygon):
            raise TypeError("region must be a stored evidence Polygon")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")


class LayoutReader(Protocol):
    """A configured reader for one closed page-layout question."""

    def read(self, rendered: RenderedPage, question: ClosedQuestion) -> LayoutReaderResult:
        """Return a permitted answer candidate or abstain, without persistence."""


class DiscriminatorQuestionSource(Protocol):
    """Structural subset of ``rules.required_inputs.DiscriminatorNeed`` used at the boundary."""

    @property
    def name(self) -> str:
        """The discriminator name, such as ``wall_config``."""

    @property
    def choices(self) -> tuple[str, ...]:
        """The closed vocabulary authored in the rulebook."""


@dataclass(frozen=True, slots=True)
class LayoutClassification:
    """The result returned to callers; persistence belongs to later stories."""

    status: LayoutStatus
    answer: str | None
    region: Polygon
    reason: str
    readings: tuple[LayoutReaderResult, ...]

    def __post_init__(self) -> None:
        if self.status is LayoutStatus.ANSWERED and self.answer is None:
            raise ValueError("an answered classification needs an answer")
        if self.status is not LayoutStatus.ANSWERED and self.answer is not None:
            raise ValueError("only an answered classification may carry an answer")
        if not isinstance(self.region, Polygon):
            raise TypeError("region must be a stored evidence Polygon")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")
        if not isinstance(self.readings, tuple):
            raise TypeError("readings must be a tuple")

    @property
    def is_answered(self) -> bool:
        """Whether a permitted answer survived the closed-question checks."""

        return self.status is LayoutStatus.ANSWERED


class BedrockClosedQuestionClient(Protocol):
    """The small Bedrock surface used by the closed-question reader."""

    def converse(self, **kwargs: object) -> Mapping[str, Any]:
        """Invoke a messages-capable model."""


@dataclass(frozen=True, slots=True)
class BedrockClosedQuestionConfig:
    """Explicit model identity for the Bedrock reader; credentials stay outside this type."""

    model_id: str
    prompt_id: str
    template_id: str
    reader: str = "bedrock-layout"

    def __post_init__(self) -> None:
        for name in ("model_id", "prompt_id", "template_id", "reader"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")


def question_from_discriminator(need: DiscriminatorQuestionSource) -> ClosedQuestion:
    """Build a closed page question from the rulebook-derived discriminator need."""

    return ClosedQuestion(
        name=need.name,
        prompt=f"Which {need.name} layout variant is shown on this page?",
        choices=need.choices,
    )


def full_page_region(rendered: RenderedPage) -> Polygon:
    """The whole rendered page as a real stored-coordinate polygon."""

    _require_rendered_page(rendered)
    return Polygon(
        points=(
            StoredPoint(Decimal(0), Decimal(0)),
            StoredPoint(Decimal(1), Decimal(0)),
            StoredPoint(Decimal(1), Decimal(1)),
            StoredPoint(Decimal(0), Decimal(1)),
        ),
        space="stored",
        document_version_id=rendered.document_version_id,
        page=rendered.page_index,
    )


def classify_layout(
    rendered: RenderedPage,
    question: ClosedQuestion,
    readers: Sequence[LayoutReader],
) -> LayoutClassification:
    """Ask configured readers and return one permitted answer, an abstention, or disagreement.

    Every model answer is checked against ``question.choices`` locally.  A near miss is not corrected
    into a choice, and multiple configured readers must agree exactly before an answer is returned.
    """

    _require_rendered_page(rendered)
    if not isinstance(question, ClosedQuestion):
        raise TypeError("question must be a ClosedQuestion")
    if isinstance(readers, (str, bytes)) or not isinstance(readers, Sequence):
        raise TypeError("readers must be a sequence of LayoutReader instances")
    if not readers:
        region = full_page_region(rendered)
        return LayoutClassification(
            LayoutStatus.ABSTAINED,
            None,
            region,
            "no layout reader is configured for this closed question",
            (),
        )

    readings = tuple(_checked_reading(reader, rendered, question) for reader in readers)
    outside = tuple(
        reading
        for reading in readings
        if reading.answer is not None and reading.answer not in question.choices
    )
    if outside:
        region = outside[0].region
        named = ", ".join(f"{reading.reader}: {reading.answer!r}" for reading in outside)
        return LayoutClassification(
            LayoutStatus.ABSTAINED,
            None,
            region,
            f"reader answer outside the permitted set was refused, not coerced: {named}. "
            f"Permitted answers are {list(question.choices)!r}.",
            readings,
        )

    answered = tuple(reading for reading in readings if reading.answer is not None)
    if not answered:
        return LayoutClassification(
            LayoutStatus.ABSTAINED,
            None,
            readings[0].region,
            "; ".join(f"{reading.reader}: {reading.reason}" for reading in readings),
            readings,
        )

    distinct_answers = sorted({cast(str, reading.answer) for reading in answered})
    if len(distinct_answers) > 1:
        return LayoutClassification(
            LayoutStatus.DISAGREEMENT,
            None,
            answered[0].region,
            "layout readers disagreed and the answer was not resolved by confidence: "
            + ", ".join(f"{reading.reader}={reading.answer!r}" for reading in answered),
            readings,
        )

    if len(answered) != len(readings):
        return LayoutClassification(
            LayoutStatus.ABSTAINED,
            None,
            answered[0].region,
            "not every configured layout reader answered the closed question: "
            + "; ".join(f"{reading.reader}: {reading.reason}" for reading in readings),
            readings,
        )

    answer = cast(str, answered[0].answer)
    return LayoutClassification(
        LayoutStatus.ANSWERED,
        answer,
        answered[0].region,
        f"all configured layout readers returned the permitted answer {answer!r}",
        readings,
    )


class BedrockClosedQuestionReader:
    """Forced-tool Bedrock reader whose answer field is constrained to rulebook choices."""

    def __init__(
        self,
        config: BedrockClosedQuestionConfig,
        client: BedrockClosedQuestionClient,
    ) -> None:
        self._config = config
        self._client = client

    def read(self, rendered: RenderedPage, question: ClosedQuestion) -> LayoutReaderResult:
        """Return Bedrock's structured answer, or abstain with a real crop region."""

        _require_rendered_page(rendered)
        if not isinstance(question, ClosedQuestion):
            raise TypeError("question must be a ClosedQuestion")
        fallback = full_page_region(rendered)
        try:
            response = self._client.converse(**self._request(rendered, question))
            payload = _single_tool_payload(response)
            return _payload_to_reading(
                payload,
                reader=self._config.reader,
                rendered=rendered,
                fallback=fallback,
            )
        except Exception as error:  # noqa: BLE001
            reason = str(error).strip() or type(error).__name__
            return LayoutReaderResult(
                self._config.reader,
                None,
                fallback,
                f"layout reader abstained: {reason}",
            )

    def _request(self, rendered: RenderedPage, question: ClosedQuestion) -> dict[str, object]:
        schema = _tool_schema(question)
        return {
            "modelId": self._config.model_id,
            "system": [
                {
                    "text": (
                        "Answer only by calling the required tool. Choose one permitted answer only "
                        "when the page visibly supports it; otherwise omit answer and explain why."
                    )
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "image": {
                                "format": "png",
                                "source": {
                                    "bytes": encode_png(
                                        rendered.width_px,
                                        rendered.height_px,
                                        rendered.rgb_bytes,
                                    )
                                },
                            }
                        },
                        {"text": question.prompt},
                        {"text": f"Permitted answers: {', '.join(question.choices)}"},
                    ],
                }
            ],
            "toolConfig": {
                "tools": [
                    {
                        "toolSpec": {
                            "name": TOOL_NAME,
                            "description": (
                                "Answer one closed page-layout question or abstain with a reason."
                            ),
                            "inputSchema": {"json": schema},
                        }
                    }
                ],
                "toolChoice": {"tool": {"name": TOOL_NAME}},
            },
        }


def _require_rendered_page(rendered: RenderedPage) -> None:
    if not isinstance(rendered, RenderedPage):
        raise TypeError("rendered must be a RenderedPage")
    if rendered.render_failed:
        raise ValueError("rendered page is marked failed")


def _checked_reading(
    reader: LayoutReader,
    rendered: RenderedPage,
    question: ClosedQuestion,
) -> LayoutReaderResult:
    reading = reader.read(rendered, question)
    if not isinstance(reading, LayoutReaderResult):
        raise TypeError("layout readers must return LayoutReaderResult")
    _require_region_on_page(reading.region, rendered)
    return reading


def _require_region_on_page(region: Polygon, rendered: RenderedPage) -> None:
    if (
        region.document_version_id != rendered.document_version_id
        or region.page != rendered.page_index
    ):
        raise ValueError("layout reader returned a region from a different rendered page")


def _tool_schema(question: ClosedQuestion) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "answer": {
                "type": "string",
                "enum": list(question.choices),
                "description": "Omit this field when the page does not visibly support one choice.",
            },
            "reason": {"type": "string", "minLength": 1},
            "region": {
                "type": "array",
                "minItems": 3,
                "items": {
                    "type": "array",
                    "prefixItems": [
                        {"type": "string", "pattern": r"^(0(\.\d+)?|1(\.0+)?)$"},
                        {"type": "string", "pattern": r"^(0(\.\d+)?|1(\.0+)?)$"},
                    ],
                    "minItems": 2,
                    "maxItems": 2,
                },
            },
        },
        "required": ["reason", "region"],
    }


def _single_tool_payload(response: Mapping[str, Any]) -> object:
    stop_reason = response.get("stopReason")
    if stop_reason in {"content_filtered", "guardrail_intervened"}:
        raise ValueError(f"Bedrock stopped the request: {stop_reason}")
    output = response.get("output")
    message = output.get("message") if isinstance(output, Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        raise TypeError("Bedrock response has no tool content")
    tool_calls = [
        block.get("toolUse")
        for block in content
        if isinstance(block, Mapping) and isinstance(block.get("toolUse"), Mapping)
    ]
    if len(tool_calls) != 1 or len(content) != 1:
        raise ValueError("Bedrock must return exactly one tool call and no model text")
    tool_call = cast(Mapping[str, Any], tool_calls[0])
    if tool_call.get("name") != TOOL_NAME:
        raise ValueError(f"Bedrock called an unexpected tool: {tool_call.get('name')!r}")
    return tool_call.get("input")


def _payload_to_reading(
    payload: object,
    *,
    reader: str,
    rendered: RenderedPage,
    fallback: Polygon,
) -> LayoutReaderResult:
    if not isinstance(payload, Mapping):
        raise TypeError("layout tool payload must be an object")
    unknown = set(payload) - {"answer", "reason", "region"}
    if unknown:
        raise ValueError(f"layout tool payload contained unknown field(s): {sorted(unknown)}")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("layout tool payload needs a non-empty reason")
    answer = payload.get("answer")
    if answer is not None and (not isinstance(answer, str) or not answer.strip()):
        raise ValueError("layout tool answer must be a non-empty string when present")
    try:
        region = _region_from_payload(payload.get("region"), rendered)
    except (TypeError, ValueError) as error:
        return LayoutReaderResult(reader, None, fallback, f"layout region was refused: {error}")
    return LayoutReaderResult(reader, answer, region, reason)


def _region_from_payload(payload: object, rendered: RenderedPage) -> Polygon:
    if not isinstance(payload, Iterable) or isinstance(payload, (str, bytes, Mapping)):
        raise TypeError("region must be an array of coordinate pairs")
    points: list[StoredPoint] = []
    for index, raw_point in enumerate(payload):
        if (
            not isinstance(raw_point, Sequence)
            or isinstance(raw_point, (str, bytes))
            or len(raw_point) != 2
        ):
            raise TypeError(f"region point {index} must contain exactly two coordinates")
        points.append(StoredPoint(_stored_decimal(raw_point[0]), _stored_decimal(raw_point[1])))
    return Polygon(
        points=tuple(points),
        space="stored",
        document_version_id=rendered.document_version_id,
        page=rendered.page_index,
    )


def _stored_decimal(value: object) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError("region coordinates must be exact Decimal-compatible values, never floats")
    if not isinstance(value, (str, int, Decimal)):
        raise TypeError("region coordinates must be strings, integers or Decimals")
    try:
        decimal = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"region coordinate {value!r} is not a decimal") from error
    if not decimal.is_finite() or decimal < 0 or decimal > 1:
        raise ValueError("region coordinates must stay within stored page bounds 0..1")
    return decimal
