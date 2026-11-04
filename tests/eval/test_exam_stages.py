"""The stage exams (rebuild plan Step 1A): pairing scored on its own, findings scored on the pages
paired right, so a pairing failure is never read as a finding failure.

Every fixture is synthetic: run tags, page numbers and verdicts made up for the test.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from eval.exam import truth
from eval.exam.replay import (
    PAIRING_CLASSES,
    diff_stage,
    guessed_pages,
    pairing_class,
    pairing_stage,
    right_paired_pages,
    score_cases,
)

ROOT = Path(__file__).resolve().parents[2]


def _script():
    spec = importlib.util.spec_from_file_location("exam_replay", ROOT / "scripts/exam_replay.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("exam_replay", module)
    spec.loader.exec_module(module)
    return module


def _row(page: int, used: str, right: str, correct: str, run: str = "r") -> truth.Pairing:
    return truth.Pairing(run, page, used, right, "plan", correct, "a")


def test_a_guess_is_read_from_the_coverage_the_stamp_and_the_stored_reason():
    coverage = {
        "pages": [
            {"page": 1, "pairing_unsure": True, "pairing_unsure_pages": [{"page": 7}, {"page": 9}]},
            {"page": 2, "pairing_unsure": False, "pairing_unsure_pages": []},
        ]
    }
    findings = [
        {"id": "F1", "page": 3, "pairing_guess": {"page": 8, "file": "a", "words": "x"}},
        {"id": "F2", "page": 4, "proof": {"reason": "best guess: architect page 6; the rest"}},
        {"id": "F3", "page": 2, "proof": {"reason": "proved by code"}},
    ]
    assert guessed_pages(findings, coverage) == {1: {7, 9}, 3: {8}, 4: {6}}
    assert guessed_pages([], None) == {}


def test_each_audited_page_falls_in_one_pairing_class():
    assert pairing_class(_row(1, "7", "7", "yes"), guessed=False) == "exact"
    assert pairing_class(_row(1, "7", "7", "yes"), guessed=True) == "best_guess"
    assert pairing_class(_row(1, "none", "none", "yes"), guessed=False) == "nothing_to_compare"
    assert pairing_class(_row(1, "7", "7", "view"), guessed=False) == "wrong_view"
    assert pairing_class(_row(1, "7", "9", "no"), guessed=True) == "wrong_page"
    assert pairing_class(_row(1, "none", "9", "no"), guessed=False) == "unpaired"
    assert pairing_class(_row(1, "7", "9", "maybe"), guessed=False) == "unknown"


def test_the_pairing_stage_counts_every_class_and_the_wrong_guesses():
    rows = [
        _row(1, "7", "7", "yes"),
        _row(2, "7", "7", "yes"),
        _row(3, "7", "9", "no"),
        _row(4, "7", "7", "view"),
        _row(5, "none", "9", "no"),
        _row(6, "none", "none", "yes"),
    ]
    stage = pairing_stage(rows, {"r": {2: {7}, 3: set()}})["r"]
    assert stage["pages"] == 6
    assert [stage[name] for name in PAIRING_CLASSES] == [1, 1, 1, 1, 1, 1]
    assert stage["wrong_marked_guess"] == 1  # page 3: a wrong page, at least marked a guess
    # A run with no saved guesses scores every right page as exact.
    assert pairing_stage(rows[:2], {})["r"]["exact"] == 2


def test_the_diff_stage_scores_reds_and_please_checks_apart_and_on_right_pages():
    cases = [
        truth.Case("r", "F1", 1, "helper", "other", "x", "wrong", "TRUE", "a"),
        truth.Case("r", "F2", 1, "helper", "other", "x", "likely_problem", "TRUE", "a"),
        truth.Case("r", "F3", 2, "helper", "other", "x", "likely_problem", "FALSE", "a"),
        truth.Case("r", "F4", 2, "helper", "other", "x", "wrong", "FALSE", "a"),
    ]
    groups = {"r": {case.finding_id: (case.stored_group, case.stored_group) for case in cases}}
    pairing = [_row(1, "7", "7", "yes"), _row(2, "7", "9", "no")]
    pages = right_paired_pages(pairing, {})
    assert pages == {"r": {1}}
    every = score_cases(cases, groups)
    clean = score_cases(cases, groups, pages)
    stored = next(score for score in every if score.side == "stored")
    assert (stored.reds, stored.reds_true, stored.checks, stored.checks_true) == (2, 1, 2, 1)
    recall = {"r": {"known_problems": 3, "hand": {"found": 1, "partly": 1, "missed": 1}}}
    stage = diff_stage(every, clean, recall)["r"]["stored"]
    assert stage["red_precision"] == 0.5 and stage["check_precision"] == 0.5
    # On the page paired right, both the red and the please-check were real.
    assert stage["right_pages"]["red_precision"] == 1.0
    assert stage["right_pages"]["check_precision"] == 1.0
    assert stage["misses"] == {
        "known": 3,
        "found": 1,
        "partly": 1,
        "missed": 1,
        "unscored": 0,
        "recall": None,
        "recall_with_partial": None,
    }
    # A run the page filter does not name scores no case at all.
    assert all(score.cases == 0 for score in score_cases(cases, groups, {}))


def test_the_stage_tables_print_one_line_per_run():
    script = _script()
    result = {
        "pairing_stage": {
            "r": {
                "pages": 3,
                "exact": 1,
                "best_guess": 1,
                "nothing_to_compare": 0,
                "wrong_view": 0,
                "wrong_page": 1,
                "unpaired": 0,
                "wrong_marked_guess": 1,
            }
        },
        "diff_stage": {
            "r": {
                "stored": {
                    "reds": 2,
                    "reds_true": 1,
                    "checks": 0,
                    "checks_true": 0,
                    "right_pages": None,
                    "misses": {
                        "known": 2,
                        "found": 1,
                        "partly": 0,
                        "missed": 1,
                        "unscored": 0,
                        "recall": 0.5,
                    },
                }
            }
        },
    }
    pairing = script.pairing_report(result)
    assert any(line.startswith("r ") and line.endswith("1 of 1") for line in pairing)
    diff = script.diff_report(result)
    assert any(line.startswith("r ") and "1/2 (50%)" in line for line in diff)
    assert any(line.startswith("r ") and "50.0%" in line for line in diff)
