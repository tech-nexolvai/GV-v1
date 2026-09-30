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
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

pytest_plugins: tuple[str, ...] = ()

if importlib.util.find_spec("sqlalchemy") is not None:
    pytest_plugins = ("postgres_fixture",)


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
