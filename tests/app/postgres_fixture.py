"""PostgreSQL fixture loaded only by persistence integration tests."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError

from alembic import command

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


#: Tables that must survive the reset between tests.
#:
#: `alembic_version` above all. Truncating it would make the next `command.upgrade(..., "head")` —
#: which forty test modules call in their own `_upgrade` helper — believe the database was empty and
#: replay every migration into a schema that already has the tables. The whole saving here rests on
#: that call being a no-op, and this is what keeps it one.
PRESERVED_TABLES: frozenset[str] = frozenset({"alembic_version"})


#: The only database names the per-worker setup below may create or drop: CI's `gv_test` and the local
#: `gvtest_<N>` scratch databases. Everything else on a server (a live app's database, `postgres`, a
#: template) is refused by name before any SQL runs.
TEST_DATABASE_PREFIXES: tuple[str, ...] = ("gv_test", "gvtest")

#: A worker's database name, suffix included: lower-case letters, digits and `_` only (#1293). The name
#: is put inside double quotes in `CREATE` and `DROP DATABASE`, so a quote must never reach it, and a
#: quoted name keeps its case where an unquoted one is folded, so upper case would make two spellings.
_DATABASE_NAME = re.compile(r"[a-z0-9_]+")

#: PostgreSQL keeps 63 bytes of a name and silently drops the rest (`NAMEDATALEN - 1`), so two long names
#: that differ only in their worker suffix would become the same database (#1293).
MAX_DATABASE_NAME_LENGTH = 63

#: The local stack's PostgreSQL (docker-compose.yml), which the running app uses. Refused outright, even
#: for a test-looking name, so a mistyped `DATABASE_URL` can never create or drop anything there.
LOCAL_STACK_PORT = 5433

#: libpq's port when neither the connection nor `PGPORT` gives one.
DEFAULT_PORT = "5432"

#: The worker ids pytest-xdist hands out: `gw0`, `gw1`, ... Anything else is refused, which also keeps
#: the id safe to put inside a quoted database name.
_WORKER_ID = re.compile(r"gw[0-9]+")


def _effective_ports(url: URL, environ: Mapping[str, str]) -> list[str]:
    """Every port a connection to `url` could reach, worked out the way the driver and libpq do (#1293).

    The port before the database name is only one of the places a port can come from. SQLAlchemy hands
    the driver the URL's query string as well, so `?port=` and `?host=name:port` replace it (several
    hosts give several ports); and a connection with no port at all takes `PGPORT`, then 5432. So the
    check reads the very arguments SQLAlchemy would pass, rather than `url.port`.

    Two things it refuses rather than follow: a connection service (`?service=` or `PGSERVICE`), whose
    file can set the port and the database where nothing here can see them, and a database named again
    in the query string (`?dbname=`), which would replace the name the prefix rule checked. Neither is
    used by CI or by the local scratch databases.
    """
    _args, params = url.get_dialect()().create_connect_args(url)
    if params.get("service") or environ.get("PGSERVICE"):
        raise ValueError(
            "DATABASE_URL uses a connection service (service= or PGSERVICE), which can set the port and "
            "the database out of sight of this check; give the host, port and database in the URL instead"
        )
    if str(params.get("dbname", url.database)) != url.database:
        raise ValueError(
            f"DATABASE_URL's query string names the database {params['dbname']!r}, not "
            f"{url.database!r}; refusing to create per-worker databases beside it"
        )
    default = [
        port.strip() or DEFAULT_PORT for port in (environ.get("PGPORT") or DEFAULT_PORT).split(",")
    ]
    ports: list[str] = []
    for port in str(params.get("port") or "").split(","):
        ports.extend([port.strip()] if port.strip() else default)
    return ports


def worker_database_url(url: URL, worker: str, environ: Mapping[str, str] = os.environ) -> URL:
    """The URL of one xdist worker's own database: same server and login, `<name>_<worker>` (#1289).

    Raises `ValueError` unless `url` names a test database that is not reached on the local stack's port
    (however that port is given, see `_effective_ports`), the worker's name fits PostgreSQL's limit and
    holds only `a-z`, `0-9` and `_`, and `worker` is an id xdist would give, because the caller goes on
    to drop and create the database it returns. `environ` is where libpq would read `PGPORT` and
    `PGSERVICE`: the process's own environment unless a test passes another.
    """
    if not _WORKER_ID.fullmatch(worker):
        raise ValueError(f"{worker!r} is not a pytest-xdist worker id (gw0, gw1, ...)")
    name = url.database or ""
    if not name.startswith(TEST_DATABASE_PREFIXES):
        raise ValueError(
            f"{name!r} is not a test database (names start with {' or '.join(TEST_DATABASE_PREFIXES)}); "
            "refusing to create per-worker databases beside it"
        )
    own = f"{name}_{worker}"
    if not _DATABASE_NAME.fullmatch(own):
        raise ValueError(
            f"{own!r} is not a usable worker database name: only a-z0-9_ (lower case, digits, "
            "underscore) are allowed"
        )
    if len(own.encode()) > MAX_DATABASE_NAME_LENGTH:
        raise ValueError(
            f"{own!r} is longer than PostgreSQL's {MAX_DATABASE_NAME_LENGTH}-byte limit, which would cut "
            "it short; use a shorter test database name"
        )
    if any(port.lstrip("0") == str(LOCAL_STACK_PORT) for port in _effective_ports(url, environ)):
        raise ValueError(
            f"DATABASE_URL reaches port {LOCAL_STACK_PORT}, the local stack's database; "
            "per-worker test databases are never created there"
        )
    return url.set(database=own)


def xdist_worker_id(config: object) -> str | None:
    """This process's pytest-xdist worker id (`gw0`, ...), or `None` when it is not an xdist worker.

    Read from `config.workerinput`, which xdist gives only to the worker processes it starts, and not from
    `PYTEST_XDIST_WORKER` (#1293). The variable is inherited: a pytest that a test starts as a subprocess
    has it too, and would take itself for that worker and nest a database inside the worker's
    (`gv_test_gw0_gw0`). That child pytest is an ordinary single-process run and uses the worker's
    `DATABASE_URL` as it is.
    """
    workerinput = getattr(config, "workerinput", None)
    if workerinput is None:
        return None
    return str(workerinput["workerid"])


def worker_database_for(environ: Mapping[str, str], worker: str | None) -> tuple[URL, URL] | None:
    """`(DATABASE_URL, this worker's own database URL)` under xdist, or `None` to leave things as they are.

    `None` without a worker id (a plain single-process run: nothing changes), without `DATABASE_URL` (the
    database tests skip, as they always have), and for a URL that is not PostgreSQL, which
    `_database_url` already fails with its own message.
    """
    raw_url = environ.get("DATABASE_URL")
    if worker is None or raw_url is None:
        return None
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        return None
    return url, worker_database_url(url, worker, environ)


def _drop_and_create(base: URL, own: URL, *, create: bool) -> None:
    name = own.database
    # Checked again here, not only when the URL was built: this is the function that runs DROP.
    if name is None or worker_database_url(base, name.rsplit("_", 1)[-1]) != own:
        raise ValueError(f"{own.database!r} is not a worker database of {base.database!r}")
    # Connected to the base test database, never to `postgres`: the setup touches nothing it was not
    # given. AUTOCOMMIT because CREATE and DROP DATABASE cannot run inside a transaction.
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            # `WITH (FORCE)` (PostgreSQL 13+) ends any connection still open on it, such as an engine a
            # test never disposed, or a crashed earlier run's.
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            if create:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()


#: The advisory lock that lets one worker at a time create the cluster-wide objects below. Any fixed
#: number works; it is taken on the base database, which every worker of a run shares.
CLUSTER_SETUP_LOCK = 1289

#: How long a worker waits for that lock before it gives up (#1293). The setup it guards takes about a
#: second per worker, so five minutes is only ever reached when a holder is stuck (a worker paused in a
#: debugger, or a crashed run's connection the server has not noticed yet). Better a clear error then
#: than every worker hanging until the CI job's own timeout.
CLUSTER_SETUP_LOCK_TIMEOUT_SECONDS = 300

#: PostgreSQL's code for "canceling statement due to lock timeout".
_LOCK_NOT_AVAILABLE = "55P03"


def _create_cluster_wide_objects(base: URL, own: URL, *, lock_timeout_seconds: float) -> None:
    """Run every migration once, in a throwaway schema of the worker's database, one worker at a time.

    **Why (found on the first CI run of #1289).** Almost everything a migration makes lives in one
    database, so four workers with four databases never meet. Roles do not: they belong to the whole
    server. Migration 0025 creates its four roles if they are missing, and two workers migrating at the
    same moment both found them missing; the loser failed on `pg_authid_rolname_index`, a unique
    violation that 0025's `duplicate_object` handler did not catch then. Every database test in that
    worker errored at setup.

    So before any test runs, each worker migrates a throwaway schema while holding an advisory lock on
    the shared base database: the first creates the roles, the others find them. Running the real
    migrations rather than creating the roles here means a later migration's cluster-wide object is
    covered too, with nothing to keep in step. The schema is dropped again; what stays is exactly what a
    single-process run leaves after its first migration (the roles, and the extensions in `public`).

    **Kept after #1293 fixed 0025 itself.** 0025 now takes the unique violation as "the role is there",
    so two upgrades at once succeed without this lock (`test_worker_databases.py` proves both). The lock
    stays because it costs about a second per worker and guards what no test of today's migrations can:
    the next migration that makes a server-wide object (another role, a role setting) without a
    race-safe handler would otherwise first show up as thousands of setup errors on one CI run in ten.
    With the lock, the workers of a run never migrate side by side, whatever a migration does. The
    0025 fix covers what the lock cannot: two separate runs, or two deploys, on one server.

    **The wait is bounded** (`lock_timeout_seconds`, #1293). Past it, `TimeoutError` names the lock and
    how to find who holds it.
    """
    schema = f"gv_worker_setup_{uuid4().hex}"
    lock_engine = create_engine(base, isolation_level="AUTOCOMMIT")
    admin = create_engine(own, isolation_level="AUTOCOMMIT")
    try:
        with lock_engine.connect() as lock:
            lock.execute(text(f"SET lock_timeout = '{int(lock_timeout_seconds * 1000)}ms'"))
            try:
                lock.execute(text("SELECT pg_advisory_lock(:key)"), {"key": CLUSTER_SETUP_LOCK})
            except OperationalError as error:
                if getattr(error.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
                    raise
                raise TimeoutError(
                    f"waited {lock_timeout_seconds:g} s for advisory lock {CLUSTER_SETUP_LOCK} on "
                    f"{base.database!r}, which one pytest-xdist worker at a time holds while it sets up "
                    "the server-wide roles; another run's worker seems stuck holding it. Find it with "
                    f"SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND objid = {CLUSTER_SETUP_LOCK} "
                    "AND granted, then end that run (or pg_terminate_backend(pid))"
                ) from error
            try:
                with admin.connect() as connection:
                    connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                schema_url = own.update_query_dict({"options": f"-csearch_path={schema},public"})
                config = alembic_config()
                config.attributes["database_url"] = schema_url.render_as_string(hide_password=False)
                command.upgrade(config, "head")
                with admin.connect() as connection:
                    connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            finally:
                lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": CLUSTER_SETUP_LOCK})
    finally:
        admin.dispose()
        lock_engine.dispose()


def create_worker_database(
    base: URL,
    own: URL,
    *,
    lock_timeout_seconds: float = CLUSTER_SETUP_LOCK_TIMEOUT_SECONDS,
) -> None:
    """Create the worker's database, replacing any leftover of the same name from an earlier run.

    It starts with no tables: the tests migrate it themselves, exactly as they migrate `DATABASE_URL`
    today (the session schema below, and the modules that run their own `command.upgrade`). Only the
    server-wide objects the migrations need are made first, one worker at a time (see
    `_create_cluster_wide_objects`). If that setup fails, the database is dropped again before the error
    goes on, so a failed start leaves nothing behind.
    """
    _drop_and_create(base, own, create=True)
    try:
        _create_cluster_wide_objects(base, own, lock_timeout_seconds=lock_timeout_seconds)
    except BaseException:
        _drop_and_create(base, own, create=False)
        raise


def drop_worker_database(base: URL, own: URL) -> None:
    """Drop the worker's database at the end of its run. No error if it is already gone."""
    _drop_and_create(base, own, create=False)


def _database_url() -> URL:
    raw_url = os.environ.get("DATABASE_URL")
    if raw_url is None:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration tests locally")
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        pytest.fail("DATABASE_URL must use PostgreSQL for persistence integration tests")
    return url


@pytest.fixture(scope="session")
def _migrated_schema() -> Iterator[tuple[Engine, str, tuple[str, ...]]]:
    """One schema, migrated once for the whole session (#522).

    **Why this is not per test any more.** Building a schema by running every migration costs about
    0.4 seconds, against 0.005 for an empty schema and 0.06 to empty a full one — so migrations were
    roughly 98% of the setup of every database test, and the cost grew with each migration added as
    well as with each test. Measured before changing anything; the numbers are on #522.

    **Why it could not be done until now.** A schema that stays alive holds the database-wide
    extensions, and until #513 those were installed unqualified — so the second schema to exist could
    not resolve `gin_trgm_ops` or the `vector` type. Keeping one alive was precisely the thing that
    broke.

    The isolation tests actually rely on is *data* isolation, and `postgres_engine` still provides
    that by emptying every table between tests. What is no longer provided is a private *schema* per
    test, which nothing was using: no test creates, drops or alters one.
    """
    url = _database_url()
    schema = f"gv_test_{uuid4().hex}"
    admin_engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        # **`public` on the path as well as the test's own schema (#513).** Production's default
        # search path is `"$user", public`, and the extensions' operator classes, types and
        # `similarity()` live in public — so a path of only the test schema is both unlike production
        # and unable to resolve them. The test schema comes first, so nothing it defines is shadowed.
        schema_url = url.update_query_dict({"options": f"-csearch_path={schema},public"})
        engine = create_engine(schema_url)
        config = alembic_config()
        config.attributes["database_url"] = schema_url.render_as_string(hide_password=False)
        command.upgrade(config, "head")

        with engine.connect() as connection:
            tables = tuple(
                row[0]
                for row in connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = :schema"),
                    {"schema": schema},
                )
                if row[0] not in PRESERVED_TABLES
            )

        try:
            yield engine, schema, tables
        finally:
            engine.dispose()
            with admin_engine.connect() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        admin_engine.dispose()


@pytest.fixture
def postgres_engine(_migrated_schema: tuple[Engine, str, tuple[str, ...]]) -> Iterator[Engine]:
    """An engine on a migrated schema holding no rows.

    Emptied *before* the test rather than after, so a test never inherits what a previous one left
    even if that one died partway through its own cleanup. `RESTART IDENTITY` because a sequence that
    kept counting would make ids differ between running a test alone and running it in the suite,
    which is the kind of difference that produces a failure nobody can reproduce.

    `CASCADE` because the tables reference each other; without it PostgreSQL refuses to empty a table
    another still points at, and the order that would satisfy every foreign key is one more thing to
    keep in step with the schema.
    """
    engine, schema, tables = _migrated_schema
    with engine.begin() as connection:
        # **Anything the schema did not start with is dropped, not just emptied.** A per-test schema
        # threw away tables a test created; a shared one would keep them, and the next test to
        # compare the database against the models would see a table the models have never heard of.
        # `test_db_conventions.py` creates temporary mapped tables and removes them from the metadata
        # without dropping them, which is what found this.
        #
        # Restoring the guarantee here rather than in that test: "a test cannot leave the schema
        # changed" is a property of the harness, and asking every future test to remember it is how
        # it stops being true.
        strays = [
            row[0]
            for row in connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = :schema"),
                {"schema": schema},
            )
            if row[0] not in tables and row[0] not in PRESERVED_TABLES
        ]
        for stray in strays:
            connection.execute(text(f'DROP TABLE "{schema}"."{stray}" CASCADE'))

        if tables:
            joined = ", ".join(f'"{table}"' for table in tables)
            connection.execute(text(f"TRUNCATE {joined} RESTART IDENTITY CASCADE"))
    yield engine
