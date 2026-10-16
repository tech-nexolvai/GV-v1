"""Run the AI page review for a revision read before it existed (decision 2026-10-16).

The worker runs the AI page review after the reading when `GV_AI_PAGE_REVIEW` is on. A revision
read before that has none. This reads each shop document's stored bytes (checked against their
recorded digest), reviews every combined sheet (`workflow/ai_page_review.py`) and stores the
reviews under a run of its own. Append-only: a second run writes a new set, and the newest counts;
a stored answer to the identical question is reused, never paid twice.

**Paid** unless every answer is stored: it calls Opus 5.5 through OpenRouter with
`OPENROUTER_API_KEY`, priced from `GV_MODEL_RATES_FILE`, within `GV_AI_PAGE_REVIEW_BUDGET_USD`
(default $6) for the revision. `--stored-only` makes no call at all (pages without a stored answer
are stored `failed`, saying so). `--pages 2,7` limits it to those sheets (from 1).

Run it with the database and the storage root the worker uses (`GV_DATABASE_URL`,
`GV_DEV_STORAGE`):

    python scripts/ai_page_review_backfill.py --revision <package revision id> [--stored-only]

It prints counts and cost only, never a value off the drawing.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys
from collections.abc import Sequence
from uuid import UUID, uuid4

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--revision", required=True, help="the package revision to review")
    parser.add_argument("--dpi", type=int, default=None, help="the resolution to read at")
    parser.add_argument("--pages", default=None, help="only these sheets, from 1: 2,7")
    parser.add_argument("--stored-only", action="store_true", help="make no paid call")
    args = parser.parse_args(argv)

    import os

    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session

    from app.config import Settings
    from app.models.runs import ExtractionRun, ModelInvocation, TaskRun, WorkflowRun
    from app.runs.rates import rates_from_environment
    from storage.local import LocalStore
    from workflow.ai_page_review import (
        AI_PAGE_REVIEW_EXTRACTOR,
        ai_page_review_budget,
        configured_client,
        persist_ai_page_review,
        spend_guard,
    )
    from workflow.stages import (
        EXTRACTOR,
        _document_records_for,
        _fetch,
        _one_file_as_both_roles,
        _shop_document_versions_for,
    )

    settings = Settings()  # type: ignore[call-arg]
    root = os.environ.get("GV_DEV_STORAGE")
    if not root:
        print("GV_DEV_STORAGE is not set: where the stored drawings are", file=sys.stderr)
        return 2
    store = LocalStore(root=pathlib.Path(root), ticket_secret=b"read-only-backfill")
    revision_id = UUID(args.revision)
    pages = None if not args.pages else {int(p) for p in args.pages.split(",") if p.strip()}
    rates = rates_from_environment()
    client = None if args.stored_only or rates is None else configured_client(os.environ)
    if not args.stored_only and client is None:
        print(
            "no paid calls: OPENROUTER_API_KEY or GV_MODEL_RATES_FILE is not set "
            "(stored answers only)",
            file=sys.stderr,
        )
    guard = spend_guard(rates, ai_page_review_budget(os.environ))
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        if _one_file_as_both_roles(session, revision_id):
            print("not reviewed: the same file was uploaded as both drawings")
            return 0
        dpi = args.dpi or session.scalar(
            select(ExtractionRun.dpi)
            .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
            .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
            .where(
                WorkflowRun.package_revision_id == revision_id, ExtractionRun.extractor == EXTRACTOR
            )
            .order_by(ExtractionRun.created_at.desc())
            .limit(1)
        )
        if not dpi:
            print("the revision's pages were never read: give --dpi", file=sys.stderr)
            return 2
        workflow = WorkflowRun(
            package_revision_id=revision_id, engine_run_id=f"ai-page-review-backfill-{uuid4()}"
        )
        session.add(workflow)
        session.flush()
        task = TaskRun(
            workflow_run_id=workflow.id,
            idempotency_key=f"ai-page-review-backfill-{uuid4()}",
            task_type="ai_page_review_backfill",
            attempt=1,
            outcome="SUCCEEDED",
        )
        session.add(task)
        session.flush()
        shop = set(_shop_document_versions_for(session, revision_id))
        for version, key, sha256, _pages in _document_records_for(session, revision_id):
            if version not in shop:
                continue
            data = _fetch(store, key)
            if hashlib.sha256(data).hexdigest() != sha256:
                print(f"document {version}: its stored bytes do not match their digest; skipped")
                continue
            counts = persist_ai_page_review(
                session,
                package_revision_id=revision_id,
                version_id=version,
                data=data,
                task_run_id=task.id,
                dpi=dpi,
                client=client,
                rates=rates,
                guard=guard,
                pages_wanted=pages,
            )
            print(f"document {version}: pages by status {counts}")
        spent = session.scalar(
            select(func.coalesce(func.sum(ModelInvocation.cost_micros), 0))
            .join(ExtractionRun, ExtractionRun.id == ModelInvocation.extraction_run_id)
            .where(
                ExtractionRun.task_run_id == task.id,
                ExtractionRun.extractor == AI_PAGE_REVIEW_EXTRACTOR,
            )
        )
        session.commit()
    print(f"cost of this run: ${int(spent or 0) / 1_000_000:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
