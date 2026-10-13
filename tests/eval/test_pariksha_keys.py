"""The Pariksha answer-key format (#1219): versioned, strict, identified without database ids.

Verification for: `eval/pariksha/keys.py`. Synthetic keys only; no client value appears here.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from eval.pariksha.keys import (
    KEY_FORMAT,
    AnswerKey,
    ArchitectAnswerKey,
    KeyFormatError,
    LabelTruth,
    RowTruth,
    dump_key,
    load_key,
    read_key,
)

PAGE = "a" * 64
OTHER_PAGE = "b" * 64


def _label_key(*cases: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": KEY_FORMAT,
        "agent": "akshara",
        "name": "synthetic labels",
        "version": "1",
        "cases": list(cases),
    }


def _label_case(question_id: str = "p1:slot0:0", **answer: Any) -> dict[str, Any]:
    return {
        "family": "claude-slot-span",
        "page_sha256": PAGE,
        "question_id": question_id,
        "answer": answer or {"texts": ['3/4"']},
    }


def test_a_label_key_loads_with_normalised_accepted_texts_and_a_stable_case_id() -> None:
    key = load_key(
        _label_key(
            _label_case(texts=["3/4″", "3/4"]),
            _label_case("p1:overall:0", no_label=True),
        )
    )

    assert isinstance(key, AnswerKey)
    first, second = key.cases
    assert first.truth == LabelTruth(texts=('3/4"', "3/4"))
    assert second.truth == LabelTruth(texts=())
    assert second.truth.no_label
    # The case id is the question's identity: the page's content hash and the question id. Never a
    # database id, so the same question asked in another database gets the same id.
    assert first.case_id == f"{PAGE[:16]}/p1:slot0:0"
    assert first.question.family == "claude-slot-span"
    assert len(key.sha256) == 64


def test_a_row_key_reads_a_box_number_or_one_of_the_two_no_row_answers() -> None:
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
                    "answer": {"box": 2},
                },
                {
                    "family": "slot-row-choice",
                    "page_sha256": OTHER_PAGE,
                    "question_id": "p2:row-choice",
                    "answer": {"kind": "no_countertop"},
                },
            ],
        }
    )

    assert isinstance(key, AnswerKey)
    assert [case.truth for case in key.cases] == [
        RowTruth(box=2, kind="row"),
        RowTruth(box=None, kind="no_countertop"),
    ]


def test_the_architect_key_reuses_the_existing_page_shape() -> None:
    key = load_key(
        {
            "format": KEY_FORMAT,
            "agent": "vastu",
            "name": "synthetic architect",
            "version": "1",
            "document_sha256": "c" * 64,
            "pages": [{"page": 1, "arch_widths": [{"text": "2'-0\"", "in": 24}]}],
        }
    )

    assert isinstance(key, ArchitectAnswerKey)
    assert key.document_sha256 == "c" * 64
    assert key.pages[0].widths == (Fraction(24),)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"format": "pariksha-key/0"}, "format"),
        ({"agent": "someone"}, "agent"),
        ({"version": " "}, "version"),
        ({"surprise": 1}, "unknown"),
    ],
)
def test_a_key_with_a_wrong_header_is_refused(change: dict[str, Any], message: str) -> None:
    with pytest.raises(KeyFormatError, match=message):
        load_key({**_label_key(_label_case()), **change})


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ({**_label_case(), "family": "slot-row-choice"}, "family"),
        ({**_label_case(), "page_sha256": "not-a-hash"}, "page_sha256"),
        ({**_label_case(), "question_id": ""}, "question_id"),
        ({**_label_case(), "answer": {"texts": []}}, "texts"),
        ({**_label_case(), "answer": {"texts": ["1"], "no_label": True}}, "answer"),
        ({**_label_case(), "slot_box": "1,2,3"}, "slot_box"),
        ({**_label_case(), "picture_sha256": "abc"}, "picture_sha256"),
        ({**_label_case(), "row": 1}, "unknown"),
        # A database id is not an identity: the same question has another id in another database.
        ({**_label_case(), "invocation_id": "0" * 8}, "unknown"),
    ],
)
def test_a_malformed_case_is_refused(case: dict[str, Any], message: str) -> None:
    with pytest.raises(KeyFormatError, match=message):
        load_key(_label_key(case))


def test_two_cases_for_one_question_are_refused() -> None:
    with pytest.raises(KeyFormatError, match="twice"):
        load_key(_label_key(_label_case(), _label_case(texts=['1/2"'])))


@pytest.mark.parametrize("answer", [{"box": 0}, {"box": True}, {"kind": "row"}, {"kind": "x"}])
def test_a_row_answer_must_be_a_positive_box_or_a_named_no_row_answer(
    answer: dict[str, Any],
) -> None:
    with pytest.raises(KeyFormatError):
        load_key(
            {
                "format": KEY_FORMAT,
                "agent": "rekha",
                "name": "rows",
                "version": "1",
                "cases": [
                    {
                        "family": "slot-row-choice",
                        "page_sha256": PAGE,
                        "question_id": "p1:row-choice",
                        "answer": answer,
                    }
                ],
            }
        )


def test_a_key_round_trips_through_its_file_and_keeps_its_hash(tmp_path: Path) -> None:
    key = load_key(_label_key(_label_case(), _label_case("p2:slot1:0", no_label=True)))
    assert isinstance(key, AnswerKey)
    path = tmp_path / "key.json"
    path.write_text(json.dumps(dump_key(key)))

    again = read_key(path)

    assert again == key
