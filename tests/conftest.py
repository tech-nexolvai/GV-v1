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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

pytest_plugins: tuple[str, ...] = ()

if importlib.util.find_spec("sqlalchemy") is not None:
    pytest_plugins = ("postgres_fixture",)
