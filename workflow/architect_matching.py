"""Match each vendor countertop row with one view of the architect's own file: code, both AIs, then a reviewer (#1166).

When the architect's drawings are uploaded as their own PDF, type 1 must first know which of the
architect's views draws the same countertop as each vendor row. This module decides that for every
vendor row the slot reader chose and read, on a revision whose architect file was indexed
(`workflow/architect_view_index.py`). It never compares a value.

1. **Code** (`extraction/architect/view_matching.match_by_code`): a printed reference to exactly one
   view, else a clear geometry winner (the run's length after scaling), else no pick. Every view of
   the file is ranked; the position-pairing of the vendor row against each view with rows is kept
   as that candidate's `code_pairing`, for the reviewer and for Phase 4.
2. **Both Claude readers**, in the slot reader's own batch (same pacer, effort, spend guard and
   stored-answer reuse, #1112): one picture, the vendor's view on the left with the row outlined in
   red (`V`), the top three ranked architect views on the right numbered in blue, all at the same
   size per real inch when every scale is known; the fixed question `arch-view-match-v1`, judged
   only by what is drawn.
3. **Decided by decision D1** (`view_matching.decide_match`): automatic only when code picks a view
   and both AIs pick that same view. Twins, ties and doubt go to the reviewer with nothing chosen.
4. **Remembered** (decision D3): a person's earlier decision for the same vendor item is carried
   (`carried`) only when the vendor's page is identical and code does not contradict it; otherwise
   it is shown on its candidate as "remembered", never chosen. No AI is asked for a carried row.

**Never silent.** Every vendor row with a chosen row gets exactly one `RowMatch`, with its status and
reasons. **Combined sheets never reach it:** a row whose own page has an architect view confirmed
on it is skipped, and the stage builds a matcher only when the revision's architect file was
indexed.

`MatchingArchitectPairing` runs the matcher inside the slot reader's architect step, before the
pairing (`ArchitectPairing.pair`), and attaches each row's match to its result
(`PageSlotResult.architect_match`). Phase 4 pairs against the matched view from there.

Source: issue #1166 · Plan: "Type 1 with a separate architect PDF (2026-10-10)" §3 ·
Verification: `tests/workflow/test_architect_matching.py`
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

from evidence.crop import _crop_rgb, decode_rgb_png, encode_png
from extraction.architect.pairing import DrawnRow, DrawnSpan
from extraction.architect.view_matching import (
    CandidateScore,
    CodeMatch,
    MatchDecision,
    MatchSettings,
    VendorViewFacts,
    decide_match,
    match_by_code,
)
from extraction.geometry.rows import Box
from extraction.slot_reader.bedrock import ARCH_MATCH_PROMPT_ID, ArchMatchAnswer, CropJob
from workflow.architect_match_contract import MatchStatus, RowMatch, restrict_to_view
from workflow.architect_match_records import RememberedMatch, same_vendor_item
from workflow.architect_pairing import (
    _ARCHITECT_COLOUR,
    _VENDOR_COLOUR,
    MEASURED_PAIRING_SETTINGS,
    ArchitectPageInput,
    ArchitectPairing,
    VendorRowInput,
    _badge,
    _badge_size,
    _outline,
    _union,
    pair_by_code,
    vendor_row_input,
    vendor_scale,
)
from workflow.architect_view_index import ArchitectViewCrop, IndexedView

if TYPE_CHECKING:
    from storage.store import ArtifactStore
    from workflow.slot_reader import PageSlotResult, SlotPage

__all__ = [
    "MATCH_PICTURE_MAX_SIDE",
    "MEASURED_MATCH_SETTINGS",
    "ArchitectMatcher",
    "MatchQuestion",
    "MatchingArchitectPairing",
    "VendorPageFacts",
    "match_picture",
    "vendor_drawn_row",
]

#: The matching thresholds. On the split keyed set (#1166, local proof) every vendor run and its
#: architect counterpart's run agreed within a quarter of an inch after scaling; a tolerance of one
#: inch keeps every such pair, and a clear winner must lead every other view by three more, so two
#: kitchenettes a few inches apart are a tie for the reviewer, never a pick. The position pairing's
#: settings are the pairing's own (`MEASURED_PAIRING_SETTINGS`). Three views are shown to the AIs.
MEASURED_MATCH_SETTINGS: Final = MatchSettings(
    run_length_tolerance_in=Fraction(1),
    clear_margin_in=Fraction(3),
    shown_to_ais=3,
    pairing=MEASURED_PAIRING_SETTINGS,
)

MATCH_PICTURE_MAX_SIDE: Final = 1800
_GUTTER_PX: Final = 24
_MARGIN_PX: Final = 12
_DOT_PX: Final = 3
_OUTLINE_PX: Final = 3
_FRAME_MARGIN_PX: Final = 20
_WHITE: Final = 255


@dataclass(frozen=True, slots=True)
class VendorPageFacts:
    """What the matcher knows of one vendor page besides its reading."""

    page_index: int
    """Its place in the vendor's file (0-based)."""
    content_hash: str
    references: tuple[str, ...]
    """Architect view references printed on it, normalised (`view_matching.find_references`)."""
    view_boxes: tuple[Box, ...]
    """The drawings pasted on it (pdfplumber's frame), for framing the vendor's view."""
    has_architect_view: bool
    """A view on this page is confirmed as the architect's: a combined sheet, never matched."""
    title: str | None = None


@dataclass(frozen=True, slots=True)
class MatchQuestion:
    """The one picture both readers are shown for a row, and what its numbers stand for."""

    picture_png: bytes
    shown: tuple[str, ...]
    """1..k → these view keys (index row ids as text)."""
    common_scale: bool
    packet: Mapping[str, object]


# --- inputs ----------------------------------------------------------------------------------------


def vendor_drawn_row(vendor: VendorRowInput) -> DrawnRow | None:
    """The vendor's row as drawn, with its scale from its sealed pieces; `None` when its pieces
    are not one unbroken chain."""
    try:
        spans = tuple(DrawnSpan(piece.x0_pt, piece.x1_pt, None) for piece in vendor.pieces)
        overall = (
            None
            if vendor.overall_x is None or not vendor.overall_x[0] < vendor.overall_x[1]
            else DrawnSpan(vendor.overall_x[0], vendor.overall_x[1], None)
        )
        return DrawnRow(
            key="vendor", spans=spans, overall=overall, pt_per_inch=vendor_scale(vendor.pieces)
        )
    except (TypeError, ValueError):
        return None


def _item_key(result: PageSlotResult, facts: VendorPageFacts | None) -> dict[str, object]:
    row = result.plan.row
    return {
        "page_content_hash": None if facts is None else facts.content_hash,
        "page_index": None if facts is None else facts.page_index,
        "pieces": len(result.plan.slots),
        "row_y_pt": None if row is None else str(row.y),
    }


# --- the picture -----------------------------------------------------------------------------------


def _resized(rgb: Any, width: int, height: int) -> Any:
    import cv2

    return cv2.resize(rgb, (max(1, width), max(1, height)), interpolation=cv2.INTER_AREA)


def match_picture(
    page: SlotPage,
    frame_px: tuple[int, int, int, int],
    row_px: tuple[int, int, int, int],
    vendor_px_per_inch: Fraction | None,
    crops: Sequence[ArchitectViewCrop],
) -> tuple[bytes, bool]:
    """The vendor's view (`frame_px` of the page) beside the architect views, and whether every
    panel is at the same size per real inch.

    At the same size when the vendor's and every view's scale are known (each shrunk to the
    smallest pixels-per-inch among them), else each fitted to the same height. Then everything is
    shrunk together to at most `MATCH_PICTURE_MAX_SIDE` a side. The vendor's row is outlined in red
    and tagged `V`; the architect views are tagged 1..k in blue. The marks say only which drawing is
    which; no value comes from them.
    """
    import numpy as np

    left, top, right, bottom = frame_px
    vendor = np.frombuffer(_crop_rgb(page.rendered, frame_px), dtype=np.uint8).reshape(
        bottom - top, right - left, 3
    )
    panels: list[Any] = [vendor]
    for crop in crops:
        if crop.png is None:
            raise ValueError("an architect view without a picture cannot be shown")
        crop_width, crop_height, pixels = decode_rgb_png(crop.png)
        panels.append(np.frombuffer(pixels, dtype=np.uint8).reshape(crop_height, crop_width, 3))
    scales = [vendor_px_per_inch, *(crop.px_per_inch for crop in crops)]
    common = all(scale is not None and scale > 0 for scale in scales)
    if common:
        target = min(scale for scale in scales if scale is not None)
        factors = [float(target / scale) for scale in scales if scale is not None]
    else:
        height = min(panel.shape[0] for panel in panels)
        factors = [height / panel.shape[0] for panel in panels]
    _, badge_height = _badge_size("V", _DOT_PX)
    fixed_width = 2 * _MARGIN_PX + _GUTTER_PX * (len(panels) - 1)
    fixed_height = 2 * _MARGIN_PX + badge_height + _DOT_PX
    content_width = sum(
        panel.shape[1] * factor for panel, factor in zip(panels, factors, strict=True)
    )
    content_height = max(
        panel.shape[0] * factor for panel, factor in zip(panels, factors, strict=True)
    )
    shrink = min(
        1.0,
        (MATCH_PICTURE_MAX_SIDE - fixed_width) / content_width,
        (MATCH_PICTURE_MAX_SIDE - fixed_height) / content_height,
    )
    sized: list[Any] = [
        _resized(
            panel,
            int(panel.shape[1] * factor * shrink),
            int(panel.shape[0] * factor * shrink),
        )
        for panel, factor in zip(panels, factors, strict=True)
    ]
    width = fixed_width + sum(int(panel.shape[1]) for panel in sized)
    height = fixed_height + max(int(panel.shape[0]) for panel in sized)
    canvas = np.full((height, width, 3), _WHITE, dtype=np.uint8)
    x = _MARGIN_PX
    y = _MARGIN_PX + badge_height + _DOT_PX
    origins: list[tuple[int, int]] = []
    for panel in sized:
        h, w = int(panel.shape[0]), int(panel.shape[1])
        canvas[y : y + h, x : x + w] = panel
        origins.append((x, y))
        x += w + _GUTTER_PX
    painted = bytearray(canvas.tobytes())
    vendor_factor = factors[0] * shrink
    vx, vy = origins[0]
    row_box = (
        vx + int((row_px[0] - left) * vendor_factor) - _OUTLINE_PX,
        vy + int((row_px[1] - top) * vendor_factor) - _OUTLINE_PX,
        vx + int((row_px[2] - left) * vendor_factor) + _OUTLINE_PX,
        vy + int((row_px[3] - top) * vendor_factor) + _OUTLINE_PX,
    )
    _outline(painted, width, height, row_box, _VENDOR_COLOUR, _OUTLINE_PX)
    _badge(painted, width, height, (vx, _MARGIN_PX), "V", _VENDOR_COLOUR, _DOT_PX)
    for number, (ox, _oy) in enumerate(origins[1:], start=1):
        _badge(painted, width, height, (ox, _MARGIN_PX), str(number), _ARCHITECT_COLOUR, _DOT_PX)
    return encode_png(width, height, bytes(painted)), common


def _frame(
    page: SlotPage,
    result: PageSlotResult,
    facts: VendorPageFacts | None,
    row_px: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """The smallest drawing pasted on the page that holds the whole row, else the whole page."""
    row = result.plan.row
    width, height = page.rendered.width_px, page.rendered.height_px
    holding: list[tuple[int, int, int, int]] = []
    if row is not None and facts is not None:
        x0, x1 = (row.ticks[0], row.ticks[-1]) if row.ticks else (None, None)
        for box in facts.view_boxes:
            if x0 is None or x1 is None:
                break
            if box.x0 <= x0 and x1 <= box.x1 and box.top <= row.y <= box.bottom:
                first = page.rows.to_pixels(box.x0, box.top)
                second = page.rows.to_pixels(box.x1, box.bottom)
                holding.append(
                    (
                        min(first[0], second[0]),
                        min(first[1], second[1]),
                        max(first[0], second[0]),
                        max(first[1], second[1]),
                    )
                )
    if holding:
        chosen = min(holding, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
        chosen = _union(chosen, row_px)
    else:
        chosen = (0, 0, width, height)
    return (
        max(0, chosen[0] - _FRAME_MARGIN_PX),
        max(0, chosen[1] - _FRAME_MARGIN_PX),
        min(width, chosen[2] + _FRAME_MARGIN_PX),
        min(height, chosen[3] + _FRAME_MARGIN_PX),
    )


# --- the matcher -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Pending:
    result: PageSlotResult
    vendor: VendorRowInput | None
    code: CodeMatch
    item_key: Mapping[str, object]
    facts: VendorPageFacts | None
    remembered: RememberedMatch | None
    remembered_view: str | None
    """The remembered decision's view, as a key of this run's index; `None` for none or gone."""
    forced_reasons: tuple[str, ...]
    """Why a person's earlier decision makes the reviewer decide again (code contradicts it)."""
    question: MatchQuestion | None


def _key(page_index: int) -> str:
    return f"p{page_index}:arch-match"


@dataclass(frozen=True)
class ArchitectMatcher:
    """Matches vendor rows with the views of the revision's architect file (#1166).

    `views` and `crops` come from `record_architect_view_index` (this run's index); `remembered`
    from `remembered_matches`; `vendor_pages` from the stage (references printed, page identity,
    pasted drawings, and whether the page holds an architect view of its own); `architect_pages`
    is the pairing's input for every page the architect reader read, for each candidate's position
    pairing.
    """

    settings: MatchSettings
    views: tuple[IndexedView, ...]
    crops: Mapping[UUID, ArchitectViewCrop]
    remembered: tuple[RememberedMatch, ...] = ()
    vendor_pages: Mapping[UUID, VendorPageFacts] = field(default_factory=dict)
    architect_pages: Mapping[UUID, ArchitectPageInput] = field(default_factory=dict)

    def _view(self, key: str) -> IndexedView:
        return next(view for view in self.views if str(view.view.view_id) == key)

    def _remembered(
        self, item_key: Mapping[str, object]
    ) -> tuple[RememberedMatch | None, bool, str | None]:
        """The newest person's decision for this vendor item, whether the item is identical, and the
        view it names in this run's index."""
        for remembered in self.remembered:
            same, identical = same_vendor_item(remembered.vendor_item_key, item_key)
            if not same:
                continue
            view = next(
                (
                    str(view.view.view_id)
                    for view in self.views
                    if remembered.view_key is not None and view.carry_key == remembered.view_key
                ),
                None,
            )
            return remembered, identical, view
        return None, False, None

    def _carried(
        self, pending: _Pending
    ) -> tuple[MatchStatus, str | None, tuple[str, ...]] | tuple[None, None, tuple[str, ...]]:
        """A person's decision carried to an identical vendor item, or why it cannot be."""
        remembered = pending.remembered
        assert remembered is not None
        code = pending.code
        named = f"on revision {remembered.revision_number}"
        if remembered.view_key is None:
            if code.pick is not None:
                return (
                    None,
                    None,
                    (
                        (
                            f"The reviewer found no matching architect view {named}, but code now finds "
                            "one: the reviewer decides again."
                        ),
                    ),
                )
            return (
                "none_matches",
                None,
                (
                    (
                        f"Carried from {named}: the reviewer found no view in the architect's drawings "
                        "that shows this countertop, and the vendor's page is unchanged."
                    ),
                ),
            )
        if pending.remembered_view is None:
            return (
                None,
                None,
                (
                    (
                        f"The architect view the reviewer chose {named} is not in the architect's "
                        "drawings any more: the reviewer decides again."
                    ),
                ),
            )
        if code.pick is not None and code.pick != pending.remembered_view:
            return (
                None,
                None,
                (
                    (
                        f"Code now picks another architect view than the one the reviewer chose {named}: "
                        "the reviewer decides again."
                    ),
                ),
            )
        view = self._view(pending.remembered_view)
        if not view.view.separated:
            return (
                "not_separated",
                pending.remembered_view,
                (
                    (
                        f"Carried from {named}: the architect view chosen is not clearly apart from its "
                        "neighbour, so its dimensions were not read."
                    ),
                ),
            )
        return (
            "carried_over",
            pending.remembered_view,
            (
                (
                    f"Carried from {named}: the reviewer chose this architect view, and the vendor's page "
                    "is unchanged."
                ),
            ),
        )

    def _code_pairing(
        self, vendor: VendorRowInput | None, view: IndexedView
    ) -> dict[str, object] | None:
        if vendor is None or not view.facts.rows:
            return None
        page = self.architect_pages.get(view.view.page_id)
        if page is None:
            return None
        restricted = restrict_to_view(page, view.view_number, view.extent)
        if not restricted.rows:
            return None
        outcome, _raw = pair_by_code(vendor, restricted, self.settings.pairing)
        details = outcome.details
        return {
            "status": outcome.status,
            "source": "code",
            "pairs": [pair.as_json() for pair in outcome.pairs],
            "details": {
                key: details[key] for key in ("code", "excluded", "reasons") if key in details
            },
        }

    def _candidates(
        self,
        pending: _Pending,
        shown: Sequence[str],
        picked_by: Mapping[str, list[str]],
        remembered_view: str | None,
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "view_id": score.key,
                "rank": score.rank,
                "shown_number": shown.index(score.key) + 1 if score.key in shown else None,
                "score": _score_json(score),
                "evidence": [
                    *score.evidence,
                    *(
                        [f"remembered from revision {pending.remembered.revision_number}"]
                        if pending.remembered is not None and score.key == remembered_view
                        else []
                    ),
                ],
                "code_pairing": self._code_pairing(pending.vendor, self._view(score.key)),
                "remembered": score.key == remembered_view,
                "ai_picked_by": list(picked_by.get(score.key, [])),
            }
            for score in pending.code.ranked
        )

    def _question(
        self,
        page: SlotPage,
        pending: _Pending,
        *,
        store: ArtifactStore | None,
        effort: str | None,
    ) -> MatchQuestion | None:
        vendor = pending.vendor
        if vendor is None or not vendor.pieces:
            return None
        shown: list[str] = []
        for score in pending.code.ranked[: self.settings.shown_to_ais]:
            crop = self.crops.get(UUID(score.key))
            if crop is None or crop.png is None:
                break
            shown.append(score.key)
        if not shown:
            return None
        crops = [self.crops[UUID(key)] for key in shown]
        row_px = _union(*(piece.box_px for piece in vendor.pieces))
        frame = _frame(page, pending.result, pending.facts, row_px)
        scale = vendor_scale(vendor.pieces)
        picture, common = match_picture(
            page,
            frame,
            row_px,
            None if scale is None else scale * page.rendered.dpi / 72,
            crops,
        )
        digest = hashlib.sha256(picture).hexdigest()
        storage_key: str | None = None
        if store is not None:
            storage_key = (
                f"reader-questions/{page.document_version_id}/pages/{page.page_index}/"
                f"arch-match-{digest}.png"
            )
            saved = store.put(storage_key, io.BytesIO(picture), content_type="image/png")
            if saved.sha256 != digest:
                raise ValueError("stored architect-match picture hash does not match its bytes")
        packet: dict[str, object] = {
            "question_id": _key(page.page_index),
            "document_version_id": str(page.document_version_id),
            "page_index": page.page_index,
            "source_page_sha256": page.rendered.page_content_hash,
            "prompt_id": ARCH_MATCH_PROMPT_ID,
            "vendor_slots": [piece.slot_index for piece in vendor.pieces],
            "architect_view_ids": list(shown),
            "architect_pictures": [crop.sha256 for crop in crops],
            "common_scale": common,
            "images": {"arch_match_view": {"sha256": digest, "storage_key": storage_key}},
            **({} if effort is None else {"effort": effort}),
        }
        packet["packet_sha256"] = hashlib.sha256(
            json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return MatchQuestion(picture, tuple(shown), common, packet)

    def match(
        self,
        results: Sequence[PageSlotResult],
        pages: Sequence[SlotPage],
        *,
        ask: Callable[[Sequence[CropJob]], Mapping[tuple[str, str], object]],
        readers: tuple[str, ...],
        ask_the_ais: bool,
        store: ArtifactStore | None,
        effort: str | None,
    ) -> dict[int, RowMatch]:
        """Every chosen vendor row's match, by page index: code, one batched question to both
        readers (`ask_the_ais` only when they are the Claude pair), decided by D1; a person's earlier
        decision carried only to an identical item code does not contradict."""
        by_index = {page.page_index: page for page in pages}
        facts = [view.facts for view in self.views]
        pending: list[_Pending] = []
        carried: dict[int, RowMatch] = {}
        jobs: list[CropJob] = []
        for result in results:
            anchor = result.owner_candidate_ids.get("slot:0")
            if anchor is None or result.plan.row is None:
                continue
            page_facts = self.vendor_pages.get(result.page_id)
            if page_facts is not None and page_facts.has_architect_view:
                continue
            vendor = vendor_row_input(result)
            drawn = None if vendor is None else vendor_drawn_row(vendor)
            code = match_by_code(
                VendorViewFacts(drawn, () if page_facts is None else page_facts.references),
                facts,
                self.settings,
            )
            item_key = _item_key(result, page_facts)
            remembered, identical, remembered_view = self._remembered(item_key)
            row = _Pending(
                result=result,
                vendor=vendor,
                code=code,
                item_key=item_key,
                facts=page_facts,
                remembered=remembered,
                remembered_view=remembered_view,
                forced_reasons=(),
                question=None,
            )
            if remembered is not None and identical and self.views:
                status, chosen, reasons = self._carried(row)
                if status is not None:
                    carried[result.page_index] = self._row_match(
                        row,
                        status=status,
                        source="carried",
                        chosen=chosen,
                        reasons=reasons,
                        ai_picks=(),
                        shown=(),
                        picked_by={},
                        extra={
                            "carried_from_id": str(remembered.record_id),
                            "carried_from_revision": remembered.revision_number,
                        },
                    )
                    continue
                row = replace(row, forced_reasons=reasons)
            page = by_index.get(result.page_index)
            if ask_the_ais and readers and page is not None and self.views:
                question = self._question(page, row, store=store, effort=effort)
                if question is not None:
                    row = replace(row, question=question)
                    jobs.extend(
                        CropJob(
                            key=_key(result.page_index),
                            model_id=model,
                            page_index=result.page_index,
                            png=question.picture_png,
                            arch_match_question=True,
                            architect_candidates=len(question.shown),
                            arch_match_common_scale=question.common_scale,
                            question_packet=question.packet,
                        )
                        for model in readers
                    )
            pending.append(row)
        answers = ask(jobs) if jobs else {}
        decided: dict[int, RowMatch] = dict(carried)
        for row in pending:
            index = row.result.page_index
            question = row.question
            shown = () if question is None else question.shown
            picks: list[str | None] = []
            ai_picks: list[dict[str, object]] = []
            picked_by: dict[str, list[str]] = {}
            if question is not None:
                for model in readers:
                    answer = answers.get((_key(index), model))
                    pick, entry = _ai_pick(model, answer, shown)
                    picks.append(pick)
                    ai_picks.append(entry)
                    if pick is not None:
                        picked_by.setdefault(pick, []).append(model)
            decision = decide_match(row.code, picks, shown, [view.facts for view in self.views])
            decision = _second_look(decision, row)
            decided[index] = self._row_match(
                row,
                status=decision.status,  # type: ignore[arg-type]
                source="automatic",
                chosen=decision.chosen,
                reasons=(*row.forced_reasons, *decision.reasons),
                ai_picks=tuple(ai_picks),
                shown=shown,
                picked_by=picked_by,
                extra={},
            )
        return decided

    def _row_match(
        self,
        row: _Pending,
        *,
        status: MatchStatus,
        source: Literal["automatic", "carried"],
        chosen: str | None,
        reasons: Sequence[str],
        ai_picks: tuple[Mapping[str, object], ...],
        shown: Sequence[str],
        picked_by: Mapping[str, list[str]],
        extra: Mapping[str, object],
    ) -> RowMatch:
        facts = row.facts
        return RowMatch(
            status=status,
            source=source,
            chosen=None if chosen is None else self._view(chosen).view,
            code=row.code,
            ai_picks=ai_picks,
            candidate_json=self._candidates(row, shown, picked_by, row.remembered_view),
            reasons=(*reasons, *row.code.reasons),
            question_packet=None if row.question is None else row.question.packet,
            details={
                "vendor_item_key": dict(row.item_key),
                "vendor_title": None if facts is None else facts.title,
                "vendor_references": [] if facts is None else list(facts.references),
                **extra,
            },
        )


def _second_look(decision: MatchDecision, row: _Pending) -> MatchDecision:
    """A person's earlier decision for this item that the automatic result would go against sends
    the row to the reviewer: doubt is never decided by code alone."""
    if row.forced_reasons and decision.status in {"auto_matched", "not_separated"}:
        return MatchDecision("needs_reviewer", None, decision.reasons)
    remembered = row.remembered
    if (
        remembered is not None
        and decision.status in {"auto_matched", "not_separated"}
        and decision.chosen != row.remembered_view
    ):
        return MatchDecision(
            "needs_reviewer",
            None,
            (
                *decision.reasons,
                (
                    f"The reviewer decided otherwise for this item on revision "
                    f"{remembered.revision_number}: the reviewer decides again."
                ),
            ),
        )
    return decision


def _ai_pick(
    model: str, answer: object, shown: Sequence[str]
) -> tuple[str | None, dict[str, object]]:
    if not isinstance(answer, ArchMatchAnswer):
        return None, {
            "model_id": model,
            "answer": "no_answer",
            "view_id": None,
            "shown_number": None,
            "same": [],
            "why": "",
            "invocation_id": None,
        }
    word = "unsure" if answer.pick is None else "none" if answer.pick == 0 else "view"
    view = None if answer.pick is None or answer.pick == 0 else shown[answer.pick - 1]
    return view, {
        "model_id": model,
        "answer": word,
        "view_id": view,
        "shown_number": None if view is None else answer.pick,
        "same": list(answer.same),
        "why": answer.why,
        "invocation_id": None,
    }


def _fraction_text(value: Fraction | None) -> str | None:
    if value is None:
        return None
    return str(value.numerator) if value.denominator == 1 else f"{value}"


def _score_json(score: CandidateScore) -> dict[str, object]:
    return {
        "reference_match": score.reference_match,
        "fits": score.fits,
        "run_length_error_in": _fraction_text(score.run_length_error_in),
        "run_length_error_display": (
            None
            if score.run_length_error_in is None
            else str(
                (
                    Decimal(score.run_length_error_in.numerator)
                    / Decimal(score.run_length_error_in.denominator)
                ).quantize(Decimal("0.1"))
            )
        ),
        "best_row": score.best_row_key,
        "bays_vendor": score.bays_vendor,
        "bays_architect": score.bays_architect,
        "pair_status": score.pair_status,
        "pair_support": score.pair_support,
        "ticks_aligned": score.ticks_aligned,
    }


# --- inside the slot reader's architect step -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MatchingArchitectPairing(ArchitectPairing):
    """The slot reader's architect step on a revision whose architect file is a separate, indexed
    PDF: first each row's match (`ArchitectMatcher.match`, in the same batch machinery), attached
    to its result, then the pairing exactly as `ArchitectPairing.pair` does it."""

    matcher: ArchitectMatcher | None = None

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
        matched = results
        if self.matcher is not None:
            found = self.matcher.match(
                results,
                pages,
                ask=ask,
                readers=readers,
                ask_the_ais=ask_the_ais,
                store=store,
                effort=effort,
            )
            matched = tuple(
                (
                    replace(result, architect_match=found[result.page_index])
                    if result.page_index in found
                    else result
                )
                for result in results
            )
        return ArchitectPairing.pair(
            self,
            matched,
            pages,
            ask=ask,
            readers=readers,
            ask_the_ais=ask_the_ais,
            store=store,
            effort=effort,
        )
