"""Database foundation conventions and transaction behavior for issue #191."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import (
    Column,
    Engine,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import NAMING_CONVENTION, Base, TimestampedUUID, UTCDateTime
from app.db.session import engine_from_settings, session_factory, unit_of_work

pytest_plugins = ("tests.app.postgres_fixture",)

EXPECTED_NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _record_model(table_name: str) -> type[Base]:
    """Create one temporary mapped model without polluting migration metadata afterward."""

    class Record(TimestampedUUID, Base):
        __module__ = f"{__name__}.{table_name}"
        __tablename__ = table_name

        name: Mapped[str] = mapped_column(String, unique=True)
        observed_at: Mapped[datetime] = mapped_column(UTCDateTime())

    return Record


def test_naming_convention_matches_the_fixed_design_key_for_key() -> None:
    """Input is the design dictionary; every migration name must remain byte-for-byte stable."""

    assert NAMING_CONVENTION == EXPECTED_NAMING_CONVENTION
    assert dict(Base.metadata.naming_convention or {}) == EXPECTED_NAMING_CONVENTION


def test_unnamed_unique_constraint_receives_the_stable_convention_name() -> None:
    """An unnamed uniqueness rule on widgets.code becomes uq_widgets_code."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    table = Table(
        "widgets",
        metadata,
        Column("code", String),
        UniqueConstraint("code"),
    )
    constraint = next(item for item in table.constraints if isinstance(item, UniqueConstraint))

    assert constraint.name == "uq_widgets_code"


def test_uuid_and_utc_timestamp_exist_before_session_flush() -> None:
    """A newly constructed object already has identity and an aware UTC creation time."""

    Record = _record_model("test_identity_records")
    try:
        record = Record(name="cabinet", observed_at=datetime(2026, 8, 15, tzinfo=UTC))

        assert isinstance(record.id, UUID)
        assert record.created_at.tzinfo is UTC
    finally:
        Base.metadata.remove(Record.__table__)


def test_naive_datetime_is_rejected_before_it_reaches_database_storage(
    postgres_engine: Engine,
) -> None:
    """Input 2026-08-15 09:00 without timezone raises instead of being assumed UTC."""

    Record = _record_model("test_naive_timestamp_records")
    try:
        Record.__table__.create(postgres_engine)
        factory = session_factory(postgres_engine)
        with (
            pytest.raises(StatementError, match="timezone-aware"),
            unit_of_work(factory) as session,
        ):
            session.add(
                Record(
                    name="cabinet",
                    observed_at=datetime(2026, 8, 15, 9, 0),  # noqa: DTZ001
                )
            )
    finally:
        Base.metadata.remove(Record.__table__)


def test_aware_timestamp_round_trips_as_utc(postgres_engine: Engine) -> None:
    """An aware timestamp is accepted and returned with explicit UTC identity."""

    Record = _record_model("test_aware_timestamp_records")
    try:
        Record.__table__.create(postgres_engine)
        factory = session_factory(postgres_engine)
        record_id: UUID
        with unit_of_work(factory) as session:
            record = Record(name="cabinet", observed_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC))
            record_id = record.id
            session.add(record)
        with unit_of_work(factory) as session:
            restored = session.get(Record, record_id)
            assert restored is not None
            assert restored.observed_at.tzinfo is UTC
    finally:
        Base.metadata.remove(Record.__table__)


def test_unit_of_work_commits_successful_changes(postgres_engine: Engine) -> None:
    """Input one record with no exception; expected persisted count is one."""

    Record = _record_model("test_commit_records")
    try:
        Record.__table__.create(postgres_engine)
        factory = session_factory(postgres_engine)
        with unit_of_work(factory) as session:
            session.add(Record(name="kept", observed_at=datetime.now(UTC)))
        with unit_of_work(factory) as session:
            assert session.scalar(select(func.count()).select_from(Record)) == 1
    finally:
        Base.metadata.remove(Record.__table__)


def test_unit_of_work_rolls_back_on_exception(postgres_engine: Engine) -> None:
    """Input one record followed by an exception; expected persisted count is zero."""

    Record = _record_model("test_rollback_records")
    try:
        Record.__table__.create(postgres_engine)
        factory = session_factory(postgres_engine)
        with (
            pytest.raises(RuntimeError, match="stop transaction"),
            unit_of_work(factory) as session,
        ):
            session.add(Record(name="discarded", observed_at=datetime.now(UTC)))
            session.flush()
            raise RuntimeError("stop transaction")
        with unit_of_work(factory) as session:
            assert session.scalar(select(func.count()).select_from(Record)) == 0
    finally:
        Base.metadata.remove(Record.__table__)


def test_engine_uses_only_the_narrow_database_settings_contract(
    postgres_engine: Engine,
) -> None:
    """Any future Settings object works if it supplies a non-empty database_url."""

    engine = engine_from_settings(
        SimpleNamespace(database_url=postgres_engine.url.render_as_string(hide_password=False))
    )
    try:
        assert engine.url.drivername.startswith("postgresql")
    finally:
        engine.dispose()


def test_empty_database_url_is_rejected_instead_of_defaulted() -> None:
    """Missing configuration raises; it never silently selects a local database."""

    with pytest.raises(ValueError, match="database_url"):
        engine_from_settings(SimpleNamespace(database_url=" "))
