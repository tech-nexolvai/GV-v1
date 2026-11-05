"""The exam's own tests: the audit parser, the verdict mapping, and that no client text leaks.

The last one is the reason this file is not optional. The exam is derived from the client's
drawings, so every row in `eval/exam/` is a chance to put an item tag or a room number on GitHub.
`CLIENT_FACTS.md` forbids it and `~/gv-local/client-values/check.sh` catches the known values, but
the check only knows the values someone remembered to list. This asserts the shape instead: a
drawing code is a token with letters and digits stuck together, and there are none.
"""

from __future__ import annotations

import re

import pytest

from eval.exam import truth
from eval.exam.audit_parse import VERDICTS, rows_of_text
from eval.exam.labels import label_of
from eval.exam.replay import GROUPS, auto_status, never_found, score_cases, stability

#: A drawing number, item tag, finish code or sheet id: letters and digits in one token. Plain
#: sizes ("24", "1/2") and plain words pass; "ML-015R", "A3.10b", "GR-213" and "6.7B" do not.
CODE_LIKE = re.compile(r"\b(?=[A-Za-z0-9.\-/]*[A-Za-z])(?=[A-Za-z0-9.\-/]*\d)[A-Za-z0-9.\-/]{3,}\b")

#: The exam's own identifiers, which are the shape above and are not the client's: a run tag
#: (`setA-flag`), a miss id (`setA-m01`) and a finding id (`F545`, the run's own numbering).
OURS = re.compile(r"^(set[A-Z]-(run\d+|flag|m\d+)|F\d+)$")

#: A capitalised word in the middle of a sentence: how a client's brand, hotel, city or person
#: would show up in a description. The exam's own prose needs only these four, so anything else is
#: a name that should not be here. The listed client values themselves are checked by
#: `~/gv-local/client-values/check.sh` before every pull request, from a list kept off GitHub —
#: this file cannot hold that list, because writing the names here would be the leak it prevents.
PROPER_NOUN = re.compile(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]+\b")
ORDINARY = frozenset({"Left", "Right", "Not", "The"})

DATA_FILES = (truth.CASES, truth.PAIRING, truth.MISSES, truth.MISS_STATUS, truth.RUNS)


@pytest.mark.parametrize("path", DATA_FILES, ids=lambda path: path.name)
def test_no_client_text_in_the_exam_data(path):
    """No drawing code and no client name in any committed exam file."""
    text = path.read_text(encoding="utf-8")
    found = [token for token in CODE_LIKE.findall(text) if not OURS.match(token)]
    assert not found, f"{path.name} holds drawing-code-shaped tokens: {sorted(set(found))[:10]}"
    names = {
        match.group()
        for line in text.splitlines()
        for field in line.split("\t")
        for match in PROPER_NOUN.finditer(field)
        if match.start() and match.group() not in ORDINARY
    }
    assert not names, f"{path.name} holds name-shaped words: {sorted(names)}"


def test_the_parser_reads_every_shape_the_helpers_wrote():
    """One table per shape the audits use, including the row-number-first column that must not be
    read as a page and the verdict asides the helpers added."""
    text = """
| # | F | page | group | item | verdict |
|---|---|---|---|---|---|
| #1 | F6 | p1 | please-check | a top tag | **TRUE-minor** |
| #7 | F545 | p1 | not compared | a top length | **(b) real difference** |

| answer | page | group | verdict |
|---|---|---|---|
| #236 F86 | p31 | please-check | **FALSE** |

| # | Fid | Kind | Verdict | note |
|---|---|---|---|---|
| 45 | F22 | wrong | **TRUE-real** (under-graded) | a plan string |
| 88 | F148 | not_compared | SHOULD-FLAG (really red) | a plan string |
| 21 | F4 | right | **CORRECT** | the fronts agree |
"""
    rows = {row.finding_id: row for row in rows_of_text(text, "sample")}
    assert [row.verdict for row in rows.values()] == [
        "TRUE_MINOR",
        "NC_B",
        "FALSE",
        "TRUE",
        "NC_B",
        "RIGHT_OK",
    ]
    assert rows["F6"].page == 1 and rows["F86"].page == 31
    # The audit's own row number opens the last table; it must not be read as a page.
    assert rows["F22"].page is None


def test_a_flagged_answer_keeps_its_group_and_a_false_one_should_not_be_listed():
    assert truth.truth_of("TRUE", "wrong") == truth.Truth("wrong", True)
    assert truth.truth_of("TRUE_MINOR", "likely_problem") == truth.Truth("likely_problem", True)
    assert truth.truth_of("FALSE", "likely_problem") == truth.Truth("not_listed", False)


def test_a_not_compared_answer_that_is_really_a_difference_is_a_leak():
    """The failure flag mode exists to stop: a real difference filed where nobody reads it."""
    assert truth.truth_of("NC_B", "not_compared") == truth.Truth("likely_problem", True, leak=True)
    assert truth.truth_of("NC_C", "not_compared") == truth.Truth("right", False, leak=True)
    # The same verdict on an answer the run did list is not a leak: it is already in front of a person.
    assert truth.truth_of("NC_B", "likely_problem").leak is False


def test_the_exam_data_joins_up():
    cases = truth.load_cases()
    runs = truth.load_runs()
    misses = {miss.miss_id for miss in truth.load_misses()}
    assert cases, "the exam has no cases"
    assert {case.run for case in cases} <= set(runs)
    assert {case.verdict for case in cases} <= set(VERDICTS.values())
    assert {case.stored_group for case in cases} <= set(GROUPS)
    assert {row.run for row in truth.load_pairing()} <= set(runs)
    assert {row.correct for row in truth.load_pairing()} <= {"yes", "no", "view"}
    status = truth.load_miss_status()
    assert {row.miss_id for row in status} <= misses
    assert {row.run for row in status} <= set(runs)
    assert {row.status for row in status} <= {"found", "partly", "missed"}
    # One finding is audited once per run, or a score would count it twice.
    seen = [(case.run, case.finding_id) for case in cases]
    assert len(seen) == len(set(seen))


def test_the_scorer_counts_a_leak_and_a_false_alarm_apart():
    cases = [
        truth.Case(
            "r", "F1", 1, "page_reviewer", "other", "cut-out", "likely_problem", "TRUE", "a"
        ),
        truth.Case(
            "r", "F2", 1, "page_reviewer", "other", "cut-out", "likely_problem", "FALSE", "a"
        ),
        truth.Case("r", "F3", 1, "page_reviewer", "other", "cut-out", "not_compared", "NC_B", "a"),
        truth.Case("r", "F4", 1, "page_reviewer", "other", "cut-out", "not_compared", "NC_C", "a"),
    ]
    groups = {
        "r": {f"F{n}": (case.stored_group, case.stored_group) for n, case in enumerate(cases, 1)}
    }
    stored = next(score for score in score_cases(cases, groups) if score.side == "stored")
    assert stored.listed == 2 and stored.listed_right == 1  # one of the two raised was real
    assert stored.should_list == 2  # the real one, plus the difference filed as Not compared
    assert stored.leaks_problem == 1 and stored.leaks_agreement == 1


def test_the_miss_matcher_needs_the_page_and_several_cues():
    """It is meant to be mean: a hint for a person, never a verdict."""
    miss = truth.Miss(
        "m1",
        "setA",
        (7,),
        "gv rule",
        "should-flag",
        "the top is short",
        "the top length",
        ("field cut", "top length", "run", "wall end"),
        "x",
    )
    on_another_page = [
        {"id": "F1", "page": 8, "item": "field cut on the top length at the wall end"}
    ]
    assert auto_status(miss, on_another_page) == ("missed", "")
    too_few_cues = [{"id": "F2", "page": 7, "item": "the top length is short"}]
    assert auto_status(miss, too_few_cues) == ("missed", "")
    enough = [{"id": "F3", "page": 7, "item": "no field cut: the top length is under the run"}]
    assert auto_status(miss, enough) == ("found", "F3")


def test_stability_names_the_problems_one_run_found_and_another_missed():
    misses = [truth.Miss(f"m{n}", "setA", (1,), "k", "s", "d", "w", ("a",), "x") for n in (1, 2)]
    status = [
        truth.MissStatus("m1", "run1", "found", ""),
        truth.MissStatus("m1", "run2", "missed", ""),
        truth.MissStatus("m2", "run1", "missed", ""),
        truth.MissStatus("m2", "run2", "missed", ""),
    ]
    jobs = {"run1": "setA", "run2": "setA"}
    assert stability(misses, status, jobs)["setA"]["swing"] == 1
    assert [miss.miss_id for miss in never_found(misses, status)] == ["m2"]


def test_a_label_is_a_domain_word_and_never_the_client_s_text():
    assert label_of("Top plan: sink cut-out HSS-UMS-7") == "cut-out"
    assert label_of("Kitchen: toe kick height") == "toe kick"
    assert label_of("something nobody wrote a rule for") == "other"
