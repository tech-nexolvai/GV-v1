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
    CodeWallClues,
    HatchSeen,
    Side,
    WallAnswer,
    _bin,
    code_wall_outcome,
    hatch_at,
    seal_walls,
)
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
    for pair in (
        (answer(QWEN, YES, YES), answer(QWEN, YES, YES)),
        (
            answer("anthropic.claude-opus-5-5", YES, YES),
            answer("anthropic.claude-sonnet-5-5", YES, YES),
        ),
    ):
        outcome = seal_walls(pair, hatch=None, row_ambiguity=None)
        assert outcome.config is None and outcome.code == "reader-independence"
        assert "reviewer must choose" in (outcome.reason or "")


def test_the_approved_claude_pair_can_only_propose_after_exact_agreement() -> None:
    pair = (answer(OPUS, YES, YES), answer(SONNET, YES, YES))
    held = seal_walls(pair, hatch=None, row_ambiguity=None)
    proposed = seal_walls(pair, hatch=None, row_ambiguity=None, allow_claude_pair=True)

    assert held.code == "reader-independence"
    assert proposed.config == BACK_LEFT_RIGHT and proposed.source == "readers"


def test_code_clues_only_set_positive_ends_and_never_infer_an_open_end() -> None:
    both_ends = code_wall_outcome(CodeWallClues(left=True, right=True), row_ambiguity=None)
    assert both_ends is not None and both_ends.config == BACK_LEFT_RIGHT
    assert code_wall_outcome(CodeWallClues(left=True, right=None), row_ambiguity=None) is None
    assert code_wall_outcome(CodeWallClues(left=None, right=None), row_ambiguity=None) is None
    assert (
        code_wall_outcome(CodeWallClues(left=True, right=True), row_ambiguity="uncertain row")
        is None
    )


def test_partial_code_clue_needs_both_readers_to_agree_on_the_other_end() -> None:
    accepted = seal_walls(
        both(YES, YES),
        hatch=NO_HATCH,
        row_ambiguity=None,
        code_clues=CodeWallClues(left=True),
    )
    disagreement = seal_walls(
        (answer(OPUS, YES, YES), answer(SONNET, YES, NO)),
        hatch=NO_HATCH,
        row_ambiguity=None,
        code_clues=CodeWallClues(left=True),
        allow_claude_pair=True,
    )

    assert accepted.config == BACK_LEFT_RIGHT and accepted.source == "drawing-and-readers"
    assert disagreement.config is None and disagreement.code == "walls-not-agreed"


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


def test_the_wall_question_says_the_vendors_words_may_decide_a_wall() -> None:
    """`seal_walls` lets both ends' vendor-word clues decide before any reader's no (unchanged);
    the question now says so instead of leaving the reader to think its answer always counts."""
    assert "code decides the walls whatever the answers say" in WALL_PROMPT
    outcome = seal_walls(
        both(NO, NO), hatch=NO_HATCH, row_ambiguity=None, code_clues=CodeWallClues(True, True)
    )
    assert outcome.config == BACK_LEFT_RIGHT and outcome.source == "vendor-drawing-clues"


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
        (stored(KIMI, first), stored(QWEN, second)), hatch=NO_HATCH, row_ambiguity=None
    )
    assert (outcome.config, outcome.code) == (config, code)
