"""Isolated PostgreSQL schemas for persistence integration tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    """Yield an engine confined to a temporary schema in the configured PostgreSQL database."""

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
