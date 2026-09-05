"""Do the work the API accepted but may not perform itself.

`POST .../checks` writes an outbox row and commits. It cannot run the checks: every import under
`app/api/` is walked by `tests/api/test_no_heavy_work.py`, and `workflow/stages.py` reaches
`extraction.reader` since the OCR route landed — so the control plane may not import the thing that
does the work. That separation is `DESIGN_PLATFORM.md` §4.2, not an inconvenience.

**This is the stand-in for a registered worker, and it says so.** Phase 6 puts a Hatchet workflow
behind the outbox; until then the row would sit there looking accepted while nothing ran, which is
worse than not offering the button. This drains it in one pass, in a process that is allowed to
import extraction.

It reuses `workflow/outbox.py:dispatch_committed` rather than polling itself, which is the whole
point: `FOR UPDATE SKIP LOCKED`, attempt counting, the increment-then-start-then-stamp ordering and
the at-least-once guarantee are already written and tested there. A second poll loop here would be a
second set of those decisions, and the two would drift.

Usage:

    python scripts/drain_outbox.py            # one pass
    python scripts/drain_outbox.py --watch    # keep draining, for a demo

Verification: `tests/test_drain_outbox.py`
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time
from collections.abc import Mapping
from uuid import UUID

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

#: How long `--watch` sleeps between passes. Two seconds matches `GV_OUTBOX_POLL_SECONDS`'s default,
#: which `.env.example` describes as "the visible wait between a package being accepted and its
#: workflow starting, and somebody is watching".
WATCH_SECONDS = 2.0


def _run_checks(
    session: object,
    package_revision_id: UUID,
    discriminators: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    """Run the checks for one revision, with what the reviewer supplied.

    **Discriminators come from the request rather than the database**, because that is what they are:
    a statement about how to read this package on this run. Without them `CT-WIDTH-001` and
    `CAB-FILLER-001` abstain with REVIEW_REQUIRED however complete the measurements are — the
    resolver cannot choose a variant nobody stated, and refuses to guess one.
    """
    from workflow.measurements import operands_for
    from workflow.stages import DatabaseStages

    operands = operands_for(session, package_revision_id)  # type: ignore[arg-type]
    stages = DatabaseStages(operands=operands, discriminators=dict(discriminators or {}))
    return stages.run_checks(session, package_revision_id)  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="keep draining rather than one pass")
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker

    from app.config import Settings
    from app.db.session import session_factory
    from workflow.outbox import dispatch_committed

    settings = Settings()  # type: ignore[call-arg]
    factory: sessionmaker[Session] = session_factory(create_engine(settings.database_url))

    def _start(*, workflow: str, payload: Mapping[str, object], idempotency_key: str) -> None:
        """The starter `dispatch_committed` calls, doing the work in its own session.

        **Its own session on purpose.** `dispatch_committed` holds the outbox row's transaction, and
        the ordering it documents — increment attempts, start, then stamp `dispatched_at` — only
        means anything if starting is separable from that bookkeeping. Writing findings into the
        dispatcher's transaction would tie a rolled-back dispatch to discarded findings, which is a
        different failure mode than the one that module reasoned about.

        A repeat of the same key must be a no-op, and `run_checks` already is: it supersedes prior
        runs for the revision and writes a fresh set, so running it twice leaves one live set rather
        than two.
        """
        if workflow != "run_checks":
            print(f"  no consumer for {workflow!r} — leaving it for the worker that owns it")
            raise NotImplementedError(f"no local consumer for {workflow!r}")

        revision_id = UUID(str(payload["package_revision_id"]))
        raw = payload.get("discriminators")
        # Narrowed rather than cast: the payload is JSON from a database row, so its shape is a claim
        # this process should check rather than assume. A malformed entry runs the checks without a
        # discriminator, which abstains — visible — instead of raising here and stalling the queue.
        stated = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        with factory() as session:
            result = _run_checks(session, revision_id, stated)
            session.commit()
        print(f"  run_checks {revision_id}: {dict(result)}")

    passes = 0
    while True:
        try:
            started = dispatch_committed(factory, _start)
        except Exception as failed:  # noqa: BLE001 - reported, and the loop continues
            print(f"dispatch reported failures: {failed}", file=sys.stderr)
            started = 0
        passes += 1
        if started:
            print(f"dispatched {started} row(s)")
        if not args.watch:
            if not started:
                print("nothing to dispatch")
            return 0
        time.sleep(WATCH_SECONDS)


if __name__ == "__main__":  # pragma: no cover - exercised through main() in tests
    raise SystemExit(main())
