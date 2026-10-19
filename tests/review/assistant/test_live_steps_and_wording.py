"""The assistant panel looks and reads like claude.ai (owner, 2026-10-16). Scripted models, $0.

- Each tool the assistant uses streams as a `step` event the moment it finishes (its command and
  output, or a small picture for a `view`), the lead's with `agent` null and a helper's with its
  description, so the panel grows the step list live instead of showing only "Thinking".
- The code-written subject "On page N," never leaves a capital behind it ("On page 3, The …"),
  never goes on a table line, and no space is left before a full stop.
- The AI page review's `clouded` flag reaches the model as what it means: OUR cloud PDF will cloud
  the row. GV's own markup is "GV's reviewer comments".
- The footer names the model that actually leads (Opus 5.5 when the tool-using assistant runs).

All names, numbers and pages here are made up.
"""

from __future__ import annotations

import base64
from typing import Any

from app.review.assistant.agent import AGENT_SYSTEM_PROMPT, SUB_SYSTEM_PROMPT, run_tool
from app.review.assistant.contract import AgentStep, StepEvent, model_label
from app.review.assistant.placeholders import render
from app.review.assistant.sandbox import SCRATCH, Workspace
from tests.review.assistant import synthetic as syn
from tests.review.assistant.test_agent import (
    PDF,
    DispatchModel,
    FakeSandbox,
    ScriptedAgentModel,
    _agents,
    _answer,
    _ask,
    _bash,
    _final,
    _png,
    _turn,
    _use,
    workspace,  # noqa: F401 - the fixture
)

SNAPSHOT = syn.snapshot()


def _steps(events: list[tuple[str, Any]]) -> list[StepEvent]:
    return [data for name, data in events if name == "step"]


# ---- live steps ----------------------------------------------------------------------------------


def test_each_step_streams_as_it_finishes(workspace: Workspace) -> None:  # noqa: F811
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
    events = _ask("What did the vendor write for the countertop?", model, workspace)

    live = _steps(events)
    assert [(item.agent, item.step.kind, item.step.label) for item in live] == [
        (None, "bash", "Find the countertop"),
        (None, "bash", "Render page 2"),
        (None, "view", "Look at page 2"),
    ]
    assert live[0].step.output == "12: MADE-UP COUNTERTOP 37 3/8\n"
    # Each step comes after the stage that announced it and before the answer.
    names = [name for name, _ in events]
    assert names.index("step") > names.index("stage")
    assert names[-1] == "answer"
    assert _answer(events).steps == tuple(item.step for item in live)


def test_a_viewed_picture_carries_a_small_thumbnail(workspace: Workspace) -> None:  # noqa: F811
    (workspace.out / "p-02.png").write_bytes(_png(3000, 2000))
    tools: Any = type("T", (), {"sandbox": FakeSandbox(workspace)})()
    ran = run_tool("view", {"path": f"{SCRATCH}/p-02.png", "description": "Look"}, tools)
    thumb = ran.step.thumbnail if ran.step else None
    assert thumb is not None and thumb.startswith("data:image/jpeg;base64,")
    from io import BytesIO

    from PIL import Image

    picture = Image.open(BytesIO(base64.b64decode(thumb.split(",", 1)[1])))
    assert max(picture.size) <= 480
    assert len(thumb) < 120_000
    # A text file has no thumbnail.
    (workspace.out / "notes.txt").write_text("made-up line\n")
    text = run_tool("view", {"path": f"{SCRATCH}/notes.txt"}, tools)
    assert text.step is not None and text.step.thumbnail is None


def test_helper_steps_stream_with_their_agent(workspace: Workspace) -> None:  # noqa: F811
    model = DispatchModel(main=[_agents(1, 2), _final("Checked; code: page 4.")])
    events = _ask("Which pages differ?", model, workspace)
    live = _steps(events)
    helpers = sorted((item.agent, item.step.label) for item in live if item.agent is not None)
    assert helpers == [("Page 1", "List the pictures"), ("Page 2", "List the pictures")]
    lead = [item.step for item in live if item.agent is None]
    assert [step.kind for step in lead] == ["agents"]
    assert lead[0].label == "Ran 2 agents · Page 1"


def test_a_step_event_is_json_ready() -> None:
    event = StepEvent(agent=None, step=AgentStep(kind="bash", label="Made-up", ok=True))
    assert event.model_dump(mode="json")["step"]["label"] == "Made-up"


# ---- wording -------------------------------------------------------------------------------------


def _first_countertop() -> str:
    return SNAPSHOT.countertops[0].id


def test_the_code_subject_never_leaves_a_capital_behind_it() -> None:
    key = _first_countertop()
    text = render(f"The recorded reason is {{{key}.reason}}.", SNAPSHOT).text
    assert text.startswith("On page ")
    assert ", the recorded reason is" in text
    assert ", The " not in text


def test_an_acronym_after_the_code_subject_keeps_its_capitals() -> None:
    key = _first_countertop()
    text = render(f"GV markup sits near {{{key}.printed}}.", SNAPSHOT).text
    assert ", GV markup" in text


def test_no_space_is_left_before_a_full_stop() -> None:
    key = _first_countertop()
    text = render(f"The countertop on {{{key}.page}} {{{key}.outcome}} .", SNAPSHOT).text
    assert " ." not in text.replace(" [[0]]", "")
    assert text.endswith("[[0]].")


def test_a_table_line_gets_no_code_subject() -> None:
    key = _first_countertop()
    template = f"| Printed | Needed |\n|---|---|\n| {{{key}.printed}} | {{{key}.needed}} |"
    text = render(template, SNAPSHOT).text
    assert "On page" not in text
    assert text.splitlines()[2].startswith("| ")


def test_the_prompts_say_what_clouded_means_and_name_gvs_reviewer_comments() -> None:
    for prompt in (AGENT_SYSTEM_PROMPT, SUB_SYSTEM_PROMPT):
        assert "GV's reviewer comments" in prompt
    assert "will_be_clouded_on_our_cloud_pdf" in AGENT_SYSTEM_PROMPT
    assert "not GV's markup" in AGENT_SYSTEM_PROMPT


def test_ai_review_rows_reach_the_model_with_clouded_spelled_out() -> None:
    from app.api.review_assistant import ai_review_row_for_model

    row = ai_review_row_for_model({"key": "made-up", "result": "different", "clouded": True})
    assert "clouded" not in row
    assert row["will_be_clouded_on_our_cloud_pdf"] is True


# ---- footer --------------------------------------------------------------------------------------


def test_model_labels_name_the_model() -> None:
    assert model_label("anthropic.claude-opus-5-5") == "Opus 5.5"
    assert model_label("anthropic.claude-sonnet-5-5") == "Sonnet 5.5"
