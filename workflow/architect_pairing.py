"""Pair the architect's dimensions with the vendor's countertop row: code and both AIs, then a reviewer (#1053).

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
2. **Both Claude readers, on every page with an architect span code has not refused** (stored,
   unheld, not a centre line), whatever code decided: one numbered picture (vendor pieces in red,
   V1..Vn; those architect spans in blue, A1..Am), the fixed question `arch-pair-v3`, the same
   readers, effort, spend guard and batch as the slot reader. Each AI says what EVERY A measures
   (countertop, cabinet run, one cabinet, filler, blocking, a centre line, a clearance...) and which
   A measures the same physical thing as the vendor's overall and each piece, `none`, or `unsure`
   (#1109). `unsure` from either AI is a refusal: the reviewer pairs. Only two **identical**
   pairings count, with measures for every A they pair that code treats alike (two words code
   handles the same way, such as one cabinet and a filler, are not a disagreement), and code then
   checks every pair: what it measures must be the same kind of thing (`MEASURES_FOR_OVERALL`,
   `MEASURES_FOR_PIECES`), the span must be unheld and on the drawn outline, a vendor split must be
   contiguous, and the two drawn lengths through their scales must agree within a quarter.
   Anything else is dropped with its reason; both raw answers are kept.
3. **Weighed together** (`combine`): code's pairs and the AIs' accepted pairs identical →
   `code+ais`, the only automatic pairing. Code alone (`code`) or the AIs alone (`both-ais`) is one
   judgment: the rule must not let a PASS or a FAIL rest on it without a reviewer's confirmation
   (decision log 2026-10-09). Neither → `none`, with the reasons in plain words. When the AIs find
   nothing to pair but code could not decide (`ambiguous`, `no_fit`), code's status is kept, so the
   check asks the reviewer whenever the architect prints a usable dimension (#1109).
4. **The reviewer** pairs with one click (`app/api/slot_rows.py`), a new record superseding the
   latest (`reviewer`).

**What never pairs.** A span running to a fixture centre line, or whose ends are not known to sit
on the casework outline (decision D4); a held architect value; a span from another page.

`latest_architect_pairing` is the read path the rule uses (`workflow/architect_pairing_contract.py`).
It and the reviewer's helpers live in `workflow/architect_pairing_records.py`, which reads no drawing
so the API may import it; they are re-exported here unchanged.

Source: issue #1053 · Plan: "Type 1 vendor vs architect (reasoned, 2026-10-09)" §3, §5 T2, §7 ·
Verification: `tests/workflow/test_architect_pairing.py`, `tests/api/test_architect_pairing.py`
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise
from typing import TYPE_CHECKING, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evidence import ArchitectPairingRecord, ObservationCandidate
from app.models.runs import ModelInvocation
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
from extraction.slot_reader.bedrock import (
    ARCH_PAIR_PROMPT_ID,
    ARCH_PAIR_PROMPT_IDS,
    ArchPairAnswer,
    CropJob,
)
from extraction.slot_reader.seal import LabelState
from units.measurement import Unit
from workflow.architect_pairing_contract import PairingSource
from workflow.architect_pairing_records import (
    DecidedPair,
    ReviewerPairingRefused,
    _contiguous,
    _span_key,
    architect_spans_for_row,
    latest_architect_pairing,
    latest_record,
    record_reviewer_pairing,
)

if TYPE_CHECKING:
    from storage.store import ArtifactStore
    from workflow.slot_reader import PageSlotResult, SlotPage

__all__ = [
    "AI_DRAWN_LENGTH_BAND",
    "MEASURED_PAIRING_SETTINGS",
    "MEASURES_FOR_OVERALL",
    "MEASURES_FOR_PIECES",
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
    "combine",
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

#: What an architect dimension must measure, as both AIs agree (`arch-pair-v2`/`-v3`), to be paired
#: with the vendor's whole run: the countertop or a run of cabinets.
MEASURES_FOR_OVERALL: Final = frozenset({"countertop", "cabinet_run"})
#: ... and to be paired with vendor pieces: one cabinet or a filler/end panel (several vendor
#: pieces may split one), or a run of cabinets only when the pieces are the whole run. A dimension to
#: a centre line, to blocking or backing, or between a wall and an object's edge never pairs.
MEASURES_FOR_PIECES: Final = frozenset({"single_cabinet", "filler_or_end_panel"})
_OVERALL_MEASURES: Final = MEASURES_FOR_OVERALL
#: Code's own statuses that mean it could not decide (#1088), which two AI "nothing" answers never
#: overwrite (#1109).
_CODE_UNDECIDED: Final = frozenset({PairingStatus.AMBIGUOUS.value, PairingStatus.NO_FIT.value})
_PIECE_MEASURES: Final = MEASURES_FOR_PIECES
#: Each measure in plain words, for the reasons a reviewer reads.
_MEASURE_WORDS: Final = {
    "countertop": "the countertop",
    "cabinet_run": "a run of cabinets",
    "single_cabinet": "one cabinet",
    "filler_or_end_panel": "a filler or end panel",
    "wall_to_wall": "wall to wall",
    "clearance_or_gap": "a clearance or gap",
    "blocking_or_backing": "blocking or backing",
    "fixture_or_appliance_centre": "to a fixture or appliance centre line",
    "appliance_opening": "an appliance opening",
    "height_or_other": "a height or something else",
    "unsure": "something neither AI is sure of",
}

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
    architect_tags: Collection[str] | None = None,
) -> ArchitectPageInput:
    """One page's architect rows for the pairing.

    `architect_views` are the annotation indices of the drawings whose role is the architect's now
    (code's two judgments or a person's); `candidate_ids` maps `(view, row rank, slot)` to the
    candidate the architect reader stored for that span.
    """

    def tag(source: str, number: int) -> str:
        return f"view-{number}" if source == "content" else f"panel-{number}"

    def is_architect(source: str, number: int) -> bool:
        # By tag when given (#1163): `panel-<n>` and `view-<n>` share a number.
        if architect_tags is not None:
            return tag(source, number) in architect_tags
        return number in architect_views

    views = {(view.source, view.annotation_index): view for view in reading.views}
    rows: list[ArchitectRowInput] = []
    for row in reading.rows:
        if not is_architect(row.view_source, row.view_annotation_index):
            continue
        view = views.get((row.view_source, row.view_annotation_index))
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
    if not any(is_architect(view.source, view.annotation_index) for view in reading.views):
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
                "none", PairingStatus.NO_FIT.value, (), (reason,), {**base, "reasons": [reason]}
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
    status = result.status.value
    if result.status is PairingStatus.PAIRED and not pairs:
        status = PairingStatus.NOTHING_COMPARABLE.value
        reasons.append(
            "Code lined the rows up, but no architect dimension that lines up can be compared."
        )
    details = {
        **base,
        "code": _result_details(result),
        "excluded": excluded,
        "reasons": reasons,
    }
    return (
        PairingOutcome("code" if pairs else "none", status, tuple(pairs), tuple(reasons), details),
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


def _askable(span: ArchitectSpanInput) -> bool:
    """Whether the two AIs are shown this span: stored, unheld, and not refused by code as a
    centre-line (or other off-outline) dimension. An unknown outline is shown, so the AIs can say
    what it measures; a pair with it is still dropped by the code witness."""
    return (
        span.on_outline is not False and span.held_reason is None and span.candidate_id is not None
    )


def _askable_spans(page: ArchitectPageInput) -> list[tuple[int, ArchitectSpanInput]]:
    return [(row.rank, span) for row in page.rows for span in row.spans if _askable(span)]


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

    spans = _askable_spans(architect)
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
        "measures": list(answer.measures),
        "unsure": list(answer.unsure),
        "why": answer.why,
    }


def _measure_words(measure: str) -> str:
    return _MEASURE_WORDS.get(measure, measure.replace("_", " "))


@dataclass(frozen=True, slots=True)
class _AiJudgment:
    """What both AIs' answers, checked by code, pair on their own (before code's pairing is
    weighed): `paired`, `nothing_comparable`, `ais-disagree` or `ais-refused`."""

    status: str
    pairs: tuple[DecidedPair, ...]
    reasons: tuple[str, ...]
    dropped: tuple[dict[str, object], ...]
    measures: tuple[tuple[ArchitectSpanInput, str], ...]
    """Every numbered span whose measure both AIs gave alike, in A order."""
    complete: bool
    """Whether both AIs agree what every numbered span measures."""
    ai: dict[str, object]
    answers: tuple[ArchPairAnswer | None, ...]


def _judge(
    question: PairQuestion,
    answers: Sequence[tuple[str, ArchPairAnswer | None]],
    vendor: VendorRowInput,
    architect: ArchitectPageInput,
) -> _AiJudgment:
    """Both readers' answers to one pairing question, checked by code (the AIs' judgment alone).

    `unsure` from either reader, for a pairing or for what a dimension they pair measures, is a
    refusal (`ais-refused`): the reviewer pairs (#1109). Only identical pairings count: the same
    overall and the same pieces, with measures for every architect dimension they pair that code
    treats alike (`_code_result`: two words that lead to the same pairs, such as one cabinet and a
    filler for a piece, are not a disagreement; the words are kept in the reasons). Then every pair
    is witnessed. What it measures must be the same
    kind of thing (`_OVERALL_MEASURES` for the vendor's overall; `_PIECE_MEASURES` for pieces, or a
    run of cabinets only when the pieces are the whole run). Its architect span must be comparable
    (unheld, on the outline); several vendor pieces given one A-number must be next to each other;
    an A-number for the whole run must not also name only some of its pieces; and the two drawn
    lengths through their scales must agree within `AI_DRAWN_LENGTH_BAND`.
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
    ai: dict[str, object] = {
        "prompt_id": ARCH_PAIR_PROMPT_ID,
        "picture_sha256": question.picture_sha256,
        "packet_sha256": question.packet.get("packet_sha256"),
        "numbering": numbering,
        "answers": [_answer_json(answer, model) for model, answer in answers],
    }
    given = tuple(answer for _model, answer in answers)
    count = len(question.architect)
    usable = [answer for answer in given if answer is not None and len(answer.measures) == count]
    if not given or len(usable) != len(given):
        return _AiJudgment(
            "ais-refused",
            (),
            ("A reader gave no usable answer to the pairing question.",),
            (),
            (),
            False,
            ai,
            given,
        )
    first = usable[0]
    agreed = {
        a_number: first.measures[a_number - 1]
        for a_number in range(1, count + 1)
        if all(answer.measures[a_number - 1] == first.measures[a_number - 1] for answer in usable)
    }
    measures = tuple((question.architect[a - 1], measure) for a, measure in agreed.items())
    complete = len(agreed) == count

    def disagree(reason: str) -> _AiJudgment:
        return _AiJudgment("ais-disagree", (), (reason,), (), measures, complete, ai, given)

    def refuse(reason: str) -> _AiJudgment:
        return _AiJudgment("ais-refused", (), (reason,), (), measures, complete, ai, given)

    unsure = sorted({item for answer in usable for item in answer.unsure}, key=_unsure_order)
    if unsure:
        return refuse(
            "A reader is unsure which architect dimension, if any, measures "
            + _listed([_unsure_words(item) for item in unsure])
            + "; the reviewer pairs."
        )
    if any((answer.overall, answer.pieces) != (first.overall, first.pieces) for answer in usable):
        return disagree("The two readers paired the architect's dimensions differently.")
    groups: dict[int, list[int]] = {}
    for position, a_number in enumerate(first.pieces):
        if a_number:
            groups.setdefault(a_number, []).append(position)
    named = sorted({*groups, *([first.overall] if first.overall else [])})
    every = list(range(len(vendor.pieces)))

    def said(a_number: int) -> list[str]:
        """What the readers say A measures, each word once, in reader order."""
        return list(dict.fromkeys(answer.measures[a_number - 1] for answer in usable))

    not_sure_what = [a_number for a_number in named if "unsure" in said(a_number)]
    if not_sure_what:
        return refuse(
            "A reader is unsure what "
            + ", ".join(f"A{a_number}" for a_number in not_sure_what)
            + " measures, though both readers paired it; the reviewer pairs."
        )
    unsettled = [
        a_number
        for a_number in named
        if len(
            {
                _code_result(
                    measure,
                    overall=first.overall == a_number,
                    positions=groups.get(a_number, []),
                    every=every,
                )
                for measure in said(a_number)
            }
        )
        > 1
    ]
    if unsettled:
        return disagree(
            "The two readers paired the same dimensions but disagree about what "
            + ", ".join(f"A{a_number}" for a_number in unsettled)
            + " measures."
        )

    rows = {row.rank: row for row in architect.rows}
    vendor_pt = vendor_scale(vendor.pieces)
    dropped: list[dict[str, object]] = []
    reasons: list[str] = ["Both readers gave the same pairing; code checked it."]
    for a_number in named:
        if a_number not in agreed:
            reasons.append(
                f"The readers use different words for what A{a_number} measures ("
                + "; ".join(_measure_words(measure) for measure in said(a_number))
                + "), which code treats the same way, so this is not a disagreement."
            )

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

    def other_thing(span: ArchitectSpanInput, a_number: int) -> str:
        return (
            f"the architect's {span.text or 'unlabelled dimension'} measures "
            + " or ".join(_measure_words(measure) for measure in said(a_number))
            + ", not the same thing"
        )

    pairs: list[DecidedPair] = []
    whole = (
        Fraction(vendor.overall_x[1]) - Fraction(vendor.overall_x[0])
        if vendor.overall_x is not None
        else Fraction(vendor.pieces[-1].x1_pt) - Fraction(vendor.pieces[0].x0_pt)
    )
    for a_number in named:
        rank, span = question.architect_rows[a_number - 1], question.architect[a_number - 1]
        # Every reader's word for it leads to the same pairs (`_code_result`): the first decides.
        measure = first.measures[a_number - 1]
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
            if measure not in _OVERALL_MEASURES:
                drop(a_number, other_thing(span, a_number))
                continue
            if not plausible(a_number, architect_in, whole):
                continue
            pairs.append(DecidedPair("overall", span.candidate_id, ()))
        if positions:
            if not (
                measure in _PIECE_MEASURES or (measure == "cabinet_run" and positions == every)
            ):
                if first.overall != a_number:
                    drop(a_number, other_thing(span, a_number))
                continue
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
    return _AiJudgment(
        PairingStatus.PAIRED.value if pairs else PairingStatus.NOTHING_COMPARABLE.value,
        tuple(pairs),
        tuple(reasons),
        tuple(dropped),
        measures,
        complete,
        ai,
        given,
    )


def _code_result(
    measure: str, *, overall: bool, positions: Sequence[int], every: Sequence[int]
) -> tuple[bool, bool]:
    """What `_judge` does, by measure alone, with an architect dimension both readers paired:
    whether it may pair with the vendor's whole run, and whether with the pieces given for it. Two words
    with the same result (one cabinet and a filler for a piece; the countertop and a run of
    cabinets for the whole run alone; any two words that never pair) are the same answer to code."""
    if overall and measure not in _OVERALL_MEASURES:
        return False, False
    pieces = bool(positions) and (
        measure in _PIECE_MEASURES or (measure == "cabinet_run" and list(positions) == list(every))
    )
    return overall, pieces


def _unsure_order(item: str) -> tuple[int, int]:
    return (0, 0) if item == "overall" else (1, int(item.removeprefix("V")))


def _unsure_words(item: str) -> str:
    return "the vendor's whole run" if item == "overall" else f"vendor piece {item}"


def _listed(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _pair_set(pairs: Sequence[DecidedPair]) -> frozenset[tuple[str, UUID, tuple[int, ...]]]:
    return frozenset(
        (pair.kind, pair.architect_candidate_id, tuple(sorted(pair.vendor_slot_indices)))
        for pair in pairs
    )


def combine(
    code: PairingOutcome,
    judged: _AiJudgment | None,
    question: PairQuestion | None = None,
) -> PairingOutcome:
    """Code's pairing and both AIs' pairing, weighed together: the outcome that is recorded.

    * Code paired, and both AIs' accepted pairing is identical: `code+ais` (two independent
      judgments, automatic).
    * Code paired, and the AIs paired differently, disagreed, refused or were not asked: `code`
      with code's pairs (one judgment: a reviewer confirms the pairing).
    * Only the AIs paired: `both-ais` (one judgment: a reviewer confirms the pairing).
    * Neither: `none`, with the reasons; when both AIs agree what every numbered architect
      dimension measures, the reason says so in plain words. The status is the AIs' disagreement or
      refusal; else, when the AIs found nothing to pair but code could not decide (`ambiguous`,
      `no_fit`), code's status, so the check asks the reviewer whenever the architect prints a
      usable dimension (#1109: a reader that could not tell used to answer 0, and two such answers
      overwrote code's undecided status); else nothing comparable.
    """
    code_pairs = code.pairs if code.status == PairingStatus.PAIRED.value else ()
    reasons = list(code.reasons)
    details: dict[str, object] = dict(code.details)
    if judged is not None:
        details["ai"] = judged.ai
        details["dropped"] = list(judged.dropped)
        details["architect_measures"] = {
            str(span.candidate_id): measure for span, measure in judged.measures
        }
        reasons.extend(judged.reasons)
    source: PairingSource
    if code_pairs:
        pairs = code_pairs
        status = PairingStatus.PAIRED.value
        if (
            judged is not None
            and judged.status == PairingStatus.PAIRED.value
            and _pair_set(judged.pairs) == _pair_set(code_pairs)
        ):
            source = "code+ais"
            reasons.append(
                "Code (by where the dimensions are drawn) and both AIs (by what each dimension "
                "measures) paired the same dimensions."
            )
        else:
            source = "code"
            if judged is None:
                why = "the AIs were not asked"
            elif judged.status == PairingStatus.PAIRED.value:
                why = "both AIs paired them differently"
            elif judged.status == PairingStatus.NOTHING_COMPARABLE.value:
                why = "both AIs found nothing on the architect's drawing to pair"
            elif judged.status == "ais-disagree":
                why = "the two AIs disagreed with each other"
            else:
                why = "an AI gave no usable answer or was unsure"
            reasons.append(
                f"Only code paired these dimensions ({why}); a reviewer confirms the pairing "
                "before any result on it counts."
            )
    elif judged is not None and judged.status == PairingStatus.PAIRED.value:
        source, status, pairs = "both-ais", PairingStatus.PAIRED.value, judged.pairs
        reasons.append(
            "Only the two AIs paired these dimensions (code could not line the rows up); a "
            "reviewer confirms the pairing before any result on it counts."
        )
    else:
        source, pairs = "none", ()
        code_undecided = code.status in _CODE_UNDECIDED
        if judged is None:
            status = code.status
        elif judged.status in {"ais-disagree", "ais-refused"}:
            status = judged.status
        elif code_undecided:
            status = code.status
            reasons.append(
                "Both AIs found nothing on the architect's drawing to pair, but code could not "
                "decide how the rows line up, so that is not settled."
            )
        else:
            status = PairingStatus.NOTHING_COMPARABLE.value
        if judged is not None and judged.complete and judged.measures:
            reasons.append(
                "Both AIs agree what the architect's dimensions here measure: "
                + "; ".join(
                    f"{span.text or 'an unlabelled dimension'} {_measure_words(measure)}"
                    for span, measure in judged.measures
                )
                + (
                    ". None of them is paired with the vendor's row."
                    if status != PairingStatus.NOTHING_COMPARABLE.value
                    else ". None of them is paired with the vendor's row, so nothing is compared."
                )
            )
        if status != PairingStatus.NOTHING_COMPARABLE.value or judged is None:
            reasons.append("Nothing is paired; the reviewer can pair them.")
    details["reasons"] = reasons
    return PairingOutcome(
        source,
        status,
        tuple(pairs),
        tuple(reasons),
        details,
        question,
        () if judged is None else judged.answers,
    )


def resolve_answers(
    question: PairQuestion,
    answers: Sequence[tuple[str, ArchPairAnswer | None]],
    vendor: VendorRowInput,
    architect: ArchitectPageInput,
    code: PairingOutcome,
) -> PairingOutcome:
    """Both readers' answers to one pairing question, checked by code (`_judge`) and weighed
    with code's own pairing (`combine`)."""
    return combine(code, _judge(question, answers, vendor, architect), question)


def _ask_the_ais(vendor: VendorRowInput, page: ArchitectPageInput) -> bool:
    """Whether a row's page is worth one question: the vendor row is not held, and at least one
    architect span is stored, unheld and not refused by code. Asked whatever code decided: an
    automatic pairing needs both judgments."""
    return vendor.held_reason is None and bool(vendor.pieces) and bool(_askable_spans(page))


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
        """Every result with its pairing: code's, and one batched question to both readers for
        every row whose page has an architect span code has not refused (`ask_the_ais` only when
        both readers are the Claude pair), weighed together by `combine`."""
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
            if not (ask_the_ais and _ask_the_ais(vendor, architect)):
                decided[result.page_index] = combine(code, None)
                continue
            question = pair_question(
                by_index[result.page_index], vendor, architect, store=store, effort=effort
            )
            if question is None:
                decided[result.page_index] = combine(code, None)
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
            ModelInvocation.prompt_id.in_(tuple(ARCH_PAIR_PROMPT_IDS)),
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
