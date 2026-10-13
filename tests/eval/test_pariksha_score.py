"""Pariksha's scorers on synthetic stored answers (#1219): Akshara, Rekha and Vastu.

Verification for: `eval/pariksha/score.py`. Every stored answer here is invented; the replies go
through the repository's own reader code exactly as a stored reply does on a re-run. No model call.
"""

from __future__ import annotations

import json
from fractions import Fraction
from typing import Any

import pytest

from eval.architect_reader import ReadSpan
from eval.pariksha.keys import KEY_FORMAT, AnswerKey, ArchitectAnswerKey, load_key
from eval.pariksha.score import (
    AGREED_RIGHT,
    AGREED_WRONG,
    EXTRA_USABLE,
    FOUND,
    HELD,
    MISSING,
    NOT_ASKED,
    SPLIT,
    score_akshara,
    score_rekha,
    score_vastu,
)
from eval.pariksha.stored import (
    QuestionIdentity,
    StoredArchitectRun,
    StoredAttempt,
    StoredQuestion,
    StoredRun,
)
from eval.runs import MissingProvenanceError

PAGE = "a" * 64
OPUS = "reader-one"
SONNET = "reader-two"


def _span(
    text: str = "", *, belongs: str = "yes", readable: bool = True, no_dimension: bool = False
) -> str:
    return json.dumps(
        {
            "belongs": belongs,
            "text": text,
            "stacked": False,
            "combined": False,
            "readable": readable,
            "no_dimension": no_dimension,
        }
    )


def _row(row: int, kind: str = "row") -> str:
    return json.dumps({"row": row, "kind": kind, "also": [], "why": "invented"})


def _ok(model: str, raw: str, cost: int = 1000, *, reused: bool = False) -> StoredAttempt:
    return StoredAttempt(
        model_id=model, outcome="ok", raw_response=raw, cost_micros=cost, reused=reused
    )


def _question(
    question_id: str,
    *attempts: StoredAttempt,
    family: str = "claude-slot-span",
    prompt_id: str = "claude-slot-span-v4",
    slot_box: str | None = None,
    candidates: int = 3,
) -> StoredQuestion:
    packet: dict[str, Any] = {
        "question_id": question_id,
        "page_index": 1,
        "prompt_id": prompt_id,
        "source_page_sha256": PAGE,
        "images": {
            "full_view": {"sha256": "1" * 64, "storage_key": None},
            "close_up": {"sha256": "2" * 64, "storage_key": None},
        },
    }
    if family == "slot-row-choice":
        packet["candidate_count"] = candidates
        packet["images"] = {"numbered_vendor_view": {"sha256": "3" * 64, "storage_key": None}}
    return StoredQuestion(
        identity=QuestionIdentity(family=family, page_sha256=PAGE, question_id=question_id),
        prompt_id=prompt_id,
        packet=packet,
        slot_box=slot_box,
        attempts=attempts,
    )


def _run(*questions: StoredQuestion) -> StoredRun:
    return StoredRun(
        database="synthetic",
        extraction_run_id="run-1",
        extractor="extraction.form_reader",
        extractor_version="slot-reader-test",
        config_hash="test",
        questions=questions,
    )


def _label_key(cases: dict[str, dict[str, Any]], **extra: dict[str, Any]) -> AnswerKey:
    key = load_key(
        {
            "format": KEY_FORMAT,
            "agent": "akshara",
            "name": "synthetic",
            "version": "1",
            "cases": [
                {
                    "family": "claude-slot-span",
                    "page_sha256": PAGE,
                    "question_id": question_id,
                    "answer": answer,
                    **extra.get(question_id, {}),
                }
                for question_id, answer in cases.items()
            ],
        }
    )
    assert isinstance(key, AnswerKey)
    return key


def _outcomes(card: Any) -> dict[str, str]:
    return {case.case_id.split("/", 1)[1]: case.outcome for case in card.cases}


def test_akshara_counts_each_pair_and_each_reader_including_an_agreed_wrong_answer() -> None:
    key = _label_key(
        {
            "p1:slot0:0": {"texts": ['3/4"']},
            "p1:slot1:0": {"texts": ['1/2"']},
            "p1:slot2:0": {"texts": ['24"']},
            "p1:slot3:0": {"texts": ['2"']},
            "p1:overall:0": {"no_label": True},
        }
    )
    run = _run(
        # Both right; one writes the inch mark as a double prime, which normalises to `"`.
        _question("p1:slot0:0", _ok(OPUS, _span('3/4"')), _ok(SONNET, _span("3/4″"))),
        # Both say the same wrong text: the one outcome that must stay at zero.
        _question("p1:slot1:0", _ok(OPUS, _span('1 1/2"')), _ok(SONNET, _span('1 1/2"'))),
        # They differ: held, one right and one wrong.
        _question("p1:slot2:0", _ok(OPUS, _span('24"')), _ok(SONNET, _span('26"'))),
        # One unsure: held.
        _question("p1:slot3:0", _ok(OPUS, _span('2"')), _ok(SONNET, _span(belongs="unsure"))),
        # No label: a sure "no" is right; reading a number there is wrong.
        _question("p1:overall:0", _ok(OPUS, _span(belongs="no")), _ok(SONNET, _span('5"'))),
    )

    card = score_akshara(key, run, code_version="abc123")

    assert _outcomes(card) == {
        "p1:slot0:0": AGREED_RIGHT,
        "p1:slot1:0": AGREED_WRONG,
        "p1:slot2:0": HELD,
        "p1:slot3:0": HELD,
        "p1:overall:0": HELD,
    }
    metrics = card.metrics
    assert (metrics["pair_agreed_right"].numerator, metrics["pair_agreed_right"].denominator) == (
        1,
        5,
    )
    assert metrics["pair_agreed_wrong"].numerator == 1
    assert metrics["pair_held"].numerator == 3
    assert metrics[f"reader_right:{OPUS}"].numerator == 4
    assert metrics[f"reader_wrong:{OPUS}"].numerator == 1
    assert metrics[f"reader_right:{SONNET}"].numerator == 1
    assert metrics[f"reader_wrong:{SONNET}"].numerator == 3
    assert metrics[f"reader_unsure:{SONNET}"].numerator == 1
    # Cost: two answers of 1000 micro-dollars per question.
    assert metrics["cost_micros_per_question"].value == Fraction(2000)
    assert card.extractor_versions["agent"] == "akshara"
    assert card.extractor_versions["prompt_ids"] == "claude-slot-span-v4"
    assert card.extractor_versions["model_ids"] == f"{OPUS},{SONNET}"
    assert card.code_version == "abc123"


def test_a_reader_with_no_accepted_answer_is_invalid_and_the_pair_is_held() -> None:
    key = _label_key({"p1:slot0:0": {"texts": ['3/4"']}})
    failed = StoredAttempt(
        model_id=SONNET, outcome="timeout", raw_response=None, cost_micros=None, reused=False
    )
    malformed = _ok(SONNET, "not json at all")
    run = _run(_question("p1:slot0:0", _ok(OPUS, _span('3/4"')), failed, malformed))

    card = score_akshara(key, run, code_version="abc123")

    (case,) = card.cases
    assert case.outcome == HELD
    assert card.metrics[f"reader_invalid:{SONNET}"].numerator == 1


def test_a_key_case_the_run_never_asked_is_listed_with_its_reason_never_dropped() -> None:
    key = _label_key({"p1:slot0:0": {"texts": ['3/4"']}, "p1:slot9:0": {"texts": ['1"']}})
    run = _run(_question("p1:slot0:0", _ok(OPUS, _span('3/4"')), _ok(SONNET, _span('3/4"'))))

    card = score_akshara(key, run, code_version="abc123")

    missing = next(case for case in card.cases if case.case_id.endswith("p1:slot9:0"))
    assert missing.outcome == NOT_ASKED
    assert "not asked" in missing.note
    assert (card.metrics["asked"].numerator, card.metrics["asked"].denominator) == (1, 2)
    # Rates are over the questions asked: the unasked case is neither right nor wrong.
    assert card.metrics["pair_agreed_right"].value == 1


def test_a_question_about_a_different_span_is_not_scored_against_the_key() -> None:
    """Slot numbering follows the chosen row: on another row `slot2` is another span."""
    key = _label_key({"p1:slot2:0": {"texts": ['3/4"']}}, **{"p1:slot2:0": {"slot_box": "1,2,3,4"}})
    run = _run(
        _question(
            "p1:slot2:0",
            _ok(OPUS, _span('9"')),
            _ok(SONNET, _span('9"')),
            slot_box="5,6,7,8",
        )
    )

    card = score_akshara(key, run, code_version="abc123")

    (case,) = card.cases
    assert case.outcome == NOT_ASKED
    assert "different span" in case.note
    assert card.metrics["pair_agreed_wrong"].value is None  # nothing asked: not measured, not 0


def test_questions_the_key_does_not_cover_are_counted() -> None:
    key = _label_key({"p1:slot0:0": {"texts": ['3/4"']}})
    run = _run(
        _question("p1:slot0:0", _ok(OPUS, _span('3/4"')), _ok(SONNET, _span('3/4"'))),
        _question("p1:slot1:0", _ok(OPUS, _span('1"')), _ok(SONNET, _span('1"'))),
    )

    card = score_akshara(key, run, code_version="abc123")

    assert (card.metrics["unkeyed_questions"].numerator,) == (1,)


def test_a_scorecard_without_the_code_version_of_the_run_is_refused() -> None:
    key = _label_key({"p1:slot0:0": {"texts": ['3/4"']}})
    with pytest.raises(MissingProvenanceError):
        score_akshara(key, _run(), code_version=" ")


def _row_key(cases: dict[str, dict[str, Any]]) -> AnswerKey:
    key = load_key(
        {
            "format": KEY_FORMAT,
            "agent": "rekha",
            "name": "synthetic rows",
            "version": "1",
            "cases": [
                {
                    "family": "slot-row-choice",
                    "page_sha256": PAGE,
                    "question_id": question_id,
                    "answer": answer,
                }
                for question_id, answer in cases.items()
            ],
        }
    )
    assert isinstance(key, AnswerKey)
    return key


def _row_question(question_id: str, opus: str, sonnet: str) -> StoredQuestion:
    return _question(
        question_id,
        _ok(OPUS, opus),
        _ok(SONNET, sonnet),
        family="slot-row-choice",
        prompt_id="slot-row-choice-v3",
    )


def test_rekha_counts_agreed_right_agreed_wrong_and_split() -> None:
    key = _row_key(
        {
            "p1:row-choice": {"box": 2},
            "p2:row-choice": {"box": 1},
            "p3:row-choice": {"box": 1},
            "p4:row-choice": {"box": 1},
            "p5:row-choice": {"kind": "no_countertop"},
        }
    )
    run = _run(
        _row_question("p1:row-choice", _row(2), _row(2)),
        _row_question("p2:row-choice", _row(3), _row(3)),
        _row_question("p3:row-choice", _row(1), _row(0, "not_among_boxes")),
        # Both unsure is not an agreement on a row: it is split, never agreed-wrong.
        _row_question("p4:row-choice", _row(0, "unsure"), _row(0, "unsure")),
        _row_question("p5:row-choice", _row(0, "no_countertop"), _row(0, "no_countertop")),
    )

    card = score_rekha(key, run, code_version="abc123")

    assert _outcomes(card) == {
        "p1:row-choice": AGREED_RIGHT,
        "p2:row-choice": AGREED_WRONG,
        "p3:row-choice": SPLIT,
        "p4:row-choice": SPLIT,
        "p5:row-choice": AGREED_RIGHT,
    }
    assert card.metrics["pair_agreed_right"].numerator == 2
    assert card.metrics["pair_agreed_wrong"].numerator == 1
    assert card.metrics["pair_split"].numerator == 2
    assert card.metrics[f"reader_right:{OPUS}"].numerator == 3
    assert card.metrics[f"reader_wrong:{SONNET}"].numerator == 2
    assert card.metrics[f"reader_unsure:{SONNET}"].numerator == 1


def test_an_out_of_range_row_answer_is_invalid_as_the_live_reader_would_find_it() -> None:
    key = _row_key({"p1:row-choice": {"box": 2}})
    run = _run(_row_question("p1:row-choice", _row(2), _row(9)))

    card = score_rekha(key, run, code_version="abc123")

    assert card.metrics[f"reader_invalid:{SONNET}"].numerator == 1
    assert card.cases[0].outcome == SPLIT


def test_vastu_wraps_the_architect_scorer_with_one_case_per_key_width() -> None:
    key = load_key(
        {
            "format": KEY_FORMAT,
            "agent": "vastu",
            "name": "synthetic architect",
            "version": "1",
            "pages": [
                {
                    "page": 1,
                    "arch_widths": [{"in": 24}, {"in": 24}, {"in": 36}],
                },
            ],
        }
    )
    assert isinstance(key, ArchitectAnswerKey)
    run = StoredArchitectRun(
        database="synthetic",
        extraction_run_id="run-a",
        extractor="architect-text",
        extractor_version="architect-text-v1",
        config_hash="test",
        document_sha256="c" * 64,
        pages={
            1: (
                ReadSpan("2'-0\"", Fraction(24), Fraction(24), None),
                ReadSpan("3'-1\"", Fraction(37), Fraction(37), None),
            )
        },
    )

    card = score_vastu(key, run, code_version="abc123")

    outcomes = {case.case_id: case.outcome for case in card.cases}
    assert outcomes == {
        "p1:24#1": FOUND,
        "p1:24#2": MISSING,
        "p1:36#1": MISSING,
        "p1:extra:37#1": EXTRA_USABLE,
    }
    assert card.metrics["found"].numerator == 1
    assert card.metrics["missing"].numerator == 2
    assert card.metrics["extra_usable"].numerator == 1


def test_a_row_case_tied_to_its_numbered_picture_is_not_scored_on_another_picture() -> None:
    """Box numbers belong to one numbered picture; on a re-numbered one "box 2" is another line."""
    key = load_key(
        {
            "format": KEY_FORMAT,
            "agent": "rekha",
            "name": "synthetic rows",
            "version": "1",
            "cases": [
                {
                    "family": "slot-row-choice",
                    "page_sha256": PAGE,
                    "question_id": "p1:row-choice",
                    "picture_sha256": "9" * 64,
                    "answer": {"box": 2},
                }
            ],
        }
    )
    assert isinstance(key, AnswerKey)
    run = _run(_row_question("p1:row-choice", _row(3), _row(3)))  # its picture is "3" * 64

    card = score_rekha(key, run, code_version="abc123")

    (case,) = card.cases
    assert case.outcome == NOT_ASKED
    assert "different picture" in case.note
