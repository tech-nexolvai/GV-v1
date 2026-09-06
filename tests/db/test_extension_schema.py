"""Extensions live in one named schema, so any session can find them (#513).

Verification for: `app/models/drawing.py:EXTENSION_SCHEMA` and migration `0034_pg_trgm_in_public`.

The bug this guards was not a crash in a code path — it was a property of the database that made
whole batches of tests fail for reasons that had nothing to do with the tests. `CREATE EXTENSION IF
NOT EXISTS pg_trgm` with no schema puts the extension wherever `search_path` points; an extension is
database-wide and lives in exactly one schema; and its operator classes and types are then resolved
by name. So the first session to run the migration owned the extension, every later one could not
resolve `gin_trgm_ops`, and dropping the first session's schema silently handed ownership to whoever
came next.

It presented as flakiness under concurrency for months, and it blocked #522: every fast way to stop
re-running migrations per test needs one migrated schema to stay alive, which is exactly the shape
that broke.
"""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from alembic import command
from app.models.drawing import EXTENSION_SCHEMA
from tests.app.postgres_fixture import REPO_ROOT, alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

#: Every extension the schema installs. Both were unqualified, and only one of them announced it.
EXTENSIONS = ("pg_trgm", "vector")


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.mark.parametrize("extension", EXTENSIONS)
def test_an_extension_lives_in_the_named_schema(postgres_engine: Engine, extension: str) -> None:
    """Not merely installed — installed *somewhere in particular*.

    Asserting only that the extension exists would have passed throughout the bug: it did exist, in
    a schema the next session could not see.
    """
    _upgrade(postgres_engine)

    with postgres_engine.connect() as connection:
        schema = connection.execute(
            text(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = :name"
            ),
            {"name": extension},
        ).scalar_one()

    assert schema == EXTENSION_SCHEMA


def test_a_migrated_schema_that_stays_alive_does_not_stop_the_next_one(
    postgres_engine: Engine,
) -> None:
    """**The property that was actually broken**, exercised the way it broke.

    One schema is migrated and deliberately *kept*, then another is migrated beside it. Before #513
    the second failed with `operator class "gin_trgm_ops" does not exist` — or, once that was fixed
    alone, with `type "vector" does not exist`, because the second extension had the same bug and a
    different error message.

    This is the shape every fast test-database strategy needs (#522), and the shape two pytest
    processes sharing a database take by accident.
    """
    _upgrade(postgres_engine)

    url = make_url(postgres_engine.url.render_as_string(hide_password=False))
    second = f"beside_{uuid4().hex[:12]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE SCHEMA "{second}"'))
        beside = create_engine(
            url.update_query_dict({"options": f"-csearch_path={second},{EXTENSION_SCHEMA}"})
        )
        try:
            # The assertion is that this does not raise. A migration that cannot run beside a live
            # schema is the whole of #513.
            _upgrade(beside)
        finally:
            beside.dispose()
            with admin.connect() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{second}" CASCADE'))
    finally:
        admin.dispose()


def _executed_sql(path: Path) -> list[tuple[int, str]]:
    """Every string literal in a file that is not a docstring, with its line number.

    Parsed rather than grepped. The first version of this test read lines, and its first failure was
    a docstring in the very migration that fixes the bug — prose *quoting* the broken form. A guard
    that cannot tell an instruction from a description of one is a guard that gets suppressed.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    # An f-string's literal fragments are `Constant` nodes in their own right, and `ast.walk` visits
    # them separately — so `f"CREATE EXTENSION ... SCHEMA {x}"` would be reported once whole (fine)
    # and once as the fragment before the first placeholder (which has no SCHEMA in it). The whole
    # f-string is what the database receives, so the fragments are skipped.
    fragments = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and id(node) not in fragments:
                found.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            found.append((node.lineno, text))
    return found


def test_no_create_extension_anywhere_omits_its_schema() -> None:
    """Every `CREATE EXTENSION` the code executes names the schema it installs into.

    **This is the test that actually bites, and the other two do not.** Both of those assert the end
    state of a live database — and an extension is database-wide and outlives the schema that created
    it, so once any run has relocated it they pass whatever the code says. Removing `vector` from the
    migration's list left all three green, which is how this one came to be written.

    A static check has no such blind spot: it reads what the code will do rather than what a database
    happens to have had done to it already.
    """
    offenders: list[str] = []
    for root in (REPO_ROOT / "alembic" / "versions", REPO_ROOT / "app"):
        for path in sorted(root.rglob("*.py")):
            # 0020 and 0021 are shipped migrations, never edited, and 0034 repairs what they did.
            # They are the historical record of the bug rather than a live instance of it.
            if path.name.startswith(("0020_", "0021_")):
                continue
            for line, literal in _executed_sql(path):
                if "CREATE EXTENSION" in literal and "SCHEMA" not in literal:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}: {literal.strip()}")

    assert not offenders, (
        "an unqualified CREATE EXTENSION puts the extension wherever `search_path` points, and an "
        "extension is database-wide — so the next session with a different search path cannot "
        "resolve its types or operator classes (#513):\n  " + "\n  ".join(offenders)
    )
