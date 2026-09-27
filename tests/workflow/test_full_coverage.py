"""A reviewer fills in everything the rulebook asks for, and the checks decide.

**The property under test is negative and it is the point:** no check may abstain because of a field
nothing offered. If one abstains, this file asserts *which* one and *why*, so that a new abstention is
a failure rather than a shrug.
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
from workflow.classifications import record_classifications
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

#: The reviewer's classification, one per cabinet, in the same order — the 36" cabinet is the sink
#: cabinet, which is equipment and must not be resized. Entered by a person, never inferred (deck
#: slide 11).
CABINET_TYPES = ("single_door", "double_door", "equipment")

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
    "CAB-FILLER-001:architectural_fillers": FILLERS,
    "CAB-FILLER-001:shop_fillers": FILLERS,
    # v2 compares the cabinet run too, not just the fillers.
    "CAB-FILLER-001:architectural_cabinets": CABINETS,
    "CAB-FILLER-001:shop_cabinets": CABINETS,
    # `cabinet_type` is not here because it is not a measurement: it has no unit, and it is written
    # to `item_classifications` by the fixture below (#684).
    # The sink cabinet, from the deck's own relation (#537). **It is the 36" cabinet in `CABINETS`,
    # not a fourth cabinet from nowhere.** The first version of this row said 35", which added up
    # inside its own rule and described a run that did not contain the cabinet it had just checked —
    # and a 29 1/2" cutout does not fit a 30" cabinet at all. Two CRITICAL checks would both have
    # passed on a package no fabricator could build, which is the false-PASS shape this file exists
    # to catch rather than to contain. Found in review on #537.
    #
    #   3/4 panel + 2 1/2 clearance + 29 1/2 cutout + 2 1/2 clearance + 3/4 panel = 36
    #
    # The cutout is the same measurement `CT-SINK-CUTOUT-WIDTH-001` reads — one quantity a reviewer
    # measures once, which is why `required-inputs` groups the form by quantity, not by rule.
    "CT-SINK-CABINET-WIDTH-001:sink_cabinet_width": '36"',
    "CT-SINK-CABINET-WIDTH-001:clearance_left": '2 1/2"',
    "CT-SINK-CABINET-WIDTH-001:cutout_width": '29 1/2"',
    "CT-SINK-CABINET-WIDTH-001:clearance_right": '2 1/2"',
}

PROJECT_PARAMETERS = {
    # The distribution bounds. **v2 of CAB-FILLER-001 carries no defaults** — CLIENT_FACTS Q21 has
    # the filler pair at three different values and the per-type cabinet bounds have never been
    # given, so every one is a form field. These are this package's numbers, not the rulebook's:
    # the point of the change is that a package states them and nobody's guess is applied silently.
    "filler_min": '1"',
    "filler_max": '2"',
    "single_door_cab_width_min": '9"',
    "single_door_cab_width_max": '36"',
    "double_door_cab_width_min": '24"',
    "double_door_cab_width_max": '48"',
    "drawer_cab_width_min": '12"',
    "drawer_cab_width_max": '36"',
    "cabinet_depth": '24"',
    "countertop_overhang": '1 1/2"',
    "field_cut": '0"',
    # Both `Specified` in the countertop deck's acquisition column — the client gives them per
    # project, so they are form fields with no default (#537). Without them the back-offset rule
    # abstains on the backsplash before it ever reaches the value the client still owes, which is
    # how this test caught them being missing.
    "backsplash_thickness": '3/4"',
    "cabinet_side_thickness": '3/4"',
}

#: Off the sink's cut sheet, true for this review only.
RUN_PARAMETERS = {"sink_interior_depth": '16"', "sink_interior_width": '30"'}

DISCRIMINATORS = {"wall_config": "back_only", "filler_symmetry": "equal_unless_noted"}

#: Checks that cannot decide, the text their abstention must contain, and who has to act.
#:
#: Named individually rather than counted, because an unchanged count could conceal a different rule
#: abstaining for a reason the form could have fixed.
#:
#: **`owed_by` is the part that matters.** A rule waiting on a client value and a rule waiting on
#: something we have not built are both abstentions, and telling them apart is the difference
#: between "chase Raj" and "finish the work". Entering `ours` here is an admission with a date on
#: it, not a way to make a test green.
UNDECIDED: dict[str, tuple[str, str]] = {}

#: Kept for the tests that read it: the subset genuinely waiting on the client.
CLIENT_BLOCKED: dict[str, str] = {
    rule: missing for rule, (missing, owed_by) in UNDECIDED.items() if owed_by == "client"
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
    # The one reviewer input that is not a dimension (#684). It goes to its own table because a
    # category has no unit and `parameter_values` is numeric to the column level.
    record_classifications(
        session,
        package_revision_id=revision.id,
        rule_id="CAB-FILLER-001",
        input_name="cabinet_type",
        categories=CABINET_TYPES,
        confirmed_by="reviewer",
    )
    session.flush()
    return revision


def test_every_check_that_can_decide_does(session: Session, filled: PackageRevision) -> None:
    """**The acceptance property: nothing abstains for want of a field.**

    Every rule should reach PASS or FAIL. If a future rule is waiting on the client, this asserts the
    membership of that set rather than its size — another rule joining it would otherwise pass here
    while a reviewer stared at an abstention they could have fixed.

    The count comes from the rulebook rather than a literal, because a literal is a number somebody
    has to remember to change: authoring `CT-SINK-CABINET-WIDTH-001` from the countertop deck (#537)
    made this fail at `9 == 8` while the rule itself was working correctly. What matters is that
    every published rule produced a finding, which is the same property either way.
    """
    operands = operands_for(session, filled.id)
    DatabaseStages(operands=operands, discriminators=DISCRIMINATORS).run_checks(session, filled.id)

    outcomes = _outcomes(session, filled)
    undecided = {rule for rule, outcome in outcomes.items() if outcome not in ("PASS", "FAIL")}

    assert undecided == set(UNDECIDED), (
        "the set of checks that cannot decide has changed. Every member must be listed in UNDECIDED "
        f"with what it waits on and who owes it: {sorted(undecided)}"
    )
    # The same glob `_publish_rulebook` iterates, so the test cannot disagree about how many rules
    # there are.
    published = len(list(RULEBOOK.glob("*.yaml")))
    assert len(outcomes) == published, (
        f"{published} rules are published but {len(outcomes)} produced a finding. A rule that "
        "produced no row is a check the reviewer cannot tell did not run."
    )


def test_any_check_that_cannot_decide_says_why_in_client_terms(
    session: Session, filled: PackageRevision
) -> None:
    """Each abstention names the missing client value, not a system fault.

    `CT-BACK-OFFSET-MIN-001` uses its V1 default; `CAB-ARCH-VS-SHOP-001` now uses Q2's exact-match
    answer. This test stays as a guard for the next genuinely client-blocked rule: the message must
    tell a reviewer who to ask, which is the difference between a useful abstention and a dead end.
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
        if rule_id not in UNDECIDED:
            continue
        missing, _owed_by = UNDECIDED[rule_id]
        trace = finding.trace or {}
        said = str(trace.get("reason") or trace.get("comparison") or "")
        assert missing in said, f"{rule_id} abstained without naming {missing!r}: {said!r}"


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


def test_without_a_discriminator_a_variant_rule_cannot_decide(
    session: Session, filled: PackageRevision
) -> None:
    """A rule whose variant nobody stated abstains, however complete the measurements are.

    This is what made `wall_config` a field rather than an oversight: it is a judgement a reviewer
    makes from the drawing, and the resolver refuses to guess one.

    **CAB-FILLER-001 used to be the second half of this test and is deliberately not any more.**
    v2 of the rule is global (#681): its `filler_symmetry` discriminator selected an
    `allow_asymmetric` flag the two-step operation does not have, so the question moved into the
    arithmetic, which abstains on unequal fillers *that have to move* with the numbers in hand.
    It decides here, with no discriminator stated, and that is the change working.
    """
    operands = operands_for(session, filled.id)
    DatabaseStages(operands=operands).run_checks(session, filled.id)

    outcomes = _outcomes(session, filled)
    assert outcomes["CT-WIDTH-001"] not in ("PASS", "FAIL")
    assert outcomes["CAB-FILLER-001"] in ("PASS", "FAIL")


#: Raj's own worked example, slide 4 — the layout his deck uses to show how they do it.
#: 3 + 24 + 36 + 24 + 3 = 90 on the architectural drawing; the site measures 82.
RAJ_CABINETS = ('24"', '36"', '24"')
RAJ_FILLERS = ('3"', '3"')
RAJ_TYPES = ("double_door", "equipment", "double_door")


def test_rajs_worked_example_produces_his_answer_through_run_checks(session: Session) -> None:
    """**The whole point of #676, #678, #681 and #684, asserted in one place.**

    Not through the operation, and not through the reviewer's calculator — through the check a
    package actually runs. Slide 4 says the fillers go to 2" and the two regular cabinets to 21",
    with the equipment cabinet holding 36". Until the classification could be stored, this run
    abstained: the arithmetic was right and nobody could tell it which cabinet was the sink cabinet.
    """
    revision = _revision(session)
    _publish_rulebook(session)
    _store(
        session,
        revision,
        ParameterLayer.PROJECT,
        # Raj's example moves two double-door cabinets from 24" to 21", so the package has to
        # allow that. The fixture's own minimum is 24" and correctly refuses it — a reminder that
        # the answer is only right within the bounds the package states.
        {
            **PROJECT_PARAMETERS,
            "filler_min": '2"',
            "filler_max": '3"',
            "double_door_cab_width_min": '18"',
        },
    )
    _store(
        session,
        revision,
        ParameterLayer.RUN,
        {
            **RUN_PARAMETERS,
            "CAB-FILLER-001:field_width": '82"',
            "CAB-FILLER-001:design_width": '90"',
            "CAB-FILLER-001:architectural_fillers": RAJ_FILLERS,
            "CAB-FILLER-001:shop_fillers": RAJ_FILLERS,
            "CAB-FILLER-001:architectural_cabinets": RAJ_CABINETS,
            "CAB-FILLER-001:shop_cabinets": RAJ_CABINETS,
        },
    )
    record_classifications(
        session,
        package_revision_id=revision.id,
        rule_id="CAB-FILLER-001",
        input_name="cabinet_type",
        categories=RAJ_TYPES,
        confirmed_by="reviewer",
    )
    session.flush()

    operands = operands_for(session, revision.id)
    DatabaseStages(operands=operands, discriminators=DISCRIMINATORS).run_checks(
        session, revision.id
    )

    finding = next(
        row
        for row, snapshot in session.execute(
            select(Finding, RuleSnapshot)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
            .where(Finding.package_revision_id == revision.id)
        ).all()
        if from_row(snapshot).rule.id == "CAB-FILLER-001"
    )
    trace = finding.trace or {}
    facts = dict(trace.get("intermediates") or [])

    # The shop drawing still shows the architectural widths, so it FAILs — and what it should say
    # is the answer Raj wrote down.
    assert finding.outcome == "FAIL"
    # Rendered the way the trace stores a run: each value with its unit, in order.
    assert facts["expected_fillers"] == "2 in, 2 in"
    assert facts["expected_cabinets"] == "21 in, 36 in, 21 in"
    assert facts["condition"] == "cabinets_absorb_remainder"

    # And the reviewer is told why, in the deck's own terms (#682).
    said = str(trace.get("explanation") or "")
    for figure in ('90"', '82"', '8"', '2"', '6"', '36"', '24"', '21"'):
        assert figure in said, f"{figure} is missing from the explanation: {said}"


def test_the_equipment_cabinet_a_reviewer_named_is_the_one_that_holds(
    session: Session,
) -> None:
    """Move the classification and the arithmetic follows it — nothing infers from the width.

    With the *first* cabinet marked as equipment, 24" is what holds and the 36" cabinet moves. The
    numbers are otherwise identical, so this fails if anything anywhere decided "the widest one is
    the appliance".
    """
    revision = _revision(session)
    _publish_rulebook(session)
    _store(
        session,
        revision,
        ParameterLayer.PROJECT,
        # Raj's example moves two double-door cabinets from 24" to 21", so the package has to
        # allow that. The fixture's own minimum is 24" and correctly refuses it — a reminder that
        # the answer is only right within the bounds the package states.
        {
            **PROJECT_PARAMETERS,
            "filler_min": '2"',
            "filler_max": '3"',
            "double_door_cab_width_min": '18"',
        },
    )
    _store(
        session,
        revision,
        ParameterLayer.RUN,
        {
            **RUN_PARAMETERS,
            "CAB-FILLER-001:field_width": '82"',
            "CAB-FILLER-001:design_width": '90"',
            "CAB-FILLER-001:architectural_fillers": RAJ_FILLERS,
            "CAB-FILLER-001:shop_fillers": RAJ_FILLERS,
            "CAB-FILLER-001:architectural_cabinets": RAJ_CABINETS,
            "CAB-FILLER-001:shop_cabinets": RAJ_CABINETS,
        },
    )
    record_classifications(
        session,
        package_revision_id=revision.id,
        rule_id="CAB-FILLER-001",
        input_name="cabinet_type",
        categories=("equipment", "double_door", "double_door"),
        confirmed_by="reviewer",
    )
    session.flush()

    operands = operands_for(session, revision.id)
    DatabaseStages(operands=operands, discriminators=DISCRIMINATORS).run_checks(
        session, revision.id
    )

    finding = next(
        row
        for row, snapshot in session.execute(
            select(Finding, RuleSnapshot)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
            .where(Finding.package_revision_id == revision.id)
        ).all()
        if from_row(snapshot).rule.id == "CAB-FILLER-001"
    )
    facts = dict((finding.trace or {}).get("intermediates") or [])

    # 24" holds; 36" and 24" each give up 3".
    assert facts["expected_cabinets"] == "24 in, 33 in, 21 in"
