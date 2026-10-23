"""A size the architect specifies that breaks a GV rule, printed the same by the vendor (#1839).

Decided, never red, never Not compared: its own group, "Breaks a GV rule (architect's size): ask
the architect", with the rule and GV's question for the architect, counted on every screen and file.
Made-up values only (no client data).
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

from app.review.assistant import chat_contract, classification, not_compared, proof, rule_notes
from app.review.assistant.chat_findings import make_proof_file
from app.review.assistant.chat_summary import code_summary
from app.review.assistant.review_action import RULE_NOTE_NOTE, review_action
from app.review.assistant.sandbox import ChatWorkspace
from app.review.assistant.send_package import (
    QUESTION_COLUMNS,
    QUESTION_SHEET,
    PackageIdentity,
    letter_of,
    make_package_file,
    package_context,
    questions_of,
    register_rows,
    register_status,
)
from tests.review.assistant.test_proof_drawing import VENDOR_12, _box, _proved, _right, _sheet

IDENTITY = PackageIdentity(
    project="Test project",
    vendor="Test vendor",
    submittal=7,
    received="2026-10-01",
    products=("cabinet",),
)


def _label(side: str, page: int, inches: str = "0.625", route: str = "text") -> dict[str, Any]:
    return {
        "id": "D3" if side == "vendor" else "D7",
        "page": page,
        "file": "sheet.pdf",
        "text": '5/8" FILLER',
        "inches": inches,
        "route": route,
        "side": side,
        "box": [400.0, 590.0, 424.0, 603.0],
    }


def _filler(**changes: Any) -> dict[str, Any]:
    """A filler the architect sizes at 5/8" (outside GV's 1"-2"), printed the same by the vendor:
    the filler range check's item, as stored."""
    finding: dict[str, Any] = {
        "id": "F9",
        "page": 1,
        "item": "Pantry: filler width",
        "category": "cabinet",
        "architect": '5/8" FILLER',
        "vendor": '5/8" FILLER',
        "status": "check",
        "source": "architect",
        "why": "The vendor prints the architect's filler size; the architect specifies 5/8\", "
        'outside GV\'s 1"-2" filler range.',
        "trust_level": "likely",
        "witnesses": ["printed"],
        "checks": {"printed_architect": "match", "printed_vendor": "match"},
        "vendor_box": _box(VENDOR_12),
        "arch_box": None,
        "dims": {"vendor": _label("vendor", 1), "architect": _label("architect", 1)},
        "created_by": "filler_range",
        "keep_reason": rule_notes.KEEP_REASON,
        "gv_note": 'the architect specifies 5/8", outside GV\'s 1"-2" filler range',
        "gv_standard": {
            "rule": "CAB-FILLER-001",
            "name": "filler width",
            "value": '1"-2"',
            "origin": "GV standard",
            "architect": '5/8"',
            "proved": True,
            "follows_architect": True,
        },
    }
    finding.update(changes)
    return finding


def _sink(**changes: Any) -> dict[str, Any]:
    """The architect prints a 6" sink front offset (GV's standard: 4"), the vendor prints 6"."""
    finding = _filler(
        id="F10",
        item="Kitchen: sink cut-out, front offset",
        category="countertop",
        architect='6"',
        vendor='6"',
        why='architect 6" vs GV standard 4" (sink front offset): reviewer decides. The vendor '
        "prints 6\"; code proved the dimension runs from the sink cut-out's front edge.",
        created_by="sink_offset",
        gv_note='architect 6" vs GV standard 4" (sink front offset): reviewer decides',
        dims={"vendor": _label("vendor", 1, "6"), "architect": _label("architect", 2, "6")},
        gv_standard={
            "rule": "CT-SINK-OFFSET-FRONT-001",
            "name": "sink front offset",
            "value": '4"',
            "origin": "project value",
            "architect": '6"',
            "proved": True,
            "follows_architect": True,
        },
    )
    finding.update(changes)
    return finding


# ---- what makes a rule note ------------------------------------------------------------------------


def test_each_gv_rule_path_makes_a_rule_note_with_its_question() -> None:
    filler = rule_notes.rule_note_of(_filler())
    assert filler is not None
    assert (filler.name, filler.settings, filler.origin, filler.architect) == (
        "filler width",
        '1"-2"',
        "GV standard",
        '5/8"',
    )
    assert filler.question == (
        "Architect: the filler width is specified at 5/8\" (Pantry, architect's page 1); "
        'GV\'s standard is 1"-2". Please confirm the size.'
    )
    sink = rule_notes.rule_note_of(_sink())
    assert sink is not None and sink.rule_id == "CT-SINK-OFFSET-FRONT-001"
    assert sink.standard == "this project's GV value is 4\""
    assert sink.question.startswith('Architect: the sink front offset is specified at 6" (Kitchen')


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "different"},  # a difference is the comparison's, never a note
        {"keep_reason": "architect_prints_nothing"},  # another reason
        {"dims": {"vendor": _label("vendor", 1)}},  # the architect's side was not read at a spot
        {"dims": {"vendor": _label("vendor", 1), "architect": _label("architect", 1, "0.75")}},
        {
            "dims": {
                "vendor": _label("vendor", 1),
                "architect": _label("architect", 1, route="drawn"),
            }
        },
        {"gv_standard": {**_filler()["gv_standard"], "proved": False}},
        {"gv_standard": {**_filler()["gv_standard"], "follows_architect": False}},
    ],
)
def test_anything_unsure_stays_not_compared(changes: dict[str, Any]) -> None:
    finding = _filler(**changes)
    assert rule_notes.rule_note_of(finding) is None
    assert proof.proof_group(finding) != "rule_note"


def test_an_item_stored_before_the_rule_note_record_reads_its_own_words() -> None:
    """A stored chat keeps its findings as they were (no `gv_standard`, no `gv_note`): the rule and
    its settings come from the item's own words, so an old check shows the new group on read."""
    old_filler = {k: v for k, v in _filler().items() if k not in ("gv_standard", "gv_note")}
    note = rule_notes.rule_note_of(old_filler)
    assert note is not None and note.settings == '1"-2"' and note.origin == "GV standard"
    old_sink = {k: v for k, v in _sink().items() if k not in ("gv_standard", "gv_note")}
    note = rule_notes.rule_note_of(old_sink)
    assert note is not None and (note.name, note.settings) == ("sink front offset", '4"')
    unknown = {**old_filler, "why": "the drawings do not settle it"}
    assert rule_notes.rule_note_of(unknown) is None


# ---- the group, the counts and the overrides ---------------------------------------------------


def test_a_rule_note_is_decided_and_counted_apart() -> None:
    findings = [_proved(), _filler(), _sink(), _right()]
    found = classification.classify(_filler(), proved=False)
    assert (found.outcome, found.reason) == ("rule_note", "architect_outside_gv_rule")
    assert proof.proof_counts(findings) == {
        "clouded": 1,
        "right": 1,
        "rule_note": 2,
        "likely_problem": 0,
        "not_compared": 0,
        "dismissed": 0,
    }
    shown = proof.proof_of(_filler())
    assert shown["group"] == "rule_note" and shown["should_be"] is None and shown["proof"] is None
    assert shown["not_compared"] is None and shown["settle"] is None
    assert shown["label"] == rule_notes.SHORT_WORDS
    # The screen's contract takes it as it is.
    assert chat_contract.ChatFindingProofOut(**shown).rule_note is not None
    # Not on the Not-compared list, and not in its summary line.
    assert not_compared.rows(findings) == []
    summary = code_summary(
        findings, coverage=None, page_count=1, products=("cabinet",), stopped=None
    )
    assert "1 wrong (proved by code), 1 right, 2 break a GV rule (ask the architect), 0 not" in (
        summary
    )
    assert "2 sizes the architect specifies break a GV rule" in summary
    assert "Every item could be compared." in summary


def test_the_reviewer_can_make_one_a_red_mark_or_a_right_and_undo_it() -> None:
    agreed = _filler(decision={"decision": "agree", "by": "R", "should_be": '1"'})
    assert proof.proof_group(agreed) == "clouded"
    assert proof.proof_of(agreed)["rule_note"] is not None  # what code found stays on the item
    right = _filler(decision={"decision": "not_a_problem", "by": "R"})
    assert proof.proof_group(right) == "dismissed"
    assert register_status(right) == "right"
    assert proof.proof_group(_filler(decision=None)) == "rule_note"


def test_the_review_result_notes_the_architect_questions() -> None:
    action = review_action([_filler(), _right()])
    assert (action.action, action.rule) == ("approved_as_noted", "S6")
    assert "1 size the architect specifies breaks a GV rule" in action.reason
    assert action.note == RULE_NOTE_NOTE


# ---- the package ------------------------------------------------------------------------------------


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
    return " ".join(" ".join(page.extract_text() or "" for page in reader.pages).split())


def test_the_package_asks_the_architect_and_never_the_vendor(workspace: ChatWorkspace) -> None:
    findings = [_proved(), _filler(), _right()]
    context = package_context(workspace, findings, IDENTITY, today="2026-10-21")
    drawing = workspace.files / "drawing.pdf"
    made = make_proof_file(workspace, findings, drawing, context=context, today="2026-10-21")
    out = workspace.files / "package.zip"
    make_package_file(workspace, findings, [(made.name, drawing)], out, context)
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        cloud = _text(archive.read(names[0]))
        letter = _text(archive.read(names[1]))
        register = archive.read(names[2])
    # The letter: one comment (the proved Wrong), the question section apart, no red words for it.
    assert "Comments (1)" in letter
    assert "Questions for the architect (GV rules) (1)" in letter
    assert "No change is asked of the vendor" in letter
    assert (
        'the architect specifies 5/8", as the vendor shows; GV rule: filler width 1"-2"' in letter
    )
    assert "Not compared (0)" in letter
    # The cloud drawing: one cloud only; the cover's fourth tile and the notes pages' section.
    assert "Clouded items (1)" in cloud
    assert "Ask the architect" in cloud
    assert "Questions for the architect (GV rules) (1)" in cloud
    # The register: its own status, no comment, no "should be", and the questions sheet.
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.load_workbook(io.BytesIO(register))
    rows = list(book["Comments"].iter_rows(values_only=True))
    statuses = [r[5] for r in rows[1:]]
    assert statuses == ["Wrong", rule_notes.SHORT_WORDS, "Right"]
    noted = rows[2]
    assert noted[0] in (None, "") and noted[7] in (None, "") and noted[9] in (None, "")
    assert noted[6] == "None (GV asks the architect)"
    assert "no change is asked of the vendor" in noted[11]
    sheet = list(book[QUESTION_SHEET].iter_rows(values_only=True))
    assert tuple(sheet[0]) == QUESTION_COLUMNS
    assert sheet[1][5] == '5/8"' and sheet[1][8] == '1"-2"'
    assert sheet[1][10] == (
        "Architect: the filler width is specified at 5/8\" (Pantry, architect's page 1); "
        'GV\'s standard is 1"-2". Please confirm the size.'
    )
    submittal = dict(book["Submittal"].iter_rows(min_row=2, values_only=True))
    assert submittal["Questions for the architect (GV rules)"] == "1"
    assert submittal["Not compared"] == "0"
    # Nothing internal reaches the vendor.
    for words in (letter, cloud, *(str(c) for r in sheet for c in r if c)):
        assert re.search(r"(?<![\w-])AI(?![\w-])|reviewer decides", words) is None


def test_register_rows_and_questions_from_the_same_list(workspace: ChatWorkspace) -> None:
    findings = [_filler(), _sink(page=1), _right()]
    context = package_context(workspace, findings, IDENTITY, today="2026-10-21")
    rows = register_rows(findings, context)
    assert [row[5] for row in rows] == [rule_notes.SHORT_WORDS, rule_notes.SHORT_WORDS, "Right"]
    asked = questions_of(findings)
    assert [q.rule for q in asked] == ["GV rule: sink front offset", "GV rule: filler width"]
    letter = letter_of(findings, context, drawing_set="sheet.pdf")
    assert len(letter.questions) == 2 and letter.not_compared == 0


def test_a_reviewers_red_mark_on_a_rule_note_remakes_the_package(workspace: ChatWorkspace) -> None:
    """The reviewer makes the rule note a red mark (#1803 overrides): the package made again holds
    it as a comment, and no longer asks the architect about it."""
    agreed = _filler(
        decision={"decision": "agree", "by": "R", "should_be": '1"', "note": "Too thin"}
    )
    findings = [_proved(), agreed, _right()]
    context = package_context(workspace, findings, IDENTITY, today="2026-10-21")
    drawing = workspace.files / "drawing.pdf"
    made = make_proof_file(workspace, findings, drawing, context=context, today="2026-10-21")
    out = workspace.files / "package.zip"
    make_package_file(workspace, findings, [(made.name, drawing)], out, context)
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        letter = _text(archive.read(names[1]))
        cloud = _text(archive.read(names[0]))
    assert "Comments (2)" in letter and "Questions for the architect" not in letter
    assert "Clouded items (2)" in cloud and "Ask the architect" not in cloud
    assert [q.finding_id for q in questions_of(findings)] == []
