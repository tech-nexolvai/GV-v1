"""The two-reader agreement gate, replayed against a person's answers (#851; #728 step 6, #726).

**What it counts.** Each crop of a human-read key is matched to stored readings of it — a run's
database rows, or the reader pair's in an agent scorecard's working file — and the gate judges those
readings again. Where the gate agrees, the agreed value is held against the person's: **agreed and
right**, **agreed and wrong**, or **agreed on a crop the person could not vouch for** — cut off,
unreadable, two dimensions and an operator, or no dimension at all (#867). The bake-off's pair score
(`model_bakeoff.PairwiseScore`) never counts the middle one, and it is the failure the gate exists
to prevent.

**The gate is called as it is.** Every judgement is `evidence.corroborate.corroborate`, asked the
way production asks it: a non-model route's `mm [in]` label alone, as `app.evidence.record` asks
about it, and every other reading of one region together, as
`workflow.stages._apply_cross_route_corroboration` groups them. The stage asks a second time once
the reading agent has added its looks; here a region's readings, the agent's among them, are judged
in the one call. A second-reader agreement is what counts, because it is the only lane
`evidence/gate.py` seals.

**Candidate guards are measured here and wired in nowhere.** Each is a pure pre-filter, decided
from the region's geometry and readings before the gate is asked. Four can only hold an agreement
back; the fifth asks what a label's own millimetres would add if they could confirm its inches, on
the file's own text only (#691). The admin decided on 2026-10-03, before any of this was counted,
that a kind of reading whose readers agree on a wrong value always goes to a person.

**Zero wrong is not safe.** With no wrong agreement among n, the true rate is only known to be
below about 3/n, with about 95% confidence (the rule of three): nine agreements bound it below a
third, and no lower. Every zero is reported beside its bound.

**Readings are joined to a crop by where they are.** A stored region belongs to a key crop when its
centre lies inside the crop the person read, and its agreement is held against that crop's value. A
region joined that should not have been can only add agreements, counted wrong unless the two labels
say the same; a region the rule leaves out is not counted at all.

Source: issue #851 · Verification: `tests/eval/test_gate_replay.py`
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from eval.experiments.agent_scorecard import KeyCrop, Kind
from eval.experiments.model_bakeoff import KeyFrame, ModelBakeoffError, key_frame, key_polygon_dpi
from evidence.candidate import ObservationCandidate as DomainCandidate
from evidence.canonical import CorroborationLane, EvidenceStatus
from evidence.coordinates import ImagePoint
from evidence.corroborate import corroborate, independence_key
from extraction.agent.observations import value_of
from units.dual import DualDimension, DualDimensionParseError, parse_dual
from units.measurement import Measurement
from workflow.stages import EXACT_TEXT_EXTRACTORS

__all__ = [
    "Agreement",
    "Box",
    "Facts",
    "Guard",
    "GuardTally",
    "Outcome",
    "Reading",
    "Region",
    "Replay",
    "ReplayError",
    "StackedCatch",
    "StoredRow",
    "frame_of",
    "gate",
    "group_rows",
    "held_back",
    "join",
    "judge",
    "outcome",
    "pixels",
    "promoted_numerator",
    "region_of",
    "regions_from_scorecard",
    "render_markdown",
    "results_json",
    "rule_of_three",
    "stacked_catch",
    "tally",
]

#: How many readers a sideways label needs before an agreement on it counts (the fourth guard).
THIRD_READER: Final = 3

_POINTS_PER_INCH: Final = 72

Box = tuple[Fraction, Fraction, Fraction, Fraction]
"""left, top, right, bottom, in points from the page's top-left corner — the frame every rendering
of a page shares, whatever its dpi."""


class ReplayError(ValueError):
    """The key, the frame or a source of readings cannot be replayed as given."""


# ---------------------------------------------------------------------------
# Where a crop is
# ---------------------------------------------------------------------------


def frame_of(case_dir: Path, *, polygon_dpi: int | None, margin_pt: Decimal | None) -> KeyFrame:
    """The frame a key's crops are in: the one the key records, or the one its caller names (#835).

    The dpi follows `model_bakeoff.key_polygon_dpi`, the rule every loader of a key applies. **The
    margin is needed as well**, because a crop is joined to readings by the region it was cut round:
    a key that records no frame records no margin either, so both must then be named, and a margin
    named for a key that records a different one is refused.
    """
    try:
        dpi = key_polygon_dpi(case_dir, polygon_dpi=polygon_dpi)
        recorded = key_frame(case_dir)
    except ModelBakeoffError as error:
        raise ReplayError(str(error)) from error
    if recorded is None:
        if margin_pt is None:
            raise ReplayError(
                f"the key in {case_dir} does not record how much page its polygons hold round "
                "their regions. Name it (--key-margin-pt) only if you know it: 0 for a key cut "
                "round the region itself, production's vision margin for one cut as production "
                "cuts a crop."
            )
        return KeyFrame(polygon_dpi=dpi, margin_pt=margin_pt)
    if margin_pt is not None and margin_pt != recorded.margin_pt:
        raise ReplayError(
            f"the key in {case_dir} records a {recorded.margin_pt} pt margin, and {margin_pt} was "
            "named. Leave it out to use the key's own."
        )
    return recorded


def _points(box_px: tuple[int, int, int, int], dpi: int) -> Box:
    left, top, right, bottom = box_px
    return (
        Fraction(left * _POINTS_PER_INCH, dpi),
        Fraction(top * _POINTS_PER_INCH, dpi),
        Fraction(right * _POINTS_PER_INCH, dpi),
        Fraction(bottom * _POINTS_PER_INCH, dpi),
    )


def _grown(box: Box, by_pt: Fraction) -> Box:
    return (box[0] - by_pt, box[1] - by_pt, box[2] + by_pt, box[3] + by_pt)


def region_of(crop: KeyCrop, frame: KeyFrame) -> Box:
    """The region a key crop was cut round: its polygon less the key's margin on each side."""
    return _grown(_points(crop.crop_px, frame.polygon_dpi), -Fraction(frame.margin_pt))


def pixels(box: Box, dpi: int) -> tuple[int, int, int, int]:
    """A box in a page's pixels at `dpi`, each edge rounded down as the agent scorecard rounds."""
    left, top, right, bottom = (int(edge * dpi / _POINTS_PER_INCH) for edge in box)
    return left, top, right, bottom


# ---------------------------------------------------------------------------
# Readings and regions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reading:
    """One reader's text for one region, and the value production stores for it."""

    extractor: str
    extractor_version: str
    """For a model reader, its model id: `independence_key` reads the vendor from it (#775)."""

    raw_text: str
    value: Measurement | None
    flags: tuple[str, ...] = ()
    """The ambiguity flags it was stored with. `corroborate` never agrees a stacked one (#726)."""


@dataclass(frozen=True, slots=True)
class Region:
    """The readings of one region, grouped as the stage groups them: one page, one polygon."""

    handle: str
    page_index: int
    box_px: tuple[int, int, int, int]
    """left, top, right, bottom of its polygon, in the pixels it was recorded at (`dpi`)."""

    dpi: int
    readings: tuple[Reading, ...]

    @property
    def box(self) -> Box:
        return _points(self.box_px, self.dpi)


@dataclass(frozen=True, slots=True)
class Facts:
    """What the file's own geometry says about a region: all a guard reads but the readings."""

    cut_at_edge: bool
    """The crop production cuts round it cuts a label (`RegionFacts.cut_at_edge`)."""

    sideways: bool
    """The label runs up the page (`RegionFacts.rotation_degrees` is not 0)."""

    stacked: bool
    """The crop shows a stacked fraction (`RegionFacts.stacked_fraction`)."""

    gv_mark: bool
    """The crop shows a GV mark. Production's crops leave GV's own notes out (#742), so what is left
    to see is markup baked into the vendor's drawing, drawn in colour."""


@dataclass(frozen=True, slots=True)
class StoredRow:
    """One stored reading, where it was recorded: the database's `observation_candidates` row."""

    page_index: int
    polygon: tuple[tuple[int, int], ...]
    dpi: int
    reading: Reading


def group_rows(rows: Sequence[StoredRow]) -> tuple[Region, ...]:
    """One region per page and recorded polygon — the key `_apply_cross_route_corroboration` uses.

    Every row of a region is a reading of it, whichever route wrote it, and they are judged
    together: a reading with no value blocks an agreement here, as in the stage's first pass.
    """
    groups: dict[tuple[int, tuple[tuple[int, int], ...]], list[StoredRow]] = {}
    for row in rows:
        if not row.polygon:
            raise ReplayError("a stored reading has no polygon, so it is in no region")
        groups.setdefault((row.page_index, row.polygon), []).append(row)
    regions: list[Region] = []
    for number, ((page_index, polygon), members) in enumerate(sorted(groups.items())):
        resolutions = {member.dpi for member in members}
        if len(resolutions) != 1:
            raise ReplayError(
                f"the readings of one region on page {page_index + 1} were recorded at different "
                "resolutions, so their one polygon is not one place"
            )
        xs = [x for x, _ in polygon]
        ys = [y for _, y in polygon]
        regions.append(
            Region(
                handle=f"p{page_index + 1}r{number}",
                page_index=page_index,
                box_px=(min(xs), min(ys), max(xs), max(ys)),
                dpi=resolutions.pop(),
                readings=tuple(member.reading for member in members),
            )
        )
    return tuple(regions)


def join(
    crops: Sequence[KeyCrop],
    regions: Sequence[Region],
    *,
    frame: KeyFrame,
    view_margin_pt: Decimal,
) -> dict[str, tuple[Region, ...]]:
    """Each key crop's regions: every one whose centre lies inside the crop the person read.

    That crop is the key's region with `view_margin_pt` round it — production's vision margin, so
    the page a reader is shown. A centre on the crop's edge is inside it. Nothing about the
    readings decides the join, so it cannot pick the readings that happen to agree.
    """
    joined: dict[str, tuple[Region, ...]] = {}
    for crop in crops:
        left, top, right, bottom = _grown(region_of(crop, frame), Fraction(view_margin_pt))
        joined[crop.crop_id] = tuple(
            region
            for region in regions
            if region.page_index == crop.page_index
            and left <= (region.box[0] + region.box[2]) / 2 <= right
            and top <= (region.box[1] + region.box[3]) / 2 <= bottom
        )
    return joined


def regions_from_scorecard(
    rows: Sequence[Mapping[str, Any]],
    crops: Sequence[KeyCrop],
    *,
    frame: KeyFrame,
    stage_dpi: int,
    model_ids: Mapping[str, str],
) -> dict[str, tuple[Region, ...]]:
    """The reader pair's readings in an agent scorecard's working file: a region per crop (#757).

    **Only the pair's**, the two readers the gate exists for; the agent's looks are left out. A
    reader that returned no text gave no reading, as it writes no row in the stage. The file keeps
    each reader's text, not its value, so the value is parsed again by `value_of`, the rule the
    stage stores a vision reading's value by.
    """
    by_crop = {str(row.get("crop")): row for row in rows}
    wanted = {crop.crop_id for crop in crops}
    if set(by_crop) != wanted or len(by_crop) != len(rows):
        raise ReplayError("this scorecard was not made on this key: its crops are not the key's")
    joined: dict[str, tuple[Region, ...]] = {}
    for crop in crops:
        readings: list[Reading] = []
        try:
            pair = [(str(entry[0]), entry[1]) for entry in by_crop[crop.crop_id]["pair"]]
        except (KeyError, IndexError, TypeError) as error:
            raise ReplayError(f"crop {crop.crop_id}'s pair could not be read: {error}") from error
        for extractor, text in pair:
            if text is None:
                continue
            if extractor not in model_ids:
                raise ReplayError(
                    f"no model id is known for {extractor}, so its vendor cannot be judged (#775)"
                )
            readings.append(_model_reading(extractor, model_ids[extractor], str(text)))
        joined[crop.crop_id] = (
            Region(
                handle=crop.crop_id,
                page_index=crop.page_index,
                box_px=pixels(region_of(crop, frame), stage_dpi),
                dpi=stage_dpi,
                readings=tuple(readings),
            ),
        )
    return joined


def _model_reading(extractor: str, model_id: str, text: str) -> Reading:
    return Reading(
        extractor=extractor,
        extractor_version=model_id,
        raw_text=text,
        value=value_of(_candidate(extractor, model_id, text, None, (), number=0)),
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Agreement:
    """A value the gate agreed, and the lane that agreed it."""

    value: Measurement
    lane: CorroborationLane


def _candidate(
    extractor: str,
    version: str,
    text: str,
    value: Measurement | None,
    flags: tuple[str, ...],
    *,
    number: int,
) -> DomainCandidate:
    return DomainCandidate(
        candidate_id=f"reading-{number}",
        extractor=extractor,
        extractor_version=version,
        raw_text=text,
        parsed_value=value,
        unit_guess=None,
        semantic_guess=None,
        page=0,
        polygon=(ImagePoint(0, 0), ImagePoint(1, 0), ImagePoint(1, 1)),
        confidence=None,
        ambiguity_flags=flags,
    )


def _own_dual(reading: Reading) -> DualDimension | None:
    """The `mm [in]` label a reading states, where production judges it on its own.

    `app.evidence.record` asks the dual-unit lane about every row a route that is not a model writes
    — the file's text, OCR, the reviewer's markup, the shape reader — whose text is a bracketed
    pair, and the stage then leaves those rows out of the region's group. A model's reading is never
    asked: the stage writes vision rows without it. A model reader is one `independence_key` counts
    by its vendor.
    """
    if (
        independence_key(reading.extractor, reading.extractor_version)
        != f"route:{reading.extractor}"
    ):
        return None
    try:
        dual = parse_dual(reading.raw_text)
    except DualDimensionParseError:
        return None
    return dual if dual.alternate is not None else None


def _never(reading: Reading) -> bool:
    del reading
    return False


def _file_text(reading: Reading) -> bool:
    return reading.extractor in EXACT_TEXT_EXTRACTORS


def gate(
    readings: Sequence[Reading], *, mm_confirms: Callable[[Reading], bool] = _never
) -> Agreement | None:
    """The gate's agreement on one region's readings, or `None` — asked as production asks it.

    1. Each non-model route's own `mm [in]` label is judged alone. If its two halves disagree, the
       region holds a conflict, and nothing in it is agreed.
    2. Every other reading of the region is judged together. A conflict there agrees nothing either;
       a second-reader agreement is the region's agreement.
    3. Otherwise a label whose millimetres confirmed its inches is agreed — only for a reading
       `mm_confirms` accepts. By default none is, because `evidence/gate.py` seals no such label.
    """
    duals: list[tuple[Reading, DualDimension]] = []
    grouped: list[Reading] = []
    for reading in readings:
        dual = _own_dual(reading)
        if dual is None:
            grouped.append(reading)
        else:
            duals.append((reading, dual))

    confirmed: list[Measurement] = []
    for number, (reading, dual) in enumerate(duals):
        result = corroborate(
            (
                _candidate(
                    reading.extractor,
                    reading.extractor_version,
                    reading.raw_text,
                    dual.primary,
                    reading.flags,
                    number=number,
                ),
            ),
            dual_dimension=dual,
        )
        if result.status is EvidenceStatus.CONFLICTING:
            return None
        if (
            result.lane is CorroborationLane.DUAL_UNIT
            and dual.alternate is not None
            and mm_confirms(reading)
        ):
            confirmed.append(dual.alternate)

    if len(grouped) >= 2:
        result = corroborate(
            tuple(
                _candidate(
                    reading.extractor,
                    reading.extractor_version,
                    reading.raw_text,
                    reading.value,
                    reading.flags,
                    number=number,
                )
                for number, reading in enumerate(grouped)
            )
        )
        if result.status is EvidenceStatus.CONFLICTING:
            return None
        if result.lane is CorroborationLane.SECOND_READER:
            value = grouped[0].value
            # `corroborate` agrees only where every reading has the same value, so this one is it.
            assert value is not None
            return Agreement(value, CorroborationLane.SECOND_READER)
    if confirmed:
        return Agreement(confirmed[0], CorroborationLane.DUAL_UNIT)
    return None


# ---------------------------------------------------------------------------
# The candidate guards
# ---------------------------------------------------------------------------


class Guard(StrEnum):
    """A candidate pre-filter on the gate. Only `NONE` is what production does today."""

    NONE = "the gate as it is"
    CUT = "held back where the crop cuts the label"
    GV_MARK = "held back where a GV mark is in the crop"
    PROMOTED_NUMERATOR = "held back where a reading is `n n/d`"
    SIDEWAYS = "a sideways label needs a third reader"
    MM_ON_FILE_TEXT = "a label's mm confirms its inches, on the file's own text only"


def promoted_numerator(value: Measurement) -> bool:
    """Whether a value has the shape `n n/d`: a whole number equal to its fraction's numerator.

    It is the shape of a stacked `3/4"` read as `3 3/4"`, which two readers from different vendors
    agreed on (#726). It is also the shape of a real `1 1/2"`, which is what holding it back costs.
    """
    whole, part = divmod(value.exact, 1)
    return whole >= 1 and part != 0 and part.numerator == whole


def held_back(region: Region, facts: Facts, guard: Guard) -> bool:
    """Whether `guard` keeps the gate from agreeing anything in `region`, before it is asked.

    A sideways label's third reader is a third extractor with a value.
    """
    if guard is Guard.CUT:
        return facts.cut_at_edge
    if guard is Guard.GV_MARK:
        return facts.gv_mark
    if guard is Guard.PROMOTED_NUMERATOR:
        return any(
            reading.value is not None and promoted_numerator(reading.value)
            for reading in region.readings
        )
    if guard is Guard.SIDEWAYS:
        readers = {reading.extractor for reading in region.readings if reading.value is not None}
        return facts.sideways and len(readers) < THIRD_READER
    return False


def judge(region: Region, facts: Facts, guard: Guard) -> Agreement | None:
    """What the gate agrees in `region` with `guard` in front of it."""
    if held_back(region, facts, guard):
        return None
    return gate(
        region.readings, mm_confirms=_file_text if guard is Guard.MM_ON_FILE_TEXT else _never
    )


# ---------------------------------------------------------------------------
# Against the person
# ---------------------------------------------------------------------------


class Outcome(StrEnum):
    """What one crop comes to under one guard."""

    RIGHT = "agreed, right"
    WRONG = "agreed, wrong"
    UNVOUCHED = "agreed on a crop the person could not vouch for"
    NOT_AGREED = "nothing agreed"


def outcome(crop: KeyCrop, agreements: Sequence[Agreement]) -> Outcome:
    """One crop's outcome from every agreement among its regions: **one wrong makes it wrong**."""
    if not agreements:
        return Outcome.NOT_AGREED
    if crop.kind is not Kind.SCORED or crop.expected is None:
        return Outcome.UNVOUCHED
    expected = (crop.expected.exact, crop.expected.unit)
    if any((agreement.value.exact, agreement.value.unit) != expected for agreement in agreements):
        return Outcome.WRONG
    return Outcome.RIGHT


@dataclass(frozen=True, slots=True)
class Replay:
    """One source's readings for one key, joined to its crops, with each region's geometry."""

    source: str
    crops: tuple[KeyCrop, ...]
    regions: Mapping[str, tuple[Region, ...]]
    """Each crop's regions, by crop id."""

    facts: Mapping[str, Facts]
    """Each region's geometry, by region handle."""

    def __post_init__(self) -> None:
        missing = [crop.crop_id for crop in self.crops if crop.crop_id not in self.regions]
        if missing:
            raise ReplayError(f"{self.source}: no regions were joined for crops {missing}")
        unseen = sorted(
            {
                region.handle
                for regions in self.regions.values()
                for region in regions
                if region.handle not in self.facts
            }
        )
        if unseen:
            raise ReplayError(f"{self.source}: no geometry for regions {unseen}")

    def agreements(self, crop: KeyCrop, guard: Guard) -> tuple[Agreement, ...]:
        return tuple(
            agreement
            for region in self.regions[crop.crop_id]
            if (agreement := judge(region, self.facts[region.handle], guard)) is not None
        )


def rule_of_three(agreed: int, wrong: int) -> Fraction | None:
    """The upper bound on the agreed-wrong rate, with about 95% confidence, when none was wrong.

    With no wrong agreement among `agreed`, the rate is below 3/`agreed` (the rule of three), and
    never above 1. **`None` where it does not apply:** where nothing was agreed there is nothing to
    bound, and where a wrong agreement was seen the count is the finding, not a bound on it.
    """
    if agreed < 0 or wrong < 0 or wrong > agreed:
        raise ValueError("agreed and wrong must be counts, with no more wrong than agreed")
    if agreed == 0 or wrong:
        return None
    return min(Fraction(3, agreed), Fraction(1))


@dataclass(frozen=True, slots=True)
class GuardTally:
    """What one guard leaves agreed, on one key, from one source."""

    guard: Guard
    crops: int
    scored: int
    """Crops the person read a value for."""

    right: int
    wrong: int
    unvouched: int

    @property
    def agreed(self) -> int:
        """Agreements on crops the person read: what the bound is a bound over."""
        return self.right + self.wrong

    @property
    def coverage(self) -> Fraction | None:
        """The share of the crops the person read on which the gate agreed a value."""
        return None if self.scored == 0 else Fraction(self.agreed, self.scored)

    @property
    def bound(self) -> Fraction | None:
        return rule_of_three(self.agreed, self.wrong)


def tally(replay: Replay, guard: Guard) -> GuardTally:
    counts: Counter[Outcome] = Counter(
        outcome(crop, replay.agreements(crop, guard)) for crop in replay.crops
    )
    return GuardTally(
        guard=guard,
        crops=len(replay.crops),
        scored=sum(crop.kind is Kind.SCORED for crop in replay.crops),
        right=counts[Outcome.RIGHT],
        wrong=counts[Outcome.WRONG],
        unvouched=counts[Outcome.UNVOUCHED],
    )


@dataclass(frozen=True, slots=True)
class StackedCatch:
    """How the stacked-fraction finder did against the crops the key marks stacked."""

    marked: int
    caught: int
    flagged_unmarked: int
    """Crops it flags that the key does not mark — a cost in coverage, never a safety failure."""


def stacked_catch(crops: Sequence[KeyCrop], found: Mapping[str, bool]) -> StackedCatch:
    """The finder's verdict on each crop's region (`found`, by crop id) against the key's marks."""
    return StackedCatch(
        marked=sum(crop.stacked for crop in crops),
        caught=sum(crop.stacked and found[crop.crop_id] for crop in crops),
        flagged_unmarked=sum(not crop.stacked and found[crop.crop_id] for crop in crops),
    )


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _share(part: Fraction) -> str:
    percent = Decimal(part.numerator * 100) / Decimal(part.denominator)
    return f"{percent.quantize(Decimal('0.1'))}%"


def _bound_text(counts: GuardTally) -> str:
    if counts.agreed == 0:
        return "nothing agreed, so nothing is bounded"
    if counts.wrong:
        return f"not bounded: {counts.wrong} of {counts.agreed} agreed wrong"
    bound = counts.bound
    assert bound is not None
    return f"below {_share(bound)} (0 wrong in {counts.agreed})"


def _stacked_text(stacked: StackedCatch) -> str:
    if stacked.marked == 0:
        return (
            "**The stacked-fraction finder** is not measured here: the key marks no crop stacked. "
            f"It flags {stacked.flagged_unmarked} of the key's crops."
        )
    return (
        f"**The stacked-fraction finder**, on each crop's region, flags {stacked.caught} of the "
        f"{stacked.marked} crops the key marks stacked, and {stacked.flagged_unmarked} it does not "
        "mark."
    )


def render_markdown(
    *,
    header: str,
    crops: Sequence[KeyCrop],
    stacked: StackedCatch,
    replays: Sequence[Replay],
) -> str:
    """The replay as counts only: no value, no text, nothing from the drawing."""
    scored = sum(crop.kind is Kind.SCORED for crop in crops)
    lines = [
        header,
        "",
        (
            f"{len(crops)} crops: {scored} a person read a value for, {len(crops) - scored} they "
            "could not vouch for (cut off, unreadable, two values and an operator, or not a "
            "dimension)."
        ),
        "",
        (
            "**0 wrong is not 0 risk.** With no wrong agreement among n, the true rate is only "
            "known to be below about 3/n, with about 95% confidence (the rule of three)."
        ),
        "",
        _stacked_text(stacked),
    ]
    for replay in replays:
        with_value = sum(
            any(
                reading.value is not None
                for region in replay.regions[crop.crop_id]
                for reading in region.readings
            )
            for crop in replay.crops
        )
        lines += [
            "",
            f"### {replay.source}",
            "",
            f"{with_value} of {len(replay.crops)} crops have a joined reading with a value.",
            "",
            (
                "| Guard | Agreed, right | **Agreed, wrong** | Agreed on a crop nobody could vouch "
                "for | Coverage of the crops read | Agreed-wrong rate, upper bound |"
            ),
            "|---|---:|---:|---:|---:|---|",
        ]
        for guard in Guard:
            counts = tally(replay, guard)
            coverage = counts.coverage
            lines.append(
                f"| {guard.value} | {counts.right} | **{counts.wrong}** | {counts.unvouched} | "
                + (
                    "-"
                    if coverage is None
                    else f"{counts.agreed}/{counts.scored} ({_share(coverage)})"
                )
                + f" | {_bound_text(counts)} |"
            )
    return "\n".join(lines) + "\n"


def results_json(replay: Replay) -> list[dict[str, object]]:
    """Every crop's joined readings and outcomes, for the local working file (never committed)."""

    def value(measurement: Measurement | None) -> str | None:
        return None if measurement is None else f"{measurement.exact} {measurement.unit.value}"

    return [
        {
            "crop": crop.crop_id,
            "kind": crop.kind.value,
            "expected": value(crop.expected),
            "regions": [
                {
                    "region": region.handle,
                    "readings": [
                        [reading.extractor, reading.raw_text, value(reading.value)]
                        for reading in region.readings
                    ],
                    "facts": {
                        "cut": replay.facts[region.handle].cut_at_edge,
                        "sideways": replay.facts[region.handle].sideways,
                        "stacked": replay.facts[region.handle].stacked,
                        "gv_mark": replay.facts[region.handle].gv_mark,
                    },
                }
                for region in replay.regions[crop.crop_id]
            ],
            "outcomes": {
                guard.value: outcome(crop, replay.agreements(crop, guard)).value for guard in Guard
            },
        }
        for crop in replay.crops
    ]
