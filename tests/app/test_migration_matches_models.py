"""The migration and the models must describe the same schema, checked without a database.

`tests/app/test_migrations_roundtrip.py` is the real comparison: it runs the migrations against
PostgreSQL and diffs the result against `Base.metadata`. It is also the only thing that catches a
hand-written migration, and it cannot run on a machine without a database — which is exactly where
hand-written migrations get written.

Two faults reached CI from that gap on #198, both of the same kind: a migration describing a schema
nobody had compared to the models. It declared an `updated_at` column that `TimestampedUUID` does not
supply, so every insert failed on a NOT NULL nothing wrote. And it passed already-qualified names to
`CheckConstraint`, where the naming convention embeds whatever it is given — producing
`ck_rule_definitions_ck_rule_definitions_definition_rule_3b42`, prefixed twice and truncated to fit.

This catches both statically. It is strictly weaker than the round-trip test and does not replace it.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

import app.models  # noqa: F401  - registers every model on Base.metadata
from app.db.base import Base

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATIONS = sorted((REPO_ROOT / "alembic" / "versions").glob("*.py"))


def _upgrade_body(path: Path) -> list[ast.AST]:
    """Only what `upgrade()` does.

    `downgrade()` describes the reverse of the schema, so counting it cancels out everything
    `upgrade()` declares — which is exactly what happened when the column comparison below first
    learned to read `add_column`: `upgrade` added `workflow_run_id`, `downgrade` dropped it, and the
    two netted to nothing while the model plainly had the column.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for definition in tree.body
        if isinstance(definition, ast.FunctionDef) and definition.name == "upgrade"
        for node in ast.walk(definition)
    ]


def _create_table_calls(path: Path) -> dict[str, ast.Call]:
    return {
        node.args[0].value: node
        for node in _upgrade_body(path)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "create_table"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }


def _named(call: ast.Call, kind: str) -> set[str]:
    return {
        keyword.value.value
        for argument in call.args[1:]
        if isinstance(argument, ast.Call) and getattr(argument.func, "attr", "") == kind
        for keyword in argument.keywords
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant)
    }


#: Columns supplied by the `_identity_columns()` helper the migrations spread with `*`. Named here
#: because an AST walk cannot see through a call, and treating them as absent would report every
#: existing migration as broken.
IDENTITY_COLUMNS = {"id", "created_at"}


def _columns(call: ast.Call) -> set[str]:
    spread = any(isinstance(argument, ast.Starred) for argument in call.args[1:])
    return (IDENTITY_COLUMNS if spread else set()) | {
        argument.args[0].value
        for argument in call.args[1:]
        if isinstance(argument, ast.Call)
        and getattr(argument.func, "attr", "") == "Column"
        and argument.args
        and isinstance(argument.args[0], ast.Constant)
    }


def _column_changes(table: str) -> tuple[set[str], set[str]]:
    """Columns added and dropped by `op.add_column` / `op.drop_column`, across every migration.

    **A table is not only what created it.** The comparison below used to read the `create_table` call
    alone, which silently assumed no table is ever altered afterwards — true until the first
    `add_column`, and then it reports a correct migration and a correct model as disagreeing. #210's
    `workflow_run_id` was the first, and exempting the table would have traded a real check for a
    growing list.

    Read as text, like everything else here, so a migration name hidden behind a module constant is
    invisible to it. That is a limitation worth stating rather than working around: a migration is one
    fixed historical state and spelling it out is no hardship.
    """
    added: set[str] = set()
    dropped: set[str] = set()
    for path in MIGRATIONS:
        for node in _upgrade_body(path):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            operation = getattr(node.func, "attr", "")
            named = node.args[0]
            if not isinstance(named, ast.Constant) or named.value != table:
                continue
            if operation == "add_column":
                column = node.args[1]
                if (
                    isinstance(column, ast.Call)
                    and column.args
                    and isinstance(column.args[0], ast.Constant)
                ):
                    added.add(column.args[0].value)
            elif operation == "drop_column" and isinstance(node.args[1], ast.Constant):
                dropped.add(node.args[1].value)
    return added, dropped


ALL_TABLES = [
    (path, table)
    for path in MIGRATIONS
    for table in _create_table_calls(path)
    if table in Base.metadata.tables
]


@pytest.mark.parametrize(("path", "table"), ALL_TABLES, ids=lambda v: getattr(v, "name", str(v)))
def test_the_migration_declares_the_columns_the_model_has(path: Path, table: str) -> None:
    """A column in one and not the other is a NOT NULL nobody writes, or a field nobody stores.

    The migrations are compared as a *chain*: what `create_table` declared, plus what later migrations
    added, minus what they dropped. Comparing the creating migration alone would fail every table any
    later migration touches.
    """
    added, dropped = _column_changes(table)
    declared = (_columns(_create_table_calls(path)[table]) | added) - dropped
    expected = set(Base.metadata.tables[table].columns.keys())
    assert declared == expected, (
        f"{path.name}/{table}: only in the migrations {sorted(declared - expected)}, "
        f"only in the model {sorted(expected - declared)}"
    )


@pytest.mark.parametrize(("path", "table"), ALL_TABLES, ids=lambda v: getattr(v, "name", str(v)))
def test_check_constraint_names_are_unqualified(path: Path, table: str) -> None:
    """The `ck_` convention embeds the name it is given, so passing an already-qualified one gets it
    prefixed twice and then truncated with a hash — a name that silently differs from the model's and
    changes whenever the text around it does."""
    for name in _named(_create_table_calls(path)[table], "CheckConstraint"):
        assert not name.startswith("ck_"), (
            f"{path.name}/{table}: CheckConstraint name {name!r} is already qualified. Pass the "
            f"short name; the convention adds 'ck_{table}_'."
        )


def test_every_migration_renders_without_a_database() -> None:
    """Alembic can emit the whole upgrade as SQL without connecting to anything, which makes it the
    strongest check available on a machine with no database.

    Worth having because the two checks above passed a migration whose identity helper called
    itself — `return (*_identity_columns(),)`, produced by a careless edit — and recursed until the
    interpreter gave up. Names and columns both looked right; nothing had tried to run it. A check
    that verifies shape and never execution will keep approving files that cannot execute.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        # Never connected to: offline mode needs a URL to pick a dialect and nothing more.
        env={
            **os.environ,
            "DATABASE_URL": "postgresql+psycopg://offline:offline@localhost/offline",
        },
        check=False,
    )
    assert (
        result.returncode == 0
    ), f"alembic could not render the migrations:\n{result.stderr[-2000:]}"
    assert "CREATE TABLE" in result.stdout
