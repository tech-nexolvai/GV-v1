"""Cursor-paged Documents-table summary; no per-package API follow-ups are required."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.packages import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, decode_cursor, encode_cursor
from app.auth import Action, Principal, require_action, require_project_access
from app.models import (
    Approval,
    ApprovalExportBundle,
    CheckRun,
    Finding,
    Package,
    PackageRevision,
    PackageStateEvent,
    RuleDefinition,
    RuleSnapshot,
)
from app.review.approval import approval_readiness_many
from app.schemas.visual_ui import OutcomeCountsOut, PackageSummaryItemOut, PackageSummaryPageOut

router = APIRouter(tags=["visual reviewer"])


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
) -> PackageSummaryPageOut:
    del principal
    position = None if cursor is None else decode_cursor(cursor)
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
    query = (
        select(
            Package,
            PackageRevision,
            func.coalesce(state_event.c.updated_at, PackageRevision.created_at).label("updated_at"),
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
        )
        .join(current, current.c.package_id == Package.id)
        .join(
            PackageRevision,
            and_(
                PackageRevision.package_id == Package.id,
                PackageRevision.revision_number == current.c.rev_no,
            ),
        )
        .outerjoin(
            state_event,
            and_(state_event.c.package_revision_id == PackageRevision.id, state_event.c.rank == 1),
        )
        .outerjoin(
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
        .outerjoin(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .outerjoin(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .outerjoin(Approval, Approval.package_revision_id == PackageRevision.id)
        .outerjoin(
            ApprovalExportBundle, ApprovalExportBundle.package_revision_id == PackageRevision.id
        )
        .where(Package.project_id == project_id)
        .group_by(Package.id, PackageRevision.id, state_event.c.updated_at)
        .order_by(Package.created_at.desc(), Package.id.desc())
    )
    if position is not None:
        created, package_id = position
        query = query.where(
            or_(
                Package.created_at < created,
                and_(Package.created_at == created, Package.id < package_id),
            )
        )
    rows = session.execute(query.limit(limit + 1)).all()
    page_rows = rows[:limit]
    revision_ids = [row.PackageRevision.id for row in page_rows]
    readiness = approval_readiness_many(session, revision_ids)
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
            approved=bool(row.approved),
            signed_exports_ready=bool(row.signed_ready),
        )
        for row in page_rows
    )
    next_cursor = (
        encode_cursor(page_rows[-1].Package.created_at, items[-1].package_id)
        if len(rows) > limit and items
        else None
    )
    return PackageSummaryPageOut(items=items, next_cursor=next_cursor, limit=limit)


__all__ = ["packages_summary", "router"]
