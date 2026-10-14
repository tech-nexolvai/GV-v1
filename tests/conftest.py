"""Shared test configuration.

The PostgreSQL fixture lives in `tests/app/postgres_fixture.py` (added by #191). Two test packages
now need it — `tests/app/` and `tests/eval/` — and only the first can load it by bare name, because
a plugin named that way resolves relative to the importing module's directory.

Registering it here makes it available everywhere, reusing the file #191 owns rather than moving or
duplicating it. `tests.app.postgres_fixture` as a dotted path does not work: several test
directories have no `__init__.py`, so `tests.app` is not importable on CI even though it resolves
locally.

**Why the registration is conditional.** A root conftest is imported by *every* pytest invocation,
including the `safety guards` CI job, which installs only the `dev` extra on purpose — it runs the
licence, isolation and semgrep guards and has no reason to pull in a database driver. Registering
the fixture unconditionally made that job fail on `import sqlalchemy`, in a conftest, before it
reached the guard it was there to run.

The fixture is unusable without SQLAlchemy anyway, so its absence is not a problem to report — it is
simply a context where these tests do not apply.

**One database per pytest-xdist worker (#1289).** CI runs each shard on four workers. Before any test is
collected, each worker creates its own empty database beside `DATABASE_URL`'s (`gv_test` becomes
`gv_test_gw0`) and points `DATABASE_URL` at it, so every fixture, every module that reads the variable and
every subprocess a test starts sees the worker's database and nothing else. The worker drops it when it
finishes. Without xdist there is no worker id and nothing changes. The rules on which names may be
created or dropped are in `tests/app/postgres_fixture.py` (`worker_database_url`).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from sqlalchemy.engine import URL

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

pytest_plugins: tuple[str, ...] = ()

if importlib.util.find_spec("sqlalchemy") is not None:
    pytest_plugins = ("postgres_fixture",)

#: What a worker keeps between configure and unconfigure: `DATABASE_URL` as given, its URL, its own.
_WORKER_DATABASE = pytest.StashKey[tuple[str, "URL", "URL"]]()


def pytest_configure(config: pytest.Config) -> None:
    """Under xdist, give this worker its own database and point `DATABASE_URL` at it (#1289)."""
    if not pytest_plugins or "PYTEST_XDIST_WORKER" not in os.environ:
        return  # no SQLAlchemy, or a single-process run: exactly as before
    from postgres_fixture import create_worker_database, worker_database_for

    try:
        urls = worker_database_for(os.environ)
    except ValueError as refused:
        raise pytest.UsageError(f"per-worker test database: {refused}") from refused
    if urls is None:
        return
    base, own = urls
    create_worker_database(base, own)
    config.stash[_WORKER_DATABASE] = (os.environ["DATABASE_URL"], base, own)
    os.environ["DATABASE_URL"] = own.render_as_string(hide_password=False)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Drop this worker's database and put `DATABASE_URL` back as it was."""
    saved = config.stash.get(_WORKER_DATABASE, None)
    if saved is None:
        return
    from postgres_fixture import drop_worker_database

    given, base, own = saved
    os.environ["DATABASE_URL"] = given
    drop_worker_database(base, own)


@pytest.fixture(autouse=True)
def _forget_inference_profile_routes() -> Iterator[None]:
    """Each test starts with no learned Bedrock inference-profile routes (#702).

    `extraction.models.nova.PROFILE_ROUTES` is process-wide on purpose — production builds a new
    adapter per crop — so a test that teaches it a route would change how the next test's calls are
    routed. Cleared only if the module is already loaded: importing it here would put the model layer
    into every test's `sys.modules`, including the isolation tests that check it is absent.
    """
    module = sys.modules.get("extraction.models.nova")
    if module is not None:
        module.PROFILE_ROUTES.forget()
    yield
    module = sys.modules.get("extraction.models.nova")
    if module is not None:
        module.PROFILE_ROUTES.forget()
