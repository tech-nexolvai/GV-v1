"""Pariksha's scorers on synthetic stored answers (#1219): Akshara, Rekha and Vastu.

Verification for: `eval/pariksha/score.py`. Every stored answer here is invented; the replies go
through the repository's own reader code exactly as a stored reply does on a re-run. No model call.
"""

from __future__ import annotations

import json
import zlib
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
    StoredRunError,
)
from eval.runs import MissingProvenanceError

PAGE = "a" * 64
OPUS = "reader-one"
SONNET = "reader-two"
ROW_PICTURE = "3" * 64


def _box(question_id: str) -> str:
    """An invented slot box of its own for each invented label question."""
    left = zlib.crc32(question_id.encode()) % 100_000
    return f"{left},0,{left + 5},5"


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
    packet_sha256: str = "p" * 8,
) -> StoredQuestion:
    packet: dict[str, Any] = {
        "question_id": question_id,
        "page_index": 1,
        "prompt_id": prompt_id,
        "source_page_sha256": PAGE,
        "packet_sha256": packet_sha256,
        "images": {
            "full_view": {"sha256": "1" * 64, "storage_key": None},
            "close_up": {"sha256": "2" * 64, "storage_key": None},
        },
    }
    if family == "slot-row-choice":
        packet["candidate_count"] = candidates
        packet["images"] = {"numbered_vendor_view": {"sha256": ROW_PICTURE, "storage_key": None}}
    return StoredQuestion(
        identity=QuestionIdentity(family=family, page_sha256=PAGE, question_id=question_id),
        prompt_id=prompt_id,
        packet=packet,
        slot_box=(
            slot_box if slot_box is not None or family != "claude-slot-span" else _box(question_id)
        ),
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
                    "slot_box": _box(question_id),
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

    card = score_akshara(key, run, code_version="abc1234")

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
    assert card.code_version == "abc1234"


def test_a_reader_with_no_accepted_answer_is_invalid_and_the_pair_is_held() -> None:
    key = _label_key({"p1:slot0:0": {"texts": ['3/4"']}})
    failed = StoredAttempt(
        model_id=SONNET, outcome="timeout", raw_response=None, cost_micros=None, reused=False
    )
    malformed = StoredAttempt(
        model_id=SONNET,
        outcome="rejected",
        raw_response="not json at all",
        cost_micros=10,
        reused=False,
    )
    run = _run(_question("p1:slot0:0", _ok(OPUS, _span('3/4"')), failed, malformed))

    card = score_akshara(key, run, code_version="abc1234")

    (case,) = card.cases
    assert case.outcome == HELD
    assert card.metrics[f"reader_invalid:{SONNET}"].numerator == 1


def test_a_key_case_the_run_never_asked_is_listed_with_its_reason_never_dropped() -> None:
    key = _label_key({"p1:slot0:0": {"texts": ['3/4"']}, "p1:slot9:0": {"texts": ['1"']}})
    run = _run(_question("p1:slot0:0", _ok(OPUS, _span('3/4"')), _ok(SONNET, _span('3/4"'))))

    card = score_akshara(key, run, code_version="abc1234")

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

    card = score_akshara(key, run, code_version="abc1234")

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

    card = score_akshara(key, run, code_version="abc1234")

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
                    "picture_sha256": ROW_PICTURE,
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

    card = score_rekha(key, run, code_version="abc1234")

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


def test_an_accepted_reply_todays_code_cannot_read_refuses_the_scorecard() -> None:
    """Recorded `ok` but unreadable now (here: a row outside the numbered boxes): never silently
    'invalid', which would quietly score a whole older-format run as 0% right."""
    key = _row_key({"p1:row-choice": {"box": 2}})
    run = _run(_row_question("p1:row-choice", _row(2), _row(9)))

    from eval.pariksha.score import UnreadableStoredAnswers

    with pytest.raises(UnreadableStoredAnswers, match="1 accepted answer"):
        score_rekha(key, run, code_version="abc1234")


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

    card = score_vastu(key, run, code_version="abc1234")

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

    card = score_rekha(key, run, code_version="abc1234")

    (case,) = card.cases
    assert case.outcome == NOT_ASKED
    assert "different picture" in case.note


def _v2_span(text: str, *, belongs: bool) -> str:
    """The v1-v3 answer shape: `belongs` was a boolean (true = yes, false = no)."""
    return json.dumps(
        {
            "belongs": belongs,
            "text": text,
            "stacked": False,
            "combined": False,
            "readable": True,
            "no_dimension": False,
        }
    )


def test_an_older_format_run_is_read_by_the_version_aware_parser_not_scored_invalid() -> None:
    key = _label_key({"p1:slot0:0": {"texts": ['3/4"']}, "p1:slot1:0": {"no_label": True}})
    run = _run(
        _question(
            "p1:slot0:0",
            _ok(OPUS, _v2_span('3/4"', belongs=True)),
            _ok(SONNET, _v2_span('3/4"', belongs=True)),
            prompt_id="claude-slot-span-v2",
        ),
        _question(
            "p1:slot1:0",
            _ok(OPUS, _v2_span("", belongs=False)),
            _ok(SONNET, _v2_span('5"', belongs=True)),
            prompt_id="claude-slot-span-v2",
        ),
    )

    card = score_akshara(key, run, code_version="abc1234")

    assert _outcomes(card) == {"p1:slot0:0": AGREED_RIGHT, "p1:slot1:0": HELD}
    assert card.metrics[f"reader_invalid:{OPUS}"].numerator == 0
    assert card.metrics[f"reader_right:{OPUS}"].numerator == 2
    assert card.metrics["older_shape_answers"].numerator == 4
    assert card.metrics["unreadable_stored_answers"].numerator == 0


def test_two_readers_saying_yes_with_no_text_are_not_an_agreement() -> None:
    """The seal agrees only on usable answers, and an empty text is not one."""
    key = _label_key({"p1:slot0:0": {"texts": ['3/4"']}})
    run = _run(_question("p1:slot0:0", _ok(OPUS, _span("")), _ok(SONNET, _span(""))))

    card = score_akshara(key, run, code_version="abc1234")

    assert card.cases[0].outcome == HELD


def test_a_renumbered_slot_is_followed_by_its_box_not_dropped() -> None:
    """The key's span is found by page, box and label position, whatever its slot number now."""
    box = "10,0,20,5"
    key = _label_key({"p1:slot2:0": {"texts": ['3/4"']}}, **{"p1:slot2:0": {"slot_box": box}})
    run = _run(
        _question(
            "p1:slot2:0", _ok(OPUS, _span('4"')), _ok(SONNET, _span('5"')), slot_box="0,0,9,5"
        ),
        _question("p1:slot3:0", _ok(OPUS, _span('1/2"')), _ok(SONNET, _span('1/2"')), slot_box=box),
    )

    card = score_akshara(key, run, code_version="abc1234")

    (case,) = card.cases
    assert case.outcome == AGREED_WRONG
    assert "p1:slot3:0" in case.question


def test_two_accepted_answers_from_one_reader_to_one_question_are_refused() -> None:
    key = _label_key({"p1:slot0:0": {"texts": ['3/4"']}})
    run = _run(
        _question(
            "p1:slot0:0",
            _ok(OPUS, _span('3/4"')),
            _ok(OPUS, _span('1/2"')),
            _ok(SONNET, _span('3/4"')),
        )
    )

    with pytest.raises(StoredRunError, match="more than one accepted answer"):
        score_akshara(key, run, code_version="abc1234")


def test_one_question_asked_with_two_different_packets_is_refused() -> None:
    key = _label_key({"p1:slot0:0": {"texts": ['3/4"']}})
    first = _question("p1:slot0:0", _ok(OPUS, _span('3/4"')))
    second = _question("p1:slot0:0", _ok(SONNET, _span('3/4"')), packet_sha256="q" * 8)
    run = _run(first, second)

    with pytest.raises(StoredRunError, match="two different question packets"):
        score_akshara(key, run, code_version="abc1234")


def test_rekha_counts_an_agreed_not_among_boxes_as_split_as_the_product_does() -> None:
    """The product holds such a page for the reviewer (`workflow/slot_row_scope.py`): it is not an
    automatic answer, so it is neither agreed-right nor agreed-wrong."""
    key = _row_key({"p1:row-choice": {"kind": "not_among_boxes"}, "p2:row-choice": {"box": 1}})
    run = _run(
        _row_question("p1:row-choice", _row(0, "not_among_boxes"), _row(0, "not_among_boxes")),
        _row_question("p2:row-choice", _row(0, "not_among_boxes"), _row(0, "not_among_boxes")),
    )

    card = score_rekha(key, run, code_version="abc1234")

    assert _outcomes(card) == {"p1:row-choice": SPLIT, "p2:row-choice": SPLIT}
    assert card.metrics[f"reader_right:{OPUS}"].numerator == 1


@pytest.mark.parametrize("version", ["v1", "main", "a469cd3x", "123456"])
def test_the_code_version_must_be_a_git_sha_or_unrecorded(version: str) -> None:
    key = _label_key({"p1:slot0:0": {"texts": ['3/4"']}})
    with pytest.raises(MissingProvenanceError):
        score_akshara(key, _run(), code_version=version)


def test_an_unrecorded_code_version_is_allowed_and_flagged() -> None:
    key = _label_key({"p1:slot0:0": {"texts": ['3/4"']}})
    run = _run(_question("p1:slot0:0", _ok(OPUS, _span('3/4"')), _ok(SONNET, _span('3/4"'))))

    card = score_akshara(key, run, code_version="unrecorded")

    assert card.code_version == "unrecorded"
    assert "not attributable" in card.extractor_versions["code_version_note"]
