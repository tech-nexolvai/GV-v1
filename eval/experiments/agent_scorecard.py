"""The reading agent's scorecard on the human-read key: the reader pair alone, and with the agent (#757).

**What is compared.** Every crop of the key is read three ways, and each way ends in what a reviewer
would be handed for it:

- **Pair alone** — today's two readers, Nova 2 Lite and Ministral 3B, on the crop production cuts.
- **Pair + agent, as built** — the agent runs where production's trigger lets it: a reading the
  parser refused, or the file's geometry (cut off, sideways, a stacked fraction). Never on a
  disagreement: whether one may start the agent is decision D-A1, still open.
- **Pair + agent, every unresolved crop** — the agent on every crop the pair did not confirm,
  disagreements included. The upper bound of what the agent can add, and the data D-A1 needs.

**What a reviewer is handed** is judged by production's own rules. Two readers from different
extractors agreeing is **confirmed** — the admin's decision on #641 is that it stands on its own.
One value with nothing against it is **proposed**, for a person to confirm. Any disagreement goes
to a reviewer with nothing chosen, as does a crop nothing read. Every judgement goes through
`evidence/corroborate.py`, in the order the stage applies it (`judge`), and every fact the agent
decides from comes from the stage's own code (`workflow.stages.region_facts`).

**Wrong readings first.** A wrong confirmed value is the failure that ships; a wrong proposal is one
a reviewer has to catch. Both are reported before anything read right.

**What this leaves out.** The key holds each crop, not the region's other rows: in production the
region's own OCR reading can also start the agent (a number with no unit on it), and can block an
agreement between the two readers. The full-pipeline re-run on the 17-page set (#716) measures that.

Source: issue #757 · Verification: `tests/eval/test_agent_scorecard.py`
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import ValidationError

from eval.gold_set.schema import GoldCase
from evidence.candidate import ObservationCandidate as DomainCandidate
from evidence.canonical import EvidenceStatus
from evidence.coordinates import ImagePoint
from evidence.corroborate import corroborate
from evidence.crop import RenderedPage
from evidence.polygon import Polygon
from extraction.agent.geometry import LabelReach
from extraction.agent.graph import (
    BoundedAgentGraph,
    BoundedRegionContext,
    CandidateTerminal,
    RetryableToolFailure,
)
from extraction.agent.observations import RegionFacts, trigger_reasons
from extraction.agent.outcomes import abstain
from extraction.agent.policy import policy_planner
from extraction.agent.tools import (
    AgentToolbox,
    OcrVerificationArguments,
    ToolCallRecord,
    VlmReadingArguments,
    VlmRole,
)
from extraction.agent.trigger import AmbiguityReason
from extraction.glyph_bands import FractionBarGeometry
from extraction.manifest import PageRecord
from storage.store import ArtifactStore
from units.measurement import Measurement
from units.notation import is_compound
from workflow.reading_agent import ReadingAgentSettings, RegionCrops, crop_box_px

__all__ = [
    "Arm",
    "CropReader",
    "Judgement",
    "KeyCrop",
    "Kind",
    "Outcome",
    "PageGeometry",
    "Reading",
    "ScorecardError",
    "ScorecardPage",
    "build_pages",
    "judge",
    "load_key",
    "render_markdown",
    "results_json",
    "score_crop",
]


class ScorecardError(ValueError):
    """The key or the run's inputs are not usable."""


class Kind(StrEnum):
    """What the person recorded for a crop."""

    SCORED = "scored"
    UNREADABLE = "unreadable"
    """Cut off, or unreadable: any value confirmed here is one nobody vouched for."""

    COMPOUND = "compound"


class Outcome(StrEnum):
    """What a reviewer is handed for a crop."""

    CONFIRMED = "confirmed"
    PROPOSED = "proposed"
    TO_REVIEWER = "to_reviewer"


class Arm(StrEnum):
    PAIR = "pair alone"
    AGENT = "pair + agent, as built"
    AGENT_EVERYWHERE = "pair + agent, every unresolved crop"


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeyCrop:
    """One crop of the human-read key, and what the person recorded for it."""

    crop_id: str
    page_index: int
    crop_px: tuple[int, int, int, int]
    """left, top, right, bottom in the key's own pixel frame (`key_dpi`)."""

    kind: Kind
    expected: Measurement | None
    """The person's reading, for a scored crop; `None` for the others."""

    stratum: str
    rotated: bool
    """The person's tick: the text runs sideways or upside down."""

    cut_off: bool
    """The person noted the label is cut off at the crop's edge."""


_SELF_VERIFIED = ("self-verified", "machine-self-verified", "not per-case human-read")


def load_key(case_dir: Path) -> tuple[KeyCrop, ...]:
    """The key's crops: `crops.csv` for every crop, `answer_key.json` for the scored values."""
    try:
        case = GoldCase.model_validate(
            json.loads((case_dir / "answer_key.json").read_text(encoding="utf-8"))
        )
        with (case_dir / "crops.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, ValueError, ValidationError) as error:
        raise ScorecardError(f"the key in {case_dir} could not be read: {error}") from error
    annotator = case.provenance.annotator.lower()
    if any(marker in annotator for marker in _SELF_VERIFIED):
        raise ScorecardError("the scorecard needs a key a person read; this one says it was not")
    expected = {
        observation.item_id: observation.value for observation in case.ground_truth.observations
    }
    crops: list[KeyCrop] = []
    for row in rows:
        crop_id = row["crop_id"]
        if row.get("unreadable", "").strip():
            kind = Kind.UNREADABLE
        elif row.get("not_a_single_value", "").strip():
            kind = Kind.COMPOUND
        else:
            kind = Kind.SCORED
        if kind is Kind.SCORED and crop_id not in expected:
            raise ScorecardError(f"crop {crop_id} is scored in crops.csv but not in the answer key")
        crops.append(
            KeyCrop(
                crop_id=crop_id,
                page_index=int(row["page"]) - 1,
                crop_px=(
                    int(row["left_px"]),
                    int(row["top_px"]),
                    int(row["right_px"]),
                    int(row["bottom_px"]),
                ),
                kind=kind,
                expected=expected.get(crop_id) if kind is Kind.SCORED else None,
                stratum=row.get("stratum", "") or "-",
                rotated=bool(row.get("rotated", "").strip()) or row.get("stratum") == "rotated",
                cut_off="cut off" in row.get("note", "").lower(),
            )
        )
    if not crops:
        raise ScorecardError(f"the key in {case_dir} has no crops")
    return tuple(crops)


# ---------------------------------------------------------------------------
# Readers and readings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reading:
    """What one reader returned for one crop: text, or why none, and what it cost."""

    extractor: str
    vendor: str
    model_id: str
    raw_text: str | None
    value: Measurement | None
    refusal: str | None
    input_tokens: int
    output_tokens: int
    calls: int
    """Model calls made, retries included (Nova 2 Lite's refused first attempt is one, #702)."""


class CropReader(Protocol):
    """One vision reader, asked about one crop."""

    @property
    def extractor(self) -> str: ...

    @property
    def vendor(self) -> str: ...

    def read(self, png: bytes, *, stacked_label: bool) -> Reading:
        """Read the crop. `stacked_label` is what the geometry says it shows (#735)."""


def _candidate(reading: Reading) -> DomainCandidate:
    return DomainCandidate(
        candidate_id=str(uuid4()),
        extractor=reading.extractor,
        extractor_version=reading.model_id,
        raw_text=reading.raw_text or "",
        parsed_value=reading.value,
        unit_guess=None,
        semantic_guess=None,
        page=0,
        polygon=(ImagePoint(0, 0), ImagePoint(1, 0), ImagePoint(1, 1)),
        confidence=None,
        ambiguity_flags=(),
    )


def _unparsed(reading: Reading) -> bool:
    """A reading production records flagged unparsed, which starts the agent (`_ambiguity_reasons`)."""
    return (
        reading.raw_text is not None and reading.value is None and not is_compound(reading.raw_text)
    )


# ---------------------------------------------------------------------------
# What a reviewer is handed
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Judgement:
    outcome: Outcome
    value: Measurement | None
    agreeing: tuple[str, ...] = ()
    """The extractors whose agreement confirmed the value."""

    vendors: frozenset[str] = frozenset()
    conflict: bool = False


def _agreement(readings: Sequence[Reading]) -> Judgement | None:
    if len(readings) < 2:
        return None
    result = corroborate(tuple(_candidate(reading) for reading in readings))
    if result.lane is None or result.status is EvidenceStatus.CONFLICTING:
        return None
    value = readings[0].value
    return Judgement(
        Outcome.CONFIRMED,
        value,
        agreeing=tuple(sorted({reading.extractor for reading in readings})),
        vendors=frozenset(reading.vendor for reading in readings),
    )


def judge(
    pair: Sequence[Reading], looks: Sequence[Reading] = (), proposal: Reading | None = None
) -> Judgement:
    """What a reviewer is handed, by the rules the stage applies, in its order.

    1. Any two readings with values that disagree → the region is conflicting and goes to a
       reviewer (`_mark_regions_the_agent_contradicted`, and the first pass for the pair alone).
    2. The pair agreeing → confirmed (the first pass; `corroborate` groups every row of the region,
       so a pair reading with no value blocks it).
    3. Otherwise the agent's readings, grouped with the pair's rows that have no lane yet → confirmed
       where they agree (the second pass).
    4. Otherwise one value → proposed: the agent's proposal, else the one value read.
    5. Otherwise nothing is proposed.

    Only readings that returned text are rows; a refusal is recorded as a call, not a reading.
    """
    rows = [reading for reading in pair if reading.raw_text is not None]
    added = [reading for reading in looks if reading.raw_text is not None]
    valued = [reading for reading in (*rows, *added) if reading.value is not None]
    if len(valued) >= 2:
        verdict = corroborate(tuple(_candidate(reading) for reading in valued))
        if verdict.status is EvidenceStatus.CONFLICTING:
            return Judgement(Outcome.TO_REVIEWER, None, conflict=True)
    first = _agreement(rows)
    if first is not None:
        return first
    second = _agreement([*rows, *added]) if added else None
    if second is not None:
        return second
    if proposal is not None and proposal.value is not None:
        return Judgement(Outcome.PROPOSED, proposal.value)
    values = {
        (value.exact, value.unit): value
        for value in (reading.value for reading in valued)
        if value is not None
    }
    if len(values) == 1:
        return Judgement(Outcome.PROPOSED, next(iter(values.values())))
    return Judgement(Outcome.TO_REVIEWER, None)


# ---------------------------------------------------------------------------
# One crop, three ways
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentRun:
    """What the agent did on one crop."""

    steps: tuple[str, ...]
    looks: tuple[Reading, ...]
    proposed: Reading | None
    reason: str
    """What the reader tried, in the plain English a reviewer is shown."""


@dataclass(frozen=True, slots=True)
class CropResult:
    crop: KeyCrop
    pair: tuple[Reading, ...]
    facts: RegionFacts
    reasons: frozenset[AmbiguityReason]
    agent: AgentRun | None
    judgements: Mapping[Arm, Judgement]
    agent_ran: Mapping[Arm, bool]


@dataclass(frozen=True, slots=True)
class PageGeometry:
    """How the vendor's layer is read: the stage's own settings, every one stated."""

    line_minimum_pt: Decimal
    glyph_maximum_pt: Decimal
    glyph_gap_pt: Decimal
    fraction_bar: FractionBarGeometry


class ScorecardPage:
    """One page as the stage has it: its render, its vendor layer, and the stage's own geometry.

    Every fact the agent decides from is computed by `workflow.stages.region_facts`, handed the
    rows it is handed in the stage — the region's reading and the pair's readings of it — built here
    as unsaved rows, so nothing about a crop is decided by a second copy of that code.
    """

    def __init__(
        self,
        data: bytes,
        record: PageRecord,
        *,
        version_id: UUID,
        dpi: int,
        geometry: PageGeometry,
        reach: LabelReach,
    ) -> None:
        from app.models import Page
        from extraction.annotations import read_annotation_layers
        from workflow.stages import page_transform

        self._data = data
        self._record = record
        self._version_id = version_id
        self._reach = reach
        self._renders: dict[int, RenderedPage | str] = {}
        self.page = Page(
            index=record.index,
            content_hash=record.content_hash,
            rotation=record.rotation,
            media_box=None if record.media_box is None else [str(v) for v in record.media_box],
            crop_box=None if record.crop_box is None else [str(v) for v in record.crop_box],
        )
        rendered = self.render(dpi)
        if isinstance(rendered, str):
            raise ScorecardError(f"page {record.index + 1} could not be rendered: {rendered}")
        self.rendered = rendered
        self.transform = page_transform(self.page, dpi)
        self.layers = read_annotation_layers(
            data,
            record.index,
            document_version_id=version_id,
            dpi=dpi,
            line_minimum_pt=geometry.line_minimum_pt,
            glyph_maximum_pt=geometry.glyph_maximum_pt,
            glyph_gap_pt=geometry.glyph_gap_pt,
            fraction_bar=geometry.fraction_bar,
        )

    def render(self, dpi: int) -> RenderedPage | str:
        """The page at `dpi`, vendor's drawing only (#742), exactly as the stage renders it."""
        from extraction.rasterise import PageTooLarge, render_page
        from extraction.reader import UnreadablePdf
        from workflow.stages import MAXIMUM_RENDER_PIXELS

        if dpi not in self._renders:
            try:
                self._renders[dpi] = render_page(
                    self._data,
                    self._record.index,
                    document_version_id=self._version_id,
                    page_content_hash=self._record.content_hash,
                    dpi=dpi,
                    maximum_pixels=MAXIMUM_RENDER_PIXELS,
                    vendor_only=True,
                )
            except (PageTooLarge, UnreadablePdf, ValueError) as error:
                self._renders[dpi] = str(error) or type(error).__name__
        return self._renders[dpi]

    def _row(self, box: tuple[int, int, int, int], reading: Reading | None = None) -> object:
        from app.models import ObservationCandidate as Row

        left, top, right, bottom = box
        value = None if reading is None else reading.value
        return Row(
            document_version_id=self._version_id,
            polygon=[[left, top], [right, top], [right, bottom], [left, bottom]],
            raw_text="" if reading is None else (reading.raw_text or ""),
            value_numerator=None if value is None else value.exact.numerator,
            value_denominator=None if value is None else value.exact.denominator,
            unit=None if value is None else value.unit.value,
        )

    def polygon(self, box: tuple[int, int, int, int]) -> Polygon | None:
        from workflow.stages import stored_polygon

        return stored_polygon(self._row(box), self.rendered)  # type: ignore[arg-type]

    def stacked(self, polygon: Polygon, margin_pt: Decimal) -> bool:
        from workflow.stages import crop_shows_a_stacked_fraction

        return crop_shows_a_stacked_fraction(
            crop_box_px(self.rendered, polygon, margin_pt), self.layers.stacked_fractions
        )

    def facts(
        self, box: tuple[int, int, int, int], readings: Sequence[Reading], margin_pt: Decimal
    ) -> tuple[RegionFacts, Polygon | None]:
        """The stage's facts for the region at `box`, with `readings` as its other rows."""
        from workflow.stages import region_facts

        region = self._row(box)
        others = [self._row(box, reading) for reading in readings if reading.value is not None]
        polygon = self.polygon(box)
        return region_facts(
            region,  # type: ignore[arg-type]
            [region, *others],  # type: ignore[list-item]
            version_id=self._version_id,
            page=self.page,
            rendered=self.rendered,
            polygon=polygon,
            transform=self.transform,
            reach=self._reach,
            page_glyphs=self.layers.glyph_paths,
            stacked=lambda candidate: self.stacked(candidate, margin_pt),
        )


def build_pages(
    data: bytes,
    page_indices: Sequence[int],
    *,
    version_id: UUID,
    dpi: int,
    geometry: PageGeometry,
    reach: LabelReach,
) -> dict[int, ScorecardPage]:
    """The pages a key's crops are on, read once each."""
    from extraction.manifest import build_manifest
    from extraction.reader import read_pages
    from workflow.stages import MINIMUM_VECTOR_CHARACTERS

    manifest = build_manifest(
        read_pages(data), version_id, minimum_vector_characters=MINIMUM_VECTOR_CHARACTERS
    )
    records = {record.index: record for record in manifest.pages}
    return {
        index: ScorecardPage(
            data, records[index], version_id=version_id, dpi=dpi, geometry=geometry, reach=reach
        )
        for index in sorted(set(page_indices))
    }


class _Reads:
    """The agent's two readers on one crop, asked about the crop the run is now on."""

    def __init__(self, crops: RegionCrops, readers: Mapping[VlmRole, CropReader]) -> None:
        self._crops = crops
        self._readers = readers
        self.readings: list[Reading] = []
        self._by_candidate: dict[str, Reading] = {}

    def read(self, arguments: VlmReadingArguments) -> DomainCandidate | RetryableToolFailure:
        reader = self._readers[arguments.role]
        reading = reader.read(
            self._crops.png(arguments.crop_artifact_id),
            stacked_label=self._crops.shows_stacked_fraction,
        )
        self.readings.append(reading)
        if reading.raw_text is None:
            return RetryableToolFailure(f"{reading.extractor} gave no reading: {reading.refusal}")
        candidate = _candidate(reading)
        self._by_candidate[candidate.candidate_id] = reading
        return candidate

    def reading_of(self, candidate: DomainCandidate) -> Reading | None:
        return self._by_candidate.get(candidate.candidate_id)


class _CallLog:
    def __init__(self) -> None:
        self.calls: list[ToolCallRecord] = []

    def record(self, call: ToolCallRecord) -> None:
        self.calls.append(call)


def _no_ocr(arguments: OcrVerificationArguments) -> RetryableToolFailure:
    del arguments
    return RetryableToolFailure("no OCR verification route is wired for the reading agent")


def _region_px(crop: KeyCrop, *, key_dpi: int, dpi: int, margin_pt: Decimal) -> tuple[int, ...]:
    """The region a key crop was cut round, at `dpi`: the crop less the stage's margin each side."""
    scale = Fraction(dpi, key_dpi)
    inset = Fraction(margin_pt) * key_dpi / 72
    left, top, right, bottom = crop.crop_px
    return (
        int((left + inset) * scale),
        int((top + inset) * scale),
        int((right - inset) * scale),
        int((bottom - inset) * scale),
    )


def score_crop(
    crop: KeyCrop,
    page: ScorecardPage,
    *,
    store: ArtifactStore,
    pair: tuple[CropReader, CropReader],
    readers: Mapping[VlmRole, CropReader],
    settings: ReadingAgentSettings,
    key_dpi: int,
    margin_pt: Decimal,
) -> CropResult:
    """Read one crop with the pair, run the agent where either agent arm would, and judge each arm."""
    dpi = page.rendered.dpi
    region = _region_px(crop, key_dpi=key_dpi, dpi=dpi, margin_pt=margin_pt)
    if region[2] <= region[0] or region[3] <= region[1]:
        raise ScorecardError(f"crop {crop.crop_id} is smaller than the margin it was cut with")
    box = (region[0], region[1], region[2], region[3])
    polygon = page.polygon(box)
    if polygon is None:
        raise ScorecardError(f"crop {crop.crop_id} does not lie on its page's render")

    # The geometry is the region's, whatever was read in it.
    facts, whole_run = page.facts(box, (), margin_pt)
    crops = RegionCrops(
        store=store,
        render=page.render,
        base=page.rendered,
        polygon=polygon,
        margin_pt=margin_pt,
        sharper_dpi=settings.sharper_dpi,
        whole_run=whole_run,
        rotation_degrees=facts.rotation_degrees,
        stacked=lambda candidate: page.stacked(candidate, margin_pt),
    )
    first = crops.first()
    if first is None:
        raise ScorecardError(f"crop {crop.crop_id} could not be cut")
    png = crops.png(first)
    readings = tuple(
        reader.read(png, stacked_label=crops.shows_stacked_fraction) for reader in pair
    )
    facts, _ = page.facts(box, readings, margin_pt)

    reasons = trigger_reasons(facts) | (
        {AmbiguityReason.UNREADABLE_TEXT} if any(_unparsed(r) for r in readings) else set()
    )
    alone = judge(readings)
    as_built = not alone.conflict and bool(reasons)
    everywhere = alone.outcome is not Outcome.CONFIRMED or bool(trigger_reasons(facts))

    agent: AgentRun | None = None
    if as_built or everywhere:
        reads = _Reads(crops, readers)
        log = _CallLog()
        graph = BoundedAgentGraph(
            limits=settings.limits,
            toolbox=AgentToolbox(
                refine_crop=crops.refine,
                request_ocr_verification=_no_ocr,
                request_vlm_reading=reads.read,
                abstain=abstain,
                recorder=log,
            ),
        )
        context = BoundedRegionContext(
            region_id=crop.crop_id, crop_artifact_id=first, nearby_text=(), nearby_geometry_refs=()
        )
        terminal = graph.run(context, policy_planner(facts, settings.limits))
        proposed = (
            reads.reading_of(terminal.candidate)
            if isinstance(terminal, CandidateTerminal)
            else None
        )
        agent = AgentRun(
            steps=tuple(call.call_id.split(":", 2)[-1] for call in log.calls),
            looks=tuple(reads.readings),
            proposed=proposed,
            reason=(
                "proposed" if proposed is not None else terminal.abstention.reason  # type: ignore[union-attr]
            ),
        )

    def with_agent(ran: bool) -> Judgement:
        if not ran or agent is None:
            return alone
        return judge(readings, agent.looks, agent.proposed)

    return CropResult(
        crop=crop,
        pair=readings,
        facts=facts,
        reasons=frozenset(reasons),
        agent=agent,
        judgements={
            Arm.PAIR: alone,
            Arm.AGENT: with_agent(as_built),
            Arm.AGENT_EVERYWHERE: with_agent(everywhere),
        },
        agent_ran={Arm.PAIR: False, Arm.AGENT: as_built, Arm.AGENT_EVERYWHERE: everywhere},
    )


# ---------------------------------------------------------------------------
# The scorecard
# ---------------------------------------------------------------------------


def _right(judgement: Judgement, crop: KeyCrop) -> bool | None:
    """Whether the value handed over is the person's; `None` where nothing was handed over."""
    if judgement.value is None:
        return None
    if crop.expected is None:
        return False
    return (judgement.value.exact, judgement.value.unit) == (
        crop.expected.exact,
        crop.expected.unit,
    )


@dataclass
class ArmTally:
    confirmed_wrong: int = 0
    proposed_wrong: int = 0
    confirmed_right: int = 0
    proposed_right: int = 0
    to_reviewer: int = 0
    confirmed_unvouched: int = 0
    """Crops a person could not read, or marked compound, on which a value was confirmed."""

    proposed_unvouched: int = 0
    same_vendor_confirmations: int = 0
    by_stratum: Counter[tuple[str, str]] = field(default_factory=Counter)
    calls: int = 0
    input_tokens: Counter[str] = field(default_factory=Counter)
    output_tokens: Counter[str] = field(default_factory=Counter)
    agent_runs: int = 0


def tally(results: Sequence[CropResult], arm: Arm) -> ArmTally:
    counts = ArmTally()
    for result in results:
        judgement = result.judgements[arm]
        right = _right(judgement, result.crop)
        scored = result.crop.kind is Kind.SCORED
        if judgement.outcome is Outcome.CONFIRMED and len(judgement.vendors) < 2:
            counts.same_vendor_confirmations += 1
        if not scored:
            if judgement.outcome is Outcome.CONFIRMED:
                counts.confirmed_unvouched += 1
            elif judgement.outcome is Outcome.PROPOSED:
                counts.proposed_unvouched += 1
            else:
                counts.to_reviewer += 1
        elif judgement.outcome is Outcome.CONFIRMED:
            if right:
                counts.confirmed_right += 1
            else:
                counts.confirmed_wrong += 1
        elif judgement.outcome is Outcome.PROPOSED:
            if right:
                counts.proposed_right += 1
            else:
                counts.proposed_wrong += 1
        else:
            counts.to_reviewer += 1
        if scored:
            label = (
                "reviewer"
                if judgement.outcome is Outcome.TO_REVIEWER
                else ("right" if right else "wrong")
            )
            counts.by_stratum[(result.crop.stratum, label)] += 1
        readings = list(result.pair)
        if result.agent_ran[arm] and result.agent is not None:
            counts.agent_runs += 1
            readings += list(result.agent.looks)
        for reading in readings:
            counts.calls += reading.calls
            counts.input_tokens[reading.model_id] += reading.input_tokens
            counts.output_tokens[reading.model_id] += reading.output_tokens
    return counts


def cost_usd(counts: ArmTally, rates: Callable[[str, int, int], int]) -> Decimal:
    """The arm's model cost, from recorded tokens and the shipped price file (in millionths)."""
    micros = sum(
        rates(model, counts.input_tokens[model], counts.output_tokens[model])
        for model in set(counts.input_tokens) | set(counts.output_tokens)
    )
    return Decimal(micros) / Decimal(1_000_000)


def render_markdown(
    results: Sequence[CropResult], *, rates: Callable[[str, int, int], int], header: str
) -> str:
    """The scorecard: wrong readings first, then right, then what went to a reviewer, then cost."""
    scored = sum(result.crop.kind is Kind.SCORED for result in results)
    unvouched = len(results) - scored
    tallies = {arm: tally(results, arm) for arm in Arm}
    lines = [
        header,
        "",
        (
            f"{len(results)} crops: {scored} scored, {unvouched} a person could not read or "
            "marked compound."
        ),
        "",
        (
            "| Arm | **Confirmed but wrong** | **Proposed but wrong** | Confirmed on a crop nobody "
            "could read | Confirmed right | Proposed right | To a reviewer, nothing chosen |"
        ),
        "|---|---|---|---|---|---|---|",
    ]
    for arm, counts in tallies.items():
        lines.append(
            f"| {arm.value} | **{counts.confirmed_wrong}** | **{counts.proposed_wrong}** | "
            f"{counts.confirmed_unvouched} | {counts.confirmed_right} | {counts.proposed_right} | "
            f"{counts.to_reviewer} |"
        )
    lines += [
        "",
        (
            "| Arm | Agent runs | Model calls | $ per crop | Confirmations by two readers of one "
            "vendor |"
        ),
        "|---|---|---|---|---|",
    ]
    for arm, counts in tallies.items():
        per_crop = cost_usd(counts, rates) / Decimal(len(results))
        lines.append(
            f"| {arm.value} | {counts.agent_runs} | {counts.calls} | "
            f"{per_crop.quantize(Decimal('0.00001'))} | {counts.same_vendor_confirmations} |"
        )
    strata = sorted({result.crop.stratum for result in results if result.crop.kind is Kind.SCORED})
    lines += ["", "**Scored crops by kind** (right / wrong / to a reviewer):", ""]
    lines.append("| Kind | " + " | ".join(arm.value for arm in Arm) + " |")
    lines.append("|---|" + "---|" * len(Arm))
    for stratum in strata:
        cells = []
        for arm in Arm:
            kinds = tallies[arm].by_stratum
            cells.append(
                f"{kinds[(stratum, 'right')]} / {kinds[(stratum, 'wrong')]} / "
                f"{kinds[(stratum, 'reviewer')]}"
            )
        lines.append(f"| {stratum} | " + " | ".join(cells) + " |")
    geometry = Counter[str]()
    for result in results:
        geometry["cut, person"] += result.crop.cut_off
        geometry["cut, geometry"] += result.facts.cut_at_edge
        geometry["cut, both"] += result.crop.cut_off and result.facts.cut_at_edge
        geometry["sideways, person"] += result.crop.rotated
        geometry["sideways, geometry"] += result.facts.rotation_degrees != 0
        geometry["sideways, both"] += result.crop.rotated and result.facts.rotation_degrees != 0
        geometry["stacked, geometry"] += result.facts.stacked_fraction
    lines += [
        "",
        "**What the drawing's own lines saw** (the agent's triggers, against the person's notes):",
        "",
        (
            f"- Cut off: the person noted {geometry['cut, person']}, the geometry found "
            f"{geometry['cut, geometry']}, both on {geometry['cut, both']}."
        ),
        (
            f"- Sideways: the person ticked {geometry['sideways, person']}, the geometry found "
            f"{geometry['sideways, geometry']}, both on {geometry['sideways, both']}."
        ),
        f"- Stacked fraction: the geometry found {geometry['stacked, geometry']}.",
    ]
    return "\n".join(lines) + "\n"


def results_json(results: Sequence[CropResult]) -> list[dict[str, object]]:
    """Every crop's readings and judgements, for the local working file (never committed)."""

    def value(measurement: Measurement | None) -> str | None:
        return None if measurement is None else f"{measurement.exact} {measurement.unit.value}"

    return [
        {
            "crop": result.crop.crop_id,
            "kind": result.crop.kind.value,
            "stratum": result.crop.stratum,
            "expected": value(result.crop.expected),
            "pair": [[r.extractor, r.raw_text, r.refusal] for r in result.pair],
            "facts": {
                "cut": result.facts.cut_at_edge,
                "rotation": result.facts.rotation_degrees,
                "stacked": result.facts.stacked_fraction,
            },
            "reasons": sorted(reason.value for reason in result.reasons),
            "agent": (
                None
                if result.agent is None
                else {
                    "steps": list(result.agent.steps),
                    "looks": [[r.extractor, r.raw_text, r.refusal] for r in result.agent.looks],
                    "outcome": result.agent.reason,
                }
            ),
            "judgements": {
                arm.value: [judgement.outcome.value, value(judgement.value), judgement.conflict]
                for arm, judgement in result.judgements.items()
            },
        }
        for result in results
    ]


# ---------------------------------------------------------------------------
# The real readers
# ---------------------------------------------------------------------------

_VENDORS: Mapping[str, str] = {
    "amazon.": "Amazon",
    "mistral.": "Mistral",
    "anthropic.": "Anthropic",
}


def vendor_of(model_id: str) -> str:
    """Who made a model, from its Bedrock id — the independence #641 found agreement needs."""
    bare = model_id.removeprefix("us.").removeprefix("global.")
    for prefix, vendor in _VENDORS.items():
        if bare.startswith(prefix):
            return vendor
    raise ScorecardError(f"no vendor is known for {model_id!r}")


class BedrockCropReader:
    """One production vision reader, through the production adapter, paced under its call quota.

    **Paced, and a throttle is waited out, never scored.** A reading refused because the account
    ran out of calls a minute is not a reading the model refused, and counting it as one would
    score the quota (#716). Each temporary failure is retried after a wait; the calls it cost are
    still counted.
    """

    def __init__(
        self,
        config: object,
        *,
        calls_per_minute: int,
        waits_seconds: Sequence[float] = (30, 60, 120),
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        import time

        from extraction.models.nova import NovaAdapter

        self._config = config
        self._sink = _Invocations()
        self._adapter = NovaAdapter.from_environment(config, self._sink)  # type: ignore[arg-type]
        self._interval = 60 / calls_per_minute
        self._waits = tuple(waits_seconds)
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._last: float | None = None

    @property
    def extractor(self) -> str:
        return str(self._config.extractor)  # type: ignore[attr-defined]

    @property
    def model_id(self) -> str:
        return str(self._config.model_id)  # type: ignore[attr-defined]

    @property
    def vendor(self) -> str:
        return vendor_of(self.model_id)

    def _pace(self) -> None:
        if self._last is not None:
            wait = self._interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def read(self, png: bytes, *, stacked_label: bool) -> Reading:
        from extraction.agent.observations import value_of
        from extraction.models.context import AssembledContext
        from extraction.models.nova import (
            NovaAdapterError,
            NovaRequest,
            NovaRetryExhaustedError,
            NovaTimeoutError,
        )
        from workflow.stages import VISION_CONTEXT_BOUND_PT

        before = len(self._sink.invocations)
        refusal: str | None = None
        candidate: DomainCandidate | None = None
        for wait in (*self._waits, None):
            self._pace()
            try:
                candidate = self._adapter.extract(
                    NovaRequest(
                        candidate_id=str(uuid4()),
                        page=0,
                        crop=png,
                        image_format="png",
                        context=AssembledContext(nearby_text=(), nearby_geometry=()),
                        bound_pt=VISION_CONTEXT_BOUND_PT,
                        stacked_label=stacked_label,
                    )
                )
                refusal = None
                break
            except (NovaRetryExhaustedError, NovaTimeoutError) as error:
                refusal = f"{type(error).__name__}: {str(error)[:160]}"
                if wait is None:
                    break
                self._sleep(wait)
            except NovaAdapterError as error:
                refusal = f"{type(error).__name__}: {str(error)[:160]}"
                break
        attempts = self._sink.invocations[before:]
        return Reading(
            extractor=self.extractor,
            vendor=self.vendor,
            model_id=self.model_id,
            raw_text=None if candidate is None else candidate.raw_text,
            value=None if candidate is None else value_of(candidate),
            refusal=refusal,
            input_tokens=sum(attempt.input_tokens for attempt in attempts),
            output_tokens=sum(attempt.output_tokens for attempt in attempts),
            calls=len(attempts),
        )


class _Invocations:
    def __init__(self) -> None:
        self.invocations: list[Any] = []

    def record(self, invocation: Any) -> None:
        self.invocations.append(invocation)

    def record_rejection(self, rejection: Any) -> None:
        del rejection
