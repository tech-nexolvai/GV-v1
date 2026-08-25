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

Source: issue #241; ADR-0006. Verification: ``tests/reports/test_vendor_patterns.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.package import Package, PackageRevision
from app.models.review import ReviewAction, ReviewActionKind
from app.models.verdicts import CheckRun, Finding
from verdict.outcomes import Outcome

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
    it was?* — and `steady` is returned whenever either half is too thin to say.
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
    session: Session, vendor: str, window: timedelta, *, now: datetime | None = None
) -> VendorReport:
    """What this vendor repeatedly got wrong in the last `window`, with the findings.

    `now` is injectable so a report is reproducible: a function that read the clock could not be
    tested for the boundary, and the boundary is where a windowed report is most often wrong.

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
    corrections = _corrections(session, vendor, since, until)

    return VendorReport(
        vendor=vendor,
        since=since,
        until=until,
        recurring_failures=_group([(key, finding) for key, finding, _ in failures]),
        correction_hotspots=_group(corrections),
        trend=_trend(failures, since, until),
    )
