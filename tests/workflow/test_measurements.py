"""Turning stored reviewer measurements into operands, and refusing to invent one.

The decisions worth pinning here all concern what does *not* become an operand: a value nobody
supplied, an older correction, and a RUN-layer parameter that was never a measurement.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from fractions import Fraction

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import Package, PackageRevision, PackageState, Project
from app.models.parameters import to_rows
from rules.parameters import ParameterLayer, Provenance
from rules.parameters import ParameterSet as InMemoryParameterSet
from rules.parameters import ParameterValue as InMemoryParameterValue
from rules.schema import Quantity
from tests.app.postgres_fixture import alembic_config
from units.measurement import Unit
from verdict.operands import EvidenceStatus
from workflow.measurements import SOURCE, operands_for

pytest_plugins = ("tests.app.postgres_fixture",)


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _revision(session: Session) -> PackageRevision:
    project = Project(name="operand tests")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Apex Glass & Stone")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.RUNNING_CHECKS
    )
    session.add(revision)
    session.flush()
    return revision


def _store_run_values(
    session: Session, revision: PackageRevision, values: dict[str, Fraction], *, version: int
) -> None:
    package = session.get(Package, revision.package_id)
    assert package is not None
    parameters = InMemoryParameterSet(
        project_id=str(package.project_id),
        layer=ParameterLayer.RUN,
        version=version,
        parameters={
            name: InMemoryParameterValue(
                value=Quantity(value=value, unit=Unit.INCH),
                provenance=Provenance.MEASURED,
                set_by="test reviewer",
                set_at=datetime.now(UTC),
            )
            for name, value in values.items()
        },
    )
    stored, rows = to_rows(parameters)
    session.add(stored)
    for row in rows:
        session.add(row)
    session.flush()


def test_a_stored_measurement_becomes_a_qualified_operand(session: Session) -> None:
    """HUMAN_CONFIRMED, because a person read it — one of the two statuses the gate accepts.

    And `USER_INPUT` rather than `SHOP`: the reviewer read it off the shop drawing, but the *system*
    did not, and a finding claiming `SHOP` would say extraction had produced it.
    """
    revision = _revision(session)
    _store_run_values(
        session, revision, {"CT-DEPTH-001:countertop_depth": Fraction(51, 2)}, version=1
    )

    operands = operands_for(session, revision.id)

    operand = operands["CT-DEPTH-001"]["countertop_depth"]
    assert operand.status is EvidenceStatus.HUMAN_CONFIRMED
    assert operand.source == SOURCE
    assert operand.value is not None and operand.value.exact == Fraction(51, 2)
    assert "test reviewer" in (
        operand.evidence_ref or ""
    ), "the operand does not say who supplied it, so a finding cannot be traced to a person"


def test_the_latest_correction_wins(session: Session) -> None:
    """**A reviewer who fixes a typo must be judged on the corrected number.**

    Running against the earlier set would judge the package on a value the reviewer had already
    replaced — and it would look completely normal on the findings list.
    """
    revision = _revision(session)
    _store_run_values(session, revision, {"CT-DEPTH-001:countertop_depth": Fraction(24)}, version=1)
    _store_run_values(
        session, revision, {"CT-DEPTH-001:countertop_depth": Fraction(51, 2)}, version=2
    )

    operands = operands_for(session, revision.id)

    value = operands["CT-DEPTH-001"]["countertop_depth"].value
    assert value is not None and value.exact == Fraction(51, 2), "an earlier correction was used"


def test_an_input_nobody_supplied_produces_no_operand(session: Session) -> None:
    """Absent, not defaulted. The engine then abstains with NOT_FOUND, which is the right answer.

    This is the property the whole system exists to have: a missing value is never invented.
    """
    revision = _revision(session)
    _store_run_values(
        session, revision, {"CT-DEPTH-001:countertop_depth": Fraction(51, 2)}, version=1
    )

    operands = operands_for(session, revision.id)

    assert "sink_interior_width" not in operands.get("CT-SINK-CUTOUT-WIDTH-001", {})
    assert "CT-SINK-CUTOUT-WIDTH-001" not in operands


def test_a_run_parameter_that_is_not_a_measurement_is_skipped(session: Session) -> None:
    """A RUN-layer value with no `rule_id:` prefix belongs to something else.

    `rules/parameters.py:user_input_set` is for any user input — a field wall-to-wall dimension, say
    — not only for measurements this API wrote. Guessing such a value into a rule would attach a
    number to a check nobody entered it for.
    """
    revision = _revision(session)
    _store_run_values(
        session,
        revision,
        {"field_wall_to_wall": Fraction(96), "CT-DEPTH-001:countertop_depth": Fraction(51, 2)},
        version=1,
    )

    operands = operands_for(session, revision.id)

    assert set(operands) == {"CT-DEPTH-001"}
    assert "field_wall_to_wall" not in operands.get("", {})


def test_a_revision_with_no_measurements_yields_nothing(session: Session) -> None:
    """An empty map, not an error: entering no values is an ordinary state, and the checks abstain."""
    revision = _revision(session)

    assert operands_for(session, revision.id) == {}


def test_an_unknown_revision_yields_nothing_rather_than_raising(session: Session) -> None:
    """The drain may be handed a revision that has since gone. Nothing to build is a real answer."""
    from uuid import uuid4

    assert operands_for(session, uuid4()) == {}
