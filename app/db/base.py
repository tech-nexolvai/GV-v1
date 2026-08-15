"""Shared SQLAlchemy conventions fixed before the first business table.

Source: ``docs/DESIGN_PLATFORM.md`` section 3.1 and issue #191.
Verification: ``tests/app/test_db_conventions.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, MetaData, Uuid, event
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base carrying the project's permanent constraint names."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Immutable:
    """Marker mixin for tables whose UPDATE and DELETE rights C1.12 will revoke."""


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware timestamp that rejects naive values before database binding."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Normalise aware values to UTC and reject timestamps with no timezone."""

        del dialect
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("database timestamps must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Return database timestamps with explicit UTC identity."""

        del dialect
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def utc_now() -> datetime:
    """Return an aware UTC timestamp for application-generated defaults."""

    return datetime.now(UTC)


class TimestampedUUID:
    """Application UUID identity and UTC creation time shared by persisted models."""

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


@event.listens_for(Base, "init", propagate=True)
def _assign_application_defaults(
    target: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    """Assign identity before flush; SQLAlchemy column defaults otherwise run at INSERT."""

    del args
    if not isinstance(target, TimestampedUUID):
        return
    kwargs.setdefault("id", uuid4())
    kwargs.setdefault("created_at", utc_now())
