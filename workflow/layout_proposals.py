"""Persist proposed and confirmed layout discriminator values.

The classifier proposes; the reviewer confirms. Keeping those two writes separate is what prevents a
closed-question model answer from becoming rule applicability by accident.

Source: issue #655. Verification: tests/app/test_layout_proposals.py.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evidence import EvidenceArtifact, LayoutConfirmation, LayoutProposal

__all__ = [
    "confirmed_discriminators",
    "record_layout_confirmation",
    "record_layout_proposal",
    "stored_layout_proposals",
]


def record_layout_proposal(
    session: Session,
    *,
    package_revision_id: UUID,
    discriminator_name: str,
    proposed_value: str,
    crop_artifact_id: UUID,
    model_id: str,
    prompt_id: str,
) -> LayoutProposal:
    """File one model-proposed discriminator value, idempotent over unchanged evidence."""
    artifact = session.get(EvidenceArtifact, crop_artifact_id)
    if artifact is None:
        raise ValueError("layout proposal crop artifact does not exist")

    existing = session.execute(
        select(LayoutProposal).where(
            LayoutProposal.package_revision_id == package_revision_id,
            LayoutProposal.discriminator_name == discriminator_name,
            LayoutProposal.proposed_value == proposed_value,
            LayoutProposal.crop_artifact_id == crop_artifact_id,
            LayoutProposal.model_id == model_id,
            LayoutProposal.prompt_id == prompt_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    proposal = LayoutProposal(
        package_revision_id=package_revision_id,
        discriminator_name=discriminator_name,
        proposed_value=proposed_value,
        crop_artifact_id=crop_artifact_id,
        model_id=model_id,
        prompt_id=prompt_id,
    )
    session.add(proposal)
    session.flush()
    return proposal


def stored_layout_proposals(
    session: Session, package_revision_id: UUID
) -> tuple[LayoutProposal, ...]:
    """Return the current proposal per discriminator, newest first."""
    rows = list(
        session.execute(
            select(LayoutProposal)
            .where(LayoutProposal.package_revision_id == package_revision_id)
            .order_by(
                LayoutProposal.discriminator_name,
                LayoutProposal.created_at.desc(),
                LayoutProposal.id.desc(),
            )
        ).scalars()
    )
    current: dict[str, LayoutProposal] = {}
    for row in rows:
        current.setdefault(row.discriminator_name, row)
    return tuple(current[name] for name in sorted(current))


def record_layout_confirmation(
    session: Session,
    *,
    package_revision_id: UUID,
    discriminator_name: str,
    value: str,
    actor: str,
) -> LayoutConfirmation:
    """Append the human-confirmed discriminator value."""
    confirmation = LayoutConfirmation(
        package_revision_id=package_revision_id,
        discriminator_name=discriminator_name,
        value=value,
        confirmed_by=actor,
    )
    session.add(confirmation)
    session.flush()
    return confirmation


def confirmed_discriminators(session: Session, package_revision_id: UUID) -> dict[str, str]:
    """Latest reviewer-confirmed discriminator values for a revision."""
    rows = session.execute(
        select(LayoutConfirmation)
        .where(LayoutConfirmation.package_revision_id == package_revision_id)
        .order_by(
            LayoutConfirmation.discriminator_name,
            LayoutConfirmation.created_at.desc(),
            LayoutConfirmation.id.desc(),
        )
    ).scalars()
    confirmed: dict[str, str] = {}
    for row in rows:
        confirmed.setdefault(row.discriminator_name, row.value)
    return confirmed
