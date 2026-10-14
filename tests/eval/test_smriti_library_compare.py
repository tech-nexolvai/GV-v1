"""Pariksha over the key library: a change judged against every registered key of an agent (#1268).

Verification for: `eval/smriti/library_compare.py`. Invented scorecards and invented library
entries only; no database, no model call, no client value.
"""

from __future__ import annotations

import random
from dataclasses import replace
from fractions import Fraction
from uuid import uuid4

import pytest

from eval.metrics import MetricResult
from eval.pariksha import depth as pariksha_depth
from eval.pariksha import kinds as pariksha_kinds
from eval.pariksha import walls as pariksha_walls
from eval.pariksha.compare import compare_scorecards
from eval.pariksha.depth import DEPTH_AGENTS
from eval.pariksha.kinds import KINDS_AGENT, compare_kinds
from eval.pariksha.score import (
    AGREED_RIGHT,
    AGREED_WRONG,
    EXTRA_HELD,
    EXTRA_USABLE,
    FOUND,
    HELD,
    MISSING,
    NOT_ASKED,
    SPLIT,
)
from eval.pariksha.stored import StoredRunError, _choose_run
from eval.pariksha.walls import (
    AUTOMATIC_RIGHT,
    AUTOMATIC_WRONG,
    SEALED_RIGHT,
    SEALED_WRONG,
    WALLS_AGENT,
    compare_walls,
)
from eval.smriti.library import KEY_KINDS, LibraryEntry
from eval.smriti.library_compare import (
    ACCEPT,
    COMPARES,
    EXIT_CODES,
    NEUTRAL,
    NOT_MEASURED,
    OUTCOME_CLASSES,
    REFUSED,
    REJECT,
    KeyResult,
    Side,
    judge_key,
    judge_library,
    nothing_stored,
)
from tests.eval.test_pariksha_scorecard import _card

PAGE_A, PAGE_B = "a" * 64, "b" * 64
Q1, Q2 = f"{PAGE_A[:16]}/p1:slot0:0", f"{PAGE_A[:16]}/p1:slot1:0"


def _entry(name: str = "made-up key", agent: str = "akshara") -> LibraryEntry:
    return LibraryEntry(
        agent=agent,
        kind=KEY_KINDS[agent],
        path=f"/nowhere/{name}.json",
        name=name,
        version="1",
        sha256="f" * 64,
        cases=2,
        confirmed_by="a made-up person",
        label="invented, not human-confirmed",
    )


def _side(
    outcomes: dict[str, str],
    *,
    pages: frozenset[str] = frozenset({PAGE_A}),
    agent: str = "akshara",
) -> Side:
    return Side(card=_card(outcomes, agent=agent), pages=pages)


def _judge(
    before: Side,
    after: Side,
    *,
    name: str = "made-up key",
    pages: frozenset[str] = frozenset({PAGE_A}),
    agent: str = "akshara",
) -> KeyResult:
    return judge_key(_entry(name, agent=agent), pages, before, after)


ACCEPTING = (
    _side({Q1: AGREED_RIGHT, Q2: HELD}),
    _side({Q1: AGREED_RIGHT, Q2: AGREED_RIGHT}),
)
REJECTING = (
    _side({Q1: AGREED_RIGHT, Q2: HELD}),
    _side({Q1: AGREED_RIGHT, Q2: AGREED_WRONG}),
)
UNREAD = (
    _side({Q1: NOT_ASKED, Q2: NOT_ASKED}, pages=frozenset()),
    _side({Q1: NOT_ASKED, Q2: NOT_ASKED}, pages=frozenset()),
)


def test_every_measured_key_accepting_accepts() -> None:
    results = [_judge(*ACCEPTING, name="one"), _judge(*ACCEPTING, name="two")]
    assert [result.status for result in results] == [ACCEPT, ACCEPT]

    verdict = judge_library("akshara", results)
    assert verdict.status == ACCEPT
    assert verdict.exit_code == 0
    assert verdict.verdict.startswith("ACCEPT")


def test_one_rejecting_key_makes_the_verdict_reject() -> None:
    results = [
        _judge(*ACCEPTING, name="accepting"),
        _judge(*REJECTING, name="rejecting"),
        _judge(*ACCEPTING, name="also accepting"),
    ]
    assert results[1].status == REJECT
    assert "1 new wrong" in results[1].reason

    verdict = judge_library("akshara", results)
    assert verdict.status == REJECT
    assert verdict.exit_code == 1
    assert "rejecting" in verdict.verdict
    assert "REJECT" in verdict.render()


def test_a_key_whose_pages_neither_run_read_is_not_measured_never_a_pass() -> None:
    result = _judge(*UNREAD)
    assert result.status == NOT_MEASURED
    assert result.unread == (PAGE_A,)
    assert "needed" in result.reason

    alone = judge_library("akshara", [result])
    assert alone.status == NOT_MEASURED
    assert alone.exit_code == 3
    assert not alone.verdict.startswith("ACCEPT")

    beside = judge_library("akshara", [_judge(*ACCEPTING, name="read"), result])
    assert beside.status == ACCEPT
    # The unread key is named, never counted among the keys that accept.
    assert "1 of 2 keys measured" in beside.verdict
    assert "NOT MEASURED" in beside.verdict
    assert PAGE_A[:12] in beside.render()


def test_a_key_whose_runs_stored_nothing_of_its_kind_is_not_measured() -> None:
    empty = Side(card=None, pages=frozenset(), note="this database holds no stored questions")
    result = _judge(empty, empty)
    assert result.status == NOT_MEASURED
    assert "holds no stored" in result.reason


def test_a_key_read_but_with_none_of_its_questions_asked_is_not_measured() -> None:
    asked_nothing = _side({Q1: NOT_ASKED, Q2: NOT_ASKED})
    result = _judge(asked_nothing, asked_nothing)
    assert result.status == NOT_MEASURED
    assert result.unread == ()
    assert "none of its questions" in result.reason


def test_unread_pages_of_a_measured_key_are_listed_as_not_measured() -> None:
    result = _judge(*ACCEPTING, pages=frozenset({PAGE_A, PAGE_B}))
    assert result.status == ACCEPT
    assert result.unread == (PAGE_B,)

    verdict = judge_library("akshara", [result])
    assert verdict.status == ACCEPT
    assert "NOT MEASURED" in verdict.verdict
    assert PAGE_B[:12] in verdict.render()


def test_a_key_the_after_run_no_longer_read_is_rejected_never_unmeasured() -> None:
    gone = Side(card=None, pages=frozenset(), note="extraction run x holds no stored questions")
    result = _judge(_side({Q1: AGREED_RIGHT, Q2: HELD}), gone)
    assert result.status == REJECT
    assert "no longer asked" in result.reason


def test_a_first_answer_that_is_wrong_rejects_when_the_before_run_read_nothing() -> None:
    nothing = Side(card=None, pages=frozenset(), note="this database holds no stored questions")
    result = _judge(nothing, _side({Q1: AGREED_RIGHT, Q2: AGREED_WRONG}))
    assert result.status == REJECT
    assert "1 new wrong" in result.reason


def test_a_key_scored_from_an_answers_file_is_not_measured_from_a_stored_run() -> None:
    result = judge_key(
        _entry("depths", agent="akshara-depth"),
        frozenset({PAGE_A}),
        Side(card=None, pages=frozenset()),
        Side(card=None, pages=frozenset()),
        unmeasurable="scored from a pariksha-depth-answers/1 file",
    )
    assert result.status == NOT_MEASURED
    assert result.unread == (PAGE_A,)
    assert "answers" in result.reason


def test_a_compare_that_refuses_refuses_the_library_verdict() -> None:
    other_key = Side(card=_card({Q1: AGREED_RIGHT}, key_sha="e" * 64), pages=frozenset({PAGE_A}))
    result = _judge(ACCEPTING[0], other_key)
    assert result.status == REFUSED

    verdict = judge_library("akshara", [_judge(*ACCEPTING, name="fine"), result])
    assert verdict.status == REFUSED
    assert verdict.exit_code == 2
    # A rejecting key still decides: the change is rejected whatever else was refused.
    assert judge_library("akshara", [result, _judge(*REJECTING)]).status == REJECT


def test_a_library_with_no_key_for_the_agent_is_not_measured() -> None:
    verdict = judge_library("akshara", [])
    assert verdict.status == NOT_MEASURED
    assert "no key" in verdict.verdict


def test_each_agent_is_judged_by_its_own_compare() -> None:
    assert COMPARES["akshara"] is compare_scorecards
    assert COMPARES["rekha"] is compare_scorecards
    assert COMPARES["vastu"] is compare_scorecards
    assert COMPARES[WALLS_AGENT] is compare_walls
    assert COMPARES[KINDS_AGENT] is compare_kinds


def test_the_nothing_stored_refusal_is_told_apart_from_other_refusals() -> None:
    for found, wanted in (({}, None), ({uuid4(): 1}, uuid4())):
        with pytest.raises(StoredRunError) as nothing:
            _choose_run(found, wanted, "made-up questions")
        assert nothing_stored(nothing.value)
    with pytest.raises(StoredRunError) as several:
        _choose_run({uuid4(): 1, uuid4(): 2}, None, "made-up questions")
    assert not nothing_stored(several.value)


# --- better somewhere, worse nowhere (orchestrator decision 2026-10-15) ----------------------

UNCHANGED = (
    _side({Q1: AGREED_RIGHT, Q2: HELD}),
    _side({Q1: AGREED_RIGHT, Q2: HELD}),
)


def _walls(outcomes: dict[str, str]) -> Side:
    return _side(outcomes, agent=WALLS_AGENT)


def test_an_unchanged_key_is_neutral_not_rejected() -> None:
    result = _judge(*UNCHANGED)
    assert result.status == NEUTRAL
    assert result.measured
    assert "not better" in result.reason


def test_a_walls_only_improvement_with_the_rows_keys_unchanged_is_accepted() -> None:
    walls = _judge(
        _walls({Q1: AGREED_RIGHT, Q2: HELD}),
        _walls({Q1: AGREED_RIGHT, Q2: AGREED_RIGHT}),
        name="walls",
        agent=WALLS_AGENT,
    )
    rows = [_judge(*UNCHANGED, name="rows one"), _judge(*UNCHANGED, name="rows two")]
    assert walls.status == ACCEPT
    assert [row.status for row in rows] == [NEUTRAL, NEUTRAL]

    verdict = judge_library("rekha", [*rows, walls])
    assert verdict.status == ACCEPT
    assert verdict.exit_code == 0
    assert "better on 1" in verdict.verdict


def test_one_new_wrong_anywhere_rejects_even_beside_an_improvement() -> None:
    walls = _judge(
        _walls({Q1: AGREED_RIGHT, Q2: HELD}),
        _walls({Q1: AGREED_RIGHT, Q2: AGREED_RIGHT}),
        name="walls",
        agent=WALLS_AGENT,
    )
    rows = _judge(*REJECTING, name="rows")
    verdict = judge_library("rekha", [_judge(*UNCHANGED, name="other rows"), walls, rows])
    assert verdict.status == REJECT
    assert verdict.exit_code == 1


def test_everything_unchanged_is_neutral_with_its_own_exit_code() -> None:
    verdict = judge_library("akshara", [_judge(*UNCHANGED, name="one"), _judge(*UNCHANGED)])
    assert verdict.status == NEUTRAL
    assert verdict.exit_code == EXIT_CODES[NEUTRAL] == 4
    assert verdict.verdict.startswith("NEUTRAL")
    assert not verdict.verdict.startswith("ACCEPT")


def test_not_better_with_a_higher_cost_is_rejected_not_neutral() -> None:
    before, after = UNCHANGED
    assert after.card is not None
    costlier = MetricResult(
        key="cost_micros_per_question", value=Fraction(5000, 3), numerator=5000, denominator=3
    )
    worse = Side(
        card=replace(after.card, metrics={**after.card.metrics, costlier.key: costlier}),
        pages=after.pages,
    )
    result = _judge(before, worse)
    assert result.status == REJECT
    assert "worse: cost per question rose" in result.reason


def test_a_real_loss_stays_rejected() -> None:
    lost_one = _judge(
        _side({Q1: AGREED_RIGHT, Q2: AGREED_RIGHT}), _side({Q1: AGREED_RIGHT, Q2: HELD})
    )
    assert lost_one.status == REJECT
    no_longer = _judge(
        _side({Q1: AGREED_RIGHT, Q2: HELD}), _side({Q1: AGREED_RIGHT, Q2: NOT_ASKED})
    )
    assert no_longer.status == REJECT
    assert "no longer asked" in no_longer.reason


# --- worse is judged on counts, not rates (orchestrator decision 2026-10-15) -----------------

Q3 = f"{PAGE_A[:16]}/p1:slot2:0"


def test_more_questions_asked_with_the_same_right_count_is_neutral_not_rejected() -> None:
    """Asked 1 -> 2 (as 34 -> 37): every rate over `asked` moves, but nothing was lost."""
    result = _judge(_side({Q1: AGREED_RIGHT, Q2: NOT_ASKED}), _side({Q1: AGREED_RIGHT, Q2: HELD}))
    assert result.status == NEUTRAL


def test_more_held_on_the_cases_both_runs_asked_is_worse_even_if_the_right_count_holds() -> None:
    """Right 2 -> 2 (a new right answer elsewhere), but on the cases both runs asked the held
    count rose 0 -> 1: judged on that count, the change is worse, never neutral."""
    result = _judge(
        _side({Q1: AGREED_RIGHT, Q2: AGREED_RIGHT, Q3: NOT_ASKED}),
        _side({Q1: HELD, Q2: AGREED_RIGHT, Q3: AGREED_RIGHT}),
    )
    assert result.status == REJECT
    assert "held or unsure on the same asked cases 0 -> 1" in result.reason


def test_a_fall_in_the_right_count_is_worse() -> None:
    result = _judge(
        _side({Q1: AGREED_RIGHT, Q2: AGREED_RIGHT}), _side({Q1: AGREED_RIGHT, Q2: HELD})
    )
    assert result.status == REJECT
    assert "right 2 -> 1" in result.reason


def test_a_lower_cost_alone_is_neither_better_nor_worse() -> None:
    before, after = UNCHANGED
    assert after.card is not None
    cheaper = MetricResult(
        key="cost_micros_per_question", value=Fraction(500, 3), numerator=500, denominator=3
    )
    side = Side(
        card=replace(after.card, metrics={**after.card.metrics, cheaper.key: cheaper}),
        pages=after.pages,
    )
    assert _judge(before, side).status == NEUTRAL


# --- every scorecard type counts its own outcomes (review H1, L4) ----------------------------

LAYOUT, LEFT = f"{PAGE_A[:16]}/p1:walls:layout", f"{PAGE_A[:16]}/p1:walls:left"
ROWS_GAIN = (
    _side({Q1: AGREED_RIGHT, Q2: SPLIT}, agent="rekha"),
    _side({Q1: AGREED_RIGHT, Q2: AGREED_RIGHT}, agent="rekha"),
)


def _walls_change(before: dict[str, str], after: dict[str, str], **after_metrics: int) -> KeyResult:
    after_side = _walls(after)
    assert after_side.card is not None
    if after_metrics:
        before_side = _walls(before)
        assert before_side.card is not None
        extra_before = {
            name.replace("_at_site", "@site"): MetricResult(
                key=name, value=Fraction(1, 2), numerator=1, denominator=2
            )
            for name in after_metrics
        }
        extra_after = {
            name.replace("_at_site", "@site"): MetricResult(
                key=name, value=Fraction(count, 2), numerator=count, denominator=2
            )
            for name, count in after_metrics.items()
        }
        before_side = replace(
            before_side,
            card=replace(before_side.card, metrics={**before_side.card.metrics, **extra_before}),
        )
        after_side = replace(
            after_side,
            card=replace(after_side.card, metrics={**after_side.card.metrics, **extra_after}),
        )
        return _judge(before_side, after_side, name="walls", agent=WALLS_AGENT)
    return _judge(_walls(before), after_side, name="walls", agent=WALLS_AGENT)


def _with_rows_gain(walls: KeyResult) -> str:
    rows = judge_key(_entry("rows", agent="rekha"), frozenset({PAGE_A}), *ROWS_GAIN)
    assert rows.status == ACCEPT
    return judge_library("rekha", [rows, walls]).status


@pytest.mark.parametrize(
    ("layout_after", "worse"),
    [
        (HELD, "right 2 -> 1"),
        (SEALED_RIGHT, "automatic layouts right 1 -> 0"),
    ],
)
def test_an_automatic_layout_that_is_no_longer_automatic_is_a_loss(
    layout_after: str, worse: str
) -> None:
    walls = _walls_change(
        {LAYOUT: AUTOMATIC_RIGHT, LEFT: AGREED_RIGHT}, {LAYOUT: layout_after, LEFT: AGREED_RIGHT}
    )
    assert walls.status == REJECT
    assert worse in walls.reason
    assert _with_rows_gain(walls) == REJECT


def test_a_sealed_layout_that_turns_wrong_is_a_loss() -> None:
    walls = _walls_change(
        {LAYOUT: SEALED_RIGHT, LEFT: AGREED_RIGHT}, {LAYOUT: SEALED_WRONG, LEFT: AGREED_RIGHT}
    )
    assert walls.status == REJECT
    assert "wrong 0 -> 1" in walls.reason
    assert _with_rows_gain(walls) == REJECT


@pytest.mark.parametrize(
    ("metric", "count", "shown"),
    [
        ("ink_right", 0, "ink_right 1 -> 0"),
        ("ink_wrong", 2, "ink_wrong 1 -> 2"),
        ("pair_agreed_right_at_site", 0, "pair_agreed_right@site 1 -> 0"),
        ("pair_agreed_wrong_at_site", 2, "pair_agreed_wrong@site 1 -> 2"),
        ("ink_right_at_site", 0, "ink_right@site 1 -> 0"),
    ],
)
def test_the_walls_counts_kept_in_metrics_are_judged_too(
    metric: str, count: int, shown: str
) -> None:
    same = {LAYOUT: HELD, LEFT: AGREED_RIGHT}
    walls = _walls_change(same, same, **{metric: count})
    assert walls.status == REJECT
    assert shown in walls.reason
    assert _with_rows_gain(walls) == REJECT


def test_an_outcome_the_library_does_not_know_refuses_the_key_never_held() -> None:
    result = _judge(_side({Q1: AGREED_RIGHT}), _side({Q1: "a_new_outcome_word"}))
    assert result.status == REFUSED
    assert "a_new_outcome_word" in result.reason


def test_every_scorecard_type_places_every_outcome_it_can_give() -> None:
    assert set(OUTCOME_CLASSES) == set(KEY_KINDS)
    walls = set(pariksha_walls._PAIR_OUTCOMES) | {
        AUTOMATIC_RIGHT,
        AUTOMATIC_WRONG,
        SEALED_RIGHT,
        SEALED_WRONG,
        pariksha_walls.HELD_LAYOUT,
        pariksha_walls.KEY_UNSURE,
        NOT_ASKED,
        "layout_not_keyed",
    }
    assert walls <= set(OUTCOME_CLASSES[WALLS_AGENT])
    depth = set(pariksha_depth._PAIR_OUTCOMES) | {
        pariksha_depth.KEY_OPEN,
        pariksha_depth.KEY_SINGLE_READ,
        NOT_ASKED,
    }
    for agent in DEPTH_AGENTS:
        assert depth <= set(OUTCOME_CLASSES[agent])
    kinds = {
        pariksha_kinds.RIGHT,
        pariksha_kinds.WRONG,
        pariksha_kinds.UNKNOWN,
        pariksha_kinds.ROW_UNVERIFIED,
        pariksha_kinds.NOT_SCORED,
        NOT_ASKED,
    }
    assert kinds <= set(OUTCOME_CLASSES[KINDS_AGENT])
    assert {AGREED_RIGHT, AGREED_WRONG, HELD, NOT_ASKED} <= set(OUTCOME_CLASSES["akshara"])
    assert {AGREED_RIGHT, AGREED_WRONG, SPLIT, NOT_ASKED} <= set(OUTCOME_CLASSES["rekha"])
    assert {FOUND, MISSING, HELD, EXTRA_USABLE, EXTRA_HELD} <= set(OUTCOME_CLASSES["vastu"])


def test_the_not_better_wording_neutral_relies_on_is_pinned() -> None:
    """NEUTRAL is given only to Pariksha's exact "not better" verdict, alone; any change to that
    wording, or another reason beside it, keeps the key REJECT (fail safe)."""
    unchanged = compare_scorecards(*(side.card for side in UNCHANGED))  # type: ignore[arg-type]
    assert unchanged.verdict.startswith("REJECT (not better: right ")
    assert "; " not in unchanged.verdict
    assert not unchanged.new_wrong and not unchanged.dropped

    before = _card({LAYOUT: AUTOMATIC_RIGHT, LEFT: AGREED_RIGHT}, agent=WALLS_AGENT)
    after = _card({LAYOUT: AUTOMATIC_WRONG, LEFT: AGREED_RIGHT}, agent=WALLS_AGENT)
    walls = compare_walls(before, after)
    assert walls.verdict.startswith("REJECT (not better:")
    assert "; 1 new wrong automatic layout" in walls.verdict
    result = _judge(
        Side(card=before, pages=frozenset({PAGE_A})),
        Side(card=after, pages=frozenset({PAGE_A})),
        agent=WALLS_AGENT,
    )
    assert result.status == REJECT


# --- the library's counts apply on the ACCEPT path too (re-review, 2026-10-15) ---------------

RIGHT_END = f"{PAGE_A[:16]}/p1:walls:right"


@pytest.mark.parametrize(
    ("layout_before", "layout_after", "metrics", "shown"),
    [
        (SEALED_RIGHT, SEALED_WRONG, {}, "wrong 0 -> 1"),
        (AUTOMATIC_RIGHT, HELD, {}, "automatic layouts right 1 -> 0"),
        (AUTOMATIC_RIGHT, SEALED_RIGHT, {}, "automatic layouts right 1 -> 0"),
        (HELD, HELD, {"ink_right": 0}, "ink_right 1 -> 0"),
        (HELD, HELD, {"pair_agreed_wrong_at_site": 2}, "pair_agreed_wrong@site 1 -> 2"),
    ],
)
def test_a_loss_beside_a_gain_in_one_walls_key_is_rejected_though_its_own_compare_accepts(
    layout_before: str, layout_after: str, metrics: dict[str, int], shown: str
) -> None:
    """One end goes unsure -> right (the walls compare's "better"), and in the same key a layout,
    the ink or the site answer gets worse: the walls compare accepts; the library rejects."""
    before = {LEFT: pariksha_walls.ONE_UNSURE, LAYOUT: layout_before}
    after = {LEFT: AGREED_RIGHT, LAYOUT: layout_after}
    own = compare_walls(_walls(before).card, _walls(after).card)  # type: ignore[arg-type]
    assert own.accepted

    walls = _walls_change(before, after, **metrics)
    assert walls.status == REJECT
    assert shown in walls.reason
    assert judge_library("rekha", [walls]).status == REJECT


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({LEFT: AGREED_WRONG, LAYOUT: HELD}, {LEFT: pariksha_walls.ONE_UNSURE, LAYOUT: HELD}),
        ({LEFT: pariksha_walls.ONE_UNSURE, LAYOUT: HELD}, {LEFT: AGREED_RIGHT, LAYOUT: HELD}),
        (
            {LEFT: pariksha_walls.BOTH_UNSURE, RIGHT_END: AGREED_WRONG, LAYOUT: SEALED_RIGHT},
            {LEFT: AGREED_RIGHT, RIGHT_END: pariksha_walls.ONE_UNSURE, LAYOUT: AUTOMATIC_RIGHT},
        ),
    ],
)
def test_a_genuine_walls_improvement_with_no_loss_stays_accepted(
    before: dict[str, str], after: dict[str, str]
) -> None:
    walls = _walls_change(before, after)
    assert walls.status == ACCEPT
    assert judge_library("rekha", [walls]).status == ACCEPT


def test_an_accepted_labels_key_whose_wrong_answers_became_held_stays_accepted() -> None:
    """Every scorecard type takes the ACCEPT-path count check, and "more held on the same cases"
    is deliberately not part of it there: wrong -> held is a real improvement."""
    before = _side({Q1: AGREED_RIGHT, Q2: AGREED_WRONG, Q3: AGREED_WRONG})
    after = _side({Q1: AGREED_RIGHT, Q2: HELD, Q3: HELD})
    assert _judge(before, after).status == ACCEPT


def test_a_genuine_kinds_gain_stays_accepted_through_the_count_check() -> None:
    """The ACCEPT-path count check runs for every scorecard type, kinds included."""
    before = _side({Q1: pariksha_kinds.UNKNOWN, Q2: pariksha_kinds.UNKNOWN}, agent=KINDS_AGENT)
    after = _side({Q1: pariksha_kinds.RIGHT, Q2: pariksha_kinds.UNKNOWN}, agent=KINDS_AGENT)
    assert _judge(before, after, agent=KINDS_AGENT).status == ACCEPT


# --- "better" is judged by the library's own counts too (re-review 2, 2026-10-15) ------------


@pytest.mark.parametrize(
    ("layout_before", "layout_after"),
    [
        (HELD, AUTOMATIC_RIGHT),
        (HELD, SEALED_RIGHT),
        (SEALED_WRONG, SEALED_RIGHT),
        (SEALED_WRONG, HELD),
    ],
)
def test_a_walls_layout_improvement_alone_is_accepted(
    layout_before: str, layout_after: str
) -> None:
    """The walls compare looks at the ends only and says "not better"; by the library's counts
    (right rose, or wrong fell with right held) the layout's gain is a gain."""
    before = {LEFT: AGREED_RIGHT, LAYOUT: layout_before}
    after = {LEFT: AGREED_RIGHT, LAYOUT: layout_after}
    own = compare_walls(_walls(before).card, _walls(after).card)  # type: ignore[arg-type]
    assert not own.accepted

    walls = _walls_change(before, after)
    assert walls.status == ACCEPT
    assert judge_library("rekha", [walls]).status == ACCEPT


def test_an_end_gained_while_a_sealed_layout_is_lost_is_neutral_not_accepted() -> None:
    """The swap: the walls compare accepts the end's gain, but by the library's counts the right
    count holds (a sealed-right layout fell to held), so nothing is better: NEUTRAL."""
    before = {LEFT: pariksha_walls.ONE_UNSURE, LAYOUT: SEALED_RIGHT}
    after = {LEFT: AGREED_RIGHT, LAYOUT: HELD}
    own = compare_walls(_walls(before).card, _walls(after).card)  # type: ignore[arg-type]
    assert own.accepted

    walls = _walls_change(before, after)
    assert walls.status == NEUTRAL
    assert judge_library("rekha", [walls]).status == NEUTRAL


# --- Anant's rule per case: no case may become wrong (final re-review, 2026-10-15) -----------

LAYOUT_2 = f"{PAGE_B[:16]}/p2:walls:layout"


def test_a_wrong_layout_fixed_while_a_held_layout_turns_wrong_is_rejected() -> None:
    """Wrong stays 1 -> 1 and right rises, so the totals look better; but one layout is newly
    wrong, and 0 new wrong is per case."""
    before = {LEFT: AGREED_RIGHT, LAYOUT: SEALED_WRONG, LAYOUT_2: HELD}
    after = {LEFT: AGREED_RIGHT, LAYOUT: SEALED_RIGHT, LAYOUT_2: SEALED_WRONG}
    walls = _walls_change(before, after)
    assert walls.status == REJECT
    assert "newly wrong" in walls.reason
    assert LAYOUT_2 in walls.reason
    assert judge_library("rekha", [walls]).status == REJECT


def test_a_wrong_end_fixed_while_another_end_turns_wrong_is_rejected() -> None:
    before = {LEFT: AGREED_WRONG, RIGHT_END: pariksha_walls.ONE_UNSURE, LAYOUT: HELD}
    after = {LEFT: AGREED_RIGHT, RIGHT_END: AGREED_WRONG, LAYOUT: AUTOMATIC_RIGHT}
    walls = _walls_change(before, after)
    assert walls.status == REJECT
    assert RIGHT_END in walls.reason


_MEASURED_AGENTS = ("akshara", "rekha", "vastu", WALLS_AGENT, KINDS_AGENT)


@pytest.mark.parametrize("agent", _MEASURED_AGENTS)
def test_no_random_change_is_accepted_with_a_case_newly_wrong_dropped_or_fewer_right(
    agent: str,
) -> None:
    """A seeded random search, like the re-review's: random before/after outcomes over a few
    cases, and every ACCEPT checked against the rule itself, case by case."""
    classes = OUTCOME_CLASSES[agent]
    outcomes = sorted(classes)
    cases = [f"{PAGE_A[:16]}/p1:case{index}" for index in range(5)]
    generator = random.Random(1268)
    accepted = 0
    for _ in range(20_000):
        before = {case: generator.choice(outcomes) for case in cases}
        after = {case: generator.choice(outcomes) for case in cases}
        result = _judge(_side(before, agent=agent), _side(after, agent=agent), agent=agent)
        if result.status != ACCEPT:
            continue
        accepted += 1
        was = {case: classes[outcome] for case, outcome in before.items()}
        now = {case: classes[outcome] for case, outcome in after.items()}
        for case in cases:
            assert not (now[case] == "wrong" and was[case] != "wrong"), (before, after)
            assert not (was[case] != "unmeasured" and now[case] == "unmeasured"), (before, after)
        assert sum(c == "right" for c in now.values()) >= sum(c == "right" for c in was.values())
    assert accepted  # not vacuous: the search does reach ACCEPT
