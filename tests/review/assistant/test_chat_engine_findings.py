"""The chat engine's findings, coverage, whole-set guard, second reads and trust report (chat v2/v3).

Scripted replies and a real tiny PDF: no network, no paid call, no Docker.
"""

from __future__ import annotations

import json
import types
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import chat_engine, chat_trust
from app.review.assistant.chat_findings import COVERAGE_CHECK_PREFIX
from tests.review.assistant.chat_fakes import (
    ScriptedProvider,
    events_named,
    reply,
    text,
    tool,
)
from tests.review.assistant.test_chat_engine import DOLLARS, Harness
from tests.review.assistant.test_chat_findings import (
    ARCH_BOX,
    VENDOR_BOX,
    finding_input,
    workspace_with_pdf,
)

HAIKU = "claude-haiku-5-5"


def _modules(monkeypatch: pytest.MonkeyPatch, **modules: Any) -> None:
    """The checks builder's modules as this test wants them (absent unless given)."""

    def load(name: str) -> Any:
        return modules.get(name.rsplit(".", 1)[-1])

    monkeypatch.setattr(chat_trust, "load_module", load)
    monkeypatch.setattr(chat_engine, "load_module", load)


def _sent(raw: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(raw)
    return loaded


def _tool_results(call: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block
        for message in call["messages"]
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]


def test_findings_and_coverage_are_blocks_and_events_updated_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("r1", "report_finding", finding_input()),
                tool("r2", "report_finding", finding_input(item="Sink centre")),
                tool(
                    "c1",
                    "set_coverage",
                    {
                        "pages": [
                            {"page": 1, "status": "same"},
                            {"page": 2, "pair_page": 1, "status": "different"},
                            {"page": 3, "status": "not_comparable", "note": "cover"},
                        ]
                    },
                ),
                stop="tool_use",
            ),
            reply(
                tool("u1", "update_finding", {"finding_id": "F1", "why": "9 in too long"}),
                tool("d1", "drop_finding", {"finding_id": "F2", "reason": "misread"}),
                tool("bad", "update_finding", {"finding_id": "F7", "verified": True}),
                stop="tool_use",
            ),
            reply(text("One difference on page 2.")),
        ]
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    types_ = [block["type"] for block in outcome.blocks]
    assert types_ == ["finding", "finding_dropped", "coverage", "text", "trust"]
    finding = outcome.blocks[0]["finding"]
    assert finding["id"] == "F1" and finding["why"] == "9 in too long"
    assert finding["arch_picture_id"] and finding["vendor_picture_id"]
    assert finding["created_by"] == "lead" and finding["verified"] is True
    assert finding["checks"]["second_read"] == "not_run" and finding["checks"]["lead_checked"]
    assert finding["round_status"] == "single_round"
    assert outcome.blocks[1] == {"type": "finding_dropped", "finding_id": "F2", "reason": "misread"}
    assert outcome.blocks[2]["coverage"]["counts"] == {
        "total": 3,
        "same": 1,
        "different": 1,
        "not_comparable": 1,
        "not_opened": 0,
    }
    # The events carry each block's index, so the live screen equals the stored blocks.
    shown: dict[int, dict[str, Any]] = {}
    for event, data in harness.events:
        if event == "finding":
            shown[data["index"]] = {"type": "finding", "finding": data["finding"]}
        elif event == "finding_dropped":
            shown[data["index"]] = {k: data[k] for k in ("finding_id", "reason")} | {
                "type": "finding_dropped"
            }
        elif event == "coverage":
            shown[data["index"]] = {"type": "coverage", "coverage": data["coverage"]}
        elif event == "trust":
            shown[data["index"]] = {"type": "trust", "trust": data["trust"]}
    for index, block in shown.items():
        assert outcome.blocks[index] == block
    assert events_named(harness.events, "step_start") == []
    # The model's tool results say what happened, plainly.
    results = _tool_results(_sent(provider.lead_calls[2]))
    texts = [r["content"][0]["text"] for r in results]
    assert texts[0] == "Finding F1 recorded." and texts[1] == "Finding F2 recorded."
    assert texts[2].startswith("Coverage: 1 same, 1 different, 1 not comparable, of 3 pages.")
    assert texts[3:5] == ["Finding F1 updated.", "Finding F2 dropped."]
    assert results[5]["is_error"] is True and "no finding F7" in texts[5]
    trust = outcome.blocks[4]["trust"]
    assert trust["counts"]["total"] == 1
    assert trust["coverage"]["pages_not_opened"] == []


def test_a_helpers_findings_are_unverified_and_listed_for_the_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    task = "Compare page 2 with page 1"
    provider = ScriptedProvider(
        lead=[
            reply(tool("ag1", "Agent", {"description": "Page 2", "prompt": task}), stop="tool_use"),
            reply(
                tool("u1", "update_finding", {"finding_id": "F1", "verified": True}),
                stop="tool_use",
            ),
            reply(text("Checked.")),
        ],
        helpers={
            task: [
                reply(
                    tool("h1", "report_finding", finding_input(verified=True)),
                    tool("h2", "set_coverage", {"pages": [{"page": 2, "status": "different"}]}),
                    stop="tool_use",
                    model="claude-sonnet-5-5",
                ),
                reply(text("Page 2 top is longer."), model="claude-sonnet-5-5"),
            ]
        },
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    first = events_named(harness.events, "finding")[0]["finding"]
    assert first["created_by"] == "helper:1" and first["verified"] is False
    final = next(b for b in outcome.blocks if b["type"] == "finding")["finding"]
    assert final["verified"] is True and final["created_by"] == "helper:1"
    report = _tool_results(_sent(provider.lead_calls[1]))[0]["content"][0]["text"]
    assert "Page 2 top is longer." in report
    assert "Findings it reported (not verified" in report
    assert "F1 page 2: Vanity top length" in report
    coverage = next(b for b in outcome.blocks if b["type"] == "coverage")["coverage"]
    assert coverage["pages"][1]["by"] == "helper:1"
    # A helper's finding tools leave no step in its list.
    agent = next(b for b in outcome.blocks if b["type"] == "step")["step"]["agents"][0]
    assert agent["steps"] == []


def test_the_whole_set_guard_sends_the_lead_back_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("c1", "set_coverage", {"pages": [{"page": 1, "status": "same"}]}),
                stop="tool_use",
            ),
            reply(text("All same.")),
            reply(
                tool(
                    "c2",
                    "set_coverage",
                    {"pages": [{"page": 2, "status": "same"}]},
                ),
                stop="tool_use",
            ),
            reply(text("Pages 1 and 2 are the same; page 3 not opened.")),
        ]
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert len(provider.lead_calls) == 4
    sent = _sent(provider.lead_calls[2])["messages"]
    note = sent[-1]["content"][-1]["text"]
    assert note.startswith(COVERAGE_CHECK_PREFIX) and "pages 2-3 of Vendor set.pdf" in note
    assert sent[-2]["role"] == "assistant"
    # Page 3 is still missing, but the lead is sent back only once.
    assert outcome.text == "Pages 1 and 2 are the same; page 3 not opened."
    assert outcome.stopped is None
    assert [m["role"] for m in outcome.api_content] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    trust = next(b for b in outcome.blocks if b["type"] == "trust")["trust"]
    assert trust["coverage"]["pages_not_opened"] == [3]


def test_the_guard_does_not_send_back_without_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("c1", "set_coverage", {"pages": [{"page": 1, "status": "same"}]}),
                stop="tool_use",
            ),
            reply(text("Page 1 is the same.")),
        ]
    )
    cap = 40 * DOLLARS
    # The set's budget is nearly used once both calls are made: no room to send the lead back.
    harness = Harness(
        tmp_path,
        provider,
        package_spent=lambda: 0 if len(provider.lead_calls) < 2 else cap - 1000,
    )

    outcome = harness.run()

    assert len(provider.lead_calls) == 2
    assert outcome.text == "Page 1 is the same." and outcome.stopped is None


def test_no_guard_when_coverage_was_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(lead=[reply(text("Three pages."))])
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert len(provider.lead_calls) == 1
    # A short answer stays short: no trust block without findings or coverage.
    assert [b["type"] for b in outcome.blocks] == ["text"]


def _fake_checks() -> types.SimpleNamespace:
    values = {'37"': Decimal(37), "3'-10\"": Decimal(46)}

    def check_finding(finding: Any, paths: Any) -> Any:
        assert "Vendor set.pdf" in paths and paths["Vendor set.pdf"].is_file()
        return types.SimpleNamespace(
            as_dict=lambda: {
                "printed_architect": "match",
                "printed_vendor": "match",
                "arithmetic": None,
            }
        )

    return types.SimpleNamespace(to_inches=values.get, check_finding=check_finding)


def _fake_second_read(seen: list[dict[str, Any]]) -> types.SimpleNamespace:
    def second_read(crops: Any, item: str, model_call: Any) -> Any:
        seen.append({"crops": sorted(crops), "item": item})
        content = [{"type": "text", "text": f"What value is printed for {item} on each drawing?"}]
        answer = json.loads(model_call(content))
        return types.SimpleNamespace(
            architect=answer["architect"], vendor=answer["vendor"], result="unsure"
        )

    def compare_read(read: Any, finding: Any) -> Any:
        same = read.architect == finding["architect"] and read.vendor == finding["vendor"]
        return types.SimpleNamespace(
            architect=read.architect,
            vendor=read.vendor,
            result="agrees" if same else "disagrees",
        )

    return types.SimpleNamespace(second_read=second_read, compare_read=compare_read)


def test_second_reads_run_after_the_lead_and_feed_the_trust_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    _modules(monkeypatch, finding_checks=_fake_checks(), second_read=_fake_second_read(seen))
    workspace_with_pdf(tmp_path)

    def answer(params: Any) -> Any:
        asked = params["messages"][0]["content"][0]["text"]
        if "Sink centre" in asked:
            return reply(text(json.dumps({"architect": "12", "vendor": "13"})), model=HAIKU)
        return reply(text(json.dumps({"architect": '37"', "vendor": "3'-10\""})), model=HAIKU)

    provider = ScriptedProvider(
        lead=[
            reply(
                tool("r1", "report_finding", finding_input()),
                tool("r2", "report_finding", finding_input(item="Sink centre", verified=False)),
                stop="tool_use",
            ),
            reply(text("Two differences.")),
        ],
        helper_answer=answer,
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert sorted(item["item"] for item in seen) == ["Sink centre", "Vanity top length"]
    assert seen[0]["crops"] == ["architect", "vendor"]
    findings = {b["finding"]["id"]: b["finding"] for b in outcome.blocks if b["type"] == "finding"}
    assert findings["F1"]["checks"]["second_read"] == "agrees"
    assert findings["F1"]["second_read_values"] == {"architect": '37"', "vendor": "3'-10\""}
    assert findings["F1"]["inches"] == {"architect": "37", "vendor": "46", "difference": "9"}
    assert findings["F1"]["trust_level"] == "confirmed"
    assert findings["F2"]["checks"]["second_read"] == "disagrees"
    assert findings["F2"]["trust_level"] == "to_check"
    trust = next(b for b in outcome.blocks if b["type"] == "trust")["trust"]
    assert trust["counts"] == {"confirmed": 1, "likely": 0, "to_check": 1, "total": 2}
    assert trust["checks"]["second_read"]["agrees"] == 1
    # The second reads: the cheap model, low effort, thinking off, no tools; counted and recorded.
    calls = [_sent(raw) for raws in provider.helper_calls.values() for raw in raws]
    assert len(calls) == 2
    for call in calls:
        assert call["model"] == HAIKU and call["output_config"] == {"effort": "low"}
        assert call["thinking"] == {"type": "disabled"} and call["max_tokens"] == 1024
        assert "tools" not in call and "tool_choice" not in call
    prompt_ids = [row["prompt_id"] for row in harness.ledger.rows]
    assert prompt_ids.count("review-assistant-chat-check-anthropic-chat-v4") == 2
    assert outcome.cost_micros > 0


def test_second_reads_stop_at_the_message_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    _modules(monkeypatch, second_read=_fake_second_read(seen))
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            reply(text("One difference.")),
        ]
    )
    cap = 40 * DOLLARS
    harness = Harness(
        tmp_path,
        provider,
        package_spent=lambda: 0 if len(provider.lead_calls) < 2 else cap - 10,
    )

    outcome = harness.run()

    assert provider.helper_calls == {}  # asked, but the call was never made
    trust = next(b for b in outcome.blocks if b["type"] == "trust")["trust"]
    assert trust["checks"]["second_read"]["not_run"] == 1
    assert any("Second read not run on 1" in line for line in trust["not_checked"])


def test_boxes_default_to_the_architects_file_when_the_set_has_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace = workspace_with_pdf(tmp_path)
    (workspace.uploads / "Architect.pdf").write_bytes(
        (workspace.uploads / "Vendor set.pdf").read_bytes()
    )
    listing = json.loads((workspace.root / "uploads.json").read_text())
    listing.append({"name": "Architect.pdf", "pages": 3, "kind": "the architect's drawings"})
    (workspace.root / "uploads.json").write_text(json.dumps(listing))
    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            reply(text("Done.")),
        ]
    )
    outcome = Harness(tmp_path, provider).run()
    finding = outcome.blocks[0]["finding"]
    assert finding["arch_box"] == {**ARCH_BOX, "file": "Architect.pdf"}
    assert finding["vendor_box"] == {**VENDOR_BOX, "file": "Vendor set.pdf"}
