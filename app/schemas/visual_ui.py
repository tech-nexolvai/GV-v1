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
