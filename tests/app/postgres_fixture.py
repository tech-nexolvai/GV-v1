"""PostgreSQL fixture loaded only by persistence integration tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    """The migration config, found from this file rather than from the working directory.

    `Config("alembic.ini")` resolves against the current directory, so twenty-four test modules only
    passed when pytest happened to be started from the repository root. Run from anywhere else they failed
    with `No \'script_location\' key found in configuration` — a message that points at the config file
    rather than at the caller, which is why it survived so long.

    Pointing at the absolute path is enough on its own: `alembic.ini` sets `script_location = %(here)s/alembic`,
    so `%(here)s` resolves from the file, not the caller. Verified rather than assumed.

    One helper rather than the same two lines in every module, so the next test that needs a migrated
    database cannot reintroduce the working-directory assumption by copying its neighbour.
    """
    return Config(str(REPO_ROOT / "alembic.ini"))


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    """Yield an engine confined to a temporary schema in the configured database."""

    raw_url = os.environ.get("DATABASE_URL")
    if raw_url is None:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration tests locally")
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        pytest.fail("DATABASE_URL must use PostgreSQL for persistence integration tests")

    schema = f"gv_test_{uuid4().hex}"
    admin_engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        schema_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
        engine = create_engine(schema_url)
        try:
            yield engine
        finally:
            engine.dispose()
            with admin_engine.connect() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        admin_engine.dispose()
