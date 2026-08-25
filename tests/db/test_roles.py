"""The verdict role cannot read retrieval tables — asserted by the database, not by a mock.

Source: backend proposal §11; `AGENTS.md` §2.1, §2.9 · Verification: ``app/db/roles.py``.

**Every privilege test assumes the role.** Migration `0013` recorded the trap in its own docstring:
its plan was to `REVOKE UPDATE` from `gv_app`, but CI connects as the database owner, and `REVOKE`
restricts neither an owner nor a superuser — the revoke would have run, the test would have attempted
an update, and it would have succeeded. `SET ROLE` is what closes that: after it, permission checks
use the assumed role and ownership no longer applies. A test here that forgot to `SET ROLE` would
pass while proving nothing, so `test_the_owner_is_not_restricted_which_is_why_set_role_matters`
demonstrates the bypass rather than leaving it implicit.

**The forbidden list is derived, not written down.** `forbidden_for_verdict()` subtracts the verdict
allowlist from every mapped table, so a retrieval table added tomorrow is asserted automatically. A
hand-written list would cover the tables somebody remembered.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from alembic import command
from app.db.base import immutable_table_names
from app.db.roles import (
    APPEND_ONLY_PRIVILEGES,
    ROLE_GRANTS,
    VERDICT_READS,
    VERDICT_WRITES,
    Role,
    all_table_names,
    forbidden_for_verdict,
)
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def migrated(postgres_engine: Engine) -> Engine:
    """A migrated schema, so the grants under test are the ones the migration issued.

    Run the same way `tests/db/test_append_only.py` does: through the real Alembic entry point, not
    by executing the grant statements here. A test that issued its own grants would be checking its
    own SQL rather than the migration's.
    """
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return postgres_engine


def _schema(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(text("SELECT current_schema()")).scalar_one())


def _as_role(engine: Engine, role: Role, statement: str) -> None:
    """Run one statement as `role`, in a transaction that is always rolled back.

    Rolled back rather than committed: several of these are writes that are *expected* to succeed,
    and a test that left rows behind would make the next assertion depend on the order tests ran in.
    """
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f"SET LOCAL ROLE {role.value}"))
            connection.execute(text(statement))
        finally:
            transaction.rollback()


# ---------------------------------------------------------------------------
# The verdict role is an allowlist
# ---------------------------------------------------------------------------


def test_the_verdict_role_cannot_read_a_retrieval_table(migrated: Engine) -> None:
    """The runtime half of verdict isolation.

    The import guard proves the verdict cannot import retrieval code. This proves that handing it a
    session does not hand it the tables — which is the failure the import guard cannot see, because
    the import graph is untouched when a session arrives from outside.
    """
    schema = _schema(migrated)

    with pytest.raises(ProgrammingError, match="permission denied"):
        _as_role(migrated, Role.VERDICT, f'SELECT 1 FROM "{schema}"."dense_embeddings" LIMIT 1')

    with pytest.raises(ProgrammingError, match="permission denied"):
        _as_role(migrated, Role.VERDICT, f'SELECT 1 FROM "{schema}"."match_candidates" LIMIT 1')


def test_the_verdict_role_cannot_read_the_model_invocation_record(migrated: Engine) -> None:
    """ "No model credentials" has to mean it cannot read what the models did either."""
    schema = _schema(migrated)

    with pytest.raises(ProgrammingError, match="permission denied"):
        _as_role(migrated, Role.VERDICT, f'SELECT 1 FROM "{schema}"."model_invocations" LIMIT 1')


def test_the_verdict_role_cannot_read_unqualified_observations(migrated: Engine) -> None:
    """The sealed handoff is the boundary.

    `verdict_inputs` is what the evidence gate writes. Reading `canonical_observations` or
    `observation_candidates` directly is how a `RAW_CANDIDATE` reading becomes an operand, and
    `AGENTS.md` §2.1 forbids exactly that. Here it is forbidden by there being no privilege.
    """
    schema = _schema(migrated)

    for table in ("canonical_observations", "observation_candidates"):
        with pytest.raises(ProgrammingError, match="permission denied"):
            _as_role(migrated, Role.VERDICT, f'SELECT 1 FROM "{schema}"."{table}" LIMIT 1')


@pytest.mark.parametrize("table", forbidden_for_verdict(), ids=lambda table: table)
def test_the_verdict_role_cannot_read_anything_outside_its_allowlist(
    table: str, migrated: Engine
) -> None:
    """Enumerated over every table not on the allowlist, so a new one is covered on the day it
    lands rather than when somebody remembers to add it here."""
    schema = _schema(migrated)

    with pytest.raises(ProgrammingError, match="permission denied"):
        _as_role(migrated, Role.VERDICT, f'SELECT 1 FROM "{schema}"."{table}" LIMIT 1')


@pytest.mark.parametrize("table", VERDICT_READS, ids=lambda table: table)
def test_the_verdict_role_can_read_what_a_verdict_needs(table: str, migrated: Engine) -> None:
    """The other half. Without this, an allowlist of nothing would pass every test above."""
    schema = _schema(migrated)

    _as_role(migrated, Role.VERDICT, f'SELECT 1 FROM "{schema}"."{table}" LIMIT 1')


@pytest.mark.parametrize("table", VERDICT_WRITES, ids=lambda table: table)
def test_the_verdict_role_cannot_update_what_it_writes(table: str, migrated: Engine) -> None:
    """A verdict appends. All three of these carry `Immutable`, and a role that was never granted
    `UPDATE` cannot issue one at all — which is the grant half of C1.12 that `0013` deferred."""
    schema = _schema(migrated)

    with pytest.raises(ProgrammingError, match="permission denied"):
        _as_role(migrated, Role.VERDICT, f'UPDATE "{schema}"."{table}" SET id = id')


# ---------------------------------------------------------------------------
# Reporting is read-only at the database
# ---------------------------------------------------------------------------


def test_the_reporting_role_cannot_write(migrated: Engine) -> None:
    """Read-only at the database rather than in application code.

    A reporting query that opened a write transaction by accident, or a report endpoint that grew a
    "fix this row" button, is refused by the connection rather than by whoever reviews the diff.
    """
    schema = _schema(migrated)

    with pytest.raises(ProgrammingError, match="permission denied"):
        _as_role(migrated, Role.REPORT, f'UPDATE "{schema}"."packages" SET vendor = vendor')

    with pytest.raises(ProgrammingError, match="permission denied"):
        _as_role(
            migrated,
            Role.REPORT,
            f'INSERT INTO "{schema}"."projects" (id, name) '
            "VALUES ('00000000-0000-0000-0000-000000000001', 'P')",
        )


def test_the_reporting_role_can_read_every_table(migrated: Engine) -> None:
    """A report that cannot see a table produces a number with a silent hole in it."""
    schema = _schema(migrated)

    for table in all_table_names():
        _as_role(migrated, Role.REPORT, f'SELECT 1 FROM "{schema}"."{table}" LIMIT 1')


# ---------------------------------------------------------------------------
# Immutable tables are append-only by privilege too
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.APP, Role.WORKER], ids=lambda role: role.value)
def test_the_operational_roles_cannot_update_an_immutable_table(
    role: Role, migrated: Engine
) -> None:
    """`0013` enforces this with a trigger, which a table owner can disable. A role holding no
    `UPDATE` privilege cannot issue one in the first place, and the two together are what make
    append-only a property rather than a convention."""
    schema = _schema(migrated)

    with pytest.raises(ProgrammingError, match="permission denied"):
        _as_role(migrated, role, f'UPDATE "{schema}"."findings" SET severity = severity')


@pytest.mark.parametrize("role", list(Role), ids=lambda role: role.value)
def test_no_role_holds_delete_on_anything(role: Role) -> None:
    """Nothing here deletes a row in normal operation: a package is superseded, a finding is re-run,
    a correction is a new row. A role holding `DELETE` because it might one day need it is how the
    audit trail acquires a gap nobody can date."""
    grants = ROLE_GRANTS[role]

    for table in grants.tables():
        assert not grants.may(table, "DELETE"), f"{role.value} may DELETE from {table}"


def test_every_immutable_table_is_append_only_for_every_role() -> None:
    """Asserted against the declaration as well as the database, so the intent is checked even where
    a role has no grant on the table at all."""
    immutable = set(immutable_table_names())

    for role, grants in ROLE_GRANTS.items():
        for table in grants.tables():
            if table in immutable:
                assert set(grants.privileges[table]) <= set(
                    APPEND_ONLY_PRIVILEGES
                ), f"{role.value} holds more than SELECT/INSERT on the immutable table {table}"


# ---------------------------------------------------------------------------
# The declaration matches the database
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(Role), ids=lambda role: role.value)
def test_the_granted_privileges_are_exactly_the_declared_ones(role: Role, migrated: Engine) -> None:
    """Read back from `information_schema`, so `app/db/roles.py` and the database cannot disagree.

    Both directions. A missing grant breaks a service; an extra grant is the one that matters here,
    because nothing fails and the role quietly holds more than the docstring says.
    """
    schema = _schema(migrated)
    declared = {
        (table, privilege)
        for table, privileges in ROLE_GRANTS[role].privileges.items()
        for privilege in privileges
    }

    with migrated.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.table_privileges "
                "WHERE grantee = :role AND table_schema = :schema"
            ),
            {"role": role.value, "schema": schema},
        ).all()
    actual = {(str(table), str(privilege)) for table, privilege in rows}

    assert actual == declared


def test_no_role_can_create_objects_in_the_schema(migrated: Engine) -> None:
    """`USAGE`, never `CREATE`. A role that can create a view can read anything the view selects
    from — a narrow allowlist with `CREATE` beside it is a lock with the window open."""
    schema = _schema(migrated)

    for role in Role:
        with pytest.raises(ProgrammingError, match="permission denied"):
            _as_role(migrated, role, f'CREATE TABLE "{schema}"."sneaky_{role.name}" (id int)')


def test_public_holds_nothing_on_the_schema(migrated: Engine) -> None:
    """Every role is an implicit member of `PUBLIC`, so a privilege left there is granted to all
    four — and would make each allowlist above meaningless without any of them changing."""
    schema = _schema(migrated)

    with migrated.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.table_privileges "
                "WHERE grantee = 'PUBLIC' AND table_schema = :schema"
            ),
            {"schema": schema},
        ).all()

    assert rows == []


# ---------------------------------------------------------------------------
# Why every test above assumes a role
# ---------------------------------------------------------------------------


def test_the_owner_is_not_restricted_which_is_why_set_role_matters(migrated: Engine) -> None:
    """Demonstrated rather than left implicit, because it is the trap `0013` fell into on paper.

    Without `SET ROLE`, every assertion in this file would run as the schema owner, succeed, and
    prove nothing — while reading exactly like a passing privilege test. This records the limit
    where somebody will meet it.
    """
    schema = _schema(migrated)

    with migrated.connect() as connection:
        transaction = connection.begin()
        try:
            # The owner has no grant on this table for itself and does not need one.
            connection.execute(text(f'SELECT 1 FROM "{schema}"."dense_embeddings" LIMIT 1'))
        finally:
            transaction.rollback()


# ---------------------------------------------------------------------------
# The declaration is not empty
# ---------------------------------------------------------------------------


def test_the_declaration_is_not_empty() -> None:
    """The guard against the failure this file nearly shipped with.

    `ROLE_GRANTS` is derived from `Base.metadata`, and a model registers itself only when its module
    is imported. With `app.models` unimported, three roles had no grants, `forbidden_for_verdict()`
    returned nothing, and the parametrized test enumerating forbidden tables collected zero cases —
    a green suite checking nothing, which is exactly what `app/models/__init__.py` was written to
    prevent.

    Asserted with real numbers rather than `> 0`: a metadata registry holding two tables would also
    be non-empty and would also be wrong.
    """
    assert len(all_table_names()) > 40, "the model registry looks unpopulated"
    assert len(forbidden_for_verdict()) > 30, "almost every table should be denied to the verdict"

    for role in Role:
        assert ROLE_GRANTS[role].tables(), f"{role.value} has no grants at all"

    assert len(ROLE_GRANTS[Role.REPORT].tables()) == len(all_table_names())
    assert len(ROLE_GRANTS[Role.VERDICT].tables()) == len(VERDICT_READS)


def test_the_app_role_does_not_hold_the_workers_queue() -> None:
    """The durability argument for an outbox is that exactly one thing writes it.

    An HTTP handler holding `INSERT` on `outbox_entries` could enqueue work outside a workflow, and
    holding `UPDATE` could mark work done that never ran.
    """
    app_tables = set(ROLE_GRANTS[Role.APP].tables())

    assert "outbox_entries" not in app_tables
    assert "task_runs" not in app_tables
    assert "outbox_entries" in set(ROLE_GRANTS[Role.WORKER].tables())


def test_the_verdict_role_holds_no_grant_on_any_retrieval_or_model_table() -> None:
    """Stated by name as well as by subtraction.

    The derived check above covers more, but it would also pass if the allowlist grew to include one
    of these by accident. These four are the ones the issue names.
    """
    verdict_tables = set(ROLE_GRANTS[Role.VERDICT].tables())

    for table in ("dense_embeddings", "match_candidates", "approved_matches", "model_invocations"):
        assert table not in verdict_tables
