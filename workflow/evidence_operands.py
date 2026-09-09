"""Sealed evidence, as the operands a rule can be run against (#530).

The last link of the read-to-decide chain. `run_checks` has only ever taken operands from its caller,
which in practice meant a reviewer typing values into a form — so a drawing could be read, its
readings confirmed, and the rules would still be judged on numbers somebody re-entered by hand.

**Nothing here decides anything.** `evidence/gate.py:seal` decides whether an observation may enter a
verdict at all, and refuses everything that is not `CORROBORATED` or `HUMAN_CONFIRMED`. This module
finds the observations a rule asked for and hands each to the gate; a refusal means no operand, which
means the rule abstains, which is the correct outcome and not an error to route around.

**What connects a reading to an operand is the rulebook, not a convention.** A rule declares each
input's `source` and `semantic_type` — `CT-DEPTH-001` wants `SHOP` and `CT010` — and an observation
carries exactly those two facts. So the join is the rule's own declaration, and a rule that asked for
something nobody has confirmed finds nothing rather than something approximate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import DocumentVersion, PackageRevisionDocument, Page
from app.models.evidence import CanonicalObservation
from evidence.canonical import Authority, EvidenceStatus
from evidence.canonical import CanonicalObservation as DomainObservation
from evidence.coordinates import StoredPoint
from evidence.gate import GateRefusal, seal
from evidence.polygon import Polygon
from rules.schema import Cardinality, Rule
from rules.semantic_types import DocumentRole, SemanticType
from units.measurement import Measurement, Unit
from verdict.operands import VerdictOperand

__all__ = ["operands_from_evidence"]


def _domain(row: CanonicalObservation, page_index: int) -> DomainObservation | None:
    """One stored observation as the value type the gate takes, or `None` if it will not rebuild.

    `None` rather than a raise: a row that cannot be reconstituted is a fact about storage, and it
    must cost this rule its operand — which makes the rule abstain — rather than the whole run.
    """
    try:
        return DomainObservation(
            document_version_id=row.document_version_id,
            document_role=DocumentRole(row.document_role),
            page=page_index,
            polygon=Polygon(
                points=tuple(StoredPoint(x=Decimal(x), y=Decimal(y)) for x, y in row.polygon),
                space="stored",
                document_version_id=row.document_version_id,
                page=page_index,
            ),
            semantic_type=SemanticType(row.semantic_type),
            value=Measurement(
                exact=Fraction(row.value_numerator, row.value_denominator),
                unit=Unit(row.unit),
                raw_text=None,
            ),
            status=EvidenceStatus(row.status),
            authority=Authority(row.authority),
            supported_by=(str(row.id),),
            corroborated_by=(),
            conflicts_with=(),
            evidence_crop_uri=row.evidence_crop_uri,
        )
    except (ArithmeticError, TypeError, ValueError):
        return None


def operands_from_evidence(
    session: Session, package_revision_id: UUID, rules: Sequence[Rule]
) -> dict[str, dict[str, VerdictOperand]]:
    """Every rule input this revision has sealed evidence for, keyed by rule then input name.

    Ordered by the observation's own creation time, so a many-valued input arrives in the order the
    readings were confirmed and two runs over unchanged evidence produce the same tuple. That matters
    because `CAB-ARCH-VS-SHOP-001` compares two runs position by position: a set that reordered
    between runs would report a mismatch that is an artefact of the query.
    """
    rows = session.execute(
        select(CanonicalObservation, Page.index)
        .join(Page, Page.id == CanonicalObservation.page_id)
        .join(DocumentVersion, DocumentVersion.id == CanonicalObservation.document_version_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == DocumentVersion.id,
        )
        .where(PackageRevisionDocument.package_revision_id == package_revision_id)
        .order_by(CanonicalObservation.created_at, CanonicalObservation.id)
    ).all()

    # Grouped by what a rule asks for: the document's role and the quantity's semantic type.
    by_need: dict[tuple[str, str], list[tuple[DomainObservation, UUID]]] = {}
    for row, page_index in rows:
        observation = _domain(row, page_index)
        if observation is None:
            continue
        by_need.setdefault((row.document_role, row.semantic_type), []).append((observation, row.id))

    operands: dict[str, dict[str, VerdictOperand]] = {}
    for rule in rules:
        for name, selector in (rule.inputs or {}).items():
            source = getattr(selector.source, "value", str(selector.source))
            semantic = getattr(selector.semantic_type, "value", str(selector.semantic_type))
            found = by_need.get((source, semantic), [])
            if not found:
                continue

            if selector.cardinality is Cardinality.MANY:
                sealed = [
                    _seal(observation, observation_id, name)
                    for observation, observation_id in found
                ]
                if any(isinstance(item, GateRefusal) for item in sealed):
                    # One unqualified reading in a run makes the whole run unusable: a sum of the
                    # rest would be a smaller countertop, arrived at silently.
                    continue
                # Narrowed to `Measurement`, not cast: `VerdictOperand.value` is a union that
                # already includes a tuple, and a tuple of tuples is not a run of measurements. A
                # member that is not one costs the rule its operand, which makes it abstain.
                values = tuple(
                    operand.value
                    for operand in sealed
                    if isinstance(operand, VerdictOperand)
                    and isinstance(operand.value, Measurement)
                )
                if len(values) != len(sealed):
                    continue
                first = sealed[0]
                if not isinstance(first, VerdictOperand):
                    continue
                operand = VerdictOperand(
                    name=name,
                    value=values,
                    status=first.status,
                    source=first.source,
                    evidence_ref=first.evidence_ref,
                )
            else:
                if len(found) != 1:
                    # Two confirmed readings of a quantity a rule expects once. Which one governs is
                    # a question about the drawing, and answering it here — by position, by
                    # recency — would be inventing the answer.
                    continue
                observation, observation_id = found[0]
                single = _seal(observation, observation_id, name)
                if isinstance(single, GateRefusal):
                    continue
                operand = single

            operands.setdefault(rule.id, {})[name] = operand
    return operands


def _seal(
    observation: DomainObservation, observation_id: UUID, name: str
) -> VerdictOperand | GateRefusal:
    """Seal a reading and retain its immutable database identity for the verdict record.

    The evidence gate still makes the qualification decision.  This adapter only carries the
    identity of the already-selected canonical observation across the workflow boundary, so a
    later finding can point to the actual crop rather than re-identifying a drawing region.
    """
    sealed = seal(observation, name)
    if isinstance(sealed, GateRefusal):
        return sealed
    return replace(sealed, evidence_observation_id=str(observation_id))
