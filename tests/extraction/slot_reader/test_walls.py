"""Walls by two readers' agreement, with code's hatch check able only to object (#992).

Verification for `extraction/slot_reader/walls.py`. Every drawing here is invented.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from extraction.geometry.rows import InkLine, PageInk
from extraction.slot_reader.walls import (
    BACK_LEFT_RIGHT,
    BACK_ONLY,
    E3_WALL_SETTINGS,
    WALL_PROMPT,
    WALL_PROMPT_ID,
    HatchSeen,
    Side,
    WallAnswer,
    _bin,
    hatch_at,
    seal_walls,
)
from workflow.layout_proposals import READER_AGREEMENT_PROMPT_IDS

QWEN = "qwen.qwen3-vl-235b-a22b"
KIMI = "us.moonshotai.kimi-k3"
YES, NO, UNSURE = Side.YES, Side.NO, Side.UNSURE


def answer(
    model: str, left: Side, right: Side, behind: Side = UNSURE, view: str = "elevation"
) -> WallAnswer:
    return WallAnswer(model, left, right, behind, view)  # type: ignore[arg-type]


def both(
    left: Side, right: Side, behind: Side = UNSURE, view: str = "elevation"
) -> tuple[WallAnswer, WallAnswer]:
    return answer(KIMI, left, right, behind, view), answer(QWEN, left, right, behind, view)


NO_HATCH = HatchSeen(False, False)


def test_walls_at_both_ends_seal_back_left_right() -> None:
    outcome = seal_walls(both(YES, YES), hatch=NO_HATCH, row_ambiguity=None)
    assert outcome.config == BACK_LEFT_RIGHT and outcome.code is None


def test_no_walls_on_an_elevation_leave_the_back_wall_to_the_person() -> None:
    outcome = seal_walls(both(NO, NO, YES), hatch=NO_HATCH, row_ambiguity=None)
    assert outcome.config is None and outcome.code == "back-wall-unknown"


def test_no_end_walls_on_an_agreed_plan_with_a_back_wall_seal_back_only() -> None:
    outcome = seal_walls(both(NO, NO, YES, "plan"), hatch=NO_HATCH, row_ambiguity=None)
    assert outcome.config == BACK_ONLY
    # Not when one reader calls it an elevation, nor when the back wall is not agreed.
    mixed = (answer(KIMI, NO, NO, YES, "plan"), answer(QWEN, NO, NO, YES, "elevation"))
    assert seal_walls(mixed, hatch=NO_HATCH, row_ambiguity=None).config is None
    unsure = (answer(KIMI, NO, NO, YES, "plan"), answer(QWEN, NO, NO, UNSURE, "plan"))
    assert seal_walls(unsure, hatch=NO_HATCH, row_ambiguity=None).config is None


@pytest.mark.parametrize(
    "answers",
    [
        both(YES, NO),
        both(UNSURE, YES),
        (answer(KIMI, YES, YES), answer(QWEN, YES, NO)),
        (answer(KIMI, YES, YES), answer(QWEN, UNSURE, YES)),
    ],
)
def test_anything_but_agreement_on_a_known_layout_goes_to_the_person(
    answers: tuple[WallAnswer, WallAnswer],
) -> None:
    outcome = seal_walls(answers, hatch=NO_HATCH, row_ambiguity=None)
    assert outcome.config is None and outcome.reason


def test_one_reader_alone_never_seals() -> None:
    outcome = seal_walls((answer(QWEN, YES, YES),), hatch=NO_HATCH, row_ambiguity=None)
    assert outcome.config is None and outcome.code == "one-reader-missing"


def test_two_readers_of_one_maker_are_refused() -> None:
    with pytest.raises(ValueError):
        seal_walls((answer(QWEN, YES, YES), answer(QWEN, YES, YES)), hatch=None, row_ambiguity=None)


def test_a_row_that_may_not_be_the_countertop_never_seals() -> None:
    outcome = seal_walls(both(YES, YES), hatch=NO_HATCH, row_ambiguity="another row fits as well")
    assert outcome.config is None and outcome.code == "row-ambiguous"


def test_a_hatch_where_a_reader_says_no_wall_goes_to_the_person() -> None:
    hatch = HatchSeen(left=True, right=False)
    agreed_no = seal_walls(both(NO, NO, YES, "plan"), hatch=hatch, row_ambiguity=None)
    assert agreed_no.config is None and agreed_no.code == "hatch-contradiction"
    one_says_no = (answer(KIMI, NO, YES), answer(QWEN, YES, YES))
    held = seal_walls(one_says_no, hatch=hatch, row_ambiguity=None)
    assert held.code == "hatch-contradiction"


def test_a_hatch_never_seals_and_seeing_none_proves_nothing() -> None:
    unsure = seal_walls(both(UNSURE, UNSURE), hatch=HatchSeen(True, True), row_ambiguity=None)
    assert unsure.config is None
    agreed = seal_walls(both(YES, YES), hatch=HatchSeen(True, True), row_ambiguity=None)
    assert agreed.config == BACK_LEFT_RIGHT


def test_the_prompt_is_generic_and_its_id_is_the_one_checks_trust() -> None:
    assert WALL_PROMPT_ID in READER_AGREEMENT_PROMPT_IDS
    assert "LEFT" in WALL_PROMPT and "RIGHT" in WALL_PROMPT and "BEHIND" in WALL_PROMPT
    assert re.search(r"[0-9]+ ?(\"|in|mm)", WALL_PROMPT) is None, "no dimension, no client value"


@pytest.mark.parametrize(
    ("dx", "dy", "degrees"),
    [
        ("10", "0.5", 5),
        ("10", "10", 45),
        ("-10", "-10", 45),
        ("10", "-10", 135),
        ("0.5", "10", 85),
        ("1", "1.02", 45),
        ("-1", "10", 95),
    ],
)
def test_a_strokes_direction_is_binned_without_trigonometry(dx: str, dy: str, degrees: int) -> None:
    assert _bin(Decimal(dx), Decimal(dy)) == degrees


def _hatch_lines(
    x_end: int, top: int, count: int, step: int, slope: int = 1
) -> tuple[InkLine, ...]:
    """`count` parallel 8-point diagonals just left of `x_end`, `step` points apart down the page."""
    return tuple(
        InkLine(
            Decimal(x_end - 12),
            Decimal(top + index * step),
            Decimal(x_end - 6),
            Decimal(top + index * step + 6 * slope),
        )
        for index in range(count)
    )


def _ink(lines: tuple[InkLine, ...]) -> PageInk:
    return PageInk(
        width=Decimal(600),
        height=Decimal(400),
        lines=lines,
        curves=(),
        characters=(),
        drawing_boxes=(),
    )


def test_many_parallel_strokes_beside_an_end_are_a_hatch() -> None:
    ink = _ink(_hatch_lines(100, 110, 14, 5))
    seen, why = hatch_at(
        ink,
        x_end=Decimal(100),
        side="left",
        y_low=Decimal(100),
        y_high=Decimal(200),
        settings=E3_WALL_SETTINGS,
    )
    assert seen and "parallel" in why
    other_end, _ = hatch_at(
        ink,
        x_end=Decimal(300),
        side="right",
        y_low=Decimal(100),
        y_high=Decimal(200),
        settings=E3_WALL_SETTINGS,
    )
    assert not other_end


def test_a_few_strokes_or_a_short_band_are_not_a_hatch() -> None:
    few = _ink(_hatch_lines(100, 110, 6, 5))
    assert not hatch_at(
        few,
        x_end=Decimal(100),
        side="left",
        y_low=Decimal(100),
        y_high=Decimal(200),
        settings=E3_WALL_SETTINGS,
    )[0]
    short = _ink(_hatch_lines(100, 110, 14, 2))  # 26 points of height: under the 40-point span
    assert not hatch_at(
        short,
        x_end=Decimal(100),
        side="left",
        y_low=Decimal(100),
        y_high=Decimal(200),
        settings=E3_WALL_SETTINGS,
    )[0]


def test_a_cross_hatch_is_a_hatch() -> None:
    lines = _hatch_lines(100, 110, 9, 6) + _hatch_lines(100, 113, 9, 6, slope=-1)
    seen, why = hatch_at(
        _ink(lines),
        x_end=Decimal(100),
        side="left",
        y_low=Decimal(100),
        y_high=Decimal(200),
        settings=E3_WALL_SETTINGS,
    )
    assert seen and "cross-hatch" in why


def test_wall_settings_refuse_a_float() -> None:
    from dataclasses import replace

    with pytest.raises(TypeError):
        replace(E3_WALL_SETTINGS, hatch_span_pt=40.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        replace(E3_WALL_SETTINGS, hatch_parallel_count=Decimal(12))  # type: ignore[arg-type]
