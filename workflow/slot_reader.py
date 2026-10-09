"""Slot reads in the worker: plan, crop, read in parallel, seal, map, persist (#987).

Opt-in (`GV_SLOT_READER_ENABLED`, off by default) and built on the form reader's runtime — the same
two readers, prices, pacer, concurrency cap and spend meter. A page with a row candidate is read
slot by slot; a page with none is left to the whole-page form reader, as today.

This bridge persists candidates and candidate-only proposal links, never a form value: the
reviewer still saves the form (#965's rule). Its runs are recorded under the form reader's
extractor name, `extraction.form_reader`, with their own version and configuration, so every API
path that treats form-first readings as proposals only — the measure form, the refusal to confirm
one outside the form — treats these the same way.

**The admin's V1 rules (#992), each of which only holds back or seals under stricter
conditions:** a sealed reading the drawn length rejects goes back to the person; a row whose texts
say `INCLUDING FIELD CUT` or `VIF`, or whose vendor drawing names a tall appliance or range bay
inside its slot spans, offers nothing;
and each row's walls are asked of both readers,
a layout sealed only on their agreement and recorded as a `wall_config` layout *proposal* — which
a check may use only under the conditions `workflow/layout_proposals.py` states, and only when the
reviewer has stated no layout. Agreed `a"+b"` and `N"(K EQ)` labels are expanded in `seal.py`.

**The Claude readers (#1051)** are asked at a stated effort (`GV_CLAUDE_READER_EFFORT`, recorded in
the run's configuration and on every question packet), with a fixed answer shape, and never with a
picture the API would resize. A span whose label is drawn sideways (a label run at least twice as
tall as wide that is not a stacked fraction) is also shown its close-up turned upright.

Source: issues #987, #992, #1051 · Verification: `tests/workflow/test_slot_reader.py`
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from evidence.coordinates import PageTransform
from evidence.crop import RenderedPage, _crop_rgb, decode_rgb_png, encode_png
from extraction.form_reader.bedrock import AttemptUsage
from extraction.form_reader.runner import ModelPacer
from extraction.geometry.rows import MEASURED_SETTINGS, Box, CountertopRowCandidate, RowSettings
from extraction.glyph_bands import FractionBarGeometry, stacked_fractions
from extraction.ink import InkAt, InkClass, InkLabel, PageInk
from extraction.rows import RowsAndInk
from extraction.slot_reader.bedrock import (
    CLAUDE_SPAN_PROMPT_ID,
    CLAUDE_SPAN_PROMPT_IDS,
    COUNTER_BREAK_PROMPT_ID,
    CROP_PROMPT_ID,
    ROW_PROMPT_ID,
    CounterBreakAnswer,
    CropJob,
    RowChoiceAnswer,
    crop_prompt_id,
    read_crops_parallel,
)
from extraction.slot_reader.claude_output import (
    CLAUDE_EFFORTS,
    DEFAULT_CLAUDE_EFFORT,
    ClaudeEffort,
)
from extraction.slot_reader.kinds import KindProposal, PieceKind, WallEnd, propose_kind
from extraction.slot_reader.labels import (
    RowHold,
    counter_break_hold,
    expand_label,
    row_hold,
)
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
from extraction.slot_reader.veto import DrawnReading, drawn_length_vetoes
from extraction.slot_reader.walls import (
    E3_WALL_SETTINGS,
    WALL_PROMPT_ID,
    CodeWallClues,
    HatchSeen,
    WallAnswer,
    WallOutcome,
    WallSettings,
    code_wall_outcome,
    seal_walls,
    wall_pictures,
)
from vocabulary.check_holds import STONE_INTO_WALLS, STONE_SHORT_OF_ENDS
from vocabulary.semantic_types import ProductType
from workflow.form_reader import FormReaderRuntime
from workflow.layout_proposals import (
    WALL_CANDIDATE_TEXT,
    WALL_READER_FLAG,
    WALLS_HELD_FLAG,
    WALLS_SEALED_FLAG,
)

if TYPE_CHECKING:
    from storage.store import ArtifactStore

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
    for name, setting_name in FRACTION_BAR_ENV.items():
        raw = environ.get(name, "").strip()
        if not raw:
            missing.append(name)
            continue
        try:
            values[setting_name] = Decimal(raw)
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
    wall_settings: WallSettings = E3_WALL_SETTINGS
    """The wall pictures' and hatch check's lengths (#992): E3's unless a caller states others."""
    product: ProductType | None = None
    """The drawing set's product, told to each crop reader as one line of context (#994). Set per
    package by the extraction stage; `None` sends exactly the pre-#994 request."""
    question_packets: bool = False
    """Enable storing a private, hash-bound question packet for every reader attempt."""
    spend_cap_usd: Decimal | None = None
    """Per-reading-batch maximum when using the direct Claude route; otherwise unset."""
    claude_row_reader: bool = False
    """Ask Claude Opus to select one of the first six code-ranked rows before reading labels."""
    claude_effort: ClaudeEffort = DEFAULT_CLAUDE_EFFORT
    """The effort every Claude question is asked at (`GV_CLAUDE_READER_EFFORT`, #1051)."""

    @property
    def prompt_id(self) -> str:
        """The crop prompt's recorded identity, naming the product when the requests carry it."""
        return CLAUDE_SPAN_PROMPT_ID if self.claude_row_reader else crop_prompt_id(self.product)

    @property
    def text_lane_reader(self) -> str:
        """The reader shown a text label's crop: the second configured reader (Qwen by default)."""
        return self.form.reader_ids[1]

    @property
    def config_detail(self) -> str:
        """Every setting a reading depends on, in full."""
        return (
            f"crop={self.crop_settings.config_hash};"
            f"fraction_bar={self.fraction_bar.config_hash};"
            f"walls={self.wall_settings.config_hash};"
            "rules=drawn-length-veto,label-expansion,counter-break"
            f"{',claude-span-v2,reject-only,max_concurrent=8,max_tokens=3000' if self.claude_row_reader else ''}"
            + (
                f";claude=effort:{self.claude_effort},structured-output-v1,oversized-image-error,"
                f"upright-sideways:height>={_SIDEWAYS_HEIGHT_TO_WIDTH}xwidth"
                if self.claude_row_reader
                else ""
            )
        )

    @property
    def config_hash(self) -> str:
        """The run's identity: the readers and prompts in words, the rest as a digest.

        A digest because `extraction_runs.config_hash` holds 200 characters and the crop settings
        alone are several times that: the full text made every slot-reader run unstorable.
        """
        detail = hashlib.sha256(self.config_detail.encode()).hexdigest()[:16]
        return (
            f"readers={self.form.reader_ids[0]}|{self.form.reader_ids[1]};"
            f"prompt={self.prompt_id}+{WALL_PROMPT_ID};stacked_agreement={self.allow_stacked};"
            f"question_packets={self.question_packets};"
            f"spend_cap_usd={self.spend_cap_usd};"
            f"detail={detail}"
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
    claude_enabled = bool(getattr(settings, "claude_reader_enabled", False))
    if claude_enabled:
        from extraction.form_reader.pricing import require_priced_readers
        from extraction.slot_reader.anthropic import ThreadLocalAnthropicClients

        model_ids = (
            "anthropic.claude-opus-5-5",
            "anthropic.claude-sonnet-5-5",
        )
        rates = getattr(form, "rates", None)
        require_priced_readers(model_ids, rates)
        rpm = getattr(settings, "claude_reader_model_rpm", {})
        missing = set(model_ids) - set(rpm)
        if missing:
            raise ValueError(
                "GV_CLAUDE_READER_ENABLED requires explicit per-model pacing for: "
                + ", ".join(sorted(missing))
            )
        key = getattr(settings, "anthropic_api_key", None)
        if key is None or not hasattr(key, "get_secret_value"):
            raise ValueError("GV_CLAUDE_READER_ENABLED requires ANTHROPIC_API_KEY")
        key_value = key.get_secret_value()
        if not isinstance(key_value, str) or not key_value.strip():
            raise ValueError("GV_CLAUDE_READER_ENABLED requires ANTHROPIC_API_KEY")
        clients = ThreadLocalAnthropicClients(
            key_value,
            timeout_seconds=int(getattr(settings, "claude_reader_timeout_seconds", 180)),
        )
        effort = getattr(settings, "claude_reader_effort", DEFAULT_CLAUDE_EFFORT)
        if effort not in CLAUDE_EFFORTS:
            raise ValueError(
                f"GV_CLAUDE_READER_EFFORT must be one of {', '.join(CLAUDE_EFFORTS)}: {effort!r}"
            )
        form = replace(
            form,
            reader_ids=model_ids,
            clients=clients,
            calls_per_minute=dict(rpm),
            # The shared spend guard reserves atomically before every call, so the two readers can
            # use the same bounded worker pool. Per-model RPM remains independently paced.
            max_concurrent_calls=8,
            # Claude thinks before it answers: 512 cut replies off mid-JSON (proof run 2026-10-08:
            # 23 truncated or empty answers). Row choice and walls need ~3000; spans fit well inside.
            max_tokens=3000,
        )
    return SlotReaderRuntime(
        form=form,
        crop_settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
        fraction_bar=fraction_bar_from_environment(environ),
        allow_stacked=(
            bool(getattr(settings, "slot_reader_stacked_agreement", False)) or claude_enabled
        ),
        question_packets=claude_enabled,
        spend_cap_usd=(
            getattr(settings, "claude_reader_budget_usd", Decimal("2.50"))
            if claude_enabled
            else None
        ),
        claude_row_reader=claude_enabled,
        claude_effort=effort if claude_enabled else DEFAULT_CLAUDE_EFFORT,
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
    transform: PageTransform | None = None


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
class PageWalls:
    """The row's wall question (#992): the two pictures shown, both answers, code's hatch check,
    and the layout they sealed or why the person decides."""

    row_png: bytes
    view_png: bytes
    row_box_px: tuple[int, int, int, int]
    view_box_px: tuple[int, int, int, int]
    hatch: HatchSeen
    code_clues: CodeWallClues
    answers: tuple[WallAnswer, ...]
    outcome: WallOutcome


@dataclass(frozen=True, slots=True)
class WallQuestion:
    row_png: bytes
    view_png: bytes
    row_box_px: tuple[int, int, int, int]
    view_box_px: tuple[int, int, int, int]
    hatch: HatchSeen
    code_clues: CodeWallClues
    code_outcome: WallOutcome | None


@dataclass(frozen=True, slots=True)
class PageSlotResult:
    page_index: int
    page_id: UUID
    document_version_id: UUID
    plan: SlotPlan
    slots: tuple[OwnerResult, ...]
    overall: OwnerResult | None
    mapping: SlotMapping
    row_hold: RowHold | None = None
    """Field-cut, VIF or a vendor-layer counter-break phrase: the row waits (#992)."""
    check_hold: RowHold | None = None
    """The readings stand but the width check does not apply: the stone does not end at the walls
    (`_stone_end_hold`). Its readings are still offered; only the automatic check abstains."""
    vetoed: tuple[int | None, ...] = ()
    """Sealed readings the drawn length rejected (#992), by slot (`None` for the overall)."""
    walls: PageWalls | None = None
    owner_candidate_ids: Mapping[str, UUID] = field(default_factory=dict)
    wall_candidate_id: UUID | None = None
    row_choice: RowChoiceAnswer | None = None
    row_choice_number: int | None = None
    row_candidate_ids: tuple[UUID, ...] = ()
    row_choice_png: bytes | None = None
    row_choice_box_px: tuple[int, int, int, int] | None = None


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


#: The prototype's full view was a 110 dpi render (bake `full_test_anthropic.py`, 2026-10-08).
_PROTOTYPE_VIEW_DPI: Final = 110
#: The prototype's numbered-rows picture was a 150 dpi render (bake `make_rows.py`, 2026-10-08).
_PROTOTYPE_ROWS_DPI: Final = 150
#: The prototype's close-up margin past each end: 50 px at 300 dpi (12 pt), or 15% of the span.
_CLOSE_UP_MARGIN_PT: Final = Decimal(12)
_CLOSE_UP_MARGIN_FRACTION: Final = Decimal("0.15")
_ZERO_PT: Final = Decimal(0)


def _span_view_png(page: SlotPage, owner: PlannedOwner) -> bytes:
    """The full vendor page with only the chosen span boxed; the box never supplies a value.

    The box is drawn at the prototype's size *as the reader sees it*: the prototype marked a
    110 dpi page (3 px past each end, 14 px above and below, 4 px thick) and shrank it to 1800 px.
    The product marks its 300 dpi render and shrinks it the same way, so every length scales by
    dpi/110; unscaled, the mark on a narrow piece shrank to under a pixel (proof run 2026-10-08).
    """
    scale = Fraction(page.rendered.dpi, _PROTOTYPE_VIEW_DPI)
    first = page.rows.to_pixels(owner.x0, owner.line_y)
    second = page.rows.to_pixels(owner.x1, owner.line_y)
    left, right = sorted((first[0], second[0]))
    center_y = (first[1] + second[1]) // 2
    pad_x, pad_y = round(3 * scale), round(14 * scale)
    outline = (
        max(0, left - pad_x),
        max(0, center_y - pad_y),
        min(page.rendered.width_px, max(left + 1, right + pad_x)),
        min(page.rendered.height_px, center_y + pad_y),
    )
    return _marked_png(
        page.rendered,
        (0, 0, page.rendered.width_px, page.rendered.height_px),
        ends_x=(),
        line=None,
        thickness=max(2, round(4 * scale)),
        max_side=1800,
        outline=outline,
        mark_color=bytes((255, 0, 0)),
    )


def _claude_span_plan(page: SlotPage, plan: SlotPlan) -> SlotPlan:
    """Give Claude exactly one code-defined label span per chosen slot, even if OCR found 0 or >1.

    The span is the candidate's tick-to-tick label band, not an AI-provided location. Replacing
    discovered labels is Claude-only; the original plan is retained separately for text guards.

    The close-up reaches past the span's ends as the prototype's did (at least 50 px at 300 dpi,
    or 15% of the span, a side): a narrow piece's label is wider than the piece, and a close-up
    cut at its ticks showed the readers only "1/2" of a printed 1 1/2 (proof run 2026-10-08).
    Which span is meant stays the full view's red box, never the close-up's edges.
    """
    if plan.row is None:
        return plan

    def one_span(owner: PlannedOwner) -> PlannedOwner:
        band = owner.band
        margin = max(_CLOSE_UP_MARGIN_PT, _CLOSE_UP_MARGIN_FRACTION * (band.x1 - band.x0))
        crop = Box(
            max(_ZERO_PT, band.x0 - margin),
            band.top,
            min(page.rows.ink.width, band.x1 + margin),
            band.bottom,
        )
        label = PlannedLabel(
            box=band,
            crop=crop,
            lane=Lane.GLYPHS,
            text=None,
            text_stacked=False,
            has_digit=True,
            touches_edge=(
                band.x0 <= 0
                or band.top <= 0
                or band.x1 >= page.rows.ink.width
                or band.bottom >= page.rows.ink.height
            ),
            ambiguous_slot=False,
            crowded=False,
            ticks_in_crop=True,
            path_boxes=(),
        )
        return replace(owner, labels=(label,))

    return replace(
        plan,
        slots=tuple(one_span(owner) for owner in plan.slots),
        overall=None if plan.overall is None else one_span(plan.overall),
    )


def _transform_packet(transform: PageTransform | None) -> dict[str, object] | None:
    if transform is None:
        return None
    return {
        "dpi": transform.dpi,
        "rotation": transform.rotation,
        "media_box": [str(value) for value in transform.media_box],
        "crop_box": [str(value) for value in transform.crop_box],
    }


def _question_packet(
    page: SlotPage,
    *,
    question_id: str,
    candidate_id: UUID,
    prompt_id: str,
    full_view_png: bytes,
    close_up_png: bytes,
    store: ArtifactStore | None,
    upright_png: bytes | None = None,
    effort: str | None = None,
) -> dict[str, object]:
    """Bind the exact encoded images, prompt, candidate key and coordinate transform.

    A Claude question also records the effort it was asked at and, for a sideways label, the
    upright third picture (#1051)."""
    from io import BytesIO

    full_hash = hashlib.sha256(full_view_png).hexdigest()
    close_hash = hashlib.sha256(close_up_png).hexdigest()
    upright_hash = None if upright_png is None else hashlib.sha256(upright_png).hexdigest()
    full_key: str | None = None
    close_key: str | None = None
    upright_key: str | None = None
    if store is not None:
        if page.transform is None:
            raise ValueError("a persisted reader packet requires the published PageTransform")
        base = f"reader-questions/{page.document_version_id}/pages/{page.page_index}"
        full_key = f"{base}/full-{full_hash}.png"
        close_key = f"{base}/close-{close_hash}.png"
        full_saved = store.put(full_key, BytesIO(full_view_png), content_type="image/png")
        close_saved = store.put(close_key, BytesIO(close_up_png), content_type="image/png")
        if full_saved.sha256 != full_hash or close_saved.sha256 != close_hash:
            raise ValueError("stored reader question image hash does not match its request bytes")
        if upright_png is not None:
            upright_key = f"{base}/upright-{upright_hash}.png"
            upright_saved = store.put(upright_key, BytesIO(upright_png), content_type="image/png")
            if upright_saved.sha256 != upright_hash:
                raise ValueError("stored upright question image hash does not match its bytes")
    packet_body: dict[str, object] = {
        "question_id": question_id,
        "document_version_id": str(page.document_version_id),
        "page_index": page.page_index,
        "source_page_sha256": page.rendered.page_content_hash,
        "prompt_id": prompt_id,
        "candidate_ids": [str(candidate_id)],
        "page_transform": _transform_packet(page.transform),
        "images": {
            "full_view": {"sha256": full_hash, "storage_key": full_key},
            "close_up": {"sha256": close_hash, "storage_key": close_key},
            **(
                {}
                if upright_png is None
                else {"upright_close_up": {"sha256": upright_hash, "storage_key": upright_key}}
            ),
        },
        **({} if effort is None else {"effort": effort}),
    }
    packet_body["packet_sha256"] = hashlib.sha256(
        json.dumps(packet_body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return packet_body


def _row_question_packet(
    page: SlotPage,
    *,
    candidate_ids: Sequence[UUID],
    numbered_view_png: bytes,
    store: ArtifactStore | None,
    effort: str | None = None,
) -> dict[str, object]:
    """Bind the exact numbered vendor view and the ordered code candidates to a row attempt."""
    from io import BytesIO

    digest = hashlib.sha256(numbered_view_png).hexdigest()
    storage_key: str | None = None
    if store is not None:
        if page.transform is None:
            raise ValueError("a persisted reader packet requires the published PageTransform")
        storage_key = (
            f"reader-questions/{page.document_version_id}/pages/{page.page_index}/"
            f"rows-{digest}.png"
        )
        saved = store.put(storage_key, BytesIO(numbered_view_png), content_type="image/png")
        if saved.sha256 != digest:
            raise ValueError("stored numbered row image hash does not match its request bytes")
    packet: dict[str, object] = {
        "question_id": f"p{page.page_index}:row-choice",
        "document_version_id": str(page.document_version_id),
        "page_index": page.page_index,
        "source_page_sha256": page.rendered.page_content_hash,
        "prompt_id": ROW_PROMPT_ID,
        "candidate_ids": [str(candidate_id) for candidate_id in candidate_ids],
        "candidate_count": len(candidate_ids),
        "page_transform": _transform_packet(page.transform),
        "images": {"numbered_vendor_view": {"sha256": digest, "storage_key": storage_key}},
        **({} if effort is None else {"effort": effort}),
    }
    packet["packet_sha256"] = hashlib.sha256(
        json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return packet


def _owner_candidate_key(owner_index: int | None) -> str:
    return "overall" if owner_index is None else f"slot:{owner_index}"


#: E3's mark colour for the row and its ends: a colour neither the vendor's black nor GV's red,
#: yellow or blue uses.
_MAGENTA: Final = bytes((230, 0, 200))
_ROW_COLOURS: Final = (
    bytes((220, 20, 60)),
    bytes((0, 100, 220)),
    bytes((0, 135, 70)),
    bytes((145, 70, 190)),
    bytes((220, 125, 0)),
    bytes((0, 135, 150)),
)


@functools.lru_cache(maxsize=64)
def _digit_mask(number: int, width: int, height: int) -> tuple[tuple[bool, ...], ...]:
    """Which pixels of a `width` x `height` badge interior the number's strokes cover.

    Drawn with Pillow's own scalable typeface, sized so the digit fills the interior. The badge
    used a 3x5-dot pattern until a proof run (2026-10-09) where both Claude readers read the dotted
    "3" as "8", a box that did not exist, and the page went to the reviewer. A real typeface keeps
    every digit's shape distinct at the size the reader sees.
    """
    from PIL import Image, ImageDraw, ImageFont

    text = str(number)
    size = height
    font = ImageFont.load_default(size=size)
    while size > 4:
        font = ImageFont.load_default(size=size)
        left, top, right, bottom = font.getbbox(text)
        if right - left <= width and bottom - top <= height:
            break
        size -= 1
    image = Image.new("L", (width, height), 0)
    ImageDraw.Draw(image).text((width / 2, height / 2), text, fill=255, font=font, anchor="mm")
    data = image.tobytes()
    return tuple(tuple(data[y * width + x] >= 128 for x in range(width)) for y in range(height))


def _marked_png(
    rendered: RenderedPage,
    box: tuple[int, int, int, int],
    *,
    ends_x: Sequence[int],
    line: tuple[int, int, int] | None,
    thickness: int,
    max_side: int,
    outline: tuple[int, int, int, int] | None = None,
    mark_color: bytes = _MAGENTA,
    numbered_outlines: Sequence[tuple[tuple[int, int, int, int], bytes, int]] = (),
    badge_scale: int | None = None,
    badge_beside: bool = False,
) -> bytes:
    """A crop of the render with the row marked (E3's pictures), shrunk to `max_side` at most.

    `ends_x` are page-pixel columns drawn top to bottom; `line` is `(x0, x1, y)` in page pixels.
    A numbered outline's badge is drawn `badge_scale` pixels per font dot; with `badge_beside`
    it stands just left of its box, centred on it and clear of earlier badges, so it never
    covers the row's own labels.
    """
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    rgb = bytearray(_crop_rgb(rendered, box))
    stride = width * 3

    def paint(x0: int, y0: int, x1: int, y1: int, colour: bytes = mark_color) -> None:
        x0, x1 = max(0, x0), min(width, x1)
        y0, y1 = max(0, y0), min(height, y1)
        if x0 >= x1 or y0 >= y1:
            return
        run = colour * (x1 - x0)
        for row in range(y0, y1):
            rgb[row * stride + x0 * 3 : row * stride + x1 * 3] = run

    half = thickness // 2
    for x in ends_x:
        paint(x - left - half, 0, x - left - half + thickness, height)
    if line is not None:
        x0, x1, y = line
        paint(x0 - left, y - top - half, x1 - left, y - top - half + thickness)
    if outline is not None:
        x0, y0, x1, y1 = outline
        paint(x0 - left, y0 - top, x1 - left, y0 - top + thickness)
        paint(x0 - left, y1 - top - thickness, x1 - left, y1 - top)
        paint(x0 - left, y0 - top, x0 - left + thickness, y1 - top)
        paint(x1 - left - thickness, y0 - top, x1 - left, y1 - top)
    placed_badges: list[tuple[int, int, int, int]] = []
    for (x0, y0, x1, y1), colour, number in numbered_outlines:
        paint(x0 - left, y0 - top, x1 - left, y0 - top + thickness, colour)
        paint(x0 - left, y1 - top - thickness, x1 - left, y1 - top, colour)
        paint(x0 - left, y0 - top, x0 - left + thickness, y1 - top, colour)
        paint(x1 - left - thickness, y0 - top, x1 - left, y1 - top, colour)
        scale = badge_scale or max(2, rendered.dpi // 72)
        # The badge keeps the measured 3x5-cell size; only the digit inside is a real typeface.
        badge_width = 3 * scale + 2 * scale
        badge_height = 5 * scale + 2 * scale
        if badge_beside:
            badge_top = max(0, (y0 + y1) // 2 - top - badge_height // 2)
            badge_left = x0 - left - scale - badge_width
            while badge_left >= 0 and any(
                badge_left < other_x1
                and other_x0 < badge_left + badge_width
                and badge_top < other_y1
                and other_y0 < badge_top + badge_height
                for other_x0, other_y0, other_x1, other_y1 in placed_badges
            ):
                badge_left -= badge_width + scale
            if badge_left < 0:
                badge_left = max(0, x0 - left + thickness)
        else:
            badge_left = max(0, x0 - left + thickness)
            badge_top = max(0, y0 - top + thickness)
        placed_badges.append(
            (badge_left, badge_top, badge_left + badge_width, badge_top + badge_height)
        )
        paint(badge_left, badge_top, badge_left + badge_width, badge_top + badge_height, colour)
        mask = _digit_mask(number, 3 * scale, 5 * scale)
        for glyph_y, mask_row in enumerate(mask):
            for glyph_x, inked in enumerate(mask_row):
                if inked:
                    gx = badge_left + scale + glyph_x
                    gy = badge_top + scale + glyph_y
                    paint(gx, gy, gx + 1, gy + 1, bytes((255, 255, 255)))
    factor = -(-max(width, height) // max_side)
    if factor <= 1:
        return encode_png(width, height, bytes(rgb))
    import numpy as np

    pixels = np.frombuffer(bytes(rgb), dtype=np.uint8).reshape(height, width, 3)
    small_h, small_w = height // factor, width // factor
    trimmed = pixels[: small_h * factor, : small_w * factor].astype(np.uint32)
    shrunk = trimmed.reshape(small_h, factor, small_w, factor, 3).sum(axis=(1, 3))
    shrunk = (shrunk + (factor * factor) // 2) // (factor * factor)
    return encode_png(small_w, small_h, shrunk.astype(np.uint8).tobytes())


def _numbered_rows_png(page: SlotPage) -> tuple[bytes, tuple[CountertopRowCandidate, ...]]:
    """Show the top six code-ranked candidate dimension lines, each in a distinct numbered box.

    Drawn at the prototype's size as the reader sees it (bake `make_rows.py`, 2026-10-08: a
    150 dpi page, boxes 5 px thick reaching 6 px past the ends and 14 px above and below, and a
    48 px number tag just left of each box), scaled by dpi/150. The product had drawn 2 px boxes
    and a 3x5-dot number inside the box, then shrunk the page about 3x: a reader named the right
    row by its words but gave another box's number (proof run 2026-10-08).
    """
    candidates = page.rows.candidates.rows.candidates[:6]
    if not candidates:
        return _crop_png(page.rendered, (0, 0, page.rendered.width_px, page.rendered.height_px)), ()
    scale = Fraction(page.rendered.dpi, _PROTOTYPE_ROWS_DPI)
    pad_x, pad_y = round(6 * scale), round(14 * scale)
    outlines: list[tuple[tuple[int, int, int, int], bytes, int]] = []
    for number, row in enumerate(candidates, start=1):
        first = page.rows.to_pixels(row.x0, row.y)
        last = page.rows.to_pixels(row.x1, row.y)
        x0, x1 = sorted((first[0], last[0]))
        center_y = (first[1] + last[1]) // 2
        outlines.append(
            (
                (
                    max(0, x0 - pad_x),
                    max(0, center_y - pad_y),
                    min(page.rendered.width_px, x1 + pad_x),
                    min(page.rendered.height_px, center_y + pad_y),
                ),
                _ROW_COLOURS[number - 1],
                number,
            )
        )
    full_page = (0, 0, page.rendered.width_px, page.rendered.height_px)
    image = _marked_png(
        page.rendered,
        full_page,
        ends_x=(),
        line=None,
        thickness=max(2, round(5 * scale)),
        max_side=1800,
        numbered_outlines=outlines,
        badge_scale=max(2, round(48 * scale / 7)),
        badge_beside=True,
    )
    return image, candidates


def _wall_job_pictures(
    page: SlotPage, plan: SlotPlan, settings: WallSettings
) -> tuple[bytes, bytes, tuple[int, int, int, int], tuple[int, int, int, int], HatchSeen] | None:
    """The row picture and the vendor view for the wall question, and code's hatch check."""
    row = plan.row
    if row is None:
        return None
    pictures = wall_pictures(row, page.rows.ink, settings=settings)
    row_px = _pixels(page.rows, pictures.row, page.rendered)
    view_px = _pixels(page.rows, pictures.view, page.rendered)
    left_end = page.rows.to_pixels(row.x0, row.y)
    right_end = page.rows.to_pixels(row.x1, row.y)
    thickness = max(2, page.rendered.dpi // 100)
    row_png = _marked_png(
        page.rendered,
        row_px,
        ends_x=(left_end[0], right_end[0]),
        line=(left_end[0], right_end[0], left_end[1]),
        thickness=thickness + 1,
        max_side=settings.picture_max_side_px,
    )
    view_png = _marked_png(
        page.rendered,
        view_px,
        ends_x=(),
        line=(left_end[0], right_end[0], left_end[1]),
        thickness=thickness + 2,
        max_side=settings.picture_max_side_px,
    )
    return row_png, view_png, row_px, view_px, pictures.hatch


#: A label run at least this many times as tall as it is wide is drawn sideways (#1051). Measured on
#: both client sets (2026-10-09, every label run on the first six candidate rows of every page):
#: upright single-line labels are wider than tall; a millimetre line over its [inch] line or a stacked
#: fraction stands up to 1.9 times as tall as wide, and stacked fractions are excluded by name below.
_SIDEWAYS_HEIGHT_TO_WIDTH: Final = Decimal(2)


def _sideways(label: PlannedLabel, height: Decimal, geometry: FractionBarGeometry) -> bool:
    """Whether a label run stands sideways on its horizontal row: clearly taller than wide, and not
    a stacked fraction (set as text, or found by the fraction-bar detector), which has its own
    handling and is never turned."""
    box = label.box
    return (
        box.width > 0
        and box.height >= _SIDEWAYS_HEIGHT_TO_WIDTH * box.width
        and not label.text_stacked
        and not _stacked_by_bar(label, height, geometry)
    )


def _upright_png(png: bytes) -> bytes:
    """The picture turned a quarter clockwise: a label reading bottom to top then stands upright.

    Every sideways label on both client sets reads bottom to top (the vendors' and architects'
    vertical dimensions, rendered and checked 2026-10-09), so one turn is enough."""
    import numpy as np

    width, height, rgb = decode_rgb_png(png)
    pixels = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3)
    turned = np.ascontiguousarray(np.rot90(pixels, k=-1))
    return encode_png(height, width, turned.tobytes())


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
    store: ArtifactStore | None = None,
) -> tuple[PageSlotResult, ...]:
    """Read every page's slots: plan, crop, ask the readers in parallel, seal, name, map.

    Since #992, admin-approved rules sit between sealing and mapping, each only holding back:
    a row whose texts say `INCLUDING FIELD CUT` or `VIF` offers nothing; a sealed reading the drawn
    length rejects goes back to the person; a vendor counter-break phrase in the row's pasted
    drawing and slot span holds the whole
    row; and the row's wall question is asked in the same batch
    (same pacer and limits) and sealed only when both readers agree.

    `wall_ends` says which ends of a page's row stand against a wall *for naming a piece's kind*;
    the sealed walls are not fed to it — the wall-end kind rule is not decided (#987) — so by
    default none does and no piece takes its kind from its position.
    """
    if runtime.question_packets and store is None:
        raise ValueError("reader question packets require a private artifact store")
    readers = runtime.form.reader_ids
    page_rows = {page.page_index: page.rows.candidates.rows for page in pages}
    row_images: dict[int, bytes] = {}
    row_ids: dict[int, tuple[UUID, ...]] = {}
    row_jobs: list[CropJob] = []
    if runtime.claude_row_reader:
        if not readers or readers[0] != "anthropic.claude-opus-5-5":
            raise ValueError("Claude row selection requires Opus as the first configured reader")
        for page in pages:
            candidates = page_rows[page.page_index].candidates[:6]
            if not candidates:
                continue
            numbered_png, _ = _numbered_rows_png(page)
            candidate_ids = tuple(uuid4() for _ in candidates)
            row_images[page.page_index] = numbered_png
            row_ids[page.page_index] = candidate_ids
            packet = (
                _row_question_packet(
                    page,
                    candidate_ids=candidate_ids,
                    numbered_view_png=numbered_png,
                    store=store,
                    effort=runtime.claude_effort,
                )
                if runtime.question_packets
                else None
            )
            # Both readers are asked the same question with the same picture; a row is used only
            # when they name the same one (`_agreed_row`).
            row_jobs.extend(
                CropJob(
                    key=_row_key(page.page_index),
                    model_id=model,
                    page_index=page.page_index,
                    png=numbered_png,
                    row_question=True,
                    candidate_count=len(candidates),
                    question_packet=packet,
                )
                for model in readers
            )
    shared_spend_guard = None
    shared_pacer = ModelPacer(runtime.form.calls_per_minute)
    if runtime.spend_cap_usd is not None:
        from extraction.slot_reader.anthropic import BatchSpendGuard

        if runtime.form.rates is None:
            raise ValueError("Claude reader spend cap requires published model rates")
        shared_spend_guard = BatchSpendGuard(runtime.spend_cap_usd, runtime.form.rates)

    def run_jobs(
        items: Sequence[CropJob],
    ) -> dict[
        tuple[str, str],
        ReaderAnswer | WallAnswer | RowChoiceAnswer | CounterBreakAnswer | None,
    ]:
        return read_crops_parallel(
            items,
            clients=runtime.form.clients,
            rates=runtime.form.rates,
            calls_per_minute=runtime.form.calls_per_minute,
            max_concurrent_calls=runtime.form.max_concurrent_calls,
            max_tokens=runtime.form.max_tokens,
            max_throttle_retries=runtime.form.max_throttle_retries,
            retry_backoff_seconds=runtime.form.retry_backoff_seconds,
            record_attempt=record_attempt,
            product=runtime.product,
            spend_guard=shared_spend_guard,
            spend_cap_usd=runtime.spend_cap_usd if shared_spend_guard is None else None,
            pacer=shared_pacer,
            claude_effort=runtime.claude_effort,
        )

    row_answers = run_jobs(row_jobs) if row_jobs else {}
    planned: list[tuple[SlotPage, SlotPlan, SlotPlan, RowChoiceAnswer | None, int | None]] = []
    jobs: list[CropJob] = []
    crops: dict[str, tuple[tuple[int, int, int, int], tuple[int, int, int, int], InkAt | None]] = {}
    walls_asked: dict[int, WallQuestion] = {}
    owner_candidate_ids: dict[int, dict[str, UUID]] = {}
    wall_candidate_ids: dict[int, UUID] = {}
    for page in pages:
        if runtime.question_packets and page.transform is None:
            raise ValueError("reader question packets require the published PageTransform")
        if page.ink is not None and page.ink.dpi != page.rendered.dpi:
            raise ValueError(
                f"page {page.page_index}'s ink was read at {page.ink.dpi} dpi but its picture is "
                f"at {page.rendered.dpi} dpi"
            )
        row_choice, row_number = _agreed_row(
            [row_answers.get((_row_key(page.page_index), model)) for model in readers]
            if runtime.claude_row_reader and page.page_index in row_ids
            else []
        )
        candidates = page_rows[page.page_index].candidates[:6]
        selected_row = (
            candidates[row_number - 1]
            if row_number is not None and 0 < row_number <= len(candidates)
            else None
        )
        source_plan = plan_slots(
            page.rows.candidates.rows,
            page.rows.ink,
            settings=runtime.crop_settings,
            row_settings=runtime.row_settings,
            selected_row=selected_row,
            row_choice_made=runtime.claude_row_reader and bool(candidates),
        )
        plan = _claude_span_plan(page, source_plan) if runtime.claude_row_reader else source_plan
        planned.append((page, plan, source_plan, row_choice, row_number))
        owners = [*plan.slots, *([plan.overall] if plan.overall is not None else [])]
        page_candidate_ids = {_owner_candidate_key(owner.index): uuid4() for owner in owners}
        owner_candidate_ids[page.page_index] = page_candidate_ids
        # A row the vendor's own words already hold (a tall appliance or range bay in its span) can
        # never become a proposal, so it is not read at paid prices; the hold is applied below
        # exactly as before. Claude path only.
        held_before_reading = (
            runtime.claude_row_reader
            and plan.row is not None
            and _counter_break_row_hold(page, plan, (), runtime) is not None
        )
        # Spans whose label code found drawn sideways (#1051): the Claude span is the slot's band,
        # so the label runs come from the plan code made before the spans replaced them.
        sideways_owners = (
            {
                owner.index
                for owner in (
                    *source_plan.slots,
                    *((source_plan.overall,) if source_plan.overall is not None else ()),
                )
                if any(
                    _sideways(label, page.rows.ink.height, runtime.fraction_bar)
                    for label in owner.labels
                )
            }
            if runtime.claude_row_reader
            else set()
        )
        for owner in owners:
            for position, label in enumerate(owner.labels):
                key = _key(page.page_index, owner.index, position)
                box_px = _pixels(page.rows, label.box, page.rendered)
                crop_px = _pixels(page.rows, label.crop, page.rendered)
                ink = None if page.ink is None else page.ink.at(crop_px)
                crops[key] = (box_px, crop_px, ink)
                if held_before_reading:
                    continue
                if not runtime.claude_row_reader and (
                    _hard_guarded(label, ink) or not label.has_digit
                ):
                    continue
                if label.lane is Lane.TEXT:
                    if label.text is None:
                        continue
                    printed = normalise_text(label.text)
                    # A plain dimension, or one of the two worded forms code may expand (#992):
                    # either needs the reader to print the same text before it counts.
                    if plain_dimension(printed) is None and expand_label(printed) is None:
                        continue
                    wanted: tuple[str, ...] = (
                        readers if runtime.claude_row_reader else (runtime.text_lane_reader,)
                    )
                else:
                    wanted = readers
                png = _crop_png(page.rendered, crop_px)
                view_png = (
                    _span_view_png(page, owner)
                    if runtime.claude_row_reader or runtime.question_packets
                    else None
                )
                upright_png = (
                    _upright_png(png)
                    if runtime.claude_row_reader and owner.index in sideways_owners
                    else None
                )
                if runtime.question_packets:
                    assert view_png is not None
                    packet = _question_packet(
                        page,
                        question_id=key,
                        candidate_id=page_candidate_ids[_owner_candidate_key(owner.index)],
                        prompt_id=runtime.prompt_id,
                        full_view_png=view_png,
                        close_up_png=png,
                        store=store,
                        upright_png=upright_png,
                        effort=runtime.claude_effort if runtime.claude_row_reader else None,
                    )
                    jobs.extend(
                        CropJob(
                            key,
                            model,
                            page.page_index,
                            png,
                            view_png,
                            grounded_claude=runtime.claude_row_reader,
                            question_packet=packet,
                            upright_png=upright_png,
                        )
                        for model in wanted
                    )
                else:
                    jobs.extend(
                        CropJob(
                            key,
                            model,
                            page.page_index,
                            png,
                            view_png=view_png,
                            grounded_claude=runtime.claude_row_reader,
                            upright_png=upright_png,
                        )
                        for model in wanted
                    )
        wall_pictures_for = (
            None if held_before_reading else _wall_job_pictures(page, plan, runtime.wall_settings)
        )
        if wall_pictures_for is not None:
            code_clues = _code_wall_clues(page, plan, wall_pictures_for)
            code_outcome = code_wall_outcome(code_clues, row_ambiguity=plan.ambiguity)
            walls_asked[page.page_index] = WallQuestion(
                row_png=wall_pictures_for[0],
                view_png=wall_pictures_for[1],
                row_box_px=wall_pictures_for[2],
                view_box_px=wall_pictures_for[3],
                hatch=wall_pictures_for[4],
                code_clues=code_clues,
                code_outcome=code_outcome,
            )
            wall_candidate_ids[page.page_index] = uuid4()
            row_png, view_png = wall_pictures_for[0], wall_pictures_for[1]
            if runtime.claude_row_reader:
                counter_break_key = _counter_break_key(page.page_index)
                counter_break_packet = (
                    _question_packet(
                        page,
                        question_id=counter_break_key,
                        candidate_id=wall_candidate_ids[page.page_index],
                        prompt_id=COUNTER_BREAK_PROMPT_ID,
                        full_view_png=view_png,
                        close_up_png=row_png,
                        store=store,
                        effort=runtime.claude_effort,
                    )
                    if runtime.question_packets
                    else None
                )
                jobs.extend(
                    CropJob(
                        counter_break_key,
                        model,
                        page.page_index,
                        row_png,
                        view_png,
                        counter_break_question=True,
                        question_packet=counter_break_packet,
                    )
                    for model in readers
                )
            if code_outcome is not None:
                continue
            wall_packet = (
                _question_packet(
                    page,
                    question_id=_walls_key(page.page_index),
                    candidate_id=wall_candidate_ids[page.page_index],
                    prompt_id=WALL_PROMPT_ID,
                    full_view_png=view_png,
                    close_up_png=row_png,
                    store=store,
                    effort=runtime.claude_effort if runtime.claude_row_reader else None,
                )
                if runtime.question_packets
                else None
            )
            jobs.extend(
                CropJob(
                    _walls_key(page.page_index),
                    model,
                    page.page_index,
                    row_png,
                    view_png,
                    wall_question=True,
                    question_packet=wall_packet,
                )
                for model in readers
            )

    answers = run_jobs(jobs)

    label_answers: dict[tuple[str, str], ReaderAnswer | None] = {
        key: answer for key, answer in answers.items() if isinstance(answer, ReaderAnswer)
    }
    results: list[PageSlotResult] = []
    for page, plan, source_plan, row_choice, row_number in planned:

        def owner_result(
            owner: PlannedOwner, count: int, page: SlotPage = page, plan: SlotPlan = plan
        ) -> OwnerResult:
            return _owner_result(
                page,
                plan,
                owner,
                count,
                crops=crops,
                answers=label_answers,
                runtime=runtime,
                wall_ends=wall_ends(page),
            )

        slots = tuple(owner_result(owner, len(plan.slots)) for owner in plan.slots)
        overall = None if plan.overall is None else owner_result(plan.overall, len(plan.slots))
        hold = _counter_break_row_hold(page, plan, slots, runtime)
        line_answers = tuple(
            answer
            for model in readers
            if isinstance(
                answer := answers.get((_counter_break_key(page.page_index), model)),
                CounterBreakAnswer,
            )
        )
        if hold is None and any(answer.contains_tall_appliance for answer in line_answers):
            # Positive-only: this answer can hold a row, never clear an existing hold.
            hold = counter_break_hold(("REFRIGERATOR",))
        check_hold = (
            _stone_end_hold(page, plan, line_answers)
            if hold is None and runtime.claude_row_reader
            else None
        )
        if hold is None:
            source_texts = [
                label.text
                for owner in (
                    *source_plan.slots,
                    *((source_plan.overall,) if source_plan.overall else ()),
                )
                for label in owner.labels
                if label.text
            ]
            hold = row_hold(
                [
                    *_row_texts([*slots, *((overall,) if overall is not None else ())]),
                    *source_texts,
                ]
            )
        slots, overall, vetoed = _veto_by_drawn_length(slots, overall)
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
            row_hold=None if hold is None else hold.reason,
            allow_partial_proposals=runtime.claude_row_reader,
        )
        walls: PageWalls | None = None
        asked = walls_asked.get(page.page_index)
        if asked is not None:
            row_png, view_png = asked.row_png, asked.view_png
            wall_answers = (
                ()
                if asked.code_outcome is not None
                else tuple(
                    answer
                    for model in readers
                    if isinstance(
                        answer := answers.get((_walls_key(page.page_index), model)), WallAnswer
                    )
                )
            )
            code_clues = (
                _read_wall_clues(slots, asked.code_clues)
                if runtime.claude_row_reader and asked.code_outcome is None
                else asked.code_clues
            )
            wall_outcome = asked.code_outcome or seal_walls(
                wall_answers,
                hatch=asked.hatch,
                row_ambiguity=plan.ambiguity,
                code_clues=code_clues,
                allow_claude_pair=runtime.claude_row_reader,
            )
            walls = PageWalls(
                row_png=row_png,
                view_png=view_png,
                row_box_px=asked.row_box_px,
                view_box_px=asked.view_box_px,
                answers=wall_answers,
                hatch=asked.hatch,
                code_clues=code_clues,
                outcome=wall_outcome,
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
                row_hold=hold,
                check_hold=check_hold,
                vetoed=vetoed,
                walls=walls,
                owner_candidate_ids=owner_candidate_ids[page.page_index],
                wall_candidate_id=wall_candidate_ids.get(page.page_index),
                row_choice=row_choice,
                row_choice_number=row_number,
                row_candidate_ids=row_ids.get(page.page_index, ()),
                row_choice_png=row_images.get(page.page_index),
                row_choice_box_px=(
                    (0, 0, page.rendered.width_px, page.rendered.height_px)
                    if page.page_index in row_images
                    else None
                ),
            )
        )
    return tuple(results)


def _walls_key(page_index: int) -> str:
    return f"p{page_index}:walls"


#: How far inside the row's ends a "wall to wall" line must sit to mean the stone runs past the
#: walls, in page points: more than a stroke or a tick's width, so two lines drawn to the same
#: wall face never trigger it.
_WALL_TO_WALL_INSET_PT: Final = Decimal(1)


def _stone_end_hold(
    page: SlotPage, plan: SlotPlan, answers: Sequence[CounterBreakAnswer]
) -> RowHold | None:
    """Hold a row whose stone does not end at the walls; the field cut is not the stone's there.

    Raj's field cut is added to wall-to-wall where the stone meets a wall. Where the stone stops
    at full-height fillers or panels, they take it; where it runs into wall pockets, the pocket
    detail decides (GV-Brain "Field cut - when it applies", 2026-10-08). Either way the width
    check's arithmetic would not apply, so the row goes to the reviewer. Hold-only: nothing here
    can clear a hold or approve a row.

    Code first: a line the vendor labels "wall to wall" that sits inside both ends of the row
    means the stone runs past the wall faces. Then either reader saying the stone stops short of
    an end, or runs into the walls.
    """
    row = plan.row
    if row is None:
        return None
    rows = page.rows.candidates.rows
    for other in (*rows.candidates, *(rejected for rejected in rows.rejected)):
        spans: list[tuple[Decimal, Decimal, tuple[str, ...]]] = [
            (slot.x0, slot.x1, label.lines) for slot in other.slots for label in slot.labels
        ]
        if other.overall is not None:
            spans.extend(
                (other.overall.x0, other.overall.x1, label.lines) for label in other.overall.labels
            )
        for x0, x1, lines in spans:
            words = " ".join(lines).lower()
            if "wall to wall" not in words and "wall-to-wall" not in words:
                continue
            if row.x0 + _WALL_TO_WALL_INSET_PT < x0 and x1 < row.x1 - _WALL_TO_WALL_INSET_PT:
                return RowHold(*STONE_INTO_WALLS, " ".join(lines))
    said = {answer.stone_ends for answer in answers}
    if "into_walls" in said:
        return RowHold(*STONE_INTO_WALLS, "a reader: the stone runs into the walls")
    if "short_of_ends" in said:
        return RowHold(*STONE_SHORT_OF_ENDS, "a reader: the stone stops before the row's ends")
    return None


def _counter_break_key(page_index: int) -> str:
    return f"p{page_index}:counter-break"


def _row_key(page_index: int) -> str:
    return f"p{page_index}:row-choice"


def _row_texts(owners: Sequence[OwnerResult]) -> list[str]:
    """Every text any source gave for any label of the row: the file's, each reader's, the agreed."""
    texts: list[str] = []
    for owner in owners:
        for item in owner.labels:
            if item.label.text:
                texts.append(item.label.text)
            texts.extend(text for _source, text in item.outcome.reader_texts)
            if item.outcome.sealed_text:
                texts.append(item.outcome.sealed_text)
    return texts


def _line_phrases(
    words: Sequence[InkLabel], *, gap_px: int, baseline_fraction: Decimal
) -> list[str]:
    """Join vendor words on the same baseline when their gap is within E2's fragment reach."""
    return [
        phrase
        for phrase, _left, _right, _baseline in _line_phrase_records(
            words, gap_px=gap_px, baseline_fraction=baseline_fraction
        )
    ]


def _line_phrase_records(
    words: Sequence[InkLabel], *, gap_px: int, baseline_fraction: Decimal
) -> list[tuple[str, int, int, int]]:
    """The same phrases plus their horizontal extent, for matching wall clues to row ends."""
    lines: list[list[InkLabel]] = []
    for word in sorted(words, key=lambda item: (item.box[3], item.box[0])):
        height = max(1, word.box[3] - word.box[1])
        match = next(
            (
                line
                for line in lines
                if abs(line[0].box[3] - word.box[3])
                <= baseline_fraction * min(height, max(1, line[0].box[3] - line[0].box[1]))
            ),
            None,
        )
        if match is None:
            lines.append([word])
        else:
            match.append(word)
    phrases: list[tuple[str, int, int, int]] = []
    for line in lines:
        ordered = sorted(line, key=lambda item: item.box[0])
        phrase = [ordered[0].text]
        phrase_left = ordered[0].box[0]
        phrase_right = ordered[0].box[2]
        baseline = (ordered[0].box[1] + ordered[0].box[3]) // 2
        previous = ordered[0]
        for word in ordered[1:]:
            if word.box[0] - previous.box[2] > gap_px:
                phrases.append((" ".join(phrase), phrase_left, phrase_right, baseline))
                phrase = []
                phrase_left = word.box[0]
                baseline = (word.box[1] + word.box[3]) // 2
            phrase.append(word.text)
            phrase_right = word.box[2]
            previous = word
        phrases.append((" ".join(phrase), phrase_left, phrase_right, baseline))
    return phrases


def _code_wall_clues(
    page: SlotPage,
    plan: SlotPlan,
    wall_pictures_for: tuple[
        bytes, bytes, tuple[int, int, int, int], tuple[int, int, int, int], HatchSeen
    ],
) -> CodeWallClues:
    """Recognise only positive vendor clues at row ends; missing text never means an open end."""
    left = right = False
    hatch = wall_pictures_for[4]
    left = hatch.left
    right = hatch.right
    if page.ink is None or plan.row is None or not plan.slots:
        return CodeWallClues(left=left or None, right=right or None)

    view = wall_pictures_for[3]
    view_left, view_top, view_right, view_bottom = view
    start = page.rows.to_pixels(plan.row.x0, plan.row.y)[0]
    end = page.rows.to_pixels(plan.row.x1, plan.row.y)[0]
    low, high = sorted((start, end))
    gap_px = int(E2_CROP_SETTINGS.fragment_gap_pt * Decimal(page.rendered.dpi) / 72)
    vendor_words = [
        word
        for word in page.ink.labels
        if word.ink is InkClass.VENDOR
        and view_left <= (word.box[0] + word.box[2]) // 2 <= view_right
        and view_top <= (word.box[1] + word.box[3]) // 2 <= view_bottom
        and low <= (word.box[0] + word.box[2]) // 2 <= high
    ]
    phrases = _line_phrase_records(
        vendor_words,
        gap_px=gap_px,
        baseline_fraction=E2_CROP_SETTINGS.baseline_fraction,
    )
    for phrase, phrase_left, phrase_right, center_y in phrases:
        normalized = re.sub(r"[^a-z]+", " ", phrase.lower()).strip()
        center = (phrase_left + phrase_right) // 2
        if normalized == "wall to wall":
            left = right = True
        endpoints: tuple[tuple[PlannedOwner, str], ...] = (
            (plan.slots[0], "left"),
            (plan.slots[-1], "right"),
        )
        if plan.slots[0].index == plan.slots[-1].index:
            distances = (abs(center - low), abs(high - center))
            endpoints = ((plan.slots[0], "left" if distances[0] < distances[1] else "right"),)
            if distances[0] == distances[1]:
                endpoints = ()
        for owner, side in endpoints:
            first = page.rows.to_pixels(owner.x0, plan.row.y)[0]
            last = page.rows.to_pixels(owner.x1, plan.row.y)[0]
            slot_left, slot_right = sorted((first, last))
            band_left, band_top, band_right, band_bottom = _pixels(
                page.rows, owner.band, page.rendered
            )
            in_slot = (
                slot_left <= center <= slot_right
                and band_left <= center <= band_right
                and band_top <= center_y <= band_bottom
            )
            if in_slot and re.search(r"\b(?:filler|field\s+cut|wall)\b", normalized):
                if side == "left":
                    left = True
                else:
                    right = True

    # The slot labels are also vendor clues, but their text and ink must both be from the file.
    if plan.slots:
        label_endpoints: tuple[tuple[PlannedOwner, str], ...] = (
            (plan.slots[0], "left"),
            (plan.slots[-1], "right"),
        )
        if plan.slots[0].index == plan.slots[-1].index:
            label_endpoints = ()
        for owner, side in label_endpoints:
            for label in owner.labels:
                if not label.text or not re.search(
                    r"\b(?:filler|field\s+cut|wall)\b", label.text, re.IGNORECASE
                ):
                    continue
                ink = page.ink.at(_pixels(page.rows, label.crop, page.rendered))
                if ink.ink is InkClass.VENDOR:
                    if side == "left":
                        left = True
                    else:
                        right = True
    return CodeWallClues(left=left or None, right=right or None)


_WALL_WORD: Final = re.compile(r"\b(?:filler|field\s+cut|wall)\b", re.IGNORECASE)


def _read_wall_clues(slots: tuple[OwnerResult, ...], clues: CodeWallClues) -> CodeWallClues:
    """Raj's row-end rule again, on the end labels the two readers sealed.

    `_code_wall_clues` reads only the file's own text, and a vendor that draws its words as lines
    hides every "Filler" from it (proof run 2026-10-08: a filler sum at both ends of a row, sealed
    by both readers, while the walls went to the person). A sealed reading on vendor ink, after
    the drawn-length veto, is evidence of the same standing as the file's text. Only positive
    clues are added: an end without the word stays whatever the drawing already said.
    """
    if len(slots) < 2:
        return clues
    left, right = clues.left, clues.right
    for owner, side in ((slots[0], "left"), (slots[-1], "right")):
        position = owner.outcome.label_index
        if owner.outcome.state is not LabelState.SEALED or position is None:
            continue
        outcome = owner.labels[position].outcome
        if outcome.ink is not InkClass.VENDOR or not outcome.sealed_text:
            continue
        if _WALL_WORD.search(outcome.sealed_text):
            if side == "left":
                left = True
            else:
                right = True
    return CodeWallClues(left=left, right=right)


def _counter_break_row_hold(
    page: SlotPage,
    plan: SlotPlan,
    slots: Sequence[OwnerResult],
    runtime: SlotReaderRuntime,
) -> RowHold | None:
    """Use vendor labels in the row's slots and vendor words in its one pasted drawing.

    A word's horizontal centre must lie between the first and last tick. The matching stamp box
    sets the vertical scope; there is deliberately no smaller vertical window. The ink classifier
    excludes GV words before grouping phrases, so its markup cannot hold the vendor row.
    """
    phrases = [
        label.label.text
        for slot in slots
        for label in slot.labels
        if label.outcome.ink is InkClass.VENDOR and label.label.text
    ]
    if page.ink is not None and plan.row is not None:
        row = plan.row
        slack = runtime.wall_settings.frame_slack_pt
        frames = [
            box
            for box in page.rows.ink.drawing_boxes
            if box.x0 - slack <= row.x0
            and row.x1 <= box.x1 + slack
            and box.top - slack <= row.y <= box.bottom + slack
        ]
        if frames:
            frame = min(frames, key=lambda box: box.width * box.height)
            bounds = _pixels(page.rows, frame, page.rendered)
            left = page.rows.to_pixels(row.x0, row.y)[0]
            right = page.rows.to_pixels(row.x1, row.y)[0]
            words = [
                word
                for word in page.ink.labels
                if word.ink is InkClass.VENDOR
                and bounds[0] <= (word.box[0] + word.box[2]) // 2 <= bounds[2]
                and bounds[1] <= (word.box[1] + word.box[3]) // 2 <= bounds[3]
                and min(left, right) <= (word.box[0] + word.box[2]) // 2 <= max(left, right)
            ]
            gap_px = int(runtime.crop_settings.fragment_gap_pt * Decimal(page.rendered.dpi) / 72)
            phrases.extend(
                _line_phrases(
                    words,
                    gap_px=gap_px,
                    baseline_fraction=runtime.crop_settings.baseline_fraction,
                )
            )
    return counter_break_hold(phrases)


def _drawn(owner: OwnerResult) -> DrawnReading | None:
    """A sealed owner's value beside its drawn length, exactly; `None` when it did not seal."""
    outcome = owner.outcome
    if outcome.state not in {LabelState.SEALED, LabelState.PROVISIONAL}:
        return None
    if outcome.label_index is None:
        return None
    label = owner.labels[outcome.label_index]
    value = label.outcome.value
    if outcome.state is LabelState.PROVISIONAL:
        value = label.outcome.suggestion
    if value is None:
        return None
    drawn = Fraction(owner.owner.x1 - owner.owner.x0)
    if drawn <= 0:
        return None
    return DrawnReading(owner.owner.index, value.exact, drawn, "stacked" in label.outcome.flags)


def _veto_by_drawn_length(
    slots: tuple[OwnerResult, ...], overall: OwnerResult | None
) -> tuple[tuple[OwnerResult, ...], OwnerResult | None, tuple[int | None, ...]]:
    """Hand back to the person every sealed reading the drawn length rejects (#992).

    The reading's value becomes its label's suggestion — shown to the person, never a value — and
    nothing else about it changes.
    """
    pieces = [reading for owner in slots if (reading := _drawn(owner)) is not None]
    whole = None if overall is None else _drawn(overall)
    vetoes = drawn_length_vetoes(pieces, whole)

    def finalize(
        owner: OwnerResult, *, code: str | None = None, reason: str | None = None
    ) -> OwnerResult:
        position = owner.outcome.label_index
        if position is None:
            return owner
        chosen = owner.labels[position]
        candidate_value = (
            chosen.outcome.suggestion
            if owner.outcome.state is LabelState.PROVISIONAL
            else chosen.outcome.value
        )
        state = LabelState.REVIEW if code is not None else LabelState.SEALED
        label_outcome = replace(
            chosen.outcome,
            state=state,
            value=None if code is not None else candidate_value,
            suggestion=candidate_value if code is not None else None,
            reason_code=code,
            reason=reason,
            flags=(*chosen.outcome.flags, *((code,) if code is not None else ())),
        )
        labels = list(owner.labels)
        labels[position] = replace(chosen, outcome=label_outcome)
        return replace(
            owner,
            labels=tuple(labels),
            outcome=OwnerOutcome(
                state,
                None if code is not None else candidate_value,
                position,
                code,
                reason,
            ),
        )

    def checked(owner: OwnerResult) -> OwnerResult:
        if owner.outcome.state is LabelState.PROVISIONAL:
            reason = vetoes.get(owner.owner.index)
            if reason is not None:
                return finalize(owner, code="drawn-length", reason=reason)
            # No derived scale means no drawn-length evidence either way. This check is reject-only:
            # it can veto a clear misfit, never hold a reading merely because scale is unavailable.
            return finalize(owner)
        reason = vetoes.get(owner.owner.index)
        return owner if reason is None else finalize(owner, code="drawn-length", reason=reason)

    held_slots = tuple(checked(owner) for owner in slots)
    held_overall = None if overall is None else checked(overall)
    order = [owner.owner.index for owner in held_slots] + [None]
    held_indices = {
        owner.owner.index
        for owner in (*held_slots, *((held_overall,) if held_overall is not None else ()))
        if owner.outcome.reason_code == "drawn-length"
    }
    return held_slots, held_overall, tuple(index for index in order if index in held_indices)


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
            allow_claude_pair=runtime.claude_row_reader,
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
    store: ArtifactStore | None = None,
    prompt_id: str = CROP_PROMPT_ID,
) -> int:
    """Persist one candidate per slot and overall, and candidate-only links for mapped ones.

    `prompt_id` is the crop prompt the readings were taken with — `SlotReaderRuntime.prompt_id`,
    which names the drawing set's product when the readers were told it (#994).

    What Phase 5's screen needs rides on the candidate as plain flags, because the current schema
    has no columns for it: `slot:<i>` or `slot:overall`, `slot-box:` and `crop-box:` in the page's
    pixels, `kind:` and `kind-evidence:`, `ink:`, `lane:`, `row-rank:`, each reader's text as
    `reader:<model>:<text>`, every guard that fired (`drawn-length` for the veto, `row-hold:<code>`
    for a held countertop). The label's box is the polygon. Each reader's raw answer is in its
    private invocation record (#985).

    **Walls (#992).** One more candidate per page whose row was asked: `walls`, its polygon the row
    picture's box, with each reader's answer, code's hatch check and the outcome as flags
    (`walls-sealed:<layout>` or `walls-held:<code>`). With a `store`, the two pictures the readers
    saw are kept as its evidence artifacts, and — only when *every* row read here sealed the same
    layout — that layout is recorded as a `wall_config` layout proposal (`WALL_PROMPT_ID`, both
    readers named). Whether a check may use it is decided when the check is asked for
    (`workflow/layout_proposals.py:reader_sealed_discriminators`), never here.
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
    wall_rows: list[tuple[PageSlotResult, UUID | None]] = []
    for result in results:
        if result.plan.row is None:
            if result.row_choice_png is not None and result.row_choice_box_px is not None:
                _persist_unselected_row_choice(
                    session,
                    result,
                    extraction_run_id=extraction_run_id,
                    store=store,
                )
                count += 1
            continue
        page = session.get(PageModel, result.page_id)
        if page is None:
            raise ValueError("slot-reader page disappeared before persistence")
        if result.walls is not None:
            wall_rows.append(
                (
                    result,
                    _persist_walls(
                        session,
                        result,
                        result.walls,
                        extraction_run_id=extraction_run_id,
                        store=store,
                    ),
                )
            )
            count += 1
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
                    held.get(index) or "not offered to the form" if sealed else owner.outcome.reason
                )
            )
            flags = [
                "slot-reader",
                f"slot:{'overall' if index is None else index}",
                _box_flag("slot-box", owner.band_px),
                f"row-rank:{result.plan.row.rank}",
                f"row-slot-count:{len(result.slots)}",
            ]
            if prompt_id in CLAUDE_SPAN_PROMPT_IDS and (
                sum(1 for slot_index in offered if slot_index is not None) < len(result.slots)
                or None not in offered
            ):
                # Persist the completeness boundary with the proposal. The check stage uses this
                # to keep a sparse form list from becoming a shorter unscoped operand.
                flags.append("row-partial")
            if prompt_id in CLAUDE_SPAN_PROMPT_IDS:
                flags.append("reader-mode:claude")
            if result.row_choice_number is not None and result.row_choice_number > 0:
                flags.append(f"row-choice:{result.row_choice_number}")
                if result.row_choice_number <= len(result.row_candidate_ids):
                    flags.append(
                        f"row-choice-candidate:{result.row_candidate_ids[result.row_choice_number - 1]}"
                    )
            if result.plan.ambiguity is not None:
                flags.append("row-ambiguous")
            if result.row_hold is not None:
                flags.append(f"row-hold:{result.row_hold.code}")
            if result.check_hold is not None:
                flags.append(f"check-hold:{result.check_hold.code}")
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
                flags.extend(f"reader-id:{source}" for source, _text in chosen.outcome.reader_texts)
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
                id=result.owner_candidate_ids.get(_owner_candidate_key(index)),
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
            if accepted and chosen is not None:
                if value is None:
                    raise ValueError("a sealed slot-reader candidate has no exact value")
                # Preserve the two independent raw readings as two evidence candidates. The
                # combined row above remains the proposal shown in the form; it is not itself
                # treated as two pieces of corroborating evidence by the verdict gate.
                for reader_id, reader_text in chosen.outcome.reader_texts:
                    support = ObservationCandidate(
                        document_version_id=candidate.document_version_id,
                        page_id=candidate.page_id,
                        extraction_run_id=extraction_run_id,
                        raw_text=reader_text,
                        value_numerator=value.exact.numerator,
                        value_denominator=value.exact.denominator,
                        unit=Unit.INCH.value,
                        polygon=polygon,
                        coordinate_space="image",
                        ambiguity_flags=[
                            "slot-reader-support",
                            f"supports:{candidate.id}",
                            f"reader-id:{reader_id}",
                        ],
                    )
                    session.add(support)
                    session.flush()
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
                    prompt_id=prompt_id,
                )
            )
    session.add_all(rows)
    session.flush()
    _propose_sealed_walls(
        session,
        package_revision_id=package_revision_id,
        reader_ids=reader_ids,
        walls=wall_rows,
    )
    return count


def _agreed_row(
    answers: Sequence[ReaderAnswer | WallAnswer | RowChoiceAnswer | CounterBreakAnswer | None],
) -> tuple[RowChoiceAnswer | None, int | None]:
    """The row every reader named, or a "no row" answer that says why there is none.

    One reader's row choice is not enough: the same page went to the countertop row on one run
    and to a table inside a reviewer's notes box on the next (proof runs 2026-10-08). A row is
    used only when every reader answered and all named the same number; a missing answer or a
    disagreement becomes row 0, which sends the page to the reviewer with the reason. Two "no
    row" answers stay no row. Nothing here can make a row more likely to be used.
    """
    if not answers:
        return None, None
    choices = [answer for answer in answers if isinstance(answer, RowChoiceAnswer)]
    rows = {choice.row for choice in choices}
    if len(choices) == len(answers) and len(rows) == 1:
        return choices[0], choices[0].row
    said = ", ".join(f"{_short_model(choice.model_id)} row {choice.row}" for choice in choices)
    missing = len(answers) - len(choices)
    why = (
        f"the readers chose different rows ({said}); the reviewer chooses"
        if len(rows) > 1
        else f"{missing} reader(s) gave no row answer ({said or 'none'}); the reviewer chooses"
    )
    return RowChoiceAnswer(" + ".join(c.model_id for c in choices) or "none", 0, why), 0


def _short_model(model_id: str) -> str:
    return model_id.removeprefix("anthropic.").removeprefix("claude-")


def _persist_unselected_row_choice(
    session: object,
    result: PageSlotResult,
    *,
    extraction_run_id: UUID,
    store: ArtifactStore | None,
) -> None:
    """Keep a Claude-0 result visible as a review-only row question with its numbered view."""
    from io import BytesIO

    from sqlalchemy.orm import Session

    from app.models.evidence import EvidenceArtifact, EvidenceArtifactKind, ObservationCandidate
    from storage.hashing import content_key, sha256_stream

    if not isinstance(session, Session):
        raise TypeError("session must be a SQLAlchemy Session")
    if result.row_choice_box_px is None or result.row_choice_png is None:
        raise ValueError("an unselected row record requires its numbered vendor view")
    reason = (
        result.row_choice.why
        if result.row_choice is not None and result.row_choice.why
        else "No candidate row was selected; the reviewer must choose the countertop row."
    )
    candidate = ObservationCandidate(
        id=uuid4(),
        document_version_id=result.document_version_id,
        page_id=result.page_id,
        extraction_run_id=extraction_run_id,
        raw_text="",
        value_numerator=None,
        value_denominator=None,
        unit=None,
        semantic_guess=None,
        polygon=_polygon(result.row_choice_box_px),
        coordinate_space="image",
        confidence=None,
        ambiguity_flags=[
            "slot-reader-row-choice",
            f"row-choice:{result.row_choice_number or 0}",
            *(
                f"row-candidate:{i}:{candidate_id}"
                for i, candidate_id in enumerate(result.row_candidate_ids, start=1)
            ),
        ],
        review_reason=reason[:300],
    )
    session.add(candidate)
    session.flush()
    if store is None:
        return
    stream = BytesIO(result.row_choice_png)
    digest, _ = sha256_stream(stream)
    key = content_key(
        f"evidence-crops/{result.document_version_id}/pages/{result.page_index}/row-choice",
        digest,
        suffix=".png",
    )
    stream.seek(0)
    store.put(key, stream, content_type="image/png")
    session.add(
        EvidenceArtifact(
            candidate_id=candidate.id,
            canonical_observation_id=None,
            document_version_id=result.document_version_id,
            page_id=result.page_id,
            kind=EvidenceArtifactKind.CROP.value,
            storage_key=key,
            sha256=digest,
            media_type="image/png",
            coordinate_space="image",
        )
    )
    session.flush()


def _persist_walls(
    session: object,
    result: PageSlotResult,
    walls: PageWalls,
    *,
    extraction_run_id: UUID,
    store: ArtifactStore | None,
) -> UUID | None:
    """The page's wall candidate, and the row picture's artifact id when a store keeps it."""
    from io import BytesIO

    from sqlalchemy.orm import Session

    from app.models.evidence import EvidenceArtifact, EvidenceArtifactKind, ObservationCandidate
    from storage.hashing import content_key, sha256_stream

    assert isinstance(session, Session)
    outcome = walls.outcome
    flags = [WALL_READER_FLAG, f"row-rank:{result.plan.row.rank if result.plan.row else None}"]
    flags.append(
        f"{WALLS_SEALED_FLAG}{outcome.config}"
        if outcome.config is not None
        else f"{WALLS_HELD_FLAG}{outcome.code}"
    )
    for answer in walls.answers:
        flags.append(
            f"wall-reader:{answer.model_id}:left={answer.left.value},right={answer.right.value},"
            f"behind={answer.behind.value},view={answer.view}"
        )
    flags.append(f"hatch:left={walls.hatch.left},right={walls.hatch.right}")
    flags.append(f"wall-clue:left={walls.code_clues.left},right={walls.code_clues.right}")
    if outcome.source is not None:
        flags.append(f"wall-source:{outcome.source}")
    flags.append(_box_flag("view-box", walls.view_box_px))
    candidate = ObservationCandidate(
        id=result.wall_candidate_id,
        document_version_id=result.document_version_id,
        page_id=result.page_id,
        extraction_run_id=extraction_run_id,
        raw_text=f"{WALL_CANDIDATE_TEXT}{outcome.config or 'for the person'}",
        polygon=_polygon(walls.row_box_px),
        coordinate_space="image",
        ambiguity_flags=list(dict.fromkeys(flags)),
        review_reason=None if outcome.reason is None else outcome.reason[:300],
    )
    session.add(candidate)
    session.flush()
    if store is None:
        return None
    row_artifact: UUID | None = None
    for png in (walls.row_png, walls.view_png):
        stream = BytesIO(png)
        digest, _ = sha256_stream(stream)
        key = content_key(
            f"evidence-crops/{result.document_version_id}/pages/{result.page_index}/walls",
            digest,
            suffix=".png",
        )
        stream.seek(0)
        store.put(key, stream, content_type="image/png")
        artifact = EvidenceArtifact(
            candidate_id=candidate.id,
            canonical_observation_id=None,
            document_version_id=result.document_version_id,
            page_id=result.page_id,
            kind=EvidenceArtifactKind.CROP.value,
            storage_key=key,
            sha256=digest,
            media_type="image/png",
            coordinate_space="image",
        )
        session.add(artifact)
        session.flush()
        row_artifact = row_artifact or artifact.id
    return row_artifact


def _propose_sealed_walls(
    session: object,
    *,
    package_revision_id: UUID,
    reader_ids: tuple[str, str],
    walls: Sequence[tuple[PageSlotResult, UUID | None]],
) -> None:
    """Record `wall_config` as a layout proposal only when every row read sealed the same layout.

    `wall_config` is one value per package revision, so one row the readers could not settle, or
    two rows that disagree, leaves no proposal: the person chooses, as before.
    """
    from sqlalchemy.orm import Session

    from rules.semantic_types import SemanticType
    from workflow.layout_proposals import (
        DRAWING_CLUE_WALL_PROMPT_ID,
        record_layout_proposal,
    )

    assert isinstance(session, Session)
    if not walls or any(result.walls is None for result, _artifact in walls):
        return
    layouts = {result.walls.outcome.config for result, _ in walls if result.walls is not None}
    if len(layouts) != 1:
        return
    (layout,) = layouts
    artifact = walls[0][1]
    if layout is None or artifact is None:
        return
    code_only = all(
        result.walls is not None and result.walls.outcome.source == "vendor-drawing-clues"
        for result, _artifact in walls
    )
    record_layout_proposal(
        session,
        package_revision_id=package_revision_id,
        discriminator_name=SemanticType.WALL_CONFIG.value,
        proposed_value=layout,
        crop_artifact_id=artifact,
        model_id=(
            "deterministic:vendor-drawing-clues"
            if code_only
            else f"{reader_ids[0]} + {reader_ids[1]}"
        ),
        prompt_id=DRAWING_CLUE_WALL_PROMPT_ID if code_only else WALL_PROMPT_ID,
    )
