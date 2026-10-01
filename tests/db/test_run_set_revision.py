"""A RUN parameter set names the package revision it was true for (#801, migration 0054).

Verification for: `app/models/parameters.py` (`to_rows`, the `run_names_its_revision` check) and
`alembic/versions/0054_run_set_names_its_revision.py`.

The one that matters most is `test_the_migration_keeps_an_old_run_row_and_refuses_a_new_one_without_
a_revision`: the check is `NOT VALID`, so a database already holding RUN rows — which name no revision,
because none could be recorded — upgrades cleanly, and from then on no RUN row is written without one.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import ParameterSet, Project
from app.models.parameters import to_rows
from rules.parameters import ParameterLayer, Provenance
from rules.parameters import ParameterSet as InMemorySet
from rules.parameters import ParameterValue as InMemoryValue
from rules.schema import Quantity
from tests.app.postgres_fixture import _database_url, alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

BEFORE = "0053_invocation_cost_unknown"
WHEN = datetime(2026, 10, 2, 9, 30, tzinfo=UTC)


def _set(project_id: str | None, layer: ParameterLayer) -> InMemorySet:
    return InMemorySet(
        project_id=project_id,
        layer=layer,
        version=1,
        parameters={
            "sink_interior_width": InMemoryValue(
                value=Quantity(value="28", unit="in"),
                provenance=Provenance.MEASURED,
                set_by="a reviewer",
                set_at=WHEN,
            )
        },
    )


@pytest.mark.parametrize(
    ("layer", "with_revision"),
    [(ParameterLayer.RUN, False), (ParameterLayer.PROJECT, True)],
)
def test_a_set_names_a_revision_exactly_when_it_is_a_run_set(
    layer: ParameterLayer, with_revision: bool
) -> None:
    """Refused before the database is asked, so the mistake names itself."""
    with pytest.raises(ValueError, match="RUN set names the package revision"):
        to_rows(_set(str(uuid4()), layer), package_revision_id=uuid4() if with_revision else None)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    with session_factory(postgres_engine)() as opened:
        yield opened
        opened.rollback()


def test_the_database_refuses_a_new_run_set_without_a_revision(session: Session) -> None:
    """The model's own row, bypassing `to_rows`. Outcome: the check refuses it."""
    project = Project(name="Ridgewood")
    session.add(project)
    session.flush()
    session.add(
        ParameterSet(set_id="sha256:" + "a" * 64, project_id=project.id, layer="run", version=1)
    )

    with pytest.raises(IntegrityError, match="run_names_its_revision"):
        session.flush()


@pytest.fixture
def schema_before_0054() -> Iterator[tuple[Engine, object]]:
    """A private schema migrated to 0053, the shape a database had before this change."""
    url = _database_url()
    schema = f"gv_0054_{uuid4().hex}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    schema_url = url.update_query_dict({"options": f"-csearch_path={schema},public"})
    config = alembic_config()
    config.attributes["database_url"] = schema_url.render_as_string(hide_password=False)
    command.upgrade(config, BEFORE)
    engine = create_engine(schema_url)
    try:
        yield engine, config
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_the_migration_keeps_an_old_run_row_and_refuses_a_new_one_without_a_revision(
    schema_before_0054: tuple[Engine, object],
) -> None:
    """**The point.** A RUN row written before 0054 names no revision. Outcome: the upgrade succeeds
    and keeps it; a RUN row written afterwards without a revision is refused."""
    engine, config = schema_before_0054
    project_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO projects (id, created_at, name) VALUES (:id, now(), 'Ridgewood')"),
            {"id": project_id},
        )
        connection.execute(
            text(
                "INSERT INTO parameter_sets (id, created_at, set_id, project_id, layer, version) "
                "VALUES (:id, now(), :set_id, :project, 'run', 1)"
            ),
            {"id": uuid4(), "set_id": "sha256:" + "b" * 64, "project": project_id},
        )

    command.upgrade(config, "head")  # type: ignore[arg-type]

    with engine.connect() as connection:
        kept = connection.execute(
            text("SELECT count(*) FROM parameter_sets WHERE package_revision_id IS NULL")
        ).scalar_one()
    assert kept == 1
    with (
        pytest.raises(IntegrityError, match="run_names_its_revision"),
        engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO parameter_sets (id, created_at, set_id, project_id, layer, version) "
                "VALUES (:id, now(), :set_id, :project, 'run', 2)"
            ),
            {"id": uuid4(), "set_id": "sha256:" + "c" * 64, "project": project_id},
        )
