"""Database engine, session factory and transaction boundary.

The verdict package never imports this module. Persistence stores deterministic results;
it does not participate in deciding them.

Source: issue #191 and ``docs/DESIGN_PLATFORM.md`` section 3.1.
Verification: ``tests/app/test_db_conventions.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


class DatabaseSettings(Protocol):
    """The narrow settings contract required by the persistence layer."""

    database_url: str


@dataclass(frozen=True, slots=True)
class EnvironmentDatabaseSettings:
    """Database settings loaded by platform entry points such as Alembic."""

    database_url: str


def settings_from_environment() -> EnvironmentDatabaseSettings:
    """Load the required database URL without inventing a development default."""

    database_url = os.environ.get("DATABASE_URL")
    if database_url is None or not database_url.strip():
        raise RuntimeError("DATABASE_URL is required")
    return EnvironmentDatabaseSettings(database_url=database_url)


def engine_from_settings(settings: DatabaseSettings, **kwargs: object) -> Engine:
    """Build an engine from an explicit database URL."""

    if not settings.database_url.strip():
        raise ValueError("database_url must not be empty")
    return create_engine(settings.database_url, **kwargs)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create the project's session factory without opening a session."""

    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def unit_of_work(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit one unit of work, rolling it back when its body raises."""

    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
