"""Pair the architect's dimensions with the vendor's lines for revisions read before the direct compare existed (#1418).

The worker stores the direct compare's pairing while it reads a package (`extract_pages`). A
revision read before that has none, and its review shows no "Compared with the architect" section.
This reads each shop document's stored bytes (checked against their recorded digest), pairs every
architect dimension by geometry only (`workflow/architect_compare.py`), and stores the records under
a run of its own. $0: no model call. Append-only: a second run writes a new set, and the newest
counts.

Run it with the database and the storage root the worker uses (`GV_DATABASE_URL`,
`GV_DEV_STORAGE`); it only reads the stored files. The resolution is the one the revision's pages
were read at (its newest page reading's dpi), unless `--dpi` says another:

    python scripts/architect_compare_backfill.py --revision <package revision id>

It prints counts only (records written per document), never a value off the drawing.
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
    parser.add_argument("--revision", required=True, help="the package revision to pair")
    parser.add_argument("--dpi", type=int, default=None, help="the resolution to read at")
    args = parser.parse_args(argv)

    import os

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.config import Settings
    from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
    from storage.local import LocalStore
    from workflow.architect_compare import persist_architect_compare
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
    # Read only: the ticket secret signs upload links, which this never makes.
    store = LocalStore(root=pathlib.Path(root), ticket_secret=b"read-only-backfill")
    revision_id = UUID(args.revision)
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        if _one_file_as_both_roles(session, revision_id):
            print("not paired: the same file was uploaded as both drawings")
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
            package_revision_id=revision_id, engine_run_id=f"architect-compare-backfill-{uuid4()}"
        )
        session.add(workflow)
        session.flush()
        task = TaskRun(
            workflow_run_id=workflow.id,
            idempotency_key=f"architect-compare-backfill-{uuid4()}",
            task_type="architect_compare_backfill",
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
            written = persist_architect_compare(
                session,
                package_revision_id=revision_id,
                version_id=version,
                data=data,
                task_run_id=task.id,
                dpi=dpi,
            )
            print(f"document {version}: {written} architect dimension(s) recorded")
        session.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
