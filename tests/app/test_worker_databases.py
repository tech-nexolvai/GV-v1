"""One test database per pytest-xdist worker (#1289).

CI runs each test shard on four workers. Four processes sharing one database race each other: migrations
run `CREATE EXTENSION IF NOT EXISTS`, which two sessions cannot do at once, and any test that looks at the
whole database would see another worker's rows. So each worker gets its own database on the same server,
named after the worker (`gv_test` becomes `gv_test_gw0`), and the root conftest points `DATABASE_URL` at it.

What must stay true:

- **Without xdist nothing changes.** A plain `pytest` run, locally or in the `safety guards` job, uses
  `DATABASE_URL` exactly as given.
- **Only test databases are ever created or dropped.** The name must start `gv_test` (CI) or `gvtest`
  (local scratch databases), never on port 5433 (the local stack the running app uses), and the worker id
  must look like xdist's own (`gw0`, `gw1`, ...), so nothing odd reaches the quoted SQL.

The pure checks run anywhere. The database ones need PostgreSQL and skip without `DATABASE_URL`, like
every other database test, and run on CI.
"""

from __future__ import annotations

import os
import threading
import time

import pytest
from postgres_fixture import (
    CLUSTER_SETUP_LOCK,
    create_worker_database,
    drop_worker_database,
    worker_database_for,
    worker_database_url,
)
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

CI_URL = "postgresql+psycopg://gv_test:gv_test@localhost:5432/gv_test"
LOCAL_URL = "postgresql+psycopg://gv:gv@localhost:5434/gvtest_7"


def test_a_worker_gets_its_own_database_on_the_same_server() -> None:
    url = worker_database_url(make_url(CI_URL), "gw0")

    assert url.database == "gv_test_gw0"
    assert (url.drivername, url.host, url.port) == ("postgresql+psycopg", "localhost", 5432)
    assert (url.username, url.password) == ("gv_test", "gv_test")


def test_a_local_scratch_database_gets_a_worker_suffix_too() -> None:
    assert worker_database_url(make_url(LOCAL_URL), "gw3").database == "gvtest_7_gw3"


@pytest.mark.parametrize(
    "name", ["gv", "postgres", "template1", "live_review", "proof_1", "aiset3"]
)
def test_a_database_that_is_not_a_test_database_is_refused(name: str) -> None:
    url = make_url(CI_URL).set(database=name)

    with pytest.raises(ValueError, match="not a test database"):
        worker_database_url(url, "gw0")


def test_the_local_stack_port_is_refused_even_for_a_test_name() -> None:
    url = make_url(LOCAL_URL).set(port=5433)

    with pytest.raises(ValueError, match="5433"):
        worker_database_url(url, "gw0")


@pytest.mark.parametrize("worker", ["", "master", "gw", "gw0; DROP DATABASE gv", 'gw0"', "GW1"])
def test_a_worker_id_xdist_would_not_give_is_refused(worker: str) -> None:
    with pytest.raises(ValueError, match="worker id"):
        worker_database_url(make_url(CI_URL), worker)


def test_without_xdist_the_database_url_is_left_alone() -> None:
    """A single-process run: no worker id in the environment, so no second database."""
    assert worker_database_for({"DATABASE_URL": CI_URL}) is None


def test_without_a_database_url_there_is_nothing_to_give_a_worker() -> None:
    assert worker_database_for({"PYTEST_XDIST_WORKER": "gw0"}) is None


def test_under_xdist_the_base_and_the_worker_database_are_both_returned() -> None:
    base, own = worker_database_for({"PYTEST_XDIST_WORKER": "gw2", "DATABASE_URL": CI_URL}) or (
        None,
        None,
    )

    assert base is not None and own is not None
    assert base.database == "gv_test"
    assert own.database == "gv_test_gw2"


def test_a_database_url_that_is_not_postgresql_is_left_for_the_fixture_to_refuse() -> None:
    """The fixture already fails such a URL with its own message; this hook must not mask it."""
    environ = {"PYTEST_XDIST_WORKER": "gw0", "DATABASE_URL": "sqlite:///gv_test.db"}

    assert worker_database_for(environ) is None


# ---------------------------------------------------------------------------------------------------
# PostgreSQL (skipped without DATABASE_URL, run in CI)
# ---------------------------------------------------------------------------------------------------


def test_this_process_runs_on_its_own_database(postgres_engine: Engine) -> None:
    """Under xdist the session's database is the worker's; without it, `DATABASE_URL`'s own."""
    with postgres_engine.connect() as connection:
        current = connection.execute(text("SELECT current_database()")).scalar_one()

    expected = make_url(os.environ["DATABASE_URL"]).database
    assert current == expected
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker is not None:
        assert current.endswith(f"_{worker}")


def _exists(admin: Engine, name: str) -> bool:
    with admin.connect() as connection:
        return (
            connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).scalar()
            is not None
        )


def test_a_worker_database_is_created_replaced_and_dropped(postgres_engine: Engine) -> None:
    """Created empty, recreated over a leftover from a crashed run, and dropped at the end.

    Uses a made-up worker id so it never touches the database this process is running on.
    """
    base = make_url(os.environ["DATABASE_URL"])
    own = worker_database_url(base, "gw99")
    assert own.database is not None
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        create_worker_database(base, own)
        assert _exists(admin, own.database)

        leftover = create_engine(own)
        with leftover.begin() as connection:
            connection.execute(text("CREATE TABLE leftover_from_a_crashed_run (id int)"))
        # A connection left open, as a test that forgot to dispose its engine would leave it: the
        # recreate must not fail on "being accessed by other users".
        still_open = leftover.connect()
        try:
            create_worker_database(base, own)
        finally:
            still_open.close()
            leftover.dispose()

        fresh = create_engine(own)
        try:
            with fresh.connect() as connection:
                tables = connection.execute(
                    text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
                ).scalar_one()
                # The throwaway schema the cluster-wide setup migrated is gone again.
                schemas = connection.execute(
                    text(
                        "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' "
                        "AND nspname <> 'information_schema' ORDER BY nspname"
                    )
                ).scalars()
                schemas = list(schemas)
        finally:
            fresh.dispose()
        assert tables == 0
        assert schemas == ["public"]

        drop_worker_database(base, own)
        assert not _exists(admin, own.database)
    finally:
        drop_worker_database(base, own)
        admin.dispose()


def test_the_server_wide_setup_waits_for_the_other_workers(postgres_engine: Engine) -> None:
    """Two workers must never create the server's roles at the same moment (#1289's first CI run).

    The test holds the setup lock as another worker would, starts a worker's setup, and sees it wait
    on that lock; released, the setup finishes and the roles the migrations need exist.
    """
    from app.db.roles import Role

    base = make_url(os.environ["DATABASE_URL"])
    own = worker_database_url(base, "gw98")
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    failure: list[BaseException] = []

    def setup() -> None:
        try:
            create_worker_database(base, own)
        except BaseException as error:  # noqa: BLE001 - re-raised in the test thread below
            failure.append(error)

    worker = threading.Thread(target=setup)
    try:
        with admin.connect() as holder:
            holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": CLUSTER_SETUP_LOCK})
            try:
                worker.start()
                deadline = time.monotonic() + 60
                waiting = 0
                while not waiting and time.monotonic() < deadline and worker.is_alive():
                    waiting = holder.execute(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                            "AND NOT granted AND objid = :key"
                        ),
                        {"key": CLUSTER_SETUP_LOCK},
                    ).scalar_one()
                    time.sleep(0.05)
                assert waiting == 1, "the setup did not wait for the lock another worker held"
                assert worker.is_alive()
            finally:
                holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": CLUSTER_SETUP_LOCK})
        worker.join(timeout=120)
        assert not worker.is_alive()
        assert not failure, failure

        with admin.connect() as connection:
            roles = set(
                connection.execute(
                    text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:names)"),
                    {"names": [role.value for role in Role]},
                ).scalars()
            )
        assert roles == {role.value for role in Role}
    finally:
        worker.join(timeout=120)
        drop_worker_database(base, own)
        admin.dispose()
