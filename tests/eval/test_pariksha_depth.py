"""Pariksha's depth and view-kind scorecards (`rekha-view`, `akshara-depth`, #1257).

Invented keys and invented reader answers only: no client value, no database, no model call.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from eval.pariksha.compare import compare_scorecards
from eval.pariksha.depth import (
    AGREED_RIGHT,
    AGREED_WRONG,
    ANSWERS_FORMAT,
    BOTH_UNSURE,
    DEPTH_AGENT,
    DISAGREED,
    HELD,
    HELD_AS_KEY,
    KEY_OPEN,
    KEY_PROVENANCE,
    KEY_SINGLE_READ,
    NO_ANSWER,
    NOT_ASKED,
    ONE_UNSURE,
    VIEW_AGENT,
    DepthAnswersError,
    HoldTruth,
    KindTruth,
    ValueTruth,
    compare_depth,
    describe_key,
    key_counts,
    load_depth_answers,
    load_depth_key,
    read_depth_key,
    score_depth,
)
from eval.pariksha.keys import DEPTH_FAMILIES, KeyFormatError, load_key
from eval.pariksha.scorecard import read_scorecard, write_scorecard
from eval.pariksha.walls import compare_walls
from eval.regression import IncomparableRuns

PAGE = "a" * 64
OTHER_PAGE = "b" * 64
COMMIT = "abc1234"
SOURCE = "made up for a test"


# --- invented keys --------------------------------------------------------------------------


def _depth_case(question_id: str, field: str, answer: Mapping[str, Any], **extra: Any) -> dict:
    case: dict[str, Any] = {
        "family": "depth-span",
        "page_sha256": PAGE,
        "set": "Made-up Set",
        "page": 4,
        "view": "plan",
        "field": field,
        "statement_id": 1,
        "confidence": "high",
        "status": "claude-double-read",
        "source": SOURCE,
        "question_id": question_id,
        "answer": dict(answer),
    }
    case.update(extra)
    return case


def depth_key(*cases: dict) -> dict:
    return {
        "format": "pariksha-key/1",
        "agent": DEPTH_AGENT,
        "name": "made-up depths",
        "version": "1",
        "status": "claude-double-read",
        "note": "invented for a test",
        "documents": {"sX": {"pdf": "/nowhere/made-up.pdf", "sha256": "c" * 64}},
        "cases": list(cases)
        or [
            _depth_case("p3:plan:CT010:depth", "CT010", {"texts": ['25 1/2"']}),
            _depth_case(
                "p3:plan:CUTOUT:hole",
                "CT012+CT008",
                {
                    "CT012": {"texts": ['19"']},
                    "CT008": {"texts": ['13 1/4"']},
                    "outline": "the one outline",
                },
            ),
            _depth_case(
                "p3:plan:CUTOUT:two-outlines",
                "CT012+CT008",
                {"held": "two outlines and no word"},
            ),
            _depth_case(
                "p3:plan:CT007:front",
                "CT007",
                {"hold": "front offset printed from the stone edge", "start_edge": "stone edge"},
            ),
            _depth_case(
                "p3:section:CT010:doubt",
                "CT010",
                {"texts": ['31"']},
                view="section",
                status="open",
                open_reason="the two readings disagree",
            ),
        ],
    }


def _view_case(question_id: str, kind: str, status: str = "claude-double-read", **extra: Any):
    case: dict[str, Any] = {
        "family": "view-kind",
        "page_sha256": PAGE,
        "question_id": question_id,
        "set": "Made-up Set",
        "page": 4,
        "region_pt": [10, 20, 300, 400],
        "heading_printed": "MADE-UP HEADING",
        "answer": {"kind": kind},
        "heading_agrees": True,
        "status": status,
        "statement_id": 1,
        "source": SOURCE,
    }
    case.update(extra)
    return case


def view_key() -> dict:
    return {
        "format": "pariksha-key/1",
        "agent": VIEW_AGENT,
        "name": "made-up views",
        "version": "1",
        "cases": [
            _view_case("p3:view@10,20,300,400", "plan"),
            _view_case("p3:view@10,500,300,700", "side"),
            _view_case("p3:view@400,20,600,400", "elevation", status="claude-single-read"),
            _view_case(
                "p5:view@1,2,3,4", "section", status="open", open_reason="the readings disagree"
            ),
        ],
    }


# --- invented reader answers ----------------------------------------------------------------


def answers(agent: str, *entries: dict, run: str = "made-up run") -> dict:
    return {
        "format": ANSWERS_FORMAT,
        "agent": agent,
        "run": run,
        "prompt_ids": ["made-up-prompt-v1"],
        "answers": list(entries),
    }


def entry(question_id: str, field: str, first: dict, second: dict, page: str = PAGE) -> dict:
    return {
        "page_sha256": page,
        "question_id": question_id,
        "field": field,
        "readers": [{"model_id": "reader-a", **first}, {"model_id": "reader-b", **second}],
    }


def value(text: str) -> dict:
    return {"value": text}


UNSURE = {"unsure": True}


def held(reason: str = "a reason") -> dict:
    return {"held": reason}


def good_depth_answers(**override: tuple[dict, dict]) -> dict:
    """Both readers right everywhere the key has a double-read value."""
    pairs: dict[str, tuple[str, str, dict, dict]] = {
        "depth": ("p3:plan:CT010:depth", "CT010", value('25 1/2"'), value("648 [25 1/2]")),
        "width": ("p3:plan:CUTOUT:hole", "CT012", value('19"'), value('19"')),
        "front_back": ("p3:plan:CUTOUT:hole", "CT008", value('13 1/4"'), value('13 1/4"')),
        "two_w": ("p3:plan:CUTOUT:two-outlines", "CT012", held(), held()),
        "two_d": ("p3:plan:CUTOUT:two-outlines", "CT008", held(), held()),
        "front": ("p3:plan:CT007:front", "CT007", held("from the stone edge"), held()),
    }
    entries = []
    for name, (question_id, field, first, second) in pairs.items():
        if name in override:
            first, second = override[name]
        entries.append(entry(question_id, field, first, second))
    return answers(DEPTH_AGENT, *entries)


def _card(key: dict, given: dict, created: str = "2026-01-01T00:00:00+00:00"):
    return score_depth(
        load_depth_key(key),
        load_depth_answers(given),
        code_version=COMMIT,
        scorer_version="test",
        created_at=created,
    )


def _outcomes(card) -> dict[str, str]:
    return {case.case_id.split("/", 1)[1]: case.outcome for case in card.cases}


# --- the key --------------------------------------------------------------------------------


def test_the_two_agents_are_registered_with_their_families() -> None:
    assert DEPTH_FAMILIES == {VIEW_AGENT: "view-kind", DEPTH_AGENT: "depth-span"}


def test_the_label_and_row_loader_refuses_a_depth_key_and_names_its_reader() -> None:
    for key in (depth_key(), view_key()):
        with pytest.raises(KeyFormatError, match="eval/pariksha/depth.py"):
            load_key(key)


def test_a_depth_key_is_read_into_one_value_per_field() -> None:
    key = load_depth_key(depth_key())

    assert key.agent == DEPTH_AGENT
    by_id = {value.case_id.split("/", 1)[1]: value for value in key.values}
    assert sorted(by_id) == [
        "p3:plan:CT007:front#CT007",
        "p3:plan:CT010:depth#CT010",
        "p3:plan:CUTOUT:hole#CT008",
        "p3:plan:CUTOUT:hole#CT012",
        "p3:plan:CUTOUT:two-outlines#CT008",
        "p3:plan:CUTOUT:two-outlines#CT012",
        "p3:section:CT010:doubt#CT010",
    ]
    depth = by_id["p3:plan:CT010:depth#CT010"].truth
    assert isinstance(depth, ValueTruth) and depth.values == (Fraction(51, 2),)
    assert by_id["p3:plan:CUTOUT:hole#CT008"].truth == ValueTruth(
        values=(Fraction(53, 4),), texts=('13 1/4"',), note="outline: the one outline"
    )
    # Statement 29's shape (`held`) and CT007's (`hold`) are both a hold the key expects.
    assert isinstance(by_id["p3:plan:CUTOUT:two-outlines#CT012"].truth, HoldTruth)
    front = by_id["p3:plan:CT007:front#CT007"].truth
    assert isinstance(front, HoldTruth) and front.start_edge == "stone edge"
    # An open case is listed with its reason, never parsed into a truth.
    doubt = by_id["p3:section:CT010:doubt#CT010"]
    assert doubt.truth is None and doubt.open_reason == "the two readings disagree"


def test_a_key_value_in_mm_or_without_a_unit_is_refused() -> None:
    # Inches are authoritative; a millimetre-only or unit-less key value is no value.
    for text in ("648 mm", "25"):
        with pytest.raises(KeyFormatError, match="exact inch"):
            load_depth_key(depth_key(_depth_case("p3:plan:CT010:x", "CT010", {"texts": [text]})))
    bracketed = load_depth_key(
        depth_key(_depth_case("p3:plan:CT010:x", "CT010", {"texts": ["648 [25 1/2]"]}))
    )
    truth = bracketed.values[0].truth
    assert isinstance(truth, ValueTruth) and truth.values == (Fraction(51, 2),)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"status": "proposed"}, "status"),
        ({"status": "open"}, "open_reason"),
        ({"open_reason": "why"}, "open_reason"),
        ({"field": "CT099"}, "field"),
        ({"family": "claude-slot-span"}, "family"),
        ({"page_sha256": "nothex"}, "page_sha256"),
        ({"typo": 1}, "unknown"),
        ({"answer": {"texts": []}}, "texts"),
        ({"answer": {"kind": "plan"}}, "CT010"),
        ({"confidence": "certain"}, "confidence"),
    ],
)
def test_a_bad_depth_case_is_refused(change: dict, message: str) -> None:
    case = _depth_case("p3:plan:CT010:x", "CT010", {"texts": ['25"']})
    case.update(change)
    with pytest.raises(KeyFormatError, match=message):
        load_depth_key(depth_key(case))


def test_a_cutout_needs_both_values_or_a_hold() -> None:
    with pytest.raises(KeyFormatError, match="CT012"):
        load_depth_key(
            depth_key(_depth_case("p3:plan:CUTOUT:x", "CT012+CT008", {"CT008": {"texts": ['13"']}}))
        )


def test_a_view_key_reads_kinds_and_folds_section_and_side() -> None:
    key = load_depth_key(view_key())

    assert key.agent == VIEW_AGENT
    kinds = {value.case_id.split("/", 1)[1]: value.truth for value in key.values}
    assert kinds["p3:view@10,20,300,400#view"] == KindTruth(kind="plan", printed="MADE-UP HEADING")
    # The reader's question has one answer for a section and a side view (view-kind-v1).
    side = kinds["p3:view@10,500,300,700#view"]
    assert isinstance(side, KindTruth) and side.kind == "section_or_side"
    with pytest.raises(KeyFormatError, match="kind"):
        load_depth_key({**view_key(), "cases": [_view_case("p3:view@1,1,2,2", "isometric")]})


def test_a_key_naming_one_question_twice_is_refused() -> None:
    case = _depth_case("p3:plan:CT010:x", "CT010", {"texts": ['25"']})
    with pytest.raises(KeyFormatError, match="twice"):
        load_depth_key(depth_key(case, dict(case)))


def test_the_key_counts_by_status_and_field(tmp_path: Path) -> None:
    path = tmp_path / "key.json"
    path.write_text(json.dumps(depth_key()))
    key = read_depth_key(path)

    counts = key_counts(key)
    assert counts[("claude-double-read", "CT010")] == 1
    assert counts[("claude-double-read", "CT012")] == 2
    assert counts[("claude-double-read", "CT008")] == 2
    assert counts[("claude-double-read", "CT007")] == 1
    assert counts[("open", "CT010")] == 1
    described = describe_key(key)
    assert KEY_PROVENANCE in described
    assert "open: 1" in described


def test_the_key_hash_names_its_answers() -> None:
    one, two = load_depth_key(depth_key()), load_depth_key(depth_key())
    changed = depth_key()
    changed["cases"][0]["answer"] = {"texts": ['25 3/4"']}
    assert one.sha256 == two.sha256 != load_depth_key(changed).sha256


# --- the answers file -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"format": "other/1"}, "format"),
        ({"agent": "akshara"}, "agent"),
        ({"run": ""}, "run"),
        ({"extra": 1}, "unknown"),
    ],
)
def test_a_bad_answers_file_is_refused(change: dict, message: str) -> None:
    with pytest.raises(DepthAnswersError, match=message):
        load_depth_answers({**good_depth_answers(), **change})


@pytest.mark.parametrize(
    ("reader", "message"),
    [
        ({"value": '25"', "unsure": True}, "exactly one"),
        ({"kind": "plan"}, "kind"),
        ({"unsure": False}, "unsure"),
        ({"held": ""}, "held"),
        ({}, "exactly one"),
    ],
)
def test_a_bad_reader_answer_is_refused(reader: dict, message: str) -> None:
    given = answers(DEPTH_AGENT, entry("p3:plan:CT010:depth", "CT010", reader, UNSURE))
    with pytest.raises(DepthAnswersError, match=message):
        load_depth_answers(given)


def test_the_pair_needs_exactly_two_readers() -> None:
    three = entry("p3:plan:CT010:depth", "CT010", UNSURE, UNSURE)
    three["readers"].append({"model_id": "reader-c", **UNSURE})
    with pytest.raises(DepthAnswersError, match="exactly two readers"):
        load_depth_answers(answers(DEPTH_AGENT, three))


def test_one_question_answered_twice_is_refused() -> None:
    one = entry("p3:plan:CT010:depth", "CT010", UNSURE, UNSURE)
    with pytest.raises(DepthAnswersError, match="twice"):
        load_depth_answers(answers(DEPTH_AGENT, one, dict(one)))


def test_answers_of_another_agent_are_refused_by_the_scorer() -> None:
    with pytest.raises(DepthAnswersError, match="rekha-view"):
        _card(depth_key(), answers(VIEW_AGENT, entry("p3:view@1,1,2,2", "view", UNSURE, UNSURE)))


# --- scoring --------------------------------------------------------------------------------


def test_right_values_and_expected_holds_score_right() -> None:
    card = _card(depth_key(), good_depth_answers())

    outcomes = _outcomes(card)
    assert outcomes["p3:plan:CT010:depth#CT010"] == AGREED_RIGHT  # 25 1/2" == 648 [25 1/2]
    assert outcomes["p3:plan:CUTOUT:hole#CT012"] == AGREED_RIGHT
    assert outcomes["p3:plan:CUTOUT:hole#CT008"] == AGREED_RIGHT
    assert outcomes["p3:plan:CUTOUT:two-outlines#CT012"] == HELD_AS_KEY
    assert outcomes["p3:plan:CT007:front#CT007"] == HELD_AS_KEY
    assert outcomes["p3:section:CT010:doubt#CT010"] == KEY_OPEN
    assert card.agent == DEPTH_AGENT
    assert card.source["key_provenance"] == KEY_PROVENANCE
    assert card.metrics["pair_agreed_wrong"].numerator == 0
    assert card.metrics["pair_agreed_right:CT010"].numerator == 1
    assert card.metrics["pair_held_as_key:CT007"].numerator == 1
    assert card.metrics["asked"].numerator == card.metrics["asked"].denominator == 6
    assert card.metrics["key_open"].numerator == 1


def test_a_value_where_the_key_expects_a_hold_is_agreed_wrong() -> None:
    # Statement 29's shape: two outlines, no word. Both readers naming one is the dangerous error.
    card = _card(
        depth_key(),
        good_depth_answers(two_w=(value('19"'), value('19"')), front=(value('4"'), value('4"'))),
    )

    outcomes = _outcomes(card)
    assert outcomes["p3:plan:CUTOUT:two-outlines#CT012"] == AGREED_WRONG
    assert outcomes["p3:plan:CT007:front#CT007"] == AGREED_WRONG
    assert card.metrics["pair_agreed_wrong"].numerator == 2


def test_each_pair_outcome() -> None:
    card = _card(
        depth_key(),
        good_depth_answers(
            depth=(value('25 1/2"'), value('25 3/4"')),
            width=(value('19"'), UNSURE),
            front_back=(UNSURE, UNSURE),
            two_w=(held("two outlines"), value('19"')),
            two_d=({"invalid": "no accepted answer"}, held()),
            front=(value('4"'), value('5"')),
        ),
    )

    outcomes = _outcomes(card)
    assert outcomes["p3:plan:CT010:depth#CT010"] == DISAGREED
    assert outcomes["p3:plan:CUTOUT:hole#CT012"] == ONE_UNSURE
    assert outcomes["p3:plan:CUTOUT:hole#CT008"] == BOTH_UNSURE
    assert outcomes["p3:plan:CUTOUT:two-outlines#CT012"] == HELD
    assert outcomes["p3:plan:CUTOUT:two-outlines#CT008"] == NO_ANSWER
    assert outcomes["p3:plan:CT007:front#CT007"] == DISAGREED


def test_a_hold_on_a_value_case_is_held_with_its_reason() -> None:
    card = _card(depth_key(), good_depth_answers(depth=(held("label cut"), held("under GV ink"))))

    case = next(c for c in card.cases if c.case_id.endswith("p3:plan:CT010:depth#CT010"))
    assert case.outcome == HELD
    assert [reader.answer for reader in case.readers] == [
        "(held: label cut)",
        "(held: under GV ink)",
    ]
    assert {reader.verdict for reader in case.readers} == {"held"}


def test_a_millimetre_value_is_no_usable_answer() -> None:
    # mm is never a value: a reader that gives only millimetres has not answered in inches.
    card = _card(depth_key(), good_depth_answers(depth=(value("648 mm"), value("648 mm"))))

    case = next(c for c in card.cases if c.case_id.endswith("p3:plan:CT010:depth#CT010"))
    assert case.outcome == NO_ANSWER
    assert {reader.verdict for reader in case.readers} == {"invalid"}


def test_each_reader_is_scored_on_its_own() -> None:
    card = _card(
        depth_key(),
        good_depth_answers(
            depth=(value('25 1/2"'), value('26"')), front=(value('4"'), held("stone edge"))
        ),
    )

    metrics = card.metrics
    assert metrics["reader_wrong:reader-b"].numerator == 1  # 26"
    assert metrics["reader_wrong:reader-a"].numerator == 1  # a value where the key holds
    assert metrics["reader_right:reader-a"].numerator == 5


def test_a_value_the_file_never_answers_is_not_asked_and_an_unkeyed_answer_is_counted() -> None:
    given = good_depth_answers()
    given["answers"] = [
        item for item in given["answers"] if item["question_id"] != "p3:plan:CT010:depth"
    ]
    given["answers"].append(entry("p9:plan:CT010:elsewhere", "CT010", UNSURE, UNSURE))
    card = _card(depth_key(), given)

    assert _outcomes(card)["p3:plan:CT010:depth#CT010"] == NOT_ASKED
    assert card.metrics["asked"].numerator == 5
    assert card.metrics["unkeyed_answers"].numerator == 1


def test_a_single_read_key_value_is_reported_apart_and_an_open_one_never_scored() -> None:
    given = answers(
        VIEW_AGENT,
        entry("p3:view@10,20,300,400", "view", {"kind": "plan"}, {"kind": "plan"}),
        entry("p3:view@10,500,300,700", "view", {"kind": "section_or_side"}, {"kind": "side"}),
        entry("p3:view@400,20,600,400", "view", {"kind": "plan"}, {"kind": "plan"}),
        entry("p5:view@1,2,3,4", "view", {"kind": "plan"}, {"kind": "plan"}),
    )
    card = _card(view_key(), given)

    outcomes = _outcomes(card)
    assert outcomes["p3:view@10,20,300,400#view"] == AGREED_RIGHT
    assert outcomes["p3:view@10,500,300,700#view"] == AGREED_RIGHT  # side = section_or_side
    # Would be agreed-wrong, but the key was read once: counted apart, never used to judge.
    assert outcomes["p3:view@400,20,600,400#view"] == KEY_SINGLE_READ
    assert card.metrics["single_read_agreed_wrong"].numerator == 1
    assert card.metrics["pair_agreed_wrong"].numerator == 0
    open_case = next(c for c in card.cases if c.case_id.endswith("p5:view@1,2,3,4#view"))
    assert open_case.outcome == KEY_OPEN
    assert "the readings disagree" in open_case.note
    assert {reader.verdict for reader in open_case.readers} == {"not_scored"}


def test_a_scorecard_round_trips_through_its_file(tmp_path: Path) -> None:
    card = _card(depth_key(), good_depth_answers())
    path = tmp_path / "card.json"
    write_scorecard(card, path)
    again = read_scorecard(path)
    assert again.cases == card.cases
    assert again.source["key_provenance"] == KEY_PROVENANCE


# --- compare --------------------------------------------------------------------------------


def test_a_seeded_agreed_wrong_fails_the_compare() -> None:
    before = _card(depth_key(), good_depth_answers(depth=(value('25 1/2"'), UNSURE)))
    # Better on the depth (now agreed-right), but one cut-out value is now agreed-wrong.
    after = _card(
        depth_key(),
        good_depth_answers(front_back=(value('13 3/4"'), value('13 3/4"'))),
        created="2026-01-02T00:00:00+00:00",
    )

    comparison = compare_depth(before, after)
    assert not comparison.accepted
    assert "1 new wrong" in comparison.verdict
    assert [change.case_id.split("/", 1)[1] for change in comparison.new_wrong] == [
        "p3:plan:CUTOUT:hole#CT008"
    ]


def test_a_value_given_where_the_key_holds_fails_the_compare() -> None:
    before = _card(depth_key(), good_depth_answers())
    after = _card(depth_key(), good_depth_answers(two_d=(value('13"'), value('13"'))))

    comparison = compare_depth(before, after)
    assert not comparison.accepted
    assert comparison.new_wrong[0].before == HELD_AS_KEY
    assert comparison.new_wrong[0].after == AGREED_WRONG


def test_better_with_no_new_wrong_is_accepted_and_an_expected_hold_counts_as_right() -> None:
    before = _card(
        depth_key(),
        good_depth_answers(depth=(value('25 1/2"'), UNSURE), two_w=(held(), UNSURE)),
    )
    after = _card(depth_key(), good_depth_answers())

    comparison = compare_depth(before, after)
    assert comparison.accepted, comparison.verdict
    fixed = sorted(change.case_id.split("/", 1)[1] for change in comparison.fixed)
    assert fixed == ["p3:plan:CT010:depth#CT010", "p3:plan:CUTOUT:two-outlines#CT012"]


def test_a_value_no_longer_asked_rejects() -> None:
    before = _card(depth_key(), good_depth_answers())
    given = good_depth_answers()
    given["answers"] = given["answers"][1:]
    comparison = compare_depth(before, _card(depth_key(), given))
    assert not comparison.accepted
    assert "no longer asked" in comparison.verdict


def test_single_read_and_open_cases_never_accept_or_reject() -> None:
    def card(single: str, opened: str):
        return _card(
            view_key(),
            answers(
                VIEW_AGENT,
                entry("p3:view@10,20,300,400", "view", {"kind": "plan"}, {"kind": "plan"}),
                entry("p3:view@10,500,300,700", "view", UNSURE, UNSURE),
                entry("p3:view@400,20,600,400", "view", {"kind": single}, {"kind": single}),
                entry("p5:view@1,2,3,4", "view", {"kind": opened}, {"kind": opened}),
            ),
        )

    # Only the single-read and the open cases change: right -> wrong on both. Not a verdict.
    comparison = compare_depth(card("elevation", "section"), card("plan", "plan"))
    assert comparison.new_wrong == ()
    assert comparison.unchanged == 4
    assert comparison.verdict.startswith("REJECT (not better")


def test_the_depth_compare_refuses_cards_of_another_agent() -> None:
    depth = _card(depth_key(), good_depth_answers())
    view = _card(
        view_key(),
        answers(VIEW_AGENT, entry("p3:view@10,20,300,400", "view", UNSURE, UNSURE)),
    )
    for before, after in ((depth, view), (view, depth)):
        with pytest.raises(IncomparableRuns):
            compare_depth(before, after)
    # The walls compare refuses a depth card, and the depth compare refuses a walls-agent card.
    with pytest.raises(IncomparableRuns, match="rekha-walls"):
        compare_walls(depth, depth)
    from dataclasses import replace

    walls_named = replace(depth, agent="rekha-walls")
    with pytest.raises(IncomparableRuns, match="akshara-depth or rekha-view"):
        compare_depth(walls_named, walls_named)
    # The generic compare refuses two different agents too.
    with pytest.raises(IncomparableRuns):
        compare_scorecards(depth, view)
