"""Reviewer correction rates derived from the append-only review record.

The denominator is every extracted evidence reading a reviewer explicitly checked: ``confirm``
plus ``correct`` evidence actions.  The numerator is the subset of those actions backed by a row in
``correction_ledger``.  Finding-level dismissals and exceptions are deliberately absent because
they judge a finding, not whether an extractor read an observation correctly.

A person's answer about a held reading (#1339) is a ``confirm`` evidence action too, and it is
counted whatever the result it was recorded through, a PASS or FAIL included (#1367). It carries the
tag ``held-reading-answer``, which keeps every reader of decisions from taking it as one; this
metric counts it like any other confirmation, because it judges a reading, not a finding.

Rates are grouped by check type, extractor and extractor version.  Keeping the extractor name
prevents unrelated readers that both call their version ``1.0`` from being merged.  ``MetricResult``
keeps the count pair visible and uses an exact ``Fraction``; no reviewed evidence is reported as
``NOT MEASURED``, never as a perfect zero-correction rate.

The simple review header (#1386) shows the same count for one package revision
(`reviewed_readings`): the same actions (`_reviewed`), scoped to the revision instead of a window.

Source: ``AGENTS.md`` section 9 and backend proposal section 12.
Design: ``docs/DESIGN_PRODUCT.md`` section 4.
Verification: ``tests/eval/test_correction_rate.py``, ``tests/api/test_review_header_accuracy.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from uuid import UUID

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.models.evidence import (
    EvidenceCandidateRole,
    EvidenceSupportingCandidate,
    ObservationCandidate,
)
from app.models.review import CorrectionLedgerEntry, ReviewAction, ReviewActionKind
from app.models.rules import RuleSnapshot
from app.models.runs import ExtractionRun
from app.models.verdicts import CheckRun, Finding
from eval.metrics import MetricResult

__all__ = [
    "CorrectionRateKey",
    "ReviewedReading",
    "correction_rate",
    "report",
    "reviewed_readings",
]


@dataclass(frozen=True, slots=True, order=True)
class CorrectionRateKey:
    """The dimensions needed to attribute a correction-rate change."""

    check_type: str
    extractor: str
    extractor_version: str


def _start(window: timedelta, *, now: datetime | None) -> datetime:
    if window <= timedelta(0):
        raise ValueError(
            "window must be a positive duration; otherwise no reviewed evidence could qualify"
        )
    instant = utc_now() if now is None else now
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("now must be timezone-aware so the measurement window is unambiguous")
    return instant - window


def _reviewed[*Row](
    statement: Select[*Row], *, since: datetime | None = None, revision_id: UUID | None = None
) -> Select[*Row]:
    """Add the joins and filters that say which review actions this metric counts, to `statement`.

    The one definition of a reviewed reading, shared by the grouped metric (`_query`) and by one
    package revision's own count (`reviewed_readings`): a ``confirm`` or ``correct`` evidence action
    on an extracted reading (it names the observation it judged), attributed to the reader that
    produced it through the primary-candidate link (corroborating readers did not supply the value
    being reviewed). A held-reading answer (#1339) is such a ``confirm``, whatever its tags (#1367).
    The correction ledger is outer-joined: an action with a ledger row is a correction.
    """

    statement = (
        statement.select_from(ReviewAction)
        .join(Finding, Finding.id == ReviewAction.finding_id)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(
            EvidenceSupportingCandidate,
            and_(
                EvidenceSupportingCandidate.canonical_observation_id
                == ReviewAction.original_observation_id,
                EvidenceSupportingCandidate.role == EvidenceCandidateRole.PRIMARY.value,
            ),
        )
        .join(
            ObservationCandidate,
            ObservationCandidate.id == EvidenceSupportingCandidate.candidate_id,
        )
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .outerjoin(
            CorrectionLedgerEntry,
            CorrectionLedgerEntry.review_action_id == ReviewAction.id,
        )
        .where(
            ReviewAction.original_observation_id.is_not(None),
            ReviewAction.action.in_(
                (ReviewActionKind.CONFIRM.value, ReviewActionKind.CORRECT.value)
            ),
        )
    )
    if since is not None:
        statement = statement.where(ReviewAction.created_at >= since)
    if revision_id is not None:
        statement = statement.where(ReviewAction.package_revision_id == revision_id)
    return statement


def _query(since: datetime) -> Select[str, str, str, int, int]:
    """Build the one authoritative query used by the metric.

    ``COUNT(DISTINCT action.id)`` prevents an observation supported by duplicate relational paths
    from inflating the denominator.  Which actions count is `_reviewed`'s, shared with
    `reviewed_readings`.
    """

    reviewed = func.count(func.distinct(ReviewAction.id))
    corrected = func.count(func.distinct(CorrectionLedgerEntry.review_action_id))
    statement = _reviewed(
        select(
            RuleSnapshot.check_type,
            ExtractionRun.extractor,
            ExtractionRun.extractor_version,
            corrected,
            reviewed,
        ),
        since=since,
    )
    return statement.group_by(
        RuleSnapshot.check_type,
        ExtractionRun.extractor,
        ExtractionRun.extractor_version,
    ).order_by(
        RuleSnapshot.check_type,
        ExtractionRun.extractor,
        ExtractionRun.extractor_version,
    )


@dataclass(frozen=True, slots=True)
class ReviewedReading:
    """One review action this metric counts: whether it was a correction, and the reading judged.

    `readers` is every primary reader candidate of the observation the action judged (one in
    practice; more only where duplicate relational paths exist, which the metric never double
    counts: one `ReviewedReading` per action).
    """

    action_id: UUID
    corrected: bool
    readers: tuple[ObservationCandidate, ...]


def reviewed_readings(session: Session, *, revision_id: UUID) -> list[ReviewedReading]:
    """Every review action this metric counts on one package revision, one entry per action.

    The same actions `correction_rate` counts (`_reviewed`), scoped to the revision the action was
    recorded on instead of a time window, so its count and its corrections are exactly the metric's
    denominator and numerator for this revision. A review action never crosses revisions
    (`app/lifecycle/carry_forward.py`); a person's answers on this revision's earlier readings (#1206)
    are on this revision and counted.
    """

    rows = session.execute(
        _reviewed(
            select(
                ReviewAction.id,
                CorrectionLedgerEntry.review_action_id.is_not(None),
                ObservationCandidate,
            ),
            revision_id=revision_id,
        ).order_by(ReviewAction.created_at, ReviewAction.id)
    ).all()
    corrected: dict[UUID, bool] = {}
    readers: dict[UUID, dict[UUID, ObservationCandidate]] = {}
    for action_id, is_correction, candidate in rows:
        corrected[action_id] = corrected.get(action_id, False) or bool(is_correction)
        readers.setdefault(action_id, {})[candidate.id] = candidate
    return [
        ReviewedReading(action_id, corrected[action_id], tuple(readers[action_id].values()))
        for action_id in corrected
    ]


def correction_rate(
    session: Session,
    *,
    window: timedelta,
    now: datetime | None = None,
) -> dict[CorrectionRateKey, MetricResult]:
    """Return correction rates for reviewed evidence in ``window``.

    ``now`` exists only to make the time boundary reproducible in tests and scheduled reports.  It
    must be timezone-aware.  An empty mapping means no group was measured; :func:`report` renders
    that state loudly rather than inventing a zero rate.
    """

    rows = session.execute(_query(_start(window, now=now)))
    results: dict[CorrectionRateKey, MetricResult] = {}
    for check_type, extractor, extractor_version, corrected, reviewed in rows:
        key = CorrectionRateKey(check_type, extractor, extractor_version)
        results[key] = MetricResult(
            key="reviewer_correction_rate",
            value=None if reviewed == 0 else Fraction(corrected, reviewed),
            numerator=corrected,
            denominator=reviewed,
            note=("no confirm or correct evidence actions in the window" if reviewed == 0 else ""),
        )
    return results


def report(results: dict[CorrectionRateKey, MetricResult]) -> str:
    """Render the metric beside its explicit denominator in stable order."""

    lines = [
        "Reviewer correction rate — corrected readings / confirmed-or-corrected readings",
        "",
    ]
    if not results:
        lines.append("NOT MEASURED — no evidence readings were explicitly reviewed in this window.")
        return "\n".join(lines)
    for key in sorted(results):
        lines.append(f"{key.check_type} · {key.extractor} {key.extractor_version}: {results[key]}")
    return "\n".join(lines)
