"""The audit trail must be complete, attributable and impossible to edit.

Source: issue #255; backend proposal §11, §12.
Verification: ``app/audit/events.py``.

The failure worth testing for is not a wrong audit row — it is a **missing** one. An absent record
reads as "nothing happened", and nobody re-checks a log they believe is complete. So most of what
follows is about categories that emit nothing, actors that are not named, and an audit write that
fails without taking the operation down with it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.audit.events import SYSTEM_ACTOR, AuditCategory, AuditEvent, emit
from app.db.base import Base, Immutable
from app.db.session import session_factory, unit_of_work

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def sessions(postgres_engine: object) -> sessionmaker[Session]:
    Base.metadata.create_all(postgres_engine)  # type: ignore[arg-type]
    return session_factory(postgres_engine)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Every category backend §11 names must actually emit
# ---------------------------------------------------------------------------


def test_the_six_audited_categories_are_all_declared() -> None:
    """Backend §11 lists six. A category nobody declared is one no report counts."""
    assert {member.value for member in AuditCategory} == {
        "STATE_CHANGE",
        "RULE_PUBLICATION",
        "FINDING",
        "REVIEW_ACTION",
        "EXCEPTION",
        "ARTIFACT_DOWNLOAD",
    }


@pytest.mark.parametrize("category", list(AuditCategory), ids=lambda c: c.value)
def test_every_category_emits_a_readable_event(
    category: AuditCategory, sessions: sessionmaker[Session]
) -> None:
    """Enumerated over the enum, so adding a category without a writer fails here.

    Input: one emit per category. Outcome: a row that can be read back with its actor and target.
    """
    target = uuid4()
    with unit_of_work(sessions) as session:
        emit(
            session,
            category=category,
            actor="anant",
            target_id=target,
            target_type="packages",
        )

    with unit_of_work(sessions) as session:
        stored = session.scalars(select(AuditEvent).where(AuditEvent.target_id == target)).one()
        assert stored.category == category.value
        assert stored.actor == "anant"
        assert stored.target_type == "packages"


# ---------------------------------------------------------------------------
# An unaudited change must not happen
# ---------------------------------------------------------------------------


def test_the_event_is_written_in_the_callers_transaction(
    sessions: sessionmaker[Session],
) -> None:
    """Input: emit, then the surrounding transaction rolls back. Outcome: no audit row.

    This is the property that makes the trail trustworthy in the other direction too: the row and
    the change it describes commit together, so the log never describes something that did not
    happen.
    """
    target = uuid4()
    with (
        pytest.raises(RuntimeError, match="the operation failed"),
        unit_of_work(sessions) as session,
    ):
        emit(
            session,
            category=AuditCategory.STATE_CHANGE,
            actor="anant",
            target_id=target,
            target_type="packages",
        )
        raise RuntimeError("the operation failed after auditing")

    with unit_of_work(sessions) as session:
        assert session.scalars(select(AuditEvent).where(AuditEvent.target_id == target)).all() == []


def test_emit_does_not_commit_on_its_own(sessions: sessionmaker[Session]) -> None:
    """A writer that committed would leave a trail describing rolled-back work."""
    target = uuid4()
    with unit_of_work(sessions) as session:
        emit(
            session,
            category=AuditCategory.FINDING,
            actor=SYSTEM_ACTOR,
            target_id=target,
            target_type="findings",
        )
        # Still inside the caller's transaction: another session must not see it yet.
        with unit_of_work(sessions) as other:
            assert (
                other.scalars(select(AuditEvent).where(AuditEvent.target_id == target)).all() == []
            )


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def test_a_system_action_is_still_a_named_actor(sessions: sessionmaker[Session]) -> None:
    """ "who did this?" answered with a blank is indistinguishable from an actor that was lost."""
    target = uuid4()
    with unit_of_work(sessions) as session:
        event = emit(
            session,
            category=AuditCategory.RULE_PUBLICATION,
            actor=SYSTEM_ACTOR,
            target_id=target,
            target_type="rule_snapshots",
        )
        assert event.actor == SYSTEM_ACTOR


@pytest.mark.parametrize("actor", ["", "   "])
def test_an_unattributed_event_is_refused(actor: str, sessions: sessionmaker[Session]) -> None:
    with unit_of_work(sessions) as session, pytest.raises(ValueError, match="must name its actor"):
        emit(
            session,
            category=AuditCategory.EXCEPTION,
            actor=actor,
            target_id=uuid4(),
            target_type="exceptions",
        )


def test_an_event_must_say_what_its_target_is(sessions: sessionmaker[Session]) -> None:
    """`target_id` alone is unfollowable: six categories point at six different tables."""
    with unit_of_work(sessions) as session, pytest.raises(ValueError, match="what kind of thing"):
        emit(
            session,
            category=AuditCategory.ARTIFACT_DOWNLOAD,
            actor="anant",
            target_id=uuid4(),
            target_type="",
        )


def test_the_trace_id_is_recorded_when_one_is_supplied(
    sessions: sessionmaker[Session],
) -> None:
    """So an event joins the request that caused it."""
    target = uuid4()
    with unit_of_work(sessions) as session:
        event = emit(
            session,
            category=AuditCategory.REVIEW_ACTION,
            actor="anant",
            target_id=target,
            target_type="review_actions",
            trace_id="a" * 32,
        )
        assert event.trace_id == "a" * 32


def test_an_absent_trace_is_null_rather_than_a_zero_id(
    sessions: sessionmaker[Session],
) -> None:
    """Outside a span there is nothing to look up, and a zero id looks like something you could."""
    with unit_of_work(sessions) as session:
        event = emit(
            session,
            category=AuditCategory.STATE_CHANGE,
            actor=SYSTEM_ACTOR,
            target_id=uuid4(),
            target_type="packages",
        )
        # Deterministic: these tests run outside any span, so the trace is absent rather than
        # "absent or a valid id" — an assertion that accepts both could not fail.
        assert event.trace_id is None


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


def test_the_table_is_marked_immutable() -> None:
    """C1.12 revokes UPDATE and DELETE on every table carrying the mixin.

    Asserted on the class rather than by attempting an UPDATE: the revoke is granted to the
    application role in production, and the test database connects as the owner. What this can
    prove is that the table is on the list C1.12 acts upon — and that is the thing a new model can
    silently fail to be.
    """
    assert issubclass(AuditEvent, Immutable)


def test_the_category_column_rejects_a_value_outside_the_enum(
    sessions: sessionmaker[Session],
) -> None:
    """The check constraint is the database's own copy of the closed set."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), unit_of_work(sessions) as session:
        session.add(
            AuditEvent(
                category="SOMETHING_ELSE",
                actor="anant",
                target_id=uuid4(),
                target_type="packages",
            )
        )


def test_the_target_index_exists_so_the_common_question_is_answerable(
    postgres_engine: Engine, sessions: sessionmaker[Session]
) -> None:
    """ "what happened to this package, in order?" is the query the trail exists to serve.

    Takes the engine from the fixture rather than reading it out of the sessionmaker's keyword
    arguments, which is an implementation detail rather than an interface.
    """
    names = {index["name"] for index in inspect(postgres_engine).get_indexes("audit_events")}
    assert "ix_audit_events_target" in names


@pytest.mark.parametrize(
    ("actor", "target_type"),
    [
        ("", "packages"),
        ("   ", "packages"),
        ("\t", "packages"),
        ("\n", "packages"),
        ("anant", ""),
        ("anant", "   "),
        ("anant", "\t\n"),
    ],
    ids=[
        "empty",
        "spaces",
        "tab",
        "newline",
        "empty-target",
        "spaces-target",
        "tab-newline-target",
    ],
)
def test_the_database_rejects_whitespace_where_emit_would(
    actor: str, target_type: str, sessions: sessionmaker[Session]
) -> None:
    """`emit` refuses these, and so must the table.

    A bare `length(...) > 0` accepts "   ", which answers "who did this?" with whitespace — no more
    useful than the blank the constraint exists to prevent. Asserted by inserting directly, because
    the point is the path that bypasses `emit`.

    The empty string is included alongside the whitespace: `^[[:space:]]*$` rejects it too, and a
    regression narrowing the pattern to `+` would otherwise leave the plainest case of missing
    attribution passing.
    """
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), unit_of_work(sessions) as session:
        session.add(
            AuditEvent(
                category=AuditCategory.STATE_CHANGE.value,
                actor=actor,
                target_id=uuid4(),
                target_type=target_type,
            )
        )


@pytest.mark.parametrize(
    "trace_id",
    [
        "not-a-trace",
        "ABCDEF01234567890ABCDEF012345678",
        "0123456789abcdef",
        "0123456789abcdef0123456789abcde",
        " 0123456789abcdef0123456789abcd",
    ],
    ids=["prose", "uppercase", "too-short", "one-short", "padded"],
)
def test_a_malformed_explicit_trace_is_refused(
    trace_id: str, sessions: sessionmaker[Session]
) -> None:
    """`String(32)` would store any of these, and the event would cite a trace nobody can open.

    Uppercase is refused rather than normalised: the same trace written both ways would not join to
    itself, and silently rewriting a caller's id hides that they passed the wrong thing. The
    defaulted path cannot produce these — this is the hand-passed one, which is exactly where a
    request id or a span id gets supplied by mistake.
    """
    with pytest.raises(ValueError, match="32 lowercase hex"), unit_of_work(sessions) as session:
        emit(
            session,
            category=AuditCategory.STATE_CHANGE,
            actor=SYSTEM_ACTOR,
            target_id=uuid4(),
            target_type="packages",
            trace_id=trace_id,
        )
