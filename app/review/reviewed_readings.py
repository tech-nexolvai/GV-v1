"""Which review actions are answers about an AI reading, and which of them corrected it (#1386).

The one definition shared by the reviewer correction rate (`eval/correction_rate.py`) and the
simple review header's accuracy (`app/review/review_items.py`). It lives in production code because
production never imports `eval/` (`tests/eval/test_run_check_boundary.py`); the metric imports it
from here.

**An answer about an AI reading** is a ``confirm`` or ``correct`` evidence action on an extracted
reading: it names the observation it judged, attributed to the reader that produced it through the
primary-candidate link (corroborating readers did not supply the value being reviewed). A
held-reading answer (#1339) is such a ``confirm``, whatever its tags (#1367). Finding-level
decisions (an Agree on a FAIL, "checked right" on a result, dismissals) name no observation and are
never counted. **A correction** is an action with a row in ``correction_ledger``.

Verification: ``tests/eval/test_correction_rate.py``, ``tests/api/test_review_header_accuracy.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session

from app.models.evidence import (
    EvidenceCandidateRole,
    EvidenceSupportingCandidate,
    ObservationCandidate,
)
from app.models.review import CorrectionLedgerEntry, ReviewAction, ReviewActionKind
from app.models.rules import RuleSnapshot
from app.models.runs import ExtractionRun
from app.models.verdicts import CheckRun, Finding

__all__ = ["ReviewedReading", "reviewed_actions", "reviewed_readings"]


def reviewed_actions[*Row](
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

    The same actions `correction_rate` counts (`reviewed_actions`), scoped to the revision the
    action was recorded on instead of a time window, so its count and its corrections are exactly
    the metric's denominator and numerator for this revision. A review action never crosses
    revisions (`app/lifecycle/carry_forward.py`); a person's answers on this revision's earlier
    readings (#1206) are on this revision and counted.
    """

    rows = session.execute(
        reviewed_actions(
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
