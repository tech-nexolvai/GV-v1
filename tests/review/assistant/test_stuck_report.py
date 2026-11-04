"""Where a full check got stuck (`stuck_report.py`): each open answer's blocking step, the totals,
the pages with no architect comparison and the set pack's reading coverage. Made-up values; $0."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import reading_coverage, stuck_report
from app.review.assistant.chat_contract import ChatStuckReportOut, ChatTrustOut

VENDOR_FILE = "vendor set.pdf"


def _finding(finding_id: str, **fields: Any) -> dict[str, Any]:
    """A finding as the API serves it (with `proof` and `card`), made-up values."""
    group = fields.pop("group", "likely_problem")
    proof = {"group": group, "reason": fields.pop("reason", None)}
    proof.update(fields.pop("proof", {}))
    key = fields.pop("key", None)
    base: dict[str, Any] = {
        "id": finding_id,
        "page": 3,
        "item": "Base cabinet width",
        "architect": "n/a",
        "vendor": "see the drawing",
        "status": "check",
        "source": "architect",
        "created_by": "page_reviewer",
        "evidence_kind": "inferred",
        "witnesses": [],
        "checks": {},
        "vendor_box": {"page": 3, "file": VENDOR_FILE, "x0": 10, "y0": 10, "x1": 40, "y1": 20},
        "proof": proof,
        "card": {"id": finding_id, "question": {"key": key, "words": "?"} if key else None},
    }
    base.update(fields)
    return base


def _string(finding_id: str, **fields: Any) -> dict[str, Any]:
    """A vendor string that does not add up (the vendor's own parts against its overall)."""
    base: dict[str, Any] = {
        "status": "different",
        "source": "vendor_arithmetic",
        "vendor": '30"',
        "architect": '30 1/8"',
        "parts": ['10"', '10 1/8"', '10"'],
    }
    return _finding(finding_id, **{**base, **fields})


def _stage(finding: dict[str, Any]) -> str | None:
    found = stuck_report.stage_of(finding)
    return None if found is None else found[0]


# ---- the stages ----------------------------------------------------------------------------------


def test_red_green_and_a_reviewers_not_a_problem_have_no_stage() -> None:
    for group in ("clouded", "right", "dismissed"):
        assert stuck_report.stage_of(_finding("F1", group=group, status="different")) is None


def test_a_string_only_an_ai_listed_is_stuck_where_code_could_not_see_it() -> None:
    reason = (
        "Code could not confirm these parts form one string on the drawing (code found no label "
        "of the 30\" overall with its own dimension line at the string's spot, so it cannot see "
        "the row); please check. Never red: code reads only the AI's list of parts here."
    )
    finding = _string(
        "F2",
        key="look:size",
        reason=reason,
        checks={"parts_read": "ai", "verify_vendor": {"result": "cannot_read"}},
    )
    stage, words, used = stuck_report.stage_of(finding) or ("", "", {})
    assert stage == "string_not_seen"
    assert words.startswith('code found no label of the 30" overall')
    assert "could not read the number either" in words
    assert used["checks.parts_read"] == "ai"
    # Code's string step showing the list is not the overall's string is the same stage.
    gap = _string("F3", group="not_compared", string_reads={"outcome": "gap", "why": "a gap"})
    assert stuck_report.stage_of(gap) == (
        "string_not_seen",
        "a gap",
        {"source": "vendor_arithmetic", "string_reads": "gap"},
    )


def test_a_proved_string_held_by_the_blind_second_look_says_what_it_read() -> None:
    # Code proved the string (its own parts, end to end); the blind second look read the overall
    # next to the wrong part, not the part itself.
    finding = _string(
        "F4",
        key="likely",
        created_by="consistency",
        vendor='12 1/2"',
        witnesses=["arithmetic"],
        checks={
            "parts_read": "code",
            "second_read": "unsure",
            "adjudication": "unsure",
            "verify_vendor": {"result": "match"},
        },
        second_read_values={"vendor": '15"'},
    )
    stage, words, used = stuck_report.stage_of(finding) or ("", "", {})
    assert stage == "second_look"
    assert words == (
        "code proved it, but the blind second reading was unsure and the third reading was "
        'unsure; it read 15" where the finding names 12 1/2"'
    )
    assert used["second_read.vendor"] == '15"'
    # A Not-compared "not proved" whose second and third readings disagree: the second look too.
    disagrees = _string(
        "F5",
        group="not_compared",
        key="not_proved",
        proof={"not_compared": "not_proved"},
        created_by="consistency",
        witnesses=["arithmetic"],
        checks={"parts_read": "code", "second_read": "disagrees", "adjudication": "disagrees"},
    )
    assert _stage(disagrees) == "second_look"
    # A blind pick of another dimension.
    assert _stage(_finding("F6", key="pick", status="different")) == "second_look"


def test_code_reading_another_value_at_the_spot_is_the_reading_stage() -> None:
    finding = _string(
        "F7",
        key="look:size",
        witnesses=["arithmetic"],
        checks={"parts_read": "code", "verify_vendor": {"result": "mismatch", "read": '31"'}},
    )
    assert stuck_report.stage_of(finding) == (
        "reading",
        "code's own check backs it, but code reads 31\" at the vendor's spot",
        {
            "witnesses": ["arithmetic"],
            "checks.parts_read": "code",
            "checks.verify_vendor": "mismatch",
        },
    )
    # An AI's size whose vendor number code could not read at its spot.
    helper = _finding(
        "F8",
        created_by="helper:3",
        status="different",
        vendor='24"',
        inches={"vendor": "24", "architect": "23.75"},
        checks={"printed_vendor": "not_found", "verify_vendor": {"result": "cannot_read"}},
    )
    assert _stage(helper) == "reading"
    for reason in ("unreadable", "no_spot", "stroke_words", "gv_sticker"):
        assert _stage(_finding("F9", group="not_compared", proof={"not_compared": reason})) == (
            "reading"
        )


@pytest.mark.parametrize(
    ("reason", "stage"),
    [
        ("nothing_to_compare", "no_architect_value"),
        ("no_overall", "no_architect_value"),
        ("round_only", "no_architect_value"),
        ("no_sheet", "pairing"),
        ("different_reference", "same_span"),
        ("field_verify", "rule"),
        ("missing_setting", "rule"),
        ("words", "ai_only"),
        ("internal", "ai_only"),
        ("tag_unconfirmed", "not_proved"),
        ("not_proved", "not_proved"),
    ],
)
def test_each_not_compared_reason_has_its_stage(reason: str, stage: str) -> None:
    finding = _finding("F10", group="not_compared", proof={"not_compared": reason}, key=reason)
    assert _stage(finding) == stage


def test_a_missing_job_value_names_the_values() -> None:
    finding = _finding(
        "F11",
        group="not_compared",
        proof={"not_compared": "missing_setting"},
        key="missing_setting:cabinet depth|front overhang",
    )
    found = stuck_report.stage_of(finding)
    assert found is not None and found[:2] == (
        "rule",
        "a job value is not entered: cabinet depth and front overhang",
    )


def test_ai_looks_rules_pairing_and_same_span() -> None:
    look = _finding(
        "F12",
        key="look:labels",
        check_reason="pairing_not_certain",
        proof={"likely_problem": {"kind": "labels"}},
    )
    stage, words, _ = stuck_report.stage_of(look) or ("", "", {})
    assert stage == "ai_only" and words.startswith("found by the page reviewer (AI)")
    assert "(labels)" in words
    # A helper unsure which architect value the vendor's pairs with.
    helper = _finding(
        "F13", key="likely", created_by="helper:2", check_reason="pairing_not_certain"
    )
    assert _stage(helper) == "pairing"
    # Code's own please-check (its rule never makes this case red), a held kind, a rule note.
    assert _stage(_finding("F14", key="look:labels", created_by="room_list_check")) == "rule"
    assert (
        _stage(_finding("F15", key="held:vendor_span", status="different", proof={"held": True}))
        == "rule"
    )
    assert _stage(_finding("F16", group="rule_note")) == "rule"
    assert _stage(_finding("F17", key="unsized_ends")) == "rule"
    # The AI's pairing of two dimensions whose ends code did not confirm.
    assert _stage(_finding("F18", key="ends", status="different")) == "same_span"
    # An AI's difference code tried and could not prove.
    tried = _finding(
        "F19", group="not_compared", key="not_proved", proof={"not_compared": "not_proved"}
    )
    assert _stage(tried) == "not_proved"


def test_the_group_without_proof_follows_the_cards_question() -> None:
    def bare(key: str | None, status: str = "check") -> dict[str, Any]:
        finding = _finding("F20", key=key, status=status)
        del finding["proof"]
        return finding

    assert stuck_report.group_of(bare(None, "different")) == "wrong"
    assert stuck_report.group_of(bare(None, "same")) == "right"
    assert stuck_report.group_of(bare("nothing_to_compare")) == "not_compared"
    assert stuck_report.group_of(bare("string_not_verified")) == "not_compared"
    assert stuck_report.group_of(bare("missing_setting:front overhang")) == "not_compared"
    assert stuck_report.group_of(bare("look:finish")) == "likely_problem"
    assert stuck_report.group_of(bare("held:vendor_span", "different")) == "likely_problem"
    # The server's group wins over the card's question.
    assert stuck_report.group_of(_finding("F21", group="not_compared", key="look:size")) == (
        "not_compared"
    )


# ---- one answer, the totals, the pages and the reading -------------------------------------------


def test_one_row_per_finding_id_the_newest_version_and_never_a_dropped_one() -> None:
    first = _finding("F1", key="look:labels")
    newer = {**first, "page": 4}
    blocks: list[dict[str, Any]] = [
        {"type": "finding", "finding": first},
        {"type": "finding", "finding": _finding("F2", key="look:finish")},
        {"type": "finding", "finding": newer},
        {"type": "finding", "finding": _finding("F3", key="look:scope")},
        {"type": "finding_dropped", "finding_id": "F3", "reason": "a duplicate"},
    ]
    found, merged = stuck_report.answers(blocks)
    assert [f["id"] for f in found] == ["F1", "F2"] and found[0]["page"] == 4
    assert merged == 1


def _coverage() -> dict[str, Any]:
    return {
        "pages": [
            {"page": 1, "status": "not_comparable", "note": "divider page"},
            {
                "page": 2,
                "status": "not_comparable",
                "drawing_needed": True,
                "drawing_needed_why": "the architect shows it only as a tag on a plan",
            },
            {
                "page": 3,
                "status": "not_comparable",
                "pairing_unsure": True,
                "pairing_unsure_why": "architect page 7 or architect page 9?",
            },
            {"page": 4, "status": "different", "pair_page": 8},
            {"page": 5, "status": "not_comparable", "note": "the architect prints no sizes"},
        ]
    }


def _reading() -> dict[str, Any]:
    return {
        "schema": reading_coverage.SCHEMA,
        "sides": {
            "vendor": {"read": 90, "printed": 100},
            "architect": {"read": 50, "printed": 80},
        },
        "not_read_by_reason": {
            "vendor": {"stacked_fraction": 4, "sideways": 6},
            "architect": {"in_note": 30},
        },
        "pages": [
            {
                "file": VENDOR_FILE,
                "page": 3,
                "sides": {"vendor": {"read": 20, "printed": 25}},
                "not_read": [
                    {"side": "vendor", "reason": "stacked_fraction", "count": 2},
                    {"side": "vendor", "reason": "sideways"},
                    {"side": "vendor", "reason": "stacked_fraction"},
                ],
            },
            {"file": VENDOR_FILE, "page": 6, "sides": {"vendor": {"read": 5, "printed": 5}}},
            {"file": "architect.pdf", "page": 3, "sides": {"architect": {"read": 9, "printed": 9}}},
        ],
    }


def _answer_blocks() -> list[dict[str, Any]]:
    copy = _finding("F31", group="not_compared", proof={"not_compared": "nothing_to_compare"})
    copy["card"] = {"id": "F30", "question": {"key": "nothing_to_compare", "words": "?"}}
    findings = [
        _finding("F30", group="not_compared", proof={"not_compared": "nothing_to_compare"}),
        copy,
        _finding("F32", key="look:labels"),
        _string("F33", key="look:size", checks={"parts_read": "ai"}),
        _finding("F34", group="clouded", status="different", page=4),
        _finding("F35", group="right", status="same", page=4),
    ]
    return [
        {"type": "coverage", "coverage": _coverage()},
        *({"type": "finding", "finding": f} for f in findings),
        {"type": "trust", "trust": {"counts": {}}},
    ]


def test_the_report_counts_stages_cards_pages_and_reading() -> None:
    report = stuck_report.from_blocks(_answer_blocks(), reading=_reading())
    assert report["schema"] == stuck_report.SCHEMA
    assert report["answers"] == 6 and report["stuck"] == 4 and report["merged"] == 0
    assert report["groups"] == {
        "wrong": 1,
        "right": 1,
        "likely_problem": 2,
        "not_compared": 2,
        "rule_note": 0,
        "dismissed": 0,
    }
    stages = {row["stage"]: row for row in report["stages"]}
    assert list(stages) == ["string_not_seen", "no_architect_value", "ai_only"]
    # Two answers on one card count as one card.
    assert stages["no_architect_value"]["answers"] == 2
    assert stages["no_architect_value"]["cards"] == 1
    assert stages["no_architect_value"]["by_group"] == {"not_compared": 2}
    rows = {row["id"]: row for row in report["findings"]}
    assert rows["F31"]["card"] == "F30" and "card" not in rows["F30"]
    assert set(rows) == {"F30", "F31", "F32", "F33"}
    # The pages: pairing unsure and no architect drawing always; nothing to compare only with
    # answers on it (a divider is not stuck); a compared page never.
    assert [(p["page"], p["stage"], p["answers"]) for p in report["pages"]] == [
        (2, "no_architect_drawing", 0),
        (3, "pairing", 4),
    ]
    assert report["pages"][1]["reason"] == "pairing unsure (architect page 7 or architect page 9?)"
    reading = report["reading"]
    assert reading["vendor"] == {
        "read": 90,
        "printed": 100,
        "percent": 90,
        "not_read": {"sideways": 6, "stacked_fraction": 4},
    }
    assert reading["architect"]["percent"] == 62
    # Only the vendor pages that have a stuck answer.
    assert reading["pages"] == [
        {
            "page": 3,
            "file": VENDOR_FILE,
            "read": 20,
            "printed": 25,
            "not_read": {"stacked_fraction": 3, "sideways": 1},
        }
    ]
    # The API's shape takes it as it is.
    ChatStuckReportOut.model_validate(
        {
            **{k: v for k, v in report.items() if k != "schema"},
            "message_id": "00000000-0000-0000-0000-000000000001",
            "created_at": "2026-11-04T00:00:00Z",
            "stored": True,
        }
    )
    words = "\n".join(stuck_report.lines(report, "A title"))
    assert "string_not_seen" in words and "pairing unsure" in words
    assert "vendor: 90 of 100 (90%)" in words


def test_a_stored_answers_findings_get_their_proof_and_card_first() -> None:
    # A stored answer's findings carry neither `proof` nor `card`: code works them out as the API
    # does, so the report is the same from a stored check as from a saved run.
    stored = {
        "id": "F40",
        "page": 2,
        "item": "Countertop depth",
        "category": "countertop",
        "architect": "n/a",
        "vendor": "see the drawing",
        "status": "check",
        "source": "architect",
        "created_by": "helper:1",
        "checks": {},
    }
    blocks = stuck_report.prepared([{"type": "finding", "finding": stored}])
    finding = blocks[0]["finding"]
    assert "group" in finding["proof"] and "card" in finding
    assert stuck_report.from_blocks([{"type": "finding", "finding": stored}])["answers"] == 1


# ---- stored with the check -----------------------------------------------------------------------


def test_attach_stores_the_report_in_the_trust_block_without_an_event(tmp_path: Path) -> None:
    (tmp_path / reading_coverage.FILE).write_text(json.dumps(_reading()), encoding="utf-8")
    events: list[Any] = []
    blocks = _answer_blocks()
    engine = SimpleNamespace(
        settings=SimpleNamespace(mode="full_check"),
        tools=SimpleNamespace(workspace=SimpleNamespace(set_pack=tmp_path)),
        _trust_index=len(blocks) - 1,
        _lock=threading.Lock(),
        blocks=blocks,
        stored_blocks=lambda: json.loads(json.dumps(blocks)),
        emit=lambda event, data: events.append((event, data)),
    )
    stuck_report.attach(engine)  # type: ignore[arg-type]
    trust = engine.blocks[-1]["trust"]
    assert trust["counts"] == {} and trust[stuck_report.FIELD]["stuck"] == 4
    assert trust[stuck_report.FIELD]["reading"]["vendor"]["printed"] == 100
    assert events == []
    assert stuck_report.stored(engine.blocks) == trust[stuck_report.FIELD]
    # The chat's read model leaves it out: the chat's payload stays the same size.
    checks = {
        "printed_at_spot": {},
        "second_read": {},
        "rules": [],
        "arithmetic": {},
        "lead_checked": 0,
    }
    shown = ChatTrustOut.model_validate(
        {**trust, "by_category": {}, "checks": checks, "not_checked": []}
    )
    assert stuck_report.FIELD not in shown.model_dump()
    # Another answer (a chat question) gets none.
    chat = SimpleNamespace(**{**vars(engine), "settings": SimpleNamespace(mode="chat")})
    chat.blocks = _answer_blocks()
    stuck_report.attach(chat)  # type: ignore[arg-type]
    assert stuck_report.FIELD not in chat.blocks[-1]["trust"]


# ---- the command line ----------------------------------------------------------------------------


def test_the_command_line_reads_the_newest_full_check_of_a_saved_chat(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    older = [{"type": "finding", "finding": _finding("F1", key="look:labels")}]
    chat = {
        "package_name": "Test set",
        "messages": [
            {"role": "user", "mode": "full_check", "blocks": []},
            {"role": "assistant", "mode": "full_check", "id": "a1", "blocks": older},
            {"role": "user", "mode": "full_check", "blocks": []},
            {"role": "assistant", "mode": "full_check", "id": "a2", "blocks": _answer_blocks()},
            {"role": "user", "mode": "full_check", "blocks": []},
            # Stopped before any check: not the newest check.
            {"role": "assistant", "mode": "full_check", "id": "a3", "blocks": [{"type": "text"}]},
        ],
    }
    run = tmp_path / "run.json"
    run.write_text(json.dumps(chat), encoding="utf-8")
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / reading_coverage.FILE).write_text(json.dumps(_reading()), encoding="utf-8")
    assert stuck_report.main([str(run), "--pack", str(pack), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["answers"] == 6 and report["reading"]["vendor"]["read"] == 90
    assert stuck_report.main([str(run)]) == 0
    words = capsys.readouterr().out
    assert "answer a2" in words and "the newest of 2 full checks" in words
    assert "F33" in words and "F1 " not in words
