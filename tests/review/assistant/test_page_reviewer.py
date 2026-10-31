"""The page reviewer (`page_reviewer`): its look, its findings and its money, with a scripted AI.

Made-up drawings and values only; no network and no paid call. The reviewer's loop runs on a
stand-in engine whose calls answer from a script (`_Script`), with GV's real zoom, text layer and
finding book; the roster test runs a whole full check with the reviewer switched on and off.

- The look: the text layer and the whole page come first; reports wait for the nine tiles; a
  coverage check sends it on while the page's text was not seen close up; the self-check needs a
  fresh tight zoom per issue.
- Reporting as it finds: each issue is in the finding book the moment it is reported.
- Money: its own pass, then the coverage check, then a final "report what you have" turn and the
  self-check, each within its share; the model is never told about money.
- Findings: construction, completeness, material, scope and label items are please-checks, never
  red; a string that does not add up takes the existing proof path (code adds it first); an item
  already recorded at the same spot is not recorded twice.
"""

from __future__ import annotations

import dataclasses
import io
import json
import re
import threading
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.review.assistant import likely_problems, page_reviewer, proof
from app.review.assistant.chat_engine import _Reply
from app.review.assistant.chat_findings import FindingBook
from app.review.assistant.chat_request import MODELS
from app.review.assistant.chat_tools import ChatTools
from app.review.assistant.page_reviewer import (
    COVERAGE_GOAL,
    MAX_NUDGES,
    MAX_TOKENS,
    MIN_REFLECT_TOKENS,
    Options,
    PageReview,
    Reviews,
    check_math,
    coverage_targets,
    dimension_strings,
    finish_facts,
    phrases,
    printed_sizes,
    review_money,
    system_prompt,
    tiles,
)
from app.review.assistant.sandbox import ChatWorkspace
from tests.review.assistant.chat_fakes import FakeSandbox

FILE = "Vendor set.pdf"
WIDTH, HEIGHT = 1224.0, 792.0
#: The made-up page: an elevation string (12 + 12 + 11 3/4 against a printed 36 1/4), a material
#: tag, a section title and a title-block cell, each at a known place (shown points, top-left).
TEXTS = [
    ('12"', 200.0, 300.0),
    ('12"', 260.0, 300.0),
    ('11 3/4"', 320.0, 300.0),
    ('36 1/4"', 260.0, 330.0),
    ("ST-01", 700.0, 420.0),
    ("SECTION A", 900.0, 200.0),
    ("ITEM 101", 1100.0, 760.0),
]


#: Page 1, another sheet of the same set: the top's tag there, a "who supplies it" note, a size.
OTHER_PAGE = [
    ("ST-02 Quartz top", 700.0, 420.0),
    ("BIN BY GC/OWNER", 300.0, 500.0),
    ('36"', 260.0, 330.0),
    ('40"', 260.0, 360.0),
]


def _pdf() -> bytes:
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(WIDTH, HEIGHT), invariant=1)
    for page in (1, 2):
        c.setFont("Helvetica", 9)
        if page == 2:
            for words, x, y in TEXTS:
                c.drawString(x, HEIGHT - y, words)
        else:  # the rest of the set: another sheet of the same job (`set_context`)
            for words, x, y in OTHER_PAGE:
                c.drawString(x, HEIGHT - y, words)
        c.line(100, 100, 400, 100)
        c.showPage()
    c.save()
    return out.getvalue()


# ---- a stand-in engine with a scripted AI ---------------------------------------------------------

#: Each call's worst case: a fixed part plus the output allowance at Opus's $20 a million.
_FIXED = 10_000
_PER_TOKEN = 20


@dataclasses.dataclass
class _Call:
    effort: str
    thinking: Mapping[str, Any]
    system: str
    tools: list[str]
    max_tokens: int
    messages: list[dict[str, Any]]
    context_management: Mapping[str, Any] | None = None


#: A scripted turn cut off at its output allowance in the middle of its thinking (unsigned: it
#: cannot go back into the conversation).
_CUT = "cut off"


class _Script:
    """Answers each call from a list of turns: a turn is a list of blocks (and a stop reason), a
    function of the conversation so far, or `_CUT`. `output`: the tokens each call writes, in its
    usage (0: not reported)."""

    def __init__(self, turns: Sequence[Any], cost: int = 100_000, output: int = 0) -> None:
        self.turns = list(turns)
        self.cost = cost
        self.output = output
        self.calls: list[_Call] = []

    def __call__(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        turn = self.turns.pop(0) if self.turns else [_text("Done.")]
        if turn == _CUT:
            return [{"type": "thinking", "thinking": "Looking at the"}], "max_tokens"
        if callable(turn):
            turn = turn(messages)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in turn) else "end_turn"
        return turn, stop


def _text(words: str) -> dict[str, Any]:
    return {"type": "text", "text": words}


_ids = iter(range(1, 100_000))


def _tool(name: str, **given: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"t{next(_ids)}", "name": name, "input": given}


class _Engine:
    """What the reviewer's loop uses of `ChatEngine`, with GV's real zoom and finding book."""

    def __init__(self, tmp_path: Path, script: _Script, cap: int = 50_000_000) -> None:
        self.workspace = ChatWorkspace.open(tmp_path / "chat")
        (self.workspace.uploads / FILE).write_bytes(_pdf())
        listing = [{"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"}]
        (self.workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
        self.tools = ChatTools(
            workspace=self.workspace,
            sandbox=FakeSandbox(self.workspace, lambda command, _w: ("ok", 0)),
            page_count=2,
            uploads=listing,
        )
        self.book = FindingBook(workspace=self.workspace, listing=listing, page_count=2)
        self.settings = SimpleNamespace(message_cap_micros=cap, page_reviewer=Options())
        self.provider = SimpleNamespace(name="anthropic")
        self.cancel = threading.Event()
        self.finding_lock = threading.RLock()
        self.script = script
        self.spent = 0
        self.own_spent = 0
        self.shown: list[dict[str, Any]] = []
        self.dropped: list[str] = []

    def _body(self, **given: Any) -> dict[str, Any]:
        return {k: v for k, v in given.items() if k not in ("model",)}

    def _estimate(self, model: Any, body: Mapping[str, Any], cached: int, previous: int) -> int:
        return _FIXED + int(body.get("max_tokens", 0)) * _PER_TOKEN

    def _cost(self, model: Any, usage: Mapping[str, Any]) -> int:
        return int(usage.get("cost_micros") or 0)

    def _call(self, **given: Any) -> _Reply:
        assert given["own_share"] is True
        messages = json.loads(json.dumps(given["messages"]))
        self.script.calls.append(
            _Call(
                effort=given["effort"],
                thinking=dict(given["thinking"]),
                system=given["system"],
                tools=[t["name"] for t in given["tools"]],
                max_tokens=given["max_tokens"],
                messages=messages,
                context_management=given.get("context_management"),
            )
        )
        content, stop = self.script(messages)
        usage: dict[str, Any] = {"cost_micros": self.script.cost}
        if stop == "max_tokens":  # it wrote its whole allowance: the worst case is paid
            usage = {"cost_micros": _FIXED + given["max_tokens"] * _PER_TOKEN}
            usage["output_tokens"] = given["max_tokens"]
        elif self.script.output:
            usage["output_tokens"] = self.script.output
        self.spent += usage["cost_micros"]
        self.own_spent += usage["cost_micros"]
        return _Reply(
            content=content,
            stop_reason=stop,
            stop_details=None,
            usage=usage,
            model="claude-opus-5-5",
        )

    def _show_finding(self, finding: dict[str, Any]) -> None:
        self.shown.append(finding)

    def _show_dropped(self, finding_id: str, reason: str) -> None:
        self.dropped.append(finding_id)


def _review(
    tmp_path: Path,
    turns: Sequence[Any],
    *,
    cap: int = 5_000_000,
    cost: int = 100_000,
    output: int = 0,
) -> tuple[_Engine, PageReview, tuple[str, bool, str | None], Reviews]:
    script = _Script(turns, cost, output)
    engine = _Engine(tmp_path, script)
    review = PageReview(page=2, file=FILE, options=Options())
    reviews = Reviews()
    agent = SimpleNamespace(model=MODELS["claude-opus-5-5"], cap_micros=cap, spent=0, last_call=0)
    run = page_reviewer._Run(engine, agent, review, reviews, {"steps": []}, 0)  # type: ignore[arg-type]
    ended = run.run()
    return engine, review, ended, reviews


def _tiles(*numbers: int) -> dict[str, Any]:
    return _tool("zoom", crops=[{"tile": n} for n in numbers])


def _tight_on_text() -> dict[str, Any]:
    """Tight zooms on every printed phrase of the made-up page."""
    crops = [
        {"box": {"x0": x - 20, "y0": y - 30, "x1": x + 120, "y1": y + 30}} for _w, x, y in TEXTS
    ]
    return _tool("zoom", crops=crops[:6])


def _tight_last() -> dict[str, Any]:
    _w, x, y = TEXTS[-1]
    return _tool("zoom", crops=[{"box": {"x0": x - 20, "y0": y - 30, "x1": x + 100, "y1": y + 30}}])


def _support_issue(**more: Any) -> dict[str, Any]:
    given = {
        "kind": "B",
        "claim": "please_check",
        "item": "Island top: overhang support",
        "wrong": "The top overhangs 14 in at the seating side; no support is shown.",
        "should_be": "a support under the overhang",
        "box": {"x0": 600, "y0": 380, "x1": 760, "y1": 440},
        "confidence": "medium",
        **more,
    }
    return _tool("report_issue", **given)


def _string_issue(parts: Sequence[str] = ('12"', '12"', '11 3/4"')) -> dict[str, Any]:
    return _tool(
        "report_issue",
        kind="A",
        claim="wrong",
        item="Elevation: overall width",
        wrong='The parts 12 + 12 + 11 3/4 add up to 35 3/4", the overall says 36 1/4".',
        should_be='35 3/4"',
        read='12" + 12" + 11 3/4" ; 36 1/4"',
        box={"x0": 190, "y0": 285, "x1": 370, "y1": 340},
        confidence="high",
        parts=list(parts),
        printed_total='36 1/4"',
    )


def _results(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every tool result's words in the conversation, in order."""
    out = []
    for message in messages:
        if message["role"] != "user" or not isinstance(message["content"], list):
            continue
        for block in message["content"]:
            if block.get("type") == "tool_result":
                out += [b.get("text", "") for b in block["content"] if b.get("type") == "text"]
    return out


def _notes(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """The text code wrote to the reviewer (not tool results)."""
    return [
        block["text"]
        for message in messages
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "text"
    ]


def _settle_all(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The self-check: a fresh tight zoom on each listed issue, then keep it."""
    note = _notes(messages)[-1]
    numbers = [int(line.split()[0][1:]) for line in note.splitlines() if line.startswith("#")]
    zoom = _tool(
        "zoom",
        crops=[
            {"box": {"x0": 600, "y0": 380, "x1": 760, "y1": 440}},
            {"box": {"x0": 190, "y0": 285, "x1": 370, "y1": 340}},
        ],
    )
    return [zoom] + [
        _tool("resolve_finding", n=n, decision="keep", why="read again") for n in numbers
    ]


# ---- pure code -----------------------------------------------------------------------------------


def test_bracketed_inches_of_a_millimetre_drawing_count_as_printed_sizes() -> None:
    # A text layer splits `5714 [225]` into `5714` and `[225]`, runs two together (`[2][3]`) and
    # reads a turned one backwards (`]4[`): each bracketed value is a printed size.
    found = [
        {"text": t, "box": [0, 0, 10, 5]} for t in ("5714", "[225]", "[2][3]", "201 ]4[", '24"')
    ]
    assert printed_sizes(found) == {Decimal(225), Decimal(2), Decimal(3), Decimal(4), Decimal(24)}


def test_dimension_strings_are_added_up_by_code_row_by_row() -> None:
    found = [
        {"text": "[11]", "box": [127, 204, 136, 210]},
        {"text": "[2]", "box": [147, 204, 153, 210]},
        {"text": "[225]", "box": [467, 204, 479, 210]},
        {"text": "[2][3]", "box": [796, 204, 809, 210]},
        {"text": "6147", "box": [457, 211, 468, 216]},  # millimetres alone: not read
        {"text": '24"', "box": [900, 400, 910, 404]},  # alone on its row: not a string
    ]
    (row,) = dimension_strings(found).splitlines()
    assert row.startswith("Row at 207") and row.endswith('= 243"')


def test_finish_facts_read_the_table_and_find_a_part_with_no_tag() -> None:
    def at(text: str, x: float, y: float, w: float = 30) -> dict[str, object]:
        return {"text": text, "box": [x, y, x + w, y + 8]}

    found = [
        at("WD-02", 950, 50), at("Brand - Oak woodgrain laminate", 990, 50, 150),
        at("WD-04", 950, 75), at("Brand - Slate Grey", 990, 75, 100),
        at("ST-03", 950, 100), at("White Quartz by GV", 990, 100, 150),
        at("COUNTER TOP & SPLASH", 218, 392, 90),  # no tag near: GV writes the tag in
        at("TOEKICK- WD-04", 415, 592, 64),
        at("TOP PLAN", 250, 280, 50),  # a view title, not a part
        at("WD-02", 260, 513), at("ST-03", 790, 384), at("ABC-801.C", 680, 746),  # item number
    ]  # fmt: skip
    facts = finish_facts(found)
    assert "WD-02: Brand - Oak woodgrain laminate [grain]" in facts
    assert "WD-04: Brand - Slate Grey [plain]" in facts
    assert "'COUNTER TOP & SPLASH': NONE" in facts
    assert "'TOEKICK- WD-04': WD-04" in facts
    assert "TOP PLAN" not in facts and "ABC-801" not in facts
    assert finish_facts([at('12"', 10, 10)]) == ""


def test_the_brief_is_short_names_every_kind_and_never_money() -> None:
    brief = system_prompt()
    # Short (the experiments: a short brief beat the long rulebook); 800 since the finish and
    # scope kinds were spelled out (2026-10-29), 850 with the sheet checklist line (2026-10-30).
    assert len(brief.split()) < 850
    for kind in ("kind A", "kind B", "kind C", "kind D", "kind E", "kind F"):
        assert kind in brief
    assert "budget" not in brief.lower() and "$" not in brief
    assert "house rules" not in brief.lower()
    with_rules = system_prompt("1. Grain arrows only on wood.")
    assert with_rules.startswith(brief) and "GV house rules" in with_rules
    assert "Grain arrows only on wood." in with_rules


def test_the_house_rules_file_is_read_after_its_marker(tmp_path: Path) -> None:
    rules = tmp_path / "rules.md"
    rules.write_text("# Notes for people\n<!-- rules -->\n1. Mark equal spaces EQ.\n")
    assert page_reviewer.house_rules_text(rules) == "1. Mark equal spaces EQ."
    assert page_reviewer.house_rules_text(None) is None
    assert page_reviewer.house_rules_text(tmp_path / "missing.md") is None


def test_nine_overlapping_tiles_cover_the_page() -> None:
    found = tiles(WIDTH, HEIGHT)
    assert len(found) == 9
    assert found[0][:2] == (0.0, 0.0) and found[-1][2:] == (WIDTH, HEIGHT)
    # Each reaches a fifth of a cell into its neighbours (2026-11-01, M8c; was 8%).
    cell = WIDTH / 3
    assert found[0][2] == pytest.approx(cell * 1.2)
    assert found[4][0] == pytest.approx(cell * 0.8) and found[4][2] == pytest.approx(cell * 2.2)
    assert page_reviewer.tile_seen(found[4], [found[4]])
    assert not page_reviewer.tile_seen(found[4], [found[0]])


def test_phrases_join_words_on_a_line_and_targets_leave_long_notes_out() -> None:
    words = [
        {"text": "SECTION", "box": [10, 10, 40, 18]},
        {"text": "A", "box": [42, 10, 48, 18]},
        {"text": '12"', "box": [200, 10, 214, 18]},
        *(
            {"text": f"w{n}", "box": [300 + 12 * n, 50, 310 + 12 * n, 58]}
            for n in range(12)  # a long note
        ),
    ]
    found = phrases(words)
    assert [p["text"] for p in found][:2] == ["SECTION A", '12"']
    # A view title is not a target since 2026-11-01 (M8a); the long note never was.
    assert [p["text"] for p in coverage_targets(found)] == ['12"']


def test_coverage_targets_are_sizes_tags_and_part_callouts_only() -> None:
    """M8a (2026-11-01): the coverage nudges chased title-block cells, view titles and notes with
    300-point zooms; only a phrase with a digit, a tag or a part word is a target now."""

    def at(text: str, n: int = 1) -> dict[str, Any]:
        return {"text": text, "box": [10, 10, 60, 18], "n": n}

    found = [
        at('24 3/4"'),  # a size
        at("QZ-01"),  # a finish tag
        at("TOE KICK", 2),  # a part callout
        at("ITEM 101", 2),  # a digit
        at("ABC-801 ELEVATION", 2),  # a view title that carries the item's tag
        at("SECTION A", 2),  # a view title
        at("COUNTERTOP PLAN", 2),  # a view title with a part word
        at("SCALE: 1/2 = 1'-0", 4),  # a title-block cell
        at("SHEET 3 OF 9", 4),  # a title-block cell
        at("10/12/2026"),  # a date
        at("VERIFY IN FIELD", 3),  # a note
        at("PROVIDED BY OTHERS", 3),  # a note
    ]
    assert [p["text"] for p in coverage_targets(found)] == [
        '24 3/4"',
        "QZ-01",
        "TOE KICK",
        "ITEM 101",
        "ABC-801 ELEVATION",
    ]


def test_check_math_adds_exactly_in_inches_and_millimetres() -> None:
    added = check_math({"values": ['12"', '12"', '11 3/4"'], "target": '36 1/4"'})
    assert 'sum = 35 3/4"' in added
    assert 'sum minus target = -1/2"' in added
    assert check_math({"values": ["304.8mm"]}).splitlines()[0] == '304.8mm = 12" = 304.8 mm'
    assert '= 12"' in check_math({"values": ["304.8"], "unit": "mm"})
    with pytest.raises(ValueError):
        check_math({"values": ["two by four"]})


def test_line_listing_names_each_piece_that_meets_the_area_longest_first() -> None:
    pieces = [
        ((100.0, 300.0), (400.0, 300.0)),
        ((150.0, 290.0), (150.0, 310.0)),
        ((900.0, 50.0), (950.0, 50.0)),
    ]
    listed = page_reviewer.line_listing(pieces, (90.0, 280.0, 420.0, 320.0))
    assert listed.splitlines()[0] == "2 line pieces (ends in page points):"
    assert listed.splitlines()[1] == "100.0,300.0 to 400.0,300.0  horizontal  300.0 pt"
    assert listed.splitlines()[2] == "150.0,290.0 to 150.0,310.0  vertical  20.0 pt"
    assert (
        page_reviewer.line_listing(pieces, (0.0, 600.0, 10.0, 610.0))
        == "No line work in that area."
    )


def test_the_money_is_the_rate_per_page_within_what_the_budget_leaves() -> None:
    options = Options(per_page_micros=900_000)
    assert review_money(options, 4, 10_000_000) == 3_600_000
    assert review_money(options, 4, 1_000_000) == 1_000_000
    assert review_money(None, 4, 10_000_000) == 0
    assert review_money(options, 0, 10_000_000) == 0


# ---- the look, in code ------------------------------------------------------------------------------


def test_the_first_message_has_the_text_the_whole_page_and_the_nine_tiles(tmp_path: Path) -> None:
    """M8c (2026-11-01): code draws the nine tiles into the first message and counts them as
    zoomed, so the reviewer may report on its first call (before, it spent two calls on them)."""
    engine, review, ended, reviews = _review(
        tmp_path,
        [
            [_support_issue()],  # at once: the tiles were in its first message
            [_tight_on_text(), _tight_last()],
            [_text("Checked the elevation, the section and the title block.")],
            _settle_all,
            [_text("All settled.")],
        ],
    )
    first = engine.script.calls[0].messages[0]["content"]
    assert first[0]["type"] == "text" and "text layer" in first[0]["text"]
    assert first[1]["type"] == "text" and first[2]["type"] == "image"  # then the whole page
    assert "The page in nine tiles" in first[3]["text"]
    labels = [b["text"] for b in first[4:22] if b["type"] == "text"]
    assert [label.split(" (")[1].split(")")[0] for label in labels] == [
        f"tile {n}" for n in range(1, 10)
    ]
    assert sum(1 for b in first[4:22] if b["type"] == "image") == 9
    assert first[-1]["text"].startswith("The nine tiles are above")
    assert not _results(engine.script.calls[1].messages)[-1].startswith("Refused")
    assert [d.kind for d in review.drafts] == ["B"]
    assert review.tiles_in_opening == 9
    assert review.counts.get("zoom") == 2  # only its two tight zooms: no call asked for a tile
    assert "first message count as zoomed" in page_reviewer.REPORT_ISSUE["description"]
    assert "first message has the nine tiles" in system_prompt()
    reviews.pages["p2"] = review
    (row,) = reviews.block()["pages"]
    assert row["tiles_sent_in_opening"] == 9 and row["tiles_left"] == []
    assert ended[2] is None and ended[1] is True


def test_the_first_message_gives_code_s_title_block_cells_and_the_log_keeps_them(
    tmp_path: Path,
) -> None:
    engine, review, _ended, reviews = _review(tmp_path, [[_text("Checked the page.")]])
    first = engine.script.calls[0].messages[0]["content"]
    texts = [b["text"] for b in first if b["type"] == "text"]
    (cells,) = [t for t in texts if t.startswith("Title block cells code read")]
    assert "Cells: ITEM: 101" in cells and "never red" in cells
    assert not any(t.startswith("Filler facts") for t in texts)  # no run with an end on this page
    reviews.pages["p2"] = review
    (row,) = reviews.block()["pages"]
    assert row["title_cells"] == {"ITEM": "101"}
    assert row["title_mismatches"] == [] and row["filler_facts"] == []


def test_without_tiles_in_the_first_message_reports_wait_for_the_nine_tiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(page_reviewer, "TILES_IN_OPENING", False)
    engine, review, ended, _ = _review(
        tmp_path,
        [
            [_support_issue()],  # too early: no tile zoomed yet
            [_tiles(1, 2, 3, 4, 5, 6)],
            [_tiles(7, 8, 9), _support_issue()],
            [_tight_on_text(), _tight_last()],
            [_text("Checked the elevation, the section and the title block.")],
            _settle_all,
            [_text("All settled.")],
        ],
    )
    first = engine.script.calls[0].messages[0]["content"]
    assert first[0]["type"] == "text" and "text layer" in first[0]["text"]
    assert '36 1/4"' in first[0]["text"]  # the page's own text, with its boxes
    assert first[1]["type"] == "text" and first[2]["type"] == "image"  # then the whole page
    assert first[3]["type"] == "text" and "The page in nine tiles" not in first[3]["text"]
    results = _results(engine.script.calls[1].messages)
    assert results[-1].startswith("Refused: not yet: first zoom on tiles [1, 2, 3, 4, 5, 6, 7")
    # Said up front, so a reviewer does not write its issues twice (2026-10-31: about 40 refused
    # reports on one 18-page check).
    assert "Issues are taken once all nine tiles were zoomed." in first[-1]["text"]
    assert review.tiles_in_opening == 0
    assert not review.tiles_left()
    assert [d.kind for d in review.drafts] == ["B"]
    assert ended[2] is None and ended[1] is True


def test_each_issue_is_in_the_book_as_soon_as_it_is_reported(tmp_path: Path) -> None:
    seen: list[int] = []

    def look(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # The call after the report: the item is recorded already, before the review ends.
        seen.append(
            len([f for f in engine_box[0].book.live() if f["created_by"] == "page_reviewer"])
        )
        return [_tight_on_text(), _tight_last()]

    engine_box: list[Any] = []
    original = _Engine.__init__

    def keep(self: _Engine, *a: Any, **k: Any) -> None:
        original(self, *a, **k)
        engine_box.append(self)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_Engine, "__init__", keep)
        _review(
            tmp_path,
            [
                [_tiles(1, 2, 3, 4, 5, 6)],
                [_tiles(7, 8, 9), _support_issue()],
                look,
                [_text("Done.")],
                _settle_all,
                [_text("Settled.")],
            ],
        )
    assert seen == [1]


def test_the_coverage_check_sends_it_on_at_most_three_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A sheet whose tiles are drawn below the close-look dpi (an ARCH D sheet's are ~114 dpi).
    monkeypatch.setattr(page_reviewer, "CLOSE_DPI", 400)
    turns: list[Any] = [[_tiles(1, 2, 3, 4, 5, 6)], [_tiles(7, 8, 9)]]
    turns += [[_text("I am done.")]] * (MAX_NUDGES + 1)
    engine, review, ended, _ = _review(tmp_path, turns)
    nudges = [n for n in _notes(engine.script.calls[-1].messages) if n.startswith("Only ")]
    assert len(nudges) == MAX_NUDGES
    assert "were seen close up" in nudges[0] and "ST-01 at" in nudges[0]
    assert "SECTION A at" not in nudges[0]  # a view title is not a target
    assert review.coverage() < COVERAGE_GOAL
    assert len(engine.script.calls) == 2 + MAX_NUDGES + 1
    assert ended[2] is None  # nothing reported: no self-check, it ends


def test_a_crop_drawn_at_200_dpi_or_more_is_a_close_look_whatever_its_box() -> None:
    """M8b (2026-11-01): on A3 a tile is drawn at about 200-250 dpi, where 6-point text reads; on
    an ARCH D sheet the tiles are about 114 dpi and the 320-point rule still holds."""
    close_look = page_reviewer.close_look
    assert close_look({"box": (0, 0, 560, 400), "dpi": 250})  # an A3 tile
    assert close_look({"box": (0, 0, 560, 400), "dpi": 200})
    assert not close_look({"box": (0, 0, 1000, 700), "dpi": 114})  # an ARCH D tile
    assert close_look({"box": (0, 0, 300, 200), "dpi": 114})  # tight: always
    assert not page_reviewer.is_tight((0, 0, 560, 400))  # the self-check's test is unchanged
    review = PageReview(page=2, file=FILE, options=Options())
    review.targets = [{"text": '12"', "box": [200, 300, 214, 308], "n": 1}]
    review.zooms = [{"box": (0.0, 0.0, 560.0, 400.0), "phase": "review", "at": 0, "dpi": 114}]
    assert review.coverage() == 0.0
    review.zooms[0]["dpi"] = 250
    assert review.coverage() == 1.0


def test_sharp_tiles_in_the_first_message_meet_the_coverage_goal_at_once(tmp_path: Path) -> None:
    """This made-up 17 x 11 in sheet's tiles are drawn at 200 dpi or more: the reviewer that
    finds nothing finishes on its first call, with no coverage nudge and no tile zoom."""
    engine, review, ended, _ = _review(tmp_path, [[_text("Nothing to report on this page.")]])
    assert min(z["dpi"] for z in review.zooms) >= page_reviewer.CLOSE_DPI
    assert review.coverage() >= COVERAGE_GOAL and not review.tiles_left()
    assert len(engine.script.calls) == 1 and ended[2] is None


def test_tight_zooms_reach_the_coverage_goal_and_no_nudge_is_sent(tmp_path: Path) -> None:
    engine, review, _ended, _ = _review(
        tmp_path,
        [
            [_tiles(1, 2, 3, 4, 5, 6)],
            [_tiles(7, 8, 9)],
            [_tight_on_text(), _tight_last()],
            [_text("Nothing to report on this page.")],
        ],
    )
    assert review.coverage() >= COVERAGE_GOAL
    assert not [n for n in _notes(engine.script.calls[-1].messages) if n.startswith("Only ")]
    assert len(engine.script.calls) == 4


def test_the_self_check_needs_a_fresh_tight_zoom_and_can_drop_or_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(page_reviewer, "MAX_SELFCHECK_ZOOMS", 0)  # no pictures by code here

    def early(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [_tool("resolve_finding", n=1, decision="keep", why="sure")]

    def settle(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            _tool("zoom", crops=[{"box": {"x0": 600, "y0": 380, "x1": 760, "y1": 440}}]),
            _tool("resolve_finding", n=1, decision="drop", why="the support is drawn in section"),
            _tool("resolve_finding", n=2, decision="fix", why="it is the tag", kind="D",
                  wrong="The panel has no finish tag.", box={"x0": 610, "y0": 385, "x1": 750, "y1": 435}),
        ]  # fmt: skip

    engine, review, _ended, _ = _review(
        tmp_path,
        [
            [_tiles(1, 2, 3, 4, 5, 6)],
            [_tiles(7, 8, 9), _tight_on_text(), _tight_last()],
            # Reported after its zooms: no zoom since, so the self-check must look again.
            [_support_issue(confidence="low"),
             _support_issue(item="Base: end panel", kind="C", confidence="low",
                            box={"x0": 605, "y0": 382, "x1": 755, "y1": 438})],
            [_text("Done.")],
            early,
            settle,
            [_text("Settled.")],
        ],
    )  # fmt: skip
    refused = _results(engine.script.calls[5].messages)
    assert any("zoom tight" in r and "issue 1" in r for r in refused)
    assert [d.status for d in review.drafts] == ["dropped", "fixed"]
    live = [f for f in engine.book.live() if f["created_by"] == "page_reviewer"]
    assert len(live) == 1
    assert live[0][likely_problems.FIELD]["kind"] == "finish"
    assert "no finish tag" in live[0][likely_problems.FIELD]["words"]
    assert len(engine.dropped) == 2  # the dropped one, and the fixed one's first version


def test_code_draws_each_draft_for_the_self_check_and_that_picture_settles_it(
    tmp_path: Path,
) -> None:
    """M7a (2026-11-01): code draws each re-checked draft's evidence into the self-check's message
    (labelled with its number), and `resolve_finding` takes that picture as the fresh look: no
    "zoom tight first" refusal, no zoom by the model. A whole-view draft (a box larger than a tight
    zoom) is settled by its code picture too."""
    whole = {"x0": 300, "y0": 200, "x1": 800, "y1": 500}

    def settle(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            _tool("resolve_finding", n=1, decision="keep", why="the picture shows it"),
            _tool("resolve_finding", n=2, decision="keep", why="the picture shows it"),
        ]

    engine, review, _ended, reviews = _review(
        tmp_path,
        [
            [_support_issue(confidence="low"),
             _issue(claim="please_check", confidence="low", whole_view=True, box=whole)],
            [_text("Done.")],
            settle,
            [_text("Settled.")],
        ],
    )  # fmt: skip
    check = engine.script.calls[2].messages[-1]["content"]
    note = next(b["text"] for b in check if b.get("type") == "text" and "Self-check" in b["text"])
    assert "code drew below" in note
    labels = [b["text"] for b in check if b.get("type") == "text" and b["text"].startswith("Crop")]
    assert ["issue #1" in labels[0], "issue #2" in labels[1]] == [True, True]
    assert sum(1 for b in check if b.get("type") == "image") == 2
    assert not [r for r in _results(engine.script.calls[3].messages) if r.startswith("Refused")]
    assert [d.status for d in review.drafts] == ["kept", "kept"]
    assert review.counts.get("zoom") is None  # the model never zoomed
    assert review.code_zooms == 2
    drawn = [z for z in review.zooms if z.get("for_draft") == 1]
    assert len(drawn) == 1 and page_reviewer.is_tight(drawn[0]["box"])
    reviews.pages["p2"] = review
    (row,) = reviews.block()["pages"]
    assert row["code_zooms_for_selfcheck"] == 2


def test_a_tight_zoom_since_the_draft_was_reported_is_a_fresh_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M7a: a tight zoom near the evidence made after the report and before the self-check counts
    (before, only one made after the self-check began)."""
    monkeypatch.setattr(page_reviewer, "MAX_SELFCHECK_ZOOMS", 0)  # no pictures by code here
    near = _tool("zoom", crops=[{"box": {"x0": 600, "y0": 380, "x1": 760, "y1": 440}}])
    engine, review, *_ = _review(
        tmp_path,
        [
            [_support_issue(confidence="low")],
            [near],
            [_text("Done.")],
            [_tool("resolve_finding", n=1, decision="keep", why="seen again")],
            [_text("Settled.")],
        ],
    )
    assert not [r for r in _results(engine.script.calls[4].messages) if r.startswith("Refused")]
    assert [d.status for d in review.drafts] == ["kept"] and review.code_zooms == 0


# ---- money ----------------------------------------------------------------------------------------


def test_a_money_stop_runs_the_coverage_check_then_a_final_report_turn(tmp_path: Path) -> None:
    """Cap $1: each call costs $0.25 (the worst case is held: $0.01 + 20 micros a token). Its own
    pass ends at $0.50, the review at $0.90; the final turn and the self-check have the rest."""

    def final(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        assert "Time is up for the review itself" in _notes(messages)[-1]
        return [_support_issue()]  # reported in the final turn: no tile gate there

    engine, review, ended, reviews = _review(
        tmp_path,
        [[_tiles(1, 2, 3)], [_tiles(4, 5, 6)], [_tiles(7, 8, 9)], final, [_text("never asked")]],
        cap=1_000_000,
        cost=250_000,
    )
    calls = engine.script.calls
    assert len(calls) == 4
    notes = _notes(calls[2].messages)
    assert any(n.startswith("Coverage check:") for n in notes)  # before the money stop
    assert [d.status for d in review.drafts] == ["kept_unreflected"]
    assert ended[2] == "cost_cap" and ended[1] is True
    assert reviews.cut_by_money == [2]
    # Every call's output allowance fits what is left of its share; never above 64k.
    assert all(c.max_tokens <= MAX_TOKENS for c in calls)
    assert calls[0].max_tokens == (500_000 - _FIXED) // _PER_TOKEN
    # Recorded, though the review never reached its self-check.
    assert [f["created_by"] for f in engine.book.live()] == ["page_reviewer"]


def test_the_model_is_never_told_about_money(tmp_path: Path) -> None:
    engine, *_ = _review(
        tmp_path,
        [[_tiles(1, 2, 3)], [_tiles(4, 5, 6)], [_support_issue()], [_text("x")]],
        cap=1_000_000,
        cost=300_000,
    )
    words = " ".join(n for c in engine.script.calls for n in _notes(c.messages)[1:]).lower()
    for said in ("money", "budget", "price", "cost", "$", "cap"):
        assert said not in words


#: Its turns when money ends it: the nine tiles, then a tight zoom per call (never done on its own).
_LOOKING = [[_tiles(1, 2, 3)], [_tiles(4, 5, 6)], [_tiles(7, 8, 9)]] + [[_tight_last()]] * 12


def _first_final_turn(calls: Sequence[_Call]) -> int:
    return next(
        n for n, c in enumerate(calls) if "Time is up for the review" in _notes(c.messages)[-1]
    )


def test_a_reviewer_whose_calls_write_little_works_on_close_to_its_share(tmp_path: Path) -> None:
    """Cap $1; each call costs $0.05 and writes 1,000 tokens (the worst case held: $0.01 + 20
    micros a token). After its first call, the least a call is made with is twice 1,000, at least
    4,000 tokens ($0.08), not 12,000 ($0.24): the review goes on to $0.80 of its $0.90 share
    (12,000 stopped it at $0.70), still leaves the final report turn its money, and the page
    never passes $1."""
    engine, review, ended, reviews = _review(
        tmp_path, _LOOKING + [[_tight_last()]] * 10, cap=1_000_000, cost=50_000, output=1_000
    )
    calls = engine.script.calls
    assert reviews.cut_by_money == [2]
    assert _first_final_turn(calls) * 50_000 == 800_000  # what the review spent
    assert len(calls) == 19 and engine.spent == 950_000 and ended[2] == "cost_cap"
    # Every call's worst case fits what is left of the cap.
    for n, call in enumerate(calls):
        assert n * 50_000 + _FIXED + call.max_tokens * _PER_TOKEN <= 1_000_000
    assert review.most_output == 1_000 and review.cut_off == 0


def test_a_call_cut_off_at_its_allowance_is_paid_once_and_the_final_turn_still_runs(
    tmp_path: Path,
) -> None:
    """As above, but the review's last call wants more than its 5,500 tokens and is cut off in its
    thinking (lost: it cannot go back). It counts as writing all 5,500, so a next call would need
    11,000: no second cut-off call, the money stop comes, and the final report turn still has its
    6,000 tokens (the review kept them back). The page stays within $1."""
    engine, review, ended, reviews = _review(
        tmp_path,
        _LOOKING + [_CUT, [_text("Nothing more to report.")]],
        cap=1_000_000,
        cost=50_000,
        output=1_000,
    )
    calls = engine.script.calls
    assert len(calls) == 17 and _first_final_turn(calls) == 16
    assert calls[15].max_tokens == 5_500
    assert "Continue where you stopped." in _notes(calls[16].messages)
    assert calls[16].max_tokens == MIN_REFLECT_TOKENS
    assert reviews.cut_by_money == [2] and ended[2] is None
    assert engine.spent == 15 * 50_000 + (_FIXED + 5_500 * _PER_TOKEN) + 50_000 <= 1_000_000
    reviews.pages["p2"] = review
    (row,) = reviews.block()["pages"]
    assert row["cut_off"] == 1 and row["most_output"] == 5_500


def _money_run(tmp_path: Path, cap: int) -> tuple[page_reviewer._Run, _Engine]:
    engine = _Engine(tmp_path, _Script([]))
    review = PageReview(page=2, file=FILE, options=Options())
    agent = SimpleNamespace(model=MODELS["claude-opus-5-5"], cap_micros=cap, spent=0, last_call=0)
    run = page_reviewer._Run(engine, agent, review, Reviews(), {"steps": []}, 0)  # type: ignore[arg-type]
    return run, engine


def test_the_least_a_call_is_made_with_follows_what_its_calls_write(tmp_path: Path) -> None:
    """Before any call is seen, the full least (12,000 tokens in the review, 6,000 after); then
    twice the most one call wrote, at least 4,000 and never above the full least."""
    run, _engine = _money_run(tmp_path, cap=2_000_000)
    body = {"max_tokens": MAX_TOKENS}
    run.agent.spent = 830_000  # its own pass ($1.00) leaves room for 8,000 tokens
    assert run._allowance(body, 0, 0) == 0  # the first call: 12,000 needed
    for most, allowed in ((1_000, 8_000), (3_000, 8_000), (4_000, 8_000), (4_001, 0)):
        run.review.most_output = most
        assert run._allowance(body, 0, 0) == allowed
    run.review.most_output = 20_000
    run.agent.spent = 700_000  # room for 14,500: the least is never above 12,000
    assert run._allowance(body, 0, 0) == 14_500
    run.review.phase = "reflect"
    run.agent.spent = 1_890_000  # the rest of the cap leaves room for 5,000 tokens
    for most, allowed in ((0, 0), (1_000, 5_000), (2_500, 5_000), (2_600, 0)):
        run.review.most_output = most
        assert run._allowance(body, 0, 0) == allowed


def test_a_calls_allowance_never_passes_the_message_cap_or_the_final_turns_money(
    tmp_path: Path,
) -> None:
    run, engine = _money_run(tmp_path, cap=1_000_000)
    body = {"max_tokens": MAX_TOKENS}
    run.review.pass_done = True
    run.review.most_output = 1_000
    run.agent.spent = 700_000
    # The review's share ends at $0.90, but the final report turn keeps its $0.01 + 6,000 tokens
    # ($0.13) of the $1: room for ($1 - 0.13 - 0.70 - 0.01) / 20 micros = 8,000, not 9,500.
    assert run._allowance(body, 0, 0) == 8_000
    # The message's own cap leaves less: that is the allowance, while it covers the least.
    engine.settings.message_cap_micros = engine.spent + _FIXED + 4_500 * _PER_TOKEN
    assert run._allowance(body, 0, 0) == 4_500
    engine.settings.message_cap_micros = engine.spent + _FIXED + 3_000 * _PER_TOKEN
    assert run._allowance(body, 0, 0) == 0


def test_every_call_is_opus_at_high_effort_with_thinking_and_fixed_tools(tmp_path: Path) -> None:
    engine, *_ = _review(
        tmp_path,
        [[_tiles(1, 2, 3, 4, 5, 6)], [_tiles(7, 8, 9)], [_tight_on_text(), _tight_last()], [_text("ok")]],
    )  # fmt: skip
    calls = engine.script.calls
    assert {c.effort for c in calls} == {"high"}
    assert all(c.thinking == {"type": "adaptive", "display": "summarized"} for c in calls)
    assert {tuple(c.tools) for c in calls} == {
        (
            "view_page",
            "zoom",
            "page_text",
            "set_page_text",
            "page_lines",
            "check_math",
            "report_issue",
            "resolve_finding",
            "answer_question",
        )
    }
    assert len({c.system for c in calls}) == 1
    assert calls[0].max_tokens == MAX_TOKENS


# ---- findings -------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["B", "C", "D", "E", "F"])
def test_kinds_b_to_f_are_please_checks_and_never_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    engine, *_ = _review(
        tmp_path,
        [[_tiles(1, 2, 3, 4, 5, 6)], [_tiles(7, 8, 9), _support_issue(kind=kind)], [_text("ok")]],
        cap=600_000,
        cost=200_000,
    )
    (found,) = [f for f in engine.book.live() if f["created_by"] == "page_reviewer"]
    assert found["status"] == "check"
    assert found[likely_problems.FIELD]["proof"] == "likely_only"
    assert found[likely_problems.FIELD]["kind"] == page_reviewer.KIND_OF[kind]
    assert proof.proof_group(found) == "likely_problem"
    assert (likely_problems.payload(found) or {})["by"] == "page_reviewer"
    # Whatever a later step says, never clouded by itself.
    monkeypatch.setattr(proof, "code_proved", lambda f: True)
    assert proof.proof_group({**found, "status": "different"}) == "likely_problem"


def test_a_string_that_does_not_add_up_takes_the_existing_proof_path(tmp_path: Path) -> None:
    engine, *_ = _review(
        tmp_path,
        [
            [_tiles(1, 2, 3, 4, 5, 6)],
            [_tiles(7, 8, 9), _string_issue(parts=('12"', '12"', '12 1/4"')), _string_issue()],
            [_text("ok")],
        ],
        cap=600_000,
        cost=200_000,
    )
    refused = _results(engine.script.calls[2].messages)
    assert any("code adds the parts you read" in r and "check your reading" in r for r in refused)
    (found,) = [f for f in engine.book.live() if f["created_by"] == "page_reviewer"]
    assert found["source"] == "vendor_arithmetic" and found["status"] == "different"
    assert found["parts"] == ['12"', '12"', '11 3/4"'] and found["printed_total"] == '36 1/4"'
    assert found["architect"] == '35 3/4"'  # code's sum, never the AI's
    # Red only through the existing gates: an AI-grouped string is never red until code's string
    # geometry verifies it; until then it is a please-check.
    assert not proof.string_verified(found)
    assert not proof.code_proved(found)
    assert proof.proof_group(found) == "likely_problem"


def test_an_item_already_recorded_at_the_spot_is_not_recorded_twice(tmp_path: Path) -> None:
    def helper_first(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # A helper recorded the same string at the same spot meanwhile.
        engine = engine_box[0]
        helper, _notes = engine.book.report(
            {
                "page": 2, "item": "Elevation string", "category": "other",
                "architect": '35 3/4"', "vendor": '36 1/4"', "status": "different",
                "verified": False, "source": "vendor_arithmetic",
                "parts": ['12"', '12"', '11 3/4"'], "printed_total": '36 1/4"',
                "vendor_box": {"page": 2, "x0": 200, "y0": 290, "x1": 360, "y1": 335},
            },
            "helper:3",
        )  # fmt: skip
        # The engine's own checks read its values (`ChatEngine._checked`).
        engine.book.replace({**helper, "inches": {"vendor": "36.25", "architect": "35.75"}})
        return [_string_issue(), _support_issue(), _support_issue(item="Island top: support")]

    engine_box: list[Any] = []
    original = _Engine.__init__

    def keep(self: _Engine, *a: Any, **k: Any) -> None:
        original(self, *a, **k)
        engine_box.append(self)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_Engine, "__init__", keep)
        engine, review, *_ = _review(
            tmp_path,
            [[_tiles(1, 2, 3, 4, 5, 6)], [_tiles(7, 8, 9)], helper_first, [_text("ok")]],
            cap=600_000,
            cost=150_000,
        )
    mine = [f for f in engine.book.live() if f["created_by"] == "page_reviewer"]
    assert len(mine) == 1  # the support once; the string is the helper's
    assert review.drafts[0].joined is not None and review.drafts[2].joined == mine[0]["id"]


def test_a_clear_the_api_reports_is_counted_once_in_the_log() -> None:
    """M8d: the API reports the same cleared tool uses on every call between clears."""
    review = PageReview(page=2, file=FILE, options=Options())
    run = page_reviewer._Run.__new__(page_reviewer._Run)
    run.review = review

    def reply(cleared: int) -> Any:
        edit = {"type": "clear_tool_uses_20250919", "cleared_tool_uses": cleared,
                "cleared_input_tokens": 30_000}  # fmt: skip
        return SimpleNamespace(applied_edits=[edit] if cleared else [])

    for cleared in (0, 0, 2, 2, 2, 5, 5):
        run._count_clears(reply(cleared))
    assert review.context_clears == 2
    reviews = Reviews()
    reviews.pages["p2"] = review
    assert reviews.block()["pages"][0]["context_clears"] == 2


def test_the_log_block_says_what_each_page_review_did(tmp_path: Path) -> None:
    _engine, _review_, _ended, reviews = _review(
        tmp_path,
        [[_tiles(1, 2, 3, 4, 5, 6)], [_tiles(7, 8, 9), _support_issue()], [_text("ok")]],
        cap=600_000,
        cost=200_000,
    )
    reviews.pages["p2"] = _review_
    (row,) = reviews.block()["pages"]
    assert row["page"] == 2 and row["issues"] == 1 and row["tiles_left"] == []
    assert row["tools"]["zoom"] == 2 and row["tools"]["report_issue"] == 1


# ---- in the full check -------------------------------------------------------------------------------


def _reviewer_turn(params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The scripted reviewer inside a real full check: tiles, one support issue, then the
    self-check, then done (the coverage check may send it on; it ends each time)."""
    from tests.review.assistant.chat_fakes import reply, text, tool

    messages = params["messages"]
    turns = sum(1 for m in messages if m["role"] == "assistant")
    last = messages[-1]["content"]
    said = " ".join(
        b.get("text", "") for b in last if isinstance(b, dict) and b.get("type") == "text"
    )
    if "Self-check." in said:
        return reply(
            tool("z", "zoom", {"crops": [{"box": {"x0": 20, "y0": 20, "x1": 200, "y1": 120}}]}),
            tool("r", "resolve_finding", {"n": 1, "decision": "keep", "why": "seen"}),
            stop="tool_use",
        )
    if turns == 0:
        return reply(
            tool("a", "zoom", {"crops": [{"tile": n} for n in range(1, 7)]}), stop="tool_use"
        )
    if turns == 1:
        page = re.search(r"reviewing page (\d+) ", messages[0]["content"][0]["text"]).group(1)  # type: ignore[union-attr]
        issue = {
            "kind": "E", "claim": "please_check", "item": f"Top on page {page}: who supplies it",
            "wrong": "The top's callout does not say it is by GV.", "confidence": "medium",
            "box": {"x0": 20, "y0": 20, "x1": 200, "y1": 120},
        }  # fmt: skip
        return reply(
            tool("b", "zoom", {"crops": [{"tile": n} for n in range(7, 10)]}),
            tool("c", "report_issue", issue),
            stop="tool_use",
        )
    return reply(text("Checked the page."))


def test_a_full_check_with_the_reviewer_on_reviews_each_vendor_page_on_its_own_money(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.review.assistant.test_chat_engine import DOLLARS
    from tests.review.assistant.test_full_check_roster import (
        _clean_ink,
        _findings,
        _Roster,
        _roster,
    )

    class _WithReviewer(_Roster):
        def __call__(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
            if params["system"][0]["text"].startswith("You are reviewing one page"):
                self._seen("page_review", params)
                return _reviewer_turn(params)
            return super().__call__(params)

    _clean_ink(monkeypatch)
    script = _WithReviewer()
    harness = _roster(tmp_path, script, price=12 * DOLLARS)
    engine = harness.engine
    options = Options(per_page_micros=500_000)
    engine.settings = dataclasses.replace(engine.settings, page_reviewer=options)

    outcome = harness.run()

    called = script.params["page_review"]
    # One cache warm-up before the reviewers, with their exact brief, tools, thinking and effort.
    (warm,) = [p for p in called if p["max_tokens"] == page_reviewer.WARM_TOKENS]
    asked = [p for p in called if p is not warm]
    assert warm["messages"][0]["content"][-1]["text"] == "Reply with OK."
    for key in ("system", "tools", "thinking", "output_config", "tool_choice"):
        assert all(p[key] == warm[key] for p in asked), key
    pages = sorted(
        {
            int(
                re.search(r"reviewing page (\d+) ", p["messages"][0]["content"][0]["text"]).group(1)
            )  # type: ignore[union-attr]
            for p in asked
        }
    )
    assert pages  # each vendor page has its own reviewer
    assert all(p["output_config"]["effort"] == "high" for p in asked)
    assert all(p["thinking"]["type"] == "adaptive" for p in asked)
    found = [f for f in _findings(outcome) if f.get("created_by") == "page_reviewer"]
    assert found and all(proof.proof_group(f) == "likely_problem" for f in found)
    assert all((likely_problems.payload(f) or {})["kind"] == "scope" for f in found)
    # Its money is its own: on top of the fixed price, and never counted in the other shares.
    reviewed = len(pages)
    assert engine.own_share_micros == reviewed * 500_000
    assert engine.counted_spent == engine.spent - engine._own_spent
    assert engine._own_spent > 0
    assert engine._own_spent <= engine.own_share_micros


def test_a_full_check_with_the_reviewer_off_is_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.review.assistant.test_chat_engine import DOLLARS
    from tests.review.assistant.test_full_check_roster import _clean_ink, _Roster, _roster

    _clean_ink(monkeypatch)
    script = _Roster()
    harness = _roster(tmp_path, script, price=12 * DOLLARS)
    assert harness.engine.settings.page_reviewer is None
    harness.run()
    assert "page_review" not in script.calls
    assert not any(
        p["system"][0]["text"].startswith("You are reviewing one page")
        for kind in script.params.values()
        for p in kind
    )
    assert harness.engine.own_share_micros == 0
    assert harness.engine.counted_spent == harness.engine.spent


def test_page_lines_reads_the_pages_own_vector_lines(tmp_path: Path) -> None:
    engine, *_ = _review(
        tmp_path,
        [[_tool("page_lines", box={"x0": 90, "y0": 680, "x1": 420, "y1": 700})], [_text("ok")]],
        cap=600_000,
        cost=200_000,
    )
    (result,) = _results(engine.script.calls[1].messages)
    assert "100.0,692.0 to 400.0,692.0  horizontal  300.0 pt" in result


@pytest.mark.parametrize(
    ("read", "why"),
    [
        ('the overall looks short (about 30")', "the two printed numbers a red size issue"),
        ('24" in plan, 25" in elevation', "does not find two of the numbers"),
    ],
)
def test_a_red_size_issue_must_quote_two_numbers_printed_on_the_page(
    tmp_path: Path, read: str, why: str
) -> None:
    """A red (claim wrong) still rests on two printed numbers (2026-11-01: only reds)."""
    size = _tool(
        "report_issue", kind="A", claim="wrong", item="Elevation: width",
        wrong="The width differs between views.", read=read, confidence="medium",
        box={"x0": 190, "y0": 285, "x1": 370, "y1": 340},
    )  # fmt: skip
    printed = _tool(
        "report_issue", kind="A", claim="wrong", item="Elevation: overall",
        wrong="The overall and the parts disagree.", read='36 1/4" overall; parts 12" + 12"',
        confidence="medium", box={"x0": 190, "y0": 285, "x1": 370, "y1": 340},
    )  # fmt: skip
    engine, review, *_ = _review(
        tmp_path,
        [[_tiles(1, 2, 3, 4, 5, 6)], [_tiles(7, 8, 9), size, printed], [_text("ok")]],
        cap=600_000,
        cost=200_000,
    )
    results = _results(engine.script.calls[2].messages)
    assert any(why in r for r in results)
    assert [d.item for d in review.drafts] == ["Elevation: overall"]
    (found,) = [f for f in engine.book.live() if f["created_by"] == "page_reviewer"]
    assert found[likely_problems.FIELD]["kind"] == "size"
    assert proof.proof_group(found) == "likely_problem"


# ---- the rest of the set (2026-10-29) --------------------------------------------------------------


def _size_issue(read: str, claim: str = "wrong") -> dict[str, Any]:
    return _tool(
        "report_issue",
        kind="A",
        claim=claim,
        item="Top length vs the countertop drawing",
        wrong='The elevation prints 36 1/4"; the countertop drawing (p1) prints 36".',
        read=read,
        box={"x0": 250, "y0": 320, "x1": 300, "y1": 340},
        confidence="medium",
    )


def test_the_first_message_lists_what_the_rest_of_the_set_prints(tmp_path: Path) -> None:
    engine, *_ = _review(tmp_path, [[_text("Nothing to report.")]], cap=600_000, cost=200_000)
    first = " ".join(
        b.get("text", "") for b in engine.script.calls[0].messages[0]["content"] if "text" in b
    )
    assert "Facts from the rest of this drawing set" in first
    assert "ST-01: no other page" in first  # this page's tags, with the other pages that print them
    assert '"BIN BY GC/OWNER": p1' in first
    assert "set_page_text" not in first.split("drawing_set>")[-1][:40]


def test_another_page_can_be_read_and_zoomed_but_never_counts_for_this_pages_coverage(
    tmp_path: Path,
) -> None:
    engine, review, *_ = _review(
        tmp_path,
        [
            [
                _tool("set_page_text", page=1),
                _tool("set_page_text", page=7),
                _tool("zoom", crops=[{"page": 1, "box": {"x0": 250, "y0": 300, "x1": 450, "y1": 520}}]),
            ],
            [_text("ok")],
        ],
        cap=600_000,
        cost=200_000,
    )  # fmt: skip
    text, missing, *_ = _results(engine.script.calls[1].messages)
    assert "BIN BY GC/OWNER" in text and "ST-02 Quartz top" in text
    assert "Refused" in missing and "no page 7" in missing
    # p1's picture is not this page's coverage: its zooms are only the tiles of its first message.
    assert [z["box"] for z in review.zooms] == review.tile_boxes
    assert review.pictures == 2 + 9  # the whole page and the tiles, then p1's area


def test_a_size_issue_may_rest_on_a_number_another_page_prints_once_read(
    tmp_path: Path,
) -> None:
    unread, *_ = _review(
        tmp_path,
        [
            [_tiles(1, 2, 3, 4, 5, 6)],
            [_tiles(7, 8, 9)],
            [_size_issue('36 1/4" ; 36"')],
            [_text("ok")],
        ],
        cap=900_000,
        cost=100_000,
    )
    assert any("two of the numbers" in r for r in _results(unread.script.calls[3].messages))
    _read, review, *_ = _review(
        tmp_path / "read",
        [
            [_tiles(1, 2, 3, 4, 5, 6), _tool("set_page_text", page=1)],
            [_tiles(7, 8, 9)],
            [_size_issue('36 1/4" ; 36"')],
            [_text("ok")],
        ],
        cap=900_000,
        cost=100_000,
    )
    assert [d.kind for d in review.drafts] == ["A"]


def test_a_size_issue_never_rests_only_on_numbers_other_pages_print(tmp_path: Path) -> None:
    engine, review, *_ = _review(
        tmp_path,
        [
            [_tiles(1, 2, 3, 4, 5, 6), _tool("set_page_text", page=1)],
            [_tiles(7, 8, 9)],
            [_size_issue('36" ; 40"')],
            [_text("ok")],
        ],
        cap=900_000,
        cost=100_000,
    )
    assert any("printed on this page" in r for r in _results(engine.script.calls[3].messages))
    assert review.drafts == []


def test_every_reviewer_call_clears_old_zoom_pictures_once_the_prompt_is_long(
    tmp_path: Path,
) -> None:
    """2026-10-29: each turn re-sent every picture (most of a page's money); old tool results are
    cleared server-side past `CONTEXT_CLEAR_TOKENS`, kept by tool uses (2026-11-01, M8d: "keep 4
    turns" cleared nothing, a reviewer has two or three turns), in large batches, never its
    reports, sums or text listings."""
    from app.review.assistant.page_reviewer import CONTEXT_CLEAR_TOKENS

    engine, *_ = _review(
        tmp_path,
        [[_tiles(1, 2, 3, 4, 5, 6)], [_tiles(7, 8, 9)], [_support_issue()], [_text("ok")]],
        cap=900_000,
        cost=100_000,
    )
    for call in engine.script.calls:
        (edit,) = call.context_management["edits"]
        assert edit["type"] == "clear_tool_uses_20250919"
        assert edit["trigger"] == {"type": "input_tokens", "value": CONTEXT_CLEAR_TOKENS}
        assert edit["keep"]["type"] == "tool_uses" and edit["keep"]["value"] >= 6
        assert edit["clear_at_least"] == {"type": "input_tokens", "value": 25_000}
        assert edit["exclude_tools"] == ["check_math", "page_text", "report_issue"]


def _tool_turns(names_and_pictures: Sequence[tuple[str, int]]) -> list[dict[str, Any]]:
    """A made-up reviewer conversation: its first message, then one tool use per call, each
    result carrying that many 1600 x 1132 px pictures (about 2,300 tokens each)."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": [_text("The page.")]}]
    for name, pictures in names_and_pictures:
        call = _tool(name)
        picture = {"type": "image", "source": {"type": "gv_picture", "picture_id": "p",
                                              "width": 1600, "height": 1132}}  # fmt: skip
        messages.append({"role": "assistant", "content": [call]})
        result = {"type": "tool_result", "tool_use_id": call["id"],
                  "content": [_text("Crop")] + [dict(picture) for _ in range(pictures)]}  # fmt: skip
        messages.append({"role": "user", "content": [result]})
    return messages


def test_the_reviewers_context_edit_clears_by_tool_uses_in_rare_large_batches() -> None:
    """M8d: the boundary moves only when a whole batch of at least 25k tokens lies before the last
    six tool uses, so between clears every call clears the same blocks (the cache holds); the
    lead's edit (`context_edits`, by turns) is unchanged."""
    from app.review.assistant.chat_context import context_edits

    keep = page_reviewer.CONTEXT_KEEP_TOOL_USES

    def kept(uses: Sequence[tuple[str, int]]) -> int:
        (edit,) = page_reviewer.review_context(_tool_turns(uses))["edits"]
        return int(edit["keep"]["value"])

    # Zooms of six crops (~14k tokens each): nothing to clear until two of them lie before the
    # last six uses; then the oldest two are cleared, and the boundary holds call after call.
    zooms = [("zoom", 6)] * 7
    assert kept(zooms) == 7  # one zoom outside the last six: under 25k, nothing cleared
    assert kept([*zooms, ("zoom", 6)]) == 6  # two (~28k) outside: cleared, six kept
    assert kept([*zooms, ("zoom", 6), ("check_math", 0)]) == 7  # the same two cleared
    # The next two zooms make a second batch outside the last six: the next clear.
    assert kept([*zooms, ("zoom", 6), ("check_math", 0), ("zoom", 1)]) == 6
    # Reports, sums and text listings never count toward a batch (they are never cleared).
    assert kept([("page_text", 9)] * 3 + [("zoom", 1)] * 6) == 9
    assert kept([("zoom", 0)] * 3) == keep  # few uses: at least the six
    lead = context_edits(_tool_turns(zooms), threshold=400_000, keep_turns=2, near=False)
    assert "exclude_tools" not in lead["edits"][0]


# ---- tight boxes and the kind by words (2026-10-29) --------------------------------------------------


def _issue(**more: Any) -> dict[str, Any]:
    given = {
        "kind": "C", "claim": "please_check", "item": "Elevation: a callout", "confidence": "medium",
        "wrong": "The elevation prints a dimension with no extension line.",
        "box": {"x0": 300, "y0": 300, "x1": 420, "y1": 340},
        **more,
    }  # fmt: skip
    return _tool("report_issue", **given)


def _after_tiles(tmp_path: Path, *issues: dict[str, Any]) -> tuple[_Engine, PageReview]:
    engine, review, *_ = _review(
        tmp_path,
        [[_tiles(1, 2, 3, 4, 5, 6)], [_tiles(7, 8, 9)], list(issues), [_text("ok")]],
        cap=900_000,
        cost=100_000,
    )
    return engine, review


def test_a_broad_box_is_refused_with_the_way_to_box_the_callout(tmp_path: Path) -> None:
    engine, review = _after_tiles(
        tmp_path,
        _issue(kind="D", box={"x0": 100, "y0": 100, "x1": 700, "y1": 500}),  # a view, no flag
        _issue(kind="D", box={"x0": 100, "y0": 100, "x1": 450, "y1": 180}),  # 350: still broad
        _issue(kind="D"),  # tight: recorded
    )
    refused = [r for r in _results(engine.script.calls[3].messages) if r.startswith("Refused")]
    assert len(refused) == 2 and all(
        "box the callout, dimension or tag itself" in r for r in refused
    )
    assert "600 points across" in refused[0] and "350 points across" in refused[1]
    assert [round(d.box[2] - d.box[0]) for d in review.drafts] == [120]


def test_a_whole_view_issue_may_box_its_view_but_not_the_page(tmp_path: Path) -> None:
    engine, review = _after_tiles(
        tmp_path,
        _issue(kind="B", wrong="The top overhangs 14 in; no support is drawn under it.", whole_view=True,
               box={"x0": 100, "y0": 100, "x1": 650, "y1": 500}),  # 550: a view
        _issue(kind="B", wrong="No support anywhere.", whole_view=True,
               box={"x0": 0, "y0": 0, "x1": 1200, "y1": 700}),  # the page
    )  # fmt: skip
    (refused,) = [r for r in _results(engine.script.calls[3].messages) if r.startswith("Refused")]
    assert "even an issue about a whole view boxes just that view" in refused
    assert [d.kind for d in review.drafts] == ["B"]


def test_a_dimension_string_may_span_its_run(tmp_path: Path) -> None:
    long_string = _tool(
        "report_issue",
        kind="A", claim="wrong", item="Elevation: overall width", confidence="high",
        wrong='The parts 12 + 12 + 11 3/4 add up to 35 3/4", the overall says 36 1/4".',
        read='12" + 12" + 11 3/4" ; 36 1/4"', parts=['12"', '12"', '11 3/4"'], printed_total='36 1/4"',
        box={"x0": 100, "y0": 285, "x1": 450, "y1": 340},  # 350 across: a string
    )  # fmt: skip
    _engine, review = _after_tiles(tmp_path, long_string)
    assert [d.kind for d in review.drafts] == ["A"]


def test_a_completeness_issue_naming_a_finish_tag_or_a_supplier_is_filed_by_code(
    tmp_path: Path,
) -> None:
    _engine, review = _after_tiles(
        tmp_path,
        _issue(wrong="The toe kick is tagged ST-03 but the table lists no ST-03."),
        _issue(wrong="The bin is drawn; nothing says it is by GC/Owner or by GV.",
               box={"x0": 500, "y0": 300, "x1": 620, "y1": 340}),
        _issue(wrong="The sink cut-out has no width.", box={"x0": 700, "y0": 300, "x1": 820, "y1": 340}),
    )  # fmt: skip
    assert [d.kind for d in review.drafts] == ["D", "E", "C"]
    assert [d.also_kind for d in review.drafts] == ["C", "C", None]  # the reviewer's C kept
    kinds = [
        likely_problems.payload(f)["kind"]
        for f in _engine.book.live()
        if f["created_by"] == "page_reviewer"
    ]
    assert kinds == ["finish", "scope", "completeness"]


def test_a_sheet_reference_or_a_size_gap_is_never_refiled(tmp_path: Path) -> None:
    _engine, review = _after_tiles(
        tmp_path,
        _issue(wrong="Detail 3/MW-501 is referenced but no such detail is on the set."),
        _issue(wrong="The refrigerator (N.I.C.) opening has no width dimension.",
               box={"x0": 500, "y0": 300, "x1": 620, "y1": 340}),
        _issue(wrong="The sink is drawn; nothing says who supplies it, by GV or by others.",
               box={"x0": 700, "y0": 300, "x1": 820, "y1": 340}),
    )  # fmt: skip
    assert [d.kind for d in review.drafts] == ["C", "C", "E"]
    assert [d.also_kind for d in review.drafts] == [None, None, "C"]


# ---- two kinds (plan M10, 2026-11-01) ----------------------------------------------------------------


def test_a_second_kind_is_kept_and_a_bad_one_refused(tmp_path: Path) -> None:
    engine, review = _after_tiles(
        tmp_path,
        _issue(kind="C", also_kind="C"),  # the same as kind
        _issue(kind="C", also_kind="G"),  # not a kind
        _issue(kind="C", also_kind="B", wrong="The end panel is drawn with no section."),
        _issue(kind="B", also_kind="", wrong="No support is drawn under the overhang.",
               box={"x0": 500, "y0": 300, "x1": 620, "y1": 340}),  # empty: none given
    )  # fmt: skip
    refused = [r for r in _results(engine.script.calls[3].messages) if r.startswith("Refused")]
    assert len(refused) == 2
    assert "also_kind must differ from kind" in refused[0]
    assert "also_kind must be one of A, B, C, D, E, F" in refused[1]
    assert [(d.kind, d.also_kind) for d in review.drafts] == [("C", "B"), ("B", None)]


def test_the_words_add_a_second_kind_and_never_replace_the_reviewers(tmp_path: Path) -> None:
    _engine, review = _after_tiles(
        tmp_path,
        _issue(kind="B", wrong="The toe kick panel tagged ST-03 has no support drawn."),
        _issue(kind="F", wrong="The title block says the bin is by GC/Owner; nothing else does.",
               box={"x0": 500, "y0": 300, "x1": 620, "y1": 340}),
        _issue(kind="A", wrong='The ST-03 top is 25 1/2" deep; the plan says 26".',
               read='25 1/2"', box={"x0": 700, "y0": 300, "x1": 820, "y1": 340}),
        _issue(kind="D", wrong="The finish table lists PL-07; the elevation says PL-08.",
               box={"x0": 900, "y0": 300, "x1": 1020, "y1": 340}),
        _issue(kind="B", also_kind="C", wrong="The ST-03 apron has no support or section.",
               box={"x0": 300, "y0": 500, "x1": 420, "y1": 540}),
    )  # fmt: skip
    assert [(d.kind, d.also_kind) for d in review.drafts] == [
        ("B", "D"),  # a finish tag named: finish added
        ("F", "E"),  # a supplier named: scope added
        ("A", None),  # a size stays a size only
        ("D", None),  # finish words on a finish issue: nothing to add
        ("B", "C"),  # the reviewer's own second kind wins
    ]


def test_the_finding_carries_its_second_kind_to_the_screen(tmp_path: Path) -> None:
    from app.review.assistant.chat_contract import ChatFindingLikelyOut

    engine, _review = _after_tiles(
        tmp_path,
        _issue(kind="B", also_kind="D", wrong="The end panel has no support drawn."),
        _issue(kind="B", wrong="The base has no toe kick drawn.",
               box={"x0": 500, "y0": 300, "x1": 620, "y1": 340}),
    )  # fmt: skip
    live = [f for f in engine.book.live() if f["created_by"] == "page_reviewer"]
    marks = [f[likely_problems.FIELD] for f in live]
    assert [(m.get("also_kind"), m.get("also_letter")) for m in marks] == [
        ("finish", "D"),
        (None, None),
    ]
    shown = [likely_problems.payload(f) or {} for f in live]
    assert [p.get("also_kind") for p in shown] == ["finish", None]
    assert "also_kind" not in shown[1]  # nothing new on an item with one kind
    assert ChatFindingLikelyOut.model_validate(shown[0]).also_kind == "finish"
    assert ChatFindingLikelyOut.model_validate(shown[1]).also_kind is None


def test_a_second_kind_never_makes_a_red(tmp_path: Path) -> None:
    engine, review = _after_tiles(
        tmp_path, _issue(kind="B", also_kind="A", claim="wrong", wrong="No support is drawn.")
    )
    (draft,) = review.drafts
    assert (draft.kind, draft.also_kind) == ("B", "A")
    (found,) = [f for f in engine.book.live() if f["created_by"] == "page_reviewer"]
    assert likely_problems.likely_only(found)


# ---- the sharp whole page (plan M8e, 2026-11-01) -------------------------------------------------------


def _whole_page_picture(first: list[dict[str, Any]]) -> tuple[str, Mapping[str, Any]]:
    """The whole page's crop line and its picture's source, from a first message."""
    at = next(
        i
        for i, block in enumerate(first)
        if block.get("type") == "text" and "(The whole page)" in str(block.get("text"))
    )
    return str(first[at]["text"]), first[at + 1]["source"]


@pytest.mark.parametrize(
    ("sharp", "dpi", "size"),
    [(False, 94, (1598, 1034)), (True, 139, (2363, 1529))],
)
def test_the_sharp_page_setting_draws_the_whole_page_at_the_high_resolution_tier(
    tmp_path: Path, sharp: bool, dpi: int, size: tuple[int, int]
) -> None:
    """This made-up 17 x 11 in sheet: 1,600 px by default; with the setting, as sharp as fits
    the 4,784-token budget (2,576 px asked: 151 dpi would be 5,423 tokens), so the API never
    shrinks it, and under the 3,000 px limit of a request with more than 20 pictures."""
    listing = [{"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"}]
    options = Options(sharp_page=sharp)
    _engine, first = _first_message(tmp_path, {}, listing, options=options)
    line, source = _whole_page_picture(first)
    assert f"{dpi} dpi, {size[0]} x {size[1]} px" in line
    assert (source["width"], source["height"]) == size
    tokens = -(-size[0] // 28) * -(-size[1] // 28)
    assert tokens <= 4784 and max(size) <= page_reviewer.SHARP_PAGE_EDGE_PX < 3000
    images = [b["source"] for b in first if b.get("type") == "image"]
    others = [o for o in images if o is not source]
    assert len(others) == 9  # the tiles: as before, at most 1,600 px either way
    assert all(max(o["width"], o["height"]) <= page_reviewer.PICTURE_EDGE_PX for o in others)


def test_the_sharp_page_setting_is_off_by_default() -> None:
    from app.api.assistant_chats import page_reviewer_options
    from app.config import Settings

    assert Settings.model_fields["assistant_page_reviewer_sharp_page"].default is False
    assert Options().sharp_page is False
    settings = SimpleNamespace(
        assistant_page_reviewer=True,
        assistant_page_reviewer_per_page_usd=Decimal("1.50"),
        assistant_page_reviewer_effort="high",
        assistant_page_reviewer_house_rules=None,
        assistant_page_reviewer_pictures=None,
        assistant_separate_verifier=False,
        assistant_sizes_lens=False,
        assistant_page_reviewer_sharp_page=True,
    )
    on = page_reviewer_options(settings)  # type: ignore[arg-type]
    assert on is not None and on.sharp_page is True


def test_a_fix_keeps_the_whole_view_allowance_it_was_accepted_with(tmp_path: Path) -> None:
    def settle(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            _tool("zoom", crops=[{"box": {"x0": 110, "y0": 110, "x1": 300, "y1": 250}}]),
            _tool("resolve_finding", n=1, decision="fix", why="better words",
                  wrong="The top overhangs 14 in at the seating side; no support is drawn."),
        ]  # fmt: skip

    engine, review, *_ = _review(
        tmp_path,
        [
            [_tiles(1, 2, 3, 4, 5, 6)],
            [_tiles(7, 8, 9), _issue(kind="B", wrong="Overhang with no support.", whole_view=True,
                                     confidence="low", box={"x0": 100, "y0": 100, "x1": 650, "y1": 500})],
            [_tight_on_text(), _tight_last()],
            [_text("Done.")],
            settle,
            [_text("Settled.")],
        ],
    )  # fmt: skip
    (draft,) = review.drafts
    assert draft.status == "fixed" and draft.whole_view and "no support is drawn" in draft.wrong
    assert not any("points across" in r for r in _results(engine.script.calls[-1].messages))


def _design_pdf() -> bytes:
    """A made-up design set: the item's top tag on a plan, with its room and an outlet beside it,
    and a finish code printed on every page (never taken for the item's tag)."""
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(WIDTH, HEIGHT), invariant=1)
    for page in range(1, 6):
        c.setFont("Helvetica", 9)
        c.drawString(100, HEIGHT - 700, "ST-01 QUARTZ" if page > 1 else "")
        if page == 1:
            for words, x, y in [
                ("ST-01T", 600.0, 400.0),
                ("SUNDRY SHOP", 640.0, 450.0),
                ("GFI", 560.0, 420.0),
            ]:
                c.drawString(x, HEIGHT - y, words)
        c.showPage()
    c.save()
    return out.getvalue()


def test_the_design_sets_plan_around_the_items_tag_is_in_the_first_message(
    tmp_path: Path,
) -> None:
    design = "Design set.pdf"
    script = _Script([[_text("Nothing to report.")]], 200_000)
    engine = _Engine(tmp_path, script)
    (engine.workspace.uploads / design).write_bytes(_design_pdf())
    listing = [
        {"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"},
        {"name": design, "pages": 5, "kind": "the architect's drawings"},
    ]
    (engine.workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    engine.tools = ChatTools(
        workspace=engine.workspace,
        sandbox=FakeSandbox(engine.workspace, lambda command, _w: ("ok", 0)),
        page_count=2,
        uploads=listing,
    )
    review = PageReview(page=2, file=FILE, options=Options())
    agent = SimpleNamespace(
        model=MODELS["claude-opus-5-5"], cap_micros=600_000, spent=0, last_call=0
    )
    page_reviewer._Run(engine, agent, review, Reviews(), {"steps": []}, 0).run()  # type: ignore[arg-type]
    first = engine.script.calls[0].messages[0]["content"]
    words = " ".join(b.get("text", "") for b in first if "text" in b)
    assert "The design set (the architect's file, which governs)" in words
    # The vendor's ST-01 is the design's ST-01T on page 1 (the top's own number); its room and
    # the outlet beside it are listed, and the design's picture follows.
    assert f"ST-01: design page 1 of {design}" in words
    assert "SUNDRY SHOP" in words and "GFI" in words
    at = next(i for i, b in enumerate(first) if "design_reference" in b.get("text", ""))
    assert first[at + 1]["type"] == "text" and first[at + 2]["type"] == "image"


# ---- more context in the first message (2026-10-31) -----------------------------------------------


def _first_message(
    tmp_path: Path,
    uploads: Mapping[str, bytes],
    listing: list[dict[str, Any]],
    *,
    page: int = 2,
    options: Options | None = None,
    reviews: Reviews | None = None,
) -> tuple[_Engine, list[dict[str, Any]]]:
    """One review's first message, with `uploads` (name to PDF bytes) and `listing` as the chat's
    files (the vendor's file is `FILE`)."""
    engine = _Engine(tmp_path, _Script([[_text("Nothing to report.")]], 200_000))
    for name, data in uploads.items():
        (engine.workspace.uploads / name).write_bytes(data)
    (engine.workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    engine.tools = ChatTools(
        workspace=engine.workspace,
        sandbox=FakeSandbox(engine.workspace, lambda command, _w: ("ok", 0)),
        page_count=2,
        uploads=listing,
    )
    review = PageReview(page=page, file=FILE, options=options or Options())
    agent = SimpleNamespace(
        model=MODELS["claude-opus-5-5"], cap_micros=600_000, spent=0, last_call=0
    )
    run = page_reviewer._Run(engine, agent, review, reviews or Reviews(), {"steps": []}, 0)  # type: ignore[arg-type]
    run.run()
    return engine, engine.script.calls[0].messages[0]["content"]


def _png_file(path: Path) -> None:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (60, 40), "white").save(out, format="PNG")
    path.write_bytes(out.getvalue())


def _picture_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "gv-pictures"
    folder.mkdir()
    for name in ("rule.png", "example.png", "this-job.png"):
        _png_file(folder / name)
    manifest = [
        {"file": "rule.png", "caption": "Overall: outside to outside.", "group": "rule"},
        {"file": "example.png", "caption": "GV asks for the filler's size.", "group": "example"},
        {
            "file": "this-job.png",
            "caption": "An example from this very job.",
            "group": "example",
            "exclude_projects": ["VENDOR SET"],
        },
    ]
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def test_gvs_reference_pictures_open_every_pages_first_message_cached_once(
    tmp_path: Path,
) -> None:
    folder = _picture_folder(tmp_path)
    listing = [{"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"}]
    reviews = Reviews()
    options = Options(reference_pictures=folder)
    engine, first = _first_message(tmp_path, {}, listing, options=options, reviews=reviews)
    kinds = [block["type"] for block in first[:6]]
    assert kinds == ["text", "image", "text", "text", "image", "text"]
    assert first[0]["text"].startswith("GV's rule pictures")
    assert first[2]["text"] == "Overall: outside to outside."
    assert first[3]["text"].startswith("Examples of what GV's reviewer marks")
    assert first[5]["text"] == "GV asks for the filler's size."
    # This job's own example is left out (its name is part of the upload's).
    assert "this very job" not in json.dumps(first)
    # The prompt-cache breakpoint sits after the last picture's caption, before the page itself.
    assert first[5]["cache_control"] == {"type": "ephemeral"}
    assert not any("cache_control" in block for block in first[:5])
    assert first[6]["text"].startswith("You are reviewing page 2")
    picture = first[1]["source"]["picture_id"]
    assert (engine.workspace.pictures / f"{picture}.png").exists()
    # Another page of the same check starts with the very same blocks (one cache entry).
    agent = SimpleNamespace(model=MODELS["claude-opus-5-5"], cap_micros=1, spent=0, last_call=0)
    other = PageReview(page=1, file=FILE, options=options)
    run = page_reviewer._Run(engine, agent, other, reviews, {"steps": []}, 0)  # type: ignore[arg-type]
    again = run.reference_blocks()
    assert again == first[:6]


def test_a_missing_picture_folder_is_no_picture_and_the_review_goes_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    listing = [{"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"}]
    options = Options(reference_pictures=tmp_path / "nowhere")
    with caplog.at_level("WARNING"):
        engine, first = _first_message(tmp_path, {}, listing, options=options)
    assert first[0]["text"].startswith("You are reviewing page 2")
    assert "reference pictures could not be read" in caplog.text
    assert engine.script.calls  # the review ran


def _room_design_pdf() -> bytes:
    """A made-up design set: page 1 an enlarged plan (the item's top tag in its room, an outlet,
    a window wall 450 points away, another item in the pantry, the issue date), page 2 an
    elevation of the same item, pages 3 to 5 a finish code only."""
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(WIDTH, HEIGHT), invariant=1)
    pages: dict[int, list[tuple[str, float, float]]] = {
        1: [
            ("XYZ-301T", 300.0, 300.0),
            ("LOUNGE", 300.0, 340.0),
            ("GFI", 360.0, 310.0),
            ("WINDOW WALL", 300.0, 700.0),
            ("XYZ-302T", 900.0, 300.0),
            ("PANTRY", 900.0, 340.0),
            ("ISSUED 2026/04/14", 1050.0, 760.0),
        ],
        2: [("XYZ-301T", 500.0, 300.0), ("ELEVATION 4", 480.0, 420.0)],
    }
    for page in range(1, 6):
        c.setFont("Helvetica", 9)
        for words, x, y in pages.get(page, [("ST-01 QUARTZ", 100.0, 700.0)]):
            c.drawString(x, HEIGHT - y, words)
        c.showPage()
    c.save()
    return out.getvalue()


def _joined_vendor_pdf() -> bytes:
    """A made-up joined shop file: page 1 the vendor's cabinet sheet (its title names the pantry,
    dated before the design's issue), page 2 GV's stone-top sheet of the same item."""
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(WIDTH, HEIGHT), invariant=1)
    pages = {
        1: [
            ("XYZ-301", 300.0, 300.0),
            ('36"', 300.0, 330.0),
            ("PANTRY A", 1000.0, 700.0),
            ("DATE 03/01/2026", 1000.0, 720.0),
        ],
        2: [
            ("XYZ-301T", 300.0, 300.0),
            ('97 1/2"', 300.0, 330.0),
            ("SINK ABC-UM-12", 300.0, 360.0),
            ("JOINT", 420.0, 360.0),
            ("STONE TOP", 1000.0, 700.0),
        ],
    }
    for page in (1, 2):
        c.setFont("Helvetica", 9)
        for words, x, y in pages[page]:
            c.drawString(x, HEIGHT - y, words)
        c.showPage()
    c.save()
    return out.getvalue()


def test_the_design_room_its_views_and_the_cross_checks_are_in_the_first_message(
    tmp_path: Path,
) -> None:
    design = "Design set.pdf"
    joined = [
        {"name": "Cabinets.pdf", "first_page": 1, "pages": 1},
        {"name": "Tops.pdf", "first_page": 2, "pages": 1},
    ]
    listing = [
        {"name": FILE, "pages": 2, "kind": "the vendor's shop drawings", "joined_from": joined},
        {"name": design, "pages": 5, "kind": "the architect's drawings"},
    ]
    uploads = {FILE: _joined_vendor_pdf(), design: _room_design_pdf()}
    _engine, first = _first_message(tmp_path, uploads, listing, page=1)
    words = " ".join(b.get("text", "") for b in first if "text" in b)
    # The plan's room (a wall 400 points off the tag is listed now) and the elevation sheet.
    assert f"XYZ-301: design page 1 of {design} (a plan)" in words
    assert "WINDOW WALL" in words and "GFI" in words
    assert f"XYZ-301: design page 2 of {design} (an elevation, section or detail sheet)" in words
    at = next(i for i, b in enumerate(first) if "design_reference" in b.get("text", ""))
    design_pictures = [
        b for b in first[at + 1 :] if b["type"] == "image"
    ]  # the room, then the elevation
    assert len(design_pictures) == 2
    # The item number against the rooms, the stone-top sheet of the same item, the dates: each
    # fenced as drawing data.
    blocks = {
        source: next(b["text"] for b in first if f'source="{source}"' in b.get("text", ""))
        for source in ("item_room", "other_shop_file", "sheet_dates")
    }
    room = blocks["item_room"]
    assert "The design prints XYZ-301 in LOUNGE (design p1); the room the sheet names" in room
    assert "The design's PANTRY holds XYZ-302T." in room
    other = blocks["other_shop_file"]
    assert "p2 (from Tops.pdf) prints XYZ-301T (this page: XYZ-301)" in other
    assert '97 1/2"' in other and "ABC-UM-12" in other and "Seams (JOINT) marked: 1" in other
    assert "STONE TOP" in other
    assert (
        "This sheet is dated 03/01/2026; the design set's latest revision is dated 2026/04/14"
        in blocks["sheet_dates"]
    )
    # The dimension rows and the start note still close the message.
    assert first[-1]["text"].startswith("The nine tiles are above")


def test_a_single_file_upload_with_no_design_adds_no_cross_check(tmp_path: Path) -> None:
    listing = [{"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"}]
    _engine, first = _first_message(tmp_path, {}, listing)
    text = json.dumps(first)
    for source in ("item_room", "other_shop_file", "sheet_dates", "design_reference"):
        assert f'source=\\"{source}\\"' not in text


# ---- code's own sizes on a hand-drawn page, the size gate, the warm-up (2026-10-31) ---------------


def _stone_and_pen_pdf() -> bytes:
    """A made-up joined shop file: page 1 GV's typed stone-top sheet (its top 50 1/4"), page 2 the
    vendor's cabinet sheet drawn in pen strokes: no size on its text layer, only words."""
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(WIDTH, HEIGHT), invariant=1)
    pages = {
        1: [("STONE TOP", 1000.0, 700.0), ('50 1/4"', 300.0, 330.0)],
        2: [("CABINET ELEVATION", 900.0, 200.0), ("ITEM 101", 1100.0, 760.0)],
    }
    for page in (1, 2):
        c.setFont("Helvetica", 9)
        for words, x, y in pages[page]:
            c.drawString(x, HEIGHT - y, words)
        c.line(100, 400, 600, 400)
        c.showPage()
    c.save()
    return out.getvalue()


#: Code's labels of page 2 (`dims.json`): two pen-stroke sizes on one row, one read by OCR at its
#: spot, one of the text layer's own and one code worked out (an equal part: not printed).
PEN_LABELS: list[dict[str, Any]] = [
    {"id": "D1", "text": '49 1/2"', "inches": "49.5", "box": [300, 320, 330, 332],
     "orientation": "h", "route": "drawn"},
    {"id": "D2", "text": '12"', "inches": "12", "box": [400, 321, 415, 333],
     "orientation": "h", "route": "drawn"},
    {"id": "D3", "text": '4"', "inches": "4", "box": [100, 500, 108, 512],
     "orientation": "v", "route": "drawn", "origin": "ocr"},
    {"id": "D4", "text": '7"', "inches": "7", "box": [700, 600, 710, 610],
     "orientation": "h", "route": "text"},
    {"id": "D5", "text": "EQ", "inches": "6", "box": [800, 600, 815, 610],
     "orientation": "h", "route": "drawn", "derived": {"from": "D2"}},
]  # fmt: skip


def _pen_review(
    tmp_path: Path, turns: Sequence[Any], labels: list[dict[str, Any]] = PEN_LABELS
) -> tuple[_Engine, PageReview, Reviews]:
    """Page 2 of the made-up joined file reviewed with `labels` in its set pack's `dims.json`."""
    engine = _Engine(tmp_path, _Script(turns, 100_000))
    pack = tmp_path / "pack"
    pack.mkdir()
    entry = {"file": FILE, "page": 2, "labels": labels}
    (pack / "dims.json").write_text(json.dumps({"pages": [entry]}), encoding="utf-8")
    engine.workspace = ChatWorkspace.open(tmp_path / "chat", set_pack=pack)
    (engine.workspace.uploads / FILE).write_bytes(_stone_and_pen_pdf())
    listing = [{"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"}]
    engine.tools = ChatTools(
        workspace=engine.workspace,
        sandbox=FakeSandbox(engine.workspace, lambda command, _w: ("ok", 0)),
        page_count=2,
        uploads=listing,
    )
    review = PageReview(page=2, file=FILE, options=Options())
    reviews = Reviews()
    reviews.pages["page-review-p2"] = review
    agent = SimpleNamespace(
        model=MODELS["claude-opus-5-5"], cap_micros=900_000, spent=0, last_call=0
    )
    page_reviewer._Run(engine, agent, review, reviews, {"steps": []}, 0).run()  # type: ignore[arg-type]
    return engine, review, reviews


def _pen_issue(read: str, claim: str = "please_check") -> dict[str, Any]:
    return _tool(
        "report_issue", kind="A", claim=claim, item="Cabinet top vs the stone top",
        wrong="The cabinet top and GV's stone top (p1) differ.", read=read,
        box={"x0": 290, "y0": 310, "x1": 340, "y1": 340}, confidence="medium",
    )  # fmt: skip


def test_a_hand_drawn_page_gets_the_sizes_code_read_with_their_ids_and_rows(
    tmp_path: Path,
) -> None:
    engine, review, _reviews = _pen_review(tmp_path, [[_text("Nothing to report.")]])
    first = engine.script.calls[0].messages[0]["content"]
    (labels,) = [b["text"] for b in first if 'source="code_labels"' in b.get("text", "")]
    assert 'D1  300,320,330,332  49 1/2"  (pen strokes)' in labels
    assert 'D3  100,500,108,512  4"  (read at its spot, two OCR engines)' in labels
    # The text layer's own label is listed with the text layer; a worked-out one is not printed.
    assert "D4" not in labels and "D5" not in labels
    assert [label["id"] for label in review.labels] == ["D1", "D2", "D3"]
    (rows,) = [b["text"] for b in first if 'source="dimension_strings"' in b.get("text", "")]
    assert 'D1 49 1/2" + D2 12" = 61 1/2"' in rows
    # A typed page (no label off its drawing) has no such block: its opening is as before.
    typed, *_ = _review(tmp_path / "typed", [[_text("Nothing to report.")]])
    assert "code_labels" not in json.dumps(typed.script.calls[0].messages[0])


def test_reading_another_page_never_turns_an_accepted_size_issue_into_a_refused_one(
    tmp_path: Path,
) -> None:
    """2026-10-31: on a hand-drawn page, 'vendor top vs GV stone top' was accepted while no other
    page was read and refused once the stone page was read (its number was printed there, the
    vendor's only in pen strokes). Reading a page now only adds sizes."""
    quote = '49 1/2" ; stone top 50 1/4" (p1)'
    tiles_then = [[_tiles(1, 2, 3, 4, 5, 6)], [_tiles(7, 8, 9)], [_pen_issue(quote)], [_text("ok")]]
    _engine, unread, _ = _pen_review(tmp_path / "unread", tiles_then)
    read_first = [[_tiles(1, 2, 3, 4, 5, 6), _tool("set_page_text", page=1)], *tiles_then[1:]]
    _engine, read, reviews = _pen_review(tmp_path / "read", read_first)
    assert [d.kind for d in unread.drafts] == ["A"]
    assert [d.kind for d in read.drafts] == ["A"]
    assert read.refusals == {}
    assert reviews.block()["pages"][0]["code_labels"] == 3


def test_a_size_issue_on_a_hand_drawn_page_still_needs_a_number_code_read(
    tmp_path: Path,
) -> None:
    turns = [
        [_tiles(1, 2, 3, 4, 5, 6)],
        [_tiles(7, 8, 9)],
        [_pen_issue('10" ; 11"', claim="wrong")],
        [_text("ok")],
    ]
    engine, review, reviews = _pen_review(tmp_path, turns)
    assert review.drafts == []
    assert any(
        "does not find any of the numbers" in r for r in _results(engine.script.calls[3].messages)
    )
    # Each refusal is counted by its reason in the run's log.
    (row,) = reviews.block()["pages"]
    assert row["refused"] == {"report_issue: size: no quoted number read by code": 1}


def test_the_cache_warm_up_sends_the_reviewers_exact_prefix_once(tmp_path: Path) -> None:
    import contextlib

    options = Options(reference_pictures=_picture_folder(tmp_path))
    engine = _Engine(tmp_path, _Script([[_text("OK")], [_text("Nothing to report.")]], 1_000))
    engine._fits = lambda _estimate: True  # type: ignore[attr-defined]
    engine.timings = SimpleNamespace(phase=lambda _name: contextlib.nullcontext())  # type: ignore[attr-defined]
    reviews = Reviews()
    model = MODELS["claude-opus-5-5"]
    page_reviewer.prewarm(engine, reviews, model, options)
    review = PageReview(page=2, file=FILE, options=options)
    agent = SimpleNamespace(model=model, cap_micros=600_000, spent=0, last_call=0)
    page_reviewer._Run(engine, agent, review, reviews, {"steps": []}, 0).run()  # type: ignore[arg-type]
    warm, first = engine.script.calls[:2]
    assert warm.max_tokens == page_reviewer.WARM_TOKENS
    for name in ("effort", "thinking", "system", "tools", "context_management"):
        assert getattr(warm, name) == getattr(first, name), name
    # GV's pictures, the same ids, then the breakpoint: the page's own blocks follow it.
    shared = warm.messages[0]["content"][:-1]
    assert shared and any(b["type"] == "image" for b in shared)
    assert first.messages[0]["content"][: len(shared)] == shared
    assert shared[-1]["cache_control"] == {"type": "ephemeral"}
    # Each call's tokens and the first call's alone are kept for the run's log.
    assert set(review.tokens) == {"input", "output", "cache_write", "cache_read"}
    assert set(review.first_call) == set(review.tokens)
    # Never where the calls keep separate caches.
    made = len(engine.script.calls)
    engine.provider = SimpleNamespace(name="somewhere-else")
    page_reviewer.prewarm(engine, reviews, model, options)
    assert len(engine.script.calls) == made


def test_the_warm_up_is_for_routes_that_share_one_cache() -> None:
    from app.review.assistant.chat_request import cache_shared

    opus = MODELS["claude-opus-5-5"]
    assert cache_shared("anthropic", opus)
    # OpenRouter pins Claude to one host with no fallbacks: one cache.
    assert cache_shared("openrouter", opus)
    assert not cache_shared(None, opus)
    outside = next((m for m in MODELS.values() if m.extra), None)
    if outside is not None:
        assert not cache_shared("openrouter", outside)


# ---- the gates since 2026-11-01 (plan M4, M5, M11) --------------------------------------------------


def _red_and_check(read: str, claim: str, **more: Any) -> dict[str, Any]:
    given = {
        "kind": "A", "claim": claim, "item": "Elevation: overall", "confidence": "medium",
        "wrong": "The overall does not match the other view.", "read": read,
        "box": {"x0": 250, "y0": 315, "x1": 300, "y1": 340},
        **more,
    }  # fmt: skip
    return _tool("report_issue", **given)


def test_a_size_please_check_is_kept_with_one_printed_number_or_at_low_confidence(
    tmp_path: Path,
) -> None:
    """GV's 'what is this dimension for?' quotes one number: a please-check is never refused for
    its numbers; with none code finds printed it is kept at low confidence, saying so."""
    engine, review = _after_tiles(
        tmp_path,
        _red_and_check('36 1/4"', "please_check"),  # one printed number: kept as it is
        _red_and_check('about 30"', "please_check", box={"x0": 600, "y0": 600, "x1": 650, "y1": 640}),
        _red_and_check("", "please_check", box={"x0": 800, "y0": 600, "x1": 850, "y1": 640}),
    )  # fmt: skip
    assert not any(r.startswith("Refused") for r in _results(engine.script.calls[3].messages))
    one, unprinted, unquoted = review.drafts
    assert one.confidence == "medium" and page_reviewer.NO_PRINTED_NUMBER not in one.wrong
    for draft in (unprinted, unquoted):
        assert draft.confidence == "low" and draft.claim == "please_check"
        assert draft.wrong.endswith(page_reviewer.NO_PRINTED_NUMBER)
    for found in engine.book.live():
        assert found["status"] == "check"
        assert proof.proof_group(found) == "likely_problem"


def test_a_red_still_needs_two_printed_numbers_where_a_please_check_needs_one(
    tmp_path: Path,
) -> None:
    """Zero false reds: the two-numbers rule still guards every red."""
    red, _ = _after_tiles(tmp_path / "red", _red_and_check('36 1/4" vs 30"', "wrong"))
    (refused,) = [r for r in _results(red.script.calls[3].messages) if r.startswith("Refused")]
    assert "does not find two of the numbers" in refused and "claim please_check" in refused
    assert not [f for f in red.book.live() if f["created_by"] == "page_reviewer"]
    _check, review = _after_tiles(
        tmp_path / "check", _red_and_check('36 1/4" vs 30"', "please_check")
    )
    assert [(d.claim, d.confidence) for d in review.drafts] == [("please_check", "medium")]


def test_a_red_refused_twice_at_one_spot_is_kept_as_a_low_please_check_never_red(
    tmp_path: Path,
) -> None:
    string = {"parts": ['10"', '10"'], "printed_total": '21"'}  # nothing of it printed here
    red = _red_and_check('10" + 10" ; 21"', "wrong", **string)
    engine, review = _after_tiles(tmp_path, red, red, red)
    refused = [r for r in _results(engine.script.calls[3].messages) if r.startswith("Refused")]
    assert len(refused) == 2
    (draft,) = review.drafts
    assert (draft.claim, draft.confidence) == ("please_check", "low")
    assert draft.wrong.endswith(page_reviewer.NO_TWO_PRINTED)
    (found,) = [f for f in engine.book.live() if f["created_by"] == "page_reviewer"]
    assert found["status"] == "check" and found.get("source") != "vendor_arithmetic"
    assert proof.proof_group(found) == "likely_problem"
    assert [r["reason"] for r in review.refused_items] == [
        "size: two quoted numbers not printed"
    ] * 2


def test_every_refused_report_and_decision_is_kept_short_for_the_log(tmp_path: Path) -> None:
    long_words = "The table lists no such finish. " * 12  # about 380 characters
    broad = _issue(kind="D", wrong=long_words, box={"x0": 100, "y0": 100, "x1": 700, "y1": 500})
    early = _tool("resolve_finding", n=1, decision="drop", why="a misread")
    _engine, review, _ended, reviews = _review(
        tmp_path,
        [[_tiles(1, 2, 3, 4, 5, 6)], [_tiles(7, 8, 9)], [broad, early], [_text("ok")]],
        cap=900_000,
        cost=100_000,
    )
    box_row, resolve_row = review.refused_items
    assert box_row["tool"] == "report_issue" and box_row["reason"] == "box: too big"
    assert box_row["kind"] == "D" and box_row["item"] == "Elevation: a callout"
    assert len(box_row["wrong"]) == page_reviewer.LOG_TEXT_CHARS
    assert box_row["box"] == [100.0, 100.0, 700.0, 500.0]
    assert resolve_row["tool"] == "resolve_finding" and resolve_row["why"] == "a misread"
    reviews.pages["p2"] = review
    (row,) = reviews.block()["pages"]
    assert row["refused_items"] == review.refused_items
    # At most 40 a page.
    _many, review2 = _after_tiles(tmp_path / "many", *[broad] * 45)
    assert len(review2.refused_items) == page_reviewer.MAX_REFUSED_ITEMS


def test_a_box_too_big_is_snapped_to_the_printed_tag_it_quotes(tmp_path: Path) -> None:
    engine, review = _after_tiles(
        tmp_path,
        _issue(kind="D", wrong="The top is tagged ST-01; the table lists no ST-01.",
               box={"x0": 500, "y0": 300, "x1": 1000, "y1": 600}),  # 500 across, around ST-01
        _issue(kind="D", wrong="The top is tagged ST-01; the table lists no ST-01.",
               box={"x0": 100, "y0": 450, "x1": 600, "y1": 700}),  # 500 across, ST-01 not in it
    )  # fmt: skip
    (draft,) = review.drafts
    assert draft.snapped
    assert draft.box == pytest.approx((694.0, 406.9, 730.5, 427.9))  # ST-01's phrase, padded 6
    (refused,) = [r for r in _results(engine.script.calls[3].messages) if r.startswith("Refused")]
    assert "give anchor" in refused and "500 points across" in refused


def test_an_anchor_is_boxed_by_code_and_the_models_box_is_only_a_hint(tmp_path: Path) -> None:
    _engine, review = _after_tiles(
        tmp_path,
        _issue(kind="F", wrong="The item number differs from the title.", anchor="ITEM 101",
               box={"x0": 1000, "y0": 650, "x1": 1200, "y1": 700}),  # near, not on it
        _issue(kind="A", wrong="A part of the string is unclear.", anchor='12" 12" 11 3/4"',
               box={"x0": 190, "y0": 280, "x1": 360, "y1": 310}),  # the parts of a row
        _issue(kind="F", wrong="Unknown words.", anchor="NOT ON THE PAGE",
               box={"x0": 300, "y0": 300, "x1": 420, "y1": 340}),  # no match: the box as given
    )  # fmt: skip
    item, row, unmatched = review.drafts
    assert item.snapped and item.box == pytest.approx((1094.0, 746.9, 1145.0, 767.9))
    assert row.snapped and row.box == pytest.approx((194.0, 286.9, 354.2, 307.9))
    assert not unmatched.snapped and unmatched.box == (300.0, 300.0, 420.0, 340.0)
    assert "anchor" in page_reviewer.REPORT_ISSUE["input_schema"]["properties"]


def test_a_thin_strip_is_tight_by_its_short_side(tmp_path: Path) -> None:
    engine, review = _after_tiles(
        tmp_path,
        _issue(kind="D", box={"x0": 100, "y0": 500, "x1": 450, "y1": 520}),  # 350 x 20: a strip
        _issue(kind="D", box={"x0": 100, "y0": 600, "x1": 450, "y1": 640}),  # 350 x 40: broad
    )  # fmt: skip
    assert [round(d.box[2] - d.box[0]) for d in review.drafts] == [350]
    (refused,) = [r for r in _results(engine.script.calls[3].messages) if r.startswith("Refused")]
    assert "350 points across" in refused


@pytest.mark.parametrize("kind", ["B", "C", "E"])
def test_construction_completeness_and_scope_may_box_an_area_up_to_a_view(
    tmp_path: Path, kind: str
) -> None:
    engine, review = _after_tiles(
        tmp_path,
        _issue(kind=kind, wrong="Something is missing in this area.",
               box={"x0": 100, "y0": 100, "x1": 650, "y1": 500}),  # 550: a view, no flag
        _issue(kind=kind, wrong="Something is missing anywhere.",
               box={"x0": 0, "y0": 0, "x1": 1200, "y1": 700}),  # the page: refused
    )  # fmt: skip
    assert [round(d.box[2] - d.box[0]) for d in review.drafts] == [550]
    assert any("1200 points across" in r for r in _results(engine.script.calls[3].messages))


def test_two_problems_at_one_spot_stay_two_and_the_same_problem_folds_once(
    tmp_path: Path,
) -> None:
    spot = {"x0": 690, "y0": 405, "x1": 740, "y1": 430}
    engine, review = _after_tiles(
        tmp_path,
        _issue(kind="D", item="Material table: ST-01 code", box=spot,
               wrong="ST-01 is not in the material table."),
        _issue(kind="D", item="Grain arrows on the stone", box=spot,
               wrong="Grain arrows are drawn on a stone top."),  # another problem: kept
        _issue(kind="D", item="Material table: ST-01 code missing", box=spot,
               wrong="The table has no ST-01 row."),  # alike words: folded
    )  # fmt: skip
    first, second, third = review.drafts
    mine = [f for f in engine.book.live() if f["created_by"] == "page_reviewer"]
    assert len(mine) == 2 and second.joined is None and second.related_to == first.finding_id
    assert third.joined == first.finding_id
    (fold,) = review.folds
    assert fold["how"] == "same kind, alike words" and fold["into"] == first.finding_id
    assert (
        "ST-01 code missing" in fold["new"] and "ST-01 is not in the material table" in fold["kept"]
    )


def test_another_kind_folds_only_on_the_same_quoted_numbers_at_the_same_spot(
    tmp_path: Path,
) -> None:
    spot = {"x0": 255, "y0": 318, "x1": 295, "y1": 336}
    engine, review = _after_tiles(
        tmp_path,
        _red_and_check('36 1/4"', "please_check", box=spot),
        _issue(kind="F", item="Elevation: overall label", read='36 1/4"', box=spot,
               wrong="The overall's label is unreadable."),  # same quote, same spot: folded
        _issue(kind="F", item="Elevation: view title", read="SECTION A", box=spot,
               wrong="The view title is missing."),  # other words, other kind: kept
    )  # fmt: skip
    size, label, title = review.drafts
    assert label.joined == size.finding_id and title.joined is None
    assert [f["how"] for f in review.folds] == ["another kind, same quoted numbers or tags"]
    assert len([f for f in engine.book.live() if f["created_by"] == "page_reviewer"]) == 2
