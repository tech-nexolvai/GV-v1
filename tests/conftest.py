"""Shared test configuration.

The PostgreSQL fixture lives in `tests/app/postgres_fixture.py` (added by #191). Registering it here
makes it available everywhere, reusing the file #191 owns rather than moving or duplicating it.

**One name: `tests.app.postgres_fixture` (#1293).** This file used to register it by the bare name
`postgres_fixture` (with `tests/app` put on `sys.path`), while over two hundred test modules register
`tests.app.postgres_fixture`. Python then loads the file twice, as two modules, and pytest takes them for
two plugins: two `_migrated_schema` fixtures, so tests collected before the second registration used one
session schema and the rest another, and both were alive at once. A test that read the catalog without a
schema filter then saw two copies (`pytest tests/app tests/db/test_evidence_models.py` failed, even on one
process). Under the dotted name every registration is the same plugin, and pytest ignores the repeats.
`tests/` and `tests/app/` are packages, so the dotted name imports wherever pytest starts.

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
finishes. Without xdist there is no worker id and nothing changes. The worker id is xdist's own
`config.workerinput`, not the inherited `PYTEST_XDIST_WORKER` variable, so a pytest that a test starts is a
plain run on the worker's database rather than a worker of its own (#1293). The rules on which names may
be created or dropped are in `tests/app/postgres_fixture.py` (`worker_database_url`).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from sqlalchemy.engine import URL

pytest_plugins: tuple[str, ...] = ()

if importlib.util.find_spec("sqlalchemy") is not None:
    pytest_plugins = ("tests.app.postgres_fixture",)

#: What a worker keeps between configure and unconfigure: `DATABASE_URL` as given, its URL, its own.
_WORKER_DATABASE = pytest.StashKey[tuple[str, "URL", "URL"]]()


def pytest_configure(config: pytest.Config) -> None:
    """Under xdist, give this worker its own database and point `DATABASE_URL` at it (#1289)."""
    if not pytest_plugins:
        return  # no SQLAlchemy: exactly as before
    from tests.app.postgres_fixture import (
        create_worker_database,
        worker_database_for,
        xdist_worker_id,
    )

    worker = xdist_worker_id(config)
    if worker is None:
        return  # a single-process run, or a pytest a test started: exactly as before
    try:
        urls = worker_database_for(os.environ, worker)
    except ValueError as refused:
        raise pytest.UsageError(f"per-worker test database: {refused}") from refused
    if urls is None:
        return
    base, own = urls
    try:
        create_worker_database(base, own)
    except TimeoutError as stuck:
        raise pytest.UsageError(f"per-worker test database: {stuck}") from stuck
    config.stash[_WORKER_DATABASE] = (os.environ["DATABASE_URL"], base, own)
    os.environ["DATABASE_URL"] = own.render_as_string(hide_password=False)


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Drop this worker's database once its tests are done.

    **Here, not in `pytest_unconfigure`.** xdist tells the main process a worker is finished at the end
    of the worker's `pytest_sessionfinish`, after this hook, and the main process may end the worker
    soon after. A drop still running in `pytest_unconfigure` was cut off locally, which left an
    "invalid" half-dropped database behind (the next run's create removes such a leftover anyway).
    """
    saved = session.config.stash.get(_WORKER_DATABASE, None)
    if saved is None:
        return
    from tests.app.postgres_fixture import drop_worker_database

    _given, base, own = saved
    drop_worker_database(base, own)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Put `DATABASE_URL` back as it was."""
    saved = config.stash.get(_WORKER_DATABASE, None)
    if saved is not None:
        os.environ["DATABASE_URL"] = saved[0]


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


@pytest.fixture(autouse=True)
def _forget_model_limiters() -> Iterator[None]:
    """Each test starts with fresh AI model limiters (#1874).

    `app.review.assistant.rate_limits` keeps one limiter per model for the whole process on
    purpose (the provider's limits are the server's); a test's calls would otherwise count against
    the next test's minute. Cleared only if the module is already loaded.
    """
    module = sys.modules.get("app.review.assistant.rate_limits")
    if module is not None:
        module.reset_limiters()
    yield
    module = sys.modules.get("app.review.assistant.rate_limits")
    if module is not None:
        module.reset_limiters()
