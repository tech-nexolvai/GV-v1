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
    "ChatFindingDecisionHelpOut",
    "ChatFindingDecisionIn",
    "ChatFindingDecisionOut",
    "ChatFindingDroppedBlock",
    "ChatFindingEstimateOut",
    "ChatFindingOut",
    "ChatListOut",
    "ChatMessageIn",
    "ChatMessageOut",
    "ChatOut",
    "ChatProgressBlock",
    "ChatProgressOut",
    "ChatResumeBlock",
    "ChatResumeOut",
    "ChatStepBlock",
    "ChatStepOut",
    "ChatSummaryOut",
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
    continue_from: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        description="Continue (#1514): the id of the answer a server restart interrupted. The "
        "answer goes on from its saved work; a full check keeps its findings and checks only the "
        "pages not yet done (`mode` and `products` are then the interrupted answer's).",
    )
    task: ChatTaskSpecIn | None = Field(
        default=None,
        description="#1551: the reviewer's correction of how the app understood the question "
        "(Edit on the 'Understood as' line); the question's words are sent unchanged in `text`.",
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
    """The AI's best estimate for a "Your decision" item, with how it got it (#1545)."""

    model_config = ConfigDict(frozen=True)

    value_in: float
    plus_minus_in: float
    method: str


class ChatFindingDecisionHelpOut(BaseModel):
    """A "Your decision" item's reasoning for the reviewer (#1545), in plain words."""

    model_config = ConfigDict(frozen=True)

    measures: str = Field(description="What the dimension measures, from where to where.")
    estimate: ChatFindingEstimateOut | None = None
    likely: Literal["probably fine", "probably wrong", "can't tell"]
    confirm: str = Field(description="What the reviewer should confirm, and with whom.")


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
        description='"lead", "helper:<n>", "gv_review" (made by code from a recorded result), or '
        '"ledger" (made by code from the reading ledger).'
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
        description="The non-AI witnesses behind it: printed, rule, arithmetic, ledger, units.",
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
        description="Status check (Your decision): what it measures, the best estimate, how "
        "likely it is fine, what to confirm (#1545).",
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
        "second_reads, third_reads, critic, finish_checks, cloud_drawing, report"
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
        "unreadable", "not_opened", "missing_job_value", "pairing_uncertain", "refused_alignment"
    ]
    needs: str
    scope: Literal["page", "item"] = "item"


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
    products: list[Literal["countertop", "cabinet"]] | None = None
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
    | ChatUnknownsBlock
    | ChatScopeBlock
    | ChatProgressBlock
    | ChatResumeBlock
    | ChatUnderstoodBlock,
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
        description="The answer's accuracy by code (its `accuracy` block, scored again with "
        "today's calibration: see `rescored`); null on older answers.",
    )
    mode: Literal["chat", "full_check"] | None = Field(
        default=None,
        description="A typed question (`chat`) or the one-click full check; null on messages "
        "stored before it was kept (#1513).",
    )
    products: list[Literal["countertop", "cabinet"]] | None = Field(
        default=None, description="The products the message was about; null on older messages."
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
