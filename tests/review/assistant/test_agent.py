"""The assistant as a tool-using agent, with claude.ai's tool shapes (owner, 2026-10-16). Scripted
models; no real call, $0.

What is asserted:

- a model that greps the text (`bash_tool`), renders a page and `view`s it, then answers, gets each
  result back in the next turn (the picture as an image block, its own turn echoed unchanged), the
  step labels are its own `description`s, the answer carries its steps, and every call is recorded
  under the agent's prompt id; cache breakpoints sit on the tools, the system prompt and the newest
  block;
- `view` shows a text file with numbered lines (and a line range) and a folder two levels deep;
- the tools check their arguments: a page outside the package, an unknown argument (another
  package's id), a path outside the two folders, a PDF given to `view`, an empty command, an unknown
  tool are each a refused result the model reads, never a crash and never a read;
- `TaskCreate` / `TaskUpdate` keep the task list ("Added task", "Started task", "Completed task")
  and the answer carries it as Progress;
- `Agent` calls of one turn run in parallel (a barrier proves it), at most four, each report goes
  back to the lead; one failing does not stop the others; they share the question's budget; a
  question that delegates gets the larger cap; helpers run on Sonnet unless asked for opus;
- "approve page 3" is refused before any model call, with the sandbox never opened;
- the cost cap stops the loop cleanly; after the last tool turn the model must answer; a provider
  failure is a plain error; without a sandbox the one-call assistant answers.

All names, numbers and pages here are made up.
"""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.agent import (
    AGENT_PROMPT_ID,
    AGENT_SYSTEM_PROMPT,
    SUB_PROMPT_ID,
    SUB_SYSTEM_PROMPT,
    TOOLS,
    AgentFiles,
    AgentTools,
    billed_input_tokens,
    estimate_micros,
    first_message,
    request_body,
    run_tool,
)
from app.review.assistant.contract import AnswerEvent, AssistantRequest, ErrorEvent
from app.review.assistant.model import ModelRefused
from app.review.assistant.placeholders import placeholder_guide
from app.review.assistant.sandbox import SCRATCH, UPLOADS, SandboxResult, Workspace
from app.review.assistant.service import AssistantBudget, AssistantRuntime, stream_answer
from app.runs.rates import ModelRate, ModelRates
from tests.review.assistant.synthetic import snapshot

SONNET = "anthropic.claude-sonnet-5-5"
OPUS = "anthropic.claude-opus-5-5"
PDF = f"{UPLOADS}/Made-up vendor set.pdf"


def _png(width: int = 40, height: int = 30) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return out.getvalue()


@dataclass
class FakeSandbox:
    """Answers commands from a script; `pdftoppm` writes a picture into the scratch folder."""

    workspace: Workspace
    kind: str = "fake"
    commands: list[str] = field(default_factory=list)

    def run(self, command: str) -> SandboxResult:
        self.commands.append(command)
        if command.startswith("pdftoppm"):
            (self.workspace.out / "p-02.png").write_bytes(_png(3000, 2000))
            return SandboxResult(output="", exit_code=0)
        return SandboxResult(output="12: MADE-UP COUNTERTOP 37 3/8\n", exit_code=0)


def _use(call_id: str, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": dict(arguments)}


def _bash(call_id: str, command: str, why: str = "Made-up step") -> dict[str, Any]:
    return _use(call_id, "bash_tool", {"command": command, "description": why})


def _turn(*blocks: dict[str, Any], stop: str = "tool_use") -> dict[str, Any]:
    return {
        "stop_reason": stop,
        "content": [{"type": "thinking", "thinking": "", "signature": "sig-made-up"}, *blocks],
        "usage": {"input_tokens": 2000, "output_tokens": 100},
        "model": "anthropic/claude-sonnet-5.5",
    }


def _final(text: str) -> dict[str, Any]:
    body = {"text": text, "evidence": [], "actions": []}
    return _turn({"type": "text", "text": json.dumps(body)}, stop="end_turn")


@dataclass
class ScriptedAgentModel:
    """Replies with the scripted turns in order and keeps every body it was sent."""

    turns: list[Mapping[str, Any]]
    model_id: str = SONNET
    bodies: list[Mapping[str, Any]] = field(default_factory=list)

    def agent_turn(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        self.bodies.append(json.loads(json.dumps(body)))
        if not self.turns:
            raise AssertionError("the model was asked more times than scripted")
        return self.turns.pop(0)

    def answer(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        raise AssertionError("the tool-using assistant must not take the one-call path")


@dataclass
class FakeLedger:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def before_call(self) -> None:
        pass

    def record(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


@pytest.fixture
def workspace() -> Any:
    space = Workspace.create([("Made-up vendor set.pdf", b"%PDF-1.4 made up")])
    yield space
    space.close()


def _tools(workspace: Workspace, opened: list[int] | None = None) -> Any:
    def factory(records: Any) -> AgentTools:
        if opened is not None:
            opened.append(1)
        return AgentTools(
            sandbox=FakeSandbox(workspace),
            snapshot=records,
            files=(AgentFiles(name="Made-up vendor set.pdf", kind="the vendor's", pages=3),),
            extra_results=lambda page: {"ai_page_review": [], "job_values": {"page": page}},
        )

    return factory


def _rates() -> ModelRates:
    return ModelRates(
        source="made-up test prices",
        retrieved=date(2026, 10, 16),
        currency="USD",
        rates={
            SONNET: ModelRate(Decimal("0.002"), Decimal("0.01")),
            OPUS: ModelRate(Decimal("0.004"), Decimal("0.02")),
        },
    )


def _budget(question: str = "0.75", package: str = "2.00", spent: int = 0) -> AssistantBudget:
    return AssistantBudget(
        package_cap_micros=int(Decimal(package) * 1_000_000),
        question_cap_micros=int(Decimal(question) * 1_000_000),
        spent_micros=lambda: spent,
        rates=_rates(),
    )


def _ask(
    question: str,
    model: Any,
    workspace: Workspace,
    *,
    ledger: FakeLedger | None = None,
    budget: AssistantBudget | None = None,
    max_tool_turns: int = 12,
    opened: list[int] | None = None,
    lead: Any = None,
) -> list[tuple[str, Any]]:
    return list(
        stream_answer(
            AssistantRequest(question=question),
            load_snapshot=snapshot,
            runtime=AssistantRuntime(
                model=model, agent_model=lead, max_history_turns=6, max_tool_turns=max_tool_turns
            ),
            ledger=ledger or FakeLedger(),
            budget=budget,
            agent_tools=_tools(workspace, opened),
        )
    )


def _answer(events: list[tuple[str, Any]]) -> AnswerEvent:
    answer = events[-1][1]
    assert isinstance(answer, AnswerEvent)
    return answer


# ---- the shell, the viewer, the results ----------------------------------------------------------


def test_search_then_view_then_answer(workspace: Workspace) -> None:
    model = ScriptedAgentModel(
        [
            _turn(_bash("t1", f"pdftotext '{PDF}' - | grep -n -i counter", "Find the countertop")),
            _turn(
                _bash("t2", f"pdftoppm -f 2 -l 2 -r 130 -png '{PDF}' {SCRATCH}/p", "Render page 2"),
                _use(
                    "t3", "view", {"path": f"{SCRATCH}/p-02.png", "description": "Look at page 2"}
                ),
            ),
            _final('On page 2 the vendor wrote 37 3/8" for the countertop.'),
        ]
    )
    ledger = FakeLedger()
    events = _ask("What did the vendor write for the countertop?", model, workspace, ledger=ledger)

    answer = _answer(events)
    assert answer.mode == "llm"
    assert [(step.kind, step.label) for step in answer.steps] == [
        ("bash", "Find the countertop"),
        ("bash", "Render page 2"),
        ("view", "Look at page 2"),
    ]
    assert answer.steps[0].command is not None and "grep" in answer.steps[0].command
    assert answer.steps[0].output == "12: MADE-UP COUNTERTOP 37 3/8\n"
    assert answer.steps[2].path == f"{SCRATCH}/p-02.png"
    # The model's own description is shown while the step runs.
    assert ("stage", "Find the countertop") in [
        (name, data.label) for name, data in events if name == "stage"
    ]

    first, second, third = model.bodies
    names = [tool["name"] for tool in first["tools"]]
    assert names == ["bash_tool", "view", "review_results", "TaskCreate", "TaskUpdate", "Agent"]
    assert first["tool_choice"] == {"type": "auto"}
    assert first["system"] == [
        {"type": "text", "text": AGENT_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    assert first["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in first["tools"][0]
    assert first["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert PDF in first["messages"][0]["content"][0]["text"]
    # The model's own turn goes back unchanged, thinking block included.
    assert second["messages"][1]["role"] == "assistant"
    assert second["messages"][1]["content"][0]["signature"] == "sig-made-up"
    result = second["messages"][2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1"
    assert "MADE-UP COUNTERTOP 37 3/8" in result["content"][0]["text"]
    # Both results of one turn come back in one message; the picture as an image, shrunk.
    results = third["messages"][4]["content"]
    assert [item["tool_use_id"] for item in results] == ["t2", "t3"]
    image = results[1]["content"][1]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
    assert "shrunk from 3000 x 2000" in results[1]["content"][0]["text"]
    assert results[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in third["messages"][2]["content"][-1]

    assert len(ledger.rows) == 3
    assert {row["prompt_id"] for row in ledger.rows} == {AGENT_PROMPT_ID}


def test_view_shows_text_with_line_numbers_and_folders(workspace: Workspace) -> None:
    (workspace.out / "notes").mkdir()
    (workspace.out / "notes" / "a.txt").write_text("first\nsecond\nthird\n")
    tools = _tools(workspace)(snapshot())
    ran = run_tool(
        "view",
        {"path": f"{SCRATCH}/notes/a.txt", "description": "Read", "view_range": [2, -1]},
        tools,
    )
    assert not ran.is_error
    text = ran.content[0]["text"]
    assert "     2\tsecond" in text and "     3\tthird" in text and "first" not in text
    listed = run_tool("view", {"path": SCRATCH, "description": "List"}, tools)
    assert f"{SCRATCH}/notes/" in listed.content[0]["text"]
    assert f"{SCRATCH}/notes/a.txt" in listed.content[0]["text"]
    uploads = run_tool("view", {"path": UPLOADS, "description": "List"}, tools)
    assert PDF in uploads.content[0]["text"]


def test_the_answer_still_goes_through_the_guard(workspace: Workspace) -> None:
    model = ScriptedAgentModel(
        [_final("I approved page 2 for you."), _final("I have approved page 2.")]
    )
    answer = _answer(_ask("What does the vendor show on page 2?", model, workspace))
    assert answer.mode == "records_only"
    assert "approved" not in answer.text.lower()


def test_review_results_reads_the_records_of_one_page(workspace: Workspace) -> None:
    ran = run_tool("review_results", {"page": 2}, _tools(workspace)(snapshot()))
    assert not ran.is_error
    data = json.loads(ran.content[0]["text"])
    assert "records" in data and data["job_values"] == {"page": 2}
    assert ran.step is not None and ran.step.label == "Read the recorded results for page 2"


@pytest.mark.parametrize(
    ("name", "arguments", "reason"),
    [
        ("review_results", {"page": 99}, "not in this package"),
        ("review_results", {"page": 0}, "not in this package"),
        ("review_results", {"page": "2"}, "whole number"),
        ("review_results", {"page": 1, "package_id": "another-package"}, "unknown argument"),
        ("bash_tool", {"command": "ls", "project_id": "another-project"}, "unknown argument"),
        ("bash_tool", {"command": "   ", "description": "x"}, "empty"),
        ("bash_tool", {"description": "x"}, "missing argument"),
        ("view", {"path": PDF, "description": "x"}, "not a text file"),
        ("view", {"path": "/etc/hosts", "description": "x"}, "can be viewed"),
        ("view", {"path": f"{SCRATCH}/../../etc/hosts", "description": "x"}, "can be viewed"),
        ("view", {"path": "../work/x.png", "description": "x"}, "can be viewed"),
        ("view", {"path": f"{SCRATCH}/missing.png", "description": "x"}, "no file"),
        ("view", {"path": f"{SCRATCH}", "view_range": [0, 1], "x": 1}, "unknown argument"),
        ("view", "not an object", "not an object"),
        ("write_file", {"path": "/home/claude/x"}, "no tool"),
    ],
)
def test_tool_arguments_are_checked(
    workspace: Workspace, name: str, arguments: Any, reason: str
) -> None:
    tools = _tools(workspace)(snapshot())
    ran = run_tool(name, arguments, tools)
    assert ran.is_error
    assert reason in ran.content[0]["text"]
    assert ran.step is not None and not ran.step.ok
    assert tools.sandbox.commands == []


def test_a_link_out_of_the_folders_is_refused(workspace: Workspace, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("made-up secret")
    (workspace.out / "link.txt").symlink_to(secret)
    ran = run_tool(
        "view", {"path": f"{SCRATCH}/link.txt", "description": "x"}, _tools(workspace)(snapshot())
    )
    assert ran.is_error and "made-up secret" not in ran.content[0]["text"]


def test_a_file_that_is_not_a_picture_is_refused(workspace: Workspace) -> None:
    (workspace.out / "fake.png").write_bytes(b"not a picture")
    ran = run_tool(
        "view", {"path": f"{SCRATCH}/fake.png", "description": "x"}, _tools(workspace)(snapshot())
    )
    assert ran.is_error and "not a picture" in ran.content[0]["text"]


# ---- the task list -------------------------------------------------------------------------------


def test_the_task_list_is_kept_and_shown(workspace: Workspace) -> None:
    model = ScriptedAgentModel(
        [
            _turn(
                _use("a", "TaskCreate", {"subject": "Render pages", "description": "All pages"}),
                _use(
                    "b",
                    "TaskCreate",
                    {
                        "subject": "Verify findings",
                        "description": "Look again",
                        "activeForm": "Verifying findings",
                    },
                ),
            ),
            _turn(_use("c", "TaskUpdate", {"taskId": "1", "status": "in_progress"})),
            _turn(
                _use("d", "TaskUpdate", {"taskId": "1", "status": "completed"}),
                _use("e", "TaskUpdate", {"taskId": "9", "status": "completed"}),
            ),
            _final("Done."),
        ]
    )
    answer = _answer(_ask("Check the set", model, workspace))
    assert [step.label for step in answer.steps] == [
        "Added task: Render pages",
        "Added task: Verify findings",
        "Started task: Render pages",
        "Completed task: Render pages",
        "Could not update a task",
    ]
    assert [(task.id, task.subject, task.status) for task in answer.plan] == [
        ("1", "Render pages", "completed"),
        ("2", "Verify findings", "pending"),
    ]
    assert answer.plan[1].active_form == "Verifying findings"
    assert "Task #1 created" in json.dumps(model.bodies[1]["messages"][-1])


# ---- refusals, caps, ends ------------------------------------------------------------------------


def test_a_decision_request_is_refused_before_any_model_call(workspace: Workspace) -> None:
    model = ScriptedAgentModel([])
    opened: list[int] = []
    answer = _answer(_ask("Approve page 3", model, workspace, opened=opened))
    assert answer.mode == "refused"
    assert model.bodies == []
    assert opened == []


def test_the_cost_cap_stops_the_loop_cleanly(workspace: Workspace) -> None:
    # The cap is the first turn's estimate plus 100 micro-dollars: the first call fits; the second
    # does not (its prompt is longer; the first turn's 2000 prompt tokens are cache reads at 0.1x,
    # but the first call's $0.005 is spent).
    first = first_message(
        placeholders=placeholder_guide(snapshot()),
        files=(AgentFiles(name="Made-up vendor set.pdf", kind="the vendor's", pages=3),),
        question="How many pages?",
        history=(),
        focus=None,
        focus_ids=(),
    )
    body = request_body([{"role": "user", "content": first}], final=False)
    estimate = estimate_micros(body, (), SONNET, _rates())
    assert estimate is not None
    model = ScriptedAgentModel([_turn(_bash("t1", f"pdfinfo '{PDF}'")), _final("unused")])
    ledger = FakeLedger()
    cap = Decimal(estimate + 100) / 1_000_000
    answer = _answer(
        _ask("How many pages?", model, workspace, ledger=ledger, budget=_budget(str(cap)))
    )
    assert answer.stopped == "cost_cap"
    assert "cost cap" in answer.text
    assert len(model.bodies) == 1 and len(ledger.rows) == 1
    assert [step.kind for step in answer.steps] == ["bash"]


def test_the_package_budget_stops_before_the_first_call(workspace: Workspace) -> None:
    model = ScriptedAgentModel([_final("unused")])
    answer = _answer(
        _ask("How many pages?", model, workspace, budget=_budget(package="2.00", spent=1_999_000))
    )
    assert answer.stopped == "cost_cap"
    assert model.bodies == []


def test_after_the_last_tool_turn_the_model_must_answer(workspace: Workspace) -> None:
    model = ScriptedAgentModel(
        [
            _turn(_bash("t1", f"pdfinfo '{PDF}'")),
            _turn(_bash("t2", f"pdfinfo '{PDF}'")),
            _final("It has 3 pages."),
        ]
    )
    answer = _answer(_ask("How many pages?", model, workspace, max_tool_turns=2))
    assert answer.mode == "llm"
    assert [body["tool_choice"] for body in model.bodies] == [
        {"type": "auto"},
        {"type": "auto"},
        {"type": "none"},
    ]


def test_a_provider_failure_is_a_plain_error(workspace: Workspace) -> None:
    @dataclass
    class Busy(ScriptedAgentModel):
        def agent_turn(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
            raise ModelRefused(429)

    events = _ask("How many pages?", Busy([]), workspace)
    assert events[-1][0] == "error"
    assert isinstance(events[-1][1], ErrorEvent) and events[-1][1].code == "model_busy"


def test_no_sandbox_falls_back_to_one_call() -> None:
    @dataclass
    class OneCall(ScriptedAgentModel):
        answered: list[int] = field(default_factory=list)

        def answer(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
            self.answered.append(1)
            body = {"text": "From the records.", "evidence": [], "actions": []}
            return {
                "stopReason": "end_turn",
                "output": {"message": {"content": [{"type": "text", "text": json.dumps(body)}]}},
                "usage": {"inputTokens": 10, "outputTokens": 10},
            }

    model = OneCall([])
    list(
        stream_answer(
            AssistantRequest(question="How many pages?"),
            load_snapshot=snapshot,
            runtime=AssistantRuntime(model=model, max_history_turns=6),
            ledger=FakeLedger(),
            agent_tools=lambda records: None,
        )
    )
    assert model.answered == [1] and model.bodies == []


def test_the_tools_have_claude_ai_shapes_and_the_prompt_keeps_the_rules() -> None:
    by_name = {tool["name"]: tool["input_schema"] for tool in TOOLS}
    assert by_name["bash_tool"]["required"] == ["command", "description"]
    assert by_name["view"]["required"] == ["path", "description"]
    assert "view_range" in by_name["view"]["properties"]
    assert by_name["TaskCreate"]["required"] == ["subject", "description"]
    assert "activeForm" in by_name["TaskCreate"]["properties"]
    assert by_name["TaskUpdate"]["required"] == ["taskId", "status"]
    assert by_name["Agent"]["required"] == ["description", "prompt"]
    assert by_name["Agent"]["properties"]["model"]["enum"] == ["sonnet", "opus"]
    for schema in by_name.values():
        assert schema["additionalProperties"] is False
    prompt = AGENT_SYSTEM_PROMPT
    assert UPLOADS in prompt and SCRATCH in prompt
    assert "GV's markup is the reviewer's comments, never the design" in prompt
    assert "never use a number from GV's markup" in prompt
    assert "VERIFY" in prompt and "never contradicted" in prompt
    assert "never use a number from them" in SUB_SYSTEM_PROMPT
    assert "{{" not in prompt and "{C1.outcome}" in prompt


def test_workspace_holds_only_the_given_files_read_only(workspace: Workspace) -> None:
    assert [path.name for path in workspace.work.iterdir()] == ["Made-up vendor set.pdf"]
    with pytest.raises(PermissionError):
        (workspace.work / "Made-up vendor set.pdf").write_bytes(b"changed")
    root = workspace.root
    workspace.close()
    assert not Path(root).exists()


def test_cached_prompt_tokens_are_priced_as_cache_reads_and_writes() -> None:
    usage = {
        "input_tokens": 100,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 10_000,
        "output_tokens": 50,
    }
    # 100 + 1.25 x 1000 + 0.1 x 10000
    assert billed_input_tokens(usage) == 2350


# ---- helper agents (`Agent`) ---------------------------------------------------------------------


def _report(page: int) -> dict[str, Any]:
    body = {
        "summary": f"Made-up check of page {page}.",
        "findings": [
            {
                "page": page,
                "what": "overall length",
                "architect": "120",
                "vendor": "118 1/2",
                "gv_flagged": "no",
                "confidence": "medium",
            }
        ],
    }
    return _turn({"type": "text", "text": json.dumps(body)}, stop="end_turn")


@dataclass
class DispatchModel:
    """The lead follows `main`; each helper (told apart by its prompt, `Check page N`) runs one
    command, then reports. `meet` makes every helper's first call wait for the others: it only
    passes when they run at the same time."""

    main: list[Mapping[str, Any]]
    failing: tuple[str, ...] = ()
    meet: Any = None
    model_id: str = SONNET
    main_bodies: list[Mapping[str, Any]] = field(default_factory=list)
    helper_bodies: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    lock: Any = field(default_factory=threading.Lock)

    def agent_turn(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        if body["system"][0]["text"] != SUB_SYSTEM_PROMPT:
            self.main_bodies.append(json.loads(json.dumps(body)))
            return self.main.pop(0)
        task = body["messages"][0]["content"][1]["text"].split("\n", 1)[1]
        with self.lock:
            seen = self.helper_bodies.setdefault(task, [])
            seen.append(body)
            count = len(seen)
        if task in self.failing:
            raise ModelRefused(429)
        if count == 1:
            if self.meet is not None:
                self.meet.wait()
            return _turn(_bash(f"h-{task}", f"ls {SCRATCH}", "List the pictures"))
        return _report(int(task.rsplit(" ", 1)[1]))


def _agents(*pages: int, model: str | None = None) -> dict[str, Any]:
    calls = []
    for page in pages:
        arguments = {"description": f"Page {page}", "prompt": f"Check page {page}"}
        if model is not None:
            arguments["model"] = model
        calls.append(_use(f"a{page}", "Agent", arguments))
    return _turn(*calls)


def test_helpers_run_in_parallel_and_report_back(workspace: Workspace) -> None:
    model = DispatchModel(
        main=[
            _agents(1, 2, 3),
            _turn(_bash("v1", f"ls {SCRATCH}", "Verify the flagged pages")),
            _final("The AI reading found a different overall length on pages 1 to 3."),
        ],
        meet=threading.Barrier(3, timeout=10),
    )
    ledger = FakeLedger()
    events = _ask("Which pages differ from the architect?", model, workspace, ledger=ledger)
    answer = _answer(events)
    assert answer.mode == "llm"
    assert [step.kind for step in answer.steps] == ["agents", "bash"]
    ran = answer.steps[0]
    assert ran.label == "Ran 3 agents · Page 1"
    assert [run.description for run in ran.agents] == ["Page 1", "Page 2", "Page 3"]
    assert all(run.ok and run.model == "sonnet" for run in ran.agents)
    assert all(run.steps[0].label == "List the pictures" for run in ran.agents)
    assert '"vendor": "118 1/2"' in ran.agents[1].report
    # One report per Agent call goes back to the lead, as leads to verify.
    results = model.main_bodies[1]["messages"][-1]["content"]
    assert [item["tool_use_id"] for item in results] == ["a1", "a2", "a3"]
    assert results[1]["content"][0]["text"].startswith("Report of agent 'Page 2' (a lead to verify")
    # Helpers get only the shell and the viewer.
    helper = model.helper_bodies["Check page 2"][0]
    assert [tool["name"] for tool in helper["tools"]] == ["bash_tool", "view"]
    labels = [data.label for name, data in events if name == "stage"]
    assert "Running 3 agents · Page 1" in labels
    assert "Page 2 · List the pictures" in labels
    prompts = [row["prompt_id"] for row in ledger.rows]
    assert prompts.count(AGENT_PROMPT_ID) == 3
    assert prompts.count(SUB_PROMPT_ID) == 6


def test_at_most_four_agents_run_at_once(workspace: Workspace) -> None:
    model = DispatchModel(main=[_agents(1, 2, 3, 1, 2), _final("Checked.")])
    answer = _answer(_ask("Which pages differ?", model, workspace))
    assert len(answer.steps[0].agents) == 4
    assert answer.steps[1].label == "Could not start a helper agent"
    assert "at most 4 agents" in (answer.steps[1].output or "")


def test_one_helper_failing_does_not_stop_the_others(workspace: Workspace) -> None:
    model = DispatchModel(
        main=[_agents(1, 2), _final("Page 1 was checked; page 2 was not.")],
        failing=("Check page 2",),
    )
    answer = _answer(_ask("Which pages differ?", model, workspace))
    assert answer.mode == "llm"
    first, second = answer.steps[0].agents
    assert first.ok and first.stopped is None
    assert not second.ok and second.stopped == "failed"
    assert "did not finish" in second.report
    results = model.main_bodies[1]["messages"][-1]["content"]
    assert "is_error" not in results[0] and results[1]["is_error"] is True


def test_helpers_share_the_question_budget(workspace: Workspace) -> None:
    # Cap $0.10: once the lead has spent its first call, three helpers share 75% of what is left,
    # too little for any helper's first call; each stops at the cost cap without a call, and the
    # lead still answers.
    model = DispatchModel(main=[_agents(1, 2, 3), _final("No helper could finish.")])
    ledger = FakeLedger()
    answer = _answer(
        _ask("Which pages differ?", model, workspace, ledger=ledger, budget=_budget("0.10"))
    )
    assert answer.mode == "llm"
    assert [run.stopped for run in answer.steps[0].agents] == ["cost_cap"] * 3
    assert model.helper_bodies == {}
    assert [row["prompt_id"] for row in ledger.rows] == [AGENT_PROMPT_ID] * 2


def test_a_delegating_question_gets_the_larger_cap(workspace: Workspace) -> None:
    # The question cap ($0.06) allows the lead's first call only; once it delegates, the
    # delegating cap ($3) lets the helpers and the lead's answer through.
    budget = AssistantBudget(
        package_cap_micros=10_000_000,
        question_cap_micros=60_000,
        delegate_cap_micros=3_000_000,
        spent_micros=lambda: 0,
        rates=_rates(),
    )
    model = DispatchModel(main=[_agents(1), _final("Page 1 differs.")])
    answer = _answer(_ask("Which pages differ?", model, workspace, budget=budget))
    assert answer.mode == "llm" and answer.stopped is None
    assert answer.steps[0].agents[0].ok


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({"description": "Page 1"}, "missing argument"),
        ({"description": "", "prompt": "x"}, "needs a description"),
        ({"description": "P", "prompt": "x", "model": "haiku"}, "sonnet or opus"),
        ({"description": "P", "prompt": "x", "subagent_type": "other"}, "unknown argument"),
        ({"description": "P", "prompt": "x" * 5000}, "longer than"),
    ],
)
def test_agent_arguments_are_checked(
    workspace: Workspace, arguments: dict[str, Any], reason: str
) -> None:
    model = DispatchModel(main=[_turn(_use("a1", "Agent", arguments)), _final("Could not.")])
    answer = _answer(_ask("Which pages differ?", model, workspace))
    step = answer.steps[0]
    assert step.kind == "agents" and not step.ok and reason in (step.output or "")
    assert model.helper_bodies == {}


def test_the_lead_and_the_helpers_use_their_own_models(workspace: Workspace) -> None:
    lead = DispatchModel(main=[_agents(1), _agents(2, model="opus"), _final("Page 1 differs.")])
    lead.model_id = OPUS
    helper = DispatchModel(main=[])
    answer = _answer(_ask("Which pages differ?", helper, workspace, lead=lead))
    assert len(lead.main_bodies) == 3
    assert list(helper.helper_bodies) == ["Check page 1"] and helper.main_bodies == []
    # An Agent call that asks for opus runs on the lead's model.
    assert list(lead.helper_bodies) == ["Check page 2"]
    assert [run.model for step in answer.steps for run in step.agents] == ["sonnet", "opus"]


def test_a_refused_answer_is_written_again_once(workspace: Workspace) -> None:
    model = ScriptedAgentModel(
        [
            _final("I confirmed page 2 for you."),
            _final('On page 2 the vendor wrote 37 3/8" for the countertop.'),
        ]
    )
    answer = _answer(_ask("What does the vendor show on page 2?", model, workspace))
    assert answer.mode == "llm" and "37 3/8" in answer.text
    second = model.bodies[1]
    assert second["tool_choice"] == {"type": "none"}
    note = second["messages"][-1]["content"][-1]["text"]
    assert note.startswith("The app could not publish that answer: it says you did")
    # Only once: a second refusal falls back to the records.
    again = ScriptedAgentModel([_final("I approved it."), _final("I approved it again.")])
    assert (
        _answer(_ask("What does the vendor show on page 2?", again, workspace)).mode
        == "records_only"
    )
    assert len(again.bodies) == 2
