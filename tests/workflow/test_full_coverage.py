"""A reviewer fills in everything the rulebook asks for, and the checks decide.

**The property under test is negative and it is the point:** no check may abstain because of a field
nothing offered. Two do abstain, and both are waiting on the client rather than on the form — this
file asserts *which* two and *why*, so that a third joining them is a failure rather than a shrug.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from datetime import UTC, datetime
from fractions import Fraction

import pytest
import yaml
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import (
    CheckRun,
    Finding,
    Package,
    PackageRevision,
    PackageState,
    Project,
    RuleDefinition,
    RuleSnapshot,
)
from app.models.parameters import to_rows
from app.verdicts.rulebook import from_row
from rules.parameters import ParameterLayer, Provenance
from rules.parameters import ParameterSet as InMemoryParameterSet
from rules.parameters import ParameterValue as InMemoryParameterValue
from rules.schema import Quantity, Rule
from rules.snapshot import publish
from tests.app.postgres_fixture import alembic_config
from units.measurement import Unit
from units.normalise import normalise_to_inches
from workflow.measurements import operands_for, run_parameters_for
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

RULEBOOK = pathlib.Path(__file__).resolve().parents[2] / "rules" / "rulebook"

#: The layout this package describes: three cabinets, a filler either side, against a back wall.
#: 24 + 30 + 36 = 90 of cabinet, 2 + 2 = 4 of filler, and `back_only` means no field cut — so the
#: countertop is 94 inches. Chosen to add up, because a package that did not would test the
#: arithmetic rather than the coverage.
#:
#: **Deliberately not a palindrome.** The first version of this was `24, 36, 24`, and reversing the
#: stored order then changed nothing — the test that claims to pin the layout order could not detect
#: a reversal, which mutation found. Order matters because `CAB-ARCH-VS-SHOP-001` compares two runs
#: position by position.
CABINETS = ('24"', '30"', '36"')
FILLERS = ('2"', '2"')

#: Everything a reviewer reads off a drawing, keyed the way the API stores it.
MEASUREMENTS: dict[str, str | tuple[str, ...]] = {
    "CT-DEPTH-001:countertop_depth": '25 1/2"',
    "CT-BACK-OFFSET-MIN-001:countertop_depth": '25 1/2"',
    "CT-BACK-OFFSET-MIN-001:front_offset": '4"',
    "CT-BACK-OFFSET-MIN-001:sink_depth": '15 1/2"',
    "CT-SINK-OFFSET-FRONT-001:front_offset": '4"',
    "CT-SINK-CUTOUT-DEPTH-001:cutout_depth": '15 1/2"',
    "CT-SINK-CUTOUT-WIDTH-001:cutout_width": '29 1/2"',
    "CT-WIDTH-001:countertop_width": '94"',
    "CT-WIDTH-001:cabinet_widths": CABINETS,
    "CT-WIDTH-001:filler_widths": FILLERS,
    "CAB-ARCH-VS-SHOP-001:architectural_cabinets": CABINETS,
    "CAB-ARCH-VS-SHOP-001:shop_cabinets": CABINETS,
    "CAB-FILLER-001:field_width": '94"',
    "CAB-FILLER-001:design_width": '94"',
    "CAB-FILLER-001:design_fillers": FILLERS,
    "CAB-FILLER-001:proposed_fillers": FILLERS,
}

PROJECT_PARAMETERS = {"cabinet_depth": '24"', "countertop_overhang": '1 1/2"', "field_cut": '0"'}

#: Off the sink's cut sheet, true for this review only.
RUN_PARAMETERS = {"sink_interior_depth": '16"', "sink_interior_width": '30"'}

DISCRIMINATORS = {"wall_config": "back_only", "filler_symmetry": "equal_unless_noted"}

#: The two checks that cannot decide, and the reason each is waiting on somebody outside this repo.
#:
#: Named individually rather than counted, because "two abstain" would stay true if a different two
#: abstained — including a pair that abstained for a reason the form could have fixed.
CLIENT_BLOCKED = {
    "CT-BACK-OFFSET-MIN-001": "back_offset_minimum",
    "CAB-ARCH-VS-SHOP-001": "tolerance",
}


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


def _publish_rulebook(session: Session) -> int:
    published = 0
    for path in sorted(RULEBOOK.glob("*.yaml")):
        rule = Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        snapshot = publish(rule)
        # Column names taken from `scripts/run_checks.py`, which does this for real, rather than
        # remembered — `RuleDefinition` carries only `rule_id`, and inventing fields here is a
        # mistake this repository has made before.
        definition = RuleDefinition(rule_id=rule.id)
        session.add(definition)
        session.flush()
        session.add(
            RuleSnapshot(
                rule_definition_id=definition.id,
                snapshot_id=snapshot.snapshot_id,
                version=rule.version,
                canonical_json=snapshot.canonical_json,
                product_type=rule.product_type.value,
                check_type=rule.check_type.value,
                unconfirmed_tolerance_count=0,
            )
        )
        published += 1
    session.flush()
    return published


def _revision(session: Session) -> PackageRevision:
    project = Project(name="full coverage")
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


def _store(
    session: Session, revision: PackageRevision, layer: ParameterLayer, values: dict
) -> None:
    package = session.get(Package, revision.package_id)
    assert package is not None
    now = datetime.now(UTC)
    flattened: dict[str, InMemoryParameterValue] = {}
    for name, raw in values.items():
        entries = raw if isinstance(raw, tuple) else (raw,)
        for index, text in enumerate(entries):
            measurement = normalise_to_inches(text)
            key = f"{name}#{index}" if isinstance(raw, tuple) else name
            flattened[key] = InMemoryParameterValue(
                value=Quantity(value=measurement.exact, unit=Unit.INCH),
                provenance=Provenance.MEASURED,
                set_by="test reviewer",
                set_at=now,
            )
    stored, rows = to_rows(
        InMemoryParameterSet(
            project_id=str(package.project_id), layer=layer, version=1, parameters=flattened
        )
    )
    session.add(stored)
    for row in rows:
        session.add(row)
    session.flush()


def _outcomes(session: Session, revision: PackageRevision) -> dict[str, str]:
    rows = session.execute(
        select(Finding, RuleSnapshot)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .where(Finding.package_revision_id == revision.id)
    ).all()
    return {from_row(snapshot).rule.id: str(finding.outcome) for finding, snapshot in rows}


@pytest.fixture
def filled(session: Session) -> PackageRevision:
    """A package with every field the rulebook asks for, filled by a reviewer."""
    revision = _revision(session)
    _publish_rulebook(session)
    _store(session, revision, ParameterLayer.PROJECT, PROJECT_PARAMETERS)
    _store(session, revision, ParameterLayer.RUN, {**RUN_PARAMETERS, **MEASUREMENTS})
    return revision


def test_every_check_that_can_decide_does(session: Session, filled: PackageRevision) -> None:
    """**The acceptance property: nothing abstains for want of a field.**

    Six of eight reach PASS or FAIL. The other two are waiting on the client, and this asserts the
    membership of that set rather than its size — a third rule joining it would otherwise pass here
    while a reviewer stared at an abstention they could have fixed.
    """
    operands = operands_for(session, filled.id)
    DatabaseStages(operands=operands, discriminators=DISCRIMINATORS).run_checks(session, filled.id)

    outcomes = _outcomes(session, filled)
    undecided = {rule for rule, outcome in outcomes.items() if outcome not in ("PASS", "FAIL")}

    assert undecided == set(CLIENT_BLOCKED), (
        "the set of checks that cannot decide has changed. Every member must be waiting on a client "
        f"value, not on a form field: {sorted(undecided)}"
    )
    assert len(outcomes) == 8


def test_the_two_that_cannot_decide_say_why_in_client_terms(
    session: Session, filled: PackageRevision
) -> None:
    """Each abstention names the missing client value, not a system fault.

    `CT-BACK-OFFSET-MIN-001` wants a vendor minimum nobody has given; `CAB-ARCH-VS-SHOP-001` wants a
    tolerance the client has not set, and *"an unset tolerance is not zero"*. Both messages tell a
    reviewer who to ask, which is the difference between a useful abstention and a dead end.
    """
    operands = operands_for(session, filled.id)
    DatabaseStages(operands=operands, discriminators=DISCRIMINATORS).run_checks(session, filled.id)

    rows = session.execute(
        select(Finding, RuleSnapshot)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .where(Finding.package_revision_id == filled.id)
    ).all()

    for finding, snapshot in rows:
        rule_id = from_row(snapshot).rule.id
        if rule_id not in CLIENT_BLOCKED:
            continue
        trace = finding.trace or {}
        said = str(trace.get("reason") or trace.get("comparison") or "")
        assert (
            CLIENT_BLOCKED[rule_id] in said
        ), f"{rule_id} abstained without naming {CLIENT_BLOCKED[rule_id]!r}: {said!r}"


def test_a_many_valued_input_keeps_its_order(session: Session, filled: PackageRevision) -> None:
    """**The order is the layout, and two rules compare runs position by position.**

    Stored as `#0` upward and regrouped into a tuple. Reversed, `CAB-ARCH-VS-SHOP-001` would compare
    the leftmost architectural cabinet against the rightmost shop cabinet — and on a symmetric run
    like this one it would still pass, which is why the assertion is on the values and not on the
    verdict.
    """
    operands = operands_for(session, filled.id)

    widths = operands["CT-WIDTH-001"]["cabinet_widths"].value
    assert isinstance(widths, tuple)
    assert [w.exact for w in widths] == [Fraction(24), Fraction(30), Fraction(36)]


def test_a_run_scope_parameter_reaches_the_resolver(
    session: Session, filled: PackageRevision
) -> None:
    """**The gap that made both sink checks unanswerable.**

    `load_parameter_sets` covers GLOBAL and PROJECT only — "RUN sets are supplied per review and are
    not loaded here" — so `sink_interior_width` reached no resolver and the cutout checks abstained
    however carefully the cut sheet was typed.
    """
    run_layer = run_parameters_for(session, filled.id)

    assert run_layer is not None
    assert set(run_layer.parameters) == set(RUN_PARAMETERS), (
        "the run layer carries something other than the run-scope parameters; measurement keys "
        "belong to operands_for, not to the resolver"
    )


def test_without_a_discriminator_the_two_variant_rules_cannot_decide(
    session: Session, filled: PackageRevision
) -> None:
    """A rule whose variant nobody stated abstains, however complete the measurements are.

    This is what made `wall_config` and `filler_symmetry` fields rather than an oversight: they are
    judgements a reviewer makes from the drawing, and the resolver refuses to guess one.
    """
    operands = operands_for(session, filled.id)
    DatabaseStages(operands=operands).run_checks(session, filled.id)

    outcomes = _outcomes(session, filled)
    assert outcomes["CT-WIDTH-001"] not in ("PASS", "FAIL")
    assert outcomes["CAB-FILLER-001"] not in ("PASS", "FAIL")
