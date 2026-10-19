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

from app.review.assistant.plan_check import MAX_DOUBTS, valid_answer
from vocabulary.product_registry import registry

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
    "ChatFindingAlsoOnOut",
    "ChatFindingBlock",
    "ChatFindingDecisionHelpOut",
    "ChatFindingDecisionIn",
    "ChatFindingDecisionOut",
    "ChatFindingDroppedBlock",
    "ChatFindingEstimateOut",
    "ChatFindingImpactOut",
    "ChatFindingLaterOut",
    "ChatFindingOut",
    "ChatFindingSettledOut",
    "ChatFindingUpdateBlock",
    "ChatFindingUpdateOut",
    "ChatFullCheckPlanOut",
    "ChatListOut",
    "ChatLiveCountsOut",
    "ChatLiveDrawingOut",
    "ChatLiveFindingOut",
    "ChatLiveImpactOut",
    "ChatLiveOut",
    "ChatLivePhaseOut",
    "ChatLiveStateOut",
    "ChatLiveStoppingOut",
    "ChatLiveTallyOut",
    "ChatMessageIn",
    "ChatMessageOut",
    "ChatOut",
    "ChatPlanAnswerIn",
    "ChatPlanDoubtOut",
    "ChatPlanOptionOut",
    "ChatProgressBlock",
    "ChatProgressOut",
    "ChatResolverBlock",
    "ChatResolverCountsOut",
    "ChatResolverEntryOut",
    "ChatResolverOut",
    "ChatResumeBlock",
    "ChatResumeOut",
    "ChatStepBlock",
    "ChatStepOut",
    "ChatSummaryOut",
    "ChatSummaryReviewOut",
    "ChatTaskOut",
    "ChatTaskSpecIn",
    "ChatTextBlock",
    "ChatThinkingBlock",
    "ChatTimingHelperOut",
    "ChatTimingPhaseOut",
    "ChatTimingsOut",
    "ChatTrustBlock",
    "ChatTrustOut",
    "ChatUnderstoodBlock",
    "ChatUnderstoodOut",
    "ChatUnderstoodScopeOut",
    "ChatUnknownOut",
    "ChatUnknownsBlock",
    "ChatWorkItemOut",
    "ChatWorkItemsBlock",
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
    "compare_rounds",
    "verify_value",
    "measure_on_drawing",
    "zoom",
    "read_under_markup",
    "remember",
    "drawing_facts",
]
type ChatIntent = Literal[
    "fact",
    "compare",
    "whole_set",
    "rules",
    "arithmetic",
    "rounds",
    "file",
    "summary",
    "clarify",
    "refuse_action",
]
type ChatOutputShape = Literal["one_line", "short", "findings", "file"]
type ChatStopped = Literal["cost_cap", "cancelled", "error", "turn_limit", "interrupted"]
type ChatCapHit = Literal["message", "package"]


class ChatPlanAnswerIn(BaseModel):
    """The reviewer's answer to one plan-check question (#1569)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(min_length=1, max_length=40, description="The question's id (its `id`).")
    answer: str = Field(min_length=1, max_length=20, description="One of its options' `value`.")


class ChatTaskSpecIn(BaseModel):
    """#1551: the task spec as the reviewer corrected it on the screen ("Understood as … Edit").
    It is used as given (checked by code), with no model call."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task: str = Field(min_length=1, max_length=200)
    intent: ChatIntent | None = None
    what_to_compare: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=6
    )
    checks_to_run: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        default_factory=list, max_length=8
    )
    pages: list[Annotated[int, Field(ge=1, le=2000)]] = Field(
        default_factory=list, max_length=60, description="Empty: the whole set."
    )
    output_shape: ChatOutputShape | None = None


class ChatMessageIn(BaseModel):
    """One message from the reviewer, with the model, effort and thinking they picked.

    `mode: full_check` (CONTRACT4) runs the app's own fixed whole-set check instead of a typed
    question (`text` is then not needed: the question shown is "Full check · Countertops · High
    effort"). `products`: the products picked in the composer (default: the set's own), each an
    ACTIVE product id of the product registry (`GET /products`, #1547)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: str = Field(default="", max_length=MAX_MESSAGE_TEXT)
    model: str = Field(min_length=1, max_length=64)
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    thinking: bool
    mode: Literal["chat", "full_check"] = "chat"
    products: list[Annotated[str, Field(min_length=1, max_length=32)]] | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        description="Active product ids from `GET /products` (e.g. countertop, cabinet): a "
        "strict scope, so only these products are checked, ruled, counted and drawn (every active "
        "product = All). Not given: every active product (All is the default).",
    )
    continue_from: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        description="Continue (#1514): the id of the answer a server restart interrupted. The "
        "answer goes on from its saved work; a full check keeps its findings and checks only the "
        "pages not yet done (`mode` and `products` are then the interrupted answer's).",
    )
    plan_answers: list[ChatPlanAnswerIn] | None = Field(
        default=None,
        max_length=MAX_DOUBTS,
        description="A full check only (#1569): the answers to the plan check's questions "
        "(`GET /packages/{package_id}/full-check-plan`), kept with the drawing set's page map.",
    )
    task: ChatTaskSpecIn | None = Field(
        default=None,
        description="#1551: the reviewer's correction of how the app understood the question "
        "(Edit on the 'Understood as' line); the question's words are sent unchanged in `text`.",
    )
    pilot: int | None = Field(
        default=None,
        ge=1,
        le=200,
        description="The simple full check only (#1665): compare only the first N drawings of the "
        "work list (a paid pilot); the rest are listed as left out on purpose.",
    )
    compare_model: Literal["claude-sonnet-5-5", "claude-opus-5-5"] | None = Field(
        default=None,
        description="The simple full check only (#1665): this run's compare call model, for every "
        "drawing (the hard ones too); none: the settings' (`GV_ASSISTANT_SIMPLE_COMPARE_MODEL`).",
    )
    compare_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = Field(
        default=None,
        description="The simple full check only: this run's compare call effort, for every drawing.",
    )
    cap_usd: float | None = Field(
        default=None,
        gt=0,
        le=1000,
        allow_inf_nan=False,
        description="This answer's per-answer cap in dollars, when lower than the server's "
        "(`GV_ASSISTANT_MESSAGE_CAP_USD`, and a roster full check's fixed price); it never raises "
        "them. The runaway guard holds what the calls in flight may cost against it, so a paid "
        "run's money stop is kept before calls start (harness audit 2026-10-19).",
    )
    replay: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9._-]{1,80}$",
        description="The simple full check only: an earlier run of this chat (its folder name under "
        "`simple/`, said in the work-items block) whose saved compare answers and blind looks are "
        "verified again, with no model call ($0).",
    )
    compare_missing: bool = Field(
        default=False,
        description="#1710, with `replay` ('Compare them again'): the drawings that run has no "
        "answer for (failed, stopped, never started) are compared now, and the blind looks it "
        "could not make are made now; every saved answer and look is used again at $0, so the new "
        "answer is the whole result.",
    )

    @model_validator(mode="after")
    def _a_question_or_a_full_check(self) -> ChatMessageIn:
        if self.mode == "chat" and not self.text.strip():
            raise ValueError("text is needed for a chat message")
        if self.pilot is not None and self.mode != "full_check":
            raise ValueError("pilot goes with a full check only")
        if (self.compare_model or self.compare_effort or self.replay) and self.mode != "full_check":
            raise ValueError("compare_model, compare_effort and replay go with a full check only")
        if self.compare_missing and not self.replay:
            raise ValueError("compare_missing goes with replay (the run to compare again)")
        if self.plan_answers is not None:
            if self.mode != "full_check":
                raise ValueError("plan_answers go with a full check only")
            if len({answer.id for answer in self.plan_answers}) != len(self.plan_answers):
                raise ValueError("each plan question at most once")
            if not all(valid_answer(answer.id, answer.answer) for answer in self.plan_answers):
                raise ValueError("a plan answer is not one of its question's options")
        if self.products is not None:
            if len(set(self.products)) != len(self.products):
                raise ValueError("each product at most once")
            known = registry()
            for product in self.products:
                entry = known.get(product)
                if entry is None:
                    raise ValueError(f"{product!r} is not a product (see GET /products)")
                if not entry.active:
                    raise ValueError(f"{entry.plural} is not available yet (coming later)")
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
    by: Literal["code"] | None = Field(
        default=None,
        description='"code": written by GV Review, not the AI (the full check\'s summary when the '
        "AI did not finish its own).",
    )


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


class ChatVerifiedValueOut(BaseModel):
    """What code read at one side's box for its value (#1531, `verify_value`): $0, no AI."""

    model_config = ConfigDict(frozen=True)

    result: str = Field(description="match | mismatch | cannot_read")
    route: str = Field(
        description="text (the drawing's text) | drawn (the vendor's drawn ink, two OCR engines) "
        "| none"
    )
    read: str | None = Field(
        default=None, description="What code read there (on a mismatch, what the drawing prints)."
    )
    reads: list[str] = Field(
        default_factory=list, description="Every reading behind it, labelled by route and engine."
    )
    reason: str = Field(description="Why, in one plain line.")
    value: str | None = Field(default=None, description="The one value checked.")
    pending: bool = Field(
        default=False, description="The box holds drawn ink still to be read (after the answer)."
    )
    words: str = Field(description="The evidence detail's line, written by code.")


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
    text: str | None = Field(
        default=None,
        description="agrees: code read the finish codes in the drawings' own text and searched "
        "every vendor sheet and round of the area (#1623); null: not that check's finding.",
    )
    units: str | None = Field(
        default=None,
        description="agrees | disagrees: millimetres and inches printed together (940 [37]).",
    )
    adjudication: str | None = Field(
        default=None,
        description="The third read's majority when the second read disagreed or was unsure: "
        "agrees_after_adjudication | disagrees | unsure (#1498).",
    )
    critic: str | None = Field(
        default=None,
        description="The adversarial critic (whole-set answers): passed | objects | unsure | "
        "not_run (#1498).",
    )
    verify_architect: ChatVerifiedValueOut | None = Field(
        default=None, description="Code's reading of the architect's value at its box (#1531)."
    )
    verify_vendor: ChatVerifiedValueOut | None = Field(
        default=None, description="Code's reading of the vendor's value at its box (#1531)."
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


class ChatFindingReadOut(BaseModel):
    """One reading of a finding's values (#1498): its own, the second read's, the third read's."""

    model_config = ConfigDict(frozen=True)

    role: Literal["first", "second", "third"] | None = None
    model: str
    value_arch: str | None = None
    value_vendor: str | None = None
    sure: bool | None = Field(default=None, description="null for the finding's own values.")
    dpi: int | None = Field(default=None, description="The third read's crop resolution.")
    turned: bool | None = Field(
        default=None, description="Whether a quarter-turned copy was shown (sideways text)."
    )


class ChatFindingCriticOut(BaseModel):
    """The critic's look at one finding (#1498)."""

    model_config = ConfigDict(frozen=True)

    result: Literal["passed", "objects", "unsure"]
    answers: dict[str, str] = Field(
        default_factory=dict,
        description="same_item, right_dimension, vendor_ink, architect_side: yes | no | unsure.",
    )
    note: str = Field(default="", description="What is wrong, when it objects.")
    model: str | None = None


type ChatDecision = Literal["agree", "not_a_problem"]


class ChatFindingDecisionIn(BaseModel):
    """The reviewer's ✓ Agree / ✗ Not a problem on one finding (#1521)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision: ChatDecision
    note: str | None = Field(default=None, max_length=2000)


class ChatFindingDecisionOut(BaseModel):
    """A finding's latest decision: what, by whom (the signed-in principal), when."""

    model_config = ConfigDict(frozen=True)

    decision: ChatDecision
    note: str | None = None
    by: str
    at: datetime


class ChatFindingEstimateOut(BaseModel):
    """The AI's best estimate for a not-compared ("check") item, with how it got it (#1545)."""

    model_config = ConfigDict(frozen=True)

    value_in: float
    plus_minus_in: float
    method: str


class ChatFindingDecisionHelpOut(BaseModel):
    """A not-compared ("check") item's reasoning for the reviewer (#1545), in plain words."""

    model_config = ConfigDict(frozen=True)

    measures: str = Field(description="What the dimension measures, from where to where.")
    estimate: ChatFindingEstimateOut | None = None
    likely: Literal["probably fine", "probably wrong", "can't tell"]
    confirm: str = Field(description="What the reviewer should confirm, and with whom.")


type ChatSettledOutcome = Literal["wrong", "right", "merged", "not_a_difference"]
type ChatKeepReason = Literal[
    "architect_prints_nothing",
    "measures_something_else",
    "which_governs",
    "unreadable",
    "needs_job_value",
    "layout",
    "vendor_disagrees",
    "architect_outside_gv_rule",
    "other",
]


class ChatFindingSettledOut(BaseModel):
    """How code settled a not-compared ("check") item by itself (#1554), shown as "Settled by GV
    Review: …"."""

    model_config = ConfigDict(frozen=True)

    outcome: ChatSettledOutcome
    method: str = Field(
        description="The rule or tool: same_item, gv_limits, vendor_sum, printed_vs_printed, "
        "newest_round, prototype_prints_it, legend, sharper_read, other_trades_tags, out_of_scope; "
        "escalation_<step> when the full check's escalation settled it (#1619)."
    )
    how: str = Field(description="What code did and found, in one plain sentence.")
    was: Literal["check", "different"] = Field(
        default="check",
        description="check: it was not compared; different: a Wrong code could not prove, settled "
        "Right by the escalation (#1619).",
    )


class ChatFindingAlsoOnOut(BaseModel):
    """The same item on another page or round, folded into this finding (#1554)."""

    model_config = ConfigDict(frozen=True)

    finding_id: str
    page: int | None = None
    vs_page: int | None = None
    round: int | None = None
    item: str | None = None
    vendor: str | None = None
    architect: str | None = None
    vendor_box: ChatBoxOut | None = Field(
        default=None, description="Its spot on that page: the cloud drawing marks it there too."
    )
    arch_box: ChatBoxOut | None = None


class ChatFindingDimOut(BaseModel):
    """A numbered dimension label a finding's side was built from (#1558)."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="The label's id on its page (D17).")
    page: int
    file: str | None = None
    text: str = Field(description="The value as printed.")
    inches: str
    route: Literal["text", "drawn"] = Field(
        description="text: the drawing's characters; drawn: the vendor's drawn ink, by two OCR "
        "engines."
    )
    side: str | None = Field(default=None, description="architect, vendor, brand or unknown.")
    box: list[float] = Field(description="x0, y0, x1, y1: PDF points, origin top-left.")


class ChatFindingImpactOut(BaseModel):
    """How much a Wrong item matters (#1569), by code: ordering only, never a status or colour."""

    model_config = ConfigDict(frozen=True)

    score: int = Field(ge=0, le=100, description="Bigger matters more.")
    rank: int | None = Field(
        default=None,
        ge=1,
        description="Its place among the answer's items of the same status (1 = biggest).",
    )
    why: str = Field(description='Plain words: "changes fabrication (overall size); 9 in off".')


class ChatFindingProofOut(BaseModel):
    """Where the zero-click cloud drawing puts a finding (#1581), worked out by code on read
    (`proof.proof_of`): only Wrong items code proved are clouded; the reviewer's Agree / Not a
    problem override."""

    model_config = ConfigDict(frozen=True)

    group: Literal["clouded", "right", "not_compared", "dismissed"] = Field(
        description="clouded: Wrong, proved by code (red cloud); right; not_compared: could not be "
        "compared under the architect-first rules (#1619: listed with its reason in the "
        "Not-compared PDF and workbook, never a question to anyone); dismissed: the reviewer "
        "marked it not a problem."
    )
    should_be: str | None = Field(
        default=None,
        description="Wrong items: what the value should be and what governs it, e.g. '37\" "
        "(architect 3'-1\", page 28)'.",
    )
    proof: str | None = Field(
        default=None, description="Clouded items: the check that proved it, in plain words."
    )
    reason: str | None = Field(
        default=None, description="The other groups: why it is not clouded, in one line."
    )
    rule: str | None = Field(
        default=None,
        description="The architect-first rule it falls under (#1619): R4 a difference, R5 the "
        "vendor's own contradiction, R6 GV's rule; for not_compared R1 (brand prototype, or the "
        "architect's size outside a GV rule), R3 "
        "(architect's drawings disagree), R7 (architect gives nothing), R8 (different reference "
        "points), R9 (unreadable), R10 (missing job value or setting).",
    )
    not_compared: (
        Literal[
            "architect_disagrees",
            "nothing_to_compare",
            "different_reference",
            "unreadable",
            "missing_setting",
            "prototype",
            "not_proved",
            "not_seen",
            "field_verify",
            "finish_not_comparable",
            "no_sheet",
            "no_overall",
            "string_not_verified",
            "architect_outside_gv_rule",
            "internal",
        ]
        | None
    ) = Field(default=None, description="not_compared: the reason (the list's group).")
    label: str | None = Field(
        default=None,
        description="not_compared: the trade's words for the reason (Field verify, RFI to the "
        "architect, Not reviewed: <reason>, Illegible).",
    )
    settle: str | None = Field(
        default=None, description="not_compared: what is needed to compare it, in plain words."
    )


class ChatFindingEscalationStepOut(BaseModel):
    """One free code step the escalation tried on an item code could not compare at first
    (#1619)."""

    model_config = ConfigDict(frozen=True)

    step: str = Field(
        description="labels, under_markup, same_item, vendor_sums, notes or pairs (cheapest first)."
    )
    outcome: str = Field(description="read, proved, folded or no_change.")
    how: str = Field(description="What code did and found, in one plain line.")


class ChatFindingEscalationOut(BaseModel):
    """What the full check's escalation did with an item code could not settle at first (#1619)."""

    model_config = ConfigDict(frozen=True)

    outcome: Literal["wrong", "right", "folded", "undecided"]
    steps: list[ChatFindingEscalationStepOut] = Field(default_factory=list)


class ChatFindingStringReadsOut(BaseModel):
    """A full check (#1641, #1652): what the blind second reading of a vendor string gave."""

    model_config = ConfigDict(frozen=True)

    outcome: Literal[
        "proved", "adds_up", "within_rounding", "disagrees", "not_found", "gap", "not_verified"
    ]
    why: str


class ChatFindingLaterOut(BaseModel):
    """The later answer of the same chat that corrected a finding (#1548)."""

    model_config = ConfigDict(frozen=True)

    message_id: str
    answer: int | None = Field(default=None, description="That answer's number in the chat.")
    version: int = Field(description="The finding's version: 1 is the original, 2 the first fix.")


class ChatFindingOut(BaseModel):
    """One difference the assistant reported, with code-made evidence and checks."""

    model_config = ConfigDict(frozen=True)

    id: str
    page: int
    vs_page: int | None = None
    item: str
    category: str = Field(
        description="The product the item belongs to (a product id from `GET /products`), or "
        "`other`."
    )
    architect: str
    vendor: str
    status: Literal["different", "check", "same"]
    why: str | None = None
    verified: bool
    arch_box: ChatBoxOut | None = None
    vendor_box: ChatBoxOut | None = None
    source: Literal[
        "architect",
        "brand_prototype",
        "rules",
        "vendor_arithmetic",
        "gv_review",
        "cross_view",
        "completeness",
    ] = Field(
        description="cross_view and completeness are made only by code, from the reading ledger."
    )
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
        description='"lead", "helper:<n>", "gv_review" (made by code from a recorded result), '
        '"ledger" (made by code from the reading ledger), "consistency" (made by code from the '
        "vendor's own drawings: a string that does not add up, a span printed twice), "
        '"architect_items" (made by code from the drawings\' text: a finish the architect tags '
        'that the vendor missed, changed or added), or "pairs" (made by code from a dimension '
        "it paired on both drawings by itself: Wrong or Right with that proof)."
    )
    ledger_ids: list[str] | None = Field(
        default=None, description="The reading ledger's entries (S<n>, R<n>) behind it."
    )
    reference_side: Literal["vendor", "architect"] | None = Field(
        default=None,
        description="cross_view: whose drawings disagree. `architect` then holds the REFERENCE "
        "view's value and `vendor` this view's.",
    )
    sum_text: str | None = Field(
        default=None,
        description="vendor_arithmetic from the ledger: code's sum written out (\"12 + 15 1/2 = "
        '27 1/2 ≠ 27 1/4 printed (off 1/4)").',
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
        description="The non-AI witnesses behind it: printed, ocr_ink, text, rule, arithmetic, "
        "ledger, units.",
    )
    compared: bool | None = Field(
        default=None,
        description='A "same" item: true when both sides were seen and its page pair is covered; '
        'false: "Not compared" (not counted as right).',
    )
    not_compared_why: Literal["not_viewed", "pair_not_covered"] | None = None
    reads: list[ChatFindingReadOut] | None = Field(
        default=None, description="Every reading, when a third read settled the second (#1498)."
    )
    critic: ChatFindingCriticOut | None = None
    decision: ChatFindingDecisionOut | None = Field(
        default=None,
        description="The reviewer's latest decision on this finding (#1521); null: not decided.",
    )
    decision_help: ChatFindingDecisionHelpOut | None = Field(
        default=None,
        description="Status check (not compared): what it measures, the best estimate, how "
        "likely it is fine, what to confirm (#1545).",
    )
    impact: ChatFindingImpactOut | None = Field(
        default=None,
        description="Wrong and Your-decision items (#1569): how much it matters, by code; the "
        "screen, the cloud drawing's numbers and the check report follow this order.",
    )
    proof: ChatFindingProofOut | None = Field(
        default=None,
        description="Where the zero-click cloud drawing puts it, and why (#1581); worked out on "
        "read, so it follows the reviewer's latest decision.",
    )
    check_reason: (
        Literal["different_extent", "which_governs", "pairing_not_certain", "scaled", "site_verify"]
        | None
    ) = Field(
        default=None,
        description="Why two printed values that differ still need a person (#1545); without "
        "one, code makes such a finding Wrong.",
    )
    dims: dict[str, ChatFindingDimOut] | None = Field(
        default=None,
        description="The numbered dimension label each side was built from (#1558), by side "
        "(architect, vendor); code filled that side's value and box from it.",
    )
    updated_in: ChatFindingLaterOut | None = Field(
        default=None,
        description="Set when a later message of the chat corrected this finding (#1548): the "
        'finding shown is its newest version ("updated in a later message").',
    )
    settled: ChatFindingSettledOut | None = Field(
        default=None,
        description="Set when code settled this item, which was not compared, by itself (#1554).",
    )
    question: str | None = Field(
        default=None,
        description="Status check: the one question a person answers, written by code (#1554).",
    )
    keep_reason: ChatKeepReason | None = Field(
        default=None, description="Status check: why it needs a person (#1554)."
    )
    also_on: list[ChatFindingAlsoOnOut] | None = Field(
        default=None, description="The same item on other pages or rounds, folded in (#1554)."
    )
    parts_sum: str | None = Field(
        default=None,
        description="Source vendor_arithmetic: code's sum of the parts, in inches (#1554).",
    )
    escalation: ChatFindingEscalationOut | None = Field(
        default=None,
        description="A full check (#1619): the free code steps tried on this item when code could "
        "not settle it at first, and how it ended.",
    )
    string_reads: ChatFindingStringReadsOut | None = Field(
        default=None,
        description="A vendor string (#1641): the blind second reading's outcome, in code's words "
        "(proved: the same parts and overall, and code's sum is off; gap: code's lines show a part "
        "left out, so no reading was asked; not_verified: the fixed price did not reach it).",
    )


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
    dropped_in: ChatFindingLaterOut | None = Field(
        default=None, description="Set when a later message of the chat dropped it (#1548)."
    )


class ChatFindingUpdateOut(BaseModel):
    """A later message's correction of an earlier answer's finding (#1548), stored in the later
    answer; the earlier answer is read with its newest version."""

    model_config = ConfigDict(frozen=True)

    ref: str = Field(description='"A1-F5": the chat\'s first answer, finding F5.')
    message_id: str = Field(description="The earlier answer the finding belongs to.")
    finding_id: str
    answer: int = Field(description="The earlier answer's number in the chat.")
    by_answer: int | None = Field(default=None, description="This answer's number in the chat.")
    version: int
    finding: ChatFindingOut = Field(description="The new version (the last one, when dropped).")
    dropped: str | None = Field(default=None, description="Why it was dropped; null: updated.")


class ChatFindingUpdateBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["finding_update"]
    update: ChatFindingUpdateOut


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


class ChatScopeOut(BaseModel):
    """What the answer is about, worked out by code (#1520): the whole set, or some pages or an
    area. Only a whole-set answer gets the whole-set panels (all pages' coverage, every recorded
    difference); a page or area answer's coverage, recorded results and trust counts cover its
    pages only."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["whole_set", "pages", "area"]
    pages: list[int] = Field(
        default_factory=list, description="The scope's pages; empty for the whole set."
    )
    area: str | None = Field(default=None, description="The area named, as typed on the set.")
    why: Literal[
        "full_check", "whole_set_words", "nothing_named", "wide_work", "pages_named", "area_named"
    ]
    label: str = Field(description='In plain words: "page 4 (Pantry)", "the whole set".')
    products: list[str] = Field(
        default_factory=list,
        description="The products checked (product ids, a strict scope: a finding of another "
        "product is never kept); empty on older answers.",
    )
    all_products: bool = Field(
        default=False, description="True when every active product was checked (All)."
    )
    products_label: str | None = Field(
        default=None, description='The products in plain words: "Countertops".'
    )


class ChatScopeBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["scope"]
    scope: ChatScopeOut


class ChatTrustRuleOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: str | None = None
    rule_check_id: str | None = None
    result: str | None = None


class ChatTrustChecksOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    printed_at_spot: dict[str, int]
    second_read: dict[str, int]
    adjudication: dict[str, int] | None = None
    critic: dict[str, int] | None = None
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
    unrequested: int = Field(
        default=0,
        description="compare_rounds: changes nobody asked for (no finding, no GV markup).",
    )
    pairs: list[ChatRoundPairOut] = Field(
        default_factory=list, description="compare_rounds: the round pairs compared by code."
    )


class ChatLedgerBoxOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    file: str | None = None


class ChatLedgerStringOut(BaseModel):
    """One dimension string the AI recorded, and code's sum of it."""

    model_config = ConfigDict(frozen=True)

    id: str
    page: int
    view: str | None = None
    side: str
    parts: list[str]
    printed_total: str
    box: ChatLedgerBoxOut | None = None
    by: str
    result: str = Field(description="adds_up | does_not_add_up | not_a_number")
    parts_sum: str | None = None
    off_by: str | None = None
    rereads: int = 0
    sum_text: str


class ChatLedgerReadingOut(BaseModel):
    """One value the AI recorded."""

    model_config = ConfigDict(frozen=True)

    id: str
    page: int
    view: str
    side: str
    item: str
    item_key: str
    instance: int | None = None
    kind: str
    value: str
    inches: str | None = None
    box: ChatLedgerBoxOut | None = None
    by: str
    rereads: int = 0


class ChatRecordedResultOut(BaseModel):
    """A result GV Review recorded (a code FAIL or an evaluator-checked AI page review row)."""

    model_config = ConfigDict(frozen=True)

    key: str
    kind: str
    pages: list[int] = Field(default_factory=list)
    what: str
    architect: str | None = None
    vendor: str | None = None


class ChatRoundPairOut(BaseModel):
    """One round pair compared by code (compare_rounds)."""

    model_config = ConfigDict(frozen=True)

    round1_page: int | None = None
    round2_page: int | None = None
    fixed: int
    still_wrong: int
    unrequested: int
    unrequested_zones: list[str | None] = Field(default_factory=list)


class ChatLedgerStringsOut(BaseModel):
    """The vendor's (and architect's) dimension strings the AI recorded, added up by code."""

    model_config = ConfigDict(frozen=True)

    total: int
    add_up: int
    off: int
    not_a_number: int
    vendor_off: int
    architect_off: int
    off_list: list[ChatLedgerStringOut] = Field(default_factory=list)


class ChatLedgerViewsOut(BaseModel):
    """The same item read on several views or pages, compared by code."""

    model_config = ConfigDict(frozen=True)

    groups: int
    consistent: int
    inconsistent: int
    not_compared: int
    typical_groups: int


class ChatLedgerCompletenessOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    compared: bool
    architect_items: int
    vendor_items: int
    missing_on_vendor: list[str] = Field(default_factory=list)
    extra_on_vendor: list[str] = Field(default_factory=list)
    not_looked_at: list[str] = Field(default_factory=list)


class ChatLedgerReportOut(BaseModel):
    """The reading ledger's counts, by code (CONTRACT3 "Ledger")."""

    model_config = ConfigDict(frozen=True)

    strings: ChatLedgerStringsOut
    views: ChatLedgerViewsOut
    completeness: ChatLedgerCompletenessOut


class ChatLedgerEntriesOut(BaseModel):
    """Everything the reading ledger recorded, so a reload shows the same."""

    model_config = ConfigDict(frozen=True)

    strings: list[ChatLedgerStringOut] = Field(default_factory=list)
    readings: list[ChatLedgerReadingOut] = Field(default_factory=list)


class ChatRecordedResultsOut(BaseModel):
    """GV Review's recorded results (code FAILs, evaluator-checked AI page review differences) and
    how many the answer includes (the ledger's check)."""

    model_config = ConfigDict(frozen=True)

    recorded: int
    included: int
    dismissed: int
    missing: list[ChatRecordedResultOut] = Field(default_factory=list)
    also_left_out: list[ChatRecordedResultOut] = Field(
        default_factory=list,
        description="Of `missing`, the ones `gv_checks` does not count (e.g. review list failures).",
    )


class ChatTrustUnknownsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    by_why: dict[str, int]


class ChatTimingPhaseOut(BaseModel):
    """One phase of an answer: its wall-clock seconds and what its model calls cost (#1512)."""

    model_config = ConfigDict(frozen=True)

    phase: str = Field(
        description="seeding, set_pack, planning, prewarm, lead, helpers, lead_verification, "
        "second_reads, third_reads, critic, finish_checks, cloud_drawing, report, package"
    )
    label: str
    seconds: float
    cost_usd: str
    calls: int


class ChatTimingHelperOut(BaseModel):
    """One helper of an answer: its pages (a full check's, planned by code), calls, time, cost."""

    model_config = ConfigDict(frozen=True)

    number: int
    description: str
    pages: list[int]
    calls: int
    seconds: float
    cost_usd: str
    stopped: str | None = None


class ChatTimingsOut(BaseModel):
    """Where an answer's time and money went, phase by phase (#1512)."""

    model_config = ConfigDict(frozen=True)

    total_seconds: float
    cost_usd: str
    phases: list[ChatTimingPhaseOut]
    helpers: list[ChatTimingHelperOut]


class ChatProductScopeOut(BaseModel):
    """The strict product scope (Anant, 2026-10-18): the products checked, and the findings of
    other products code refused or dropped, whoever reported them."""

    model_config = ConfigDict(frozen=True)

    products: list[str]
    all_products: bool
    products_label: str
    dropped_out_of_scope: int = Field(
        description="Findings of products not in scope that code did not keep."
    )


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
    ledger: ChatLedgerReportOut | None = None
    ledger_entries: ChatLedgerEntriesOut | None = None
    recorded_results: ChatRecordedResultsOut | None = None
    timings: ChatTimingsOut | None = None
    scope: ChatScopeOut | None = Field(
        default=None, description="What the counts above cover (null on older answers)."
    )
    product_scope: ChatProductScopeOut | None = Field(
        default=None,
        description="The products checked and how many findings of other products code dropped "
        "(for the record, not shown); null on older answers.",
    )


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
    rescored: bool | None = Field(
        default=None,
        description="True when this score was worked out again from the stored findings with "
        "today's calibration and differs from the score stored with the answer (#1513).",
    )


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
        "unreadable",
        "not_opened",
        "missing_job_value",
        "pairing_uncertain",
        "refused_alignment",
        "no_counterpart",
        "missing_round",
        "not_in_set",
        "not_comparable",
    ]
    needs: str
    scope: Literal["page", "item", "set"] = Field(
        default="item",
        description="page: a whole page; item: one value; set: the drawing set itself (an area "
        "on one side only, a round or a sheet the set lacks), not counted against the answer.",
    )


class ChatUnknownsBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["unknowns"]
    unknowns: list[ChatUnknownOut]


class ChatProgressOut(BaseModel):
    """A full check's progress, by code: "Checking page 12 of 60 · 8 wrong so far · about 6 min
    left". Sent as `progress` events while it runs; the last one is stored."""

    model_config = ConfigDict(frozen=True)

    phase: Literal["reading", "checking", "writing"]
    pages_done: int
    pages_total: int
    wrong_so_far: int
    decision_so_far: int
    right_so_far: int
    eta_seconds: int | None = Field(
        default=None, description="Time left at the pace so far; null before any page is done."
    )
    drawings_done: int | None = Field(
        default=None,
        description="The simple full check (#1665): vendor drawings compared so far "
        "('Comparing drawing 12 of 36'); null in the roster check.",
    )
    drawings_total: int | None = Field(
        default=None, description="The simple full check: drawings in its work list."
    )


class ChatWorkItemOut(BaseModel):
    """One vendor drawing of the simple full check's work list (#1665): compared (`answered`),
    `failed` with the reason (never dropped), or `skipped` by code with its reason."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="The drawing's id, `p23.v1` (page 23, round 1).")
    page: int
    round: int | None = None
    status: Literal["answered", "failed", "skipped"]
    reason: str | None = None
    model: str | None = Field(default=None, description="The model that answered.")
    cost_usd: str = Field(default="0", description="Its compare call's estimated cost.")


class ChatWorkItemsBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["work_items"]
    work_items: list[ChatWorkItemOut]
    run: str | None = Field(
        default=None,
        description="The run's folder (its saved compare answers and blind looks): name it in "
        "`replay` to verify it again at $0.",
    )


class ChatProgressBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["progress"]
    progress: ChatProgressOut


class ChatResumeOut(BaseModel):
    """How an answer a server restart interrupted can be continued (#1514), in plain words."""

    model_config = ConfigDict(frozen=True)

    mode: Literal["chat", "full_check"] = Field(
        description="How the interrupted answer was asked: Continue asks the same way."
    )
    products: list[str] | None = None
    findings: int = Field(description="Findings already made: kept, not made again.")
    pages_done: int = Field(description="Pages already accounted for: not opened again.")
    redo: str = Field(description="What Continue will do again, in one or two plain sentences.")


class ChatUnderstoodScopeOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["whole_set", "pages", "area"]
    pages: list[int] = Field(default_factory=list)
    area: str | None = None
    label: str = Field(description='In plain words: "page 4", "the whole set".')


class ChatUnderstoodOut(BaseModel):
    """#1551: how the app understood a typed question (its task spec), before any tool ran. Shown
    as a quiet "Understood as: <label> · Edit" line under the question. A guide for the lead and
    its helpers only: the reviewer's own words are always sent unchanged."""

    model_config = ConfigDict(frozen=True)

    task: str
    intent: ChatIntent
    scope: ChatUnderstoodScopeOut
    products: list[str] = Field(default_factory=list)
    what_to_compare: list[str] = Field(default_factory=list)
    checks_to_run: list[str] = Field(default_factory=list)
    output_shape: ChatOutputShape
    assumptions: list[str] = Field(default_factory=list)
    question_back: str | None = Field(
        default=None,
        description="With intent `clarify`: a question back the lead may ask (the lead answers).",
    )
    hint: str | None = Field(
        default=None,
        description="A short hint for the lead (an unclear question, or an order only a person "
        "carries out). Code never answers for Claude.",
    )
    label: str = Field(description='"Compare outlet heights (architect vs vendor) · page 4 · …".')
    path: Literal["code", "model", "edited"] = Field(
        description="How it was made: code rules ($0), the cheap model, or the reviewer's edit."
    )
    seconds: float = Field(description="The time it added before the work started.")
    cost_usd: str = Field(description='What it cost ("0.0003").')


class ChatUnderstoodBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["understood"]
    understood: ChatUnderstoodOut


class ChatResumeBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["resume"]
    resume: ChatResumeOut


class ChatResolverCountsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    wrong: int
    decision: int
    right: int


class ChatResolverEntryOut(BaseModel):
    """One not-compared ("check") item code settled by itself (#1554): the folded group's line."""

    model_config = ConfigDict(frozen=True)

    finding_id: str
    page: int | None = None
    vs_page: int | None = None
    item: str | None = None
    vendor: str | None = None
    architect: str | None = None
    outcome: ChatSettledOutcome
    method: str
    how: str
    into: str | None = Field(default=None, description="The finding it was folded into.")


class ChatResolverOut(BaseModel):
    """The resolver's pass on a whole-set answer (#1554): the counts before and after, every item
    it settled, why the rest still need a person, its sharper reads and their cost."""

    model_config = ConfigDict(frozen=True)

    before: ChatResolverCountsOut
    after: ChatResolverCountsOut
    settled: list[ChatResolverEntryOut]
    kept: dict[str, int]
    reads: int
    cost_usd: str


class ChatResolverBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["resolver"]
    resolver: ChatResolverOut


class ChatFallbackOut(BaseModel):
    """#1577: Anthropic's refusal fallback answered instead of the model asked."""

    model_config = ConfigDict(frozen=True)

    from_model: str = Field(description='The model that declined ("claude-fable-5-1").')
    to_model: str = Field(description='The model that answered instead ("claude-opus-4-8").')


class ChatFallbackBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["fallback"]
    fallback: ChatFallbackOut


class ChatPackageStateOut(BaseModel):
    """What the review result needs beyond the findings (#1622, `review_action.CheckState`)."""

    model_config = ConfigDict(frozen=True)

    incomplete: str | None = Field(
        default=None, description="Why the check did not finish; null when it did."
    )
    missing_areas: int = Field(
        default=0, ge=0, description="Areas the architect draws with no vendor sheet."
    )
    wrong_product: bool = Field(
        default=False, description="Code proved the vendor drew a different product."
    )
    still_open: int = Field(
        default=0, ge=0, description="Previous comments still wrong in this round."
    )


class ChatPackageOut(BaseModel):
    """A full check's send-ready package (#1622): kept with the answer so the screen and a remade
    package read the same state and comment numbers."""

    model_config = ConfigDict(frozen=True)

    state: ChatPackageStateOut
    numbers: dict[str, str] = Field(
        default_factory=dict, description='Finding id -> stable comment number ("C-07").'
    )
    submittal: str = Field(default="", description='The submittal number ("25A"); "" unknown.')
    round: int = Field(default=1, ge=1, description="The drawing set's round.")


class ChatPackageBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["package"]
    package: ChatPackageOut


ChatBlock = Annotated[
    ChatThinkingBlock
    | ChatTextBlock
    | ChatStepBlock
    | ChatFindingBlock
    | ChatFindingDroppedBlock
    | ChatFindingUpdateBlock
    | ChatCoverageBlock
    | ChatTrustBlock
    | ChatCiteBlock
    | ChatAccuracyBlock
    | ChatUnknownsBlock
    | ChatScopeBlock
    | ChatProgressBlock
    | ChatWorkItemsBlock
    | ChatResumeBlock
    | ChatResolverBlock
    | ChatUnderstoodBlock
    | ChatFallbackBlock
    | ChatPackageBlock,
    Field(discriminator="type"),
]


class ChatFileOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    size: int
    mime: str
    url: str


class ChatReviewActionOut(BaseModel):
    """A full check's review result (#1622, `review_action.review_action`, rules S0 to S7),
    decided by code on read from its findings with the reviewer's latest decisions and the state
    kept with its package; the comment letter and the cloud drawing's cover say the same."""

    model_config = ConfigDict(frozen=True)

    action: Literal[
        "approved", "approved_as_noted", "revise_resubmit", "rejected", "not_for_sending"
    ]
    rule: Literal["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"] = Field(
        description="The rule that decided it (the first that matches wins)."
    )
    words: str = Field(description='"Approved as noted", "Revise and resubmit", …')
    reason: str = Field(description='Why, in one line: "2 of 5 corrections change fabrication."')
    corrections: int = Field(ge=0, description="Comments sent (the clouds).")
    fabrication: int = Field(ge=0, description="Of them, those that change fabrication.")
    note: str | None = Field(default=None, description="S6: what was not reviewed.")
    next_step: str = Field(description="What the vendor does next (the letter's closing line).")
    required_by: str = Field(description='Who must act now ("Vendor: revise and resubmit").')
    disclaimer: str = Field(description="The standard words printed under the result.")


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
        description="The answer's accuracy by code (its `accuracy` block, scored again with "
        "today's calibration: see `rescored`); null on older answers.",
    )
    mode: Literal["chat", "full_check"] | None = Field(
        default=None,
        description="A typed question (`chat`) or the one-click full check; null on messages "
        "stored before it was kept (#1513).",
    )
    products: list[str] | None = Field(
        default=None,
        description="The products the message was about (product ids); null on older messages.",
    )
    resume: ChatResumeOut | None = Field(
        default=None,
        description="With `stopped: interrupted`: how Continue picks up (its `resume` block).",
    )
    scope: ChatScopeOut | None = Field(
        default=None,
        description="What the answer is about (its `scope` block): the whole set, or pages or an "
        "area; null on older answers.",
    )
    understood: ChatUnderstoodOut | None = Field(
        default=None,
        description="#1551: how the app understood the question before the work (its "
        "`understood` block); null when it made no task spec, and on older answers.",
    )
    review_action: ChatReviewActionOut | None = Field(
        default=None,
        description="#1622: a full check's review result, worked out on read from its findings "
        "with the reviewer's latest decisions; null on other messages and before its package.",
    )


# ---- the full check's live state (#1710) ---------------------------------------------------------
# Sent as small versioned events while a full check runs (`live_check.py`: each event is
# `{"v": 1, <name>: <shape>}`): `check_phase` {phase: ChatLivePhaseOut}, `check_drawings`
# {drawings: [ChatLiveDrawingOut]}, `check_drawing` {drawing: ChatLiveDrawingOut}, `check_finding`
# {finding: ChatLiveFindingOut}, `check_tally` {tally: ChatLiveTallyOut}, `check_stopping`
# {stopping: ChatLiveStoppingOut}, `check_service` {reason: str}. The whole state is one GET away
# (`GET /chats/{chat_id}/live`, and `ChatOut.live`), for a page that reconnects mid-run.


class ChatLivePhaseOut(BaseModel):
    """What the full check is doing now, in plain phases with counts (`check_phase`)."""

    model_config = ConfigDict(frozen=True)

    phase: Literal["getting_ready", "comparing", "double_checking", "making_files", "done"] = Field(
        description="getting_ready: the drawing set's pack (labels, page map) and the work list; "
        "comparing: the drawings (n of m); double_checking: code's own checks, code verifies the "
        "answers, then the blind second looks (n of m); making_files: n of 5 files; done: the "
        "answer is stored."
    )
    step: Literal["code_checks", "verifying", "second_looks", "final_checks", "stopped"] | None = (
        Field(
            default=None,
            description="Within double_checking: code_checks (code's own checks of the whole set, "
            "no count), verifying (code checks each answered drawing), second_looks, final_checks "
            "(no count); `stopped` on a done check that was stopped.",
        )
    )
    done: int | None = Field(default=None, description="How many of `total` are done.")
    total: int | None = Field(default=None, description="null: no count for this phase.")
    unit: Literal["drawings", "pages", "looks", "files"] | None = Field(
        default=None, description="What `done` and `total` count (pages: the roster check)."
    )
    started_at: datetime = Field(description="When the check started (the elapsed clock).")
    phase_started_at: datetime
    stopping: bool = Field(default=False, description="Stop was pressed (`check_stopping`).")


class ChatLiveDrawingOut(BaseModel):
    """One vendor drawing of the work list as a live tile (`check_drawings`, `check_drawing`)."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="The drawing's id, `p23.v1` (as in the `work_items` block).")
    page: int
    round: int | None = None
    area: str | None = Field(default=None, description="The area or sheet name (page map).")
    status: Literal["waiting", "comparing", "answered", "failed", "skipped"]
    reason: str | None = Field(
        default=None, description="Why it failed or was skipped, in plain words."
    )


class ChatLiveImpactOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    score: int = Field(ge=0, le=100)
    why: str


class ChatLiveFindingOut(BaseModel):
    """One finding as soon as code DECIDED it (`check_finding`, sent again when it changes).
    Wrong only when proved (the cloud drawing's own rule); a candidate waiting for the blind
    second look is only counted (`ChatLiveTallyOut.waiting_second_look`)."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The row's live key; never changes (later events update it).")
    id: str | None = Field(
        default=None, description="The finding's id (`F12`) once recorded in the answer."
    )
    outcome: Literal["wrong", "right", "not_compared"]
    proved_by: Literal["code", "checked_twice"] | None = Field(
        default=None,
        description="A Wrong only: code alone, or code and the blind second look ('checked twice').",
    )
    page: int | None = None
    area: str | None = None
    item: str = ""
    vendor: str | None = Field(default=None, description="The vendor's value (red when Wrong).")
    architect: str | None = None
    should_be: str | None = Field(default=None, description="What it should be (green).")
    product: str = Field(default="other", description="The product id the item is filed under.")
    reason: str | None = Field(default=None, description="Not compared: why, in plain words.")
    needs: str | None = Field(default=None, description="Not compared: what would settle it.")
    impact: ChatLiveImpactOut | None = None
    same_as: str | None = Field(
        default=None,
        description="A Wrong that is the same item as the row `same_as`: not counted, not shown "
        "(the final list keeps the item once).",
    )
    removed: bool = Field(
        default=False, description="No longer in the answer (folded into another, or left out)."
    )
    previous: Literal["wrong", "right", "not_compared"] | None = Field(
        default=None, description="Its outcome before, when it changed."
    )
    final: bool = Field(
        default=False,
        description="Decided for good (the final list). Right and Not compared may still move "
        "between each other before; a shown Wrong stays Wrong.",
    )


class ChatLiveTallyOut(BaseModel):
    """Found so far (`check_tally`): proved Wrongs only, and the differences waiting for the
    blind second look (they become Wrong only if it agrees)."""

    model_config = ConfigDict(frozen=True)

    wrong: int = Field(ge=0)
    right: int = Field(ge=0)
    not_compared: int = Field(ge=0)
    waiting_second_look: int = Field(ge=0)


class ChatLiveStoppingOut(BaseModel):
    """After Stop (`check_stopping`, sent again as drawings finish): nothing new starts."""

    model_config = ConfigDict(frozen=True)

    in_flight: int = Field(ge=0, description="Drawings being compared now: they finish, kept.")
    not_started: int = Field(ge=0, description="Drawings that will not be compared.")


class ChatLiveCountsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    drawings_done: int | None = Field(
        default=None, description="Drawings compared (answered or failed); null before the list."
    )
    drawings_total: int | None = Field(
        default=None, description="Drawings to compare (skipped ones left out)."
    )
    second_looks_done: int = 0
    second_looks_total: int = 0
    files_made: int = 0


class ChatLiveStateOut(BaseModel):
    """A running full check's whole live state, in one call (a page that reconnects)."""

    model_config = ConfigDict(frozen=True)

    v: int = Field(description="The live events' and this state's version (1).")
    mode: Literal["simple", "roster"] = Field(
        description="simple: drawings, verify and second looks; roster: pages, fewer events."
    )
    started_at: datetime
    phase: ChatLivePhaseOut
    drawings: list[ChatLiveDrawingOut]
    findings: list[ChatLiveFindingOut] = Field(
        description="Every decided finding so far (removed ones left out)."
    )
    tally: ChatLiveTallyOut
    counts: ChatLiveCountsOut
    stopping: ChatLiveStoppingOut | None = None
    service: str | None = Field(
        default=None,
        description="The AI service stopped every call (a refused key, no payment, not "
        "reachable): its plain reason.",
    )


class ChatLiveOut(BaseModel):
    """`GET /chats/{chat_id}/live`: the running full check's live state, or not running."""

    model_config = ConfigDict(frozen=True)

    running: bool
    message_id: UUID | None = Field(
        default=None, description="The answer being written (follow its events)."
    )
    live: ChatLiveStateOut | None = Field(
        default=None,
        description="null when nothing runs, or the running message is a question (no live card).",
    )


class ChatSummaryReviewOut(BaseModel):
    """A chat's newest full check's review result, for the sidebar (#1710)."""

    model_config = ConfigDict(frozen=True)

    action: Literal[
        "approved", "approved_as_noted", "revise_resubmit", "rejected", "not_for_sending"
    ]
    words: str = Field(description='"Revise and resubmit", "Check incomplete: not for sending", …')


class ChatSummaryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str | None
    created_at: datetime
    last_message_at: datetime | None
    turns: int
    mode: Literal["chat", "full_check"] | None = Field(
        default=None,
        description="#1710: the newest question's mode: a full check or a typed question; null on "
        "questions stored before it was kept.",
    )
    running: bool = Field(default=False, description="#1710: a message is being answered now.")
    drawings_done: int | None = Field(
        default=None, description="#1710: while a full check runs: drawings compared so far."
    )
    drawings_total: int | None = Field(
        default=None, description="#1710: while a full check runs: drawings to compare."
    )
    review_result: ChatSummaryReviewOut | None = Field(
        default=None,
        description="#1710: the newest full check's review result (its package made); null "
        "otherwise.",
    )


class ChatPlanOptionOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str
    label: str


class ChatPlanDoubtOut(BaseModel):
    """One question code asks before a full check, where it is unsure of the page map (#1569)."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: Literal["prototype", "role", "pair", "round", "heading"]
    page: int
    question: str
    options: list[ChatPlanOptionOut]
    picture_url: str | None = Field(
        default=None, description="The page's picture (drawings only, 100 dpi), when made."
    )
    box: list[float] | None = Field(
        default=None,
        description="The drawing it is about, as fractions of the picture (x0, y0, x1, y1).",
    )


class ChatFullCheckPlanOut(BaseModel):
    """`GET /chats/{chat_id}/full-check-plan` (#1569): code's plan and its 0-3 questions."""

    model_config = ConfigDict(frozen=True)

    state: Literal["ready", "building", "none"] = Field(
        description="ready: the page map is read; building: ask again in a moment; none: no page "
        "map can be made (no PDFs): run without questions."
    )
    summary: str | None = Field(
        default=None, description='"60 pages · 22 pairs · rounds 1-2 · 3 general pages".'
    )
    doubts: list[ChatPlanDoubtOut] = Field(default_factory=list)
    more: int = Field(default=0, description="Further unsure places, compared as read.")


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
    live: ChatLiveStateOut | None = Field(
        default=None,
        description="#1710: while a full check runs, its live state (drawings, phase, decided "
        "findings, tally), so a reloaded page draws the live card at once.",
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
    full_check_mode: Literal["roster", "simple"] = Field(
        default="roster",
        description="Which full check runs (`GV_ASSISTANT_FULL_CHECK_MODE`): the roster of "
        "specialists, or the simple check (one compare call per vendor drawing, #1665).",
    )


class ChatCancelOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    cancelled: bool


ChatAgentOut.model_rebuild()
ChatStepOut.model_rebuild()
ChatMessageOut.model_rebuild()
