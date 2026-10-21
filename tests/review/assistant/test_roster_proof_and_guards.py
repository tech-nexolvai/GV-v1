"""#1819: a roster full check's greens and reds as the simple check proves them, and two guards
so the early stop never comes back.

- A `same` item counts as looked at when code itself read the same value at both labels from the
  drawings' text; nothing else is loosened.
- Where code's geometry cannot tell, only the stronger blind pick (code's own mark, never the
  model's) may settle a pairing; geometry that shows different points always blocks.
- Guard 1: no prompt or brief any agent gets carries a made-up budget.
- Guard 2: pages still not compared while money is left go to another pass; a page left
  unchecked for money says "out of money" on it.

Every drawing value here is made up.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import full_check_roster
from app.review.assistant.chat_trust import code_read_both, not_compared_why
from app.review.assistant.full_check_advocate import STRONG_PICK, Check, decide
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_engine import SONNET, Harness
from tests.review.assistant.test_full_check_roster import (
    _clean_ink,
    _Lazy,
    _roster,
)

# ---- (a) code read both values: looked at, by code ----------------------------------------------


def _same(**given: Any) -> dict[str, Any]:
    label = {"page": 2, "file": "shop.pdf", "route": "text"}
    finding = {
        "id": "F1",
        "page": 2,
        "vs_page": 1,
        "status": "same",
        "architect": '31 1/4"',
        "vendor": '31 1/4"',
        "arch_box": {"page": 1, "x0": 10, "y0": 10, "x1": 40, "y1": 20},
        "vendor_box": {"page": 2, "x0": 10, "y0": 10, "x1": 40, "y1": 20},
        "checks": {"printed_architect": "match", "printed_vendor": "match"},
        "dims": {
            "vendor": {**label, "id": "D3"},
            "architect": {**label, "id": "D3", "page": 1, "file": "arch.pdf"},
        },
    }
    finding.update(given)
    return finding


_COVERED = {"pages": [{"page": 2, "status": "same"}]}


def test_a_same_item_code_read_on_both_sides_counts_as_looked_at() -> None:
    finding = _same()
    assert code_read_both(finding)
    # No sharp enough look in the zoom log (the architect's sheet seen whole, blurry): still Right.
    assert not_compared_why(finding, _COVERED, seen=[]) is None


@pytest.mark.parametrize(
    "change",
    [
        {"checks": {"printed_architect": "not_found", "printed_vendor": "match"}},
        {"checks": {"printed_architect": "match", "printed_vendor": "not_a_number"}},
        {"vendor": '31 1/2"'},  # two values: never Right
        {"dims": None},  # a typed value with a box, no label
        {
            "dims": {
                "vendor": {"id": "D3", "page": 2, "route": "drawn"},
                "architect": {"id": "D3", "page": 1, "route": "text"},
            }
        },  # read from drawn ink only
        {"architect": "SINK CABINET", "vendor": "same callouts"},  # words, no value
    ],
)
def test_anything_less_than_code_reading_both_values_still_needs_a_look(
    change: Mapping[str, Any],
) -> None:
    finding = _same(**change)
    assert not code_read_both(finding)
    assert not_compared_why(finding, _COVERED, seen=[]) == "not_viewed"


def test_code_reading_never_covers_a_page_pair_not_compared() -> None:
    finding = _same()
    assert not_compared_why(finding, {"pages": [{"page": 2, "status": "not_opened"}]}, []) == (
        "pair_not_covered"
    )


# ---- (b) geometry that cannot tell: the strong blind pick ---------------------------------------

_SPANS = {"from": "back_of_cabinet", "to": "sink_cutout"}
_AGREE = {
    "pick": "p1.D7",
    "vendor_spans": _SPANS,
    "architect_spans": _SPANS,
    "vendor_ink_black": "yes",
}
_CANNOT = ("cannot_tell", "a dimension line is off its drawing's scale")
_COMMON: dict[str, Any] = {
    "mode": "ai_pair_plus_code",
    "checks": [Check(n, "pass") for n in ("ink", "annotation", "roles", "axis", "round")],
    "printed": True,
    "helper_pick": "p1.D7",
    "code_paired": False,
}


def test_a_strong_pick_settles_a_pairing_code_geometry_cannot_tell() -> None:
    verdict = decide(advocate={**_AGREE, STRONG_PICK: True}, extent=_CANNOT, **_COMMON)
    assert verdict.result == "passed" and verdict.answers["pairing"] == "proved_by_two_picks"
    assert verdict.answers["extent"] == "cannot_tell"


@pytest.mark.parametrize(
    ("advocate", "extent", "words"),
    [
        ({**_AGREE, STRONG_PICK: False}, _CANNOT, "off its drawing's scale"),
        (_AGREE, _CANNOT, "off its drawing's scale"),  # an unmarked pick is not the strong one
        # No pick made (the price): said as not verified, never as the geometry's reason (#1829).
        (None, _CANNOT, "Not verified within the fixed price: the blind pick was not made"),
        ({**_AGREE, STRONG_PICK: True, "pick": "p1.D9"}, _CANNOT, "chose p1.D9"),
        (
            {**_AGREE, STRONG_PICK: True, "architect_spans": {"from": "wall", "to": "sink_cutout"}},
            _CANNOT,
            "land in different places",
        ),
        ({**_AGREE, STRONG_PICK: True, "vendor_ink_black": "no"}, _CANNOT, "in colour"),
        (
            {**_AGREE, STRONG_PICK: True},
            ("different_extent", "the ends land apart"),
            "the ends land apart",
        ),
    ],
)
def test_anything_less_keeps_the_pairing_not_proved(
    advocate: Mapping[str, Any] | None, extent: tuple[str, str], words: str
) -> None:
    verdict = decide(advocate=advocate, extent=extent, **_COMMON)
    assert verdict.result == "objects" and words in verdict.reason, verdict.reason


def test_the_strong_mark_is_codes_never_the_models(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    asked: list[tuple[str, str]] = []

    def held(self: Any, finding: Any, vendor: Any, architect: Any, page_map: Any, model: Any,
             effort: str = "medium") -> dict[str, Any]:  # fmt: skip
        asked.append((model.id, effort))
        return {**_AGREE, STRONG_PICK: True}  # the model claims to be the strong pick

    monkeypatch.setattr(full_check_roster._Run, "_held_pick", held)
    monkeypatch.setattr(full_check_roster._Run, "_phase_room", lambda self, phase, money: True)
    engine = SimpleNamespace(_price=lambda model, prompt, out: 0)
    run = full_check_roster._Run(engine=engine)  # type: ignore[arg-type]
    weak = run._blind_pick({}, {}, {}, None)
    strong = run._blind_pick({}, {}, {}, None, strong=True)
    assert weak is not None and weak[STRONG_PICK] is False
    assert strong is not None and strong[STRONG_PICK] is True
    assert asked == [("claude-sonnet-5-5", "medium"), ("claude-opus-5-5", "high")]


# ---- guard 1: no made-up budget in any prompt ---------------------------------------------------

#: A budget the code did not compute: "about 12 tool calls", "Budget:", a dollar figure ("$0", a
#: tool that costs nothing, is a fact, not a budget).
_MADE_UP = re.compile(
    r"about \d+ tool calls|\bBudget\s*:|\$\s?(?:[1-9]|0[.,]\d*[1-9])", re.IGNORECASE
)


def _texts(params: Mapping[str, Any]) -> list[str]:
    out = [block["text"] for block in params.get("system") or [] if block.get("text")]
    for message in params.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            out.append(content)
            continue
        for block in content or []:
            if block.get("type") == "text":
                out.append(str(block.get("text") or ""))
    return out


def test_no_roster_prompt_carries_a_made_up_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every call of a whole roster check (page helpers, drawn-things readers, tag reader, gap
    closers, the lead review, the verifier, the look-again, the advocate, the summary)."""
    _clean_ink(monkeypatch)
    harness = _roster(tmp_path, _Lazy())  # pages are left, so every later pass runs too
    harness.run()
    sent = [json.loads(raw) for raw in harness.provider.calls]
    assert len(sent) > 10
    kinds = " ".join(_texts(params)[1] if len(_texts(params)) > 1 else "" for params in sent)
    for start in ("TAGS:", "LEAD REVIEW", "PROVE:", full_check_roster.GAP_SCHEMA_NOTE):
        assert start in kinds, start  # each kind of agent was asked
    for params in sent:
        for words in _texts(params):
            found = _MADE_UP.search(words)
            assert found is None, (found.group(0), words[max(0, found.start() - 80) :][:200])


def test_no_chat_helper_prompt_carries_a_made_up_budget(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("ag1", "Agent", {"description": "Pages 1-2", "prompt": "Check pages 1-2"}),
                stop="tool_use",
            ),
            reply(text("Done.")),
        ],
        helpers={"Check pages 1-2": [reply(text("Page 2: sink differs."), model=SONNET)]},
    )
    harness = Harness(tmp_path, provider)
    harness.run()
    for raw in provider.calls:
        for words in _texts(json.loads(raw)):
            assert _MADE_UP.search(words) is None, words[:300]


# ---- guard 2: pages left while money is left go on; "out of money" only when it is ------------


class _SecondLook(_Lazy):
    """The page helpers and the first gap closers leave their pages; a later closer compares
    them."""

    def __init__(self) -> None:
        super().__init__()
        self.closers: dict[int, int] = {}

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        if not brief.startswith(full_check_roster.GAP_SCHEMA_NOTE):
            return super().helper(params, brief)
        self._seen("gap", params)
        page = int(brief.split(" on page", 1)[1].lstrip("s ").split()[0].rstrip(",:"))
        with self.lock:
            self.closers[page] = self.closers.get(page, 0) + 1
            second = self.closers[page] > 1
        if not second or any(m["role"] == "assistant" for m in params["messages"]):
            return reply(text(f"Closed.\nCHECKLIST p{page}: architect 1/1; strings 0/0; not compared: none"), model=SONNET)  # fmt: skip
        return reply(
            tool("cov", "set_coverage", {"pages": [{"page": page, "status": "same"}]}),
            stop="tool_use",
            model=SONNET,
        )


def _coverage(outcome: Any) -> dict[int, Mapping[str, Any]]:
    blocks = [b for b in outcome.blocks if b.get("type") == "coverage"]
    return {int(p["page"]): p for p in blocks[-1]["coverage"]["pages"]}


def test_pages_left_with_money_left_go_to_another_pass_and_get_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _SecondLook()
    harness = _roster(tmp_path, script)
    outcome = harness.run()
    price = harness.engine.settings.message_cap_micros
    assert harness.engine.spent < price * 3 // 4  # more than a quarter of the price was left
    assert all(n >= 2 for n in script.closers.values()), script.closers  # sent on again
    pages = _coverage(outcome)
    assert {pages[p]["status"] for p in (2, 3, 4, 5)} == {"same"}


class _NotComparable(_Lazy):
    """#1825: every numbers helper marks its pages "not comparable" with a plain note (no "not
    reached" words), as the paid runs of 2026-10-21 did on a combined sheet with no heading; the
    pass after the lead compares them."""

    def __init__(self) -> None:
        super().__init__()
        self.again: dict[int, str] = {}

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        if brief.startswith(full_check_roster.GAP_SCHEMA_NOTE):
            self._seen("gap", params)
            page = int(brief.split(" on page", 1)[1].lstrip("s ").split()[0].rstrip(",:"))
            if "marked not_comparable" not in brief or any(
                m["role"] == "assistant" for m in params["messages"]
            ):
                return reply(text("Closed."), model=SONNET)
            with self.lock:
                self.again[page] = brief
            return reply(
                tool("cov", "set_coverage", {"pages": [{"page": page, "status": "same"}]}),
                stop="tool_use",
                model=SONNET,
            )
        drawn = brief.startswith("OBJECTIVE: the drawn things")
        if drawn or brief.startswith(("TAGS:", "PROVE:")):
            return super().helper(params, brief)
        self._seen("numbers", params)
        pages = [int(n) for n in brief.split("on pages ", 1)[1].split(" of ", 1)[0].split(", ")]
        if any(m["role"] == "assistant" for m in params["messages"]):
            lines = [
                f"CHECKLIST p{p}: architect 0/0; strings 0/0; not compared: none" for p in pages
            ]
            return reply(text("\n".join(lines)), model=SONNET)
        entries = [
            {"page": p, "status": "not_comparable", "note": "vendor sheet only, pairing uncertain"}
            for p in pages
        ]
        return reply(tool("cov", "set_coverage", {"pages": entries}), stop="tool_use", model=SONNET)


def test_pages_marked_not_comparable_with_money_left_go_to_another_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _NotComparable()
    harness = _roster(tmp_path, script)
    outcome = harness.run()
    assert set(script.again) == {2, 3, 4, 5}, sorted(script.again)
    assert all("vendor sheet only, pairing uncertain" in b for b in script.again.values())
    pages = _coverage(outcome)
    assert {pages[p]["status"] for p in (2, 3, 4, 5)} == {"same"}


def test_a_page_left_unchecked_for_money_says_out_of_money(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    real_room = full_check_roster._Run._room

    def room(self: Any, phase: str) -> int:
        if phase == "lead" and self.lead_taken:
            return full_check_roster.MIN_GAP_RUN_MICROS - 1  # after the lead: really short
        return int(real_room(self, phase))

    monkeypatch.setattr(full_check_roster._Run, "_room", room)
    outcome = _roster(tmp_path, _Lazy()).run()
    pages = _coverage(outcome)
    for page in (2, 3, 4, 5):
        assert pages[page]["status"] not in ("same", "different")
        assert "out of money" in str(pages[page].get("note")), pages[page]
    assert "out of money" in outcome.text


def test_no_page_is_left_unchecked_without_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closers that never compare: the passes stop (bounded), and each page says why."""
    _clean_ink(monkeypatch)
    outcome = _roster(tmp_path, _Lazy()).run()
    for page in (2, 3, 4, 5):
        note = str(_coverage(outcome)[page].get("note"))
        assert "out of money" in note or "more looks" in note, note


@pytest.mark.parametrize(
    ("spans", "item"),
    [
        ({"from": "floor", "to": "box_above_unit"}, "Market: overall height"),
        (_SPANS, "Lobby: floor to soffit"),
        (_SPANS, "Pantry: top of counter to underside of bulkhead"),
    ],
)
def test_two_picks_alone_never_prove_room_context(spans: Mapping[str, str], item: str) -> None:
    advocate = {**_AGREE, STRONG_PICK: True, "vendor_spans": spans, "architect_spans": spans}
    verdict = decide(advocate=advocate, extent=_CANNOT, item=item, **_COMMON)
    assert verdict.result == "objects" and "not the unit: confirm" in verdict.reason
    # Where code's geometry proves the same extent, the item is judged as before.
    proved = decide(advocate=advocate, extent=("same_extent", "ok"), item=item, **_COMMON)
    assert proved.result == "passed"
