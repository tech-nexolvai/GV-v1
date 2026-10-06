"""Slot reads in the worker: plan, crop, read in parallel, seal, map, persist (#987).

Opt-in (`GV_SLOT_READER_ENABLED`, off by default) and built on the form reader's runtime — the same
two readers, prices, pacer, concurrency cap and spend meter. A page with a row candidate is read
slot by slot; a page with none is left to the whole-page form reader, as today.

This bridge persists candidates and candidate-only proposal links, never a form value: the
reviewer still saves the form (#965's rule). Its runs are recorded under the form reader's
extractor name, `extraction.form_reader`, with their own version and configuration, so every API
path that treats form-first readings as proposals only — the measure form, the refusal to confirm
one outside the form — treats these the same way.

Source: issue #987 · Verification: `tests/workflow/test_slot_reader.py`
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final
from uuid import UUID, uuid4

from evidence.crop import RenderedPage, _crop_rgb, encode_png
from extraction.form_reader.bedrock import AttemptUsage
from extraction.geometry.rows import MEASURED_SETTINGS, Box, RowSettings
from extraction.glyph_bands import FractionBarGeometry, stacked_fractions
from extraction.ink import InkAt, InkClass, PageInk
from extraction.rows import RowsAndInk
from extraction.slot_reader.bedrock import CROP_PROMPT_ID, CropJob, read_crops_parallel
from extraction.slot_reader.kinds import KindProposal, PieceKind, WallEnd, propose_kind
from extraction.slot_reader.mapping import PieceReading, SlotMapping, map_row
from extraction.slot_reader.runs import (
    E2_CROP_SETTINGS,
    CropSettings,
    Lane,
    PlannedLabel,
    PlannedOwner,
    SlotPlan,
    plan_slots,
)
from extraction.slot_reader.seal import (
    LabelOutcome,
    LabelState,
    OwnerOutcome,
    ReaderAnswer,
    normalise_text,
    owner_outcome,
    plain_dimension,
    seal_label,
)
from workflow.form_reader import FormReaderRuntime

__all__ = [
    "FRACTION_BAR_ENV",
    "SLOT_READER_VERSION",
    "LabelResult",
    "OwnerResult",
    "PageSlotResult",
    "SlotPage",
    "SlotReaderRuntime",
    "configured_slot_reader",
    "fraction_bar_from_environment",
    "persist_slot_readings",
    "read_slot_pages",
]

SLOT_READER_VERSION: Final = "slot-reader-v1"

#: The fraction-bar detector's eight lengths (#735), as the deployment states them for the reader.
#: Required once the slot reader is on: without them a stacked fraction drawn as paths could not be
#: told, and the rule that sends one to a person could not be kept.
FRACTION_BAR_ENV: Final = {
    "GV_READER_FRACTION_BAR_THICKNESS_MAX_PT": "bar_thickness_max_pt",
    "GV_READER_FRACTION_BAR_LENGTH_MIN_PT": "bar_length_min_pt",
    "GV_READER_FRACTION_REACH_PT": "reach_pt",
    "GV_READER_FRACTION_GLYPH_MIN_PT": "glyph_min_pt",
    "GV_READER_FRACTION_GLYPH_MAX_PT": "glyph_max_pt",
    "GV_READER_FRACTION_PROPORTION_MAX": "proportion_max",
    "GV_READER_FRACTION_CHARACTER_GAP_PT": "character_gap_pt",
    "GV_READER_FRACTION_TURNED_ASPECT_MIN": "turned_aspect_min",
}


def fraction_bar_from_environment(environ: Mapping[str, str] = os.environ) -> FractionBarGeometry:
    """The fraction-bar lengths as stated; refused, never filled in, when one is missing."""
    values: dict[str, Decimal] = {}
    missing: list[str] = []
    for name, field in FRACTION_BAR_ENV.items():
        raw = environ.get(name, "").strip()
        if not raw:
            missing.append(name)
            continue
        try:
            values[field] = Decimal(raw)
        except InvalidOperation as error:
            raise ValueError(f"{name} is not a number: {raw!r}") from error
    if missing:
        raise ValueError(
            "GV_SLOT_READER_ENABLED requires the fraction-bar lengths; missing: "
            + ", ".join(missing)
        )
    return FractionBarGeometry(**values)


@dataclass(frozen=True, slots=True)
class SlotReaderRuntime:
    form: FormReaderRuntime
    crop_settings: CropSettings
    row_settings: RowSettings
    fraction_bar: FractionBarGeometry
    allow_stacked: bool
    """The admin's yes/no (#987): may a stacked fraction seal on two readers' identical text?
    Off unless the deployment says so."""

    @property
    def text_lane_reader(self) -> str:
        """The reader shown a text label's crop: the second configured reader (Qwen by default)."""
        return self.form.reader_ids[1]

    @property
    def config_hash(self) -> str:
        return (
            f"readers={self.form.reader_ids[0]}|{self.form.reader_ids[1]};"
            f"prompt={CROP_PROMPT_ID};stacked_agreement={self.allow_stacked};"
            f"crop={self.crop_settings.config_hash};"
            f"fraction_bar={self.fraction_bar.config_hash}"
        )


def configured_slot_reader(
    settings: object,
    form: FormReaderRuntime | None,
    environ: Mapping[str, str] = os.environ,
) -> SlotReaderRuntime | None:
    """The slot reader's runtime, or `None` when it is off (the default)."""
    if not bool(getattr(settings, "slot_reader_enabled", False)):
        return None
    if form is None:
        raise ValueError(
            "GV_SLOT_READER_ENABLED requires GV_FORM_READER_ENABLED: the slot reader uses the form "
            "reader's readers, prices and limits, and its whole-page reading for pages with no row"
        )
    return SlotReaderRuntime(
        form=form,
        crop_settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
        fraction_bar=fraction_bar_from_environment(environ),
        allow_stacked=bool(getattr(settings, "slot_reader_stacked_agreement", False)),
    )


@dataclass(frozen=True, slots=True)
class SlotPage:
    """One shop page to read slot by slot: its render, its rows and ink, and whose ink it holds."""

    page_index: int
    page_id: UUID
    document_version_id: UUID
    rendered: RenderedPage
    rows: RowsAndInk
    ink: PageInk | None
    """Whose ink each word is (#979), at the render's dpi; `None` holds every reading back."""


@dataclass(frozen=True, slots=True)
class LabelResult:
    label: PlannedLabel
    outcome: LabelOutcome
    box_px: tuple[int, int, int, int]
    crop_px: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class OwnerResult:
    owner: PlannedOwner
    band_px: tuple[int, int, int, int]
    labels: tuple[LabelResult, ...]
    outcome: OwnerOutcome
    kind: KindProposal | None
    """`None` for the overall."""
    words: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PageSlotResult:
    page_index: int
    page_id: UUID
    document_version_id: UUID
    plan: SlotPlan
    slots: tuple[OwnerResult, ...]
    overall: OwnerResult | None
    mapping: SlotMapping


def _pixels(rows: RowsAndInk, box: Box, rendered: RenderedPage) -> tuple[int, int, int, int]:
    first = rows.to_pixels(box.x0, box.top)
    second = rows.to_pixels(box.x1, box.bottom)
    left = max(0, min(first[0], second[0]))
    top = max(0, min(first[1], second[1]))
    right = min(rendered.width_px, max(first[0], second[0]))
    bottom = min(rendered.height_px, max(first[1], second[1]))
    return left, top, max(left + 1, right), max(top + 1, bottom)


def _crop_png(rendered: RenderedPage, box: tuple[int, int, int, int]) -> bytes:
    left, top, right, bottom = box
    return encode_png(right - left, bottom - top, _crop_rgb(rendered, box))


def _stacked_by_bar(label: PlannedLabel, height: Decimal, geometry: FractionBarGeometry) -> bool:
    """Whether the fraction-bar detector finds a stacked fraction among the label's strokes."""
    if not label.path_boxes:
        return False
    boxes = tuple(
        (box.x0, height - box.bottom, box.x1, height - box.top) for box in label.path_boxes
    )
    return bool(stacked_fractions(boxes, geometry=geometry, ink=[True] * len(boxes)))


def _hard_guarded(label: PlannedLabel, ink: InkAt | None) -> bool:
    """A label that goes to the person whatever is read: not worth a paid call."""
    return (
        label.crowded or label.touches_edge or (ink is not None and ink.ink is not InkClass.VENDOR)
    )


def read_slot_pages(
    pages: Sequence[SlotPage],
    *,
    runtime: SlotReaderRuntime,
    record_attempt: Callable[[AttemptUsage], None],
    wall_ends: Callable[[SlotPage], frozenset[WallEnd]] = lambda _page: frozenset(),
) -> tuple[PageSlotResult, ...]:
    """Read every page's slots: plan, crop, ask the readers in parallel, seal, name, map.

    `wall_ends` says which ends of a page's row stand against a wall; no detector exists yet, so by
    default none does and no piece takes its kind from its position (#987).
    """
    readers = runtime.form.reader_ids
    planned: list[tuple[SlotPage, SlotPlan]] = []
    jobs: list[CropJob] = []
    crops: dict[str, tuple[tuple[int, int, int, int], tuple[int, int, int, int], InkAt | None]] = {}
    for page in pages:
        if page.ink is not None and page.ink.dpi != page.rendered.dpi:
            raise ValueError(
                f"page {page.page_index}'s ink was read at {page.ink.dpi} dpi but its picture is "
                f"at {page.rendered.dpi} dpi"
            )
        plan = plan_slots(
            page.rows.candidates.rows,
            page.rows.ink,
            settings=runtime.crop_settings,
            row_settings=runtime.row_settings,
        )
        planned.append((page, plan))
        owners = [*plan.slots, *([plan.overall] if plan.overall is not None else [])]
        for owner in owners:
            for position, label in enumerate(owner.labels):
                key = _key(page.page_index, owner.index, position)
                box_px = _pixels(page.rows, label.box, page.rendered)
                crop_px = _pixels(page.rows, label.crop, page.rendered)
                ink = None if page.ink is None else page.ink.at(crop_px)
                crops[key] = (box_px, crop_px, ink)
                if _hard_guarded(label, ink) or not label.has_digit:
                    continue
                if label.lane is Lane.TEXT:
                    if label.text is None or plain_dimension(normalise_text(label.text)) is None:
                        continue
                    wanted: tuple[str, ...] = (runtime.text_lane_reader,)
                else:
                    wanted = readers
                png = _crop_png(page.rendered, crop_px)
                jobs.extend(CropJob(key, model, page.page_index, png) for model in wanted)

    form = runtime.form
    answers = read_crops_parallel(
        jobs,
        clients=form.clients,
        rates=form.rates,
        calls_per_minute=form.calls_per_minute,
        max_concurrent_calls=form.max_concurrent_calls,
        max_tokens=form.max_tokens,
        max_throttle_retries=form.max_throttle_retries,
        retry_backoff_seconds=form.retry_backoff_seconds,
        record_attempt=record_attempt,
    )

    results: list[PageSlotResult] = []
    for page, plan in planned:

        def owner_result(
            owner: PlannedOwner, count: int, page: SlotPage = page, plan: SlotPlan = plan
        ) -> OwnerResult:
            return _owner_result(
                page,
                plan,
                owner,
                count,
                crops=crops,
                answers=answers,
                runtime=runtime,
                wall_ends=wall_ends(page),
            )

        slots = tuple(owner_result(owner, len(plan.slots)) for owner in plan.slots)
        overall = None if plan.overall is None else owner_result(plan.overall, len(plan.slots))
        mapping = map_row(
            None if overall is None else overall.outcome,
            [
                PieceReading(
                    index=item.owner.index,
                    outcome=item.outcome,
                    kind=item.kind or KindProposal(PieceKind.UNKNOWN, ""),
                )
                for item in slots
                if item.owner.index is not None
            ],
            row_ambiguity=plan.ambiguity,
        )
        results.append(
            PageSlotResult(
                page_index=page.page_index,
                page_id=page.page_id,
                document_version_id=page.document_version_id,
                plan=plan,
                slots=slots,
                overall=overall,
                mapping=mapping,
            )
        )
    return tuple(results)


def _owner_result(
    page: SlotPage,
    plan: SlotPlan,
    owner: PlannedOwner,
    count: int,
    *,
    crops: Mapping[str, tuple[tuple[int, int, int, int], tuple[int, int, int, int], InkAt | None]],
    answers: Mapping[tuple[str, str], ReaderAnswer | None],
    runtime: SlotReaderRuntime,
    wall_ends: frozenset[WallEnd],
) -> OwnerResult:
    """Seal each of a slot's (or the overall's) labels, take its one reading, name its kind."""
    height = page.rows.ink.height
    labels: list[LabelResult] = []
    for position, label in enumerate(owner.labels):
        key = _key(page.page_index, owner.index, position)
        box_px, crop_px, ink = crops[key]
        present = [
            answer
            for model in runtime.form.reader_ids
            if (answer := answers.get((key, model))) is not None
        ]
        sealed = seal_label(
            label,
            answers=present,
            ink=ink,
            stacked_by_bar=label.lane is Lane.GLYPHS
            and _stacked_by_bar(label, height, runtime.fraction_bar),
            allow_stacked=runtime.allow_stacked,
            row_ambiguity=plan.ambiguity,
        )
        labels.append(LabelResult(label, sealed, box_px, crop_px))
    outcome = owner_outcome([item.outcome for item in labels])
    # Only the vendor's own words name a piece: never the reviewer's ink, never an unchecked label.
    words = tuple(
        text
        for item in labels
        if item.outcome.ink is InkClass.VENDOR
        for text in (
            item.label.text if item.label.lane is Lane.TEXT else None,
            item.outcome.sealed_text,
        )
        if text
    )
    kind = (
        None
        if owner.index is None
        else propose_kind(words, index=owner.index, count=count, wall_ends=wall_ends)
    )
    return OwnerResult(
        owner=owner,
        band_px=_pixels(page.rows, owner.band, page.rendered),
        labels=tuple(labels),
        outcome=outcome,
        kind=kind,
        words=words,
    )


def _key(page_index: int, owner: int | None, position: int) -> str:
    return f"p{page_index}:{'overall' if owner is None else f'slot{owner}'}:{position}"


def _box_flag(name: str, box: tuple[int, int, int, int]) -> str:
    return f"{name}:{box[0]},{box[1]},{box[2]},{box[3]}"


def _polygon(box: tuple[int, int, int, int]) -> list[list[int]]:
    left, top, right, bottom = box
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def persist_slot_readings(
    session: object,
    *,
    package_revision_id: UUID,
    extraction_run_id: UUID,
    reader_ids: tuple[str, str],
    results: Sequence[PageSlotResult],
) -> int:
    """Persist one candidate per slot and overall, and candidate-only links for mapped ones.

    What Phase 5's screen needs rides on the candidate as plain flags, because the current schema
    has no columns for it: `slot:<i>` or `slot:overall`, `slot-box:` and `crop-box:` in the page's
    pixels, `kind:` and `kind-evidence:`, `ink:`, `lane:`, `row-rank:`, each reader's text as
    `reader:<model>:<text>`, and every guard that fired. The label's box is the polygon. Raw
    per-attempt answers wait for #983.
    """
    from sqlalchemy.orm import Session

    from app.models.document import Page as PageModel
    from app.models.evidence import MeasurementProposal, ObservationCandidate
    from evidence.canonical import CorroborationLane
    from units.measurement import Unit

    if not isinstance(session, Session):
        raise TypeError("session must be a SQLAlchemy Session")
    count = 0
    rows: list[MeasurementProposal] = []
    for result in results:
        if result.plan.row is None:
            continue
        page = session.get(PageModel, result.page_id)
        if page is None:
            raise ValueError("slot-reader page disappeared before persistence")
        candidates: dict[int | None, ObservationCandidate] = {}
        held = dict(result.mapping.held)
        offered = {proposal.slot_index for proposal in result.mapping.proposals}
        owners = [*result.slots, *([result.overall] if result.overall is not None else [])]
        for owner in owners:
            index = owner.owner.index
            chosen = (
                owner.labels[owner.outcome.label_index]
                if owner.outcome.label_index is not None
                else None
            )
            sealed = owner.outcome.state is LabelState.SEALED and owner.outcome.value is not None
            accepted = sealed and index in offered
            value = owner.outcome.value if sealed else None
            if value is None and chosen is not None:
                value = chosen.outcome.suggestion
            reason = (
                None
                if accepted
                else (
                    (held.get(index) if index is not None else None) or "not offered to the form"
                    if sealed
                    else owner.outcome.reason
                )
            )
            flags = [
                "slot-reader",
                f"slot:{'overall' if index is None else index}",
                _box_flag("slot-box", owner.band_px),
                f"row-rank:{result.plan.row.rank}",
            ]
            if result.plan.ambiguity is not None:
                flags.append("row-ambiguous")
            if owner.kind is not None:
                flags.append(f"kind:{owner.kind.kind.value}")
                flags.append(f"kind-evidence:{owner.kind.evidence}")
            if owner.outcome.reason_code is not None:
                flags.append(owner.outcome.reason_code)
            if result.plan.row.findings and index is None:
                flags.extend(f"row-finding:{finding}" for finding in result.plan.row.findings)
            polygon: list[list[int]] = _polygon(owner.band_px)
            raw_text = ""
            if chosen is not None:
                polygon = _polygon(chosen.box_px)
                flags.append(_box_flag("crop-box", chosen.crop_px))
                flags.extend(chosen.outcome.flags)
                if chosen.outcome.ink is not None:
                    flags.append(f"ink:{chosen.outcome.ink.value}")
                flags.extend(
                    f"reader:{source}:{text}" for source, text in chosen.outcome.reader_texts
                )
                raw_text = chosen.outcome.sealed_text or next(
                    (text for _, text in reversed(chosen.outcome.reader_texts)), ""
                )
            else:
                for label in owner.labels:
                    flags.extend(
                        f"reader:{source}:{text}" for source, text in label.outcome.reader_texts
                    )
            candidate = ObservationCandidate(
                document_version_id=result.document_version_id,
                page_id=result.page_id,
                extraction_run_id=extraction_run_id,
                raw_text=raw_text,
                value_numerator=None if value is None else value.exact.numerator,
                value_denominator=None if value is None else value.exact.denominator,
                unit=None if value is None else Unit.INCH.value,
                # Never the kind: a semantic guess feeds semantic typing, and a kind is a
                # proposal for the person (`kind:` flag), not a type the evidence path may use.
                semantic_guess=None,
                polygon=polygon,
                coordinate_space="image",
                confidence=None,
                ambiguity_flags=list(dict.fromkeys(flags)),
                corroboration_status="CORROBORATED" if accepted else None,
                corroboration_lane=CorroborationLane.SECOND_READER.value if accepted else None,
                review_reason=None if reason is None else reason[:300],
            )
            session.add(candidate)
            session.flush()
            candidates[index] = candidate
            count += 1
        proposal_id = uuid4()
        for proposal in result.mapping.proposals:
            rows.append(
                MeasurementProposal(
                    package_revision_id=package_revision_id,
                    page_number=page.index + 1,
                    proposal_id=proposal_id,
                    field_key=proposal.field_key,
                    position=proposal.position,
                    candidate_id=candidates[proposal.slot_index].id,
                    placement_verified=True,
                    model_id=f"{reader_ids[0]} + {reader_ids[1]}",
                    prompt_id=CROP_PROMPT_ID,
                )
            )
    session.add_all(rows)
    session.flush()
    return count
