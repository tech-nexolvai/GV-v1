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

#: The local stack's PostgreSQL (docker-compose.yml), which the running app uses. Refused outright, even
#: for a test-looking name, so a mistyped `DATABASE_URL` can never create or drop anything there.
LOCAL_STACK_PORT = 5433

#: The worker ids pytest-xdist hands out: `gw0`, `gw1`, ... Anything else is refused, which also keeps
#: the id safe to put inside a quoted database name.
_WORKER_ID = re.compile(r"gw[0-9]+")


def worker_database_url(url: URL, worker: str) -> URL:
    """The URL of one xdist worker's own database: same server and login, `<name>_<worker>` (#1289).

    Raises `ValueError` unless `url` names a test database off the local stack's port and `worker` is an
    id xdist would give, because the caller goes on to drop and create the database it returns.
    """
    if not _WORKER_ID.fullmatch(worker):
        raise ValueError(f"{worker!r} is not a pytest-xdist worker id (gw0, gw1, ...)")
    if url.port == LOCAL_STACK_PORT:
        raise ValueError(
            f"DATABASE_URL points at port {LOCAL_STACK_PORT}, the local stack's database; "
            "per-worker test databases are never created there"
        )
    name = url.database or ""
    if not name.startswith(TEST_DATABASE_PREFIXES):
        raise ValueError(
            f"{name!r} is not a test database (names start with {' or '.join(TEST_DATABASE_PREFIXES)}); "
            "refusing to create per-worker databases beside it"
        )
    return url.set(database=f"{name}_{worker}")


def worker_database_for(environ: Mapping[str, str]) -> tuple[URL, URL] | None:
    """`(DATABASE_URL, this worker's own database URL)` under xdist, or `None` to leave things as they are.

    `None` without a worker id (a plain single-process run: nothing changes), without `DATABASE_URL` (the
    database tests skip, as they always have), and for a URL that is not PostgreSQL, which
    `_database_url` already fails with its own message.
    """
    worker = environ.get("PYTEST_XDIST_WORKER")
    raw_url = environ.get("DATABASE_URL")
    if worker is None or raw_url is None:
        return None
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        return None
    return url, worker_database_url(url, worker)


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


def create_worker_database(base: URL, own: URL) -> None:
    """Create the worker's database empty, replacing any leftover of the same name from an earlier run.

    Only the database: the tests migrate it themselves, exactly as they migrate `DATABASE_URL` today (the
    session schema below, and the modules that run their own `command.upgrade`).
    """
    _drop_and_create(base, own, create=True)


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
