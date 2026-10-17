"""Harness parity with claude.ai (#1548): room in a long chat, set notes, follow-up corrections.

No network, no paid call: the provider is scripted, the sandbox is a fake.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.review.assistant import chat_engine, chat_trust
from app.review.assistant.chat_context import (
    CONTEXT_BETA,
    context_edits,
    history_summary,
    turn_starts,
)
from app.review.assistant.chat_corrections import (
    corrections_by_answer,
    earlier_findings,
    earlier_findings_note,
    parse_ref,
    with_corrections,
)
from app.review.assistant.chat_memory import MAX_NOTES, decision_note, notes_block
from app.review.assistant.chat_request import BINDING_BETA, FALLBACK_BETA, MODELS, call_params
from app.review.assistant.chat_tools import HELPER_TOOLS, LEAD_TOOLS, ChatTools, new_step
from app.review.assistant.sandbox import ChatWorkspace
from tests.review.assistant.chat_fakes import (
    FakeSandbox,
    ScriptedProvider,
    events_named,
    reply,
    text,
    tool,
)
from tests.review.assistant.test_chat_engine import Harness, _sent


@pytest.fixture(autouse=True)
def _no_checks_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_trust, "load_module", lambda name: None)
    monkeypatch.setattr(chat_engine, "load_module", lambda name: None)


# ---- a long chat: context editing ---------------------------------------------------------------


def _question(words: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": words}]}


def _tool_turn(call: str, output: str = "ok") -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "t", "signature": "s"},
                {"type": "tool_use", "id": call, "name": "bash_tool", "input": {"command": "ls"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call, "content": output}],
        },
    ]


def _answer(words: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"type": "text", "text": words}]}


def _chat(turns: int, calls: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for n in range(turns):
        messages.append(_question(f"question {n + 1}"))
        for c in range(calls):
            messages += _tool_turn(f"t{n}-{c}")
        messages.append(_answer(f"answer {n + 1}"))
    return messages


def test_the_clearing_boundary_is_a_turn_start_and_holds_while_an_answer_works() -> None:
    history = _chat(3, 4)
    assert turn_starts(history) == [0, 10, 20]
    current = [*history, _question("question 4")]
    first = context_edits(current, threshold=400_000, keep_turns=2, near=False)
    assert first == {
        "edits": [
            {
                "type": "clear_tool_uses_20250919",
                "trigger": {"type": "input_tokens", "value": 400_000},
                "keep": {"type": "tool_uses", "value": 4},
                "clear_at_least": {"type": "input_tokens", "value": 50_000},
            }
        ]
    }
    # The answer works: two more tool calls. The same 8 old tool uses stay cleared.
    working = [*current, *_tool_turn("a"), *_tool_turn("b")]
    later = context_edits(working, threshold=400_000, keep_turns=2, near=True)
    clear_thinking, clear_tools = later["edits"]
    assert clear_thinking["type"] == "clear_thinking_20251015"  # listed first, as the API asks
    assert clear_tools["keep"] == {"type": "tool_uses", "value": 6}
    total = sum(
        1
        for m in working
        if m["role"] == "assistant"
        for b in m["content"]
        if b["type"] == "tool_use"
    )
    assert total - 6 == total - 2 - 4 == 12 - 4
    assert clear_thinking["keep"] == {"type": "thinking_turns", "value": 7}


def test_the_lead_call_carries_the_context_beta_on_anthropic_only() -> None:
    edits = context_edits([_question("q")], threshold=400_000, keep_turns=2, near=False)
    common: dict[str, Any] = {
        "model": MODELS["claude-opus-5-5"],
        "effort": "high",
        "thinking": {"type": "adaptive", "display": "summarized"},
        "system": "s",
        "tools": [],
        "messages": [_question("q")],
        "final": False,
        "max_tokens": 64000,
    }
    sent = call_params(provider="anthropic", context_management=edits, **common)
    assert sent["betas"] == [BINDING_BETA, FALLBACK_BETA, CONTEXT_BETA]
    assert sent["fallbacks"] == "default"
    assert sent["context_management"] == edits
    haiku = call_params(
        provider="anthropic",
        context_management=edits,
        **{**common, "model": MODELS["claude-haiku-5-5"]},
    )
    assert haiku["betas"] == [BINDING_BETA, CONTEXT_BETA] and "fallbacks" not in haiku
    assert "betas" not in call_params(
        provider="anthropic",
        **{**common, "model": MODELS["claude-haiku-5-5"], "thinking": {"type": "disabled"}},
    )
    openrouter = call_params(provider="openrouter", context_management=edits, **common)
    assert "context_management" not in openrouter and "betas" not in openrouter


def _big_history(turns: int = 2) -> list[dict[str, Any]]:
    messages = _chat(turns, 1)
    messages[2]["content"][0]["content"] = "x" * 300_000  # a long command output
    return messages


def test_every_lead_call_asks_for_clearing_and_near_the_threshold_for_thinking_too(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        lead=[
            reply(tool("b1", "bash_tool", {"command": "ls", "description": "x"}), stop="tool_use"),
            reply(text("Done.")),
        ]
    )
    harness = Harness(tmp_path, provider, history=_big_history())
    harness.engine.settings = replace(harness.engine.settings, context_tokens=60_000)
    outcome = harness.run()
    assert outcome.text == "Done."
    calls = [_sent(raw) for raw in provider.lead_calls]
    assert all(CONTEXT_BETA in call["betas"] for call in calls)
    kinds = [[edit["type"] for edit in call["context_management"]["edits"]] for call in calls]
    assert kinds == [["clear_thinking_20251015", "clear_tool_uses_20250919"]] * 2
    # The history is replayed whole: the clearing happens server-side.
    assert calls[0]["messages"][2]["content"][0]["content"] == "x" * 300_000
    small = ScriptedProvider(lead=[reply(text("Hi."))])
    quiet = Harness(tmp_path / "b", small, history=_chat(1, 1))
    quiet.run()
    edits = _sent(small.lead_calls[0])["context_management"]["edits"]
    assert [edit["type"] for edit in edits] == ["clear_tool_uses_20250919"]


def test_without_the_beta_a_long_history_is_replaced_by_codes_summary(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[reply(text("Next."))], name="openrouter")
    history = _big_history(3)
    history[-1] = {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "f1",
                "name": "report_finding",
                "input": {
                    "page": 4,
                    "item": "Sink base",
                    "architect": '36"',
                    "vendor": '33"',
                    "status": "different",
                },
            },
            {"type": "text", "text": "One difference on page 4."},
        ],
    }
    harness = Harness(tmp_path, provider, history=history, question="And page 5?")
    harness.engine.settings = replace(harness.engine.settings, context_tokens=60_000)
    outcome = harness.run()
    sent = _sent(provider.lead_calls[0])
    assert "context_management" not in sent
    assert len(sent["messages"]) == 1  # nothing older is replayed
    blocks = sent["messages"][0]["content"]
    assert blocks[0]["text"].startswith("<earlier_turns_summary>")
    assert "Turn 3. Question: question 3" in blocks[0]["text"]
    assert 'p.4 Sink base: architect 36", vendor 33" (different)' in blocks[0]["text"]
    assert [b["text"] for b in blocks].index("And page 5?") == 1  # the question, then the scope
    assert harness.engine.summarised is True
    # The turn is stored with its summary, append-only from here on.
    assert outcome.api_content[0]["content"][0]["text"].startswith("<earlier_turns_summary>")


def test_the_summary_skips_the_apps_own_blocks_and_an_earlier_summary() -> None:
    first = {
        "role": "user",
        "content": [
            {"type": "text", "text": "<earlier_turns_summary>old</earlier_turns_summary>"},
            {"type": "text", "text": "<set_notes>n</set_notes>"},
            {"type": "text", "text": "What is on page 2?"},
            {"type": "text", "text": "The app scoped this question to page 2. Answer about it."},
        ],
    }
    summary = history_summary([first, _answer("A sink.")])
    assert "Turn 1. Question: What is on page 2?" in summary
    assert "Answer: A sink." in summary


# ---- set notes ----------------------------------------------------------------------------------


@dataclass
class _Note:
    note: str
    page: int | None
    source: str
    created_at: datetime


def test_the_notes_block_puts_the_asked_pages_first_and_keeps_it_short() -> None:
    now = datetime(2026, 10, 17, tzinfo=UTC)
    rows = [_Note(f"note {n}", n % 7 or None, "ai", now) for n in range(1, 40)]
    rows.insert(5, _Note("p.3 3'-6\" measures the blocking", 3, "decision", now))
    block = notes_block(rows, pages=[3])  # type: ignore[arg-type]
    assert block is not None
    lines = [line for line in block.splitlines() if line.startswith("- ")]
    assert len(lines) == MAX_NOTES
    assert all(line.startswith("- p.3:") for line in lines[:6])
    assert "reviewer's decision" in lines[0] or "reviewer's decision" in block
    assert "older notes not shown" in block and block.startswith("<set_notes>")
    assert "New notes" in (notes_block(rows[:1], new=True) or "")  # type: ignore[arg-type]
    assert notes_block([]) is None


def test_a_decision_becomes_a_plain_note() -> None:
    finding = {"item": "Buffet outlet height", "vendor": '46"', "architect": "3'-1\"", "page": 4}
    note, page = decision_note(finding, "agree", "reviewer-one", "tell the vendor")
    assert note == (
        'Buffet outlet height (vendor 46"; architect 3\'-1"): reviewer-one agreed it is wrong: '
        "tell the vendor"
    )
    assert page == 4
    assert decision_note(finding, "not_a_problem", "r")[0].endswith("r marked it not a problem")


def test_remember_keeps_a_note_and_is_the_leads_only(tmp_path: Path) -> None:
    workspace = ChatWorkspace.open(tmp_path / "chat")
    kept: list[tuple[str, int | None]] = []
    tools = ChatTools(
        workspace=workspace,
        sandbox=FakeSandbox(workspace),
        remember_note=lambda note, page: kept.append((note, page)),
    )
    done = tools.remember(
        {"note": "  The ID drawing   governs over the brand prototype ", "page": 12},
        new_step("m1", "remember", {}),
    )
    assert done.is_error is False and done.step["ok"] is True
    assert kept == [("The ID drawing governs over the brand prototype", 12)]
    assert done.step["label"] == "Remembered: The ID drawing governs over the brand prototype"
    for bad in ({"note": ""}, {"note": "x" * 301}, {"note": "x", "page": 0}, {"page": 2}):
        assert tools.remember(bad, new_step("m2", "remember", {})).is_error is True
    assert "remember" in {t["name"] for t in LEAD_TOOLS}
    assert "remember" not in {t["name"] for t in HELPER_TOOLS}
    none = ChatTools(workspace=workspace, sandbox=FakeSandbox(workspace))
    assert none.remember({"note": "x"}, new_step("m3", "remember", {})).is_error is True


def test_the_lead_remembers_with_a_step(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("m1", "remember", {"note": "The 3'-6\" on p.3 is the blocking"}),
                stop="tool_use",
            ),
            reply(text("Noted.")),
        ]
    )
    harness = Harness(tmp_path, provider)
    kept: list[tuple[str, int | None]] = []
    harness.engine.tools.remember_note = lambda note, page: kept.append((note, page))
    outcome = harness.run()
    steps = [b["step"] for b in outcome.blocks if b["type"] == "step"]
    assert [step["tool"] for step in steps] == ["remember"] and steps[0]["ok"] is True
    assert kept == [("The 3'-6\" on p.3 is the blocking", None)]


# ---- follow-up corrections ----------------------------------------------------------------------


@dataclass
class _Row:
    id: Any
    role: str
    blocks: list[dict[str, Any]] | None


def _finding(finding_id: str, vendor: str = '46"', **more: Any) -> dict[str, Any]:
    return {
        "id": finding_id,
        "page": 4,
        "item": "Outlet height",
        "category": "cabinet",
        "architect": "3'-1\"",
        "vendor": vendor,
        "status": "different",
        "verified": True,
        "source": "architect",
        "created_by": "lead",
        **more,
    }


def test_refs_parse() -> None:
    assert parse_ref("A1-F5") == (1, "F5") and parse_ref(" A12-F30 ") == (12, "F30")
    for bad in ("F5", "A0-F1", "A1-F0", "a1-f5", None, "A1-F5x"):
        assert parse_ref(bad) is None


def test_the_newest_correction_wins_and_the_stored_blocks_never_change() -> None:
    first = _Row(
        uuid4(),
        "assistant",
        [
            {"type": "finding", "finding": _finding("F1")},
            {"type": "finding", "finding": _finding("F2")},
        ],
    )
    update = {
        "ref": "A1-F1",
        "message_id": str(first.id),
        "finding_id": "F1",
        "answer": 1,
        "by_answer": 2,
        "version": 2,
        "finding": _finding("F1", '37"'),
        "dropped": None,
    }
    second = _Row(uuid4(), "assistant", [{"type": "finding_update", "update": update}])
    third_update = {**update, "by_answer": 3, "version": 3, "finding": _finding("F1", '36"')}
    drop = {
        **update,
        "ref": "A1-F2",
        "finding_id": "F2",
        "by_answer": 3,
        "finding": _finding("F2"),
        "dropped": "It is the blocking",
    }
    third = _Row(
        uuid4(),
        "assistant",
        [
            {"type": "finding_update", "update": third_update},
            {"type": "finding_update", "update": drop},
        ],
    )
    rows = [_Row(uuid4(), "user", None), first, _Row(uuid4(), "user", None), second, third]
    newest = corrections_by_answer(rows)[str(first.id)]
    shown = with_corrections(first.blocks, newest)
    assert shown is not None
    assert shown[0]["finding"]["vendor"] == '36"'
    assert shown[0]["finding"]["updated_in"] == {
        "message_id": str(third.id),
        "answer": 3,
        "version": 3,
    }
    assert shown[1] == {
        "type": "finding_dropped",
        "finding_id": "F2",
        "reason": "It is the blocking",
        "dropped_in": {"message_id": str(third.id), "answer": 3, "version": 2},
    }
    assert first.blocks[0]["finding"]["vendor"] == '46"'  # type: ignore[index]
    found = earlier_findings(rows)
    assert list(found) == ["A1-F1"]  # F2 was dropped
    assert found["A1-F1"]["version"] == 3 and found["A1-F1"]["finding"]["vendor"] == '36"'
    note = earlier_findings_note(found)
    assert (
        note is not None
        and '- A1-F1 p.4 Outlet height: architect 3\'-1", vendor 36" (wrong)' in note
    )
    assert earlier_findings_note({}) is None


def _earlier(tmp_path: Path) -> dict[str, Any]:
    message_id = str(uuid4())
    return {
        "A1-F1": {
            "ref": "A1-F1",
            "message_id": message_id,
            "answer": 1,
            "version": 1,
            "finding": _finding("F1"),
        },
        "A1-F2": {
            "ref": "A1-F2",
            "message_id": message_id,
            "answer": 1,
            "version": 1,
            "finding": _finding("F2", '30"', item="Sink base"),
        },
    }


def test_a_follow_up_corrects_an_earlier_answers_findings(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        lead=[
            reply(
                tool(
                    "u1",
                    "update_finding",
                    {"finding_id": "A1-F1", "vendor": '45"', "why": "Read again at 300 dpi"},
                ),
                tool(
                    "u2",
                    "drop_finding",
                    {"finding_id": "A1-F2", "reason": "It measures the blocking"},
                ),
                tool("u3", "update_finding", {"finding_id": "A1-F9", "vendor": "1"}),
                stop="tool_use",
            ),
            reply(
                tool("u4", "update_finding", {"finding_id": "A1-F1", "vendor": '44"'}),
                tool("u5", "update_finding", {"finding_id": "A1-F2", "vendor": "1"}),
                stop="tool_use",
            ),
            reply(text('Correction to my earlier table: F1 is 44"; F2 is not a finding.')),
        ]
    )
    harness = Harness(tmp_path, provider, question="Check p.4 again")
    harness.engine.book.earlier = _earlier(tmp_path)
    harness.engine.answer_number = 2
    outcome = harness.run()
    updates = [b["update"] for b in outcome.blocks if b["type"] == "finding_update"]
    assert [(u["ref"], u["version"], u["finding"]["vendor"], u["dropped"]) for u in updates] == [
        ("A1-F1", 2, '44"', None),  # corrected twice in one answer: one version, replaced
        ("A1-F2", 2, '30"', "It measures the blocking"),
    ]
    assert updates[0]["finding"]["why"] == "Read again at 300 dpi"
    assert updates[0]["by_answer"] == 2 and updates[0]["answer"] == 1
    assert updates[0]["finding"]["checks"] is not None  # code's checks ran on the new version
    # This answer's own findings are untouched; nothing of the earlier answer is in them.
    assert not [b for b in outcome.blocks if b["type"] == "finding"]
    events = events_named(harness.events, "finding_update")
    assert [e["update"]["ref"] for e in events] == ["A1-F1", "A1-F2", "A1-F1"]
    assert events[0]["index"] == events[2]["index"]
    results = json.dumps(_sent(provider.lead_calls[1])["messages"][-1])
    assert "there is no finding A1-F9" in results
    results = json.dumps(_sent(provider.lead_calls[2])["messages"][-1])
    assert "finding A1-F2 was dropped in this answer" in results
