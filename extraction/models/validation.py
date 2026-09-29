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
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from evidence.candidate import ObservationCandidate
from evidence.coordinates import ImagePoint
from units.measurement import Unit
from units.normalise import UnitNormalisationError, normalise_to_inches
from units.notation import canonical_notation, is_compound

#: Said once, because all four coordinates carry the same contract and four near-identical
#: sentences are how three of them end up saying something slightly different.
_BOX_DESCRIPTION = (
    "Pixel coordinate of the bounding box around the text you read, within the crop dimensions "
    "given in the message. The box must have non-zero width and height."
)


class NovaToolPayload(BaseModel):
    """The complete payload understood from Nova; unexpected fields are errors.

    **The descriptions are the contract, and they are load-bearing.** This model becomes the Bedrock
    tool schema, so they are the only thing a reader is told about what an acceptable answer looks
    like. Without them a model receives the bare names `reading`, `unit_guess`, `x1`..`y2` and has to
    guess — Amazon's own models guess Amazon's conventions and pass, and a different vendor does not.

    Measured on 2026-09-29 against `mistral.ministral-3-3b-instruct` on a real crop reading `12 3/4"`:
    with bare field names **0 of 3** replies were accepted, every one rejected for answering
    `unit_guess` as `"inch"` or `"inches"`; with these descriptions **4 of 4** were accepted. The
    model had read the crop correctly every single time. We were discarding correct readings over the
    spelling of a unit, and Ministral alone accounted for 851 of one run's 1,743 rejections (#718).

    An `enum` on `unit_guess` was measured too and is deliberately absent: it scored *worse* than
    descriptions alone (2 of 3), because constraining the field pushed the malformation into
    `reading` instead.

    Each description states what `validate_payload` below actually rejects. Change one and the other
    has to move with it, or the schema starts promising something the validator will not accept.
    """

    model_config = ConfigDict(extra="forbid")

    reading: str = Field(
        min_length=1,
        description=(
            'The dimension exactly as printed on the drawing. Use the inch mark " rather than the '
            'word "inches". Keep a fraction as a fraction: 12 3/4", never 12.75.'
        ),
    )
    unit_guess: str | None = Field(
        description=(
            'The unit of the reading, as a short code: "in" or "mm". Null when the drawing does '
            "not say."
        ),
    )
    x1: StrictInt = Field(description=_BOX_DESCRIPTION)
    y1: StrictInt = Field(description=_BOX_DESCRIPTION)
    x2: StrictInt = Field(description=_BOX_DESCRIPTION)
    y2: StrictInt = Field(description=_BOX_DESCRIPTION)


class CoordinateMode(StrEnum):
    """How the model's rectangle coordinates should be read."""

    PIXELS = "pixels"
    NOVA_GRID = "nova_grid"


@dataclass(frozen=True, slots=True)
class CropSize:
    """Trusted dimensions of the exact crop image sent to the reader."""

    width_px: int
    height_px: int

    def __post_init__(self) -> None:
        for name in ("width_px", "height_px"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")


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


def _stacked_fraction_refusal(reading: str, *, stacked: bool) -> str | None:
    """Why a reading contradicts the way its label was drawn, or `None` (#541).

    The one check here that does not read the string. `28 3/4"` came back from a real crop as `284`
    and was accepted, because `284` is a perfectly good dimension token — the string carries no
    evidence of what went wrong. The drawing does: the label was drawn in two bands, and a reading
    with no `/` in it cannot be a reading of two bands.

    **Fails closed and never corrects.** It does not say what the number should have been, only that
    what came back does not describe what was drawn. Same trade as the bare-fraction guard: a false
    abstention costs a reviewer one look at a crop, and the alternative is 28 3/4 inches silently
    becoming 284.
    """
    if not stacked:
        return None
    if "/" in reading:
        return None
    return (
        f"reading {reading!r} has no fraction, but the label was drawn in two bands. A stacked "
        "fraction absorbed into the digits reads as a valid dimension, so this abstains rather "
        "than accepting it"
    )


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
    # **The drawing's own notation, through `units.notation` (#733).** Before, only inch-mark spellings
    # were rewritten here, so a correct `25-1/2"`, `381 [15]` or `2" (VIF)` was refused as not a
    # dimension — every one of seven readers read `25-1/2"` right in the bake-off and every one was
    # thrown away. The candidate keeps the characters the model produced; only this check sees the
    # canonical form.
    probed = canonical_notation(reading)[0]
    try:
        measured = normalise_to_inches(probed, unmarked_unit=Unit.INCH)
    except UnitNormalisationError as error:
        return f"reading {reading!r} is not a dimension token: {error}"
    if measured.exact == 0:
        # `00` came back from a real crop and was accepted, because zero parses. A dimension line of
        # no length is not a dimension, nobody draws one, and a zero operand in a rule is a silent
        # way to satisfy a sum — so this is the one magnitude that says the reading failed rather
        # than that the drawing is unusual.
        return f"reading {reading!r} measures zero, which is not a dimension anything drew"
    # On the canonical form, not the characters returned: `19 [3/4]` does not look like a bare
    # fraction, but its value is one, and judging the raw text would let a dual token carry a
    # dropped whole number straight past the check that exists to stop it.
    if _BARE_FRACTION_RE.match(probed):
        return (
            f"reading {reading!r} is a fraction with no whole number. A dropped whole number reads "
            "as a valid dimension, so this abstains rather than accepting it"
        )
    return None


def _round_pixel(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _bounds_error(axis: str, value: int, maximum: int, crop_size: CropSize) -> ValueError | None:
    if 0 <= value <= maximum:
        return None
    return ValueError(
        f"{axis} coordinate {value} is outside the {crop_size.width_px}x{crop_size.height_px} "
        f"crop; expected 0..{maximum}"
    )


def _pixel_coordinate(value: int, *, axis: str, crop_size: CropSize) -> int:
    maximum = crop_size.width_px if axis == "x" else crop_size.height_px
    error = _bounds_error(axis, value, maximum, crop_size)
    if error is not None:
        raise error
    return value


def _nova_grid_coordinate(value: int, *, axis: str, crop_size: CropSize) -> int:
    grid_limit = 1000
    error = _bounds_error(axis, value, grid_limit, crop_size)
    if error is not None:
        raise error
    maximum = crop_size.width_px if axis == "x" else crop_size.height_px
    mapped = Decimal(value) * Decimal(maximum) / Decimal(grid_limit)
    pixel = _round_pixel(mapped)
    pixel_error = _bounds_error(axis, pixel, maximum, crop_size)
    if pixel_error is not None:
        raise pixel_error
    return pixel


def _rectangle_polygon(
    payload: NovaToolPayload,
    *,
    crop_size: CropSize,
    coordinate_mode: CoordinateMode,
) -> tuple[ImagePoint, ...]:
    coordinate = (
        _nova_grid_coordinate if coordinate_mode is CoordinateMode.NOVA_GRID else _pixel_coordinate
    )
    left = coordinate(payload.x1, axis="x", crop_size=crop_size)
    top = coordinate(payload.y1, axis="y", crop_size=crop_size)
    right = coordinate(payload.x2, axis="x", crop_size=crop_size)
    bottom = coordinate(payload.y2, axis="y", crop_size=crop_size)
    if right <= left or bottom <= top:
        raise ValueError(
            f"rectangle must have positive width and height inside the "
            f"{crop_size.width_px}x{crop_size.height_px} crop; got "
            f"x1={payload.x1}, y1={payload.y1}, x2={payload.x2}, y2={payload.y2}"
        )
    return (
        ImagePoint(left, top),
        ImagePoint(right, top),
        ImagePoint(right, bottom),
        ImagePoint(left, bottom),
    )


def validate_payload(
    payload: object,
    *,
    context: CandidateContext,
    crop_size: CropSize,
    coordinate_mode: CoordinateMode,
    recorder: RejectionRecorder,
    stacked_label: bool = False,
) -> ValidationOutcome:
    """Return a complete candidate or a recorded abstention, never a partial result.

    `stacked_label` is what the sheet's own geometry said about this crop — two glyph bands, so a
    stacked fraction (#541). It defaults to `False` because most callers have no geometry to offer
    and a caller that cannot say must not be treated as having said "stacked": that would refuse
    every reading without a `/`. The default is the direction that accepts, and the guard only
    engages where the drawing has been measured.
    """

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
        polygon = _rectangle_polygon(
            validated,
            crop_size=crop_size,
            coordinate_mode=coordinate_mode,
        )
    except (TypeError, ValueError) as error:
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason=(
                "coordinate_out_of_bounds"
                if "outside the" in str(error)
                else "candidate_conversion_failed"
            ),
            errors=(str(error),),
        )

    # **Its own reason, and before the shape check (#730, #733).** `39 1/4"+6"` is two dimensions and an
    # operator: there is no single value to accept or refuse, and a reviewer's next action — read both
    # — differs from the one "not a dimension" asks for. Adding them up would put arithmetic we did
    # into a reading the model did not make.
    if is_compound(validated.reading):
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason="not_a_single_value",
            errors=(f"reading {validated.reading!r} is two dimensions and an operator",),
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

    # Recorded under its own reason, not folded into the one above. "Not a dimension" sends a
    # reviewer to look at whether the crop holds a dimension at all; this one says the crop holds a
    # dimension the reader got wrong, and those are different next actions.
    stacked_refusal = _stacked_fraction_refusal(validated.reading, stacked=stacked_label)
    if stacked_refusal is not None:
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason="stacked_fraction_absorbed",
            errors=(stacked_refusal,),
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
        ambiguity_flags=(
            f"{context.extractor}_model_reading",
            f"{context.extractor}_rectangle_polygon_derived",
        ),
    )
