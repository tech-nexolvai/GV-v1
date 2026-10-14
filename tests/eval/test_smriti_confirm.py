"""Smriti: nothing joins a key without a person's confirmation (#1232).

Verification for: `eval/smriti/confirm.py` and the candidates file in `eval/smriti/candidates.py`.
Invented candidates only; no database, no model call.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from eval.pariksha.keys import AnswerKey, LabelTruth, RowTruth, dump_key, load_key
from eval.smriti.candidates import (
    CANDIDATES_FORMAT,
    NEEDS_CONFIRMATION,
    Candidate,
    CandidateExport,
    CandidatesFileError,
    read_candidates,
    write_candidates,
)
from eval.smriti.confirm import CONFIRMATIONS_FORMAT, ConfirmationRefused, confirm_candidates

PAGE = "a" * 64
PICTURE = "c" * 64


def _row(candidate_id: str = "no_countertop_answers:1") -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        table="no_countertop_answers",
        decision_id=candidate_id.split(":", 1)[1],
        agent="rekha",
        corrects="a page listed with no countertop",
        kind="confirmed",
        decided_by="reviewer-a",
        decided_at="2026-10-14T00:00:00+00:00",
        page_sha256=PAGE,
        page_number=1,
        recorded={"answer": "no_countertop"},
        question={
            "family": "slot-row-choice",
            "page_sha256": PAGE,
            "question_id": "p0:row-choice",
            "picture_sha256": PICTURE,
        },
        answer={"kind": "no_countertop"},
        answer_needed=None,
        not_convertible=None,
    )


def _label(candidate_id: str = "correction_ledger:2") -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        table="correction_ledger",
        decision_id=candidate_id.split(":", 1)[1],
        agent="akshara",
        corrects="a value read",
        kind="corrected",
        decided_by="reviewer-a",
        decided_at="2026-10-14T00:00:00+00:00",
        page_sha256=PAGE,
        page_number=1,
        recorded={"corrected": "5/2"},
        question={
            "family": "claude-slot-span",
            "page_sha256": PAGE,
            "question_id": "p0:slot0:0",
            "slot_box": "1,2,3,4",
        },
        answer=None,
        answer_needed="the printed text of the label",
        not_convertible=None,
    )


def _milan() -> Candidate:
    return Candidate(
        candidate_id="architect_pairing_records:3",
        table="architect_pairing_records",
        decision_id="3",
        agent="milan",
        corrects="a pairing",
        kind="decided",
        decided_by="reviewer-a",
        decided_at="2026-10-14T00:00:00+00:00",
        page_sha256=PAGE,
        page_number=1,
        recorded={"pairs": []},
        question=None,
        answer=None,
        answer_needed=None,
        not_convertible="pariksha-key/1 has no Milan answer yet",
    )


def _on_another_page(candidate: Candidate) -> Candidate:
    return replace(candidate, question={**(candidate.question or {}), "page_sha256": "b" * 64})


def _export(*candidates: Candidate) -> CandidateExport:
    return CandidateExport(
        database="synthetic",
        exported_at="2026-10-14T00:00:00+00:00",
        code_version="test",
        candidates=candidates,
    )


def _confirmations(agent: str, *entries: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": CONFIRMATIONS_FORMAT,
        "confirmed_by": "a person",
        "key": {"agent": agent, "name": "invented key", "version": "1"},
        "confirm": list(entries),
    }


def test_only_the_confirmed_candidates_join_the_key() -> None:
    key = confirm_candidates(
        _export(_row("no_countertop_answers:1")),
        _confirmations("rekha", {"candidate": "no_countertop_answers:1"}),
    )
    assert isinstance(key, AnswerKey)
    assert key.agent == "rekha"
    assert len(key.cases) == 1
    case = key.cases[0]
    assert case.truth == RowTruth(box=None, kind="no_countertop")
    assert case.picture_sha256 == PICTURE
    assert "no_countertop_answers:1" in case.source and "a person" in case.source
    # What it writes is a key Pariksha reads.
    assert load_key(dump_key(key)).sha256 == key.sha256


def test_an_unlisted_candidate_never_joins() -> None:
    other_page = _on_another_page(_row("no_countertop_answers:9"))
    key = confirm_candidates(
        _export(_row("no_countertop_answers:1"), other_page),
        _confirmations("rekha", {"candidate": "no_countertop_answers:1"}),
    )
    assert [case.question.page_sha256 for case in key.cases] == [PAGE]


def test_nothing_confirmed_is_refused() -> None:
    with pytest.raises(ConfirmationRefused, match="nothing"):
        confirm_candidates(_export(_row()), _confirmations("rekha"))


def test_a_candidate_not_in_the_export_is_refused() -> None:
    with pytest.raises(ConfirmationRefused, match="not in"):
        confirm_candidates(_export(_row()), _confirmations("rekha", {"candidate": "nope:1"}))


def test_a_not_convertible_candidate_is_refused_even_when_confirmed() -> None:
    with pytest.raises(ConfirmationRefused, match="not convertible"):
        confirm_candidates(
            _export(_milan()),
            _confirmations("rekha", {"candidate": "architect_pairing_records:3"}),
        )


def test_another_agents_candidate_is_refused() -> None:
    with pytest.raises(ConfirmationRefused, match="akshara"):
        confirm_candidates(
            _export(_label()),
            _confirmations("rekha", {"candidate": "correction_ledger:2", "texts": ['2 1/2"']}),
        )


def test_a_label_candidate_needs_the_printed_text_from_the_person() -> None:
    with pytest.raises(ConfirmationRefused, match="printed text"):
        confirm_candidates(
            _export(_label()), _confirmations("akshara", {"candidate": "correction_ledger:2"})
        )
    key = confirm_candidates(
        _export(_label()),
        _confirmations("akshara", {"candidate": "correction_ledger:2", "texts": ["2½″"]}),
    )
    assert key.cases[0].truth == LabelTruth(texts=('2 1/2"',))
    assert key.cases[0].slot_box == "1,2,3,4"


def test_a_person_cannot_overwrite_an_answer_the_reviewer_recorded() -> None:
    with pytest.raises(ConfirmationRefused, match="recorded"):
        confirm_candidates(
            _export(_row()),
            _confirmations(
                "rekha", {"candidate": "no_countertop_answers:1", "texts": ["anything"]}
            ),
        )


def test_a_candidate_confirmed_twice_or_an_unknown_field_is_refused() -> None:
    with pytest.raises(ConfirmationRefused, match="twice"):
        confirm_candidates(
            _export(_row()),
            _confirmations(
                "rekha",
                {"candidate": "no_countertop_answers:1"},
                {"candidate": "no_countertop_answers:1"},
            ),
        )
    with pytest.raises(ConfirmationRefused, match="unknown"):
        confirm_candidates(
            _export(_row()),
            _confirmations("rekha", {"candidate": "no_countertop_answers:1", "box": 2}),
        )
    unnamed = _confirmations("rekha", {"candidate": "no_countertop_answers:1"})
    unnamed["confirmed_by"] = " "
    with pytest.raises(ConfirmationRefused, match="confirmed_by"):
        confirm_candidates(_export(_row()), unnamed)


def test_confirmed_cases_add_to_an_existing_key_and_a_conflict_is_refused() -> None:
    base = confirm_candidates(
        _export(_row()), _confirmations("rekha", {"candidate": "no_countertop_answers:1"})
    )
    moved = _on_another_page(_row("no_countertop_answers:9"))
    merged = confirm_candidates(
        _export(moved),
        _confirmations("rekha", {"candidate": "no_countertop_answers:9"}),
        base=base,
    )
    assert len(merged.cases) == 2
    # The same question confirmed again with the same answer is kept once.
    again = confirm_candidates(
        _export(_row()),
        _confirmations("rekha", {"candidate": "no_countertop_answers:1"}),
        base=base,
    )
    assert len(again.cases) == 1
    conflicting = replace(
        _row(), candidate_id="no_countertop_answers:5", answer={"kind": "not_among_boxes"}
    )
    with pytest.raises(ConfirmationRefused, match="already"):
        confirm_candidates(
            _export(conflicting),
            _confirmations("rekha", {"candidate": "no_countertop_answers:5"}),
            base=base,
        )


def test_the_candidates_file_is_written_once_and_refused_when_edited(tmp_path: Path) -> None:
    path = tmp_path / "candidates.json"
    exported = _export(_row(), _label(), _milan())
    write_candidates(exported, path)
    with pytest.raises(FileExistsError):
        write_candidates(exported, path)
    read = read_candidates(path)
    assert read == exported
    document = json.loads(path.read_text())
    assert document["format"] == CANDIDATES_FORMAT
    assert {entry["status"] for entry in document["candidates"]} == {NEEDS_CONFIRMATION}
    assert document["counts"]["by_agent"]["rekha"]["ready"] == 1
    document["candidates"][0]["answer"] = {"kind": "not_among_boxes"}
    path.write_text(json.dumps(document))
    with pytest.raises(CandidatesFileError, match="changed"):
        read_candidates(path)
