"""Queue the job values reader (#1409) for sets already read. Starts nothing itself.

A set read before `GV_JOB_VALUES_READER` was switched on has no job values. This queues one
`read_job_values` request per named revision on the outbox, in one transaction; the worker
(`scripts/drain_outbox.py`, run with the switch on) reads that revision's job values only, never
the set again, and pays only for questions with no stored answer. A revision whose values were
already read for the same request reads nothing again.

Usage (the worker's own environment: `DATABASE_URL`):

    python scripts/job_values_queue.py --package <package-uuid> [--package ...]
    python scripts/job_values_queue.py --revision <package-revision-uuid> [--revision ...]

`--package` queues its newest revision. Exit codes: 0 queued, 2 refused (nothing queued).
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from collections.abc import Sequence
from uuid import UUID

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

REFUSED = 2


def revisions_to_queue(
    session: object, packages: Sequence[UUID], revisions: Sequence[UUID]
) -> list[UUID]:
    """The newest revision of each package, then each revision named; refused if one is unknown."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models.package import PackageRevision

    assert isinstance(session, Session)
    out: list[UUID] = []
    for package_id in packages:
        newest = session.scalar(
            select(PackageRevision.id)
            .where(PackageRevision.package_id == package_id)
            .order_by(PackageRevision.revision_number.desc())
            .limit(1)
        )
        if newest is None:
            raise LookupError(f"no package {package_id}")
        out.append(newest)
    for revision_id in revisions:
        if session.get(PackageRevision, revision_id) is None:
            raise LookupError(f"no package revision {revision_id}")
        out.append(revision_id)
    return list(dict.fromkeys(out))


def queue(session: object, revision_ids: Sequence[UUID]) -> list[UUID]:
    """One `read_job_values` outbox request per revision, in the caller's transaction."""
    from sqlalchemy.orm import Session

    from workflow.job_values import READ_JOB_VALUES_WORKFLOW
    from workflow.outbox import enqueue

    assert isinstance(session, Session)
    return [
        enqueue(
            session,
            workflow=READ_JOB_VALUES_WORKFLOW,
            payload={"package_revision_id": str(revision_id)},
        )
        for revision_id in revision_ids
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--package", action="append", default=[], help="a package's newest revision"
    )
    parser.add_argument("--revision", action="append", default=[], help="a package revision")
    args = parser.parse_args(argv)
    if not args.package and not args.revision:
        print("refused: name at least one --package or --revision", file=sys.stderr)
        return REFUSED

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.config import Settings

    settings = Settings()  # type: ignore[call-arg]
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        try:
            wanted = revisions_to_queue(
                session,
                [UUID(value) for value in args.package],
                [UUID(value) for value in args.revision],
            )
        except (LookupError, ValueError) as error:
            print(f"refused: {error}", file=sys.stderr)
            return REFUSED
        queued = queue(session, wanted)
        session.commit()
    for revision_id, entry in zip(wanted, queued, strict=True):
        print(f"queued job values for revision {revision_id} (outbox {entry})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
