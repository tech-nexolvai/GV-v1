"""Phase 3's try-again in the reading (#1309): up to three smarter tries, all off by default.

Verification for the try-again wiring in `workflow/slot_reader.py` (the line finder's and the number
reader's tries, the allowance, the records), `workflow/pariksha_run_check.py` (the tally and the
fault) and `eval/pariksha/score.py` (`--with-try-again`). Synthetic sheets, invented values and fake
readers only; no network, no model call.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from extraction.form_reader.bedrock import AttemptUsage
from extraction.geometry.rows import MEASURED_SETTINGS, PageRows
from extraction.slot_reader.bedrock import ROW_TIEBREAK_PROMPT_ID
from extraction.slot_reader.runs import E2_CROP_SETTINGS, plan_slots
from extraction.slot_reader.seal import LabelState
from extraction.slot_reader.stop import ReadingStop
from extraction.slot_reader.try_again import (
    AKSHARA_SEAL,
    REKHA_OTHER_LINES,
    REKHA_PRINTED_NUMBERS,
    REKHA_READERS_AGREE,
    TRY_AGAIN_GATES,
    TryRecord,
)
from tests.workflow.test_reader_reuse import TRANSFORM, MemoryStore, PricedRates
from tests.workflow.test_slot_reader import (
    OPUS,
    SONNET,
    TEXTS,
    _scaffold,
    claude_runtime,
    named_sheet,
    session,
    slot_page,
)
from workflow.reader_reuse import StoredReaderAnswers, question_identity
from workflow.slot_reader import (
    PageSlotResult,
    SlotPage,
    SlotReaderRuntime,
    _claude_span_plan,
    _focused_rows_png,
    _marked_close_up_png,
    _numbered_rows_png,
    _pixels,
    _row_area_png,
    _row_x_px,
    persist_slot_readings,
    read_slot_pages,
)

pytest_plugins = ("tests.app.postgres_fixture",)

__all__ = ["session"]

ALL_GATES = frozenset(TRY_AGAIN_GATES)
FIRST_ROW_TEXT = "Numbered coloured boxes mark candidate"
TIE_BREAK_TEXT = "second, closer look"
#: A span answer that says "not sure this label belongs to the span".
UNSURE = "<unsure>"


def _row(row: int, kind: str = "row") -> dict[str, object]:
    return {"row": row, "kind": kind, "also": [], "why": "invented"}


class TryReaders:
    """Two fake Claude readers that tell a first-round question from a try.

    `rows(model)` answers the first row question and `tie_break(model, shown_png)` the try-again
    one. A span question is answered by `span(model, owner, first_round)`: the owner (slot index,
    `None` for the overall) is known by Picture 1, which a try keeps byte for byte; the round by
    whether the close-up is the first one. `None` is a malformed reply (the reader abstains).
    `usage` is what each kind of call reports, so a test can spend the allowance.
    """

    def __init__(
        self,
        page: SlotPage,
        *,
        rows: Callable[[str], Mapping[str, object]] = lambda _model: _row(1),
        tie_break: Callable[[str, bytes], Mapping[str, object]] = lambda _model, _png: _row(1),
        span: Callable[[str, int | None, bool], str | None] | None = None,
        try_usage: tuple[int, int] = (10, 5),
    ) -> None:
        self.rows = rows
        self.tie_break = tie_break
        self.span = span or (lambda _model, owner, _first: TEXTS.get(owner, '2"'))
        self.try_usage = try_usage
        self.lock = threading.Lock()
        self.row_requests: list[tuple[str, bytes]] = []
        self.tie_break_requests: list[tuple[str, bytes]] = []
        self.span_requests: list[tuple[str, int | None, bool, tuple[bytes, ...]]] = []
        self.other_requests = 0
        plan = _claude_span_plan(
            page,
            plan_slots(
                page.rows.candidates.rows,
                page.rows.ink,
                settings=E2_CROP_SETTINGS,
                row_settings=MEASURED_SETTINGS,
                selected_row=page.rows.candidates.rows.candidates[0],
                row_choice_made=True,
            ),
        )
        row_x = _row_x_px(page, plan)
        self.owner_by_view: dict[bytes, int | None] = {}
        self.first_close_ups: set[bytes] = set()
        for owner in (*plan.slots, *((plan.overall,) if plan.overall else ())):
            box = _pixels(page.rows, owner.labels[0].box, page.rendered)
            crop = _pixels(page.rows, owner.labels[0].crop, page.rendered)
            self.owner_by_view[_row_area_png(page.rendered, row_x, box)] = owner.index
            self.first_close_ups.add(
                _marked_close_up_png(page.rendered, crop, box, max_long_edge=2576)
            )

    def for_current_thread(self) -> TryReaders:
        return self

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        model = kwargs["modelId"]
        content = kwargs["messages"][0]["content"]
        pictures = tuple(part["image"]["source"]["bytes"] for part in content if "image" in part)
        texts = " ".join(part.get("text", "") for part in content)
        usage = {"inputTokens": 10, "outputTokens": 5}
        if TIE_BREAK_TEXT in texts:
            with self.lock:
                self.tie_break_requests.append((model, pictures[0]))
            reply: object = dict(self.tie_break(model, pictures[0]))
            usage = {"inputTokens": self.try_usage[0], "outputTokens": self.try_usage[1]}
        elif FIRST_ROW_TEXT in texts:
            with self.lock:
                self.row_requests.append((model, pictures[0]))
            reply = dict(self.rows(model))
        elif "hold-only safety question" in texts:
            with self.lock:
                self.other_requests += 1
            reply = {"contains_tall_appliance": False, "stone_ends": "unsure", "why": "invented"}
        elif (
            "wall" in texts.lower() and len(pictures) == 2 and pictures[0] not in self.owner_by_view
        ):
            with self.lock:
                self.other_requests += 1
            reply = {
                "left": "unsure",
                "right": "unsure",
                "behind": "unsure",
                "view": "elevation",
                "left_evidence": "",
                "right_evidence": "",
                "behind_evidence": "",
            }
        else:
            owner = self.owner_by_view[pictures[0]]
            first = pictures[1] in self.first_close_ups
            with self.lock:
                self.span_requests.append((model, owner, first, pictures))
            text = self.span(model, owner, first)
            if not first:
                usage = {"inputTokens": self.try_usage[0], "outputTokens": self.try_usage[1]}
            if text is None:
                return {
                    "stopReason": "end_turn",
                    "output": {"message": {"content": [{"text": "not json"}]}},
                    "usage": usage,
                }
            reply = {
                "text": "" if text == UNSURE else text,
                "stacked": False,
                "combined": False,
                "readable": True,
                "no_dimension": not text,
                "belongs": "unsure" if text == UNSURE else "yes" if text else "no",
            }
        return {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": json.dumps(reply)}]}},
            "usage": usage,
        }

    def tries(self) -> list[tuple[str, int | None, tuple[bytes, ...]]]:
        return [(m, o, p) for m, o, first, p in self.span_requests if not first]


def _page(**changes: Any) -> SlotPage:
    return replace(slot_page(named_sheet()), transform=TRANSFORM, **changes)


def _two_row_page() -> SlotPage:
    """The named sheet with two numbered lines (the second a copy of the first)."""
    page = _page()
    first = page.rows.candidates.rows.candidates[0]
    return _with_rows(page, (first, replace(first, rank=2)))


def _stopped_line_page() -> SlotPage:
    """Line 1 carries no printed number (Rekha's gate stops it); line 2 is the numbered chain."""
    page = _page()
    first = page.rows.candidates.rows.candidates[0]
    bare = replace(
        first, slots=tuple(replace(slot, labels=()) for slot in first.slots), overall=None
    )
    return _with_rows(page, (bare, replace(first, rank=2, overall=None)))


def _with_rows(page: SlotPage, rows: Sequence[Any]) -> SlotPage:
    return replace(
        page,
        rows=replace(
            page.rows,
            candidates=replace(
                page.rows.candidates, rows=PageRows(candidates=tuple(rows), rejected=())
            ),
        ),
    )


def _runtime(
    readers: TryReaders, gates: frozenset[str] = ALL_GATES, cap: str = "5"
) -> SlotReaderRuntime:
    configured = claude_runtime(
        readers,  # type: ignore[arg-type]
        question_packets=True,
        spend_cap_usd=Decimal(cap),
        try_again_gates=gates,
    )
    return replace(configured, form=replace(configured.form, rates=PricedRates()))  # type: ignore[arg-type]


class Events:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def record(self, events: Sequence[Any]) -> None:
        self.events.extend(events)


def _read(
    page: SlotPage,
    readers: TryReaders,
    gates: frozenset[str] = ALL_GATES,
    *,
    cap: str = "5",
    stop: ReadingStop | None = None,
    activity: Events | None = None,
    stored: Any = None,
) -> tuple[PageSlotResult, list[AttemptUsage]]:
    attempts: list[AttemptUsage] = []
    (result,) = read_slot_pages(
        [page],
        runtime=_runtime(readers, gates, cap),
        record_attempt=attempts.append,
        store=MemoryStore(),  # type: ignore[arg-type]
        stop=stop,
        activity=activity,
        stored_answers=stored,
    )
    return result, attempts


def _packets(attempts: Sequence[AttemptUsage]) -> list[tuple[str, str, str]]:
    """Every question asked: `(model, prompt, question identity)`, in a stable order."""
    return sorted(
        (attempt.model_id, attempt.prompt_id, question_identity(attempt.question_packet) or "")
        for attempt in attempts
        if attempt.question_packet is not None
    )


def _split_rows(model: str) -> Mapping[str, object]:
    return _row(1) if model == OPUS else _row(2)


def _slot0_differs(model: str, owner: int | None, first: bool) -> str | None:
    if owner == 0 and first:
        return '12"' if model == OPUS else '13"'
    return TEXTS.get(owner, '2"')


# --- off by default: nothing changes -------------------------------------------------------------


def test_with_no_gate_on_the_reading_is_exactly_as_before() -> None:
    """A split page and a held label, with the setting empty: only the first round is asked, and
    the run's identity is the one it had before the setting existed."""
    page = _two_row_page()
    off = TryReaders(page, rows=_split_rows, span=_slot0_differs)
    result, attempts = _read(page, off, frozenset())

    assert off.tie_break_requests == [] and off.tries() == []
    assert not any(":again" in str(attempt.question_packet["question_id"]) for attempt in attempts)
    assert result.row_try_again == () and result.row_first_picks == ()
    assert (
        _runtime(off, frozenset()).config_hash
        == replace(_runtime(off, frozenset()), try_again_gates=frozenset()).config_hash
    )
    assert "try-again" not in _runtime(off, frozenset()).config_detail
    assert "try-again=akshara:seal" in _runtime(off, frozenset({AKSHARA_SEAL})).config_detail


def test_a_page_with_nothing_to_try_asks_the_same_questions_with_every_gate_on() -> None:
    page = _page()
    off_readers, on_readers = TryReaders(page), TryReaders(page)
    off, off_attempts = _read(page, off_readers, frozenset())
    on, on_attempts = _read(page, on_readers)

    assert _packets(on_attempts) == _packets(off_attempts)
    assert [item.outcome for item in on.slots] == [item.outcome for item in off.slots]
    assert on.mapping == off.mapping
    assert on.row_try_again == () and all(
        label.try_again == () for item in on.slots for label in item.labels
    )


# --- the line finder -----------------------------------------------------------------------------


def test_a_split_line_choice_is_tried_again_on_only_the_two_lines_and_settled_on_the_new_pair() -> (
    None
):
    page = _two_row_page()
    readers = TryReaders(page, rows=_split_rows, tie_break=lambda _m, _png: _row(1))
    events = Events()
    result, attempts = _read(page, readers, frozenset({REKHA_READERS_AGREE}), activity=events)

    (opus_try, sonnet_try) = sorted(readers.tie_break_requests)
    assert {model for model, _ in readers.tie_break_requests} == {OPUS, SONNET}
    assert opus_try[1] == sonnet_try[1], "both readers see the same try picture"
    tie_breaks = [a for a in attempts if a.prompt_id == ROW_TIEBREAK_PROMPT_ID]
    assert len(tie_breaks) == 2
    for attempt in tie_breaks:
        packet = attempt.question_packet
        assert packet["question_id"] == "p0:row-choice:again1"
        assert packet["try_again"] == {
            "of": "p0:row-choice",
            "because": "readers-differ",
            "change": "tie-break-disputed-lines",
            "attempt": 1,
        }
        assert packet["shown_rows"] == [1, 2]
    # Settled: the line is read, on the try's pair; the first picks are kept.
    assert result.plan.row is not None and result.row_choice_number == 1
    assert result.row_choice_picks == ((OPUS, 1), (SONNET, 1))
    assert result.row_first_picks == ((OPUS, 1), (SONNET, 2))
    assert result.row_try_again == (
        TryRecord(
            "rekha", "two-readers-agree", "readers-differ", "tie-break-disputed-lines", "helped"
        ),
    )
    assert result.row_gate is not None and not result.row_gate.stopped
    told = [
        (e.kind.value, e.code, e.change, e.attempt)
        for e in events.events
        if e.item == "p0:row-choice"
    ]
    assert told == [
        ("answered", "all-answered", None, None),
        ("stopped", "readers-differ", None, None),
        ("trying_again", "readers-differ", "tie-break-disputed-lines", 1),
        ("try_again_result", "helped", None, 1),
    ]


def test_the_tie_break_picture_shows_only_the_disputed_lines() -> None:
    page = _two_row_page()
    candidates = page.rows.candidates.rows.candidates
    assert len(candidates) == 2
    one = _focused_rows_png(page, (1,), alone=False)
    both = _focused_rows_png(page, (1, 2), alone=False)
    alone = _focused_rows_png(page, (1, 2), alone=True)
    full, _ = _numbered_rows_png(page)

    from evidence.crop import decode_rgb_png
    from extraction.slot_reader.bedrock import ROW_BOX_COLOURS

    def colours(png: bytes) -> set[bytes]:
        _w, _h, rgb = decode_rgb_png(png)
        return {rgb[i : i + 3] for i in range(0, len(rgb), 3)}

    green, magenta = ROW_BOX_COLOURS[0][1], ROW_BOX_COLOURS[1][1]
    assert green in colours(one) and magenta not in colours(one)
    assert {green, magenta} <= colours(both)
    assert len({one, both, alone, full}) == 4
    for png in (one, both, alone):
        width, height, _rgb = decode_rgb_png(png)
        assert max(width, height) == 1800


def test_rekhas_gate_still_judges_a_tied_line_and_the_next_try_shows_the_numbered_lines() -> None:
    """Both pick line 1, which has no printed number: the gate stops it. The tie-break would show
    only that line, so it is not made; `other-numbered-lines` shows line 2 too, both pick it, and
    the gate passes it."""
    page = _stopped_line_page()
    readers = TryReaders(page, rows=lambda _m: _row(1), tie_break=lambda _m, _png: _row(2))
    result, attempts = _read(page, readers, frozenset({REKHA_PRINTED_NUMBERS, REKHA_OTHER_LINES}))

    keys = {
        a.question_packet["question_id"] for a in attempts if a.prompt_id == ROW_TIEBREAK_PROMPT_ID
    }
    assert keys == {"p0:row-choice:again2"}
    assert result.row_choice_number == 2 and result.plan.row is not None
    assert result.row_try_again == (
        TryRecord(
            "rekha",
            "row-has-printed-numbers",
            "no-printed-numbers",
            "other-numbered-lines",
            "helped",
        ),
    )


def test_a_tied_line_the_gate_stops_is_not_helped_and_without_its_switch_nothing_is_tried() -> None:
    page = _stopped_line_page()
    # Without `other-numbered-lines`, a stopped line has nothing a try could show.
    readers = TryReaders(page, rows=lambda _m: _row(1))
    result, _attempts = _read(page, readers, frozenset({REKHA_PRINTED_NUMBERS}))
    assert readers.tie_break_requests == [] and result.row_try_again == ()
    assert result.plan.row is None and result.row_gate is not None and result.row_gate.stopped

    # With it, the readers keep picking the stopped line: every try is judged by the gate again.
    readers = TryReaders(page, rows=lambda _m: _row(1), tie_break=lambda _m, _png: _row(1))
    result, _attempts = _read(page, readers, frozenset({REKHA_PRINTED_NUMBERS, REKHA_OTHER_LINES}))
    assert [record.outcome for record in result.row_try_again] == ["not-helped", "not-helped"]
    assert [record.change for record in result.row_try_again] == [
        "other-numbered-lines",
        "each-line-alone",
    ]
    assert result.plan.row is None and result.row_choice_number == 0


def test_a_line_finder_never_makes_a_fourth_try() -> None:
    page = _two_row_page()
    readers = TryReaders(page, rows=_split_rows, tie_break=lambda model, _png: _split_rows(model))
    result, attempts = _read(page, readers)

    keys = sorted(
        {
            a.question_packet["question_id"]
            for a in attempts
            if a.prompt_id == ROW_TIEBREAK_PROMPT_ID
        }
    )
    assert keys == ["p0:row-choice:again1", "p0:row-choice:again3"]  # no other numbered line
    assert len(readers.tie_break_requests) == 4
    assert [record.outcome for record in result.row_try_again] == ["not-helped", "not-helped"]
    assert result.plan.row is None
    # The page waits as a split on its first round's own picks.
    assert result.row_choice_picks == ((OPUS, 1), (SONNET, 2))


# --- the number reader ---------------------------------------------------------------------------


def test_a_label_the_readers_read_differently_is_asked_again_closer_and_sealed_on_the_new_pair() -> (
    None
):
    page = _page()
    readers = TryReaders(page, span=_slot0_differs)
    events = Events()
    result, attempts = _read(page, readers, frozenset({AKSHARA_SEAL}), activity=events)

    tries = readers.tries()
    assert sorted(model for model, _owner, _p in tries) == [OPUS, SONNET]
    assert {owner for _m, owner, _p in tries} == {0}
    from extraction.slot_reader.claude_output import png_size

    first_close = next(p for m, o, f, p in readers.span_requests if f and o == 0)[1]
    try_close = tries[0][2][1]
    assert png_size(try_close)[0] >= 2 * png_size(first_close)[0]
    assert tries[0][2][0] == next(p for m, o, f, p in readers.span_requests if f and o == 0)[0]
    (label,) = result.slots[0].labels
    assert label.outcome.reason_code is None and label.outcome.sealed_text == '12"'
    assert label.try_again == (
        TryRecord("akshara", "seal", "readers-differ", "closer-look-2x", "helped"),
    )
    assert label.try_answers == (f"{OPUS}:1", f"{SONNET}:1")
    assert 0 in {proposal.slot_index for proposal in result.mapping.proposals}
    packets = [a.question_packet for a in attempts if ":again" in a.question_packet["question_id"]]
    assert {p["question_id"] for p in packets} == {"p0:slot0:0:again1"}
    assert all(p["try_again"]["change"] == "closer-look-2x" for p in packets)
    told = [(e.kind.value, e.code, e.attempt) for e in events.events if e.item == "p0:slot0:0"]
    assert told == [
        ("stopped", "readers-differ", None),
        ("trying_again", "readers-differ", 1),
        ("try_again_result", "helped", 1),
    ]


def test_mixed_rounds_never_seal() -> None:
    """First round: one reader `12"`, the other `13"`. On every try the first reader abstains and the
    second says `12"`: a first-round `12"` and a try `12"` would agree, but rounds never mix."""

    def span(model: str, owner: int | None, first: bool) -> str | None:
        if owner != 0:
            return TEXTS.get(owner, '2"')
        if first:
            return '12"' if model == OPUS else '13"'
        return None if model == OPUS else '12"'

    page = _page()
    readers = TryReaders(page, span=span)
    result, _attempts = _read(page, readers, frozenset({AKSHARA_SEAL}))

    (label,) = result.slots[0].labels
    assert label.outcome.state is LabelState.REVIEW
    assert label.outcome.reason_code == "readers-differ"
    assert label.outcome.reader_texts == ((OPUS, '12"'), (SONNET, '13"'))
    assert [record.outcome for record in label.try_again] == ["not-helped"] * 3
    assert label.try_answers == ()
    assert 0 not in {proposal.slot_index for proposal in result.mapping.proposals}


def test_the_drawn_length_still_rejects_a_pair_a_try_settled() -> None:
    def span(model: str, owner: int | None, first: bool) -> str | None:
        if owner != 1:
            return TEXTS.get(owner, '2"')
        if first:
            return '24"' if model == OPUS else '25"'
        return '36"'  # both agree on the try, on a width the drawing does not draw

    page = _page()
    result, _attempts = _read(page, TryReaders(page, span=span), frozenset({AKSHARA_SEAL}))

    (label,) = result.slots[1].labels
    assert [record.outcome for record in label.try_again] == ["helped"]
    assert label.outcome.reason_code == "drawn-length" and label.outcome.value is None
    assert 1 in result.vetoed
    assert 1 not in {proposal.slot_index for proposal in result.mapping.proposals}


def test_only_the_readers_own_disagreement_or_doubt_is_tried_again() -> None:
    def span(model: str, owner: int | None, first: bool) -> str | None:
        if owner == 0:
            return '12"' if model == OPUS else None  # one reader missing: not tried
        return TEXTS.get(owner, '2"')

    page = _page()
    readers = TryReaders(page, span=span)
    result, _attempts = _read(page, readers, frozenset({AKSHARA_SEAL}))
    assert result.slots[0].outcome.reason_code == "one-reader-missing"
    assert readers.tries() == []

    # A label code's own guard holds (here: its ink could not be checked) is not tried either,
    # even when a reader was unsure: asking again cannot clear what code saw.
    unsure = TryReaders(
        page,
        span=lambda m, o, f: (UNSURE if m == OPUS else '12"') if o == 0 else TEXTS.get(o, '2"'),
    )
    result, _attempts = _read(replace(page, ink=None), unsure, frozenset({AKSHARA_SEAL}))
    assert result.slots[0].labels[0].outcome.reason_code == "unsure"
    assert unsure.tries() == []
    # With the ink checked, the same doubt is tried again.
    tried = TryReaders(
        page,
        span=lambda m, o, f: (
            (UNSURE if m == OPUS and f else '12"') if o == 0 else TEXTS.get(o, '2"')
        ),
    )
    result, _attempts = _read(page, tried, frozenset({AKSHARA_SEAL}))
    assert [r.outcome for r in result.slots[0].labels[0].try_again] == ["helped"]
    assert result.slots[0].labels[0].try_again[0].because == "unsure"


def test_a_number_reader_never_makes_a_fourth_try() -> None:
    page = _page()
    readers = TryReaders(
        page, span=lambda m, o, f: ('12"' if m == OPUS else '13"') if o == 0 else TEXTS.get(o, '2"')
    )
    result, attempts = _read(page, readers, frozenset({AKSHARA_SEAL}))

    keys = sorted(
        {
            a.question_packet["question_id"]
            for a in attempts
            if ":again" in a.question_packet["question_id"]
        }
    )
    assert keys == ["p0:slot0:0:again1", "p0:slot0:0:again2", "p0:slot0:0:again3"]
    assert [record.change for record in result.slots[0].labels[0].try_again] == [
        "closer-look-2x",
        "closer-look-3x-upright",
        "tighter-crop",
    ]
    three = [p for _m, o, p in readers.tries() if len(p) == 3]
    assert len(three) == 2, "the 3x try also shows its close-up turned upright, to both readers"


def test_a_close_up_too_large_to_zoom_is_recorded_and_never_asked() -> None:
    from workflow import slot_reader

    page = _page()
    readers = TryReaders(page, span=_slot0_differs)
    original = slot_reader._zoomed_close_up_png
    try:
        slot_reader._zoomed_close_up_png = lambda *a, **k: None  # type: ignore[assignment]
        result, _attempts = _read(page, readers, frozenset({AKSHARA_SEAL}))
    finally:
        slot_reader._zoomed_close_up_png = original  # type: ignore[assignment]
    assert readers.tries() == []
    assert [record.outcome for record in result.slots[0].labels[0].try_again] == ["cannot-zoom"] * 3


# --- the allowance -------------------------------------------------------------------------------


def test_a_used_allowance_stops_only_the_tries_and_the_reading_goes_on() -> None:
    """Every try reports a large spend ($0.16 a call): the line finder's two tie-break calls take
    the whole allowance (a tenth of the $3 cap), so the number reader's tries are never sent. The
    main reading is never stopped."""

    def span(model: str, owner: int | None, first: bool) -> str | None:
        if first and owner in (0, 2):
            return '12"' if model == OPUS else '13"'
        return TEXTS.get(owner, '2"')

    page = _two_row_page()
    readers = TryReaders(
        page,
        rows=_split_rows,
        tie_break=lambda _m, _png: _row(1),
        span=span,
        try_usage=(1000, 3000),
    )
    stop = ReadingStop()
    result, attempts = _read(page, readers, cap="3", stop=stop)

    assert not stop.stopped and stop.cause is None
    assert result.plan.row is not None, "the line a try settled is read"
    assert result.row_try_again[0].outcome == "helped"
    outcomes = {
        item.owner.index: [record.outcome for record in item.labels[0].try_again]
        for item in result.slots
    }
    assert outcomes[0] == ["allowance-used"] and outcomes[2] == ["allowance-used"]
    assert readers.tries() == []
    # The rest of the reading went on: the other pieces are read and sealed, the walls asked.
    assert result.slots[1].outcome.reason_code is None and result.walls is not None
    spent = [a.output_tokens for a in attempts if ":again" in str(a.question_packet["question_id"])]
    assert spent == [3000, 3000], "only the two tie-break calls were made"


# --- reuse ---------------------------------------------------------------------------------------


def test_a_try_never_takes_the_first_answer_and_a_rerun_reuses_the_tries_at_no_cost(
    session: Any,
) -> None:
    from workflow.stages import DatabaseStages, _SpendMeter

    _revision, version, page_row, run = _scaffold(session)
    page = replace(_two_row_page(), page_id=page_row.id, document_version_id=version.id)

    def read_and_record(readers: TryReaders) -> tuple[PageSlotResult, list[AttemptUsage], Any]:
        result, attempts = _read(page, readers, stored=StoredReaderAnswers(session))
        configured = _runtime(readers)
        stages = object.__new__(DatabaseStages)
        stages._form_reader = configured.form
        stages._meter = _SpendMeter(cap_micros=10_000_000)
        stages._record_reader_attempts(session, run.id, attempts)
        session.flush()
        return result, attempts, stages._meter

    first, first_attempts, first_meter = read_and_record(
        TryReaders(page, rows=_split_rows, tie_break=lambda _m, _p: _row(1), span=_slot0_differs)
    )
    assert first.row_try_again[0].helped and first.slots[0].labels[0].try_again[0].helped
    try_packets = [
        a.question_packet for a in first_attempts if ":again" in a.question_packet["question_id"]
    ]
    first_packets = [
        a.question_packet
        for a in first_attempts
        if a.question_packet["question_id"] in ("p0:slot0:0", "p0:row-choice")
    ]
    assert try_packets and first_packets
    assert {question_identity(p) for p in try_packets}.isdisjoint(
        {question_identity(p) for p in first_packets}
    ), "a try is never the identical question, so a stored first answer is never its answer"
    assert first_meter.spent_micros > 0

    # A re-run with readers that would answer everything differently asks nothing at all.
    again = TryReaders(
        page,
        rows=lambda _m: _row(0, "no_countertop"),
        tie_break=lambda _m, _p: _row(2),
        span=lambda *_a: '99"',
    )
    second, second_attempts, second_meter = read_and_record(again)
    assert again.row_requests == [] and again.tie_break_requests == []
    assert again.span_requests == [] and again.other_requests == 0
    assert second.row_try_again == first.row_try_again
    assert second.row_choice_picks == first.row_choice_picks == ((OPUS, 1), (SONNET, 1))
    assert second_meter.spent_micros == 0
    assert second.slots[0].labels[0].try_again == first.slots[0].labels[0].try_again
    assert second.slots[0].outcome == first.slots[0].outcome
    assert len(second_attempts) == len(first_attempts)


def test_try_pictures_are_the_same_bytes_on_every_build() -> None:
    page = _two_row_page()
    readers_one = TryReaders(
        page, rows=_split_rows, tie_break=lambda m, _p: _split_rows(m), span=_slot0_differs
    )
    readers_two = TryReaders(
        page, rows=_split_rows, tie_break=lambda m, _p: _split_rows(m), span=_slot0_differs
    )
    _one, one = _read(page, readers_one)
    _two, two = _read(page, readers_two)

    def tries(attempts: Sequence[AttemptUsage]) -> list[tuple[str, str]]:
        return sorted(
            (
                str(a.question_packet["question_id"]),
                json.dumps(a.question_packet["images"], sort_keys=True),
            )
            for a in attempts
            if ":again" in str(a.question_packet["question_id"])
        )

    assert tries(one) and tries(one) == tries(two)


# --- what is recorded ----------------------------------------------------------------------------


def test_the_tries_are_kept_on_the_candidates_and_counted_by_the_run_check(session: Any) -> None:
    from sqlalchemy import select

    from app.models.evidence import ObservationCandidate
    from workflow.pariksha_run_check import TRY_AGAIN_UNPAIRED, gate_tally, try_again_faults

    revision, version, page_row, run = _scaffold(session)
    page = replace(_two_row_page(), page_id=page_row.id, document_version_id=version.id)
    readers = TryReaders(
        page, rows=_split_rows, tie_break=lambda _m, _p: _row(1), span=_slot0_differs
    )
    result, _attempts = _read(page, readers)
    persist_slot_readings(
        session,
        package_revision_id=revision.id,
        extraction_run_id=run.id,
        reader_ids=(OPUS, SONNET),
        results=[result],
    )
    session.flush()
    flags = {
        flag
        for candidate in session.scalars(
            select(ObservationCandidate).where(ObservationCandidate.extraction_run_id == run.id)
        )
        for flag in candidate.ambiguity_flags or ()
    }
    assert {
        "try-again:rekha:two-readers-agree:readers-differ:tie-break-disputed-lines:helped",
        "try-again-first-pick:opus-5-5:1",
        "try-again-first-pick:sonnet-5-5:2",
        "try-again:akshara:seal:readers-differ:closer-look-2x:helped",
        f"try-again-answer:{OPUS}:1",
        f"try-again-answer:{SONNET}:1",
    } <= flags

    tally = {(count.agent.value, count.gate): count for count in gate_tally(session, revision.id)}
    assert (
        tally[("Rekha", "two-readers-agree")].tried_again,
        tally[("Rekha", "two-readers-agree")].helped,
    ) == (1, 1)
    assert (tally[("Akshara", "seal")].tried_again, tally[("Akshara", "seal")].helped) == (1, 1)
    assert tally[("Akshara", "seal")].to_dict()["tried_again"] == 1
    assert try_again_faults(session, revision.id) == []

    # A sealed reading whose try answers are not two, from two readers, is a fault.
    tampered_revision, tampered_version, tampered_page, tampered_run = _scaffold(session)
    label = result.slots[0].labels[0]
    one_answer = replace(label, try_answers=(f"{OPUS}:1",))
    tampered = replace(
        result,
        page_id=tampered_page.id,
        document_version_id=tampered_version.id,
        slots=(replace(result.slots[0], labels=(one_answer,)), *result.slots[1:]),
        owner_candidate_ids={key: uuid4() for key in result.owner_candidate_ids},
        wall_candidate_id=uuid4(),
    )
    persist_slot_readings(
        session,
        package_revision_id=tampered_revision.id,
        extraction_run_id=tampered_run.id,
        reader_ids=(OPUS, SONNET),
        results=[tampered],
    )
    session.flush()
    (fault,) = try_again_faults(session, tampered_revision.id)
    assert fault.code == TRY_AGAIN_UNPAIRED and fault.agent.value == "Akshara"
    assert fault.ids == (("candidate", str(tampered.owner_candidate_ids["slot:0"])),)


def test_a_page_no_try_settled_keeps_its_first_picks_and_its_tries_on_its_record(
    session: Any,
) -> None:
    from sqlalchemy import select

    from app.models.evidence import ObservationCandidate

    revision, version, page_row, run = _scaffold(session)
    page = replace(_two_row_page(), page_id=page_row.id, document_version_id=version.id)
    readers = TryReaders(page, rows=_split_rows, tie_break=lambda m, _p: _split_rows(m))
    result, _attempts = _read(page, readers)
    persist_slot_readings(
        session,
        package_revision_id=revision.id,
        extraction_run_id=run.id,
        reader_ids=(OPUS, SONNET),
        results=[result],
        store=None,
    )
    (record,) = session.scalars(
        select(ObservationCandidate).where(ObservationCandidate.extraction_run_id == run.id)
    )
    flags = record.ambiguity_flags
    assert "row-pick:opus-5-5:1" in flags and "row-pick:sonnet-5-5:2" in flags
    assert [flag for flag in flags if flag.startswith("try-again:")] == [
        "try-again:rekha:two-readers-agree:readers-differ:tie-break-disputed-lines:not-helped",
        "try-again:rekha:two-readers-agree:readers-differ:each-line-alone:not-helped",
    ]


def test_the_setting_reaches_the_reader_and_needs_the_claude_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.workflow.test_slot_reader import FRACTION_ENV
    from workflow.slot_reader import configured_slot_reader

    settings = SimpleNamespace(slot_reader_enabled=True, try_again_gates="akshara:seal")
    with pytest.raises(ValueError, match="GV_CLAUDE_READER_ENABLED"):
        configured_slot_reader(settings, form=object(), environ=FRACTION_ENV)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown gates"):
        configured_slot_reader(
            SimpleNamespace(slot_reader_enabled=True, try_again_gates="akshara:nope"),
            form=object(),  # type: ignore[arg-type]
            environ=FRACTION_ENV,
        )


# --- the allowance funds real tries --------------------------------------------------------------


def _largest_picture(width: int, height: int) -> bytes:
    from evidence.crop import encode_png

    return encode_png(width, height, bytes(width * height * 3))


@pytest.mark.parametrize("pages", [1, 35, 60])
def test_a_realistic_cap_funds_a_try_pair_at_its_worst_case(pages: int) -> None:
    """The coordinator's check (2026-10-15): a tenth of a fresh reading's cap must pay for at least
    one try of each agent at its worst-case reservation, with the published prices. The smallest
    cap is the page-scaled budget's minimum ($3, so $0.30 for tries); a tie-break pair reserves about
    $0.14 and the largest number-reader pair (three pictures at the readers' limits) about $0.19."""
    from app.runs.rates import load_model_rates
    from extraction.slot_reader.anthropic import BatchSpendGuard, _reserved_input_tokens
    from extraction.slot_reader.bedrock import build_crop_request, build_row_request
    from extraction.slot_reader.try_again import TryAgainGuard, try_again_allowance
    from workflow.ai_budget import AiBudget

    rates = load_model_rates("deploy/model_rates.us-east-1.json")
    cap = AiBudget().for_pages(pages)
    tie_break = _largest_picture(1800, 1800)
    close_up = _largest_picture(2576, 1736)  # 92 x 62 tiles: within 4,784 visual tokens
    for build in (
        lambda model: build_row_request(
            model_id=model, page_png=tie_break, max_tokens=3000, tiebreak=True
        ),
        lambda model: build_crop_request(
            model_id=model,
            crop_png=close_up,
            max_tokens=3000,
            full_view_png=tie_break,
            grounded_claude=True,
            upright_png=_largest_picture(1736, 2576),
        ),
    ):
        guard = TryAgainGuard(try_again_allowance(cap), rates, BatchSpendGuard(cap, rates))
        for model in (OPUS, SONNET):
            guard.reserve(model, _reserved_input_tokens(build(model)), 3000)  # never refused
