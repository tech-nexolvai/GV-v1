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


#: Words that must never reach the vendor (#1622, `vendor_text`).
FORBIDDEN = re.compile(
    r"see GV Review'?s record|recorded check|rule_check_id|\bair:|\bhelpers?\b|\bthe lead\b|"
    r"check (?:it )?before sending|(?<![\w-])AI(?![\w-])",
    re.IGNORECASE,
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


def test_a_comment_number_names_one_item_across_checks() -> None:
    """The pipeline audit (2026-10-19): a layout finding of an early check, drawn round the whole
    elevation, joined every item on it, so one number named the fridge outlet in one check and
    the J-box in another. Each item keeps its own number; the layout keeps its own."""
    now = datetime(2026, 10, 18, tzinfo=UTC)
    whole = {"page": 23, "x0": 300.0, "y0": 50.0, "x1": 900.0, "y1": 400.0}

    def wrong(fid: str, item: str, spot: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": fid,
            "item": item,
            "page": 23,
            "status": "different",
            "round": 1,
            "vendor": '16"',
            "architect": '14"',
            "category": "cabinet",
            "source": "architect",
            "vendor_box": spot,
        }

    def spot(x: float) -> dict[str, Any]:
        return {"page": 23, "x0": x, "y0": 300.0, "x1": x + 30, "y1": 310.0}

    layout = _answer([wrong("L1", "Layout: drawers and uppers", whole)], now)
    first = _answer(
        [wrong("F1", "Fridge outlet to wall", spot(700)), wrong("F2", "Floor to J-box", spot(400))],
        now + timedelta(days=1),
    )
    second = _answer(
        [
            wrong("G1", "Fridge outlet CL to wall", spot(701)),
            wrong("G2", "J-box height", spot(402)),
        ],
        now + timedelta(days=2),
    )
    found = comment_numbers([layout, first, second])
    number = {
        fid: found[(str(a.message_id), fid)]
        for a, fid in ((layout, "L1"), (first, "F1"), (first, "F2"), (second, "G1"), (second, "G2"))
    }
    assert number["G1"] == number["F1"] and number["G2"] == number["F2"]
    assert len({number["L1"], number["F1"], number["F2"]}) == 3


def test_comment_labels_of_one_check_and_an_agreed_item() -> None:
    agreed = _decision(decision={"decision": "agree", "by": "R"})
    labels = comment_labels([_second(), _proved(), agreed])
    assert labels == {"F1": "C-01", "F5": "C-02", "F3": "C-03"}
    # Two comments the tracker links as one item (the same words) never share a number.
    twin = _second(id="F6", page=2)
    assert comment_labels([_second(), twin], {"F5": "C-03", "F6": "C-03"}) == {
        "F5": "C-03",
        "F6": "C-04",
    }
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
    assert rows[1][10] and not any(FORBIDDEN.search(cell) for row in rows for cell in row)


def _c10(**changes: Any) -> dict[str, Any]:
    """The C-10 case from the real run: a GV width rule result seeded with no needed overall."""
    given: dict[str, Any] = {
        "id": "F10",
        "page": 1,
        "item": "Countertop row on page 5",
        "category": "countertop",
        "architect": "see GV Review's record",
        "vendor": '38 1/4"',
        "source": "rules",
        "created_by": "gv_review",
        "rule_check_id": "C1",
        "record_id": "C1",
        "witnesses": ["rule"],
        "inches": {"architect": None, "vendor": "38.25", "difference": None},
        "vendor_box": _box(VENDOR_12),
        "arch_box": None,
        "critic": None,
    }
    return _proved(**{**given, **changes})


def test_a_gv_rule_result_without_a_stated_requirement_is_never_a_comment(
    workspace: ChatWorkspace,
) -> None:
    """C-10: "GV rule requires see GV Review's record" must never reach the vendor. With no
    requirement it can state, the item is Not compared ("GV rule result without a stated
    requirement") and logged; it is no comment on the letter, the register or the cloud drawing."""
    from app.review.assistant.proof import NO_REQUIREMENT_REASON, proof_group, proof_of

    c10 = _c10()
    assert proof_group(c10) == "not_compared"
    assert proof_of(c10)["reason"] == NO_REQUIREMENT_REASON
    # Even the reviewer's Agree cannot make it a comment with nothing to require.
    assert proof_group({**c10, "decision": {"decision": "agree", "by": "R"}}) != "clouded"
    findings = [_proved(), c10, _right()]
    assert review_action(findings).corrections == 1
    context = package_context(workspace, findings, IDENTITY, today="2026-10-18")
    drawing = workspace.files / "drawing.pdf"
    made = make_proof_file(workspace, findings, drawing, context=context)
    out = workspace.files / "package.zip"
    make_package_file(workspace, findings, [(made.name, drawing)], out, context)
    with zipfile.ZipFile(out) as archive:
        letter = _text(archive.read("sheet - Comment letter.pdf"))
        cloud = _text(archive.read("sheet - GV cloud drawing.pdf"))
        register = archive.read("sheet - Issue register.xlsx")
    for words in (letter, cloud.split("Notes")[0]):
        assert FORBIDDEN.search(words) is None, FORBIDDEN.search(words)
    assert "Comments (1)" in letter and "Countertop row on page 5" not in letter
    openpyxl = pytest.importorskip("openpyxl")
    rows = list(
        openpyxl.load_workbook(io.BytesIO(register))["Comments"].iter_rows(values_only=True)
    )
    row = next(r for r in rows if r[4] == "Countertop row on page 5")
    assert row[0] in (None, "") and row[5] == "Not compared"
    assert row[10] == NO_REQUIREMENT_REASON
    assert not any(FORBIDDEN.search(str(c or "")) for r in rows for c in r)


def test_a_gv_rule_states_its_requirement_from_its_own_comparison(tmp_path: Path) -> None:
    """The width rule's comparison gives the requirement with its computed value: the seeded
    finding says what it should be, and the letter prints it."""
    from app.review.assistant.chat_findings import FindingBook
    from app.review.assistant.chat_trust import recorded_from
    from app.review.assistant.send_package import requires_words
    from app.review.assistant.vendor_text import rule_requirement, vendor_text

    assert rule_requirement("|38.25 - 40.5| = 2.25 > 0 in") == '40 1/2"'
    assert rule_requirement("|72 - 68| = 4 > 1/8 in") == '68" (± 1/8")'
    assert rule_requirement("30 < 34 in") == 'at least 34"'
    assert rule_requirement("3 > 2 in") == 'at most 2"'
    assert rule_requirement("0.5 <= 3 <= 2 in is False") == 'between 1/2" and 2"'
    assert rule_requirement("count = 3 != 4") is None
    assert vendor_text("see GV Review's record") is None
    assert vendor_text("air:0000aaaa-1111-2222 Buffet top", "x") == "Buffet top"
    assert vendor_text("AI reading only", "fallback") == "fallback"
    assert vendor_text("AI_Set_3 sheet") == "AI_Set_3 sheet"
    assert vendor_text("GV's rule check C18 fails it") == "GV's rule check fails it"

    document = {
        "records": {
            "countertops": [
                {
                    "id": "C1",
                    "page": 2,
                    "label": "Countertop row on page 5",
                    "outcome": "Needs correction",
                    "printed_overall": '38 1/4"',
                    "rule": {"comparison": "|38.25 - 40.5| = 2.25 > 0 in"},
                }
            ]
        }
    }
    recorded = recorded_from(document)
    workspace = ChatWorkspace.open(tmp_path / "chat")
    _sheet(workspace.uploads / "sheet.pdf", pages=3)
    listing = [{"name": "sheet.pdf", "pages": 3, "kind": "the vendor's shop drawings"}]
    seeded = FindingBook(workspace=workspace, listing=listing, page_count=3).seed(
        recorded.differences[0]
    )
    assert seeded is not None and seeded["architect"] == '40 1/2"'
    proved = _c10(architect=seeded["architect"], inches={"architect": "40.5", "vendor": "38.25"})
    assert requires_words(proved) == 'GV rule requires 40 1/2"'


def _outlet(finding_id: str, **changes: Any) -> dict[str, Any]:
    """One of the triple from the fake-server run: the same outlet spacing, reported three times."""
    given: dict[str, Any] = {
        "id": finding_id,
        "page": 1,
        "item": "Outlet spacing",
        "architect": '8"',
        "vendor": "254 [10]",
        "inches": {"architect": "8", "vendor": "10", "difference": "2"},
        "vendor_box": _box(VENDOR_12),
        "arch_box": None,
        "dims": {"vendor": {"id": "D30", "page": 1}},
    }
    return _proved(**{**given, **changes})


def test_the_same_wrong_item_reported_three_times_is_one_comment() -> None:
    """The triple (C-04, C-73, C-74 in the fake-server run): folded into the first before numbering,
    so the letter, the register, the clouds and the counts list it once, with the lowest number."""
    from app.review.assistant.full_check_lenses import same_wrongs

    triple = [_outlet("F50"), _outlet("F81", trust_level="likely"), _outlet("F90")]
    other_value = _outlet("F91", inches={"architect": "8", "vendor": "12", "difference": "4"})
    other_spot = _outlet("F92", dims={"vendor": {"id": "D31", "page": 1}})
    recorded = _outlet("F93", record_id="c7")
    folds = same_wrongs([*triple, other_value, other_spot, recorded])
    # A recorded difference is never folded away: it is the one kept.
    assert {(f.keep, f.drop) for f in folds} == {("F93", "F50"), ("F93", "F81"), ("F93", "F90")}
    folds = same_wrongs([*triple, other_value, other_spot])
    assert {(f.keep, f.drop) for f in folds} == {("F50", "F81"), ("F50", "F90")}
    assert "the same item reported again" in folds[0].reason
    # Without code's label, an overlapping box is the same item only for the same words.
    unlabelled = {"dims": None}
    assert not same_wrongs(
        [_outlet("H1", **unlabelled), _outlet("H2", item="Outlet 2 height", **unlabelled)]
    )
    assert [
        f.drop for f in same_wrongs([_outlet("H1", **unlabelled), _outlet("H2", **unlabelled)])
    ] == ["H2"]
    # Another round of the same drawing is never the same comment.
    assert not same_wrongs([_outlet("F1", round=1), _outlet("F2", round=2)])

    # An earlier check (before this fold) numbered the triple C-01, C-02, C-03: the folded comment
    # of a later check keeps the lowest of them, and the letter lists it once.
    now = datetime(2026, 10, 18, tzinfo=UTC)
    earlier = _answer(triple, now)
    later = _answer([_outlet("G1")], now + timedelta(days=1))
    found = comment_numbers([earlier, later])
    assert sorted(found[(str(earlier.message_id), i)] for i in ("F50", "F81", "F90")) == [
        "C-01",
        "C-02",
        "C-03",
    ]
    assert found[(str(later.message_id), "G1")] == "C-01"
    kept = [f for f in triple if f["id"] not in {fold.drop for fold in folds}]
    context = PackageContext(
        identity=IDENTITY,
        date="2026-10-18",
        round_no=1,
        rounds=None,
        state=CheckState(),
        labels=comment_labels(kept, {"F50": "C-01"}),
    )
    letter = letter_of(kept, context, drawing_set="sheet.pdf")
    assert [(c.label, c.item) for c in letter.corrections] == [("C-01", "Outlet spacing")]
    assert review_action(kept).corrections == 1
