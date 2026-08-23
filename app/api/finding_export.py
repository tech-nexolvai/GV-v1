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

import logging
from collections import Counter
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.finding_chain import FindingChain, build_chain
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
from app.telemetry.tracing import current_trace_id
from verdict.outcomes import Outcome, is_decision

router = APIRouter(tags=["findings"])

#: Stdlib logging, following the one existing precedent in this codebase (`gv.auth`), now carrying the
#: trace id from `app/telemetry/tracing.py`.
#:
#: The earlier version of this comment deferred the whole question to #259 F2.1 and emitted no trace id at
#: all, which was the right call only while there was nothing to defer *to*. There is now: the shared
#: helper landed with this change, so this event can be found next to the request that produced it without
#: a second convention being invented here.
logger = logging.getLogger("gv.api.finding_export")

#: The only schema version this module emits. A literal rather than a free string: a consumer pinning "1"
#: must fail on a change, and a version field that could hold anything is a version field nobody trusts.
SCHEMA_VERSION: Literal["1"] = "1"

NOT_FOUND_DETAIL = "Not found"

#: The most findings one export will return.
#:
#: A synchronous endpoint with no bound ties request duration to package size, and this is the endpoint
#: reports and spreadsheets poll. Chosen rather than measured — there is no real package to measure yet —
#: so it is a documented maximum a reader can find and argue with, not a silent behaviour. Exceeding it
#: refuses; see the handler for why refusing beats truncating.
#:
#: **This sentence was untrue when I first wrote it**, which review caught: the handler fetched every row
#: and compared afterwards, so an oversized package was fully transferred and materialised before being
#: refused. The limit bounded the response and not the request, so the cost it claims to prevent was still
#: paid in full. The count now runs first — the cap is only a bound if nothing large happens before it.
MAX_FINDINGS = 5000


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


def _summarise(classified: list[tuple[str, bool]]) -> ExportSummary:
    """Count already-classified outcomes: each `(outcome, decided)` pair as the classifier judged it.

    **This function no longer decides anything, and that is the point.** It counts. The previous version
    called the classifier a second time — one answer per finding in the handler, another per distinct value
    here. Two callers agreeing is weaker than one caller: it made the classification a thing that happened
    twice, so an unrecognised outcome was reported as news once per finding *and* again from the summary.
    Taking the verdicts as an argument means there is exactly one place that judges.
    """
    counts = Counter(outcome for outcome, _ in classified)
    decisions = sum(1 for _, decided in classified if decided)
    return ExportSummary(
        findings=len(classified),
        decisions=decisions,
        abstentions=len(classified) - decisions,
        by_outcome=dict(sorted(counts.items())),
    )


def _operands_by_run(
    session: Session, run_ids: list[UUID]
) -> dict[UUID, list[tuple[VerdictInput, CanonicalObservation | None, Page | None]]]:
    """Every operand for every run in the export, in one query, grouped by run.

    Ordered by the same keys `build_chain` uses when it fetches its own, so a finding exported here and the
    same finding fetched from the chain endpoint present their operands identically. A different order
    would be a difference a consumer could see between two endpoints that claim to show the same thing.
    """
    if not run_ids:
        return {}
    rows = session.execute(
        select(VerdictInput, CanonicalObservation, Page)
        .outerjoin(
            CanonicalObservation,
            VerdictInput.canonical_observation_id == CanonicalObservation.id,
        )
        .outerjoin(Page, CanonicalObservation.page_id == Page.id)
        .where(VerdictInput.check_run_id.in_(run_ids))
        .order_by(VerdictInput.check_run_id, VerdictInput.operand_name, VerdictInput.id)
    ).all()

    grouped: dict[UUID, list[tuple[VerdictInput, CanonicalObservation | None, Page | None]]] = {}
    for row in rows:
        grouped.setdefault(row[0].check_run_id, []).append((row[0], row[1], row[2]))
    return grouped


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

    # **Counted before anything is loaded, because the cap has to bound the work and not just the
    # response.** The first version fetched every row and *then* compared `len(rows)` — so an oversized
    # package was transferred out of the database and materialised into ORM objects in full, and only then
    # refused. That bounds what we send while leaving the expensive part unbounded, which is the opposite of
    # what the limit is for, and `MAX_FINDINGS` claimed otherwise in its own docstring.
    #
    # The same join chain as the fetch below rather than the shorter one the filter would allow: the inner
    # joins cannot drop rows today because every key in the chain is non-nullable, but counting over exactly
    # what we are about to select means the two can never disagree about how many rows that is.
    counted = (
        select(func.count())
        .select_from(Finding)
        .join(CheckRun, Finding.check_run_id == CheckRun.id)
        .join(RuleSnapshot, CheckRun.rule_snapshot_id == RuleSnapshot.id)
        .join(RuleDefinition, RuleSnapshot.rule_definition_id == RuleDefinition.id)
        .join(PackageRevision, Finding.package_revision_id == PackageRevision.id)
        .join(Package, PackageRevision.package_id == Package.id)
        .where(Package.id == package_id, Package.project_id == project_id)
    )
    total = session.execute(counted).scalar_one()

    if total > MAX_FINDINGS:
        # **Refused, not truncated.** A silently shortened export is a consumer confidently reporting on
        # findings it never saw, which is the whole failure this endpoint's labelling exists to prevent.
        # The paginated list endpoint exists for packages this large.
        raise HTTPException(
            # `CONTENT_TOO_LARGE`, not the deprecated `REQUEST_ENTITY_TOO_LARGE` spelling — same 413, and
            # the old name emits a DeprecationWarning that would eventually fail a `-W error` run.
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"this package has {total} findings and the export is capped at {MAX_FINDINGS}. "
                "Nothing is returned rather than a partial export, because a truncated export cannot be "
                "told apart from a complete one. Page through GET .../findings instead."
            ),
        )

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

    # **One operand query for the whole export, not one per finding.** Calling `build_chain` without this
    # made an export of N findings cost N+1 round trips, on the endpoint reports and spreadsheets poll.
    operands = _operands_by_run(session, [run.id for _, run, _, _ in rows])

    # **Judged once per finding, then reused for both the flag and the count.** The per-finding `abstained`
    # and the envelope's `decisions` are the same question asked about the same row, so asking twice bought
    # nothing and cost an unrecognised outcome one log line per finding plus one more from the summary.
    classified = [
        (
            str(finding.outcome),
            _classify(
                str(finding.outcome),
                finding_id=finding.id,
                project_id=project_id,
                package_id=package_id,
            ),
        )
        for finding, _, _, _ in rows
    ]

    exported = tuple(
        ExportedFinding(
            chain=build_chain(
                session,
                finding,
                run,
                snapshot,
                definition,
                operand_rows=operands.get(run.id, ()),
            ),
            abstained=not decided,
        )
        # `strict=True`: the two lists are built from `rows` in the same order, and a silent zip truncation
        # would pair a finding with another finding's verdict — mislabelling rather than failing.
        for (finding, run, snapshot, definition), (_, decided) in zip(rows, classified, strict=True)
    )
    return FindingExportV1(
        schema_version=SCHEMA_VERSION,
        project_id=project_id,
        package_id=package_id,
        summary=_summarise(classified),
        findings=exported,
    )


def _classify(
    outcome: str,
    *,
    finding_id: UUID,
    project_id: UUID,
    package_id: UUID,
) -> bool:
    """Whether this stored outcome is a decision, treating anything unrecognised as an abstention.

    The column is a string, so a value outside the enum is possible in a way the enum alone would not
    suggest. Reporting such a row as a decision would be claiming the engine reached a verdict it may not
    have, so the unknown case falls the other way.

    **The ids are required arguments because a warning without them cannot be acted on.** "some outcome was
    unrecognised" tells an operator that a row somewhere has left the engine's vocabulary and gives them no
    way to find it; naming the finding turns the log line into something a person can go and look at. They
    are not optional, so no call site can produce the unactionable version.
    """
    try:
        return is_decision(Outcome(outcome))
    except ValueError:
        # **Safe, and now also visible — once.** Falling to "abstained" is the right direction: a value this
        # code cannot interpret must never be reported as a verdict the engine reached. But absorbing it in
        # silence means persisted data has left the engine's vocabulary and nobody finds out, because
        # `by_outcome` just grows a key nobody is watching. §2.2 again — silence must not read as completion.
        #
        # **The trace id comes from the shared helper, not from a convention invented here.** The event is
        # only useful next to the request that produced it, and `app/telemetry/tracing.py` is what makes
        # "the same trace id" mean the same thing in both places. `None` when there is no active trace —
        # this function is also called from unit tests and from anything outside a request — and the log
        # says so rather than printing a zero id that looks lookup-able.
        logger.warning(
            "finding %s (project %s, package %s) has outcome %r, which is outside the Outcome "
            "vocabulary; counted as an abstention [trace %s]",
            finding_id,
            project_id,
            package_id,
            outcome,
            current_trace_id() or "none",
            extra={
                "finding_id": str(finding_id),
                "project_id": str(project_id),
                "package_id": str(package_id),
                "outcome": outcome,
                "trace_id": current_trace_id(),
            },
        )
        return False
