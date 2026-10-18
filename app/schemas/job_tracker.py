"""The job tracker's HTTP shapes (#1578): `GET /projects/{project_id}/tracker`.

Built by code from the stored chat answers and the reviewer's decisions
(`app/review/assistant/job_tracker.py`); read-only, no AI call.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "TrackerCheckOut",
    "TrackerItemLinkOut",
    "TrackerItemOut",
    "TrackerOut",
    "TrackerRankOut",
    "TrackerRoundOut",
    "TrackerSetOut",
    "TrackerSightingOut",
    "TrackerTotalsOut",
    "TrackerVendorOut",
]

type TrackerState = Literal["open", "fixed", "not_a_problem", "right_later"]
type TrackerStatus = Literal["different", "check", "same"]
type TrackerRoundStatus = Literal[
    "fixed", "still_wrong", "new", "not_in_later_round", "single_round"
]
type TrackerCheckKind = Literal["full_check", "question"]


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TrackerTotalsOut(_Out):
    sets: int = Field(description="Drawing sets with at least one check that found items.")
    checks: int = Field(description="Stored answers with findings (full checks and questions).")
    items: int
    open: int
    open_wrong: int
    open_decision: int
    fixed: int
    not_a_problem: int
    right_later: int


class TrackerSightingOut(_Out):
    """One check that saw the item: when, which round and page, and what it said."""

    at: str
    check: TrackerCheckKind
    check_title: str
    chat_id: str
    message_id: str
    finding_id: str = Field(description="The live finding a click opens (the carrier of a fold).")
    round: int
    page: int | None
    status: str = Field(
        description="different (Wrong), check (Not compared, #1619) or same (Right)."
    )
    compared: bool = Field(description="False: a Right that was not compared, so no proof.")
    round_status: TrackerRoundStatus | None
    folded: bool = Field(description="A page the resolver folded into another finding (#1554).")
    vendor: str
    architect: str
    decision: str | None
    decided_by: str | None


class TrackerItemLinkOut(_Out):
    package_id: str
    chat_id: str
    message_id: str
    finding_id: str


class TrackerItemOut(_Out):
    """One item found, across its checks and rounds."""

    key: str = Field(description="Stable id: from the first check that found it.")
    package_id: str
    set_name: str
    vendor_name: str
    vendor_key: str
    item: str
    page: int | None
    vendor_value: str
    architect_value: str
    category: str
    category_label: str
    kind: str = Field(description='What it touches, by code ("Overall size", "Height").')
    check: str
    check_label: str
    state: TrackerState
    open_kind: Literal["wrong", "decision"] | None
    status_now: str
    ever_wrong: bool
    fixed_round: int | None
    tags: list[str] = Field(description='"New in round 2", "Still wrong".')
    first_round: int
    round_now: int
    rounds: list[int]
    first_seen_at: str
    last_seen_at: str
    checks_seen: int
    impact: int | None = Field(description="Open items: how much it matters (#1569), 0-100.")
    decision: str | None
    decided_by: str | None
    decided_at: str | None
    link: TrackerItemLinkOut
    history: list[TrackerSightingOut]


class TrackerRoundOut(_Out):
    round: int
    items: int
    open: int
    wrong: int
    decision: int
    fixed: int = Field(description="Items this round fixed (Wrong before, right here).")


class TrackerCheckOut(_Out):
    chat_id: str
    message_id: str
    at: str
    kind: TrackerCheckKind
    title: str
    stopped: str | None
    rounds: list[int]
    wrong: int
    decision: int
    right: int


class TrackerSetOut(_Out):
    package_id: str
    name: str
    vendor_name: str
    vendor_key: str
    product: str | None
    items: int
    open: int
    fixed: int
    not_a_problem: int
    rounds: list[TrackerRoundOut]
    last_checked_at: str
    checks: list[TrackerCheckOut]


class TrackerRankOut(_Out):
    label: str
    count: int
    share: str | None = Field(description="Percent of the vendor's mistakes, whole number.")


class TrackerVendorOut(_Out):
    """The vendor scorecard: what it gets wrong most, and how it fixes."""

    vendor_key: str
    vendor_name: str
    sets: int
    rounds: int
    items: int
    mistakes: int = Field(description="Items Wrong at least once, minus Not a problem.")
    open: int
    fixed_next_round: int
    fixed_next_of: int = Field(description="Wrong items seen again in the next round.")
    fixed_next_share: str | None
    average_open_per_round: str | None
    by_product: list[TrackerRankOut]
    by_kind: list[TrackerRankOut]
    by_check: list[TrackerRankOut]


class TrackerOut(_Out):
    project_id: str
    made_at: str
    totals: TrackerTotalsOut
    sets: list[TrackerSetOut]
    items: list[TrackerItemOut]
    vendors: list[TrackerVendorOut]
