"""Re-check the "shows GV markup" flag of evidence crops cut before #1078 (#1141). One-time job.

Crops cut before #1078 asked only about GV marks baked into the vendor's drawing, so many say "no
GV markup" over GV's own note, and the screen does not offer the vendor-only view there (#952).
This cuts each stored crop's pixels again, asks the corrected check, and records a re-check row
only where the answer differs (`workflow/crop_mark_rechecks.py`). No crop, reading, finding or
decision changes; no model is called.

Run it with the same environment as the worker that cut the crops (`DATABASE_URL`,
`GV_DEV_STORAGE`, the reader settings `scripts/drain_outbox.py` reads): the stages are built the
same way, so the same pixels come back. A crop whose pixels do not come back byte-identical is left
alone and counted.

Usage:

    python scripts/recheck_crop_marks.py --run-by <who> --dry-run     # counts only, writes nothing
    python scripts/recheck_crop_marks.py --run-by <who>               # writes the re-check rows
    python scripts/recheck_crop_marks.py --run-by <who> --revision <package-revision-uuid>

Re-running is safe: crops already re-checked are skipped, so a second run writes nothing.
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
    parser.add_argument("--run-by", required=True, help="who is running this, for the record")
    parser.add_argument(
        "--dry-run", action="store_true", help="count what would change and write nothing"
    )
    parser.add_argument("--revision", help="only this package revision's crops")
    args = parser.parse_args(argv)
    if not args.run_by.strip():
        parser.error("--run-by must name who is running this")

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.config import Settings
    from scripts.drain_outbox import _stages
    from workflow.crop_mark_rechecks import recheck_crop_marks
    from workflow.stages import DatabaseStages

    settings = Settings()  # type: ignore[call-arg]
    stages = _stages()
    if not isinstance(stages, DatabaseStages) or stages.artifact_store is None:
        print(
            "the worker's stages have no artifact store; nothing can be re-checked", file=sys.stderr
        )
        return 2

    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        outcome = recheck_crop_marks(
            session,
            stages,
            run_by=args.run_by,
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
