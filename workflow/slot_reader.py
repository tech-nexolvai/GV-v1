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

Source: issues #987, #992 · Verification: `tests/workflow/test_slot_reader.py`
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from evidence.coordinates import PageTransform
from evidence.crop import RenderedPage, _crop_rgb, encode_png
from extraction.form_reader.bedrock import AttemptUsage
from extraction.form_reader.runner import ModelPacer
from extraction.geometry.rows import MEASURED_SETTINGS, Box, CountertopRowCandidate, RowSettings
from extraction.glyph_bands import FractionBarGeometry, stacked_fractions
from extraction.ink import InkAt, InkClass, InkLabel, PageInk
from extraction.rows import RowsAndInk
from extraction.slot_reader.bedrock import (
    CLAUDE_SPAN_PROMPT_ID,
    CROP_PROMPT_ID,
    ROW_PROMPT_ID,
    CropJob,
    RowChoiceAnswer,
    crop_prompt_id,
    read_crops_parallel,
)
from extraction.slot_reader.kinds import KindProposal, PieceKind, WallEnd, propose_kind
from extraction.slot_reader.labels import RowHold, counter_break_hold, expand_label, row_hold
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
    HatchSeen,
    WallAnswer,
    WallOutcome,
    WallSettings,
    seal_walls,
    wall_pictures,
)
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
            f"{',claude-row-v1' if self.claude_row_reader else ''}"
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
        form = replace(
            form,
            reader_ids=model_ids,
            clients=clients,
            calls_per_minute=dict(rpm),
            # One in-flight paid request means the batch reservation cannot be oversubscribed by
            # concurrent calls. The two models are still independently paced.
            max_concurrent_calls=1,
            max_tokens=max(form.max_tokens, 3000),
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
            getattr(settings, "claude_reader_budget_usd", Decimal("2.00"))
            if claude_enabled
            else None
        ),
        claude_row_reader=claude_enabled,
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
    answers: tuple[WallAnswer, ...]
    outcome: WallOutcome


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


def _span_view_png(page: SlotPage, owner: PlannedOwner) -> bytes:
    """The full vendor page with only the chosen span boxed; the box never supplies a value."""
    first = page.rows.to_pixels(owner.x0, owner.line_y)
    second = page.rows.to_pixels(owner.x1, owner.line_y)
    left, right = sorted((first[0], second[0]))
    center_y = (first[1] + second[1]) // 2
    outline = (
        max(0, left),
        max(0, center_y - 14),
        min(page.rendered.width_px, max(left + 1, right)),
        min(page.rendered.height_px, center_y + 14),
    )
    return _marked_png(
        page.rendered,
        (0, 0, page.rendered.width_px, page.rendered.height_px),
        ends_x=(),
        line=None,
        thickness=max(2, page.rendered.dpi // 120),
        max_side=1800,
        outline=outline,
        mark_color=bytes((255, 0, 0)),
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
) -> dict[str, object]:
    """Bind the exact encoded images, prompt, candidate key and coordinate transform."""
    from io import BytesIO

    full_hash = hashlib.sha256(full_view_png).hexdigest()
    close_hash = hashlib.sha256(close_up_png).hexdigest()
    full_key: str | None = None
    close_key: str | None = None
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
        },
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
_DIGIT_PIXELS: Final = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("110", "001", "010", "100", "111"),
    "3": ("110", "001", "010", "001", "110"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "110", "001", "110"),
    "6": ("011", "100", "110", "101", "010"),
}


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
) -> bytes:
    """A crop of the render with the row marked (E3's pictures), shrunk to `max_side` at most.

    `ends_x` are page-pixel columns drawn top to bottom; `line` is `(x0, x1, y)` in page pixels.
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
    for (x0, y0, x1, y1), colour, number in numbered_outlines:
        paint(x0 - left, y0 - top, x1 - left, y0 - top + thickness, colour)
        paint(x0 - left, y1 - top - thickness, x1 - left, y1 - top, colour)
        paint(x0 - left, y0 - top, x0 - left + thickness, y1 - top, colour)
        paint(x1 - left - thickness, y0 - top, x1 - left, y1 - top, colour)
        scale = max(2, rendered.dpi // 72)
        badge_left = max(0, x0 - left + thickness)
        badge_top = max(0, y0 - top + thickness)
        glyph = _DIGIT_PIXELS[str(number)]
        badge_width = len(glyph[0]) * scale + 2 * scale
        badge_height = len(glyph) * scale + 2 * scale
        paint(badge_left, badge_top, badge_left + badge_width, badge_top + badge_height, colour)
        for glyph_y, glyph_row in enumerate(glyph):
            for glyph_x, pixel in enumerate(glyph_row):
                if pixel == "1":
                    gx = badge_left + scale + glyph_x * scale
                    gy = badge_top + scale + glyph_y * scale
                    paint(gx, gy, gx + scale, gy + scale, bytes((255, 255, 255)))
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
    """Show the top six code-ranked candidate dimension lines, each in a distinct numbered box."""
    candidates = page.rows.candidates.rows.candidates[:6]
    if not candidates:
        return _crop_png(page.rendered, (0, 0, page.rendered.width_px, page.rendered.height_px)), ()
    outlines: list[tuple[tuple[int, int, int, int], bytes, int]] = []
    for number, row in enumerate(candidates, start=1):
        first = page.rows.to_pixels(row.x0, row.y)
        last = page.rows.to_pixels(row.x1, row.y)
        x0, x1 = sorted((first[0], last[0]))
        center_y = (first[1] + last[1]) // 2
        outlines.append(
            (
                (
                    max(0, x0 - 8),
                    max(0, center_y - 18),
                    min(page.rendered.width_px, x1 + 8),
                    min(page.rendered.height_px, center_y + 18),
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
        thickness=max(2, page.rendered.dpi // 120),
        max_side=1800,
        numbered_outlines=outlines,
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
                )
                if runtime.question_packets
                else None
            )
            row_jobs.append(
                CropJob(
                    key=_row_key(page.page_index),
                    model_id=readers[0],
                    page_index=page.page_index,
                    png=numbered_png,
                    row_question=True,
                    candidate_count=len(candidates),
                    question_packet=packet,
                )
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
    ) -> dict[tuple[str, str], ReaderAnswer | WallAnswer | RowChoiceAnswer | None]:
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
        )

    row_answers = run_jobs(row_jobs) if row_jobs else {}
    planned: list[tuple[SlotPage, SlotPlan, RowChoiceAnswer | None, int | None]] = []
    jobs: list[CropJob] = []
    crops: dict[str, tuple[tuple[int, int, int, int], tuple[int, int, int, int], InkAt | None]] = {}
    walls_asked: dict[
        int, tuple[bytes, bytes, tuple[int, int, int, int], tuple[int, int, int, int], HatchSeen]
    ] = {}
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
        page_row_answer = row_answers.get((_row_key(page.page_index), readers[0]))
        row_choice = page_row_answer if isinstance(page_row_answer, RowChoiceAnswer) else None
        row_number = None if row_choice is None else row_choice.row
        candidates = page_rows[page.page_index].candidates[:6]
        selected_row = (
            candidates[row_number - 1]
            if row_number is not None and 0 < row_number <= len(candidates)
            else None
        )
        plan = plan_slots(
            page.rows.candidates.rows,
            page.rows.ink,
            settings=runtime.crop_settings,
            row_settings=runtime.row_settings,
            selected_row=selected_row,
            row_choice_made=runtime.claude_row_reader and bool(candidates),
        )
        planned.append((page, plan, row_choice, row_number))
        owners = [*plan.slots, *([plan.overall] if plan.overall is not None else [])]
        page_candidate_ids = {_owner_candidate_key(owner.index): uuid4() for owner in owners}
        owner_candidate_ids[page.page_index] = page_candidate_ids
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
                if runtime.question_packets:
                    view_png = _span_view_png(page, owner)
                    packet = _question_packet(
                        page,
                        question_id=key,
                        candidate_id=page_candidate_ids[_owner_candidate_key(owner.index)],
                        prompt_id=runtime.prompt_id,
                        full_view_png=view_png,
                        close_up_png=png,
                        store=store,
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
                        )
                        for model in wanted
                    )
                else:
                    jobs.extend(CropJob(key, model, page.page_index, png) for model in wanted)
        wall_pictures_for = _wall_job_pictures(page, plan, runtime.wall_settings)
        if wall_pictures_for is not None:
            walls_asked[page.page_index] = wall_pictures_for
            wall_candidate_ids[page.page_index] = uuid4()
            row_png, view_png = wall_pictures_for[0], wall_pictures_for[1]
            wall_packet = (
                _question_packet(
                    page,
                    question_id=_walls_key(page.page_index),
                    candidate_id=wall_candidate_ids[page.page_index],
                    prompt_id=WALL_PROMPT_ID,
                    full_view_png=view_png,
                    close_up_png=row_png,
                    store=store,
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
    for page, plan, row_choice, row_number in planned:

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
        if hold is None:
            hold = row_hold(_row_texts([*slots, *((overall,) if overall is not None else ())]))
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
        )
        walls: PageWalls | None = None
        asked = walls_asked.get(page.page_index)
        if asked is not None:
            row_png, view_png, row_px, view_px, hatch = asked
            wall_answers = tuple(
                answer
                for model in readers
                if isinstance(
                    answer := answers.get((_walls_key(page.page_index), model)), WallAnswer
                )
            )
            walls = PageWalls(
                row_png=row_png,
                view_png=view_png,
                row_box_px=row_px,
                view_box_px=view_px,
                hatch=hatch,
                answers=wall_answers,
                outcome=seal_walls(wall_answers, hatch=hatch, row_ambiguity=plan.ambiguity),
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
    phrases: list[str] = []
    for line in lines:
        ordered = sorted(line, key=lambda item: item.box[0])
        phrase = [ordered[0].text]
        previous = ordered[0]
        for word in ordered[1:]:
            if word.box[0] - previous.box[2] > gap_px:
                phrases.append(" ".join(phrase))
                phrase = []
            phrase.append(word.text)
            previous = word
        phrases.append(" ".join(phrase))
    return phrases


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
    if page.ink is not None and plan.row is not None and slots:
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

    def has_length_witness(owner: OwnerResult) -> bool:
        index = owner.owner.index
        sources = [
            reading
            for reading in pieces
            if not reading.stacked and (index is None or reading.index != index)
        ]
        return len(sources) >= 2

    def checked(owner: OwnerResult) -> OwnerResult:
        if owner.outcome.state is LabelState.PROVISIONAL:
            reason = vetoes.get(owner.owner.index)
            if reason is not None:
                return finalize(owner, code="drawn-length", reason=reason)
            if not has_length_witness(owner):
                return finalize(
                    owner,
                    code="drawn-length-unverified",
                    reason=(
                        "the drawing did not provide enough other dimensions to check this reading"
                    ),
                )
            return finalize(owner)
        reason = vetoes.get(owner.owner.index)
        return owner if reason is None else finalize(owner, code="drawn-length", reason=reason)

    held_slots = tuple(checked(owner) for owner in slots)
    held_overall = None if overall is None else checked(overall)
    order = [owner.owner.index for owner in held_slots] + [None]
    held_indices = {
        owner.owner.index
        for owner in (*held_slots, *((held_overall,) if held_overall is not None else ()))
        if owner.outcome.reason_code in {"drawn-length", "drawn-length-unverified"}
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
            ]
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
    from workflow.layout_proposals import record_layout_proposal

    assert isinstance(session, Session)
    if not walls:
        return
    layouts = {result.walls.outcome.config for result, _ in walls if result.walls is not None}
    if len(layouts) != 1:
        return
    (layout,) = layouts
    artifact = walls[0][1]
    if layout is None or artifact is None:
        return
    record_layout_proposal(
        session,
        package_revision_id=package_revision_id,
        discriminator_name=SemanticType.WALL_CONFIG.value,
        proposed_value=layout,
        crop_artifact_id=artifact,
        model_id=f"{reader_ids[0]} + {reader_ids[1]}",
        prompt_id=WALL_PROMPT_ID,
    )
