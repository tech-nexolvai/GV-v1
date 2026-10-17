"""The plan check before a paid full check (#1569): code asks only where the page map is unsure.

Synthetic page maps (the set pack's `page_map.json` shape), no PDF, no AI call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.review.assistant.chat_tools import helper_footer
from app.review.assistant.full_check_brief import page_lines
from app.review.assistant.full_check_plan import plan_jobs, read_page_map
from app.review.assistant.plan_check import (
    ANSWERS_FILE,
    MAX_DOUBTS,
    SKIP_NOTE,
    answer_note,
    apply_answers,
    doubts,
    plan_summary,
    read_answers,
    record_answers,
    valid_answer,
)
from app.review.assistant.sandbox import ChatWorkspace

FILE = "Vendor set.pdf"
ARCH = "Architect.pdf"


def _half(role: str, y: float = 0, round_: int | None = None) -> dict[str, Any]:
    return {"box": [36, y, 576, y + 300], "role": role, "heading": None, "round": round_}


def _page(
    page: int, halves: list[dict[str, Any]], area: str | None = None, **more: Any
) -> dict[str, Any]:
    roles = sorted({h["role"] for h in halves} - {"general"})
    return {
        "file": FILE,
        "page": page,
        "role": "+".join(roles) if roles else "general",
        "halves": halves,
        "rounds": sorted({h["round"] for h in halves if h["round"]}),
        "section": "1. Kitchens",
        "area": area,
        "area_from_page": None,
        "sheet_numbers": [],
        "headings": [],
        **more,
    }


def _map(*pages: dict[str, Any], files: tuple[str, ...] = (FILE,)) -> dict[str, Any]:
    return {"version": 1, "files": list(files), "pages": list(pages)}


def _sure_map() -> dict[str, Any]:
    """Every sheet labelled, every vendor drawing paired with the architect's on its page."""
    return _map(
        _page(1, [], None, section=None),
        _page(2, [_half("architect"), _half("vendor", 320, 1)], "1. Buffet"),
        _page(3, [_half("architect"), _half("vendor", 320, 2)], "1. Buffet"),
        _page(4, [_half("architect"), _half("vendor", 320, 1)], "2. Bar"),
    )


def test_nothing_to_ask_when_the_page_map_is_sure() -> None:
    page_map = _sure_map()
    assert doubts(page_map, FILE) == []
    assert doubts(None, FILE) == [] and doubts({"pages": []}, FILE) == []
    assert plan_summary(page_map, FILE) == "4 pages · 3 pairs · rounds 1-2 · 1 general page"


def test_each_kind_of_doubt_on_a_synthetic_page_map() -> None:
    page_map = _map(
        # p2: a labelled vendor drawing and an unlabelled one above it.
        _page(2, [_half("general"), _half("vendor", 320, 1)], "1. Buffet"),
        _page(3, [_half("architect"), _half("vendor", 320, 1)], "1. Buffet"),
        # p5: the lower drawing headed as a brand prototype, and no architect in its area.
        _page(5, [_half("vendor", 0, 1), _half("brand_prototype", 320)], "2. Coffee point"),
        # p6-7: two rounds in the area, p7's drawing has no round.
        _page(6, [_half("architect"), _half("vendor", 320, 1)], "3. Bar"),
        _page(7, [_half("vendor", 0, 2), _half("vendor", 320, None)], "3. Bar"),
        # p9: a vendor drawing in an area no architect drawing is in.
        _page(9, [_half("vendor", 0, 1)], "4. Pantry"),
        # p11: drawings, no heading, no area, no section.
        _page(11, [_half("general")], None, section=None),
    )

    every = doubts(page_map, FILE, limit=None)

    assert [(d.kind, d.page) for d in every] == [
        ("prototype", 5),
        ("role", 2),
        ("pair", 9),
        ("round", 7),
        ("heading", 11),
    ]
    prototype, role, pair, rounds, heading = every
    assert prototype.id == "prototype:f0:p5:h1"
    assert prototype.question.startswith("Page 5: is the lower drawing the brand prototype or")
    assert [v for v, _label in prototype.options] == ["brand_prototype", "architect"]
    assert role.question == "Page 2: whose is the upper drawing? It has no heading."
    assert pair.options == (("pair:6", "Compare with page 6"), ("skip", "Skip it"))
    assert [label for _v, label in rounds.options] == ["Round 1", "Round 2"]
    assert heading.id == "heading:f0:p11" and heading.box == (36.0, 0.0, 576.0, 300.0)
    # The popover shows at most three, the most harmful first.
    assert [d.kind for d in doubts(page_map, FILE)] == ["prototype", "role", "pair"]
    assert len(doubts(page_map, FILE)) == MAX_DOUBTS
    # An answered question is not asked again.
    assert [d.kind for d in doubts(page_map, FILE, answered={prototype.id})][:1] == ["role"]


def test_another_upload_with_the_architects_drawing_of_the_area_settles_it() -> None:
    page_map = _map(
        _page(5, [_half("vendor", 0, 1), _half("brand_prototype", 320)], "2. Coffee point"),
        {**_page(1, [_half("architect")], "2. Coffee point"), "file": ARCH},
        files=(FILE, ARCH),
    )
    assert doubts(page_map, FILE) == []


def test_answers_are_kept_append_only_and_laid_over_the_page_map(tmp_path: Path) -> None:
    page_map = _map(
        _page(2, [_half("general"), _half("vendor", 320, 1)], "1. Buffet"),
        _page(3, [_half("architect"), _half("vendor", 320, 1)], "1. Buffet"),
        _page(5, [_half("vendor", 0, 1), _half("brand_prototype", 320)], "2. Coffee point"),
        _page(7, [_half("architect")], "3. Bar"),
        _page(9, [_half("vendor", 0, 1)], "4. Pantry"),
        _page(10, [_half("vendor", 0, 1)], "5. Store"),
    )
    (tmp_path / "page_map.json").write_text(json.dumps(page_map), encoding="utf-8")
    (tmp_path / "status.json").write_text(json.dumps({"state": "ready", "done": ["page_map"]}))
    kept = record_answers(
        tmp_path,
        {
            "prototype:f0:p5:h1": "architect",
            "role:f0:p2:h0": "brand_prototype",
            "pair:f0:p9": "pair:7",
            "pair:f0:p10": "skip",
            "role:f0:p2:h9": "nonsense",
        },
        "reviewer-1",
    )
    assert kept == 4
    record_answers(tmp_path, {"role:f0:p2:h0": "architect"}, "reviewer-2")
    rows = (tmp_path / ANSWERS_FILE).read_text(encoding="utf-8").splitlines()
    assert len(rows) == 5 and json.loads(rows[-1])["by"] == "reviewer-2"
    answers = read_answers(tmp_path)
    assert answers["role:f0:p2:h0"] == "architect"  # the newest answer counts

    read = read_page_map(tmp_path)

    assert read is not None
    by_page = {entry["page"]: entry for entry in read["pages"]}
    assert by_page[5]["halves"][1] == {
        **_half("brand_prototype", 320),
        "role": "architect",
        "heading_by": "reviewer",
    }
    assert by_page[5]["role"] == "architect+vendor"
    assert by_page[2]["halves"][0]["role"] == "architect"
    assert by_page[9]["pair_page"] == 7 and by_page[10]["skip"] == SKIP_NOTE
    # Nothing left to ask; the plan skips p10 and gives p9's helper p7 to look at.
    assert doubts(read, FILE, answers) == []
    plan = plan_jobs(read, FILE, parallel=8)
    assert plan is not None
    assert (10, SKIP_NOTE) in plan.skipped
    holder = next(job for job in plan.jobs if 9 in job.pages)
    assert (FILE, 7) in holder.reference or 7 in holder.pages
    # The helper's brief and footer show the reviewer's word.
    assert page_lines([5, 9], read) == [
        "- p5: 2. Coffee point; vendor round 1 + architect (the reviewer said so in the plan check)",
        "- p9: 4. Pantry; vendor round 1; the architect's drawing is on p7 (the reviewer said so in the plan check)",
    ]
    workspace = ChatWorkspace.open(tmp_path / "chat", set_pack=tmp_path)
    footer = helper_footer("brief", workspace, pages=[5])
    line = next(row for row in footer.splitlines() if row.startswith("- page 5 "))
    assert line.endswith("drawings: vendor round 1 at 36,0,576,300; architect at 36,320,576,620")
    # The page map on disk is never changed.
    assert json.loads((tmp_path / "page_map.json").read_text(encoding="utf-8")) == page_map


def test_answers_must_fit_their_question() -> None:
    assert valid_answer("role:f0:p2:h0", "vendor")
    assert valid_answer("prototype:f1:p28:h1", "brand_prototype")
    assert valid_answer("round:f0:p7:h1", "2")
    assert valid_answer("pair:f0:p9", "pair:7") and valid_answer("pair:f0:p9", "skip")
    assert valid_answer("heading:f0:p11", "skip")
    assert not valid_answer("role:f0:p2:h0", "pair:7")
    assert not valid_answer("round:f0:p7:h1", "0")
    assert not valid_answer("pair:f0:p9", "vendor")
    assert not valid_answer("../../etc", "skip")
    assert apply_answers(_sure_map(), {"role:f0:p99:h0": "vendor"}) == _sure_map()


def test_an_answer_reads_as_a_set_note() -> None:
    assert answer_note("prototype:f0:p28:h1", "architect") == (
        "Plan check: on page 28, drawing 2 is: architect's drawing.",
        28,
    )
    assert answer_note("pair:f0:p41", "pair:40") == (
        "Plan check: on page 41, compare it with page 40.",
        41,
    )
    assert answer_note("pair:f0:p41", "skip") == (
        "Plan check: on page 41, skip it in a full check.",
        41,
    )
    assert answer_note("round:f0:p7:h1", "2") == ("Plan check: on page 7, drawing 2 is round 2.", 7)
    assert answer_note("role:f0:p2:h0", "pair:3") is None
