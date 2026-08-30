"""Retrieve the sealed calculation and evidence chain for one finding (#223, D1.2).

The response contains the exact rule snapshot, every persisted operand, the parameter-set
versions, the engine trace and the evidence location used by each drawing operand. Exact integer
parts are rendered as decimal strings: JSON numbers are exact on the wire, but common clients turn
them into binary floating-point values and can silently change a large numerator.

This endpoint explains and reproduces an existing verdict. It does not execute a rule, select
evidence or issue storage capabilities; those responsibilities remain in their owning layers.

Source: backend proposal section 10.2; Design: ``docs/DESIGN_PRODUCT.md`` section 3.1;
Verification: ``tests/api/test_finding_chain.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.auth import Principal, require_project_access
from app.models import (
    CanonicalObservation,
    CheckRun,
    Finding,
    Package,
    PackageRevision,
    Page,
    RuleDefinition,
    RuleSnapshot,
    VerdictInput,
)

router = APIRouter(tags=["findings"])
NOT_FOUND_DETAIL = "Not found"


class EvidenceLocation(BaseModel):
    """The immutable drawing location behind one operand."""

    model_config = ConfigDict(frozen=True)

    canonical_observation_id: UUID
    document_version_id: UUID
    page_id: UUID
    page_index: int
    polygon: list[list[str]]
    coordinate_space: str
    crop_uri: str | None
    document_role: str
    semantic_type: str
    authority: str


class ExactOperand(BaseModel):
    """One exact operand, with evidence when it came from a drawing."""

    model_config = ConfigDict(frozen=True)

    name: str
    numerator: str
    denominator: str
    unit: str
    evidence_status: str
    evidence: EvidenceLocation | None


class RuleSnapshotRecord(BaseModel):
    """The complete immutable rule content used by the check run."""

    model_config = ConfigDict(frozen=True)

    database_id: UUID
    snapshot_id: str
    rule_id: str
    version: str
    canonical_json: str
    product_type: str
    check_type: str


class TracedOperandOut(BaseModel):
    """One input to the calculation, as the engine recorded it."""

    model_config = ConfigDict(frozen=True)

    name: str
    value: str
    """Rendered as text rather than a number. The engine's operand values are exact rationals and
    measurements, and a JSON number would be turned into binary floating point by most clients —
    which under exact match is not a rounding error but a different verdict."""

    source: str
    evidence_ref: str | None = None


class CalculationTraceOut(BaseModel):
    """The arithmetic, when arithmetic ran. Mirrors `verdict.trace.CalculationTrace`."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["calculation"] = "calculation"
    operation: str
    operands: tuple[TracedOperandOut, ...] = ()
    intermediates: tuple[tuple[str, str], ...] = ()
    comparison: str = ""
    tolerance: str | None = None
    """Always absent in V1. Raj settled on exact match with no band, so there is no tolerance to
    record — the reviewer clearing a flag is the tolerance. Kept because graded tolerances are
    deferred past iteration 1 rather than ruled out."""

    arithmetic_unit: str | None = None
    outcome: str = ""
    engine_version: str = ""
    operation_version: str = ""


class AbstentionTraceOut(BaseModel):
    """Why nothing was calculated.

    A real and separate shape, not a degenerate calculation: `app/budget/overflow.py` writes one when
    a package exhausts its model budget, and it carries a cause and a reason rather than operands.
    Reporting it as an empty calculation would render an abstention as a check that ran and found
    nothing, which is the reading V1 must never invite.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["abstention"] = "abstention"
    cause: str
    reason: str = ""
    regions_done: int | None = None
    review_complete: bool | None = None


class OpaqueTraceOut(BaseModel):
    """A trace this API does not recognise, handed over intact.

    **Not an error and not a silent empty object.** A recompute is compared against what the engine
    recorded, so dropping a shape nobody has taught this endpoint about would lose the only copy. A
    reader gets the content and is told plainly that it was not understood.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["unrecognised"] = "unrecognised"
    content: dict[str, Any]


#: What the wire carries. Discriminated on `kind`, which this API adds when it reads the row — the
#: stored JSON has no tag of its own, and classifying on read means no migration and no rewriting of
#: rows that already exist.
TraceOut = Annotated[
    CalculationTraceOut | AbstentionTraceOut | OpaqueTraceOut,
    Field(discriminator="kind"),
]


def classify_trace(stored: Mapping[str, Any]) -> TraceOut:
    """Decide which shape a stored trace is, without guessing.

    Recognised by the field that only one shape has: a calculation names an `operation`, an
    abstention names a `cause`. Anything else is handed over as-is rather than coerced into whichever
    model is closest — a trace bent to fit would be read as the engine's own record of the
    calculation, and it would not be.
    """
    if "operation" in stored:
        return CalculationTraceOut(
            operation=str(stored.get("operation", "")),
            operands=tuple(
                TracedOperandOut(
                    name=str(operand.get("name", "")),
                    value=str(operand.get("value", "")),
                    source=str(operand.get("source", "")),
                    evidence_ref=(
                        str(operand["evidence_ref"])
                        if operand.get("evidence_ref") is not None
                        else None
                    ),
                )
                for operand in stored.get("operands", ())
                if isinstance(operand, Mapping)
            ),
            intermediates=tuple(
                (str(name), str(value)) for name, value in stored.get("intermediates", ()) or ()
            ),
            comparison=str(stored.get("comparison", "")),
            tolerance=None if stored.get("tolerance") is None else str(stored["tolerance"]),
            arithmetic_unit=(
                None if stored.get("arithmetic_unit") is None else str(stored["arithmetic_unit"])
            ),
            outcome=str(stored.get("outcome", "")),
            engine_version=str(stored.get("engine_version", "")),
            operation_version=str(stored.get("operation_version", "")),
        )

    if "cause" in stored:
        return AbstentionTraceOut(
            cause=str(stored["cause"]),
            reason=str(stored.get("reason", "")),
            regions_done=stored.get("regions_done"),
            review_complete=stored.get("review_complete"),
        )

    return OpaqueTraceOut(content=dict(stored))


class FindingChain(BaseModel):
    """Everything persisted to explain and recompute one finding."""

    model_config = ConfigDict(frozen=True)

    finding_id: UUID
    outcome: str
    severity: str
    rule_snapshot: RuleSnapshotRecord
    parameter_versions: dict[str, str]
    operands: tuple[ExactOperand, ...]
    trace: TraceOut
    engine_version: str


@router.get(
    "/projects/{project_id}/packages/{package_id}/findings/{finding_id}/chain",
    response_model=FindingChain,
)
def finding_chain(
    project_id: UUID,
    package_id: UUID,
    finding_id: UUID,
    principal: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
) -> FindingChain:
    """Return the stored chain, while preserving the project isolation boundary."""

    del principal  # Access was established by the dependency; SQL establishes row ownership.
    row = session.execute(
        select(Finding, CheckRun, RuleSnapshot, RuleDefinition)
        .join(CheckRun, Finding.check_run_id == CheckRun.id)
        .join(RuleSnapshot, CheckRun.rule_snapshot_id == RuleSnapshot.id)
        .join(RuleDefinition, RuleSnapshot.rule_definition_id == RuleDefinition.id)
        .join(PackageRevision, Finding.package_revision_id == PackageRevision.id)
        .join(Package, PackageRevision.package_id == Package.id)
        .where(
            Finding.id == finding_id,
            Package.id == package_id,
            Package.project_id == project_id,
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    finding, run, snapshot, definition = row
    return build_chain(session, finding, run, snapshot, definition)


def build_chain(
    session: Session,
    finding: Finding,
    run: CheckRun,
    snapshot: RuleSnapshot,
    definition: RuleDefinition,
    operand_rows: (
        Sequence[tuple[VerdictInput, CanonicalObservation | None, Page | None]] | None
    ) = None,
) -> FindingChain:
    """Assemble one finding's chain from rows the caller has already resolved.

    Extracted for `app/api/finding_export.py` (#224), which needs the same assembly for many findings.
    Copying it would have been forty lines of duplicated provenance logic, and two copies of "how a finding
    explains itself" is two answers waiting to differ — the export could keep rendering an operand shape
    this endpoint had stopped using and nothing would notice.

    Takes resolved rows rather than ids, so each caller keeps its own project-isolation query: the export
    must not inherit a narrower or wider access boundary by accident.

    `operand_rows` lets a caller supply rows it has already fetched. The export needs that: calling this
    once per finding meant one operand query per finding, so an export of N findings cost N+1 round trips
    on the endpoint reports and spreadsheets poll. Passing pre-grouped rows keeps one assembly
    implementation while removing the per-finding query — the alternative was a second assembly, which is
    the duplication this function was extracted to avoid.
    """
    if operand_rows is not None:
        return _assemble(finding, run, snapshot, definition, operand_rows)

    fetched = session.execute(
        select(VerdictInput, CanonicalObservation, Page)
        .outerjoin(
            CanonicalObservation,
            VerdictInput.canonical_observation_id == CanonicalObservation.id,
        )
        .outerjoin(Page, CanonicalObservation.page_id == Page.id)
        .where(VerdictInput.check_run_id == run.id)
        .order_by(VerdictInput.operand_name, VerdictInput.id)
    ).all()

    return _assemble(
        finding,
        run,
        snapshot,
        definition,
        [(row[0], row[1], row[2]) for row in fetched],
    )


def _assemble(
    finding: Finding,
    run: CheckRun,
    snapshot: RuleSnapshot,
    definition: RuleDefinition,
    operand_rows: Sequence[tuple[VerdictInput, CanonicalObservation | None, Page | None]],
) -> FindingChain:
    """Render the chain from rows, whoever fetched them. One place, so the two paths cannot diverge."""
    operands = tuple(
        _operand_record(verdict_input, observation, page)
        for verdict_input, observation, page in operand_rows
    )
    return FindingChain(
        finding_id=finding.id,
        outcome=finding.outcome,
        severity=finding.severity,
        rule_snapshot=RuleSnapshotRecord(
            database_id=snapshot.id,
            snapshot_id=snapshot.snapshot_id,
            rule_id=definition.rule_id,
            version=snapshot.version,
            canonical_json=snapshot.canonical_json,
            product_type=snapshot.product_type,
            check_type=snapshot.check_type,
        ),
        parameter_versions=dict(finding.parameter_set_versions),
        operands=operands,
        trace=classify_trace(finding.trace),
        engine_version=run.engine_version,
    )


def _operand_record(
    verdict_input: VerdictInput,
    observation: CanonicalObservation | None,
    page: Page | None,
) -> ExactOperand:
    """Render one operand without manufacturing provenance for literals or user input."""

    evidence: EvidenceLocation | None = None
    if observation is not None:
        if (
            page is None
        ):  # A foreign key should make this impossible; fail loudly if storage drifted.
            raise RuntimeError(f"observation {observation.id} has no page")
        evidence = EvidenceLocation(
            canonical_observation_id=observation.id,
            document_version_id=observation.document_version_id,
            page_id=page.id,
            page_index=page.index,
            polygon=observation.polygon,
            coordinate_space=observation.coordinate_space,
            crop_uri=observation.evidence_crop_uri,
            document_role=observation.document_role,
            semantic_type=observation.semantic_type,
            authority=observation.authority,
        )
    return ExactOperand(
        name=verdict_input.operand_name,
        numerator=str(verdict_input.value_numerator),
        denominator=str(verdict_input.value_denominator),
        unit=verdict_input.unit,
        evidence_status=verdict_input.evidence_status,
        evidence=evidence,
    )


__all__ = ["FindingChain", "get_session", "router"]
