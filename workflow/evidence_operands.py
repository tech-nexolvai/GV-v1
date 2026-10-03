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

**Readings on different drawings are never summed into one check** (#826). A rule asking for the
cabinets *of the same assembly* as its countertop used to get every labelled cabinet in the package,
so labelling a second countertop's run made the first check's sum wrong — exactly, and confidently.
Until drawing items exist (#748), one drawing stands in for one assembly: a rule whose readings from
one side lie on more than one drawing gets none of them for those inputs, and the engine returns the
rule's own `on_ambiguous` with the reason. One drawing can still show two runs (a kitchenette's wall
and base cabinets); labelling both as cabinets would still sum them, which #168's resolver will fix.

**A run an operation compares member by member gets nothing from evidence** (#794, #833). No
reading carries its cabinet's identifier or its place in a confirmed run yet (#748), and a run in
the order readings were labelled, compared position by position, passed two swapped cabinets — in
the pairing check and again in the filler check. The form still supplies both runs, in the order
the reviewer states.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from types import MappingProxyType
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.sides import ReadingSides
from app.models.document import DocumentVersion, PackageRevisionDocument, Page
from app.models.evidence import (
    CanonicalObservation,
    EvidenceCorroborationLane,
    EvidenceSupportingCandidate,
)
from evidence.canonical import Authority, CorroborationLane, EvidenceStatus
from evidence.canonical import CanonicalObservation as DomainObservation
from evidence.coordinates import StoredPoint
from evidence.gate import GateRefusal, seal
from evidence.polygon import Polygon
from rules.schema import Cardinality, Rule, Scope
from rules.semantic_types import DocumentRole, SemanticType
from units.measurement import Measurement, Unit
from verdict.operands import VerdictOperand
from verdict.operations import POSITION_SENSITIVE_OPERATIONS

__all__ = [
    "DRAWINGS_SPANNED",
    "IDENTIFIER_PAIRING_WITHHELD",
    "RUN_ORDER_WITHHELD",
    "WITHHELD_FOR_ORDER",
    "EvidenceOperands",
    "evidence_operands",
    "operands_from_evidence",
    "position_sensitive_inputs",
]

#: The scopes that promise a rule its readings belong to one assembly (#826). `package` makes no
#: such promise and keeps the old behaviour.
_ONE_ASSEMBLY: Final = frozenset({Scope.SAME_ASSEMBLY, Scope.SAME_VIEW})

#: What a check says when one side's readings span drawings (#826), for the reviewer: why the
#: readings they labelled were not used, and the two ways to give the check one assembly.
DRAWINGS_SPANNED: Final = (
    "The labelled readings for this check are on {count} different drawings, so they may belong to "
    "different countertops. Label one countertop's run, or enter it on the form."
)

#: Which drawing a reading lies on: a drawing view, a page with no views (the page is the drawing),
#: or — for a reading on a page with views that no single view holds — the reading alone, so that it
#: can never be taken to share a drawing with anything.
_Drawing = tuple[str, UUID]

#: What a check that pairs by identifier says when it abstains for want of one (#794), for the
#: reviewer: why the readings they labelled were not used, and what will compare them today.
IDENTIFIER_PAIRING_WITHHELD: Final = (
    "This check pairs each cabinet with the one carrying the same tag, and no reading carries its "
    "cabinet's tag yet, so labelled readings are not paired by the order they were labelled. Enter "
    "both lists on the form, in the same order, to compare them."
)

#: What a check that compares two runs along the wall says when it abstains for want of their order
#: (#833). The sentence above is not true of it: the filler check pairs nothing by tag. It compares
#: each filler and cabinet with the one in the same place, so it is the order that is missing.
RUN_ORDER_WITHHELD: Final = (
    "This check compares the two runs filler by filler and cabinet by cabinet, in order along the "
    "wall, and the system does not yet know that order for labelled readings, so they are not "
    "compared by the order they were labelled. Enter both runs on the form, in wall order from "
    "left to right, to compare them."
)

#: Each position-sensitive operation's sentence for the reviewer, by operation. Every member of
#: `POSITION_SENSITIVE_OPERATIONS` has one, and a test holds the two to the same names.
WITHHELD_FOR_ORDER: Final[Mapping[str, str]] = MappingProxyType(
    {
        "cabinet_run_distribution": RUN_ORDER_WITHHELD,
        "pairwise_within_tolerance": IDENTIFIER_PAIRING_WITHHELD,
    }
)

#: The sources evidence can fill: the roles of the documents an observation is read from. A value a
#: reviewer types, or a literal, is never an observation, so evidence has nothing to withhold.
_READ_FROM_DOCUMENTS: Final = frozenset(role.value for role in DocumentRole)


def position_sensitive_inputs(rule: Rule) -> dict[str, str]:
    """The runs `rule` hands to an operation that compares them member by member, each with the
    sentence the reviewer reads when it is withheld (#794, #833).

    Read from the rule's own bindings — its final operation and every derivation — so a rule that
    compares through an intermediate is caught as surely as one that compares at the end.

    **Only a many-valued input read from a document.** A single reading has no order to get wrong,
    and a value the reviewer types — the site width, a cabinet's classification — never comes from
    evidence, so a sentence about labelled readings would not be true of it.
    """
    steps: list[tuple[str, tuple[str, ...]]] = [
        (rule.operation.type, tuple(rule.operation.operands.values()))
    ]
    for derivation in rule.derivations:
        steps.append(
            (
                derivation.operation,
                tuple(
                    name
                    for binding in derivation.operands.values()
                    for name in ((binding,) if isinstance(binding, str) else binding)
                ),
            )
        )
    runs = {
        name
        for name, selector in (rule.inputs or {}).items()
        if selector.cardinality is Cardinality.MANY
        and getattr(selector.source, "value", str(selector.source)) in _READ_FROM_DOCUMENTS
    }
    withheld: dict[str, str] = {}
    for operation, names in steps:
        if operation in POSITION_SENSITIVE_OPERATIONS:
            for name in names:
                if name in runs:
                    withheld.setdefault(name, WITHHELD_FOR_ORDER[operation])
    return withheld


def _domain(
    row: CanonicalObservation,
    page_index: int,
    *,
    supported_by: tuple[str, ...],
    corroborated_by: tuple[CorroborationLane, ...],
) -> DomainObservation | None:
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
            supported_by=supported_by,
            corroborated_by=corroborated_by,
            conflicts_with=(),
            evidence_crop_uri=row.evidence_crop_uri,
        )
    except (ArithmeticError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class EvidenceOperands:
    """What evidence gives each rule, and what it found but withheld as ambiguous (#826)."""

    operands: dict[str, dict[str, VerdictOperand]] = field(default_factory=dict)
    """Keyed by rule id, then input name."""
    ambiguous: dict[str, dict[str, str]] = field(default_factory=dict)
    """Inputs whose readings were found and not used, keyed by rule id then input name, with why."""


def operands_from_evidence(
    session: Session, package_revision_id: UUID, rules: Sequence[Rule]
) -> dict[str, dict[str, VerdictOperand]]:
    """Every rule input this revision has sealed evidence for, keyed by rule then input name.

    The operands of `evidence_operands`, for a caller that does not report ambiguity.
    """
    return evidence_operands(session, package_revision_id, rules).operands


def evidence_operands(
    session: Session, package_revision_id: UUID, rules: Sequence[Rule]
) -> EvidenceOperands:
    """Every rule input this revision has sealed evidence for, and every one withheld as ambiguous.

    Ordered by the observation's own creation time, so a many-valued input arrives in the order the
    readings were confirmed and two runs over unchanged evidence produce the same tuple. **That order
    says nothing about which reading is which**, so no input an operation compares member by member
    is given one (`position_sensitive_inputs`, #794, #833); a sum or a count is indifferent to it.
    """
    rows = session.execute(
        select(CanonicalObservation, Page)
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
    by_need: dict[tuple[str, str], list[tuple[DomainObservation, UUID, _Drawing]]] = {}
    sides = ReadingSides(session)
    for row, page in rows:
        page_index = page.index
        # Rebuild the evidence provenance rather than treating a database id as synthetic support.
        # In particular, the mechanical-tag lane has a numeric candidate plus its exact tag; without
        # both this adapter would quietly turn qualified evidence back into an abstention.
        supported_by = tuple(
            str(candidate_id)
            for candidate_id in session.scalars(
                select(EvidenceSupportingCandidate.candidate_id).where(
                    EvidenceSupportingCandidate.canonical_observation_id == row.id
                )
            ).all()
        )
        try:
            corroborated_by = tuple(
                CorroborationLane(lane)
                for lane in session.scalars(
                    select(EvidenceCorroborationLane.lane).where(
                        EvidenceCorroborationLane.canonical_observation_id == row.id
                    )
                ).all()
            )
        except ValueError:
            # A stored lane unknown to this code must cost the rule its operand.  Treating it as a
            # known qualifier would let a newer database declaration bypass this older gate.
            continue
        observation = _domain(
            row,
            page_index,
            supported_by=supported_by,
            corroborated_by=corroborated_by,
        )
        if observation is None:
            continue
        holding = sides.drawings_holding(page, observation.polygon)
        drawing: _Drawing = (
            ("page", page.id)
            if holding is None
            else ("view", holding[0].id) if len(holding) == 1 else ("unplaced", row.id)
        )
        by_need.setdefault((row.document_role, row.semantic_type), []).append(
            (observation, row.id, drawing)
        )

    operands: dict[str, dict[str, VerdictOperand]] = {}
    ambiguous: dict[str, dict[str, str]] = {}
    for rule in rules:
        ordered = position_sensitive_inputs(rule)
        withheld = _spanning(rule, ordered, by_need)
        if withheld:
            ambiguous[rule.id] = withheld
        for name, selector in (rule.inputs or {}).items():
            if name in ordered or name in withheld:
                continue
            source = getattr(selector.source, "value", str(selector.source))
            semantic = getattr(selector.semantic_type, "value", str(selector.semantic_type))
            found = [(o, i) for o, i, _drawing in by_need.get((source, semantic), [])]
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
    return EvidenceOperands(operands=operands, ambiguous=ambiguous)


def _spanning(
    rule: Rule,
    ordered: Mapping[str, str],
    by_need: dict[tuple[str, str], list[tuple[DomainObservation, UUID, _Drawing]]],
) -> dict[str, str]:
    """The one-assembly inputs whose side's readings lie on more than one drawing, with why (#826).

    **Per side**, because an architect-versus-vendor check reads two drawings by design: what must
    be one drawing is each side's readings, not all of them. **Every** such input of that side, not
    only the one that spans: a countertop from one drawing judged against cabinets from two is the
    same mixture, and dropping just the cabinets would leave a countertop with nothing to equal.
    """
    scoped: dict[str, str] = {}
    drawings: dict[str, set[_Drawing]] = {}
    for name, selector in (rule.inputs or {}).items():
        if name in ordered or selector.scope not in _ONE_ASSEMBLY:
            continue
        source = getattr(selector.source, "value", str(selector.source))
        semantic = getattr(selector.semantic_type, "value", str(selector.semantic_type))
        scoped[name] = source
        for _observation, _id, drawing in by_need.get((source, semantic), []):
            drawings.setdefault(source, set()).add(drawing)
    return {
        name: DRAWINGS_SPANNED.format(count=len(drawings[source]))
        for name, source in scoped.items()
        if len(drawings.get(source, ())) > 1
    }


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
