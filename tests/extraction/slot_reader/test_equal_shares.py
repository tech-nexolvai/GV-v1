"""A row of N unlabelled pieces read through an `X"(N EQ)` chain (#1086).

Verification for `extraction/slot_reader/equal_shares.py`. Every value and position is invented;
no client value. Near-misses are the point: each one must change nothing.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise

import pytest

from extraction.geometry.rows import (
    MEASURED_SETTINGS,
    Box,
    CountertopRowCandidate,
    OverallRow,
    Slot,
    StoredBox,
    TickSource,
    Tiling,
)
from extraction.slot_reader.equal_shares import (
    NO_LABEL,
    EqualShareChain,
    equal_share_chain,
    equal_share_partner,
)
from extraction.slot_reader.seal import LabelState, OwnerOutcome
from units.measurement import Measurement, Unit

_STORED = StoredBox(Decimal(0), Decimal(0), Decimal(1), Decimal(1))
END = MEASURED_SETTINGS.overall_end_pt
SLACK = Decimal(2)
#: Two pasted drawings, one above the other.
UPPER = Box(Decimal(100), Decimal(100), Decimal(700), Decimal(300))
LOWER = Box(Decimal(100), Decimal(350), Decimal(700), Decimal(600))


def row(
    ticks: tuple[float, ...], y: float, *, overall_y: float | None = None, rank: int = 1
) -> CountertopRowCandidate:
    xs = tuple(Decimal(str(x)) for x in ticks)
    line = Decimal(str(y))
    slots = tuple(
        Slot(
            index=index,
            x0=a,
            x1=b,
            box=Box(a, line - 16, b, line + 16),
            stored=_STORED,
            labels=(),
        )
        for index, (a, b) in enumerate(pairwise(xs))
    )
    overall = None
    if overall_y is not None:
        top = Decimal(str(overall_y))
        overall = OverallRow(
            y=top,
            x0=xs[0],
            x1=xs[-1],
            tick_source=TickSource.SLASH,
            tiling=Tiling.COINCIDENT,
            left_excess_pt=Decimal(0),
            right_excess_pt=Decimal(0),
            box=Box(xs[0], top - 16, xs[-1], top + 16),
            stored=_STORED,
            labels=(),
        )
    return CountertopRowCandidate(
        y=line,
        ticks=xs,
        tick_source=TickSource.SLASH,
        slots=slots,
        overall=overall,
        findings=(),
        labelled=len(slots),
        rank=rank,
        rejected_because=None,
    )


#: The labelled chain: an end piece, the shares, an end piece, and its overall above.
CHAIN = row((200, 220, 580, 600), 400, overall_y=385, rank=1)
#: The chosen row: the same run in eight pieces, nothing printed on them.
STONE = row(tuple(200 + 50 * i for i in range(9)), 500, rank=2)


def partner(chosen: CountertopRowCandidate, *others: CountertopRowCandidate) -> object:
    return equal_share_partner(
        chosen,
        (chosen, *others),
        (UPPER, LOWER),
        end_tolerance_pt=END,
        frame_slack_pt=SLACK,
    )


def test_the_chain_with_the_same_ends_in_the_same_drawing_is_the_partner() -> None:
    assert partner(STONE, CHAIN) is CHAIN


def test_ends_off_by_more_than_the_row_finders_end_tolerance_find_no_partner() -> None:
    off = Decimal("0.01")
    shifted = row((200 - float(END + off), 220, 580, 600), 400, overall_y=385)
    assert partner(STONE, shifted) is None
    shifted_right = row((200, 220, 580, 600 + float(END + off)), 400, overall_y=385)
    assert partner(STONE, shifted_right) is None


def test_ends_within_the_end_tolerance_still_match() -> None:
    within = row((200 + float(END), 220, 580, 600 - float(END)), 400, overall_y=385)
    assert partner(STONE, within) is within


def test_a_chain_in_another_pasted_drawing_is_never_the_partner() -> None:
    assert CHAIN.overall is not None
    other_view = replace(CHAIN, y=Decimal(200), overall=replace(CHAIN.overall, y=Decimal(185)))
    assert partner(STONE, other_view) is None


def test_a_chain_whose_overall_lies_outside_the_drawing_is_never_the_partner() -> None:
    assert CHAIN.overall is not None
    outside = replace(CHAIN, overall=replace(CHAIN.overall, y=Decimal(330)))
    assert partner(STONE, outside) is None


def test_no_drawing_box_means_no_partner() -> None:
    assert (
        equal_share_partner(STONE, (STONE, CHAIN), (), end_tolerance_pt=END, frame_slack_pt=SLACK)
        is None
    )


def test_a_chain_without_an_overall_or_end_pieces_is_not_a_partner() -> None:
    assert partner(STONE, replace(CHAIN, overall=None)) is None
    two_pieces = row((200, 400, 600), 400, overall_y=385)
    assert partner(STONE, two_pieces) is None


def test_two_matching_chains_are_a_choice_so_neither_is_used() -> None:
    twin = replace(CHAIN, y=Decimal(420), rank=3)
    assert partner(STONE, CHAIN, twin) is None


def test_an_architect_row_is_never_a_partner() -> None:
    assert partner(STONE, replace(CHAIN, in_architect_view=True)) is None


def test_a_chosen_row_of_one_piece_has_no_partner() -> None:
    assert partner(row((200, 600), 500), CHAIN) is None


# --- after reading -------------------------------------------------------------------------------


def sealed(value: int) -> OwnerOutcome:
    return OwnerOutcome(
        LabelState.SEALED, Measurement(Fraction(value), Unit.INCH, None), 0, None, None
    )


NOTHING = OwnerOutcome(
    LabelState.REVIEW, None, None, NO_LABEL, "no dimension label found for this piece"
)
HELD = OwnerOutcome(LabelState.REVIEW, None, 0, "readers-differ", "readers differ")
EIGHT_BLANK = (NOTHING,) * 8
GOOD_CHAIN = (
    (sealed(5), '4"+1" Filler'),
    (sealed(96), '96"(8 EQ)'),
    (sealed(5), '4"+1" Filler'),
)
GOOD_OVERALL = (sealed(106), '106"')


def test_eight_blank_pieces_read_through_a_sealed_eight_share_chain() -> None:
    assert equal_share_chain(EIGHT_BLANK, None, GOOD_CHAIN, GOOD_OVERALL) == EqualShareChain(1, 8)


def test_a_blank_overall_on_the_chosen_row_does_not_stop_it() -> None:
    assert equal_share_chain(EIGHT_BLANK, NOTHING, GOOD_CHAIN, GOOD_OVERALL) is not None


@pytest.mark.parametrize("count", [7, 9])
def test_a_share_count_one_off_the_piece_count_changes_nothing(count: int) -> None:
    assert equal_share_chain((NOTHING,) * count, None, GOOD_CHAIN, GOOD_OVERALL) is None


def test_a_chosen_row_with_any_reading_or_held_piece_changes_nothing() -> None:
    assert equal_share_chain((sealed(10), *EIGHT_BLANK[1:]), None, GOOD_CHAIN, GOOD_OVERALL) is None
    assert equal_share_chain((HELD, *EIGHT_BLANK[1:]), None, GOOD_CHAIN, GOOD_OVERALL) is None
    assert equal_share_chain(EIGHT_BLANK, sealed(106), GOOD_CHAIN, GOOD_OVERALL) is None


@pytest.mark.parametrize("position", [0, 1, 2])
def test_any_unsealed_chain_piece_changes_nothing(position: int) -> None:
    chain: list[tuple[OwnerOutcome, str | None]] = list(GOOD_CHAIN)
    chain[position] = (HELD, None)
    assert equal_share_chain(EIGHT_BLANK, None, chain, GOOD_OVERALL) is None


def test_an_unsealed_or_missing_chain_overall_changes_nothing() -> None:
    assert equal_share_chain(EIGHT_BLANK, None, GOOD_CHAIN, (HELD, None)) is None
    assert equal_share_chain(EIGHT_BLANK, None, GOOD_CHAIN, None) is None


def test_a_chain_with_no_share_label_or_two_share_labels_changes_nothing() -> None:
    plain = ((sealed(5), '5"'), (sealed(96), '100"'), (sealed(5), '5"'))
    assert equal_share_chain(EIGHT_BLANK, None, plain, GOOD_OVERALL) is None
    two = (
        (sealed(5), '5"'),
        (sealed(48), '48"(8 EQ)'),
        (sealed(48), '48"(8 EQ)'),
        (sealed(5), '5"'),
    )
    assert equal_share_chain(EIGHT_BLANK, None, two, GOOD_OVERALL) is None


def test_shares_on_an_end_piece_are_not_the_pattern() -> None:
    first = ((sealed(96), '96"(8 EQ)'), (sealed(5), '5"'), (sealed(5), '5"'))
    assert equal_share_chain(EIGHT_BLANK, None, first, GOOD_OVERALL) is None
    last = ((sealed(5), '5"'), (sealed(5), '5"'), (sealed(96), '96"(8 EQ)'))
    assert equal_share_chain(EIGHT_BLANK, None, last, GOOD_OVERALL) is None


def test_a_share_value_that_differs_from_its_label_changes_nothing() -> None:
    chain = list(GOOD_CHAIN)
    chain[1] = (sealed(95), '96"(8 EQ)')
    assert equal_share_chain(EIGHT_BLANK, None, chain, GOOD_OVERALL) is None


def test_a_chain_of_two_pieces_or_a_chosen_row_of_one_changes_nothing() -> None:
    assert equal_share_chain(EIGHT_BLANK, None, GOOD_CHAIN[:2], GOOD_OVERALL) is None
    one = (sealed(5), '100"(1 EQ)')
    assert (
        equal_share_chain((NOTHING,), None, (GOOD_CHAIN[0], one, GOOD_CHAIN[2]), GOOD_OVERALL)
        is None
    )


# --- #1110: the read-through starts only from an explicit "no label" -------------------------------


def _piece_from_answers(belongs: str, *, no_dimension: bool) -> OwnerOutcome:
    """One blank piece of the chosen row, from the two Claude readers' answers, through the same
    `seal_label` and `owner_outcome` the slot reader uses."""
    from extraction.ink import InkAt, InkClass
    from extraction.slot_reader.runs import Lane, PlannedLabel
    from extraction.slot_reader.seal import ReaderAnswer, owner_outcome, parse_belongs, seal_label

    box = Box(Decimal(10), Decimal(10), Decimal(20), Decimal(15))
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
    answers = tuple(
        ReaderAnswer(
            model_id=model,
            text="",
            readable=False,
            no_dimension=no_dimension,
            stacked=False,
            combined=False,
            belongs=parse_belongs(belongs),
        )
        for model in ("anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5")
    )
    outcome = seal_label(
        label,
        answers=answers,
        ink=InkAt(InkClass.VENDOR, ""),
        stacked_by_bar=False,
        allow_stacked=False,
        row_ambiguity=None,
        allow_claude_pair=True,
    )
    return owner_outcome([outcome])


def test_pieces_both_readers_were_unsure_about_never_start_the_read_through() -> None:
    unsure = _piece_from_answers("unsure", no_dimension=False)
    assert unsure.reason_code == "unsure"

    assert equal_share_chain((unsure,) * 8, None, GOOD_CHAIN, GOOD_OVERALL) is None
    assert equal_share_chain((unsure, *EIGHT_BLANK[1:]), None, GOOD_CHAIN, GOOD_OVERALL) is None


def test_pieces_both_readers_explicitly_called_blank_still_read_through() -> None:
    by_flag = _piece_from_answers("no", no_dimension=True)
    by_no = _piece_from_answers("no", no_dimension=False)
    assert by_flag.reason_code == NO_LABEL and by_no.reason_code == NO_LABEL

    assert equal_share_chain((by_flag,) * 8, None, GOOD_CHAIN, GOOD_OVERALL) == EqualShareChain(
        1, 8
    )
    assert equal_share_chain((by_no,) * 8, None, GOOD_CHAIN, GOOD_OVERALL) == EqualShareChain(1, 8)
