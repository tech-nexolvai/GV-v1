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
    reviewer_value_refusal,
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
    assert proof_line(made).startswith("Decided by GV's reviewer: Hinge side")
    assert "reviewer-one" not in proof_line(made)
    assert group_words(made, "clouded") == "Wrong, decided by GV's reviewer (clouded)"
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
    assert proof_line(made).startswith("Decided by GV's reviewer")
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


def test_an_older_workbooks_set_rows_are_read_back_as_they_were_listed() -> None:
    """#1803: an answer stored before its set gaps were kept: the list made again after a
    decision keeps the set's own rows, read back from the workbook the check made."""
    from app.review.assistant import not_compared

    gaps = not_compared.gap_rows(
        [
            {
                "what": "Sheet A-3: the island elevation",
                "why": "no_sheet",
                "needs": "the sheet",
                "page": 4,
            },
            {"what": "A finish code the architect lists without a size", "needs": "a size"},
        ]
    )
    assert gaps and all(row.level == "set" for row in gaps)
    item = not_compared.Row(
        finding_id="F9",
        page=2,
        area="",
        product="Countertops",
        item="Sink base width",
        vendor='33"',
        architect="",
        reason=gaps[0].reason,
        why="no size",
        needed="a size",
        reference="page 2",
        file=None,
        box=None,
    )
    back = not_compared.set_rows_of(not_compared.xlsx([item, *gaps]))
    assert back is not None
    assert [(r.page, r.item, r.reason, r.why, r.needed, r.reference, r.level) for r in back] == [
        (r.page, r.item, r.reason, r.why, r.needed, r.reference, r.level) for r in gaps
    ]
    assert not_compared.set_rows_of(b"not a workbook") is None


def test_an_older_single_sheet_workbook_and_the_csv_give_back_their_set_rows_only() -> None:
    """Workbooks before the set sheet, and the CSV fallback, list items and set rows together: a
    set row has no product and no vendor or architect value; the items come back from findings."""
    from app.review.assistant import not_compared
    from app.review.assistant.workbook import write_xlsx

    gaps = not_compared.gap_rows(
        [{"what": "Sheet A-3: the island elevation", "needs": "the sheet", "page": 4}]
    )
    item = not_compared.Row(
        finding_id="F9",
        page=2,
        area="",
        product="Countertops",
        item="Sink base width",
        vendor='33"',
        architect="",
        reason=gaps[0].reason,
        why="no size",
        needed="a size",
        reference="page 2",
        file=None,
        box=None,
    )
    table = not_compared._table([item, *gaps])
    single = write_xlsx([("Not compared", not_compared.COLUMNS, table)])
    for back in (
        not_compared.set_rows_of(single),
        not_compared.set_rows_of(
            not_compared.csv_text([item, *gaps]).encode("utf-8-sig"), csv=True
        ),
    ):
        assert back is not None
        assert [(r.item, r.reason, r.needed, r.level) for r in back] == [
            (r.item, r.reason, r.needed, r.level) for r in gaps
        ]
    assert not_compared.set_rows_of(b"\xff\xfe", csv=True) is None


def test_a_reviewers_value_is_never_credited_to_the_vendors_own_parts() -> None:
    """#1803 review: an agreed vendor-arithmetic item with a value the reviewer typed says it is
    the reviewer's, never "the vendor's own parts add up to" the typed value."""
    from app.review.assistant.send_package import requires_words

    summed = {
        "id": "S1",
        "item": "Run of cabinets",
        "status": "different",
        "source": "vendor_arithmetic",
        "parts": ['10"', '10"'],
        "printed_total": '21"',
        "vendor": '21"',
        "architect": '20"',
        "inches": {"vendor": "21", "architect": "20"},
    }
    typed = {**summed, "decision": {"decision": "agree", "should_be": '34 1/2"'}}
    assert requires_words(typed) == "GV's reviewer requires 34 1/2\""
    assert not requires_words(summed).startswith("GV's reviewer")
    assert reviewer_value_refusal(summed, '21 "') is not None  # the vendor's own value
    assert reviewer_value_refusal(summed, "wider") is not None  # no number
    assert reviewer_value_refusal(summed, '20"') is None


def test_keeping_the_architects_pre_filled_value_still_names_the_architect_everywhere() -> None:
    """#1803 re-review: "add a red mark" pre-fills the architect's value; kept as it is, every file
    names the architect (the cloud note, the letter, the card), the check is the reviewer's, and
    the card never says no drawing prints it."""
    from app.review.assistant import proof_words
    from app.review.assistant.send_package import requires_words

    kept = {
        "id": "K1",
        "item": "Sink base width",
        "status": "check",
        "source": "architect",
        "architect": '34"',
        "vendor": '33"',
        "inches": {"architect": "34", "vendor": "33"},
        "page": 3,
        "arch_box": {"page": 1, "x0": 1, "y0": 1, "x1": 2, "y1": 2},
        "decision": {"decision": "agree", "should_be": ' 34 " ', "note": "Hinge side"},
    }
    assert governs(kept) != "reviewer"
    assert proof_words.reference_kind(kept) == "architect"
    assert requires_words(kept).startswith("Architect requires 34")
    assert "architect" in should_be(kept)
    assert proof_words.checked_words(kept).startswith("Decided by GV's reviewer")
    assert "GV's reviewer requires" not in proof_words.sentence(kept)
    assert proof_words.should_text(kept) == '34"'
    # A value code does not know is the reviewer's, as before.
    typed = {**kept, "decision": {**kept["decision"], "should_be": '34 1/2"'}}
    assert proof_words.reference_kind(typed) == "reviewer"
