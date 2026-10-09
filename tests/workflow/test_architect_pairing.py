"""Pairing the architect's dimensions with the vendor's chosen row: code, then both AIs (#1053).

Verification for `workflow/architect_pairing.py` (no database here; records and the read path are
in `test_architect_pairing_records.py`). Invented geometry only: the vendor's view at 2 pt per inch,
the architect's at 3/2 pt per inch, pieces of 30, 24 and 18 inches. No client value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from evidence.crop import RenderedPage, decode_rgb_png
from extraction.geometry.rows import Box
from extraction.ink import InkClass
from extraction.slot_reader.bedrock import ARCH_PAIR_PROMPT_ID, ArchPairAnswer, CropJob
from extraction.slot_reader.claude_output import picture_fits
from extraction.slot_reader.seal import LabelState
from units.measurement import Measurement, Unit
from workflow.architect_pairing import (
    AI_DRAWN_LENGTH_BAND,
    MEASURED_PAIRING_SETTINGS,
    ArchitectPageInput,
    ArchitectPairing,
    ArchitectRowInput,
    ArchitectSpanInput,
    DecidedPair,
    PairQuestion,
    VendorPiece,
    VendorRowInput,
    pair_by_code,
    pair_picture,
    resolve_answers,
    vendor_row_input,
    vendor_scale,
)
from workflow.slot_reader import PageSlotResult, SlotPage

OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
VENDOR_PT = Fraction(2)
ARCH_PT = Fraction(3, 2)
WIDTHS = (30, 24, 18)


def _ticks(start: int, widths: Sequence[int], scale: Fraction) -> list[Decimal]:
    at = [Fraction(start)]
    for width in widths:
        at.append(at[-1] + width * scale)
    return [Decimal(value.numerator) / Decimal(value.denominator) for value in at]


def vendor(
    widths: Sequence[int] = WIDTHS,
    *,
    sealed: Sequence[int | None] | None = None,
    held: str | None = None,
    overall: bool = True,
) -> VendorRowInput:
    ticks = _ticks(100, widths, VENDOR_PT)
    values = list(widths) if sealed is None else list(sealed)
    sealed_values = [None if value is None else Fraction(value) for value in values]
    pieces = tuple(
        VendorPiece(
            slot_index=k,
            x0_pt=ticks[k],
            x1_pt=ticks[k + 1],
            sealed_inches=sealed_values[k],
            box_px=(int(ticks[k]), 300, int(ticks[k + 1]), 320),
        )
        for k in range(len(widths))
    )
    return VendorRowInput(
        pieces=pieces, overall_x=(ticks[0], ticks[-1]) if overall else None, held_reason=held
    )


def arch_row(
    rank: int,
    widths: Sequence[int],
    *,
    start: int = 400,
    outline: Sequence[bool | None] | None = None,
    held: Mapping[int, str] | None = None,
    scale: Fraction | None = ARCH_PT,
) -> ArchitectRowInput:
    ticks = _ticks(start, widths, ARCH_PT)
    flags = list(outline) if outline is not None else [True] * len(widths)
    holds = held or {}
    return ArchitectRowInput(
        rank=rank,
        view_annotation_index=1,
        pt_per_inch=scale,
        scale_reason="test scale",
        spans=tuple(
            ArchitectSpanInput(
                index=k,
                x0_pt=ticks[k],
                x1_pt=ticks[k + 1],
                on_outline=flags[k],
                text=f"span {rank}.{k}",
                held_reason=holds.get(k),
                candidate_id=uuid4(),
                box_px=(int(ticks[k]), 50 + 20 * rank, int(ticks[k + 1]), 60 + 20 * rank),
            )
            for k in range(len(widths))
        ),
    )


def page_input(*rows: ArchitectRowInput) -> ArchitectPageInput:
    return ArchitectPageInput(
        page_id=uuid4(),
        architect_run_id=uuid4(),
        rows=rows,
        view_boxes=(
            Box(Decimal(380), Decimal(20), Decimal(560), Decimal(200)),
            Box(Decimal(80), Decimal(220), Decimal(300), Decimal(380)),
        ),
    )


# --- vendor scale and row ------------------------------------------------------------------------


def test_the_vendor_scale_comes_from_sealed_pieces_only() -> None:
    assert vendor_scale(vendor().pieces) == VENDOR_PT
    # A value that is not sealed (None) never counts, whatever it would have said.
    assert vendor_scale(vendor(sealed=(30, None, 18)).pieces) == VENDOR_PT
    assert vendor_scale(vendor(sealed=(30, None, None)).pieces) is None
    assert vendor_scale(vendor(sealed=(None, None, None)).pieces) is None


def _owner_result(index: int, x0: int, x1: int, *, state: LabelState, ink: InkClass) -> Any:
    return SimpleNamespace(
        owner=SimpleNamespace(index=index, x0=Decimal(x0), x1=Decimal(x1)),
        outcome=SimpleNamespace(
            state=state,
            value=Measurement(Fraction((x1 - x0) // 2), Unit.INCH, None),
            label_index=0,
        ),
        labels=(SimpleNamespace(outcome=SimpleNamespace(ink=ink)),),
        band_px=(x0, 300, x1, 320),
    )


def _result(owners: Sequence[Any], *, vetoed: tuple[int | None, ...] = ()) -> PageSlotResult:
    plan = SimpleNamespace(
        row=object(),
        slots=tuple(owner.owner for owner in owners),
        overall=SimpleNamespace(index=None, x0=owners[0].owner.x0, x1=owners[-1].owner.x1),
    )
    return PageSlotResult(
        page_index=0,
        page_id=uuid4(),
        document_version_id=uuid4(),
        plan=plan,  # type: ignore[arg-type]
        slots=tuple(owners),
        overall=None,
        mapping=None,  # type: ignore[arg-type]
        vetoed=vetoed,
    )


def test_unsealed_vetoed_and_gv_ink_readings_never_give_the_vendor_scale() -> None:
    owners = [
        _owner_result(0, 100, 160, state=LabelState.SEALED, ink=InkClass.VENDOR),
        _owner_result(1, 160, 208, state=LabelState.REVIEW, ink=InkClass.VENDOR),
        _owner_result(2, 208, 244, state=LabelState.SEALED, ink=InkClass.GV),
        _owner_result(3, 244, 284, state=LabelState.SEALED, ink=InkClass.VENDOR),
    ]

    row = vendor_row_input(_result(owners, vetoed=(3,)))

    assert row is not None
    assert [piece.sealed_inches for piece in row.pieces] == [Fraction(30), None, None, None]
    assert vendor_scale(row.pieces) is None, "one sealed vendor-ink piece gives no scale"
    assert row.overall_x == (Decimal(100), Decimal(284))


# --- 1. code ------------------------------------------------------------------------------------


def test_an_exact_fit_pairs_each_architect_span_with_its_vendor_piece_by_code() -> None:
    row = arch_row(1, WIDTHS)

    outcome, raw = pair_by_code(vendor(), page_input(row), MEASURED_PAIRING_SETTINGS)

    assert raw is not None
    assert (outcome.source, outcome.status) == ("code", "paired")
    assert outcome.pairs == tuple(
        DecidedPair("piece", span.candidate_id, (k,))  # type: ignore[arg-type]
        for k, span in enumerate(row.spans)
    )
    assert outcome.details["code"]["support"] == 4  # type: ignore[index]
    assert outcome.details["vendor_pt_per_inch"] == "2"


def test_printed_architect_text_never_changes_the_code_pairing() -> None:
    row = arch_row(1, WIDTHS)
    renamed = replace(row, spans=tuple(replace(span, text="99' - 9\"") for span in row.spans))

    first, _ = pair_by_code(vendor(), page_input(row), MEASURED_PAIRING_SETTINGS)
    second, _ = pair_by_code(vendor(), page_input(renamed), MEASURED_PAIRING_SETTINGS)

    assert first.pairs == second.pairs
    assert first.status == second.status


def test_a_held_span_gives_geometry_but_never_becomes_a_compared_pair() -> None:
    row = arch_row(1, WIDTHS, held={1: "the drawn length disagrees with the label"})

    outcome, _ = pair_by_code(vendor(), page_input(row), MEASURED_PAIRING_SETTINGS)

    assert outcome.status == "paired", "its ticks still align the row"
    assert row.spans[1].candidate_id not in {pair.architect_candidate_id for pair in outcome.pairs}
    assert len(outcome.pairs) == 2
    entries: Any = outcome.details["excluded"]
    excluded = {entry["slot"]: entry["reason"] for entry in entries}
    assert "held" in str(excluded[1])


def test_a_centre_line_span_never_pairs_even_when_it_lines_up() -> None:
    row = arch_row(1, WIDTHS, outline=(True, False, True))

    outcome, _ = pair_by_code(vendor(), page_input(row), MEASURED_PAIRING_SETTINGS)

    assert row.spans[1].candidate_id not in {pair.architect_candidate_id for pair in outcome.pairs}


def test_only_centre_lines_is_nothing_comparable_and_needs_no_ai() -> None:
    row = arch_row(1, WIDTHS, outline=(False, False, False))

    outcome, _ = pair_by_code(vendor(), page_input(row), MEASURED_PAIRING_SETTINGS)

    assert (outcome.status, outcome.pairs) == ("nothing_comparable", ())


def test_two_ticks_only_is_no_fit_for_code() -> None:
    # One unit width on the outline: two ticks, fewer than the three code needs.
    outcome, _ = pair_by_code(vendor(), page_input(arch_row(1, (72,))), MEASURED_PAIRING_SETTINGS)

    assert outcome.status == "no_fit"
    assert outcome.pairs == ()


def test_a_vendor_row_with_too_few_sealed_pieces_has_no_scale() -> None:
    outcome, _ = pair_by_code(
        vendor(sealed=(30, None, None)), page_input(arch_row(1, WIDTHS)), MEASURED_PAIRING_SETTINGS
    )

    assert outcome.status == "no_scale"


def test_the_settings_are_the_measured_ones() -> None:
    assert MEASURED_PAIRING_SETTINGS.tick_tolerance_in == Fraction(1, 2)
    assert MEASURED_PAIRING_SETTINGS.tolerance_fraction_of_smallest_bay == Fraction(1, 3)
    assert MEASURED_PAIRING_SETTINGS.minimum_coincident_ticks == 3
    assert MEASURED_PAIRING_SETTINGS.minimum_support_margin == 2
    assert AI_DRAWN_LENGTH_BAND == Fraction(1, 4)


# --- 2. both AIs --------------------------------------------------------------------------------


def question(row: ArchitectRowInput) -> PairQuestion:
    return PairQuestion(
        picture_png=b"picture",
        vendor_slots=(0, 1, 2),
        architect=row.spans,
        architect_rows=tuple(row.rank for _ in row.spans),
        packet={"packet_sha256": "f" * 64},
    )


def _resolve(
    row: ArchitectRowInput,
    first: tuple[int, tuple[int, ...]] | None,
    second: tuple[int, tuple[int, ...]] | None = None,
    *,
    vendor_row: VendorRowInput | None = None,
) -> Any:
    second = first if second is None else second
    given = [
        (model, None if pick is None else ArchPairAnswer(model, pick[0], pick[1], "same things"))
        for model, pick in ((OPUS, first), (SONNET, second))
    ]
    page = page_input(row)
    current = vendor() if vendor_row is None else vendor_row
    code, _ = pair_by_code(current, page, MEASURED_PAIRING_SETTINGS)
    return resolve_answers(question(row), given, current, page, code)


def test_identical_answers_pair_and_are_recorded_as_both_ais() -> None:
    row = arch_row(1, WIDTHS)

    outcome = _resolve(row, (0, (1, 2, 3)))

    assert (outcome.source, outcome.status) == ("both-ais", "paired")
    assert [pair.vendor_slot_indices for pair in outcome.pairs] == [(0,), (1,), (2,)]
    ai = outcome.details["ai"]
    assert ai["prompt_id"] == ARCH_PAIR_PROMPT_ID
    assert ai["picture_sha256"] and ai["packet_sha256"] == "f" * 64
    assert [answer["pieces"] for answer in ai["answers"]] == [[1, 2, 3], [1, 2, 3]]


def test_different_answers_go_to_the_reviewer_with_both_kept() -> None:
    outcome = _resolve(arch_row(1, WIDTHS), (0, (1, 2, 3)), (0, (1, 2, 0)))

    assert (outcome.source, outcome.status, outcome.pairs) == ("none", "ais-disagree", ())
    assert [answer["pieces"] for answer in outcome.details["ai"]["answers"]] == [
        [1, 2, 3],
        [1, 2, 0],
    ]


def test_a_missing_answer_is_a_refusal_never_one_readers_pairing() -> None:
    row = arch_row(1, WIDTHS)
    given = [(OPUS, ArchPairAnswer(OPUS, 0, (1, 2, 3), "x")), (SONNET, None)]
    page = page_input(row)
    code, _ = pair_by_code(vendor(), page, MEASURED_PAIRING_SETTINGS)

    outcome = resolve_answers(question(row), given, vendor(), page, code)

    assert (outcome.source, outcome.status, outcome.pairs) == ("none", "ais-refused", ())
    ai: Any = outcome.details["ai"]
    assert ai["answers"][1] == {"model_id": SONNET, "answered": False}


def test_a_pair_two_drawings_draw_at_very_different_lengths_is_dropped() -> None:
    # A1 is 30" drawn; the AIs give it V1 + V2 (54" drawn): not the same thing.
    outcome = _resolve(arch_row(1, WIDTHS), (0, (1, 1, 3)))

    assert [pair.vendor_slot_indices for pair in outcome.pairs] == [(2,)]
    assert any("quarter" in str(entry["reason"]) for entry in outcome.details["dropped"])


def test_a_real_mismatch_within_the_band_is_kept_for_the_compare() -> None:
    # The architect draws 32" where the vendor has 30": a real difference the rule must flag.
    row = arch_row(1, (32, 22, 18))

    outcome = _resolve(row, (0, (1, 2, 3)))

    assert len(outcome.pairs) == 3


def test_a_split_that_is_not_contiguous_is_dropped() -> None:
    row = arch_row(1, (30, 24, 18))

    outcome = _resolve(row, (0, (1, 0, 1)))

    assert row.spans[0].candidate_id not in {pair.architect_candidate_id for pair in outcome.pairs}
    assert any("next to each other" in str(entry["reason"]) for entry in outcome.details["dropped"])


def test_a_contiguous_split_of_one_bay_pairs_with_both_pieces() -> None:
    row = arch_row(1, (54, 18))

    outcome = _resolve(row, (0, (1, 1, 2)))

    assert [pair.vendor_slot_indices for pair in outcome.pairs] == [(0, 1), (2,)]


def test_the_whole_run_pairs_as_the_overall_with_no_vendor_pieces() -> None:
    row = arch_row(1, (72,))

    outcome = _resolve(replace(row), (1, (0, 0, 0)))

    assert outcome.pairs == (DecidedPair("overall", row.spans[0].candidate_id, ()),)  # type: ignore[arg-type]


def test_one_architect_dimension_cannot_be_the_whole_run_and_only_one_piece() -> None:
    row = arch_row(1, (72,))

    outcome = _resolve(row, (1, (1, 0, 0)))

    assert outcome.pairs == ()


def test_all_zero_answers_mean_nothing_comparable() -> None:
    outcome = _resolve(arch_row(1, WIDTHS), (0, (0, 0, 0)))

    assert (outcome.source, outcome.status, outcome.pairs) == (
        "both-ais",
        "nothing_comparable",
        (),
    )


def test_an_ai_pair_on_a_span_that_is_not_comparable_is_dropped() -> None:
    # Defence in depth: such a span is never numbered, but if it were, code drops it.
    row = arch_row(1, WIDTHS, outline=(True, False, True))

    outcome = _resolve(row, (0, (1, 2, 3)))

    assert row.spans[1].candidate_id not in {pair.architect_candidate_id for pair in outcome.pairs}
    assert any("centre-line" in str(entry["reason"]) for entry in outcome.details["dropped"])


# --- the batch -----------------------------------------------------------------------------------


def _slot_page(page_id: UUID) -> SlotPage:
    width, height = 600, 400
    rendered = RenderedPage(
        document_version_id=uuid4(),
        page_index=0,
        page_content_hash="1" * 64,
        rotation=0,
        render_failed=False,
        width_px=width,
        height_px=height,
        dpi=72,
        rgb_bytes=bytes([255]) * (width * height * 3),
    )
    rows = SimpleNamespace(to_pixels=lambda x, top: (int(x), int(top)))
    return SlotPage(
        page_index=0,
        page_id=page_id,
        document_version_id=rendered.document_version_id,
        rendered=rendered,
        rows=rows,  # type: ignore[arg-type]
        ink=None,
    )


def _sealed_owners(widths: Sequence[int] = WIDTHS) -> list[Any]:
    ticks = [int(tick) for tick in _ticks(100, widths, VENDOR_PT)]
    return [
        _owner_result(k, ticks[k], ticks[k + 1], state=LabelState.SEALED, ink=InkClass.VENDOR)
        for k in range(len(widths))
    ]


def _run_batch(
    page: ArchitectPageInput,
    *,
    ask_the_ais: bool = True,
    hold: str | None = None,
    answer: tuple[int, tuple[int, ...]] = (0, (1, 0, 0)),
) -> tuple[PageSlotResult, list[CropJob]]:
    result = replace(_result(_sealed_owners()), page_id=page.page_id)
    if hold is not None:
        result = replace(result, row_hold=SimpleNamespace(reason=hold, code="vif"))  # type: ignore[arg-type]
    asked: list[CropJob] = []

    def ask(jobs: Sequence[CropJob]) -> Mapping[tuple[str, str], object]:
        asked.extend(jobs)
        return {
            (job.key, job.model_id): ArchPairAnswer(job.model_id, answer[0], answer[1], "x")
            for job in jobs
        }

    (paired,) = ArchitectPairing(MEASURED_PAIRING_SETTINGS, {page.page_id: page}).pair(
        [result],
        [_slot_page(page.page_id)],
        ask=ask,
        readers=(OPUS, SONNET),
        ask_the_ais=ask_the_ais,
        store=None,
        effort="high",
    )
    return paired, asked


def test_code_decides_without_asking_any_reader() -> None:
    paired, asked = _run_batch(page_input(arch_row(1, WIDTHS)))

    assert asked == []
    assert paired.architect_pairing is not None
    assert paired.architect_pairing.source == "code"


def test_when_code_cannot_decide_both_readers_get_one_numbered_picture() -> None:
    paired, asked = _run_batch(page_input(arch_row(1, (30,), start=400)))

    assert [job.model_id for job in asked] == [OPUS, SONNET]
    assert all(job.arch_pair_question for job in asked)
    assert len({job.png for job in asked}) == 1, "the same picture for both"
    assert all((job.vendor_pieces, job.architect_spans) == (3, 1) for job in asked)
    assert asked[0].question_packet is not None
    assert asked[0].question_packet["prompt_id"] == ARCH_PAIR_PROMPT_ID
    assert paired.architect_pairing is not None
    assert paired.architect_pairing.source == "both-ais"
    assert [pair.vendor_slot_indices for pair in paired.architect_pairing.pairs] == [(0,)]


def test_no_reader_is_asked_without_the_claude_pair() -> None:
    paired, asked = _run_batch(page_input(arch_row(1, (30,))), ask_the_ais=False)

    assert asked == []
    assert paired.architect_pairing is not None
    assert (paired.architect_pairing.source, paired.architect_pairing.status) == (
        "code",
        "no_fit",
    )


def test_no_reader_is_asked_when_no_architect_span_could_be_compared() -> None:
    _paired, asked = _run_batch(page_input(arch_row(1, (30,), held={0: "role not decided"})))

    assert asked == []


def test_no_reader_is_asked_for_a_held_vendor_row() -> None:
    _paired, asked = _run_batch(page_input(arch_row(1, (30,))), hold="INCLUDING FIELD CUT")

    assert asked == []


def test_a_page_the_architect_reader_did_not_read_gets_no_pairing() -> None:
    result = _result(_sealed_owners())
    (paired,) = ArchitectPairing(MEASURED_PAIRING_SETTINGS, {}).pair(
        [result],
        [_slot_page(result.page_id)],
        ask=lambda _jobs: pytest.fail("no reader is asked"),
        readers=(OPUS, SONNET),
        ask_the_ais=True,
        store=None,
        effort="high",
    )

    assert paired.architect_pairing is None
    assert paired == result


def test_the_picture_marks_both_rows_and_fits_the_readers_limits() -> None:
    page = page_input(arch_row(1, WIDTHS))
    slot_page = _slot_page(page.page_id)

    png = pair_picture(
        slot_page.rendered,
        slot_page.rows.to_pixels,
        vendor().pieces,
        page.rows[0].spans,
        page.view_boxes,
    )

    width, height, rgb = decode_rgb_png(png)
    assert picture_fits(width, height, OPUS)
    colours = {bytes(rgb[i : i + 3]) for i in range(0, len(rgb), 3)}
    assert bytes((220, 20, 60)) in colours, "vendor pieces in red"
    assert bytes((0, 90, 220)) in colours, "architect spans in blue"


# --- inside the slot reader's batch --------------------------------------------------------------


def _read_named_sheet(architect: ArchitectPairing | None, page_id: UUID) -> tuple[Any, list[Any]]:
    import json

    from tests.workflow.test_slot_reader import (
        TEXTS,
        FakeReaders,
        claude_crops_to_texts,
        claude_runtime,
        named_sheet,
        slot_page,
    )
    from workflow.slot_reader import read_slot_pages

    page = replace(slot_page(named_sheet()), page_id=page_id)
    lookup = claude_crops_to_texts(page, TEXTS)

    class PairingReaders(FakeReaders):
        pair_requests: list[str]

        def _answer(self, **kwargs: Any) -> Mapping[str, Any]:
            content = kwargs["messages"][0]["content"]
            if any("SAME PHYSICAL THING" in part.get("text", "") for part in content):
                self.pair_requests.append(kwargs["modelId"])
                payload = {"overall": 1, "pieces": [0, 0, 0], "why": "A1 spans the whole run"}
                return {
                    "output": {"message": {"content": [{"text": json.dumps(payload)}]}},
                    "usage": {"inputTokens": 20, "outputTokens": 9},
                }
            return super()._answer(**kwargs)

    readers = PairingReaders(lambda _model, png: lookup.get(png, '2"'))
    readers.pair_requests = []
    attempts: list[Any] = []
    (result,) = read_slot_pages(
        [page], runtime=claude_runtime(readers), record_attempt=attempts.append, architect=architect
    )
    return result, [
        *readers.pair_requests,
        *[a for a in attempts if a.prompt_id == ARCH_PAIR_PROMPT_ID],
    ]


def _same_reading(first: Any, second: Any) -> None:
    assert first.slots == second.slots
    assert first.overall == second.overall
    assert first.mapping == second.mapping
    assert (first.row_hold, first.check_hold, first.vetoed) == (
        second.row_hold,
        second.check_hold,
        second.vetoed,
    )
    assert (first.walls is None) == (second.walls is None)
    if first.walls is not None:
        assert first.walls.outcome == second.walls.outcome


def test_pairing_inside_the_batch_changes_nothing_the_slot_reader_read() -> None:
    page_id = uuid4()
    plain, plain_pair_calls = _read_named_sheet(None, page_id)
    # The vendor's row there: 12, 24, 12 drawn at 25/6 pt per inch; the architect's the same.
    architect = page_input(arch_row(1, (12, 24, 12), start=10))
    architect = replace(architect, page_id=page_id)

    paired, pair_calls = _read_named_sheet(
        ArchitectPairing(MEASURED_PAIRING_SETTINGS, {page_id: architect}), page_id
    )

    _same_reading(plain, paired)
    assert plain.architect_pairing is None and plain_pair_calls == []
    assert paired.architect_pairing is not None
    assert (paired.architect_pairing.source, paired.architect_pairing.status) == ("code", "paired")
    assert [pair.vendor_slot_indices for pair in paired.architect_pairing.pairs] == [
        (0,),
        (1,),
        (2,),
    ]
    assert pair_calls == [], "code decided: no reader was asked"


def test_the_ai_question_rides_the_same_batch_and_changes_no_reading() -> None:
    page_id = uuid4()
    plain, _ = _read_named_sheet(None, page_id)
    architect = replace(page_input(arch_row(1, (48,), start=10)), page_id=page_id)

    paired, pair_calls = _read_named_sheet(
        ArchitectPairing(MEASURED_PAIRING_SETTINGS, {page_id: architect}), page_id
    )

    _same_reading(plain, paired)
    outcome = paired.architect_pairing
    assert outcome is not None
    assert (outcome.source, outcome.status) == ("both-ais", "paired")
    assert outcome.pairs == (
        DecidedPair("overall", architect.rows[0].spans[0].candidate_id, ()),  # type: ignore[arg-type]
    )
    asked = [call for call in pair_calls if isinstance(call, str)]
    recorded = [call for call in pair_calls if not isinstance(call, str)]
    assert sorted(asked) == [OPUS, SONNET]
    assert len(recorded) == 2 and all(attempt.question_packet for attempt in recorded)
