"""Plain, frozen reviewer wording, separate from the unchanged check outcome.

**"Checked by hand: wrong" (#1358).** On the simple review a person may say a result they checked by
hand is wrong. It is recorded as DISMISS (the one verb that never counts as OK or accepted) with the
fixed note `CHECKED_WRONG_NOTE`, the one constant every writer and reader uses. The signed record
prints it as "Checked by reviewer: WRONG — problem for the vendor", and `checked_wrong_by_hand` is
the mark the cloud PDF reads (W4a), so nothing ever matches the words themselves.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

__all__ = [
    "CHECKED_WRONG_NOTE",
    "DEPTH_NOT_THIS_PREFIX",
    "ReviewDisposition",
    "SignedReview",
    "SignedReviewFinding",
]

#: The fixed note of a person's "checked by hand: wrong" (a DISMISS on the simple review). Written
#: by the old Review list's answer actions (removed 2026-10-28), read here and by the cloud PDF through
#: `ReviewDisposition.checked_wrong_by_hand`.
CHECKED_WRONG_NOTE: Final = "Checked by hand: wrong (simple review)."

#: How every "Not this" on a depth or sink reading (a DISMISS on the simple review, #1360) begins;
#: the rest names the countertop and the reading. Written by the old Review list (removed 2026-10-28),
#: read through `ReviewDisposition.not_this_reading`: such a result is printed NOT CHECKED
#: (Anant, 2026-10-16: "Not this" = Not checked).
DEPTH_NOT_THIS_PREFIX: Final = "Not checked: a person said the AI's reading "


class ReviewDisposition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: UUID
    action: Literal["confirm", "correct", "except", "dismiss"]
    reviewer: str
    at: AwareDatetime
    note: str | None = None
    carried_from_finding_id: UUID | None = None
    """Set when this decision was carried over an unchanged check re-run (#1073): the finding the
    reviewer actually decided on. Still the reviewer's own action, with their name, time and note."""

    @property
    def checked_wrong_by_hand(self) -> bool:
        """A person checked this result by hand and found it wrong: a problem for the vendor. The
        result stays as recorded; this is the mark the cloud PDF reads (#1358)."""
        return self.action == "dismiss" and self.note == CHECKED_WRONG_NOTE

    @property
    def not_this_reading(self) -> bool:
        """A person said a depth or sink reading is not this value (#1360): the check is not
        checked, and printed NOT CHECKED."""
        return self.action == "dismiss" and (self.note or "").startswith(DEPTH_NOT_THIS_PREFIX)

    def wording(self, outcome: str) -> str:
        who = f"by reviewer {self.reviewer} on {self.at.isoformat()}"
        if self.checked_wrong_by_hand and outcome in {"REVIEW_REQUIRED", "NOT_FOUND"}:
            # Never "OK", "accepted" or "dismissed": the person said it is wrong (#1358).
            heading = (
                f"Checked by reviewer: WRONG — problem for the vendor, {who}. "
                f"Recorded check remains {outcome}."
            )
            return heading + self._carried()
        if self.action == "confirm" and outcome == "FAIL":
            heading = f"FAIL: problem for the vendor, confirmed {who}."
        elif self.action == "confirm" and outcome in {"REVIEW_REQUIRED", "NOT_FOUND"}:
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
        return (
            heading + (f" Reason: {self.note}" if self.note is not None else "") + self._carried()
        )

    def _carried(self) -> str:
        if self.carried_from_finding_id is None:
            return ""
        return (
            " Carried over from the previous check run, whose result was the same "
            f"(decided on finding {self.carried_from_finding_id})."
        )


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
    carried_answers_confirmation: str | None = None
    """When results rested on answers carried from an earlier revision (#1180): who confirmed at
    sign-off that they still apply, and when. Absent from records signed before #1180."""

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
            + (f" {self.carried_answers_confirmation}" if self.carried_answers_confirmation else "")
        )
