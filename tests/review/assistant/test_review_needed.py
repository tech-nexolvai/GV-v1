"""Review needed (Anant, 2026-10-22): never "Approved" just because the system could not look.

A finished check with no proved Wrong (or only minor ones) whose Not compared items are more than
10% of all its items is "Review needed: N items need a person before this can be approved". The
vendor reads only "Under review". Every count here is made up.
"""

from __future__ import annotations

import io
import re
import zipfile
from decimal import Decimal
from typing import Any

import pytest

from app.review.assistant import proof
from app.review.assistant.chat_findings import make_proof_file
from app.review.assistant.chat_store import message_out
from app.review.assistant.chat_summary import code_summary
from app.review.assistant.review_action import (
    NEXT_STEP,
    NOT_REVIEWED_NOTE,
    REVIEW_NEEDED_SHARE,
    REVIEW_NEEDED_VARIABLE,
    RULE_NOTE_NOTE,
    CheckState,
    review_action,
    review_needed_share,
    stamp_words,
)
from app.review.assistant.sandbox import ChatWorkspace
from app.review.assistant.send_package import (
    PackageContext,
    letter_of,
    make_package_file,
    package_context,
    submittal_rows,
)
from tests.review.assistant.test_proof_drawing import _decision, _proved, _right
from tests.review.assistant.test_rule_notes import _filler
from tests.review.assistant.test_send_package import (
    FORBIDDEN,
    IDENTITY,
    _message,
    _minor,
    _text,
    workspace,  # noqa: F401  (the fixture)
)

_APPROVED = re.compile(r"\bapproved\b", re.IGNORECASE)


def _rights(count: int) -> list[dict[str, Any]]:
    return [_right(id=f"R{n}") for n in range(count)]


def _unproved(count: int) -> list[dict[str, Any]]:
    """Items code could not prove the same end points of: Not compared (readable, so never the
    illegible-submittal rule)."""
    return [_decision(id=f"N{n}", keep_reason="measures_something_else") for n in range(count)]


def _decided(decision: str, **more: str) -> dict[str, str]:
    """A reviewer's decision row, as the screen's override stores it (#1814)."""
    return {"decision": decision, "by": "R", "at": "2026-10-22T10:00:00Z", **more}


def _context() -> PackageContext:
    return PackageContext(
        identity=IDENTITY,
        date="2026-10-22",
        round_no=1,
        rounds=None,
        state=CheckState(),
        labels={},
    )


# ---- the threshold ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("not_compared", "right", "action"),
    [
        (0, 0, "approved"),  # nothing at all: nothing to look at
        (0, 10, "approved"),
        (1, 9, "approved_as_noted"),  # exactly 10%: not MORE than 10%
        (2, 19, "approved_as_noted"),  # 2 of 21 = 9.5%
        (2, 17, "review_needed"),  # 2 of 19 = 10.5%
        (10, 90, "approved_as_noted"),  # 10% again, at a larger size
        (11, 89, "review_needed"),  # 11%
        (52, 3, "review_needed"),  # most items not compared
    ],
)
def test_more_than_ten_percent_not_compared_is_review_needed(
    not_compared: int, right: int, action: str
) -> None:
    found = review_action([*_unproved(not_compared), *_rights(right)])
    assert found.action == action
    assert (found.not_compared, found.items) == (not_compared, not_compared + right)


def test_the_line_says_how_many_items_need_a_person() -> None:
    found = review_action([*_unproved(5), _right()])
    assert (found.action, found.rule, found.words) == ("review_needed", "RN", "Review needed")
    assert found.reason == (
        "5 items need a person before this can be approved: 5 of 6 items (83.3%) were not "
        "compared, more than the 10% allowed."
    )
    assert found.note == NOT_REVIEWED_NOTE
    assert found.required_by == "GV: confirm the remaining items"
    assert found.stamped  # a finished check: the package is made, marked Under review
    single = review_action(_unproved(1))
    assert single.reason.startswith("1 item needs a person before this can be approved: 1 of ")


def test_ask_the_architect_items_are_decided_not_unchecked() -> None:
    """#1843: a size the architect specifies that breaks a GV rule is decided (GV asks the
    architect): it counts among all items but never as Not compared."""
    one = review_action([_filler(), *_unproved(1), *_rights(8)])
    assert (one.action, one.rule) == ("approved_as_noted", "S6")  # 1 of 10 not compared
    assert one.not_compared == 1 and one.items == 10
    many = review_action([_filler(id=f"A{n}") for n in range(9)] + _unproved(1))
    assert many.action == "approved_as_noted"
    over = review_action([_filler(), *_unproved(2), *_rights(7)])
    assert (over.action, over.note) == ("review_needed", f"{NOT_REVIEWED_NOTE} {RULE_NOTE_NOTE}")


def test_the_architect_giving_no_information_still_counts_as_not_compared() -> None:
    """Nothing could be looked at for those items either: a person decides them."""
    assert review_action([_decision(), *_rights(9)]).action == "approved"
    assert review_action([_decision(id="D1"), _decision(id="D2"), *_rights(8)]).action == (
        "review_needed"
    )


def test_minor_corrections_are_never_approved_as_noted_with_too_much_not_compared() -> None:
    found = review_action([_minor(), *_unproved(3), *_rights(2)])
    assert (found.action, found.corrections) == ("review_needed", 1)
    assert found.reason.endswith("1 minor correction noted as well.")
    assert review_action([_minor(), *_rights(9)]).action == "approved_as_noted"


def test_revise_and_resubmit_and_check_incomplete_are_unchanged() -> None:
    many = _unproved(40)
    assert review_action([_proved(), *many]).action == "revise_resubmit"
    assert review_action(many, CheckState(still_open=1)).action == "revise_resubmit"
    assert review_action(many, CheckState(incomplete="it was stopped")).action == (
        "not_for_sending"
    )


def test_the_threshold_is_a_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert REVIEW_NEEDED_SHARE == Decimal("0.10")
    assert review_needed_share({}) == Decimal("0.10")
    assert review_needed_share({REVIEW_NEEDED_VARIABLE: "0.25"}) == Decimal("0.25")
    for bad in ("", "  ", "ten", "1.5", "-0.1", "nan", "inf"):
        assert review_needed_share({REVIEW_NEEDED_VARIABLE: bad}) == Decimal("0.10"), bad
    findings = [*_unproved(2), *_rights(8)]  # 20%
    assert review_action(findings).action == "review_needed"
    assert review_action(findings, share=Decimal("0.25")).action == "approved_as_noted"
    assert review_action(findings, share=Decimal("0.20")).action == "approved_as_noted"
    assert review_action(findings, share=Decimal("0.19")).action == "review_needed"
    assert review_action(_rights(3) + _unproved(1), share=Decimal(0)).action == "review_needed"
    assert review_action(_unproved(5), share=Decimal(1)).action == "approved_as_noted"
    monkeypatch.setenv(REVIEW_NEEDED_VARIABLE, "0.25")
    assert review_action(findings).action == "approved_as_noted"
    found = review_action([*_unproved(3), *_rights(7)])
    assert found.reason.endswith("more than the 25% allowed.")


# ---- the reviewer's decisions re-compute it ----------------------------------------------------


def test_the_reviewers_decisions_turn_review_needed_into_approved_as_noted() -> None:
    blocks: list[dict[str, Any]] = [
        {"type": "finding", "finding": {**finding, "verified": True, "created_by": "lead"}}
        for finding in [*_unproved(5), *_rights(5)]
    ]
    blocks.append({"type": "package", "package": {"state": CheckState().as_dict(), "numbers": {}}})
    message = _message(blocks)
    before = message_out(message, []).review_action
    assert before is not None and before.action == "review_needed"
    assert (before.not_compared, before.items) == (5, 10)
    # Three marked Right: 2 of 10 still Not compared (20%): still Review needed.
    some = {f"N{n}": _decided("not_a_problem") for n in range(3)}
    still = message_out(message, [], some).review_action
    assert still is not None and still.action == "review_needed"
    assert "2 items need a person" in still.reason
    # Four marked Right: 1 of 10 (10%): Approved as noted.
    enough = {f"N{n}": _decided("not_a_problem") for n in range(4)}
    after = message_out(message, [], enough).review_action
    assert after is not None and (after.action, after.rule) == ("approved_as_noted", "S6")
    # All five marked Right: nothing left to look at: Approved.
    every = {f"N{n}": _decided("not_a_problem") for n in range(5)}
    approved = message_out(message, [], every).review_action
    assert approved is not None and approved.action == "approved"
    # Made Wrong instead: a correction that changes fabrication, Revise and resubmit.
    wrong = {"N0": _decided("agree", should_be='32"')}
    resubmit = message_out(message, [], wrong).review_action
    assert resubmit is not None and resubmit.action == "revise_resubmit"


# ---- what the vendor reads -------------------------------------------------------------------


def test_the_vendor_reads_under_review_never_approved() -> None:
    found = review_action([*_unproved(5), _right()])
    assert found.vendor_words == "Under review"
    assert found.vendor_reason == (
        "GV will confirm the remaining items before giving its review result."
    )
    assert found.next_step == NEXT_STEP["review_needed"]
    assert found.next_step.startswith("Under review: GV will confirm the remaining items")
    for words in (found.vendor_words, found.vendor_reason, found.next_step, found.required_by):
        assert _APPROVED.search(words) is None, words
    assert stamp_words(found, "2026-10-22", "7", 1) == (
        "GV REVIEW: UNDER REVIEW. Checked automatically by GV Review on 2026-10-22. "
        "Submittal 7, round 1."
    )
    # Every other result reads the same on the screen and to the vendor.
    other = review_action([_proved()])
    assert (other.vendor_words, other.vendor_reason) == (other.words, other.reason)
    shown = found.as_dict()
    assert shown["vendor_words"] == "Under review" and shown["words"] == "Review needed"
    assert (shown["not_compared"], shown["items"]) == (5, 6)


def test_the_register_says_under_review() -> None:
    findings = [*_unproved(5), _right()]
    letter = letter_of(findings, _context(), drawing_set="sheet.pdf")
    rows = {row[0]: row[1] for row in submittal_rows(letter)}
    assert rows["Review result"] == "Under review"
    assert rows["Why"] == "GV will confirm the remaining items before giving its review result."
    assert rows["Action required by"] == "GV: confirm the remaining items"
    assert rows["Stamp"].startswith("GV REVIEW: UNDER REVIEW.")


def test_the_package_letter_and_cover_say_under_review(
    workspace: ChatWorkspace,  # noqa: F811
) -> None:
    findings = [*_unproved(5), _right()]
    context = package_context(workspace, findings, IDENTITY, today="2026-10-22")
    assert context.action(findings).action == "review_needed"
    drawing = workspace.files / "drawing.pdf"
    made = make_proof_file(workspace, findings, drawing, context=context)
    out = workspace.files / "package.zip"
    make_package_file(workspace, findings, [(made.name, drawing)], out, context)
    with zipfile.ZipFile(out) as archive:
        letter = " ".join(_text(archive.read("sheet - Comment letter.pdf")).split())
        cloud = " ".join(_text(archive.read("sheet - GV cloud drawing.pdf")).split())
        register = archive.read("sheet - Issue register.xlsx")
    assert "Under review" in letter  # the letter's result box
    for words in (letter, cloud):
        assert "GV REVIEW: UNDER REVIEW." in words
        assert "Review needed" not in words
        assert "Approved as noted" not in words
        assert "GV will confirm the remaining items" in words
        assert "need a person" not in words  # GV's own line stays on GV's screen
        assert FORBIDDEN.search(words) is None, FORBIDDEN.search(words)
    assert "Under review: GV will confirm the remaining items" in letter  # the next step
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.load_workbook(io.BytesIO(register))
    first = {row[0]: row[1] for row in book.worksheets[0].iter_rows(values_only=True)}
    assert first["Review result"] == "Under review"


# ---- the summary's next step -------------------------------------------------------------------


def test_the_summary_says_a_person_decides_before_approval() -> None:
    text = code_summary(
        [*_unproved(5), _right()],
        coverage={"total": 3, "same": 3, "different": 0},
        page_count=3,
        products=["cabinet"],
        stopped=None,
    )
    assert "Next: a person decides the 5 Not compared items before this can be approved." in text
    approved = code_summary(
        [_unproved(1)[0], *_rights(9)],
        coverage={"total": 3, "same": 3, "different": 0},
        page_count=3,
        products=["cabinet"],
        stopped=None,
    )
    assert "Next: look over the report before signing off." in approved


def test_the_next_step_names_the_likely_problems_and_never_zero_items() -> None:
    """La Quinta re-run, 2026-10-29: 0 not compared and 63 likely problems said "a person decides
    the 0 Not compared items"."""
    likely = [
        {
            **_right(id=f"L{n}"),
            "status": "check",
            "whole_look": {
                "by": "page_reviewer",
                "proof": "likely_only",
                "kind": "construction",
                "why": "A support may be missing.",
            },
        }
        for n in range(5)
    ]
    assert {proof.proof_group(f) for f in likely} == {"likely_problem"}
    text = code_summary(
        [*likely, _right()],
        coverage={"total": 3, "same": 3, "different": 0},
        page_count=3,
        products=["cabinet"],
        stopped=None,
    )
    assert "Next: a person decides the 5 likely problems before this can be approved." in text
    both = code_summary(
        [*likely, *_unproved(2)],
        coverage={"total": 3, "same": 3, "different": 0},
        page_count=3,
        products=["cabinet"],
        stopped=None,
    )
    assert (
        "Next: a person decides the 2 Not compared items and the 5 likely problems before this "
        "can be approved." in both
    )
