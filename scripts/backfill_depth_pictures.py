"""Cut the missing picture of every depth reading made before #1389. One-time job.

"Read depths again" (#1308) and a run that stopped after matching never ran the crop stage, so
their depth readings have no picture, and the simple screen cannot offer Confirm on them. This cuts
each one with the crop stage's own code, exactly as the crop stage would have
(`workflow/depth_pictures.py`). No reading, finding or decision changes; no model is called.

Run it with the same environment as the worker (`DATABASE_URL`, `GV_DEV_STORAGE`, the reader
settings `scripts/drain_outbox.py` reads): the stages are built the same way, so the same pixels
come out. A reading that cannot be cut keeps no picture, and the output says why.

Usage:

    python scripts/backfill_depth_pictures.py --dry-run     # counts only, stores and writes nothing
    python scripts/backfill_depth_pictures.py               # cuts and records the pictures
    python scripts/backfill_depth_pictures.py --revision <package-revision-uuid>

Re-running is safe: a reading with a picture is skipped, so a second run cuts nothing.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Sequence
from uuid import UUID

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="count what would be cut, and store nothing"
    )
    parser.add_argument("--revision", help="only this package revision's depth readings")
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.config import Settings
    from scripts.drain_outbox import _stages
    from workflow.depth_pictures import cut_depth_pictures
    from workflow.stages import DatabaseStages

    settings = Settings()  # type: ignore[call-arg]
    stages = _stages()
    if not isinstance(stages, DatabaseStages) or stages.artifact_store is None:
        print("the worker's stages have no artifact store; no picture can be cut", file=sys.stderr)
        return 2

    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        outcome = cut_depth_pictures(
            session,
            stages,
            dry_run=args.dry_run,
            package_revision_id=None if args.revision is None else UUID(args.revision),
        )
        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    print(json.dumps(outcome.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
