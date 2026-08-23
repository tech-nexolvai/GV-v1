"""`python -m workflow.worker` — run the package review stages (#415, F3.1).

Deliberately thin, for the reason `workflow/dispatcher.py` gives: the logic is in
`workflow/entrypoints.py` where a test can reach it without starting a process.

This is the half that does the work. The dispatcher only hands packages to the engine; without a worker
the engine accepts them and nothing runs them, so both processes are needed for a package to move.
"""

from __future__ import annotations

import logging
import sys

from workflow.entrypoints import EXIT_MISCONFIGURED, run_worker, settings_or_none


def main() -> int:
    """Read settings, build the worker, and block until it stops."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = settings_or_none()
    if settings is None:
        return EXIT_MISCONFIGURED

    from sqlalchemy import create_engine

    from app.db.session import session_factory

    factory = session_factory(create_engine(settings.database_url))
    return run_worker(settings, factory=factory)


if __name__ == "__main__":  # pragma: no cover - exercised by running the module
    sys.exit(main())
