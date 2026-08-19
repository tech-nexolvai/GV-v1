"""Reading what the engine decided: list and filter a package's findings (#222, D1.1).

This is how a reviewer, a redline generator and a spreadsheet export all get at the verdict plane —
`docs/DESIGN_PRODUCT.md` §3.1 makes them the same consumer, so there is one query rather than one per
audience.

Four things this module is built to get right.

**Abstentions are in the list by default.** `NOT_FOUND`, `REVIEW_REQUIRED` and `NO_APPLICABLE_RULE`
are results. Leaving them out of the default would let a package look clean because the reviewer
never saw what the system declined to judge — the failure `docs/DESIGN_PRODUCT.md` §3.2 exists to
prevent. Narrowing to a subset takes an explicit `outcome=` filter, so it is always somebody's
decision rather than a default nobody noticed.

**Paging is keyset, not offset.** `OFFSET 20` means "skip the first twenty rows *of the query you are
running now*". A finding inserted ahead of the boundary between page one and page two shifts every
later row down by one, and the reviewer never sees the row that moved across the boundary. A keyset
cursor names the last row rather than counting rows, so an insert cannot move it. Findings are
`Immutable`, so a row's sort key never changes either — the two together are what make "a reviewer
never skips a finding by paging" a property rather than a hope.

**The order is total, and it is documented.** Severity, then outcome, then `created_at`, then `id`.
The `id` is what makes it total: two findings written in the same microsecond would otherwise tie,
and a tie is a boundary that can fall in two different places on two different requests. The order is
described in `app/schemas/findings.py` and repeated in every response.

**Project scope is an isolation boundary, not a filter.** The route carries
`require_project_access`, and the SQL filters on the project as well — the dependency establishes
that the caller may see this project, and the `WHERE` clause establishes that these rows are this
project's. A package that is not in this project is `404`, exactly as a project the caller does not
belong to is: `403` would confirm it exists, which is what the boundary is for.

`app/main.py` includes this router under `API_PREFIX`, alongside the packages and documents routers
added by #205.

Source: backend proposal §10.2 Findings · Design: `docs/DESIGN_PRODUCT.md` §3.1 ·
Verification: `tests/api/test_findings_query.py`
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import ColumnElement, Integer, Select, and_, case, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.api.dependencies import get_session
from app.auth import Principal, require_project_access
from app.models import (
    CheckRun,
    Finding,
    Package,
    PackageRevision,
    RuleDefinition,
    RuleSnapshot,
)
from app.schemas.findings import (
    OUTCOME_ORDER,
    SEVERITY_ORDER,
    FindingOut,
    FindingPage,
)
from verdict.outcomes import Outcome, Severity

router = APIRouter(tags=["findings"])

#: The most findings one request will return. A package can carry thousands, and a report generator
#: asking for all of them in one response is how the control plane starts doing heavy work
#: (`DESIGN_PLATFORM.md` §4.2, C2.6).
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50

#: What a refusal says. Nothing about the project, the role required or the reason — a message that
#: explained itself would give back exactly what the 404 was chosen to hide.
NOT_FOUND_DETAIL = "Not found"


#: The request-scoped session. Defined in `app/api/dependencies.py` since #205, because the packages
#: and documents routers need the same one — re-exported here so `findings.get_session` keeps naming
#: it, which is what `dependency_overrides` in the existing tests is keyed on.
__all__ = ["get_session", "router"]


# ---------------------------------------------------------------------------
# The sort key, and the cursor that names a position in it
# ---------------------------------------------------------------------------


def _rank(
    column: InstrumentedAttribute[str], order: Sequence[Outcome] | Sequence[Severity]
) -> ColumnElement[int]:
    """Turn a stored value into its position in the documented order.

    A `CASE` rather than an `ORDER BY` on the text itself: the columns store `'CRITICAL'` and
    `'MINOR'`, and alphabetical order would put `ADVISORY` above `CRITICAL`. The rank is built from
    the tuples in `app/schemas/findings.py`, which are validated at import to cover every member, so
    a new outcome cannot quietly land in the unranked bucket.
    """
    ranks = {member.value: position for position, member in enumerate(order)}
    return case(ranks, value=column, else_=len(ranks)).cast(Integer)


_SEVERITY_RANK = _rank(Finding.severity, SEVERITY_ORDER)
_OUTCOME_RANK = _rank(Finding.outcome, OUTCOME_ORDER)


@dataclass(frozen=True, slots=True)
class Cursor:
    """The exact position of the last row of a page, in the documented sort order.

    Every component of the sort key is here. A cursor holding only `created_at` and `id` could not
    express "after the last CRITICAL FAIL", so the query would have to re-sort and re-scan, and the
    boundary would move whenever a row was inserted with an earlier timestamp but a worse severity.
    """

    severity: Severity
    outcome: Outcome
    created_at: datetime
    id: UUID


def encode_cursor(cursor: Cursor) -> str:
    """Render a position as one opaque string.

    Opaque on purpose — the components are an implementation detail of the ordering, and a client
    that parsed them would break the day the ordering gains a tie-break. Base64url with the padding
    stripped so it survives being put in a query string unescaped.
    """
    payload = json.dumps(
        {
            "s": cursor.severity.value,
            "o": cursor.outcome.value,
            "t": cursor.created_at.isoformat(),
            "i": str(cursor.id),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(raw: str) -> Cursor:
    """Read a cursor back, refusing anything we did not issue.

    Refuses rather than falling back to the first page. A cursor that silently means "start again"
    turns a client bug into an endless loop over page one, and the client has no way to notice: every
    request succeeds and the data looks plausible.
    """
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        return Cursor(
            severity=Severity(payload["s"]),
            outcome=Outcome(payload["o"]),
            created_at=datetime.fromisoformat(payload["t"]),
            id=UUID(payload["i"]),
        )
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        UnicodeDecodeError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "The cursor is not one this endpoint issued. Use the `next_cursor` from the "
                "previous page verbatim, or omit it to start from the beginning."
            ),
        ) from error


def _strictly_after(keys: Sequence[tuple[Any, Any]]) -> ColumnElement[bool]:
    """Everything after this position, in the same lexicographic order the query sorts by.

    Written out as `OR`-ed `AND` chains rather than as a row-value comparison. The row form is
    shorter, but it needs the database to infer a type for each side, and a computed `CASE` on one
    side with a bound parameter on the other is exactly where that inference goes wrong — quietly,
    by comparing as text. Being explicit costs four lines and cannot be wrong in a way that only
    shows up as a page that skips rows.
    """
    clauses = [
        and_(*[prior == prior_value for prior, prior_value in keys[:index]], expression > value)
        for index, (expression, value) in enumerate(keys)
    ]
    return or_(*clauses)


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------


def _base_query(project_id: UUID, package_id: UUID) -> Select[Any]:
    """Every finding for one package, with the versions that explain it.

    Joined explicitly rather than by inferred relationship: `findings.check_run_id` and
    `findings.package_revision_id` are covered by one composite foreign key to `check_runs`, so
    SQLAlchemy cannot work out a single-column join condition for either on its own.
    """
    return (
        select(
            Finding.id,
            Finding.check_run_id,
            Finding.package_revision_id,
            Finding.outcome,
            Finding.severity,
            Finding.parameter_set_versions,
            Finding.created_at,
            PackageRevision.revision_number,
            CheckRun.engine_version,
            RuleSnapshot.id.label("rule_snapshot_id"),
            RuleSnapshot.snapshot_id.label("rule_snapshot_hash"),
            RuleSnapshot.version.label("rule_version"),
            RuleSnapshot.check_type,
            RuleSnapshot.product_type,
            RuleDefinition.rule_id,
        )
        .join(PackageRevision, PackageRevision.id == Finding.package_revision_id)
        .join(Package, Package.id == PackageRevision.package_id)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        # Both halves matter. The package pins the resource; the project is the isolation boundary,
        # and leaving it to the dependency alone would mean a package id from another project reached
        # the database with nothing but a membership claim standing between them.
        .where(Package.project_id == project_id, Package.id == package_id)
    )


def _package_is_in_project(session: Session, project_id: UUID, package_id: UUID) -> bool:
    """Whether this package exists *and* belongs to this project — the two are one question here.

    Answering them separately would leak: "the package exists but not for you" is the 403 this
    boundary is built to avoid, spelled differently.
    """
    statement = select(Package.id).where(Package.id == package_id, Package.project_id == project_id)
    return session.execute(statement).first() is not None


@router.get(
    "/projects/{project_id}/packages/{package_id}/findings",
    response_model=FindingPage,
    summary="List a package's findings, worst first",
)
def list_findings(
    principal: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    package_id: UUID,
    project_id: UUID,
    outcome: Annotated[
        list[Outcome] | None,
        Query(
            description=(
                "Restrict to these outcomes. Omit for all of them, abstentions included — a list "
                "that quietly dropped REVIEW_REQUIRED would make a package look clean."
            )
        ),
    ] = None,
    severity: Annotated[
        list[Severity] | None, Query(description="Restrict to these severities.")
    ] = None,
    check_type: Annotated[
        list[str] | None,
        Query(description="Restrict to rules of these check types, e.g. `internal`."),
    ] = None,
    product_type: Annotated[
        list[str] | None,
        Query(description="Restrict to rules about these product types, e.g. `countertop`."),
    ] = None,
    cursor: Annotated[
        str | None, Query(description="The `next_cursor` from the previous page.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> FindingPage:
    """One page of this package's findings, worst first.

    **Every outcome is included unless you ask for fewer.** The abstentions — `NOT_FOUND`,
    `REVIEW_REQUIRED`, `NO_APPLICABLE_RULE` — are things the engine concluded, and a default that
    hid them would report a package as clean on the strength of checks nobody looked at.

    **Order:** severity worst-first, then outcome (failures, then abstentions, then passes), then
    oldest first, then id. The full description comes back in `ordering` on every page.

    **Paging:** pass the `next_cursor` from the previous page. `next_cursor` is `null` on the last
    page. The cursor names the last row rather than counting rows, so findings written while you are
    paging cannot push a row across a page boundary and out of sight.

    **Scope:** a package in another project is reported as not found, in the same words as a package
    that does not exist. That is deliberate — see `docs/DESIGN_PLATFORM.md` §4.3.

    The response carries identity and versions, not calculation traces. Reconstructing a finding
    from its own operands is a per-finding request (`docs/DESIGN_PRODUCT.md` §3.1).
    """
    del principal  # the dependency is the check; the endpoint needs nothing from the caller

    # Arguments before resources. A cursor we did not issue is a malformed request whatever package
    # it names, and checking it first keeps a bad one from costing a database round trip.
    position = None if cursor is None else decode_cursor(cursor)

    if not _package_is_in_project(session, project_id, package_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    statement = _base_query(project_id, package_id)
    if outcome:
        statement = statement.where(Finding.outcome.in_([value.value for value in outcome]))
    if severity:
        statement = statement.where(Finding.severity.in_([value.value for value in severity]))
    if check_type:
        statement = statement.where(RuleSnapshot.check_type.in_(check_type))
    if product_type:
        statement = statement.where(RuleSnapshot.product_type.in_(product_type))

    if position is not None:
        statement = statement.where(
            _strictly_after(
                [
                    (_SEVERITY_RANK, SEVERITY_ORDER.index(position.severity)),
                    (_OUTCOME_RANK, OUTCOME_ORDER.index(position.outcome)),
                    (Finding.created_at, position.created_at),
                    (Finding.id, position.id),
                ]
            )
        )

    statement = statement.order_by(
        _SEVERITY_RANK, _OUTCOME_RANK, Finding.created_at, Finding.id
    ).limit(limit + 1)

    # One row more than asked for, discarded before the response. It is the only honest way to say
    # "there is a next page": a full page is not evidence of one, and issuing a cursor that leads to
    # an empty page makes a client walk an extra round trip to find the end.
    rows = session.execute(statement).all()
    page = rows[:limit]
    # `_mapping` rather than attribute access: a `Row` is a tuple subclass, and handing one to a
    # model that expects named fields is the sort of thing that works until a library decides to
    # treat the tuple half first. The mapping is unambiguous.
    items = [FindingOut.model_validate(dict(row._mapping)) for row in page]

    next_cursor = None
    if len(rows) > limit and items:
        last = items[-1]
        next_cursor = encode_cursor(
            Cursor(
                severity=last.severity,
                outcome=last.outcome,
                created_at=last.created_at,
                id=last.id,
            )
        )

    return FindingPage(items=items, next_cursor=next_cursor, limit=limit)
