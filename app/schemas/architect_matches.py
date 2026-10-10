"""What the reviewer is shown to match a vendor countertop with the architect's view (#1166).

`ArchitectViewRefOut` names one view of the architect's own file wherever a screen or a report
points at it (the picker here; the countertop results in Phase 5). Plain values only.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.review.row_location import RowLocation

__all__ = [
    "ArchitectCandidateCodeOut",
    "ArchitectMatchCurrentOut",
    "ArchitectMatchVendorOut",
    "ArchitectViewCandidateOut",
    "ArchitectViewMatchOut",
    "ArchitectViewPickIn",
    "ArchitectViewRefOut",
]


class ArchitectViewRefOut(BaseModel):
    """One view of the architect's own file."""

    view_id: UUID
    document_id: UUID
    document_version_id: UUID
    file_name: str
    page_number: int = Field(description="1-based, as a person counts pages.")
    sheet_number: str | None
    bubble: str | None
    title: str | None
    scale_note: str | None
    label: str = Field(description="How a person names it: page, view, title and sheet.")
    region: RowLocation | None = Field(
        description="The view's extent on its page, in stored space; null when not stored."
    )
    picture_url: str | None = Field(
        description="The stored picture of the view; null when none was rendered."
    )
    separated: bool = Field(
        description="False when the view is not clearly apart from its neighbour: its dimensions "
        "were not read."
    )


class ArchitectMatchVendorOut(BaseModel):
    page_number: int
    document_version_id: UUID
    title: str | None
    references: list[str] = Field(
        description="Architect view references printed on the vendor's sheet, normalised."
    )
    region: RowLocation | None


class ArchitectMatchCurrentOut(BaseModel):
    record_id: UUID
    status: str
    source: str
    decided_by: str | None
    decided_at: datetime
    supersedes_id: UUID | None
    note: str | None
    reasons: list[str]


class ArchitectCandidateCodeOut(BaseModel):
    fits: bool
    reference_match: bool
    run_length_error_display: str | None = Field(
        description="How far the view's run is from the vendor's, in inches to a tenth (display "
        "only; code decides with exact values)."
    )
    bays_vendor: int | None
    bays_architect: int | None
    pair_support: int | None


class ArchitectViewCandidateOut(BaseModel):
    rank: int
    view: ArchitectViewRefOut
    shown_to_ais: bool
    code: ArchitectCandidateCodeOut
    score_summary: str
    evidence: list[str]
    ai_picked_by: list[str]
    remembered: bool = Field(
        description="A reviewer chose this view for the same item on an earlier revision. "
        "Evidence only: it is never pre-selected."
    )
    can_pick: bool
    refusal: str | None


class ArchitectViewMatchOut(BaseModel):
    row_id: UUID
    vendor: ArchitectMatchVendorOut
    current: ArchitectMatchCurrentOut | None
    candidates: list[ArchitectViewCandidateOut]
    can_choose_none: bool = True


class ArchitectViewPickIn(BaseModel):
    """A reviewer's pick: exactly one of a view and "none of these"."""

    model_config = ConfigDict(extra="forbid")

    view_id: UUID | None = None
    none_of_these: bool = False
    note: str | None = Field(default=None, max_length=500)
    expected_record_id: UUID = Field(
        description="The `current.record_id` the reviewer was shown. When the row's match has "
        "moved on since, nothing is recorded and the answer is 409."
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> ArchitectViewPickIn:
        if (self.view_id is None) == (not self.none_of_these):
            raise ValueError("choose one of the architect's views, or 'none of these'")
        return self
