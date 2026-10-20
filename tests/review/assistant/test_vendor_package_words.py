"""Every file the vendor's package holds speaks only the vendor's words (#1766), $0, made-up values.

The package is the cloud drawing, the comment letter, the issue register and the Not-compared PDF
and workbook. GV's own working words never reach them: AI model or provider names, ticket numbers,
"owner decision" notes, the old "vendor to confirm" sentences (replaced 2026-10-18 by the
Not-compared reasons), the reviewer's "Not a problem" as a fourth group (it is Right, noted
"(reviewer)"), and the old round-2 words ("Fixed / Still wrong / NEW": the letter's words are
Closed / Previous comment not addressed / New, everywhere).

The end-to-end test seeds a synthetic package with internal words in every field that reaches a
file, makes every file, and reads each one back.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from app.review.assistant import not_compared
from app.review.assistant.chat_findings import make_proof_file
from app.review.assistant.review_action import NOT_REVIEWED_NOTE
from app.review.assistant.round_check import RoundItem, RoundReport
from app.review.assistant.sandbox import ChatWorkspace
from app.review.assistant.send_package import (
    PackageContext,
    PackageIdentity,
    check_state,
    make_package_file,
    register_rows,
)
from app.review.assistant.vendor_text import internal, vendor_text
from tests.review.assistant.test_proof_drawing import (
    VENDOR_12,
    VENDOR_30,
    VENDOR_46,
    _ai_only,
    _box,
    _decision,
    _proved,
    _right,
    _sheet,
)
from tests.review.assistant.test_send_package import FORBIDDEN, _c10

IDENTITY = PackageIdentity(
    project="Test project",
    vendor="Test vendor",
    submittal=25,
    received="2026-10-01",
    products=("cabinet",),
)

#: Words that never reach any vendor file (case-insensitive).
BANNED = re.compile(
    FORBIDDEN.pattern
    + r"|\bclaude\b|\banthropic\b|\bopus\s*\d|\bsonnet\b|\bhaiku\b|openrouter|\bgpt-?\d|"
    r"\bgemini\b|deepseek|\bLLM\b|\(#\d+\)|#1\d{3}\b|\bPR\s*#\d|owner decision|"
    r"vendor to confirm|field verify (?:or|/) confirm|fixed in round|still wrong|"
    r"not a problem|your decision|your call|check report|devil'?s advocate|\bwitness|"
    r"\bp\d+\.D\d+\b",
    re.IGNORECASE,
)
#: The old round-2 heading, in capitals (case matters: "New" is the decided word).
OLD_NEW = re.compile(r"\bNEW\b")


def _found(text: str) -> list[str]:
    return [m.group(0) for m in BANNED.finditer(text)] + [
        m.group(0) for m in OLD_NEW.finditer(text)
    ]


# ---- the guard ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "words",
    [
        "Claude read it",
        "read by claude-opus-5-5",
        "Sonnet said so",
        "Opus 4 looked again",
        "the OpenRouter reader",
        "GPT-4o agrees",
        "Gemini reader",
        "DeepSeek's answer",
        "the LLM says",
        "kept for later (#1652)",
        "see PR #1622",
        "issue #1738 fixed it",
        "Right only (owner decision 2026-10-19)",
    ],
)
def test_the_guard_refuses_model_names_ticket_numbers_and_owner_notes(words: str) -> None:
    assert internal(words), words
    left = vendor_text(words, "")
    assert left is not None and not BANNED.search(left), left


@pytest.mark.parametrize(
    "words",
    ["Room #101 base cabinet", "Hinge #2", 'Opus faucet, 8" centres', "New sink base"],
)
def test_the_guard_keeps_the_vendors_own_words(words: str) -> None:
    """A room or hardware number, or a product's own name, is the vendor's: kept."""
    assert not internal(words), words
    assert vendor_text(words) == words


def test_a_ticket_number_in_brackets_is_cleaned_out_of_an_item_name() -> None:
    assert vendor_text("Toe kick height (#1652)") == "Toe kick height"
    assert vendor_text("Toe kick height (PR #1622, #1630)") == "Toe kick height"


# ---- one reason per item, and the decided words ----------------------------------------------


def _context(**changes: Any) -> PackageContext:
    given: dict[str, Any] = {
        "identity": IDENTITY,
        "date": "2026-10-18",
        "round_no": 1,
        "rounds": None,
        "state": check_state(incomplete=None, page_map=None, rounds=None),
        "labels": {"F1": "C-01"},
    }
    return PackageContext(**{**given, **changes})


def test_the_register_and_the_not_compared_list_give_the_same_reason() -> None:
    """The same "why" for an item in the register and the Not-compared list, including a GV
    rule result with no stated requirement (rule R6)."""
    findings = [
        _proved(),
        _ai_only(),
        _decision(),
        _decision(id="F7", item="Sink offset", keep_reason="measures_something_else"),
        _c10(),
    ]
    listed = {row.finding_id: row for row in not_compared.rows(findings)}
    assert set(listed) == {"F2", "F3", "F7", "F10"}
    rows = register_rows(findings, _context())
    by_item = {row[4]: row for row in rows}
    for finding in findings[1:]:
        row = by_item[vendor_text(finding["item"])]
        assert row[5] == "Not compared"
        assert row[11] == not_compared.vendor_reason(listed[str(finding["id"])]), row
    # The rule result names its rule and says why plainly, the same in both files.
    assert "GV's rule" in by_item["Countertop row on page 5"][11]


def test_no_vendor_to_confirm_sentence_of_gvs_own() -> None:
    rows = register_rows([_ai_only(), _decision()], _context())
    assert not any(
        re.search(r"vendor to confirm", cell, re.IGNORECASE) for row in rows for cell in row
    )
    assert not re.search(r"vendor to (?:field verify|confirm)", NOT_REVIEWED_NOTE, re.IGNORECASE)


def test_the_reviewers_not_a_problem_is_right_noted_reviewer() -> None:
    dismissed = _proved(decision={"decision": "not_a_problem", "by": "Ann", "note": "Fine"})
    rows = register_rows([dismissed], _context())
    assert rows[0][5] == "Right"
    assert rows[0][11] == "Right (reviewer): Fine"


# ---- round 2 ----------------------------------------------------------------------------------


def _rounds() -> RoundReport:
    spot46 = (VENDOR_46[0] - 2, VENDOR_46[1] - 10, VENDOR_46[0] + 22, VENDOR_46[1] + 3)
    spot12 = (VENDOR_12[0] - 2, VENDOR_12[1] - 10, VENDOR_12[0] + 22, VENDOR_12[1] + 3)
    spot30 = (VENDOR_30[0] - 2, VENDOR_30[1] - 10, VENDOR_30[0] + 22, VENDOR_30[1] + 3)
    return RoundReport(
        items=[
            RoundItem("F1", "fixed", "Buffet outlet height", '46"', '37"', '37"', 1, spot46, None),
            RoundItem(
                "F5", "still_wrong", "Drawer front width", '12"', '12"', '10"', 1, spot12, None
            ),
            RoundItem("F6", "new", "Panel width (#1652)", '30"', '30"', '24"', 1, spot30, None),
            RoundItem(
                "F9",
                "not_compared",
                "Claude's side panel",
                '9"',
                None,
                '8"',
                1,
                None,
                None,
                reason="the lead model could not line it up (#1603)",
            ),
        ]
    )


def test_the_round_two_line_says_the_letters_words() -> None:
    line = _rounds().line()
    assert line == (
        "Round 2: 1 closed, 1 previous comment not addressed, 1 new; "
        "1 could not be compared by code."
    )


# ---- every file of a synthetic package ---------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> ChatWorkspace:
    space = ChatWorkspace.open(tmp_path / "chat")
    _sheet(space.uploads / "sheet.pdf")
    (space.root / "uploads.json").write_text(
        json.dumps([{"name": "sheet.pdf", "kind": "the vendor's shop drawings", "pages": 1}]),
        encoding="utf-8",
    )
    return space


def _seeded() -> list[dict[str, Any]]:
    """A final list with GV's working words in every field that reaches a file."""
    return [
        _proved(item="Buffet outlet height (#1652)"),
        _proved(
            id="F5",
            item="Drawer front width",
            architect='10"',
            vendor='12"',
            inches={"architect": "10", "vendor": "12", "difference": "2"},
            vendor_box=_box(VENDOR_12),
            arch_box=None,
        ),
        _proved(
            id="F6",
            item="Panel width",
            architect='24"',
            vendor='30"',
            inches={"architect": "24", "vendor": "30", "difference": "6"},
            vendor_box=_box(VENDOR_30),
            arch_box=None,
        ),
        _ai_only(item="Claude's base cabinet height", why="the lead model read it (#1652)"),
        _decision(architect="no width printed, per the helper (PR #1622)"),
        _c10(),
        _right(architect='4" (owner decision 2026-10-19)'),
        _right(
            id="F8",
            item="Sonnet's toe kick",
            vendor='5"',
            architect='5"',
            decision={
                "decision": "not_a_problem",
                "by": "Ann",
                "note": "Claude was unsure; owner decision 2026-10-19",
            },
            status="different",
        ),
    ]


def _pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_no_banned_word_reaches_any_file_of_the_package(workspace: ChatWorkspace) -> None:
    findings = _seeded()
    rounds = _rounds()
    context = _context(
        round_no=2,
        rounds=rounds,
        state=check_state(incomplete=None, page_map=None, rounds=rounds),
        labels={"F1": "C-01", "F5": "C-02", "F6": "C-03"},
        areas={1: "Kitchen (#1234), read by Claude"},
    )
    rows = not_compared.rows(findings, areas={1: "Kitchen (#1234), read by Claude"})
    rows += not_compared.gap_rows([{"page": 1, "what": "Claude: finish codes", "why": "x"}])
    nc_pdf, nc_xlsx = workspace.files / "nc.pdf", workspace.files / "nc.xlsx"
    pdf_made, sheet_made = not_compared.make_files(
        workspace, rows, nc_pdf, nc_xlsx, today="2026-10-18"
    )
    drawing = workspace.files / "drawing.pdf"
    made = make_proof_file(
        workspace,
        findings,
        drawing,
        context=context,
        today="2026-10-18",
        not_compared=len([r for r in rows if r.level == "item"]),
    )
    out = workspace.files / "package.zip"
    make_package_file(
        workspace,
        findings,
        [(made.name, drawing), (pdf_made.name, nc_pdf), (sheet_made.name, nc_xlsx)],
        out,
        context,
    )
    texts: dict[str, str] = {}
    with zipfile.ZipFile(out) as archive:
        for name in archive.namelist():
            data = archive.read(name)
            if name.endswith(".pdf"):
                texts[name] = _pdf_text(data)
            elif name.endswith(".xlsx"):
                openpyxl = pytest.importorskip("openpyxl")
                book = openpyxl.load_workbook(io.BytesIO(data))
                texts[name] = "\n".join(
                    " | ".join(str(c) for c in line if c is not None)
                    for sheet in book.worksheets
                    for line in sheet.iter_rows(values_only=True)
                )
            else:
                texts[name] = data.decode("utf-8", "replace")
    assert len(texts) == 5, sorted(texts)
    for name, text in texts.items():
        assert text.strip(), name
        assert _found(text) == [], (name, _found(text))
    cloud = " ".join(texts["sheet - GV cloud drawing.pdf"].split())
    # Round 2 in the letter's words, on the sheets and the cover.
    assert "Closed in round 2" in cloud
    assert "Previous comment not addressed" in cloud
    assert "Round 2: 1 closed, 1 previous comment not addressed, 1 new" in cloud
    # The reviewer's Not a problem is Right, noted (reviewer): never a fourth group.
    assert "Right (reviewer)" in cloud and "Right (2)" in cloud
    letter = " ".join(texts["sheet - Comment letter.pdf"].split())
    assert "Not compared (3)" in letter
