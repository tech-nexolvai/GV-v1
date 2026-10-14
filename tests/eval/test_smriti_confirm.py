"""Smriti: a person confirms every case, for every key type; nothing joins a key otherwise (#1232, #1271).

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
from eval.pariksha.kinds import KindsKey, dump_kinds_key, load_kinds_key
from eval.pariksha.walls import WallsKey, dump_walls_key, load_walls_key
from eval.smriti.actors import REVIEWER, UNCHECKED, UNKNOWN, Confirmers
from eval.smriti.candidates import (
    CANDIDATES_FORMAT,
    MISTAKE_UNANSWERED,
    NEEDS_CONFIRMATION,
    Candidate,
    CandidateExport,
    CandidatesFileError,
    read_candidates,
    write_candidates,
)
from eval.smriti.confirm import (
    CONFIRMATIONS_FORMAT,
    ConfirmationRefused,
    confirm_candidates,
    ready_candidates,
)

PAGE = "a" * 64
OTHER_PAGE = "b" * 64
PICTURE = "c" * 64
FILE_SHA = "f" * 64
#: The stored picture every invented question showed.
PIC = "d" * 64
#: The local confirmers list: the invented person who confirms (never the reviewer who decided).
PEOPLE = Confirmers(names=frozenset({"a person"}), source="test")


def _candidate(candidate_id: str, **fields: Any) -> Candidate:
    base: dict[str, Any] = {
        "candidate_id": candidate_id,
        "table": candidate_id.split(":", 1)[0],
        "decision_id": candidate_id.split(":")[1],
        "agent": "rekha",
        "corrects": "something",
        "kind": "decided",
        "decided_by": "reviewer-a",
        "decided_at": "2026-10-14T00:00:00+00:00",
        "page_sha256": PAGE,
        "page_number": 1,
        "recorded": {},
        "question": None,
        "answer": None,
        "answer_needed": None,
        "not_convertible": None,
        "actor_status": REVIEWER,
    }
    base.update(fields)
    return Candidate(**base)


def _no_countertop(candidate_id: str = "no_countertop_answers:1", page: str = PAGE) -> Candidate:
    return _candidate(
        candidate_id,
        kind="confirmed",
        recorded={"answer": "no_countertop", "has_note": False, "batch": False},
        question={
            "family": "slot-row-choice",
            "page_sha256": page,
            "question_id": "p0:row-choice",
            "picture_sha256": PICTURE,
        },
        answer={"kind": "no_countertop"},
        answer_source="decision",
    )


def _countertop(candidate_id: str = "no_countertop_answers:2") -> Candidate:
    return replace(
        _no_countertop(candidate_id),
        kind="corrected",
        recorded={"answer": "countertop", "has_note": False, "batch": False},
        answer=None,
        answer_needed="the box",
        answer_source=None,
    )


def _batch(candidate_id: str = "no_countertop_answers:3") -> Candidate:
    return replace(
        _no_countertop(candidate_id),
        recorded={"answer": "no_countertop", "has_note": False, "batch": True},
        answer=None,
        answer_needed="its own look",
        answer_source=None,
    )


def _correction(candidate_id: str = "correction_ledger:4") -> Candidate:
    return _candidate(
        candidate_id,
        agent="akshara",
        kind="corrected",
        recorded={"original": "1/3", "corrected": "5/2"},
        question={
            "family": "claude-slot-span",
            "page_sha256": PAGE,
            "question_id": "p0:slot0:0",
            "slot_box": "1,2,3,4",
        },
        answer_needed="the printed text of the label",
        mistake_needed=MISTAKE_UNANSWERED,
    )


def _confirmed_reading(candidate_id: str = "review_actions:5") -> Candidate:
    return _candidate(
        candidate_id,
        agent="akshara",
        kind="confirmed",
        recorded={"action": "confirm", "reading": '12 1/2"', "pdf_text": ['12-1/2"']},
        question={
            "family": "claude-slot-span",
            "page_sha256": PAGE,
            "question_id": "p0:slot1:0",
            "slot_box": "40,20,60,40",
        },
        answer={"texts": ['12-1/2"']},
        answer_source="pdf-text",
    )


def _walls(
    candidate_id: str = "slot_row_review_decisions:6:walls", layout: str = "back_and_left"
) -> Candidate:
    return _candidate(
        candidate_id,
        recorded={"wall_config": layout},
        question={
            "family": "slot-walls",
            "page_sha256": PAGE,
            "question_id": "p0:walls",
            "row_box": "5,5,95,80",
        },
        answer_needed="what the vendor drew at each end",
    )


def _piece(
    candidate_id: str = "part_confirmations:7", piece: int = 1, kind: str = "cabinet"
) -> Candidate:
    return _candidate(
        candidate_id,
        agent="bala",
        kind="corrected",
        recorded={"decision": "confirmed", "kind": kind, "suggested": "filler", "piece": piece},
        question={
            "family": "piece-kind",
            "page_sha256": PAGE,
            "question_id": f"p0:piece{piece}",
            "pieces": 2,
        },
        answer={"kinds": [kind]},
        answer_source="decision",
    )


def _milan() -> Candidate:
    return _candidate(
        "architect_pairing_records:8",
        agent="milan",
        recorded={"pairs": []},
        not_convertible="pariksha-key/1 has no Milan answer yet",
    )


def _export(*candidates: Candidate) -> CandidateExport:
    return CandidateExport(
        database="synthetic",
        exported_at="2026-10-14T00:00:00+00:00",
        code_version="test",
        candidates=candidates,
    )


def _answers(*entries: dict[str, Any], sha: str = FILE_SHA) -> dict[str, Any]:
    return {
        "format": CONFIRMATIONS_FORMAT,
        "confirmed_by": "a person",
        "candidates_sha256": sha,
        "answers": list(entries),
    }


def _yes(candidate_id: str, **fields: Any) -> dict[str, Any]:
    return {"candidate": candidate_id, "verdict": "yes", "pictures": [PIC], **fields}


def _given(export: CandidateExport, **kwargs: Any) -> dict[str, Any]:
    """What the script reads from storage: each question's pictures and its numbered box count."""
    kwargs.setdefault("pictures", {c.candidate_id: [PIC] for c in export.candidates})
    kwargs.setdefault(
        "boxes",
        {
            c.candidate_id: 3
            for c in export.candidates
            if (c.question or {}).get("family") == "slot-row-choice"
        },
    )
    kwargs.setdefault("confirmers", PEOPLE)
    # Every stored picture's bytes were read from the artifact store and matched its fingerprint.
    kwargs.setdefault("store", {PIC: None})
    return {"candidates_sha256": FILE_SHA, **kwargs}


def _confirm(export: CandidateExport, *entries: dict[str, Any], **kwargs: Any) -> Any:
    return confirm_candidates(export, _answers(*entries), **_given(export, **kwargs))


# --- every key type, round-tripped through its Pariksha loader ------------------------------------


def test_a_proposed_label_answered_yes_becomes_an_akshara_case_pariksha_reads() -> None:
    result = _confirm(_export(_confirmed_reading()), _yes("review_actions:5"))
    key = result.keys["akshara"]
    assert isinstance(key, AnswerKey)
    assert key.cases[0].truth == LabelTruth(texts=('12-1/2"',))
    assert key.cases[0].slot_box == "40,20,60,40"
    assert "review_actions:5" in key.cases[0].source and "a person" in key.cases[0].source
    assert load_key(dump_key(key)).sha256 == key.sha256
    assert result.documents["akshara"] == dump_key(key)


def test_a_proposed_answer_is_never_replaced_by_typed_text() -> None:
    with pytest.raises(ConfirmationRefused, match="answer No"):
        _confirm(_export(_confirmed_reading()), _yes("review_actions:5", texts=["13"]))


def test_a_confirmed_no_countertop_page_becomes_a_rekha_row_case() -> None:
    key = _confirm(_export(_no_countertop()), _yes("no_countertop_answers:1")).keys["rekha"]
    assert isinstance(key, AnswerKey)
    assert key.cases[0].truth == RowTruth(box=None, kind="no_countertop")
    assert key.cases[0].picture_sha256 == PICTURE
    assert load_key(dump_key(key)).sha256 == key.sha256


def test_there_is_a_countertop_needs_a_box_or_not_among_boxes() -> None:
    export = _export(_countertop())
    with pytest.raises(ConfirmationRefused, match="box"):
        _confirm(export, _yes("no_countertop_answers:2"))
    with pytest.raises(ConfirmationRefused, match="not both"):
        _confirm(export, _yes("no_countertop_answers:2", box=2, not_among_boxes=True))
    with pytest.raises(ConfirmationRefused, match="whole number"):
        _confirm(export, _yes("no_countertop_answers:2", box=0))
    boxed = _confirm(export, _yes("no_countertop_answers:2", box=2)).keys["rekha"]
    assert boxed.cases[0].truth == RowTruth(box=2, kind="row")
    among = _confirm(export, _yes("no_countertop_answers:2", not_among_boxes=True)).keys["rekha"]
    assert among.cases[0].truth == RowTruth(box=None, kind="not_among_boxes")
    assert load_key(dump_key(among)).sha256 == among.sha256


def test_there_is_a_countertop_answered_no_countertop_conflicts_with_the_reviewer() -> None:
    with pytest.raises(ConfirmationRefused, match="conflict"):
        _confirm(_export(_countertop()), _yes("no_countertop_answers:2", no_countertop=True))


def test_a_batch_page_needs_its_own_look_and_its_own_answer() -> None:
    export = _export(_batch())
    with pytest.raises(ConfirmationRefused, match="own look"):
        _confirm(export, _yes("no_countertop_answers:3"))
    with pytest.raises(ConfirmationRefused, match="conflict"):
        _confirm(export, _yes("no_countertop_answers:3", box=1))
    key = _confirm(export, _yes("no_countertop_answers:3", no_countertop=True)).keys["rekha"]
    assert key.cases[0].truth == RowTruth(box=None, kind="no_countertop")
    assert "own look" in key.cases[0].source


def test_a_wall_choice_becomes_a_rekha_walls_case_with_the_drawn_ends_the_person_states() -> None:
    export = _export(_walls())
    with pytest.raises(ConfirmationRefused, match="each end"):
        _confirm(export, _yes("slot_row_review_decisions:6:walls"))
    with pytest.raises(ConfirmationRefused, match="drawn"):
        _confirm(export, _yes("slot_row_review_decisions:6:walls", left="wall", right="maybe"))
    result = _confirm(
        export, _yes("slot_row_review_decisions:6:walls", left="wall", right="not_shown")
    )
    key = result.keys["rekha-walls"]
    assert isinstance(key, WallsKey)
    case = key.cases[0]
    assert (case.left.drawn, case.right.drawn) == ("wall", "not_shown")
    # The reviewer's layout is the site answer, never what the vendor drew.
    assert (case.left.site, case.right.site) == ("wall", "open")
    assert case.site_layout == "back_and_left"
    assert case.row_box == (5, 5, 95, 80)
    assert load_walls_key(dump_walls_key(key)).sha256 == key.sha256
    assert result.documents["rekha-walls"] == dump_walls_key(key)


def test_a_back_only_layout_leaves_the_ends_site_unsettled() -> None:
    result = _confirm(
        _export(_walls(layout="back_only")),
        _yes("slot_row_review_decisions:6:walls", left="open", right="open"),
    )
    case = result.keys["rekha-walls"].cases[0]
    assert (case.left.site, case.right.site) == ("key_unsure", "key_unsure")
    assert case.site_layout == "back_only"


def test_a_piece_kind_becomes_a_bala_kinds_case_with_its_stored_row_widths() -> None:
    export = _export(_piece())
    with pytest.raises(ConfirmationRefused, match="row_widths"):
        _confirm(export, _yes("part_confirmations:7"))
    result = _confirm(
        export, _yes("part_confirmations:7"), row_widths={"part_confirmations:7": ["2", "25/2"]}
    )
    key = result.keys["bala-kinds"]
    assert isinstance(key, KindsKey)
    case = key.cases[0]
    assert case.question_id == "p0:piece1" and case.pieces == 2
    assert case.truth.kinds == ("cabinet",)
    assert [str(width) for width in case.row_widths or ()] == ["2", "25/2"]
    assert load_kinds_key(dump_kinds_key(key)).sha256 == key.sha256


def test_one_page_of_answers_writes_one_key_per_agent() -> None:
    result = _confirm(
        _export(_no_countertop(), _confirmed_reading(), _walls(), _piece()),
        _yes("no_countertop_answers:1"),
        _yes("review_actions:5"),
        _yes("slot_row_review_decisions:6:walls", left="wall", right="open"),
        _yes("part_confirmations:7"),
        row_widths={"part_confirmations:7": ["2", None]},
    )
    assert sorted(result.keys) == ["akshara", "bala-kinds", "rekha", "rekha-walls"]
    assert sorted(result.joined) == sorted(
        [
            "no_countertop_answers:1",
            "review_actions:5",
            "slot_row_review_decisions:6:walls",
            "part_confirmations:7",
        ]
    )


# --- corrections: AI misread or vendor drew it wrong ----------------------------------------------


def test_a_correction_needs_misread_or_vendor() -> None:
    export = _export(_correction())
    with pytest.raises(ConfirmationRefused, match="misread"):
        _confirm(export, _yes("correction_ledger:4", texts=['2 1/2"']))
    with pytest.raises(ConfirmationRefused, match="mistake"):
        _confirm(export, _yes("correction_ledger:4", texts=['2 1/2"'], mistake="both"))
    with pytest.raises(ConfirmationRefused, match="printed text"):
        _confirm(export, _yes("correction_ledger:4", mistake="ai_misread"))
    key = _confirm(export, _yes("correction_ledger:4", mistake="ai_misread", texts=["2½″"])).keys[
        "akshara"
    ]
    assert key.cases[0].truth == LabelTruth(texts=('2 1/2"',))
    assert "AI misread" in key.cases[0].source


def test_a_vendor_error_is_kept_out_of_the_ais_key_and_listed_with_its_printed_text() -> None:
    export = _export(_correction(), _no_countertop())
    with pytest.raises(ConfirmationRefused, match="printed text"):
        _confirm(export, _yes("correction_ledger:4", mistake="vendor_drew_it"))
    result = _confirm(
        export,
        _yes("correction_ledger:4", mistake="vendor_drew_it", texts=['1 1/3"']),
        _yes("no_countertop_answers:1"),
    )
    assert "akshara" not in result.keys
    assert [error.candidate_id for error in result.vendor] == ["correction_ledger:4"]
    (error,) = result.vendor
    assert error.printed == {"texts": ['1 1/3"']}
    assert error.recorded == {"original": "1/3", "corrected": "5/2"}
    assert error.confirmed_by == "a person"
    assert "correction_ledger:4" not in result.joined


def test_an_unsure_mistake_is_kept_out() -> None:
    result = _confirm(
        _export(_correction(), _no_countertop()),
        _yes("correction_ledger:4", mistake="unsure", texts=["2"]),
        _yes("no_countertop_answers:1"),
    )
    assert "akshara" not in result.keys
    assert [cid for cid, _why in result.kept_out] == ["correction_ledger:4"]


def test_a_mistake_is_not_asked_where_the_candidate_does_not_ask_it() -> None:
    with pytest.raises(ConfirmationRefused, match="not asked"):
        _confirm(_export(_no_countertop()), _yes("no_countertop_answers:1", mistake="ai_misread"))


# --- only Yes joins; only listed reviewers; nothing unnamed --------------------------------------


def test_no_and_unsure_never_join_a_key() -> None:
    result = _confirm(
        _export(_no_countertop(), _confirmed_reading(), _countertop()),
        {"candidate": "no_countertop_answers:1", "verdict": "no"},
        {"candidate": "review_actions:5", "verdict": "unsure"},
        _yes("no_countertop_answers:2", box=3),
    )
    assert sorted(result.keys) == ["rekha"]
    assert [case.truth for case in result.keys["rekha"].cases] == [RowTruth(box=3, kind="row")]
    assert sorted(cid for cid, _why in result.kept_out) == [
        "no_countertop_answers:1",
        "review_actions:5",
    ]


def test_answers_with_no_yes_make_no_key() -> None:
    result = _confirm(
        _export(_no_countertop()), {"candidate": "no_countertop_answers:1", "verdict": "no"}
    )
    assert result.keys == {} and result.joined == ()


@pytest.mark.parametrize("status", [UNKNOWN, UNCHECKED, "code"])
def test_an_unlisted_actor_is_never_written_even_when_convertible(status: str) -> None:
    stranger = replace(_no_countertop(), actor_status=status, decided_by="ui reviewer")
    assert ready_candidates(_export(stranger)) == ()
    with pytest.raises(ConfirmationRefused, match="real-reviewer list"):
        _confirm(_export(stranger), _yes("no_countertop_answers:1"))


def test_a_v1_file_with_a_scripted_reviewer_is_refused(tmp_path: Path) -> None:
    from eval.smriti.candidates import _candidate_document, _digest

    entry = {
        name: value
        for name, value in _candidate_document(
            replace(_no_countertop(), decided_by="ui reviewer")
        ).items()
        if name not in {"overturned", "mistake_needed", "actor_status", "answer_source"}
    }
    document: dict[str, Any] = {
        "format": "smriti-candidates/1",
        "database": "synthetic",
        "exported_at": "2026-10-14T00:00:00+00:00",
        "code_version": "test",
        "candidates": [entry],
        "counts": {},
    }
    document["sha256"] = _digest(document)
    path = tmp_path / "v1.json"
    path.write_text(json.dumps(document))
    export = read_candidates(path)
    (only,) = export.candidates
    assert only.actor_status == UNCHECKED and not only.convertible
    assert ready_candidates(export) == ()
    with pytest.raises(ConfirmationRefused, match="cannot be confirmed"):
        _confirm(export, _yes(only.candidate_id))


def test_nothing_answered_is_refused() -> None:
    with pytest.raises(ConfirmationRefused, match="nothing"):
        _confirm(_export(_no_countertop()))


def test_a_candidate_not_in_the_export_or_not_convertible_is_refused() -> None:
    with pytest.raises(ConfirmationRefused, match="not in"):
        _confirm(_export(_no_countertop()), _yes("nope:1"))
    with pytest.raises(ConfirmationRefused, match="not convertible"):
        _confirm(_export(_milan()), _yes("architect_pairing_records:8"))


def test_answers_for_another_candidates_file_are_refused() -> None:
    export = _export(_no_countertop())
    with pytest.raises(ConfirmationRefused, match="another candidates file"):
        confirm_candidates(
            export, _answers(_yes("no_countertop_answers:1"), sha="0" * 64), **_given(export)
        )


def test_a_candidate_answered_twice_an_unknown_field_or_no_person_is_refused() -> None:
    export = _export(_no_countertop())
    with pytest.raises(ConfirmationRefused, match="twice"):
        _confirm(export, _yes("no_countertop_answers:1"), _yes("no_countertop_answers:1"))
    with pytest.raises(ConfirmationRefused, match="unknown"):
        _confirm(export, _yes("no_countertop_answers:1", colour="red"))
    with pytest.raises(ConfirmationRefused, match="verdict"):
        _confirm(export, {"candidate": "no_countertop_answers:1", "verdict": "maybe"})
    unnamed = _answers(_yes("no_countertop_answers:1"))
    unnamed["confirmed_by"] = " "
    with pytest.raises(ConfirmationRefused, match="confirmed_by"):
        confirm_candidates(export, unnamed, **_given(export))
    with pytest.raises(ConfirmationRefused, match="format"):
        confirm_candidates(
            export, {**unnamed, "format": "smriti-confirmations/1"}, **_given(export)
        )


# --- conflicts ------------------------------------------------------------------------------------


def test_two_answers_to_one_question_that_differ_are_refused() -> None:
    first = _countertop("no_countertop_answers:2")
    second = _countertop("no_countertop_answers:9")
    with pytest.raises(ConfirmationRefused, match="already"):
        _confirm(
            _export(first, second),
            _yes("no_countertop_answers:2", box=1),
            _yes("no_countertop_answers:9", box=2),
        )
    # The same answer twice is one case.
    same = _confirm(
        _export(first, second),
        _yes("no_countertop_answers:2", box=1),
        _yes("no_countertop_answers:9", box=1),
    )
    assert len(same.keys["rekha"].cases) == 1


def test_a_base_keys_cases_are_kept_and_a_conflict_with_them_is_refused() -> None:
    base = _confirm(_export(_countertop()), _yes("no_countertop_answers:2", box=1)).keys["rekha"]
    moved = _no_countertop("no_countertop_answers:1", page=OTHER_PAGE)
    merged = _confirm(_export(moved), _yes("no_countertop_answers:1"), bases=[base])
    assert len(merged.keys["rekha"].cases) == 2
    with pytest.raises(ConfirmationRefused, match="already"):
        _confirm(_export(_countertop()), _yes("no_countertop_answers:2", box=3), bases=[base])
    walls_base = _confirm(
        _export(_walls()), _yes("slot_row_review_decisions:6:walls", left="wall", right="open")
    ).keys["rekha-walls"]
    with pytest.raises(ConfirmationRefused, match="already"):
        _confirm(
            _export(_walls("slot_row_review_decisions:9:walls")),
            _yes("slot_row_review_decisions:9:walls", left="open", right="open"),
            bases=[walls_base],
        )


# --- which candidates a page shows ----------------------------------------------------------------


def test_ready_candidates_are_the_current_convertible_listed_ones_with_a_key_question() -> None:
    superseded = replace(_no_countertop("no_countertop_answers:10"), kind="superseded")
    carried = replace(_walls("slot_row_review_decisions:11:walls"), kind="carried")
    no_question = replace(_countertop("no_countertop_answers:12"), question=None)
    shown = (_no_countertop(), _countertop(), _batch(), _correction(), _walls(), _piece())
    export = _export(*shown, superseded, carried, no_question, _milan())
    assert [c.candidate_id for c in ready_candidates(export)] == [c.candidate_id for c in shown]


# --- the candidates file --------------------------------------------------------------------------


def test_the_candidates_file_is_written_once_and_refused_when_edited(tmp_path: Path) -> None:
    path = tmp_path / "candidates.json"
    exported = _export(_no_countertop(), _correction(), _milan())
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


# --- review of #1278: who confirms, which pictures, which boxes -----------------------------------


def test_the_confirmer_must_be_on_the_local_confirmers_list() -> None:
    export = _export(_no_countertop())
    stranger = {**_answers(_yes("no_countertop_answers:1")), "confirmed_by": "someone else"}
    with pytest.raises(ConfirmationRefused, match="confirmers list"):
        confirm_candidates(export, stranger, **_given(export))
    nobody_listed = Confirmers(names=None, source=None)
    with pytest.raises(ConfirmationRefused, match="GV_SMRITI_CONFIRMERS"):
        _confirm(export, _yes("no_countertop_answers:1"), confirmers=nobody_listed)


def test_a_reviewer_who_decides_is_not_a_confirmer_unless_on_the_confirmers_list() -> None:
    # Another reviewer (one who decides in the app) is not on the confirmers list: refused.
    export = _export(_no_countertop())
    reviewer = {**_answers(_yes("no_countertop_answers:1")), "confirmed_by": "reviewer-b"}
    with pytest.raises(ConfirmationRefused, match="not on the local confirmers list"):
        confirm_candidates(export, reviewer, **_given(export))


@pytest.mark.parametrize("verdict", ["yes", "no", "unsure"])
def test_a_person_never_confirms_a_case_they_decided_themselves(verdict: str) -> None:
    export = _export(_no_countertop())  # decided by "reviewer-a"
    both = Confirmers(names=frozenset({"reviewer-a"}), source="test")
    answers = {
        **_answers({**_yes("no_countertop_answers:1"), "verdict": verdict}),
        "confirmed_by": " Reviewer-A ",
    }
    with pytest.raises(ConfirmationRefused, match="their own decision"):
        confirm_candidates(export, answers, **_given(export, confirmers=both))


@pytest.mark.parametrize(
    "name",
    [
        "claude",
        "Claude Opus",
        "code:bot",
        "codex agent",
        "gpt-5",
        "an AI",
        "review-bot",
        "claudebot",
        "ClaudeCode",
        "gptreviewer",
        "opus",
        "\u0441laude",
        "a pers\u043en",
        "c1aude",
        "cl4ude",
        "0pus",
        "s0nnet",
        "g3mini",
        "c0dex",
        "anthr0pic",
        "copi1ot",
        "c l a u d e",
        "cla-ude",
        "G.P.T",
        "A.I.",
        "deepseek",
        "kimi",
    ],
)
def test_an_ai_an_agent_or_code_never_confirms_even_when_listed(name: str) -> None:
    export = _export(_no_countertop())
    listed = Confirmers(names=frozenset({name}), source="test")
    answers = {**_answers(_yes("no_countertop_answers:1")), "confirmed_by": name}
    with pytest.raises(ConfirmationRefused, match="a person"):
        confirm_candidates(export, answers, **_given(export, confirmers=listed))


@pytest.mark.parametrize(
    ("store", "why"),
    [
        ({PIC: "the picture is not in the store (FileNotFoundError)"}, "not in the store"),
        ({PIC: "the stored picture is not the one the question recorded"}, "not the one"),
        ({}, "not checked in the artifact store"),
    ],
)
def test_a_yes_is_refused_when_its_picture_bytes_are_missing_or_differ_in_the_store(
    store: dict[str, str | None], why: str
) -> None:
    export = _export(_no_countertop())
    with pytest.raises(ConfirmationRefused, match=why):
        _confirm(export, _yes("no_countertop_answers:1"), store=store)
    # A No joins nothing, so its picture is not needed.
    no = _confirm(export, {"candidate": "no_countertop_answers:1", "verdict": "no"}, store=store)
    assert no.keys == {}


def test_the_store_check_reads_the_bytes_and_compares_them_with_the_fingerprint(
    tmp_path: Path,
) -> None:
    import hashlib
    import io

    from eval.smriti.confirm import read_stored_picture
    from storage.local import LocalStore

    picture = b"an invented picture"
    sha = hashlib.sha256(picture).hexdigest()
    store = LocalStore(root=tmp_path)
    store.put("q/right.png", io.BytesIO(picture), content_type="image/png")
    store.put("q/other.png", io.BytesIO(b"another invented picture"), content_type="image/png")
    assert read_stored_picture(tmp_path, sha, "q/right.png") == (picture, None)
    data, problem = read_stored_picture(tmp_path, sha, "q/other.png")
    assert data is None and "not the one the question recorded" in (problem or "")
    data, problem = read_stored_picture(tmp_path, sha, "q/missing.png")
    assert data is None and "not in the store" in (problem or "")
    data, problem = read_stored_picture(tmp_path, sha, None)
    assert data is None and "not stored" in (problem or "")


def test_a_yes_needs_the_stored_pictures_the_page_showed() -> None:
    export = _export(_no_countertop())
    without = {"candidate": "no_countertop_answers:1", "verdict": "yes"}
    with pytest.raises(ConfirmationRefused, match="picture"):
        _confirm(export, without)
    with pytest.raises(ConfirmationRefused, match="picture"):
        _confirm(export, _yes("no_countertop_answers:1", pictures=["e" * 64]))
    with pytest.raises(ConfirmationRefused, match="picture"):
        _confirm(export, _yes("no_countertop_answers:1"), pictures={})
    # A No or an Unsure joins nothing, so it needs no picture.
    result = _confirm(export, {"candidate": "no_countertop_answers:1", "verdict": "no"})
    assert result.keys == {}


def test_a_box_must_be_one_the_stored_picture_numbers() -> None:
    export = _export(_countertop())
    with pytest.raises(ConfirmationRefused, match="numbers 3 boxes"):
        _confirm(export, _yes("no_countertop_answers:2", box=4))
    with pytest.raises(ConfirmationRefused, match="not recorded"):
        _confirm(export, _yes("no_countertop_answers:2", box=1), boxes={})
    among = _confirm(export, _yes("no_countertop_answers:2", not_among_boxes=True), boxes={})
    assert among.keys["rekha"].cases[0].truth == RowTruth(box=None, kind="not_among_boxes")


@pytest.mark.parametrize("decided_by", ["cb@example.test", "CB@Example.Test", "user 7", "User7"])
def test_a_confirmer_never_confirms_a_decision_made_under_one_of_their_app_names(
    decided_by: str,
) -> None:
    export = _export(replace(_no_countertop(), decided_by=decided_by))
    known = Confirmers(
        names=frozenset({"a person"}),
        source="test",
        app_names={"a person": frozenset({"cb@example.test", "user 7"})},
    )
    with pytest.raises(ConfirmationRefused, match="their own decision"):
        _confirm(export, _yes("no_countertop_answers:1"), confirmers=known)
    # Someone else's decision is still theirs to confirm.
    other = _export(replace(_no_countertop(), decided_by="reviewer-a"))
    assert _confirm(other, _yes("no_countertop_answers:1"), confirmers=known).keys
