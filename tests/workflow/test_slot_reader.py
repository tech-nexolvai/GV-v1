"""Slot reads in the worker, end to end on hand-built sheets with fake readers (#987).

Verification for `workflow/slot_reader.py`. The fake reader "reads" a crop by looking up which
label it was cut for, so these tests exercise the real planning, cropping, ink check, sealing,
naming and mapping. No network, no client drawing, no client value.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.config import Settings
from evidence.coordinates import PageTransform
from extraction.form_reader.bedrock import AttemptUsage
from extraction.geometry.rows import MEASURED_SETTINGS, Box, PageRows
from extraction.ink import read_page_ink
from extraction.rasterise import render_page
from extraction.rows import page_rows_and_ink
from extraction.slot_reader.kinds import PieceKind, propose_kind
from extraction.slot_reader.mapping import (
    CABINET_FIELD,
    FILLER_FIELD,
    OVERALL_FIELD,
    PIECE_FIELD,
)
from extraction.slot_reader.runs import E2_CROP_SETTINGS, plan_slots
from extraction.slot_reader.seal import LabelState
from tests.extraction.slot_reader import sheets
from workflow.form_reader import FormReaderRuntime
from workflow.slot_reader import (
    FRACTION_BAR_ENV,
    PageSlotResult,
    SlotPage,
    SlotReaderRuntime,
    _crop_png,
    _pixels,
    configured_slot_reader,
    read_slot_pages,
)

QWEN = "qwen.qwen3-vl-235b-a22b"
KIMI = "us.moonshotai.kimi-k3"
OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
DPI = 150
FRACTION_ENV = {
    "GV_READER_FRACTION_BAR_THICKNESS_MAX_PT": "0.3",
    "GV_READER_FRACTION_BAR_LENGTH_MIN_PT": "1",
    "GV_READER_FRACTION_REACH_PT": "3",
    "GV_READER_FRACTION_GLYPH_MIN_PT": "1",
    "GV_READER_FRACTION_GLYPH_MAX_PT": "12",
    "GV_READER_FRACTION_PROPORTION_MAX": "2.5",
    "GV_READER_FRACTION_CHARACTER_GAP_PT": "4",
    "GV_READER_FRACTION_TURNED_ASPECT_MIN": "1.1",
}


class Rates:
    def rate_for(self, model_id: str) -> object | None:
        return object()


UNSURE_WALLS = {"left": "unsure", "right": "unsure", "behind": "unsure", "view": "elevation"}
BOTH_WALLS = {"left": "yes", "right": "yes", "behind": "yes", "view": "elevation"}


class FakeReaders:
    """Answers each crop with `read(model, crop_png)` and each row's wall question with
    `walls(model)`; records every request."""

    def __init__(
        self,
        read: Callable[[str, bytes], str],
        walls: Callable[[str], Mapping[str, str]] = lambda _model: UNSURE_WALLS,
        row: Callable[[str], Mapping[str, object]] = lambda _model: {
            "row": 1,
            "why": "the candidate follows the front elevation",
        },
        counter_break: Callable[[str], Mapping[str, object]] = lambda _model: {
            "contains_tall_appliance": False,
            "why": "no tall unit is drawn in the marked span",
        },
    ) -> None:
        self.read = read
        self.walls = walls
        self.row = row
        self.counter_break = counter_break
        self.requests: list[tuple[str, bytes]] = []
        self.wall_requests: list[tuple[str, bytes, bytes]] = []
        self.counter_break_requests: list[tuple[str, bytes, bytes]] = []
        self.row_requests: list[tuple[str, bytes]] = []
        self.span_pictures: list[tuple[str, tuple[bytes, ...]]] = []
        self.span_texts: list[tuple[str, tuple[str, ...]]] = []
        self.lock = threading.Lock()

    def for_current_thread(self) -> FakeReaders:
        return self

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        return {"stopReason": "end_turn", **self._answer(**kwargs)}

    def _answer(self, **kwargs: Any) -> Mapping[str, Any]:
        model = kwargs["modelId"]
        content = kwargs["messages"][0]["content"]
        pictures = [part["image"]["source"]["bytes"] for part in content if "image" in part]
        is_row_question = any("Numbered coloured boxes" in part.get("text", "") for part in content)
        if is_row_question:
            with self.lock:
                self.row_requests.append((model, pictures[0]))
            return {
                "output": {"message": {"content": [{"text": json.dumps(dict(self.row(model)))}]}},
                "usage": {"inputTokens": 20, "outputTokens": 9},
            }
        is_counter_break_question = any(
            "hold-only safety question" in part.get("text", "") for part in content
        )
        if is_counter_break_question:
            with self.lock:
                self.counter_break_requests.append((model, pictures[0], pictures[1]))
            return {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": json.dumps(
                                    {"stone_ends": "unsure"} | dict(self.counter_break(model))
                                )
                            }
                        ]
                    }
                },
                "usage": {"inputTokens": 20, "outputTokens": 9},
            }
        is_wall_question = any("wall" in part.get("text", "").lower() for part in content)
        if is_wall_question:
            with self.lock:
                self.wall_requests.append((model, pictures[0], pictures[1]))
            return {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": json.dumps(
                                    {
                                        "left_evidence": "",
                                        "right_evidence": "",
                                        "behind_evidence": "",
                                    }
                                    | dict(self.walls(model))
                                )
                            }
                        ]
                    }
                },
                "usage": {"inputTokens": 20, "outputTokens": 9},
            }
        # A sideways span's upright copy comes third (#1051); the close-up is always second.
        png = pictures[1] if len(pictures) == 3 else pictures[-1]
        with self.lock:
            self.requests.append((model, png))
            self.span_pictures.append((model, tuple(pictures)))
            self.span_texts.append(
                (model, tuple(part["text"] for part in content if "text" in part))
            )
        text = self.read(model, png)
        payload = {
            "text": text,
            "stacked": False,
            "combined": False,
            "readable": True,
            "no_dimension": not text,
            "belongs": bool(text),
        }
        return {
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }


def runtime(
    clients: FakeReaders, *, allow_stacked: bool = False, claude_row_reader: bool = False
) -> SlotReaderRuntime:
    from workflow.slot_reader import fraction_bar_from_environment

    form = FormReaderRuntime(
        reader_ids=(OPUS, SONNET) if claude_row_reader else (KIMI, QWEN),
        clients=clients,
        rates=Rates(),  # type: ignore[arg-type]
        calls_per_minute={
            (OPUS if claude_row_reader else KIMI): 6000,
            (SONNET if claude_row_reader else QWEN): 6000,
        },
        max_concurrent_calls=1 if claude_row_reader else 4,
        max_tokens=400,
        max_throttle_retries=0,
        retry_backoff_seconds=0.001,
        prompt=None,  # type: ignore[arg-type]
    )
    return SlotReaderRuntime(
        form=form,
        crop_settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
        fraction_bar=fraction_bar_from_environment(FRACTION_ENV),
        allow_stacked=allow_stacked,
        claude_row_reader=claude_row_reader,
    )


def slot_page(data: bytes) -> SlotPage:
    return SlotPage(
        page_index=0,
        page_id=uuid4(),
        document_version_id=uuid4(),
        rendered=render_page(
            data,
            0,
            document_version_id=uuid4(),
            page_content_hash="1" * 64,
            dpi=DPI,
            maximum_pixels=10_000_000,
            vendor_only=True,
        ),
        rows=page_rows_and_ink(data, 0, dpi=DPI, settings=MEASURED_SETTINGS),
        ink=read_page_ink(data, 0, dpi=DPI),
    )


def crops_to_texts(page: SlotPage, texts: Mapping[int | None, str]) -> dict[bytes, str]:
    """Which crop was cut for which slot's dimension label: what a perfect reader would say."""
    plan = plan_slots(
        page.rows.candidates.rows,
        page.rows.ink,
        settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
    )
    found: dict[bytes, str] = {}
    for owner in (*plan.slots, *((plan.overall,) if plan.overall is not None else ())):
        for label in owner.labels:
            png = _crop_png(page.rendered, _pixels(page.rows, label.crop, page.rendered))
            if label.text is None or any(ch.isdigit() for ch in label.text):
                found[png] = texts.get(owner.index, "")
    return found


def claude_crops_to_texts(page: SlotPage, texts: Mapping[int | None, str]) -> dict[bytes, str]:
    from workflow.slot_reader import _claude_span_plan

    plan = plan_slots(
        page.rows.candidates.rows,
        page.rows.ink,
        settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
    )
    span_plan = _claude_span_plan(page, plan)
    return {
        _crop_png(
            page.rendered, _pixels(page.rows, owner.labels[0].crop, page.rendered)
        ): texts.get(owner.index, "")
        for owner in (*span_plan.slots, *((span_plan.overall,) if span_plan.overall else ()))
    }


def read(page: SlotPage, readers: FakeReaders, **options: Any) -> PageSlotResult:
    attempts: list[AttemptUsage] = []
    (result,) = read_slot_pages(
        [page], runtime=runtime(readers, **options), record_attempt=attempts.append
    )
    assert len(attempts) == (
        len(readers.requests)
        + len(readers.wall_requests)
        + len(readers.row_requests)
        + len(readers.counter_break_requests)
    )
    return result


def test_claude_packet_mode_stores_both_exact_images_and_page_transform() -> None:
    class MemoryStore:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def put(self, key: str, data: BytesIO, *, content_type: str) -> SimpleNamespace:
            assert content_type == "image/png"
            content = data.read()
            self.objects[key] = content
            return SimpleNamespace(sha256=hashlib.sha256(content).hexdigest())

    page = replace(
        slot_page(named_sheet()),
        transform=PageTransform(
            dpi=DPI,
            rotation=0,
            media_box=(Decimal(0), Decimal(0), Decimal(612), Decimal(792)),
            crop_box=(Decimal(0), Decimal(0), Decimal(612), Decimal(792)),
        ),
    )
    lookup = crops_to_texts(page, TEXTS)
    readers = FakeReaders(lambda _model, png: lookup.get(png, '2"'))
    attempts: list[AttemptUsage] = []
    store = MemoryStore()

    results = read_slot_pages(
        [page],
        runtime=replace(runtime(readers), question_packets=True),
        record_attempt=attempts.append,
        store=store,
    )

    assert results and attempts
    result = results[0]
    expected_candidate_ids = {str(value) for value in result.owner_candidate_ids.values()}
    if result.wall_candidate_id is not None:
        expected_candidate_ids.add(str(result.wall_candidate_id))
    for attempt in attempts:
        packet = attempt.question_packet
        assert packet is not None
        assert packet["candidate_ids"]
        assert set(packet["candidate_ids"]) <= expected_candidate_ids
        assert packet["page_transform"] == {
            "dpi": DPI,
            "rotation": 0,
            "media_box": ["0", "0", "612", "792"],
            "crop_box": ["0", "0", "612", "792"],
        }
        images = packet["images"]
        assert isinstance(images, dict)
        for image_info in images.values():
            assert isinstance(image_info, dict)
            key = image_info["storage_key"]
            digest = image_info["sha256"]
            assert isinstance(key, str) and isinstance(digest, str)
            assert hashlib.sha256(store.objects[key]).hexdigest() == digest


def test_claude_opus_selects_only_a_numbered_code_candidate_before_slot_reading() -> None:
    page = slot_page(named_sheet())
    first = page.rows.candidates.rows.candidates[0]
    second = replace(first, rank=2)
    page = replace(
        page,
        rows=replace(
            page.rows,
            candidates=replace(
                page.rows.candidates,
                rows=PageRows(candidates=(first, second), rejected=()),
            ),
        ),
    )
    text_by_crop = claude_crops_to_texts(page, TEXTS)
    readers = FakeReaders(
        lambda _model, png: text_by_crop.get(png, '2"'),
        row=lambda _model: {"row": 2, "why": "box 2 follows the front elevation"},
    )
    form = replace(
        runtime(readers).form,
        reader_ids=(OPUS, SONNET),
        calls_per_minute={OPUS: 6000, SONNET: 6000},
        max_concurrent_calls=1,
    )
    configured = replace(runtime(readers), form=form, claude_row_reader=True)
    attempts: list[AttemptUsage] = []

    (result,) = read_slot_pages([page], runtime=configured, record_attempt=attempts.append)

    assert result.plan.row is second
    assert result.row_choice_number == 2
    assert (
        result.row_choice is not None
        and result.row_choice.why == "box 2 follows the front elevation"
    )
    assert result.row_candidate_ids and len(result.row_candidate_ids) == 2
    asked = sorted(model for model, _image in readers.row_requests)
    assert asked == sorted([OPUS, SONNET]), "both readers are asked the row question"
    assert len({image for _model, image in readers.row_requests}) == 1, "with the same picture"
    row_attempt = next(attempt for attempt in attempts if attempt.prompt_id == "slot-row-choice-v2")
    assert row_attempt.raw_response_text is not None
    assert row_attempt.question_packet is None
    assert result.mapping.proposals, "the selected non-rank-one candidate supplies the slot plan"


def test_claude_selected_row_without_overall_still_reads_and_offers_its_pieces() -> None:
    page = slot_page(named_sheet())
    candidate = replace(page.rows.candidates.rows.candidates[0], overall=None)
    page = replace(
        page,
        rows=replace(
            page.rows,
            candidates=replace(
                page.rows.candidates,
                rows=PageRows(candidates=(candidate,), rejected=()),
            ),
        ),
    )
    span_lookup = claude_crops_to_texts(page, TEXTS)
    readers = FakeReaders(lambda _model, png: span_lookup.get(png, '2"'))
    base = runtime(readers)
    configured = replace(
        base,
        form=replace(
            base.form,
            reader_ids=(OPUS, SONNET),
            calls_per_minute={OPUS: 6000, SONNET: 6000},
        ),
        claude_row_reader=True,
    )

    (result,) = read_slot_pages([page], runtime=configured, record_attempt=lambda _attempt: None)

    assert result.plan.row is candidate
    assert result.plan.ambiguity is None
    assert result.overall is None
    assert all(slot.outcome.state is LabelState.SEALED for slot in result.slots)
    assert result.mapping.proposals
    assert all(proposal.field_key == PIECE_FIELD for proposal in result.mapping.proposals)


def test_claude_asks_every_code_span_even_when_label_detection_is_empty_or_duplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workflow.slot_reader as slot_workflow

    page = slot_page(named_sheet())
    source = slot_workflow.plan_slots

    def sparse_plan(*args: Any, **kwargs: Any) -> Any:
        plan = source(*args, **kwargs)
        slots = list(plan.slots)
        slots[0] = replace(slots[0], labels=())
        if len(slots) > 1 and slots[1].labels:
            slots[1] = replace(slots[1], labels=slots[1].labels * 2)
        return replace(plan, slots=tuple(slots))

    monkeypatch.setattr(slot_workflow, "plan_slots", sparse_plan)
    readers = FakeReaders(lambda _model, _png: '2"')
    base = runtime(readers)
    configured = replace(
        base,
        form=replace(
            base.form,
            reader_ids=(OPUS, SONNET),
            calls_per_minute={OPUS: 6000, SONNET: 6000},
        ),
        claude_row_reader=True,
    )
    attempts: list[AttemptUsage] = []

    (result,) = read_slot_pages([page], runtime=configured, record_attempt=attempts.append)

    owner_count = len(result.plan.slots) + (result.plan.overall is not None)
    label_attempts = [attempt for attempt in attempts if attempt.prompt_id == "claude-slot-span-v3"]
    assert len(readers.requests) == owner_count * 2
    assert len(label_attempts) == owner_count * 2
    assert all(len(owner.labels) == 1 for owner in result.plan.slots)
    assert all(owner.labels[0].box == owner.band for owner in result.plan.slots)
    assert all(
        owner.labels[0].crop.x0 <= owner.band.x0 and owner.labels[0].crop.x1 >= owner.band.x1
        for owner in result.plan.slots
    )


def test_claude_zero_row_choice_holds_the_page_for_the_reviewer() -> None:
    page = slot_page(named_sheet())
    readers = FakeReaders(
        lambda _model, _png: '2"',
        row=lambda _model: {"row": 0, "why": "none of the candidate rows is the countertop"},
    )
    form = replace(
        runtime(readers).form,
        reader_ids=(OPUS, SONNET),
        calls_per_minute={OPUS: 6000, SONNET: 6000},
        max_concurrent_calls=1,
    )
    configured = replace(runtime(readers), form=form, claude_row_reader=True)

    (result,) = read_slot_pages([page], runtime=configured, record_attempt=lambda _attempt: None)

    assert result.plan.row is None
    assert (
        result.plan.ambiguity
        == "the row reader selected no candidate; the reviewer must choose the row"
    )
    assert result.row_choice_number == 0
    assert result.row_choice is not None
    assert result.slots == () and result.overall is None
    assert not result.mapping.proposals


def test_row_selection_packet_binds_the_numbered_page_and_ordered_candidates() -> None:
    class MemoryStore:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def put(self, key: str, data: BytesIO, *, content_type: str) -> SimpleNamespace:
            assert content_type == "image/png"
            content = data.read()
            self.objects[key] = content
            return SimpleNamespace(sha256=hashlib.sha256(content).hexdigest())

    page = slot_page(named_sheet())
    page = replace(
        page,
        transform=PageTransform(
            dpi=DPI,
            rotation=0,
            media_box=(Decimal(0), Decimal(0), Decimal(612), Decimal(792)),
            crop_box=(Decimal(0), Decimal(0), Decimal(612), Decimal(792)),
        ),
    )
    store = MemoryStore()
    readers = FakeReaders(lambda _model, _png: '2"')
    form = replace(
        runtime(readers).form,
        reader_ids=(OPUS, SONNET),
        calls_per_minute={OPUS: 6000, SONNET: 6000},
        max_concurrent_calls=1,
    )
    configured = replace(runtime(readers), form=form, claude_row_reader=True, question_packets=True)
    attempts: list[AttemptUsage] = []

    (result,) = read_slot_pages(
        [page], runtime=configured, record_attempt=attempts.append, store=store
    )

    row_attempt = next(attempt for attempt in attempts if attempt.prompt_id == "slot-row-choice-v2")
    packet = row_attempt.question_packet
    assert packet is not None
    assert packet["candidate_ids"] == [str(value) for value in result.row_candidate_ids]
    assert packet["candidate_count"] == len(result.row_candidate_ids)
    image = packet["images"]["numbered_vendor_view"]
    assert hashlib.sha256(store.objects[image["storage_key"]]).hexdigest() == image["sha256"]
    assert result.row_choice_png == store.objects[image["storage_key"]]


#: Drawn to scale, as a shop drawing is: the sheet's slots are 50, 100 and 50 points long, so at
#: 0.24" a point they print 12", 24" and 12", and the 200-point overall 48".
PIECES = ('12"', '24"', '12"')
OVERALL = '48"'
TEXTS: dict[int | None, str] = {0: '12"', 1: '24"', 2: '12"', None: '48"'}
FOUR_PIECE_TEXTS: dict[int | None, str] = {
    0: '12"',
    1: '12"',
    2: '12"',
    3: '12"',
    None: '48"',
}


def text_labels() -> bytes:
    return sheets.text_labels(PIECES, OVERALL)


def named_sheet(extra: bytes = b"") -> bytes:
    """Text labels with the vendor's words beside them: `Filler`, a cabinet tag, `Filler`."""
    drawing = text_labels().replace(
        b'1 0 0 1 318.00 604.00 Tm (12") Tj', b'1 0 0 1 333.00 604.00 Tm (12") Tj'
    )
    drawing += sheets.text(190, sheets.CHAIN_Y + 4, "Filler")
    drawing += sheets.text(265, sheets.CHAIN_Y + 4, "B24")
    drawing += sheets.text(302, sheets.CHAIN_Y + 4, "Filler")
    return sheets.sheet(drawing + extra)


def four_piece_sheet(*, covered_slot: int | None = None) -> bytes:
    """A calibrated four-piece chain lets three sealed labels witness one held label."""
    centers = (175.0, 225.0, 275.0, 325.0)
    drawing = b"".join(sheets.text(x - 7, sheets.CHAIN_Y + 4, '12"') for x in centers)
    drawing += sheets.text(243, sheets.OVERALL_Y + 4, '48"')
    from tests.extraction.test_rows import _slash

    marks = b"".join(_slash(x, sheets.CHAIN_Y) for x in (150, 200, 250, 300, 350))
    marks += _slash(150, sheets.OVERALL_Y) + _slash(350, sheets.OVERALL_Y)
    extra = (
        b""
        if covered_slot is None
        else sheets.yellow_box(centers[covered_slot] - 7, sheets.CHAIN_Y + 3, 13, 7)
    )
    return sheets.sheet(drawing + extra, marks)


def test_text_labels_seal_on_the_file_and_one_reader_and_unnamed_pieces_still_fill_the_width() -> (
    None
):
    page = slot_page(sheets.sheet(text_labels()))
    lookup = crops_to_texts(page, TEXTS)
    readers = FakeReaders(lambda _model, png: lookup[png])

    result = read(page, readers)

    assert {model for model, _ in readers.requests} == {QWEN}, "a text label needs one reader"
    assert result.overall is not None and result.overall.outcome.state is LabelState.SEALED
    assert all(slot.outcome.state is LabelState.SEALED for slot in result.slots)
    # #992: a fully sealed row offers every piece, whatever its kind; unnamed pieces still keep
    # the cabinet and filler fields back.
    assert [(p.field_key, p.position, p.slot_index) for p in result.mapping.proposals] == [
        (OVERALL_FIELD, 0, None),
        (PIECE_FIELD, 0, 0),
        (PIECE_FIELD, 1, 1),
        (PIECE_FIELD, 2, 2),
    ]


def test_a_named_sealed_chain_fills_the_form_left_to_right() -> None:
    page = slot_page(named_sheet())
    lookup = crops_to_texts(page, TEXTS)
    result = read(page, FakeReaders(lambda _model, png: lookup.get(png, '12"')))

    assert [slot.kind.kind.value for slot in result.slots if slot.kind] == [
        "filler",
        "cabinet",
        "filler",
    ]
    # Piece widths only: CT-WIDTH-001 (#993) sends piece widths beside a cabinet or filler list to
    # review, so a named row is offered whole, not twice.
    assert [(p.field_key, p.position, p.slot_index) for p in result.mapping.proposals] == [
        (OVERALL_FIELD, 0, None),
        (PIECE_FIELD, 0, 0),
        (PIECE_FIELD, 1, 1),
        (PIECE_FIELD, 2, 2),
    ]
    keys = {p.field_key for p in result.mapping.proposals}
    assert CABINET_FIELD not in keys and FILLER_FIELD not in keys


def test_a_reader_that_differs_from_the_file_holds_the_piece_and_the_chain() -> None:
    page = slot_page(four_piece_sheet())
    indexed_crops = claude_crops_to_texts(
        page, {0: "slot-0", 1: "slot-1", 2: "slot-2", 3: "slot-3", None: "overall"}
    )
    middle_crop = [png for png, position in indexed_crops.items() if position == "slot-1"][-1]
    lookup = {
        png: FOUR_PIECE_TEXTS[
            None if position == "overall" else int(position.removeprefix("slot-"))
        ]
        for png, position in indexed_crops.items()
    }
    result = read(
        page,
        FakeReaders(
            lambda model, png: '13"' if model == OPUS and png == middle_crop else lookup[png]
        ),
        claude_row_reader=True,
    )

    assert result.slots[1].outcome.reason_code == "readers-differ"
    assert [(proposal.position, proposal.slot_index) for proposal in result.mapping.proposals] == [
        (0, 0),
        (2, 2),
        (3, 3),
    ]


def test_a_label_under_the_reviewers_yellow_box_is_never_read_or_sealed() -> None:
    """Yellow painted over the middle label, as the reviewer does: covered, no value, no call."""
    page = slot_page(named_sheet(sheets.yellow_box(241, sheets.CHAIN_Y + 3, 13, 7)))
    lookup = crops_to_texts(page, TEXTS)
    readers = FakeReaders(lambda _model, png: lookup.get(png, '99"'))

    result = read(page, readers)

    middle = result.slots[1]
    assert middle.outcome.state is LabelState.REVIEW
    assert middle.outcome.reason is not None
    assert middle.outcome.reason.startswith("covered by reviewer markup")
    assert middle.outcome.value is None
    covered_crop = _crop_png(page.rendered, middle.labels[0].crop_px)
    assert covered_crop not in {
        png for _model, png in readers.requests
    }, "the covered label itself costs no call"
    assert result.mapping.proposals == ()


def test_glyph_labels_need_two_makers_to_agree() -> None:
    page = slot_page(sheets.sheet(sheets.glyph_labels()))
    lookup = crops_to_texts(page, TEXTS)

    agreed = read(page, FakeReaders(lambda _model, png: lookup[png]))
    assert all(slot.outcome.state is LabelState.SEALED for slot in agreed.slots)
    assert agreed.overall is not None and agreed.overall.outcome.state is LabelState.SEALED

    def kimi_misreads(model: str, png: bytes) -> str:
        return '17"' if model == KIMI and lookup[png] == '12"' else lookup[png]

    differ = read(page, FakeReaders(kimi_misreads))
    assert differ.slots[0].outcome.reason_code == "readers-differ"
    assert differ.slots[0].outcome.value is None


def test_a_reader_whose_answer_stays_malformed_abstains_and_the_reading_waits() -> None:
    """Kimi's answer is not JSON, twice: it abstains on every crop; Qwen alone never seals."""
    page = slot_page(sheets.sheet(sheets.glyph_labels()))
    lookup = crops_to_texts(page, TEXTS)
    readers = FakeReaders(lambda _model, png: lookup[png])
    answer_properly = readers.converse

    def converse(**kwargs: Any) -> Mapping[str, Any]:
        if kwargs["modelId"] == KIMI:
            return {"output": {"message": {"content": [{"text": "not json"}]}}}
        return answer_properly(**kwargs)

    readers.converse = converse  # type: ignore[method-assign]
    (result,) = read_slot_pages([page], runtime=runtime(readers), record_attempt=lambda _a: None)

    assert all(slot.outcome.reason_code == "one-reader-missing" for slot in result.slots)
    assert result.mapping.proposals == ()


def test_the_slot_reader_is_off_unless_switched_on(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(database_url="postgresql+psycopg://x@localhost/x")
    assert settings.slot_reader_enabled is False
    assert settings.slot_reader_stacked_agreement is False
    assert configured_slot_reader(settings, None) is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-router-key")
    with_key = Settings(database_url="postgresql+psycopg://x@localhost/x")
    assert "private-test-key" not in repr(with_key)
    assert "private-router-key" not in repr(with_key)


def test_switching_it_on_needs_the_form_reader_and_the_fraction_bar_lengths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GV_FORM_READER_ENABLED"):
        Settings(database_url="postgresql+psycopg://x@localhost/x", slot_reader_enabled=True)
    with pytest.raises(ValueError, match="GV_SLOT_READER_ENABLED"):
        Settings(database_url="postgresql+psycopg://x@localhost/x", claude_reader_enabled=True)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        Settings(
            _env_file=None,  # a developer's own .env may hold the key
            database_url="postgresql+psycopg://x@localhost/x",
            slot_reader_enabled=True,
            claude_reader_enabled=True,
        )

    class On:
        slot_reader_enabled = True
        slot_reader_stacked_agreement = False
        claude_reader_enabled = False

    with pytest.raises(ValueError, match="GV_FORM_READER_ENABLED"):
        configured_slot_reader(On(), None, environ=FRACTION_ENV)
    form = runtime(FakeReaders(lambda _m, _p: "")).form
    with pytest.raises(ValueError, match="fraction-bar"):
        configured_slot_reader(On(), form, environ={})
    configured = configured_slot_reader(On(), form, environ=FRACTION_ENV)
    assert configured is not None and configured.allow_stacked is False
    assert configured.question_packets is False
    claude_settings = type(
        "ClaudeOn",
        (On,),
        {
            "claude_reader_enabled": True,
            "openrouter_api_key": SecretStr("private-test-key"),
            "claude_reader_model_rpm": {
                "anthropic.claude-opus-5-5": 60,
                "anthropic.claude-sonnet-5-5": 60,
            },
            "claude_reader_timeout_seconds": 180,
        },
    )()
    claude_enabled = configured_slot_reader(
        claude_settings,
        form,
        environ=FRACTION_ENV,
    )
    assert claude_enabled is not None and claude_enabled.question_packets is True
    assert claude_enabled.allow_stacked is True
    assert claude_enabled.spend_cap_usd == Decimal("2.50")
    assert claude_enabled.form.max_concurrent_calls == 8
    assert claude_enabled.form.reader_ids == (
        "anthropic.claude-opus-5-5",
        "anthropic.claude-sonnet-5-5",
    )
    assert claude_enabled.form.max_tokens == 3000
    assert set(FRACTION_BAR_ENV) == set(FRACTION_ENV)


def test_the_run_identity_fits_its_column() -> None:
    """`extraction_runs.config_hash` holds 200 characters; the full settings are many times that."""
    configured = runtime(FakeReaders(lambda _m, _p: ""))
    assert len(f"dpi=300;{configured.config_hash}") <= 200
    assert len(configured.config_detail) > 200


# ---------------------------------------------------------------------------------------------
# The admin's three rules (#992): drawn-length veto, labels from agreed text, walls by agreement
# ---------------------------------------------------------------------------------------------


def glyph_page() -> SlotPage:
    return slot_page(sheets.sheet(sheets.glyph_labels()))


def agreed(page: SlotPage, texts: Mapping[int | None, str], **options: Any) -> PageSlotResult:
    """Both readers print `texts` for each label: whatever they agree on, right or wrong."""
    lookup = crops_to_texts(page, texts)
    return read(page, FakeReaders(lambda _model, png: lookup[png], **options))


def test_a_misread_both_readers_agree_on_is_rejected_by_the_drawn_length() -> None:
    page = glyph_page()
    result = agreed(page, TEXTS | {2: '120"'})

    misread = result.slots[2]
    assert misread.outcome.state is LabelState.REVIEW
    assert misread.outcome.reason == "doesn't match the drawn length"
    assert misread.outcome.value is None
    label = misread.labels[misread.outcome.label_index or 0].outcome
    assert label.suggestion is not None and label.suggestion.exact == 120, "kept, never changed"
    assert 2 in result.vetoed
    assert result.mapping.proposals == (), "a vetoed piece holds the whole row"


def test_correct_readings_drawn_to_scale_are_never_vetoed() -> None:
    result = agreed(glyph_page(), TEXTS)
    assert result.vetoed == ()
    assert [p.field_key for p in result.mapping.proposals].count(PIECE_FIELD) == 3


def test_an_agreed_sum_is_one_piece_and_fills_the_width_in_order() -> None:
    result = agreed(glyph_page(), TEXTS | {0: '10"+2" Filler'})

    first = result.slots[0]
    assert first.outcome.state is LabelState.SEALED
    assert first.outcome.value is not None and first.outcome.value.exact == 12
    assert first.kind is not None and first.kind.kind.value == "filler"
    pieces = [p for p in result.mapping.proposals if p.field_key == PIECE_FIELD]
    assert [(p.position, p.slot_index) for p in pieces] == [(0, 0), (1, 1), (2, 2)]


def test_a_text_label_with_a_sum_is_shown_to_its_reader_and_seals_on_the_same_text() -> None:
    page = slot_page(sheets.sheet(sheets.text_labels(('10"+2"', '24"', '12"'), OVERALL)))
    lookup = crops_to_texts(page, TEXTS | {0: '10"+2"'})
    readers = FakeReaders(lambda _model, png: lookup[png])

    result = read(page, readers)

    assert len(readers.requests) == 4, "the summed label is read too"
    assert result.slots[0].outcome.state is LabelState.SEALED
    assert result.slots[0].outcome.value is not None
    assert result.slots[0].outcome.value.exact == 12


def test_an_agreed_equal_share_count_is_one_entry_of_its_total() -> None:
    result = agreed(glyph_page(), TEXTS | {1: '24"(2EQ)'})
    middle = result.slots[1]
    assert middle.outcome.state is LabelState.SEALED
    assert middle.outcome.value is not None and middle.outcome.value.exact == 24
    assert PIECE_FIELD in {p.field_key for p in result.mapping.proposals}


@pytest.mark.parametrize(
    ("texts", "code", "reason"),
    [
        (
            {None: '48" (INCLUDING FIELD CUT)'},
            "field-cut-included",
            "width already includes the field cut",
        ),
        ({1: '24" VIF'}, "vif", "VIF: provisional, verify in field"),
    ],
)
def test_field_cut_or_vif_anywhere_holds_the_whole_countertop(
    texts: Mapping[int | None, str], code: str, reason: str
) -> None:
    result = agreed(glyph_page(), TEXTS | texts)

    assert result.row_hold is not None and result.row_hold.code == code
    assert result.mapping.proposals == ()
    held = dict(result.mapping.held)
    assert held, "every sealed reading says why it waits"
    assert all(why == reason for why in held.values())


def test_one_reader_seeing_vif_is_enough_to_hold_the_row() -> None:
    page = glyph_page()
    lookup = crops_to_texts(page, TEXTS)

    def kimi_sees_vif(model: str, png: bytes) -> str:
        text = lookup[png]
        return f"{text} VIF" if model == KIMI and text == '24"' else text

    result = read(page, FakeReaders(kimi_sees_vif))
    assert result.row_hold is not None and result.row_hold.code == "vif"
    assert result.mapping.proposals == ()


@pytest.mark.parametrize(
    "word",
    [
        "REF",
        "refrigerator",
        "FRIDGE",
        "RANGE",
        "STOVE",
        "OVEN",
        "W/D",
        "WASHER",
        "DRYER",
        "TALL",
        "PANTRY",
    ],
)
def test_vendor_counter_break_word_inside_a_slot_span_holds_the_whole_row(word: str) -> None:
    page = slot_page(sheets.sheet(text_labels() + sheets.text(230, sheets.CHAIN_Y - 40, word)))
    lookup = crops_to_texts(page, TEXTS)
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.row_hold is not None and result.row_hold.code == "counter-break"
    assert result.row_hold.reason == (
        "this row includes a tall appliance or range bay; it may be the wall-to-wall line, not the countertop"
    )
    assert result.mapping.proposals == ()
    assert result.mapping.held


def test_claude_line_question_holds_a_line_drawn_tall_appliance() -> None:
    # The synthetic vendor appliance is only a rectangle made from strokes; there is no text token
    # for the existing ink/text-layer guard to match.
    appliance_lines = b"0.3 w 205 535 m 245 535 l 245 590 l 205 590 l h S\n"
    page = slot_page(sheets.sheet(sheets.text_labels() + appliance_lines))
    lookup = claude_crops_to_texts(page, TEXTS)
    readers = FakeReaders(
        lambda _model, png: lookup[png],
        counter_break=lambda model: {
            "contains_tall_appliance": model == OPUS,
            "why": "a tall outlined unit crosses the marked span",
        },
    )
    base = runtime(readers)
    configured = replace(
        base,
        form=replace(
            base.form,
            reader_ids=(OPUS, SONNET),
            calls_per_minute={OPUS: 6000, SONNET: 6000},
        ),
        claude_row_reader=True,
    )
    attempts: list[AttemptUsage] = []

    (result,) = read_slot_pages([page], runtime=configured, record_attempt=attempts.append)

    assert result.row_hold is not None and result.row_hold.code == "counter-break"
    assert result.mapping.proposals == ()
    assert {model for model, _row, _view in readers.counter_break_requests} == {OPUS, SONNET}
    assert len(readers.counter_break_requests) == 2
    assert sum(attempt.prompt_id == "claude-counter-break-v2" for attempt in attempts) == 2


def test_claude_line_question_two_no_answers_leave_the_row_unchanged() -> None:
    page = slot_page(sheets.sheet(text_labels()))
    lookup = claude_crops_to_texts(page, TEXTS)
    readers = FakeReaders(lambda _model, png: lookup[png])
    base = runtime(readers)
    configured = replace(
        base,
        form=replace(
            base.form,
            reader_ids=(OPUS, SONNET),
            calls_per_minute={OPUS: 6000, SONNET: 6000},
        ),
        claude_row_reader=True,
    )

    (result,) = read_slot_pages([page], runtime=configured, record_attempt=lambda _attempt: None)

    assert result.row_hold is None
    assert result.mapping.proposals


def test_counter_break_word_in_a_slot_label_holds_the_whole_row() -> None:
    page = slot_page(sheets.sheet(sheets.text_labels(('12"', '24" RANGE', '12"'), OVERALL)))
    lookup = crops_to_texts(page, TEXTS | {1: '24" RANGE'})
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.row_hold is not None and result.row_hold.code == "counter-break"
    assert result.mapping.proposals == ()


@pytest.mark.parametrize(
    "drawing", [sheets.text_labels(PIECES, OVERALL), sheets.glyph_labels()], ids=["text", "glyph"]
)
def test_labels_on_a_page_held_before_reading_carry_the_hold_not_one_reader(
    drawing: bytes,
) -> None:
    """#1114: a vendor counter-break word holds the row before any label is read; its labels say
    the row waits, never "only one reader", and they stay held."""
    page = slot_page(sheets.sheet(drawing + sheets.text(230, sheets.CHAIN_Y - 40, "OVEN")))
    readers = FakeReaders(lambda _model, _png: '99"')

    result = read(page, readers, claude_row_reader=True)

    assert readers.requests == [], "a held page's labels cost no call"
    assert result.row_hold is not None and result.row_hold.code == "counter-break"
    assert result.mapping.proposals == ()
    owners = [*result.slots, *((result.overall,) if result.overall is not None else ())]
    labelled = [owner for owner in owners if owner.labels]
    assert labelled
    for owner in labelled:
        assert owner.outcome.state is LabelState.REVIEW and owner.outcome.value is None
        assert owner.outcome.reason_code == "not-asked"
        assert owner.outcome.reason == (
            f"the readers were not asked; this row waits: {result.row_hold.reason}"
        )


def test_reader_only_counter_break_word_does_not_count_as_vendor_ink() -> None:
    page = slot_page(sheets.sheet(text_labels()))
    lookup = crops_to_texts(page, TEXTS)

    def reader_text(model: str, png: bytes) -> str:
        text = lookup[png]
        return f"{text} RANGE" if model == KIMI and text == '24"' else text

    result = read(page, FakeReaders(reader_text))

    assert result.row_hold is None


def test_a_gv_counter_break_word_does_not_hold_an_otherwise_sealed_vendor_row() -> None:
    page = slot_page(
        sheets.sheet(
            text_labels() + sheets.text(230, sheets.CHAIN_Y - 40, "RANGE", colour="1 0 0 rg")
        )
    )
    lookup = crops_to_texts(page, TEXTS)
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.row_hold is None
    assert len(result.mapping.proposals) == 4


def test_a_substring_is_not_an_appliance_word() -> None:
    page = slot_page(sheets.sheet(text_labels() + sheets.text(230, sheets.CHAIN_Y - 40, "NICE")))
    lookup = crops_to_texts(page, TEXTS)
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.row_hold is None
    assert len(result.mapping.proposals) == 4


@pytest.mark.parametrize("word", ["DW", "DISHWASHER", "ICE", "WINE", "MW", "MICROWAVE", "COOKTOP"])
def test_an_undercounter_appliance_word_does_not_hold_the_row(word: str) -> None:
    page = slot_page(sheets.sheet(text_labels() + sheets.text(230, sheets.CHAIN_Y - 40, word)))
    lookup = crops_to_texts(page, TEXTS)
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.row_hold is None
    assert len(result.mapping.proposals) == 4


@pytest.mark.parametrize(
    "words",
    [
        ("Undercounter", "Refrigerator"),
        ("UNDER-COUNTER", "FRIDGE"),
        ("UNDER", "COUNTER", "REF"),
        ("U/C", "REF"),
        ("UC", "REF"),
        ("Microwave", "Oven"),
        ("MW", "OVEN"),
    ],
)
def test_a_safe_phrase_does_not_hold_the_row(words: tuple[str, ...]) -> None:
    x = 220
    drawing = text_labels()
    for word in words:
        drawing += sheets.text(x, sheets.CHAIN_Y - 40, word)
        x += len(word) * 3 + 4
    page = slot_page(sheets.sheet(drawing))
    lookup = crops_to_texts(page, TEXTS)
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.row_hold is None
    assert len(result.mapping.proposals) == 4


def test_counter_break_word_outside_row_span_does_not_hold() -> None:
    page = slot_page(sheets.sheet(text_labels() + sheets.text(120, sheets.CHAIN_Y - 40, "RANGE")))
    lookup = crops_to_texts(page, TEXTS)
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.row_hold is None
    assert len(result.mapping.proposals) == 4


def test_ambiguous_row_with_break_word_outside_span_offers_no_countertop() -> None:
    """A future row chooser must not turn an ambiguous bay-spanning row into a proposal."""
    page = slot_page(sheets.sheet(text_labels() + sheets.text(120, sheets.CHAIN_Y - 40, "RANGE")))
    original = page.rows.candidates.rows
    first = original.candidates[0]
    twin = replace(first, y=first.y + 100, rank=2)
    page = replace(
        page,
        rows=replace(
            page.rows,
            candidates=replace(
                page.rows.candidates,
                rows=PageRows(candidates=(first, twin), rejected=original.rejected),
            ),
        ),
    )
    lookup = crops_to_texts(page, TEXTS)
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.plan.ambiguity == "another row on the page fits as well"
    assert result.row_hold is None
    assert result.mapping.proposals == ()


def test_counter_break_word_in_another_drawing_box_does_not_hold() -> None:
    page = slot_page(sheets.sheet(text_labels() + sheets.text(230, sheets.CHAIN_Y - 40, "RANGE")))
    plan = plan_slots(
        page.rows.candidates.rows,
        page.rows.ink,
        settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
    )
    assert plan.row is not None
    y = plan.row.y
    width, height = page.rows.ink.width, page.rows.ink.height
    row_drawing = Box(Decimal(0), y - 20, width, y + 20)
    other_drawing = Box(Decimal(0), y + 21, width, height)
    page = replace(
        page,
        rows=replace(
            page.rows,
            ink=replace(page.rows.ink, drawing_boxes=(row_drawing, other_drawing)),
        ),
    )
    lookup = crops_to_texts(page, TEXTS)
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.row_hold is None
    assert len(result.mapping.proposals) == 4


def test_dishwasher_can_name_a_piece_without_holding_its_row() -> None:
    assert (
        propose_kind(("DW",), index=1, count=3, wall_ends=frozenset()).kind
        is PieceKind.APPLIANCE_SPACE
    )


def test_other_words_still_go_to_the_person() -> None:
    page = glyph_page()
    lookup = crops_to_texts(page, TEXTS | {0: '12" Panel'})
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))
    assert result.slots[0].outcome.reason_code == "not-plain"
    assert result.mapping.proposals == ()


def test_walls_are_asked_once_per_row_of_both_readers_and_seal_on_agreement() -> None:
    page = glyph_page()
    lookup = crops_to_texts(page, TEXTS)
    readers = FakeReaders(lambda _model, png: lookup[png], walls=lambda _model: BOTH_WALLS)

    result = read(page, readers)

    asked = [model for model, _row, _view in readers.wall_requests]
    assert len(asked) == 2 and set(asked) == {KIMI, QWEN}
    row_png, view_png = readers.wall_requests[0][1], readers.wall_requests[0][2]
    assert row_png.startswith(b"\x89PNG") and view_png.startswith(b"\x89PNG")
    assert result.walls is not None
    assert result.walls.outcome.config == "back_left_right"
    assert len(result.walls.answers) == 2


def test_walls_the_readers_do_not_agree_on_go_to_the_person() -> None:
    page = glyph_page()
    lookup = crops_to_texts(page, TEXTS)
    readers = FakeReaders(
        lambda _model, png: lookup[png],
        walls=lambda model: BOTH_WALLS if model == QWEN else UNSURE_WALLS,
    )
    result = read(page, readers)
    assert result.walls is not None and result.walls.outcome.config is None
    assert result.walls.outcome.reason


def test_the_wall_answer_never_changes_a_reading_or_a_proposal() -> None:
    page = glyph_page()
    lookup = crops_to_texts(page, TEXTS)
    sealed = read(page, FakeReaders(lambda _m, png: lookup[png], walls=lambda _m: BOTH_WALLS))
    unsure = read(page, FakeReaders(lambda _m, png: lookup[png]))
    assert sealed.mapping == unsure.mapping
    assert [slot.outcome for slot in sealed.slots] == [slot.outcome for slot in unsure.slots]


# ---------------------------------------------------------------------------------------------
# Persistence (PostgreSQL; skipped without DATABASE_URL, run in CI)
# ---------------------------------------------------------------------------------------------

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def session(postgres_engine: Any) -> Iterator[Any]:
    from alembic import command
    from app.db.session import session_factory
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _scaffold(session: Any) -> tuple[Any, Any, Any, Any]:
    """A revision with one shop page and an extraction run: (revision, version, page, run)."""
    from app.models import (
        Document,
        DocumentVersion,
        Package,
        PackageRevision,
        PackageRevisionDocument,
        PackageState,
        Project,
        SourceArtifact,
    )
    from app.models.document import DocumentKind, Page
    from app.models.runs import ExtractionRun, TaskRun, WorkflowRun

    project = Project(id=uuid4(), name="slot reader tests")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Example Millwork")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.NEEDS_INPUT
    )
    source = SourceArtifact(storage_key=f"s/{uuid4()}", sha256="1" * 64, size=1)
    document = Document(package_id=package.id, kind=DocumentKind.SHOP.value)
    session.add_all([revision, source, document])
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=source.id, sha256="1" * 64, page_count=1
    )
    session.add(version)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision.id,
            package_id=package.id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    page_row = Page(
        document_version_id=version.id,
        index=0,
        content_hash="1" * 64,
        width_pt=Decimal(400),
        height_pt=Decimal(300),
        rotation=0,
        has_vector_text=True,
    )
    session.add(page_row)
    session.flush()
    workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    task_run = TaskRun(
        workflow_run_id=workflow_run.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task_run)
    session.flush()
    run = ExtractionRun(
        task_run_id=task_run.id,
        extractor="extraction.form_reader",
        extractor_version="slot-reader-v1-ink-v1",
        config_hash="test",
    )
    session.add(run)
    session.flush()
    return revision, version, page_row, run


def _read_persisted(
    session: Any,
    data: bytes,
    readers: Callable[[SlotPage], FakeReaders],
    *,
    claude_row_reader: bool = False,
    **persist: Any,
) -> tuple[Any, Any, PageSlotResult, int]:
    from workflow.slot_reader import CLAUDE_SPAN_PROMPT_ID, CROP_PROMPT_ID, persist_slot_readings

    revision, version, page_row, run = _scaffold(session)
    page = slot_page(data)
    page = SlotPage(
        page_index=0,
        page_id=page_row.id,
        document_version_id=version.id,
        rendered=page.rendered,
        rows=page.rows,
        ink=page.ink,
    )
    (result,) = read_slot_pages(
        [page],
        runtime=runtime(readers(page), claude_row_reader=claude_row_reader),
        record_attempt=lambda _a: None,
    )
    count = persist_slot_readings(
        session,
        package_revision_id=revision.id,
        extraction_run_id=run.id,
        reader_ids=(OPUS, SONNET) if claude_row_reader else (KIMI, QWEN),
        results=[result],
        prompt_id=CLAUDE_SPAN_PROMPT_ID if claude_row_reader else CROP_PROMPT_ID,
        **persist,
    )
    return revision, run, result, count


def _by_slot(session: Any, run: Any) -> dict[str, Any]:
    from sqlalchemy import select

    from app.models.evidence import ObservationCandidate

    rows = session.scalars(
        select(ObservationCandidate).where(ObservationCandidate.extraction_run_id == run.id)
    ).all()
    found: dict[str, Any] = {}
    for row in rows:
        if "wall-reader" in row.ambiguity_flags:
            found["walls"] = row
            continue
        slot_flag = next(
            (flag for flag in row.ambiguity_flags if flag.startswith("slot:")),
            None,
        )
        if slot_flag is not None:
            found[slot_flag] = row
    return found


def test_a_claude_zero_choice_persists_a_review_reason_and_numbered_picture(session: Any) -> None:
    from sqlalchemy import select

    from app.models.evidence import EvidenceArtifact, ObservationCandidate
    from workflow.slot_reader import persist_slot_readings

    class MemoryStore:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def put(self, key: str, data: BytesIO, *, content_type: str) -> SimpleNamespace:
            assert content_type == "image/png"
            content = data.read()
            self.objects[key] = content
            return SimpleNamespace(sha256=hashlib.sha256(content).hexdigest())

    revision, version, page_row, run = _scaffold(session)
    source_page = slot_page(named_sheet())
    page = replace(
        source_page,
        page_id=page_row.id,
        document_version_id=version.id,
    )
    readers = FakeReaders(
        lambda _model, _png: '2"',
        row=lambda _model: {"row": 0, "why": "none of the numbered rows is the countertop"},
    )
    form = replace(
        runtime(readers).form,
        reader_ids=(OPUS, SONNET),
        calls_per_minute={OPUS: 6000, SONNET: 6000},
        max_concurrent_calls=1,
    )
    configured = replace(runtime(readers), form=form, claude_row_reader=True)
    (result,) = read_slot_pages([page], runtime=configured, record_attempt=lambda _attempt: None)
    store = MemoryStore()

    count = persist_slot_readings(
        session,
        package_revision_id=revision.id,
        extraction_run_id=run.id,
        reader_ids=(OPUS, SONNET),
        results=[result],
        store=store,
    )
    candidate = session.scalar(
        select(ObservationCandidate).where(
            ObservationCandidate.extraction_run_id == run.id,
            ObservationCandidate.ambiguity_flags.contains(["slot-reader-row-choice"]),
        )
    )
    assert candidate is not None
    artifact = session.scalar(
        select(EvidenceArtifact).where(EvidenceArtifact.candidate_id == candidate.id)
    )

    assert count == 1
    assert candidate.value_numerator is None and candidate.value_denominator is None
    assert candidate.review_reason == "none of the numbered rows is the countertop"
    assert "row-choice:0" in candidate.ambiguity_flags
    assert artifact is not None and store.objects
    assert hashlib.sha256(store.objects[artifact.storage_key]).hexdigest() == artifact.sha256


@pytest.mark.parametrize(
    ("answers", "flags", "kind"),
    [
        (
            {OPUS: {"row": 1, "why": "synthetic: the top line"}, SONNET: {"row": 0, "why": "-"}},
            {"row-pick:opus-5-5:1", "row-pick:sonnet-5-5:0"},
            "split",
        ),
        (
            {OPUS: {"row": 0, "why": "synthetic: no countertop"}, SONNET: {"row": 0, "why": "-"}},
            {"row-pick:opus-5-5:0", "row-pick:sonnet-5-5:0"},
            "none",
        ),
        (
            {OPUS: {"row": 1, "why": "synthetic: the top line"}, SONNET: {"no": "answer"}},
            {"row-pick:opus-5-5:1", "row-pick:sonnet-5-5:none"},
            "split",
        ),
    ],
)
def test_an_unselected_row_record_stores_each_readers_pick(
    session: Any, answers: dict[str, dict[str, object]], flags: set[str], kind: str
) -> None:
    """Each reader's own pick rides on the record, so a split page is told from a "both said no
    row" page by data, never by reason text (#1093)."""
    from sqlalchemy import select

    from app.models.evidence import ObservationCandidate
    from workflow.slot_reader import persist_slot_readings
    from workflow.slot_row_scope import unchosen_row_pages

    revision, version, page_row, run = _scaffold(session)
    page = replace(slot_page(named_sheet()), page_id=page_row.id, document_version_id=version.id)
    readers = FakeReaders(lambda _model, _png: '2"', row=lambda model: answers[model])
    form = replace(
        runtime(readers).form,
        reader_ids=(OPUS, SONNET),
        calls_per_minute={OPUS: 6000, SONNET: 6000},
        max_concurrent_calls=1,
    )
    configured = replace(runtime(readers), form=form, claude_row_reader=True)
    (result,) = read_slot_pages([page], runtime=configured, record_attempt=lambda _attempt: None)
    persist_slot_readings(
        session,
        package_revision_id=revision.id,
        extraction_run_id=run.id,
        reader_ids=(OPUS, SONNET),
        results=[result],
    )
    record = session.scalar(
        select(ObservationCandidate).where(
            ObservationCandidate.extraction_run_id == run.id,
            ObservationCandidate.ambiguity_flags.contains(["slot-reader-row-choice"]),
        )
    )

    assert record is not None
    assert {flag for flag in record.ambiguity_flags if flag.startswith("row-pick:")} == flags
    (unchosen,) = unchosen_row_pages(session, revision.id)
    assert unchosen.record.id == record.id
    assert unchosen.kind == kind


def test_counter_break_row_persists_the_reason_but_no_form_proposals(session: Any) -> None:
    from sqlalchemy import select

    from app.models.evidence import MeasurementProposal

    def readers(page: SlotPage) -> FakeReaders:
        lookup = crops_to_texts(page, TEXTS)
        return FakeReaders(lambda _model, png: lookup[png])

    revision, run, result, _count = _read_persisted(
        session,
        sheets.sheet(text_labels() + sheets.text(230, sheets.CHAIN_Y - 40, "RANGE")),
        readers,
    )

    assert result.row_hold is not None and result.row_hold.code == "counter-break"
    for candidate in _by_slot(session, run).values():
        if "wall-reader" not in candidate.ambiguity_flags:
            assert "row-hold:counter-break" in candidate.ambiguity_flags
            assert candidate.review_reason == result.row_hold.reason
    assert (
        session.scalars(
            select(MeasurementProposal).where(
                MeasurementProposal.package_revision_id == revision.id
            )
        ).all()
        == []
    )


def test_persisted_candidates_carry_what_the_screen_needs_and_only_offered_ones_are_linked(
    session: Any,
) -> None:
    from sqlalchemy import select

    from app.models.evidence import MeasurementProposal, ObservationCandidate

    def readers(page: SlotPage) -> FakeReaders:
        indexed = claude_crops_to_texts(
            page,
            {0: "slot-0", 1: "slot-1", 2: "slot-2", 3: "slot-3", None: "overall"},
        )
        lookup = {
            png: FOUR_PIECE_TEXTS[
                None if position == "overall" else int(position.removeprefix("slot-"))
            ]
            for png, position in indexed.items()
        }
        return FakeReaders(lambda _model, png: lookup[png])

    revision, run, result, count = _read_persisted(
        session,
        four_piece_sheet(covered_slot=1),
        readers,
        claude_row_reader=True,
    )

    assert count == 6  # four slots, the overall and the row's walls
    by_slot = _by_slot(session, run)
    assert {
        key: by_slot[f"slot:{key.removeprefix('slot:')}"].id for key in result.owner_candidate_ids
    } == result.owner_candidate_ids
    assert by_slot["walls"].id == result.wall_candidate_id
    covered = by_slot["slot:1"]
    assert covered.value_numerator is None and covered.corroboration_status is None
    assert covered.review_reason is not None and covered.review_reason.startswith("covered by")
    assert covered.semantic_guess is None
    assert "ink:covered" in covered.ambiguity_flags
    first = by_slot["slot:0"]
    assert "row-partial" in first.ambiguity_flags
    assert "row-slot-count:4" in first.ambiguity_flags
    assert any(flag.startswith("slot-box:") for flag in first.ambiguity_flags)
    assert any(flag.startswith("crop-box:") for flag in first.ambiguity_flags)
    assert first.corroboration_status == "CORROBORATED", "the sealed piece is shown as a proposal"
    assert first.value_numerator == 12 and first.review_reason is None
    assert {flag for flag in first.ambiguity_flags if flag.startswith("reader-id:")} == {
        f"reader-id:{OPUS}",
        f"reader-id:{SONNET}",
    }
    supports = session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.extraction_run_id == run.id,
            ObservationCandidate.ambiguity_flags.contains(["slot-reader-support"]),
        )
    ).all()
    assert len(supports) == 6, "each of the three accepted values retains both independent answers"
    assert all(
        any(flag.startswith("supports:") for flag in row.ambiguity_flags) for row in supports
    )
    overall = by_slot["slot:overall"]
    assert overall.corroboration_status is None and overall.value_numerator == 48
    assert overall.review_reason is not None and overall.review_reason.startswith("held back")

    proposals = session.scalars(
        select(MeasurementProposal)
        .where(MeasurementProposal.package_revision_id == revision.id)
        .order_by(MeasurementProposal.field_key, MeasurementProposal.position)
    ).all()
    assert [(proposal.field_key, proposal.position) for proposal in proposals] == [
        (PIECE_FIELD, 0),
        (PIECE_FIELD, 2),
        (PIECE_FIELD, 3),
    ], "sealed neighbors are proposed in their original slots; the missing slot stays blank"
    from app.api.measurements import _stored_proposal_out
    from tests.workflow.test_stages import _publish_rulebook

    _publish_rulebook(session)
    api_field = next(
        field for field in _stored_proposal_out(session, revision) if field.field_key == PIECE_FIELD
    )
    assert api_field.expected_count == 4
    assert [reading.position for reading in api_field.values] == [0, 2, 3]


def test_a_fully_sealed_row_links_every_piece_in_order_and_a_veto_is_kept_as_a_suggestion(
    session: Any,
) -> None:
    from sqlalchemy import select

    from app.models.evidence import MeasurementProposal

    def readers(page: SlotPage) -> FakeReaders:
        lookup = crops_to_texts(page, TEXTS)
        return FakeReaders(lambda _model, png: lookup[png])

    revision, run, _result, _count = _read_persisted(
        session, sheets.sheet(sheets.glyph_labels()), readers
    )
    by_slot = _by_slot(session, run)
    links = session.scalars(
        select(MeasurementProposal)
        .where(
            MeasurementProposal.package_revision_id == revision.id,
            MeasurementProposal.field_key == PIECE_FIELD,
        )
        .order_by(MeasurementProposal.position)
    ).all()
    assert [link.candidate_id for link in links] == [
        by_slot["slot:0"].id,
        by_slot["slot:1"].id,
        by_slot["slot:2"].id,
    ]

    def misreaders(page: SlotPage) -> FakeReaders:
        lookup = crops_to_texts(page, TEXTS | {2: '120"'})
        return FakeReaders(lambda _model, png: lookup[png])

    revision, run, _result, _count = _read_persisted(
        session, sheets.sheet(sheets.glyph_labels()), misreaders
    )
    vetoed = _by_slot(session, run)["slot:2"]
    assert "drawn-length" in vetoed.ambiguity_flags
    assert vetoed.review_reason == "doesn't match the drawn length"
    assert vetoed.corroboration_status is None
    assert vetoed.value_numerator == 120, "the agreed text stays, as a suggestion for the person"
    assert (
        session.scalars(
            select(MeasurementProposal).where(
                MeasurementProposal.package_revision_id == revision.id
            )
        ).all()
        == []
    )


def test_claude_drawn_length_is_reject_only_when_no_scale_can_be_derived() -> None:
    from extraction.geometry.rows import Box
    from extraction.ink import InkClass
    from extraction.slot_reader.runs import Lane, PlannedLabel, PlannedOwner
    from extraction.slot_reader.seal import LabelOutcome, OwnerOutcome, plain_dimension
    from workflow.slot_reader import LabelResult, OwnerResult, _veto_by_drawn_length

    def provisional_owner(index: int, inches: str) -> OwnerResult:
        x0 = Decimal(index * 10)
        x1 = x0 + Decimal(10)
        box = Box(x0, Decimal(1), x1, Decimal(3))
        label = PlannedLabel(
            box=box,
            crop=box,
            lane=Lane.GLYPHS,
            text=None,
            text_stacked=False,
            has_digit=True,
            touches_edge=False,
            ambiguous_slot=False,
            crowded=False,
            ticks_in_crop=True,
            path_boxes=(),
        )
        value = plain_dimension(f'{inches}"')
        assert value is not None
        outcome = LabelOutcome(
            LabelState.PROVISIONAL,
            None,
            value,
            None,
            None,
            None,
            InkClass.VENDOR,
            (),
            (("opus", f'{inches}"'), ("sonnet", f'{inches}"')),
        )
        owner = PlannedOwner(index, x0, x1, Decimal(2), box, (label,))
        label_result = LabelResult(label, outcome, (0, 0, 1, 1), (0, 0, 1, 1))
        return OwnerResult(
            owner,
            (0, 0, 1, 1),
            (label_result,),
            OwnerOutcome(LabelState.PROVISIONAL, None, 0, None, None),
            None,
            (),
        )

    # With no independent dimensions, the drawn-length check has no evidence and cannot hold it.
    only_one = provisional_owner(0, "10")
    accepted, _, vetoed = _veto_by_drawn_length((only_one,), None)
    assert accepted[0].outcome.state is LabelState.SEALED
    assert accepted[0].outcome.value is not None
    assert vetoed == ()

    # Three agreeing, proportionate dimensions provide a deterministic witness.
    proportionate = tuple(provisional_owner(index, "10") for index in range(3))
    accepted, _, _ = _veto_by_drawn_length(proportionate, None)
    assert all(owner.outcome.state is LabelState.SEALED for owner in accepted)

    # A unanimous but geometrically inconsistent copy is still held, never parsed into a value.
    inconsistent = tuple(
        provisional_owner(index, "20" if index == 2 else "10") for index in range(3)
    )
    rejected, _, _ = _veto_by_drawn_length(inconsistent, None)
    assert rejected[2].outcome.state is LabelState.REVIEW
    assert rejected[2].outcome.reason_code == "drawn-length"
    assert rejected[2].outcome.value is None


def test_a_sealed_wall_layout_is_kept_with_its_pictures_and_proposed(
    session: Any, tmp_path: Any
) -> None:
    from sqlalchemy import select

    from app.models.evidence import EvidenceArtifact, LayoutProposal
    from storage.local import LocalStore
    from workflow.layout_proposals import reader_sealed_wall_config

    store = LocalStore(tmp_path / "artifacts")

    def readers(page: SlotPage) -> FakeReaders:
        lookup = crops_to_texts(page, TEXTS)
        return FakeReaders(lambda _model, png: lookup[png], walls=lambda _model: BOTH_WALLS)

    revision, run, result, _count = _read_persisted(
        session, sheets.sheet(sheets.glyph_labels()), readers, store=store
    )

    walls = _by_slot(session, run)["walls"]
    assert "walls-sealed:back_left_right" in walls.ambiguity_flags
    assert sum(flag.startswith("wall-reader:") for flag in walls.ambiguity_flags) == 2
    assert walls.value_numerator is None, "a wall answer is never a value"
    artifacts = session.scalars(
        select(EvidenceArtifact).where(EvidenceArtifact.candidate_id == walls.id)
    ).all()
    assert len(artifacts) == 2
    assert result.walls is not None
    stored = {store.get(artifact.storage_key).read() for artifact in artifacts}
    assert stored == {result.walls.row_png, result.walls.view_png}, "exactly what the readers saw"
    proposal = session.scalars(
        select(LayoutProposal).where(LayoutProposal.package_revision_id == revision.id)
    ).one()
    assert proposal.discriminator_name == "wall_config"
    assert proposal.proposed_value == "back_left_right"
    assert proposal.prompt_id == "slot-walls-v1"
    assert proposal.model_id == f"{KIMI} + {QWEN}"
    sealed = reader_sealed_wall_config(session, revision.id)
    assert sealed is not None and sealed.value == "back_left_right"


def test_slot_reader_wall_answers_are_selected_from_the_page_only(
    session: Any, tmp_path: Any
) -> None:
    from storage.local import LocalStore
    from workflow.layout_proposals import slot_reader_has_rows, slot_reader_wall_for_page

    def readers(page: SlotPage) -> FakeReaders:
        lookup = crops_to_texts(page, TEXTS)
        return FakeReaders(lambda _model, png: lookup[png], walls=lambda _model: BOTH_WALLS)

    revision, _run, _result, _count = _read_persisted(
        session,
        sheets.sheet(sheets.glyph_labels()),
        readers,
        store=LocalStore(tmp_path / "artifacts"),
    )

    assert slot_reader_has_rows(session, revision.id)
    page_zero = slot_reader_wall_for_page(session, revision.id, 0)
    assert page_zero is not None
    assert page_zero.selected and not page_zero.held and not page_zero.row_held
    assert page_zero.layout == "back_left_right"
    assert page_zero.source == "readers"
    assert slot_reader_wall_for_page(session, revision.id, 1) is None


def test_walls_the_readers_do_not_settle_propose_nothing(session: Any, tmp_path: Any) -> None:
    from sqlalchemy import select

    from app.models.evidence import LayoutProposal
    from storage.local import LocalStore
    from workflow.layout_proposals import reader_sealed_wall_config

    def readers(page: SlotPage) -> FakeReaders:
        lookup = crops_to_texts(page, TEXTS)
        return FakeReaders(
            lambda _model, png: lookup[png],
            walls=lambda model: BOTH_WALLS if model == QWEN else UNSURE_WALLS,
        )

    revision, run, _result, _count = _read_persisted(
        session,
        sheets.sheet(sheets.glyph_labels()),
        readers,
        store=LocalStore(tmp_path / "artifacts"),
    )

    walls = _by_slot(session, run)["walls"]
    assert any(flag.startswith("walls-held:") for flag in walls.ambiguity_flags)
    assert walls.review_reason
    assert (
        session.scalars(
            select(LayoutProposal).where(LayoutProposal.package_revision_id == revision.id)
        ).all()
        == []
    )
    assert reader_sealed_wall_config(session, revision.id) is None


def test_vendor_filler_word_is_a_positive_wall_clue_but_gv_ink_is_not() -> None:
    from dataclasses import replace

    from extraction.ink import InkClass, InkLabel
    from extraction.slot_reader.walls import E3_WALL_SETTINGS
    from workflow.slot_reader import _code_wall_clues, _wall_job_pictures

    page = slot_page(sheets.sheet(text_labels()))
    plan = plan_slots(
        page.rows.candidates.rows,
        page.rows.ink,
        settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
    )
    assert plan.row is not None and plan.ambiguity is None and page.ink is not None
    pictures = _wall_job_pictures(page, plan, E3_WALL_SETTINGS)
    assert pictures is not None
    slot = plan.slots[0]
    center_x = page.rows.to_pixels((slot.x0 + slot.x1) / 2, plan.row.y)[0]
    center_y = page.rows.to_pixels(plan.row.x0, plan.row.y)[1]

    def clues(ink_class: InkClass, x: int = center_x) -> Any:
        label = InkLabel("FILLER", (x - 8, center_y - 3, x + 8, center_y + 3), ink_class)
        updated = replace(page, ink=replace(page.ink, labels=(*page.ink.labels, label)))
        return _code_wall_clues(updated, plan, pictures)

    assert clues(InkClass.VENDOR).left is True
    assert clues(InkClass.GV).left is None
    row_start = page.rows.to_pixels(plan.row.x0, plan.row.y)[0]
    assert clues(InkClass.VENDOR, row_start - 20).left is None


def test_two_positive_drawing_wall_clues_skip_the_reader_question(monkeypatch: Any) -> None:
    from extraction.slot_reader.walls import CodeWallClues

    page = slot_page(sheets.sheet(text_labels()))
    clients = FakeReaders(lambda _model, _png: '12"')
    monkeypatch.setattr(
        "workflow.slot_reader._code_wall_clues",
        lambda _page, _plan, _pictures: CodeWallClues(left=True, right=True),
    )

    (result,) = read_slot_pages(
        [page], runtime=runtime(clients), record_attempt=lambda _attempt: None
    )

    assert result.walls is not None
    assert result.walls.outcome.config == "back_left_right"
    assert result.walls.outcome.source == "vendor-drawing-clues"
    assert clients.wall_requests == []


def _claude_walls_read(texts: Mapping[int | None, str]) -> PageSlotResult:
    page = glyph_page()
    lookup = claude_crops_to_texts(page, texts)
    readers = FakeReaders(
        lambda _model, png: lookup.get(png, '2"'), walls=lambda _model: UNSURE_WALLS
    )
    base = runtime(readers)
    configured = replace(
        base,
        form=replace(
            base.form,
            reader_ids=(OPUS, SONNET),
            calls_per_minute={OPUS: 6000, SONNET: 6000},
        ),
        claude_row_reader=True,
    )
    (result,) = read_slot_pages([page], runtime=configured, record_attempt=lambda _attempt: None)
    return result


def test_a_filler_both_readers_sealed_at_each_row_end_settles_the_walls() -> None:
    """Proof run 2026-10-08: a vendor draws its "Filler" sums as lines, so the file's text has no
    "Filler"; the sealed readings are the only place the drawing's own clue shows up."""
    result = _claude_walls_read(TEXTS | {0: '10"+2"Filler', 2: '10"+2"Filler'})

    assert [slot.outcome.state for slot in result.slots][::2] == [LabelState.SEALED] * 2
    assert result.walls is not None
    assert result.walls.code_clues.left is True and result.walls.code_clues.right is True
    assert result.walls.outcome.config == "back_left_right"
    assert result.walls.outcome.source == "vendor-drawing-clues"


def test_a_filler_read_at_one_end_only_leaves_the_walls_to_the_person() -> None:
    result = _claude_walls_read(TEXTS | {0: '10"+2"Filler'})

    assert result.walls is not None
    assert result.walls.code_clues.left is True and result.walls.code_clues.right is None
    assert result.walls.outcome.config is None


def test_claude_close_up_reaches_past_a_narrow_pieces_ticks() -> None:
    """A 1 1/2 panel's label is wider than the panel; cut at its ticks the readers saw only
    "1/2" (proof run 2026-10-08). The close-up reaches 12 pt (50 px at 300 dpi) or 15% of the
    span past each end, as the prototype's did, and stops at the page edge."""
    from workflow.slot_reader import _claude_span_plan

    page = glyph_page()
    plan = plan_slots(
        page.rows.candidates.rows,
        page.rows.ink,
        settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
    )
    span = _claude_span_plan(page, plan)
    for owner in (*span.slots, *((span.overall,) if span.overall else ())):
        band, crop = owner.band, owner.labels[0].crop
        margin = max(Decimal(12), Decimal("0.15") * (band.x1 - band.x0))
        assert crop.x0 == max(Decimal(0), band.x0 - margin)
        assert crop.x1 == min(page.rows.ink.width, band.x1 + margin)
        assert (crop.top, crop.bottom) == (band.top, band.bottom)
        assert owner.labels[0].box == band, "which span is meant never moves"


def test_the_full_view_mark_stays_visible_after_the_picture_is_shrunk() -> None:
    """The prototype's mark was 4 px thick on a 110 dpi page. The product marks a higher-dpi
    render and shrinks it, so the line must be drawn 4 * dpi/110 thick to look the same; the old
    fixed 2 px line faded to under a pixel on a 300 dpi page (proof run 2026-10-08)."""
    from io import BytesIO

    from PIL import Image

    from workflow.slot_reader import _span_view_png

    page = glyph_page()
    plan = plan_slots(
        page.rows.candidates.rows,
        page.rows.ink,
        settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
    )
    view = Image.open(BytesIO(_span_view_png(page, plan.slots[0]))).convert("RGB")
    pixels = view.load()
    assert pixels is not None

    def red(x: int, y: int) -> bool:
        r, g, b = pixels[x, y][:3]
        return r >= 200 and g <= 60 and b <= 60

    marked = [(x, y) for y in range(view.height) for x in range(view.width) if red(x, y)]
    assert marked, "the span is boxed in red"
    left = min(x for x, _ in marked)
    middle = (min(y for _, y in marked) + max(y for _, y in marked)) // 2
    thickness = 0
    while left + thickness < view.width and red(left + thickness, middle):
        thickness += 1
    shrink = max(page.rendered.width_px, page.rendered.height_px) / max(view.size)
    assert thickness >= round(4 * page.rendered.dpi / 110 / shrink) - 1


def _two_row_claude_read(row: Callable[[str], Mapping[str, object]]) -> PageSlotResult:
    page = slot_page(named_sheet())
    first = page.rows.candidates.rows.candidates[0]
    second = replace(first, rank=2)
    page = replace(
        page,
        rows=replace(
            page.rows,
            candidates=replace(
                page.rows.candidates,
                rows=PageRows(candidates=(first, second), rejected=()),
            ),
        ),
    )
    readers = FakeReaders(lambda _model, _png: '2"', row=row)
    form = replace(
        runtime(readers).form,
        reader_ids=(OPUS, SONNET),
        calls_per_minute={OPUS: 6000, SONNET: 6000},
        max_concurrent_calls=1,
    )
    configured = replace(runtime(readers), form=form, claude_row_reader=True)
    (result,) = read_slot_pages([page], runtime=configured, record_attempt=lambda _attempt: None)
    return result


def test_readers_naming_different_rows_send_the_page_to_the_reviewer() -> None:
    """One reader's row is not enough: on a client page one run picked the countertop row and
    the next a table in a reviewer's notes box (proof runs 2026-10-08)."""
    result = _two_row_claude_read(
        lambda model: {"row": 2 if model == OPUS else 1, "why": "this one"}
    )

    assert result.plan.row is None
    assert result.row_choice_number == 0
    assert result.row_choice is not None
    assert "different rows" in result.row_choice.why
    assert "opus-5-5 row 2" in result.row_choice.why and "sonnet-5-5 row 1" in result.row_choice.why
    assert result.slots == () and result.overall is None
    assert not result.mapping.proposals


def test_a_reader_with_no_row_answer_sends_the_page_to_the_reviewer() -> None:
    result = _two_row_claude_read(
        lambda model: {"row": 2, "why": "box 2"} if model == OPUS else {"why": "no number"}
    )

    assert result.plan.row is None
    assert result.row_choice_number == 0
    assert result.row_choice is not None and "no row answer" in result.row_choice.why
    assert not result.mapping.proposals


def test_readers_agreeing_on_no_row_keep_the_page_for_the_reviewer() -> None:
    result = _two_row_claude_read(lambda _model: {"row": 0, "why": "none is the countertop"})

    assert result.plan.row is None
    assert result.row_choice_number == 0
    assert not result.mapping.proposals


def test_row_picture_tags_stand_beside_their_boxes_at_the_prototypes_size() -> None:
    """A reader named the right row by its words but gave another box's number when the tags
    were 3x5 dots inside the boxes, shrunk about 3x (proof run 2026-10-08). Each tag is now a
    filled square just left of its box, about 48 px tall at 150 dpi as the prototype drew it."""
    from io import BytesIO

    from PIL import Image

    from workflow.slot_reader import _ROW_COLOURS, _numbered_rows_png

    page = slot_page(named_sheet())
    png, candidates = _numbered_rows_png(page)
    view = Image.open(BytesIO(png)).convert("RGB")
    shrink = max(page.rendered.width_px, page.rendered.height_px) / max(view.size)
    pixels = view.load()
    assert pixels is not None and candidates

    colour = tuple(_ROW_COLOURS[0])
    filled = [
        (x, y)
        for y in range(view.height)
        for x in range(view.width)
        if all(abs(a - b) <= 40 for a, b in zip(pixels[x, y][:3], colour, strict=True))
    ]
    first = candidates[0]
    box_left = min(
        page.rows.to_pixels(first.x0, first.y)[0], page.rows.to_pixels(first.x1, first.y)[0]
    )
    tag = [(x, y) for x, y in filled if x < (box_left / shrink) - 1]
    assert tag, "the tag stands left of its box, clear of the row's labels"
    height = max(y for _, y in tag) - min(y for _, y in tag) + 1
    expected = 48 * page.rendered.dpi / 150 / shrink
    assert height >= 0.8 * expected


def _claude_line_read(counter_break: Callable[[str], Mapping[str, object]]) -> PageSlotResult:
    page = slot_page(sheets.sheet(sheets.text_labels()))
    lookup = claude_crops_to_texts(page, TEXTS)
    readers = FakeReaders(lambda _model, png: lookup[png], counter_break=counter_break)
    base = runtime(readers)
    configured = replace(
        base,
        form=replace(
            base.form,
            reader_ids=(OPUS, SONNET),
            calls_per_minute={OPUS: 6000, SONNET: 6000},
        ),
        claude_row_reader=True,
    )
    (result,) = read_slot_pages([page], runtime=configured, record_attempt=lambda _attempt: None)
    return result


def test_a_stone_stopping_short_of_the_row_ends_goes_to_the_reviewer() -> None:
    """Stone between full-height fillers: the fillers take the field cut, so the width check's
    arithmetic does not apply (GV-Brain field-cut note, 2026-10-08). One reader is enough."""
    result = _claude_line_read(
        lambda model: {
            "contains_tall_appliance": False,
            "stone_ends": "short_of_ends" if model == SONNET else "to_walls",
            "why": "stone sits between full-height fillers",
        }
    )

    assert result.row_hold is None, "the readings stand"
    assert result.check_hold is not None and result.check_hold.code == "stone-short-of-ends"
    assert result.mapping.proposals, "the numbers are still offered to the reviewer"


def test_a_stone_running_into_the_walls_goes_to_the_reviewer() -> None:
    result = _claude_line_read(
        lambda model: {
            "contains_tall_appliance": False,
            "stone_ends": "into_walls" if model == OPUS else "unsure",
            "why": "stone runs into pockets in both walls",
        }
    )

    assert result.row_hold is None
    assert result.check_hold is not None and result.check_hold.code == "stone-into-walls"
    assert result.mapping.proposals


def test_a_stone_ending_at_the_walls_is_not_held() -> None:
    result = _claude_line_read(
        lambda _model: {
            "contains_tall_appliance": False,
            "stone_ends": "to_walls",
            "why": "stone runs over the fillers to both walls",
        }
    )

    assert result.row_hold is None and result.check_hold is None
    assert result.mapping.proposals


def _fake_rows(
    row_x: tuple[int, int], wall_x: tuple[int, int], words: tuple[str, ...]
) -> tuple[Any, Any]:
    from types import SimpleNamespace

    label = SimpleNamespace(lines=words)
    wall_row = SimpleNamespace(
        slots=(),
        overall=SimpleNamespace(x0=Decimal(wall_x[0]), x1=Decimal(wall_x[1]), labels=(label,)),
    )
    page = SimpleNamespace(
        rows=SimpleNamespace(
            candidates=SimpleNamespace(rows=SimpleNamespace(candidates=(wall_row,), rejected=()))
        )
    )
    plan = SimpleNamespace(row=SimpleNamespace(x0=Decimal(row_x[0]), x1=Decimal(row_x[1])))
    return page, plan


def test_a_wall_to_wall_line_inside_the_row_means_the_stone_runs_into_the_walls() -> None:
    """Code's own clue, whatever the readers say: the vendor's "wall to wall" line sits inside
    both ends of the stone's row, so the stone runs past the wall faces."""
    from workflow.slot_reader import _stone_end_hold

    page, plan = _fake_rows((100, 400), (130, 370), ("915", "[36]", "wall to wall"))
    hold = _stone_end_hold(page, plan, ())

    assert hold is not None and hold.code == "stone-into-walls"


def test_a_wall_to_wall_line_reaching_the_row_ends_is_not_a_pocket() -> None:
    from workflow.slot_reader import _stone_end_hold

    page, plan = _fake_rows((100, 400), (100, 400), ("wall to wall",))
    assert _stone_end_hold(page, plan, ()) is None
    page, plan = _fake_rows((100, 400), (130, 400), ("wall to wall",))
    assert _stone_end_hold(page, plan, ()) is None, "inside one end only is not a pocket at both"


def test_the_claude_reader_budget_is_two_fifty_by_default_and_five_at_most() -> None:
    """The admin raised the per-set cap on 2026-10-08 (at $2 the worst-case reservation refused
    the last calls of a $1.72 run); $5 stays the hard ceiling."""
    from pydantic import ValidationError

    url = "postgresql+psycopg://x@localhost/x"
    assert Settings(database_url=url).claude_reader_budget_usd == Decimal("2.50")
    assert (
        Settings(database_url=url, claude_reader_budget_usd=Decimal(5)).claude_reader_budget_usd
        == 5
    )
    with pytest.raises(ValidationError):
        Settings(database_url=url, claude_reader_budget_usd=Decimal("5.01"))


# ---------------------------------------------------------------------------------------------
# #1051: effort on every Claude call, upright copies of sideways labels
# ---------------------------------------------------------------------------------------------


def sideways_sheet() -> bytes:
    """The middle slot's label drawn sideways: a glyph run 4 pt wide and 10 pt tall."""
    marks = b"".join(
        (
            f"{248:.2f} {sheets.CHAIN_Y + 2 + k * 3.5:.2f} m {252:.2f} "
            f"{sheets.CHAIN_Y + 2 + k * 3.5:.2f} {252:.2f} {sheets.CHAIN_Y + 3.5 + k * 3.5:.2f} "
            f"{252:.2f} {sheets.CHAIN_Y + 5 + k * 3.5:.2f} c {248:.2f} "
            f"{sheets.CHAIN_Y + 5 + k * 3.5:.2f} l h f\n"
        ).encode()
        for k in range(3)
    )
    drawing = sheets.text(168, sheets.CHAIN_Y + 4, '12"') + sheets.text(
        318, sheets.CHAIN_Y + 4, '12"'
    )
    return sheets.sheet(drawing + marks + sheets.text(243, sheets.OVERALL_Y + 4, '48"'))


def claude_runtime(readers: FakeReaders, **changes: Any) -> SlotReaderRuntime:
    base = runtime(readers)
    return replace(
        base,
        form=replace(
            base.form,
            reader_ids=(OPUS, SONNET),
            calls_per_minute={OPUS: 6000, SONNET: 6000},
        ),
        claude_row_reader=True,
        **changes,
    )


def test_a_sideways_label_adds_its_upright_close_up_as_a_third_picture() -> None:
    from extraction.slot_reader.bedrock import CLAUDE_SPAN_PROMPT_ID, CLAUDE_UPRIGHT_NOTE
    from workflow.slot_reader import _upright_png

    page = slot_page(sideways_sheet())
    readers = FakeReaders(lambda _model, _png: '2"')
    attempts: list[AttemptUsage] = []

    (result,) = read_slot_pages(
        [page], runtime=claude_runtime(readers), record_attempt=attempts.append
    )

    three = [pictures for _model, pictures in readers.span_pictures if len(pictures) == 3]
    two = [pictures for _model, pictures in readers.span_pictures if len(pictures) == 2]
    owners = len(result.plan.slots) + (result.plan.overall is not None)
    assert len(three) == 2, "the sideways span, asked of both readers"
    assert len(two) == 2 * (owners - 1), "every other span keeps its two pictures"
    for _view, close_up, upright in three:
        assert upright == _upright_png(close_up)
    upright_spans = {texts for _model, texts in readers.span_texts if CLAUDE_UPRIGHT_NOTE in texts}
    assert len(upright_spans) == 1
    assert all(
        attempt.prompt_id == CLAUDE_SPAN_PROMPT_ID == "claude-slot-span-v3"
        for attempt in attempts
        if attempt.prompt_id.startswith("claude-slot-span")
    )


def test_a_label_that_is_not_sideways_keeps_exactly_two_pictures() -> None:
    page = slot_page(named_sheet())
    readers = FakeReaders(lambda _model, _png: '2"')

    read_slot_pages([page], runtime=claude_runtime(readers), record_attempt=lambda _a: None)

    assert readers.span_pictures
    assert all(len(pictures) == 2 for _model, pictures in readers.span_pictures)


def test_a_stacked_fraction_is_never_taken_for_a_sideways_label() -> None:
    from extraction.slot_reader.runs import Lane, PlannedLabel
    from workflow.slot_reader import _sideways, fraction_bar_from_environment

    tall = Box(Decimal(0), Decimal(0), Decimal(4), Decimal(10))
    label = PlannedLabel(
        box=tall,
        crop=tall,
        lane=Lane.GLYPHS,
        text=None,
        text_stacked=False,
        has_digit=True,
        touches_edge=False,
        ambiguous_slot=False,
        crowded=False,
        ticks_in_crop=True,
        path_boxes=(),
    )
    geometry = fraction_bar_from_environment(FRACTION_ENV)
    height = Decimal(792)

    assert _sideways(label, height, geometry) is True
    assert _sideways(replace(label, text_stacked=True), height, geometry) is False
    # Measured on both client sets: a stacked fraction or a millimetre line over its [inch] line
    # stands up to 1.9 times as tall as wide; none of them is turned.
    almost = Box(Decimal(0), Decimal(0), Decimal(10), Decimal(19))
    assert _sideways(replace(label, box=almost), height, geometry) is False


def test_the_upright_copy_is_the_close_up_turned_a_quarter_clockwise() -> None:
    from evidence.crop import decode_rgb_png, encode_png
    from workflow.slot_reader import _upright_png

    red, blue, white = bytes((255, 0, 0)), bytes((0, 0, 255)), bytes((255, 255, 255))
    # 3 wide, 2 tall: the top-left pixel red, the bottom-left blue.
    original = encode_png(3, 2, red + white + white + blue + white + white)

    width, height, rgb = decode_rgb_png(_upright_png(original))

    assert (width, height) == (2, 3)
    # Turned clockwise, the left column becomes the top row, read from bottom to top.
    assert rgb[0:3] == blue and rgb[3:6] == red


def test_every_claude_question_packet_records_the_effort_it_was_asked_at() -> None:
    class MemoryStore:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def put(self, key: str, data: BytesIO, *, content_type: str) -> SimpleNamespace:
            content = data.read()
            self.objects[key] = content
            return SimpleNamespace(sha256=hashlib.sha256(content).hexdigest())

    page = replace(
        slot_page(sideways_sheet()),
        transform=PageTransform(
            dpi=DPI,
            rotation=0,
            media_box=(Decimal(0), Decimal(0), Decimal(612), Decimal(792)),
            crop_box=(Decimal(0), Decimal(0), Decimal(612), Decimal(792)),
        ),
    )
    readers = FakeReaders(lambda _model, _png: '2"')
    attempts: list[AttemptUsage] = []
    store = MemoryStore()

    read_slot_pages(
        [page],
        runtime=claude_runtime(readers, question_packets=True, claude_effort="xhigh"),
        record_attempt=attempts.append,
        store=store,
    )

    assert attempts
    assert all(
        attempt.question_packet is not None and attempt.question_packet["effort"] == "xhigh"
        for attempt in attempts
    )
    upright = [
        attempt.question_packet["images"]["upright_close_up"]
        for attempt in attempts
        if attempt.question_packet is not None
        and "upright_close_up" in attempt.question_packet["images"]
    ]
    assert len(upright) == 2
    for image in upright:
        assert hashlib.sha256(store.objects[image["storage_key"]]).hexdigest() == image["sha256"]


def test_the_effort_setting_defaults_to_high_and_refuses_an_unknown_level() -> None:
    from pydantic import ValidationError

    assert (
        Settings(database_url="postgresql+psycopg://x@localhost/x").claude_reader_effort == "high"
    )
    assert (
        Settings(
            database_url="postgresql+psycopg://x@localhost/x", claude_reader_effort="max"
        ).claude_reader_effort
        == "max"
    )
    with pytest.raises(ValidationError, match="claude_reader_effort"):
        Settings(database_url="postgresql+psycopg://x@localhost/x", claude_reader_effort="adaptive")


def test_the_configured_effort_reaches_the_runtime_and_the_run_identity() -> None:
    form = runtime(FakeReaders(lambda _m, _p: "")).form

    class ClaudeOn:
        slot_reader_enabled = True
        slot_reader_stacked_agreement = False
        claude_reader_enabled = True
        openrouter_api_key = SecretStr("private-test-key")
        claude_reader_model_rpm: ClassVar[dict[str, int]] = {OPUS: 60, SONNET: 60}
        claude_reader_timeout_seconds = 180
        claude_reader_effort = "medium"

    configured = configured_slot_reader(ClaudeOn(), form, environ=FRACTION_ENV)
    assert configured is not None and configured.claude_effort == "medium"
    assert "effort:medium" in configured.config_detail
    assert len(f"dpi=300;{configured.config_hash}") <= 200
    high = replace(configured, claude_effort="high")
    assert high.config_hash != configured.config_hash

    ClaudeOn.claude_reader_effort = "adaptive"
    with pytest.raises(ValueError, match="GV_CLAUDE_READER_EFFORT"):
        configured_slot_reader(ClaudeOn(), form, environ=FRACTION_ENV)


def test_the_claude_readers_go_through_openrouter_unless_the_admin_says_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1094: the same two models, called through OpenRouter by default; Anthropic's own API is
    one setting away. Each route needs its own key, and starts without the other's."""
    from extraction.slot_reader.anthropic import ThreadLocalAnthropicClients
    from extraction.slot_reader.openrouter import ThreadLocalOpenRouterClients

    for name in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "GV_CLAUDE_READER_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    url = "postgresql+psycopg://x@localhost/x"
    assert Settings(_env_file=None, database_url=url).claude_reader_provider == "openrouter"
    # `_env_file=None`: a developer's own .env may hold a key.
    on: dict[str, Any] = {
        "_env_file": None,
        "database_url": url,
        "slot_reader_enabled": True,
        "claude_reader_enabled": True,
    }
    with pytest.raises(ValueError, match="PROVIDER=openrouter requires OPENROUTER_API_KEY"):
        Settings(**on, anthropic_api_key=SecretStr("private-test-key"))
    with pytest.raises(ValueError, match="PROVIDER=anthropic requires ANTHROPIC_API_KEY"):
        Settings(
            **on,
            claude_reader_provider="anthropic",
            openrouter_api_key=SecretStr("private-test-key"),
        )
    with pytest.raises(ValueError, match="claude_reader_provider"):
        Settings(_env_file=None, database_url=url, claude_reader_provider="bedrock")
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        Settings(**on, openrouter_api_key=SecretStr("   "))
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-router-key")
    from_env = Settings(_env_file=None, database_url=url)
    assert from_env.openrouter_api_key is not None
    assert from_env.openrouter_api_key.get_secret_value() == "private-router-key"

    form = runtime(FakeReaders(lambda _m, _p: "")).form

    class ClaudeOn:
        slot_reader_enabled = True
        slot_reader_stacked_agreement = False
        claude_reader_enabled = True
        openrouter_api_key = SecretStr("private-test-key")
        claude_reader_model_rpm: ClassVar[dict[str, int]] = {OPUS: 60, SONNET: 60}
        claude_reader_timeout_seconds = 90

    through_router = configured_slot_reader(ClaudeOn(), form, environ=FRACTION_ENV)
    assert through_router is not None
    assert isinstance(through_router.form.clients, ThreadLocalOpenRouterClients)
    assert through_router.form.reader_ids == (OPUS, SONNET)
    assert "private-test-key" not in repr(through_router.form.clients)

    class Direct(ClaudeOn):
        claude_reader_provider = "anthropic"

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        configured_slot_reader(Direct(), form, environ=FRACTION_ENV)
    Direct.anthropic_api_key = SecretStr("private-test-key")  # type: ignore[attr-defined]
    direct = configured_slot_reader(Direct(), form, environ=FRACTION_ENV)
    assert direct is not None
    assert isinstance(direct.form.clients, ThreadLocalAnthropicClients)
    assert direct.form.reader_ids == through_router.form.reader_ids
    # A reading says where it was made; the direct route's identity is exactly as before.
    assert ",route=openrouter:google-vertex/global" in through_router.config_detail
    assert "oversized-image-error" not in through_router.config_detail
    assert direct.claude_route is None
    assert "route=" not in direct.config_detail
    assert "oversized-image-error" in direct.config_detail
    assert replace(through_router, claude_route=None).config_detail == direct.config_detail
    assert through_router.config_hash != direct.config_hash
    assert len(f"dpi=300;{through_router.config_hash}") <= 200

    class Elsewhere(ClaudeOn):
        claude_reader_provider = "bedrock"

    with pytest.raises(ValueError, match="GV_CLAUDE_READER_PROVIDER"):
        configured_slot_reader(Elsewhere(), form, environ=FRACTION_ENV)


def test_box_numbers_are_drawn_in_a_real_typeface_so_three_never_reads_as_eight() -> None:
    """Input: the digit masks the numbered-rows picture draws. Outcome: every digit's mask is
    distinct and a 3 has no closed left side. Why: in a proof run both Claude readers read the
    3x5-dot "3" tag as "8", a box that does not exist, and the page went to the reviewer."""
    from workflow.slot_reader import _digit_mask

    width, height = 18, 30
    masks = {digit: _digit_mask(digit, width, height) for digit in range(10)}
    assert all(any(any(row) for row in mask) for mask in masks.values())
    for first in range(10):
        for second in range(first + 1, 10):
            differing = sum(
                a != b
                for row_a, row_b in zip(masks[first], masks[second], strict=True)
                for a, b in zip(row_a, row_b, strict=True)
            )
            assert differing > width * height // 20, (first, second)

    def left_ink(mask: tuple[tuple[bool, ...], ...]) -> int:
        lower_left = [row[: width // 3] for row in mask[height // 2 + 2 : height - 4]]
        return sum(sum(row) for row in lower_left)

    assert left_ink(masks[8]) > left_ink(masks[3])


# --- #1086: a row of N unlabelled pieces read through the vendor's `X"(N EQ)` chain ---------------

#: The glyph sheet's chain, drawn to scale (0.24" a point): pieces 12", 24" as two equal shares,
#: 12", overall 48". Invented values.
EQ_TEXTS: dict[int | None, str] = {0: '12"', 1: '24"(2 EQ)', 2: '12"', None: '48"'}


def _blank_row(
    chain: Any, *, pieces: int, shift: Decimal = Decimal(0), y: Decimal | None = None
) -> Any:
    """The same run as `chain`, `pieces` equal pieces with nothing printed on them, drawn below it
    in the same pasted drawing (or at `y`), its ends moved by `shift`."""
    from itertools import pairwise

    from extraction.geometry.rows import Slot

    x0, x1 = chain.x0 + shift, chain.x1 + shift
    line = chain.y + 40 if y is None else y
    ticks = tuple(x0 + (x1 - x0) * i / pieces for i in range(pieces + 1))
    slots = tuple(
        Slot(
            index=i,
            x0=a,
            x1=b,
            box=Box(a, line - 16, b, line + 16),
            stored=chain.slots[0].stored,
            labels=(),
        )
        for i, (a, b) in enumerate(pairwise(ticks))
    )
    return replace(chain, y=line, ticks=ticks, slots=slots, overall=None, rank=1)


def _equal_share_read(
    texts: Mapping[int | None, str] = EQ_TEXTS,
    *,
    pieces: int = 2,
    shift: Decimal = Decimal(0),
    y: Decimal | None = None,
    blank_text: str = "",
) -> tuple[PageSlotResult, FakeReaders, Any, Any]:
    """Both readers choose the blank row (box 1); the chain with its overall is box 2."""
    from workflow.slot_reader import _claude_span_plan

    page = slot_page(sheets.sheet(sheets.glyph_labels()))
    chain = replace(page.rows.candidates.rows.candidates[0], rank=2)
    blank = _blank_row(chain, pieces=pieces, shift=shift, y=y)
    page = replace(
        page,
        rows=replace(
            page.rows,
            candidates=replace(
                page.rows.candidates,
                rows=PageRows(candidates=(blank, chain), rejected=()),
            ),
        ),
    )
    chain_plan = _claude_span_plan(
        page,
        plan_slots(
            page.rows.candidates.rows,
            page.rows.ink,
            settings=E2_CROP_SETTINGS,
            row_settings=MEASURED_SETTINGS,
            selected_row=chain,
            row_choice_made=True,
        ),
    )
    chain_crops = {
        _crop_png(
            page.rendered, _pixels(page.rows, owner.labels[0].crop, page.rendered)
        ): texts.get(owner.index, "")
        for owner in (*chain_plan.slots, *((chain_plan.overall,) if chain_plan.overall else ()))
    }
    readers = FakeReaders(lambda _model, png: chain_crops.get(png, blank_text))
    (result,) = read_slot_pages(
        [page],
        runtime=runtime(readers, claude_row_reader=True),
        record_attempt=lambda _attempt: None,
    )
    return result, readers, blank, chain


def test_blank_pieces_are_read_through_the_equal_share_chain_with_the_same_ends() -> None:
    result, readers, blank, chain = _equal_share_read()

    assert result.row_choice_number == 1, "the readers' own choice is kept on record"
    assert result.read_through is blank
    assert result.plan.row is chain
    values = [slot.outcome.value.exact if slot.outcome.value else None for slot in result.slots]
    assert values == [12, 24, 12]
    assert all(slot.outcome.state is LabelState.SEALED for slot in result.slots)
    assert result.overall is not None and result.overall.outcome.value is not None
    assert result.overall.outcome.value.exact == 48
    assert {p.field_key for p in result.mapping.proposals} >= {OVERALL_FIELD, PIECE_FIELD}
    # Each reader read the blank row's two pieces, then the chain's three pieces and overall.
    assert len(readers.requests) == 2 * (2 + 4)


def test_a_share_count_that_is_not_the_blank_piece_count_changes_nothing() -> None:
    result, _readers, blank, _chain = _equal_share_read(EQ_TEXTS | {1: '24"(3 EQ)'})

    assert result.read_through is None
    assert result.plan.row is blank
    assert all(slot.outcome.reason_code == "no-label" for slot in result.slots)
    assert not result.mapping.proposals


def test_blank_pieces_whose_ends_are_off_the_chains_are_never_read_through_it() -> None:
    off = MEASURED_SETTINGS.overall_end_pt + Decimal("0.5")
    result, readers, blank, _chain = _equal_share_read(shift=off)

    assert result.read_through is None
    assert result.plan.row is blank
    assert len(readers.requests) == 2 * 2, "the chain is not even read"


def test_blank_pieces_in_another_pasted_drawing_are_never_read_through_the_chain() -> None:
    # The stamp's drawing ends at 250 points from the top of the page; this row lies below it.
    result, readers, blank, _chain = _equal_share_read(y=Decimal(280))

    assert result.read_through is None
    assert result.plan.row is blank
    assert len(readers.requests) == 2 * 2


def test_a_chosen_row_with_anything_read_on_it_is_never_read_through_the_chain() -> None:
    result, readers, blank, _chain = _equal_share_read(blank_text='9"')

    assert result.read_through is None
    assert result.plan.row is blank
    assert len(readers.requests) == 2 * 2


def test_an_unread_end_piece_on_the_chain_changes_nothing() -> None:
    result, _readers, blank, _chain = _equal_share_read(EQ_TEXTS | {0: ""})

    assert result.read_through is None
    assert result.plan.row is blank
    assert not result.mapping.proposals


def test_a_chain_without_equal_shares_changes_nothing() -> None:
    result, _readers, blank, _chain = _equal_share_read(EQ_TEXTS | {1: '24"'})

    assert result.read_through is None
    assert result.plan.row is blank


def test_a_chain_read_through_persists_its_rows_rank_and_the_chosen_rows(session: Any) -> None:
    from workflow.slot_reader import CLAUDE_SPAN_PROMPT_ID, persist_slot_readings

    result, _readers, blank, chain = _equal_share_read()
    revision, version, page_row, run = _scaffold(session)
    result = replace(result, page_id=page_row.id, document_version_id=version.id)

    persist_slot_readings(
        session,
        package_revision_id=revision.id,
        extraction_run_id=run.id,
        reader_ids=(OPUS, SONNET),
        results=[result],
        prompt_id=CLAUDE_SPAN_PROMPT_ID,
    )

    found = _by_slot(session, run)
    for key in ("slot:0", "slot:1", "slot:2", "slot:overall"):
        flags = found[key].ambiguity_flags
        assert f"row-rank:{chain.rank}" in flags
        assert f"equal-shares-for-row-rank:{blank.rank}" in flags
        assert found[key].corroboration_status == "CORROBORATED"
    assert f"row-rank:{chain.rank}" in found["walls"].ambiguity_flags
