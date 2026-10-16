"""The chat's HTTP shapes (#1463), as the frontend builds against them (CONTRACT, "HTTP API").

A message's `blocks` keep the order things happened, as claude.ai shows them: thinking, text and
steps interleaved. A step is one tool the assistant used; an `Agent` step carries the helpers it ran
and their own steps. The SSE events (`chat_engine.py`) carry the same shapes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "MAX_MESSAGE_TEXT",
    "AssistantModelOut",
    "AssistantModelsOut",
    "ChatAccuracyBlock",
    "ChatAccuracyOut",
    "ChatAgentOut",
    "ChatBlock",
    "ChatBoxOut",
    "ChatCancelOut",
    "ChatCiteBlock",
    "ChatCiteOut",
    "ChatCoverageBlock",
    "ChatCoverageOut",
    "ChatCreatedOut",
    "ChatFileOut",
    "ChatFindingBlock",
    "ChatFindingDroppedBlock",
    "ChatFindingOut",
    "ChatListOut",
    "ChatMessageIn",
    "ChatMessageOut",
    "ChatOut",
    "ChatStepBlock",
    "ChatStepOut",
    "ChatSummaryOut",
    "ChatTaskOut",
    "ChatTextBlock",
    "ChatThinkingBlock",
    "ChatTrustBlock",
    "ChatTrustOut",
    "ChatUnknownOut",
    "ChatUnknownsBlock",
]

MAX_MESSAGE_TEXT = 8000

type ChatStepTool = Literal[
    "bash_tool",
    "view",
    "create_file",
    "str_replace",
    "present_files",
    "TaskCreate",
    "TaskUpdate",
    "Agent",
    "review_results",
]
type ChatStopped = Literal["cost_cap", "cancelled", "error", "turn_limit"]
type ChatCapHit = Literal["message", "package"]


class ChatMessageIn(BaseModel):
    """One message from the reviewer, with the model, effort and thinking they picked.

    `mode: full_check` (CONTRACT4) runs the app's own fixed whole-set check instead of a typed
    question (`text` is then not needed: the question shown is "Full check · Countertops · High
    effort"). `products`: the products picked in the composer (default: the set's own)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: str = Field(default="", max_length=MAX_MESSAGE_TEXT)
    model: str = Field(min_length=1, max_length=64)
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    thinking: bool
    mode: Literal["chat", "full_check"] = "chat"
    products: list[Literal["countertop", "cabinet"]] | None = Field(
        default=None, min_length=1, max_length=2
    )

    @model_validator(mode="after")
    def _a_question_or_a_full_check(self) -> ChatMessageIn:
        if self.mode == "chat" and not self.text.strip():
            raise ValueError("text is needed for a chat message")
        if self.products is not None and len(set(self.products)) != len(self.products):
            raise ValueError("each product at most once")
        return self


class ChatTaskOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    subject: str
    active_form: str | None = None
    status: Literal["pending", "in_progress", "completed"]


class ChatAgentOut(BaseModel):
    """One helper the assistant ran (`Agent`)."""

    model_config = ConfigDict(frozen=True)

    id: str
    description: str
    model: str = Field(description="The model it ran on, e.g. claude-sonnet-5-5.")
    ok: bool | None = Field(default=None, description="Null while it runs.")
    report: str | None = None
    steps: list[ChatStepOut] = Field(default_factory=list)
    stopped: Literal["cost_cap", "turn_limit", "cancelled", "error"] | None = None


class ChatStepOut(BaseModel):
    """One tool the assistant used: a command and its output, a file or picture it viewed, a file
    it wrote, the files it handed over, its task list, its helpers, or the recorded results."""

    model_config = ConfigDict(frozen=True)

    id: str
    tool: ChatStepTool
    label: str
    command: str | None = None
    path: str | None = None
    output: str | None = Field(
        default=None, description="What the tool answered, cut for the screen."
    )
    ok: bool | None = Field(default=None, description="Null while it runs.")
    seconds: float | None = None
    picture_id: str | None = Field(
        default=None, description="A picture it viewed: GET /chats/{chat_id}/pictures/{picture_id}."
    )
    tasks: list[ChatTaskOut] | None = None
    agents: list[ChatAgentOut] | None = None


class ChatThinkingBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["thinking"]
    text: str
    seconds: int


class ChatTextBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["text"]
    text: str


class ChatStepBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["step"]
    step: ChatStepOut


# ---- findings, coverage and trust (chat v2 / v3: CONTRACT2-findings, CONTRACT3-trust) ------------


class ChatBoxOut(BaseModel):
    """Where an item is: PDF points of the upload page as shown, origin top-left."""

    model_config = ConfigDict(frozen=True)

    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    file: str | None = Field(default=None, description="The upload's file name the box is on.")


class ChatFindingChecksOut(BaseModel):
    """The checks run on a finding, by code (and one cheap independent AI read)."""

    model_config = ConfigDict(frozen=True)

    printed_architect: str = Field(
        description="match | not_found | no_text | no_box | not_a_number | not_run"
    )
    printed_vendor: str
    second_read: str = Field(description="agrees | disagrees | unsure | not_run")
    rule: str | None = Field(default=None, description="agrees | disagrees | not_run (rules)")
    arithmetic: str | None = Field(
        default=None, description="adds_up | does_not_add_up | not_a_number (vendor arithmetic)"
    )
    lead_checked: bool
    ledger: str | None = Field(
        default=None,
        description="agrees | disagrees: code's reading ledger (a string that adds up, other "
        "views agreeing); null: the ledger says nothing about it.",
    )
    units: str | None = Field(
        default=None,
        description="agrees | disagrees: millimetres and inches printed together (940 [37]).",
    )


class ChatFindingInchesOut(BaseModel):
    """Code's numbers (decimal strings, inches): never the AI's arithmetic."""

    model_config = ConfigDict(frozen=True)

    architect: str | None = None
    vendor: str | None = None
    difference: str | None = Field(default=None, description="Vendor minus architect.")


class ChatSecondReadValuesOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    architect: str | None = None
    vendor: str | None = None


class ChatFindingOut(BaseModel):
    """One difference the assistant reported, with code-made evidence and checks."""

    model_config = ConfigDict(frozen=True)

    id: str
    page: int
    vs_page: int | None = None
    item: str
    category: Literal["countertop", "cabinet", "other"]
    architect: str
    vendor: str
    status: Literal["different", "check", "same"]
    why: str | None = None
    verified: bool
    arch_box: ChatBoxOut | None = None
    vendor_box: ChatBoxOut | None = None
    source: Literal["architect", "brand_prototype", "rules", "vendor_arithmetic", "gv_review"]
    parts: list[str] | None = None
    printed_total: str | None = None
    rule_check_id: str | None = None
    record_id: str | None = Field(
        default=None, description="The difference GV Review recorded that this finding is."
    )
    round: int | None = None
    arch_picture_id: str | None = Field(
        default=None, description="Evidence crop: GET /chats/{chat_id}/pictures/{picture_id}."
    )
    vendor_picture_id: str | None = None
    created_by: str = Field(
        description='"lead", "helper:<n>", or "gv_review" (made by code from a recorded result).'
    )
    checks: ChatFindingChecksOut | None = None
    inches: ChatFindingInchesOut | None = None
    trust_level: Literal["confirmed", "likely", "to_check"] | None = None
    round_status: (
        Literal[
            "fixed_in_later_round",
            "still_wrong",
            "new_in_later_round",
            "not_in_later_round",
            "single_round",
        ]
        | None
    ) = None
    second_read_values: ChatSecondReadValuesOut | None = None
    evidence_kind: Literal["observed", "derived", "inferred"] | None = Field(
        default=None,
        description="observed: printed on the drawing at its spots (text layer); derived: worked "
        "out by code (rule, arithmetic, ledger, mm/inch); inferred: AI reading only.",
    )
    witnesses: list[str] | None = Field(
        default=None,
        description="The non-AI witnesses behind it: printed, rule, arithmetic, ledger, units.",
    )
    compared: bool | None = Field(
        default=None,
        description='A "same" item: true when both sides were seen and its page pair is covered; '
        'false: "Not compared" (not counted as right).',
    )
    not_compared_why: Literal["not_viewed", "pair_not_covered"] | None = None


class ChatFindingBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["finding"]
    finding: ChatFindingOut


class ChatFindingDroppedBlock(BaseModel):
    """A finding the assistant dropped (in the place its finding block was)."""

    model_config = ConfigDict(frozen=True)

    type: Literal["finding_dropped"]
    finding_id: str
    reason: str


class ChatCoveragePageOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    page: int
    pair_page: int | None = None
    status: Literal["same", "different", "not_comparable", "not_opened"]
    note: str | None = None
    by: str | None = Field(default=None, description="Who recorded it; null: never recorded.")


class ChatCoverageCountsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    same: int
    different: int
    not_comparable: int
    not_opened: int


class ChatCoverageOut(BaseModel):
    """Every page of the vendor's file (a page never recorded is not_opened), and the counts."""

    model_config = ConfigDict(frozen=True)

    pages: list[ChatCoveragePageOut]
    counts: ChatCoverageCountsOut


class ChatCoverageBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["coverage"]
    coverage: ChatCoverageOut


class ChatTrustRuleOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: str | None = None
    rule_check_id: str | None = None
    result: str | None = None


class ChatTrustChecksOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    printed_at_spot: dict[str, int]
    second_read: dict[str, int]
    rules: list[ChatTrustRuleOut]
    arithmetic: dict[str, int]
    lead_checked: int


class ChatTrustCoverageOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    counts: dict[str, int]
    pages_not_opened: list[int]


class ChatRecordedDifferenceOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    record_id: str | None = None
    source: Literal["code", "ai_page_review"]
    id: str | None = None
    page: int | None = None
    what: str


class ChatDismissedOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    record_id: str
    reason: str


class ChatGvChecksOut(BaseModel):
    """Of the differences GV Review's own checks recorded, how many this answer includes."""

    model_config = ConfigDict(frozen=True)

    recorded: int
    included: int
    dismissed: list[ChatDismissedOut] = Field(default_factory=list)
    left_out: list[ChatRecordedDifferenceOut]


class ChatRoundsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    rounds: list[int]
    earlier_items: int
    fixed: int
    still_wrong: int
    not_in_later_round: int
    new: int
    line: str


class ChatTrustUnknownsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    by_why: dict[str, int]


class ChatTrustOut(BaseModel):
    """Why the answer can be believed, built by code from its findings and coverage."""

    model_config = ConfigDict(frozen=True)

    counts: dict[str, int] = Field(description="confirmed, likely, to_check, total.")
    evidence: dict[str, int] | None = Field(
        default=None, description="Findings by evidence kind: observed, derived, inferred."
    )
    not_compared: int | None = Field(
        default=None, description='"What\'s right" items not compared (not counted above).'
    )
    unknowns: ChatTrustUnknownsOut | None = None
    by_category: dict[str, dict[str, int]]
    checks: ChatTrustChecksOut
    coverage: ChatTrustCoverageOut | None = None
    gv_checks: ChatGvChecksOut | None = None
    rounds: ChatRoundsOut | None = None
    not_checked: list[str]


class ChatTrustBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["trust"]
    trust: ChatTrustOut


class ChatCiteOut(BaseModel):
    """A value the answer states, backed by where it is printed (checked by code)."""

    model_config = ConfigDict(frozen=True)

    id: str
    value: str
    fact: Literal["page_count", "file_name"] | None = None
    page: int | None = None
    box: ChatBoxOut | None = None
    file: str | None = None
    picture_id: str | None = None
    printed: str = Field(
        description="match | not_found | no_text | no_box | not_a_number | not_run"
    )
    evidence_kind: Literal["observed", "derived", "inferred"] | None = None


class ChatCiteBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["cite"]
    cite: ChatCiteOut


class ChatAccuracyClaimOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    claim: str
    how: str
    points: float
    evidence_kind: Literal["observed", "derived", "inferred"] | None = None


class ChatAccuracyOut(BaseModel):
    """The answer's accuracy, computed by code (CONTRACT5); `score` null: nothing to check."""

    model_config = ConfigDict(frozen=True)

    score: int | None
    level: Literal["green", "orange", "yellow", "n/a"]
    claims: int
    breakdown: list[ChatAccuracyClaimOut]
    coverage_factor: float | None = None
    measured: str
    calibration: str | None = Field(
        default=None, description="How the points per claim were calibrated on GV's test sets."
    )
    not_compared: int | None = Field(
        default=None, description='"What\'s right" items not compared: not scored.'
    )
    unknowns: int | None = Field(default=None, description="What the answer did not check.")


class ChatAccuracyBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["accuracy"]
    accuracy: ChatAccuracyOut


class ChatUnknownOut(BaseModel):
    """One thing the answer did not check, and what would settle it (built by code)."""

    model_config = ConfigDict(frozen=True)

    id: str
    page: int | None = None
    what: str
    why: Literal[
        "unreadable", "not_opened", "missing_job_value", "pairing_uncertain", "refused_alignment"
    ]
    needs: str
    scope: Literal["page", "item"] = "item"


class ChatUnknownsBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["unknowns"]
    unknowns: list[ChatUnknownOut]


ChatBlock = Annotated[
    ChatThinkingBlock
    | ChatTextBlock
    | ChatStepBlock
    | ChatFindingBlock
    | ChatFindingDroppedBlock
    | ChatCoverageBlock
    | ChatTrustBlock
    | ChatCiteBlock
    | ChatAccuracyBlock
    | ChatUnknownsBlock,
    Field(discriminator="type"),
]


class ChatFileOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    size: int
    mime: str
    url: str


class ChatMessageOut(BaseModel):
    """A message as stored: the reviewer's question, or the assistant's answer."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    role: Literal["user", "assistant"]
    created_at: datetime
    text: str = Field(description="The question, or the answer's final markdown.")
    model: str | None = None
    effort: str | None = None
    thinking: bool | None = None
    blocks: list[ChatBlock] | None = None
    files: list[ChatFileOut] | None = None
    cost_usd: str | None = None
    stopped: ChatStopped | None = None
    error: str | None = None
    message_cap_usd: str | None = Field(
        default=None, description='The per-answer cap this answer ran under ("5.00").'
    )
    package_cap_usd: str | None = Field(
        default=None, description='The drawing set\'s AI budget this answer ran under ("40.00").'
    )
    cap_hit: ChatCapHit | None = Field(
        default=None,
        description="With `stopped: cost_cap`: `message` (this answer's cap) or `package` (the "
        "drawing set's budget ran out).",
    )
    accuracy: ChatAccuracyOut | None = Field(
        default=None,
        description="The answer's accuracy by code (its `accuracy` block); null on older answers.",
    )


class ChatSummaryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str | None
    created_at: datetime
    last_message_at: datetime | None
    turns: int


class ChatListOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    chats: list[ChatSummaryOut]
    package_name: str | None = Field(
        default=None, description="The drawing set's name, for a new chat's greeting."
    )


class ChatCreatedOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str | None
    created_at: datetime


class ChatOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    package_id: UUID
    package_name: str
    title: str | None
    created_at: datetime
    messages: list[ChatMessageOut]
    running: bool = Field(
        default=False,
        description="True while a message of this chat is being answered (its answer is not "
        "stored yet).",
    )
    running_message_id: UUID | None = Field(
        default=None,
        description="The answer being written while `running`: follow it with "
        "GET /chats/{chat_id}/messages/{running_message_id}/events.",
    )


class AssistantModelOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    default: bool
    efforts: list[str]
    default_effort: str
    thinking: Literal["always", "optional"]
    thinking_off_max_effort: str | None


class AssistantModelsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool
    reason: str | None = None
    provider: Literal["openrouter", "anthropic"]
    models: list[AssistantModelOut]
    message_cap_usd: str
    package_cap_usd: str
    package_spent_usd: str


class ChatCancelOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    cancelled: bool


ChatAgentOut.model_rebuild()
ChatStepOut.model_rebuild()
ChatMessageOut.model_rebuild()
