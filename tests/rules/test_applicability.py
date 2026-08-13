"""Resolution must never answer with silence.

The failure these tests exist to prevent is not a wrong tolerance — it is an item that no rule
covered, coming back indistinguishable from one that was checked and was fine. So most of what
follows asserts that something was *said* about an uncovered scope, and that what was said
sends a reviewer to the right place.
"""

from __future__ import annotations

import pytest

from rules.applicability import (
    Abstention,
    ApplicableRule,
    CheckContext,
    Resolution,
    resolve,
)
from rules.project import ProjectScope
from rules.schema import (
    Applicability,
    ApplicabilityVariant,
    Cardinality,
    CheckType,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Quantity,
    Rule,
    Tolerance,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType, WallConfig
from rules.snapshot import SnapshotStore, publish
from units.measurement import Unit
from verdict.outcomes import Outcome, Severity

THREE_WALL = WallConfig.BACK_LEFT_RIGHT.value
ISLAND = WallConfig.ISLAND.value


def _wall_config(*, tolerance: str = "1/8") -> Applicability:
    """The client's only authored layout: walls on three sides."""
    return Applicability(
        discriminator="wall_config",
        variants=(
            ApplicabilityVariant(
                when=THREE_WALL,
                tolerance=Tolerance(value=tolerance, unit=Unit.INCH),
                extras={"field_cut_count": 1},
            ),
        ),
    )


def _rule(
    *,
    rule_id: str = "CT-WIDTH-001",
    version: str = "1.0.0",
    product_type: ProductType = ProductType.COUNTERTOP,
    applicability: Applicability | GlobalApplicability | None = None,
) -> Rule:
    return Rule(
        id=rule_id,
        version=version,
        product_type=product_type,
        check_type=CheckType.INTERNAL,
        severity=Severity.CRITICAL,
        arithmetic_unit=Unit.MM,
        inputs={
            "countertop_width": InputSelector(
                source=OperandSource.SHOP,
                semantic_type=SemanticType.COUNTERTOP_OVERALL_WIDTH,
                cardinality=Cardinality.ONE,
            )
        },
        applicability=applicability if applicability is not None else _wall_config(),
        operation=OperationRef(type="exists", operands={"x": "countertop_width"}),
    )


def _store(*rules: Rule) -> SnapshotStore:
    store = SnapshotStore()
    for rule in rules:
        store.add(publish(rule))
    return store


def _project(project_id: str = "PRJ-1", **overrides: Quantity) -> ProjectScope:
    return ProjectScope(project_id=project_id, parameter_overrides=overrides)


def _context(
    *,
    product_type: ProductType = ProductType.COUNTERTOP,
    wall_config: str | None = THREE_WALL,
    project: ProjectScope | None = None,
) -> CheckContext:
    discriminators = {} if wall_config is None else {"wall_config": wall_config}
    return CheckContext(
        product_type=product_type,
        project=project if project is not None else _project(),
        discriminators=discriminators,
    )


# ---------------------------------------------------------------------------
# The happy path, stated once so the failures below are legible
# ---------------------------------------------------------------------------


def test_a_three_wall_countertop_resolves_to_its_variant() -> None:
    resolution = resolve(_store(_rule()), _context())

    assert resolution.is_fully_covered
    (applied,) = resolution.applicable
    assert applied.rule_id == "CT-WIDTH-001"
    assert applied.variant is not None
    assert applied.variant.when == THREE_WALL
    assert applied.variant.extras == {"field_cut_count": 1}


def test_resolution_is_reproducible_from_the_context_alone() -> None:
    """Same store, same context, same answer — no clock, no state, no inference."""
    store, context = _store(_rule()), _context()

    assert resolve(store, context) == resolve(store, context)


# ---------------------------------------------------------------------------
# An island countertop cannot render as clean — the criterion this issue exists for
# ---------------------------------------------------------------------------


def test_an_island_countertop_does_not_render_as_clean() -> None:
    """The whole point of ADR-0004.

    Only the three-wall layout has an authored rule, so an island matches nothing. If that
    produced an empty result, a reviewer would see no findings and conclude the countertop was
    checked and was fine.
    """
    resolution = resolve(_store(_rule()), _context(wall_config=ISLAND))

    assert resolution.applicable == ()
    assert not resolution.is_fully_covered, "an unchecked island must not look fully covered"

    (abstention,) = resolution.abstentions
    assert abstention.outcome is Outcome.NO_APPLICABLE_RULE
    assert abstention.rule_id == "CT-WIDTH-001"
    assert "island" in abstention.reason


def test_an_island_still_counts_towards_coverage() -> None:
    """ "We checked 0 of 1" must be reportable, rather than implied by silence."""
    resolution = resolve(_store(_rule()), _context(wall_config=ISLAND))

    assert resolution.checked_count == 0
    assert resolution.considered_count == 1


def test_no_rule_at_all_for_the_product_type_is_still_said_out_loud() -> None:
    resolution = resolve(_store(_rule()), _context(product_type=ProductType.CABINET))

    assert resolution.applicable == ()
    (abstention,) = resolution.abstentions
    assert abstention.outcome is Outcome.NO_APPLICABLE_RULE
    assert abstention.rule_id is None, "no single rule is to blame when none are published"
    assert "cabinet" in abstention.reason


# ---------------------------------------------------------------------------
# The two abstentions are different, and must stay different
# ---------------------------------------------------------------------------


def test_an_unestablished_discriminator_is_review_required_not_no_applicable_rule() -> None:
    """We do not know which rule *would* apply, so we cannot claim none exists."""
    resolution = resolve(_store(_rule()), _context(wall_config=None))

    assert resolution.applicable == ()
    (abstention,) = resolution.abstentions
    assert abstention.outcome is Outcome.REVIEW_REQUIRED
    assert abstention.outcome is not Outcome.NO_APPLICABLE_RULE
    assert "wall_config" in abstention.reason


def test_the_two_abstentions_send_a_reviewer_to_different_places() -> None:
    """Same abstention, different instruction: the drawing, or the rulebook.

    Folding these together would send reviewers hunting a drawing for a dimension when the
    real gap is that no rule was ever written.
    """
    unknown_layout = resolve(_store(_rule()), _context(wall_config=None))
    uncovered_layout = resolve(_store(_rule()), _context(wall_config=ISLAND))

    assert unknown_layout.abstentions[0].outcome is Outcome.REVIEW_REQUIRED
    assert uncovered_layout.abstentions[0].outcome is Outcome.NO_APPLICABLE_RULE


def test_the_layout_is_never_guessed() -> None:
    """With one variant authored, guessing would be right often enough to be tempting."""
    resolution = resolve(_store(_rule()), _context(wall_config=None))

    assert resolution.applicable == ()


def test_an_abstention_cannot_carry_a_decisive_outcome() -> None:
    for outcome in (Outcome.PASS, Outcome.FAIL):
        with pytest.raises(ValueError, match="decides nothing"):
            Abstention(outcome=outcome, reason="...")


# ---------------------------------------------------------------------------
# Effective version — the highest, and only the highest
# ---------------------------------------------------------------------------


def test_the_highest_version_wins() -> None:
    store = _store(
        _rule(version="1.0.0", applicability=_wall_config(tolerance="1/8")),
        _rule(version="1.1.0", applicability=_wall_config(tolerance="1/16")),
    )

    (applied,) = resolve(store, _context()).applicable

    assert applied.snapshot.version == "1.1.0"
    assert applied.variant is not None
    assert str(applied.variant.tolerance.value) == "1/16"


def test_version_ten_beats_version_nine() -> None:
    """String ordering puts "1.0.10" below "1.0.9" — a silently wrong newest."""
    store = _store(_rule(version="1.0.9"), _rule(version="1.0.10"))

    (applied,) = resolve(store, _context()).applicable

    assert applied.snapshot.version == "1.0.10"


def test_the_resolved_snapshot_is_carried_so_a_finding_can_pin_it() -> None:
    """ADR-0005: a review is reproduced by replaying snapshot hashes, not by re-resolving."""
    (applied,) = resolve(_store(_rule()), _context()).applicable

    assert applied.snapshot.snapshot_id.startswith("sha256:")
    applied.snapshot.verify()


# ---------------------------------------------------------------------------
# Rules do not compete
# ---------------------------------------------------------------------------


def test_every_applicable_rule_is_returned() -> None:
    """Width, depth and sink checks all apply to the same countertop."""
    store = _store(
        _rule(rule_id="CT-WIDTH-001"),
        _rule(rule_id="CT-DEPTH-001"),
        _rule(rule_id="CT-SINK-001"),
    )

    resolution = resolve(store, _context())

    assert {a.rule_id for a in resolution.applicable} == {
        "CT-WIDTH-001",
        "CT-DEPTH-001",
        "CT-SINK-001",
    }


def test_the_result_does_not_depend_on_the_order_rules_were_published() -> None:
    """A resolver whose answer depends on load order is not deterministic in any useful sense."""
    forwards = _store(
        _rule(rule_id="CT-WIDTH-001"),
        _rule(rule_id="CT-DEPTH-001"),
        _rule(rule_id="CT-SINK-001"),
    )
    backwards = _store(
        _rule(rule_id="CT-SINK-001"),
        _rule(rule_id="CT-DEPTH-001"),
        _rule(rule_id="CT-WIDTH-001"),
    )

    assert resolve(forwards, _context()) == resolve(backwards, _context())


def test_one_rule_abstaining_does_not_suppress_another() -> None:
    """No firing order, so an abstention is not a stopping condition."""
    store = _store(
        _rule(rule_id="CT-WIDTH-001"),
        _rule(rule_id="CT-GLOBAL-001", applicability=GlobalApplicability(scope="global")),
    )

    resolution = resolve(store, _context(wall_config=ISLAND))

    assert [a.rule_id for a in resolution.applicable] == ["CT-GLOBAL-001"]
    assert [a.rule_id for a in resolution.abstentions] == ["CT-WIDTH-001"]


# ---------------------------------------------------------------------------
# A global rule must say it is global
# ---------------------------------------------------------------------------


def test_a_rule_declared_global_applies_without_a_discriminator() -> None:
    store = _store(_rule(applicability=GlobalApplicability(scope="global")))

    (applied,) = resolve(store, _context(wall_config=None)).applicable

    assert applied.variant is None


def test_a_rule_that_omits_its_applicability_does_not_publish() -> None:
    """Absence is never silently a positive.

    A forgotten discriminator read as "applies to everything" would apply one layout's
    tolerance to every layout — worse than applying it to none.
    """
    with pytest.raises(ValueError):
        Rule(
            id="CT-WIDTH-001",
            version="1.0.0",
            product_type=ProductType.COUNTERTOP,
            check_type=CheckType.INTERNAL,
            severity=Severity.CRITICAL,
            arithmetic_unit=Unit.MM,
            operation=OperationRef(type="exists"),
        )  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Product type is a controlled vocabulary, matched exactly
# ---------------------------------------------------------------------------


def test_a_product_type_outside_the_vocabulary_does_not_publish() -> None:
    """A free string would let a typo publish cleanly and then match nothing."""
    with pytest.raises(ValueError):
        _rule(product_type="Countertop")  # type: ignore[arg-type]


def test_a_cabinet_rule_does_not_apply_to_a_countertop() -> None:
    store = _store(_rule(rule_id="CAB-001", product_type=ProductType.CABINET))

    resolution = resolve(store, _context(product_type=ProductType.COUNTERTOP))

    assert resolution.applicable == ()
    assert resolution.abstentions[0].outcome is Outcome.NO_APPLICABLE_RULE


# ---------------------------------------------------------------------------
# Project scope — carried, never a filter
# ---------------------------------------------------------------------------


def test_the_same_rule_applies_in_every_project() -> None:
    """Rules are GV's own standards, so selecting by project would hold one vendor to a
    different standard than another (ADR-0006)."""
    store = _store(_rule())

    one = resolve(store, _context(project=_project("PRJ-1")))
    two = resolve(store, _context(project=_project("PRJ-2")))

    assert [a.rule_id for a in one.applicable] == [a.rule_id for a in two.applicable]


def test_a_resolution_carries_its_own_project_and_no_other() -> None:
    override = Quantity(value="1/2", unit=Unit.INCH)
    mine = _project("PRJ-1", filler_width_min=override)
    theirs = _project("PRJ-2", filler_width_min=Quantity(value="3/4", unit=Unit.INCH))

    resolution = resolve(_store(_rule()), _context(project=mine))

    assert resolution.project.project_id == "PRJ-1"
    assert resolution.project.override_for("filler_width_min") == override
    assert resolution.project != theirs


def test_the_project_does_not_change_which_variant_is_chosen() -> None:
    store = _store(_rule())

    for project_id in ("PRJ-1", "PRJ-2", "PRJ-3"):
        (applied,) = resolve(store, _context(project=_project(project_id))).applicable
        assert applied.variant is not None
        assert applied.variant.when == THREE_WALL


# ---------------------------------------------------------------------------
# An unconfirmed tolerance resolves, and stays countable
# ---------------------------------------------------------------------------


def test_a_rule_with_an_unconfirmed_tolerance_still_resolves() -> None:
    """The engine (#47) is where the refusal to decide belongs, because that is where
    tolerances are read. Diverting it here would hide it from coverage."""
    store = _store(_rule(applicability=_wall_config(tolerance="UNCONFIRMED")))

    resolution = resolve(store, _context())

    (applied,) = resolution.applicable
    assert applied.variant is not None
    assert not applied.variant.tolerance.is_confirmed
    assert resolution.considered_count == 1


# ---------------------------------------------------------------------------
# Nothing is decided here
# ---------------------------------------------------------------------------


def test_resolution_never_produces_a_verdict() -> None:
    """Resolution selects rules. Only arithmetic in verdict/ may PASS or FAIL."""
    store = _store(_rule(), _rule(rule_id="CT-DEPTH-001"))

    for context in (_context(), _context(wall_config=ISLAND), _context(wall_config=None)):
        resolution = resolve(store, context)
        for abstention in resolution.abstentions:
            assert abstention.outcome not in (Outcome.PASS, Outcome.FAIL)


def test_an_empty_store_is_not_a_clean_bill_of_health() -> None:
    resolution = resolve(SnapshotStore(), _context())

    assert isinstance(resolution, Resolution)
    assert resolution.applicable == ()
    assert resolution.abstentions[0].outcome is Outcome.NO_APPLICABLE_RULE
    assert not resolution.is_fully_covered


def test_an_applicable_rule_reports_the_rule_id_it_resolved() -> None:
    (applied,) = resolve(_store(_rule()), _context()).applicable

    assert isinstance(applied, ApplicableRule)
    assert applied.rule_id == applied.snapshot.rule.id
