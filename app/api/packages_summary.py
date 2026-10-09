"""Cursor-paged Documents-table summary; no per-package API follow-ups are required.

Search and sort (#1065). Read-only and project-scoped, like the rest of the summary:

- `q` keeps packages whose vendor name contains the text, ignoring case, or whose product's label
  (the words the upload screen shows) does. LIKE wildcards in `q` are matched as plain text.
- `sort` is `updated` (default: newest change first), `vendor` (A to Z ignoring case, unnamed last)
  or `needs_decision` (most findings still needing a reviewer first, then newest). Every order ends
  in the package id, so it is total and a page boundary falls in one place only.
- The cursor carries the sort and the search it was issued for. Reusing it with a different sort or
  search is refused (422), never silently reinterpreted: a position in one order is not a position
  in another, and treating it as one skips or repeats rows without any sign of it.

`needs_decision` is the sign-off readiness answer (`approval_readiness_many`), so sorting by it
computes readiness for every package that matches the search: a fixed number of queries whatever
the project's size, with rows that grow with it. The other sorts and every page's own readiness use
only the page's revisions.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import ColumnElement, Select, and_, case, collate, func, or_, select, tuple_
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.packages import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, PRODUCT_LABELS
from app.auth import Action, Principal, require_action, require_project_access
from app.models import (
    Approval,
    ApprovalExportBundle,
    CheckRun,
    Finding,
    Package,
    PackageRevision,
    PackageStateEvent,
)
from app.review.approval import ApprovalReadiness, approval_readiness_many
from app.schemas.visual_ui import (
    NeedsDecisionByOutcomeOut,
    OutcomeCountsOut,
    PackageSummaryItemOut,
    PackageSummaryPageOut,
)

router = APIRouter(tags=["visual reviewer"])

SummarySort = Literal["updated", "vendor", "needs_decision"]

#: Longer than any vendor name a person would type into a search box, short enough to be harmless.
MAX_SEARCH_LENGTH: Final = 200

#: Which recorded outcome each split bucket counts. A blocking finding with any other outcome can
#: only be blocking because a reviewer's correction awaits a check re-run; it is counted as
#: `other`, never relabelled as a FAIL or a hold.
_SPLIT: Final = {"FAIL": "fail", "REVIEW_REQUIRED": "review", "NOT_FOUND": "not_found"}

_REFUSED_CURSOR: Final = (
    "The cursor is not one this endpoint issued. Use the `next_cursor` from the previous page "
    "verbatim, or omit it to start from the beginning."
)
_MISMATCHED_CURSOR: Final = (
    "This cursor was issued for a different sort or search. Repeat the same `sort` and `q` with it, "
    "or omit the cursor to start the new order from the beginning."
)


def _search(q: str | None) -> str | None:
    if q is None:
        return None
    stripped = q.strip()
    return stripped or None


def _encode(sort: SummarySort, q: str | None, key: Sequence[object]) -> str:
    parts = [
        (
            value.isoformat()
            if isinstance(value, datetime)
            else str(value) if isinstance(value, UUID) else value
        )
        for value in key
    ]
    payload = json.dumps(
        {"s": sort, "q": q, "k": parts}, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode(raw: str, sort: SummarySort, q: str | None) -> tuple[Any, ...]:
    """The position in `sort` order, refusing a cursor we did not issue for this sort and search."""
    try:
        payload = json.loads(base64.urlsafe_b64decode((raw + "=" * (-len(raw) % 4)).encode()))
        issued_sort, issued_q, parts = payload["s"], payload["q"], payload["k"]
        if not isinstance(parts, list):
            raise TypeError("cursor key is not a list")
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        UnicodeDecodeError,
    ) as error:
        raise HTTPException(422, _REFUSED_CURSOR) from error
    if issued_sort != sort or issued_q != q:
        raise HTTPException(422, _MISMATCHED_CURSOR)
    try:
        if sort == "updated":
            when, identity = parts
            return (datetime.fromisoformat(when), UUID(identity))
        if sort == "vendor":
            unnamed, name, identity = parts
            if unnamed not in (0, 1) or not isinstance(name, str):
                raise ValueError("vendor cursor is malformed")
            return (unnamed, name, UUID(identity))
        needs, when, identity = parts
        if not isinstance(needs, int) or isinstance(needs, bool) or needs < 0:
            raise ValueError("needs-decision cursor is malformed")
        return (needs, datetime.fromisoformat(when), UUID(identity))
    except (TypeError, ValueError) as error:
        raise HTTPException(422, _REFUSED_CURSOR) from error


@router.get(
    "/projects/{project_id}/packages-summary",
    response_model=PackageSummaryPageOut,
    summary="Summarize packages for the Documents table",
)
def packages_summary(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.READ_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    q: Annotated[
        str | None,
        Query(
            max_length=MAX_SEARCH_LENGTH,
            description=(
                "Case-insensitive text to find in the vendor name or the product label. "
                "Surrounding spaces are ignored; empty means no search."
            ),
        ),
    ] = None,
    sort: Annotated[
        SummarySort,
        Query(
            description=(
                "`updated`: newest change first. `vendor`: A to Z ignoring case, unnamed last. "
                "`needs_decision`: most findings still needing a reviewer first, then newest. "
                "Ties end in the package id. A cursor only continues the sort and search it was "
                "issued for."
            )
        ),
    ] = "updated",
) -> PackageSummaryPageOut:
    del principal
    search = _search(q)
    position = None if cursor is None else _decode(cursor, sort, search)

    current = (
        select(
            PackageRevision.package_id, func.max(PackageRevision.revision_number).label("rev_no")
        )
        .group_by(PackageRevision.package_id)
        .subquery()
    )
    latest_event = select(
        PackageStateEvent.package_revision_id,
        PackageStateEvent.created_at.label("updated_at"),
        func.row_number()
        .over(
            partition_by=PackageStateEvent.package_revision_id,
            order_by=(PackageStateEvent.sequence.desc(), PackageStateEvent.id.desc()),
        )
        .label("rank"),
    ).subquery()
    state_event = select(latest_event).where(latest_event.c.rank == 1).subquery()
    updated_at = func.coalesce(state_event.c.updated_at, PackageRevision.created_at)
    unnamed = func.coalesce(func.btrim(Package.vendor), "") == ""
    unnamed_rank = case((unnamed, 1), else_=0)
    vendor_key = collate(case((unnamed, ""), else_=func.lower(func.btrim(Package.vendor))), "C")

    filters: list[ColumnElement[bool]] = [Package.project_id == project_id]
    if search is not None:
        needle = search.lower()
        products = [
            product.value for product, label in PRODUCT_LABELS.items() if needle in label.lower()
        ]
        matched = Package.vendor.icontains(search, autoescape=True)
        filters.append(or_(matched, Package.product_type.in_(products)) if products else matched)

    def scoped(statement: Select[*tuple[Any, ...]]) -> Select[*tuple[Any, ...]]:
        """The current revision of each package in the project that matches the search."""
        return (
            statement.join(current, current.c.package_id == Package.id)
            .join(
                PackageRevision,
                and_(
                    PackageRevision.package_id == Package.id,
                    PackageRevision.revision_number == current.c.rev_no,
                ),
            )
            .outerjoin(
                state_event,
                and_(
                    state_event.c.package_revision_id == PackageRevision.id,
                    state_event.c.rank == 1,
                ),
            )
            .where(*filters)
        )

    query = scoped(
        select(
            Package,
            PackageRevision,
            updated_at.label("updated_at"),
            unnamed_rank.label("unnamed_rank"),
            vendor_key.label("vendor_key"),
            func.count(func.distinct(case((Finding.outcome == "PASS", Finding.id)))).label(
                "pass_count"
            ),
            func.count(func.distinct(case((Finding.outcome == "FAIL", Finding.id)))).label(
                "fail_count"
            ),
            func.count(
                func.distinct(case((Finding.outcome == "REVIEW_REQUIRED", Finding.id)))
            ).label("review_count"),
            func.count(func.distinct(case((Finding.outcome == "NOT_FOUND", Finding.id)))).label(
                "not_found_count"
            ),
            func.count(
                func.distinct(case((Finding.outcome == "NO_APPLICABLE_RULE", Finding.id)))
            ).label("no_rule_count"),
            func.bool_or(Approval.id.is_not(None)).label("approved"),
            func.bool_or(ApprovalExportBundle.id.is_not(None)).label("signed_ready"),
        ).select_from(Package)
    )
    query = (
        query.outerjoin(
            CheckRun,
            and_(
                CheckRun.package_revision_id == PackageRevision.id, CheckRun.superseded_at.is_(None)
            ),
        )
        .outerjoin(
            Finding,
            and_(
                Finding.check_run_id == CheckRun.id,
                Finding.package_revision_id == PackageRevision.id,
            ),
        )
        .outerjoin(Approval, Approval.package_revision_id == PackageRevision.id)
        .outerjoin(
            ApprovalExportBundle, ApprovalExportBundle.package_revision_id == PackageRevision.id
        )
        .group_by(Package.id, PackageRevision.id, state_event.c.updated_at)
    )

    readiness: dict[UUID, ApprovalReadiness]
    if sort == "needs_decision":
        # Readiness is Python, not SQL, and must stay the one answer sign-off uses; so the order is
        # computed from it over every matching package, and only the page's rows are then summed.
        candidates = session.execute(
            scoped(
                select(Package.id, PackageRevision.id, updated_at.label("updated_at")).select_from(
                    Package
                )
            )
        ).all()
        readiness = approval_readiness_many(session, [row[1] for row in candidates])
        keyed = sorted(
            (
                (readiness[revision_id].blocking_findings, when, package_id)
                for package_id, revision_id, when in candidates
            ),
            reverse=True,
        )
        if position is not None:
            keyed = [key for key in keyed if key < position]
        window = keyed[: limit + 1]
        order = {key[2]: index for index, key in enumerate(window)}
        found = session.execute(query.where(Package.id.in_(order))).all() if order else []
        rows = sorted(found, key=lambda row: order[row.Package.id])
        has_more = len(window) > limit
    else:
        if sort == "updated":
            ordered = query.order_by(updated_at.desc(), Package.id.desc())
            if position is not None:
                ordered = ordered.where(tuple_(updated_at, Package.id) < tuple_(*position))
        else:
            ordered = query.order_by(unnamed_rank, vendor_key, Package.id)
            if position is not None:
                ordered = ordered.where(
                    tuple_(unnamed_rank, vendor_key, Package.id) > tuple_(*position)
                )
        rows = list(session.execute(ordered.limit(limit + 1)).all())
        has_more = len(rows) > limit
        readiness = approval_readiness_many(
            session, [row.PackageRevision.id for row in rows[:limit]]
        )
    page_rows = rows[:limit]

    blocking = [
        identity
        for row in page_rows
        for identity in readiness[row.PackageRevision.id].blocking_finding_ids
    ]
    outcome_of: dict[UUID, str] = (
        {
            identity: outcome
            for identity, outcome in session.execute(
                select(Finding.id, Finding.outcome).where(Finding.id.in_(blocking))
            )
        }
        if blocking
        else {}
    )

    def split(row: Any) -> NeedsDecisionByOutcomeOut:
        counts = {"fail": 0, "review": 0, "not_found": 0, "other": 0}
        for identity in readiness[row.PackageRevision.id].blocking_finding_ids:
            counts[_SPLIT.get(outcome_of.get(identity, ""), "other")] += 1
        return NeedsDecisionByOutcomeOut(**counts)

    items = tuple(
        PackageSummaryItemOut(
            package_id=row.Package.id,
            revision_id=row.PackageRevision.id,
            revision_number=row.PackageRevision.revision_number,
            vendor=row.Package.vendor,
            product_type=row.Package.product_type,
            state=row.PackageRevision.state,
            created_at=row.Package.created_at,
            updated_at=row.updated_at,
            outcomes=OutcomeCountsOut(
                **{
                    "pass": int(row.pass_count or 0),
                    "fail": int(row.fail_count or 0),
                    "review": int(row.review_count or 0),
                    "not_found": int(row.not_found_count or 0),
                    "no_rule": int(row.no_rule_count or 0),
                }
            ),
            needs_decision=readiness[row.PackageRevision.id].blocking_findings,
            needs_decision_by_outcome=split(row),
            approved=bool(row.approved),
            signed_exports_ready=bool(row.signed_ready),
        )
        for row in page_rows
    )
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        key: tuple[object, ...]
        if sort == "updated":
            key = (last.updated_at, last.Package.id)
        elif sort == "vendor":
            key = (int(last.unnamed_rank), last.vendor_key, last.Package.id)
        else:
            key = (
                readiness[last.PackageRevision.id].blocking_findings,
                last.updated_at,
                last.Package.id,
            )
        next_cursor = _encode(sort, search, key)
    return PackageSummaryPageOut(
        items=items, next_cursor=next_cursor, limit=limit, sort=sort, q=search
    )


__all__ = ["packages_summary", "router"]
