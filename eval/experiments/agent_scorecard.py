"""The reading agent's scorecard on the human-read key: the reader pair alone, and with the agent (#757).

**What is compared.** Every crop of the key is read three ways, and each way ends in what a reviewer
would be handed for it:

- **Pair alone** — the two vision readers named for the run, on the crop production cuts (since
  #907 Qwen3-VL and Nova 2 Lite as the trial measured it; before, Nova 2 Lite and Ministral 3B).
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

**Each pair reader is shown its own picture (#907)**, made by the stage's own code
(`workflow.reader_pictures`): the crop as cut, or the same page area rendered at the stated sharper
dpi and turned upright by the drawing's own facts — never by the key's sideways tick. Each reader is
also scored alone — right, wrong, or no value — and a run stops before any call once its stated
spending cap is reached (`SpendCap`).

Source: issues #757, #907 · Verification: `tests/eval/test_agent_scorecard.py`
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

from eval.experiments.model_bakeoff import ModelBakeoffError, key_frame, key_polygon_dpi
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
from extraction.glyph_bands import FractionBarGeometry, FractionLayout
from extraction.manifest import PageRecord
from extraction.models.nova import ReaderPicture
from storage.store import ArtifactStore
from units.measurement import Measurement
from units.notation import is_compound
from workflow.reader_pictures import PictureSettings, PrintedRun, label_turn, reader_picture
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
    "SpendCap",
    "SpendCapReached",
    "build_pages",
    "judge",
    "key_frame_dpi",
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

    NOT_A_DIMENSION = "not_a_dimension"
    """The person marked the crop as holding no dimension at all (#867): a symbol, a word."""


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

    stacked: bool = False
    """The key marks the label as a stacked fraction: a `stacked` tick, or a crop picked into the
    `stacked_fraction` group. The gate replay scores the stacked-fraction finder against it (#851)."""

    dual_unit: bool = False
    """The person ticked `dual_unit`: millimetres with their inches (#867)."""

    gv_value_seen: str | None = None
    """GV's own number as the person saw it in the crop, as typed (#867); never the vendor's."""


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
        # A cut-off crop is not scored, like one nobody could read (`build` refuses a value on one).
        cut_off = bool(row.get("cut_off", "").strip()) or "cut off" in row.get("note", "").lower()
        if row.get("unreadable", "").strip() or row.get("cut_off", "").strip():
            kind = Kind.UNREADABLE
        elif row.get("not_a_dimension", "").strip():
            kind = Kind.NOT_A_DIMENSION
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
                cut_off=cut_off,
                stacked=bool(row.get("stacked", "").strip())
                or row.get("stratum") == "stacked_fraction",
                dual_unit=bool(row.get("dual_unit", "").strip()),
                gv_value_seen=row.get("gv_value_seen", "").strip() or None,
            )
        )
    if not crops:
        raise ScorecardError(f"the key in {case_dir} has no crops")
    return tuple(crops)


def key_frame_dpi(case_dir: Path, *, key_dpi: int | None, margin_pt: Decimal) -> int:
    """The pixel frame the key's crops are in, refused where the scorecard would misplace them (#835).

    The frame comes from `model_bakeoff.key_polygon_dpi`, the rule every loader of a key applies.
    **The margin is checked as well**, because `score_crop` finds each region by taking `margin_pt`
    off the crop: a key cut with a different margin would hand the agent a region that is not the
    one the crop was cut round. A key that records no frame records no margin either, and is read
    as cut with `margin_pt`, as it always was.
    """
    try:
        dpi = key_polygon_dpi(case_dir, polygon_dpi=key_dpi)
        frame = key_frame(case_dir)
    except ModelBakeoffError as error:
        raise ScorecardError(str(error)) from error
    if frame is not None and frame.margin_pt != margin_pt:
        raise ScorecardError(
            f"the key in {case_dir} was cut with a {frame.margin_pt} pt margin, and the scorecard "
            f"takes {margin_pt} pt off each crop to find its region. Recut the key with the stage's "
            "margin; scored as it is, each crop's region would be the wrong size."
        )
    return dpi


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

    @property
    def picture(self) -> ReaderPicture:
        """What this reader is shown (#907): its definition's measured picture."""

    def read(
        self, png: bytes, *, stacked_label: bool, stacked_layouts: tuple[FractionLayout, ...]
    ) -> Reading:
        """Read the crop. `stacked_label` is what the geometry says it shows (#735), and
        `stacked_layouts` where the parts of each stacked label in it were drawn (#834)."""


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

    **Not applied here: the GV-mark guard (#901).** The stage does not confirm an agreement whose
    crop shows markup drawn in colour; this judgement has no crop facts and does, so a scorecard
    counts such an agreement as confirmed. `scripts/gate_replay.py` replays these files with the
    guard, by the stage's own function.
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
    turn: int = 0
    """How far the label was turned for a reader shown it upright (#907), by the drawing's facts."""


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
        self.printed = self._printed(dpi)

    def _printed(self, dpi: int) -> tuple[PrintedRun, ...]:
        """The page's own text and its pasted drawings' font text, as the stage reads both (#907)."""
        from extraction.reader import UnreadablePdf, read_page_contents
        from extraction.stamp_text import read_stamp_text
        from workflow.reader_pictures import printed_runs

        texts: list[object] = []
        try:
            texts += read_page_contents(
                self._data, self._record.index, document_version_id=self._version_id, dpi=dpi
            ).texts
        except UnreadablePdf:
            pass
        if self.layers.vendor_stamps:
            try:
                texts += read_stamp_text(
                    self._data, self._record.index, document_version_id=self._version_id, dpi=dpi
                ).contents.texts
            except UnreadablePdf:
                pass
        return printed_runs(texts)  # type: ignore[arg-type]

    def render_region(self, box: tuple[int, int, int, int], dpi: int) -> bytes:
        """One area of the page at `dpi`, vendor's drawing only, as the stage renders it (#907)."""
        from extraction.rasterise import render_region
        from workflow.stages import MAXIMUM_RENDER_PIXELS

        return render_region(
            self._data,
            self._record.index,
            box_px=box,
            dpi=dpi,
            maximum_pixels=MAXIMUM_RENDER_PIXELS,
            vendor_only=True,
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

    def layouts(self, polygon: Polygon, margin_pt: Decimal) -> tuple[FractionLayout, ...]:
        from workflow.stages import stacked_layouts_shown

        return stacked_layouts_shown(
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
            stacked_layouts=self._crops.stacked_layouts,
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
    pictures: PictureSettings | None,
    run_agent: bool = True,
) -> CropResult:
    """Read one crop with the pair, run the agent where either agent arm would, and judge each arm.

    Each pair reader is shown its own picture (#907); `pictures` is how a sharper one is made, and
    `None` where no pair reader is shown one. `run_agent` `False` scores the pair alone: both agent
    arms are then the pair's judgement, and no agent call is made.
    """
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
        layouts=lambda candidate: page.layouts(candidate, margin_pt),
    )
    first = crops.first()
    if first is None:
        raise ScorecardError(f"crop {crop.crop_id} could not be cut")
    png = crops.png(first)
    crop_box = crop_box_px(page.rendered, polygon, margin_pt)
    turn = label_turn(box, page.printed, geometry_degrees=facts.rotation_degrees)
    readings = tuple(
        reader.read(
            reader_picture(
                reader.picture,
                as_cut=png,
                crop_box=crop_box,
                base_dpi=dpi,
                settings=pictures,
                turn=turn,
                render=page.render_region,
            ),
            stacked_label=crops.shows_stacked_fraction,
            stacked_layouts=crops.stacked_layouts,
        )
        for reader in pair
    )
    facts, _ = page.facts(box, readings, margin_pt)

    reasons = trigger_reasons(facts) | (
        {AmbiguityReason.UNREADABLE_TEXT} if any(_unparsed(r) for r in readings) else set()
    )
    alone = judge(readings)
    as_built = run_agent and not alone.conflict and bool(reasons)
    everywhere = run_agent and (
        alone.outcome is not Outcome.CONFIRMED or bool(trigger_reasons(facts))
    )

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
        turn=turn,
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


@dataclass
class ReaderTally:
    """One pair reader alone, on the crops it was shown (#907)."""

    right: int = 0
    wrong: int = 0
    no_value: int = 0
    """A scored crop it gave no value for: it abstained, was refused, or wrote what does not parse."""

    valued_unvouched: int = 0
    """Crops a person could not vouch for on which it gave a value anyway."""


def reader_tallies(results: Sequence[CropResult]) -> dict[str, ReaderTally]:
    """Each pair reader's own right, wrong and no-value counts, by extractor, in pair order."""
    tallies: dict[str, ReaderTally] = {}
    for result in results:
        for reading in result.pair:
            counts = tallies.setdefault(reading.extractor, ReaderTally())
            if result.crop.kind is not Kind.SCORED:
                counts.valued_unvouched += reading.value is not None
            elif reading.value is None:
                counts.no_value += 1
            elif result.crop.expected is not None and (
                reading.value.exact,
                reading.value.unit,
            ) == (result.crop.expected.exact, result.crop.expected.unit):
                counts.right += 1
            else:
                counts.wrong += 1
    return tallies


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
            f"{len(results)} crops: {scored} scored, {unvouched} a person could not read, found "
            "cut off, marked compound or marked not a dimension."
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
    lines += [
        "",
        "**Each pair reader alone** (scored crops, then crops nobody could vouch for):",
        "",
        "| Reader | Right | Wrong | No value | Gave a value where nobody could vouch |",
        "|---|---|---|---|---|",
    ]
    for extractor, alone in reader_tallies(results).items():
        lines.append(
            f"| {extractor} | {alone.right} | **{alone.wrong}** | {alone.no_value} | "
            f"{alone.valued_unvouched} |"
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
        geometry["stacked, person"] += result.crop.stacked
        geometry["dual, person"] += result.crop.dual_unit
        geometry["gv seen, person"] += result.crop.gv_value_seen is not None
        geometry["turned"] += result.turn != 0
        geometry["turned, person"] += result.crop.rotated and result.turn != 0
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
        (
            f"- Stacked fraction: the person marked {geometry['stacked, person']}, the geometry "
            f"found {geometry['stacked, geometry']}."
        ),
        f"- Dual unit: the person ticked {geometry['dual, person']}.",
        f"- GV's own number in the crop: the person saw it in {geometry['gv seen, person']}.",
        (
            f"- Turned upright for a reader shown the label upright (#907), by the drawing's own "
            f"text and paths: {geometry['turned']}, of which the person ticked "
            f"{geometry['turned, person']} sideways."
        ),
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
                "turn": result.turn,
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
    "qwen.": "Qwen",
}


class SpendCapReached(ScorecardError):
    """The run's stated spending cap was reached: no further call is made (#907)."""


@dataclass
class SpendCap:
    """What a scorecard run may spend on model calls, in millionths of a dollar (#907).

    Checked before every call and added to after it, from each attempt's recorded tokens and the
    stated price file. A model the file does not price cannot be counted, so it is refused before
    the run rather than spent unseen.
    """

    cap_micros: int
    price: Callable[[str, int, int], int | None]
    spent_micros: int = 0
    calls: int = 0

    tripped: bool = False
    """Whether a call was refused for the cap — inside the agent's graph too, which turns any
    tool's failure into an abstention, so the run must look here to know its last crop was cut."""

    def check(self) -> None:
        if self.spent_micros >= self.cap_micros:
            self.tripped = True
            raise SpendCapReached(
                f"the run spent ${Decimal(self.spent_micros) / 1_000_000} of its "
                f"${Decimal(self.cap_micros) / 1_000_000} cap, so no further call is made"
            )

    def add(self, model_id: str, input_tokens: int, output_tokens: int) -> None:
        micros = self.price(model_id, input_tokens, output_tokens)
        if micros is None:
            raise ScorecardError(f"{model_id} has no price, so what it cost cannot be counted")
        self.spent_micros += micros
        self.calls += 1


class Pacer:
    """Calls to one model, spaced under its quota — shared by every reader of that model."""

    def __init__(
        self,
        calls_per_minute: int,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        import time

        self._interval = 60 / calls_per_minute
        self._clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            wait = self._interval - (self._clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self._clock()


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
        pacer: Pacer | None = None,
        cap: SpendCap | None = None,
    ) -> None:
        from extraction.models.nova import NovaAdapter

        self._config = config
        self._sink = _Invocations()
        self._adapter = NovaAdapter.from_environment(config, self._sink)  # type: ignore[arg-type]
        self._pacer = pacer or Pacer(calls_per_minute, clock=clock, sleep=sleep)
        self._waits = tuple(waits_seconds)
        self._sleep = sleep or self._pacer.sleep
        self._cap = cap

    @property
    def extractor(self) -> str:
        return str(self._config.extractor)  # type: ignore[attr-defined]

    @property
    def model_id(self) -> str:
        return str(self._config.model_id)  # type: ignore[attr-defined]

    @property
    def vendor(self) -> str:
        return vendor_of(self.model_id)

    @property
    def picture(self) -> ReaderPicture:
        picture: ReaderPicture = self._config.picture  # type: ignore[attr-defined]
        return picture

    def _pace(self) -> None:
        if self._cap is not None:
            self._cap.check()
        self._pacer.wait()

    def read(
        self, png: bytes, *, stacked_label: bool, stacked_layouts: tuple[FractionLayout, ...]
    ) -> Reading:
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
                        stacked_layouts=stacked_layouts,
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
        if self._cap is not None:
            for attempt in attempts:
                self._cap.add(
                    getattr(attempt, "model_id", self.model_id),
                    attempt.input_tokens,
                    attempt.output_tokens,
                )
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
