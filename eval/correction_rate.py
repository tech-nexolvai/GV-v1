"""Reviewer correction rates derived from the append-only review record.

The denominator is every extracted evidence reading a reviewer explicitly checked: ``confirm``
plus ``correct`` evidence actions.  The numerator is the subset of those actions backed by a row in
``correction_ledger``.  Finding-level dismissals and exceptions are deliberately absent because
they judge a finding, not whether an extractor read an observation correctly.

Rates are grouped by check type, extractor and extractor version.  Keeping the extractor name
prevents unrelated readers that both call their version ``1.0`` from being merged.  ``MetricResult``
keeps the count pair visible and uses an exact ``Fraction``; no reviewed evidence is reported as
``NOT MEASURED``, never as a perfect zero-correction rate.

Source: ``AGENTS.md`` section 9 and backend proposal section 12.
Design: ``docs/DESIGN_PRODUCT.md`` section 4.
Verification: ``tests/eval/test_correction_rate.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction

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

__all__ = ["CorrectionRateKey", "correction_rate", "report"]


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


def _query(since: datetime) -> Select[tuple[str, str, str, int, int]]:
    """Build the one authoritative query used by the metric.

    ``COUNT(DISTINCT action.id)`` prevents an observation supported by duplicate relational paths
    from inflating the denominator.  The primary-candidate link attributes the reading to the
    extractor that produced it; corroborating readers did not supply the value being reviewed.
    """

    reviewed = func.count(func.distinct(ReviewAction.id))
    corrected = func.count(func.distinct(CorrectionLedgerEntry.review_action_id))
    return (
        select(
            RuleSnapshot.check_type,
            ExtractionRun.extractor,
            ExtractionRun.extractor_version,
            corrected,
            reviewed,
        )
        .select_from(ReviewAction)
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
            ReviewAction.created_at >= since,
            ReviewAction.original_observation_id.is_not(None),
            ReviewAction.action.in_(
                (ReviewActionKind.CONFIRM.value, ReviewActionKind.CORRECT.value)
            ),
        )
        .group_by(
            RuleSnapshot.check_type,
            ExtractionRun.extractor,
            ExtractionRun.extractor_version,
        )
        .order_by(
            RuleSnapshot.check_type,
            ExtractionRun.extractor,
            ExtractionRun.extractor_version,
        )
    )


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
