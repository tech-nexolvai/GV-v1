"""The chat engine (#1463), with scripted replies: no network, no paid call, no Docker.

- The stream reaches the screen as the contract's events, in block order: thinking, text, steps.
- The turn's API messages are stored exactly as sent and received (thinking blocks and their
  signatures unchanged), and the next turn replays them byte for byte, pictures included.
- Tools run in order, their results go back in one user message; `present_files` hands over files.
- Helpers run in parallel and ALWAYS report: near a limit they are told to wrap up and get one final
  call; when even that fails, their report is written from their steps.
- Caps, the turn limit, cancel, a refusal, a provider failure (asked once more), a reply from
  another model: each ends the message cleanly, stored, and says so.
"""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.review.assistant.chat_engine import (
    COST_CAP_NOTE,
    HELPER_SHARE_PERCENT,
    MAX_AGENTS_PER_TURN,
    TURN_LIMIT_NOTE,
    ChatEngine,
    EngineSettings,
    MessageOutcome,
)
from app.review.assistant.chat_provider import ProviderFailure
from app.review.assistant.chat_request import MODELS
from app.review.assistant.chat_tools import HELPER_FOOTER_MARK, ChatTools
from app.review.assistant.sandbox import ChatWorkspace
from app.runs.rates import call_cost_micros, load_model_rates
from tests.review.assistant.chat_fakes import (
    OPUS,
    SONNET,
    FakeLedger,
    FakeSandbox,
    ScriptedProvider,
    events_named,
    reply,
    server_error,
    strip_cache_control,
    text,
    thinking,
    tool,
)

ADAPTIVE = {"type": "adaptive", "display": "summarized"}
RATES = load_model_rates(
    Path(__file__).resolve().parents[3] / "deploy" / "model_rates.us-east-1.json"
)
DOLLARS = 1_000_000


def _png(path: Path, size: tuple[int, int] = (40, 30)) -> None:
    out = io.BytesIO()
    Image.new("RGB", size, (200, 10, 10)).save(out, format="PNG")
    path.write_bytes(out.getvalue())


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        provider: ScriptedProvider,
        *,
        history: Sequence[Mapping[str, Any]] = (),
        model: str = "claude-opus-5-5",
        effort: str = "high",
        thinking_on: bool = True,
        message_cap: int = 5 * DOLLARS,
        package_cap: int = 40 * DOLLARS,
        package_spent: Callable[[], int] = lambda: 0,
        lead_turns: int = 40,
        helper_turns: int = 20,
        handler: Callable[[str, ChatWorkspace], tuple[str, int]] | None = None,
        cancel: threading.Event | None = None,
        question: str = "Which items are wrong?",
    ) -> None:
        self.workspace = ChatWorkspace.open(tmp_path / "chat")
        self.sandbox = FakeSandbox(self.workspace, handler or (lambda command, _w: ("ok", 0)))
        self.files: list[dict[str, Any]] = []
        self.events: list[tuple[str, Any]] = []
        self.ledger = FakeLedger()
        self.cancel = cancel or threading.Event()
        provider.cancel = self.cancel
        self.provider = provider

        def register(fid: str, name: str, size: int, mime: str, sha: str) -> dict[str, Any]:
            shown = {"id": fid, "name": name, "size": size, "mime": mime, "url": f"/f/{fid}"}
            self.files.append({**shown, "sha256": sha})
            return shown

        self.engine = ChatEngine(
            settings=EngineSettings(
                model=MODELS[model],
                effort=effort,
                thinking=thinking_on,
                lead_max_turns=lead_turns,
                helper_max_turns=helper_turns,
                message_cap_micros=message_cap,
                package_cap_micros=package_cap,
                rates=lambda _key: RATES,
            ),
            provider=provider,  # type: ignore[arg-type]
            tools=ChatTools(
                workspace=self.workspace,
                sandbox=self.sandbox,
                page_count=60,
                results=lambda page: {"records": {"page": page}},
                register_file=register,
            ),
            ledger=self.ledger,
            emit=lambda event, data: self.events.append((event, data)),
            history=history,
            user_content=[{"type": "text", "text": question}],
            package_spent=package_spent,
            read_picture=lambda pid: (self.workspace.pictures / f"{pid}.png").read_bytes(),
            cancel=self.cancel,
            sleep=lambda _seconds: False,
        )

    def run(self) -> MessageOutcome:
        return self.engine.run()

    def names(self) -> list[str]:
        return [event for event, _ in self.events]


def _sent(raw: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(raw)
    return result


# ---- streaming ---------------------------------------------------------------------------------


def test_thinking_and_text_stream_as_contract_events_in_block_order(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[reply(thinking("Looking at it."), text("All **good**."))])
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    names = [n for n in harness.names() if n != "usage"]
    assert names == [
        "scope",
        "thinking_start",
        "thinking_delta",
        "thinking_delta",
        "thinking_end",
        "text_delta",
        "text_delta",
        "accuracy",
    ]
    assert events_named(harness.events, "thinking_start") == [{"index": 0}]
    assert "".join(d["text"] for d in events_named(harness.events, "thinking_delta")) == (
        "Looking at it."
    )
    assert events_named(harness.events, "thinking_end")[0]["index"] == 0
    assert {d["index"] for d in events_named(harness.events, "text_delta")} == {1}
    assert outcome.text == "All **good**."
    assert outcome.stopped is None and outcome.error is None
    assert [b["type"] for b in outcome.blocks] == ["thinking", "text", "scope", "accuracy"]
    assert outcome.blocks[0]["text"] == "Looking at it." and outcome.blocks[0]["seconds"] >= 1
    # The turn as sent and received, the thinking block with its signature unchanged.
    assert outcome.api_content == [
        {"role": "user", "content": [{"type": "text", "text": "Which items are wrong?"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Looking at it.", "signature": "sig-abc"},
                {"type": "text", "text": "All **good**."},
            ],
        },
    ]
    assert outcome.cost_micros > 0
    assert events_named(harness.events, "usage")[-1]["cost_usd"]


def test_a_tool_turn_runs_the_tool_and_sends_the_reply_back_unchanged(tmp_path: Path) -> None:
    first = reply(
        thinking("I should list the pages."),
        text("Let me look."),
        tool("t1", "bash_tool", {"command": "pdfinfo x.pdf", "description": "Count the pages"}),
        stop="tool_use",
    )
    provider = ScriptedProvider(lead=[first, reply(text("It has 60 pages."))])
    harness = Harness(tmp_path, provider, handler=lambda c, _w: ("Pages: 60", 0))

    outcome = harness.run()

    assert harness.sandbox.commands == ["pdfinfo x.pdf"]
    starts = events_named(harness.events, "step_start")
    ends = events_named(harness.events, "step_end")
    assert starts[0]["index"] == 2 and starts[0]["step"]["ok"] is None
    assert starts[0]["step"]["label"] == "Count the pages"
    assert ends[0]["step"]["output"] == "Pages: 60" and ends[0]["step"]["ok"] is True
    assert ends[0]["step"]["command"] == "pdfinfo x.pdf"
    second = _sent(provider.lead_calls[1])
    assert strip_cache_control(second["messages"]) == [
        {"role": "user", "content": [{"type": "text", "text": "Which items are wrong?"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "I should list the pages.",
                    "signature": "sig-abc",
                },
                {"type": "text", "text": "Let me look."},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "bash_tool",
                    "input": {"command": "pdfinfo x.pdf", "description": "Count the pages"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [{"type": "text", "text": "[exit code 0]\nPages: 60"}],
                }
            ],
        },
    ]
    # The newest block carries the cache breakpoint.
    assert second["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert outcome.text == "It has 60 pages."
    assert [b["type"] for b in outcome.blocks] == [
        "thinking",
        "text",
        "step",
        "text",
        "scope",
        "accuracy",
    ]
    assert len(harness.ledger.rows) == 2
    assert {row["prompt_id"] for row in harness.ledger.rows} == {
        "review-assistant-chat-anthropic-chat-v16"
    }


def test_history_is_replayed_byte_for_byte_with_pictures_put_back(tmp_path: Path) -> None:
    def render(command: str, workspace: ChatWorkspace) -> tuple[str, int]:
        _png(workspace.home / "p1.png", (60, 40))
        return "", 0

    turn_one = ScriptedProvider(
        lead=[
            reply(
                thinking("Render page 1."),
                tool("a", "bash_tool", {"command": "pdftoppm", "description": "Render"}),
                stop="tool_use",
            ),
            reply(
                tool("b", "view", {"path": "/home/claude/p1.png", "description": "Look"}),
                stop="tool_use",
            ),
            reply(thinking("Seen it."), text("Page 1 shows a counter.")),
        ]
    )
    one = Harness(tmp_path, turn_one, handler=render)
    first = one.run()
    picture_steps = [
        b["step"] for b in first.blocks if b["type"] == "step" and b["step"]["picture_id"]
    ]
    assert len(picture_steps) == 1
    picture_id = picture_steps[0]["picture_id"]
    assert (one.workspace.pictures / f"{picture_id}.png").is_file()
    # Stored with a reference, never the bytes.
    stored = json.dumps(first.api_content)
    assert picture_id in stored and '"data"' not in stored

    # Stored and read back as the database's `json` column does (keys keep their order).
    replay = json.loads(json.dumps(first.api_content))
    turn_two = ScriptedProvider(lead=[reply(text("Yes."))])
    two = Harness(tmp_path, turn_two, history=replay, question="Is it the kitchen?")
    two.run()

    last_of_one = _sent(turn_one.lead_calls[-1])["messages"]
    sent_two = _sent(turn_two.lead_calls[0])["messages"]
    prefix = strip_cache_control(sent_two[: len(last_of_one)])
    assert json.dumps(prefix) == json.dumps(strip_cache_control(last_of_one))
    # The picture went back as the same bytes both times.
    pictures = [
        block
        for message in sent_two
        for result in message["content"]
        if isinstance(result, dict) and result.get("type") == "tool_result"
        for block in result["content"]
        if block.get("type") == "image"
    ]
    assert len(pictures) == 1 and pictures[0]["source"]["type"] == "base64"
    # Then the answer, then the new question.
    assert sent_two[len(last_of_one)] == first.api_content[-1]
    assert strip_cache_control(sent_two[-1]) == {
        "role": "user",
        "content": [{"type": "text", "text": "Is it the kitchen?"}],
    }


# ---- tools -------------------------------------------------------------------------------------


def test_present_files_hands_over_a_copy_with_a_file_event(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        lead=[
            reply(
                tool(
                    "c",
                    "create_file",
                    {
                        "path": "/mnt/user-data/outputs/summary.csv",
                        "file_text": "page,item\n4,sink\n",
                        "description": "Write the summary",
                    },
                ),
                tool("p", "present_files", {"filepaths": ["/mnt/user-data/outputs/summary.csv"]}),
                stop="tool_use",
            ),
            reply(text("Here is the summary.")),
        ]
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert len(harness.files) == 1
    handed = harness.files[0]
    assert handed["name"] == "summary.csv" and handed["mime"] == "text/csv"
    assert (harness.workspace.files / handed["id"]).read_text() == "page,item\n4,sink\n"
    assert events_named(harness.events, "file") == []  # the service emits it; the tool registers
    step = [b["step"] for b in outcome.blocks if b["type"] == "step"][1]
    assert step["tool"] == "present_files" and step["ok"] is True


def test_tasks_event_carries_the_whole_list(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("t1", "TaskCreate", {"subject": "Read the set", "description": "x"}),
                tool("t2", "TaskUpdate", {"taskId": "1", "status": "in_progress"}),
                stop="tool_use",
            ),
            reply(text("Done.")),
        ]
    )
    harness = Harness(tmp_path, provider)
    harness.run()

    tasks = events_named(harness.events, "tasks")
    assert tasks[-1] == {
        "tasks": [
            {"id": "1", "subject": "Read the set", "active_form": None, "status": "in_progress"}
        ]
    }


def test_an_unknown_tool_and_a_cut_off_input_are_errors_the_model_reads(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        lead=[reply(tool("x", "delete_review", {}), stop="tool_use"), reply(text("Sorry."))]
    )
    harness = Harness(tmp_path, provider)
    harness.run()

    result = _sent(provider.lead_calls[1])["messages"][-1]["content"][0]
    assert result["is_error"] is True and "no tool" in result["content"][0]["text"]


# ---- helpers -----------------------------------------------------------------------------------


def _agents(*tasks: str) -> list[dict[str, Any]]:
    return reply(
        *(
            tool(f"ag{n}", "Agent", {"description": f"Pages part {n}", "prompt": task})
            for n, task in enumerate(tasks, 1)
        ),
        stop="tool_use",
    )


def test_helpers_run_together_and_their_reports_go_back_to_the_lead(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        lead=[_agents("Check pages 1-5", "Check pages 6-9"), reply(text("Two issues."))],
        helpers={
            "Check pages 1-5": [
                reply(
                    tool("h1", "bash_tool", {"command": "pdftotext", "description": "Read"}),
                    stop="tool_use",
                    model=SONNET,
                ),
                reply(text("Page 4: sink differs."), model=SONNET),
            ],
            "Check pages 6-9": [reply(text("Page 7: depth differs."), model=SONNET)],
        },
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    starts = events_named(harness.events, "agent_start")
    ends = events_named(harness.events, "agent_end")
    assert {s["agent"]["description"] for s in starts} == {"Pages part 1", "Pages part 2"}
    assert {e["agent"]["report"] for e in ends} == {
        "Page 4: sink differs.",
        "Page 7: depth differs.",
    }
    assert all(e["agent"]["ok"] for e in ends)
    step_index = starts[0]["step_index"]
    assert events_named(harness.events, "agent_step")[0]["step_index"] == step_index
    group = outcome.blocks[step_index]["step"]
    assert group["tool"] == "Agent" and group["ok"] is True
    assert len(group["agents"]) == 2 and group["agents"][0]["steps"]
    results = _sent(provider.lead_calls[1])["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["ag1", "ag2"]
    assert "Page 4: sink differs." in results[0]["content"][0]["text"]
    # An Opus lead's helpers run on Sonnet by default, at medium effort under a high-effort lead.
    helper_call = _sent(provider.helper_calls["Check pages 6-9"][0])
    assert helper_call["model"] == SONNET and helper_call["max_tokens"] == 32000
    assert helper_call["output_config"] == {"effort": "medium"}
    assert _sent(provider.lead_calls[0])["output_config"] == {"effort": "high"}
    assert [t["name"] for t in helper_call["tools"]] == [
        "bash_tool",
        "view",
        "zoom",
        "create_file",
        "str_replace",
        "report_finding",
        "set_coverage",
        "record_string",
        "record_reading",
        "verify_value",
        "measure_on_drawing",
        "read_under_markup",
    ]


def test_a_helper_at_its_turn_limit_is_told_to_wrap_up_and_reports(tmp_path: Path) -> None:
    task = "Check every page"
    provider = ScriptedProvider(
        lead=[_agents(task), reply(text("Done."))],
        helpers={
            task: [
                reply(
                    tool("h1", "bash_tool", {"command": "ls", "description": "List"}),
                    stop="tool_use",
                    model=SONNET,
                ),
                reply(text("I read pages 1-3; pages 4-60 not opened."), model=SONNET),
            ]
        },
    )
    harness = Harness(tmp_path, provider, helper_turns=1)

    harness.run()

    final = _sent(provider.helper_calls[task][1])
    assert final["tool_choice"] == {"type": "none"} and final["max_tokens"] == 8000
    assert "Wrap up now" in final["messages"][-1]["content"][-1]["text"]
    end = events_named(harness.events, "agent_end")[0]["agent"]
    assert end["ok"] is True and end["stopped"] == "turn_limit"
    assert end["report"] == "I read pages 1-3; pages 4-60 not opened."


def test_a_helper_near_the_money_cap_still_gets_its_final_call(tmp_path: Path) -> None:
    task = "Check every page"
    provider = ScriptedProvider(
        lead=[_agents(task), reply(text("Done."))],
        helpers={task: [reply(text("Nothing opened yet; pages 1-60 left."), model=SONNET)]},
    )
    # Room for the lead's first call, not for a helper's normal one: it wraps up at once.
    spent = {"n": 0}

    def package_spent() -> int:
        spent["n"] += 1
        return 0 if spent["n"] == 1 else 39_700_000

    harness = Harness(tmp_path, provider, package_spent=package_spent)
    harness.run()

    calls = provider.helper_calls[task]
    assert len(calls) == 1
    assert _sent(calls[0])["tool_choice"] == {"type": "none"}
    end = events_named(harness.events, "agent_end")[0]["agent"]
    assert end["ok"] is True and end["stopped"] == "cost_cap"


def test_a_helper_whose_final_call_fails_still_reports_from_its_steps(tmp_path: Path) -> None:
    task = "Check every page"
    failure = ProviderFailure(
        "The AI service refused the request (error 400).", status=400, retryable=False
    )
    provider = ScriptedProvider(
        lead=[_agents(task), reply(text("One helper could not finish."))],
        helpers={
            task: [
                reply(
                    tool("h1", "bash_tool", {"command": "pdfinfo a.pdf", "description": "Info"}),
                    stop="tool_use",
                    model=SONNET,
                ),
                failure,
            ]
        },
    )
    harness = Harness(tmp_path, provider)
    harness.run()

    end = events_named(harness.events, "agent_end")[0]["agent"]
    assert end["ok"] is False and end["stopped"] == "error"
    assert "stopped before it could write its report" in end["report"]
    assert "pdfinfo a.pdf" in end["report"]
    result = _sent(provider.lead_calls[1])["messages"][-1]["content"][0]
    assert result["is_error"] is True and "pdfinfo a.pdf" in result["content"][0]["text"]


def test_agents_past_the_parallel_limit_wait_in_the_same_turn(tmp_path: Path) -> None:
    """Eight helpers run at once by default (`GV_ASSISTANT_MAX_PARALLEL_AGENTS`); a ninth waits
    for a free place in the same turn instead of being refused (which cost the lead a turn)."""
    tasks = [f"Task {n}" for n in range(9)]
    provider = ScriptedProvider(
        lead=[_agents(*tasks), reply(text("Done."))],
        helpers={task: [reply(text(f"{task} report"), model=SONNET)] for task in tasks},
    )
    harness = Harness(tmp_path, provider)
    harness.run()

    results = _sent(provider.lead_calls[1])["messages"][-1]["content"]
    assert len(results) == 9
    assert not any(result.get("is_error") for result in results)
    assert "Task 8 report" in results[8]["content"][0]["text"]
    assert len(provider.lead_calls) == 2


def test_an_agent_past_the_per_turn_limit_is_refused(tmp_path: Path) -> None:
    tasks = [f"Task {n}" for n in range(MAX_AGENTS_PER_TURN + 1)]
    provider = ScriptedProvider(
        lead=[_agents(*tasks), reply(text("Done."))],
        helpers={task: [reply(text(f"{task} report"), model=SONNET)] for task in tasks},
    )
    harness = Harness(tmp_path, provider)
    harness.run()

    results = _sent(provider.lead_calls[1])["messages"][-1]["content"]
    assert len(results) == MAX_AGENTS_PER_TURN + 1
    assert not any(result.get("is_error") for result in results[:-1])
    refused = results[-1]
    assert refused["is_error"] is True
    assert (
        f"at most {MAX_AGENTS_PER_TURN} agents start in one turn" in refused["content"][0]["text"]
    )


@pytest.mark.parametrize(
    ("lead", "helper"),
    [
        ("claude-opus-5-5", "claude-sonnet-5-5"),
        ("claude-sonnet-5-5", "claude-sonnet-5-5"),
        ("claude-haiku-5-5", "claude-haiku-5-5"),
    ],
)
def test_a_helper_with_no_model_runs_on_sonnet_under_opus_else_on_the_leads_model(
    tmp_path: Path, lead: str, helper: str
) -> None:
    task = "Check pages 1-5"
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("ag1", "Agent", {"description": "Pages 1-5", "prompt": task}),
                stop="tool_use",
                model=lead,
            ),
            reply(text("Done."), model=lead),
        ],
        helpers={task: [reply(text("Page 4: sink differs."), model=helper)]},
    )
    harness = Harness(tmp_path, provider, model=lead)

    outcome = harness.run()

    assert _sent(provider.helper_calls[task][0])["model"] == helper
    assert events_named(harness.events, "agent_start")[0]["agent"]["model"] == helper
    assert events_named(harness.events, "agent_end")[0]["agent"]["ok"] is True
    assert outcome.stopped is None


def test_a_model_the_lead_names_for_a_helper_is_used(tmp_path: Path) -> None:
    task = "Look closely at the sink on page 4"
    provider = ScriptedProvider(
        lead=[
            reply(
                tool(
                    "ag1", "Agent", {"description": "Sink check", "prompt": task, "model": "opus"}
                ),
                stop="tool_use",
            ),
            reply(text("Done.")),
        ],
        helpers={task: [reply(text("The sink model differs."), model=OPUS)]},
    )
    harness = Harness(tmp_path, provider)

    harness.run()

    assert _sent(provider.helper_calls[task][0])["model"] == OPUS
    assert events_named(harness.events, "agent_end")[0]["agent"]["ok"] is True


@pytest.mark.parametrize(
    ("lead_model", "helper_model", "lead_effort", "helper_effort", "thinking_on"),
    [
        # #1577: a Fable 5.1 lead's helpers run on Sonnet 5.5, never at Fable's price.
        ("claude-fable-5-1", SONNET, "high", "medium", True),
        ("claude-fable-5-1", SONNET, "max", "medium", True),
        ("claude-opus-5-5", SONNET, "low", "low", True),
        ("claude-opus-5-5", SONNET, "medium", "medium", True),
        ("claude-opus-5-5", SONNET, "high", "medium", True),
        ("claude-opus-5-5", SONNET, "xhigh", "medium", True),
        ("claude-opus-5-5", SONNET, "max", "medium", True),
        ("claude-sonnet-5-5", SONNET, "high", "medium", False),
        ("claude-haiku-5-5", "claude-haiku-5-5", "high", "medium", False),
    ],
)
def test_helpers_think_at_most_at_medium_and_the_lead_keeps_its_effort(
    tmp_path: Path,
    lead_model: str,
    helper_model: str,
    lead_effort: str,
    helper_effort: str,
    thinking_on: bool,
) -> None:
    task = "Check pages 1-5"
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("ag1", "Agent", {"description": "Pages 1-5", "prompt": task}),
                stop="tool_use",
                model=lead_model,
            ),
            reply(text("Done."), model=lead_model),
        ],
        helpers={
            task: [
                reply(
                    tool("h1", "bash_tool", {"command": "pdftotext", "description": "Read"}),
                    stop="tool_use",
                    model=helper_model,
                ),
                reply(text("Page 4: sink differs."), model=helper_model),
            ]
        },
    )
    harness = Harness(
        tmp_path, provider, model=lead_model, effort=lead_effort, thinking_on=thinking_on
    )

    outcome = harness.run()

    assert outcome.stopped is None
    lead_calls = [_sent(raw) for raw in provider.lead_calls]
    helper_calls = [_sent(raw) for raw in provider.helper_calls[task]]
    assert len(lead_calls) == 2 and len(helper_calls) == 2
    # The reviewer's pick goes straight into every lead call; every helper call is capped.
    assert all(call["output_config"] == {"effort": lead_effort} for call in lead_calls)
    assert all(call["output_config"] == {"effort": helper_effort} for call in helper_calls)
    lead_model_row = MODELS[lead_model]
    if thinking_on:
        # Adaptive with the summary shown, and the preserved-thinking safety net (#1577).
        bound = {**ADAPTIVE, "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        assert lead_calls[0]["thinking"] == bound
        assert helper_calls[0]["thinking"] == bound
    else:
        # Thinking off at the lead's high effort, and still off for the helper at medium.
        assert lead_calls[0]["thinking"] == dict(lead_model_row.thinking_off or {})
        assert helper_calls[0]["thinking"] == dict(MODELS[helper_model].thinking_off or {})
    # Effort is a request setting, never part of the stored history a later turn replays.
    assert "output_config" not in json.dumps(outcome.api_content)


def test_helpers_stop_at_their_share_and_the_lead_keeps_the_rest(tmp_path: Path) -> None:
    """Helpers that never finish on their own: every normal call runs one more command at a big
    output, a final call (no tools) writes the report."""
    tasks = ("Check pages 1-30", "Check pages 31-60")

    def busy(params: Mapping[str, Any]) -> list[dict[str, Any]]:
        task = params["messages"][0]["content"][0]["text"].split(HELPER_FOOTER_MARK)[0]
        if params["tool_choice"] == {"type": "none"}:
            return reply(text(f"Report for {task}"), model=SONNET)
        return reply(
            tool("h", "bash_tool", {"command": "pdftotext", "description": "Read"}),
            stop="tool_use",
            model=SONNET,
            usage={"output_tokens": 30_000},
        )

    provider = ScriptedProvider(
        lead=[_agents(*tasks), reply(text("Checked the helpers' leads."))],
        helper_answer=busy,
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    # Every helper reports, told to wrap up because the helpers' share is used.
    ends = [end["agent"] for end in events_named(harness.events, "agent_end")]
    assert sorted(end["report"] for end in ends) == [f"Report for {task}" for task in tasks]
    assert all(end["ok"] and end["stopped"] == "cost_cap" for end in ends)
    for task in tasks:
        last = _sent(provider.helper_calls[task][-1])
        assert last["tool_choice"] == {"type": "none"}
        assert "the helpers have used their share" in last["messages"][-1]["content"][-1]["text"]

    def cost(row: Mapping[str, Any]) -> int:
        micros = call_cost_micros(
            RATES, row["model_key"], row["input_tokens"], row["output_tokens"]
        )
        assert micros is not None
        return micros

    lead_rows = [row for row in harness.ledger.rows if "helper" not in row["prompt_id"]]
    helper_rows = [row for row in harness.ledger.rows if "helper" in row["prompt_id"]]
    share = (5 * DOLLARS - cost(lead_rows[0])) * HELPER_SHARE_PERCENT // 100
    working = sum(cost(row) for row in helper_rows if row["stop_reason"] == "tool_use")
    # They used most of their share, never more; only the two short reports come on top.
    assert share - 2 * DOLLARS // 3 < working <= share
    assert len(helper_rows) == len(provider.helper_calls[tasks[0]]) + len(
        provider.helper_calls[tasks[1]]
    )

    # The lead still had the money for a normal call after the helpers: no cost-cap stop.
    assert len(provider.lead_calls) == 2
    after = _sent(provider.lead_calls[1])
    assert after["tool_choice"] == {"type": "auto"} and after["max_tokens"] == 64000
    assert COST_CAP_NOTE not in json.dumps(after["messages"])
    assert outcome.stopped is None and outcome.text == "Checked the helpers' leads."
    assert harness.engine.spent < 5 * DOLLARS


#: A turn stored by an earlier harness (chat-v1): the lead started a helper without a model.
_EARLIER_TURN: list[dict[str, Any]] = [
    {"role": "user", "content": [{"type": "text", "text": "Check the whole set."}]},
    {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "Split the pages.", "signature": "sig-v1"},
            {
                "type": "tool_use",
                "id": "ag1",
                "name": "Agent",
                "input": {"description": "Pages 1-30", "prompt": "Check pages 1-30"},
            },
        ],
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "ag1",
                "content": [{"type": "text", "text": "Report from helper agent: all fine."}],
            }
        ],
    },
    {"role": "assistant", "content": [{"type": "text", "text": "Pages 1-30 look fine."}]},
]


def test_an_earlier_chats_history_still_replays_byte_identical(tmp_path: Path) -> None:
    stored = json.loads(json.dumps(_EARLIER_TURN))
    provider = ScriptedProvider(lead=[reply(text("Yes."))])
    harness = Harness(tmp_path, provider, history=stored, question="And pages 31-60?")

    harness.run()

    sent = _sent(provider.lead_calls[0])["messages"]
    replayed = strip_cache_control(sent[: len(_EARLIER_TURN)])
    assert json.dumps(replayed) == json.dumps(_EARLIER_TURN)
    last = strip_cache_control(sent[-1])
    assert last["role"] == "user"
    assert last["content"][0] == {"type": "text", "text": "And pages 31-60?"}
    # A question about some pages gets the app's scope note after it (#1520).
    assert last["content"][1]["text"].startswith("The app scoped this question to pages 31-60.")


# ---- limits ------------------------------------------------------------------------------------


def test_the_lead_at_its_turn_limit_answers_without_tools(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        lead=[
            reply(tool("a", "bash_tool", {"command": "ls", "description": "x"}), stop="tool_use"),
            reply(text("Answer from what I have.")),
        ]
    )
    harness = Harness(tmp_path, provider, lead_turns=1)

    outcome = harness.run()

    final = _sent(provider.lead_calls[1])
    assert final["tool_choice"] == {"type": "none"}
    assert final["messages"][-1]["content"][-1]["text"] == TURN_LIMIT_NOTE
    assert outcome.stopped == "turn_limit"
    # The stop is carried by `stopped` (the screen says it); the answer is not given a note.
    assert outcome.text == "Answer from what I have."
    assert [b["text"] for b in outcome.blocks if b["type"] == "text"] == [outcome.text]


def test_at_the_money_cap_the_lead_gets_one_smaller_final_call(tmp_path: Path) -> None:
    # Opus: 64,000 output tokens is $1.28 on its own; 16,000 is $0.32. A $1 cap fits only the
    # smaller, final call.
    provider = ScriptedProvider(lead=[reply(text("Short answer."))])
    harness = Harness(tmp_path, provider, message_cap=1 * DOLLARS)

    outcome = harness.run()

    final = _sent(provider.lead_calls[0])
    assert final["tool_choice"] == {"type": "none"} and final["max_tokens"] == 16000
    assert final["messages"][-1]["content"][-1]["text"] == COST_CAP_NOTE
    assert outcome.stopped == "cost_cap" and outcome.text == "Short answer."
    assert [b["text"] for b in outcome.blocks if b["type"] == "text"] == ["Short answer."]


def test_a_forced_final_call_with_no_words_still_answers_with_the_stop(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[reply(thinking("Hmm."))])
    harness = Harness(tmp_path, provider, message_cap=1 * DOLLARS)

    outcome = harness.run()

    assert outcome.stopped == "cost_cap" and "$1 limit" in outcome.text
    assert outcome.cap_hit == "message"


def test_over_the_cap_no_call_is_made_and_the_answer_says_so(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[])
    harness = Harness(tmp_path, provider, message_cap=100_000)

    outcome = harness.run()

    assert provider.calls == []
    assert outcome.stopped == "cost_cap" and "this answer reached its $0.10 limit" in outcome.text
    assert outcome.cap_hit == "message"
    assert outcome.api_content == []  # an unanswered message never enters the history
    assert events_named(harness.events, "text_delta")[0]["text"] == outcome.text


def test_the_drawing_sets_budget_counts_too(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[])
    harness = Harness(tmp_path, provider, package_spent=lambda: 40 * DOLLARS - 1000)

    outcome = harness.run()

    assert provider.calls == [] and outcome.stopped == "cost_cap"
    # The set's budget, not this answer's limit, is what the answer names.
    assert outcome.cap_hit == "package"
    assert "this drawing set has used its $40 AI budget" in outcome.text


# ---- stops -------------------------------------------------------------------------------------


def test_cancel_between_steps_stops_and_closes_the_open_calls(tmp_path: Path) -> None:
    cancel = threading.Event()

    def first_command_cancels(command: str, _w: ChatWorkspace) -> tuple[str, int]:
        cancel.set()
        return "done", 0

    provider = ScriptedProvider(
        lead=[
            reply(
                tool("a", "bash_tool", {"command": "one", "description": "x"}),
                # A file write runs alone, after the command: the cancel is seen before it.
                tool("b", "create_file", {"path": "n.txt", "file_text": "y", "description": "y"}),
                stop="tool_use",
            )
        ]
    )
    harness = Harness(tmp_path, provider, handler=first_command_cancels, cancel=cancel)

    outcome = harness.run()

    assert harness.sandbox.commands == ["one"]
    assert outcome.stopped == "cancelled"
    results = outcome.api_content[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert "cancelled" in results[1]["content"][0]["text"] and results[1]["is_error"] is True


def test_cancel_in_the_middle_of_a_stream_keeps_what_was_shown(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[reply(text("Half an answer here"), cancel_after=3)])
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert outcome.stopped == "cancelled"
    assert outcome.text == "Half an a"
    assert outcome.api_content == []
    assert len(harness.ledger.rows) == 1  # the call is still recorded


def test_a_refusal_answers_plainly_and_never_stores_the_partial_reply(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        lead=[
            reply(
                text("Partial words"),
                stop="refusal",
                stop_details={"type": "refusal", "category": "cyber", "explanation": None},
            )
        ]
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert outcome.stopped is None and outcome.error is None
    assert outcome.text.startswith("Claude declined to answer this request")
    assert [b["text"] for b in outcome.blocks if b["type"] == "text"] == [outcome.text]
    assert outcome.api_content == []


def test_a_server_error_is_asked_once_more(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[server_error(), reply(text("Fine now."))])
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert outcome.text == "Fine now." and outcome.stopped is None
    assert len(provider.lead_calls) == 2
    assert len(harness.ledger.rows) == 2 and harness.ledger.rows[0]["error"] is not None


def test_a_second_failure_stops_with_a_plain_error(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[server_error(), server_error()])
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert outcome.stopped == "error"
    assert outcome.error == "The AI service had a problem (error 529). Please ask again."
    assert outcome.api_content == []


def test_a_failed_attempts_partial_text_is_not_stored(tmp_path: Path) -> None:
    broken = reply(text("Garbled start"))
    broken.insert(4, {"type": "x", "__raise__": server_error()})
    provider = ScriptedProvider(lead=[broken, reply(text("Clean answer."))])
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert [b["text"] for b in outcome.blocks if b["type"] == "text"] == ["Clean answer."]


def test_a_reply_from_another_model_is_not_used(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[reply(text("Hi"), model="claude-sonnet-4-6")])
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert outcome.stopped == "error" and "different model" in str(outcome.error)


def test_a_dated_snapshot_and_a_refusal_fallback_are_accepted(tmp_path: Path) -> None:
    fallback = {
        "type": "fallback",
        "from": {"model": "claude-opus-5-5"},
        "to": {"model": "claude-opus-4-8"},
    }
    provider = ScriptedProvider(
        lead=[
            reply(thinking("declined part"), fallback, text("Answer."), model="claude-opus-4-8"),
        ]
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert outcome.text == "Answer."
    # The declined model's thinking and the switch marker are left out of the history.
    assert outcome.api_content[-1]["content"] == [{"type": "text", "text": "Answer."}]


def test_a_reply_cut_at_max_tokens_does_not_run_its_tool(tmp_path: Path) -> None:
    events = reply(
        text("Writing"),
        tool("a", "create_file", {"path": "x", "file_text": "y", "description": "z"}),
        stop="max_tokens",
    )
    # The tool input never finished.
    events = [e for e in events if not (e["type"] == "content_block_stop" and e["index"] == 1)]
    provider = ScriptedProvider(lead=[events])
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert not (harness.workspace.home / "x").exists()
    assert "length limit" in outcome.text
    assert outcome.api_content[-1]["role"] == "user"
    assert outcome.api_content[-1]["content"][0]["is_error"] is True


def test_a_conversation_already_moved_to_the_fallback_model_is_accepted(tmp_path: Path) -> None:
    events = reply(text("From the fallback."), model="claude-opus-4-8")
    events[-2]["usage"]["iterations"] = [{"type": "message"}, {"type": "fallback_message"}]
    harness = Harness(tmp_path, ScriptedProvider(lead=[events]))

    outcome = harness.run()

    assert outcome.stopped is None and outcome.text == "From the fallback."


class _SilentStream:
    """A stream that sends its start, then nothing until it is closed (a long, quiet think)."""

    def __init__(self) -> None:
        self.closed = threading.Event()

    def __iter__(self):  # type: ignore[no-untyped-def]
        yield reply(text("x"))[0]
        self.closed.wait(10)
        raise OSError("the stream was closed")

    def close(self) -> None:
        self.closed.set()


class _SilentProvider:
    name = "anthropic"

    def __init__(self) -> None:
        self.stream = _SilentStream()

    def open_stream(self, params: Mapping[str, Any]) -> _SilentStream:
        return self.stream


def test_cancel_closes_a_quiet_stream_at_once(tmp_path: Path) -> None:
    provider = _SilentProvider()
    harness = Harness(tmp_path, ScriptedProvider())
    harness.engine.provider = provider  # type: ignore[assignment]
    done: list[MessageOutcome] = []
    worker = threading.Thread(target=lambda: done.append(harness.engine.run()))
    worker.start()
    for _ in range(100):
        if harness.engine._streams:
            break
        threading.Event().wait(0.01)

    harness.cancel.set()
    harness.engine.stop_streams()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert done[0].stopped == "cancelled" and done[0].error is None
    assert len(harness.ledger.rows) == 1  # recorded with what it used


# ---- Claude Fable 5.1, refusals and refusal fallbacks (#1577) ---------------------------------

FABLE = "claude-fable-5-1"


def test_a_fable_lead_names_no_fable_helper_and_its_helpers_default_to_sonnet(
    tmp_path: Path,
) -> None:
    task = "Read pages 1-2."
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("ag1", "Agent", {"description": "Pages 1-2", "prompt": task}),
                tool("ag2", "Agent", {"description": "As Fable", "prompt": "x", "model": "fable"}),
                stop="tool_use",
                model=FABLE,
            ),
            reply(text("Done."), model=FABLE),
        ],
        helpers={task: [reply(text("Nothing differs."), model=SONNET)]},
    )
    harness = Harness(tmp_path, provider, model=FABLE)

    outcome = harness.run()

    assert outcome.stopped is None
    assert _sent(provider.helper_calls[task][0])["model"] == SONNET
    # A helper on Fable is refused in plain words (the Agent tool takes opus, sonnet or haiku).
    results = outcome.api_content[2]["content"]
    refused = next(r for r in results if r["tool_use_id"] == "ag2")
    assert refused["is_error"] is True and "opus, sonnet or haiku" in json.dumps(refused)


def test_a_fable_refusal_before_any_output_answers_plainly(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        lead=[
            reply(stop="refusal", model=FABLE, stop_details={"type": "refusal", "category": None})
        ]
    )
    harness = Harness(tmp_path, provider, model=FABLE)

    outcome = harness.run()

    assert outcome.stopped is None and outcome.error is None
    assert outcome.text.startswith("Claude declined to answer this request.")
    assert outcome.api_content == [] and "fallback" not in harness.names()


def test_a_fallback_is_shown_once_stored_and_priced_per_attempt(tmp_path: Path) -> None:
    switch = {"type": "fallback", "from": {"model": FABLE}, "to": {"model": "claude-opus-4-8"}}
    events = reply(
        thinking("declined part"),
        switch,
        text("The sink differs."),
        model="claude-opus-4-8",
        usage={"input_tokens": 2000, "output_tokens": 400},
    )
    attempts = [
        {
            "type": "message",
            "model": FABLE,
            "input_tokens": 1000,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 40_000,
        },
        {
            "type": "fallback_message",
            "model": "claude-opus-4-8",
            "input_tokens": 2000,
            "output_tokens": 400,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    ]
    events[-2]["usage"] = {"output_tokens": 400, "iterations": attempts}
    harness = Harness(tmp_path, ScriptedProvider(lead=[events]), model=FABLE)

    outcome = harness.run()

    assert outcome.text == "The sink differs."
    line = {"from_model": FABLE, "to_model": "claude-opus-4-8"}
    assert [b for b in outcome.blocks if b["type"] == "fallback"] == [
        {"type": "fallback", "fallback": line}
    ]
    assert [e["fallback"] for e in events_named(harness.events, "fallback")] == [line]
    # One Usage row per attempt, each on its own model; Fable's cache reads at 0.025 times.
    rows = harness.ledger.rows
    assert [row["model_key"] for row in rows] == [
        "anthropic.claude-fable-5-1",
        "anthropic.claude-opus-4-8",
    ]
    assert (rows[0]["input_tokens"], rows[0]["output_tokens"]) == (1000 + 1000, 0)
    assert rows[0]["stop_reason"] is None and rows[1]["stop_reason"] == "end_turn"
    fable = call_cost_micros(RATES, "anthropic.claude-fable-5-1", 2000, 0)
    opus = call_cost_micros(RATES, "anthropic.claude-opus-4-8", 2000, 400)
    assert fable is not None and opus is not None
    assert outcome.cost_micros == fable + opus
    # The declined model's thinking and the switch marker stay out of the history.
    assert outcome.api_content[-1]["content"] == [{"type": "text", "text": "The sink differs."}]


def test_a_sticky_fallback_turn_is_shown_against_the_model_asked(tmp_path: Path) -> None:
    events = reply(text("From the fallback."), model="claude-opus-4-8")
    events[-2]["usage"]["iterations"] = [{"type": "fallback_message"}]
    harness = Harness(tmp_path, ScriptedProvider(lead=[events]), model=FABLE)

    outcome = harness.run()

    assert [b["fallback"] for b in outcome.blocks if b["type"] == "fallback"] == [
        {"from_model": FABLE, "to_model": "claude-opus-4-8"}
    ]


def test_a_fallback_model_without_a_price_is_priced_as_the_model_asked(tmp_path: Path) -> None:
    events = reply(text("Answer."), model="claude-opus-9")
    events[-2]["usage"] = {
        "output_tokens": 10,
        "iterations": [
            {
                "type": "fallback_message",
                "model": "claude-opus-9",
                "input_tokens": 100,
                "output_tokens": 10,
            }
        ],
    }
    harness = Harness(tmp_path, ScriptedProvider(lead=[events]), model=FABLE)

    outcome = harness.run()

    assert harness.ledger.rows[0]["model_key"] == "anthropic.claude-fable-5-1"
    assert outcome.cost_micros == call_cost_micros(RATES, "anthropic.claude-fable-5-1", 100, 10)


def test_fable_on_an_account_without_data_retention_says_so(tmp_path: Path) -> None:
    missing = ProviderFailure(
        "The AI service refused the request (error 404).", status=404, retryable=False
    )
    harness = Harness(tmp_path, ScriptedProvider(lead=[missing]), model=FABLE)

    outcome = harness.run()

    assert outcome.stopped == "error"
    assert outcome.error is not None and "30-day data retention" in outcome.error
    assert "Pick Claude Opus 5.5" in outcome.error


def test_thinking_the_api_dropped_for_a_changed_conversation_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    events = reply(text("Hi."), model=FABLE)
    events[0]["message"]["input_transformations"] = [
        {
            "type": "thinking_dropped",
            "path": "messages.1.content.0",
            "reason": "prefix_binding_mismatch",
        },
        {
            "type": "thinking_dropped",
            "path": "messages.3.content.0",
            "reason": "model_binding_mismatch",
        },
    ]
    harness = Harness(tmp_path, ScriptedProvider(lead=[events]), model=FABLE)

    with caplog.at_level("INFO"):
        harness.run()

    assert harness.engine.mismatched_thinking == 1
    assert "no longer match their conversation" in caplog.text
    assert "from another model left out" in caplog.text


def test_a_chats_kept_system_prompt_and_tools_go_out_unchanged(tmp_path: Path) -> None:
    """#1577: the engine sends the chat's own lead prompt and tools, not today's."""
    kept_tools = [{"name": "Agent", "description": "old", "input_schema": {"type": "object"}}]
    provider = ScriptedProvider(lead=[reply(text("Fine."))])
    harness = Harness(tmp_path, provider)
    harness.engine.lead_system = "An older lead prompt."
    harness.engine.lead_tools = tuple(kept_tools)

    harness.run()

    sent = _sent(provider.lead_calls[0])
    assert sent["system"][0]["text"] == "An older lead prompt."
    assert strip_cache_control(sent["tools"]) == kept_tools


def test_a_paused_tool_search_is_sent_back_as_it_is_and_the_answer_goes_on(
    tmp_path: Path,
) -> None:
    searched = {
        "type": "server_tool_use",
        "id": "srv_1",
        "name": "tool_search_tool_regex",
        "input": {"query": "facts"},
    }
    paused = reply(text("Searching my tools."), searched, stop="pause_turn")
    provider = ScriptedProvider(lead=[paused, reply(text("Done."))])
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert outcome.text.endswith("Done.")
    second = _sent(provider.lead_calls[1])
    # The paused turn goes back unchanged, with no turn of ours after it.
    assert second["messages"][-1]["role"] == "assistant"
    assert strip_cache_control(second["messages"][-1]["content"]) == [
        {"type": "text", "text": "Searching my tools."},
        searched,
    ]
    # Anthropic's tool search leads the tools; the deferred tools are sent with their flag.
    assert second["tools"][0]["type"] == "tool_search_tool_regex_20251119"
    assert any(tool.get("defer_loading") for tool in second["tools"])
