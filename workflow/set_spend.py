"""What a drawing set's AI readers have spent, and what the depth and sink reader may still spend.

Moved word for word from `workflow/stages.py` (#1308), which still names each one under its old
private name, so the API can say how much of the set's AI budget is left before a person asks to
read the depths again (`workflow/depth_again.py`), and so the API and the worker compute it with
the same code. No extraction import: the API reaches it (`tests/api/test_no_heavy_work.py`).

Verification: `tests/workflow/test_ai_budget.py`, `tests/workflow/test_read_depths_again.py`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import func, not_, or_, select
from sqlalchemy.orm import Session

from app.models.document import DocumentVersion, PackageRevisionDocument
from app.models.runs import ExtractionRun, ModelInvocation, TaskRun, WorkflowRun
from workflow.ai_budget import AiBudget

__all__ = [
    "UNMETERED_PROMPT_FAMILIES",
    "depth_cap_usd",
    "micros",
    "remaining_claude_budget",
    "revision_page_count",
    "revision_spend",
    "set_budget_left_usd",
]

#: Recorded calls that are not reading spend (#1221), by prompt family: every version of the
#: layout classifier's prompt and of the reading assignment's. They record every call since #1221,
#: but they never counted towards the drawing set's AI budget, and phase 1 changes no behaviour:
#: `stage_spend_so_far` leaves them out until the shared budget (Sutra step 3,
#: `docs/decisions/SUTRA_HARNESS.md`) decides how they count. A family rather than exact ids, so a
#: new prompt version cannot start counting by accident. The assignment's rows belong to a package
#: revision, never to a run, so naming it is a safety net.
UNMETERED_PROMPT_FAMILIES: Final = ("layout-discriminator-", "reading-assignment-")


def micros(amount: Decimal) -> int:
    """US dollars as whole millionths, rounded down: a cap never rounds up past what was set."""
    return int(amount * 1_000_000)


def revision_page_count(session: Session, package_revision_id: UUID) -> int:
    """How many pages the revision's documents hold: the drawing set's page-scaled budget's pages."""
    return int(
        session.scalar(
            select(func.coalesce(func.sum(DocumentVersion.page_count), 0))
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.document_version_id == DocumentVersion.id,
            )
            .where(PackageRevisionDocument.package_revision_id == package_revision_id)
        )
        or 0
    )


def revision_spend(session: Session, package_revision_id: UUID) -> tuple[int, int]:
    """What every reader of this revision has spent, in micros, and how many calls had no price.

    Every call recorded under any run of the revision's workflow runs, the call ledger's own runs
    included (a twin row costs nothing, so each call counts once). The layout classifier's and the
    assignment's rows are not reading spend (`UNMETERED_PROMPT_FAMILIES`), as for the stage's meter.
    """
    spent, unpriced = session.execute(
        select(
            func.coalesce(func.sum(ModelInvocation.cost_micros), 0),
            func.count().filter(ModelInvocation.cost_micros.is_(None)),
        )
        .join(ExtractionRun, ModelInvocation.extraction_run_id == ExtractionRun.id)
        .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
        .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
        .where(WorkflowRun.package_revision_id == package_revision_id)
        .where(
            not_(
                or_(
                    *(
                        ModelInvocation.prompt_id.startswith(family, autoescape=True)
                        for family in UNMETERED_PROMPT_FAMILIES
                    )
                )
            )
        )
    ).one()
    return int(spent or 0), int(unpriced or 0)


def remaining_claude_budget(
    requested_usd: Decimal,
    *,
    cap_micros: int,
    spent_micros: int,
    unpriced_calls: int,
) -> Decimal:
    """Claude's cap is bounded by both its per-set limit and the set-wide invocation meter."""
    if unpriced_calls:
        return Decimal(0)
    remaining = Decimal(max(0, cap_micros - spent_micros)) / Decimal(1_000_000)
    return min(requested_usd, remaining)


def _set_budget_micros(
    session: Session, package_revision_id: UUID, *, budget: AiBudget, fixed_usd: Decimal | None
) -> int:
    """The drawing set's whole AI budget, in micros: the fixed amount stated for a set, or else
    its page-scaled budget for all its pages."""
    return (
        micros(fixed_usd)
        if fixed_usd is not None
        else micros(budget.for_pages(revision_page_count(session, package_revision_id)))
    )


def set_budget_left_usd(
    session: Session,
    package_revision_id: UUID,
    *,
    budget: AiBudget,
    fixed_usd: Decimal | None,
) -> Decimal:
    """What the drawing set's whole AI budget has left (#1328): its budget less everything its
    readers spent, never below nothing. Nothing while a call has no price, as for the cap.

    Not what one request may spend: that is `depth_cap_usd`, which is also held under the Claude
    readers' ceiling. A run that spent $0.29 under a $1.50 ceiling may still spend $1.50 next time,
    while this goes down by $0.29."""
    budget_micros = _set_budget_micros(
        session, package_revision_id, budget=budget, fixed_usd=fixed_usd
    )
    spent_micros, unpriced = revision_spend(session, package_revision_id)
    if unpriced:
        return Decimal(0)
    return Decimal(max(0, budget_micros - spent_micros)) / Decimal(1_000_000)


def depth_cap_usd(
    session: Session,
    package_revision_id: UUID,
    *,
    budget: AiBudget,
    fixed_usd: Decimal | None,
    claude_ceiling_usd: Decimal | None,
) -> Decimal:
    """What the depth and sink reader may still spend on this set (#1290, #1308): the smaller of
    the Claude readers' configured ceiling and what the set's AI budget has left (its page-scaled
    budget for all its pages, or the fixed amount stated, less everything its readers spent).
    Nothing while a call has no price. The worker's cap and the API's "this request may spend up
    to" are this; what the set has left is `set_budget_left_usd` (#1328)."""
    budget_micros = _set_budget_micros(
        session, package_revision_id, budget=budget, fixed_usd=fixed_usd
    )
    spent_micros, unpriced = revision_spend(session, package_revision_id)
    whole = Decimal(budget_micros) / 1_000_000
    return remaining_claude_budget(
        whole if claude_ceiling_usd is None else min(claude_ceiling_usd, whole),
        cap_micros=budget_micros,
        spent_micros=spent_micros,
        unpriced_calls=unpriced,
    )
