"""What one vendor keeps getting wrong, over a window, with the findings to prove it.

ADR-0006 is explicit that **vendor identity is metadata, never a rule key**: every vendor is held to
the same rule for the same layout, and selecting a rulebook by vendor would mean holding one to a
different standard than another. This module is the one legitimate use of the field — a vendor who
repeatedly gets filler distribution wrong is a conversation, not a different rulebook.

Nothing here decides anything. It reads findings that have already been decided and counts them.

**Every aggregate drills to its findings.** A count on its own is an assertion the vendor cannot
check and the reviewer cannot defend: "you get this wrong a lot" invites an argument, while "these
eleven findings, here they are" invites a look. So each entry carries the finding ids that produced
it rather than only a number.

**Windowed, because "improving" and "always been like this" are different conversations.** The trend
compares the first half of the window with the second, so a vendor who has fixed something is not
told they still do it.

**Person-confirmed only, on request (`confirmed_only`, #1272).** A FAIL nobody looked at may rest on
a number the AI misread, so it is as likely the AI's mistake as the vendor's. With `confirmed_only`
a FAIL counts only when a real reviewer stood behind it: their newest decision on that finding is a
`confirm`, or they signed the revision (`approved_findings`) and did not dismiss or correct that
finding (sign-off covers every finding, including the dismissed ones). The newest decision counts,
so a later dismiss replaces an earlier confirm; at the same instant the more cautious decision
(dismiss or correct, then except) wins over a confirm.

* **Only names on the local real-reviewer list count** (`reviewers`, the names the caller read from
  the `GV_SMRITI_REVIEWERS` list, `eval/smriti/actors.py`; compared without case or surrounding
  spaces). Most "person" rows in our copies were written by test scripts. With no list given,
  nothing counts and the report says why (`not_counted`). A `code:` actor
  (`workflow.parts.CODE_ACTOR_PREFIX`) is never a reviewer, whatever a list says.
* **A failure counts once across its carried copies.** A re-run's FAIL that inherits a decision
  (`finding_decision_carryovers`) is the same failure as the one it was carried from: however many
  of the copies were confirmed or signed, the chain counts once, under its earliest qualifying
  finding. A carried confirm alone is a copy, never a new confirmation.

The default (`confirmed_only=False`) is unchanged. Correction hotspots are not filtered; which
agent a correction belongs to (AI misread or vendor drew it) is Smriti's report
(`eval/smriti/report.py`).

Source: issues #241, #1272; ADR-0006. Verification: ``tests/reports/test_vendor_patterns.py``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.package import Package, PackageRevision
from app.models.review import (
    Approval,
    ApprovedFinding,
    FindingDecisionCarryover,
    ReviewAction,
    ReviewActionKind,
)
from app.models.verdicts import CheckRun, Finding
from verdict.outcomes import Outcome
from workflow.parts import CODE_ACTOR_PREFIX

#: How a vendor's failure rate moved across the window. `steady` is the honest default: with few
#: findings either side, a change of one or two is noise, and reporting it as a direction invites a
#: conversation the data does not support.
Trend = Literal["improving", "steady", "worsening"]

#: The minimum failures one half must reach before a direction is claimed at all.
#:
#: Either half reaching it is enough, deliberately. Nought failures followed by five is a
#: real deterioration and the report should say so; requiring both halves to be busy would
#: report the vendor as steady in exactly the case worth raising. What it suppresses is the
#: thin case — one against two — where the direction is arithmetic rather than signal.
_MINIMUM_FOR_A_TREND = 3

#: How much the failure rate must move before it counts as a direction rather than noise.
_MATERIAL_CHANGE = 0.1


@dataclass(frozen=True, slots=True)
class VendorReport:
    """One vendor's pattern over one window, with the evidence attached."""

    vendor: str
    since: datetime
    until: datetime

    recurring_failures: Mapping[str, tuple[UUID, ...]]
    """Rule snapshot id -> the findings that failed. Keyed by snapshot rather than by rule id so a
    pattern is attributed to the exact rule version that produced it: a rule whose tolerance changed
    mid-window would otherwise pool two different checks under one heading."""

    correction_hotspots: Mapping[str, tuple[UUID, ...]]
    """Rule snapshot id -> the findings a reviewer had to correct.

    Corrections matter as much as failures and are easier to miss. A check that passes only because
    somebody fixed the reading every time is not a check that works — it is a check whose input is
    unreliable, and the failure count alone says nothing about it.
    """

    trend: Trend

    confirmed_only: bool = False
    """Whether only FAILs a person confirmed or signed were counted (see the module docstring).
    Said on the report, so a person-confirmed count is never read as every FAIL, or the reverse."""

    not_counted: str | None = None
    """Why no FAIL could count as person-confirmed (no real-reviewer list given), or `None`."""

    @property
    def has_findings(self) -> bool:
        """False when nothing was decided for this vendor in the window.

        Worth asking before reading the rest: empty aggregates and a `steady` trend look identical
        to a clean record, and they are not the same thing.
        """
        return bool(self.recurring_failures or self.correction_hotspots)


def _failures(
    session: Session, vendor: str, since: datetime, until: datetime
) -> list[tuple[str, UUID, datetime]]:
    """(rule snapshot id, finding id, when) for every FAIL by this vendor in the window."""
    rows = session.execute(
        select(CheckRun.rule_snapshot_id, Finding.id, Finding.created_at)
        .join(CheckRun, Finding.check_run_id == CheckRun.id)
        .join(PackageRevision, CheckRun.package_revision_id == PackageRevision.id)
        .join(Package, PackageRevision.package_id == Package.id)
        .where(
            Package.vendor == vendor,
            Finding.outcome == Outcome.FAIL.value,
            Finding.created_at >= since,
            Finding.created_at < until,
        )
    ).all()
    return [(str(snapshot), finding, when) for snapshot, finding, when in rows]


def _is_code(actor: str | None) -> bool:
    return isinstance(actor, str) and actor.strip().casefold().startswith(CODE_ACTOR_PREFIX)


def _folded(name: str) -> str:
    return name.strip().casefold()


#: A signed FAIL still counts after these decisions: the person stood behind the failure (a
#: confirm), or accepted the deviation for this drawing (an exception), which still says it is one.
_SIGNED_STILL_STANDS: frozenset[str] = frozenset(
    {ReviewActionKind.CONFIRM.value, ReviewActionKind.EXCEPT.value}
)

#: At the same instant, the more cautious decision wins: a dismiss or a correction over an
#: exception, and any of them over a confirm (higher sorts later, so it is "newest").
_CAUTION: Mapping[str, int] = MappingProxyType(
    {
        ReviewActionKind.CONFIRM.value: 0,
        ReviewActionKind.EXCEPT.value: 1,
        ReviewActionKind.CORRECT.value: 2,
        ReviewActionKind.DISMISS.value: 2,
    }
)

NO_REVIEWER_LIST = (
    "no real-reviewer list was given (GV_SMRITI_REVIEWERS), so no FAIL counts as person-confirmed"
)


def _carried_chains(session: Session, finding_ids: list[UUID]) -> dict[UUID, UUID]:
    """Each finding's chain: the findings joined by carried decisions, named by one of them."""
    parent: dict[UUID, UUID] = {finding: finding for finding in finding_ids}

    def root(finding: UUID) -> UUID:
        while parent[finding] != finding:
            parent[finding] = parent[parent[finding]]
            finding = parent[finding]
        return finding

    for new, old in session.execute(
        select(FindingDecisionCarryover.new_finding_id, FindingDecisionCarryover.from_finding_id)
    ):
        if new in parent and old in parent:
            parent[root(new)] = root(old)
    return {finding: root(finding) for finding in finding_ids}


def _person_confirmed(
    session: Session, findings: list[tuple[UUID, datetime]], reviewers: frozenset[str]
) -> set[UUID]:
    """The FAILs among these a real reviewer confirmed or signed, one per carried chain."""
    finding_ids = [finding for finding, _ in findings]
    if not finding_ids:
        return set()

    def reviewer(actor: str | None) -> bool:
        return isinstance(actor, str) and not _is_code(actor) and _folded(actor) in reviewers

    # Every reviewer decision on each finding, its own and those carried to it, newest last.
    decisions: dict[UUID, list[tuple[datetime, int, str, str, bool]]] = {}
    own = session.execute(
        select(
            ReviewAction.finding_id,
            ReviewAction.created_at,
            ReviewAction.id,
            ReviewAction.action,
            ReviewAction.actor,
        ).where(ReviewAction.finding_id.in_(finding_ids))
    ).all()
    carried = session.execute(
        select(
            FindingDecisionCarryover.new_finding_id,
            ReviewAction.created_at,
            ReviewAction.id,
            ReviewAction.action,
            ReviewAction.actor,
        )
        .join(ReviewAction, ReviewAction.id == FindingDecisionCarryover.review_action_id)
        .where(FindingDecisionCarryover.new_finding_id.in_(finding_ids))
    ).all()
    for rows, is_carried in ((own, False), (carried, True)):
        for finding_id, when, action_id, action, actor in rows:
            if not reviewer(actor):
                continue
            decisions.setdefault(finding_id, []).append(
                (when, _CAUTION.get(action, 2), str(action_id), action, is_carried)
            )
    signed = {
        finding_id
        for finding_id, approver in session.execute(
            select(ApprovedFinding.finding_id, Approval.approved_by)
            .join(Approval, Approval.id == ApprovedFinding.approval_id)
            .where(ApprovedFinding.finding_id.in_(finding_ids))
        )
        if reviewer(approver)
    }
    qualifying: list[tuple[datetime, str, UUID]] = []
    for finding_id, created in findings:
        made = sorted(decisions.get(finding_id, ()))
        newest = made[-1] if made else None
        # The reviewer's own newest decision is a confirm (a carried one is a copy, not new) ...
        own_confirm = (
            newest is not None and newest[3] == ReviewActionKind.CONFIRM.value and not newest[4]
        )
        # ... or they signed the revision and did not dismiss or correct this finding.
        signed_standing = finding_id in signed and (
            newest is None or newest[3] in _SIGNED_STILL_STANDS
        )
        if own_confirm or signed_standing:
            qualifying.append((created, str(finding_id), finding_id))
    # One per carried chain: its earliest qualifying finding.
    chains = _carried_chains(session, finding_ids)
    kept: dict[UUID, UUID] = {}
    for _created, _id, finding_id in sorted(qualifying):
        kept.setdefault(chains[finding_id], finding_id)
    return set(kept.values())


def _corrections(
    session: Session, vendor: str, since: datetime, until: datetime
) -> list[tuple[str, UUID]]:
    """(rule snapshot id, finding id) for every correction this vendor's drawings needed.

    Counted from the review **action**, not from `correction_ledger`. The action is the event — a
    reviewer corrected this finding — while the ledger row is its detail: which observation, what
    the value was before and after. For a pattern report the event is the unit, and joining the
    ledger would also mean a correction recorded without one silently dropped out of the count.
    """
    rows = session.execute(
        select(CheckRun.rule_snapshot_id, Finding.id)
        .join(ReviewAction, ReviewAction.finding_id == Finding.id)
        .join(CheckRun, Finding.check_run_id == CheckRun.id)
        .join(PackageRevision, CheckRun.package_revision_id == PackageRevision.id)
        .join(Package, PackageRevision.package_id == Package.id)
        .where(
            Package.vendor == vendor,
            ReviewAction.action == ReviewActionKind.CORRECT.value,
            ReviewAction.created_at >= since,
            ReviewAction.created_at < until,
        )
    ).all()
    return [(str(snapshot), finding) for snapshot, finding in rows]


def _group(pairs: list[tuple[str, UUID]]) -> Mapping[str, tuple[UUID, ...]]:
    """Group findings by rule snapshot, in an order that does not change between runs.

    Sorted twice over, and both matter for a report somebody may reconcile against a previous copy:
    the keys, and the finding ids inside each. The query has no ORDER BY, so row order is whatever
    the database returns — a report that listed the same findings in a different order each time
    would look like it had changed when it had not.

    Returned read-only: `VendorReport` is frozen, and a frozen dataclass holding a plain dict is not
    actually immutable — the caller could edit the aggregate and the report would change underneath
    whoever else is holding it.
    """
    grouped: dict[str, list[UUID]] = {}
    for key, finding_id in pairs:
        grouped.setdefault(key, []).append(finding_id)
    return MappingProxyType(
        {key: tuple(sorted(values, key=str)) for key, values in sorted(grouped.items())}
    )


def _trend(failures: list[tuple[str, UUID, datetime]], since: datetime, until: datetime) -> Trend:
    """Compare the first half of the window with the second.

    Counts, not rates: the total drawings a vendor submitted per half is not in scope here, so a
    vendor who simply sent more work would read as "worsening" on a rate we cannot compute. Counting
    failures answers the question actually being asked — *is this happening more or less often than
    it was?*

    `steady` when **both** halves fall below `_MINIMUM_FOR_A_TREND` — not when either does. Nought
    failures followed by five is the case most worth raising, and requiring both halves to be busy
    would report it as steady. What the threshold suppresses is one against two, where the direction
    is arithmetic rather than signal.
    """
    midpoint = since + (until - since) / 2
    earlier = sum(1 for _, _, when in failures if when < midpoint)
    later = len(failures) - earlier

    if earlier < _MINIMUM_FOR_A_TREND and later < _MINIMUM_FOR_A_TREND:
        return "steady"

    total = earlier + later
    if total == 0:
        return "steady"

    movement = (later - earlier) / total
    if movement <= -_MATERIAL_CHANGE:
        return "improving"
    if movement >= _MATERIAL_CHANGE:
        return "worsening"
    return "steady"


def vendor_patterns(
    session: Session,
    vendor: str,
    window: timedelta,
    *,
    now: datetime | None = None,
    confirmed_only: bool = False,
    reviewers: Collection[str] | None = None,
) -> VendorReport:
    """What this vendor repeatedly got wrong in the last `window`, with the findings.

    `now` is injectable so a report is reproducible: a function that read the clock could not be
    tested for the boundary, and the boundary is where a windowed report is most often wrong.

    `confirmed_only` counts only the FAILs a real reviewer confirmed or signed (#1272); the trend
    is then the trend of those. `reviewers` names the real reviewers (the local list); with none,
    nothing counts and `not_counted` says why.

    Reads only. This is reporting, and ADR-0006 keeps vendor identity out of every decision path —
    nothing here selects a rule, resolves a parameter or influences a verdict.
    """
    if not vendor.strip():
        raise ValueError(
            "vendor must be named. An empty vendor would aggregate every package whose vendor is "
            "unrecorded into one report and attribute it to nobody."
        )

    if window <= timedelta(0):
        raise ValueError(
            f"window must be positive, got {window}. A zero or negative window returns an empty "
            "report, which is indistinguishable from a vendor with nothing against them."
        )

    until = now if now is not None else datetime.now(UTC)
    since = until - window

    failures = _failures(session, vendor, since, until)
    not_counted = None
    if confirmed_only:
        if reviewers is None:
            not_counted = NO_REVIEWER_LIST
            failures = []
        else:
            names = frozenset(_folded(name) for name in reviewers if name.strip())
            kept = _person_confirmed(
                session, [(finding, when) for _, finding, when in failures], names
            )
            failures = [failure for failure in failures if failure[1] in kept]
    corrections = _corrections(session, vendor, since, until)

    return VendorReport(
        vendor=vendor,
        since=since,
        until=until,
        recurring_failures=_group([(key, finding) for key, finding, _ in failures]),
        correction_hotspots=_group(corrections),
        trend=_trend(failures, since, until),
        confirmed_only=confirmed_only,
        not_counted=not_counted,
    )
