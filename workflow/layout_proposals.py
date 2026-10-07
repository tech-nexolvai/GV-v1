"""Persist proposed and confirmed layout discriminator values.

The classifier proposes; the reviewer confirms. Keeping those two writes separate is what prevents a
closed-question model answer from becoming rule applicability by accident.

**One exception, approved by the admin on 2026-10-07 (#992): the wall layout two readers agreed on.**
The slot reader asks Qwen3-VL and Kimi K3 about each countertop row's walls and seals a layout only
when both, of different makers, say the same and code's hatch check does not object
(`extraction/slot_reader/walls.py`). When the reviewer has not stated `wall_config`, the check
request may use that sealed layout — and records that it came from the readers. It may only when
all of these hold, each of which can only withhold it:

- the newest slot-reader run of the revision asked about walls, and **every** row it asked about
  sealed the **same** layout (one row for the person, or two that disagree, and there is none);
- a layout proposal with the readers' agreement prompt id and that value belongs to that run;
- no `wall_config` proposal of any source, as new or newer, says otherwise.

A single model's proposal, one from an older run, or one a newer proposal contradicts is never used.
A value the reviewer states always wins.

Source: issues #655, #992. Verification: tests/app/test_layout_proposals.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import PackageRevisionDocument
from app.models.evidence import (
    EvidenceArtifact,
    LayoutConfirmation,
    LayoutProposal,
    ObservationCandidate,
)
from app.models.runs import ExtractionRun
from rules.semantic_types import SemanticType

__all__ = [
    "DISCRIMINATOR_FROM_READERS",
    "DISCRIMINATOR_FROM_REVIEWER",
    "READER_AGREEMENT_PROMPT_IDS",
    "WALLS_HELD_FLAG",
    "WALLS_SEALED_FLAG",
    "WALL_CANDIDATE_TEXT",
    "WALL_READER_FLAG",
    "ReaderSealedLayout",
    "confirmed_discriminators",
    "discriminator_note",
    "reader_sealed_wall_config",
    "record_layout_confirmation",
    "record_layout_proposal",
    "stored_layout_proposals",
]

#: The prompt ids whose layout proposals are two readers' agreement, not one model's answer:
#: `extraction/slot_reader/walls.py:WALL_PROMPT_ID` (not imported: the API must not reach
#: extraction code; `tests/extraction/slot_reader/test_walls.py` keeps the two the same).
READER_AGREEMENT_PROMPT_IDS: Final = frozenset({"slot-walls-v1"})
#: How the slot reader marks each row's wall candidate, and its outcome.
WALL_READER_FLAG: Final = "wall-reader"
WALLS_SEALED_FLAG: Final = "walls-sealed:"
WALLS_HELD_FLAG: Final = "walls-held:"
WALL_CANDIDATE_TEXT: Final = "walls: "
WALL_CONFIG: Final = SemanticType.WALL_CONFIG.value
#: Where a discriminator in a check request came from, as its payload records it (#992).
DISCRIMINATOR_FROM_REVIEWER: Final = "reviewer"
DISCRIMINATOR_FROM_READERS: Final = "two AI readers agreed:"


def discriminator_note(name: str, value: str, source: str | None) -> str | None:
    """The sentence a finding carries when its layout came from the readers, not a person."""
    if source is None or not source.startswith(DISCRIMINATOR_FROM_READERS):
        return None
    return (
        f"{name.replace('_', ' ').capitalize()} {value} was read off the drawing by two AI "
        "readers who agreed; no reviewer chose it. Check it before signing off."
    )


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


@dataclass(frozen=True, slots=True)
class ReaderSealedLayout:
    """A wall layout two readers agreed on, usable by a check the reviewer stated none for."""

    value: str
    proposal_id: UUID
    model_id: str
    prompt_id: str
    extraction_run_id: UUID


def reader_sealed_wall_config(
    session: Session, package_revision_id: UUID
) -> ReaderSealedLayout | None:
    """The readers' sealed `wall_config` for this revision, or `None` — see the module docstring.

    Every condition only withholds: no wall run, a row of the newest run that did not seal, two
    layouts, no agreement proposal from that run, or a proposal at least as new that says
    otherwise.
    """
    versions = select(PackageRevisionDocument.document_version_id).where(
        PackageRevisionDocument.package_revision_id == package_revision_id
    )
    rows = session.execute(
        select(ObservationCandidate, ExtractionRun.created_at)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .where(
            ObservationCandidate.document_version_id.in_(versions),
            ObservationCandidate.raw_text.startswith(WALL_CANDIDATE_TEXT),
        )
    ).all()
    walls = [(candidate, created) for candidate, created in rows if _is_wall(candidate)]
    if not walls:
        return None
    newest_run = max(walls, key=lambda item: (item[1], str(item[0].extraction_run_id)))[0]
    run_id = newest_run.extraction_run_id
    layouts: set[str | None] = set()
    for candidate, _created in walls:
        if candidate.extraction_run_id != run_id:
            continue
        sealed = [
            flag.removeprefix(WALLS_SEALED_FLAG)
            for flag in candidate.ambiguity_flags
            if flag.startswith(WALLS_SEALED_FLAG)
        ]
        layouts.add(sealed[0] if len(sealed) == 1 and sealed[0] else None)
    if len(layouts) != 1:
        return None
    (layout,) = layouts
    if layout is None:
        return None
    agreed = session.execute(
        select(LayoutProposal)
        .join(EvidenceArtifact, EvidenceArtifact.id == LayoutProposal.crop_artifact_id)
        .join(ObservationCandidate, ObservationCandidate.id == EvidenceArtifact.candidate_id)
        .where(
            LayoutProposal.package_revision_id == package_revision_id,
            LayoutProposal.discriminator_name == WALL_CONFIG,
            LayoutProposal.proposed_value == layout,
            LayoutProposal.prompt_id.in_(READER_AGREEMENT_PROMPT_IDS),
            ObservationCandidate.extraction_run_id == run_id,
        )
        .order_by(LayoutProposal.created_at.desc(), LayoutProposal.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if agreed is None:
        return None
    contradicted = session.execute(
        select(LayoutProposal.id)
        .where(
            LayoutProposal.package_revision_id == package_revision_id,
            LayoutProposal.discriminator_name == WALL_CONFIG,
            LayoutProposal.created_at >= agreed.created_at,
            LayoutProposal.proposed_value != layout,
        )
        .limit(1)
    ).scalar_one_or_none()
    if contradicted is not None:
        # A proposal as new as the readers' (one transaction shares one clock) or newer says
        # something else: which is current cannot be told, so neither is used.
        return None
    return ReaderSealedLayout(
        value=layout,
        proposal_id=agreed.id,
        model_id=agreed.model_id,
        prompt_id=agreed.prompt_id,
        extraction_run_id=run_id,
    )


def _is_wall(candidate: ObservationCandidate) -> bool:
    flags = candidate.ambiguity_flags or []
    return WALL_READER_FLAG in flags
