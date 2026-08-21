"""Strict model-payload validation into raw observation candidates.

Validation is fail-closed: an output is either a complete ``ObservationCandidate`` or
an explicitly recorded rejection. Unknown fields and binary floating-point values are
never silently coerced or discarded.

Source: ``docs/DESIGN_AI.md`` section 4.1 and issue #250.
Verification: ``tests/extraction/models/test_validation.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evidence.candidate import ObservationCandidate
from evidence.coordinates import ImagePoint
from units.measurement import Unit


class NovaToolPayload(BaseModel):
    """The complete payload understood from Nova; unexpected fields are errors."""

    model_config = ConfigDict(extra="forbid")

    reading: str = Field(min_length=1)
    unit_guess: str | None
    polygon: list[tuple[Decimal, Decimal]] = Field(min_length=3)


@dataclass(frozen=True, slots=True)
class CandidateContext:
    """Trusted provenance supplied by the caller, never by the model."""

    candidate_id: str
    extractor_version: str
    page: int
    extractor: str = "nova"

    def __post_init__(self) -> None:
        for name in ("candidate_id", "extractor_version", "extractor"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 0:
            raise ValueError("page must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ValidationRejection:
    """An abstention retaining the model payload and validation reasons."""

    reason: str
    raw_response: str
    errors: tuple[str, ...]
    candidate_id: str
    extractor_version: str


class RejectionRecorder(Protocol):
    """Controlled persistence boundary for diagnostic model output."""

    def record_rejection(self, rejection: ValidationRejection) -> None:
        """Persist one rejection without writing drawing data to ordinary logs."""


type ValidationOutcome = ObservationCandidate | ValidationRejection


class _FloatFound(ValueError):
    """Internal signal carrying the location of a forbidden float."""


def _reject_floats(value: object, *, path: str = "$") -> None:
    if isinstance(value, float):
        raise _FloatFound(f"{path} contains a float; model numeric values must remain exact")
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_floats(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_floats(child, path=f"{path}[{index}]")


def _serialise_raw(payload: object) -> str:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return repr(payload)


def _validation_errors(error: ValidationError) -> tuple[str, ...]:
    messages: list[str] = []
    for detail in error.errors(include_url=False):
        location = ".".join(str(part) for part in detail["loc"])
        messages.append(f"{location}: {detail['msg']}" if location else str(detail["msg"]))
    return tuple(messages)


def _record_rejection(
    *,
    payload: object,
    context: CandidateContext,
    recorder: RejectionRecorder,
    reason: str,
    errors: tuple[str, ...],
) -> ValidationRejection:
    rejection = ValidationRejection(
        reason=reason,
        raw_response=_serialise_raw(payload),
        errors=errors,
        candidate_id=context.candidate_id,
        extractor_version=context.extractor_version,
    )
    recorder.record_rejection(rejection)
    return rejection


def _pixel(value: Decimal) -> int:
    integral = value.to_integral_value()
    if value != integral:
        raise ValueError("image-space coordinates must be integral pixels")
    return int(integral)


def validate_payload(
    payload: object,
    *,
    context: CandidateContext,
    recorder: RejectionRecorder,
) -> ValidationOutcome:
    """Return a complete candidate or a recorded abstention, never a partial result."""

    try:
        _reject_floats(payload)
    except _FloatFound as error:
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason="float_not_allowed",
            errors=(str(error),),
        )

    try:
        validated = NovaToolPayload.model_validate(payload)
    except ValidationError as error:
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason="schema_validation_failed",
            errors=_validation_errors(error),
        )

    try:
        unit = Unit(validated.unit_guess) if validated.unit_guess is not None else None
        polygon = tuple(ImagePoint(_pixel(x), _pixel(y)) for x, y in validated.polygon)
    except (TypeError, ValueError) as error:
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason="candidate_conversion_failed",
            errors=(str(error),),
        )

    return ObservationCandidate(
        candidate_id=context.candidate_id,
        extractor=context.extractor,
        extractor_version=context.extractor_version,
        raw_text=validated.reading,
        parsed_value=None,
        unit_guess=unit,
        semantic_guess=None,
        page=context.page,
        polygon=polygon,
        confidence=None,
        ambiguity_flags=("nova_model_reading",),
    )
