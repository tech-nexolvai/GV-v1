"""The full check's helper brief and per-page checklist (#1515), all code, no AI call.

- the brief names each page with its area, its drawings and where its architect drawing is;
- GV's recorded results on the helper's pages are listed as "already recorded" but never close the
  area; the page review's `unsure` / `not_shown` rows come as leads;
- the brief carries the checklist and the exact report line code reads back;
- `checklist_gaps` finds a missing line, a missing count, and a short count with "none" left;
- the whole-set `review_results` keeps the page review's leads beside its `different` rows;
- SKILL.md says the rules the recall study asked for;
- the checklist and SKILL.md name the kinds of differences the helpers used to miss (#1543):
  hardware, construction notes, position, extent and count, ID set vs brand prototype, and scaled
  values (only ever "Your decision"), in generic words with no client value.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.review.assistant.full_check_brief import (
    CHECKLIST_FIELDS,
    PAGE_CHECKLIST,
    checklist_gaps,
    checklist_send_back,
    helper_brief,
    leads_on_pages,
    page_lines,
    recorded_on_pages,
)

SKILL = Path(__file__).parents[3] / "app/review/assistant/skills/gv-review/SKILL.md"

PAGE_MAP: dict[str, Any] = {
    "version": 1,
    "pages": [
        {
            "page": 7,
            "role": "architect+vendor",
            "area": "2. Coffee bar",
            "halves": [
                {"role": "architect", "round": None, "heading": "ID SET ELEVATION"},
                {"role": "vendor", "round": 1, "heading": "VENDOR'S SHOP DRAWING"},
            ],
        },
        {
            "page": 8,
            "role": "vendor",
            "area": None,
            "section": "2. Coffee bar",
            "area_from_page": 7,
            "halves": [{"role": "vendor", "round": 2, "heading": "VENDOR'S SHOP DRAWING"}],
        },
        {
            "page": 9,
            "role": "architect+brand_prototype",
            "area": "3. Front desk",
            "halves": [
                {"role": "architect", "round": None},
                {"role": "brand_prototype", "round": None},
            ],
        },
    ],
}

DOCUMENT: dict[str, Any] = {
    "records": {
        "countertops": [
            {
                "id": "C4",
                "page": 7,
                "label": "Countertop row on page 7",
                "outcome": "Needs correction",
                "needed_overall": "68 in",
                "printed_overall": "72 in",
            },
            {"id": "C5", "page": 20, "label": "Far away", "outcome": "Needs correction"},
        ],
        "other_checks": [],
    },
    "ai_page_review": [
        {"page": 7, "status": "done", "same": 2, "different": []},
        {
            "page": 8,
            "status": "done",
            "different": [
                {
                    "key": "air:a1",
                    "measures": "floor to outlet",
                    "architect_text": "3'-1\"",
                    "vendor_text": '46"',
                    "result": "different",
                    "verified_by": "evaluator",
                }
            ],
            "leads": [
                {
                    "key": "air:a2",
                    "measures": "wall to first outlet",
                    "architect_text": "1'-0\"",
                    "vendor_text": '7"',
                    "result": "unsure",
                },
                {
                    "key": "air:a3",
                    "measures": "outlet above the counter",
                    "architect_text": "1'-6\"",
                    "vendor_text": None,
                    "result": "not_shown",
                },
                {"key": "air:a4", "measures": "never a lead", "result": "same"},
            ],
        },
    ],
}


# ---- the page lines --------------------------------------------------------------------------------


def test_page_lines_name_the_area_the_drawings_and_the_architect_page() -> None:
    lines = page_lines([9, 8, 7, 8], PAGE_MAP)
    assert lines == [
        "- p7: 2. Coffee bar; architect + vendor round 1",
        "- p8: 2. Coffee bar; vendor round 2; the architect's drawing is on p7",
        "- p9: 3. Front desk; architect + brand prototype",
    ]


def test_a_page_the_map_does_not_know_is_still_listed() -> None:
    assert page_lines([4], PAGE_MAP) == ["- p4: not in the page map; look at it"]
    assert page_lines([4], None) == ["- p4: not in the page map; look at it"]
    # The page map's list alone is accepted too.
    assert page_lines([9], PAGE_MAP["pages"])[0].startswith("- p9: 3. Front desk")


# ---- recorded results and leads ------------------------------------------------------------------


def test_recorded_results_are_only_the_helpers_pages() -> None:
    lines = recorded_on_pages(DOCUMENT, [7, 8])
    assert lines == [
        "- p7: Countertop row on page 7 (architect 68 in; vendor 72 in) [C4]",
        '- p8: air:a1 (architect 3\'-1"; vendor 46") [ai-review:p8:air:a1]',
    ]
    assert recorded_on_pages(DOCUMENT, [9]) == []
    assert recorded_on_pages(None, [7]) == []


def test_leads_are_the_unsure_and_not_shown_rows_never_the_others() -> None:
    assert leads_on_pages(DOCUMENT, [8]) == [
        '- p8: unsure: wall to first outlet (architect 1\'-0"; vendor 7")',
        "- p8: not shown: outlet above the counter (architect 1'-6\")",
    ]
    assert leads_on_pages(DOCUMENT, [7, 9]) == []
    assert leads_on_pages(None, [8]) == []


def test_a_one_page_document_gives_its_leads_from_its_rows() -> None:
    one_page = {
        "ai_page_review": [
            {
                "page": 3,
                "rows": [
                    {"key": "air:b1", "measures": "sink centre", "result": "unsure"},
                    {"key": "air:b2", "measures": "depth", "result": "different"},
                ],
            }
        ]
    }
    assert leads_on_pages(one_page, [3]) == ["- p3: unsure: sink centre"]


# ---- the brief -----------------------------------------------------------------------------------


def test_the_brief_carries_pages_recorded_results_leads_and_the_checklist() -> None:
    brief = helper_brief(
        [8, 7],
        file_name="/mnt/user-data/uploads/set.pdf",
        page_map=PAGE_MAP,
        review_document=DOCUMENT,
    )
    assert "pages 7, 8 of /mnt/user-data/uploads/set.pdf" in brief
    assert "- p8: 2. Coffee bar; vendor round 2; the architect's drawing is on p7" in brief
    # Recorded: not reported again, but never closing the area.
    assert "[C4]" in brief and "[ai-review:p8:air:a1]" in brief
    assert "a recorded item never closes its area" in brief
    assert "- p8: unsure: wall to first outlet" in brief
    for name, text in PAGE_CHECKLIST:
        assert f"- {name}: {text}" in brief
    assert "each segment of a chain" in brief
    assert "CHECKLIST p<N>: architect a/b; notes a/b; strings a/b; not compared:" in brief
    assert "starting with its area" in brief
    assert "{" not in brief and "}" not in brief  # every slot filled


def test_the_brief_says_when_there_is_nothing_recorded_or_nothing_could_be_read() -> None:
    nothing = helper_brief([9], file_name="set.pdf", page_map=PAGE_MAP, review_document={})
    assert "its area):\n- none\n" in nothing
    unread = helper_brief([9], file_name="set.pdf", page_map=PAGE_MAP, review_document=None)
    assert "GV Review's results could not be read: compare everything" in unread
    with pytest.raises(ValueError):
        helper_brief([], file_name="set.pdf")


# ---- the checklist read back ---------------------------------------------------------------------

FULL = """Page 7: two differences (F3, F4).
CHECKLIST p7: architect 7/7; notes 3/3; strings 4/4; not compared: none
CHECKLIST p8: architect 5/6; notes 2/2; strings 3/3; not compared: the 1'-6" outlet is under a cloud on every copy
"""


def test_a_full_checklist_has_no_gaps() -> None:
    assert checklist_gaps(FULL, [7, 8]) == {}
    assert checklist_send_back({}) is None


def test_a_missing_line_a_missing_count_and_a_silent_skip_are_gaps() -> None:
    report = """CHECKLIST p7: architect 7/7; strings 4/4; not compared: none
CHECKLIST p.8: architect 5/6; notes 2/2; strings 3/3; not compared: none
CHECKLIST p9: architect 2/2; notes 1/1; strings 0/0
"""
    gaps = checklist_gaps(report, [7, 8, 9, 10])
    assert gaps == {
        7: "no count for notes",
        8: "architect not all done, and 'not compared' says none",
        9: "no 'not compared' part",
        10: "no CHECKLIST line",
    }
    note = checklist_send_back(gaps)
    assert note is not None
    assert "- p10: no CHECKLIST line" in note
    assert "segment by segment" in note


def test_the_counted_fields_are_the_first_three_checklist_items() -> None:
    assert CHECKLIST_FIELDS == tuple(name for name, _ in PAGE_CHECKLIST[:3])


# ---- review_results keeps the leads --------------------------------------------------------------


def _row(key: str, result: str) -> SimpleNamespace:
    data = {
        "key": key,
        "measures": key,
        "architect_text": "1'-0\"",
        "vendor_text": '7"',
        "result": result,
        "why": "",
        "status": "done",
        "clouded": result == "different",
        "verified_by": None,
        "evaluator_reason": None,
    }

    def dump(mode: str = "json", include: set[str] | None = None) -> dict[str, Any]:
        return {k: v for k, v in data.items() if include is None or k in include}

    return SimpleNamespace(result=result, model_dump=dump)


def test_the_whole_set_review_results_lists_the_leads(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import company_settings
    from app.api.review_assistant import ai_review_and_job_values
    from app.review import ai_review, review_items

    sheet = SimpleNamespace(
        page=5,
        status="done",
        rows=[
            _row("air:1", "different"),
            _row("air:2", "unsure"),
            _row("air:3", "not_shown"),
            _row("air:4", "same"),
        ],
    )
    monkeypatch.setattr(
        ai_review, "ai_review_for_revision", lambda _s, _r: SimpleNamespace(pages=[sheet])
    )
    monkeypatch.setattr(company_settings, "_project_set", lambda _s, _p: (1, {}, []))
    monkeypatch.setattr(review_items, "_job_value_suggestions", lambda _s, _r: [])

    whole = ai_review_and_job_values(None, uuid4(), uuid4(), None)  # type: ignore[arg-type]
    (page,) = whole["ai_page_review"]
    # The counts stay; the different rows and the leads are listed.
    assert page["same"] == 1 and page["unsure"] == 1 and page["not_shown"] == 1
    assert [row["key"] for row in page["different"]] == ["air:1"]
    assert [row["key"] for row in page["leads"]] == ["air:2", "air:3"]
    assert leads_on_pages(whole, [5]) == [
        '- p5: unsure: air:2 (architect 1\'-0"; vendor 7")',
        '- p5: not shown: air:3 (architect 1\'-0"; vendor 7")',
    ]

    one_page = ai_review_and_job_values(None, uuid4(), uuid4(), 5)  # type: ignore[arg-type]
    assert "leads" not in one_page["ai_page_review"][0]  # a page lists every row already


# ---- SKILL.md -------------------------------------------------------------------------------------


def test_skill_md_says_the_recall_rules() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 260
    for words in (
        "Every architect dimension is its own item",
        "segment by segment",
        "notes and callouts",
        "A vendor number under GV's markup",
        "a recorded item never closes",
        "CHECKLIST p<N>: architect a/b; notes a/b; strings a/b; not compared:",
        "starting with its area from the page map",
        "`unsure` or `not_shown` are leads",
        "**First** the gaps",
    ):
        assert words in text, words


# ---- the kinds of differences the helpers used to miss (#1543) ------------------------------------

#: Each new checklist item, with words its line must carry.
NEW_ITEMS: dict[str, tuple[str, ...]] = {
    "hardware": ("pulls (type, size, finish)", "cam lock", "grommets", "power and data units"),
    "construction": ("Construction notes", "brand sheet", "contradicts", "metal lining"),
    "position": ("centred on or aligned with", "collides with a neighbour", "its own opening"),
    "extent": ("vendor omits", "extra pieces", "doors, drawers, bins and fronts", "island faces"),
    "brand": ("ID set vs brand prototype", "Your decision", "which_governs"),
    "scaled": ("measure_on_drawing", "only ever status check", "never different (Wrong)"),
    # #1545: what a dimension spans, and the reasoning on every Your-decision item.
    "span": ("measure_on_drawing with `dimension`", "box, soffit", "different_extent", "printed"),
    "decision": ("decision_help", "what it measures", "best estimate", "what to confirm"),
}

#: Shapes of client values (the same as `test_skills_export.py`): a drawing or sheet number, the
#: name of a client drawing set, a job number.
CLIENT_VALUE_SHAPES = (
    re.compile(r"\b[A-Z]{1,3}-?\d{3}\b"),
    re.compile(r"\b[A-Z]{1,2}\d\.\d{2}\b"),
    re.compile(r"\bAI[ _]Set\b", re.IGNORECASE),
    re.compile(r"\bSet \d\b"),
    re.compile(r"\b(?:job|project|PO) ?(?:no\.?|number|#) ?\d", re.IGNORECASE),
)


def test_the_checklist_names_the_differences_helpers_used_to_miss() -> None:
    checklist = dict(PAGE_CHECKLIST)
    brief = helper_brief([7], file_name="set.pdf", page_map=PAGE_MAP, review_document={})
    for name, words in NEW_ITEMS.items():
        assert name in checklist, name
        assert f"- {name}: {checklist[name]}" in brief, name
        for word in words:
            assert word in checklist[name], (name, word)
    # The report line code reads back is unchanged: the new items are not counted fields.
    assert CHECKLIST_FIELDS == ("architect", "notes", "strings")
    assert [name for name, _ in PAGE_CHECKLIST][-1] == "rounds"


def test_the_brief_holds_no_client_value() -> None:
    brief = helper_brief([7, 8, 9], file_name="set.pdf", page_map=PAGE_MAP, review_document={})
    for shape in CLIENT_VALUE_SHAPES:
        assert not shape.search(brief), shape.search(brief)


def test_skill_md_says_what_to_compare_beyond_dimensions() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 260
    section = text[text.index("## 7. What to compare") : text.index("## 8. ")]
    for words in (
        "**Hardware and accessories**",
        "cam lock",
        "**Position, not only size**",
        "misses its opening",
        "**Extent and count**",
        "which way an island faces",
        "**Construction notes**",
        "metal lining",
        "**ID set vs brand prototype**",
        "`check_reason:\n  which_governs`",
        "**Scaled values**",
        '"about X (±T)": status `check` (Your decision), never `different` (Wrong)',
        "**Extent first**",
        "`check_reason: different_extent`",
        "**Printed vs printed is Wrong**",
    ):
        assert words in section, words
    for shape in CLIENT_VALUE_SHAPES:
        assert not shape.search(text), shape.search(text)
