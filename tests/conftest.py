"""Shared test configuration.

The PostgreSQL fixture lives in `tests/app/postgres_fixture.py` (added by #191). Two test packages
now need it — `tests/app/` and `tests/eval/` — and only the first can load it by bare name, because
a plugin named that way resolves relative to the importing module's directory.

Registering it here instead makes it available everywhere, without moving the file that #191 owns
and without duplicating a fixture. `tests.app.postgres_fixture` as a dotted path does not work:
several test directories have no `__init__.py`, so `tests.app` is not an importable package on CI
even though it resolves locally — which is exactly the difference that broke this on the first push.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

pytest_plugins = ("postgres_fixture",)
