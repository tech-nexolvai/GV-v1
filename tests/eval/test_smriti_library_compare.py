"""Pariksha over the key library: a change judged against every registered key of an agent (#1268).

Verification for: `eval/smriti/library_compare.py`. Invented scorecards and invented library
entries only; no database, no model call, no client value.
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from uuid import uuid4

import pytest

from eval.metrics import MetricResult
from eval.pariksha.compare import compare_scorecards
from eval.pariksha.kinds import KINDS_AGENT, compare_kinds
from eval.pariksha.score import AGREED_RIGHT, AGREED_WRONG, HELD, NOT_ASKED
from eval.pariksha.stored import StoredRunError, _choose_run
from eval.pariksha.walls import WALLS_AGENT, compare_walls
from eval.smriti.library import KEY_KINDS, LibraryEntry
from eval.smriti.library_compare import (
    ACCEPT,
    COMPARES,
    EXIT_CODES,
    NEUTRAL,
    NOT_MEASURED,
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


def test_not_better_with_a_metric_worse_is_rejected_not_neutral() -> None:
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
    assert "worse: cost_micros_per_question" in result.reason


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
