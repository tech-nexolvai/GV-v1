"""Strict model-payload validation into raw observation candidates.

Validation is fail-closed: an output is either a complete ``ObservationCandidate`` or
an explicitly recorded rejection. Unknown fields and binary floating-point values are
never silently coerced or discarded.

**A second request kind, digits (#865).** A piece of a stacked label — its whole number, its
numerator or its denominator — drawn alone is shown to a model, which answers one plain number.
`validate_digits_payload` holds that answer to the same rule the local reader of the same piece is
held to: one to three ASCII digits, exactly as many as the drawing has characters in the piece.

Source: ``docs/DESIGN_AI.md`` section 4.1 and issues #250, #834, #865.
Verification: ``tests/extraction/models/test_validation.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError
from pydantic.types import StringConstraints

from evidence.candidate import ObservationCandidate
from evidence.coordinates import ImagePoint
from extraction.glyph_bands import FractionLayout
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


#: The most digits one piece of a stacked label is read as (#848). A whole number of inches on a
#: cabinet or countertop drawing runs to three digits; a numerator or a denominator to two.
MAXIMUM_PIECE_DIGITS: Final = 3

#: One to three ASCII digits and nothing else. Not `str.isdigit`, which is true of `²` and of other
#: scripts' digits — none of them a number this drawing wrote. The one rule for a piece's reading,
#: whichever reader made it: `extraction/fraction_parts.py` holds local OCR to it too.
PIECE_DIGITS_RE: Final = re.compile(rf"[0-9]{{1,{MAXIMUM_PIECE_DIGITS}}}")

#: Why a digits answer was refused: not one to three ASCII digits, or not as many as the piece has.
DIGITS_NOT_A_NUMBER: Final = "digits_not_a_number"
DIGITS_WRONG_COUNT: Final = "digits_wrong_count"


class DigitsToolPayload(BaseModel):
    """What a model may answer about one piece of a stacked label: its digits, and nothing else.

    **One field, a string.** Not an integer: `08` and `8` are different readings of a drawing, and a
    number type would make them the same answer. `StrictStr`, so a JSON number is refused rather than
    turned into text the model did not write. The description is the contract, as it is for
    `NovaToolPayload`: it says what `validate_digits_payload` accepts.
    """

    model_config = ConfigDict(extra="forbid")

    digits: StrictStr = Field(
        min_length=1,
        description=(
            "The number drawn in the picture, written as its digits only, such as 28, 3 or 16: one "
            "to three digits, with no unit, no fraction, no spaces and no words."
        ),
    )


class ReadingOnlyPayload(BaseModel):
    """What a reader on the plain-JSON answer path may answer: its reading, or `null` (#907).

    **No rectangle.** The stage places a vision reading at the region it sent and never where a
    model says it is (`workflow.stages._record_vision_candidate`), so a rectangle was only ever a
    bounds check — and asking a reader whose answer space was never measured for one is how a
    rectangle in the wrong units passes that check (#664). So a reader on this path is asked for
    none, and its candidate is placed at the whole crop it was shown (`CoordinateMode.CROP`).

    **`null` is an answer.** The reader saying there is no dimension it can read here — cut off,
    unclear, or none — which the teaching prompt asks for rather than a guess. It is recorded as
    an abstention under `READER_GAVE_NO_READING`, never turned into a reading.
    """

    model_config = ConfigDict(extra="forbid")

    reading: Annotated[StrictStr, StringConstraints(min_length=1)] | None = Field(
        description="The dimension exactly as printed, or null when there is none to read."
    )


#: Why a reader on the plain-JSON path gave no candidate when it answered `null` (#907).
READER_GAVE_NO_READING: Final = "reader_gave_no_reading"


class CoordinateMode(StrEnum):
    """How the model's rectangle coordinates should be read."""

    PIXELS = "pixels"
    NOVA_GRID = "nova_grid"
    CROP = "crop"
    """No rectangle was asked for (#907): the reader answers on the plain-JSON path
    (`ReadingOnlyPayload`), and its candidate is placed at the whole crop it was shown."""


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


#: The reason a reading of a stacked fraction is recorded under. Its own reason, so a reviewer knows
#: the drawing's layout sent it — not a bad reading, and not a reading that is not a dimension.
STACKED_FRACTION_REASON: Final = "stacked_fraction_requires_review"


def _stacked_fraction_refusal(reading: str, *, stacked: bool) -> str | None:
    """Why a reading of this crop may not be accepted, or `None` (#541, #735).

    The one check here that does not read the string. **A crop that shows a stacked fraction always
    abstains, whatever came back** — the admin's rule on #726. It used to abstain only when the reading
    had no `/`, which caught `28 3/4"` read as `284`. It did not catch what the first human-keyed
    bake-off found: a stacked `3/4"` read as `3 3/4"` by two readers from different vendors, who then
    agreed. That reading has its `/`, parses, and would have been sealed by the agreement gate. The
    string carries no evidence of what went wrong, so no rule about the string can be the guard.

    **Fails closed and never corrects.** It does not say what the number should have been. A false
    abstention costs a reviewer one look at a crop; a missed one is a wrong dimension with two readers
    vouching for it.
    """
    if not stacked:
        return None
    return (
        f"reading {reading!r} comes from a crop that shows a stacked fraction. Readers mistake a "
        "stacked numerator for a whole number and agree with each other doing it, so a stacked "
        "fraction always goes to a reviewer"
    )


#: The reason a reading of a stacked label is recorded under when the drawing contradicts it (#834).
#: Its own reason, because it says more than `STACKED_FRACTION_REASON`: not only that the label goes
#: to a reviewer, but that this reading cannot be what the label shows.
STACKED_LAYOUT_REASON: Final = "reading_contradicts_stacked_layout"

#: A reading written the way a stacked label is drawn: a whole number or none, a fraction, and an inch
#: mark or none. Matched on the canonical form, where `39-1/2"` is already `39 1/2"`.
_STACKED_READING_RE = re.compile(
    r'^\s*(?:(?P<whole>\d+)\s+)?(?P<numerator>\d+)\s*/\s*(?P<denominator>\d+)\s*"?\s*$'
)


def _counted(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def stacked_layout_refusal(reading: str, layouts: Sequence[FractionLayout]) -> str | None:
    """Why a reading cannot be any of the stacked labels these layouts describe, or `None` (#834).

    **The false PASS this exists for.** Two readers from different vendors read a stacked `3/4"` as
    `3 3/4"` and agreed (#726). Nothing in the string shows it — it has its `/` and parses — but the
    drawing does: the label has no whole number. So a reading is checked against where the label's
    parts were drawn (`extraction/glyph_bands.py`): as many whole-number digits as the drawing has
    characters before the fraction, as many above the bar and as many below. `9 1/2"` read from a
    `39 1/2"` is refused the same way.

    **It reads the string and the layout and nothing else**, so a reading from any lane — a model,
    OCR, the shape reader — can be held to it; `validate_payload` holds the model readers' to it.
    A reading shaped otherwise is refused too: no fraction (`284` for `28 3/4"`), feet and inches,
    or a millimetre number beside the inches, since a stacked label is drawn as one number with a
    fraction. Where a crop shows more than one stacked label, matching any one of them is enough,
    because the reading may be of any of them.

    **What it does not do.** It counts; it does not read. `3/8"` for a `3/4"` has the right count and
    passes, so this never makes a reading right — it only refuses one the drawing rules out. The
    inch mark is not compared: it is not a digit, and readers drop it and add it. With no layouts —
    no stacked label in the crop, or one set in text, which has no paths to count — it says nothing.
    """
    if not layouts:
        return None
    canonical, millimetres = canonical_notation(reading)
    shaped = _STACKED_READING_RE.match(canonical) if millimetres is None else None
    drawn = "; ".join(
        f"{_counted(len(layout.whole), 'whole-number character')} and "
        f"{len(layout.numerator)} over {len(layout.denominator)}"
        for layout in layouts
    )
    label = (
        "the stacked label this crop shows is"
        if len(layouts) == 1
        else f"the {len(layouts)} stacked labels this crop shows are"
    )
    if shaped is None:
        return (
            f"reading {reading!r} is not one number with a fraction, and {label} drawn as one "
            f"({drawn})"
        )
    counts = (
        len(shaped["whole"] or ""),
        len(shaped["numerator"]),
        len(shaped["denominator"]),
    )
    if any(
        counts == (len(layout.whole), len(layout.numerator), len(layout.denominator))
        for layout in layouts
    ):
        return None
    return (
        f"reading {reading!r} has {_counted(counts[0], 'whole-number digit')} and "
        f"{counts[1]} over {counts[2]}, but {label} drawn with {drawn}, so it cannot be what the "
        "drawing says"
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


def _crop_polygon(crop_size: CropSize) -> tuple[ImagePoint, ...]:
    """The whole crop, for a reader asked for no rectangle: where it read is all it was shown."""
    return (
        ImagePoint(0, 0),
        ImagePoint(crop_size.width_px, 0),
        ImagePoint(crop_size.width_px, crop_size.height_px),
        ImagePoint(0, crop_size.height_px),
    )


def validate_payload(
    payload: object,
    *,
    context: CandidateContext,
    crop_size: CropSize,
    coordinate_mode: CoordinateMode,
    recorder: RejectionRecorder,
    stacked_label: bool,
    stacked_layouts: Sequence[FractionLayout],
) -> ValidationOutcome:
    """Return a complete candidate or a recorded abstention, never a partial result.

    `stacked_label` is what the sheet's own geometry said about this crop: that it shows a stacked
    fraction the bar detector found (`extraction/glyph_bands.py`, #735). **It has no default.** It
    had one, `False`, and both production callers relied on it — so the guard it gates was tested,
    worked when called, and never ran (#735). Every caller now states what it knows; a caller with
    no geometry says `False` in its own code, where a reader can see the gap.

    `stacked_layouts` is where those labels' parts were drawn (#834), and has no default for the
    same reason: empty where the crop shows no stacked label, or only ones set in text. A reading
    they contradict is refused under `STACKED_LAYOUT_REASON` (`stacked_layout_refusal`). Layouts
    with `stacked_label` `False` contradict each other; that is the caller's mistake, not the
    model's, so it raises `ValueError` rather than recording a refusal of the reading.

    **`CoordinateMode.CROP` is the plain-JSON answer path (#907)**: the payload is a
    `ReadingOnlyPayload`, a `null` reading is recorded under `READER_GAVE_NO_READING`, and the
    candidate is placed at the whole crop. Every check of the reading itself — floats, compounds,
    the shape check, both stacked-fraction refusals — is the same one, in the same order, whichever
    path the answer came by.
    """
    if stacked_layouts and not stacked_label:
        raise ValueError(
            "stacked_layouts describe stacked labels in this crop, so stacked_label must be True"
        )

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

    validated: NovaToolPayload | ReadingOnlyPayload
    try:
        validated = (
            ReadingOnlyPayload.model_validate(payload)
            if coordinate_mode is CoordinateMode.CROP
            else NovaToolPayload.model_validate(payload)
        )
    except ValidationError as error:
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason="schema_validation_failed",
            errors=_validation_errors(error),
        )

    if validated.reading is None:
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason=READER_GAVE_NO_READING,
            errors=("the reader answered that there is no dimension it can read in this crop",),
        )
    reading = validated.reading

    try:
        if isinstance(validated, ReadingOnlyPayload):
            unit = None
            polygon = _crop_polygon(crop_size)
        else:
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
    if is_compound(reading):
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason="not_a_single_value",
            errors=(f"reading {reading!r} is two dimensions and an operator",),
        )

    # Before the candidate exists, because a candidate is a reading somebody may act on.
    refusal = _reading_refusal(reading)
    # A dimension, or a bare fraction — which on a stacked crop may be the right reading (see below).
    dimension_shaped = refusal is None or bool(
        _BARE_FRACTION_RE.match(canonical_notation(reading)[0])
    )

    # **Before the general stacked refusal, because it says more** (#834): this reading is not only
    # sent to a reviewer, the drawing rules it out. Only for a dimension-shaped reading, for the
    # reason given below: a reading that is not a dimension at all keeps that, truer, reason.
    layout_refusal = stacked_layout_refusal(reading, stacked_layouts)
    if layout_refusal is not None and dimension_shaped:
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason=STACKED_LAYOUT_REASON,
            errors=(layout_refusal,),
        )

    # **On a stacked crop, the layout is the reason** — recorded under its own reason so a reviewer
    # knows the drawing sent it, not a bad reading (#735). That holds for every dimension-shaped
    # reading, and for a bare fraction too: `3/4"` is the *right* reading of a stacked `3/4"`, and
    # the bare-fraction guard would otherwise refuse it as suspect on its face. Only a reading that is
    # not a dimension at all keeps that reason: it is the truer one, and most of the detector's false
    # alarms — hatching beside a label — land there.
    stacked_refusal = _stacked_fraction_refusal(reading, stacked=stacked_label)
    if stacked_refusal is not None and dimension_shaped:
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason=STACKED_FRACTION_REASON,
            errors=(stacked_refusal,),
        )

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
        raw_text=reading,
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
            (
                f"{context.extractor}_crop_polygon"
                if coordinate_mode is CoordinateMode.CROP
                else f"{context.extractor}_rectangle_polygon_derived"
            ),
        ),
    )


def validate_digits_payload(
    payload: object,
    *,
    context: CandidateContext,
    digit_count: int,
    recorder: RejectionRecorder,
) -> str | ValidationRejection:
    """The digits a model read in one piece of a stacked label, or a recorded refusal (#865).

    `digit_count` is how many characters the drawing has in the piece (`FractionLayout`): the
    answer must be exactly that many ASCII digits, one to three. **The count is the drawing's, never
    the model's**, so a piece read with a digit the drawing does not have — `33` for a `3` — is
    refused here, before anything compares it with another reader.

    **Fails closed and never corrects**, like `validate_payload`: a float anywhere, a field that is
    not asked for, a number where a string belongs, anything but digits, or the wrong number of them
    is a recorded refusal. Nothing here turns the digits into a value; the caller puts the label
    together in code.
    """
    if (
        isinstance(digit_count, bool)
        or not isinstance(digit_count, int)
        or not 1 <= digit_count <= MAXIMUM_PIECE_DIGITS
    ):
        raise ValueError(f"digit_count must be a whole number from 1 to {MAXIMUM_PIECE_DIGITS}")
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
        validated = DigitsToolPayload.model_validate(payload)
    except ValidationError as error:
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason="schema_validation_failed",
            errors=_validation_errors(error),
        )
    if not PIECE_DIGITS_RE.fullmatch(validated.digits):
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason=DIGITS_NOT_A_NUMBER,
            errors=(
                f"digits {validated.digits!r} are not one to {MAXIMUM_PIECE_DIGITS} ASCII digits",
            ),
        )
    if len(validated.digits) != digit_count:
        return _record_rejection(
            payload=payload,
            context=context,
            recorder=recorder,
            reason=DIGITS_WRONG_COUNT,
            errors=(
                (
                    f"digits {validated.digits!r} are "
                    f"{_counted(len(validated.digits), 'digit')}, and the drawing has "
                    f"{_counted(digit_count, 'character')} in this piece"
                ),
            ),
        )
    return validated.digits
