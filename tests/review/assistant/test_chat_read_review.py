"""The engine's third reads and critic after its second reads (#1498), end to end with scripted
replies and a real tiny PDF: when they run, the caps, the cost, and how they reach the finding,
the trust report and the accuracy score. No network, no paid call."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.runs.rates import call_cost_micros
from tests.review.assistant.chat_fakes import SONNET, ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_engine import DOLLARS, RATES, Harness
from tests.review.assistant.test_chat_findings import finding_input, workspace_with_pdf

HAIKU = "claude-haiku-5-5"
COVERAGE = {
    "pages": [
        {"page": 1, "status": "same"},
        {"page": 2, "pair_page": 1, "status": "different"},
        {"page": 3, "status": "not_comparable", "note": "cover"},
    ]
}


@pytest.fixture(autouse=True)
def _reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GV_READER_MISSING_SPACE_HEIGHTS", "0.1")


def _asked(params: Mapping[str, Any]) -> str:
    return "\n".join(b["text"] for b in params["messages"][0]["content"] if b["type"] == "text")


def _critic_entry(finding_id: str, **answers: str) -> dict[str, Any]:
    entry = {
        "id": finding_id,
        "same_item": "yes",
        "right_dimension": "yes",
        "vendor_ink": "yes",
        "architect_side": "yes",
        "note": "",
    }
    entry.update(answers)
    return entry


def _cost(row: Mapping[str, Any]) -> int:
    micros = call_cost_micros(RATES, row["model_key"], row["input_tokens"], row["output_tokens"])
    assert micros is not None
    return micros


def _blocks(outcome: Any, kind: str) -> list[dict[str, Any]]:
    return [block[kind] for block in outcome.blocks if block["type"] == kind]


def test_a_whole_set_answer_gets_the_critic_and_its_no_makes_the_finding_to_check(
    tmp_path: Path,
) -> None:
    workspace_with_pdf(tmp_path)
    sure = {"architect": '37"', "vendor": "3'-10\"", "sure": True}

    def answer(params: Mapping[str, Any]) -> Any:
        if params["model"] == HAIKU:
            return reply(text(json.dumps(sure)), model=HAIKU)
        assert "vendor_ink:" in _asked(params)  # the critic; no third read is needed
        critic = {
            "findings": [
                _critic_entry("F1", vendor_ink="no", note="The vendor value is GV's red note."),
                _critic_entry("F2"),
            ]
        }
        return reply(text(json.dumps(critic)), model=SONNET)

    provider = ScriptedProvider(
        lead=[
            reply(
                tool("r1", "report_finding", finding_input()),
                tool("r2", "report_finding", finding_input(item="Sink centre", status="check")),
                tool("c1", "set_coverage", COVERAGE),
                stop="tool_use",
            ),
            reply(text("Two findings.")),
        ],
        helper_answer=answer,
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    findings = {f["id"]: f for f in _blocks(outcome, "finding")}
    flagged, passed = findings["F1"], findings["F2"]
    assert flagged["checks"]["critic"] == "objects"
    assert flagged["critic"]["note"] == "The vendor value is GV's red note."
    assert flagged["trust_level"] == "to_check"  # printed and the second read agree, but no
    assert passed["checks"]["critic"] == "passed"
    assert passed["trust_level"] == "confirmed"  # the critic is never the reason for Confirmed
    trust = _blocks(outcome, "trust")[0]
    assert trust["checks"]["critic"] == {"passed": 1, "objects": 1, "unsure": 0, "not_run": 0}
    assert trust["counts"]["to_check"] == 1
    accuracy = _blocks(outcome, "accuracy")[0]
    claim = next(item for item in accuracy["breakdown"] if item["claim"].startswith("F1"))
    assert claim == {**claim, "points": 0.0}
    assert claim["how"] == "the critic objects: The vendor value is GV's red note."
    # One critic call, at low effort, on its own prompt id, counted in the message's cost.
    critic_calls = [
        json.loads(raw)
        for raws in provider.helper_calls.values()
        for raw in raws
        if json.loads(raw)["model"] == SONNET
    ]
    assert len(critic_calls) == 1 and critic_calls[0]["output_config"]["effort"] == "low"
    assert critic_calls[0]["output_config"]["format"]["schema"]["required"] == ["findings"]
    prompt_ids = [row["prompt_id"] for row in harness.ledger.rows]
    assert prompt_ids.count("review-assistant-chat-critic-anthropic-chat-v17") == 1
    assert outcome.cost_micros == sum(_cost(row) for row in harness.ledger.rows)


def test_a_one_line_answer_gets_no_critic(tmp_path: Path) -> None:
    workspace_with_pdf(tmp_path)
    sure = {"architect": '37"', "vendor": "3'-10\"", "sure": True}
    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            reply(text("The top is 9 in longer.")),
        ],
        helper_answer=lambda params: reply(text(json.dumps(sure)), model=HAIKU),
    )
    outcome = Harness(tmp_path, provider).run()

    finding = _blocks(outcome, "finding")[0]
    assert finding["checks"]["critic"] is None and finding["critic"] is None
    assert finding["trust_level"] == "confirmed"
    assert len(provider.helper_calls) == 1  # the second read only


def test_at_most_ten_third_reads_per_answer(tmp_path: Path) -> None:
    workspace_with_pdf(tmp_path)
    other = {"architect": '37"', "vendor": '45"', "sure": True}
    thirds: list[str] = []
    lock = threading.Lock()

    def answer(params: Mapping[str, Any]) -> Any:
        if params["model"] == SONNET:
            with lock:
                thirds.append(_asked(params))
            return reply(text(json.dumps(other)), model=SONNET)
        return reply(text(json.dumps(other)), model=HAIKU)

    reports = [
        tool(f"r{n}", "report_finding", finding_input(item=f"Outlet {n} height"))
        for n in range(1, 13)
    ]
    provider = ScriptedProvider(
        lead=[reply(*reports, stop="tool_use"), reply(text("Twelve outlets differ."))],
        helper_answer=answer,
    )
    outcome = Harness(tmp_path, provider, message_cap=15 * DOLLARS).run()

    assert len(thirds) == 10
    assert all("turned a quarter turn" not in asked for asked in thirds)  # wide boxes
    findings = _blocks(outcome, "finding")
    adjudicated = [f for f in findings if f["checks"]["adjudication"] is not None]
    assert len(adjudicated) == 10
    assert {f["checks"]["adjudication"] for f in adjudicated} == {"disagrees"}
    assert all(f["trust_level"] == "to_check" for f in findings)
    trust = _blocks(outcome, "trust")[0]
    assert trust["checks"]["adjudication"]["disagrees"] == 10
    assert any(line.startswith("Third read not run on 2 ") for line in trust["not_checked"])


def test_no_third_read_without_room_under_the_cap(tmp_path: Path) -> None:
    workspace_with_pdf(tmp_path)
    cap = 40 * DOLLARS
    second_done = threading.Event()

    def answer(params: Mapping[str, Any]) -> Any:
        assert params["model"] == HAIKU, "the third read must not be made"
        second_done.set()
        unsure = {"architect": None, "vendor": '45"', "sure": False}
        return reply(text(json.dumps(unsure)), model=HAIKU)

    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            reply(text("One difference.")),
        ],
        helper_answer=answer,
    )
    harness = Harness(
        tmp_path, provider, package_spent=lambda: cap - 10 if second_done.is_set() else 0
    )

    outcome = harness.run()

    finding = _blocks(outcome, "finding")[0]
    assert finding["checks"]["second_read"] == "unsure"
    assert finding["checks"]["adjudication"] is None and finding["reads"] is None
    trust = _blocks(outcome, "trust")[0]
    assert any(line.startswith("Third read not run on 1 ") for line in trust["not_checked"])
