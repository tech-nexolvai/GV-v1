"""The filler-first client rule executes exactly and abstains before choosing cabinets.

Source: issue #61; Cabinet_Checks.xlsx H18-H25 and N18-N22; client facts Q8, Q9 and Q21.
Verification: ``rules/rulebook/cab_filler_001.yaml``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from rules.parameters import ParameterLayer, ParameterValue, Provenance, ResolvedParameter
from rules.schema import Cardinality, Quantity, Rule
from rules.snapshot import publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, OperandValue, VerdictOperand
from verdict.operations import register_all
from verdict.operations.distribution import DistributionCondition, filler_distribution
from verdict.outcomes import Outcome, Severity
from verdict.registry import REGISTRY, RuleAuthoringError

RULE_PATH = Path(__file__).resolve().parents[2] / "rules" / "rulebook" / "cab_filler_001.yaml"


@pytest.fixture(autouse=True)
def _registered_operations() -> object:
    previous = dict(REGISTRY)
    REGISTRY.clear()
    register_all()
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


def _load_rule() -> Rule:
    return Rule.model_validate(yaml.safe_load(RULE_PATH.read_text(encoding="utf-8")))


def _inch(value: int | Fraction, raw_text: str | None = None) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, raw_text)


def _operand(
    name: str,
    value: OperandValue,
    *,
    source: str,
    status: EvidenceStatus = EvidenceStatus.CORROBORATED,
) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=value,
        status=status,
        source=source,
        evidence_ref=f"{source.lower()}:assembly-1:{name}",
    )


def _parameter(name: str, value: int | Fraction) -> ResolvedParameter:
    return ResolvedParameter(
        name=name,
        value=ParameterValue(
            value=Quantity(value=value, unit=Unit.INCH),
            provenance=Provenance.COMPANY_STANDARD,
            set_by="GV",
            set_at=datetime(2026, 8, 22, tzinfo=UTC),
        ),
        layer=ParameterLayer.GLOBAL,
    )


#: Every bound the rule needs, supplied per test. **Not defaults** — CAB-FILLER-001 v2 has none, by
#: the same Q21 reasoning that removed them from the rule, so each one arrives here explicitly and a
#: test that wants a different bound says so.
def _parameters(**overrides: int | Fraction) -> dict[str, ResolvedParameter]:
    values: dict[str, int | Fraction] = {
        "filler_min": 1,
        "filler_max": 2,
        # Wide, so no test is decided by a cabinet bound unless it sets one.
        "single_door_cab_width_min": 9,
        "single_door_cab_width_max": 48,
        "double_door_cab_width_min": 9,
        "double_door_cab_width_max": 48,
        "drawer_cab_width_min": 9,
        "drawer_cab_width_max": 48,
    }
    values.update(overrides)
    return {name: _parameter(name, value) for name, value in values.items()}


def _operands(
    *,
    field: int | Fraction = 90,
    design: int | Fraction = 88,
    design_fillers: tuple[int | Fraction, ...] = (1, 1),
    proposed_fillers: tuple[int | Fraction, ...] = (2, 2),
    design_cabinets: tuple[int | Fraction, ...] = (30, 30, 26),
    proposed_cabinets: tuple[int | Fraction, ...] | None = None,
    cabinet_type: tuple[str, ...] = ("double_door", "equipment", "double_door"),
) -> dict[str, VerdictOperand]:
    """The default run totals 88" with its fillers: 1 + 30 + 30 + 26 + 1.

    `proposed_cabinets` mirrors the architectural run unless a test says otherwise, because the
    shop drawing starts from it — which makes every case below a statement about the *site*
    difference rather than about a shop drawing that already disagreed.
    """
    if proposed_cabinets is None:
        proposed_cabinets = design_cabinets
    return {
        "field_width": _operand(
            "field_width",
            _inch(field),
            source="USER_INPUT",
            status=EvidenceStatus.HUMAN_CONFIRMED,
        ),
        "design_width": _operand("design_width", _inch(design), source="ARCH"),
        "architectural_fillers": _operand(
            "architectural_fillers",
            tuple(_inch(value) for value in design_fillers),
            source="ARCH",
        ),
        "shop_fillers": _operand(
            "shop_fillers",
            tuple(_inch(value) for value in proposed_fillers),
            source="SHOP",
        ),
        "architectural_cabinets": _operand(
            "architectural_cabinets",
            tuple(_inch(value) for value in design_cabinets),
            source="ARCH",
        ),
        "shop_cabinets": _operand(
            "shop_cabinets",
            tuple(_inch(value) for value in proposed_cabinets),
            source="SHOP",
        ),
        # The reviewer's classification, sealed like any other operand. Slide 11 puts it with them.
        "cabinet_type": _operand(
            "cabinet_type",
            cabinet_type,
            source="USER_INPUT",
            status=EvidenceStatus.HUMAN_CONFIRMED,
        ),
    }


def _intermediate(finding: object, name: str) -> object:
    trace = finding.trace  # type: ignore[attr-defined]
    assert trace is not None
    return dict(trace.intermediates)[name]


def test_rule_declares_the_client_sources_bounds_and_exact_operation() -> None:
    """Input: authored YAML. Output: the two-step operation, and not one bound defaulted."""
    rule = _load_rule()

    assert rule.severity is Severity.FLAG
    assert rule.arithmetic_unit is Unit.INCH
    assert rule.operation.type == "cabinet_run_distribution"
    assert rule.inputs["field_width"].source.value == "USER_INPUT"
    # The reviewer's classification is an input like the field width, for the same reason: it is
    # not on either drawing as a value this system can read (deck slide 11).
    assert rule.inputs["cabinet_type"].source.value == "USER_INPUT"
    assert rule.inputs["cabinet_type"].cardinality is Cardinality.MANY
    for run in ("architectural_fillers", "shop_fillers", "architectural_cabinets", "shop_cabinets"):
        assert rule.inputs[run].cardinality is Cardinality.MANY

    # v1 shipped filler_min 1" / filler_max 2" — numbers CLIENT_FACTS Q21 records at three
    # different values — so every package was checked against bounds nobody confirmed.
    assert [name for name, p in rule.parameters.items() if p.default is not None] == []


def test_the_first_worked_example_runs_as_a_check() -> None:
    """Raj's slide 4 through the engine: 90" to 82" gives fillers 2" and regulars 21".

    **This is what #681 is for.** The arithmetic has been right since #676 and reachable through the
    API since #678, but the rule a package check runs still called the step-one-only operation — so
    this exact drawing came back "a reviewer must choose the adjustable cabinet" instead of with the
    answer.
    """
    finding = execute(
        publish(_load_rule()),
        _operands(
            field=82,
            design=90,
            design_fillers=(3, 3),
            proposed_fillers=(3, 3),
            design_cabinets=(24, 36, 24),
        ),
        _parameters(filler_min=2, filler_max=3),
    )

    assert finding.outcome is Outcome.FAIL, "the shop drawing still shows the architectural widths"
    assert _intermediate(finding, "expected_fillers") == (_inch(2), _inch(2))
    assert _intermediate(finding, "expected_cabinets") == (_inch(21), _inch(36), _inch(21))
    assert _intermediate(finding, "condition") == (
        DistributionCondition.CABINETS_ABSORB_REMAINDER.value
    )


def test_the_second_worked_example_grows_the_run() -> None:
    """Slide 8: 88" to 96" gives fillers 3" and regulars 27", equipment untouched."""
    finding = execute(
        publish(_load_rule()),
        _operands(
            field=96,
            design=88,
            design_fillers=(2, 2),
            proposed_fillers=(3, 3),
            design_cabinets=(24, 36, 24),
            proposed_cabinets=(27, 36, 27),
        ),
        _parameters(filler_min=2, filler_max=3),
    )

    assert finding.outcome is Outcome.PASS
    assert _intermediate(finding, "expected_cabinets") == (_inch(27), _inch(36), _inch(27))


def test_larger_site_is_absorbed_by_equal_fillers_before_any_cabinet() -> None:
    """Input: 88-inch design, 90-inch site, two 1-inch design fillers. Output: 2+2 exact PASS.

    Unchanged from v1 in substance: the fillers take the whole difference and no cabinet moves.
    """
    finding = execute(
        publish(_load_rule()),
        _operands(proposed_fillers=(2, 2)),
        _parameters(),
    )

    assert finding.outcome is Outcome.PASS
    assert _intermediate(finding, "expected_fillers") == (_inch(2), _inch(2))
    assert _intermediate(finding, "condition") == DistributionCondition.FILLERS_ABSORB.value
    assert _intermediate(finding, "cabinets_retained") is True


def test_smaller_site_shrinks_fillers_exactly_without_a_false_pass() -> None:
    """Input: 88-inch design with 2+2 fillers and 87-inch site. Output: exact 1.5+1.5 PASS.

    A half inch each. The point of keeping this case is that the split is exact: a float would put
    1.4999999 beside a drawing that says 1 1/2 and the exact-match verdict would fail a correct
    drawing.
    """
    finding = execute(
        publish(_load_rule()),
        _operands(
            field=87,
            design=88,
            design_fillers=(2, 2),
            proposed_fillers=(Fraction(3, 2), Fraction(3, 2)),
            # 2 + 30 + 30 + 24 + 2 = 88, so the run and the design width agree.
            design_cabinets=(30, 30, 24),
        ),
        _parameters(),
    )

    assert finding.outcome is Outcome.PASS
    assert _intermediate(finding, "site_difference") == _inch(-1)
    assert _intermediate(finding, "expected_fillers") == (
        _inch(Fraction(3, 2)),
        _inch(Fraction(3, 2)),
    )


@pytest.mark.parametrize(
    ("field", "expected_share"),
    [(91, Fraction(1, 2)), (87, Fraction(-1, 2))],
    ids=["site-wider", "site-narrower"],
)
def test_what_the_fillers_cannot_absorb_now_reaches_the_cabinets(
    field: int, expected_share: Fraction
) -> None:
    """**The behaviour change v2 exists for**, in both directions.

    This case was `test_overflow_in_either_direction_requires_reviewer_cabinet_selection` and it
    asserted `CABINET_SELECTION_REQUIRED` — the reading of Q9 that slide 11 superseded. The fillers
    still stop at their bound; what is left is now divided equally between the two regular cabinets
    instead of being handed back to the reviewer as a question.
    """
    finding = execute(
        publish(_load_rule()),
        _operands(field=field, design=88, design_fillers=(1, 1)),
        _parameters(),
    )

    assert _intermediate(finding, "condition") == (
        DistributionCondition.CABINETS_ABSORB_REMAINDER.value
    )
    assert _intermediate(finding, "share_per_regular_cabinet") == _inch(expected_share)
    # The equipment cabinet in the middle keeps its width in both directions.
    expected = _intermediate(finding, "expected_cabinets")
    assert isinstance(expected, tuple)
    assert expected[1] == _inch(30)


def test_unequal_fillers_that_must_move_come_back_for_the_reviewer() -> None:
    """What the `filler_symmetry` discriminator used to decide, decided by the arithmetic.

    v1 had two variants — `equal_unless_noted` and `reviewer_noted_asymmetric` — selecting an
    `allow_asymmetric` flag, so whether an uneven pair was acceptable was settled before any number
    was looked at. v2 has no flag: slides 3 and 7 say only that each filler honours its bound and
    never how a change is shared between two that started unequal, so it abstains with the total
    they must reach. Same protection, decided where the numbers are.
    """
    finding = execute(
        publish(_load_rule()),
        _operands(field=86, design=90, design_fillers=(2, 4), proposed_fillers=(1, 3)),
        _parameters(),
    )

    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert _intermediate(finding, "condition") == (
        DistributionCondition.FILLER_APPORTIONMENT_NOT_DETERMINED.value
    )


def test_a_bound_no_layer_supplies_is_not_found_rather_than_a_guess() -> None:
    """#67 and AGENTS.md §2.4, now reachable: v2 removed the defaults that hid this.

    A distribution is only as right as its bounds, and Q21 has never settled them. A package that
    has not supplied one must be told so — not handed a confident verdict computed from 1"/2".
    """
    without_bound = _parameters()
    del without_bound["double_door_cab_width_min"]

    finding = execute(publish(_load_rule()), _operands(), without_bound)

    assert finding.outcome is Outcome.NOT_FOUND
    assert finding.trace is None


def test_a_filler_count_this_check_cannot_compare_abstains_instead_of_raising() -> None:
    """A one-filler run is a wall on one side, not a rule whose text is wrong (#673).

    This used to raise `RuleAuthoringError`, which `verdict/engine.py` re-raises rather than
    abstaining — so one such drawing stopped every other check on the package and reported the
    rulebook as broken. The count came off a drawing, so the operation now says what shape it
    found and the package carries on.
    """
    result = filler_distribution(
        field_width=_inch(90),
        design_width=_inch(88),
        design_fillers=(_inch(1),),
        proposed_fillers=(_inch(2), _inch(2)),
        filler_min=_inch(1),
        filler_max=_inch(2),
        allow_asymmetric=0,
    )

    facts = dict(result.intermediates)
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert facts["condition"] == DistributionCondition.RUN_SHAPE_UNSUPPORTED.value
    # The reason names both halves: what arrived, and what this check handles.
    assert "1 value(s)" in str(facts["shape_found"])
    assert "exactly two" in str(facts["shape_supported"])


def test_operation_still_refuses_malformed_modes_and_bounds() -> None:
    """Input: unsafe authoring values. Output: loud errors rather than guessed distribution.

    #673 moved data-shaped refusals to abstentions; it must not have turned `RuleAuthoringError`
    into a catch-all. These two can only come from the rule text or the registry — a boolean where
    a reviewed integer is required, and a minimum above its own maximum — so they still stop the
    run.
    """
    with pytest.raises(RuleAuthoringError, match="reviewed integer 0 or 1"):
        filler_distribution(
            field_width=_inch(90),
            design_width=_inch(88),
            design_fillers=(_inch(1), _inch(1)),
            proposed_fillers=(_inch(2), _inch(2)),
            filler_min=_inch(1),
            filler_max=_inch(2),
            allow_asymmetric=True,
        )
    with pytest.raises(RuleAuthoringError, match="must not exceed"):
        filler_distribution(
            field_width=_inch(90),
            design_width=_inch(88),
            design_fillers=(_inch(1), _inch(1)),
            proposed_fillers=(_inch(2), _inch(2)),
            filler_min=_inch(2),
            filler_max=_inch(1),
            allow_asymmetric=0,
        )
