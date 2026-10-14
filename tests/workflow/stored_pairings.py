"""Store each injected architect pairing as a real pairing record (#1228 review).

The architect tests hand the check stage a pairing (`DatabaseStages(architect_pairing=...)`) whose
record id is invented. Production always reads a stored record (`effective_architect_pairing`),
and Pariksha's run check and Nina's gate check the record a result's note names. This wraps such a
lookup so every pairing it returns is first stored as an `ArchitectPairingRecord` with that same
id, on the row it is returned for: the fixtures then look like production. Synthetic values only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ArchitectPairingRecord, ObservationCandidate, PackageRevisionDocument
from workflow.architect_pairing_contract import EffectivePairing

Lookup = Callable[[Session, UUID], EffectivePairing | None]


def stored_pairings(lookup: Callable[[Any, UUID], EffectivePairing | None]) -> Lookup:
    """`lookup`, with each pairing it returns backed by a real record on its row.

    A row with no record gets one (a root record, with the pairing's own id when that id is free).
    A row a test already gave records keeps them untouched: the pairing names the newest one with
    the same source, or is returned as it is. "No pairing" (`none`) is never stored."""

    def wrapped(session: Session, anchor_id: UUID) -> EffectivePairing | None:
        pairing = lookup(session, anchor_id)
        if pairing is None or pairing.source == "none":
            return pairing
        named = session.get(ArchitectPairingRecord, pairing.record_id)
        if named is not None and named.row_anchor_candidate_id == anchor_id:
            return pairing
        existing = session.scalars(
            select(ArchitectPairingRecord)
            .where(ArchitectPairingRecord.row_anchor_candidate_id == anchor_id)
            .order_by(ArchitectPairingRecord.created_at.desc(), ArchitectPairingRecord.id)
        ).all()
        if existing:
            same = next((record for record in existing if record.source == pairing.source), None)
            return pairing if same is None else replace(pairing, record_id=same.id)
        anchor = session.get(ObservationCandidate, anchor_id)
        if anchor is None:
            return pairing
        revision_id = session.scalar(
            select(PackageRevisionDocument.package_revision_id)
            .where(PackageRevisionDocument.document_version_id == anchor.document_version_id)
            .limit(1)
        )
        record_id = pairing.record_id if named is None else uuid4()
        reviewer = pairing.source == "reviewer"
        session.add(
            ArchitectPairingRecord(
                id=record_id,
                package_revision_id=revision_id,
                page_id=anchor.page_id,
                row_anchor_candidate_id=anchor_id,
                extraction_run_id=None if reviewer else anchor.extraction_run_id,
                source=pairing.source,
                status="reviewer" if reviewer else pairing.status,
                pairs=[],
                details={},
                decided_by="synthetic reviewer" if reviewer else None,
            )
        )
        session.flush()
        return replace(pairing, record_id=record_id)

    return wrapped
