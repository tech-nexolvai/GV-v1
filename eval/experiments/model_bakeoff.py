"""Per-model crop-reading bake-off for the Phase C vision-reader choice.

This measures model outputs as extraction candidates only. It never seals evidence,
never writes findings, and never imports the verdict path. The comparison is exact
measurement arithmetic through ``units/`` rather than fuzzy string matching.

Source: issue #637 and ``docs/V1_TO_WORKING_PLAN.md`` Phase C.
Verification: ``tests/eval/test_model_bakeoff.py``.
"""

from __future__ import annotations

import binascii
import csv
import io
import json
import struct
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from importlib import import_module
from itertools import combinations
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from eval.gold_set.schema import GoldCase
from extraction.models.context import AssembledContext, NearbyText
from extraction.models.validation import ValidationRejection
from rules.semantic_types import OperandSource
from units.imperial import ImperialParseError
from units.measurement import Measurement, Unit, to_exact_fraction
from units.normalise import UnitNormalisationError, normalise_to_inches

HARD_CASE_TAGS = ("fraction", "rotated", "small_glyph")
DEFAULT_PAIRINGS = (
    ("nova-pro", "claude-haiku-4.5"),
    ("nova-pro", "qwen3-vl-235b"),
    ("claude-sonnet-4.6", "nova-pro"),
)
_MILLION = Decimal(1000000)
_POINTS_PER_INCH = Decimal(72)
_ANSWER_KEY = "answer_key.json"
_BAKEOFF_METADATA = "model_bakeoff_metadata.json"
_SELF_VERIFIED_MARKERS = (
    "self-verified",
    "machine-self-verified",
    "not per-case human-read",
    "heuristic-unconfirmed",
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ModelBakeoffError(ValueError):
    """The bake-off inputs are malformed or incomplete."""


class ReadingParseError(ValueError):
    """A model or answer-key reading could not be parsed as an exact dimension."""


@dataclass(frozen=True, slots=True)
class Crop:
    """One fixed crop and its human-reviewed answer value."""

    crop_id: str
    expected: Measurement
    image: bytes = b""
    tags: frozenset[str] = frozenset()
    image_format: Literal["jpeg", "png"] = "png"
    page: int = 0
    context: AssembledContext = field(
        default_factory=lambda: AssembledContext(nearby_text=(), nearby_geometry=())
    )
    bound_pt: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if not self.crop_id.strip():
            raise ValueError("crop_id must be non-empty")
        if not isinstance(self.expected, Measurement):
            raise TypeError("expected must be a Measurement")
        if not isinstance(self.image, bytes):
            raise TypeError("image must be bytes")
        unknown = sorted(tag for tag in self.tags if tag not in HARD_CASE_TAGS)
        if unknown:
            raise ValueError(f"unknown hard-case tag(s): {unknown}")
        if self.image_format not in {"jpeg", "png"}:
            raise ValueError("image_format must be 'jpeg' or 'png'")
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 0:
            raise ValueError("page must be a non-negative integer")
        if not isinstance(self.bound_pt, Decimal) or not self.bound_pt.is_finite():
            raise ValueError("bound_pt must be a finite Decimal")


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One model and the token prices supplied for this run."""

    name: str
    model_id: str
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal

    def __post_init__(self) -> None:
        for field_name in ("name", "model_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        for field_name in ("input_usd_per_million", "output_usd_per_million"):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{field_name} must be a finite non-negative Decimal")


@dataclass(frozen=True, slots=True)
class ModelRead:
    """What one model returned for one crop, plus accounting metadata."""

    model_name: str
    model_id: str
    crop_id: str
    raw_text: str | None
    unit_guess: Unit | None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    error: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("model_name", "model_id", "crop_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        for field_name in ("input_tokens", "output_tokens", "latency_ms"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.raw_text is not None and not isinstance(self.raw_text, str):
            raise TypeError("raw_text must be a string or None")
        if self.unit_guess is not None and not isinstance(self.unit_guess, Unit):
            raise TypeError("unit_guess must be a Unit or None")
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("error must be a string or None")


class BakeoffAdapter(Protocol):
    """The small adapter surface the pure harness needs."""

    @property
    def spec(self) -> ModelSpec:
        """The model identity and pricing supplied for this run."""
        ...

    def read(self, crop: Crop) -> ModelRead:
        """Read one crop and return the raw candidate reading plus accounting."""


@dataclass(frozen=True, slots=True)
class ScoredRead:
    """One model read after exact local scoring."""

    read: ModelRead
    expected: Measurement
    parsed: Measurement | None
    exact: bool
    parse_error: str | None = None

    @property
    def scoreable(self) -> bool:
        return self.parsed is not None


@dataclass(frozen=True, slots=True)
class ModelScore:
    """Aggregated score and accounting for one model."""

    spec: ModelSpec
    reads: tuple[ScoredRead, ...]
    tags_by_crop: Mapping[str, frozenset[str]]

    @property
    def exact_count(self) -> int:
        return sum(1 for read in self.reads if read.exact)

    @property
    def total(self) -> int:
        return len(self.reads)

    @property
    def exact_rate(self) -> Fraction | None:
        return _rate(self.exact_count, self.total)

    def tag_rate(self, tag: str) -> Fraction | None:
        tagged = [read for read in self.reads if tag in self.tags_by_crop[read.read.crop_id]]
        if not tagged:
            return None
        return Fraction(sum(1 for read in tagged if read.exact), len(tagged))

    @property
    def input_tokens(self) -> int:
        return sum(read.read.input_tokens for read in self.reads)

    @property
    def output_tokens(self) -> int:
        return sum(read.read.output_tokens for read in self.reads)

    @property
    def total_latency_ms(self) -> int:
        return sum(read.read.latency_ms for read in self.reads)

    @property
    def average_latency_ms(self) -> Decimal | None:
        if not self.reads:
            return None
        return Decimal(self.total_latency_ms) / Decimal(len(self.reads))

    @property
    def cost_usd(self) -> Decimal:
        return (
            Decimal(self.input_tokens) * self.spec.input_usd_per_million
            + Decimal(self.output_tokens) * self.spec.output_usd_per_million
        ) / _MILLION

    @property
    def error_count(self) -> int:
        return sum(1 for read in self.reads if read.read.error or read.parse_error)


@dataclass(frozen=True, slots=True)
class PairwiseScore:
    """Agreement signal for a two-reader pairing."""

    left: str
    right: str
    total: int
    disagreements: int
    both_exact: int

    @property
    def disagree_rate(self) -> Fraction | None:
        return _rate(self.disagreements, self.total)

    @property
    def both_exact_rate(self) -> Fraction | None:
        return _rate(self.both_exact, self.total)

    @property
    def names(self) -> frozenset[str]:
        return frozenset((self.left, self.right))


@dataclass(frozen=True, slots=True)
class BakeoffScorecard:
    """The complete result of one reproducible crop bake-off."""

    crops: tuple[Crop, ...]
    models: tuple[ModelScore, ...]
    pairwise: tuple[PairwiseScore, ...]
    required_pairs: tuple[tuple[str, str], ...] = DEFAULT_PAIRINGS

    @property
    def recommendation(self) -> str:
        return recommend(self)


def run_bakeoff(
    models: Sequence[BakeoffAdapter],
    crops: Sequence[Crop],
    *,
    required_pairs: Sequence[tuple[str, str]] = DEFAULT_PAIRINGS,
) -> BakeoffScorecard:
    """Run every model over every crop and return an exact scorecard.

    The adapters are the only effectful piece. Everything after ``read`` is pure local scoring, so
    tests can use stubs and CI never needs Bedrock credentials.
    """

    if not crops:
        raise ModelBakeoffError("cannot run a model bake-off over zero crops")
    if not models:
        raise ModelBakeoffError("cannot run a model bake-off over zero models")
    names = [adapter.spec.name for adapter in models]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ModelBakeoffError(f"duplicate model name(s): {duplicates}")

    tags_by_crop = {crop.crop_id: crop.tags for crop in crops}

    score_by_model: dict[str, ModelScore] = {}
    by_model_crop: dict[tuple[str, str], ScoredRead] = {}
    for adapter in models:
        scored: list[ScoredRead] = []
        for crop in crops:
            read = adapter.read(crop)
            if read.crop_id != crop.crop_id:
                raise ModelBakeoffError(
                    f"adapter {adapter.spec.name!r} returned crop {read.crop_id!r} "
                    f"while scoring {crop.crop_id!r}"
                )
            if read.model_name != adapter.spec.name:
                raise ModelBakeoffError(
                    f"adapter {adapter.spec.name!r} returned model {read.model_name!r}"
                )
            scored_read = score_read(read, crop.expected)
            scored.append(scored_read)
            by_model_crop[(adapter.spec.name, crop.crop_id)] = scored_read
        score_by_model[adapter.spec.name] = ModelScore(
            adapter.spec,
            tuple(scored),
            tags_by_crop,
        )

    pair_names = _pair_names(tuple(score_by_model), tuple(required_pairs))
    return BakeoffScorecard(
        crops=tuple(crops),
        models=tuple(score_by_model[name] for name in names),
        pairwise=tuple(
            _score_pair(left, right, crops, by_model_crop) for left, right in pair_names
        ),
        required_pairs=tuple(required_pairs),
    )


def score_read(read: ModelRead, expected: Measurement) -> ScoredRead:
    """Parse and compare one model read exactly."""

    if read.error is not None:
        return ScoredRead(read=read, expected=expected, parsed=None, exact=False, parse_error=None)
    if read.raw_text is None:
        return ScoredRead(
            read=read,
            expected=expected,
            parsed=None,
            exact=False,
            parse_error="no reading returned",
        )
    try:
        parsed = parse_dimension_reading(read.raw_text, read.unit_guess)
    except ReadingParseError as error:
        return ScoredRead(
            read=read,
            expected=expected,
            parsed=None,
            exact=False,
            parse_error=str(error),
        )
    return ScoredRead(
        read=read,
        expected=expected,
        parsed=parsed,
        exact=(parsed.exact, parsed.unit) == (expected.exact, expected.unit),
    )


def parse_dimension_reading(reading: str, unit_guess: Unit | None) -> Measurement:
    """Parse a model reading without fuzzy matching or inferred units."""

    token = reading.strip()
    if not token:
        raise ReadingParseError("reading is empty")

    lowered = token.lower()
    if lowered.endswith("mm"):
        numeric = token[:-2].strip()
        return _measurement(numeric, Unit.MM, reading)
    if lowered.endswith((" in", " inch", " inches")) or token.endswith('"') or "'" in token:
        try:
            return normalise_to_inches(token, unmarked_unit=Unit.INCH)
        except UnitNormalisationError as error:
            raise ReadingParseError(str(error)) from error
    if unit_guess is None:
        raise ReadingParseError(
            f"{reading!r} has no unit marker and the model supplied no unit_guess"
        )
    return _measurement(token, unit_guess, reading)


def recommend(scorecard: BakeoffScorecard) -> str:
    """Return a one-paragraph, data-derived recommendation with no hidden threshold."""

    if not scorecard.pairwise:
        return (
            "Recommendation: no pair could be evaluated, so the reading pair cannot be confirmed."
        )
    ranked = sorted(
        scorecard.pairwise,
        key=lambda pair: (
            pair.both_exact_rate or Fraction(-1),
            Fraction(pair.total - pair.disagreements, pair.total) if pair.total else Fraction(-1),
        ),
        reverse=True,
    )
    best = ranked[0]
    default_pair = frozenset(DEFAULT_PAIRINGS[0])
    if best.names == default_pair:
        action = "confirm Nova Pro + Claude Haiku 4.5"
    else:
        action = f"revise toward {best.left} + {best.right}"

    default = _find_pair(scorecard.pairwise, *DEFAULT_PAIRINGS[0])
    default_text = (
        "the default pair was not present in this run"
        if default is None
        else (
            "Nova Pro + Claude Haiku 4.5 scored "
            f"{_format_rate(default.both_exact_rate)} both-exact and "
            f"{_format_rate(default.disagree_rate)} disagreement"
        )
    )
    return (
        f"Recommendation: {action}. The strongest measured pair is {best.left} + {best.right} "
        f"with {_format_rate(best.both_exact_rate)} both-exact reads and "
        f"{_format_rate(best.disagree_rate)} disagreement; {default_text}."
    )


def render_markdown(scorecard: BakeoffScorecard) -> str:
    """Render a human-readable Markdown scorecard."""

    lines = [
        "# Model Dimension-Read Bake-Off",
        "",
        (
            "| Model | Exact read rate | Fractions | Rotated | Small glyphs | Input tokens | "
            "Output tokens | Avg latency ms | Cost USD | Errors |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for score in scorecard.models:
        lines.append(
            "| "
            + " | ".join(
                [
                    score.spec.name,
                    _format_rate(score.exact_rate),
                    _format_rate(score.tag_rate("fraction")),
                    _format_rate(score.tag_rate("rotated")),
                    _format_rate(score.tag_rate("small_glyph")),
                    str(score.input_tokens),
                    str(score.output_tokens),
                    _format_decimal(score.average_latency_ms),
                    _format_money(score.cost_usd),
                    str(score.error_count),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "| Pair | Disagree / review rate | Both exact | Disagreements |",
            "|---|---:|---:|---:|",
        ]
    )
    for pair in scorecard.pairwise:
        lines.append(
            f"| {pair.left} + {pair.right} | {_format_rate(pair.disagree_rate)} | "
            f"{_format_rate(pair.both_exact_rate)} | {pair.disagreements}/{pair.total} |"
        )
    lines.extend(["", scorecard.recommendation, ""])
    return "\n".join(lines)


def render_csv(scorecard: BakeoffScorecard) -> str:
    """Render scorecard rows as CSV with explicit row types."""

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "kind",
            "model",
            "pair",
            "metric",
            "value",
            "count",
            "total",
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "cost_usd",
        ]
    )
    for score in scorecard.models:
        writer.writerow(
            [
                "model",
                score.spec.name,
                "",
                "exact_read_rate",
                _format_rate(score.exact_rate),
                score.exact_count,
                score.total,
                score.input_tokens,
                score.output_tokens,
                score.total_latency_ms,
                _format_money(score.cost_usd),
            ]
        )
        for tag in HARD_CASE_TAGS:
            tagged = [read for read in score.reads if tag in score.tags_by_crop[read.read.crop_id]]
            writer.writerow(
                [
                    "model",
                    score.spec.name,
                    "",
                    f"{tag}_exact_rate",
                    _format_rate(score.tag_rate(tag)),
                    sum(1 for read in tagged if read.exact),
                    len(tagged),
                    "",
                    "",
                    "",
                    "",
                ]
            )
    for pair in scorecard.pairwise:
        writer.writerow(
            [
                "pair",
                "",
                f"{pair.left}+{pair.right}",
                "disagree_rate",
                _format_rate(pair.disagree_rate),
                pair.disagreements,
                pair.total,
                "",
                "",
                "",
                "",
            ]
        )
    writer.writerow(
        ["recommendation", "", "", "text", scorecard.recommendation, "", "", "", "", "", ""]
    )
    return output.getvalue()


class BedrockBakeoffAdapter:
    """Bake-off adapter backed by the existing forced-tool Bedrock adapter."""

    def __init__(
        self,
        spec: ModelSpec,
        *,
        prompt_id: str = "dimension-reader-v1",
        template_id: str = "bounded-crop-v1",
        connect_timeout_seconds: int = 10,
        read_timeout_seconds: int = 120,
        region_name: str | None = None,
    ) -> None:
        nova = import_module("extraction.models.nova")
        self.spec = spec
        self._sink = _InvocationSink()
        config = nova.NovaConfig(
            model_id=spec.model_id,
            prompt_id=prompt_id,
            template_id=template_id,
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
            max_attempts=1,
            region_name=region_name,
        )
        self._adapter = nova.NovaAdapter.from_environment(config, self._sink)

    def read(self, crop: Crop) -> ModelRead:
        nova = import_module("extraction.models.nova")
        if not crop.image:
            raise ModelBakeoffError(f"crop {crop.crop_id!r} has no rendered image bytes")
        before = len(self._sink.invocations)
        try:
            candidate = self._adapter.extract(
                nova.NovaRequest(
                    candidate_id=f"bakeoff:{crop.crop_id}:{self.spec.name}",
                    page=crop.page,
                    crop=crop.image,
                    image_format=crop.image_format,
                    context=crop.context,
                    bound_pt=crop.bound_pt,
                )
            )
            attempts = self._sink.invocations[before:]
            return ModelRead(
                model_name=self.spec.name,
                model_id=self.spec.model_id,
                crop_id=crop.crop_id,
                raw_text=candidate.raw_text,
                unit_guess=candidate.unit_guess,
                input_tokens=sum(attempt.input_tokens for attempt in attempts),
                output_tokens=sum(attempt.output_tokens for attempt in attempts),
                latency_ms=sum(attempt.latency_ms for attempt in attempts),
            )
        except (nova.NovaAdapterError, OSError) as error:
            attempts = self._sink.invocations[before:]
            return ModelRead(
                model_name=self.spec.name,
                model_id=self.spec.model_id,
                crop_id=crop.crop_id,
                raw_text=None,
                unit_guess=None,
                input_tokens=sum(attempt.input_tokens for attempt in attempts),
                output_tokens=sum(attempt.output_tokens for attempt in attempts),
                latency_ms=sum(attempt.latency_ms for attempt in attempts),
                error=error.__class__.__name__,
            )


class _InvocationSink:
    def __init__(self) -> None:
        self.invocations: list[Any] = []
        self.rejections: list[ValidationRejection] = []

    def record(self, invocation: Any) -> None:
        self.invocations.append(invocation)

    def record_rejection(self, rejection: ValidationRejection) -> None:
        self.rejections.append(rejection)


class _MeasurementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exact: str
    unit: Unit
    raw_text: str | None = None

    @field_validator("exact", mode="before")
    @classmethod
    def _exact_is_text(cls, value: object) -> object:
        if isinstance(value, (bool, float, int)):
            raise ValueError(  # noqa: TRY004 - Pydantic must attach the field path.
                "measurement exact must be authored as exact text"
            )
        return value


class _CropInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    image: Path
    image_format: Literal["jpeg", "png"] = "png"
    expected: _MeasurementInput
    tags: tuple[Literal["fraction", "rotated", "small_glyph"], ...] = ()
    page: int = Field(default=0, ge=0)
    bound_pt: Decimal = Decimal(0)
    nearby_text: tuple[str, ...] = ()


class _CropManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crops: tuple[_CropInput, ...] = Field(min_length=1)


class _BakeoffMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provenance: str | None = None
    tags: Mapping[str, tuple[Literal["fraction", "rotated", "small_glyph"], ...]] = {}


class _ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal

    @field_validator("input_usd_per_million", "output_usd_per_million", mode="before")
    @classmethod
    def _price_is_exact(cls, value: object) -> object:
        if isinstance(value, (bool, float)):
            raise ValueError(  # noqa: TRY004 - Pydantic must attach the field path.
                "model prices must be authored as exact text or integers"
            )
        return value


class _ModelManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: tuple[_ModelInput, ...] = Field(min_length=1)


def render_crop(
    pdf: bytes,
    *,
    page: int,
    polygon: tuple[int, int, int, int],
    polygon_dpi: int,
    output_dpi: int = 600,
) -> bytes:
    """Render one answer-key polygon from a PDF page as PNG bytes.

    ``polygon`` is a top-left-origin image-space box authored at ``polygon_dpi``. The PDF crop is
    computed exactly in points, then rendered directly by PDFium so the model sees only the
    bounded crop rather than a whole sheet.
    """

    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ModelBakeoffError("answer-key page must be a one-based positive integer")
    for name, value in (("polygon_dpi", polygon_dpi), ("output_dpi", output_dpi)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ModelBakeoffError(f"{name} must be a positive integer")
    left_px, top_px, right_px, bottom_px = polygon
    if right_px <= left_px or bottom_px <= top_px:
        raise ModelBakeoffError(f"polygon has no area: {polygon!r}")

    pdfium = import_module("pypdfium2")
    document = pdfium.PdfDocument(pdf)
    try:
        try:
            pdf_page = document[page - 1]
        except Exception as error:
            raise ModelBakeoffError(f"page {page} is not in the source PDF: {error}") from error
        width_pt, height_pt = (Decimal(str(value)) for value in pdf_page.get_size())
        scale_to_pt = _POINTS_PER_INCH / Decimal(polygon_dpi)
        left = Decimal(left_px) * scale_to_pt
        right = Decimal(right_px) * scale_to_pt
        top = height_pt - Decimal(top_px) * scale_to_pt
        bottom = height_pt - Decimal(bottom_px) * scale_to_pt
        if left < 0 or bottom < 0 or right > width_pt or top > height_pt:
            raise ModelBakeoffError(
                f"polygon {polygon!r} at {polygon_dpi} dpi falls outside page {page}"
            )
        bitmap = pdf_page.render(
            scale=float(Decimal(output_dpi) / _POINTS_PER_INCH),
            crop=(
                float(left),
                float(bottom),
                float(width_pt - right),
                float(height_pt - top),
            ),
            rev_byteorder=True,
        )
        return _png_from_pdfium(bitmap)
    finally:
        document.close()


def load_crops(path: str | Path, *, polygon_dpi: int) -> tuple[Crop, ...]:
    """Load and render crops from a human-read gold-case directory."""

    case_dir = Path(path)
    if case_dir.is_dir():
        return _load_case_directory(case_dir, polygon_dpi=polygon_dpi)

    # Backward-compatible synthetic manifest support for local tests and ad-hoc dry runs. The
    # issue path uses a case directory with PDFs + answer_key.json.
    return _load_crop_manifest(case_dir)


def _load_crop_manifest(path: Path) -> tuple[Crop, ...]:
    """Load a synthetic crop manifest whose images are already files."""

    try:
        manifest = _CropManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise ModelBakeoffError(f"invalid crop manifest {path}: {error}") from error
    base = path.parent
    return tuple(_crop_from_input(item, base=base) for item in manifest.crops)


def _load_case_directory(case_dir: Path, *, polygon_dpi: int) -> tuple[Crop, ...]:
    answer_key = case_dir / _ANSWER_KEY
    try:
        case = GoldCase.model_validate(json.loads(answer_key.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise ModelBakeoffError(f"invalid human answer key {answer_key}: {error}") from error

    metadata = _load_bakeoff_metadata(case_dir / _BAKEOFF_METADATA)
    _refuse_machine_self_verified(case, metadata)

    pdf_cache: dict[OperandSource, bytes] = {}
    crops: list[Crop] = []
    for index, observation in enumerate(case.ground_truth.observations):
        source = observation.source
        if source not in {OperandSource.SHOP, OperandSource.ARCH}:
            raise ModelBakeoffError(
                f"observation {index} uses {source.value}; model bake-off can render only SHOP/ARCH PDFs"
            )
        pdf = pdf_cache.get(source)
        if pdf is None:
            pdf_path = case_dir / (case.shop if source is OperandSource.SHOP else case.arch)
            try:
                pdf = pdf_path.read_bytes()
            except OSError as error:
                raise ModelBakeoffError(f"could not read source PDF {pdf_path}: {error}") from error
            pdf_cache[source] = pdf
        crop_id = f"{case.id}:{index}:{source.value}:p{observation.page}"
        crops.append(
            Crop(
                crop_id=crop_id,
                expected=observation.value,
                image=render_crop(
                    pdf,
                    page=observation.page,
                    polygon=observation.polygon,
                    polygon_dpi=polygon_dpi,
                ),
                tags=_tags_for(crop_id, index, observation.value, metadata),
                image_format="png",
                page=observation.page - 1,
            )
        )
    if not crops:
        raise ModelBakeoffError(f"answer key {answer_key} contains no observations to score")
    return tuple(crops)


def _load_bakeoff_metadata(path: Path) -> _BakeoffMetadata | None:
    if not path.exists():
        return None
    try:
        return _BakeoffMetadata.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise ModelBakeoffError(f"invalid bake-off metadata {path}: {error}") from error


def _refuse_machine_self_verified(case: GoldCase, metadata: _BakeoffMetadata | None) -> None:
    text = case.provenance.annotator
    if metadata is not None:
        text += " " + json.dumps(metadata.model_dump(mode="json"), sort_keys=True)
    lowered = text.lower()
    marker = next((marker for marker in _SELF_VERIFIED_MARKERS if marker in lowered), None)
    if marker is not None:
        raise ModelBakeoffError(
            "model bake-off requires a human-read answer key; refused key marked " f"{marker!r}"
        )


def _tags_for(
    crop_id: str,
    index: int,
    expected: Measurement,
    metadata: _BakeoffMetadata | None,
) -> frozenset[str]:
    tags: set[str] = set()
    if "/" in str(expected.raw_text or "") or expected.exact.denominator != 1:
        tags.add("fraction")
    if metadata is not None:
        tags.update(metadata.tags.get(crop_id, ()))
        tags.update(metadata.tags.get(str(index), ()))
    return frozenset(tags)


def load_model_specs(path: str | Path) -> tuple[ModelSpec, ...]:
    """Load the run-specific model ids and token prices."""

    manifest_path = Path(path)
    try:
        manifest = _ModelManifest.model_validate(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise ModelBakeoffError(f"invalid model manifest {manifest_path}: {error}") from error
    return tuple(
        ModelSpec(
            name=item.name,
            model_id=item.model_id,
            input_usd_per_million=item.input_usd_per_million,
            output_usd_per_million=item.output_usd_per_million,
        )
        for item in manifest.models
    )


def adapters_from_specs(
    specs: Sequence[ModelSpec],
    *,
    region_name: str | None = None,
    connect_timeout_seconds: int = 10,
    read_timeout_seconds: int = 120,
) -> tuple[BedrockBakeoffAdapter, ...]:
    """Create Bedrock-backed adapters for a real bake-off run."""

    return tuple(
        BedrockBakeoffAdapter(
            spec,
            region_name=region_name,
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
        )
        for spec in specs
    )


def _crop_from_input(item: _CropInput, *, base: Path) -> Crop:
    image_path = (base / item.image).resolve()
    try:
        image = image_path.read_bytes()
    except OSError as error:
        raise ModelBakeoffError(f"could not read crop image {image_path}: {error}") from error
    return Crop(
        crop_id=item.id,
        expected=_measurement(item.expected.exact, item.expected.unit, item.expected.raw_text),
        image=image,
        tags=frozenset(item.tags),
        image_format=item.image_format,
        page=item.page,
        context=AssembledContext(
            nearby_text=tuple(NearbyText(text, Decimal(0)) for text in item.nearby_text),
            nearby_geometry=(),
        ),
        bound_pt=item.bound_pt,
    )


def _measurement(value: str, unit: Unit, raw_text: str | None) -> Measurement:
    try:
        exact = to_exact_fraction(_strip_unit_marker(value, unit))
    except (ValueError, ImperialParseError) as error:
        raise ReadingParseError(
            f"unreadable {unit.value} measurement {value!r}: {error}"
        ) from error
    return Measurement(exact=exact, unit=unit, raw_text=raw_text)


def _png_from_pdfium(bitmap: Any) -> bytes:
    width = int(bitmap.width)
    height = int(bitmap.height)
    channels = int(bitmap.n_channels)
    stride = int(bitmap.stride)
    source = bytes(bitmap.buffer)
    rows: list[bytes] = []
    for row in range(height):
        line = source[row * stride : row * stride + width * channels]
        if channels == 3:
            rows.append(line)
        else:
            rows.append(
                b"".join(line[index : index + 3] for index in range(0, len(line), channels))
            )
    return _encode_png(width, height, b"".join(rows))


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", binascii.crc32(body))


def _encode_png(width: int, height: int, rgb: bytes) -> bytes:
    stride = width * 3
    scanlines = b"".join(b"\x00" + rgb[row * stride : (row + 1) * stride] for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(scanlines))
        + _chunk(b"IEND", b"")
    )


def _strip_unit_marker(value: str, unit: Unit) -> str:
    token = value.strip()
    lowered = token.lower()
    if unit is Unit.MM and lowered.endswith("mm"):
        return token[:-2].strip()
    if unit is Unit.INCH:
        for suffix in (" inches", " inch", " in"):
            if lowered.endswith(suffix):
                return token[: -len(suffix)].strip()
    return token


def _score_pair(
    left: str,
    right: str,
    crops: Sequence[Crop],
    reads: Mapping[tuple[str, str], ScoredRead],
) -> PairwiseScore:
    disagreements = 0
    both_exact = 0
    for crop in crops:
        left_read = reads[(left, crop.crop_id)]
        right_read = reads[(right, crop.crop_id)]
        if left_read.exact and right_read.exact:
            both_exact += 1
        if (
            left_read.parsed is None
            or right_read.parsed is None
            or (left_read.parsed.exact, left_read.parsed.unit)
            != (right_read.parsed.exact, right_read.parsed.unit)
        ):
            disagreements += 1
    return PairwiseScore(
        left=left,
        right=right,
        total=len(crops),
        disagreements=disagreements,
        both_exact=both_exact,
    )


def _pair_names(
    model_names: tuple[str, ...], required_pairs: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, str], ...]:
    present = set(model_names)
    pairs: list[tuple[str, str]] = []
    for left, right in required_pairs:
        if left in present and right in present:
            pairs.append((left, right))
    for left, right in combinations(model_names, 2):
        if frozenset((left, right)) not in {frozenset(pair) for pair in pairs}:
            pairs.append((left, right))
    return tuple(pairs)


def _find_pair(pairs: Sequence[PairwiseScore], left: str, right: str) -> PairwiseScore | None:
    wanted = frozenset((left, right))
    for pair in pairs:
        if pair.names == wanted:
            return pair
    return None


def _rate(count: int, total: int) -> Fraction | None:
    if total == 0:
        return None
    return Fraction(count, total)


def _format_rate(rate: Fraction | None) -> str:
    if rate is None:
        return "not measured"
    percent = Decimal(rate.numerator * 100) / Decimal(rate.denominator)
    return f"{rate.numerator}/{rate.denominator} ({percent.quantize(Decimal('0.1'))}%)"


def _format_decimal(value: Decimal | None) -> str:
    if value is None:
        return "not measured"
    return str(value.quantize(Decimal("0.1")))


def _format_money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.000001'))}"
