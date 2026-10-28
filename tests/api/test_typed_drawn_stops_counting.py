"""Drawn numbers typed before confirm-only stop counting (#1303).

Since #1253 a number drawn on the vendor's or the architect's drawing is confirmed from a reading and
never typed. Values typed before that stayed in the database and still counted, outranking a later
confirmed reading. Decided 2026-10-15: they stop counting. A drawn value must come from a confirmed
reading.

* A drawn field holding only an old typed value: the check waits (NOT_FOUND), and its reason says
  the typed value no longer counts and why. The typed value stays stored (append-only).
* An old typed value and a reading of the same number: the reading is used, as before.
* An old typed value and a reading of a different number (person-confirmed or corroborated by two
  readers): neither decides. The check waits for a person (REVIEW_REQUIRED), naming both values, so
  stopping the typed value can never turn a FAIL into a PASS, or a PASS into a FAIL.
* A value typed by design (the site width, a typed list, a setting) is unchanged.

"Drawn" is #1253's own definition, `workflow.measurements.confirm_only`: one value on a SHOP or ARCH
drawing. Every check runs through the real engine (`DatabaseStages.run_checks`). Every number and
name is synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from fractions import Fraction
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import to_rows
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.schema import Quantity
from tests.api.test_waiting_values import (
    PROJECT,
    _package,
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
    _typed_before_in(session, revision, "in", values)


def _typed_before_in(
    session: Session, revision: UUID, unit: str, values: dict[str, Fraction]
) -> None:
    """`_typed_before`, each number in `unit`."""
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
                value=Quantity(value=value, unit=Unit(unit)),
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


def test_agrees_needs_the_same_number_and_the_same_unit() -> None:
    """#1307: `agrees` directly. Same number and unit agrees; same number in another unit, or the
    same length written in the other unit, does not."""
    from units.measurement import Measurement
    from workflow.measurements import TypedDrawn, agrees

    when = datetime(2026, 10, 1, tzinfo=UTC)
    reading = Measurement(Fraction(51, 2), Unit.INCH, '25 1/2"')

    def typed(value: Fraction, unit: Unit) -> TypedDrawn:
        return TypedDrawn(value=Measurement(value, unit, None), set_at=when)

    assert agrees(typed(Fraction(51, 2), Unit.INCH), reading)
    assert not agrees(typed(Fraction(51, 2), Unit.MM), reading)
    assert not agrees(typed(Fraction(6477, 10), Unit.MM), reading)
    assert not agrees(TypedDrawn(value=None, set_at=when), reading)


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
