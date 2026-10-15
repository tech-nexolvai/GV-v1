"""Drawn numbers typed before confirm-only stop counting (#1303).

Since #1253 a number drawn on the vendor's or the architect's drawing is confirmed from a reading and
never typed. Values typed before that stayed in the database and still counted, outranking a later
confirmed reading. Decided 2026-10-15: they stop counting. A drawn value must come from a confirmed
reading.

* A drawn field holding only an old typed value: the check waits (NOT_FOUND), and its reason says
  the typed value no longer counts and why. The typed value stays stored (append-only).
* An old typed value and a confirmed reading: the reading is used.
* A value typed by design (the site width, a typed list, a setting) is unchanged.

"Drawn" is #1253's own definition, `workflow.measurements.confirm_only`: one value on a SHOP or ARCH
drawing. Every check runs through the real engine (`DatabaseStages.run_checks`). Every number and
name is synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import VerdictInput
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import ParameterValue as StoredParameterValue
from app.models.parameters import to_rows
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.schema import Quantity
from tests.api.test_waiting_values import (
    PROJECT,
    _client,
    _findings,
    _package,
    _read,
    _run,
    _settings,
    _upgrade,
)
from tests.workflow.test_stages import _publish_rulebook
from units.measurement import Unit
from workflow.measurements import TYPED_DRAWN_STOPPED, operands_for, run_parameters_for

pytest_plugins = ("tests.app.postgres_fixture",)

#: The part of the reason that says why: the same words whatever the inputs are.
NO_LONGER_COUNTS = "before drawn numbers became confirm-only no longer counts"


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    _publish_rulebook(opened)
    opened.commit()
    try:
        yield opened
    finally:
        opened.close()


def _typed_before(session: Session, revision: UUID, **values: Fraction) -> None:
    """Numbers typed on Measurements before #1253, stored the way the endpoint used to store them:
    one RUN set for the revision, keyed `rule:input`."""
    versions = session.scalars(
        select(StoredParameterSet.version).where(
            StoredParameterSet.package_revision_id == revision,
            StoredParameterSet.layer == ParameterLayer.RUN.value,
        )
    ).all()
    earlier = ParameterSet(
        project_id=str(PROJECT),
        layer=ParameterLayer.RUN,
        version=max(versions, default=0) + 1,
        parameters={
            key.replace("__", ":"): ParameterValue(
                value=Quantity(value=value, unit=Unit("in")),
                provenance=Provenance.MEASURED,
                set_by="synthetic reviewer",
                set_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
            for key, value in values.items()
        },
    )
    stored, rows = to_rows(earlier, package_revision_id=revision)
    session.add(stored)
    for row in rows:
        session.add(row)
    session.commit()


def _depth_settings(client: Any, package: UUID) -> None:
    """The job's cabinet depth and overhang: the countertop depth should be 25 1/2"."""
    _settings(client, package, cabinet_depth='24"', countertop_overhang='1 1/2"')


def _used(session: Session, finding: dict[str, Any], name: str) -> VerdictInput:
    rows = session.scalars(
        select(VerdictInput).where(
            VerdictInput.check_run_id == UUID(finding["check_run_id"]),
            VerdictInput.operand_name == name,
        )
    ).all()
    assert len(rows) == 1, rows
    return rows[0]


def test_a_drawn_value_typed_before_no_longer_counts_and_the_check_waits(
    session: Session,
) -> None:
    """Outcome: a countertop depth typed before confirm-only, with no reading, no longer decides
    CT-DEPTH-001 (on the base it PASSed on the typed value). The check waits, and says why in plain
    words. The typed value is still in the database, and the waiting card lists the drawn number as
    waiting for the drawing, not as typed since."""
    client = _client(session)
    package, revision = _package(session)
    _depth_settings(client, package)
    _typed_before(session, revision, **{"CT-DEPTH-001__countertop_depth": Fraction(51, 2)})

    _run(session, revision)
    finding = _findings(client, package)["CT-DEPTH-001"]

    assert finding["outcome"] == "NOT_FOUND", finding
    assert NO_LONGER_COUNTS in finding["reason"], finding["reason"]
    assert "countertop_depth" in finding["reason"]
    assert "Confirm a reading" in finding["reason"]
    assert "CT-DEPTH-001" not in operands_for(session, revision)

    # Append-only: the typed value is still stored, exactly as typed.
    kept = session.scalar(
        select(func.count())
        .select_from(StoredParameterValue)
        .where(StoredParameterValue.name == "CT-DEPTH-001:countertop_depth")
    )
    assert kept == 1

    drawn = next(n for n in finding["missing_inputs"] if n["key"] == "SHOP:CT010")
    assert drawn["scope"] == "drawing"
    assert (
        drawn["saved"] is None
    ), "a typed drawn value no longer counts, so it is not 'saved since'"


def test_the_reason_names_every_stopped_input_of_the_check(session: Session) -> None:
    """Two drawn inputs of one check typed before, neither read: one sentence names both."""
    client = _client(session)
    package, revision = _package(session)
    _typed_before(
        session,
        revision,
        **{
            "CT-BACK-OFFSET-MIN-001__countertop_depth": Fraction(51, 2),
            "CT-BACK-OFFSET-MIN-001__front_offset": Fraction(4),
        },
    )

    _run(session, revision)
    finding = _findings(client, package)["CT-BACK-OFFSET-MIN-001"]

    assert finding["outcome"] == "NOT_FOUND", finding
    assert finding["reason"].count(NO_LONGER_COUNTS) == 1, finding["reason"]
    assert "countertop_depth and front_offset" in finding["reason"]


def test_a_confirmed_reading_is_used_instead_of_a_drawn_value_typed_before(
    session: Session,
) -> None:
    """Outcome: with a typed 25 1/2" (which PASSed on the base) and a confirmed reading of 26", the
    check is judged on the reading: the operand it recorded is the reading's, and it FAILs on it."""
    client = _client(session)
    package, revision = _package(session)
    _depth_settings(client, package)
    _read(session, client, package, CT010='26"')
    _typed_before(session, revision, **{"CT-DEPTH-001__countertop_depth": Fraction(51, 2)})

    _run(session, revision)
    finding = _findings(client, package)["CT-DEPTH-001"]

    used = _used(session, finding, "countertop_depth")
    assert Fraction(used.value_numerator, used.value_denominator) == 26
    assert used.canonical_observation_id is not None, "the reading, not the typed value"
    assert finding["outcome"] == "FAIL", finding
    assert NO_LONGER_COUNTS not in (finding["reason"] or "")


def test_typed_inputs_that_are_not_drawn_are_unchanged(session: Session) -> None:
    """The site width (USER_INPUT), a typed list (cabinet runs) and a run setting typed beside an
    old drawn value all still count, exactly as typed; only the drawn one is left out."""
    _package_id, revision = _package(session)
    _typed_before(
        session,
        revision,
        **{
            "CAB-FILLER-001__field_width": Fraction(120),
            "CT-WIDTH-001__cabinet_widths#0": Fraction(30),
            "CT-WIDTH-001__cabinet_widths#1": Fraction(36),
            "CT-WIDTH-001__countertop_width": Fraction(66),
            "sink_interior_width": Fraction(15),
        },
    )

    typed = operands_for(session, revision)

    field = typed["CAB-FILLER-001"]["field_width"]
    assert field.value.exact == 120  # type: ignore[union-attr]
    run = typed["CT-WIDTH-001"]["cabinet_widths"]
    assert [m.exact for m in run.value] == [30, 36]  # type: ignore[union-attr]
    assert "countertop_width" not in typed["CT-WIDTH-001"]
    layer = run_parameters_for(session, revision)
    assert layer is not None
    assert layer.parameters["sink_interior_width"].value.value == 15


def test_the_sentence_is_plain_and_says_what_to_do() -> None:
    """The reason is one plain sentence a reviewer can act on: what happened, and what clears it."""
    sentence = TYPED_DRAWN_STOPPED.format(inputs="countertop_depth")
    assert NO_LONGER_COUNTS in sentence
    assert "Confirm a reading" in sentence
    assert "{" not in sentence
