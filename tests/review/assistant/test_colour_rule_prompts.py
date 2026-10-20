"""Every prompt follows the colour rule (decided 2026-10-19; issue #1775, C2). $0, no model call.

Colour alone never makes ink GV's markup: only on GV's combined review sheets (drawings pasted as
snapshots under GV's headings) may coloured ink be GV's earlier markup; on an ordinary upload every
colour is the drawing's own. Code knows each file's kind and the simple check's prompts say it per
drawing; the other prompts state both kinds.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.review.assistant import (
    chat_tools,
    finding_critic,
    full_check_advocate,
    full_check_brief,
    full_check_roster,
    full_check_specialists,
    read_adjudication,
    second_read,
)
from app.review.assistant import agent as review_agent
from app.review.assistant.colour_rule import COLOUR_RULE
from app.review.assistant.simple_check import blind, prompts, resolve
from app.review.assistant.simple_check.schema import Drawing, Label, WorkItem
from app.review.assistant.simple_check.worklist import combined_files

#: Words that make every coloured mark GV's, whatever the file (the old rule).
OLD_RULE = re.compile(
    r"anything coloured (?:that GV|is GV|on a drawing is GV)"
    r"|any other colour is a reviewer"
    r"|coloured marks are a reviewer"
    r"|coloured ink inside a drawing \([^)]*\) is earlier review markup"
    r"|coloured notes drawn over a drawing are earlier review markup"
    r"|the drawings' own ink is black or grey"
    r"|read only the drawing's own black or grey ink"
    r"|printed in the drawing's own black or grey ink"
    r"|coloured text and callouts ending \"by GV\" are GV's notes"
    r"|GV's coloured markup"
    r"|a coloured value, or a callout ending \"by GV\", is GV's",
    re.IGNORECASE,
)


def _prompts() -> dict[str, str]:
    return {
        "compare system": prompts.system_prompt("- a rule", "Cabinets"),
        "blind system": blind.BLIND_SYSTEM,
        "resolve system": resolve.RESOLVE_SYSTEM,
        "advocate system": full_check_advocate.ADVOCATE_SYSTEM,
        "string system": full_check_advocate.STRING_SYSTEM,
        "critic": finding_critic._INSTRUCTIONS,
        "second read": second_read.question("upper height", ("architect", "vendor")),
        "lead": chat_tools.LEAD_SYSTEM_PROMPT,
        "helper": chat_tools.HELPER_SYSTEM_PROMPT,
        "drawn brief": full_check_brief.DRAWN_BRIEF,
        "specialists": full_check_specialists.HELPER_CORE,
        "reader pair adjudication": read_adjudication.question(
            "upper height", (("architect", False),)
        ),
        "tag brief": full_check_roster.TAG_BRIEF,
        "chat agent": review_agent.AGENT_SYSTEM_PROMPT,
        "chat subagent": review_agent.SUB_SYSTEM_PROMPT,
        "vendor mistakes": (
            Path(review_agent.__file__).parent / "skills/gv-review/knowledge/vendor-mistakes.md"
        ).read_text(encoding="utf-8"),
    }


@pytest.mark.parametrize("name", list(_prompts()))
def test_no_prompt_says_every_colour_is_gv_markup(name: str) -> None:
    text = " ".join(_prompts()[name].split())
    assert OLD_RULE.search(text) is None, f"{name}: {OLD_RULE.search(text)}"


@pytest.mark.parametrize("name", ["advocate system", "string system", "critic", "second read"])
def test_prompts_without_a_file_kind_state_both_kinds(name: str) -> None:
    assert COLOUR_RULE in _prompts()[name]


def _label(page: int, n: int, text: str) -> Label:
    return Label(
        ref=f"p{page}.D{n}",
        page=page,
        id=f"D{n}",
        text=text,
        inches=None,
        box=(0, 0, 1, 1),
        side="vendor" if page == 3 else "architect",
        orientation="h",
    )


def _drawing(ref: str, page: int, role: str, sheet: str | None) -> Drawing:
    return Drawing(
        ref=ref,
        file="set.pdf",
        page=page,
        half=0,
        role=role,  # type: ignore[arg-type]
        box=(0, 0, 100, 100),
        crop_box=(0, 0, 100, 100),
        labels=(_label(page, 1, '24"'),),
        sheet=sheet,  # type: ignore[arg-type]
    )


def _item(vendor_sheet: str | None, architect_sheet: str | None) -> WorkItem:
    return WorkItem(
        id="p3.v1",
        file="set.pdf",
        page=3,
        vendor=_drawing("p3.v1", 3, "vendor", vendor_sheet),
        architect=(_drawing("p2.a1", 2, "architect", architect_sheet),),
    )


def test_compare_text_names_each_drawings_file_kind() -> None:
    text = prompts.item_text(_item("ordinary", "combined"), [])
    vendor_line = next(line for line in text.splitlines() if line.startswith("Vendor drawing"))
    architect_line = next(line for line in text.splitlines() if line.startswith("Architect"))
    assert "an ordinary upload: every colour in it is the drawing's own" in vendor_line
    assert "GV's combined review sheet: coloured ink in it may be GV's markup" in architect_line


def test_compare_text_says_nothing_when_the_kind_is_not_known() -> None:
    text = prompts.item_text(_item(None, None), [])
    assert "ordinary upload" not in text and "combined review sheet" not in text
    # The system prompt's rule 6 then reads it as a combined sheet (the strict words).
    assert "kind not given: read it as a combined review sheet" in prompts.SYSTEM_PROMPT


@pytest.mark.parametrize("kind", ["pair", "chain", "values", "absence"])
def test_blind_question_names_the_file_kinds(kind: str) -> None:
    text = blind.question(kind, "p3.D1", "p2.D1", ("ordinary", "combined"))
    assert "The vendor's drawing is an ordinary upload" in text
    assert "the architect's drawing is on GV's combined review sheet" in text
    assert "vendor_ink_black" not in text
    assert "black or grey" not in blind.question(kind, "p3.D1", "p2.D1")


def test_blind_form_no_longer_asks_for_black_ink() -> None:
    assert "vendor_ink_black" not in blind.BLIND_SCHEMA["properties"]
    assert "vendor_ink_black" not in blind.BLIND_SCHEMA["required"]
    # An older saved look that has it still reads (a replay).
    answer = blind.answer_of({"pick": "D1", "verdict": "same_thing", "vendor_ink_black": "yes"})
    assert answer is not None and answer.pick == "D1"


def test_worklist_tells_combined_sheets_from_ordinary_uploads() -> None:
    page_map = {"files": ["combined.pdf", "plain.pdf"]}
    annotations = [
        {"file": "combined.pdf", "page": 1, "kind": "Stamp", "box": [0, 0, 10, 10]},
        {"file": "combined.pdf", "page": 1, "kind": "FreeText", "box": [0, 0, 1, 1]},
        {"file": "plain.pdf", "page": 1, "kind": "FreeText", "box": [0, 0, 1, 1]},
    ]
    assert combined_files(page_map, annotations) == {"combined.pdf": True, "plain.pdf": False}
    assert combined_files(page_map, None) is None


def test_a_rectangle_annotation_never_makes_a_file_ordinary() -> None:
    """Review of #1785: the label reader's own test (`gv_marks.gv_combined`) also counts a
    snapshot pasted as a `/Square` annotation with the drawing inside it, and the pack's list
    cannot tell that from a reviewer's rectangle. So a file with one is of unknown kind (the strict
    combined-sheet words), never an ordinary upload, where every colour would be read."""
    page_map = {"files": ["square.pdf", "both.pdf", "plain.pdf"]}
    annotations = [
        {"file": "square.pdf", "page": 1, "kind": "Square", "box": [0, 0, 10, 10]},
        {"file": "both.pdf", "page": 1, "kind": "Square", "box": [0, 0, 10, 10]},
        {"file": "both.pdf", "page": 2, "kind": "Stamp", "box": [0, 0, 10, 10]},
        {"file": "plain.pdf", "page": 1, "kind": "Ink", "box": [0, 0, 1, 1]},
    ]
    kinds = combined_files(page_map, annotations)
    assert kinds == {"both.pdf": True, "plain.pdf": False}
    assert "square.pdf" not in kinds  # unknown: the prompts use the strict words
