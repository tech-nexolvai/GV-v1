"""The assistant's request and event shapes (#1128), and the internal draft an answer starts as.

The panel is built against these shapes, so they are the contract: change one only with the
frontend. A `Draft` uses the model's short record ids (`C1`, `F1`, `P4`); `publish` in
`app.review.assistant.answers` turns it into an `AnswerEvent` with the screen's real ids.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MAX_HISTORY_TEXT",
    "MAX_HISTORY_TURNS",
    "MAX_QUESTION_LENGTH",
    "MODEL_LABEL",
    "STAGE_LABELS",
    "Action",
    "AgentStep",
    "AnswerEvent",
    "AssistantInfoOut",
    "AssistantRequest",
    "Citation",
    "CountertopEvidence",
    "Draft",
    "ErrorEvent",
    "Evidence",
    "Focus",
    "GroupEvidence",
    "HistoryTurn",
    "OpenPageAction",
    "OpenQueueItemAction",
    "OpenReviewItemAction",
    "PlanTask",
    "StageEvent",
    "StageId",
    "StepEvent",
    "SubAgentRun",
    "model_label",
]

MAX_QUESTION_LENGTH: Final = 500
MAX_HISTORY_TURNS: Final = 6
#: A turn of history is a short chat message; anything longer is cut by the client or refused here.
MAX_HISTORY_TEXT: Final = 2000
MODEL_LABEL: Final = "Claude Sonnet 5.5"
_MODEL_LABELS: Final[dict[str, str]] = {
    "anthropic.claude-sonnet-5-5": "Claude Sonnet 5.5",
    "anthropic.claude-opus-5-5": "Claude Opus 5.5",
}


def model_label(model_id: str) -> str:
    """The model in words, for the panel's footer ("Claude Opus 5.5")."""
    return _MODEL_LABELS.get(model_id, MODEL_LABEL)


type StageId = Literal["records", "model", "tool", "guard"]
STAGE_LABELS: Final[dict[str, str]] = {
    "records": "Reading this review's records",
    "model": "Asking Claude Sonnet",
    "tool": "Using a tool",
    "guard": "Checking the answer against the records",
}


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class HistoryTurn(_Request):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=MAX_HISTORY_TEXT)


class Focus(_Request):
    """What the reviewer is looking at: a page, a record (countertop row id or finding id), both."""

    page_number: int | None = Field(default=None, ge=1)
    record_id: str | None = Field(default=None, min_length=1, max_length=64)


class AssistantRequest(_Request):
    """A reviewer question. It carries no value, outcome or decision the server would accept."""

    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    # Not strict only so a JSON array is accepted as the tuple; each turn is still strict.
    history: tuple[HistoryTurn, ...] = Field(default=(), max_length=MAX_HISTORY_TURNS, strict=False)
    focus: Focus | None = None


class AssistantInfoOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool
    model_label: str = Field(
        default=MODEL_LABEL,
        description="The model that answers, in words: the lead (Claude Opus 5.5) when the "
        "tool-using assistant runs, else Claude Sonnet 5.5.",
    )
    keeps_no_data: bool = True
    reads_drawings: bool = Field(
        default=False,
        description="True when a question can open this set's drawings (the tool-using "
        "assistant, or the page pictures): the footer says 'reads the drawings'.",
    )
    starters: tuple[str, ...]


class StageEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: StageId
    label: str


class StepEvent(BaseModel):
    """`step`: one tool the assistant just finished using, streamed as it happens so the panel grows
    its step list live (owner, 2026-10-16). `agent` is the helper agent's description for a
    helper's step, `None` for the lead's. The answer still carries the lead's steps in order."""

    model_config = ConfigDict(frozen=True)

    agent: str | None
    step: AgentStep


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["page", "countertop", "finding", "review_item"]
    page_number: int | None
    record_id: str | None
    label: str


class CountertopEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["countertop"]
    record_id: str


class GroupEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["blockers", "no_countertop_pages", "rows_not_checked"]


Evidence = Annotated[CountertopEvidence | GroupEvidence, Field(discriminator="kind")]


class OpenPageAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["open_page"]
    page_number: int
    label: str


class OpenQueueItemAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["open_queue_item"]
    record_id: str
    label: str


class OpenReviewItemAction(BaseModel):
    """Opens one entry of the simple review list (#1352): navigation only, never a decision."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["open_review_item"]
    item_key: str
    """The list's own key for the entry (`ReviewItemOut.key`, `FailureOut.key` …)."""
    page_number: int | None
    label: str


Action = Annotated[
    OpenPageAction | OpenQueueItemAction | OpenReviewItemAction, Field(discriminator="kind")
]


class PlanTask(BaseModel):
    """One task of the assistant's task list (claude.ai's "Progress" panel; `TaskCreate`)."""

    model_config = ConfigDict(frozen=True)

    id: str
    subject: str
    active_form: str | None = Field(default=None, description="Shown while it is in progress.")
    status: Literal["pending", "in_progress", "completed"]


class AgentStep(BaseModel):
    """One tool the assistant used for an answer, shown like claude.ai's steps (owner, 2026-10-16):
    a command it ran in the locked sandbox and its output, a file or picture it viewed, the
    recorded results it read, a task it added or updated, or the helper agents it ran. Read-only;
    none changes the review."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["bash", "view", "review_results", "task", "agents"]
    label: str = Field(
        description="The step in plain words: the model's own `description` of a command or a "
        "view ('Inspect page 2 text'), 'Added task: …', 'Running 4 agents · …'."
    )
    command: str | None = Field(default=None, description="The command it ran (bash only).")
    path: str | None = Field(default=None, description="What it viewed (view only).")
    page: int | None = Field(default=None, description="The page of results it read, if one.")
    output: str | None = Field(
        default=None, description="What the tool answered, cut for the screen; null for a picture."
    )
    ok: bool = Field(description="False when the tool refused or the command failed or timed out.")
    tasks: tuple[PlanTask, ...] = Field(
        default=(), description="The task list after this step (task only)."
    )
    agents: tuple[SubAgentRun, ...] = Field(
        default=(), description="The helper agents run together, and what each did (agents only)."
    )
    thumbnail: str | None = Field(
        default=None,
        description="A small JPEG of the picture it viewed, as a data URL (view of a picture only).",
    )


class SubAgentRun(BaseModel):
    """One helper agent (`Agent`), run in parallel with the others of its turn."""

    model_config = ConfigDict(frozen=True)

    description: str = Field(description="Its 3-5 word title, as the assistant gave it.")
    model: Literal["sonnet", "opus"]
    steps: tuple[AgentStep, ...]
    report: str = Field(description="Its report, as the assistant read it.")
    ok: bool
    stopped: Literal["cost_cap", "failed"] | None = None


class AnswerEvent(BaseModel):
    """The answer. In `text`, `[[0]]`, `[[1]]` … mark the `citations` entry a sentence rests on."""

    model_config = ConfigDict(frozen=True)

    text: str
    citations: tuple[Citation, ...]
    evidence: tuple[Evidence, ...]
    actions: tuple[Action, ...]
    suggestions: tuple[str, ...] = Field(max_length=3)
    checked: bool
    mode: Literal["llm", "records_only", "refused", "disabled"]
    model_id: str | None
    sources: tuple[str, ...]
    looked_at_page: int | None = None
    """The page whose two drawings the model was shown for this answer (#1431), else `None`."""
    not_looked_at_page: int | None = None
    """The page the question was about whose pictures were NOT shown to the model, else `None`
    (never silent, QA 2026-10-16): the screen says "Answered without looking at page N"."""
    not_looked_at_reason: Literal["cost_cap"] | None = None
    """Why `not_looked_at_page` was not looked at: `cost_cap`, the question was over its cost cap
    with the pictures."""
    steps: tuple[AgentStep, ...] = ()
    """The tools the assistant used for this answer, in order (empty for a one-call answer)."""
    plan: tuple[PlanTask, ...] = ()
    """The assistant's last task list for this answer, if it made one."""
    stopped: Literal["cost_cap"] | None = None
    """`cost_cap`: the assistant stopped before finishing because the question reached its cost
    cap; the text says so."""


class ErrorEvent(BaseModel):
    """A plain-English failure: never a stack trace, a provider's words or a key."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Draft:
    """An answer before it is checked and published: a template in the records' short ids.

    `text` states facts only through placeholders (`{C1.outcome}`, `{count.needs_you}`; see
    `app.review.assistant.placeholders`), which code fills and cites. `evidence` are `C1` or a
    group name (`blockers`, `no_countertop_pages`, `rows_not_checked`); `actions` are
    `("open_page", "P4")`, `("open_queue_item", "C1")` or `("open_review_item", "R1")`.
    """

    text: str
    evidence: tuple[str, ...] = ()
    actions: tuple[tuple[str, str], ...] = field(default=())


AgentStep.model_rebuild()
StepEvent.model_rebuild()
