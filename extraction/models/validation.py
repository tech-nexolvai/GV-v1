"""Strict model-payload validation into raw observation candidates.

Validation is fail-closed: an output is either a complete ``ObservationCandidate`` or
an explicitly recorded rejection. Unknown fields and binary floating-point values are
never silently coerced or discarded.

Source: ``docs/DESIGN_AI.md`` section 4.1 and issue #250.
Verification: ``tests/extraction/models/test_validation.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evidence.candidate import ObservationCandidate
from evidence.coordinates import ImagePoint
from units.measurement import Unit
from units.normalise import UnitNormalisationError, normalise_to_inches


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


#: A fraction with no whole number in front of it: `1/2`, `3/4`, `15/16`, with an optional inch mark.
#: Deliberately not narrowed to "suspicious-looking" fractions — every bare fraction matches.
_BARE_FRACTION_RE = re.compile(r'^\s*\d+\s*/\s*\d+\s*"?\s*$')

#: Typewriter and typographic spellings of the inch and foot marks, as a model tends to emit them.
_MARK_SPELLINGS = (("\u2033", '"'), ("\u2032", "'"), ("''", '"'))


def _probe_text(reading: str) -> str:
    """The reading with the inch mark spelled the way the parser knows it.

    Asked for `8' - 6"` at 600 dpi, `minicpm-v` answered `8'-6''` — the same dimension with the inch
    double-prime typed as two apostrophes, which is how it has been written on typewriters and in
    plain ASCII for a century. `normalise_to_inches` refuses it, so a correct reading was being
    thrown away over a character.

    This is a transcription equivalence, not a value guess: `''` and `\u2033` mean inches and nothing
    else, and no number changes. **Only this probe sees the substitution.** The candidate keeps
    exactly the characters the model produced, because a reviewer comparing a reading with a crop
    must see what was actually returned.
    """
    probed = reading
    for spelling, canonical in _MARK_SPELLINGS:
        probed = probed.replace(spelling, canonical)
    return probed


def _reading_refusal(reading: str) -> str | None:
    """Why this reading is not usable as a dimension, or `None` if it is.

    **The validator used to check the polygon and say nothing about the reading.** On real crops that
    inverted its job: it refused `33"`, `120"` and `8'-6''` over their polygons, and accepted `1/2` —
    which was the model's reading of a label that says `28 1/2"`. A dropped whole number is the worst
    failure available to this seam, because `1/2"` is a dimension a fabricator could plausibly be
    given, so nothing downstream has cause to question it.

    Two refusals:

    **It must be a dimension token.** Judged by `units.normalise.normalise_to_inches`, the same
    reader the deterministic vector lane uses, so "is this a dimension" has one answer in this
    codebase instead of one per caller. Prose, `189 1 1/4` and `abc` fail here.

    **A bare fraction abstains.** `1/2"` parses perfectly and is a legitimate thing to write on a
    drawing — a 3/4" side panel is in the rulebook — so this refuses readings that are probably
    right. That is the trade, made deliberately: a false abstention costs a reviewer one look at a
    crop, and the alternative cost a 28½-inch dimension becoming a half-inch one in silence. It
    applies to the model lane only; text read from the PDF itself never comes through here.

    **What this does not do.** It does not check that the reading is *correct* — `10.8` is a
    well-formed dimension token and was a misreading of `8' - 6"`. No validator can catch that, and
    claiming otherwise would be worse than not checking.

    `unmarked_unit=Unit.INCH` is passed unconditionally because this asks about **shape**, not
    meaning: the parse result is discarded, nothing here produces a value, and the candidate's
    `parsed_value` stays `None`. Which unit the number is in remains the `unit_guess` field's
    business, and `evidence/normalize.py` already refuses a candidate that has none.
    """
    try:
        measured = normalise_to_inches(_probe_text(reading), unmarked_unit=Unit.INCH)
    except UnitNormalisationError as error:
        return f"reading {reading!r} is not a dimension token: {error}"
    if measured.exact == 0:
        # `00` came back from a real crop and was accepted, because zero parses. A dimension line of
        # no length is not a dimension, nobody draws one, and a zero operand in a rule is a silent
        # way to satisfy a sum — so this is the one magnitude that says the reading failed rather
        # than that the drawing is unusual.
        return f"reading {reading!r} measures zero, which is not a dimension anything drew"
    if _BARE_FRACTION_RE.match(reading):
        return (
            f"reading {reading!r} is a fraction with no whole number. A dropped whole number reads "
            "as a valid dimension, so this abstains rather than accepting it"
        )
    return None


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

    # Before the candidate exists, because a candidate is a reading somebody may act on.
    refusal = _reading_refusal(validated.reading)
    if refusal is not None:
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason="reading_not_a_dimension",
            errors=(refusal,),
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
        # Derived from the extractor rather than hardcoded, because this validator serves more than
        # one adapter now and a reading from a local model was being flagged `nova_model_reading`.
        # Nova's own value is unchanged — its context defaults `extractor` to "nova" — so this
        # corrects the misnomer without moving anything that already depended on it.
        ambiguity_flags=(f"{context.extractor}_model_reading",),
    )
