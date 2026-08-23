"""A versioned export of a package's findings, in which an abstention cannot pass for an all-clear
(#224, D1.3).

`docs/DESIGN_PRODUCT.md` §3.1: reports, spreadsheets and any eventual front end need the same data in a
stable shape. Two things make that shape safe rather than merely stable.

**The version is explicit.** `schema_version` is a literal, so a consumer that pins `"1"` breaks loudly the
day the shape changes instead of silently reading a moved field as absent.

**Abstentions are labelled, and counted.** Only `PASS` and `FAIL` are decisions; `NOT_FOUND`,
`REVIEW_REQUIRED` and `NO_APPLICABLE_RULE` are the system declining to decide. A spreadsheet that filters
`outcome == "FAIL"`, finds none, and reports "no problems" would be reading abstention as approval — the
failure `AGENTS.md` §2.2 exists to prevent. So each entry carries `abstained`, and the envelope carries a
count, which is the part a consumer cannot filter away without seeing it.

**The split is not restated here.** `verdict/outcomes.py` owns `ABSTAINING_OUTCOMES` and `is_decision`,
because the false-PASS metric and automation coverage need the same split; a second definition in the
export layer is how the two start disagreeing about what counted as decided.

**No drawing bytes, ever.** Every drawing reference is an id, a polygon or a URI — `AGENTS.md` §6. The
chain models this reuses were already built that way, and `tests/api/test_finding_export.py` walks the
serialised payload to check rather than trusting that they still are.

Source: backend proposal §10.2 · Design: `docs/DESIGN_PRODUCT.md` §3.1 ·
Verification: `tests/api/test_finding_export.py`
"""

from __future__ import annotations

from collections import Counter
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.finding_chain import FindingChain, build_chain
from app.auth import Principal, require_project_access
from app.models import (
    CheckRun,
    Finding,
    Package,
    PackageRevision,
    RuleDefinition,
    RuleSnapshot,
)
from verdict.outcomes import Outcome, is_decision

router = APIRouter(tags=["findings"])

#: The only schema version this module emits. A literal rather than a free string: a consumer pinning "1"
#: must fail on a change, and a version field that could hold anything is a version field nobody trusts.
SCHEMA_VERSION: Literal["1"] = "1"

NOT_FOUND_DETAIL = "Not found"


class ExportedFinding(BaseModel):
    """One finding's full chain, with whether the engine actually decided anything."""

    model_config = ConfigDict(frozen=True)

    chain: FindingChain

    abstained: bool
    """True when the outcome is an abstention rather than a decision.

    **Duplicated from `outcome` on purpose, and it is not redundancy.** A consumer reading the raw string
    has to know that `NOT_FOUND` is not a pass, and the ones that get this wrong are exactly the ones that
    matter: a spreadsheet, a summary email, an early front end. Derived from `verdict/outcomes.py` so it
    cannot drift from the definition the release metrics use.
    """


class ExportSummary(BaseModel):
    """Counts, so a consumer that ignores every flag still cannot report a clean package.

    The per-finding label can be filtered out of a view. A total sitting at the top of the payload is
    harder to lose, and "42 findings, 39 of which abstained" is a sentence that stops somebody saying the
    drawings passed.
    """

    model_config = ConfigDict(frozen=True)

    findings: int
    decisions: int
    abstentions: int
    by_outcome: dict[str, int]


class FindingExportV1(BaseModel):
    """The export envelope. Version first, so a consumer can check before it parses."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1"]
    project_id: UUID
    package_id: UUID
    summary: ExportSummary
    findings: tuple[ExportedFinding, ...]


def _summarise(outcomes: list[str]) -> ExportSummary:
    """Count the outcomes, splitting decisions from abstentions by the engine's own definition.

    An unrecognised outcome string counts as an abstention rather than a decision. That is the safe
    direction: a value this code does not understand must not be reported as a verdict the engine reached.
    """
    counts = Counter(outcomes)
    decisions = 0
    for value, count in counts.items():
        try:
            outcome = Outcome(value)
        except ValueError:
            continue  # Unknown: left out of the decision count, so it lands in abstentions.
        if is_decision(outcome):
            decisions += count
    return ExportSummary(
        findings=len(outcomes),
        decisions=decisions,
        abstentions=len(outcomes) - decisions,
        by_outcome=dict(sorted(counts.items())),
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/findings/export",
    response_model=FindingExportV1,
    summary="Export a package's findings in a versioned shape",
)
def export_findings(
    principal: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> FindingExportV1:
    """Every finding for one package, with its chain, labelled and counted.

    The project boundary is established by the dependency and then again in SQL, the same belt-and-braces
    `finding_chain` uses: the dependency proves the caller may see the project, and the join proves these
    rows belong to it.

    A package with no findings is an empty export, not a 404 — but it is only reached for a package that
    exists in this project, so "nothing found" cannot be confused with "no such package".
    """
    del principal  # Access established by the dependency; SQL establishes row ownership.

    exists = session.execute(
        select(Package.id).where(Package.id == package_id, Package.project_id == project_id)
    ).one_or_none()
    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    rows = session.execute(
        select(Finding, CheckRun, RuleSnapshot, RuleDefinition)
        .join(CheckRun, Finding.check_run_id == CheckRun.id)
        .join(RuleSnapshot, CheckRun.rule_snapshot_id == RuleSnapshot.id)
        .join(RuleDefinition, RuleSnapshot.rule_definition_id == RuleDefinition.id)
        .join(PackageRevision, Finding.package_revision_id == PackageRevision.id)
        .join(Package, PackageRevision.package_id == Package.id)
        .where(Package.id == package_id, Package.project_id == project_id)
        # Ordered so two exports of unchanged data are byte-identical: a consumer diffing yesterday's
        # export against today's should see only what actually changed.
        .order_by(PackageRevision.revision_number, Finding.created_at, Finding.id)
    ).all()

    exported = tuple(
        ExportedFinding(
            chain=build_chain(session, finding, run, snapshot, definition),
            abstained=not _decided(finding.outcome),
        )
        for finding, run, snapshot, definition in rows
    )
    return FindingExportV1(
        schema_version=SCHEMA_VERSION,
        project_id=project_id,
        package_id=package_id,
        summary=_summarise([str(finding.outcome) for finding, _, _, _ in rows]),
        findings=exported,
    )


def _decided(outcome: str) -> bool:
    """Whether this stored outcome is a decision, treating anything unrecognised as an abstention.

    The column is a string, so a value outside the enum is possible in a way the enum alone would not
    suggest. Reporting such a row as a decision would be claiming the engine reached a verdict it may not
    have, so the unknown case falls the other way.
    """
    try:
        return is_decision(Outcome(outcome))
    except ValueError:
        return False
