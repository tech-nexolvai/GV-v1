"""The send-ready package (#1622), $0: the review result, stable comment numbers, the letter, the
register and the zip, on synthetic PDFs.

- the review result: rules S0 to S7, first match wins, in AWI's words; "changes fabrication";
- comment numbers: stable across checks and rounds (the tracker's item identity), biggest first is
  the order, not the number; the cloud drawing, the letter and the register print the same ones;
- the rendered letter: identity, result, disclaimer, comments, round-2 words, no "AI";
- the register: every cell text, the vendor-response column, the submittal sheet;
- the zip: what it holds, the same bytes every time.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pypdf import PdfReader

from app.models import AssistantChatMessage
from app.review.assistant.chat_findings import make_proof_file
from app.review.assistant.chat_store import message_out
from app.review.assistant.comment_letter import comment_letter_pdf
from app.review.assistant.job_tracker import TrackerAnswer, comment_numbers
from app.review.assistant.review_action import (
    ACTION_WORDS,
    DISCLAIMER,
    MAX_NOTED,
    NOT_REVIEWED_NOTE,
    CheckState,
    changes_fabrication,
    item_action,
    review_action,
    stamp_words,
)
from app.review.assistant.round_check import RoundItem, RoundReport
from app.review.assistant.sandbox import ChatWorkspace
from app.review.assistant.send_package import (
    REGISTER_COLUMNS,
    Member,
    PackageContext,
    PackageIdentity,
    check_state,
    comment_labels,
    incomplete_reason,
    letter_of,
    make_package_file,
    package_context,
    package_zip,
    register_rows,
    register_xlsx,
    submittal_label,
)
from tests.review.assistant.test_proof_drawing import (
    VENDOR_12,
    _ai_only,
    _box,
    _decision,
    _proved,
    _right,
    _sheet,
)

IDENTITY = PackageIdentity(
    project="Test project",
    vendor="Test vendor",
    submittal=25,
    received="2026-10-01",
    products=("cabinet",),
)


def _minor(**changes: Any) -> dict[str, Any]:
    """A clouded Wrong that does not change fabrication: a filler a sixteenth off."""
    given: dict[str, Any] = {
        "id": "M1",
        "item": "Filler width",
        "architect": '1"',
        "vendor": '1 1/16"',
        "inches": {"architect": "1", "vendor": "1.0625", "difference": "0.0625"},
    }
    return _proved(**{**given, **changes})


def _second(**changes: Any) -> dict[str, Any]:
    """A second clouded Wrong, smaller than the outlet height, printed at VENDOR_12."""
    given: dict[str, Any] = {
        "id": "F5",
        "item": "Drawer front width",
        "architect": '10"',
        "vendor": '12"',
        "inches": {"architect": "10", "vendor": "12", "difference": "2"},
        "vendor_box": _box(VENDOR_12),
        "arch_box": None,
    }
    return _proved(**{**given, **changes})


# ---- the review result -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("findings", "state", "action", "rule"),
    [
        ([], CheckState(), "approved", "S7"),
        ([_right()], CheckState(), "approved", "S7"),
        # The architect gives no information: still Approved (the list is attached).
        ([_decision()], CheckState(), "approved", "S7"),
        # Something code could not prove or compare: Approved as noted, with the note.
        (
            [_decision(keep_reason="measures_something_else")],
            CheckState(),
            "approved_as_noted",
            "S6",
        ),
        ([_ai_only()], CheckState(), "approved_as_noted", "S6"),
        ([_minor()], CheckState(), "approved_as_noted", "S5"),
        ([_proved()], CheckState(), "revise_resubmit", "S4"),
        ([_minor(id=f"M{n}") for n in range(MAX_NOTED + 1)], CheckState(), "revise_resubmit", "S4"),
        ([_minor()], CheckState(still_open=1), "revise_resubmit", "S3"),
        ([_minor()], CheckState(wrong_product=True), "rejected", "S2"),
        ([_minor()], CheckState(missing_areas=2), "revise_resubmit", "S1"),
        (
            [_decision(id=f"U{n}", keep_reason="unreadable") for n in range(4)],
            CheckState(),
            "revise_resubmit",
            "S1",
        ),
        # An unfinished check is never stamped, whatever it found.
        (
            [_proved()],
            CheckState(incomplete="it stopped at its fixed price"),
            "not_for_sending",
            "S0",
        ),
        # The reviewer's overrides: not a problem drops a correction; agree makes one.
        (
            [_proved(decision={"decision": "not_a_problem", "by": "R"})],
            CheckState(),
            "approved",
            "S7",
        ),
        (
            [_ai_only(decision={"decision": "agree", "by": "R"})],
            CheckState(),
            "revise_resubmit",
            "S4",
        ),
    ],
)
def test_the_first_rule_that_matches_decides_the_result(
    findings: list[dict[str, Any]], state: CheckState, action: str, rule: str
) -> None:
    found = review_action(findings, state)
    assert (found.action, found.rule) == (action, rule)
    assert found.words == ACTION_WORDS[found.action]
    assert found.disclaimer == DISCLAIMER


def test_the_words_are_awis_four_and_no_stamp() -> None:
    assert set(ACTION_WORDS.values()) == {
        "Approved",
        "Approved as noted",
        "Revise and resubmit",
        "Rejected",
        "Check incomplete: not for sending",
    }
    noted = review_action([_ai_only()])
    assert noted.note == NOT_REVIEWED_NOTE
    assert noted.required_by == "Vendor: make the corrections noted"
    assert review_action([]).required_by == "No action required"
    assert not review_action([], CheckState(incomplete="x")).stamped
    assert review_action([_proved(), _minor()]).reason == (
        "1 of 2 corrections changes fabrication."
    )
    assert review_action([_minor()]).reason == (
        "1 correction, and it is minor (no change to fabrication)."
    )


@pytest.mark.parametrize(
    ("finding", "changes"),
    [
        (_proved(), True),  # a height
        (_minor(), False),  # a sixteenth on a filler
        (_minor(inches={"architect": "1", "vendor": "1.5", "difference": "0.5"}), True),
        # A size code could not work out the difference of is never assumed small.
        (_minor(inches={"architect": None, "vendor": None, "difference": None}), True),
        (_proved(item="Overall run length"), True),
        (_proved(item="Sink cut-out"), True),
        (_proved(item="Missing tall pantry cabinet", vendor="not shown"), True),
        (
            _proved(
                item="Panel label",
                inches={"architect": "1", "vendor": "1.25", "difference": "0.25"},
            ),
            False,
        ),
        # A wrong material or finish tag: Submit specified item, which changes fabrication.
        (_proved(item="Countertop finish tag", architect="ST-2", vendor="ST-3", inches=None), True),
    ],
)
def test_what_changes_fabrication(finding: dict[str, Any], changes: bool) -> None:
    assert changes_fabrication(finding) is changes


def test_item_actions() -> None:
    tag = _proved(item="Countertop finish tag", architect="ST-2", vendor="ST-3")
    assert item_action(tag) == "Submit specified item"
    assert item_action(_proved()) == "Correct as noted"


def test_stamp_line_submittal_and_unfinished_checks() -> None:
    action = review_action([_proved()])
    assert stamp_words(action, "2026-10-18", "25A", 2) == (
        "GV REVIEW: REVISE AND RESUBMIT. Checked automatically by GV Review on 2026-10-18. "
        "Submittal 25A, round 2."
    )
    assert submittal_label(25, 1) == "25"
    assert submittal_label(25, 2) == "25A"
    assert submittal_label(25, 3) == "25B"
    assert submittal_label(25, 28) == "25AA"
    assert submittal_label(None, 2) == ""
    assert incomplete_reason("cost_cap", None) == "it stopped at its fixed price"
    assert incomplete_reason(None, {"not_opened": 3}) == "3 pages were not reached"
    assert incomplete_reason(None, {"not_opened": 0}) is None


# ---- stable comment numbers ------------------------------------------------------------------


def _answer(findings: list[dict[str, Any]], at: datetime) -> TrackerAnswer:
    return TrackerAnswer(
        package_id=UUID(int=1),
        chat_id=UUID(int=2),
        message_id=uuid4(),
        at=at,
        kind="full_check",
        title="",
        stopped=None,
        findings=findings,
    )


def test_comment_numbers_stay_with_the_item_across_checks() -> None:
    """Round 1 numbers the outlet C-01 (biggest) and the drawer C-02; round 2 finds the drawer still
    wrong and a new, bigger item: the drawer keeps C-02 and the new one gets C-03, though it is
    listed first."""
    now = datetime(2026, 10, 18, tzinfo=UTC)
    first = _answer([_second(), _proved(), _right()], now)
    later_drawer = _second(id="G1", round=2)
    bigger = _proved(
        id="G2", item="Sink cut-out", inches={"architect": "30", "vendor": "36", "difference": "6"}
    )
    bigger["vendor_box"] = _box((100.0, 100.0))
    second = _answer([later_drawer, bigger], now + timedelta(days=7))
    found = comment_numbers([second, first])
    assert found[(str(first.message_id), "F1")] == "C-01"
    assert found[(str(first.message_id), "F5")] == "C-02"
    assert found[(str(second.message_id), "G1")] == "C-02"
    assert found[(str(second.message_id), "G2")] == "C-03"
    # A Right item never gets a number; the same answers give the same numbers.
    assert (str(first.message_id), "F4") not in found
    assert comment_numbers([first, second]) == found


def test_comment_labels_of_one_check_and_an_agreed_item() -> None:
    agreed = _decision(decision={"decision": "agree", "by": "R"})
    labels = comment_labels([_second(), _proved(), agreed])
    assert labels == {"F1": "C-01", "F5": "C-02", "F3": "C-03"}
    # Stable numbers given win; an agreed item with none takes the next free one.
    assert comment_labels([_proved(), agreed], {"F1": "C-07"}) == {"F1": "C-07", "F3": "C-08"}


# ---- the files, from one workspace ------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> ChatWorkspace:
    space = ChatWorkspace.open(tmp_path / "chat")
    _sheet(space.uploads / "sheet.pdf")
    (space.root / "uploads.json").write_text(
        json.dumps([{"name": "sheet.pdf", "kind": "the vendor's shop drawings", "pages": 1}]),
        encoding="utf-8",
    )
    return space


def _text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _labels_in(text: str) -> list[str]:
    return re.findall(r"C-\d{2,}", text)


def test_cloud_drawing_letter_and_register_print_the_same_comment_numbers(
    workspace: ChatWorkspace,
) -> None:
    findings = [_right(), _second(), _ai_only(), _proved()]
    context = package_context(
        workspace, findings, IDENTITY, numbers={"F1": "C-04", "F5": "C-09"}, today="2026-10-18"
    )
    drawing = workspace.files / "drawing.pdf"
    made = make_proof_file(workspace, findings, drawing, context=context, today="2026-10-18")
    out = workspace.files / "package.zip"
    package = make_package_file(workspace, findings, [(made.name, drawing)], out, context)
    assert package.name == "sheet - GV review package.zip"
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        assert names == [
            "sheet - GV cloud drawing.pdf",
            "sheet - Comment letter.pdf",
            "sheet - Issue register.xlsx",
        ]
        letter = _text(archive.read(names[1]))
        cloud = _text(archive.read(names[0]))
        register = archive.read(names[2])
    # Biggest first is the order (the outlet height, then the drawer); the numbers are stable.
    assert _labels_in(letter)[:2] == ["C-04", "C-09"]
    cover = _text(drawing.read_bytes()).split("Proof")[0]
    assert [n for n in _labels_in(cover) if n in ("C-04", "C-09")][:2] == ["C-04", "C-09"]
    assert "C-04" in cloud and "C-09" in cloud
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.load_workbook(io.BytesIO(register))
    comments = book["Comments"]
    rows = list(comments.iter_rows(values_only=True))
    assert tuple(rows[0]) == REGISTER_COLUMNS
    assert [r[0] for r in rows[1:3]] == ["C-04", "C-09"]
    assert [r[5] for r in rows[1:]] == ["Wrong", "Wrong", "Not compared", "Right"]
    assert all(r[-1] in (None, "") for r in rows[1:])  # the vendor's response, blank
    for line in comments.iter_rows(min_row=1):
        for cell in line:
            assert cell.number_format == "@"
            assert cell.value is None or isinstance(cell.value, str)
    submittal = dict(book["Submittal"].iter_rows(min_row=2, values_only=True))
    assert submittal["Submittal"] == "25"
    assert submittal["Review result"] == "Revise and resubmit"
    assert submittal["Disclaimer"] == DISCLAIMER
    # The cloud drawing's cover carries the same result, stamp and disclaimer.
    assert "GV REVIEW: REVISE AND RESUBMIT." in " ".join(cover.split())
    assert "Approval of an item is not approval of an assembly" in " ".join(cover.split())


def test_the_letter_reads_like_a_transmittal(workspace: ChatWorkspace) -> None:
    findings = [_proved(), _decision(keep_reason="measures_something_else"), _right()]
    context = package_context(workspace, findings, IDENTITY, today="2026-10-18")
    letter = letter_of(
        findings,
        context,
        drawing_set="sheet.pdf",
        reviewers=("Anant",),
        enclosures=["sheet - GV cloud drawing.pdf"],
    )
    out = workspace.files / "letter.pdf"
    comment_letter_pdf(letter, out)
    text = " ".join(_text(out.read_bytes()).split())
    for words in (
        "Submittal review comments",
        "Test project",
        "Test vendor",
        "Submittal 25",
        "Received 2026-10-01",
        "Reviewed 2026-10-18",
        "Checked automatically by GV Review, and reviewed by Anant",
        "Revise and resubmit",
        "Action required by: Vendor: revise and resubmit",
        "C-01",
        'Vendor shows 46"',
        'Architect requires 37"',
        "Correct as noted",
        "Source: architect",
        "Not compared (1)",
        "Approval of an item is not approval of an assembly it is part of.",
        "Enclosures",
    ):
        assert words in text, words
    assert re.search(r"\bAI\b", text) is None
    assert "Spec section" in text  # blank when unknown, but always there


def test_round_two_words_and_still_open_comments() -> None:
    fixed = RoundItem("F1", "fixed", "Buffet outlet height", '46"', '37"', '37"', 2, None, None)
    still = RoundItem("F5", "still_wrong", "Drawer front width", '12"', '12"', '10"', 2, None, None)
    rounds = RoundReport(items=[fixed, still])
    state = check_state(incomplete=None, page_map=None, rounds=rounds)
    assert state.still_open == 1
    assert state.closed == ("F1",)
    findings = [_proved(), _minor(id="F5")]
    context = PackageContext(
        identity=IDENTITY,
        date="2026-10-18",
        round_no=2,
        rounds=rounds,
        state=state,
        labels={"F1": "C-01", "F5": "C-02"},
    )
    assert context.submittal == "25A"
    letter = letter_of(findings, context, drawing_set="sheet.pdf")
    assert letter.action.rule == "S3"
    assert letter.previous_line == "1 closed, 1 not addressed"
    assert [(p.label, p.words) for p in letter.previous] == [
        ("C-01", "Closed"),
        ("C-02", "Previous comment not addressed"),
    ]
    # A closed comment is no longer a correction; the one still wrong says so.
    assert [(c.label, c.round_word) for c in letter.corrections] == [
        ("C-02", "Previous comment not addressed")
    ]
    rows = register_rows(findings, context)
    history = {row[0]: row[12] for row in rows}
    status = {row[0]: row[5] for row in rows}
    assert status == {"C-01": "Right", "C-02": "Wrong"}
    assert history["C-01"] == "Round 1: wrong · Round 2: Closed"
    assert history["C-02"] == "Round 1: wrong · Round 2: Previous comment not addressed"


def test_register_without_openpyxl_falls_back_and_zip_is_the_same_every_time(
    tmp_path: Path,
) -> None:
    a = tmp_path / "a.pdf"
    a.write_bytes(b"%PDF-1.4 one")
    first, second = tmp_path / "1.zip", tmp_path / "2.zip"
    members = [Member("x.pdf", a), Member("x.pdf", a)]
    assert package_zip(members, first, "2026-10-18") == package_zip(members, second, "2026-10-18")
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["x.pdf", "x (2).pdf"]


def test_register_xlsx_has_the_submittal_sheet_first() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    context = PackageContext(
        identity=IDENTITY,
        date="2026-10-18",
        round_no=1,
        rounds=None,
        state=CheckState(),
        labels={"F1": "C-01"},
    )
    findings = [_proved()]
    letter = letter_of(findings, context, drawing_set="sheet.pdf")
    book = openpyxl.load_workbook(
        io.BytesIO(register_xlsx(register_rows(findings, context), letter))
    )
    assert book.sheetnames == ["Submittal", "Comments"]


# ---- the screen's result ---------------------------------------------------------------------


def _message(blocks: list[dict[str, Any]]) -> AssistantChatMessage:
    return AssistantChatMessage(
        id=uuid4(),
        chat_id=uuid4(),
        seq=2,
        role="assistant",
        created_by="tester",
        text="",
        blocks=blocks,
        mode="full_check",
        created_at=datetime(2026, 10, 18, tzinfo=UTC),
    )


def test_the_screen_reads_the_result_once_the_package_is_made() -> None:
    findings = [{"type": "finding", "finding": _minor(verified=True, created_by="lead")}]
    assert message_out(_message(findings), []).review_action is None
    kept = {
        "type": "package",
        "package": {"state": CheckState(still_open=1).as_dict(), "numbers": {"M1": "C-01"}},
    }
    shown = message_out(_message([*findings, kept]), []).review_action
    assert shown is not None
    assert (shown.action, shown.rule) == ("revise_resubmit", "S3")
    assert shown.disclaimer == DISCLAIMER


def test_the_register_speaks_to_the_vendor() -> None:
    """Numbers only on comments, ranks in the register's order, the reviewer's notes reworded."""
    context = PackageContext(
        identity=IDENTITY,
        date="2026-10-18",
        round_no=1,
        rounds=None,
        state=CheckState(),
        labels={"F1": "C-01", "F2": "C-02"},
    )
    rows = register_rows([_ai_only(), _right(), _proved()], context)
    assert [(r[0], r[5], r[11]) for r in rows] == [
        ("C-01", "Wrong", "1"),
        ("", "Not compared", "2"),
        ("", "Right", ""),
    ]
    assert rows[1][10] == "Possible difference, not proved by GV's code: vendor to confirm."
    assert all("before sending" not in cell for row in rows for cell in row)


def test_a_rule_with_no_printable_value_names_the_check() -> None:
    from app.review.assistant.send_package import requires_words

    rule = _proved(
        source="rules",
        rule_check_id="C1",
        architect="see GV Review's record",
        inches={"architect": None, "vendor": "39.5", "difference": None},
    )
    assert requires_words(rule) == "Does not meet GV rule C1"
    assert requires_words(_proved()) == 'Architect requires 37"'
