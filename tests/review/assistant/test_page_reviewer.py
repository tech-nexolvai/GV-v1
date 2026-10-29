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
    Options,
    PageReview,
    Reviews,
    check_math,
    coverage_targets,
    phrases,
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


def _pdf() -> bytes:
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(WIDTH, HEIGHT), invariant=1)
    for page in (1, 2):
        c.setFont("Helvetica", 9)
        if page == 2:
            for words, x, y in TEXTS:
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


class _Script:
    """Answers each call from a list of turns: a turn is a list of blocks (and a stop reason), or
    a function of the conversation so far."""

    def __init__(self, turns: Sequence[Any], cost: int = 100_000) -> None:
        self.turns = list(turns)
        self.cost = cost
        self.calls: list[_Call] = []

    def __call__(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        turn = self.turns.pop(0) if self.turns else [_text("Done.")]
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
            )
        )
        content, stop = self.script(messages)
        self.spent += self.script.cost
        self.own_spent += self.script.cost
        return _Reply(
            content=content,
            stop_reason=stop,
            stop_details=None,
            usage={"cost_micros": self.script.cost},
            model="claude-opus-5-5",
        )

    def _show_finding(self, finding: dict[str, Any]) -> None:
        self.shown.append(finding)

    def _show_dropped(self, finding_id: str, reason: str) -> None:
        self.dropped.append(finding_id)


def _review(
    tmp_path: Path, turns: Sequence[Any], *, cap: int = 5_000_000, cost: int = 100_000
) -> tuple[_Engine, PageReview, tuple[str, bool, str | None], Reviews]:
    script = _Script(turns, cost)
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


def test_the_brief_is_short_names_every_kind_and_never_money() -> None:
    brief = system_prompt()
    assert len(brief.split()) < 700
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
    assert found[0][2] > WIDTH / 3  # each a little larger than its cell
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
    assert [p["text"] for p in coverage_targets(found)] == ["SECTION A", '12"']


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


def test_text_first_then_the_whole_page_then_tiles_before_any_report(tmp_path: Path) -> None:
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
    results = _results(engine.script.calls[1].messages)
    assert results[-1].startswith("Refused: not yet: first zoom on tiles [1, 2, 3, 4, 5, 6, 7")
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


def test_the_coverage_check_sends_it_on_at_most_three_times(tmp_path: Path) -> None:
    turns: list[Any] = [[_tiles(1, 2, 3, 4, 5, 6)], [_tiles(7, 8, 9)]]
    turns += [[_text("I am done.")]] * (MAX_NUDGES + 1)
    engine, review, ended, _ = _review(tmp_path, turns)
    nudges = [n for n in _notes(engine.script.calls[-1].messages) if n.startswith("Only ")]
    assert len(nudges) == MAX_NUDGES
    assert "were seen in a tight zoom" in nudges[0] and "ST-01 at" in nudges[0]
    assert review.coverage() < COVERAGE_GOAL
    assert len(engine.script.calls) == 2 + MAX_NUDGES + 1
    assert ended[2] is None  # nothing reported: no self-check, it ends


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


def test_the_self_check_needs_a_fresh_tight_zoom_and_can_drop_or_fix(tmp_path: Path) -> None:
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
            [_tiles(7, 8, 9), _support_issue(), _support_issue(item="Base: end panel", kind="C",
                                                              box={"x0": 605, "y0": 382, "x1": 755, "y1": 438})],
            [_tight_on_text(), _tight_last()],
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
    assert live[0][likely_problems.FIELD]["kind"] == "material"
    assert "no finish tag" in live[0][likely_problems.FIELD]["words"]
    assert len(engine.dropped) == 2  # the dropped one, and the fixed one's first version


# ---- money ----------------------------------------------------------------------------------------


def test_a_money_stop_runs_the_coverage_check_then_a_final_report_turn(tmp_path: Path) -> None:
    """Cap $1: each call costs $0.30 (the worst case is held: $0.01 + 20 micros a token). Its own
    pass ends at $0.50, the review at $0.72; the final turn and the self-check have the rest."""

    def final(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        assert "Time is up for the review itself" in _notes(messages)[-1]
        return [_support_issue()]  # reported in the final turn: no tile gate there

    engine, review, ended, reviews = _review(
        tmp_path,
        [[_tiles(1, 2, 3)], [_tiles(4, 5, 6)], final, [_text("never asked")]],
        cap=1_000_000,
        cost=300_000,
    )
    calls = engine.script.calls
    assert len(calls) == 3
    notes = _notes(calls[1].messages)
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
            "page_lines",
            "check_math",
            "report_issue",
            "resolve_finding",
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
        return [_string_issue(), _support_issue(), _support_issue(item="Same support again")]

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

    asked = script.params["page_review"]
    pages = sorted(
        {
            int(re.search(r"reviewing page (\d+) ", p["messages"][0]["content"][0]["text"]).group(1))  # type: ignore[union-attr]
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
        ('the overall looks short (about 30")', "quotes the two printed numbers"),
        ('24" in plan, 25" in elevation', "does not find two of the numbers"),
    ],
)
def test_a_size_issue_must_quote_two_numbers_printed_on_the_page(
    tmp_path: Path, read: str, why: str
) -> None:
    size = _tool(
        "report_issue", kind="A", claim="please_check", item="Elevation: width",
        wrong="The width differs between views.", read=read, confidence="medium",
        box={"x0": 190, "y0": 285, "x1": 370, "y1": 340},
    )  # fmt: skip
    printed = _tool(
        "report_issue", kind="A", claim="please_check", item="Elevation: overall",
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
