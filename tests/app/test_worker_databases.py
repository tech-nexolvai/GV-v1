"""One test database per pytest-xdist worker (#1289), and migrations that can run side by side (#1293).

CI runs each test shard on four workers. Four processes sharing one database race each other: migrations
run `CREATE EXTENSION IF NOT EXISTS`, which two sessions cannot do at once, and any test that looks at the
whole database would see another worker's rows. So each worker gets its own database on the same server,
named after the worker (`gv_test` becomes `gv_test_gw0`), and the root conftest points `DATABASE_URL` at it.

What must stay true:

- **Without xdist nothing changes.** A plain `pytest` run, locally or in the `safety guards` job, uses
  `DATABASE_URL` exactly as given. That includes a pytest a test starts as a subprocess: it inherits the
  worker's environment, but it is not a worker, so it makes no database of its own (#1293).
- **Only test databases are ever created or dropped.** The name must start `gv_test` (CI) or `gvtest`
  (local scratch databases), hold only `a-z`, `0-9` and `_`, fit PostgreSQL's 63-character limit with the
  worker suffix, and never be reached on port 5433 (the local stack the running app uses), however the
  port is given. The worker id must look like xdist's own (`gw0`, `gw1`, ...).
- **Two migrations at once both succeed (#1293).** Roles belong to the whole server, so two databases
  migrating at the same moment both try to create them; migration 0025 must let the second one through.
- **The fixture file is one plugin, under one name (#1293).** Loaded twice, it kept two session schemas
  alive at once, and a test reading the catalog without a schema filter saw both.

The pure checks run anywhere. The database ones need PostgreSQL and skip without `DATABASE_URL`, like
every other database test, and run on CI.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError

from tests.app.postgres_fixture import (
    CLUSTER_SETUP_LOCK,
    REPO_ROOT,
    _drop_and_create,
    create_worker_database,
    drop_worker_database,
    worker_database_for,
    worker_database_url,
    xdist_worker_id,
)

CI_URL = "postgresql+psycopg://gv_test:gv_test@localhost:5432/gv_test"
LOCAL_URL = "postgresql+psycopg://gv:gv@localhost:5434/gvtest_7"
NO_ENVIRONMENT: dict[str, str] = {}


def test_a_worker_gets_its_own_database_on_the_same_server() -> None:
    url = worker_database_url(make_url(CI_URL), "gw0", NO_ENVIRONMENT)

    assert url.database == "gv_test_gw0"
    assert (url.drivername, url.host, url.port) == ("postgresql+psycopg", "localhost", 5432)
    assert (url.username, url.password) == ("gv_test", "gv_test")


def test_a_local_scratch_database_gets_a_worker_suffix_too() -> None:
    assert (
        worker_database_url(make_url(LOCAL_URL), "gw3", NO_ENVIRONMENT).database == "gvtest_7_gw3"
    )


@pytest.mark.parametrize(
    "name", ["gv", "postgres", "template1", "live_review", "proof_1", "aiset3"]
)
def test_a_database_that_is_not_a_test_database_is_refused(name: str) -> None:
    url = make_url(CI_URL).set(database=name)

    with pytest.raises(ValueError, match="not a test database"):
        worker_database_url(url, "gw0", NO_ENVIRONMENT)


def test_the_local_stack_port_is_refused_even_for_a_test_name() -> None:
    url = make_url(LOCAL_URL).set(port=5433)

    with pytest.raises(ValueError, match="5433"):
        worker_database_url(url, "gw0", NO_ENVIRONMENT)


@pytest.mark.parametrize(
    "raw_url",
    [
        # The driver takes a port from the query string over the one before the path ...
        "postgresql+psycopg://gv:gv@localhost:5434/gvtest_7?port=5433",
        # ... and from a `host=name:port` pair,
        "postgresql+psycopg://gv:gv@localhost/gvtest_7?host=localhost:5433",
        # ... including one of several hosts.
        "postgresql+psycopg://gv:gv@/gvtest_7?host=elsewhere:5434&host=localhost:5433",
    ],
)
def test_the_local_stack_port_is_refused_when_the_query_string_gives_it(raw_url: str) -> None:
    """#1293: the effective port is checked, not only the one written before the database name."""
    with pytest.raises(ValueError, match="5433"):
        worker_database_url(make_url(raw_url), "gw0", NO_ENVIRONMENT)


def test_the_local_stack_port_is_refused_when_pgport_gives_it() -> None:
    """With no port in the URL, libpq takes `PGPORT`; that is the port the connection would use."""
    url = make_url("postgresql+psycopg://gv:gv@localhost/gvtest_7")

    with pytest.raises(ValueError, match="5433"):
        worker_database_url(url, "gw0", {"PGPORT": "5433"})


def test_pgport_does_not_matter_when_the_url_names_a_port() -> None:
    assert worker_database_url(make_url(LOCAL_URL), "gw0", {"PGPORT": "5433"}).port == 5434


def test_a_url_without_a_port_uses_postgresqls_default() -> None:
    url = make_url("postgresql+psycopg://gv:gv@localhost/gvtest_7")

    assert worker_database_url(url, "gw0", NO_ENVIRONMENT).database == "gvtest_7_gw0"


@pytest.mark.parametrize(
    ("raw_url", "environ"),
    [
        ("postgresql+psycopg://gv:gv@localhost:5434/gvtest_7?service=gv", NO_ENVIRONMENT),
        (LOCAL_URL, {"PGSERVICE": "gv"}),
    ],
)
def test_a_connection_service_is_refused(raw_url: str, environ: dict[str, str]) -> None:
    """A service file can set the port and the database where no check here can read them."""
    with pytest.raises(ValueError, match="service"):
        worker_database_url(make_url(raw_url), "gw0", environ)


def test_a_database_named_again_in_the_query_string_is_refused() -> None:
    """`?dbname=` overrides the name the prefix rule checked, so the connection would go elsewhere."""
    url = make_url("postgresql+psycopg://gv:gv@localhost:5434/gvtest_7?dbname=live_review")

    with pytest.raises(ValueError, match="live_review"):
        worker_database_url(url, "gw0", NO_ENVIRONMENT)


@pytest.mark.parametrize(
    "name", ["gvtest_Seven", 'gvtest_7"x', "gvtest-7", "gvtest 7", "gvtest_7é"]
)
def test_a_name_with_characters_outside_a_z_0_9_and_underscore_is_refused(name: str) -> None:
    """#1293: the name goes inside quoted SQL, and PostgreSQL folds or keeps case depending on quoting."""
    with pytest.raises(ValueError, match="a-z0-9_"):
        worker_database_url(make_url(CI_URL).set(database=name), "gw0", NO_ENVIRONMENT)


def test_a_name_that_postgresql_would_cut_short_is_refused() -> None:
    """#1293: PostgreSQL silently cuts names at 63 bytes, so two workers' names could become one."""
    longest = "gvtest_" + "x" * (63 - len("gvtest_") - len("_gw10"))
    assert worker_database_url(make_url(CI_URL).set(database=longest), "gw10", NO_ENVIRONMENT)

    with pytest.raises(ValueError, match="63"):
        worker_database_url(make_url(CI_URL).set(database=longest + "x"), "gw10", NO_ENVIRONMENT)


@pytest.mark.parametrize("worker", ["", "master", "gw", "gw0; DROP DATABASE gv", 'gw0"', "GW1"])
def test_a_worker_id_xdist_would_not_give_is_refused(worker: str) -> None:
    with pytest.raises(ValueError, match="worker id"):
        worker_database_url(make_url(CI_URL), worker, NO_ENVIRONMENT)


def test_without_xdist_the_database_url_is_left_alone() -> None:
    """A single-process run: no worker id, so no second database."""
    assert worker_database_for({"DATABASE_URL": CI_URL}, None) is None


def test_without_a_database_url_there_is_nothing_to_give_a_worker() -> None:
    assert worker_database_for({}, "gw0") is None


def test_under_xdist_the_base_and_the_worker_database_are_both_returned() -> None:
    base, own = worker_database_for({"DATABASE_URL": CI_URL}, "gw2") or (None, None)

    assert base is not None and own is not None
    assert base.database == "gv_test"
    assert own.database == "gv_test_gw2"


def test_a_database_url_that_is_not_postgresql_is_left_for_the_fixture_to_refuse() -> None:
    """The fixture already fails such a URL with its own message; this hook must not mask it."""
    assert worker_database_for({"DATABASE_URL": "sqlite:///gv_test.db"}, "gw0") is None


def test_the_worker_id_comes_from_xdist_itself() -> None:
    """xdist hands each worker its id in `config.workerinput`; the worker's pytest is the one with it."""
    worker = SimpleNamespace(workerinput={"workerid": "gw3"})

    assert xdist_worker_id(worker) == "gw3"


def test_a_pytest_started_by_a_test_is_not_a_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1293: a child pytest inherits `PYTEST_XDIST_WORKER` from the worker that started it.

    Read from the environment, it would take itself for that worker and create a database beside the
    worker's own (`gv_test_gw0_gw0`). Read from xdist's `workerinput`, which only a real worker has, it
    is a plain single-process run.
    """
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")

    assert xdist_worker_id(SimpleNamespace()) is None


# ---------------------------------------------------------------------------------------------------
# The fixture file is loaded once, under one name (#1293)
# ---------------------------------------------------------------------------------------------------

FIXTURE_PLUGIN = "tests.app.postgres_fixture"


def test_the_postgres_fixture_is_loaded_under_one_name(pytestconfig: pytest.Config) -> None:
    """Two names made two plugins, two `_migrated_schema` fixtures and two session schemas.

    Tests collected before a module registered the second name used one, the rest the other, so a run of
    `tests/app tests/db/test_evidence_models.py` had two migrated schemas alive at once.
    """
    manager = pytestconfig.pluginmanager

    assert manager.get_plugin(FIXTURE_PLUGIN) is not None
    assert manager.get_plugin("postgres_fixture") is None
    assert "postgres_fixture" not in sys.modules


def test_every_module_names_the_postgres_fixture_the_same_way() -> None:
    """A module that registered the bare name would bring the second copy back."""
    declared = re.compile(r"^pytest_plugins\b.*$", re.MULTILINE)
    odd = [
        f"{path.relative_to(REPO_ROOT)}: {line}"
        for path in sorted((REPO_ROOT / "tests").rglob("*.py"))
        for line in declared.findall(path.read_text(encoding="utf-8"))
        if "postgres_fixture" in line and f'"{FIXTURE_PLUGIN}"' not in line
    ]

    assert not odd, odd


# ---------------------------------------------------------------------------------------------------
# `xdist_group` and the shard-coverage check (#1293)
# ---------------------------------------------------------------------------------------------------


def test_no_xdist_group_mark_without_the_coverage_check_knowing_about_it() -> None:
    """A test marked `xdist_group` is renamed `<id>@<group>` before pytest-split divides the tests.

    xdist does that on each worker under `--dist loadgroup`, before pytest-split picks the shard. The
    renamed test matches no time in `.test_durations`, so the shards CI runs differ from the ones
    `scripts/check_pytest_shard_coverage.py` proves (it collects without xdist). There are no marks
    today. The first one has to come with a coverage check that collects the same names; this test
    stops it otherwise. CONTRIBUTING, "Running the tests on several workers", says the same.
    """
    this_file = Path(__file__).resolve()
    marked = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "tests").rglob("*.py")
        if path.resolve() != this_file and "xdist_group" in path.read_text(encoding="utf-8")
    )
    coverage_check = (REPO_ROOT / "scripts" / "check_pytest_shard_coverage.py").read_text(
        encoding="utf-8"
    )

    assert not marked or "xdist_group" in coverage_check, (
        f"{marked} use xdist_group, so under --dist loadgroup their names become <id>@<group> before "
        "pytest-split divides the tests; make scripts/check_pytest_shard_coverage.py collect the same "
        "names (and say so in it) before adding the mark"
    )


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


#: Waiting for the setup lock, as `pg_locks` shows it: in this database only (#1293), because advisory
#: lock keys are per database and another run's workers on the same server may hold the same key.
_WAITING_FOR_THE_SETUP_LOCK = text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND objid = :key "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
)


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
                        _WAITING_FOR_THE_SETUP_LOCK, {"key": CLUSTER_SETUP_LOCK}
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


def test_a_worker_gives_up_on_a_setup_lock_held_too_long(postgres_engine: Engine) -> None:
    """#1293: a worker stuck holding the lock must not hang every other worker until the job times out.

    The wait is bounded; past it the setup stops with a message that says which lock and how to find
    who holds it, and it leaves no half-made database behind. The setup runs in a thread so that, if it
    did wait for ever, the test fails instead of hanging. No bound on how long it takes in all: creating
    and dropping a database is slow on a busy server, and the lock wait is what is being tested.
    """
    base = make_url(os.environ["DATABASE_URL"])
    own = worker_database_url(base, "gw97")
    assert own.database is not None
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    outcome: list[BaseException] = []

    def setup() -> None:
        try:
            create_worker_database(base, own, lock_timeout_seconds=1)
        except BaseException as error:  # noqa: BLE001 - checked in the test thread below
            outcome.append(error)

    worker = threading.Thread(target=setup)
    try:
        with admin.connect() as holder:
            holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": CLUSTER_SETUP_LOCK})
            try:
                worker.start()
                worker.join(timeout=240)
                assert not worker.is_alive(), "the setup is still waiting for the lock"
            finally:
                holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": CLUSTER_SETUP_LOCK})
        assert len(outcome) == 1 and isinstance(outcome[0], TimeoutError), outcome
        assert f"advisory lock {CLUSTER_SETUP_LOCK}" in str(outcome[0])
        assert not _exists(admin, own.database)
    finally:
        worker.join(timeout=240)
        drop_worker_database(base, own)
        admin.dispose()


# ---------------------------------------------------------------------------------------------------
# Migration 0025 under concurrent upgrades (#1293)
# ---------------------------------------------------------------------------------------------------


def _migration_0025() -> ModuleType:
    path = REPO_ROOT / "alembic" / "versions" / "0025_database_roles.py"
    spec = importlib.util.spec_from_file_location("gv_migration_0025_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _backend_waiting_on_a_lock(watcher: Engine, marker: str) -> bool:
    with watcher.connect() as connection:
        return (
            connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                    "AND datname = current_database() AND query LIKE :marker"
                ),
                {"marker": f"%{marker}%"},
            ).scalar_one()
            > 0
        )


def test_two_sessions_creating_the_same_role_at_once_both_succeed(postgres_engine: Engine) -> None:
    """The race #1289's first CI run hit, made to happen every time, on a made-up role.

    The first session creates the role and has not committed; the second finds no role, tries to create
    it and waits on the first. When the first commits, the second gets a unique violation on
    `pg_authid_rolname_index`, not `duplicate_object`. Migration 0025's statement must take that as "the
    role is there" too. A made-up role, so the test never drops one it did not create.
    """
    role = f"gv_race_{uuid4().hex[:16]}"
    create_role = _migration_0025().CREATE_ROLE.format(role=role)
    base = make_url(os.environ["DATABASE_URL"])
    engine = create_engine(base)
    watcher = create_engine(base, isolation_level="AUTOCOMMIT")
    failure: list[BaseException] = []

    def second_session() -> None:
        try:
            with engine.begin() as connection:
                connection.execute(text(create_role))
        except BaseException as error:  # noqa: BLE001 - re-raised in the test thread below
            failure.append(error)

    second = threading.Thread(target=second_session)
    try:
        with engine.connect() as first:
            transaction = first.begin()
            first.execute(text(create_role))
            second.start()
            deadline = time.monotonic() + 60
            while not _backend_waiting_on_a_lock(watcher, role) and time.monotonic() < deadline:
                assert second.is_alive(), failure
                time.sleep(0.05)
            assert _backend_waiting_on_a_lock(watcher, role), "the second session did not wait"
            transaction.commit()
        second.join(timeout=60)
        assert not second.is_alive()
        assert not failure, failure

        with watcher.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM pg_roles WHERE rolname = :role"), {"role": role}
                ).scalar_one()
                == 1
            )
    finally:
        second.join(timeout=60)
        with watcher.connect() as connection:
            connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        watcher.dispose()
        engine.dispose()


_UPGRADE = """
import os
from alembic import command
from tests.app.postgres_fixture import alembic_config

config = alembic_config()
config.attributes["database_url"] = os.environ["GV_UPGRADE_URL"]
command.upgrade(config, "head")
"""


def test_two_upgrades_at_once_on_two_fresh_databases_both_succeed(postgres_engine: Engine) -> None:
    """Two databases on one server migrated to head at the same moment, as two workers or two runs do.

    Separate processes, because Alembic's `op` is process-wide. On a server that already has the roles
    this shows the rest of the migrations do not collide either; on one without them (a fresh CI server)
    it is the race itself. Roles this test created are dropped again; roles that were there before are
    never touched.
    """
    from app.db.roles import Role

    base = make_url(os.environ["DATABASE_URL"])
    databases: list[URL] = [worker_database_url(base, worker) for worker in ("gw95", "gw96")]
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    names = [role.value for role in Role]
    role_query = text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:names)")
    with admin.connect() as connection:
        existed = set(connection.execute(role_query, {"names": names}).scalars())
    try:
        for database in databases:
            _drop_and_create(base, database, create=True)
        environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        upgrades = [
            subprocess.Popen(
                [sys.executable, "-c", _UPGRADE],
                cwd=REPO_ROOT,
                env={
                    **environment,
                    "GV_UPGRADE_URL": database.render_as_string(hide_password=False),
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for database in databases
        ]
        outputs = [upgrade.communicate(timeout=300)[0] for upgrade in upgrades]

        for upgrade, output in zip(upgrades, outputs, strict=True):
            assert upgrade.returncode == 0, output[-4000:]
        with admin.connect() as connection:
            assert set(connection.execute(role_query, {"names": names}).scalars()) == set(names)
    finally:
        for database in databases:
            drop_worker_database(base, database)
        with admin.connect() as connection:
            for role in sorted(set(names) - existed):
                # Only roles this test made, and only if nothing else has started using them since: a
                # role another database now holds grants for cannot be dropped, and is left in place.
                with contextlib.suppress(DBAPIError):
                    connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        admin.dispose()
