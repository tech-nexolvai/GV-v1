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
    """`lookup`, with each pairing it returns stored once per row as a real record."""
    stored: dict[tuple[UUID, UUID], EffectivePairing] = {}
    used_ids: set[UUID] = set()

    def wrapped(session: Session, anchor_id: UUID) -> EffectivePairing | None:
        pairing = lookup(session, anchor_id)
        if pairing is None:
            return None
        key = (anchor_id, pairing.record_id)
        if key in stored:
            return stored[key]
        anchor = session.get(ObservationCandidate, anchor_id)
        if anchor is None:
            return pairing
        revision_id = session.scalar(
            select(PackageRevisionDocument.package_revision_id)
            .where(PackageRevisionDocument.document_version_id == anchor.document_version_id)
            .limit(1)
        )
        record_id = pairing.record_id if pairing.record_id not in used_ids else uuid4()
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
        used_ids.add(record_id)
        stored[key] = replace(pairing, record_id=record_id)
        return stored[key]

    return wrapped
