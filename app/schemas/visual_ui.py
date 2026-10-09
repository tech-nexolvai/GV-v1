"""Read-only data contracts for the visual reviewer and project dashboard."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.review.row_location import RowLocation
from verdict.outcomes import Outcome


class ExactValueOut(BaseModel):
    """An exact rational and its reviewer-facing inch notation."""

    model_config = ConfigDict(frozen=True)

    numerator: str
    denominator: str
    display: str


class CountertopPieceOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    value: ExactValueOut | None
    source: Literal["sealed", "typed", "missing"]
    kind: str | None = None


class ReviewerDecisionOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: str
    note: str | None
    actor: str
    time: datetime


class WallLayoutOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    config: str | None
    label: str | None
    source: Literal[
        "drawing clues", "both readers", "reviewer", "between panels", "not established"
    ]


class AgreementFactsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    both_readers_agreed_on_row: bool | None
    values_agreed: tuple[bool | None, ...]
    code_clue_used: bool


class HoldOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    reason: str


class ArchitectComparedOut(BaseModel):
    """One width both drawings print for the same thing, as the architect check compared it."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["overall", "piece"]
    vendor_piece: int | None = Field(
        description="The vendor piece, counted from 1 as the row shows it; null for the overall."
    )
    vendor: ExactValueOut | None
    architect: ExactValueOut | None
    delta: ExactValueOut | None = Field(
        description="The vendor's value minus the architect's, exactly."
    )
    vendor_display: str | None
    architect_display: str | None
    delta_display: str | None
    outcome: Outcome | None


class ArchitectResultOut(BaseModel):
    """The vendor-vs-architect check (CT-ARCH-WIDTH-001) for one countertop row (#1054).

    `finding_id` and `outcome` are null when nothing was compared for this row; then
    `not_compared_reason` says why, and no reviewer decision is needed for it. Values are the ones
    the recorded check used, never recomputed.
    """

    model_config = ConfigDict(frozen=True)

    outcome: Outcome | None = None
    finding_id: UUID | None = None
    reason: str | None = None
    needs_decision: bool = False
    compared: tuple[ArchitectComparedOut, ...] = ()
    not_compared_reason: str | None = None
    pairing_source: Literal["code", "both-ais", "reviewer", "none"] | None = None


class CountertopResultOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: UUID | None
    row_id: UUID
    page_number: int
    label: str
    row_location: RowLocation | None
    outcome: Outcome | None
    reviewer_decision: ReviewerDecisionOut | None
    needs_decision: bool
    printed_overall: ExactValueOut | None
    pieces: tuple[CountertopPieceOut, ...]
    field_cut_per_end: ExactValueOut | None
    field_cut_count: int | None
    expected_total: ExactValueOut | None
    delta: ExactValueOut | None
    wall_layout: WallLayoutOut
    agreement: AgreementFactsOut
    hold: HoldOut | None
    architect: ArchitectResultOut = Field(
        default_factory=ArchitectResultOut,
        description="Whether this row matches the architect's drawing (CT-ARCH-WIDTH-001).",
    )


class CountertopResultsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    package_id: UUID
    revision_id: UUID
    items: tuple[CountertopResultOut, ...]


class OutcomeCountsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    pass_: int = Field(alias="pass")
    fail: int
    review: int
    not_found: int
    no_rule: int


class PackageSummaryItemOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    package_id: UUID
    revision_id: UUID
    revision_number: int
    vendor: str | None
    product_type: str | None
    state: str
    created_at: datetime
    updated_at: datetime
    outcomes: OutcomeCountsOut
    needs_decision: int
    approved: bool
    signed_exports_ready: bool


class PackageSummaryPageOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[PackageSummaryItemOut, ...]
    next_cursor: str | None
    limit: int


class UsageTotalsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    calls: int
    failed_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: str
    unpriced_calls: int


class ModelUsageOut(UsageTotalsOut):
    model_config = ConfigDict(frozen=True)

    model: str


class UsageGroupOut(UsageTotalsOut):
    model_config = ConfigDict(frozen=True)

    day: date | None = None
    package_id: UUID | None = None
    models: tuple[ModelUsageOut, ...]


class PackageReadingTimeOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    package_id: UUID
    revision_id: UUID
    run_id: UUID
    first_call_at: datetime
    last_call_at: datetime
    duration_ms: int


class UsageOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None
    group_by: Literal["day", "package"]
    totals: UsageTotalsOut
    groups: tuple[UsageGroupOut, ...]
    package_reading_times: tuple[PackageReadingTimeOut, ...]
