"""Walls by two readers' agreement, code's wall ink as the second judgment (#992, #1202).

Verification for `extraction/slot_reader/walls.py`. Every drawing here is invented.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from extraction.geometry.rows import Box, InkCurve, InkLine, PageInk
from extraction.slot_reader.walls import (
    BACK_AND_LEFT,
    BACK_AND_RIGHT,
    BACK_LEFT_RIGHT,
    BACK_ONLY,
    E3_WALL_SETTINGS,
    WALL_PROMPT,
    WALL_PROMPT_ID,
    Side,
    WallAnswer,
    WallInkSeen,
    _bin,
    band_at,
    hatch_at,
    seal_walls,
    tall_line_at,
)
from vocabulary.wall_layouts import AUTOMATIC_WALL_SOURCE, READERS_WALL_SOURCE
from workflow.layout_proposals import READER_AGREEMENT_PROMPT_IDS

QWEN = "qwen.qwen3-vl-235b-a22b"
KIMI = "us.moonshotai.kimi-k3"
OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
YES, NO, UNSURE = Side.YES, Side.NO, Side.UNSURE


def answer(
    model: str, left: Side, right: Side, behind: Side = UNSURE, view: str = "elevation"
) -> WallAnswer:
    return WallAnswer(model, left, right, behind, view)  # type: ignore[arg-type]


def both(
    left: Side, right: Side, behind: Side = UNSURE, view: str = "elevation"
) -> tuple[WallAnswer, WallAnswer]:
    return answer(KIMI, left, right, behind, view), answer(QWEN, left, right, behind, view)


NO_HATCH = WallInkSeen(False, False)


def test_walls_at_both_ends_seal_back_left_right() -> None:
    outcome = seal_walls(both(YES, YES), ink=NO_HATCH, row_ambiguity=None)
    assert outcome.config == BACK_LEFT_RIGHT and outcome.code is None


def test_no_walls_on_an_elevation_leave_the_back_wall_to_the_person() -> None:
    outcome = seal_walls(both(NO, NO, YES), ink=NO_HATCH, row_ambiguity=None)
    assert outcome.config is None and outcome.code == "back-wall-unknown"


def test_no_end_walls_on_an_agreed_plan_with_a_back_wall_seal_back_only() -> None:
    outcome = seal_walls(both(NO, NO, YES, "plan"), ink=NO_HATCH, row_ambiguity=None)
    assert outcome.config == BACK_ONLY
    # Not when one reader calls it an elevation, nor when the back wall is not agreed.
    mixed = (answer(KIMI, NO, NO, YES, "plan"), answer(QWEN, NO, NO, YES, "elevation"))
    assert seal_walls(mixed, ink=NO_HATCH, row_ambiguity=None).config is None
    unsure = (answer(KIMI, NO, NO, YES, "plan"), answer(QWEN, NO, NO, UNSURE, "plan"))
    assert seal_walls(unsure, ink=NO_HATCH, row_ambiguity=None).config is None


@pytest.mark.parametrize(
    "answers",
    [
        both(YES, UNSURE),
        both(UNSURE, YES),
        both(NO, UNSURE),
        (answer(KIMI, YES, YES), answer(QWEN, YES, NO)),
        (answer(KIMI, YES, YES), answer(QWEN, UNSURE, YES)),
    ],
)
def test_anything_but_agreement_on_a_known_layout_goes_to_the_person(
    answers: tuple[WallAnswer, WallAnswer],
) -> None:
    outcome = seal_walls(answers, ink=NO_HATCH, row_ambiguity=None)
    assert outcome.config is None and outcome.reason


def test_a_wall_at_one_end_and_an_agreed_open_end_seal_that_end() -> None:
    """#1138: one wall end is one field cut, so the row is checkable; the side names the layout."""
    for left, right, layout in ((YES, NO, BACK_AND_LEFT), (NO, YES, BACK_AND_RIGHT)):
        outcome = seal_walls(both(left, right), ink=NO_HATCH, row_ambiguity=None)
        assert outcome.config == layout and outcome.code is None
        assert (outcome.left, outcome.right) == (left, right)
        # Sealed by the readers alone: a proposal the row's reviewer must still confirm.
        assert outcome.source == "readers"


def test_one_end_needs_both_readers_and_code_ink_never_fills_an_end() -> None:
    """#1202: code's ink never stands in for a reader's answer; only both readers set each end."""
    ink_and_no = seal_walls(both(UNSURE, NO), ink=WallInkSeen(True, False), row_ambiguity=None)
    assert ink_and_no.config is None
    ink_only = seal_walls(both(UNSURE, UNSURE), ink=WallInkSeen(True, True), row_ambiguity=None)
    assert ink_only.config is None
    split = (answer(KIMI, YES, NO), answer(QWEN, YES, UNSURE))
    assert seal_walls(split, ink=NO_HATCH, row_ambiguity=None).config is None
    hatched = seal_walls(both(YES, NO), ink=WallInkSeen(False, True), row_ambiguity=None)
    assert hatched.config is None and hatched.code == "hatch-contradiction"
    ambiguous = seal_walls(both(YES, NO), ink=NO_HATCH, row_ambiguity="another row fits")
    assert ambiguous.config is None


def test_one_reader_alone_never_seals() -> None:
    outcome = seal_walls((answer(QWEN, YES, YES),), ink=NO_HATCH, row_ambiguity=None)
    assert outcome.config is None and outcome.code == "one-reader-missing"


def test_two_readers_of_one_maker_are_refused() -> None:
    for pair in (
        (answer(QWEN, YES, YES), answer(QWEN, YES, YES)),
        (
            answer("anthropic.claude-opus-5-5", YES, YES),
            answer("anthropic.claude-sonnet-5-5", YES, YES),
        ),
    ):
        outcome = seal_walls(pair, ink=None, row_ambiguity=None)
        assert outcome.config is None and outcome.code == "reader-independence"
        assert "reviewer must choose" in (outcome.reason or "")


def test_the_approved_claude_pair_can_only_propose_after_exact_agreement() -> None:
    pair = (answer(OPUS, YES, YES), answer(SONNET, YES, YES))
    held = seal_walls(pair, ink=None, row_ambiguity=None)
    proposed = seal_walls(pair, ink=None, row_ambiguity=None, allow_claude_pair=True)

    assert held.code == "reader-independence"
    assert proposed.config == BACK_LEFT_RIGHT and proposed.source == "readers"


# ---------------------------------------------------------------------------------------------
# #1202: "walls at both ends" is automatic only on two judgments, and nothing else is
# ---------------------------------------------------------------------------------------------

CLAUDE_YES_YES = (answer(OPUS, YES, YES), answer(SONNET, YES, YES))


def _sealed(
    answers: tuple[WallAnswer, ...], ink: WallInkSeen | None, *, stone_meets_walls: bool = True
) -> tuple[str | None, str | None]:
    outcome = seal_walls(
        answers,
        ink=ink,
        row_ambiguity=None,
        stone_meets_walls=stone_meets_walls,
        allow_claude_pair=True,
    )
    return outcome.config, outcome.source


def test_code_ink_at_both_ends_and_both_ais_and_stone_to_the_walls_is_automatic() -> None:
    assert _sealed(CLAUDE_YES_YES, WallInkSeen(True, True)) == (
        BACK_LEFT_RIGHT,
        AUTOMATIC_WALL_SOURCE,
    )


def test_code_ink_at_only_one_end_goes_to_the_reviewer() -> None:
    for ink in (WallInkSeen(True, False), WallInkSeen(False, True)):
        assert _sealed(CLAUDE_YES_YES, ink) == (BACK_LEFT_RIGHT, READERS_WALL_SOURCE)


def test_both_ais_agreeing_with_no_code_ink_goes_to_the_reviewer() -> None:
    assert _sealed(CLAUDE_YES_YES, WallInkSeen(False, False)) == (
        BACK_LEFT_RIGHT,
        READERS_WALL_SOURCE,
    )
    assert _sealed(CLAUDE_YES_YES, None) == (BACK_LEFT_RIGHT, READERS_WALL_SOURCE)


def test_code_ink_and_both_ais_but_stone_stopping_at_panels_goes_to_the_reviewer() -> None:
    assert _sealed(CLAUDE_YES_YES, WallInkSeen(True, True), stone_meets_walls=False) == (
        BACK_LEFT_RIGHT,
        READERS_WALL_SOURCE,
    )


def test_code_ink_never_decides_without_both_ais_saying_both_ends() -> None:
    for answers in (
        (answer(OPUS, YES, UNSURE), answer(SONNET, YES, YES)),
        (answer(OPUS, UNSURE, UNSURE), answer(SONNET, UNSURE, UNSURE)),
        (answer(OPUS, YES, YES),),
    ):
        config, source = _sealed(answers, WallInkSeen(True, True))
        assert source != AUTOMATIC_WALL_SOURCE and config is None


def test_an_island_drawn_with_a_wall_hatch_at_one_end_is_never_automatic() -> None:
    """The reception-desk case: the vendor hatched one end of a free-standing desk."""
    one_end = WallInkSeen(False, True)
    for answers in (
        (answer(OPUS, NO, YES), answer(SONNET, NO, YES)),
        CLAUDE_YES_YES,
        (answer(OPUS, UNSURE, YES), answer(SONNET, UNSURE, YES)),
    ):
        _config, source = _sealed(answers, one_end)
        assert source != AUTOMATIC_WALL_SOURCE
    one_wall = _sealed((answer(OPUS, NO, YES), answer(SONNET, NO, YES)), one_end)
    assert one_wall == (BACK_AND_RIGHT, READERS_WALL_SOURCE)


def test_every_other_layout_is_only_a_readers_proposal() -> None:
    ink = WallInkSeen(True, True)
    for answers, layout in (
        ((answer(OPUS, YES, NO), answer(SONNET, YES, NO)), BACK_AND_LEFT),
        ((answer(OPUS, NO, YES), answer(SONNET, NO, YES)), BACK_AND_RIGHT),
    ):
        # Wall ink at an end a reader calls open objects; with no ink the readers propose.
        assert _sealed(answers, ink)[0] is None
        assert _sealed(answers, WallInkSeen(False, False)) == (layout, READERS_WALL_SOURCE)
    plan = (answer(OPUS, NO, NO, YES, "plan"), answer(SONNET, NO, NO, YES, "plan"))
    assert _sealed(plan, WallInkSeen(False, False)) == (BACK_ONLY, READERS_WALL_SOURCE)


def test_a_row_that_may_not_be_the_countertop_never_seals() -> None:
    outcome = seal_walls(both(YES, YES), ink=NO_HATCH, row_ambiguity="another row fits as well")
    assert outcome.config is None and outcome.code == "row-ambiguous"


def test_a_hatch_where_a_reader_says_no_wall_goes_to_the_person() -> None:
    hatch = WallInkSeen(left=True, right=False)
    agreed_no = seal_walls(both(NO, NO, YES, "plan"), ink=hatch, row_ambiguity=None)
    assert agreed_no.config is None and agreed_no.code == "hatch-contradiction"
    one_says_no = (answer(KIMI, NO, YES), answer(QWEN, YES, YES))
    held = seal_walls(one_says_no, ink=hatch, row_ambiguity=None)
    assert held.code == "hatch-contradiction"


def test_a_hatch_never_seals_and_seeing_none_proves_nothing() -> None:
    unsure = seal_walls(both(UNSURE, UNSURE), ink=WallInkSeen(True, True), row_ambiguity=None)
    assert unsure.config is None
    agreed = seal_walls(both(YES, YES), ink=WallInkSeen(True, True), row_ambiguity=None)
    # Without the stone question answered, even ink and both readers are only a proposal.
    assert agreed.config == BACK_LEFT_RIGHT and agreed.source == READERS_WALL_SOURCE


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


# ---------------------------------------------------------------------------------------------
# #1202: code's wall ink — a hatch running up the end, a solid band, a tall wall line
# ---------------------------------------------------------------------------------------------


def _ink_with(lines: tuple[InkLine, ...] = (), curves: tuple[InkCurve, ...] = ()) -> PageInk:
    return PageInk(
        width=Decimal(600),
        height=Decimal(400),
        lines=lines,
        curves=curves,
        characters=(),
        drawing_boxes=(),
    )


def _hatch(x_end: int, side: str = "left") -> dict[str, object]:
    return {
        "x_end": Decimal(x_end),
        "side": side,
        "y_low": Decimal(100),
        "y_high": Decimal(200),
        "settings": E3_WALL_SETTINGS,
    }


def test_a_hatch_lying_across_the_end_is_not_a_wall() -> None:
    """Long, shallow strokes passing the end: a band lying across it (wider than tall), such as a
    plan's back wall, is not a wall at the end."""
    across = tuple(
        InkLine(
            Decimal(72 + 2 * index) - Decimal(25),
            Decimal(100 + 3 * index),
            Decimal(72 + 2 * index) + Decimal(25),
            Decimal(104 + 3 * index),
        )
        for index in range(16)
    )
    seen, why = hatch_at(_ink_with(across), **_hatch(100))  # type: ignore[arg-type]
    assert not seen and "lying across the end" in why


def test_a_hatch_band_that_runs_on_into_the_row_is_not_a_wall_at_its_end() -> None:
    beside = _hatch_lines(100, 110, 14, 5)
    running_on = tuple(
        InkLine(line.x0 + Decimal(20), line.y0, line.x1 + Decimal(20), line.y1) for line in beside
    )
    assert hatch_at(_ink(beside), **_hatch(100))[0]  # type: ignore[arg-type]
    seen, why = hatch_at(_ink(beside + running_on), **_hatch(100))  # type: ignore[arg-type]
    assert not seen and "runs on into the row" in why


def _band(x0: int, x1: int, top: int, bottom: int, lightness: str | None) -> InkCurve:
    box = Box(Decimal(x0), Decimal(top), Decimal(x1), Decimal(bottom))
    return InkCurve(
        box=box,
        points=((box.x0, box.top), (box.x1, box.top), (box.x1, box.bottom), (box.x0, box.bottom)),
        closed=True,
        fill_lightness=None if lightness is None else Decimal(lightness),
    )


def test_a_solid_grey_band_beside_the_end_is_wall_ink() -> None:
    seen, why = band_at(_ink_with(curves=(_band(84, 100, 100, 190, "0.73"),)), **_hatch(100))  # type: ignore[arg-type]
    assert seen and "band" in why
    right, _ = band_at(
        _ink_with(curves=(_band(300, 316, 100, 190, "0"),)), **_hatch(300, "right")  # type: ignore[arg-type]
    )
    assert right


@pytest.mark.parametrize(
    "band",
    [
        _band(84, 100, 100, 190, "1"),  # white: paper, not a wall
        _band(84, 100, 100, 190, None),  # an outline, not filled
        _band(84, 100, 100, 120, "0.5"),  # too short for the drawing's height
        _band(84, 160, 100, 190, "0.5"),  # reaches into the row
        _band(20, 100, 150, 190, "0.5"),  # wider than tall
        _band(40, 60, 100, 190, "0.5"),  # well away from the end
    ],
)
def test_other_fills_are_not_a_wall_band(band: InkCurve) -> None:
    assert not band_at(_ink_with(curves=(band,)), **_hatch(100))[0]  # type: ignore[arg-type]


def _cabinet_and_wall(wall_top: int, wall_bottom: int, wall_x: int = 98) -> PageInk:
    """A cabinet box beside the left end (top at 140) and an upright wall line at the end."""
    cabinet = _band(102, 130, 140, 200, None)
    wall = InkLine(Decimal(wall_x), Decimal(wall_top), Decimal(wall_x), Decimal(wall_bottom))
    return _ink_with(lines=(wall,), curves=(cabinet,))


def _tall(ink: PageInk, frame: int = 400) -> tuple[bool, str]:
    return tall_line_at(
        ink,
        x_end=Decimal(100),
        side="left",
        row_width=Decimal(200),
        y_low=Decimal(40),
        y_high=Decimal(200),
        frame_height=Decimal(frame),
        settings=E3_WALL_SETTINGS,
    )


def test_a_tall_wall_line_rising_above_the_cabinets_is_wall_ink() -> None:
    seen, why = _tall(_cabinet_and_wall(60, 200))
    assert seen and "tall wall line" in why


@pytest.mark.parametrize(
    ("top", "bottom", "x", "frame"),
    [
        (120, 200, 98, 400),  # not 40 points above the cabinets
        (60, 120, 98, 400),  # does not come down to the floor
        (60, 200, 40, 400),  # too far outside the end
        (60, 200, 98, 150),  # as tall as the pasted drawing: its frame
    ],
)
def test_other_upright_lines_are_not_a_wall(top: int, bottom: int, x: int, frame: int) -> None:
    assert not _tall(_cabinet_and_wall(top, bottom, x), frame)[0]


def test_wall_settings_refuse_a_float() -> None:
    from dataclasses import replace

    with pytest.raises(TypeError):
        replace(E3_WALL_SETTINGS, hatch_span_pt=40.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        replace(E3_WALL_SETTINGS, hatch_parallel_count=Decimal(12))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# The wall question, v2 (#1111): every field defined, old answers unchanged
# ---------------------------------------------------------------------------------------------


def _wall_definition(field: str) -> str:
    """The prompt's definition of one answer field: the line that starts `- "field"`."""
    lines = [line for line in WALL_PROMPT.splitlines() if line.startswith("- ") and field in line]
    assert len(lines) == 1, f"{field} is defined once, on its own line: {lines}"
    return lines[0]


def test_the_wall_question_defines_every_field_with_a_yes_and_a_no_example() -> None:
    from extraction.slot_reader.claude_output import WALL_SCHEMA

    assert WALL_PROMPT_ID == "slot-walls-v2"
    for field in WALL_SCHEMA["properties"]:  # type: ignore[attr-defined]
        assert f'"{field}"' in _wall_definition(f'"{field}"'), field
    for side in ("left", "right", "behind"):
        definition = _wall_definition(f'- "{side}"')
        assert "Example yes:" in definition and "Example no:" in definition, side
    view = _wall_definition('- "view"')
    assert "Picture 2" in view
    for kind in ('"elevation"', '"plan"', '"other"'):
        assert kind in view
    assert "Example plan:" in view and "Example not plan" in view
    evidence = _wall_definition('"left_evidence"')
    assert '"right_evidence"' in evidence and '"behind_evidence"' in evidence
    assert "a few words" in evidence and "Example:" in evidence and "Not an example:" in evidence


def test_the_wall_question_says_how_to_judge_a_back_wall_in_an_elevation() -> None:
    behind = _wall_definition('- "behind"')
    assert "In an elevation" in behind and "In a plan" in behind


def test_a_wall_to_wall_dimension_counts_only_where_its_ends_meet_the_runs_ends() -> None:
    assert "wall-to-wall dimension is evidence for an end ONLY when" in WALL_PROMPT
    assert "a run can be shorter than wall to wall" in WALL_PROMPT


def test_the_vendors_words_no_longer_decide_a_wall_and_the_question_is_unchanged() -> None:
    """#1202: words are evidence only and `seal_walls` takes none. The question keeps its id and
    text (v2), so stored answers still replay; it still tells the reader to answer from the
    pictures."""
    assert WALL_PROMPT_ID == "slot-walls-v2"
    assert "Still answer only from what you see in the pictures." in WALL_PROMPT
    outcome = seal_walls(both(NO, NO), ink=NO_HATCH, row_ambiguity=None)
    assert outcome.config is None and outcome.code == "back-wall-unknown"


def test_the_wall_marks_are_named_magenta_and_reviewer_colours_are_not_ours() -> None:
    assert "magenta" in WALL_PROMPT and "no reviewer markup uses" in WALL_PROMPT
    assert "Red or yellow marks are a reviewer's markup" in WALL_PROMPT


def test_both_wall_ids_are_recognised_where_stored_runs_are_replayed() -> None:
    from extraction.slot_reader.walls import WALL_PROMPT_IDS

    assert WALL_PROMPT_IDS == {"slot-walls-v2", "slot-walls-v1"}
    assert WALL_PROMPT_IDS <= READER_AGREEMENT_PROMPT_IDS


#: Stored wall answers (the shape is the same for v1 and v2), the two readers' pair, and the
#: outcome `seal_walls` gave them before #1111. Invented.
_STORED_WALLS = [
    (
        ("yes", "yes", "unsure", "elevation"),
        ("yes", "yes", "no", "elevation"),
        BACK_LEFT_RIGHT,
        None,
    ),
    (("no", "no", "yes", "plan"), ("no", "no", "yes", "plan"), BACK_ONLY, None),
    (("no", "no", "yes", "elevation"), ("no", "no", "yes", "plan"), None, "back-wall-unknown"),
    (("yes", "no", "yes", "plan"), ("yes", "yes", "yes", "plan"), None, "walls-not-agreed"),
    (
        ("yes", "unsure", "unsure", "other"),
        ("yes", "yes", "unsure", "other"),
        None,
        "walls-not-agreed",
    ),
]


@pytest.mark.parametrize(("first", "second", "config", "code"), _STORED_WALLS)
def test_stored_wall_answers_parse_and_seal_exactly_as_before(
    first: tuple[str, str, str, str],
    second: tuple[str, str, str, str],
    config: str | None,
    code: str | None,
) -> None:
    import json

    from extraction.slot_reader.bedrock import _wall_answer, _WallReply, parse_stored_reader_answer

    def stored(model: str, sides: tuple[str, str, str, str]) -> WallAnswer:
        left, right, behind, view = sides
        raw = json.dumps(
            {
                "left": left,
                "right": right,
                "behind": behind,
                "left_evidence": "synthetic",
                "right_evidence": "synthetic",
                "behind_evidence": "synthetic",
                "view": view,
            }
        )
        return _wall_answer(model, _WallReply.model_validate(parse_stored_reader_answer(raw)))

    outcome = seal_walls(
        (stored(KIMI, first), stored(QWEN, second)), ink=NO_HATCH, row_ambiguity=None
    )
    assert (outcome.config, outcome.code) == (config, code)
