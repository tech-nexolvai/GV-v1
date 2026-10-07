"""A package says what product its drawing set is for (#994).

Verification for: `alembic/versions/0073_package_product_type.py` and `Package.product_type`.

Three things are pinned here, against what the *migrations* install rather than what `create_all`
builds from the models (which agrees with itself by construction):

- the column is nullable and an existing package keeps NULL — NULL is "every product", today's
  behaviour, and nothing is backfilled by guessing;
- the CHECK constraint holds exactly the `ProductType` vocabulary, so a typo such as `Countertop`
  cannot be stored and then match no rule;
- the migration goes down and up again cleanly.
"""

from __future__ import annotations

import re
from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.models.package import Package, package_product
from tests.app.postgres_fixture import alembic_config
from vocabulary.semantic_types import ProductType

pytest_plugins = ("tests.app.postgres_fixture",)

REVISION = "0073_package_product_type"
BEFORE = "0072_countertop_piece_width"
CONSTRAINT = "ck_packages_package_product_type"
DECLARED = frozenset(member.value for member in ProductType)


def _config(engine: Engine):  # type: ignore[no-untyped-def]
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    return config


def _insert_package(engine: Engine, product_type: str | None) -> None:
    with engine.begin() as connection:
        project_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO projects (id, name, created_at) VALUES (:id, 'product type test', now())"
            ),
            {"id": project_id},
        )
        connection.execute(
            text(
                "INSERT INTO packages (id, project_id, created_at, vendor, product_type) "
                "VALUES (:id, :project, now(), NULL, :product)"
            ),
            {"id": uuid4(), "project": project_id, "product": product_type},
        )


def test_the_revision_id_fits_the_version_column() -> None:
    """`alembic_version.version_num` is 32 characters; a longer id fails CI at upgrade time."""
    assert len(REVISION) <= 32
    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_revision(REVISION) is not None
    assert script.get_revision(REVISION).down_revision == BEFORE  # type: ignore[union-attr]


def test_the_migration_lists_every_product_type() -> None:
    """Runs without a database. A product added to the vocabulary without a migration fails here."""
    module = ScriptDirectory.from_config(alembic_config()).get_revision(REVISION).module  # type: ignore[union-attr]
    assert frozenset(re.findall(r"'([^']+)'", module.PRODUCT_TYPES)) == DECLARED


def test_the_model_reads_no_product_as_every_product() -> None:
    assert package_product(Package(project_id=uuid4())) is None
    assert package_product(Package(project_id=uuid4(), product_type="countertop")) is (
        ProductType.COUNTERTOP
    )


def test_a_migrated_database_enforces_the_vocabulary(postgres_engine: Engine) -> None:
    command.upgrade(_config(postgres_engine), "head")

    with postgres_engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = :name AND conrelid = CAST('packages' AS regclass)"
            ),
            {"name": CONSTRAINT},
        ).scalar_one()
    assert frozenset(re.findall(r"'([^']+)'", definition)) == DECLARED

    column = next(
        c for c in inspect(postgres_engine).get_columns("packages") if c["name"] == "product_type"
    )
    assert column["nullable"] is True

    _insert_package(postgres_engine, "countertop")
    _insert_package(postgres_engine, None)
    with pytest.raises(IntegrityError):
        _insert_package(postgres_engine, "Countertop")
    with pytest.raises(IntegrityError):
        _insert_package(postgres_engine, "lighting")


def test_the_migration_goes_down_and_up_and_keeps_old_packages_null(
    postgres_engine: Engine,
) -> None:
    """Down removes the column; a package written before it comes back up with no product."""
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    command.downgrade(config, BEFORE)

    assert "product_type" not in {
        column["name"] for column in inspect(postgres_engine).get_columns("packages")
    }
    with postgres_engine.begin() as connection:
        project_id = uuid4()
        package_id = uuid4()
        connection.execute(
            text("INSERT INTO projects (id, name, created_at) VALUES (:id, 'before #994', now())"),
            {"id": project_id},
        )
        connection.execute(
            text("INSERT INTO packages (id, project_id, created_at) VALUES (:id, :project, now())"),
            {"id": package_id, "project": project_id},
        )

    command.upgrade(config, "head")

    with postgres_engine.connect() as connection:
        stored = connection.execute(
            text("SELECT product_type FROM packages WHERE id = :id"), {"id": package_id}
        ).scalar_one()
    assert stored is None
