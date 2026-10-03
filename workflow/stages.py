"""The pipeline stages: what each one actually does, and where the work stops.

All six do work. `ingest` checks a document is still the one that was uploaded; `extract_pages`
reads its pages and records what was on them; `validate_evidence` cuts a picture of every reading;
`match` proposes which architectural item is which shop item; `run_checks` runs the rules;
`generate_outputs` turns the findings into a workbook somebody can be handed. `NoStages` still
answers `{"implemented": False}` to all six and is deliberately loud about it — a default returning
`{}` would let a package walk the whole pipeline and arrive at review looking processed.

**The stages were wired in the opposite order to the one the data flows in**, and that was
deliberate. `run_checks` came first, when extraction did not exist, because the engine had been
finished and tested for a long time and had never had a caller — every finding anyone had seen was
inserted by hand. Wiring the last stage first proved the spine and made extraction a matter of
supplying operands to something that already worked. The reading half (#517) followed the same
principle: `evidence/crop.py` and `retrieval/matching.py` were finished, tested and unreachable from
production, so what was missing was the connection rather than the algorithm.

**Raw candidates stay raw.** The default pipeline stops at untyped readings. An opt-in deployment
may additionally use the semantic-typing gate: only an exact vector vocabulary tag and numeric
reading already attached to one deterministic line may mint a corroborated observation. Position and
agent suggestions remain reviewer work, so the verdict still sees no operand when the proof is
missing. `docs/decisions/SEMANTIC_TYPING_GATE.md` records the whole boundary.

**A check therefore still abstains unless a reviewer supplies the reading**, which `CLIENT_FACTS` Q7
blesses for exactly this. That is the honest result, not a broken one: no observations means no
operands means nothing to decide from.

**Why here and not in `app/`.** The `Stages` protocol is handed a SQLAlchemy `Session`, and
`sqlalchemy` is in the banned set for `verdict/` and `rules/` — those packages could not implement
this protocol if they wanted to. `workflow/` is the sanctioned bridge: it already imports `app` models
and the domain layers side by side (`workflow/retry.py`), which is exactly what a stage has to do.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from io import BytesIO
from typing import Final, Protocol, TypeVar, cast
from uuid import UUID, uuid4

from opentelemetry.trace import Status, StatusCode
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from app.api.documents import storage_key
from app.db.base import utc_now
from app.evidence.automatic_typing import AutomaticTypingSettings, qualify_exact_tags_for_revision
from app.evidence.record import (
    NOT_A_SINGLE_VALUE_FLAG,
    UNKNOWN_UNIT_FLAG,
    UNPARSED_FLAG,
    dual_unit_lane,
    open_extraction_run,
    persist_manifest,
    record_associations,
    record_candidates,
    record_digest_mismatch,
    record_markup_candidates,
    record_ocr_candidates,
    record_unreadable_document,
    record_unreadable_page,
)
from app.models.document import (
    Document,
    DocumentKind,
    DocumentVersion,
    PackageRevisionDocument,
    Page,
)
from app.models.drawing import DrawingItem, DrawingView, ItemIdentifier, ViewRole, ViewRoleProposal
from app.models.evidence import (
    EvidenceArtifact,
    EvidenceArtifactKind,
    ObservationCandidate,
    line_key,
)
from app.models.matching import MatchCandidate as MatchCandidateRow
from app.models.package import Package, PackageRevision
from app.models.parameters import declared_defaults, load_parameter_sets
from app.models.rules import RuleDefinition
from app.models.rules import RuleSnapshot as RuleSnapshotRow
from app.models.runs import ExtractionRun, ModelInvocation, TaskRun
from app.models.verdicts import CheckRun, OutputArtifact, OutputArtifactKind
from app.models.verdicts import Finding as FindingRow
from app.runs.invocations import (
    BedrockConverseInvocationRecorder,
)
from app.runs.invocations import (
    record as record_model_invocation,
)
from app.runs.rates import call_cost_micros, rates_from_environment
from app.telemetry.tracing import traced
from app.verdicts.record import record_finding, supersede_runs
from app.verdicts.rulebook import snapshot_store
from evidence.candidate import STACKED_FRACTION_FLAG
from evidence.candidate import ObservationCandidate as DomainCandidate
from evidence.canonical import EvidenceStatus
from evidence.coordinates import ImagePoint, PageTransform, StoredPoint
from evidence.corroborate import corroborate
from evidence.crop import CropSpec, CropStatus, RenderedPage, crop_pixel_box, generate_crop
from evidence.polygon import Polygon
from extraction.agent.geometry import Box, LabelReach, label_geometry
from extraction.agent.graph import (
    AbstentionTerminal,
    BoundedAgentGraph,
    BoundedRegionContext,
    CandidateTerminal,
    GraphLimits,
    Planner,
    RetryableToolFailure,
)
from extraction.agent.observations import RegionFacts, trigger_reasons
from extraction.agent.outcomes import abstain as agent_abstain
from extraction.agent.policy import policy_planner
from extraction.agent.tools import (
    AgentToolbox,
    OcrVerificationArguments,
    ToolCallRecord,
    VlmReadingArguments,
    VlmRole,
)
from extraction.agent.trigger import AmbiguityReason, RegionContext, evaluate_trigger
from extraction.annotations import (
    LINE_DIMENSION_INTENT,
    OutlinedTextRegion,
    PageLayers,
    StackedFraction,
    VectorPath,
    page_box_polygon,
    read_annotation_layers,
    read_markup_layer,
)
from extraction.fraction_parts import (
    FRACTION_PARTS_EXTRACTOR,
    FRACTION_PARTS_VERSION,
    DrawnPiece,
    FractionPartsRefusal,
    PieceDrawing,
    read_fraction_parts,
)
from extraction.geometry.containment import DimensionExtent
from extraction.geometry.dimension_lines import DetectedDimensions, detect
from extraction.geometry.text_association import DimensionText, associate
from extraction.glyph_bands import FractionLayout
from extraction.layout import (
    BedrockClosedQuestionConfig,
    BedrockClosedQuestionReader,
    LayoutClassification,
    LayoutReader,
    LayoutStatus,
    classify_layout,
    question_from_discriminator,
)
from extraction.localized_ocr import read_localized_vendor_regions
from extraction.manifest import build_manifest
from extraction.model.part_proposals import (
    PROPOSER_SOURCE,
    PROPOSER_VERSION,
    PrintedText,
    ViewOutline,
    propose_parts,
)
from extraction.models.context import AssembledContext
from extraction.models.invocations import InvocationRecord
from extraction.models.nova import (
    NovaAdapter,
    NovaAdapterError,
    NovaConfig,
    NovaDigitsRequest,
    NovaInvocation,
    NovaInvocationOutcome,
    NovaPayloadRejectedError,
    NovaProtocolError,
    NovaRefusalError,
    NovaRequest,
    NovaRetryExhaustedError,
    NovaTimeoutError,
    vision_config_for_extractor,
    vision_configs_from_environment,
)
from extraction.models.sanitisation import DIGITS_PROMPT_ID
from extraction.models.validation import STACKED_FRACTION_REASON, ValidationRejection
from extraction.ocr import OcrEngine, OcrItem, RapidOcrEngine, could_be_a_reading, read_page
from extraction.panels import propose_panel_roles
from extraction.rasterise import VISION_CROP_DPI, PageTooLarge, render_page
from extraction.reader import (
    PageContents,
    SetAsideLabel,
    SetAsideReason,
    TextItem,
    UnreadablePdf,
    read_page_contents,
    read_pages,
)
from extraction.stamp_text import read_stamp_text
from extraction.text_sources import survey_page
from extraction.vector_first import plan_reads
from reports.findings_pdf import FINDINGS_PDF_MEDIA_TYPE, FindingsPdfInput, write_findings_pdf
from reports.spreadsheet import StoredFinding, decode_reference, write_stored_workbook
from retrieval.identifiers import NormalizedIdentifier, normalize_identifier
from retrieval.matching import MatchableItem, MatchDocumentRole, exact_match
from rules.applicability import Abstention, CheckContext, resolve
from rules.parameters import ParameterSet, resolve_all
from rules.project import ProjectScope
from rules.required_inputs import DiscriminatorNeed, required_inputs
from rules.semantic_types import ProductType
from rules.snapshot import RuleSnapshot
from storage.hashing import ArtifactCorrupt, content_key, sha256_stream
from storage.store import ArtifactStore
from units.imperial import format_inches
from units.measurement import Measurement, Unit
from units.normalise import UnitNormalisationError, normalise_to_inches
from units.notation import canonical_notation, is_compound
from verdict.engine import execute
from verdict.finding import Finding
from verdict.operands import VerdictOperand
from verdict.operations import register_all
from verdict.outcomes import Outcome
from vocabulary.part_kinds import PartKind
from workflow.association import (
    AssociationSettings,
    LocalizedOcrSettings,
    ReadItem,
    dimension_texts,
)
from workflow.config import READER_RASTER_DPI
from workflow.evidence_operands import evidence_operands, position_sensitive_inputs
from workflow.findings_composer import (
    ComposerFinding,
    ComposerOperand,
    FindingsLanguageModel,
    compose_findings,
    reviewer_reason,
)
from workflow.glyph_route import GLYPH_EXTRACTOR, GlyphRoute, read_page_labels
from workflow.idempotency import stage_idempotency_key
from workflow.layout_proposals import record_layout_proposal
from workflow.measurements import run_parameters_for
from workflow.parts import live_part_item_ids, record_part_proposal
from workflow.reading_agent import ReadingAgentSettings, RegionCrops, crop_box_px
from workflow.redline_outputs import render_evidence_grounded_redline
from workflow.review import ENGINE_VERSION, PageResult
from workflow.view_roles import record_panel_view, revision_views

#: What produced these readings, recorded on the extraction run so a candidate can say what read it.
EXTRACTOR = "pdfplumber"
EXTRACTOR_VERSION = "extraction.reader/1"

#: The markup route's own identity, so its rows are distinguishable from the pixel routes' (#543).
#:
#: A separate run rather than a column, following the OCR route: `open_extraction_run` keys a run on
#: extractor, version and configuration, and `_read_page_by_ocr` opens its own precisely so a
#: reviewer can tell one route's reading from another's without inspecting the text. Two routes
#: reading one page is now the ordinary case rather than a collision, because the `(run, page)`
#: idempotency guard in each writer only ever sees its own route's rows.
MARKUP_EXTRACTOR = "extraction.annotations"
MARKUP_EXTRACTOR_VERSION = "extraction.annotations/1"

#: The vendor's own exact text, where its CAD program kept it: AutoCAD's `AutoCAD SHX Text` notes
#: (`DrawingLayer.VENDOR_TEXT`). Its own route, and never the markup route's: the markup route holds
#: the reviewer's corrections, and a vendor string recorded under it would carry a reviewer's
#: authority. As a route that is not a model it counts as independent of every model reader
#: (`evidence.corroborate.independence_key`), which is right: nothing read it but the file.
CAD_TEXT_EXTRACTOR = "extraction.cad_text"
CAD_TEXT_EXTRACTOR_VERSION = "extraction.cad_text/1"

#: The font text inside a pasted drawing (`extraction/stamp_text.py`): exact, like the vector route,
#: and its own route for the same reason the CAD text has one — a reader of a page's own content and a
#: reader of its pasted drawings are two different things a reviewer must be able to tell apart.
STAMP_TEXT_EXTRACTOR = "extraction.stamp_text"
STAMP_TEXT_EXTRACTOR_VERSION = "extraction.stamp_text/1"

#: The routes whose text is the file's own characters rather than a reading of its pixels (#792): the
#: vector text, the reviewer's `/FreeText` markup, the vendor's CAD notes and a pasted drawing's font
#: text. A model shown a picture of that text can only read what the file already says, so a string of
#: theirs that does not parse as a number — a word, a tag, a title — is no reason to ask one.
EXACT_TEXT_EXTRACTORS: Final = frozenset(
    {EXTRACTOR, MARKUP_EXTRACTOR, CAD_TEXT_EXTRACTOR, STAMP_TEXT_EXTRACTOR}
)

#: The association step's own identity, so its thresholds are part of a run's identity (#545).
#:
#: A third run per page, and for the same reason as the second: `open_extraction_run` keys a run on
#: extractor, version and configuration. The five lengths go into the configuration, which is what
#: makes a re-association under different numbers a different run rather than the same one quietly
#: meaning something else.
ASSOCIATION_EXTRACTOR = "extraction.geometry.text_association"
ASSOCIATION_EXTRACTOR_VERSION = "extraction.geometry.text_association/1"

#: One character is enough to call a page text-bearing. `build_manifest` requires the threshold from
#: its caller and gives it no default, because "enough text to be worth reading" is a judgement about
#: real drawings. One is the only value that is not a guess: it separates a page with text from a page
#: with none, which is the distinction the reader already reports.
MINIMUM_VECTOR_CHARACTERS = 1


class _RecordingFindingsLanguageModel(FindingsLanguageModel, Protocol):
    """Optional extension supplied by the configured Bedrock narration adapter."""

    def with_invocation_recorder(
        self, recorder: BedrockConverseInvocationRecorder
    ) -> FindingsLanguageModel:
        """Return the same model adapter with per-call persistence attached."""


class _VisionReader(Protocol):
    """One bounded model reader route."""

    @property
    def config(self) -> NovaConfig:
        """The model identity and extractor name this route records under."""

    def extract(self, request: NovaRequest, recorder: _BufferedVisionRecorder) -> DomainCandidate:
        """Return one raw model candidate or raise an adapter error."""

    def read_digits(self, request: NovaDigitsRequest, recorder: _BufferedVisionRecorder) -> str:
        """Return the validated digits of one drawn piece, or raise an adapter error (#865)."""


@dataclass(frozen=True, slots=True)
class _LayoutReaderRoute:
    """One closed-question reader plus the identity persisted with its proposal."""

    reader: LayoutReader
    model_id: str
    prompt_id: str


@dataclass(frozen=True, slots=True)
class BedrockVisionReader:
    """A Bedrock Converse reader using the existing strict Nova adapter contract."""

    config: NovaConfig

    def extract(self, request: NovaRequest, recorder: _BufferedVisionRecorder) -> DomainCandidate:
        return NovaAdapter.from_environment(self.config, recorder).extract(request)

    def read_digits(self, request: NovaDigitsRequest, recorder: _BufferedVisionRecorder) -> str:
        return NovaAdapter.from_environment(self.config, recorder).read_digits(request)


def configured_vision_readers_from_environment() -> tuple[BedrockVisionReader, ...]:
    """Return the two Phase C Bedrock readers when a deployment explicitly enables them."""

    if os.environ.get(VISION_READERS_ENV, "").lower() not in {"1", "true", "yes"}:
        return ()
    return tuple(BedrockVisionReader(config) for config in vision_configs_from_environment())


def configured_layout_readers_from_environment() -> tuple[_LayoutReaderRoute, ...]:
    """Return the Bedrock layout reader only when a deployment explicitly configures it."""

    if os.environ.get(LAYOUT_READERS_ENV, "").lower() not in {"1", "true", "yes"}:
        return ()
    model_id = os.environ.get(LAYOUT_MODEL_ENV) or os.environ.get("GV_BEDROCK_MODEL", "")
    if not model_id.strip():
        return ()
    try:
        import boto3  # type: ignore[import-untyped]
    except Exception:  # noqa: BLE001
        return ()
    config = BedrockClosedQuestionConfig(
        model_id=model_id,
        prompt_id=LAYOUT_PROMPT_ID,
        template_id=LAYOUT_TEMPLATE_ID,
    )
    client = boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("GV_BEDROCK_REGION", "us-east-1"),
    )
    return (
        _LayoutReaderRoute(BedrockClosedQuestionReader(config, client), model_id, config.prompt_id),
    )


def _stored_invocation_outcome(outcome: NovaInvocationOutcome) -> str:
    if outcome is NovaInvocationOutcome.OK:
        return "ok"
    if outcome is NovaInvocationOutcome.REFUSED:
        return "refused"
    if outcome is NovaInvocationOutcome.REJECTED:
        return "rejected"
    if outcome is NovaInvocationOutcome.TIMEOUT:
        return "timeout"
    return "failed"


def _candidate_evidence_status(row: ObservationCandidate) -> EvidenceStatus:
    if row.corroboration_status is None:
        return EvidenceStatus.RAW_CANDIDATE
    return EvidenceStatus(row.corroboration_status)


def _vision_candidate_value(raw_text: str) -> tuple[Measurement | None, str | None]:
    """The exact value a vision reading is stored with, or the flag saying why it has none.

    **It must agree with the shape check** (`extraction/models/validation.py`). Before #733 this was a
    bare `normalise_to_inches(raw_text)` while the check canonicalised inch marks first, so the two
    disagreed: a model's `8'-6''` passed validation and was then stored with no value, unable to take
    part in any agreement — and `25-1/2"`, `381 [15]` and `2" (VIF)` could not be valued at all. Both
    now read `units.notation`, and `tests/workflow/test_vision_candidate_value.py` fails if they part.

    The row keeps the characters the model returned; only the value comes from the canonical form.
    """
    if is_compound(raw_text):
        return None, NOT_A_SINGLE_VALUE_FLAG
    try:
        return normalise_to_inches(canonical_notation(raw_text)[0]), None
    except UnitNormalisationError:
        return None, UNPARSED_FLAG


def _ambiguity_reasons(
    row: ObservationCandidate, *, exact_text: bool
) -> frozenset[AmbiguityReason]:
    flags = set(row.ambiguity_flags)
    reasons: set[AmbiguityReason] = set()
    if UNKNOWN_UNIT_FLAG in flags:
        reasons.add(AmbiguityReason.UNKNOWN_UNIT)
    # An exact route's string that does not parse is still exactly what the file says (#792), so no
    # look at its picture can read it differently. Measured on AI_Set_2, this flag alone sent 999
    # regions of a pasted drawing's own text to two paid readers. Its unit and its geometry still can.
    if UNPARSED_FLAG in flags and not exact_text:
        reasons.add(AmbiguityReason.UNREADABLE_TEXT)
    # `NOT_A_SINGLE_VALUE_FLAG` is deliberately **not** mapped (#733). These reasons trigger a bounded,
    # paid agent retry; a compound like `39 1/4"+6"` was read correctly and is genuinely two values, so
    # no retry can turn it into one. Before #733 a compound was flagged unparsed and retried for nothing.
    return frozenset(reasons)


def _shows_a_numeral(text: str | None) -> bool:
    """Whether any character of `text` is a numeral — a vulgar fraction such as `½` included."""

    return text is not None and any(character.isnumeric() for character in text)


#: Where a deployment states what the AI readers may spend on one drawing set, in US dollars.
AI_BUDGET_ENV: Final = "GV_AI_BUDGET_PER_SET_USD"

#: The admin's cap (#757, decided 2026-10-01): $3 for one drawing set. Measured use is at most about
#: $1.60 per 1,000 labels — under a dollar for a set the size of the client's 17 pages — so the cap
#: never limits ordinary work. It stops a runaway: once a set has spent it, every label left goes to
#: a reviewer without a model reading, and the page result says so.
DEFAULT_AI_BUDGET_USD: Final = Decimal(3)


def ai_budget_from_environment(environ: Mapping[str, str] = os.environ) -> Decimal:
    """The deployment's stated cap, or the admin's $3 where it states none."""
    raw = environ.get(AI_BUDGET_ENV, "").strip()
    if not raw:
        return DEFAULT_AI_BUDGET_USD
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"{AI_BUDGET_ENV} must be an amount in dollars, not {raw!r}") from error
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{AI_BUDGET_ENV} must be more than zero dollars, not {raw!r}")
    return value


#: The switch for reading each stacked fraction piece by piece (#848). Off unless a deployment turns
#: it on, and `scripts/demo.sh` does not.
FRACTION_PARTS_ENV: Final = "GV_FRACTION_PARTS"

#: How each piece is drawn for the reader (`extraction.fraction_parts.PieceDrawing`). Required once
#: the route is on, and none has a default: each changes what the reader is shown.
FRACTION_PARTS_SETTINGS: Final = {
    "GV_FRACTION_PARTS_HEIGHT_PX": "height_px",
    "GV_FRACTION_PARTS_STROKE_PX": "stroke_px",
    "GV_FRACTION_PARTS_BEZIER_STEPS": "bezier_steps",
    "GV_FRACTION_PARTS_MARGIN_PX": "margin_px",
}


def fraction_parts_from_environment(environ: Mapping[str, str] = os.environ) -> PieceDrawing | None:
    """How the deployment draws the pieces of a stacked fraction, or `None` where the route is off.

    A switch that is on with a setting missing or not a whole number is refused, not filled in: a
    guessed size would decide what the reader is shown on every drawing anybody runs.
    """
    if environ.get(FRACTION_PARTS_ENV, "").strip().lower() not in {"1", "true", "yes"}:
        return None
    missing = [name for name in FRACTION_PARTS_SETTINGS if not environ.get(name, "").strip()]
    if missing:
        raise ValueError(
            f"{FRACTION_PARTS_ENV} is on and these are not stated: {', '.join(missing)}. None of "
            "them has a default"
        )
    stated: dict[str, int] = {}
    for name, field_name in FRACTION_PARTS_SETTINGS.items():
        raw = environ[name].strip()
        if not raw.isascii() or not raw.isdigit():
            raise ValueError(f"{name} must be a whole number, not {raw!r}")
        stated[field_name] = int(raw)
    return PieceDrawing(**stated)


@dataclass(slots=True)
class _SpendMeter:
    """What the AI readers have spent on one drawing set, against its cap (#757).

    Counted from each call's recorded cost. A call whose model has no stated price has no cost to
    count (#700 keeps that unknown rather than zero), so it is counted by number instead and reported:
    the cap holds only where the deployment's price file names its models.
    """

    cap_micros: int
    spent_micros: int = 0
    unpriced_calls: int = 0

    def add(self, cost_micros: int | None) -> None:
        if cost_micros is None:
            self.unpriced_calls += 1
        else:
            self.spent_micros += cost_micros

    @property
    def reached(self) -> bool:
        return self.spent_micros >= self.cap_micros

    @property
    def reason(self) -> str:
        cap = Decimal(self.cap_micros) / 1_000_000
        return (
            f"this drawing set has spent its AI budget of ${cap:.2f}, so the label goes to a "
            "reviewer without a model reading"
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "cap_usd": str(Decimal(self.cap_micros) / 1_000_000),
            "spent_usd": str(Decimal(self.spent_micros) / 1_000_000),
            "calls_without_a_price": self.unpriced_calls,
            "reached": self.reached,
        }


@dataclass(slots=True)
class _BufferedVisionRecorder:
    """Collect adapter attempt records until the workflow can persist them with run context."""

    session: Session
    extraction_run_id: UUID
    request_candidate_id: UUID
    crop_artifact_id: UUID | None = None
    meter: _SpendMeter | None = None
    """The drawing set's spend, which every call persisted here adds to."""
    _invocations: list[NovaInvocation] = field(init=False, default_factory=list)
    _rejections: list[ValidationRejection] = field(init=False, default_factory=list)

    def record(self, invocation: NovaInvocation) -> None:
        self._invocations.append(invocation)

    def record_rejection(self, rejection: ValidationRejection) -> None:
        self._rejections.append(rejection)

    @property
    def rejections(self) -> tuple[ValidationRejection, ...]:
        return tuple(self._rejections)

    def persist(self, *, candidate_id: UUID | None, flush: bool = True) -> int:
        written = 0
        for invocation in self._invocations:
            stored = record_model_invocation(
                self.session,
                InvocationRecord(
                    extraction_run_id=self.extraction_run_id,
                    model_id=invocation.model_id,
                    prompt_id=invocation.prompt_id,
                    template_id=invocation.template_id,
                    crop_artifact_id=self.crop_artifact_id,
                    input_tokens=invocation.input_tokens,
                    output_tokens=(
                        0
                        if invocation.outcome
                        in {NovaInvocationOutcome.REFUSED, NovaInvocationOutcome.TIMEOUT}
                        else invocation.output_tokens
                    ),
                    # From the deployment's stated price file, or unknown — never a literal zero (#700).
                    cost_micros=call_cost_micros(
                        rates_from_environment(),
                        invocation.model_id,
                        invocation.input_tokens,
                        (
                            0
                            if invocation.outcome
                            in {NovaInvocationOutcome.REFUSED, NovaInvocationOutcome.TIMEOUT}
                            else invocation.output_tokens
                        ),
                    ),
                    latency_ms=invocation.latency_ms,
                    outcome=_stored_invocation_outcome(invocation.outcome),
                    candidate_id=(
                        candidate_id if invocation.outcome is NovaInvocationOutcome.OK else None
                    ),
                    assembled_context=invocation.context,
                    bound_pt=invocation.bound_pt,
                    rejection_reason=invocation.rejection_reason,
                ),
                flush=flush,
            )
            if self.meter is not None:
                self.meter.add(stored.cost_micros)
            written += 1
        return written


def _stored_measurement(row: ObservationCandidate) -> Measurement | None:
    if row.value_numerator is None or row.value_denominator is None or row.unit is None:
        return None
    return Measurement(
        Fraction(row.value_numerator, row.value_denominator),
        Unit(row.unit),
        row.raw_text,
    )


def _domain_candidate_from_row(
    row: ObservationCandidate, run: ExtractionRun, page_index: int
) -> DomainCandidate:
    return DomainCandidate(
        candidate_id=str(row.id),
        extractor=run.extractor,
        extractor_version=run.extractor_version,
        raw_text=row.raw_text,
        parsed_value=_stored_measurement(row),
        unit_guess=None if row.unit_guess is None else Unit(row.unit_guess),
        semantic_guess=None,
        page=page_index,
        polygon=tuple(ImagePoint(x=int(x), y=int(y)) for x, y in row.polygon),
        confidence=row.confidence,
        ambiguity_flags=tuple(row.ambiguity_flags),
    )


#: The pixel ceiling for one rendered page, used only by the OCR route.
#:
#: 40 megapixels is about 120 MB of raw RGB. The 300-DPI reader fits the measured Board Room sheet,
#: while an ANSI E sheet at 300 DPI exceeds this ceiling and is refused instead of risking a roughly
#: 400-MB raw RGB allocation. Above it `render_page` raises `PageTooLarge` rather than shrinking,
#: because a page quietly rendered smaller is a page read at a resolution nobody chose.
MAXIMUM_RENDER_PIXELS = 40_000_000

#: How much page to keep around an evidence crop, in PDF points (72 to the inch).
#:
#: Nine points is an eighth of an inch. A crop of exactly the text box is unreadable as evidence — a
#: reviewer looking at `38 3/4"` needs to see what it is dimensioning — and a crop of the whole page
#: is not evidence of anything in particular. `CropSpec` gives this no default and requires it from
#: the caller, which is the right call: it is a judgement about drawings, and this value is the
#: smallest one that shows a dimension line either side of its text. **Expect to tune it against the
#: real GV drawings when #274 lands**; it is a starting point chosen deliberately, not a measured one.
CROP_CONTEXT_MARGIN_PT = Decimal(9)

#: The bounded crop and context sent to vision readers. It deliberately reuses the evidence crop
#: margin so the model sees a region, not a full page, and no model chooses its own context.
VISION_CROP_CONTEXT_MARGIN_PT = CROP_CONTEXT_MARGIN_PT
VISION_CONTEXT_BOUND_PT = CROP_CONTEXT_MARGIN_PT

#: Explicit opt-in for paid/network vision reads in the local worker path. Tests and local extraction
#: stay deterministic unless a caller injects readers or a deployment opts in.
VISION_READERS_ENV = "GV_BEDROCK_VISION_ENABLED"

#: Explicit opt-in for paid/network closed-question layout reads. Without this and a model id, the
#: stage still records an abstention for each discriminator instead of failing extraction.
LAYOUT_READERS_ENV = "GV_BEDROCK_LAYOUT_ENABLED"
LAYOUT_MODEL_ENV = "GV_BEDROCK_LAYOUT_MODEL"
LAYOUT_PROMPT_ID = "layout-discriminator-v1"
LAYOUT_TEMPLATE_ID = "closed-question-page-v1"
UNCONFIGURED_LAYOUT_MODEL_ID = "layout-reader-unconfigured"

#: How many individual refusals a stage payload carries, before it reports only the count.
#:
#: The payload is stored as JSON on the task run. A document whose pages will not render produces one
#: refusal per candidate — thousands of near-identical sentences — and a reviewer reads the first few
#: or none at all. The exact number is always reported alongside.
REPORTED_REFUSALS = 20

#: Part of an OCR run's identity since #703: text that could not be a reading is no longer recorded,
#: so a run from before holds rows a run from after does not, and the two must not be one run.
OCR_FRAGMENTS_CONFIG = "fragments=unrecorded"


def _split_ocr_readings(
    items: Sequence[OcrItem],
) -> tuple[tuple[OcrItem, ...], tuple[OcrItem, ...]]:
    """OCR items whose text could be a reading, and the ones whose text could not (#703).

    Order is kept within each, because `_ordered_ocr_rows` and `dimension_texts` pair rows with
    items by position. The rule is `could_be_a_reading`'s, and it reads only the text: no
    confidence, no size, no position.
    """
    readings = tuple(item for item in items if could_be_a_reading(item.text))
    fragments = tuple(item for item in items if not could_be_a_reading(item.text))
    return readings, fragments


def _fragment_texts(fragments: Sequence[OcrItem]) -> list[str]:
    """What the unrecorded fragments said, most frequent first — `7 × 'L'` — for the page result.

    Bounded like every other list a payload carries; the exact total is `ocr_fragments`.
    """
    counts = Counter(item.text for item in fragments)
    return [
        f"{count} × {text!r}"
        for text, count in sorted(counts.items(), key=lambda entry: (-entry[1], entry[0]))
    ][:REPORTED_REFUSALS]


#: The media type of an `.xlsx` workbook, spelled out once.
#:
#: The long OOXML name rather than a friendly alias, because it is what a browser and a mail client
#: dispatch on: `application/octet-stream` makes the client's own spreadsheet tool decline to open
#: the file it is meant to open.
WORKBOOK_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: The identifier kinds that can say two drawn items are the same item.
#:
#: `catalogue` is deliberately absent. `app/models/drawing.py` states the reason on the column
#: itself: a catalogue number is shared by every unit of that model, while a mark is unique to a
#: drawing. Matching on a catalogue number would propose that every cabinet of one model is the same
#: cabinet — mass ambiguity presented as evidence, which is worse than no match at all.
MATCHABLE_IDENTIFIER_KINDS: tuple[str, ...] = ("vendor_unique", "mark")

#: `Document.kind` to the two roles arch-to-shop matching accepts.
#:
#: Schedules and product specs are absent because they are not drawings of the item: a schedule
#: tabulates, and matching a tabulated row to a drawn cabinet is a different problem with a different
#: answer. They are ingested and read like any other document; they just do not take part in this.
MATCH_ROLES: Mapping[str, MatchDocumentRole] = {
    DocumentKind.ARCHITECTURAL.value: MatchDocumentRole.ARCH,
    DocumentKind.SHOP.value: MatchDocumentRole.SHOP,
}
VIEW_MATCH_ROLES: Mapping[str, MatchDocumentRole] = {
    ViewRole.ARCH.value: MatchDocumentRole.ARCH,
    ViewRole.SHOP.value: MatchDocumentRole.SHOP,
}

__all__ = [
    "DatabaseStages",
    "crop_shows_a_stacked_fraction",
    "page_transform",
    "region_facts",
    "stacked_layouts_shown",
    "stored_polygon",
]

type _AgentPlannerFactory = Callable[[BoundedRegionContext, RegionFacts, GraphLimits], Planner]


def _default_bounded_agent_planner(
    context: BoundedRegionContext, facts: RegionFacts, limits: GraphLimits
) -> Planner:
    """The decision table (#757): each next step chosen from what the checks found, not a script.

    Before #757 this returned a fixed sequence — OCR, OCR, VLM, abstain — played whatever each step
    returned, so the agent never looked at a result. `extraction/agent/policy.py` looks at every one.
    """
    del context
    return policy_planner(facts, limits)


def _agent_readers(
    settings: ReadingAgentSettings, readers: Sequence[_VisionReader], dpi: int
) -> dict[VlmRole, _VisionReader]:
    """The reading agent's primary and escalation readers, found by the extractor each is named by.

    A configured vision reader first; otherwise a defined one (`VISION_READERS`), enabled or not.
    **The escalation reader need not read every region**: mistral-large-3 (#757 D-A2) is defined but
    off for the vision route, and the agent asks it by name only after the primary.

    **Refused, not guessed, when a name matches nothing** — a worker that started with the agent on
    and quietly asked no reader, or the wrong one, would report readings its settings never chose.
    """
    by_name = {reader.config.extractor: reader for reader in readers}
    chosen: dict[VlmRole, _VisionReader] = {}
    for role, name in (
        (VlmRole.PRIMARY, settings.primary_reader),
        (VlmRole.ESCALATION, settings.escalation_reader),
    ):
        if name is None:
            continue
        if name in by_name:
            chosen[role] = by_name[name]
            continue
        config = vision_config_for_extractor(name)
        if config is None:
            raise ValueError(
                f"the reading agent's {role.value} reader is {name!r}, which is neither a "
                f"configured vision reader (configured: {sorted(by_name) or 'none'}) nor a defined "
                "one with a measured coordinate space"
            )
        chosen[role] = BedrockVisionReader(config)
    if settings.sharper_dpi <= dpi:
        raise ValueError(
            f"a sharper look must render above the stage's {dpi} dpi; {settings.sharper_dpi} is not"
        )
    return chosen


def page_transform(page: Page, dpi: int) -> PageTransform | None:
    """The page's recorded transform at `dpi`, or `None` where the manifest recorded none."""
    if page.media_box is None or page.crop_box is None:
        return None
    media = tuple(Decimal(value) for value in page.media_box)
    crop = tuple(Decimal(value) for value in page.crop_box)
    return PageTransform(
        dpi=dpi,
        rotation=page.rotation,
        media_box=(media[0], media[1], media[2], media[3]),
        crop_box=(crop[0], crop[1], crop[2], crop[3]),
    )


def _pdf_box(transform: PageTransform, corners: Sequence[tuple[int, int]]) -> Box:
    """Image-pixel corners as the PDF-point box the vendor's paths are in."""
    points = [transform.to_pdf(ImagePoint(x=int(x), y=int(y))) for x, y in corners]
    return (
        min(point.x for point in points),
        min(point.y for point in points),
        max(point.x for point in points),
        max(point.y for point in points),
    )


@dataclass(slots=True)
class _AgentPageOutcome:
    """What the agent did on one page, for the page's payload."""

    rows: list[ObservationCandidate] = field(default_factory=list)
    """Every reading the agent recorded — each look, not only the one it proposed."""

    proposals: int = 0
    abstentions: int = 0
    regions: int = 0
    """Regions the trigger let in — each run once, however many readers' rows it holds."""

    reused: int = 0
    """Regions an earlier delivery of this stage already ran, whose rows were reused unpaid."""

    without_a_numeral: int = 0
    """Regions set aside before any geometry because no route read a numeral in them (#792)."""

    invocations: int = 0
    """Model calls the agent made and recorded, retries included."""


@dataclass(slots=True)
class _AgentCallLog:
    """Every tool call a region's run attempted, in order (`AgentToolbox` records before it runs).

    Held for the run only. Storing it — so the confirm screen can say what the reader tried — needs
    a table for it, which is its own change (#757).
    """

    calls: list[ToolCallRecord] = field(default_factory=list)

    def record(self, call: ToolCallRecord) -> None:
        self.calls.append(call)


def _no_ocr_verification(arguments: OcrVerificationArguments) -> RetryableToolFailure:
    """No OCR verification route is wired for the reading agent, and its limits permit none."""
    del arguments
    return RetryableToolFailure("no OCR verification route is wired for the reading agent")


@dataclass(slots=True)
class _AgentReads:
    """The reading agent's readers on one region. Every call is recorded under the run it belongs to.

    A reader is asked about the crop the run is now on — the graph refuses a call that names any
    other — and the request says whether that crop shows a stacked fraction, so the validator
    refuses a reading of one here exactly as it does on the vision route (#735).
    """

    session: Session
    page_index: int
    crops: RegionCrops
    readers: Mapping[VlmRole, _VisionReader]
    open_run: Callable[[_VisionReader], UUID]
    meter: _SpendMeter | None = None
    recorders: list[tuple[_BufferedVisionRecorder, list[DomainCandidate]]] = field(
        default_factory=list
    )
    """Each call's recorder, and the reading it returned (empty where it returned none)."""

    def read(self, arguments: VlmReadingArguments) -> DomainCandidate | RetryableToolFailure:
        reader = self.readers.get(arguments.role)
        if reader is None:
            return RetryableToolFailure(f"no {arguments.role.value} reader is configured")
        if self.meter is not None and self.meter.reached:
            return RetryableToolFailure(self.meter.reason)
        request_candidate_id = uuid4()
        recorder = _BufferedVisionRecorder(
            session=self.session,
            extraction_run_id=self.open_run(reader),
            request_candidate_id=request_candidate_id,
            meter=self.meter,
        )
        produced: list[DomainCandidate] = []
        self.recorders.append((recorder, produced))
        request = NovaRequest(
            candidate_id=str(request_candidate_id),
            page=self.page_index,
            crop=self.crops.png(arguments.crop_artifact_id),
            image_format="png",
            context=AssembledContext(nearby_text=(), nearby_geometry=()),
            bound_pt=VISION_CONTEXT_BOUND_PT,
            stacked_label=self.crops.shows_stacked_fraction,
            stacked_layouts=self.crops.stacked_layouts,
        )
        try:
            candidate = reader.extract(request, recorder)
        except NovaAdapterError as error:
            return RetryableToolFailure(f"{reader.config.extractor} gave no reading: {error}")
        produced.append(candidate)
        return candidate


def _second_reader_refusal(error: NovaAdapterError) -> str:
    """Why the second reader gave no digits for a piece, in this module's words (#865).

    One sentence per kind of failure, never one for all of them: a single sentence for every failure
    once hid that a model had answered, too long for its limit (#712). A refused answer names the
    validator's reason code and a failed call its AWS error code; neither is the model's text, and
    the exact error stays in `model_invocations`.
    """
    if isinstance(error, NovaPayloadRejectedError):
        return f"the second reader's answer was refused: {error.rejection.reason}"
    if isinstance(error, NovaProtocolError):
        return "the second reader did not answer with exactly one call to its tool"
    if isinstance(error, NovaRefusalError):
        return "the second reader's provider stopped the request"
    if isinstance(error, NovaTimeoutError):
        return "the second reader did not answer in time"
    if isinstance(error, NovaRetryExhaustedError):
        return "the second reader's call failed on every attempt"
    cause = error.__cause__
    response = getattr(cause, "response", None)
    details = response.get("Error") if isinstance(response, Mapping) else None
    code = details.get("Code") if isinstance(details, Mapping) else None
    named = code if isinstance(code, str) else type(cause).__name__
    return f"the second reader's call failed: {named}"


@dataclass(slots=True)
class _PieceReads:
    """The fraction-parts route's second reader on one page (#865): the vision gate reader, asked for
    the digits of each drawn piece (`extraction.fraction_parts.SecondReader`).

    **Every call is on the budget and on the record.** Before each one the drawing set's meter is
    read, and a spent budget is a refusal with the meter's own reason, so no call is made. After each
    one, every attempt is written to `model_invocations` under the route's run and added to the
    meter, whether or not it answered, before the next piece is asked about.
    """

    session: Session
    run_id: UUID
    page_index: int
    reader: _VisionReader
    meter: _SpendMeter | None
    invocations: int = 0
    """Model calls made and recorded on this page, retries included."""

    @property
    def extractor(self) -> str:
        return self.reader.config.extractor

    @property
    def extractor_version(self) -> str:
        return self.reader.config.model_id

    def read_digits(self, piece: DrawnPiece) -> str | FractionPartsRefusal:
        if self.meter is not None and self.meter.reached:
            return FractionPartsRefusal(self.meter.reason)
        request_id = uuid4()
        recorder = _BufferedVisionRecorder(
            session=self.session,
            extraction_run_id=self.run_id,
            request_candidate_id=request_id,
            meter=self.meter,
        )
        try:
            return self.reader.read_digits(
                NovaDigitsRequest(
                    request_id=str(request_id),
                    page=self.page_index,
                    picture=piece.png,
                    digit_count=piece.digit_count,
                ),
                recorder,
            )
        except NovaAdapterError as error:
            return FractionPartsRefusal(_second_reader_refusal(error))
        finally:
            # No call is linked to the row it helped read: the column holds one call per row, and a
            # row is read from up to three. They belong to the route's run, as the row does.
            self.invocations += recorder.persist(candidate_id=None, flush=False)


@dataclass(frozen=True, slots=True)
class _LocatedOcrReading:
    """OCR geometry whose orientation was established by its token layout."""

    extent: Polygon
    rotation_degrees: int


@dataclass(frozen=True, slots=True)
class _VisionAssociationLink:
    """A model reading and the fixed-reader region it was asked to read."""

    row: ObservationCandidate
    source_candidate_id: UUID


@dataclass(frozen=True, slots=True)
class _VisionRegion:
    """Where a vision reader is pointed: a recorded reading's box, or an OCR box that read as no number.

    **Not every region is a reading (#703).** On the client's drawing RapidOCR returned 903 boxes on
    2026-09-29 and not one parsed to a value — line-work read as `一`, `口`, `L`, `/`. But those
    boxes are also where the vision readers look: 903 of the run's roughly 940 vision regions were
    OCR boxes, and the vision readers' values were read from crops cut around them. So an OCR box
    whose text cannot be a reading keeps its place on the vision readers' list, and its text is not
    recorded as a candidate.
    """

    id: UUID
    """What a vision reading is linked back to for association (`_association_source_items`). A
    recorded reading's own id, or a fresh one for a box nothing recorded. It is never persisted."""

    polygon: list[list[int]]
    document_version_id: UUID

    @classmethod
    def of(cls, row: ObservationCandidate) -> _VisionRegion:
        return cls(id=row.id, polygon=row.polygon, document_version_id=row.document_version_id)


#: What an OCR item's geometry is paired with: its recorded row, or the region kept in its place.
_Source = TypeVar("_Source", ObservationCandidate, _VisionRegion)


def _association_source_items(
    *groups: tuple[Sequence[ReadItem], Sequence[ObservationCandidate | _VisionRegion]],
) -> dict[UUID, ReadItem]:
    """The fixed-reader geometry that a later model crop is allowed to reuse.

    This map is keyed by the row that bounded the model crop, not by text content. The model may
    read a corrected value from the pixels, but it does not get to supply the page location or text
    orientation used for association; those come only from readers that already expose deterministic
    geometry.
    """
    sources: dict[UUID, ReadItem] = {}
    for items, rows in groups:
        if len(items) != len(rows):
            # This map is only an extra allowance for source-backed model association. If an
            # idempotent rerun hands back already-recorded rows that no longer line up exactly with
            # this pass's in-memory readings, there is no safe source pairing for model rows from
            # that route. Skipping it preserves the older fixed-reader association path, where
            # `dimension_texts` still raises before it would silently mispair rows.
            continue
        for item, row in zip(items, rows, strict=True):
            sources[row.id] = item
    return sources


def _vision_association_inputs(
    links: Sequence[_VisionAssociationLink],
    sources: Mapping[UUID, ReadItem],
) -> tuple[tuple[ReadItem, ...], tuple[ObservationCandidate, ...]]:
    """Make model-read values eligible for association through their source region.

    Before #698, vision rows were recorded but never handed to `associate`, which made a full
    `demo_pair` run report zero attachments even when the detector had line-work. This does not
    promote model geometry: a model row is paired only when the fixed-reader candidate that produced
    its crop already had a deterministic extent and rotation.
    """
    items: list[ReadItem] = []
    rows: list[ObservationCandidate] = []
    for link in links:
        source = sources.get(link.source_candidate_id)
        if source is None:
            continue
        items.append(source)
        rows.append(link.row)
    return tuple(items), tuple(rows)


class DatabaseStages:
    """The pipeline as far as it is built: checks run, everything else still says it did not.

    Deliberately not a subclass of `NoStages` and deliberately without a `__getattr__`. A catch-all
    once made `join_pages` count a phantom page, because `extract_pages` returned a mapping that a
    fall-through produced — so every unimplemented stage is written out, and adding a seventh stage to
    the protocol will fail loudly here instead of being silently answered.
    """

    def __init__(
        self,
        store: ArtifactStore | None = None,
        *,
        dpi: int = READER_RASTER_DPI,
        ocr_engine: OcrEngine | None = None,
        operands: Mapping[str, Mapping[str, VerdictOperand]] | None = None,
        discriminators: Mapping[str, str] | None = None,
        association: AssociationSettings | None = None,
        localized_ocr: LocalizedOcrSettings | None = None,
        automatic_typing: AutomaticTypingSettings | None = None,
        findings_composer: FindingsLanguageModel | None = None,
        vision_readers: Sequence[_VisionReader] | None = None,
        layout_readers: Sequence[LayoutReader] | None = None,
        layout_model_id: str = "injected-layout-reader",
        layout_prompt_id: str = LAYOUT_PROMPT_ID,
        bounded_agent: BoundedAgentGraph | None = None,
        bounded_agent_planner: _AgentPlannerFactory | None = None,
        glyph_route: GlyphRoute | None = None,
        reading_agent: ReadingAgentSettings | None = None,
        vision_gate: str | None = None,
        ai_budget_usd: Decimal | None = None,
        fraction_parts: PieceDrawing | None = None,
    ) -> None:
        """`store` is optional because nothing builds one for a worker yet.

        The artifact store lives on `app.state` in the API and is constructed by
        `scripts/dev_server.py`; no settings-driven factory exists. Rather than invent one here,
        `extract_pages` reports that it has no store and does nothing — which is a fact a caller can
        act on, where a crash on a missing dependency would look like a broken document.

        """
        self._store = store
        self._dpi = dpi
        # **`None` means the association step does not run, and that is recorded as not run.**
        # Association is inherently thresholded — unlike reading a `/FreeText`, there is no
        # threshold-free version of it — so the five lengths are a deployment's to state. A default
        # here would be this module choosing which line a dimension belongs to on every drawing
        # anybody ever runs, which is the guess `text_association` refuses to make for itself.
        self._association = association
        self._localized_ocr = localized_ocr
        # Optional and deliberately empty by default.  A deployment has to state the vocabulary
        # final for its layout; exact tags then qualify only through the separate evidence gate.
        # This never writes ``semantic_guess`` and an agent suggestion is not accepted here.
        self._automatic_typing = automatic_typing
        # Post-verdict presentation only. The composer receives frozen stored findings in
        # ``generate_outputs``; it is unreachable from ``run_checks`` and an error falls back to a
        # complete deterministic summary rather than delaying or changing a verdict.
        self._findings_composer = findings_composer
        self._vision_readers = (
            tuple(configured_vision_readers_from_environment())
            if vision_readers is None
            else tuple(vision_readers)
        )
        self._layout_reader_routes = (
            tuple(configured_layout_readers_from_environment())
            if layout_readers is None
            else tuple(
                _LayoutReaderRoute(reader, layout_model_id, layout_prompt_id)
                for reader in layout_readers
            )
        )
        # **The gate reader (#787), off unless a deployment names one.** It reads every region
        # first; every other vision reader reads only where it read a value. Nova 2 Lite's quota is
        # 20 a minute on this account (#716), and a crop the gate found no value in can never be
        # confirmed — a confirmation is two readers' values agreeing (#775) — so asking the others
        # about it buys at most a lone reading for a person to check.
        if vision_gate is not None and vision_gate not in {
            reader.config.extractor for reader in self._vision_readers
        }:
            raise ValueError(
                f"the vision gate reader is {vision_gate!r}, which is not a configured vision "
                "reader"
            )
        self._vision_gate = vision_gate
        # **What the AI readers may spend on one drawing set (#757)**: the admin's $3 unless the
        # deployment states its own. Metered per `extract_pages`, from the set's recorded calls.
        budget = ai_budget_from_environment() if ai_budget_usd is None else ai_budget_usd
        if not budget.is_finite() or budget <= 0:
            raise ValueError("the AI budget for a drawing set must be more than zero dollars")
        self._ai_budget_micros = int(budget * 1_000_000)
        self._meter: _SpendMeter | None = None
        if bounded_agent is None and reading_agent is None and bounded_agent_planner is not None:
            raise ValueError("bounded_agent_planner cannot be supplied without a bounded_agent")
        if bounded_agent is not None and reading_agent is not None:
            raise ValueError(
                "a bounded_agent brings its own tools; the reading agent builds real ones per "
                "region — supply one or the other"
            )
        self._bounded_agent = bounded_agent
        # **The reading agent (#757), off unless a deployment turns it on.** Its readers are named,
        # never picked: both must be among the configured vision readers.
        self._reading_agent = reading_agent
        self._agent_readers: dict[VlmRole, _VisionReader] = {}
        if reading_agent is not None:
            self._agent_readers = _agent_readers(reading_agent, self._vision_readers, dpi)
        # **The shape reader (#756), off unless a deployment points it at a template set.** Its
        # readings are confirmed only by a second witness (#756 D2); see `workflow/glyph_route`.
        self._glyph_route = glyph_route
        # **Stacked fractions read piece by piece (#848), off unless a deployment turns it on.** It
        # reads the layouts the stacked-fraction detector draws, and the detector runs only with the
        # association settings, so without them the route could never read anything: refused here
        # rather than left on and silent.
        if fraction_parts is not None and association is None:
            raise ValueError(
                "the fraction-parts route reads stacked fractions the detector has laid out, and "
                "the detector runs only with association settings, which these stages do not have"
            )
        # **And it needs its second reader (#865)**: a label is pre-filled only where two readers
        # of different vendors agree on every piece, and the second is the vision gate reader. On
        # without one, the route could never pre-fill anything: refused here, for the same reason.
        if fraction_parts is not None and vision_gate is None:
            raise ValueError(
                "the fraction-parts route pre-fills a label only where a second reader agrees on "
                "every piece, and its second reader is the vision gate reader "
                "(GV_VISION_GATE_READER), which these stages do not have"
            )
        self._fraction_parts = fraction_parts
        self._bounded_agent_planner = (
            _default_bounded_agent_planner
            if (bounded_agent is not None or reading_agent is not None)
            and bounded_agent_planner is None
            else bounded_agent_planner
        )
        # Injected so a test can pass a stub: building the real one loads ONNX models, and a suite
        # that loaded them to test row-writing would be paying for a model it is not testing.
        self._ocr_engine = ocr_engine
        # **Supplied operands and discriminators, keyed by rule id.**
        #
        # Nothing in the pipeline produces a verdict operand yet: a candidate has no semantic type,
        # `evidence/gate.py:seal` needs a canonical observation, and nothing mints one. So `run_checks`
        # executed every rule against `{}` and every rule abstained — correct, and not a demonstration
        # of anything.
        #
        # These let a caller supply what a reviewer would supply. `CLIENT_FACTS` Q7 blesses exactly
        # that for sink specs: *"the reviewer types the values into input fields for that drawing set"*.
        # A reviewer's own reading is HUMAN_CONFIRMED, which the evidence gate already treats as
        # qualified — so this is the sanctioned route with a human at the reading end, not a bypass.
        #
        # Empty by default, so the production path is unchanged and still abstains until evidence
        # exists. Nothing here invents a value: a caller that supplies none gets the old behaviour.
        # `None` means load reviewer-confirmed operands at check time. An explicit mapping remains
        # useful to isolated tests and evaluators. This prevents an untyped OCR proposal from ever
        # becoming a verdict input while allowing the live worker to see a later human confirmation.
        self._operands = None if operands is None else dict(operands)
        self._discriminators = dict(discriminators or {})

    def _not_built(self, stage: str) -> Mapping[str, object]:
        """The same answer `NoStages` gives, for the stages that are still not built.

        Repeated rather than delegated so the two cannot drift: a reader comparing this class with the
        protocol sees six methods and can tell at a glance which one does work.
        """
        return {"implemented": False, "stage": stage}

    def ingest(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        """Confirm every document this revision names is still the bytes that were uploaded.

        The API hashed each file on the way in and recorded the digest and the page count. Nothing
        has ever checked them again. Until now a document that was truncated, replaced or corrupted
        in storage would be read by `extract_pages` without a word, and the readings would look like
        readings of the drawing somebody submitted.

        **Three facts are checked, and all three are already recorded** — no new authority is
        invented here. The bytes hash to `DocumentVersion.sha256`; the file still parses as a PDF;
        it still has `DocumentVersion.page_count` pages.

        **A failed digest halts the package (#532).** It used to be reported in the payload and the
        pipeline carried on, so a review could be produced of a document nobody submitted — and
        nothing about that result would look wrong.

        Reporting was right when it was decided, for the reason #491 gave: raising would roll the
        claim back and retry the same broken file for ever. What changed is where the raise lands.
        `ArtifactCorrupt` is classified `PERMANENT`, so the failure machinery enters
        `FAILED_PERMANENT` — the one failure state that does not resume. The package stops once,
        with a reason on it, instead of looping.

        The other two findings are still reported rather than raised. A document that will not parse
        is #491's own case and is recorded per page by `extract_pages`; a page count that has moved
        is a fact a person should see, and neither means the package is a different package.
        """
        if self._store is None:
            # The same answer `extract_pages` gives, and for the same reason: no store is a fact
            # about this worker's configuration, not about the drawings.
            return {
                "implemented": True,
                "ran": False,
                "reason": "no artifact store is configured",
                "documents": 0,
            }

        records = _document_records_for(session, package_revision_id)
        verified = 0
        unreadable: list[str] = []
        miscounted: list[str] = []
        for version_id, key, sha256, page_count in records:
            data = _fetch(self._store, key)
            if hashlib.sha256(data).hexdigest() != sha256:
                # **Raised, which halts the package (#532).** This used to be reported and the
                # pipeline carried on, so a review could be produced of a document that is not the
                # one anybody submitted — and nothing about that result looks wrong.
                #
                # Reporting was the right call when it was made, for the reason #491 gave: raising
                # would roll the claim back and retry the same broken file for ever. That is no
                # longer the consequence. `ArtifactCorrupt` is classified `PERMANENT`, so
                # `enter_failure` puts the revision in `FAILED_PERMANENT`, which is the one failure
                # state that does not resume — the package stops, once, with a reason on it.
                #
                # Everything read so far in this stage rolls back with the raise, which is correct:
                # a partial verification of a package that is going no further is not a fact worth
                # keeping.
                raise ArtifactCorrupt(
                    f"document version {version_id} does not match the digest recorded when it was "
                    "uploaded, so this package is not the one that was submitted"
                )
            try:
                pages = read_pages(data)
            except UnreadablePdf as error:
                unreadable.append(f"{version_id}: {error}")
                continue
            if len(pages) != page_count:
                miscounted.append(f"{version_id}: {len(pages)} pages, {page_count} recorded")
                continue
            verified += 1

        return {
            "implemented": True,
            "ran": True,
            "documents": len(records),
            "verified": verified,
            "unreadable": unreadable,
            "page_count_changed": miscounted,
        }

    def extract_pages(self, session: Session, package_revision_id: UUID) -> Sequence[PageResult]:
        """Read every document attached to this revision, and write down what was on its pages.

        Persists three things nothing has ever written: the page manifest, an extraction run, and the
        observation candidates themselves. All three go through the session inside the stage, because
        `run_stage` discards what a stage returns — the payload is for the record, not the work.

        **The candidates carry no semantic type**, and that is the state rather than a shortfall:
        nothing in the system assigns one, and normalisation refuses to infer one from position.
        """
        if self._store is None:
            return ()

        documents = _document_records_for(session, package_revision_id)
        if not documents:
            return ()

        task_run = _task_run_for(session, package_revision_id, "extract_pages")
        if task_run is None:
            # The stage is always called through `run_stage`, which claims a task run first. Reached
            # only when something called this directly, and a candidate with no run could not say
            # what read it.
            return ()

        run = open_extraction_run(
            session,
            task_run_id=task_run.id,
            extractor=EXTRACTOR,
            extractor_version=EXTRACTOR_VERSION,
            config_hash=f"dpi={self._dpi}",
            dpi=self._dpi,
        )
        layout_discriminators = _layout_discriminators(session)
        # **The drawing set's AI spend so far** (#757): every call this task's runs have recorded,
        # so a stage run again after a failure carries on from what was already spent.
        with session.no_autoflush:
            spent, unpriced = session.execute(
                select(
                    func.coalesce(func.sum(ModelInvocation.cost_micros), 0),
                    func.count().filter(ModelInvocation.cost_micros.is_(None)),
                )
                .join(ExtractionRun, ModelInvocation.extraction_run_id == ExtractionRun.id)
                .where(ExtractionRun.task_run_id == task_run.id)
            ).one()
        self._meter = _SpendMeter(
            cap_micros=self._ai_budget_micros,
            spent_micros=int(spent),
            unpriced_calls=int(unpriced),
        )

        results: list[PageResult] = []
        for version, key, sha256, _ in documents:
            # No `try` around the fetch. An artifact this stage cannot read must fail the stage, not
            # be skipped — see `_fetch`.
            data = _fetch(self._store, key)

            # **The bytes are checked before they are read (#523).** `ingest` performs the same
            # comparison and could only report it; refusing is an entry condition on this stage, and
            # this is the stage. Checking here rather than trusting the earlier report also closes
            # the window between the two — the artifact could change in between, and the place that
            # reads a document is the right place to establish it is the right document.
            #
            # Recorded and skipped rather than raised, for the reason #491 gave: a corrupt artifact
            # is not transient, so raising would roll the claim back and retry the same file for
            # ever. The row is what stops a skipped document looking like a document with nothing
            # on it.
            if hashlib.sha256(data).hexdigest() != sha256:
                record_digest_mismatch(
                    session, extraction_run_id=run.id, document_version_id=version
                )
                continue

            with traced(
                "extraction.document",
                document_version_id=str(version),
                task_run_id=str(task_run.id),
                extractor_version=EXTRACTOR_VERSION,
            ):
                # ``Page.index`` is scoped to one document.  The workflow join, however, receives
                # every page in the package and requires a unique ordering key.  Preserve the
                # drawing-local index in the payload and give the package fan-out a deterministic
                # ordinal, so page 0 of the architectural PDF and page 0 of the shop PDF are two
                # distinct work results rather than a false duplicate.
                for document_page in self._read_document(
                    session,
                    package_revision_id=package_revision_id,
                    version_id=version,
                    data=data,
                    run=run,
                    layout_discriminators=layout_discriminators,
                ):
                    results.append(
                        PageResult(
                            index=len(results),
                            payload={
                                **document_page.payload,
                                "document_page_index": document_page.index,
                                "document_version_id": str(version),
                            },
                        )
                    )
        return tuple(results)

    def _read_document(
        self,
        session: Session,
        *,
        package_revision_id: UUID,
        version_id: UUID,
        data: bytes,
        run: ExtractionRun,
        layout_discriminators: Sequence[DiscriminatorNeed],
    ) -> list[PageResult]:
        """One document: its manifest, then its text, page by page."""
        try:
            raw_pages = read_pages(data)
        except UnreadablePdf as error:
            # A document that will not parse is not a document with no dimensions — and until #491 the
            # difference was invisible, because this returned an empty list and the package still
            # reported extraction as complete. Recorded rather than raised: a corrupt file is not
            # transient, so raising would roll back the claim and retry it for ever (#491).
            record_unreadable_document(
                session,
                extraction_run_id=run.id,
                document_version_id=version_id,
                error=error,
            )
            return []

        manifest = build_manifest(
            raw_pages, version_id, minimum_vector_characters=MINIMUM_VECTOR_CHARACTERS
        )
        pages = persist_manifest(session, manifest)

        results: list[PageResult] = []
        for page in pages:
            written = 0
            route = "vector"
            vector_rows: list[ObservationCandidate] = []
            ocr_items: tuple[OcrItem, ...] = ()
            ocr_rows: list[ObservationCandidate] = []
            ocr_fragments: tuple[OcrItem, ...] = ()
            glyph_rows: list[ObservationCandidate] = []
            glyph_association: tuple[tuple[_LocatedOcrReading, ...], list[ObservationCandidate]] = (
                (),
                [],
            )
            glyph_abstentions: Counter[str] | None = None
            vision_rows: list[ObservationCandidate] = []
            vision_invocations = 0
            vision_held_back = 0
            vision_refusals: list[str] = []
            read: PageContents | None = None
            if page.has_vector_text:
                with traced(
                    "extraction.page",
                    document_version_id=str(version_id),
                    page_index=page.index,
                    extractor_version=EXTRACTOR_VERSION,
                ) as span:
                    try:
                        contents = read_page_contents(
                            data, page.index, document_version_id=version_id, dpi=self._dpi
                        )
                    except UnreadablePdf as error:
                        # One page that will not parse, in a document whose other pages might. The
                        # count below stays 0, so without a row this would be indistinguishable from a
                        # page that was read and had nothing on it. The span says so too, but a span
                        # is ephemeral and unexported — the row is the durable half (#491).
                        contents = None
                        record_unreadable_page(
                            session,
                            extraction_run_id=run.id,
                            document_version_id=version_id,
                            page_index=page.index,
                            error=error,
                        )
                        span.set_status(Status(StatusCode.ERROR, "page did not parse"))
                    if contents is not None:
                        vector_rows = record_candidates(
                            session,
                            contents.texts,
                            document_version_id=version_id,
                            page_id=page.id,
                            extraction_run_id=run.id,
                            page_index=page.index,
                            flush=False,
                        )
                        written = len(vector_rows)
                        read = contents
            # **The reviewer's own corrections, which no route above can see.** They are `/FreeText`
            # annotations: text in the file, not marks on a picture of it. Every page of the first
            # real client set reaches here having been sent to OCR, because none of them has vector
            # text — and the twenty corrections on one of those pages were being rasterised and
            # guessed at while sitting in the file as exact strings (#543).
            #
            # Additive, not a third branch of the route decision. Whatever read the page's pixels
            # read them; this reads what was written on top, and both are recorded. Where the two
            # disagree — the vendor's own overall width against the reviewer's correction of it —
            # that disagreement is the review signal and neither row is allowed to replace the other.
            markup_rows, layers = self._read_page_markup(
                session,
                version_id=version_id,
                data=data,
                page=page,
                task_run_id=run.task_run_id,
            )
            # **The vendor's own text, where its CAD program kept it exactly.** A drawing exported
            # from AutoCAD with SHX fonts draws every string as strokes — the shapes #756 reads — and
            # also keeps each string as an invisible note. Read here, not guessed from the strokes.
            cad_text_rows = self._read_page_cad_text(
                session,
                version_id=version_id,
                page=page,
                task_run_id=run.task_run_id,
                layers=layers,
            )
            # **The text inside the pasted drawings, read exactly.** #738 took it for unreadable; it
            # is font text a snapshot carried over from the sheet it was taken from (formats phase 1).
            stamp_texts, stamp_set_aside, stamp_text_rows = self._read_page_stamp_text(
                session,
                version_id=version_id,
                data=data,
                page=page,
                task_run_id=run.task_run_id,
                layers=layers,
            )
            # **Stacked fractions set in text join the ones the bar detector found** (#738). The text
            # readers set them aside rather than read `24 3/4"` as `2434"`; listing them here is what
            # sends them to a reviewer and makes every model crop that shows one abstain (#726).
            text_set_aside = (read.set_aside if read is not None else ()) + stamp_set_aside
            # A stacked fraction read whole (`24 3/4"`, recorded flagged as stacked) is still one: its
            # place is listed with those set aside, so the same rule applies to it (#726).
            stacked_read = tuple(
                item
                for item in (read.texts if read is not None else ()) + stamp_texts
                if item.stacked
            )
            # Set in text, so no paths to lay out (#834): `layout` is `None`.
            text_stacked = tuple(
                StackedFraction(extent=label.extent, image_extent=label.image_extent, layout=None)
                for label in text_set_aside
                if label.reason is SetAsideReason.STACKED_FRACTION
            ) + tuple(
                StackedFraction(extent=item.extent, image_extent=item.image_extent, layout=None)
                for item in stacked_read
            )
            if layers is not None and text_stacked:
                layers = replace(layers, stacked_fractions=layers.stacked_fractions + text_stacked)
            page_stacked = layers.stacked_fractions if layers is not None else text_stacked
            # **Each laid-out stacked label read piece by piece (#848)**, where a deployment turned the
            # route on. What it reads pre-fills the form for a person to tick; it is never agreed into
            # evidence (#726), and the whole label is still never shown to a vision reader (#762).
            fraction_rows: list[ObservationCandidate] = []
            fraction_refusals: Counter[str] | None = None
            fraction_invocations: int | None = None
            if self._fraction_parts is not None:
                (
                    fraction_rows,
                    fraction_refusals,
                    fraction_invocations,
                ) = self._read_page_by_fraction_parts(
                    session,
                    version_id=version_id,
                    page=page,
                    task_run_id=run.task_run_id,
                    fractions=page_stacked,
                )
            # **Which forms this page's text arrived in, and which of them nothing reads yet.** A
            # vendor does not choose how its PDF stores numbers; the page result says, so a page whose
            # text sits in a form no route reads is reported as that, not as a page with no numbers.
            try:
                text_sources: dict[str, object] | None = survey_page(data, page.index).as_payload()
            except UnreadablePdf:
                text_sources = None
            # **Which drawing is which, on a combined sheet (#710).** Each drawing becomes a view, and
            # the label the sheet prints above it gives a suggestion. Never the role: only a person's
            # confirmation sets that, through the API.
            panels = None if layers is None else _record_panel_views(session, page, layers)

            if not page.has_vector_text:
                # A stamp-only vendor drawing has no content-stream text, but it does have exact
                # candidate geometry in its vendor layer. Read those regions as bounded 600-DPI
                # crops rather than asking OCR to find tiny labels on a full sheet. This needs the
                # deployment's association geometry settings: without them the pipeline would be
                # inventing the detector configuration that turns paths into candidate regions.
                # A page with no usable vendor regions keeps the normal full-page OCR route for
                # scans and other non-vector inputs.
                if (
                    self._association is not None
                    and self._localized_ocr is not None
                    and layers is not None
                ):
                    plan = plan_reads(
                        layers,
                        proximity_limit=self._association.proximity_limit,
                        minimum_paths=self._localized_ocr.minimum_paths,
                        maximum_span=self._localized_ocr.maximum_span,
                    )
                    if plan.to_read:
                        route = "localized_ocr"
                        ocr_items, ocr_rows, ocr_fragments = self._read_page_by_localized_ocr(
                            session,
                            version_id=version_id,
                            data=data,
                            page=page,
                            task_run_id=run.task_run_id,
                            layers=layers,
                            regions=tuple(entry.region for entry in plan.to_read),
                        )
                        if self._glyph_route is not None:
                            glyph_rows, glyph_association, glyph_abstentions = (
                                self._read_page_by_glyphs(
                                    session,
                                    version_id=version_id,
                                    page=page,
                                    task_run_id=run.task_run_id,
                                    layers=layers,
                                    regions=tuple(entry.region for entry in plan.to_read),
                                )
                            )
                    else:
                        # No line-selected region is an explicit localized abstention. Falling
                        # back to full-page OCR would reintroduce the tiny-text failure and could
                        # grab an unrelated number; reviewers can see the geometry set-asides.
                        route = "localized_ocr"
                        ocr_items, ocr_rows, ocr_fragments = (), [], ()
                else:
                    route = "ocr"
                    ocr_items, ocr_rows, ocr_fragments = self._read_page_by_ocr(
                        session,
                        version_id=version_id,
                        data=data,
                        page=page,
                        task_run_id=run.task_run_id,
                    )
                written = len(ocr_rows)

            vector_association_inputs = (
                (read.texts if read is not None else ()),
                tuple(vector_rows),
            )
            ocr_association_inputs = self._ocr_association_inputs(ocr_items, ocr_rows)
            # **The boxes of OCR text that could not be a reading** (#703). Not candidates, and not
            # given to `associate` as readings. A vision reading of one is associated under the
            # rule a recorded row's is — only where the OCR layout established an orientation,
            # which for a box like these it usually has not — so nothing here attaches more than
            # the recorded row would have.
            fragment_regions = tuple(
                _VisionRegion(
                    id=uuid4(),
                    polygon=[[point.x, point.y] for point in item.image_extent],
                    document_version_id=version_id,
                )
                for item in ocr_fragments
            )
            association_sources: Mapping[UUID, ReadItem] = {}

            vision_association_links: tuple[_VisionAssociationLink, ...] = ()
            if self._vision_readers:
                association_sources = _association_source_items(
                    vector_association_inputs,
                    ocr_association_inputs,
                    self._ocr_association_inputs(ocr_fragments, fragment_regions),
                )
                (
                    vision_rows,
                    vision_invocations,
                    vision_refusals,
                    vision_association_links,
                    vision_held_back,
                ) = self._read_page_by_vision(
                    session,
                    version_id=version_id,
                    data=data,
                    page=page,
                    task_run_id=run.task_run_id,
                    # The shape reader's boxes too (#756 D2): a vision reading of the same box is the
                    # second witness a glyph reading may be confirmed by.
                    regions=(
                        tuple(_VisionRegion.of(row) for row in vector_rows + ocr_rows + glyph_rows)
                        + fragment_regions
                    ),
                    stacked_fractions=page_stacked,
                )

            self._apply_cross_route_corroboration(
                session,
                page_index=page.index,
                candidates=tuple(
                    vector_rows
                    + ocr_rows
                    + markup_rows
                    + cad_text_rows
                    + stamp_text_rows
                    + vision_rows
                    + glyph_rows
                    # Judged with the rest, and its flag keeps every group it is in a raw candidate.
                    + fraction_rows
                ),
            )
            agent = self._run_bounded_agent_for_ambiguous_regions(
                session,
                version_id=version_id,
                data=data,
                page=page,
                task_run_id=run.task_run_id,
                candidates=tuple(
                    vector_rows
                    + ocr_rows
                    + markup_rows
                    + cad_text_rows
                    + stamp_text_rows
                    + vision_rows
                ),
                layers=layers,
            )
            agent_rows = agent.rows
            if agent_rows:
                page_rows = tuple(
                    vector_rows
                    + ocr_rows
                    + markup_rows
                    + cad_text_rows
                    + stamp_text_rows
                    + vision_rows
                    + agent_rows
                )
                self._apply_cross_route_corroboration(
                    session, page_index=page.index, candidates=page_rows
                )
                self._mark_regions_the_agent_contradicted(
                    session, page_index=page.index, candidates=page_rows, agent_rows=agent_rows
                )
            layout_written, layout_refusals = self._classify_page_layouts(
                session,
                package_revision_id=package_revision_id,
                version_id=version_id,
                data=data,
                page=page,
                extraction_run_id=run.id,
                discriminators=layout_discriminators,
            )
            session.flush()

            # **Both routes' readings against all of the page's lines, in one pass.** A reviewer's
            # correction labels a line the vendor drew, so judging the two layers against separate
            # sets of geometry would refuse exactly the associations that matter most.
            # `associate` neither knows nor needs to know which layer a line came from: keeping the
            # layers apart is about the authority of a *value*, not about what is near it.
            associated = self._associate_page(
                session,
                page=page,
                task_run_id=run.task_run_id,
                readings=(
                    vector_association_inputs,
                    ocr_association_inputs,
                    _vision_association_inputs(
                        vision_association_links,
                        association_sources,
                    ),
                    ((layers.markup if layers is not None else ()), markup_rows),
                    ((layers.vendor_text if layers is not None else ()), cad_text_rows),
                    (stamp_texts, stamp_text_rows),
                    # Glyph readings attach to the line they label like any reading — the reader
                    # established which way each label runs, which is what `associate` needs.
                    glyph_association,
                ),
                lines=(
                    (read.segments if read is not None else ())
                    + (layers.drawing_segments if layers is not None else ())
                ),
            )
            # **What each vendor drawing's parts might be (#868)**, suggested for a person to confirm
            # and never written as items. The same strokes, and the same readings less the
            # reviewer's markup: a code is what the vendor printed on a part, and a reviewer's note
            # beside it is a reviewer's word about the drawing, not part of it.
            parts = self._propose_page_parts(
                session,
                page=page,
                readings=(
                    vector_association_inputs,
                    ocr_association_inputs,
                    _vision_association_inputs(vision_association_links, association_sources),
                    ((layers.vendor_text if layers is not None else ()), cad_text_rows),
                    (stamp_texts, stamp_text_rows),
                    glyph_association,
                ),
                lines=(
                    (read.segments if read is not None else ())
                    + (layers.drawing_segments if layers is not None else ())
                ),
            )
            results.append(
                PageResult(
                    index=page.index,
                    payload={
                        "candidates": written
                        + len(vision_rows)
                        + len(agent_rows)
                        + len(glyph_rows)
                        + len(fraction_rows),
                        "markup_candidates": len(markup_rows),
                        # The reviewer's measurement lines (#805): read as markup when they carry
                        # text, set aside when they carry none. `None` when the annotations could
                        # not be read at all.
                        "measurement_lines": (
                            None
                            if layers is None
                            else sum(
                                1 for note in layers.markup if note.intent == LINE_DIMENSION_INTENT
                            )
                        ),
                        "measurement_lines_without_text": (
                            None
                            if layers is None
                            else sum(
                                1
                                for note in layers.other_layer_notes
                                if note.intent == LINE_DIMENSION_INTENT
                            )
                        ),
                        # The vendor's exact CAD text (AutoCAD SHX notes): zero on a drawing that
                        # was printed to PDF rather than exported, which is every drawing so far.
                        "cad_text_candidates": len(cad_text_rows),
                        "stamp_text_candidates": len(stamp_text_rows),
                        # Runs of text never read as numbers (#738), by why: the stacked fractions
                        # go to a reviewer; the others are left for the readers that see the whole.
                        "text_set_aside": dict(
                            Counter(label.reason.value for label in text_set_aside)
                        ),
                        # Stacked fractions read whole and recorded as a reviewer's suggestion.
                        "stacked_fractions_read": len(stacked_read),
                        "text_sources": text_sources,
                        "vision_candidates": len(vision_rows),
                        # OCR text that could not be a reading (#703): counted here because it is
                        # not a candidate, so without this the page would simply look smaller.
                        # Each box was still offered to the vision readers.
                        "ocr_fragments": len(ocr_fragments),
                        # The shape reader (#756): `None` when it did not run on this page, which
                        # is not the same fact as its having read nothing.
                        "glyph_readings": None if glyph_abstentions is None else len(glyph_rows),
                        # Of those, the stacked fractions pre-filled for a person (#756 D3).
                        "glyph_stacked_fractions": (
                            None
                            if glyph_abstentions is None
                            else sum(
                                STACKED_FRACTION_FLAG in row.ambiguity_flags for row in glyph_rows
                            )
                        ),
                        # Stacked fractions read piece by piece (#848), each pre-filled for a
                        # person: `None` when the route is off, which is not the same fact as its
                        # having read nothing. The reasons are this route's own sentences.
                        "fraction_parts_readings": (
                            None if fraction_refusals is None else len(fraction_rows)
                        ),
                        "fraction_parts_refusals": (
                            None if fraction_refusals is None else sum(fraction_refusals.values())
                        ),
                        "fraction_parts_refusal_reasons": (
                            None
                            if fraction_refusals is None
                            else [
                                f"{count} × {reason}"
                                for reason, count in fraction_refusals.most_common(
                                    REPORTED_REFUSALS
                                )
                            ]
                        ),
                        # The second reader's calls for those pieces (#865), each in
                        # `model_invocations` and on the drawing set's budget.
                        "fraction_parts_invocations": fraction_invocations,
                        # What the AI readers have spent on this drawing set so far, against its
                        # cap (#757): once reached, labels go to a reviewer without a model reading.
                        "ai_budget": None if self._meter is None else self._meter.as_payload(),
                        "glyph_abstentions": (
                            None if glyph_abstentions is None else sum(glyph_abstentions.values())
                        ),
                        "glyph_abstention_reasons": (
                            None
                            if glyph_abstentions is None
                            else [
                                f"{count} × {reason}"
                                for reason, count in glyph_abstentions.most_common(
                                    REPORTED_REFUSALS
                                )
                            ]
                        ),
                        "ocr_fragment_texts": _fragment_texts(ocr_fragments),
                        "agent_candidates": len(agent_rows),
                        "agent_proposals": agent.proposals,
                        "agent_abstentions": agent.abstentions,
                        # `None` where no agent is configured: not run, which is not zero regions.
                        "agent_regions": (
                            None
                            if self._bounded_agent is None and self._reading_agent is None
                            else agent.regions
                        ),
                        "agent_invocations": agent.invocations,
                        "agent_regions_reused": agent.reused,
                        "agent_regions_without_a_numeral": agent.without_a_numeral,
                        "layout_proposals": layout_written,
                        # `None` when the page's annotations could not be read at all.
                        "panels": panels,
                        "layout_refusals": layout_refusals[:REPORTED_REFUSALS],
                        "vision_invocations": vision_invocations,
                        # Regions a gated reader was not asked about, the gate having read no value
                        # in them (#787). `None` where no gate is configured.
                        "vision_gate_held_back": (
                            None if self._vision_gate is None else vision_held_back
                        ),
                        "vision_refusals": vision_refusals[:REPORTED_REFUSALS],
                        # `None` when no thresholds were configured: the step did not run, which is
                        # not the same fact as its having found nothing.
                        "associations": associated,
                        # The parts suggested in this page's vendor drawings (#868), counted; `None`
                        # when no association settings were stated, which is not the same as none.
                        "part_proposals": parts,
                        "has_vector_text": page.has_vector_text,
                        # Which route read this page. Two readings of the same page by different
                        # routes are the basis of corroboration, so the route has to be visible.
                        "route": route,
                    },
                )
            )
        return results

    def _classify_page_layouts(
        self,
        session: Session,
        *,
        package_revision_id: UUID,
        version_id: UUID,
        data: bytes,
        page: Page,
        extraction_run_id: UUID,
        discriminators: Sequence[DiscriminatorNeed],
    ) -> tuple[int, list[str]]:
        """Ask each rulebook layout discriminator as a closed question for this rendered page."""

        if self._store is None or not discriminators:
            return 0, []
        try:
            rendered = render_page(
                data,
                page.index,
                document_version_id=version_id,
                page_content_hash=page.content_hash,
                dpi=self._dpi,
                maximum_pixels=MAXIMUM_RENDER_PIXELS,
                # A model classifies this page's layout: it is shown the vendor's drawing only (#742).
                vendor_only=True,
            )
        except (PageTooLarge, UnreadablePdf, ValueError) as error:
            reason = str(error).strip() or type(error).__name__
            return 0, [f"page {page.index}: {reason}"]

        written = 0
        refused: list[str] = []
        readers = tuple(route.reader for route in self._layout_reader_routes)
        model_id, prompt_id = _layout_proposal_identity(self._layout_reader_routes)
        for discriminator in discriminators:
            classification = classify_layout(
                rendered,
                question_from_discriminator(discriminator),
                readers,
            )
            crop_artifact_id = self._layout_crop_artifact_id(
                session,
                rendered=rendered,
                page=page,
                extraction_run_id=extraction_run_id,
                discriminator_name=discriminator.name,
                classification=classification,
            )
            if crop_artifact_id is None:
                refused.append(f"page {page.index}: {discriminator.name}: crop was not available")
                continue
            record_layout_proposal(
                session,
                package_revision_id=package_revision_id,
                discriminator_name=discriminator.name,
                proposed_value=_layout_proposed_value(classification),
                crop_artifact_id=crop_artifact_id,
                model_id=model_id,
                prompt_id=prompt_id,
            )
            written += 1
        return written, refused

    def _layout_crop_artifact_id(
        self,
        session: Session,
        *,
        rendered: RenderedPage,
        page: Page,
        extraction_run_id: UUID,
        discriminator_name: str,
        classification: LayoutClassification,
    ) -> UUID | None:
        if self._store is None:
            return None
        crop = generate_crop(
            rendered,
            CropSpec(
                polygon=classification.region,
                context_margin_pt=CROP_CONTEXT_MARGIN_PT,
                dpi=self._dpi,
            ),
            self._store,
        )
        if crop.status is not CropStatus.AVAILABLE or crop.artifact is None:
            return None

        existing = session.execute(
            select(EvidenceArtifact).where(
                EvidenceArtifact.storage_key == crop.artifact.key,
                EvidenceArtifact.sha256 == crop.artifact.sha256,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id

        candidate = ObservationCandidate(
            document_version_id=rendered.document_version_id,
            page_id=page.id,
            extraction_run_id=extraction_run_id,
            raw_text=(
                f"layout {discriminator_name}: {classification.status.value}: "
                f"{classification.reason}"
            ),
            polygon=_image_polygon(classification.region, rendered),
            ambiguity_flags=[],
        )
        session.add(candidate)
        session.flush()
        artifact = EvidenceArtifact(
            candidate_id=candidate.id,
            canonical_observation_id=None,
            document_version_id=page.document_version_id,
            page_id=page.id,
            kind=EvidenceArtifactKind.CROP.value,
            storage_key=crop.artifact.key,
            sha256=crop.artifact.sha256,
            media_type="image/png",
            coordinate_space="image",
        )
        session.add(artifact)
        session.flush()
        return artifact.id

    @staticmethod
    def _ocr_association_inputs(
        items: tuple[OcrItem, ...], rows: Sequence[_Source]
    ) -> tuple[tuple[_LocatedOcrReading, ...], tuple[_Source, ...]]:
        """Keep only OCR readings whose layout states an axis; retain row pairing exactly.

        Arbitrary OCR boxes carry no declared rotation, and production association deliberately
        refuses to infer one. The tightly recognised dual-unit layout does establish that its two
        rows read horizontally, so those readings can use the same association machinery as vector
        and markup text. Everything else remains recorded but unassociated.
        """
        located: list[_LocatedOcrReading] = []
        located_rows: list[_Source] = []
        for item, row in zip(items, rows, strict=True):
            if item.extent is None or item.rotation_degrees is None:
                continue
            located.append(_LocatedOcrReading(item.extent, item.rotation_degrees))
            located_rows.append(row)
        return tuple(located), tuple(located_rows)

    @staticmethod
    def _ordered_ocr_rows(
        items: tuple[OcrItem, ...], rows: list[ObservationCandidate]
    ) -> list[ObservationCandidate]:
        """Match persisted rows to OCR items by stored content, never query return order."""
        available: dict[
            tuple[str, tuple[tuple[int, int], ...], Decimal], list[ObservationCandidate]
        ] = {}
        for row in rows:
            if row.confidence is None:
                raise ValueError("a persisted OCR row has no OCR confidence")
            key = (
                row.raw_text,
                tuple((int(x), int(y)) for x, y in row.polygon),
                row.confidence,
            )
            available.setdefault(key, []).append(row)
        ordered: list[ObservationCandidate] = []
        for item in items:
            key = (
                item.text,
                tuple((point.x, point.y) for point in item.image_extent),
                item.confidence,
            )
            matches = available.get(key, [])
            if not matches:
                raise ValueError("a persisted OCR row does not match the reading that produced it")
            ordered.append(matches.pop())
        return ordered

    @staticmethod
    def _apply_cross_route_corroboration(
        session: Session,
        *,
        page_index: int,
        candidates: Sequence[ObservationCandidate],
    ) -> None:
        """Run the second-reader lane across same-region readings before first insert."""

        pending_ids = {row.id for row in candidates if inspect(row).pending}
        if not pending_ids:
            return

        by_region: dict[tuple[UUID, tuple[tuple[int, int], ...]], list[ObservationCandidate]] = {}
        for row in candidates:
            if row.corroboration_lane is not None:
                continue
            key = (row.page_id, tuple((int(x), int(y)) for x, y in row.polygon))
            by_region.setdefault(key, []).append(row)

        run_ids = {row.extraction_run_id for rows in by_region.values() for row in rows}
        with session.no_autoflush:
            runs = {
                run.id: run
                for run in session.execute(
                    select(ExtractionRun).where(ExtractionRun.id.in_(run_ids))
                ).scalars()
            }
        for rows in by_region.values():
            if not any(row.id in pending_ids for row in rows):
                continue
            result = corroborate(
                tuple(
                    _domain_candidate_from_row(row, runs[row.extraction_run_id], page_index)
                    for row in rows
                )
            )
            if result.lane is None:
                continue
            for row in rows:
                if row.id in pending_ids:
                    row.corroboration_status = result.status.value
                    row.corroboration_lane = result.lane.value

    @staticmethod
    def _mark_regions_the_agent_contradicted(
        session: Session,
        *,
        page_index: int,
        candidates: Sequence[ObservationCandidate],
        agent_rows: Sequence[ObservationCandidate],
    ) -> None:
        """Mark a whole region conflicting where the agent's new readings disagree with its old ones.

        **The second pass cannot see this on its own.** `_apply_cross_route_corroboration` groups
        only rows with no lane yet, so an agent reading lands in a group of its own when the
        region's first readers had already agreed. In the #641 case — both readers shown a crop
        that cut `10192"` agree on `92"`, and the agent, shown the whole label, reads `10192"` twice
        — the region would end holding two different values, each marked as two readers agreeing,
        and automatic typing (`app/evidence/automatic_typing.py`) could seal either.

        **Only ever towards a reviewer.** Every row of the region with a value is judged together,
        by `evidence/corroborate.py`, whatever lane it already has; where that judgement is
        `CONFLICTING`, every row of the region is marked so. Any other judgement changes nothing,
        so this can take an agreement away and can never make one.

        Only regions holding an agent row written by this delivery are judged. A redelivered stage
        reuses its agent rows rather than writing them, and leaves what the first delivery decided.
        """
        new_agent_regions = {
            (row.page_id, tuple((int(x), int(y)) for x, y in row.polygon))
            for row in agent_rows
            if inspect(row).pending
        }
        by_region: dict[tuple[UUID, tuple[tuple[int, int], ...]], list[ObservationCandidate]] = {}
        for row in candidates:
            key = (row.page_id, tuple((int(x), int(y)) for x, y in row.polygon))
            if key in new_agent_regions:
                by_region.setdefault(key, []).append(row)
        run_ids = {row.extraction_run_id for rows in by_region.values() for row in rows}
        with session.no_autoflush:
            runs = {
                run.id: run
                for run in session.execute(
                    select(ExtractionRun).where(ExtractionRun.id.in_(run_ids))
                ).scalars()
            }
        for rows in by_region.values():
            valued = [row for row in rows if row.value_numerator is not None]
            if len(valued) < 2:
                continue
            result = corroborate(
                tuple(
                    _domain_candidate_from_row(row, runs[row.extraction_run_id], page_index)
                    for row in valued
                )
            )
            if result.status is not EvidenceStatus.CONFLICTING or result.lane is None:
                continue
            # **Only readings not yet saved (#790).** A reading is append-only once it is in the
            # database, and the page's panel step saves earlier routes' readings before the vision
            # readers run. A saved reading in the region keeps its status; the region still counts
            # as conflicting, because automatic typing refuses any region that holds one
            # (`app/evidence/automatic_typing._second_reader_candidate_ids`), and these readings —
            # the agent's own among them — are marked so.
            for row in rows:
                if not inspect(row).pending:
                    continue
                row.corroboration_status = result.status.value
                row.corroboration_lane = result.lane.value

    def _run_bounded_agent_for_ambiguous_regions(
        self,
        session: Session,
        *,
        version_id: UUID,
        data: bytes,
        page: Page,
        task_run_id: UUID,
        candidates: Sequence[ObservationCandidate],
        layers: PageLayers | None = None,
    ) -> _AgentPageOutcome:
        """Run the bounded agent only for regions the deterministic trigger permits, once each.

        **One run per region, not per row** (#757). A region read by three routes holds three rows
        at one polygon — the rule cross-route corroboration groups by — and the geometry that can
        trigger the agent is the region's, so without this one label would be read three times.

        **The trigger's reasons now include the file's geometry** — cut off at the crop's edge,
        sideways, a stacked fraction — each from `RegionFacts`, which holds no reading's text.

        **Only a region some route read a numeral in is looked at, and an exact route's unparsed
        string is no reason** (#792): what is left out is words, whose only "value" a model can find
        is a neighbouring label's.

        **The shape reader's readings are not handed to the agent.** The admin's #756 D2
        (2026-10-01) decided what confirms one — a second witness, through corroboration — not what
        the agent may do with it. The decision table can take one as a witness —
        `RegionFacts.shape_reading`, which can turn a proposal into an abstention and never the
        reverse — and it stays `None` here until that is built and measured on its own.
        """
        outcome = _AgentPageOutcome()
        if (
            (self._bounded_agent is None and self._reading_agent is None)
            or self._bounded_agent_planner is None
            or self._store is None
            or not candidates
        ):
            return outcome

        renders: dict[int, RenderedPage | str] = {}

        def render(dpi: int) -> RenderedPage | str:
            if dpi not in renders:
                try:
                    renders[dpi] = render_page(
                        data,
                        page.index,
                        document_version_id=version_id,
                        page_content_hash=page.content_hash,
                        dpi=dpi,
                        maximum_pixels=MAXIMUM_RENDER_PIXELS,
                        # The bounded agent asks a model about these crops: vendor's drawing only
                        # (#742).
                        vendor_only=True,
                    )
                except (PageTooLarge, UnreadablePdf, ValueError) as error:
                    renders[dpi] = str(error) or type(error).__name__
            return renders[dpi]

        rendered = render(self._dpi)
        if isinstance(rendered, str):
            return outcome

        settings = self._reading_agent
        transform = page_transform(page, self._dpi)
        reach = (
            settings.reach(self._association.glyph_gap_pt)
            if settings is not None and self._association is not None
            else None
        )
        fractions = () if layers is None else layers.stacked_fractions
        page_glyphs = () if layers is None else layers.glyph_paths

        def stacked(polygon: Polygon) -> bool:
            return crop_shows_a_stacked_fraction(
                crop_box_px(rendered, polygon, VISION_CROP_CONTEXT_MARGIN_PT), fractions
            )

        def layouts(polygon: Polygon) -> tuple[FractionLayout, ...]:
            return stacked_layouts_shown(
                crop_box_px(rendered, polygon, VISION_CROP_CONTEXT_MARGIN_PT), fractions
            )

        def region_of(row: ObservationCandidate) -> tuple[tuple[int, ...], ...]:
            return tuple(tuple(int(value) for value in point) for point in row.polygon)

        # **A region no route read a numeral in is not the agent's** (#792). Every route's text
        # counts — the file's own, OCR's, the gate reader's — so a label any of them saw a digit of is
        # still looked at. What is left is words and marks. On AI_Set_2 the agent's readers returned a
        # value in 148 such regions — 92 words, 23 single letters, 33 marks like `"` — and what they
        # read was a label elsewhere in the crop (`Vendor` read as `24"`): a value pinned to a place
        # that does not hold it, which two readers of different vendors could agree on. Set aside
        # here, before any geometry is computed or any model is asked.
        with_a_numeral = {region_of(row) for row in candidates if _shows_a_numeral(row.raw_text)}
        without_a_numeral: set[tuple[tuple[int, ...], ...]] = set()
        run_ids = {row.extraction_run_id for row in candidates}
        with session.no_autoflush:
            extractor_of = {
                run_id: extractor
                for run_id, extractor in session.execute(
                    select(ExtractionRun.id, ExtractionRun.extractor).where(
                        ExtractionRun.id.in_(run_ids)
                    )
                ).tuples()
            }

        handled: set[tuple[tuple[int, ...], ...]] = set()
        for candidate in candidates:
            region = region_of(candidate)
            if region in handled:
                continue
            if region not in with_a_numeral:
                without_a_numeral.add(region)
                continue
            status = _candidate_evidence_status(candidate)
            if status is not EvidenceStatus.RAW_CANDIDATE:
                continue
            polygon = stored_polygon(candidate, rendered)
            facts, whole_run = region_facts(
                candidate,
                candidates,
                version_id=version_id,
                page=page,
                rendered=rendered,
                polygon=polygon,
                transform=transform,
                reach=reach,
                page_glyphs=page_glyphs,
                stacked=stacked,
            )
            reasons = _ambiguity_reasons(
                candidate,
                exact_text=extractor_of[candidate.extraction_run_id] in EXACT_TEXT_EXTRACTORS,
            ) | trigger_reasons(facts)
            if not reasons:
                continue

            crops = (
                None
                if polygon is None
                else RegionCrops(
                    store=self._store,
                    render=render,
                    base=rendered,
                    polygon=polygon,
                    margin_pt=VISION_CROP_CONTEXT_MARGIN_PT,
                    sharper_dpi=(settings.sharper_dpi if settings is not None else self._dpi),
                    whole_run=whole_run,
                    rotation_degrees=facts.rotation_degrees,
                    stacked=stacked,
                    layouts=layouts,
                )
            )
            crop_artifact_id = None if crops is None else crops.first()
            trigger_context = RegionContext(
                region_id=str(candidate.id),
                fixed_extraction_complete=True,
                crop_available=crop_artifact_id is not None,
                ambiguity_reasons=reasons,
            )
            decision = evaluate_trigger(status, trigger_context)
            if not decision.triggered:
                continue
            if crop_artifact_id is None or crops is None:
                raise AssertionError("triggered agent region without a crop artifact")
            handled.add(region)
            outcome.regions += 1

            graph_context = BoundedRegionContext(
                region_id=decision.region_id,
                crop_artifact_id=crop_artifact_id,
                nearby_text=(),
                nearby_geometry_refs=(),
            )
            reads: _AgentReads | None = None
            if settings is None:
                assert self._bounded_agent is not None
                graph = self._bounded_agent
            else:
                # **A region already run in this task run is not paid for again.** A redelivered stage
                # finds the run it opened and the rows it recorded; one with no row abstained.
                recorded = self._recorded_agent_rows(session, task_run_id, candidate.id)
                if recorded is not None:
                    outcome.rows.extend(recorded)
                    outcome.reused += 1
                    continue
                source_candidate_id = candidate.id

                def open_run(reader: _VisionReader, source: UUID = source_candidate_id) -> UUID:
                    return self._agent_run(
                        session,
                        task_run_id=task_run_id,
                        extractor=reader.config.extractor,
                        extractor_version=reader.config.model_id,
                        source_candidate_id=source,
                    ).id

                reads = _AgentReads(
                    session=session,
                    page_index=page.index,
                    crops=crops,
                    readers=self._agent_readers,
                    open_run=open_run,
                    meter=self._meter,
                )
                graph = BoundedAgentGraph(
                    limits=settings.limits,
                    toolbox=AgentToolbox(
                        refine_crop=crops.refine,
                        request_ocr_verification=_no_ocr_verification,
                        request_vlm_reading=reads.read,
                        abstain=agent_abstain,
                        recorder=_AgentCallLog(),
                    ),
                )
            terminal = graph.run(
                graph_context,
                self._bounded_agent_planner(graph_context, facts, graph.limits),
            )
            if isinstance(terminal, CandidateTerminal):
                outcome.proposals += 1
            elif isinstance(terminal, AbstentionTerminal):
                outcome.abstentions += 1
            else:
                raise TypeError("bounded agent returned an unknown terminal")
            if reads is None:
                # An injected graph brings its own tools, so only what it proposed is known here.
                if isinstance(terminal, CandidateTerminal):
                    outcome.rows.append(
                        self._record_agent_candidate(
                            session,
                            terminal.candidate,
                            document_version_id=version_id,
                            page_id=page.id,
                            task_run_id=task_run_id,
                            source_candidate=candidate,
                            flush=False,
                        )
                    )
                continue
            # **Every look is a candidate, and every paid call is recorded with it** (DESIGN_AI
            # §3.2: a new look adds a new candidate; corroboration decides). A proposal is one of
            # these — the graph refuses any other — and an abstention keeps them too: where the
            # agent read the whole label and the region's other readers read the part a crop cut,
            # it hands the region over *with* the whole reading, for the reviewer to see.
            for recorder, produced in reads.recorders:
                row = None
                if produced:
                    row = self._record_agent_candidate(
                        session,
                        produced[0],
                        document_version_id=version_id,
                        page_id=page.id,
                        task_run_id=task_run_id,
                        source_candidate=candidate,
                        flush=False,
                    )
                    outcome.rows.append(row)
                outcome.invocations += recorder.persist(
                    candidate_id=None if row is None else row.id, flush=False
                )
        outcome.without_a_numeral = len(without_a_numeral)
        return outcome

    def _agent_config_hash(self, source_candidate_id: UUID) -> str:
        """An agent run's identity: the region it read, and every setting it was read under."""
        return (
            f"dpi={self._dpi};route=bounded_agent;layers=vendor;"
            f"source_candidate_id={source_candidate_id}"
            + ("" if self._reading_agent is None else f";{self._reading_agent.config_hash}")
        )

    def _agent_run(
        self,
        session: Session,
        *,
        task_run_id: UUID,
        extractor: str,
        extractor_version: str,
        source_candidate_id: UUID,
    ) -> ExtractionRun:
        """The run one reader's agent readings of one region are recorded under.

        **Under the reader's own extractor**, never an agent-wide one: `evidence/corroborate.py`
        counts independence by extractor, and the same model reading the same label a second time
        is not a second witness.
        """
        return open_extraction_run(
            session,
            task_run_id=task_run_id,
            extractor=extractor,
            extractor_version=extractor_version,
            config_hash=self._agent_config_hash(source_candidate_id),
            dpi=self._dpi,
        )

    def _recorded_agent_rows(
        self, session: Session, task_run_id: UUID, source_candidate_id: UUID
    ) -> list[ObservationCandidate] | None:
        """The rows an earlier delivery of this stage recorded for this region, or `None` if none ran."""
        with session.no_autoflush:
            runs = list(
                session.execute(
                    select(ExtractionRun.id).where(
                        ExtractionRun.task_run_id == task_run_id,
                        ExtractionRun.config_hash == self._agent_config_hash(source_candidate_id),
                    )
                ).scalars()
            )
            if not runs:
                return None
            return list(
                session.execute(
                    select(ObservationCandidate).where(
                        ObservationCandidate.extraction_run_id.in_(runs)
                    )
                ).scalars()
            )

    def _record_agent_candidate(
        self,
        session: Session,
        candidate: DomainCandidate,
        *,
        document_version_id: UUID,
        page_id: UUID,
        task_run_id: UUID,
        source_candidate: ObservationCandidate,
        flush: bool = True,
    ) -> ObservationCandidate:
        run = self._agent_run(
            session,
            task_run_id=task_run_id,
            extractor=candidate.extractor,
            extractor_version=candidate.extractor_version,
            source_candidate_id=source_candidate.id,
        )
        return self._record_vision_candidate(
            session,
            candidate,
            document_version_id=document_version_id,
            page_id=page_id,
            extraction_run_id=run.id,
            page_polygon=source_candidate.polygon,
            flush=flush,
        )

    def _associate_page(
        self,
        session: Session,
        *,
        page: Page,
        task_run_id: UUID,
        readings: Sequence[tuple[Sequence[ReadItem], Sequence[ObservationCandidate]]],
        lines: tuple[DimensionExtent, ...],
    ) -> int | None:
        """Attach each of a page's readings to the line it annotates, or record why not.

        Returns the number of decisions recorded, or `None` when no thresholds were configured —
        which is a different fact from zero. Zero means the step ran and had nothing to decide; `None`
        means nobody asked it to run, and a caller that could not tell them apart would read an
        unconfigured pipeline as a page with no readings on it.

        **Its own extraction run, carrying the thresholds.** The third per page, for the same reason
        as the second: a run is keyed on its configuration, so a re-association under a different
        proximity limit is a different run and its rows cannot be confused with the first's.

        **Every reading gets a row, attached or refused.** `AssociationResult` refuses to be built if
        one goes missing from both halves, and the refusals are the half that matters today: two
        lines equally close to one number is the ordinary case on a dimensioned elevation, and an
        unattached number is what a reviewer has to look at.
        """
        settings = self._association
        if settings is None:
            return None

        texts: list[DimensionText] = []
        for items, rows in readings:
            if not items:
                continue
            texts.extend(dimension_texts(items, [row.id for row in rows]))
        if not texts:
            return 0

        with traced(
            "extraction.page.association",
            document_version_id=str(page.document_version_id),
            page_index=page.index,
            extractor_version=ASSOCIATION_EXTRACTOR_VERSION,
        ):
            # **Only the dimension lines are offered, not every stroke on the page.**
            #
            # `associate` was previously handed all of them, which meant a reading could attach
            # itself to the edge of a cabinet as readily as to the dimension that measures it — and
            # once attached the two are indistinguishable. `DESIGN_EXTRACTION.md` §6 names that as
            # the failure this layer exists to prevent: the number reads correctly, the arithmetic
            # is exact, and the finding is about the wrong thing.
            #
            # On the first real sheet this takes 136 strokes down to 11 candidates. The other 125
            # are cabinets, borders and hatching, and every one of them used to be somewhere a
            # number could land.
            detected = detect(
                lines,
                witness_tolerance=settings.witness_tolerance,
                minimum_span=settings.minimum_span,
                straightness=settings.straightness,
                crossing_margin=settings.crossing_margin,
            )
            result = associate(
                tuple(texts),
                tuple(line.extent for line in detected.lines),
                proximity_limit=settings.proximity_limit,
                ambiguity_margin=settings.ambiguity_margin,
            )
            association_run = open_extraction_run(
                session,
                task_run_id=task_run_id,
                extractor=ASSOCIATION_EXTRACTOR,
                extractor_version=ASSOCIATION_EXTRACTOR_VERSION,
                config_hash=f"dpi={self._dpi};{settings.config_hash}",
                dpi=self._dpi,
            )
            return len(
                record_associations(
                    session,
                    result,
                    extraction_run_id=association_run.id,
                    chains=_chain_membership(detected, page_id=page.id),
                    # Zero here is the fact a scanned drawing turns on: no line-work was found, so
                    # the attachment check could not run, and a refusal recorded against it must not
                    # read downstream as an examination.
                    lines_on_page=len(detected.lines),
                )
            )

    def _propose_page_parts(
        self,
        session: Session,
        *,
        page: Page,
        readings: Sequence[tuple[Sequence[ReadItem], Sequence[ObservationCandidate]]],
        lines: tuple[DimensionExtent, ...],
    ) -> dict[str, int] | None:
        """Suggest the parts of each vendor drawing on the page, as `part_proposals` rows (#868).

        Returns counts, or `None` when no association settings were stated. The detector takes its
        four lengths from them, so without them there are no dimensions to suggest anything from,
        which is a different fact from a page that has none.

        **Suggestions only.** Every row is written by `record_part_proposal`, and no drawing item is
        written: only a person's confirmation makes one (#852).

        **Which drawing is the vendor's** is the role a person confirmed, or, where nobody has yet,
        the role the sheet's own label suggests (#710). A suggestion is enough to aim a suggestion:
        a person decides on every part before anything is made of it.

        **Its one tolerance is the detector's own witness tolerance.** That is the number `detect()`
        used to decide which dimensions run end to end, and the suggester asks the same kind of
        question three times: whether two strokes are one dimension drawn twice, which chained
        dimensions share the lowest row, and whether a countertop's ends meet its cabinets'. A
        second number for the same question could disagree with the first. The call to `detect()`
        is the one `_associate_page` makes, on the same strokes under the same settings, so where
        both run they find the same lines.
        """
        settings = self._association
        if settings is None:
            return None
        counts = {"cabinets": 0, "countertops": 0, "with_code": 0, "nested_views": 0}
        views = list(
            session.scalars(
                select(DrawingView)
                .where(DrawingView.page_id == page.id)
                .order_by(DrawingView.tag, DrawingView.id)
            )
        )
        if not views:
            return counts

        with traced(
            "extraction.page.part_proposals",
            document_version_id=str(page.document_version_id),
            page_index=page.index,
            extractor_version=PROPOSER_VERSION,
        ):
            outlines = [
                ViewOutline(
                    view_id=view.id,
                    region=Polygon(
                        # Kept as text by `record_panel_view`, so read back exactly.
                        points=tuple(
                            StoredPoint(Decimal(x), Decimal(y))
                            for x, y in cast(list[list[str]], view.region["points"])
                        ),
                        space="stored",
                        document_version_id=page.document_version_id,
                        page=page.index,
                    ),
                    vendor=_view_role(session, view) == ViewRole.SHOP.value,
                )
                for view in views
            ]
            detected = detect(
                lines,
                witness_tolerance=settings.witness_tolerance,
                minimum_span=settings.minimum_span,
                straightness=settings.straightness,
                crossing_margin=settings.crossing_margin,
            )
            texts = [
                PrintedText(candidate_id=row.id, text=row.raw_text, extent=item.extent)
                for items, rows in readings
                for item, row in zip(items, rows, strict=True)
            ]
            proposed = propose_parts(
                outlines, detected, texts, edge_tolerance=settings.witness_tolerance
            )
            for part in proposed.parts:
                record_part_proposal(
                    session,
                    drawing_view_id=part.view_id,
                    kind=part.kind,
                    extent=[(point.x, point.y) for point in part.extent.points],
                    defining_line=(
                        (part.defining_line.start.x, part.defining_line.start.y),
                        (part.defining_line.end.x, part.defining_line.end.y),
                    ),
                    code_as_printed=None if part.code is None else part.code.text,
                    code_candidate_id=None if part.code is None else part.code.candidate_id,
                    reason=part.reason,
                    source=PROPOSER_SOURCE,
                    source_version=PROPOSER_VERSION,
                )
                counts["countertops" if part.kind is PartKind.COUNTERTOP else "cabinets"] += 1
                if part.code is not None:
                    counts["with_code"] += 1
            counts["nested_views"] = len(proposed.nested)
        return counts

    def _read_page_markup(
        self,
        session: Session,
        *,
        version_id: UUID,
        data: bytes,
        page: Page,
        task_run_id: UUID,
    ) -> tuple[list[ObservationCandidate], PageLayers | None]:
        """Record one page's reviewer annotations as candidates, and return them with the layers.

        The layers come back because the association step needs them: the notes it will attach and,
        when the thresholds are configured, the vendor line-work to attach them to. `None` means the
        page's annotations could not be read at all, which is not the same as a page with none.

        **Reads only the markup half**, through `read_markup_layer`. The other half of that module
        classifies the vendor's path geometry and cannot be called without three empirical lengths
        that #179 gates on real drawings — and a pipeline is the last place a threshold should
        acquire a value. Reading a `/FreeText` needs none: the text and the rectangle are dictionary
        values.

        **Its own extraction run**, for the reason the OCR route gives, with the same `dpi` in the
        configuration because the same dpi decides the stored geometry these rows carry.

        **A page whose annotations will not parse is recorded, not skipped.** Returning zero would be
        indistinguishable from a page nobody had annotated, which is the failure `#491` exists to
        stop for the routes above.
        """
        try:
            if self._association is None:
                layers = read_markup_layer(
                    data,
                    page.index,
                    document_version_id=version_id,
                    dpi=self._dpi,
                )
            else:
                # **The full read, because the association step needs the vendor's line-work too.**
                # `read_markup_layer` skips it deliberately: it needs no thresholds and the pipeline
                # must not invent any. When a deployment has stated them, there is nothing to invent
                # and the geometry is exactly what a reading has to be attached to.
                layers = read_annotation_layers(
                    data,
                    page.index,
                    document_version_id=version_id,
                    dpi=self._dpi,
                    line_minimum_pt=self._association.line_minimum_pt,
                    glyph_maximum_pt=self._association.glyph_maximum_pt,
                    glyph_gap_pt=self._association.glyph_gap_pt,
                    fraction_bar=self._association.fraction_bar,
                )
        except UnreadablePdf as error:
            # The run is opened here rather than before the read, because a run is a record of work
            # and most pages have no markup to do any on. A *failed* attempt is work: the failure
            # points at the run that attempted it, and attributing it to the vector or OCR run would
            # blame the wrong route for it.
            failed_run = open_extraction_run(
                session,
                task_run_id=task_run_id,
                extractor=MARKUP_EXTRACTOR,
                extractor_version=MARKUP_EXTRACTOR_VERSION,
                config_hash=f"dpi={self._dpi}",
                dpi=self._dpi,
            )
            record_unreadable_page(
                session,
                extraction_run_id=failed_run.id,
                document_version_id=version_id,
                page_index=page.index,
                error=error,
            )
            return [], None
        if not layers.markup:
            # Nothing to record and nothing wrong: most sheets in most sets carry no markup at all,
            # and a run opened for a page with no notes would be a row claiming work that did not
            # happen.
            return [], layers

        with traced(
            "extraction.page.markup",
            document_version_id=str(version_id),
            page_index=page.index,
            extractor_version=MARKUP_EXTRACTOR_VERSION,
        ):
            markup_run = open_extraction_run(
                session,
                task_run_id=task_run_id,
                extractor=MARKUP_EXTRACTOR,
                extractor_version=MARKUP_EXTRACTOR_VERSION,
                config_hash=f"dpi={self._dpi}",
                dpi=self._dpi,
            )
            return (
                record_markup_candidates(
                    session,
                    layers.markup,
                    document_version_id=version_id,
                    page_id=page.id,
                    extraction_run_id=markup_run.id,
                    page_index=page.index,
                    flush=False,
                ),
                layers,
            )

    def _read_page_cad_text(
        self,
        session: Session,
        *,
        version_id: UUID,
        page: Page,
        task_run_id: UUID,
        layers: PageLayers | None,
    ) -> list[ObservationCandidate]:
        """Record the vendor's exact CAD text notes as candidates, under a route of their own.

        **Exact, like the markup route, and for the same reason:** the string and its rectangle are
        dictionary values, so there is nothing between the file and the reading that could misread
        it. The writer is the markup route's, because what it does with a note — parse the value it
        states, keep its box, never infer a type — is the same. The run is not: a reviewer's
        correction and the vendor's own string must stay distinguishable in every row.

        No run is opened for a page without such notes, which is every page printed to PDF rather
        than exported from CAD: a run is a record of work, and there was none.
        """
        if layers is None or not layers.vendor_text:
            return []
        with traced(
            "extraction.page.cad_text",
            document_version_id=str(version_id),
            page_index=page.index,
            extractor_version=CAD_TEXT_EXTRACTOR_VERSION,
        ):
            cad_run = open_extraction_run(
                session,
                task_run_id=task_run_id,
                extractor=CAD_TEXT_EXTRACTOR,
                extractor_version=CAD_TEXT_EXTRACTOR_VERSION,
                config_hash=f"dpi={self._dpi}",
                dpi=self._dpi,
            )
            return record_markup_candidates(
                session,
                layers.vendor_text,
                document_version_id=version_id,
                page_id=page.id,
                extraction_run_id=cad_run.id,
                page_index=page.index,
                flush=False,
            )

    def _read_page_stamp_text(
        self,
        session: Session,
        *,
        version_id: UUID,
        data: bytes,
        page: Page,
        task_run_id: UUID,
        layers: PageLayers | None,
    ) -> tuple[tuple[TextItem, ...], tuple[SetAsideLabel, ...], list[ObservationCandidate]]:
        """Record the font text inside the page's pasted drawings, under a route of its own.

        Returns the runs read, the runs set aside unread (#738), and the rows written.

        Read by `extraction/stamp_text.py` from a copy of the page holding only its stamps — never
        the reviewer's markup, never the page's own content — and written by the vector route's own
        writer, because a run of text is a run of text wherever it was put.

        **No run for a page with nothing to read.** A page with no stamps, or stamps holding only
        drawn strokes (most of `AI_Set_2`), opens none. A page whose stamps could not be prepared is
        recorded as unreadable under this route, not skipped, as every other route does (#491).
        """
        if layers is None or not layers.vendor_stamps:
            return (), (), []
        try:
            stamp = read_stamp_text(data, page.index, document_version_id=version_id, dpi=self._dpi)
        except UnreadablePdf as error:
            failed_run = open_extraction_run(
                session,
                task_run_id=task_run_id,
                extractor=STAMP_TEXT_EXTRACTOR,
                extractor_version=STAMP_TEXT_EXTRACTOR_VERSION,
                config_hash=f"dpi={self._dpi}",
                dpi=self._dpi,
            )
            record_unreadable_page(
                session,
                extraction_run_id=failed_run.id,
                document_version_id=version_id,
                page_index=page.index,
                error=error,
            )
            return (), (), []
        stacked = stamp.contents.set_aside
        if not stamp.contents.texts:
            return (), stacked, []
        with traced(
            "extraction.page.stamp_text",
            document_version_id=str(version_id),
            page_index=page.index,
            extractor_version=STAMP_TEXT_EXTRACTOR_VERSION,
        ):
            stamp_run = open_extraction_run(
                session,
                task_run_id=task_run_id,
                extractor=STAMP_TEXT_EXTRACTOR,
                extractor_version=STAMP_TEXT_EXTRACTOR_VERSION,
                config_hash=f"dpi={self._dpi}",
                dpi=self._dpi,
            )
            rows = record_candidates(
                session,
                stamp.contents.texts,
                document_version_id=version_id,
                page_id=page.id,
                extraction_run_id=stamp_run.id,
                page_index=page.index,
                flush=False,
            )
            return stamp.contents.texts, stacked, rows

    def _read_page_by_ocr(
        self,
        session: Session,
        *,
        version_id: UUID,
        data: bytes,
        page: Page,
        task_run_id: UUID,
    ) -> tuple[tuple[OcrItem, ...], list[ObservationCandidate], tuple[OcrItem, ...]]:
        """Render one page and read it with the OCR engine, recording what it found.

        Returns the readings, their rows, and the OCR items whose text could not be a reading
        (`could_be_a_reading`, #703): not recorded, and returned so their boxes still reach the
        vision readers and their count still reaches the page's result.

        **A separate extraction run, not the vector one.** A candidate points at a run to say what
        read it, and `open_extraction_run` keys a run on extractor, version and config — so OCR
        readings land under their own run and a reviewer can tell a scanned reading from a vector one
        without inspecting the text.

        **The dpi is part of that run's identity**, because it is part of the reading: the same page
        at 150 and at 300 gives the engine different pixels and can give different text.

        **Refuses rather than skips when the engine is unavailable.** A missing optional dependency is
        a configuration fact, not a fact about the drawing — the same distinction `_fetch` draws for a
        storage failure. Skipping would put the pipeline back where it started, reporting a scanned
        page as read and empty.
        """
        engine = self._ocr()
        rendered = render_page(
            data,
            page.index,
            document_version_id=version_id,
            page_content_hash=page.content_hash,
            dpi=self._dpi,
            # The rasteriser's own ceiling, passed explicitly because it has no default: a page that
            # would not fit in memory must raise `PageTooLarge` rather than be silently shrunk.
            maximum_pixels=MAXIMUM_RENDER_PIXELS,
            # OCR must not read the reviewer's notes as the drawing's text; they have their own exact
            # lane (#742).
            vendor_only=True,
        )
        with traced(
            "extraction.page.ocr",
            document_version_id=str(version_id),
            page_index=page.index,
            extractor_version=engine.version,
        ):
            read = read_page(rendered, engine=engine)
            readings, fragments = _split_ocr_readings(read.items)
            ocr_run = open_extraction_run(
                session,
                task_run_id=task_run_id,
                extractor=engine.name,
                extractor_version=engine.version,
                # `layers=vendor` because the pixels changed (#742): a run from before it read the
                # reviewer's notes too, and must not be reused as though it had not.
                # `fragments=unrecorded` because what is recorded changed (#703): a run from before
                # it holds rows for text that could not be a reading.
                config_hash=f"dpi={self._dpi};layers=vendor;{OCR_FRAGMENTS_CONFIG}",
                dpi=self._dpi,
            )
            rows = record_ocr_candidates(
                session,
                readings,
                document_version_id=version_id,
                page_id=page.id,
                extraction_run_id=ocr_run.id,
                page_index=page.index,
                flush=False,
            )
            return readings, self._ordered_ocr_rows(readings, rows), fragments

    def _read_page_by_localized_ocr(
        self,
        session: Session,
        *,
        version_id: UUID,
        data: bytes,
        page: Page,
        task_run_id: UUID,
        layers: PageLayers,
        regions: Sequence[OutlinedTextRegion],
    ) -> tuple[tuple[OcrItem, ...], list[ObservationCandidate], tuple[OcrItem, ...]]:
        """Read vendor outlined-text regions as bounded, vendor-only high-DPI crops.

        Returns what `_read_page_by_ocr` returns, and splits readings from fragments the same way.

        ``layers`` was read with the deployment's explicit geometry thresholds. ``region_crop``
        independently strips non-stamp annotations from each rendered crop, so a reviewer
        annotation cannot leak into OCR even when the original upload carries one.

        Candidate polygons remain in the shared reader DPI frame. The extraction run configuration
        separately records the 600-DPI crop pixels RapidOCR actually saw, avoiding the provenance
        error of claiming that a 300-DPI full-page render produced a crop-local reading.
        """
        if page.media_box is None or page.crop_box is None:
            # A manifest row without transform metadata cannot carry a crop reading back to page
            # coordinates. Full-page OCR is the honest fallback rather than a made-up polygon.
            return self._read_page_by_ocr(
                session,
                version_id=version_id,
                data=data,
                page=page,
                task_run_id=task_run_id,
            )
        media = tuple(Decimal(value) for value in page.media_box)
        crop = tuple(Decimal(value) for value in page.crop_box)
        if len(media) != 4 or len(crop) != 4:
            return self._read_page_by_ocr(
                session,
                version_id=version_id,
                data=data,
                page=page,
                task_run_id=task_run_id,
            )
        transform = PageTransform(
            dpi=self._dpi,
            rotation=page.rotation,
            media_box=(media[0], media[1], media[2], media[3]),
            crop_box=(crop[0], crop[1], crop[2], crop[3]),
        )
        engine = self._ocr()
        with traced(
            "extraction.page.localized_ocr",
            document_version_id=str(version_id),
            page_index=page.index,
            extractor_version=engine.version,
        ):
            items = read_localized_vendor_regions(
                data,
                page_index=page.index,
                regions=regions,
                engine=engine,
                page_transform=transform,
                margin_pt=(
                    self._localized_ocr.crop_margin_pt
                    if self._localized_ocr is not None
                    else CROP_CONTEXT_MARGIN_PT
                ),
            )
            readings, fragments = _split_ocr_readings(items)
            ocr_run = open_extraction_run(
                session,
                task_run_id=task_run_id,
                extractor=engine.name,
                extractor_version=engine.version,
                config_hash=(
                    f"dpi={self._dpi};route=localized_vendor_regions;crop_dpi={VISION_CROP_DPI};"
                    f"{self._localized_ocr.config_hash if self._localized_ocr is not None else ''}"
                    f";{OCR_FRAGMENTS_CONFIG}"
                ),
                # Candidate polygons use this full-page frame. The actual pixel resolution used by
                # OCR is separately retained in config_hash above.
                dpi=self._dpi,
            )
            rows = record_ocr_candidates(
                session,
                readings,
                document_version_id=version_id,
                page_id=page.id,
                extraction_run_id=ocr_run.id,
                page_index=page.index,
                flush=False,
            )
            return readings, self._ordered_ocr_rows(readings, rows), fragments

    def _read_page_by_glyphs(
        self,
        session: Session,
        *,
        version_id: UUID,
        page: Page,
        task_run_id: UUID,
        layers: PageLayers,
        regions: Sequence[OutlinedTextRegion],
    ) -> tuple[
        list[ObservationCandidate],
        tuple[tuple[_LocatedOcrReading, ...], list[ObservationCandidate]],
        Counter[str],
    ]:
        """Read the planned regions' labels from their shapes; record each reading; count the rest.

        **Confirmed only by a second witness** — the admin's decision (#756 D2, 2026-10-01). A
        mislabelled template would repeat its mistake on every sheet, and a witness that did not
        use the templates is what catches it. So a reading is never confirmed alone: the label's
        own millimetres agreeing with its inches (the dual lane, set here), or another reader
        agreeing on the same box (the page asks the vision readers about each reading's box, and
        cross-route corroboration decides). Otherwise it is a value a reviewer confirms, pre-filled.
        Its rows are not given to the bounded agent.

        **A stacked fraction is flagged** (#756 D3): read and pre-filled, never confirmed by any
        number of readers (#726).

        **Its own extraction run**, keyed on the template set's hash, so a reading made with one set
        is never mistaken for a reading made with another. A re-run finds its rows and reads nothing.

        Returns the rows, the pair `associate` takes (empty on a re-run, whose rows were associated
        when they were written), and why each label that abstained did.
        """
        route = self._glyph_route
        assert route is not None
        run = open_extraction_run(
            session,
            task_run_id=task_run_id,
            extractor=GLYPH_EXTRACTOR,
            extractor_version=route.templates.set_hash[:12],
            config_hash=f"dpi={self._dpi};{route.config_hash}",
            dpi=self._dpi,
        )
        with session.no_autoflush:
            existing = list(
                session.execute(
                    select(ObservationCandidate).where(
                        ObservationCandidate.extraction_run_id == run.id,
                        ObservationCandidate.page_id == page.id,
                    )
                ).scalars()
            )
        if existing:
            return existing, ((), []), Counter()
        if page.media_box is None or page.crop_box is None:
            return (
                [],
                ((), []),
                Counter({"the page has no recorded transform, so no reading could be placed": 1}),
            )
        media = tuple(Decimal(value) for value in page.media_box)
        crop = tuple(Decimal(value) for value in page.crop_box)
        transform = PageTransform(
            dpi=self._dpi,
            rotation=page.rotation,
            media_box=(media[0], media[1], media[2], media[3]),
            crop_box=(crop[0], crop[1], crop[2], crop[3]),
        )
        readings, abstentions = read_page_labels(regions, layers.glyph_paths, route)
        rows: list[ObservationCandidate] = []
        items: list[_LocatedOcrReading] = []
        for reading in readings:
            try:
                extent, corners = page_box_polygon(reading.box, transform, version_id, page.index)
            except (TypeError, ValueError):
                abstentions["the label lies outside the visible page"] += 1
                continue
            row = ObservationCandidate(
                document_version_id=version_id,
                page_id=page.id,
                extraction_run_id=run.id,
                raw_text=reading.text,
                value_numerator=reading.value.exact.numerator,
                value_denominator=reading.value.exact.denominator,
                unit=reading.value.unit.value,
                unit_guess=reading.value.unit.value,
                semantic_guess=None,
                polygon=[[corner.x, corner.y] for corner in corners],
                coordinate_space="image",
                confidence=None,
                ambiguity_flags=[STACKED_FRACTION_FLAG] if reading.stacked else [],
            )
            # Set before the insert: the table is append-only (see `record_candidates`).
            row.corroboration_status, row.corroboration_lane = dual_unit_lane(
                row, run=run, page_index=page.index
            )
            session.add(row)
            rows.append(row)
            items.append(_LocatedOcrReading(extent, reading.rotation_degrees))
        return rows, (tuple(items), rows), abstentions

    def _fraction_parts_config_hash(
        self, drawing: PieceDrawing, engine: OcrEngine, second: _VisionReader
    ) -> str:
        """The fraction-parts run's identity: every setting its readings depend on.

        The resolution, how a piece is drawn and the second reader's model are kept readable; the
        rest — both readers' names and versions, the digits prompt, the detector's settings — goes
        into a fingerprint beside them, because a run's identity column holds 200 characters and
        all of it written out runs past that, as the reading agent's did (`ReadingAgentSettings`).
        """
        everything = (
            f"dpi={self._dpi};{drawing.config_hash};engine={engine.name}/{engine.version}"
            f";second={second.config.extractor}/{second.config.model_id}"
            f";prompt={DIGITS_PROMPT_ID}"
            + (
                ""
                if self._association is None
                else f";fraction_bar={self._association.fraction_bar.config_hash}"
            )
        )
        digest = hashlib.sha256(everything.encode()).hexdigest()[:16]
        return (
            f"dpi={self._dpi};{drawing.config_hash};second={second.config.model_id};"
            f"readers={digest}"
        )

    def _read_page_by_fraction_parts(
        self,
        session: Session,
        *,
        version_id: UUID,
        page: Page,
        task_run_id: UUID,
        fractions: Sequence[StackedFraction],
    ) -> tuple[list[ObservationCandidate], Counter[str], int]:
        """Read each laid-out stacked label piece by piece; record each reading; count the rest.

        `extraction/fraction_parts.py` draws the whole number, numerator and denominator from their
        own paths, reads each with the OCR engine, and puts the value together in code. A label set
        in text has no paths and no layout, and is not looked at.

        **A row only where two readers agree on every piece (#865).** The second reader is the vision
        gate reader, asked for each piece's digits once the OCR engine has read them all
        (`_PieceReads`); a piece they read differently, or that it does not read, leaves no row and
        a counted reason, and a person types the value. Its calls are on the drawing set's budget:
        once that is spent it is asked nothing more, and a label it has not seconded gets no row.

        **Every row carries `STACKED_FRACTION_FLAG`** (#726): however exactly it was read, a stacked
        fraction goes to a person. `corroborate` keeps any group it is in a raw candidate with no
        lane, automatic typing refuses it, and the form shows it filled in for a person to tick. Its
        rows are not handed to the vision readers or the agent.

        **Its own extraction run**, keyed on how the pieces are drawn, which engine and which second
        reader read them, the second reader's prompt, and the detector's settings, so a reading made
        under other numbers is another run. A re-run finds its rows and reads nothing; a page where
        no label was read has no rows to find, so a re-run reads it again and pays the second reader
        again, under the same budget. A page with no laid-out label opens no run.

        Returns the rows, why each label that was not read was not, and how many calls the second
        reader made.
        """
        drawing = self._fraction_parts
        assert drawing is not None
        refusals: Counter[str] = Counter()
        laid_out = [fraction for fraction in fractions if fraction.layout is not None]
        if not laid_out:
            return [], refusals, 0
        engine = self._ocr()
        second = next(
            reader
            for reader in self._vision_readers
            if reader.config.extractor == self._vision_gate
        )
        run = open_extraction_run(
            session,
            task_run_id=task_run_id,
            extractor=FRACTION_PARTS_EXTRACTOR,
            extractor_version=FRACTION_PARTS_VERSION,
            config_hash=self._fraction_parts_config_hash(drawing, engine, second),
            dpi=self._dpi,
        )
        with session.no_autoflush:
            existing = list(
                session.execute(
                    select(ObservationCandidate).where(
                        ObservationCandidate.extraction_run_id == run.id,
                        ObservationCandidate.page_id == page.id,
                    )
                ).scalars()
            )
        if existing:
            return existing, refusals, 0
        if page.media_box is None or page.crop_box is None:
            refusals["the page has no recorded transform, so no reading could be placed"] += len(
                laid_out
            )
            return [], refusals, 0
        media = tuple(Decimal(value) for value in page.media_box)
        crop = tuple(Decimal(value) for value in page.crop_box)
        transform = PageTransform(
            dpi=self._dpi,
            rotation=page.rotation,
            media_box=(media[0], media[1], media[2], media[3]),
            crop_box=(crop[0], crop[1], crop[2], crop[3]),
        )
        rows: list[ObservationCandidate] = []
        reads = _PieceReads(
            session=session, run_id=run.id, page_index=page.index, reader=second, meter=self._meter
        )
        with traced(
            "extraction.page.fraction_parts",
            document_version_id=str(version_id),
            page_index=page.index,
            extractor_version=FRACTION_PARTS_VERSION,
        ):
            for fraction in laid_out:
                result = read_fraction_parts(fraction, engine=engine, drawing=drawing, second=reads)
                if isinstance(result, FractionPartsRefusal):
                    refusals[result.reason] += 1
                    continue
                try:
                    _extent, corners = page_box_polygon(
                        result.box, transform, version_id, page.index
                    )
                except (TypeError, ValueError):
                    refusals["the label lies outside the visible page"] += 1
                    continue
                row = ObservationCandidate(
                    document_version_id=version_id,
                    page_id=page.id,
                    extraction_run_id=run.id,
                    raw_text=result.text,
                    value_numerator=result.value.exact.numerator,
                    value_denominator=result.value.exact.denominator,
                    unit=result.value.unit.value,
                    unit_guess=result.value.unit.value,
                    semantic_guess=None,
                    polygon=[[corner.x, corner.y] for corner in corners],
                    coordinate_space="image",
                    # Put together in code from the pieces' readings: it has no confidence of its own.
                    confidence=None,
                    ambiguity_flags=[STACKED_FRACTION_FLAG],
                )
                session.add(row)
                rows.append(row)
        return rows, refusals, reads.invocations

    def _read_page_by_vision(
        self,
        session: Session,
        *,
        version_id: UUID,
        data: bytes,
        page: Page,
        task_run_id: UUID,
        regions: Sequence[_VisionRegion],
        stacked_fractions: Sequence[StackedFraction],
    ) -> tuple[list[ObservationCandidate], int, list[str], tuple[_VisionAssociationLink, ...], int]:
        """Read each region with every configured vision reader.

        **A region is a recorded reading's box or an OCR box whose text was not a reading** (#703).
        Both are read the same way; see `_VisionRegion` for why the second kind stays on the list.

        **Each request says whether its crop shows a stacked fraction** (#735), found on the vendor's
        layer by `extraction/glyph_bands.py` when the page's geometry was read. The validator then
        abstains on any reading of that crop: a stacked fraction always goes to a reviewer (#726).
        Where the geometry was not read — no association settings — `stacked_fractions` is empty and
        nothing is flagged; so is a label the vendor drew as font text rather than paths, which the
        geometry reader does not see (#738).

        This route is additive: a model output is another raw candidate, never a fact, and a failed
        model call leaves the fixed readers' candidates untouched. The crop bound and one-call-per
        reader-per-region loop live here rather than in the prompt.

        The returned association links are deliberately source-backed. A vision reader reports a
        string, not a trusted page location or text orientation. When association later considers a
        model row, it may reuse only the fixed-reader region that caused this exact crop to be sent.
        Before #698 the vision rows were left out of association entirely, so a real run could have
        model-read numbers and detected line-work but zero `observation_associations` rows.
        """
        if not regions or self._store is None:
            return [], 0, [], (), 0

        try:
            rendered = render_page(
                data,
                page.index,
                document_version_id=version_id,
                page_content_hash=page.content_hash,
                dpi=self._dpi,
                maximum_pixels=MAXIMUM_RENDER_PIXELS,
                # **Vendor's drawing only (#742).** A reviewer's note painted into a crop is a number
                # a model can return as the vendor's, and two readers doing so would seal it as one.
                vendor_only=True,
            )
        except (PageTooLarge, UnreadablePdf, ValueError) as error:
            return [], 0, [f"page {page.index}: {error}"], (), 0

        rows: list[ObservationCandidate] = []
        association_links: list[_VisionAssociationLink] = []
        invocations = 0
        refusals: list[str] = []
        gate = self._vision_gate
        # The gate first, so the others know where it read a value; otherwise configured order.
        readers = sorted(self._vision_readers, key=lambda reader: reader.config.extractor != gate)
        gate_valued: set[tuple[tuple[int, ...], ...]] = set()
        held_back = 0

        def place(polygon: Sequence[Sequence[int]]) -> tuple[tuple[int, ...], ...]:
            return tuple(tuple(int(value) for value in point) for point in polygon)

        for reader in readers:
            gated = gate is not None and reader.config.extractor != gate
            run = open_extraction_run(
                session,
                task_run_id=task_run_id,
                extractor=reader.config.extractor,
                extractor_version=reader.config.model_id,
                config_hash=(
                    f"dpi={self._dpi};route=vision;layers=vendor;"
                    f"crop_margin_pt={VISION_CROP_CONTEXT_MARGIN_PT};"
                    f"context_bound_pt={VISION_CONTEXT_BOUND_PT}"
                    # Which readings are accepted depends on it, so a run under other numbers is
                    # another run, not this one reused.
                    + (
                        ""
                        if self._association is None
                        else f";fraction_bar={self._association.fraction_bar.config_hash}"
                    )
                    # A gated reader read only what the gate found a value in: another run.
                    + (f";gate={gate}" if gated else "")
                ),
                dpi=self._dpi,
            )
            with session.no_autoflush:
                existing = list(
                    session.execute(
                        select(ObservationCandidate).where(
                            ObservationCandidate.extraction_run_id == run.id,
                            ObservationCandidate.page_id == page.id,
                        )
                    ).scalars()
                )
            if existing:
                rows.extend(existing)
                if not gated and gate is not None:
                    gate_valued = {
                        place(row.polygon) for row in existing if row.value_numerator is not None
                    }
                continue

            targets = regions
            if gated:
                targets = [region for region in regions if place(region.polygon) in gate_valued]
                held_back += len(regions) - len(targets)
            for region in targets:
                cropped = self._vision_crop(rendered, region)
                if cropped is None:
                    refusals.append(
                        f"page {page.index}: a candidate's polygon could not be cropped for vision"
                    )
                    continue
                crop, crop_box = cropped
                pre_call_refusal = _vision_pre_call_refusal(crop_box, stacked_fractions)
                if pre_call_refusal is not None:
                    refusals.append(
                        f"page {page.index}: {reader.config.extractor}: "
                        f"{pre_call_refusal}: crop skipped before model call"
                    )
                    continue
                if self._meter is not None and self._meter.reached:
                    refusals.append(
                        f"page {page.index}: {reader.config.extractor}: {self._meter.reason}"
                    )
                    continue
                request_candidate_id = uuid4()
                recorder = _BufferedVisionRecorder(
                    session=session,
                    extraction_run_id=run.id,
                    request_candidate_id=request_candidate_id,
                    meter=self._meter,
                )
                request = NovaRequest(
                    candidate_id=str(request_candidate_id),
                    page=page.index,
                    crop=crop,
                    image_format="png",
                    context=AssembledContext(nearby_text=(), nearby_geometry=()),
                    bound_pt=VISION_CONTEXT_BOUND_PT,
                    # Both still computed, though `_vision_pre_call_refusal` has already kept every
                    # crop that shows one from this call: the validator's guards stay behind it.
                    stacked_label=crop_shows_a_stacked_fraction(crop_box, stacked_fractions),
                    stacked_layouts=stacked_layouts_shown(crop_box, stacked_fractions),
                )
                try:
                    candidate = reader.extract(request, recorder)
                except NovaAdapterError as error:
                    invocations += recorder.persist(candidate_id=None, flush=False)
                    refusals.append(f"page {page.index}: {reader.config.extractor}: {error}")
                    continue
                row = self._record_vision_candidate(
                    session,
                    candidate,
                    document_version_id=version_id,
                    page_id=page.id,
                    extraction_run_id=run.id,
                    page_polygon=region.polygon,
                    flush=False,
                )
                invocations += recorder.persist(candidate_id=row.id, flush=False)
                rows.append(row)
                if gate is not None and not gated and row.value_numerator is not None:
                    gate_valued.add(place(region.polygon))
                association_links.append(
                    _VisionAssociationLink(row=row, source_candidate_id=region.id)
                )
        return rows, invocations, refusals, tuple(association_links), held_back

    def _vision_crop(
        self, rendered: RenderedPage, candidate: _VisionRegion
    ) -> tuple[bytes, tuple[int, int, int, int]] | None:
        """The crop a vision reader is shown, and the page pixels it was cut from.

        The pixels come back with it because what the crop *shows* is what decides whether a
        reading of it may be accepted (#735), and `crop_pixel_box` is the one computation of that
        rectangle — the same one `generate_crop` cuts by.
        """
        if self._store is None:
            return None
        polygon = stored_polygon(candidate, rendered)
        if polygon is None:
            return None
        spec = CropSpec(
            polygon=polygon,
            context_margin_pt=VISION_CROP_CONTEXT_MARGIN_PT,
            dpi=self._dpi,
        )
        result = generate_crop(rendered, spec, self._store)
        if result.status is not CropStatus.AVAILABLE or result.artifact is None:
            return None
        with self._store.get(result.artifact.key) as stored:
            return stored.read(), crop_pixel_box(rendered, spec)

    @staticmethod
    def _record_vision_candidate(
        session: Session,
        candidate: DomainCandidate,
        *,
        document_version_id: UUID,
        page_id: UUID,
        extraction_run_id: UUID,
        page_polygon: list[list[int]],
        flush: bool = True,
    ) -> ObservationCandidate:
        try:
            row_id = UUID(candidate.candidate_id)
        except ValueError as error:
            raise ValueError("vision candidate ids must be UUID strings") from error

        with session.no_autoflush:
            existing = session.get(ObservationCandidate, row_id)
        if existing is not None:
            return existing

        flags = list(candidate.ambiguity_flags)
        measurement, flag = _vision_candidate_value(candidate.raw_text)
        if flag is not None:
            flags.append(flag)

        row = ObservationCandidate(
            id=row_id,
            document_version_id=document_version_id,
            page_id=page_id,
            extraction_run_id=extraction_run_id,
            raw_text=candidate.raw_text,
            value_numerator=None if measurement is None else measurement.exact.numerator,
            value_denominator=None if measurement is None else measurement.exact.denominator,
            unit=None if measurement is None else measurement.unit.value,
            unit_guess=None if candidate.unit_guess is None else candidate.unit_guess.value,
            semantic_guess=None,
            polygon=[list(point) for point in page_polygon],
            coordinate_space="image",
            confidence=candidate.confidence,
            ambiguity_flags=flags,
        )
        session.add(row)
        if flush:
            session.flush()
        return row

    def _ocr(self) -> OcrEngine:
        """The OCR engine, built once and only when a page actually needs it.

        Constructing `RapidOcrEngine` loads ONNX models, which is slow and pointless for a package of
        vector drawings. Injected rather than imported at the call site so a test can pass a stub and
        the suite never loads a model it is not testing.
        """
        if self._ocr_engine is None:
            self._ocr_engine = RapidOcrEngine()
        return self._ocr_engine

    def match(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        """Propose which architectural item is which shop item, and write the proposals down.

        `retrieval/matching.py` is finished and tested and has never had a caller: no
        `match_candidates` row has ever been written by anything but a test. This runs the real exact
        lane over the revision's own drawing items and persists what it proposes.

        **Per identifier kind, not per item.** An item can carry a vendor code and a mark at once,
        and the model keeps both deliberately because they disagree often enough that keeping one
        would lose the disagreement. Collapsing them here would re-make that choice by guess, so each
        kind is matched against its own kind and the lane is recorded on every row.

        **A proposal, and nothing more.** `match_candidates` has no approval column by design, and
        this writes nothing else — no approved match, no verdict operand. Approval is a separate
        insert that names who decided, and nothing here decides.

        **Only parts a person confirmed and has not taken back (#882).** Writing a candidate needs
        two `drawing_items` rows, and outside tests an item exists only once a person confirms a
        suggested part (`workflow/parts.py`). A withdrawn or corrected part keeps its row, so both
        queries this reads keep to `live_part_item_ids`. With none, it returns an honest zero with
        the reason, naming how many drawings were found and how many roles are confirmed.
        """
        role_summary = _match_role_summary(session, package_revision_id)
        items = _matchable_items(session, package_revision_id)
        if not items:
            if role_summary.total_items:
                missing = _missing_role_names(role_summary.roles)
                return {
                    "implemented": True,
                    "ran": True,
                    "items": role_summary.total_items,
                    "candidates": 0,
                    "reason": (
                        "matching needs confirmed architectural and shop views; missing: "
                        f"{missing}"
                    ),
                }
            views = revision_views(session, package_revision_id)
            confirmed = [entry.view.role for entry in views if entry.view.role]
            return {
                "implemented": True,
                "ran": True,
                "items": 0,
                "candidates": 0,
                "reason": (
                    "no confirmed parts exist for this revision: an item exists only once a person "
                    f"confirms a part of a drawing (#748). Drawings found: {len(views)}; roles "
                    f"confirmed by a reviewer: {confirmed.count('arch')} architect, "
                    f"{confirmed.count('shop')} vendor"
                ),
            }
        missing = _missing_role_names(role_summary.roles)
        if missing:
            return {
                "implemented": True,
                "ran": True,
                "items": len({item.item_id for item, _ in items}),
                "candidates": 0,
                "reason": (
                    "matching needs confirmed architectural and shop views; missing: " f"{missing}"
                ),
            }

        written = 0
        proposed = 0
        for kind in MATCHABLE_IDENTIFIER_KINDS:
            architectural = [
                item
                for item, item_kind in items
                if item_kind == kind and item.document_role is MatchDocumentRole.ARCH
            ]
            shop = [
                item
                for item, item_kind in items
                if item_kind == kind and item.document_role is MatchDocumentRole.SHOP
            ]
            if not architectural or not shop:
                continue
            for result in exact_match(architectural, shop):
                for candidate in result.candidates:
                    proposed += 1
                    # The pair is unique on (left, right, lane), and a re-run proposes the same pair
                    # again. Checked rather than caught: an IntegrityError would abort the whole
                    # transaction, taking the rows that were fine with it.
                    exists = session.execute(
                        select(MatchCandidateRow.id).where(
                            MatchCandidateRow.left_item_id == candidate.left_item_id,
                            MatchCandidateRow.right_item_id == candidate.right_item_id,
                            MatchCandidateRow.lane == candidate.lane.value,
                        )
                    ).first()
                    if exists is not None:
                        continue
                    session.add(
                        MatchCandidateRow(
                            left_item_id=candidate.left_item_id,
                            right_item_id=candidate.right_item_id,
                            lane=candidate.lane.value,
                            score=candidate.score,
                        )
                    )
                    written += 1

        session.flush()
        return {
            "implemented": True,
            "ran": True,
            "items": len({item.item_id for item, _ in items}),
            "proposed": proposed,
            "candidates": written,
        }

    def validate_evidence(
        self, session: Session, package_revision_id: UUID
    ) -> Mapping[str, object]:
        """Cut the picture of the region every candidate was read from.

        `evidence/crop.py` has been finished and tested for months with **no production caller**, so
        no reviewer has ever been shown the pixels behind a reading. A number in a table that a
        person cannot check against the sheet is a number they have to take on trust, which is the
        one thing this system is not supposed to ask for.

        **What this does not do by itself.** A crop is only a picture of a region and never assigns
        meaning.  When the deployment has explicitly enabled the exact-tag gate, this stage may
        then qualify a candidate whose vector tag is an approved vocabulary member on the very same
        associated dimension line.  Every other candidate stays untyped for reviewer confirmation.

        **The coordinate round trip is the delicate part, so it is exact rather than trusted.** A
        candidate's polygon is integer image pixels at the dpi the reader used. `CropSpec` wants
        stored space: the same points normalised to 0..1. Dividing by the rendered page's own pixel
        dimensions is the exact inverse of the multiplication `crop_pixel_box` performs, so the pixels
        that come back are the pixels the reader was looking at — provided the render matches the
        read. Rendering at `self._dpi`, the dpi the candidates were read at, is what makes that true,
        and `stored_polygon` refuses rather than guesses when a point falls outside the page.
        """
        if self._store is None:
            return {
                "implemented": True,
                "ran": False,
                "reason": "no artifact store is configured",
                "crops": 0,
            }

        keys = dict(_documents_for(session, package_revision_id))
        if not keys:
            return {"implemented": True, "ran": True, "candidates": 0, "crops": 0}

        rows = session.execute(
            select(ObservationCandidate, Page)
            .join(Page, Page.id == ObservationCandidate.page_id)
            .where(ObservationCandidate.document_version_id.in_(list(keys)))
            .order_by(Page.index, ObservationCandidate.created_at)
        ).all()
        if not rows:
            return {"implemented": True, "ran": True, "candidates": 0, "crops": 0}

        # **Which candidates already have a crop.** `evidence_artifacts` is append-only and unique on
        # (storage_key, sha256), and a crop's key is content-addressed — so re-running this stage on
        # the same page would regenerate byte-identical crops and collide. A redelivery is not a
        # second reading, exactly as `record_candidates` says of its own rows.
        existing = session.execute(
            select(
                EvidenceArtifact.candidate_id,
                EvidenceArtifact.storage_key,
                EvidenceArtifact.sha256,
            ).where(EvidenceArtifact.document_version_id.in_(list(keys)))
        ).all()
        already = {candidate_id for candidate_id, _, _ in existing}

        by_page: dict[UUID, list[ObservationCandidate]] = {}
        pages: dict[UUID, Page] = {}
        for candidate, page in rows:
            by_page.setdefault(page.id, []).append(candidate)
            pages[page.id] = page

        written = 0
        abstained: list[str] = []
        skipped = 0
        # **Seeded from the database, not empty.** Two candidates whose crops are byte-identical
        # share one content-addressed key, so only the first gets a row and the second is skipped
        # without one. On the next pass that second candidate is not in `already` — it has no
        # artifact — and regenerating its crop collides with the row its twin wrote. An in-memory set
        # cannot see that, because the collision is with work from a previous call.
        seen: set[tuple[str, str]] = {(key, digest) for _, key, digest in existing}
        documents: dict[UUID, bytes] = {}
        for page_id, candidates in by_page.items():
            page = pages[page_id]
            key = keys.get(page.document_version_id)
            if key is None:
                continue
            if page.render_failed:
                # The manifest already recorded that this page would not render. Asking the
                # rasteriser again would produce the same failure more slowly.
                abstained.append(f"page {page.index}: the manifest recorded a failed render")
                skipped += len(candidates)
                continue
            if page.document_version_id not in documents:
                documents[page.document_version_id] = _fetch(self._store, key)
            try:
                rendered = render_page(
                    documents[page.document_version_id],
                    page.index,
                    document_version_id=page.document_version_id,
                    page_content_hash=page.content_hash,
                    dpi=self._dpi,
                    maximum_pixels=MAXIMUM_RENDER_PIXELS,
                    # **Both layers.** A person reviewing evidence is shown what the sheet shows,
                    # reviewer notes included; only what a model or OCR reads is vendor-only (#742).
                    vendor_only=False,
                )
            except (PageTooLarge, UnreadablePdf, ValueError) as error:
                # One page that will not render, in a document whose others might. Every candidate on
                # it keeps its reading and goes without a picture, which is the honest pair.
                abstained.append(f"page {page.index}: {error}")
                skipped += len(candidates)
                continue

            for candidate in candidates:
                if candidate.id in already:
                    skipped += 1
                    continue
                polygon = stored_polygon(candidate, rendered)
                if polygon is None:
                    abstained.append(
                        f"page {page.index}: a candidate's polygon does not describe a region of "
                        "this rendering"
                    )
                    continue
                result = generate_crop(
                    rendered,
                    CropSpec(
                        polygon=polygon, context_margin_pt=CROP_CONTEXT_MARGIN_PT, dpi=self._dpi
                    ),
                    self._store,
                )
                if result.status is not CropStatus.AVAILABLE or result.artifact is None:
                    # `generate_crop` abstains rather than raising, and the reason is a sentence. It
                    # is carried through rather than counted, because "17 crops failed" tells a
                    # reviewer nothing they can act on.
                    abstained.append(f"page {page.index}: {result.reason}")
                    continue
                artifact = result.artifact
                identity = (artifact.key, artifact.sha256)
                if identity in seen:
                    # Two candidates whose crops are byte-identical — the same region read twice, or
                    # two identical labels in the same place. The image is stored once and belongs to
                    # whichever candidate reached it first; this one gets no artifact row, because
                    # the unique constraint on (storage_key, sha256) permits only one. That is a
                    # real limit rather than a tidy outcome: a reviewer following the second
                    # candidate finds no picture. Fixing it means letting two rows share one stored
                    # object, which is a schema decision and not this change's to make.
                    skipped += 1
                    continue
                seen.add(identity)
                session.add(
                    EvidenceArtifact(
                        candidate_id=candidate.id,
                        canonical_observation_id=None,
                        document_version_id=page.document_version_id,
                        page_id=page.id,
                        kind=EvidenceArtifactKind.CROP.value,
                        storage_key=artifact.key,
                        sha256=artifact.sha256,
                        media_type="image/png",
                        # The crop is pixels of a rendered page, so the space it is expressed in is
                        # the image's, not the normalised one the polygon was converted to.
                        coordinate_space="image",
                    )
                )
                written += 1

        session.flush()
        # This is the only automatic route across the semantic wall: an exact, vector-extracted
        # vocabulary tag must already be associated with the same dimension line. It runs after
        # crops exist so any resulting prefilled field remains inspectable by the reviewer.
        typing = (
            qualify_exact_tags_for_revision(
                session,
                package_revision_id=package_revision_id,
                settings=self._automatic_typing,
            )
            if self._automatic_typing is not None
            else None
        )
        return {
            "implemented": True,
            "ran": True,
            "candidates": len(rows),
            "crops": written,
            "already_had_one": skipped,
            "refused": len(abstained),
            "automatic_types_qualified": 0 if typing is None else len(typing.qualified),
            "semantic_typing_review_required": 0 if typing is None else len(typing.review_required),
            # Capped, because this payload is persisted as JSON and a document that fails to render
            # would otherwise put one sentence per candidate into it. The count above is exact; these
            # are the examples a person reads first.
            "refusals": abstained[:REPORTED_REFUSALS],
        }

    def generate_outputs(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        """Turn this revision's findings into a workbook and branded PDF somebody can be handed.

        The last stage, and the one that closes the loop: until now checks ran, findings were
        recorded, and nothing produced a file. `reports/spreadsheet.py` had been finished and tested
        for months with no production caller — the same gap #517 closed for crops and matching.

        **Live findings only.** `run_checks` supersedes previous runs before writing new ones, and a
        handoff containing both would show a reviewer two verdicts for one rule with nothing saying
        which is in force. The join filters on `superseded_at IS NULL`, which is the same question
        the findings list asks.

        **Built from stored rows, not from rebuilt engine values.** `StoredFinding` carries the
        reason in full: the stored trace renders operand values as display text, so reconstructing a
        `verdict.finding.Finding` would mean parsing presentation output back into exact arithmetic.
        Four columns the engine's value type carries are not in the database at all — the prose
        reason of a decision, the delta, the variant and the notes — and the workbook marks them
        `not recorded in the database` rather than leaving them blank.

        **Redline placement comes only from stored typed evidence.** The optional third artifact
        joins a live finding through its sealed `VerdictInput` to a typed canonical observation and its
        recorded page transform. It never reads a raw candidate, a trace's display string, or a
        reviewer-entered literal to decide where to draw. When no such location exists, no redline
        artifact is made; the workbook and findings PDF remain complete and truthful.
        """
        if self._store is None:
            return {
                "implemented": True,
                "ran": False,
                "reason": "no artifact store is configured",
                "outputs": 0,
            }

        rows = session.execute(
            select(FindingRow, CheckRun, RuleSnapshotRow, RuleDefinition)
            .join(CheckRun, FindingRow.check_run_id == CheckRun.id)
            .join(RuleSnapshotRow, CheckRun.rule_snapshot_id == RuleSnapshotRow.id)
            .join(RuleDefinition, RuleSnapshotRow.rule_definition_id == RuleDefinition.id)
            .where(
                FindingRow.package_revision_id == package_revision_id,
                CheckRun.superseded_at.is_(None),
            )
            # Ordered so regenerating an unchanged revision produces the same bytes, which is what
            # makes the content-addressed key below mean anything.
            .order_by(FindingRow.created_at, FindingRow.id)
        ).all()

        if not rows:
            # No file. An empty workbook would be a deliverable asserting a package was checked and
            # found clean, when in fact nothing ran.
            return {
                "implemented": True,
                "ran": True,
                "findings": 0,
                "outputs": 0,
                "reason": "this revision has no live findings, so there is nothing to report on",
            }

        stored_findings: list[StoredFinding] = []
        composition_facts: list[ComposerFinding] = []
        for finding, run, snapshot, definition in rows:
            stored_finding = StoredFinding(
                rule_id=definition.rule_id,
                outcome=finding.outcome,
                severity=finding.severity,
                snapshot_id=snapshot.snapshot_id,
                engine_version=run.engine_version,
                trace=finding.trace,
                reason=finding.reason,
                # Rendered here rather than in the writer, because the exact rational lives in
                # three columns and reassembling it is this layer's job. `format_inches` writes
                # `1 1/2`, the way a drawing does — a reviewer is comparing this against a sheet.
                delta=_delta_text(finding),
                variant=finding.variant,
                notes=None if finding.notes is None else tuple(finding.notes),
            )
            stored_findings.append(stored_finding)
            try:
                snapshot_data = json.loads(snapshot.canonical_json)
                check_name_value = (
                    snapshot_data.get("name") if isinstance(snapshot_data, Mapping) else None
                )
            except (TypeError, ValueError):
                check_name_value = None
            check_name = (
                check_name_value
                if isinstance(check_name_value, str) and check_name_value.strip()
                else definition.rule_id
            )
            composition_facts.append(
                _finding_facts(key=str(finding.id), check_name=check_name, finding=stored_finding)
            )

        findings_composer = self._findings_composer
        if findings_composer is not None and hasattr(findings_composer, "with_invocation_recorder"):
            recording_composer = cast(_RecordingFindingsLanguageModel, findings_composer)
            findings_composer = recording_composer.with_invocation_recorder(
                BedrockConverseInvocationRecorder(session, package_revision_id)
            )

        composition = compose_findings(composition_facts, findings_composer)
        narratives = {narrative.finding_key: narrative.text for narrative in composition.narratives}
        rendered_findings = [
            replace(finding, reviewer_summary=narratives[facts.key])
            for finding, facts in zip(stored_findings, composition_facts, strict=True)
        ]
        workbook = write_stored_workbook(rendered_findings)
        revision = session.get(PackageRevision, package_revision_id)
        if revision is None:
            raise ValueError(f"package revision {package_revision_id} does not exist")
        package = session.get(Package, revision.package_id)
        if package is None:
            raise ValueError(f"package {revision.package_id} does not exist")
        findings_pdf = write_findings_pdf(
            FindingsPdfInput(
                package_revision_id=revision.id,
                revision_number=revision.revision_number,
                vendor=package.vendor,
                findings=tuple(rendered_findings),
            )
        )
        composition_status: dict[str, object] = {
            "mode": composition.mode.value,
            "model_id": composition.model_id,
            "prompt_id": composition.prompt_id,
            "template_id": composition.template_id,
        }
        if composition.fallback_reason is not None:
            composition_status["fallback_reason"] = composition.fallback_reason

        redline = render_evidence_grounded_redline(
            session,
            self._store,
            package_revision_id=package_revision_id,
            findings=tuple(
                (finding, run, definition.rule_id, snapshot.snapshot_id)
                for finding, run, snapshot, definition in rows
            ),
        )

        outputs = (
            (OutputArtifactKind.FINDINGS_WORKBOOK, workbook, WORKBOOK_MEDIA_TYPE, ".xlsx"),
            (OutputArtifactKind.FINDINGS_PDF, findings_pdf, FINDINGS_PDF_MEDIA_TYPE, ".pdf"),
        )
        written: dict[str, str] = {}
        for kind, document, media_type, suffix in outputs:
            digest, _ = sha256_stream(BytesIO(document))
            key = content_key(f"outputs/{package_revision_id}", digest, suffix=suffix)
            existing = session.execute(
                select(OutputArtifact.id).where(
                    OutputArtifact.storage_key == key, OutputArtifact.sha256 == digest
                )
            ).first()
            if existing is not None:
                continue
            stored = self._store.put(key, BytesIO(document), content_type=media_type)
            session.add(
                OutputArtifact(
                    package_revision_id=package_revision_id,
                    kind=kind.value,
                    storage_key=stored.key,
                    sha256=stored.sha256,
                    media_type=media_type,
                    findings=len(rows),
                )
            )
            written[kind.value] = stored.key
        if redline.artifact is not None:
            existing = session.execute(
                select(OutputArtifact.id).where(
                    OutputArtifact.storage_key == redline.artifact.key,
                    OutputArtifact.sha256 == redline.artifact.sha256,
                )
            ).first()
            if existing is None:
                session.add(
                    OutputArtifact(
                        package_revision_id=package_revision_id,
                        kind=OutputArtifactKind.REDLINE.value,
                        storage_key=redline.artifact.key,
                        sha256=redline.artifact.sha256,
                        media_type="application/pdf",
                        findings=len(rows),
                    )
                )
                written[OutputArtifactKind.REDLINE.value] = redline.artifact.key
        session.flush()
        return {
            "implemented": True,
            "ran": True,
            "findings": len(rows),
            "outputs": len(written),
            "already_recorded": not written,
            "storage_keys": written,
            "findings_composition": composition_status,
            "redline": {
                "generated": redline.artifact is not None,
                "reason": redline.reason,
            },
        }

    def run_checks(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        """Run every applicable rule against this revision and record what each decided.

        **`register_all()` first, every time.** The operation registry is global and empty until
        somebody fills it; today the only thing that does is importing `app/api/operations.py`, which
        a worker process never touches. Without this call every operation lookup fails and the engine
        converts the failure into `REVIEW_REQUIRED` — so the whole package would abstain, plausibly,
        for a reason that appears nowhere. It is idempotent.

        **Previous runs are superseded before new ones are written**, inside this transaction, so no
        reader ever sees two sets of findings for one revision.
        """
        register_all()
        reviewer_operands: Mapping[str, Mapping[str, VerdictOperand]]
        if self._operands is None:
            from workflow.measurements import operands_for

            reviewer_operands = operands_for(session, package_revision_id)
        else:
            reviewer_operands = self._operands

        revision = session.get(PackageRevision, package_revision_id)
        if revision is None:
            return {"implemented": True, "ran": False, "reason": "no such package revision"}

        package = session.get(Package, revision.package_id)
        if package is None:
            return {"implemented": True, "ran": False, "reason": "no such package"}

        store = snapshot_store(session)
        if not store.rule_ids():
            # Not a failure. Nothing is published, so there is nothing to check — and saying so is
            # different from running zero rules and reporting success.
            return {
                "implemented": True,
                "ran": False,
                "reason": "no rules are published; nothing to check",
            }

        # **The RUN layer, which nothing else loads.** `load_parameter_sets` covers GLOBAL and
        # PROJECT and says why — "RUN sets are supplied per review and are not loaded here" — so a
        # run-scope parameter reached no resolver at all. `sink_interior_depth` and
        # `sink_interior_width` come off the sink cut sheet for one review, and without this both
        # sink-cutout checks abstained however carefully a reviewer typed them, for a reason nothing
        # on the findings list could explain.
        #
        # Loaded here rather than injected, because unlike operands these values *are* in the
        # database: reading them is the same act as reading the project's own settings.
        stored_layers = [
            *load_parameter_sets(session, package.project_id),
            *(
                run
                for run in (run_parameters_for(session, package_revision_id),)
                if run is not None
            ),
        ]
        rules = [store.latest(rule_id) for rule_id in store.rule_ids()]
        typing = (
            qualify_exact_tags_for_revision(
                session,
                package_revision_id=package_revision_id,
                settings=self._automatic_typing,
            )
            if self._automatic_typing is not None
            else None
        )
        defaults = declared_defaults(
            [snapshot.rule for snapshot in rules if snapshot is not None], when=utc_now()
        )
        layers = _layered(defaults, stored_layers)
        cited = _cited_sets(defaults, stored_layers)
        resolved = resolve_all(*layers)

        # `ProjectScope` wants the pinned project layer. Where a project has set nothing, the
        # rulebook's own defaults stand in — they are a real published answer, not a fabricated one.
        project_layer = next(
            (layer for layer in layers if layer.project_id == str(package.project_id)),
            None,
        )
        scope = ProjectScope(
            project_id=str(package.project_id),
            parameter_set=project_layer if project_layer is not None else _empty_project(package),
        )

        # **Every product type, not one.** The resolver keys candidates on an exact product-type
        # match, so asking about countertops alone would leave the cabinet rules unrun — and unrun is
        # indistinguishable from passing once the reviewer is looking at the list. A package carries
        # no product type today (there is no column for it), and guessing one from the vendor or the
        # filename would decide which checks apply by inference. Running the whole rulebook is the
        # honest reading until a package can say what is in it: a cabinet rule against a countertop
        # package abstains, which is visible, where omitting it is not.
        #
        # No discriminator can be established without extraction, so a rule that declares one
        # abstains rather than being resolved to a variant nobody read off a drawing.
        superseded = supersede_runs(session, package_revision_id)

        written = 0
        skipped = 0
        for product_type in ProductType:
            resolution = resolve(
                store,
                CheckContext(
                    product_type=product_type,
                    project=scope,
                    # A reviewer stating the wall layout is the same manual input as a reviewer
                    # typing a dimension. Empty unless supplied, so a rule with a discriminator
                    # still abstains rather than being resolved to a variant nobody established.
                    discriminators=self._discriminators,
                ),
            )
            # **A rule that could not even be attempted becomes a finding too.**
            # The resolver abstains when it cannot establish which variant applies — today that is
            # every rule with a discriminator, because nothing reads `wall_config` off a drawing. If
            # those were only counted, the reviewer would see the checks that ran and have no way to
            # learn that two more never started. Unrun and passed are indistinguishable on a list,
            # which is the failure this whole system is built to prevent, so they are recorded with
            # the resolver's own reason.
            for abstention in resolution.abstentions:
                if abstention.rule_id is None:
                    # Nothing to attribute it to, so nothing to write. Counted instead, and returned,
                    # rather than attached to an arbitrary rule.
                    skipped += 1
                    continue
                snapshot = store.latest(abstention.rule_id)
                if snapshot is None:
                    skipped += 1
                    continue
                record_finding(
                    session,
                    package_revision_id=package_revision_id,
                    finding=_unresolved(snapshot, abstention),
                    operands={},
                    parameter_set_ids=cited,
                )
                written += 1

            # **Sealed evidence, before anything a caller supplied (#530).** This is the link that
            # was missing: until now the only way a rule got an operand was for somebody to type the
            # number into a form, so a drawing could be read and confirmed and the checks would still
            # be judged on a reviewer's transcription.
            #
            # A caller's operand still wins where both exist. Supplying one is a deliberate act — the
            # reviewer entered that number for this run — and evidence is derived, so overriding the
            # explicit thing with the derived one would take an answer away from the person who gave
            # it. It also keeps the Q7 form path behaving exactly as it did.
            evidence = evidence_operands(
                session,
                package_revision_id,
                [applicable.snapshot.rule for applicable in resolution.applicable],
            )
            from_evidence = evidence.operands

            for applicable in resolution.applicable:
                rule_id = applicable.snapshot.rule.id
                supplied = {
                    **from_evidence.get(rule_id, {}),
                    **reviewer_operands.get(rule_id, {}),
                }
                finding = execute(
                    applicable.snapshot,
                    supplied,
                    resolved,
                    discriminators=self._discriminators,
                    # Readings found on more than one drawing (#826). A value typed for the same
                    # input still wins, as it does over evidence: the engine ignores an ambiguous
                    # input it was given an operand for.
                    ambiguous=evidence.ambiguous.get(rule_id, {}),
                )
                # A run evidence was not allowed to fill (#794, #833), named in its check's own
                # sentence — otherwise "could not resolve 'shop_cabinets'" reads as though labelling
                # a cabinet would fix it, when it is the tag, or the order along the wall, that is
                # missing. Sorted so that a rule with two such sentences reads the same every run.
                unfilled = sorted(
                    {
                        why
                        for name, why in position_sensitive_inputs(applicable.snapshot.rule).items()
                        if name not in supplied
                    }
                )
                if finding.outcome is Outcome.NOT_FOUND and unfilled:
                    finding = replace(finding, reason=f"{' '.join(unfilled)} ({finding.reason})")
                record_finding(
                    session,
                    package_revision_id=package_revision_id,
                    finding=finding,
                    operands=supplied,
                    parameter_set_ids=cited,
                    missing=_declared_inputs(applicable.snapshot.rule),
                )
                written += 1

        return {
            "implemented": True,
            "ran": True,
            "findings": written,
            "rules_published": len(store.rule_ids()),
            "superseded_runs": superseded,
            "not_applicable": skipped,
            "automatic_types_qualified": 0 if typing is None else len(typing.qualified),
            "semantic_typing_review_required": 0 if typing is None else len(typing.review_required),
        }


def _unresolved(snapshot: RuleSnapshot, abstention: Abstention) -> Finding:
    """A rule the resolver could not even attempt, as the finding a reviewer will read.

    Built here rather than by the engine because the engine was never reached: applicability is
    decided before any arithmetic, and a rule whose variant is unknown has no operands to trace. The
    severity comes from the rule itself, so an unattempted critical check is still critical.
    """
    return Finding(
        rule_id=snapshot.rule.id,
        outcome=abstention.outcome,
        severity=snapshot.rule.severity,
        reason=abstention.reason,
        snapshot_id=snapshot.snapshot_id,
        engine_version=ENGINE_VERSION,
    )


def _layered(defaults: ParameterSet, stored: Sequence[ParameterSet]) -> tuple[ParameterSet, ...]:
    """The rulebook defaults beneath whatever the database supplies, merged by name (#812).

    `rules.parameters.resolve` refuses two sets in one layer, and the defaults are GLOBAL, as company
    standards are. **They used to be replaced wholesale** by any stored company set — so recording one
    company standard, the usual cabinet depth, silently removed every rule author's default: the
    2.5" back-offset minimum, the 4" front offset, the 1/4" clearance, the 1"/2" filler bounds, and
    each of those checks went back to NOT_FOUND. Now a recorded company value replaces the rulebook's
    default **of the same name** and nothing else. A rule author's default is what applies until
    somebody records that one.

    The merged set is resolution's input, not a record: a finding cites the stored company set's own
    hash (`_cited_sets`), and the defaults beneath it are pinned by the rule snapshot it also cites.
    """
    company = next((layer for layer in stored if layer.layer is defaults.layer), None)
    if company is None:
        return (defaults, *stored)
    merged = ParameterSet(
        project_id=company.project_id,
        layer=company.layer,
        version=company.version,
        parameters={**defaults.parameters, **company.parameters},
    )
    return tuple(merged if layer is company else layer for layer in stored)


def _cited_sets(defaults: ParameterSet, stored: Sequence[ParameterSet]) -> dict[str, str]:
    """Which parameter sets judged a finding, by layer, as hashes that resolve to a record.

    The stored sets as stored — never the merged one `_layered` builds, whose hash no row holds — and
    the rulebook defaults' set only where no company set was recorded.
    """
    return {layer.layer.value: layer.set_id for layer in (defaults, *stored)}


def _empty_project(package: Package) -> ParameterSet:
    """A project layer with nothing in it, for a project that has configured nothing.

    `ProjectScope` requires a pinned set and cannot take `None`. An empty one is truthful — this
    project has set no overrides — and resolution then falls through to the layers beneath it.
    """
    from rules.parameters import ParameterLayer

    return ParameterSet(
        project_id=str(package.project_id),
        layer=ParameterLayer.PROJECT,
        version=1,
        parameters={},
    )


def _declared_inputs(rule: object) -> dict[str, str]:
    """The operands a rule needs, so an abstention can name what was not read.

    Reported from the rule rather than from the empty operand mapping, because "nothing was supplied"
    is not useful and "no dimension was read for cutout_width (SHOP)" sends somebody to the drawing.
    """
    inputs = getattr(rule, "inputs", {})
    return {name: getattr(selector, "source", "?") for name, selector in inputs.items()}


def _documents_for(session: Session, package_revision_id: UUID) -> list[tuple[UUID, str]]:
    """Every document version attached to this revision, with the key its bytes live under.

    Read through `package_revision_documents` rather than from `documents`, because a document
    belongs to a *package* while a revision is composed of specific *versions* — going the short way
    would read whatever version is newest rather than the one this revision was built from, and a
    review would then be of drawings nobody submitted.

    The storage key is derived rather than stored: `app/api/documents.py` builds it from the document
    id and the content hash, and it is recomputed the same way here. Deriving it in two places is
    worth naming as a smell — change that scheme and this breaks — but the alternative is a column
    that does not exist, and adding one is a migration this change should not carry.
    """
    rows = session.execute(
        select(
            PackageRevisionDocument.document_version_id,
            PackageRevisionDocument.document_id,
            DocumentVersion.sha256,
        )
        .join(
            DocumentVersion,
            DocumentVersion.id == PackageRevisionDocument.document_version_id,
        )
        .where(PackageRevisionDocument.package_revision_id == package_revision_id)
        .order_by(DocumentVersion.created_at, DocumentVersion.id)
    ).all()
    return [(version_id, storage_key(document_id, sha)) for version_id, document_id, sha in rows]


def _delta_text(finding: FindingRow) -> str | None:
    """A finding's stored delta as exact text, or `None` when it has none.

    The three columns are all-present or all-absent, enforced by `finding_delta_complete`, so testing
    the numerator answers for all three. `Fraction` keeps it exact through the reassembly — building
    a float here would undo the reason it was stored as a rational in the first place (ADR-0001).
    """
    if finding.delta_numerator is None or finding.delta_denominator is None:
        return None
    exact = Fraction(finding.delta_numerator, finding.delta_denominator)
    return f"{format_inches(exact)} {finding.delta_unit}"


def _composer_text(value: object) -> str:
    return "" if value is None else str(value)


def _composer_evidence_page(reference: object) -> str | None:
    """A human page number for narration, without document ids or hash fragments."""
    if not isinstance(reference, str) or not reference:
        return None
    decoded = decode_reference(reference)
    return None if decoded is None else decoded[0]


def _composer_operands(trace: Mapping[str, object]) -> tuple[ComposerOperand, ...]:
    raw = trace.get("operands")
    if not isinstance(raw, list):
        return ()
    result: list[ComposerOperand] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        result.append(
            ComposerOperand(
                name=_composer_text(item.get("name")),
                value=_composer_text(item.get("value")),
                source=_composer_text(item.get("source")),
                evidence_page=_composer_evidence_page(item.get("evidence_ref")),
            )
        )
    return tuple(result)


def _finding_facts(*, key: str, check_name: str, finding: StoredFinding) -> ComposerFinding:
    """Build narration facts without re-parsing any value or re-running any rule."""
    trace = finding.trace
    operands = _composer_operands(trace)
    pages = tuple(
        dict.fromkeys(op.evidence_page for op in operands if op.evidence_page is not None)
    )
    return ComposerFinding(
        key=key,
        check=finding.rule_id,
        check_name=check_name or finding.rule_id,
        outcome=finding.outcome,
        severity=finding.severity,
        reason=reviewer_reason(
            finding.reason or _composer_text(trace.get("reason")) or "No reason was recorded.",
            finding.outcome,
        ),
        comparison=_composer_text(trace.get("comparison")) or None,
        difference=finding.delta,
        tolerance=_composer_text(trace.get("tolerance")) or None,
        arithmetic_unit=_composer_text(trace.get("arithmetic_unit")) or None,
        operands=operands,
        evidence_pages=pages,
        notes=() if finding.notes is None else finding.notes,
    )


def _document_records_for(
    session: Session, package_revision_id: UUID
) -> list[tuple[UUID, str, str, int]]:
    """Every document version in this revision, with its key, recorded digest and page count.

    Separate from `_documents_for` rather than replacing it. `validate_evidence` wants only somewhere
    to read bytes from; `ingest` and `extract_pages` want the facts to check those bytes against as
    well. Handing a four-tuple to a caller that uses two of it is how a helper starts drifting, and
    the split says which callers care about verification.
    """
    rows = session.execute(
        select(
            PackageRevisionDocument.document_version_id,
            PackageRevisionDocument.document_id,
            DocumentVersion.sha256,
            DocumentVersion.page_count,
        )
        .join(
            DocumentVersion,
            DocumentVersion.id == PackageRevisionDocument.document_version_id,
        )
        .where(PackageRevisionDocument.package_revision_id == package_revision_id)
        .order_by(DocumentVersion.created_at)
    ).all()
    return [
        (version_id, storage_key(document_id, sha), sha, page_count)
        for version_id, document_id, sha, page_count in rows
    ]


def _layout_discriminators(session: Session) -> tuple[DiscriminatorNeed, ...]:
    """The closed layout questions declared by the currently published rulebook."""

    store = snapshot_store(session)
    rules = tuple(
        snapshot.rule
        for rule_id in store.rule_ids()
        for snapshot in (store.latest(rule_id),)
        if snapshot is not None
    )
    return required_inputs(rules).discriminators


def _layout_proposal_identity(routes: Sequence[_LayoutReaderRoute]) -> tuple[str, str]:
    """The model/prompt identity stored beside a layout proposal."""

    if not routes:
        return UNCONFIGURED_LAYOUT_MODEL_ID, LAYOUT_PROMPT_ID
    return (
        "+".join(route.model_id for route in routes),
        "+".join(dict.fromkeys(route.prompt_id for route in routes)),
    )


def _layout_proposed_value(classification: LayoutClassification) -> str:
    """A closed answer, or a visible non-choice marker for abstention/disagreement."""

    if classification.status is LayoutStatus.ANSWERED:
        if classification.answer is None:
            raise ValueError("answered layout classification did not carry an answer")
        return classification.answer
    return classification.status.value


def _image_polygon(polygon: Polygon, rendered: RenderedPage) -> list[list[int]]:
    """Convert stored-space layout evidence into the image-space shape the artifact owner needs."""

    return [
        [
            int((point.x * Decimal(rendered.width_px)).to_integral_value()),
            int((point.y * Decimal(rendered.height_px)).to_integral_value()),
        ]
        for point in polygon.points
    ]


def _record_panel_views(session: Session, page: Page, layers: PageLayers) -> dict[str, int]:
    """One view per drawing on the page, each with what its label suggests, counted (#710)."""
    counts = {"views": 0, "suggested_arch": 0, "suggested_shop": 0, "no_suggestion": 0}
    for proposal, stamp in zip(
        propose_panel_roles(layers.vendor_stamps, layers.markup), layers.vendor_stamps, strict=True
    ):
        record_panel_view(
            session,
            page_id=page.id,
            annotation_index=stamp.annotation_index,
            stored_points=[(point.x, point.y) for point in stamp.extent.points],
            proposed_role=proposal.role,
            heading=proposal.heading,
            reason=proposal.reason,
        )
        counts["views"] += 1
        counts[f"suggested_{proposal.role}" if proposal.role else "no_suggestion"] += 1
    return counts


def _view_role(session: Session, view: DrawingView) -> str | None:
    """The role a person confirmed for a view, or else the latest one its label suggests (#710)."""
    if view.role is not None:
        return view.role
    return session.execute(
        select(ViewRoleProposal.proposed_role)
        .where(ViewRoleProposal.drawing_view_id == view.id)
        .order_by(ViewRoleProposal.created_at.desc(), ViewRoleProposal.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def region_facts(
    candidate: ObservationCandidate,
    candidates: Sequence[ObservationCandidate],
    *,
    version_id: UUID,
    page: Page,
    rendered: RenderedPage,
    polygon: Polygon | None,
    transform: PageTransform | None,
    reach: LabelReach | None,
    page_glyphs: Sequence[VectorPath],
    stacked: Callable[[Polygon], bool],
) -> tuple[RegionFacts, Polygon | None]:
    """What the file established about one region, and the region widened to its whole label.

    Public so the agent's scorecard (`eval/experiments/agent_scorecard.py`) judges a crop by this
    code, not a copy of it: a second computation of what the geometry says is a second answer.

    **Geometry only.** The cut and the direction come from the vendor's paths
    (`extraction/agent/geometry.py`), the stacked fraction from the bar detector, the witnesses
    from other routes' recorded values. Where the paths were not read — no reading agent, no
    association settings, no recorded page transform — the geometry says nothing, and the
    region is read as it stands, as before #757.
    """
    # **What the region's other readers read**, as independent witnesses. Same region means the
    # same recorded polygon, the rule cross-route corroboration groups by.
    witnesses = tuple(
        value
        for other in candidates
        if other is not candidate
        and other.polygon == candidate.polygon
        and (value := _stored_measurement(other)) is not None
    )
    cut_at_edge = False
    rotation_degrees = 0
    whole_run: Polygon | None = None
    if reach is not None and transform is not None and polygon is not None:
        region_box = _pdf_box(transform, [(point[0], point[1]) for point in candidate.polygon])
        left, top, right, bottom = crop_box_px(rendered, polygon, VISION_CROP_CONTEXT_MARGIN_PT)
        crop_box = _pdf_box(transform, [(left, top), (right, bottom)])
        geometry = label_geometry(region_box, crop_box, page_glyphs, reach)
        cut_at_edge = geometry.cut_at_edge
        rotation_degrees = geometry.rotation_degrees
        if geometry.label_box is not None and geometry.closed:
            label = geometry.label_box
            widened: Box = (
                min(label[0], region_box[0]),
                min(label[1], region_box[1]),
                max(label[2], region_box[2]),
                max(label[3], region_box[3]),
            )
            try:
                whole_run = page_box_polygon(widened, transform, version_id, page.index)[0]
            except (TypeError, ValueError):
                whole_run = None
    facts = RegionFacts(
        cut_at_edge=cut_at_edge,
        rotation_degrees=rotation_degrees,
        stacked_fraction=polygon is not None and stacked(polygon),
        # Not handed to the agent; see the caller's docstring.
        shape_reading=None,
        other_route_values=witnesses,
    )
    return facts, whole_run


def _shows(crop_box: tuple[int, int, int, int], fraction: StackedFraction) -> bool:
    left, top, right, bottom = crop_box
    xs = [point.x for point in fraction.image_extent]
    ys = [point.y for point in fraction.image_extent]
    return min(xs) <= right and left <= max(xs) and min(ys) <= bottom and top <= max(ys)


def crop_shows_a_stacked_fraction(
    crop_box: tuple[int, int, int, int], fractions: Sequence[StackedFraction]
) -> bool:
    """Whether any part of a detected stacked fraction falls inside a crop's page pixels (#735).

    **Any part, edges included.** A model reads whatever it is shown, and a numerator at the crop's
    edge is still there to be promoted into a whole number. The crop's own `(left, top, right,
    bottom)` and the fraction's corners are both page pixels at the reader's dpi.
    """
    return any(_shows(crop_box, fraction) for fraction in fractions)


def stacked_layouts_shown(
    crop_box: tuple[int, int, int, int], fractions: Sequence[StackedFraction]
) -> tuple[FractionLayout, ...]:
    """The layouts of the stacked fractions a crop shows, by the rule above (#834).

    A fraction set in text has no layout and adds none, so a crop showing only such fractions shows
    a stacked fraction (`crop_shows_a_stacked_fraction`) and has no layouts to check a reading by.
    """
    return tuple(
        fraction.layout
        for fraction in fractions
        if fraction.layout is not None and _shows(crop_box, fraction)
    )


def _vision_pre_call_refusal(
    crop_box: tuple[int, int, int, int], fractions: Sequence[StackedFraction]
) -> str | None:
    """Why a crop should not be sent to a vision reader, before spending a model call."""

    if crop_shows_a_stacked_fraction(crop_box, fractions):
        return STACKED_FRACTION_REASON
    return None


def stored_polygon(
    candidate: ObservationCandidate | _VisionRegion, rendered: RenderedPage
) -> Polygon | None:
    """A candidate's image-pixel polygon as the normalised one a `CropSpec` takes.

    Returns `None` rather than raising, and rather than clamping. A point outside the rendering means
    the pixels in front of us are not the pixels the reader measured against — a different dpi, a
    different page, a re-rendered document. Clamping would produce a crop of a real region that is
    not the region the reading came from, which is worse than no crop: it is a picture that argues
    for the wrong number. `validate_evidence` counts the refusal and says which page it was on.
    """
    width = Decimal(rendered.width_px)
    height = Decimal(rendered.height_px)
    try:
        points = tuple(
            StoredPoint(x=Decimal(int(x)) / width, y=Decimal(int(y)) / height)
            for x, y in candidate.polygon
        )
        return Polygon(
            points=points,
            space="stored",
            document_version_id=candidate.document_version_id,
            page=rendered.page_index,
        )
    except (ArithmeticError, TypeError, ValueError):
        # `Polygon` refuses a degenerate, self-intersecting or out-of-page shape, and a text run with
        # zero width is degenerate. Every one of those is a reason not to cut a crop.
        return None


def _projection(
    item: DrawingItem,
    role: MatchDocumentRole,
    identifier: NormalizedIdentifier | None,
    project_id: UUID,
    package_revision_id: UUID,
) -> MatchableItem:
    """One drawing item as the matcher's own projection of it."""
    return MatchableItem(
        item_id=item.id,
        identifier=identifier,
        project_id=project_id,
        package_revision_id=package_revision_id,
        # The item's own type, which is the scope the matcher compares within: a countertop is not a
        # candidate match for a cabinet however alike their marks.
        category=item.item_type,
        document_role=role,
    )


@dataclass(frozen=True, slots=True)
class _MatchRoleSummary:
    total_items: int
    roles: frozenset[MatchDocumentRole]


def _revision_document_roles(
    session: Session, package_revision_id: UUID
) -> frozenset[MatchDocumentRole]:
    roles = {
        role
        for (kind,) in session.execute(
            select(Document.kind)
            .join(PackageRevisionDocument, PackageRevisionDocument.document_id == Document.id)
            .where(PackageRevisionDocument.package_revision_id == package_revision_id)
        )
        if (role := MATCH_ROLES.get(kind)) is not None
    }
    return frozenset(roles)


def _fallback_to_document_kind_allowed(session: Session, package_revision_id: UUID) -> bool:
    """Whether legacy two-PDF role inference is available for this revision."""

    return _revision_document_roles(session, package_revision_id) >= {
        MatchDocumentRole.ARCH,
        MatchDocumentRole.SHOP,
    }


def _resolved_match_role(
    view_role: str | None, document_kind: str, *, document_fallback_allowed: bool
) -> MatchDocumentRole | None:
    """The role to hand to matching, or None when no role has been established.

    Combined documents may carry both roles under one `Document.kind`, so a null view role cannot be
    silently replaced by that kind. The old document-kind inference remains only for genuine two-PDF
    packages, where the revision contains both architectural and shop drawings.
    """

    if view_role is not None:
        return VIEW_MATCH_ROLES.get(view_role)
    if document_fallback_allowed:
        return MATCH_ROLES.get(document_kind)
    return None


def _missing_role_names(roles: frozenset[MatchDocumentRole]) -> str:
    missing: list[str] = []
    if MatchDocumentRole.ARCH not in roles:
        missing.append("architectural")
    if MatchDocumentRole.SHOP not in roles:
        missing.append("shop")
    return ", ".join(missing)


def _match_role_summary(session: Session, package_revision_id: UUID) -> _MatchRoleSummary:
    document_fallback_allowed = _fallback_to_document_kind_allowed(session, package_revision_id)
    rows = session.execute(
        select(DrawingItem.id, DrawingView.role, Document.kind)
        .join(DrawingView, DrawingView.id == DrawingItem.drawing_view_id)
        .join(Page, Page.id == DrawingView.page_id)
        .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == DocumentVersion.id,
        )
        .where(PackageRevisionDocument.package_revision_id == package_revision_id)
        .where(DrawingItem.id.in_(live_part_item_ids()))
    ).all()
    roles = {
        role
        for _, view_role, document_kind in rows
        if (
            role := _resolved_match_role(
                view_role,
                document_kind,
                document_fallback_allowed=document_fallback_allowed,
            )
        )
        is not None
    }
    return _MatchRoleSummary(
        total_items=len({item_id for item_id, _, _ in rows}), roles=frozenset(roles)
    )


def _matchable_items(
    session: Session, package_revision_id: UUID
) -> list[tuple[MatchableItem, str]]:
    """This revision's drawing items as the matcher's own projection, paired with identifier kind.

    One entry per (item, identifier): an item carrying both a vendor code and a mark takes part in
    both lanes, and each is matched only against its own kind. An item with no identifier at all is
    included once with `identifier=None`, because the matcher's answer for it — unmatched, for a
    stated reason — is a result a reviewer needs, not an absence to hide.

    The role comes from the view when it has been established. For legacy two-PDF packages only, a
    null view role falls back to `Document.kind`; combined sheets must not infer every view from one
    upload kind. Schedules and product specs are filtered out because they have no match role.

    **Only live parts (#882).** An item a person withdrew, or replaced with a correction, keeps its
    row; reading it would match a part that no longer exists. So only the items
    `live_part_item_ids` names are read.
    """
    project_id = session.execute(
        select(Package.project_id)
        .join(PackageRevision, PackageRevision.package_id == Package.id)
        .where(PackageRevision.id == package_revision_id)
    ).scalar_one_or_none()
    if project_id is None:
        return []

    document_fallback_allowed = _fallback_to_document_kind_allowed(session, package_revision_id)
    rows = session.execute(
        select(DrawingItem, DrawingView.role, Document.kind, ItemIdentifier)
        .join(DrawingView, DrawingView.id == DrawingItem.drawing_view_id)
        .join(Page, Page.id == DrawingView.page_id)
        .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == DocumentVersion.id,
        )
        .outerjoin(ItemIdentifier, ItemIdentifier.drawing_item_id == DrawingItem.id)
        .where(PackageRevisionDocument.package_revision_id == package_revision_id)
        .where(DrawingItem.id.in_(live_part_item_ids()))
        .order_by(DrawingItem.created_at)
    ).all()

    # Grouped by item first, because the decision below is per item and not per row. An item with
    # two identifiers arrives as two rows, and an item whose only identifier is a catalogue number
    # must still appear — as unmatchable, which is a result — rather than vanish because its one row
    # was filtered out. Filtering row by row made exactly that mistake.
    grouped: dict[UUID, tuple[DrawingItem, str | None, str, list[ItemIdentifier]]] = {}
    for item, view_role, kind, identifier in rows:
        entry = grouped.setdefault(item.id, (item, view_role, kind, []))
        if identifier is not None:
            entry[3].append(identifier)

    items: list[tuple[MatchableItem, str]] = []
    for item, view_role, kind, identifiers in grouped.values():
        role = _resolved_match_role(
            view_role,
            kind,
            document_fallback_allowed=document_fallback_allowed,
        )
        if role is None:
            continue
        usable = [
            identifier
            for identifier in identifiers
            if identifier.kind in MATCHABLE_IDENTIFIER_KINDS
        ]

        if not usable:
            # No identifier this lane can use — either none at all, or only a catalogue number,
            # which names a model rather than a unit. The matcher's answer is unmatched with a
            # reason, and a reviewer needs to see that far more than they need the row hidden.
            items.append((_projection(item, role, None, project_id, package_revision_id), ""))
            continue
        for identifier in usable:
            items.append(
                (
                    _projection(
                        item,
                        role,
                        normalize_identifier(identifier.value_as_printed),
                        project_id,
                        package_revision_id,
                    ),
                    identifier.kind,
                )
            )
    return items


def _task_run_for(session: Session, package_revision_id: UUID, stage: str) -> TaskRun | None:
    """The task run `run_stage` claimed for this stage.

    Looked up by recomputing the key rather than taken as an argument, because `run_stage` holds the
    `Claim` and does not hand it to the stage body. Widening the `Stages` protocol for one caller
    would change a seam five other implementations already satisfy; recomputing a deterministic key
    does not.
    """
    key = stage_idempotency_key(
        package_revision_id=package_revision_id, stage=stage, engine_version=ENGINE_VERSION
    )
    return session.execute(
        select(TaskRun).where(TaskRun.idempotency_key == key)
    ).scalar_one_or_none()


def _fetch(store: ArtifactStore, key: str) -> bytes:
    """The stored bytes for one document. Raises when they cannot be read.

    **This used to catch `Exception` and return `None`, and that was the bug** (found in review on
    #484, fixed in #487). The caller skipped a document that returned nothing, so a missing artifact,
    an object whose digest no longer matches, and a document that simply is not attached to the
    revision all became the same silent outcome: a revision that reported extraction as done, with
    one of its drawings never read. A package that looks checked while a drawing in it was never
    opened is the package-level shape of a false PASS.

    The old docstring even said the store "verifies its own digest on the way out, so a corrupt
    object arrives here as an exception rather than as wrong bytes" — and then discarded that
    exception. The sentence was true and the code threw the value away.

    So it raises, and the raise is the right mechanism rather than a nuisance: a storage failure is
    infrastructure, not a fact about the drawing, and `run_stage` rolls the stage back for
    re-delivery, which is exactly what should happen to a fetch that may well succeed next time.
    """
    with store.get(key) as stored:
        return stored.read()


def _chain_membership(detected: DetectedDimensions, *, page_id: UUID) -> dict[str, tuple[str, int]]:
    """Which run each detected dimension line belongs to, keyed by where the line is.

    **The grouping was being computed and thrown away.** `detect` returns chains — dimension lines
    drawn end to end along one axis, which is what a cabinet run looks like on a sheet — and
    `associate` is handed only the extents, so the chains went out of scope on the next line. They
    are the fact `workflow/assignment.py` needs to refuse a many-valued field gathered from two
    different runs, and that refusal could not fire on a real drawing without them.

    The key is page-scoped and deterministic: the page's own id, the axis, and the chain's index in
    detection order. Page-scoped because one extraction run covers every page of every document in a
    stage execution, so two pages with the same geometry would otherwise be handed the same chain
    name and their readings would look like one run.

    A line in no chain is simply absent, which is most lines and is not a gap: a single dimension is
    not a run, and reporting it as one would make every dimension on the sheet look like a closure
    waiting to be validated.
    """
    membership: dict[str, tuple[str, int]] = {}
    for index, chain in enumerate(detected.chains):
        key = f"{page_id}:{chain.axis.value}:{index}"
        for position, line in enumerate(chain.lines):
            extent = line.extent
            membership[line_key(extent.start.x, extent.start.y, extent.end.x, extent.end.y)] = (
                key,
                position,
            )
    return membership
