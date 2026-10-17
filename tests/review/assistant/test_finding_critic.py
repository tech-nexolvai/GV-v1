"""The adversarial critic of a whole-set answer's findings (#1498): which findings, the batches,
the request, and the strict reading of its reply. $0: the model is a fake."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.review.assistant.finding_critic import (
    CRITIC_BATCH,
    CRITIC_BATCHES,
    batches,
    candidates,
    criticise,
    parse_reply,
    verdict_of,
)

YES: dict[str, Any] = {
    "same_item": "yes",
    "right_dimension": "yes",
    "vendor_ink": "yes",
    "architect_side": "yes",
}


def _entry(finding_id: str = "F1", note: str = "", **answers: str) -> dict[str, Any]:
    return {"id": finding_id, **YES, **answers, "note": note}


def _reply(*entries: dict[str, Any]) -> str:
    return json.dumps({"findings": list(entries)})


def _finding(finding_id: str, status: str = "different", **changes: Any) -> dict[str, Any]:
    finding = {
        "id": finding_id,
        "page": 4,
        "item": "Upper outlet height",
        "architect": '72"',
        "vendor": '73 1/2"',
        "status": status,
        "source": "architect",
        "arch_picture_id": f"{finding_id}-a",
        "vendor_picture_id": f"{finding_id}-v",
    }
    finding.update(changes)
    return finding


# ---- code's reading of the answers ---------------------------------------------------------------


def test_any_no_objects_with_the_critics_note() -> None:
    verdict = parse_reply(
        _reply(_entry(vendor_ink="no", note="The 73 1/2 is GV's red note.")), ["F1"]
    )["F1"]
    assert verdict.result == "objects" and verdict.note == "The 73 1/2 is GV's red note."


def test_a_no_without_a_note_gets_codes_plain_words() -> None:
    verdict = verdict_of({**YES, "architect_side": "no"}, "")
    assert verdict.result == "objects"
    assert verdict.note == "the reference may not be the architect's drawing"


def test_four_yes_pass_and_any_unsure_is_unsure() -> None:
    passed = parse_reply(_reply(_entry()), ["F1"])["F1"]
    assert passed.result == "passed" and passed.note == ""
    assert parse_reply(_reply(_entry(same_item="unsure")), ["F1"])["F1"].result == "unsure"


def test_a_fenced_reply_is_read() -> None:
    fenced = "```json\n" + _reply(_entry()) + "\n```"
    assert parse_reply(fenced, ["F1"])["F1"].result == "passed"


@pytest.mark.parametrize(
    "reply",
    [
        "The findings look right.",
        "",
        json.dumps([_entry()]),
        json.dumps({"findings": [_entry()], "summary": "ok"}),
        json.dumps({"findings": "all fine"}),
        "{" + _reply(_entry())[1:-1],
    ],
)
def test_a_reply_that_is_not_the_object_is_unsure_for_every_finding(reply: str) -> None:
    verdicts = parse_reply(reply, ["F1", "F2"])
    assert {key: verdict.result for key, verdict in verdicts.items()} == {
        "F1": "unsure",
        "F2": "unsure",
    }


def test_a_reply_that_is_not_text_never_crashes() -> None:
    assert parse_reply(None, ["F1"])["F1"].result == "unsure"
    assert parse_reply(b"{}", ["F1"])["F1"].result == "unsure"


def test_a_bad_entry_is_unsure_for_that_finding_only() -> None:
    reply = _reply(
        _entry("F1", vendor_ink="maybe"),
        {**_entry("F2"), "confidence": 0.9},
        _entry("F3", note=None),  # type: ignore[arg-type]
        _entry("F4", vendor_ink="no", note="GV's note"),
        _entry("F9", same_item="no"),
    )
    verdicts = parse_reply(reply, ["F1", "F2", "F3", "F4", "F5"])
    assert {key: verdict.result for key, verdict in verdicts.items()} == {
        "F1": "unsure",
        "F2": "unsure",
        "F3": "unsure",
        "F4": "objects",
        "F5": "unsure",  # not answered
    }  # F9 was never asked: ignored


def test_a_finding_answered_twice_is_unsure() -> None:
    reply = _reply(_entry("F1"), _entry("F1", same_item="no"))
    assert parse_reply(reply, ["F1"])["F1"].result == "unsure"


# ---- which findings, in which batches ------------------------------------------------------------


def test_only_findings_compared_against_a_drawing_with_both_crops_differences_first() -> None:
    findings = [
        _finding("F1", status="same"),
        _finding("F2", status="check"),
        _finding("F3"),
        _finding("F4", source="rules"),
        _finding("F5", source="vendor_arithmetic"),
        _finding("F6", arch_picture_id=None),
        _finding("F7", source="brand_prototype"),
    ]
    assert [f["id"] for f in candidates(findings)] == ["F3", "F7", "F2", "F1"]


def test_batches_hold_at_most_eight_and_there_are_at_most_four() -> None:
    findings = [_finding(f"F{n}") for n in range(1, 40)]
    chunks = batches(findings)
    assert len(chunks) == CRITIC_BATCHES == 4
    assert all(len(chunk) == CRITIC_BATCH == 8 for chunk in chunks)
    assert [len(chunk) for chunk in batches(findings[:10])] == [8, 2]


# ---- the request ------------------------------------------------------------------------------------


def test_each_finding_is_shown_with_its_claims_and_both_crops() -> None:
    sent: list[list[dict[str, Any]]] = []

    def model(content: list[dict[str, Any]]) -> str:
        sent.append(content)
        return _reply(_entry("F1"), _entry("F2", vendor_ink="no", note="Red note."))

    batch = [_finding("F1"), _finding("F2", source="brand_prototype"), _finding("F3")]
    crops = {"F1": (b"arch1", b"vendor1"), "F2": (b"arch2", b"vendor2")}
    verdicts = criticise(batch, crops, model)

    assert {key: verdict.result for key, verdict in verdicts.items()} == {
        "F1": "passed",
        "F2": "objects",
    }  # F3 had no crops: not asked
    content = sent[0]
    assert [block["type"] for block in content] == ["text", "image", "image"] * 2 + ["text"]
    assert 'Claimed reference value: 72"' in content[0]["text"]
    assert 'Claimed vendor value: 73 1/2"' in content[0]["text"]
    assert "a brand prototype drawing" in content[3]["text"]
    assert "vendor_ink:" in content[-1]["text"] and "GV's reviewers" in content[-1]["text"]


def test_nothing_to_show_makes_no_call() -> None:
    def model(content: list[dict[str, Any]]) -> str:
        raise AssertionError("no call expected")

    assert criticise([_finding("F1")], {}, model) == {}
