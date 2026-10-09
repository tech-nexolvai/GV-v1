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
from typing import Any, Literal
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


# --- 2. both AIs, and the two judgments combined -------------------------------------------------

type Pick = tuple[int, tuple[int, ...]] | tuple[int, tuple[int, ...], tuple[str, ...]]


def question(row: ArchitectRowInput) -> PairQuestion:
    return PairQuestion(
        picture_png=b"picture",
        vendor_slots=(0, 1, 2),
        architect=row.spans,
        architect_rows=tuple(row.rank for _ in row.spans),
        packet={"packet_sha256": "f" * 64},
    )


def _measures_for(pick: Pick, spans: int) -> tuple[str, ...]:
    """What the AIs say each A measures: stated, or the plain reading of their pairing."""
    if len(pick) == 3:
        return pick[2]
    overall, pieces = pick[0], pick[1]
    return tuple(
        "cabinet_run" if k == overall else "single_cabinet" if k in pieces else "unsure"
        for k in range(1, spans + 1)
    )


def answer(model: str, pick: Pick, spans: int) -> ArchPairAnswer:
    return ArchPairAnswer(model, pick[0], pick[1], "same things", _measures_for(pick, spans))


def _resolve(
    row: ArchitectRowInput,
    first: Pick | None,
    second: Pick | None | Literal["same"] = "same",
    *,
    vendor_row: VendorRowInput | None = None,
    code_paired: bool = False,
    code_status: str = "no_fit",
    unsure: tuple[tuple[str, ...], tuple[str, ...]] = ((), ()),
) -> Any:
    """Both AIs' answers combined with code. `None` is a reader that gave no answer.
    `code_paired=False` stands code's judgment down with `code_status` (by default it found no
    clear alignment), so only the AIs' pairing can be made. `unsure` is what each reader answered
    `unsure` (v3): `overall` and/or `V<k>`."""
    second = first if second == "same" else second
    given = [
        (
            model,
            (
                None
                if pick is None
                else replace(answer(model, pick, len(row.spans)), unsure=not_sure)
            ),
        )
        for (model, pick), not_sure in zip(((OPUS, first), (SONNET, second)), unsure, strict=True)
    ]
    page = page_input(row)
    current = vendor() if vendor_row is None else vendor_row
    code, _ = pair_by_code(current, page, MEASURED_PAIRING_SETTINGS)
    if not code_paired:
        code = replace(code, status=code_status, pairs=())
    return resolve_answers(question(row), given, current, page, code)


def _slots(outcome: Any) -> list[tuple[int, ...]]:
    return [pair.vendor_slot_indices for pair in outcome.pairs]


def _dropped(outcome: Any) -> str:
    return " | ".join(str(entry["reason"]) for entry in outcome.details["dropped"])


def test_identical_answers_alone_pair_and_are_recorded_as_both_ais() -> None:
    row = arch_row(1, WIDTHS)

    outcome = _resolve(row, (0, (1, 2, 3)))

    assert (outcome.source, outcome.status) == ("both-ais", "paired")
    assert _slots(outcome) == [(0,), (1,), (2,)]
    ai = outcome.details["ai"]
    assert ai["prompt_id"] == ARCH_PAIR_PROMPT_ID == "arch-pair-v3"
    assert ai["picture_sha256"] and ai["packet_sha256"] == "f" * 64
    assert [answer["pieces"] for answer in ai["answers"]] == [[1, 2, 3], [1, 2, 3]]
    assert [answer["measures"] for answer in ai["answers"]] == [["single_cabinet"] * 3] * 2


def test_code_and_both_ais_pairing_the_same_way_is_code_plus_ais() -> None:
    row = arch_row(1, WIDTHS)

    outcome = _resolve(row, (0, (1, 2, 3)), code_paired=True)

    assert (outcome.source, outcome.status) == ("code+ais", "paired")
    assert _slots(outcome) == [(0,), (1,), (2,)]
    assert outcome.details["code"]["status"] == "paired"


def test_code_paired_but_the_ais_paired_differently_is_code_alone() -> None:
    row = arch_row(1, WIDTHS)

    outcome = _resolve(row, (0, (1, 2, 0)), code_paired=True)

    assert (outcome.source, outcome.status) == ("code", "paired")
    assert _slots(outcome) == [(0,), (1,), (2,)], "code's own pairs, for the reviewer to confirm"
    assert any("differently" in reason for reason in outcome.reasons)


def test_code_paired_but_the_ais_disagree_with_each_other_is_code_alone() -> None:
    row = arch_row(1, WIDTHS)

    outcome = _resolve(row, (0, (1, 2, 3)), (0, (1, 2, 0)), code_paired=True)

    assert (outcome.source, outcome.status) == ("code", "paired")
    assert len(outcome.pairs) == 3


def test_code_paired_but_a_reader_gave_no_answer_is_code_alone() -> None:
    row = arch_row(1, WIDTHS)

    outcome = _resolve(row, (0, (1, 2, 3)), None, code_paired=True)

    assert (outcome.source, outcome.status) == ("code", "paired")
    assert outcome.details["ai"]["answers"][1] == {"model_id": SONNET, "answered": False}


def test_code_paired_but_the_ais_say_it_measures_blocking_is_code_alone_with_the_reason() -> None:
    row = arch_row(1, WIDTHS)
    blocking = ("blocking_or_backing", "single_cabinet", "single_cabinet")

    outcome = _resolve(row, (0, (1, 2, 3), blocking), code_paired=True)

    assert (outcome.source, outcome.status) == ("code", "paired")
    assert "measures blocking or backing, not the same thing" in _dropped(outcome)


def test_different_answers_go_to_the_reviewer_with_both_kept() -> None:
    outcome = _resolve(arch_row(1, WIDTHS), (0, (1, 2, 3)), (0, (1, 2, 0)))

    assert (outcome.source, outcome.status, outcome.pairs) == ("none", "ais-disagree", ())
    assert [answer["pieces"] for answer in outcome.details["ai"]["answers"]] == [
        [1, 2, 3],
        [1, 2, 0],
    ]


def test_same_pairing_but_a_different_measure_for_a_paired_dimension_is_a_disagreement() -> None:
    first = (0, (1, 2, 3), ("single_cabinet", "single_cabinet", "single_cabinet"))
    second = (0, (1, 2, 3), ("single_cabinet", "blocking_or_backing", "single_cabinet"))

    outcome = _resolve(arch_row(1, WIDTHS), first, second)

    assert (outcome.source, outcome.status, outcome.pairs) == ("none", "ais-disagree", ())
    assert any("disagree about what A2 measures" in reason for reason in outcome.reasons)


# --- #1109: two words code treats the same are one answer; "unsure" is the reviewer's ------------


def test_one_cabinet_and_a_filler_for_a_piece_is_not_a_disagreement() -> None:
    """Two words that lead to the same pairs are the same answer to code; the words are kept."""
    first = (0, (1, 2, 3), ("single_cabinet", "single_cabinet", "single_cabinet"))
    second = (0, (1, 2, 3), ("single_cabinet", "filler_or_end_panel", "single_cabinet"))

    outcome = _resolve(arch_row(1, WIDTHS), first, second)

    assert (outcome.source, outcome.status) == ("both-ais", "paired")
    assert _slots(outcome) == [(0,), (1,), (2,)]
    assert any(
        "different words for what A2 measures (one cabinet; a filler or end panel)" in reason
        for reason in outcome.reasons
    ), outcome.reasons
    row_ids = [str(span.candidate_id) for span in outcome.question.architect]
    assert row_ids[1] not in outcome.details["architect_measures"], "only words both AIs gave"


def test_the_countertop_and_a_run_for_the_whole_run_is_not_a_disagreement() -> None:
    row = arch_row(1, (72,))

    outcome = _resolve(row, (1, (0, 0, 0), ("countertop",)), (1, (0, 0, 0), ("cabinet_run",)))

    assert (outcome.source, outcome.status) == ("both-ais", "paired")
    assert outcome.pairs == (DecidedPair("overall", row.spans[0].candidate_id, ()),)  # type: ignore[arg-type]


def test_two_words_that_never_pair_are_not_a_disagreement_and_say_both() -> None:
    row = arch_row(1, (72,))

    outcome = _resolve(
        row, (1, (0, 0, 0), ("wall_to_wall",)), (1, (0, 0, 0), ("clearance_or_gap",))
    )

    assert (outcome.source, outcome.pairs) == ("none", ())
    assert outcome.status != "ais-disagree"
    assert "measures wall to wall or a clearance or gap, not the same thing" in _dropped(outcome)


def test_the_countertop_and_a_run_for_the_whole_run_and_every_piece_is_a_disagreement() -> None:
    # A run of cabinets also pairs with every piece; the countertop does not: different results.
    row = arch_row(1, (72,))

    outcome = _resolve(row, (1, (1, 1, 1), ("countertop",)), (1, (1, 1, 1), ("cabinet_run",)))

    assert (outcome.source, outcome.status) == ("none", "ais-disagree")


@pytest.mark.parametrize("who", [0, 1])
@pytest.mark.parametrize("item", ["overall", "V2"])
def test_unsure_from_either_reader_is_the_reviewers(who: int, item: str) -> None:
    unsure: list[tuple[str, ...]] = [(), ()]
    unsure[who] = (item,)

    outcome = _resolve(arch_row(1, WIDTHS), (0, (1, 0, 3)), unsure=(unsure[0], unsure[1]))

    assert (outcome.source, outcome.status, outcome.pairs) == ("none", "ais-refused", ())
    words = "the vendor's whole run" if item == "overall" else "vendor piece V2"
    said = f"unsure which architect dimension, if any, measures {words}"
    assert any(said in reason for reason in outcome.reasons), outcome.reasons
    assert outcome.details["ai"]["answers"][who]["unsure"] == [item]


def test_unsure_never_reads_as_nothing_comparable_when_code_found_nothing() -> None:
    outcome = _resolve(
        arch_row(1, WIDTHS),
        (0, (0, 0, 0)),
        code_status="nothing_comparable",
        unsure=(("V1", "V2", "V3"), ()),
    )

    assert (outcome.source, outcome.status) == ("none", "ais-refused")


def test_unsure_does_not_undo_code_s_own_pairing() -> None:
    outcome = _resolve(arch_row(1, WIDTHS), (0, (0, 0, 0)), code_paired=True, unsure=(("V1",), ()))

    assert (outcome.source, outcome.status, len(outcome.pairs)) == ("code", "paired", 3)
    assert any("was unsure" in reason for reason in outcome.reasons)


def test_unsure_what_a_paired_dimension_measures_is_the_reviewers() -> None:
    first = (0, (1, 2, 3), ("single_cabinet", "unsure", "single_cabinet"))

    outcome = _resolve(arch_row(1, WIDTHS), first)

    assert (outcome.source, outcome.status, outcome.pairs) == ("none", "ais-refused", ())
    assert any("unsure what A2 measures" in reason for reason in outcome.reasons)


@pytest.mark.parametrize("code_status", ["ambiguous", "no_fit"])
def test_both_ais_finding_nothing_keeps_code_s_undecided_status(code_status: str) -> None:
    """Two "nothing" answers never overwrite code's own undecided status (#1109), so the check
    asks the reviewer when the architect prints a usable dimension."""
    outcome = _resolve(arch_row(1, WIDTHS), (0, (0, 0, 0)), code_status=code_status)

    assert (outcome.source, outcome.status, outcome.pairs) == ("none", code_status, ())
    assert any("code could not decide" in reason for reason in outcome.reasons)
    assert any("the reviewer can pair them" in reason for reason in outcome.reasons)


def test_a_different_measure_for_an_unpaired_dimension_is_not_a_disagreement() -> None:
    first = (0, (1, 2, 0), ("single_cabinet", "single_cabinet", "unsure"))
    second = (0, (1, 2, 0), ("single_cabinet", "single_cabinet", "height_or_other"))

    outcome = _resolve(arch_row(1, WIDTHS), first, second)

    assert (outcome.source, outcome.status) == ("both-ais", "paired")
    assert _slots(outcome) == [(0,), (1,)]
    row_ids = [str(span.candidate_id) for span in outcome.question.architect]
    assert outcome.details["architect_measures"] == {
        row_ids[0]: "single_cabinet",
        row_ids[1]: "single_cabinet",
    }, "only the measures both AIs agree on are kept"


def test_the_same_pieces_but_a_different_overall_is_a_disagreement() -> None:
    row = arch_row(1, (72,))
    outcome = _resolve(row, (1, (0, 0, 0), ("countertop",)), (0, (0, 0, 0), ("countertop",)))

    assert (outcome.source, outcome.status) == ("none", "ais-disagree")


def test_a_missing_answer_is_a_refusal_never_one_readers_pairing() -> None:
    outcome = _resolve(arch_row(1, WIDTHS), (0, (1, 2, 3)), None)

    assert (outcome.source, outcome.status, outcome.pairs) == ("none", "ais-refused", ())
    ai: Any = outcome.details["ai"]
    assert ai["answers"][1] == {"model_id": SONNET, "answered": False}


def test_the_overall_never_pairs_with_blocking_even_when_both_ais_say_so() -> None:
    # The false FAIL the paid proof found: both AIs gave the vendor's overall the architect's
    # dimension of a hatched blocking strip. Agreed, drawn about the right length: still refused.
    row = arch_row(1, (72,))

    outcome = _resolve(row, (1, (0, 0, 0), ("blocking_or_backing",)))

    assert (outcome.source, outcome.pairs) == ("none", ())
    assert "the architect's span 1.0 measures blocking or backing, not the same thing" in (
        _dropped(outcome)
    )
    assert any(
        "measure: span 1.0 blocking or backing" in reason for reason in outcome.reasons
    ), outcome.reasons


@pytest.mark.parametrize(
    ("measure", "words"),
    [
        ("fixture_or_appliance_centre", "to a fixture or appliance centre line"),
        ("clearance_or_gap", "a clearance or gap"),
        ("wall_to_wall", "wall to wall"),
        ("single_cabinet", "one cabinet"),
        ("filler_or_end_panel", "a filler or end panel"),
    ],
)
def test_the_overall_pairs_only_with_a_countertop_or_a_run(measure: str, words: str) -> None:
    row = arch_row(1, (72,))

    outcome = _resolve(row, (1, (0, 0, 0), (measure,)))

    assert outcome.pairs == ()
    assert f"measures {words}, not the same thing" in _dropped(outcome)


@pytest.mark.parametrize("measure", ["countertop", "cabinet_run"])
def test_the_overall_pairs_with_a_countertop_or_a_run(measure: str) -> None:
    row = arch_row(1, (72,))

    outcome = _resolve(row, (1, (0, 0, 0), (measure,)))

    assert outcome.pairs == (DecidedPair("overall", row.spans[0].candidate_id, ()),)  # type: ignore[arg-type]
    assert outcome.source == "both-ais"


@pytest.mark.parametrize(
    "measure",
    [
        "fixture_or_appliance_centre",
        "clearance_or_gap",
        "blocking_or_backing",
        "countertop",
        "appliance_opening",
        "height_or_other",
    ],
)
def test_a_piece_pairs_only_with_a_cabinet_or_a_filler(measure: str) -> None:
    row = arch_row(1, WIDTHS)

    outcome = _resolve(row, (0, (1, 2, 3), (measure, "single_cabinet", "filler_or_end_panel")))

    assert _slots(outcome) == [(1,), (2,)]
    assert "the architect's span 1.0 measures" in _dropped(outcome)


def test_a_run_pairs_with_pieces_only_when_they_are_the_whole_run() -> None:
    whole = arch_row(1, (72,))
    part = arch_row(1, (54, 18))

    every = _resolve(whole, (0, (1, 1, 1), ("cabinet_run",)))
    some = _resolve(part, (0, (1, 1, 2), ("cabinet_run", "single_cabinet")))

    assert _slots(every) == [(0, 1, 2)]
    assert _slots(some) == [(2,)]
    assert "measures a run of cabinets, not the same thing" in _dropped(some)


def test_nothing_comparable_says_in_plain_words_what_the_architect_measures() -> None:
    row = arch_row(1, (30, 24))

    outcome = _resolve(
        row,
        (0, (0, 0, 0), ("blocking_or_backing", "fixture_or_appliance_centre")),
        code_status="nothing_comparable",
    )

    assert (outcome.source, outcome.status, outcome.pairs) == ("none", "nothing_comparable", ())
    assert any(
        "the architect's dimensions here measure: span 1.0 blocking or backing; span 1.1 to a "
        "fixture or appliance centre line" in reason
        for reason in outcome.reasons
    ), outcome.reasons


def test_the_agreed_measures_are_kept_by_candidate() -> None:
    row = arch_row(1, (30, 24))

    outcome = _resolve(row, (0, (0, 0, 0), ("blocking_or_backing", "clearance_or_gap")))

    assert outcome.details["architect_measures"] == {
        str(row.spans[0].candidate_id): "blocking_or_backing",
        str(row.spans[1].candidate_id): "clearance_or_gap",
    }


def test_a_pair_two_drawings_draw_at_very_different_lengths_is_dropped() -> None:
    # A1 is 30" drawn; the AIs give it V1 + V2 (54" drawn): not the same thing.
    outcome = _resolve(arch_row(1, WIDTHS), (0, (1, 1, 3)))

    assert _slots(outcome) == [(2,)]
    assert "quarter" in _dropped(outcome)


def test_a_real_mismatch_within_the_band_is_kept_for_the_compare() -> None:
    # The architect draws 32" where the vendor has 30": a real difference the rule must flag.
    row = arch_row(1, (32, 22, 18))

    outcome = _resolve(row, (0, (1, 2, 3)))

    assert len(outcome.pairs) == 3


def test_a_split_that_is_not_contiguous_is_dropped() -> None:
    row = arch_row(1, (30, 24, 18))

    outcome = _resolve(row, (0, (1, 0, 1)))

    assert row.spans[0].candidate_id not in {pair.architect_candidate_id for pair in outcome.pairs}
    assert "next to each other" in _dropped(outcome)


def test_a_contiguous_split_of_one_bay_pairs_with_both_pieces() -> None:
    row = arch_row(1, (54, 18))

    outcome = _resolve(row, (0, (1, 1, 2)))

    assert _slots(outcome) == [(0, 1), (2,)]


def test_the_whole_run_pairs_as_the_overall_with_no_vendor_pieces() -> None:
    row = arch_row(1, (72,))

    outcome = _resolve(row, (1, (0, 0, 0)))

    assert outcome.pairs == (DecidedPair("overall", row.spans[0].candidate_id, ()),)  # type: ignore[arg-type]


def test_one_architect_dimension_cannot_be_the_whole_run_and_only_one_piece() -> None:
    row = arch_row(1, (72,))

    outcome = _resolve(row, (1, (1, 0, 0)))

    assert outcome.pairs == ()


def test_all_zero_answers_mean_nothing_comparable_when_code_found_nothing_either() -> None:
    outcome = _resolve(arch_row(1, WIDTHS), (0, (0, 0, 0)), code_status="nothing_comparable")

    assert (outcome.source, outcome.status, outcome.pairs) == ("none", "nothing_comparable", ())


def test_an_ai_pair_on_a_centre_line_span_is_dropped() -> None:
    # Defence in depth: such a span is never numbered, but if it were, code drops it.
    row = arch_row(1, WIDTHS, outline=(True, False, True))

    outcome = _resolve(row, (0, (1, 2, 3)))

    assert row.spans[1].candidate_id not in {pair.architect_candidate_id for pair in outcome.pairs}
    assert "centre-line" in _dropped(outcome)


def test_an_ai_pair_on_a_span_of_unknown_outline_is_dropped_by_the_code_witness() -> None:
    row = arch_row(1, WIDTHS, outline=(True, None, True))

    outcome = _resolve(row, (0, (1, 2, 3)))

    assert _slots(outcome) == [(0,), (2,)]
    assert "not known to sit on the casework outline" in _dropped(outcome)


def test_code_alone_when_the_ais_were_not_asked_and_none_when_nothing_paired() -> None:
    page = page_input(arch_row(1, WIDTHS))
    code, _ = pair_by_code(vendor(), page, MEASURED_PAIRING_SETTINGS)
    no_fit, _ = pair_by_code(vendor(), page_input(arch_row(1, (72,))), MEASURED_PAIRING_SETTINGS)

    assert (code.source, code.status, len(code.pairs)) == ("code", "paired", 3)
    assert (no_fit.source, no_fit.status, no_fit.pairs) == ("none", "no_fit", ())


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
    pick: Pick = (0, (1, 0, 0)),
) -> tuple[PageSlotResult, list[CropJob]]:
    result = replace(_result(_sealed_owners()), page_id=page.page_id)
    if hold is not None:
        result = replace(result, row_hold=SimpleNamespace(reason=hold, code="vif"))  # type: ignore[arg-type]
    asked: list[CropJob] = []

    def ask(jobs: Sequence[CropJob]) -> Mapping[tuple[str, str], object]:
        asked.extend(jobs)
        return {
            (job.key, job.model_id): answer(job.model_id, pick, job.architect_spans or 0)
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


def test_both_readers_are_asked_even_when_code_pairs_and_agreeing_is_code_plus_ais() -> None:
    paired, asked = _run_batch(page_input(arch_row(1, WIDTHS)), pick=(0, (1, 2, 3)))

    assert [job.model_id for job in asked] == [OPUS, SONNET]
    assert paired.architect_pairing is not None
    assert (paired.architect_pairing.source, paired.architect_pairing.status) == (
        "code+ais",
        "paired",
    )


def test_code_pairing_the_ais_do_not_confirm_is_code_alone() -> None:
    paired, _asked = _run_batch(page_input(arch_row(1, WIDTHS)), pick=(0, (0, 0, 0)))

    assert paired.architect_pairing is not None
    assert (paired.architect_pairing.source, len(paired.architect_pairing.pairs)) == ("code", 3)


def test_the_picture_numbers_every_span_code_has_not_refused() -> None:
    # A centre line is refused by code and never numbered; an unknown outline is numbered (the AIs
    # say what it measures) but a pair with it is still dropped by the code witness.
    page = page_input(arch_row(1, WIDTHS, outline=(True, None, False)))

    _paired, asked = _run_batch(page, pick=(0, (1, 2, 0)))

    assert {job.architect_spans for job in asked} == {2}
    packet = asked[0].question_packet
    assert packet is not None
    assert packet["architect_candidate_ids"] == [
        str(span.candidate_id) for span in page.rows[0].spans[:2]
    ]


def test_no_reader_is_asked_when_code_refused_every_span() -> None:
    _paired, asked = _run_batch(page_input(arch_row(1, WIDTHS, outline=(False, False, False))))

    assert asked == []


def test_when_code_cannot_decide_both_readers_get_one_numbered_picture() -> None:
    paired, asked = _run_batch(page_input(arch_row(1, (30,), start=400)))

    assert [job.model_id for job in asked] == [OPUS, SONNET]
    assert all(job.arch_pair_question for job in asked)
    assert len({job.png for job in asked}) == 1, "the same picture for both"
    assert all((job.vendor_pieces, job.architect_spans) == (3, 1) for job in asked)
    assert asked[0].question_packet is not None
    assert asked[0].question_packet["prompt_id"] == ARCH_PAIR_PROMPT_ID == "arch-pair-v3"
    assert paired.architect_pairing is not None
    assert paired.architect_pairing.source == "both-ais"
    assert [pair.vendor_slot_indices for pair in paired.architect_pairing.pairs] == [(0,)]


def test_no_reader_is_asked_without_the_claude_pair() -> None:
    paired, asked = _run_batch(page_input(arch_row(1, (30,))), ask_the_ais=False)

    assert asked == []
    assert paired.architect_pairing is not None
    assert (paired.architect_pairing.source, paired.architect_pairing.status) == (
        "none",
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


def _read_named_sheet(
    architect: ArchitectPairing | None,
    page_id: UUID,
    payload: Mapping[str, object] | None = None,
) -> tuple[Any, list[Any]]:
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
        ArchitectPairing(MEASURED_PAIRING_SETTINGS, {page_id: architect}),
        page_id,
        {
            "architect": [{"a": k, "measures": "single_cabinet"} for k in (1, 2, 3)],
            "overall": "none",
            "pieces": ["A1", "A2", "A3"],
            "why": "each architect bay is the vendor's cabinet below it",
        },
    )

    _same_reading(plain, paired)
    assert plain.architect_pairing is None and plain_pair_calls == []
    assert paired.architect_pairing is not None
    assert (paired.architect_pairing.source, paired.architect_pairing.status) == (
        "code+ais",
        "paired",
    )
    assert [pair.vendor_slot_indices for pair in paired.architect_pairing.pairs] == [
        (0,),
        (1,),
        (2,),
    ]
    assert sorted(call for call in pair_calls if isinstance(call, str)) == [OPUS, SONNET]


def test_the_ai_question_rides_the_same_batch_and_changes_no_reading() -> None:
    page_id = uuid4()
    plain, _ = _read_named_sheet(None, page_id)
    architect = replace(page_input(arch_row(1, (48,), start=10)), page_id=page_id)

    paired, pair_calls = _read_named_sheet(
        ArchitectPairing(MEASURED_PAIRING_SETTINGS, {page_id: architect}),
        page_id,
        {
            "architect": [{"a": 1, "measures": "countertop"}],
            "overall": "A1",
            "pieces": ["none", "none", "none"],
            "why": "A1 spans the whole run",
        },
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
