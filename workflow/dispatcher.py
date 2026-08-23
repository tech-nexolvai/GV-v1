"""`python -m workflow.dispatcher` — drain the outbox, forever (#415, F3.1).

Deliberately thin. Everything it does lives in `workflow/entrypoints.py`, which is testable without a
process; this file exists so there is something to put in a systemd unit or a Compose service.

Run it with the same environment as the API: it needs `GV_DATABASE_URL` to read the outbox and
`GV_HATCHET_TOKEN` to start what it finds.
"""

from __future__ import annotations

import logging
import sys

from workflow.entrypoints import (
    EXIT_MISCONFIGURED,
    hatchet_starter,
    run_dispatcher,
    settings_or_none,
)


def main() -> int:
    """Read settings, build the pieces, and poll until stopped.

    The token is checked here rather than left to the first dispatch. Without it the process would come
    up, log "0 rows dispatched" on a queue that is not empty, and look exactly like a healthy dispatcher
    with nothing to do — which is the failure this story was written to end.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = settings_or_none()
    if settings is None:
        return EXIT_MISCONFIGURED

    if not settings.hatchet_token:
        logging.getLogger("gv.workflow.dispatcher").error(
            "cannot start the dispatcher: GV_HATCHET_TOKEN is empty. Without it every poll would "
            "fail to start the rows it found, so the queue would grow while the process looked well."
        )
        return EXIT_MISCONFIGURED

    from sqlalchemy import create_engine

    from app.db.session import session_factory

    factory = session_factory(create_engine(settings.database_url))
    return run_dispatcher(settings, factory=factory, start=hatchet_starter(settings))


if __name__ == "__main__":  # pragma: no cover - exercised by running the module
    sys.exit(main())
