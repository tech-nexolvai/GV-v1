"""The reviewer's overrides (#1803): what each decision does to an item, in every file's words.

- "It's a mistake: add a red mark" = Agree, with the value it should be (`decision.should_be`):
  Wrong (clouded) also where the architect prints no value; the green "should be" is the
  reviewer's, marked "(reviewer)"; the proof is "decided by the reviewer" with the note when the
  vendor may read it, never the reviewer's login.
- "It's fine: mark Right" and "Not a mistake: remove the red mark" = Not a problem: Right
  (reviewer) in the vendor's files, on any item (a Not-compared "same" too).

Pure code, $0.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.review.assistant import not_compared
from app.review.assistant.chat_contract import ChatFindingDecisionIn
from app.review.assistant.proof import (
    governs,
    group_words,
    proof_group,
    proof_line,
    proof_of,
    reviewer_decided,
    should_be,
    should_be_value,
    stated_requirement,
)
from app.review.assistant.send_package import register_status
from tests.review.assistant.test_proof_drawing import _ai_only, _decision, _proved, _right


def _agree(**decision: Any) -> dict[str, Any]:
    return {"decision": "agree", "by": "reviewer-one", **decision}


def test_a_red_mark_with_the_reviewers_value_where_the_architect_gives_none() -> None:
    plain = _decision(inches={"vendor": "30"})
    assert proof_group(plain) == "not_compared"
    # Agree alone: the vendor cannot be told what it should be, so nothing changes (#1622).
    assert proof_group({**plain, "decision": _agree()}) == "not_compared"
    made = {**plain, "decision": _agree(should_be='34 1/2"', note="Hinge side")}
    assert proof_group(made) == "clouded"
    assert stated_requirement(made) == '34 1/2"'
    assert should_be_value(made) == '34 1/2"'
    assert governs(made) == "reviewer"
    assert should_be(made) == '34 1/2" (reviewer)'
    assert reviewer_decided(made)
    assert proof_line(made).startswith("Decided by the reviewer: Hinge side")
    assert "reviewer-one" not in proof_line(made)
    assert group_words(made, "clouded") == "Wrong, decided by the reviewer (clouded)"
    assert proof_of(made)["should_be"] == '34 1/2" (reviewer)'
    assert register_status(made) == "wrong"
    # It leaves the Not-compared list.
    assert [row.finding_id for row in not_compared.rows([plain])] == ["F3"]
    assert not_compared.rows([made]) == []


def test_the_architects_value_kept_by_the_reviewer_still_names_the_architect() -> None:
    finding = _ai_only()
    assert proof_group(finding) == "not_compared"
    kept = {**finding, "decision": _agree(should_be="34 ″")}
    assert proof_group(kept) == "clouded"
    assert governs(kept) == governs(finding) and should_be(kept) == should_be(finding) != ""
    assert should_be_value(kept) == '34"'


def test_an_internal_note_never_reaches_the_vendor() -> None:
    bare = _decision(inches={"vendor": "30"})
    made = {**bare, "decision": _agree(should_be='31"', note="see GV Review's record")}
    assert proof_line(made).startswith("Decided by the reviewer")
    assert "record" not in proof_line(made)
    internal = {**bare, "decision": _agree(should_be="AI said 30")}
    assert proof_group(internal) == "not_compared"


@pytest.mark.parametrize(
    "finding",
    [
        _decision(),
        _ai_only(),
        # A "same" seen on one side only is Not compared (#1684); it can be marked Right too.
        _right(compared=False),
        # A Wrong code proved: "Not a mistake: remove the red mark".
        _proved(),
    ],
)
def test_marked_right_is_right_reviewer_whatever_the_item_was(finding: dict[str, Any]) -> None:
    decided = {**finding, "decision": {"decision": "not_a_problem", "by": "R", "note": "Fits"}}
    assert proof_group(decided) == "dismissed"
    assert register_status(decided) == "right"
    assert not_compared.rows([decided]) == []
    assert proof_of(decided)["reason"] == "The reviewer marked it Right."


def test_a_code_proved_wrong_agreed_keeps_codes_proof() -> None:
    agreed = {**_proved(), "decision": _agree()}
    assert proof_group(agreed) == "clouded" and not reviewer_decided(agreed)
    assert group_words(agreed, "clouded") == "Wrong, proved by code (clouded)"


def test_the_should_be_goes_with_an_agree_only() -> None:
    assert ChatFindingDecisionIn(decision="agree", should_be='30"').should_be == '30"'
    assert ChatFindingDecisionIn(decision="not_a_problem", should_be=" ").should_be == " "
    with pytest.raises(ValidationError):
        ChatFindingDecisionIn(decision="not_a_problem", should_be='30"')
    with pytest.raises(ValidationError):
        ChatFindingDecisionIn(decision="agree", should_be="x" * 81)
