"""The upside-down guard: a sideways label seals only where exactly one turn fits its drawing.

Verification for `extraction/slot_reader/orientation.py`. Every value is invented; no client value
("68" against "89" is the generic upside-down example). The view is drawn at 1/4" per point.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from extraction.geometry.rows import Box
from extraction.ink import InkAt, InkClass
from extraction.slot_reader.orientation import (
    BOTH_FIT,
    NO_SCALE_SIDEWAYS,
    ONE_MAKER,
    GuardState,
    TurnReading,
    decide_turn,
    read_turn,
)
from extraction.slot_reader.runs import Lane, PlannedLabel
from extraction.slot_reader.seal import ReaderAnswer
from extraction.slot_reader.veto import DRAWN_LENGTH_REASON
from extraction.slot_reader.view_scale import ScaleVerdict

QWEN = "qwen.qwen3-vl-235b-a22b"
SONNET = "anthropic.claude-sonnet-5-5"
OPUS = "anthropic.claude-opus-5-5"
VENDOR = InkAt(InkClass.VENDOR, "")
SCALE = Fraction(1, 4)
_BOX = Box(Decimal(10), Decimal(10), Decimal(16), Decimal(30))
LABEL = PlannedLabel(
    box=_BOX,
    crop=_BOX,
    lane=Lane.GLYPHS,
    text=None,
    text_stacked=False,
    has_digit=True,
    touches_edge=False,
    ambiguous_slot=False,
    crowded=False,
    ticks_in_crop=False,
    path_boxes=(),
)


def answer(model: str, text: str, *, stacked: bool = False) -> ReaderAnswer:
    return ReaderAnswer(
        model_id=model,
        text=text,
        readable=True,
        no_dimension=False,
        stacked=stacked,
        combined=False,
    )


def pair(
    text: str, other: str | None = None, *, models: tuple[str, str] = (SONNET, QWEN)
) -> tuple[ReaderAnswer, ReaderAnswer]:
    return (answer(models[0], text), answer(models[1], text if other is None else other))


def turn(
    degrees: int,
    answers: tuple[ReaderAnswer, ...],
    *,
    drawn_inches: int | None = 68,
    scale: Fraction | None = SCALE,
    stacked_by_code: bool = False,
) -> TurnReading:
    drawn = None if drawn_inches is None else Fraction(drawn_inches) / SCALE
    return read_turn(
        LABEL,
        degrees,
        answers,
        ink=VENDOR,
        stacked_by_code=stacked_by_code,
        drawn=drawn,
        scale=scale,
    )


def test_68_drawn_top_to_bottom_is_rejected_at_90_and_sealed_at_270() -> None:
    """Turned the wrong way both makers read 89; the drawing (68 long) rejects it."""
    at_90 = turn(90, pair('89"'))
    at_270 = turn(270, pair('68"'))
    assert at_90.scale is ScaleVerdict.VETOED
    assert at_270.scale is ScaleVerdict.WITNESSED
    found = decide_turn([at_90, at_270], sideways=True)
    assert found.state is GuardState.SEALED_WITNESSED
    assert (found.turn, found.text, found.inches, found.why) == (270, '68"', Fraction(68), None)


def test_a_rotation_symmetric_value_is_one_reading_counted_once() -> None:
    found = decide_turn(
        [turn(90, pair('96"'), drawn_inches=96), turn(270, pair('96"'), drawn_inches=96)],
        sideways=True,
    )
    assert found.state is GuardState.SEALED_WITNESSED
    assert (found.turn, found.inches) == (90, Fraction(96))


def test_two_turns_that_both_fit_with_different_values_are_held() -> None:
    """A made-up close pair: 40 and 42 both sit within a tenth of a 41" drawn length."""
    found = decide_turn(
        [turn(90, pair('40"'), drawn_inches=41), turn(270, pair('42"'), drawn_inches=41)],
        sideways=True,
    )
    assert (found.state, found.why, found.inches) == (GuardState.HELD, BOTH_FIT, None)


def test_a_sideways_label_with_no_scale_stays_unread() -> None:
    for readings in (
        [turn(90, pair('89"'), scale=None), turn(270, pair('68"'), scale=None)],
        [turn(270, pair('68"'), drawn_inches=None)],
    ):
        found = decide_turn(readings, sideways=True)
        assert (found.state, found.why, found.inches) == (
            GuardState.UNREAD,
            NO_SCALE_SIDEWAYS,
            None,
        )


def test_an_upright_label_with_no_scale_seals_unwitnessed() -> None:
    found = decide_turn([turn(0, pair('68"'), scale=None)], sideways=False)
    assert found.state is GuardState.SEALED_UNWITNESSED
    assert (found.turn, found.inches) == (0, Fraction(68))


def test_an_upright_label_on_its_drawn_length_seals_witnessed() -> None:
    found = decide_turn([turn(0, pair('68"'))], sideways=False)
    assert found.state is GuardState.SEALED_WITNESSED


def test_a_same_maker_pair_never_seals() -> None:
    reading = turn(0, pair('68"', models=(OPUS, SONNET)))
    assert reading.inches is None and reading.outcome.reason == ONE_MAKER
    found = decide_turn([reading], sideways=False)
    assert (found.state, found.why) == (GuardState.UNREAD, ONE_MAKER)


def test_a_stacked_fraction_never_seals_whoever_says_it_is_stacked() -> None:
    by_code = turn(0, pair('68 1/2"'), stacked_by_code=True)
    by_reader = turn(0, (answer(SONNET, '68 1/2"', stacked=True), answer(QWEN, '68 1/2"')))
    for reading in (by_code, by_reader):
        assert reading.inches is None
        assert decide_turn([reading], sideways=False).state is GuardState.UNREAD


def test_readers_that_differ_leave_the_label_unread_with_the_seals_reason() -> None:
    found = decide_turn([turn(0, pair('68"', '66"'))], sideways=False)
    assert (found.state, found.why) == (GuardState.UNREAD, "readers differ")


def test_a_bare_number_is_no_inch_value_and_never_seals() -> None:
    """`units/` does not assume inches for a bare 68."""
    assert decide_turn([turn(0, pair("68"))], sideways=False).state is GuardState.UNREAD


def test_an_agreed_value_the_drawing_rejects_at_every_turn_is_held() -> None:
    found = decide_turn(
        [turn(90, pair('89"')), turn(270, pair('98"'))],
        sideways=True,
    )
    assert (found.state, found.why) == (GuardState.HELD, DRAWN_LENGTH_REASON)


def test_millimetres_never_give_the_value() -> None:
    """A millimetre label with its inches in brackets is its inch number; mm alone is no value."""
    assert decide_turn([turn(0, pair("1727"))], sideways=False).state is GuardState.UNREAD
    found = decide_turn([turn(0, pair("1727 [68]"))], sideways=False)
    assert found.inches == Fraction(68)


def test_a_turn_list_that_breaks_the_rules_is_a_mistake() -> None:
    assert decide_turn([], sideways=True).state is GuardState.UNREAD
    with pytest.raises(ValueError):
        decide_turn([turn(90, pair('68"')), turn(90, pair('68"'))], sideways=True)
    with pytest.raises(ValueError):
        decide_turn([turn(90, pair('68"'))], sideways=False)
    with pytest.raises(ValueError):
        turn(180, pair('68"'))
