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

from app.review.assistant import chat_engine, chat_trust, read_adjudication
from app.review.assistant.chat_findings import COVERAGE_CHECK_PREFIX
from tests.review.assistant.chat_fakes import (
    SONNET,
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
LISTING = [{"name": "Vendor set.pdf", "pages": 3, "kind": "the vendor's shop drawings"}]


def _looked_at(*boxes: dict[str, Any]) -> dict[str, Any]:
    """A zoom call with one sharp crop around each box (20 pt to spare on each side)."""
    crops = [
        {
            "page": box["page"],
            "box": {
                "x0": box["x0"] - 20,
                "y0": box["y0"] - 20,
                "x1": box["x1"] + 20,
                "y1": box["y1"] + 20,
            },
        }
        for box in boxes
    ]
    return {"crops": crops, "description": "Read both sides"}


def _zooms(harness: Harness) -> Harness:
    """The harness with its PDF listed, so zoom can draw crops of it."""
    harness.engine.tools.uploads = LISTING
    return harness


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
    assert types_ == [
        "finding",
        "finding_dropped",
        "coverage",
        "text",
        "scope",
        "trust",
        "accuracy",
    ]
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
    trust = outcome.blocks[5]["trust"]
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
    assert [b["type"] for b in outcome.blocks] == ["text", "scope", "accuracy"]
    assert outcome.blocks[-1]["accuracy"]["score"] is None
    assert outcome.blocks[-1]["accuracy"]["level"] == "n/a"


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
    _modules(
        monkeypatch,
        finding_checks=_fake_checks(),
        second_read=_fake_second_read(seen),
        read_adjudication=read_adjudication,
    )
    workspace_with_pdf(tmp_path)

    def answer(params: Any) -> Any:
        asked = next(b["text"] for b in params["messages"][0]["content"] if b["type"] == "text")
        if params["model"] == SONNET:  # the third read (#1498) of the finding the second disputes
            third = {"architect": "12", "vendor": "13", "sure": True}
            return reply(text(json.dumps(third)), model=SONNET)
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
    thirds = [call for call in calls if call["model"] == SONNET]
    calls = [call for call in calls if call["model"] != SONNET]
    assert len(calls) == 2
    # F2's second read disagreed: one third read, and both reads outvote the finding's values.
    assert len(thirds) == 1 and thirds[0]["output_config"] == {"effort": "low"}
    assert findings["F2"]["checks"]["adjudication"] == "disagrees"
    assert [read["role"] for read in findings["F2"]["reads"]] == ["first", "second", "third"]
    assert findings["F1"]["checks"]["adjudication"] is None and findings["F1"]["reads"] is None
    for call in calls:
        assert call["model"] == HAIKU and call["output_config"] == {"effort": "low"}
        assert call["thinking"] == {"type": "disabled"} and call["max_tokens"] == 1024
        assert "tools" not in call and "tool_choice" not in call
    prompt_ids = [row["prompt_id"] for row in harness.ledger.rows]
    assert prompt_ids.count("review-assistant-chat-check-anthropic-chat-v10") == 2
    assert prompt_ids.count("review-assistant-chat-adjudicate-anthropic-chat-v10") == 1
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


def test_the_real_checks_modules_confirm_a_finding_printed_where_it_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With #1485's modules (no stubs): printed at the spot, inches by code, a sure second read."""
    monkeypatch.setenv("GV_READER_MISSING_SPACE_HEIGHTS", "0.1")
    workspace_with_pdf(tmp_path)
    sure = {"architect": '37"', "vendor": "3'-10\"", "sure": True}
    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            reply(text("One difference.")),
        ],
        helper_answer=lambda params: reply(text(json.dumps(sure)), model=HAIKU),
    )
    outcome = Harness(tmp_path, provider).run()

    finding = outcome.blocks[0]["finding"]
    assert finding["checks"]["printed_architect"] == "match"
    assert finding["checks"]["printed_vendor"] == "match"
    assert finding["inches"] == {"architect": "37", "vendor": "46", "difference": "9"}
    assert finding["checks"]["second_read"] == "agrees"
    assert finding["trust_level"] == "confirmed"


# ---- full check (CONTRACT4), recorded results never dropped, cites and accuracy (CONTRACT5) ----

RECORDS = {
    "records": {
        "countertops": [
            {
                "id": "c1",
                "page": 2,
                "label": "Vanity top",
                "outcome": "Needs correction",
                "printed_overall": "3'-10\"",
                "needed_overall": '37"',
            }
        ],
    },
    "ai_page_review": [
        {"page": 3, "different": [{"key": "sink centre", "verified_by": "evaluator"}]}
    ],
}


def _full_check(harness: Harness, products: tuple[str, ...] = ("countertop",)) -> None:
    import dataclasses

    harness.engine.settings = dataclasses.replace(
        harness.engine.settings, mode="full_check", products=products
    )
    harness.engine.tools.results = lambda page: RECORDS


def test_a_full_check_seeds_recorded_results_and_makes_the_cloud_drawing_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    right = finding_input(item="Backsplash height", status="same", vendor='37"')
    provider = ScriptedProvider(
        lead=[
            reply(
                tool(
                    "u1",
                    "update_finding",
                    {"finding_id": "F1", "vendor_box": VENDOR_BOX, "verified": True},
                ),
                tool("d1", "drop_finding", {"finding_id": "F2", "reason": "sink is centred"}),
                tool("z1", "zoom", _looked_at(ARCH_BOX, VENDOR_BOX)),
                tool("r1", "report_finding", right),
                stop="tool_use",
            ),
            reply(text("One item wrong on page 2; the backsplash is right.")),
        ]
    )
    harness = _zooms(Harness(tmp_path, provider))
    _full_check(harness)

    outcome = harness.run()

    first = _sent(provider.lead_calls[0])["messages"][0]["content"]
    seeded = first[-1]["text"]
    assert "GV Review already recorded these differences" in seeded
    assert "F1 page 2: Vanity top" in seeded and "(c1)" in seeded
    assert "F2 page 3: sink centre" in seeded and "(ai-review:p3:sink centre)" in seeded
    findings = {b["finding"]["id"]: b["finding"] for b in outcome.blocks if b["type"] == "finding"}
    assert findings["F1"]["created_by"] == "gv_review" and findings["F1"]["record_id"] == "c1"
    assert findings["F1"]["source"] == "rules" and findings["F1"]["checks"]["rule"] == "agrees"
    assert findings["F1"]["vendor_picture_id"] and findings["F1"]["verified"] is True
    trust = next(b["trust"] for b in outcome.blocks if b["type"] == "trust")
    assert trust["gv_checks"]["left_out"] == []
    assert trust["gv_checks"]["dismissed"] == [
        {"record_id": "ai-review:p3:sink centre", "reason": "sink is centred"}
    ]
    # The two files, made by code, handed over (the service announces each with `file`).
    names = [f["name"] for f in harness.files]
    assert names == ["Vendor set - AI cloud drawing.pdf", "Vendor set - AI check report.pdf"]
    from pypdf import PdfReader

    workspace = harness.workspace
    drawing = PdfReader(str(workspace.files / harness.files[0]["id"]))
    # The wrong item's note on its page, its entry on the notes pages.
    assert "vendor 3'-10\"" in drawing.pages[1].extract_text()
    assert "Vendor: 3'-10\"" in drawing.pages[-1].extract_text()
    assert "Right: not clouded." in drawing.pages[-1].extract_text()
    report = "".join(
        page.extract_text()
        for page in PdfReader(str(workspace.files / harness.files[1]["id"])).pages
    )
    for words in (
        "AI check report: draft, not GV's signed review",
        "Countertop",
        "What's wrong (1)",
        "Your decision (0)",
        "What's right (1)",
        "Backsplash height",
        "Not checked",
    ):
        assert words in report, words
    accuracy = next(b["accuracy"] for b in outcome.blocks if b["type"] == "accuracy")
    assert accuracy["claims"] == 2 and accuracy["breakdown"][0]["points"] == 1.0


def test_a_recorded_difference_left_out_sends_the_lead_back_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    every_page = {"pages": [{"page": p, "status": "same"} for p in (1, 2, 3)]}
    provider = ScriptedProvider(
        lead=[
            reply(tool("c1", "set_coverage", every_page), stop="tool_use"),
            reply(text("All pages are the same.")),
            reply(
                tool("x1", "dismiss_recorded", {"record_id": "c1", "reason": "field cut"}),
                stop="tool_use",
            ),
            reply(text("All the same; the recorded top is a field cut.")),
        ]
    )
    harness = Harness(tmp_path, provider)
    harness.engine.tools.results = lambda page: RECORDS

    outcome = harness.run()

    note = _sent(provider.lead_calls[2])["messages"][-1]["content"][-1]["text"]
    assert note.startswith(COVERAGE_CHECK_PREFIX)
    assert "c1 (page 2: Vanity top)" in note and "dismiss_recorded" in note
    assert "ai-review:p3:sink centre" in note
    assert len(provider.lead_calls) == 4
    trust = next(b["trust"] for b in outcome.blocks if b["type"] == "trust")
    assert trust["gv_checks"]["dismissed"] == [{"record_id": "c1", "reason": "field cut"}]
    # Still missing after the one send-back: the report says so.
    assert [i["record_id"] for i in trust["gv_checks"]["left_out"]] == ["ai-review:p3:sink centre"]


def test_values_stated_without_evidence_send_the_lead_back_for_cites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(
        lead=[
            reply(text('The vanity top is 37" long.')),
            reply(
                tool("k1", "cite", {"value": '37"', "box": ARCH_BOX}),
                tool("k2", "cite", {"value": "3", "fact": "page_count"}),
                tool("k3", "cite", {"value": "9", "fact": "page_count"}),
                stop="tool_use",
            ),
            reply(text('The vanity top is 37" long (page 1).')),
        ]
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    note = _sent(provider.lead_calls[1])["messages"][-1]["content"][-1]["text"]
    assert "show the evidence" in note and "cite" in note
    cites = [b["cite"] for b in outcome.blocks if b["type"] == "cite"]
    assert [c["id"] for c in cites] == ["C1", "C2", "C3"]
    assert cites[0]["picture_id"] and cites[0]["box"]["file"] == "Vendor set.pdf"
    assert cites[0]["printed"] == "not_run"  # the checks module is off in this test
    assert [c["printed"] for c in cites[1:]] == ["match", "not_found"]
    accuracy = next(b["accuracy"] for b in outcome.blocks if b["type"] == "accuracy")
    assert [item["points"] for item in accuracy["breakdown"]] == [0.3, 1.0, 0.0]
    assert accuracy["score"] == 43 and accuracy["level"] == "yellow"
    assert events_named(harness.events, "accuracy")[0]["accuracy"] == accuracy


def test_unseen_same_items_and_unopened_pages_become_unknowns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not seen is not right: a "same" item the lead never checked is "Not compared", not scored;
    it and the page never opened are unknown records (a block and an event before the trust card).
    """
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("r1", "report_finding", finding_input()),
                tool(
                    "r2",
                    "report_finding",
                    finding_input(item="Sink centre", status="same", verified=False),
                ),
                tool(
                    "c1",
                    "set_coverage",
                    {"pages": [{"page": 1, "status": "same"}, {"page": 2, "status": "different"}]},
                ),
                stop="tool_use",
            ),
            reply(text("One difference on page 2.")),
            # Sent back once for the page not accounted for; it answers the same.
            reply(text("One difference on page 2; page 3 not opened.")),
        ]
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    types_ = [block["type"] for block in outcome.blocks]
    assert types_[-3:] == ["unknowns", "trust", "accuracy"]
    same = next(b["finding"] for b in outcome.blocks if b.get("finding", {}).get("id") == "F2")
    assert same["compared"] is False and same["not_compared_why"] == "not_viewed"
    unknowns = next(b["unknowns"] for b in outcome.blocks if b["type"] == "unknowns")
    assert [(u["id"], u["page"], u["why"]) for u in unknowns] == [
        ("U1", 2, "not_opened"),
        ("U2", 3, "not_opened"),
    ]
    assert events_named(harness.events, "unknowns")[0]["unknowns"] == unknowns
    trust = next(b["trust"] for b in outcome.blocks if b["type"] == "trust")
    assert trust["not_compared"] == 1 and trust["unknowns"]["total"] == 2
    score = next(b["accuracy"] for b in outcome.blocks if b["type"] == "accuracy")
    assert score["claims"] == 1 and score["not_compared"] == 1 and score["unknowns"] == 2


def test_findings_with_values_and_no_box_send_the_lead_back_naming_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finding that states a drawing value needs its box: the finish check names each one (a GV
    rule finding needs the vendor box of the value the rule read)."""
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    rule = finding_input(
        item="Fillers",
        architect='rule 1-2"',
        vendor='1 1/2", 2", 3"',
        source="rules",
        arch_box=None,
        vendor_box=None,
    )
    rule = {k: v for k, v in rule.items() if v is not None}
    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", rule), stop="tool_use"),
            reply(text('One filler is 3", over the rule.')),
            reply(
                tool("u1", "update_finding", {"finding_id": "F1", "vendor_box": VENDOR_BOX}),
                stop="tool_use",
            ),
            reply(text('One filler is 3", over the rule (page 2).')),
        ]
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    note = _sent(provider.lead_calls[2])["messages"][-1]["content"][-1]["text"]
    assert "F1 (page 2: Fillers; needs vendor_box)" in note
    assert "the vendor_box of the value the rule read" in note
    finding = next(b["finding"] for b in outcome.blocks if b["type"] == "finding")
    assert finding["vendor_box"] and finding["vendor_picture_id"]
    assert len(provider.lead_calls) == 4


# ---- not seen is not right: the zoom log -------------------------------------------------------


def _same_item(tmp_path: Path, lead: list[Any], helpers: dict[str, Any] | None = None) -> Any:
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(lead=lead, helpers=helpers or {})
    outcome = _zooms(Harness(tmp_path, provider)).run()
    return next(b["finding"] for b in outcome.blocks if b["type"] == "finding")


def test_a_same_item_counts_when_the_lead_zoomed_both_sides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    right = finding_input(status="same", vendor='37"')
    finding = _same_item(
        tmp_path,
        [
            reply(
                tool("z1", "zoom", _looked_at(ARCH_BOX, VENDOR_BOX)),
                tool("r1", "report_finding", right),
                stop="tool_use",
            ),
            reply(text("The top is right.")),
        ],
    )
    assert finding["compared"] is True and finding["not_compared_why"] is None


def test_a_same_item_with_one_side_zoomed_is_not_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    right = finding_input(status="same", vendor='37"')
    finding = _same_item(
        tmp_path,
        [
            reply(
                tool("z1", "zoom", _looked_at(VENDOR_BOX)),
                tool("r1", "report_finding", right),
                stop="tool_use",
            ),
            reply(text("The top is right.")),
        ],
    )
    assert finding["compared"] is False and finding["not_compared_why"] == "not_viewed"


def test_a_same_item_with_no_zoom_at_all_is_not_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lead says it checked (verified), but nothing was looked at: not seen is not right.
    _modules(monkeypatch)
    right = finding_input(status="same", vendor='37"', verified=True)
    finding = _same_item(
        tmp_path,
        [
            reply(tool("r1", "report_finding", right), stop="tool_use"),
            reply(text("The top is right.")),
        ],
    )
    assert finding["not_compared_why"] == "not_viewed"


def test_a_helpers_zoom_counts_as_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _modules(monkeypatch)
    task = "Compare page 2 with page 1"
    right = finding_input(status="same", vendor='37"')
    finding = _same_item(
        tmp_path,
        [
            reply(tool("ag1", "Agent", {"description": "Page 2", "prompt": task}), stop="tool_use"),
            reply(text("Checked.")),
        ],
        {
            task: [
                reply(
                    tool("h1", "zoom", _looked_at(ARCH_BOX, VENDOR_BOX)),
                    stop="tool_use",
                    model=SONNET,
                ),
                reply(tool("h2", "report_finding", right), stop="tool_use", model=SONNET),
                reply(text("Page 2 top is right."), model=SONNET),
            ]
        },
    )
    assert finding["created_by"] == "helper:1"
    assert finding["compared"] is True and finding["not_compared_why"] is None
