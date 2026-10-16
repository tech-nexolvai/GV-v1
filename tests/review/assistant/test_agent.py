"""The assistant as a tool-using agent (owner, 2026-10-16). A scripted model; no real call, $0.

What is asserted:

- a model that runs a command (`pdftotext | grep`), renders and views a picture, then answers, gets
  each tool's result back in the next turn (the picture as an image block, its own turn echoed
  unchanged), and the answer is published with its steps, every call recorded under the agent's
  prompt id;
- the tools check their arguments: a page outside the package, an unknown argument (another
  package's id), a picture outside /out, a file that is not a picture, an empty command, an unknown
  tool are each a refused result the model reads, never a crash and never a read;
- "approve page 3" is refused before any model call, with the sandbox never opened;
- the cost cap stops the loop cleanly before a call that would pass it, and the answer says so;
- after the last tool turn the model is asked with `tool_choice: none`, so it must answer;
- a provider failure is a plain error event.

All names, numbers and pages here are made up.
"""

from __future__ import annotations

import io
import json
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
from app.review.assistant.sandbox import SandboxResult, Workspace
from app.review.assistant.service import (
    AssistantBudget,
    AssistantRuntime,
    stream_answer,
)
from app.runs.rates import ModelRate, ModelRates
from tests.review.assistant.synthetic import snapshot

SONNET = "anthropic.claude-sonnet-5-5"


def _png(width: int = 40, height: int = 30) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return out.getvalue()


@dataclass
class FakeSandbox:
    """Answers commands from a script; `pdftoppm` writes a picture into /out."""

    workspace: Workspace
    kind: str = "fake"
    commands: list[str] = field(default_factory=list)

    def run(self, command: str) -> SandboxResult:
        self.commands.append(command)
        if command.startswith("pdftoppm"):
            (self.workspace.out / "p-02.png").write_bytes(_png(3000, 2000))
            return SandboxResult(output="", exit_code=0)
        return SandboxResult(output="12: COUNTERTOP 37 3/8\n", exit_code=0)


def _tool_use(call_id: str, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": dict(arguments)}


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
        # A deep copy, so later turns appended to the list do not change what was sent.
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
        rates={SONNET: ModelRate(Decimal("0.002"), Decimal("0.01"))},
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
    model: ScriptedAgentModel,
    workspace: Workspace,
    *,
    ledger: FakeLedger | None = None,
    budget: AssistantBudget | None = None,
    max_tool_turns: int = 12,
    opened: list[int] | None = None,
) -> list[tuple[str, Any]]:
    return list(
        stream_answer(
            AssistantRequest(question=question),
            load_snapshot=snapshot,
            runtime=AssistantRuntime(
                model=model, max_history_turns=6, max_tool_turns=max_tool_turns
            ),
            ledger=ledger or FakeLedger(),
            budget=budget,
            agent_tools=_tools(workspace, opened),
        )
    )


def test_search_then_view_then_answer(workspace: Workspace) -> None:
    model = ScriptedAgentModel(
        [
            _turn(
                _tool_use(
                    "t1",
                    "bash",
                    {"command": "pdftotext '/work/Made-up vendor set.pdf' - | grep -n -i counter"},
                )
            ),
            _turn(
                _tool_use(
                    "t2",
                    "bash",
                    {"command": "pdftoppm -f 2 -l 2 -r 100 -png '/work/Made-up vendor set.pdf' p"},
                ),
                _tool_use("t3", "view", {"path": "/out/p-02.png"}),
            ),
            _final('On page 2 the vendor wrote 37 3/8" for the countertop.'),
        ]
    )
    ledger = FakeLedger()
    events = _ask("What did the vendor write for the countertop?", model, workspace, ledger=ledger)

    answer = events[-1][1]
    assert isinstance(answer, AnswerEvent)
    assert answer.mode == "llm"
    assert "37 3/8" in answer.text
    assert [step.kind for step in answer.steps] == ["bash", "bash", "view"]
    assert answer.steps[0].command is not None and "grep" in answer.steps[0].command
    assert answer.steps[0].output == "12: COUNTERTOP 37 3/8\n"
    assert answer.steps[2].path == "/out/p-02.png"
    # Each tool's turn is shown while it runs.
    assert any(name == "stage" and data.id == "tool" for name, data in events)

    first, second, third = model.bodies
    assert [tool["name"] for tool in first["tools"]] == [
        "bash",
        "view",
        "review_results",
        "plan",
        "delegate",
    ]
    assert first["tool_choice"] == {"type": "auto"}
    assert first["system"] == [
        {"type": "text", "text": AGENT_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    # Cache breakpoints: the last tool, the system prompt, and the newest block of each turn.
    assert first["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in first["tools"][0]
    assert first["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert third["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # Earlier blocks are sent without one (at most four breakpoints in a request).
    assert "cache_control" not in third["messages"][2]["content"][-1]
    files = first["messages"][0]["content"][0]["text"]
    assert "/work/Made-up vendor set.pdf" in files
    # The model's own turn goes back unchanged, thinking block included.
    assert second["messages"][1]["role"] == "assistant"
    assert second["messages"][1]["content"][0]["signature"] == "sig-made-up"
    result = second["messages"][2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1"
    assert "COUNTERTOP 37 3/8" in result["content"][0]["text"]
    # Both results of one turn come back in one message; the picture as an image, shrunk.
    results = third["messages"][4]["content"]
    assert [item["tool_use_id"] for item in results] == ["t2", "t3"]
    image = results[1]["content"][1]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
    assert "shrunk from 3000 x 2000" in results[1]["content"][0]["text"]

    assert len(ledger.rows) == 3
    assert {row["prompt_id"] for row in ledger.rows} == {AGENT_PROMPT_ID}


def test_the_answer_still_goes_through_the_guard(workspace: Workspace) -> None:
    model = ScriptedAgentModel([_final("I approved page 2 for you.")])
    answer = _ask("Is page 2 right?", model, workspace)[-1][1]
    assert isinstance(answer, AnswerEvent)
    assert answer.mode == "records_only"
    assert "approved" not in answer.text.lower()


def test_review_results_reads_the_records_of_one_page(workspace: Workspace) -> None:
    tools = _tools(workspace)(snapshot())
    ran = run_tool("review_results", {"page": 2}, tools)
    assert not ran.is_error
    data = json.loads(ran.content[0]["text"])
    assert "records" in data and data["job_values"] == {"page": 2}
    assert ran.step.label == "Read the recorded results for page 2"


@pytest.mark.parametrize(
    ("name", "arguments", "reason"),
    [
        ("review_results", {"page": 99}, "not in this package"),
        ("review_results", {"page": 0}, "not in this package"),
        ("review_results", {"page": "2"}, "whole number"),
        ("review_results", {"page": 1, "package_id": "another-package"}, "unknown argument"),
        ("bash", {"command": "ls", "project_id": "another-project"}, "unknown argument"),
        ("bash", {"command": "   "}, "empty"),
        ("view", {"path": "/work/Made-up vendor set.pdf"}, "png or jpg"),
        ("view", {"path": "/work/p.png"}, "only files in /out"),
        ("view", {"path": "/out/../work/p.png"}, "only files in /out"),
        ("view", {"path": "/etc/hosts.png"}, "only files in /out"),
        ("view", {"path": "/out/missing.png"}, "no file"),
        ("view", "not an object", "not an object"),
        ("write_file", {"path": "/out/x"}, "no tool"),
    ],
)
def test_tool_arguments_are_checked(
    workspace: Workspace, name: str, arguments: Any, reason: str
) -> None:
    tools = _tools(workspace)(snapshot())
    ran = run_tool(name, arguments, tools)
    assert ran.is_error
    assert reason in ran.content[0]["text"]
    assert not ran.step.ok
    assert tools.sandbox.commands == []


def test_a_file_that_is_not_a_picture_is_refused(workspace: Workspace) -> None:
    (workspace.out / "fake.png").write_bytes(b"not a picture")
    ran = run_tool("view", {"path": "/out/fake.png"}, _tools(workspace)(snapshot()))
    assert ran.is_error and "not a picture" in ran.content[0]["text"]


def test_a_decision_request_is_refused_before_any_model_call(workspace: Workspace) -> None:
    model = ScriptedAgentModel([])
    opened: list[int] = []
    events = _ask("Approve page 3", model, workspace, opened=opened)
    answer = events[-1][1]
    assert isinstance(answer, AnswerEvent)
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
    model = ScriptedAgentModel(
        [_turn(_tool_use("t1", "bash", {"command": "pdfinfo /work/x.pdf"})), _final("unused")]
    )
    ledger = FakeLedger()
    cap = Decimal(estimate + 100) / 1_000_000
    events = _ask("How many pages?", model, workspace, ledger=ledger, budget=_budget(str(cap)))
    answer = events[-1][1]
    assert isinstance(answer, AnswerEvent)
    assert answer.stopped == "cost_cap"
    assert "cost cap" in answer.text
    assert len(model.bodies) == 1 and len(ledger.rows) == 1
    assert [step.kind for step in answer.steps] == ["bash"]


def test_the_package_budget_stops_before_the_first_call(workspace: Workspace) -> None:
    model = ScriptedAgentModel([_final("unused")])
    events = _ask(
        "How many pages?", model, workspace, budget=_budget(package="2.00", spent=1_999_000)
    )
    answer = events[-1][1]
    assert isinstance(answer, AnswerEvent)
    assert answer.stopped == "cost_cap"
    assert model.bodies == []


def test_after_the_last_tool_turn_the_model_must_answer(workspace: Workspace) -> None:
    model = ScriptedAgentModel(
        [
            _turn(_tool_use("t1", "bash", {"command": "pdfinfo /work/x.pdf"})),
            _turn(_tool_use("t2", "bash", {"command": "pdfinfo /work/x.pdf"})),
            _final("It has 3 pages."),
        ]
    )
    answer = _ask("How many pages?", model, workspace, max_tool_turns=2)[-1][1]
    assert isinstance(answer, AnswerEvent) and answer.mode == "llm"
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


def test_the_tools_are_read_only_and_strict() -> None:
    assert [tool["name"] for tool in TOOLS] == [
        "bash",
        "view",
        "review_results",
        "plan",
        "delegate",
    ]
    for tool in TOOLS:
        assert tool["strict"] is True
        assert tool["input_schema"]["additionalProperties"] is False
    prompt = AGENT_SYSTEM_PROMPT
    assert "/work" in prompt and "/out" in prompt
    assert "GV's markup is the reviewer's comments, never the design" in prompt
    assert "never use a number from GV's markup" in prompt
    assert "verify every item a helper flags" in prompt
    assert "never use a number from them" in SUB_SYSTEM_PROMPT
    assert "never contradicted" in prompt


def test_workspace_holds_only_the_given_files_read_only(workspace: Workspace) -> None:
    assert [path.name for path in workspace.work.iterdir()] == ["Made-up vendor set.pdf"]
    with pytest.raises(PermissionError):
        (workspace.work / "Made-up vendor set.pdf").write_bytes(b"changed")
    root = workspace.root
    workspace.close()
    assert not Path(root).exists()


# ---- task list and helper agents (owner, 2026-10-16) ---------------------------------------------


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
    """The main agent follows `main`; each helper (told apart by its task title) runs one command,
    then reports. `meet` makes every helper's first call wait for the others: it only passes when
    they run at the same time."""

    main: list[Mapping[str, Any]]
    failing: tuple[str, ...] = ()
    meet: Any = None
    model_id: str = SONNET
    main_bodies: list[Mapping[str, Any]] = field(default_factory=list)
    helper_bodies: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)

    def agent_turn(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        if body["system"][0]["text"] != SUB_SYSTEM_PROMPT:
            self.main_bodies.append(json.loads(json.dumps(body)))
            return self.main.pop(0)
        task = json.loads(body["messages"][0]["content"][1]["text"].split("\n", 1)[1])
        title = task["title"]
        seen = self.helper_bodies.setdefault(title, [])
        seen.append(body)
        if title in self.failing:
            raise ModelRefused(429)
        if len(seen) == 1:
            if self.meet is not None:
                self.meet.wait()
            return _turn(_tool_use(f"h-{title}", "bash", {"command": "ls /out/pages"}))
        return _report(task["pages"][0])


def _delegation(*titles: str) -> dict[str, Any]:
    return _turn(
        _tool_use(
            "d1",
            "delegate",
            {
                "tasks": [
                    {"title": title, "instructions": "Compare the two drawings.", "pages": [n]}
                    for n, title in enumerate(titles, start=1)
                ]
            },
        )
    )


def test_helpers_run_in_parallel_and_report_back(workspace: Workspace) -> None:
    import threading

    model = DispatchModel(
        main=[
            _turn(
                _tool_use(
                    "p1",
                    "plan",
                    {
                        "tasks": [
                            {"title": "Render every page", "status": "done"},
                            {"title": "Check pages 1 to 3", "status": "in_progress"},
                        ]
                    },
                )
            ),
            _delegation("Page 1", "Page 2", "Page 3"),
            _turn(_tool_use("v1", "bash", {"command": "ls /out"})),
            _final("The AI reading found a different overall length on pages 1 to 3."),
        ],
        meet=threading.Barrier(3, timeout=10),
    )
    ledger = FakeLedger()
    events = _ask("Which pages differ from the architect?", model, workspace, ledger=ledger)
    answer = events[-1][1]
    assert isinstance(answer, AnswerEvent) and answer.mode == "llm"
    assert [step.kind for step in answer.steps] == ["plan", "delegate", "bash"]
    assert [task.title for task in answer.plan] == ["Render every page", "Check pages 1 to 3"]
    delegated = answer.steps[1]
    assert delegated.label == "Ran 3 agents · Page 1"
    assert [run.title for run in delegated.agents] == ["Page 1", "Page 2", "Page 3"]
    assert all(run.ok and run.steps[0].kind == "bash" for run in delegated.agents)
    assert '"vendor": "118 1/2"' in delegated.agents[1].report
    # The helpers' reports go back to the main agent as leads to verify.
    reports = model.main_bodies[2]["messages"][-1]["content"][0]["content"][0]["text"]
    assert reports.startswith("Helper reports (leads to verify yourself")
    # Helpers get only the shell and the viewer.
    helper = model.helper_bodies["Page 2"][0]
    assert [tool["name"] for tool in helper["tools"]] == ["bash", "view"]
    # Each helper's steps stream while they run.
    labels = [data.label for name, data in events if name == "stage"]
    assert "Running 3 agents · Page 1" in labels
    assert any(label.startswith("Agent 2 · ") for label in labels)
    # Every call is recorded: 4 main + 2 per helper.
    prompts = [row["prompt_id"] for row in ledger.rows]
    assert prompts.count(AGENT_PROMPT_ID) == 4
    assert prompts.count(SUB_PROMPT_ID) == 6


def test_one_helper_failing_does_not_stop_the_others(workspace: Workspace) -> None:
    model = DispatchModel(
        main=[_delegation("Page 1", "Page 2"), _final("Page 1 was checked; page 2 was not.")],
        failing=("Page 2",),
    )
    answer = _ask("Which pages differ?", model, workspace)[-1][1]
    assert isinstance(answer, AnswerEvent) and answer.mode == "llm"
    first, second = answer.steps[0].agents
    assert first.ok and first.stopped is None
    assert not second.ok and second.stopped == "failed"
    assert "did not finish" in second.report


def test_helpers_share_the_question_budget(workspace: Workspace) -> None:
    # Cap $0.10 for the question and for a delegating one: once the main agent has spent its
    # first call, three helpers share 75% of what is left, too little for any helper's first call;
    # each stops at the cost cap without a call, and the main agent still answers.
    budget = _budget("0.10")
    model = DispatchModel(
        main=[_delegation("Page 1", "Page 2", "Page 3"), _final("No helper could finish.")]
    )
    ledger = FakeLedger()
    answer = _ask("Which pages differ?", model, workspace, ledger=ledger, budget=budget)[-1][1]
    assert isinstance(answer, AnswerEvent) and answer.mode == "llm"
    runs = answer.steps[0].agents
    assert [run.stopped for run in runs] == ["cost_cap"] * 3
    assert model.helper_bodies == {}
    assert [row["prompt_id"] for row in ledger.rows] == [AGENT_PROMPT_ID] * 2


def test_a_delegating_question_gets_the_larger_cap(workspace: Workspace) -> None:
    # The question cap ($0.06) allows the main agent's first call only; once it delegates, the
    # delegating cap ($3) lets the helpers and the main agent's answer through.
    budget = AssistantBudget(
        package_cap_micros=10_000_000,
        question_cap_micros=60_000,
        delegate_cap_micros=3_000_000,
        spent_micros=lambda: 0,
        rates=_rates(),
    )
    model = DispatchModel(main=[_delegation("Page 1"), _final("Page 1 differs.")])
    answer = _ask("Which pages differ?", model, workspace, budget=budget)[-1][1]
    assert isinstance(answer, AnswerEvent) and answer.mode == "llm" and answer.stopped is None
    assert answer.steps[0].agents[0].ok


@pytest.mark.parametrize(
    ("tasks", "reason"),
    [
        ([], "give 1 to 4 tasks"),
        (
            [{"title": f"T{n}", "instructions": "x", "pages": [1]} for n in range(5)],
            "give 1 to 4 tasks",
        ),
        ([{"title": "T", "instructions": "x", "pages": [9]}], "not in this package"),
        ([{"title": "T", "instructions": "x", "pages": ["1"]}], "whole numbers"),
        ([{"title": "T", "pages": [1]}], "exactly a title"),
    ],
)
def test_delegate_arguments_are_checked(
    workspace: Workspace, tasks: list[Any], reason: str
) -> None:
    model = DispatchModel(
        main=[_turn(_tool_use("d1", "delegate", {"tasks": tasks})), _final("Could not delegate.")]
    )
    answer = _ask("Which pages differ?", model, workspace)[-1][1]
    assert isinstance(answer, AnswerEvent)
    step = answer.steps[0]
    assert step.kind == "delegate" and not step.ok and reason in (step.output or "")
    assert model.helper_bodies == {}


def test_cached_prompt_tokens_are_priced_as_cache_reads_and_writes() -> None:
    usage = {
        "input_tokens": 100,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 10_000,
        "output_tokens": 50,
    }
    # 100 + 1.25 x 1000 + 0.1 x 10000
    assert billed_input_tokens(usage) == 2350


def test_the_lead_and_the_helpers_use_their_own_models(workspace: Workspace) -> None:
    lead = DispatchModel(main=[_delegation("Page 1"), _final("Page 1 differs.")])
    lead.model_id = "anthropic.claude-opus-5-5"
    helper = DispatchModel(main=[])
    list(
        stream_answer(
            AssistantRequest(question="Which pages differ?"),
            load_snapshot=snapshot,
            runtime=AssistantRuntime(model=helper, agent_model=lead, max_history_turns=6),
            ledger=FakeLedger(),
            agent_tools=_tools(workspace),
        )
    )
    assert len(lead.main_bodies) == 2 and lead.helper_bodies == {}
    assert list(helper.helper_bodies) == ["Page 1"] and helper.main_bodies == []
