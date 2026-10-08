"""Plain, frozen reviewer wording, separate from the unchanged check outcome."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator


class ReviewDisposition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: UUID
    action: Literal["confirm", "correct", "except", "dismiss"]
    reviewer: str
    at: AwareDatetime
    note: str | None = None

    def wording(self, outcome: str) -> str:
        who = f"by reviewer {self.reviewer} on {self.at.isoformat()}"
        if self.action == "confirm" and outcome in {"REVIEW_REQUIRED", "NOT_FOUND"}:
            heading = f"Checked by reviewer: OK — {who}. Recorded check remains {outcome}."
        elif self.action == "dismiss" and outcome in {"REVIEW_REQUIRED", "NOT_FOUND"}:
            heading = (
                f"Not checked: dismissed as not checkable {who}. Recorded check remains {outcome}."
            )
        else:
            verb = {
                "confirm": "confirmed",
                "correct": "corrected",
                "except": "exception recorded",
                "dismiss": "dismissed",
            }[self.action]
            heading = f"{outcome}: {verb} {who}"
        return heading + (f" Reason: {self.note}" if self.note is not None else "")


class SignedReviewFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: UUID
    rule_id: str
    outcome: str
    scope_label: str | None = None
    actions: tuple[ReviewDisposition, ...]

    @model_validator(mode="after")
    def ordered_history(self) -> SignedReviewFinding:
        times: list[datetime] = [action.at for action in self.actions]
        if times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("review action order is ambiguous")
        return self

    @property
    def wording(self) -> str:
        if not self.actions:
            return f"{self.outcome}: no per-finding action recorded"
        return self.actions[-1].wording(self.outcome)


class SignedReview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: UUID
    approved_by: str
    approved_at: AwareDatetime
    findings: tuple[SignedReviewFinding, ...]

    @model_validator(mode="after")
    def complete_record(self) -> SignedReview:
        ids = [finding.finding_id for finding in self.findings]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("signed review needs a nonempty distinct finding set")
        if any(action.at > self.approved_at for f in self.findings for action in f.actions):
            raise ValueError("review action was recorded after approval")
        return self

    @property
    def signoff(self) -> str:
        return (
            f"Signed off by {self.approved_by} on {self.approved_at.isoformat()}. "
            f"Approval id: {self.approval_id}. {len(self.findings)} findings covered."
        )
