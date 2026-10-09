"""What an evidence crop shows of GV's markup, as a reader should be told it (#952, #1141).

A crop's own `EvidenceArtifact.shows_gv_marks` was written when it was cut. Crops cut before #1078
asked an incomplete question, and the crop row cannot be changed (append-only), so a later re-check
is a row of its own (`EvidenceMarkRecheck`). **The newest re-check wins; without one, the crop's own
flag stands.** A re-check that says "not checked" (`None`) still wins: it is an answer, not an
absence.

One function, so the finding chain, the candidate list and the vendor-only view cannot disagree
about the same crop. Display only: nothing that decides an outcome reads this flag.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evidence import EvidenceMarkRecheck


def current_crop_marks(
    session: Session, stored: Mapping[UUID, bool | None]
) -> dict[UUID, bool | None]:
    """Each crop's flag as it should be shown, keyed like `stored` (crop id → stored flag).

    One query, bounded by the crops asked about: only their re-checks are read.
    """
    marks = dict(stored)
    if not marks:
        return marks
    rechecks = session.execute(
        select(EvidenceMarkRecheck.crop_artifact_id, EvidenceMarkRecheck.shows_gv_marks)
        .where(EvidenceMarkRecheck.crop_artifact_id.in_(list(marks)))
        .order_by(EvidenceMarkRecheck.created_at, EvidenceMarkRecheck.id)
    ).all()
    for crop_artifact_id, shows_gv_marks in rechecks:
        marks[crop_artifact_id] = shows_gv_marks
    return marks
