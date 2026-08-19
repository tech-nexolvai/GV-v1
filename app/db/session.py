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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

#: PostgreSQL's SQLSTATE for a unique violation.
UNIQUE_VIOLATION = "23505"


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


def is_unique_violation(error: IntegrityError, *constraints: str) -> bool:
    """Whether this integrity error is a unique violation on one of the named constraints.

    Two things have to hold: the SQLSTATE says unique violation, and the constraint the *database*
    named is one the caller expects. If the driver cannot name the constraint, the answer is no.
    Guessing would let some unrelated unique constraint be reported as "already done", and that is the
    failure that loses a write rather than the one that logs an error.

    Constraints are matched as substrings of the reported name, so a caller can name the column
    (``document_id``) rather than the installed constraint name. That keeps working if a table is ever
    rebuilt under PostgreSQL's own default name, while still being specific.

    ``workflow/idempotency.py`` has its own copy of this check for ``task_runs``, written before this
    one. It is deliberately not changed to import this: ``docs/DESIGN_PLATFORM.md`` §2 lets
    ``app/api/`` reach ``workflow/`` for ``enqueue`` only, so the helper had to live on this side of
    that boundary. Consolidating the two is a tidy-up, not a fix.
    """

    original = getattr(error, "orig", None)
    diagnostic = getattr(original, "diag", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(diagnostic, "sqlstate", None)
    if sqlstate != UNIQUE_VIOLATION:
        return False
    name = getattr(diagnostic, "constraint_name", None)
    if not isinstance(name, str):
        return False
    return any(constraint in name for constraint in constraints)


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
