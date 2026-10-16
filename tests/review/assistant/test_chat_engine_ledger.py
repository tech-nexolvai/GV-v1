"""The reading ledger and the revision diff, wired into the chat engine (#1496).

Scripted replies, fake checks modules and a real tiny PDF: no network, no paid call, no Docker.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from app.review.assistant import revision_diff
from app.review.assistant.chat_contract import ChatBlock
from app.review.assistant.chat_findings import COVERAGE_CHECK_PREFIX
from app.review.assistant.chat_tools import COMPARE_ROUNDS, HELPER_TOOLS, LEAD_TOOLS
from app.review.assistant.finding_checks import to_inches
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_engine import Harness
from tests.review.assistant.test_chat_engine_findings import RECORDS, _modules, _sent
from tests.review.assistant.test_chat_findings import VENDOR_BOX, finding_input, workspace_with_pdf

SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"


def _checks() -> types.SimpleNamespace:
    """`finding_checks` as code would answer, without reading the PDF: arithmetic from the sum."""

    def check_finding(finding: Any, _paths: Any) -> Any:
        arithmetic = None
        if finding.get("source") == "vendor_arithmetic":
            parts = [to_inches(p) for p in finding.get("parts") or []]
            total = to_inches(str(finding.get("printed_total")))
            arithmetic = (
                "not_a_number"
                if total is None or None in parts
                else (
                    "adds_up"
                    if sum(p for p in parts if p is not None) == total
                    else "does_not_add_up"
                )
            )
        return types.SimpleNamespace(
            as_dict=lambda: {
                "printed_architect": "no_box",
                "printed_vendor": "match",
                "arithmetic": arithmetic,
            }
        )

    return types.SimpleNamespace(to_inches=to_inches, check_finding=check_finding)


def _string(page: int, parts: list[str], total: str, **more: Any) -> dict[str, Any]:
    return {"page": page, "view": "plan", "side": "vendor", "parts": parts, "printed_total": total}


def _reading(page: int, view: str, side: str, item: str, value: str) -> dict[str, Any]:
    return {"page": page, "view": view, "side": side, "item": item, "kind": "width", "value": value}


def _blocks(outcome: Any, kind: str) -> list[dict[str, Any]]:
    return [block[kind] for block in outcome.blocks if block["type"] == kind]


def _tool_texts(call: dict[str, Any]) -> dict[str, str]:
    return {
        block["tool_use_id"]: block["content"][0]["text"]
        for message in call["messages"]
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    }


def test_the_tool_lists_carry_the_ledger_and_the_lead_compares_rounds() -> None:
    lead = [t["name"] for t in LEAD_TOOLS]
    helper = [t["name"] for t in HELPER_TOOLS]
    assert {"record_string", "record_reading"} <= set(lead) & set(helper)
    assert "compare_rounds" in lead and "compare_rounds" not in helper
    # The copy app/api can import is the revision diff module's own tool, exactly.
    assert COMPARE_ROUNDS == revision_diff.COMPARE_ROUNDS_TOOL


def test_recorded_strings_and_readings_become_findings_and_trust_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch, finding_checks=_checks())
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("s1", "record_string", _string(2, ['12"', '15 1/2"'], '27 1/2"')),
                tool("s2", "record_string", _string(2, ['12"', '15 1/2"'], '27 1/4"')),
                tool("r1", "record_reading", _reading(1, "plan", "architect", "B36 base", '36"')),
                tool("r2", "record_reading", _reading(2, "plan", "vendor", "B36 base", '36"')),
                tool("r3", "record_reading", _reading(3, "elevation", "vendor", "B36 base", '36"')),
                tool("r4", "record_reading", _reading(2, "detail", "vendor", "B36 base", '35"')),
                tool("r5", "record_reading", _reading(1, "plan", "architect", "W30 wall", '30"')),
                tool("bad", "record_string", {"page": 2}),
                tool(
                    "ag1",
                    "Agent",
                    {"description": "Read page 3", "prompt": "Record page 3's strings"},
                ),
                stop="tool_use",
            ),
            reply(text("Two vendor strings recorded; one does not add up.")),
        ],
        helpers={
            "Record page 3's strings": [
                reply(
                    tool("h1", "record_string", _string(3, ['10"', '20"'], '30"')),
                    stop="tool_use",
                    model=SONNET,
                ),
                reply(text("Page 3: one string, it adds up."), model=SONNET),
            ]
        },
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    # The model got code's sum back, never its own; a bad input is an error it can read.
    texts = _tool_texts(_sent(provider.lead_calls[1]))
    assert "27 1/2" in texts["s1"] and "≠" in texts["s2"] and "off 1/4" in texts["s2"]
    assert texts["bad"].startswith("Not recorded:")
    helper_texts = _tool_texts(_sent(provider.helper_calls["Record page 3's strings"][1]))
    assert "30" in helper_texts["h1"]
    findings = _blocks(outcome, "finding")
    by_source = {f["source"]: f for f in findings}
    arithmetic = by_source["vendor_arithmetic"]
    assert arithmetic["created_by"] == "ledger" and arithmetic["ledger_ids"] == ["S2"]
    assert arithmetic["parts"] == ['12"', '15 1/2"'] and arithmetic["printed_total"] == '27 1/4"'
    assert arithmetic["checks"]["arithmetic"] == "does_not_add_up"
    assert "≠" in arithmetic["sum_text"]
    cross = by_source["cross_view"]
    assert cross["reference_side"] == "vendor" and cross["status"] == "check"
    assert cross["architect"] == '36"' and cross["vendor"] == '35"'
    completeness = by_source["completeness"]
    assert completeness["item"] == "W30 wall" and completeness["vendor"] == "not found"
    assert [f["id"] for f in findings] == ["F1", "F2", "F3"]
    trust = _blocks(outcome, "trust")[0]
    ledger = trust["ledger"]
    assert ledger["strings"]["total"] == 3
    assert ledger["strings"]["add_up"] == 2 and ledger["strings"]["off"] == 1
    assert ledger["views"]["inconsistent"] == 1
    assert ledger["completeness"]["missing_on_vendor"] == ["W30 wall"]
    assert len(trust["ledger_entries"]["strings"]) == 3
    assert {s["by"] for s in trust["ledger_entries"]["strings"]} == {"lead", "helper:1"}
    # Accuracy counts the ledger's sum as code arithmetic.
    accuracy = _blocks(outcome, "accuracy")[0]
    ledger_claim = next(c for c in accuracy["breakdown"] if c["claim"].startswith("F1 "))
    assert ledger_claim["points"] == 1.0 and "reading ledger" in ledger_claim["how"]
    # A reload shows the same: the stored blocks keep the ledger through the API's shapes.
    adapter = TypeAdapter(list[ChatBlock])
    shown = adapter.dump_python(adapter.validate_python(outcome.blocks), mode="json")
    trust_shown = next(b["trust"] for b in shown if b["type"] == "trust")
    assert trust_shown["ledger"] == ledger
    assert trust_shown["ledger_entries"]["strings"][1]["sum_text"]
    finding_shown = next(b["finding"] for b in shown if b["type"] == "finding")
    assert finding_shown["sum_text"] == arithmetic["sum_text"]
    assert finding_shown["ledger_ids"] == ["S2"]


def test_a_string_the_lead_already_reported_is_not_reported_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch, finding_checks=_checks())
    workspace_with_pdf(tmp_path)
    reported = finding_input(
        source="vendor_arithmetic", parts=['12"', '15 1/2"'], printed_total='27 1/4"'
    )
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("s1", "record_string", _string(2, ['12"', '15 1/2"'], '27 1/4"')),
                tool("f1", "report_finding", reported),
                stop="tool_use",
            ),
            reply(text("One string does not add up.")),
        ]
    )

    outcome = Harness(tmp_path, provider).run()

    assert [f["created_by"] for f in _blocks(outcome, "finding")] == ["lead"]


def test_no_ledger_means_no_ledger_in_the_trust_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            reply(text("One difference.")),
        ]
    )

    trust = _blocks(Harness(tmp_path, provider).run(), "trust")[0]

    assert trust["ledger"] is None and trust["recorded_results"] is None


def test_a_recorded_result_only_the_ledger_knows_joins_the_one_send_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    records = json.loads(json.dumps(RECORDS))
    records["records"]["review_list"] = {
        "failures": [{"id": "rl7", "page": 2, "what": "Filler width"}]
    }
    every_page = {"pages": [{"page": p, "status": "same"} for p in (1, 2, 3)]}
    provider = ScriptedProvider(
        lead=[
            reply(tool("c1", "set_coverage", every_page), stop="tool_use"),
            reply(text("All pages are the same.")),
            reply(text("Still the same.")),
        ]
    )
    harness = Harness(tmp_path, provider)
    harness.engine.tools.results = lambda page: records

    outcome = harness.run()

    # One send-back, carrying both checks' missing results, never a second one.
    assert len(provider.lead_calls) == 3
    note = _sent(provider.lead_calls[2])["messages"][-1]["content"][-1]["text"]
    assert note.startswith(COVERAGE_CHECK_PREFIX) and note.count(COVERAGE_CHECK_PREFIX) == 1
    assert "c1 (page 2: Vanity top)" in note and "rl7 (page 2: Filler width)" in note
    trust = _blocks(outcome, "trust")[0]
    recorded = trust["recorded_results"]
    # (The ledger counts only AI page review rows marked `result: different`.)
    assert recorded["recorded"] == 2 and recorded["included"] == 0
    assert {item["key"] for item in recorded["missing"]} == {"c1", "rl7"}
    assert [item["key"] for item in recorded["also_left_out"]] == ["rl7"]


# ---- revision diff (compare_rounds) ------------------------------------------------------------


def _fake_diff(
    calls: list[dict[str, Any]], pictures: list[Path], fail: str | None = None
) -> types.SimpleNamespace:
    def compare_rounds(
        given: Any, pdf_paths: Any, pictures_dir: Path, *, findings: Any = ()
    ) -> dict[str, Any]:
        if fail:
            raise ValueError(fail)
        calls.append({"given": dict(given), "findings": [f["id"] for f in findings]})
        pictures_dir.mkdir(parents=True, exist_ok=True)
        (pictures_dir / "diff1.png").write_bytes(pictures[0].read_bytes())
        box_b = {"page": 3, "x0": 300, "y0": 400, "x1": 360, "y1": 416}
        return {
            "text": "Round 1 p.2 vs round 2 p.3: lined up. 3 change zones.",
            "diff": {"aligned": True, "zones": []},
            "round_status": {
                "findings": [
                    {
                        "finding_id": "F1",
                        "round_status": "changed_check",
                        "zones": ["Z1"],
                        "recheck_box": [box_b],
                    },
                    {"finding_id": "F2", "round_status": "still_wrong", "zones": []},
                    {"finding_id": "F3", "round_status": "untouched", "zones": []},
                    {
                        "finding_id": "F4",
                        "round_status": "changed_check",
                        "zones": ["Z2"],
                        "recheck_box": [box_b],
                    },
                ],
                "unrequested_changes": [{"id": "Z3"}, {"id": "Z4"}],
            },
            "pairing": {"round1_page": 2, "round2_page": 3},
            "picture": {"picture_id": "diff1", "width": 40, "height": 20},
            "files": ["Vendor set.pdf", "Vendor set.pdf"],
        }

    return types.SimpleNamespace(compare_rounds=compare_rounds)


def _round_read() -> types.SimpleNamespace:
    def second_read(crops: Any, item: str, model_call: Any) -> Any:
        answer = json.loads(model_call([{"type": "text", "text": f"Read {item}"}]))
        return types.SimpleNamespace(
            architect=None, vendor=answer["vendor"], sure=answer["sure"], result="unsure"
        )

    def compare_read(read: Any, finding: Any) -> Any:
        return types.SimpleNamespace(architect=None, vendor=read.vendor, result="unsure")

    return types.SimpleNamespace(second_read=second_read, compare_read=compare_read)


def test_compare_rounds_sets_round_status_and_the_rounds_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = workspace_with_pdf(tmp_path)
    sample = tmp_path / "sample.png"
    from PIL import Image

    Image.new("RGB", (40, 20), (255, 255, 255)).save(sample)
    calls: list[dict[str, Any]] = []
    _modules(
        monkeypatch,
        finding_checks=_checks(),
        second_read=_round_read(),
        revision_diff=_fake_diff(calls, [sample]),
    )

    def answer(params: Any) -> Any:
        asked = json.dumps(params["messages"])
        if "Read Vanity top length" in asked:
            return reply(text(json.dumps({"vendor": '37"', "sure": True})), model=HAIKU)
        return reply(text(json.dumps({"vendor": "?", "sure": False})), model=HAIKU)

    findings = [
        finding_input(),
        finding_input(item="Sink centre", vendor_box=dict(VENDOR_BOX)),
        finding_input(item="Backsplash", status="same", vendor='37"'),
        finding_input(item="Overhang"),
    ]
    provider = ScriptedProvider(
        lead=[
            reply(
                *(tool(f"r{n}", "report_finding", f) for n, f in enumerate(findings, 1)),
                stop="tool_use",
            ),
            reply(tool("cr", "compare_rounds", {"round1_page": 2}), stop="tool_use"),
            reply(text("Round 2 fixed the top; the sink is still wrong.")),
        ],
        helper_answer=answer,
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert calls == [{"given": {"round1_page": 2}, "findings": ["F1", "F2", "F3", "F4"]}]
    result = next(
        block
        for message in _sent(provider.lead_calls[2])["messages"]
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block.get("tool_use_id") == "cr"
    )
    assert "is_error" not in result
    words = result["content"][0]["text"]
    assert "3 change zones" in words and 'F1 fixed_in_later_round (round 2 reads 37")' in words
    assert "F4 still_wrong (round 2 could not be read)" in words
    assert result["content"][1]["type"] == "image"  # the diff picture, sent like `view`'s
    step = next(b["step"] for b in outcome.blocks if b["type"] == "step")
    assert step["tool"] == "compare_rounds" and step["picture_id"] == "diff1" and step["ok"]
    assert (workspace.pictures / "diff1.png").is_file()
    status = {f["id"]: f for f in _blocks(outcome, "finding")}
    assert status["F1"]["round_status"] == "fixed_in_later_round"
    assert status["F2"]["round_status"] == "still_wrong"
    assert status["F3"]["round_status"] == "single_round"
    assert status["F4"]["round_status"] == "still_wrong"
    assert status["F4"]["trust_level"] == "to_check"
    rounds = _blocks(outcome, "trust")[0]["rounds"]
    assert rounds["line"] == (
        "Vendor fixed 1 of 3 round-1 items · 2 still wrong · 2 changes nobody asked for"
    )
    assert rounds["unrequested"] == 2 and rounds["pairs"][0]["round2_page"] == 3
    adapter = TypeAdapter(list[ChatBlock])
    adapter.validate_python(outcome.blocks)


def test_a_bad_compare_rounds_input_is_a_refused_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_with_pdf(tmp_path)
    _modules(monkeypatch, revision_diff=_fake_diff([], [], fail="page 9 is outside the file"))
    provider = ScriptedProvider(
        lead=[
            reply(tool("cr", "compare_rounds", {"round1_page": 9}), stop="tool_use"),
            reply(text("That page is not in the set.")),
        ]
    )

    outcome = Harness(tmp_path, provider).run()

    texts = _tool_texts(_sent(provider.lead_calls[1]))
    assert texts["cr"] == "Refused: page 9 is outside the file"
    step = next(b["step"] for b in outcome.blocks if b["type"] == "step")
    assert step["ok"] is False
