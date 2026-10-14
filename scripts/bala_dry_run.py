#!/usr/bin/env python3
"""Bala's dry run: what it would suggest on a stored database, and what PR 2 would make, at $0 (#1236).

Reads only, inside a read-only transaction, and calls no model. For every package revision with a
slot-reader run (or the ones named), it plans Bala's suggestions exactly as the match stage files
them (`workflow/bala_items.py:plan_items`) and reports, per revision: the countertop rows, the
suggestions (a countertop and its pieces per row), the rows not suggested and why, and what would be
automatic in PR 2 (both judgments agreeing), with the reasons the rest would not be.

    DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5434/<database> \\
        .venv/bin/python scripts/bala_dry_run.py [--revision ID ...] [--json out.json]

`--database` overrides `DATABASE_URL`. **Code's part finder** (the pieces' second judgment) runs
only with `--store <root>` (the local store holding the revision's PDFs, read only) and
`GV_PART_FINDER_ENABLED=1` with the `GV_READER_*` settings, as the worker states them; otherwise the
report says it did not run. `GV_READER_MISSING_SPACE_HEIGHTS` is needed for a page with vector text.
`--cabinet-prefill` plans as `GV_BALA_CABINET_PREFILL` would (#1266): a piece with one printed cabinet
tag, as the part finder reads it, is pre-filled as a cabinet.

The report holds code's own sentences, ids, page numbers and counts, never a drawing's values; a
report about a client's drawings is still kept out of the repository. Compare it with an answer key
locally.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import PackageRevision
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from eval.pariksha.stored import _read_only
from workflow.bala_items import BalaPlan, CodeParts, plan_items


def _revisions(session: Session) -> list[UUID]:
    """Every revision with a slot-reader run, oldest first."""
    return list(
        session.scalars(
            select(PackageRevision.id)
            .where(
                PackageRevision.id.in_(
                    select(WorkflowRun.package_revision_id)
                    .join(TaskRun, TaskRun.workflow_run_id == WorkflowRun.id)
                    .join(ExtractionRun, ExtractionRun.task_run_id == TaskRun.id)
                    .where(ExtractionRun.extractor_version.like("slot-reader%"))
                )
            )
            .order_by(PackageRevision.created_at, PackageRevision.id)
        )
    )


def _code_parts(session: Session, revision: UUID, store_root: Path | None) -> CodeParts | None:
    """Code's part finder in memory, reading the PDFs from `store_root`, or `None` when not asked."""
    if store_root is None:
        return None
    from storage.local import LocalStore
    from workflow.stages import (
        DatabaseStages,
        _document_records_for,
        missing_space_from_environment,
    )
    from workflow.sutradhar import part_finder_configuration

    finder = part_finder_configuration()
    if finder is None:
        return lambda _page: "code's part finder is off: GV_PART_FINDER_ENABLED is not set"
    stages = DatabaseStages(
        LocalStore(root=store_root, ticket_secret=b"bala-dry-run-never-issues-tickets"),
        part_finder=finder,  # type: ignore[arg-type]
        missing_space=missing_space_from_environment(),
    )
    keys = {version: key for version, key, _, _ in _document_records_for(session, revision)}
    loaded: dict[UUID, bytes] = {}
    return lambda page: stages._code_parts_on(session, page, keys, loaded)


def report(plan: BalaPlan, revision: UUID) -> dict[str, Any]:
    """The plan as plain data: counts, then one entry per row and per page without a row."""
    filed = [row for row in plan.rows if row.not_proposed is None]
    why_countertop: Counter[str] = Counter()
    why_piece: Counter[str] = Counter()
    for row in filed:
        why_countertop.update(row.automatic.countertop_why_not)
        for reasons in row.automatic.pieces_why_not:
            why_piece.update(reasons)
    return {
        "revision": str(revision),
        "slot_reader_run": None if plan.run_id is None else str(plan.run_id),
        "rows": len(plan.rows),
        "rows_suggested": len(filed),
        "countertops": len(filed),
        "pieces": sum(1 for row in filed for part in row.parts if part.slot_index is not None),
        "pieces_kind_named": sum(
            1
            for row in filed
            for part in row.parts
            if part.slot_index is not None and part.kind_named
        ),
        "would_be_automatic": {
            "countertops": sum(1 for row in filed if row.automatic.countertop),
            "pieces": sum(sum(row.automatic.pieces) for row in filed),
        },
        "countertop_not_automatic_because": dict(why_countertop.most_common()),
        "piece_not_automatic_because": dict(why_piece.most_common()),
        "code_part_finder": sorted({row.code_finder for row in plan.rows if row.code_finder}),
        "row_list": [
            {
                "page_number": row.page_number,
                "label": row.label,
                "row_candidate_id": str(row.row_candidate_id),
                "suggested": row.not_proposed is None,
                "not_proposed": row.not_proposed,
                "states": list(row.states),
                "kinds": [part.kind.value for part in row.parts if part.slot_index is not None],
                "kinds_named": [
                    part.kind_named for part in row.parts if part.slot_index is not None
                ],
                "countertop_automatic": row.automatic.countertop,
                "countertop_why_not": list(row.automatic.countertop_why_not),
                "pieces_automatic": list(row.automatic.pieces),
                "pieces_code_agrees": list(row.automatic.pieces_code_agrees),
            }
            for row in plan.rows
        ],
        "pages_without_a_row": [
            {"page_number": page.page_number, "kind": page.kind, "reason": page.reason}
            for page in plan.unchosen
        ],
    }


def render(entry: dict[str, Any]) -> str:
    lines = [
        f"Revision {entry['revision']} (slot-reader run {entry['slot_reader_run']})",
        (
            f"  rows {entry['rows']} -> suggested rows {entry['rows_suggested']}: "
            f"{entry['countertops']} countertops + {entry['pieces']} pieces "
            f"({entry['pieces_kind_named']} pieces with a printed kind)"
        ),
        (
            f"  would be automatic in PR 2: {entry['would_be_automatic']['countertops']} "
            f"countertops, {entry['would_be_automatic']['pieces']} pieces"
        ),
    ]
    for reason, count in entry["countertop_not_automatic_because"].items():
        lines.append(f"    countertop not automatic: {reason} ({count})")
    for reason, count in entry["piece_not_automatic_because"].items():
        lines.append(f"    piece not automatic: {reason} ({count})")
    for reason in entry["code_part_finder"]:
        lines.append(f"  code's part finder: {reason}")
    for row in entry["row_list"]:
        verdict = "suggested" if row["suggested"] else f"NOT suggested: {row['not_proposed']}"
        automatic = "automatic" if row["countertop_automatic"] else "one click"
        lines.append(
            f"  p{row['page_number']} {row['label']}: {verdict}; countertop {automatic}; "
            f"pieces {len(row['kinds'])} ({sum(row['pieces_automatic'])} automatic)"
            + (f"; {'; '.join(row['states'])}" if row["states"] else "")
        )
    for page in entry["pages_without_a_row"]:
        lines.append(f"  p{page['page_number']} no row read ({page['kind']}): {page['reason']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--database", help="overrides DATABASE_URL")
    parser.add_argument("--revision", type=UUID, action="append", help="a revision (repeatable)")
    parser.add_argument("--store", type=Path, help="the local store holding the PDFs, read only")
    parser.add_argument("--json", type=Path, help="also save the report as JSON to this file")
    parser.add_argument(
        "--cabinet-prefill",
        action="store_true",
        help="plan as GV_BALA_CABINET_PREFILL would: one printed cabinet tag pre-fills a cabinet",
    )
    args = parser.parse_args(argv)
    url = args.database or os.environ.get("DATABASE_URL")
    if not url:
        parser.error("set DATABASE_URL or pass --database")

    engine = create_engine(url)
    entries: list[dict[str, Any]] = []
    with Session(engine) as session:
        _read_only(session)
        for revision in args.revision or _revisions(session):
            plan = plan_items(
                session,
                revision,
                code_parts=_code_parts(session, revision, args.store),
                cabinet_prefill=args.cabinet_prefill,
            )
            entry = report(plan, revision)
            entries.append(entry)
            print(render(entry))
        session.rollback()
    if args.json is not None:
        args.json.write_text(json.dumps(entries, indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
