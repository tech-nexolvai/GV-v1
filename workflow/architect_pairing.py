"""Pair the architect's dimensions with the vendor's countertop row: code first, AI second, reviewer last (#1053).

Type 1 compares the vendor's printed sizes with the architect's. Exact arithmetic can do that only
once each architect dimension is paired with the vendor piece (or pieces) it measures. This module
makes and records that pairing for every vendor row the slot reader chose and read, behind
`GV_ARCHITECT_READER_ENABLED`. It never compares a value: that is the next step's rule (T3).

1. **Code, by drawn position** (`extraction/architect/pairing.py`). The vendor row's ticks come from
   the row the readers chose, in page points; its scale is the median drawn length per printed inch
   over its **sealed** pieces only (never an unsealed or a GV-ink value). The architect's rows come
   from the architect reader (#1052), only inside a drawing whose role is the architect's (code's two
   judgments, or a person), each through that drawing's own scale. One clear alignment pairs. A
   held architect span still gives the alignment its geometry but is never a compared pair.
2. **Both Claude readers, only when code cannot decide** (no clear alignment, or none), and only
   when at least one architect span could be compared: one numbered picture (vendor pieces in red,
   V1..Vn; the comparable architect spans in blue, A1..Am), the fixed question `arch-pair-v1`, the
   same readers, effort, spend guard and batch as the slot reader. Only two **identical** answers
   count, and code then checks every pair they name: the span must be unheld and on the drawn
   outline, a vendor split must be contiguous, and the two drawn lengths through their scales must
   agree within a quarter (rejecting nonsense, keeping real mismatches). Anything else is dropped
   with its reason. Disagreement or a refusal leaves the row for the reviewer, both answers kept.
3. **The reviewer** pairs with one click (`app/api/slot_rows.py`), a new record superseding the
   latest; a pairing the two AIs alone made is *AI-only information*, which the rule must not let a
   PASS rest on without a reviewer's confirmation (decision log 2026-10-09; recorded as `both-ais`).

**What never pairs.** A span running to a fixture centre line, or whose ends are not known to sit
on the casework outline (decision D4); a held architect value; a span from another page.

`latest_architect_pairing` is the read path the rule uses (`workflow/architect_pairing_contract.py`).

Source: issue #1053 · Plan: "Type 1 vendor vs architect (reasoned, 2026-10-09)" §3, §5 T2, §7 ·
Verification: `tests/workflow/test_architect_pairing.py`, `tests/api/test_architect_pairing.py`
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.models import DrawingView, ViewRole
from app.models.evidence import ArchitectPairingRecord, ObservationCandidate
from app.models.runs import ExtractionRun, ModelInvocation
from evidence.crop import RenderedPage, _crop_rgb, encode_png
from extraction.architect.pairing import (
    DrawnRow,
    DrawnSpan,
    PairingResult,
    PairingSettings,
    PairingStatus,
    pair_rows,
)
from extraction.architect.reader import ArchitectPage, ArchitectView
from extraction.geometry.rows import Box
from extraction.ink import InkClass
from extraction.slot_reader.bedrock import ARCH_PAIR_PROMPT_ID, ArchPairAnswer, CropJob
from extraction.slot_reader.seal import LabelState
from units.measurement import Unit
from workflow.architect_pairing_contract import EffectivePair, EffectivePairing, PairingSource
from workflow.architect_reader import ARCHITECT_EXTRACTOR

if TYPE_CHECKING:
    from storage.store import ArtifactStore
    from workflow.slot_reader import PageSlotResult, SlotPage

__all__ = [
    "AI_DRAWN_LENGTH_BAND",
    "MEASURED_PAIRING_SETTINGS",
    "ArchitectPageInput",
    "ArchitectPairing",
    "ArchitectRowInput",
    "ArchitectSpanInput",
    "DecidedPair",
    "PairQuestion",
    "PairingOutcome",
    "ReviewerPairingRefused",
    "VendorPiece",
    "VendorRowInput",
    "architect_page_input",
    "architect_spans_for_row",
    "latest_architect_pairing",
    "latest_record",
    "pair_by_code",
    "pair_picture",
    "pair_question",
    "persist_architect_pairings",
    "record_reviewer_pairing",
    "resolve_answers",
    "vendor_row_input",
    "vendor_scale",
]

#: The pairing thresholds, measured on the client's sets (#1053 part A; the measurements stay in the
#: local report, read-only):
#:
#: * `tick_tolerance_in = 1/2"`: covers the architect's drafting slop (bays drawn up to about half an
#:   inch off their printed size) and stays below the point where a vendor's narrow end filler
#:   crowds the wall tick.
#: * `tolerance_fraction_of_smallest_bay = 1/3`: the tolerance never reaches a third of the
#:   architect row's smallest outline bay, so two neighbouring ticks can never share a partner.
#: * `minimum_coincident_ticks = 3`: two-tick rows aligned by chance.
#: * `minimum_support_margin = 2`: chance alignments of outlet centre lines reached three or four
#:   ticks against two, so the best must lead every conflicting alignment by two.
#:
#: No true positive existed on the measured pages to calibrate against; re-measure when one does.
MEASURED_PAIRING_SETTINGS: Final = PairingSettings(
    tick_tolerance_in=Fraction(1, 2),
    tolerance_fraction_of_smallest_bay=Fraction(1, 3),
    minimum_coincident_ticks=3,
    minimum_support_margin=2,
)

#: The code witness on a pair both AIs named: the architect span's drawn length and the paired vendor
#: piece(s)' drawn length, each in real inches through its own drawing's scale, must agree within
#: this fraction of the vendor's. Wide on purpose: it rejects a pair of two different things (a narrow
#: filler and a wide cabinet) and keeps every real mismatch the compare must flag (a few inches on a
#: cabinet is well inside it).
AI_DRAWN_LENGTH_BAND: Final = Fraction(1, 4)

_AUTOMATIC: Final = ("code", "both-ais", "none")
_REASON_LIMIT: Final = 500
_PICTURE_MAX_SIDE: Final = 1800
_VENDOR_COLOUR: Final = bytes((220, 20, 60))
_ARCHITECT_COLOUR: Final = bytes((0, 90, 220))
_WHITE: Final = bytes((255, 255, 255))
#: A 5 x 7 dot font for the tags: wide enough that a `V` never reads as a `U` once shrunk.
_GLYPHS: Final = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "A": ("00100", "01010", "10001", "10001", "11111", "10001", "10001"),
    "V": ("10001", "10001", "10001", "10001", "01010", "01010", "00100"),
}


# --- inputs ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VendorPiece:
    """One piece of the vendor's chosen row: where it is drawn and, only when sealed, its value."""

    slot_index: int
    """The row's `slot:<i>`."""
    x0_pt: Decimal
    x1_pt: Decimal
    sealed_inches: Fraction | None
    """The value both readers sealed in the vendor's own ink; `None` for anything else."""
    box_px: tuple[int, int, int, int]
    """The piece's band in the rendered page's pixels, for the picture."""


@dataclass(frozen=True, slots=True)
class VendorRowInput:
    pieces: tuple[VendorPiece, ...]
    """Left to right."""
    overall_x: tuple[Decimal, Decimal] | None
    held_reason: str | None
    """The row's own hold (field cut, VIF, a counter break): nothing on it will be compared."""


@dataclass(frozen=True, slots=True)
class ArchitectSpanInput:
    """One span of an architect row, as the architect reader read it and stored it (#1052)."""

    index: int
    x0_pt: Decimal
    x1_pt: Decimal
    on_outline: bool | None
    text: str | None
    held_reason: str | None
    """Why its value is held (or that nothing is printed on it); `None` when it is usable."""
    candidate_id: UUID | None
    """The stored architect candidate (`arch-row:`/`arch-slot:` flags); `None` when not stored."""
    box_px: tuple[int, int, int, int]

    @property
    def comparable(self) -> bool:
        """Whether a pair with this span may ever be compared: stored, unheld, on the outline."""
        return (
            self.on_outline is True and self.held_reason is None and self.candidate_id is not None
        )

    def not_comparable_reason(self) -> str:
        if self.on_outline is False:
            return "a centre-line dimension, never paired with a cabinet"
        if self.on_outline is None:
            return "its ends are not known to sit on the casework outline"
        if self.held_reason is not None:
            return f"its value is held: {self.held_reason}"
        return "it was not stored as the architect's value"


@dataclass(frozen=True, slots=True)
class ArchitectRowInput:
    rank: int
    view_annotation_index: int
    pt_per_inch: Fraction | None
    scale_reason: str
    spans: tuple[ArchitectSpanInput, ...]

    @property
    def key(self) -> str:
        return f"arch-row:{self.rank}"


@dataclass(frozen=True, slots=True)
class ArchitectPageInput:
    """What the pairing knows about one page's architect drawing."""

    page_id: UUID
    architect_run_id: UUID | None
    rows: tuple[ArchitectRowInput, ...]
    """Only rows inside a drawing whose role is the architect's (code's or a person's)."""
    view_boxes: tuple[Box, ...]
    """Every pasted drawing on the page, for framing the picture."""
    reasons: tuple[str, ...] = ()


# --- outputs ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DecidedPair:
    kind: Literal["piece", "overall"]
    architect_candidate_id: UUID
    vendor_slot_indices: tuple[int, ...]
    """Contiguous `slot:<i>` indices; empty for the overall."""

    def as_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "architect_candidate_id": str(self.architect_candidate_id),
            "vendor_slot_indices": list(self.vendor_slot_indices),
        }


@dataclass(frozen=True, slots=True)
class PairQuestion:
    """The one picture both readers are shown, and what its numbers stand for."""

    picture_png: bytes
    vendor_slots: tuple[int, ...]
    """V1..Vn → these `slot:<i>` indices."""
    architect: tuple[ArchitectSpanInput, ...]
    """A1..Am → these spans (each comparable)."""
    architect_rows: tuple[int, ...]
    """A1..Am → the rank of the row each span is on."""
    packet: Mapping[str, object]

    @property
    def picture_sha256(self) -> str:
        return hashlib.sha256(self.picture_png).hexdigest()


@dataclass(frozen=True, slots=True)
class PairingOutcome:
    source: PairingSource
    status: str
    pairs: tuple[DecidedPair, ...]
    reasons: tuple[str, ...]
    details: Mapping[str, object]
    """JSON-ready: offsets and tolerances as exact fractions written as text."""
    question: PairQuestion | None = None
    answers: tuple[ArchPairAnswer | None, ...] = ()


# --- small helpers ---------------------------------------------------------------------------------


def _text(value: Fraction | Decimal | None) -> str | None:
    if value is None:
        return None
    exact = Fraction(value)
    return str(exact.numerator) if exact.denominator == 1 else f"{exact}"


def _tenths(value: Fraction) -> str:
    """A drawn length for a reason line, to a tenth of an inch (never a decision)."""
    return str((Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.1")))


def _median(values: Sequence[Fraction]) -> Fraction:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def vendor_scale(pieces: Sequence[VendorPiece]) -> Fraction | None:
    """Page points per real inch of the vendor's view: the median, over its sealed pieces only, of
    drawn length over sealed value. `None` with fewer than two sealed pieces."""
    ratios = [
        (Fraction(piece.x1_pt) - Fraction(piece.x0_pt)) / piece.sealed_inches
        for piece in pieces
        if piece.sealed_inches is not None and piece.sealed_inches > 0
    ]
    if len(ratios) < 2:
        return None
    return _median(ratios)


def _contiguous(indices: Sequence[int]) -> bool:
    ordered = sorted(indices)
    return all(right == left + 1 for left, right in pairwise(ordered))


# --- building the inputs ---------------------------------------------------------------------------


def vendor_row_input(result: PageSlotResult) -> VendorRowInput | None:
    """The vendor's chosen row as the pairing takes it, or `None` when no row was chosen.

    A piece's value is used (for the scale only) when its reading **sealed**, was not vetoed, is in
    inches, and is the vendor's own ink. Everything else is `None`: never a suggestion, never GV's.
    """
    plan = result.plan
    if plan.row is None or not plan.slots:
        return None
    outcomes = {slot.owner.index: slot for slot in result.slots}
    pieces: list[VendorPiece] = []
    for owner in plan.slots:
        if owner.index is None:
            continue
        owned = outcomes.get(owner.index)
        sealed: Fraction | None = None
        if owned is not None:
            outcome = owned.outcome
            label = (
                owned.labels[outcome.label_index]
                if outcome.label_index is not None and outcome.label_index < len(owned.labels)
                else None
            )
            if (
                outcome.state is LabelState.SEALED
                and outcome.value is not None
                and outcome.value.unit is Unit.INCH
                and label is not None
                and label.outcome.ink is InkClass.VENDOR
                and owner.index not in result.vetoed
            ):
                sealed = outcome.value.exact
        pieces.append(
            VendorPiece(
                slot_index=owner.index,
                x0_pt=owner.x0,
                x1_pt=owner.x1,
                sealed_inches=sealed,
                box_px=owned.band_px if owned is not None else (0, 0, 1, 1),
            )
        )
    overall = plan.overall
    return VendorRowInput(
        pieces=tuple(pieces),
        overall_x=None if overall is None else (overall.x0, overall.x1),
        held_reason=None if result.row_hold is None else result.row_hold.reason,
    )


def _architect_scale(view: ArchitectView) -> tuple[Fraction | None, str]:
    """The architect drawing's scale for drawn positions.

    The printed scale times the paste factor when the reader gave the drawing a scale at all (its
    labels did not contradict the note): measured on the client's sets (#1053 part A), the architect's
    bay labels are not drawn exactly to size (they scatter by several percent), the note is. Otherwise the
    reader's own scale from its witnessed labels; `None` when it has none.
    """
    if view.points_per_inch is None:
        return None, view.scale_reason
    if view.absolute_points_per_inch is not None:
        return (
            Fraction(view.absolute_points_per_inch),
            "the printed scale and paste factor (the drawing's labels agree with it)",
        )
    return Fraction(view.points_per_inch), view.scale_reason


def _union(*boxes: tuple[int, int, int, int] | None) -> tuple[int, int, int, int]:
    present = [box for box in boxes if box is not None]
    return (
        min(box[0] for box in present),
        min(box[1] for box in present),
        max(box[2] for box in present),
        max(box[3] for box in present),
    )


def architect_page_input(
    reading: ArchitectPage,
    *,
    page_id: UUID,
    architect_views: Collection[int],
    candidate_ids: Mapping[tuple[int, int, int], UUID],
    architect_run_id: UUID | None,
) -> ArchitectPageInput:
    """One page's architect rows for the pairing.

    `architect_views` are the annotation indices of the drawings whose role is the architect's now
    (code's two judgments or a person's); `candidate_ids` maps `(view, row rank, slot)` to the
    candidate the architect reader stored for that span.
    """
    views = {view.annotation_index: view for view in reading.views}
    rows: list[ArchitectRowInput] = []
    for row in reading.rows:
        if row.view_annotation_index not in architect_views:
            continue
        view = views.get(row.view_annotation_index)
        scale, scale_reason = (
            (None, "the drawing is unknown") if view is None else (_architect_scale(view))
        )
        spans: list[ArchitectSpanInput] = []
        for span in row.spans:
            candidate = candidate_ids.get((row.view_annotation_index, row.rank, span.index))
            held = span.held_reason
            if span.text is None:
                held = held or "nothing is printed on this span"
            elif candidate is None and held is None:
                held = "the architect reader did not store this span"
            spans.append(
                ArchitectSpanInput(
                    index=span.index,
                    x0_pt=span.x0_pt,
                    x1_pt=span.x1_pt,
                    on_outline=span.on_outline,
                    text=span.text,
                    held_reason=held,
                    candidate_id=candidate,
                    box_px=_union(span.span_pixels, span.label_pixels),
                )
            )
        rows.append(
            ArchitectRowInput(
                rank=row.rank,
                view_annotation_index=row.view_annotation_index,
                pt_per_inch=scale,
                scale_reason=scale_reason,
                spans=tuple(spans),
            )
        )
    reasons: tuple[str, ...] = ()
    if not any(view.annotation_index in architect_views for view in reading.views):
        reasons = ("No drawing on this sheet is confirmed as the architect's.",)
    return ArchitectPageInput(
        page_id=page_id,
        architect_run_id=architect_run_id,
        rows=tuple(rows),
        view_boxes=tuple(view.box for view in reading.views),
        reasons=reasons,
    )


# --- 1. code ---------------------------------------------------------------------------------------


def _vendor_drawn_row(vendor: VendorRowInput) -> DrawnRow | None:
    spans = tuple(DrawnSpan(piece.x0_pt, piece.x1_pt, None) for piece in vendor.pieces)
    if any(left.x1_pt != right.x0_pt for left, right in pairwise(spans)):
        return None
    overall = (
        None
        if vendor.overall_x is None or not vendor.overall_x[0] < vendor.overall_x[1]
        else DrawnSpan(vendor.overall_x[0], vendor.overall_x[1], None)
    )
    return DrawnRow(
        key="vendor", spans=spans, overall=overall, pt_per_inch=vendor_scale(vendor.pieces)
    )


def _architect_drawn_rows(page: ArchitectPageInput) -> list[DrawnRow]:
    rows: list[DrawnRow] = []
    for row in page.rows:
        spans = tuple(
            DrawnSpan(span.x0_pt, span.x1_pt, span.on_outline, printed=span.text)
            for span in row.spans
        )
        if any(left.x1_pt != right.x0_pt for left, right in pairwise(spans)):
            continue
        rows.append(DrawnRow(key=row.key, spans=spans, overall=None, pt_per_inch=row.pt_per_inch))
    return rows


def _result_details(result: PairingResult) -> dict[str, object]:
    return {
        "status": result.status.value,
        "architect_row": result.architect_row_key,
        "offset_in": _text(result.offset_in),
        "tolerance_in": _text(result.tolerance_in),
        "support": result.support,
        "runner_up_support": result.runner_up_support,
        "reasons": list(result.reasons),
    }


def _spans_details(page: ArchitectPageInput) -> list[dict[str, object]]:
    return [
        {
            "row": row.rank,
            "slot": span.index,
            "candidate_id": None if span.candidate_id is None else str(span.candidate_id),
            "text": span.text,
            "on_outline": span.on_outline,
            "held_reason": span.held_reason,
            "comparable": span.comparable,
        }
        for row in page.rows
        for span in row.spans
    ]


def pair_by_code(
    vendor: VendorRowInput, page: ArchitectPageInput, settings: PairingSettings
) -> tuple[PairingOutcome, PairingResult | None]:
    """Code's pairing of one vendor row with the page's architect rows, by drawn position."""
    base: dict[str, object] = {
        "settings": {
            "tick_tolerance_in": _text(settings.tick_tolerance_in),
            "tolerance_fraction_of_smallest_bay": _text(
                settings.tolerance_fraction_of_smallest_bay
            ),
            "minimum_coincident_ticks": settings.minimum_coincident_ticks,
            "minimum_support_margin": settings.minimum_support_margin,
        },
        "architect_run_id": None if page.architect_run_id is None else str(page.architect_run_id),
        "vendor_pt_per_inch": _text(vendor_scale(vendor.pieces)),
        "vendor_sealed_pieces": sum(piece.sealed_inches is not None for piece in vendor.pieces),
        "architect_scales": {
            row.key: {"pt_per_inch": _text(row.pt_per_inch), "reason": row.scale_reason}
            for row in page.rows
        },
        "spans": _spans_details(page),
    }
    drawn = _vendor_drawn_row(vendor)
    if drawn is None:
        reason = (
            "The vendor's pieces are not drawn as one unbroken chain, so code cannot align them."
        )
        return (
            PairingOutcome(
                "code", PairingStatus.NO_FIT.value, (), (reason,), {**base, "reasons": [reason]}
            ),
            None,
        )
    result = pair_rows(drawn, _architect_drawn_rows(page), settings)
    reasons = list(page.reasons) + list(result.reasons)
    excluded: list[dict[str, object]] = []
    pairs: list[DecidedPair] = []
    if result.status is PairingStatus.PAIRED:
        row = next(row for row in page.rows if row.key == result.architect_row_key)
        for index, why in result.excluded:
            span = None if index == "overall" else row.spans[index]
            excluded.append(
                {
                    "row": row.rank,
                    "slot": index,
                    "candidate_id": (
                        None
                        if span is None or span.candidate_id is None
                        else str(span.candidate_id)
                    ),
                    "reason": why,
                }
            )
        for pair in result.pairs:
            if pair.architect_span_index == "overall":
                continue
            span = row.spans[pair.architect_span_index]
            if not span.comparable or span.candidate_id is None:
                why = span.not_comparable_reason()
                excluded.append(
                    {
                        "row": row.rank,
                        "slot": span.index,
                        "candidate_id": (
                            None if span.candidate_id is None else str(span.candidate_id)
                        ),
                        "reason": f"lines up, but is never compared: {why}",
                    }
                )
                reasons.append(
                    f"The architect's {span.text or 'unlabelled span'} lines up with the vendor's "
                    f"row but is not compared: {why}."
                )
                continue
            slots = tuple(vendor.pieces[k].slot_index for k in pair.vendor_span_indices)
            pairs.append(
                DecidedPair(
                    kind=pair.kind,
                    architect_candidate_id=span.candidate_id,
                    vendor_slot_indices=() if pair.kind == "overall" else slots,
                )
            )
    details = {
        **base,
        "code": _result_details(result),
        "excluded": excluded,
        "reasons": reasons,
    }
    return (
        PairingOutcome("code", result.status.value, tuple(pairs), tuple(reasons), details),
        result,
    )


# --- 2. the two AIs --------------------------------------------------------------------------------


def _paint(
    rgb: bytearray,
    width: int,
    height: int,
    box: tuple[int, int, int, int],
    colour: bytes,
) -> None:
    x0, y0, x1, y1 = max(0, box[0]), max(0, box[1]), min(width, box[2]), min(height, box[3])
    if x0 >= x1 or y0 >= y1:
        return
    run = colour * (x1 - x0)
    stride = width * 3
    for row in range(y0, y1):
        rgb[row * stride + x0 * 3 : row * stride + x1 * 3] = run


def _outline(
    rgb: bytearray,
    width: int,
    height: int,
    box: tuple[int, int, int, int],
    colour: bytes,
    thickness: int,
) -> None:
    x0, y0, x1, y1 = box
    _paint(rgb, width, height, (x0, y0, x1, y0 + thickness), colour)
    _paint(rgb, width, height, (x0, y1 - thickness, x1, y1), colour)
    _paint(rgb, width, height, (x0, y0, x0 + thickness, y1), colour)
    _paint(rgb, width, height, (x1 - thickness, y0, x1, y1), colour)


def _badge_size(text: str, dot: int) -> tuple[int, int]:
    return (len(text) * 6 * dot + dot, 9 * dot)


def _badge(
    rgb: bytearray,
    width: int,
    height: int,
    at: tuple[int, int],
    text: str,
    colour: bytes,
    dot: int,
) -> None:
    left, top = at
    badge_width, badge_height = _badge_size(text, dot)
    _paint(rgb, width, height, (left, top, left + badge_width, top + badge_height), colour)
    for position, character in enumerate(text):
        glyph = _GLYPHS[character]
        for glyph_y, line in enumerate(glyph):
            for glyph_x, pixel in enumerate(line):
                if pixel == "1":
                    x = left + dot + position * 6 * dot + glyph_x * dot
                    y = top + dot + glyph_y * dot
                    _paint(rgb, width, height, (x, y, x + dot, y + dot), _WHITE)


def pair_picture(
    rendered: RenderedPage,
    to_pixels: Callable[[Decimal, Decimal], tuple[int, int]],
    vendor: Sequence[VendorPiece],
    architect: Sequence[ArchitectSpanInput],
    view_boxes: Sequence[Box],
) -> bytes:
    """The sheet around both drawings, vendor pieces outlined red as V1..Vn and the architect spans
    outlined blue as A1..Am, shrunk to at most 1800 px a side (inside T0's picture limits).

    The tags are drawn large enough to stay legible after the shrink: each font dot is three pixels
    of the final picture (5 x 7 dots a character). The marks only say *which* dimension is meant; no value comes from them.
    """
    marked = [piece.box_px for piece in vendor] + [span.box_px for span in architect]
    views: list[tuple[int, int, int, int]] = []
    for view in view_boxes:
        first, second = to_pixels(view.x0, view.top), to_pixels(view.x1, view.bottom)
        pixels = (
            min(first[0], second[0]),
            min(first[1], second[1]),
            max(first[0], second[0]),
            max(first[1], second[1]),
        )
        if any(
            pixels[0] <= (m[0] + m[2]) // 2 <= pixels[2]
            and pixels[1] <= (m[1] + m[3]) // 2 <= pixels[3]
            for m in marked
        ):
            views.append(pixels)
    frame = _union(*marked, *views)
    margin = max(20, (frame[2] - frame[0]) // 25)
    frame = (
        max(0, frame[0] - margin),
        max(0, frame[1] - 4 * margin),
        min(rendered.width_px, frame[2] + margin),
        min(rendered.height_px, frame[3] + 4 * margin),
    )
    left, top, right, bottom = frame
    width, height = right - left, bottom - top
    factor = max(1, -(-max(width, height) // _PICTURE_MAX_SIDE))
    dot = 3 * factor
    thickness = 2 * factor
    rgb = bytearray(_crop_rgb(rendered, frame))

    def local(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return (box[0] - left, box[1] - top, box[2] - left, box[3] - top)

    for number, piece in enumerate(vendor, start=1):
        box = local(piece.box_px)
        _outline(rgb, width, height, box, _VENDOR_COLOUR, thickness)
        label = f"V{number}"
        badge_width, badge_height = _badge_size(label, dot)
        step = (number - 1) % 2
        at = (
            max(0, (box[0] + box[2]) // 2 - badge_width // 2),
            min(height - badge_height, box[3] + dot + step * (badge_height + dot)),
        )
        _badge(rgb, width, height, at, label, _VENDOR_COLOUR, dot)
    for number, span in enumerate(architect, start=1):
        box = local(span.box_px)
        _outline(rgb, width, height, box, _ARCHITECT_COLOUR, thickness)
        label = f"A{number}"
        badge_width, badge_height = _badge_size(label, dot)
        step = (number - 1) % 2
        at = (
            max(0, (box[0] + box[2]) // 2 - badge_width // 2),
            max(0, box[1] - dot - badge_height - step * (badge_height + dot)),
        )
        _badge(rgb, width, height, at, label, _ARCHITECT_COLOUR, dot)
    if factor == 1:
        return encode_png(width, height, bytes(rgb))
    import numpy as np

    image = np.frombuffer(bytes(rgb), dtype=np.uint8).reshape(height, width, 3)
    small_h, small_w = height // factor, width // factor
    trimmed = image[: small_h * factor, : small_w * factor].astype(np.uint32)
    shrunk = trimmed.reshape(small_h, factor, small_w, factor, 3).sum(axis=(1, 3))
    shrunk = (shrunk + (factor * factor) // 2) // (factor * factor)
    return encode_png(small_w, small_h, shrunk.astype(np.uint8).tobytes())


def _comparable_spans(page: ArchitectPageInput) -> list[tuple[int, ArchitectSpanInput]]:
    return [(row.rank, span) for row in page.rows for span in row.spans if span.comparable]


def pair_question(
    page: SlotPage,
    vendor: VendorRowInput,
    architect: ArchitectPageInput,
    *,
    store: ArtifactStore | None,
    effort: str | None,
) -> PairQuestion | None:
    """The one numbered picture and its hash-bound packet, or `None` when nothing could be paired."""
    from io import BytesIO

    from workflow.slot_reader import _transform_packet

    spans = _comparable_spans(architect)
    if not spans or not vendor.pieces:
        return None
    picture = pair_picture(
        page.rendered,
        page.rows.to_pixels,
        vendor.pieces,
        [span for _rank, span in spans],
        architect.view_boxes,
    )
    digest = hashlib.sha256(picture).hexdigest()
    storage_key: str | None = None
    if store is not None:
        storage_key = (
            f"reader-questions/{page.document_version_id}/pages/{page.page_index}/"
            f"arch-pair-{digest}.png"
        )
        saved = store.put(storage_key, BytesIO(picture), content_type="image/png")
        if saved.sha256 != digest:
            raise ValueError("stored architect-pairing picture hash does not match its bytes")
    packet: dict[str, object] = {
        "question_id": f"p{page.page_index}:arch-pair",
        "document_version_id": str(page.document_version_id),
        "page_index": page.page_index,
        "source_page_sha256": page.rendered.page_content_hash,
        "prompt_id": ARCH_PAIR_PROMPT_ID,
        "vendor_slots": [piece.slot_index for piece in vendor.pieces],
        "architect_candidate_ids": [str(span.candidate_id) for _rank, span in spans],
        "page_transform": _transform_packet(page.transform),
        "images": {"numbered_pairing_view": {"sha256": digest, "storage_key": storage_key}},
        **({} if effort is None else {"effort": effort}),
    }
    packet["packet_sha256"] = hashlib.sha256(
        json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return PairQuestion(
        picture_png=picture,
        vendor_slots=tuple(piece.slot_index for piece in vendor.pieces),
        architect=tuple(span for _rank, span in spans),
        architect_rows=tuple(rank for rank, _span in spans),
        packet=packet,
    )


def _answer_json(answer: ArchPairAnswer | None, model: str) -> dict[str, object]:
    if answer is None:
        return {"model_id": model, "answered": False}
    return {
        "model_id": answer.model_id,
        "answered": True,
        "overall": answer.overall,
        "pieces": list(answer.pieces),
        "why": answer.why,
    }


def resolve_answers(
    question: PairQuestion,
    answers: Sequence[tuple[str, ArchPairAnswer | None]],
    vendor: VendorRowInput,
    architect: ArchitectPageInput,
    code: PairingOutcome,
) -> PairingOutcome:
    """Both readers' answers to one pairing question, checked by code.

    Only identical answers count. Then every pair is witnessed: its architect span must be
    comparable (unheld, on the outline), several vendor pieces given one A-number must be next to
    each other, an A-number for the whole run must not also name only some of its pieces, and the two
    drawn lengths through their scales must agree within `AI_DRAWN_LENGTH_BAND`.
    """
    numbering = {
        "vendor": {f"V{k}": slot for k, slot in enumerate(question.vendor_slots, start=1)},
        "architect": {
            f"A{k}": {"candidate_id": str(span.candidate_id), "row": rank, "slot": span.index}
            for k, (rank, span) in enumerate(
                zip(question.architect_rows, question.architect, strict=True), start=1
            )
        },
    }
    details: dict[str, object] = {
        **code.details,
        "ai": {
            "prompt_id": ARCH_PAIR_PROMPT_ID,
            "picture_sha256": question.picture_sha256,
            "packet_sha256": question.packet.get("packet_sha256"),
            "numbering": numbering,
            "answers": [_answer_json(answer, model) for model, answer in answers],
        },
    }
    code_reasons = list(code.reasons)
    given = [answer for _model, answer in answers]
    if not given or any(answer is None for answer in given):
        reason = (
            "Code could not pair the architect's dimensions with this row, and a reader gave no "
            "usable answer; the reviewer pairs them."
        )
        return PairingOutcome(
            "none",
            "ais-refused",
            (),
            (*code_reasons, reason),
            {**details, "reasons": [*code_reasons, reason]},
            question,
            tuple(given),
        )
    first = given[0]
    assert first is not None
    if any(
        answer is not None and (answer.overall, answer.pieces) != (first.overall, first.pieces)
        for answer in given
    ):
        reason = (
            "Code could not pair the architect's dimensions with this row, and the two readers "
            "paired them differently; the reviewer pairs them."
        )
        return PairingOutcome(
            "none",
            "ais-disagree",
            (),
            (*code_reasons, reason),
            {**details, "reasons": [*code_reasons, reason]},
            question,
            tuple(given),
        )

    rows = {row.rank: row for row in architect.rows}
    vendor_pt = vendor_scale(vendor.pieces)
    dropped: list[dict[str, object]] = []
    reasons: list[str] = [*code_reasons, "Both readers gave the same pairing; code checked it."]

    def drop(a_number: int, why: str) -> None:
        dropped.append({"architect": f"A{a_number}", "reason": why})
        reasons.append(f"A{a_number} is not paired: {why}.")

    def drawn_in(span: ArchitectSpanInput, rank: int) -> Fraction | None:
        scale = rows[rank].pt_per_inch
        return None if scale is None else (Fraction(span.x1_pt) - Fraction(span.x0_pt)) / scale

    def plausible(a_number: int, architect_in: Fraction | None, vendor_pt_long: Fraction) -> bool:
        if architect_in is None or vendor_pt is None:
            drop(a_number, "a drawing's scale is unknown, so its drawn length cannot be checked")
            return False
        vendor_in = vendor_pt_long / vendor_pt
        if abs(architect_in - vendor_in) > AI_DRAWN_LENGTH_BAND * vendor_in:
            drop(
                a_number,
                f"its drawn length ({_tenths(architect_in)} in) and the vendor's "
                f"({_tenths(vendor_in)} in) differ by more than a quarter",
            )
            return False
        return True

    groups: dict[int, list[int]] = {}
    for position, a_number in enumerate(first.pieces):
        if a_number:
            groups.setdefault(a_number, []).append(position)
    pairs: list[DecidedPair] = []
    every = list(range(len(vendor.pieces)))
    whole = (
        Fraction(vendor.overall_x[1]) - Fraction(vendor.overall_x[0])
        if vendor.overall_x is not None
        else Fraction(vendor.pieces[-1].x1_pt) - Fraction(vendor.pieces[0].x0_pt)
    )
    named = sorted({*groups, *([first.overall] if first.overall else [])})
    for a_number in named:
        rank, span = question.architect_rows[a_number - 1], question.architect[a_number - 1]
        if not span.comparable or span.candidate_id is None:
            drop(a_number, span.not_comparable_reason())
            continue
        positions = groups.get(a_number, [])
        if first.overall == a_number and positions and positions != every:
            drop(a_number, "it cannot measure both the whole run and only some of its pieces")
            continue
        if positions and not _contiguous(positions):
            drop(a_number, "the vendor pieces given for it are not next to each other")
            continue
        architect_in = drawn_in(span, rank)
        if first.overall == a_number:
            if not plausible(a_number, architect_in, whole):
                continue
            pairs.append(DecidedPair("overall", span.candidate_id, ()))
        if positions:
            long = sum(
                (
                    Fraction(vendor.pieces[k].x1_pt) - Fraction(vendor.pieces[k].x0_pt)
                    for k in positions
                ),
                Fraction(0),
            )
            if first.overall == a_number or plausible(a_number, architect_in, long):
                pairs.append(
                    DecidedPair(
                        "piece",
                        span.candidate_id,
                        tuple(vendor.pieces[k].slot_index for k in positions),
                    )
                )
    if not named:
        reasons.append("Both readers say the architect prints nothing comparable for this row.")
    details["dropped"] = dropped
    details["reasons"] = reasons
    return PairingOutcome(
        "both-ais",
        PairingStatus.PAIRED.value if pairs else PairingStatus.NOTHING_COMPARABLE.value,
        tuple(pairs),
        tuple(reasons),
        details,
        question,
        tuple(given),
    )


def _needs_the_ais(code: PairingOutcome, vendor: VendorRowInput, page: ArchitectPageInput) -> bool:
    return (
        code.status in {PairingStatus.AMBIGUOUS.value, PairingStatus.NO_FIT.value}
        and vendor.held_reason is None
        and bool(vendor.pieces)
        and bool(_comparable_spans(page))
    )


@dataclass(frozen=True, slots=True)
class ArchitectPairing:
    """The pairing step the slot reader runs after it has read a batch of pages (#1053).

    `pages` holds the architect reader's result for each page it read, by page id. A page it did not
    read gets no pairing at all.
    """

    settings: PairingSettings
    pages: Mapping[UUID, ArchitectPageInput] = field(default_factory=dict)

    def pair(
        self,
        results: Sequence[PageSlotResult],
        pages: Sequence[SlotPage],
        *,
        ask: Callable[[Sequence[CropJob]], Mapping[tuple[str, str], object]],
        readers: tuple[str, ...],
        ask_the_ais: bool,
        store: ArtifactStore | None,
        effort: str | None,
    ) -> tuple[PageSlotResult, ...]:
        """Every result with its pairing: code first; one batched question to both readers for the
        rows code could not decide (`ask_the_ais` only when both readers are the Claude pair)."""
        by_index = {page.page_index: page for page in pages}
        pending: dict[int, tuple[VendorRowInput, ArchitectPageInput, PairingOutcome]] = {}
        decided: dict[int, PairingOutcome] = {}
        questions: dict[int, PairQuestion] = {}
        jobs: list[CropJob] = []
        for result in results:
            architect = self.pages.get(result.page_id)
            vendor = vendor_row_input(result)
            if architect is None or vendor is None:
                continue
            code, _raw = pair_by_code(vendor, architect, self.settings)
            if not (ask_the_ais and _needs_the_ais(code, vendor, architect)):
                decided[result.page_index] = code
                continue
            question = pair_question(
                by_index[result.page_index], vendor, architect, store=store, effort=effort
            )
            if question is None:
                decided[result.page_index] = code
                continue
            pending[result.page_index] = (vendor, architect, code)
            questions[result.page_index] = question
            jobs.extend(
                CropJob(
                    key=_pair_key(result.page_index),
                    model_id=model,
                    page_index=result.page_index,
                    png=question.picture_png,
                    arch_pair_question=True,
                    vendor_pieces=len(question.vendor_slots),
                    architect_spans=len(question.architect),
                    question_packet=question.packet,
                )
                for model in readers
            )
        answers = ask(jobs) if jobs else {}
        for page_index, (vendor, architect, code) in pending.items():
            given = [
                (
                    model,
                    (
                        answer
                        if isinstance(
                            answer := answers.get((_pair_key(page_index), model)), ArchPairAnswer
                        )
                        else None
                    ),
                )
                for model in readers
            ]
            decided[page_index] = resolve_answers(
                questions[page_index], given, vendor, architect, code
            )
        return tuple(
            (
                replace(result, architect_pairing=decided[result.page_index])
                if result.page_index in decided
                else result
            )
            for result in results
        )


def _pair_key(page_index: int) -> str:
    return f"p{page_index}:arch-pair"


# --- 3. storing it ---------------------------------------------------------------------------------


def _invocations(
    session: Session, extraction_run_id: UUID, packet_sha256: object
) -> dict[str, str]:
    """The answering attempt of each reader for one pairing question, by model."""
    found: dict[str, str] = {}
    if not isinstance(packet_sha256, str):
        return found
    rows = session.execute(
        select(ModelInvocation.id, ModelInvocation.model_id, ModelInvocation.reader_question_packet)
        .where(
            ModelInvocation.extraction_run_id == extraction_run_id,
            ModelInvocation.prompt_id == ARCH_PAIR_PROMPT_ID,
            ModelInvocation.outcome == "ok",
        )
        .order_by(ModelInvocation.reader_attempt_number)
    ).all()
    for invocation_id, model_id, packet in rows:
        if isinstance(packet, dict) and packet.get("packet_sha256") == packet_sha256:
            found[model_id] = str(invocation_id)
    return found


def persist_architect_pairings(
    session: Session,
    *,
    package_revision_id: UUID,
    extraction_run_id: UUID,
    results: Sequence[PageSlotResult],
) -> int:
    """One automatic record per vendor row read in this run that has a pairing. Append-only."""
    count = 0
    session.flush()
    for result in results:
        outcome = result.architect_pairing
        anchor = result.owner_candidate_ids.get("slot:0")
        if outcome is None or anchor is None or result.plan.row is None:
            continue
        details = dict(outcome.details)
        ai = details.get("ai")
        if isinstance(ai, dict):
            details["ai"] = {
                **ai,
                "invocation_ids": _invocations(session, extraction_run_id, ai.get("packet_sha256")),
            }
        session.add(
            ArchitectPairingRecord(
                package_revision_id=package_revision_id,
                page_id=result.page_id,
                row_anchor_candidate_id=anchor,
                extraction_run_id=extraction_run_id,
                source=outcome.source,
                status=outcome.status,
                pairs=[pair.as_json() for pair in outcome.pairs],
                details=details,
                supersedes_id=None,
                decided_by=None,
            )
        )
        count += 1
    session.flush()
    return count


def architect_candidate_ids(
    session: Session, extraction_run_id: UUID
) -> dict[UUID, dict[tuple[int, int, int], UUID]]:
    """The architect reader's stored spans in one run, by page, keyed `(view, row rank, slot)`."""
    found: dict[UUID, dict[tuple[int, int, int], UUID]] = {}
    for candidate in session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.extraction_run_id == extraction_run_id
        )
    ):
        key = _span_key(candidate.ambiguity_flags or ())
        if key is not None:
            found.setdefault(candidate.page_id, {})[key] = candidate.id
    return found


def _flag(flags: Iterable[str], prefix: str) -> str | None:
    return next((flag.removeprefix(prefix) for flag in flags if flag.startswith(prefix)), None)


def _span_key(flags: Sequence[str]) -> tuple[int, int, int] | None:
    parts = [_flag(flags, prefix) for prefix in ("arch-view:", "arch-row:", "arch-slot:")]
    if any(part is None or not part.isdigit() for part in parts):
        return None
    view, rank, slot = (int(part) for part in parts if part is not None)
    return view, rank, slot


def architect_views(session: Session, page_id: UUID) -> set[int]:
    """The annotation indices of the page's drawings whose role is now the architect's."""
    found: set[int] = set()
    for tag in session.scalars(
        select(DrawingView.tag).where(
            DrawingView.page_id == page_id, DrawingView.role == ViewRole.ARCH.value
        )
    ):
        number = tag.removeprefix("panel-")
        if tag.startswith("panel-") and number.isdigit():
            found.add(int(number))
    return found


# --- 4. the read path and the reviewer -------------------------------------------------------------


def latest_record(
    session: Session, row_anchor_id: UUID, *, sources: Collection[str] | None = None
) -> ArchitectPairingRecord | None:
    """The newest record for this row, optionally only of these sources."""
    query = select(ArchitectPairingRecord).where(
        ArchitectPairingRecord.row_anchor_candidate_id == row_anchor_id
    )
    if sources is not None:
        query = query.where(ArchitectPairingRecord.source.in_(tuple(sources)))
    return session.execute(
        query.order_by(
            ArchitectPairingRecord.created_at.desc(), ArchitectPairingRecord.id.desc()
        ).limit(1)
    ).scalar_one_or_none()


def _chain_tip(session: Session, row_anchor_id: UUID) -> ArchitectPairingRecord | None:
    later = aliased(ArchitectPairingRecord)
    return session.execute(
        select(ArchitectPairingRecord)
        .where(
            ArchitectPairingRecord.row_anchor_candidate_id == row_anchor_id,
            ~exists().where(later.supersedes_id == ArchitectPairingRecord.id),
        )
        .order_by(ArchitectPairingRecord.created_at.desc(), ArchitectPairingRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class EligibleSpan:
    """One architect span on the row's page, as the reviewer is offered it."""

    candidate: ObservationCandidate
    row: int | None
    slot: int | None
    on_outline: bool | None
    held_reason: str | None
    inches: Fraction | None

    @property
    def comparable(self) -> bool:
        return self.on_outline is True and self.held_reason is None and self.inches is not None

    def refusal(self) -> str:
        if self.on_outline is False:
            return "it runs to a fixture's centre line, so it never measures a cabinet"
        if self.on_outline is None:
            return "its ends are not known to sit on the casework outline"
        return f"its value is held: {self.held_reason or 'no value was stored'}"


def _eligible(candidate: ObservationCandidate, views: Collection[int]) -> EligibleSpan:
    flags = candidate.ambiguity_flags or []
    outline = _flag(flags, "arch-ticks-on-outline:")
    key = _span_key(flags)
    held = _flag(flags, "arch-held:")
    if held is None and (key is None or key[0] not in views):
        held = "this drawing is no longer confirmed as the architect's"
    inches = (
        None
        if candidate.value_numerator is None
        or candidate.value_denominator is None
        or candidate.value_denominator <= 0
        or candidate.unit != Unit.INCH.value
        else Fraction(candidate.value_numerator, candidate.value_denominator)
    )
    if held is None and inches is None:
        held = "no value was stored"
    return EligibleSpan(
        candidate=candidate,
        row=None if key is None else key[1],
        slot=None if key is None else key[2],
        on_outline={"yes": True, "no": False}.get(outline or ""),
        held_reason=held,
        inches=inches,
    )


def architect_spans_for_row(
    session: Session, anchor: ObservationCandidate, record: ArchitectPairingRecord | None
) -> list[EligibleSpan]:
    """Every architect span the architect reader stored on the row's page, in the run the row's
    automatic pairing used. Empty when the row has no automatic pairing (the reader was off)."""
    automatic = record
    while automatic is not None and automatic.source == "reviewer":
        if automatic.supersedes_id is None:
            automatic = None
            break
        automatic = session.get(ArchitectPairingRecord, automatic.supersedes_id)
    if automatic is None:
        return []
    run_id = automatic.details.get("architect_run_id")
    if not isinstance(run_id, str):
        return []
    run = session.get(ExtractionRun, UUID(run_id))
    if run is None or run.extractor != ARCHITECT_EXTRACTOR:
        return []
    views = architect_views(session, anchor.page_id)
    candidates = session.scalars(
        select(ObservationCandidate)
        .where(
            ObservationCandidate.extraction_run_id == run.id,
            ObservationCandidate.page_id == anchor.page_id,
        )
        .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
    ).all()
    spans = [_eligible(candidate, views) for candidate in candidates]
    return sorted(spans, key=lambda span: (span.row or 0, span.slot or 0))


class ReviewerPairingRefused(ValueError):
    """A reviewer's pairing that may not be recorded, with the reason in plain words."""


def record_reviewer_pairing(
    session: Session,
    *,
    anchor: ObservationCandidate,
    package_revision_id: UUID,
    piece_count: int,
    pairs: Sequence[DecidedPair],
    note: str | None,
    actor: str,
) -> ArchitectPairingRecord:
    """Append a reviewer's pairing for one row, superseding the latest record (not committed).

    `pairs` empty means the reviewer states that nothing on the architect's drawing is comparable.
    Refused (`ReviewerPairingRefused`): a span not stored on this row's page in its pairing's run, a
    held span, a centre-line or unknown-outline span, a vendor split that is not contiguous or not on
    this row, a span or a vendor piece used twice, more than one overall, or a row with no pairing.
    """
    current = _chain_tip(session, anchor.id)
    spans = {span.candidate.id: span for span in architect_spans_for_row(session, anchor, current)}
    if current is None or not spans and pairs:
        raise ReviewerPairingRefused(
            "This row has no architect reading to pair with; the architect reader did not read it."
        )
    used_spans: set[UUID] = set()
    used_pieces: set[int] = set()
    overall = 0
    for pair in pairs:
        span = spans.get(pair.architect_candidate_id)
        if span is None:
            raise ReviewerPairingRefused(
                "That architect dimension is not on this row's page of this drawing set."
            )
        if not span.comparable:
            raise ReviewerPairingRefused(
                f"The architect's {span.candidate.raw_text} cannot be paired: {span.refusal()}."
            )
        if span.candidate.id in used_spans:
            raise ReviewerPairingRefused("Each architect dimension can be paired only once.")
        used_spans.add(span.candidate.id)
        if pair.kind == "overall":
            overall += 1
            if pair.vendor_slot_indices:
                raise ReviewerPairingRefused("The overall pairs with the whole row, not pieces.")
            continue
        indices = pair.vendor_slot_indices
        if not indices or any(index < 0 or index >= piece_count for index in indices):
            raise ReviewerPairingRefused("Choose one or more of this row's own pieces.")
        if not _contiguous(indices) or len(set(indices)) != len(indices):
            raise ReviewerPairingRefused(
                "Pieces paired with one architect dimension must be next to each other."
            )
        if used_pieces & set(indices):
            raise ReviewerPairingRefused("Each vendor piece can be paired only once.")
        used_pieces.update(indices)
    if overall > 1:
        raise ReviewerPairingRefused("Only one architect dimension can pair with the overall.")
    record = ArchitectPairingRecord(
        package_revision_id=package_revision_id,
        page_id=anchor.page_id,
        row_anchor_candidate_id=anchor.id,
        extraction_run_id=None,
        source="reviewer",
        status="reviewer",
        pairs=[
            DecidedPair(
                pair.kind, pair.architect_candidate_id, tuple(sorted(pair.vendor_slot_indices))
            ).as_json()
            for pair in pairs
        ],
        details={
            "note": None if note is None else note[:_REASON_LIMIT],
            "architect_run_id": _run_of(current),
            "reasons": [
                (
                    "A reviewer paired the architect's dimensions with this row."
                    if pairs
                    else "A reviewer states that nothing on the architect's drawing is "
                    "comparable with this row."
                )
            ],
        },
        supersedes_id=current.id,
        decided_by=actor,
    )
    session.add(record)
    return record


def _run_of(record: ArchitectPairingRecord) -> object:
    return record.details.get("architect_run_id")


def latest_architect_pairing(session: Session, row_anchor_id: UUID) -> EffectivePairing | None:
    """The pairing that counts for one vendor countertop row (its `slot:0` candidate), or `None`.

    The reviewer's latest record wins; otherwise the latest automatic record (code, both AIs, or
    nobody). Its pairs are re-checked against the architect candidates as they stand: only a span
    that is stored on the row's page, unheld, has a value, sits on the drawn outline and is in a
    drawing still confirmed as the architect's is returned; any other pair is left out with the
    reason. `vendor_slot_indices` is empty for the overall. `source == "both-ais"` is AI-only
    information: a PASS resting on it needs a reviewer's confirmation (the rule's job, T3).
    """
    record = latest_record(session, row_anchor_id, sources=("reviewer",)) or latest_record(
        session, row_anchor_id, sources=_AUTOMATIC
    )
    if record is None:
        return None
    anchor = session.get(ObservationCandidate, row_anchor_id)
    views = set() if anchor is None else architect_views(session, anchor.page_id)
    stored_reasons = record.details.get("reasons")
    reasons = [str(reason) for reason in stored_reasons] if isinstance(stored_reasons, list) else []
    pairs: list[EffectivePair] = []
    for raw in record.pairs:
        try:
            candidate_id = UUID(str(raw["architect_candidate_id"]))
            kind = raw["kind"]
            stored_indices = raw.get("vendor_slot_indices")
            if not isinstance(stored_indices, list):
                raise TypeError("vendor_slot_indices is not a list")
            indices = tuple(int(index) for index in stored_indices)
        except (KeyError, TypeError, ValueError):
            reasons.append("A stored pair could not be read and is left out.")
            continue
        if kind not in ("piece", "overall"):
            reasons.append("A stored pair has no kind and is left out.")
            continue
        candidate = session.get(ObservationCandidate, candidate_id)
        run = None if candidate is None else session.get(ExtractionRun, candidate.extraction_run_id)
        if (
            candidate is None
            or anchor is None
            or candidate.page_id != anchor.page_id
            or run is None
            or run.extractor != ARCHITECT_EXTRACTOR
        ):
            reasons.append("A paired architect dimension is not on this row's page; left out.")
            continue
        span = _eligible(candidate, views)
        if not span.comparable:
            reasons.append(f"The architect's {candidate.raw_text} is left out: {span.refusal()}.")
            continue
        if kind == "piece" and (not indices or not _contiguous(indices)):
            reasons.append("A paired vendor split is not contiguous; left out.")
            continue
        pairs.append(
            EffectivePair(
                kind=kind,
                architect_candidate_id=candidate_id,
                vendor_slot_indices=() if kind == "overall" else indices,
            )
        )
    source = record.source
    if source not in ("code", "both-ais", "reviewer", "none"):
        return None
    return EffectivePairing(
        record_id=record.id,
        source=source,  # type: ignore[arg-type]
        status=record.status,
        pairs=tuple(pairs),
        reasons=tuple(reasons),
    )
