"""Raj's two worked distribution examples, and the step they do not yet reach.

`docs/decisions/CAB_CHECKS_FORMAT.md` records two examples from the cabinet deck (received
2026-09-04) as CAB-DIST-1 and CAB-DIST-2. They are synthetic — no drawings, no extraction — so they
are unit cases for the distribution arithmetic rather than gold-set package cases, which need real
arch and shop PDFs.

**Both examples deliberately exercise both steps.** Fillers alone cannot absorb the 8": they give 2"
and the regular cabinets must take the remaining 6". That is what makes them worth having, and it is
also why they do not pass today.

**What the shipped operation does, and why that is not a bug.** `filler_distribution` implements step
one and stops: when the fillers cannot absorb the difference it returns REVIEW_REQUIRED and records
the residual, and its docstring says the refusal is deliberate — *"Q9 assigns cabinet selection to the
reviewer, so no code path here can move a non-adjustable cabinet."* `CAB_CHECKS_FORMAT.md` reads the
same Q9 the other way, as *"only regular cabinets move"*, with step two the system's job to calculate.

**Step two landed with #676, and the disagreement is settled.** The 2026-09-21 deck decides it:
slide 11 has the reviewer draw a box around a cabinet and *categorise* it, and the program compute
from that. Reviewer picks what may move; arithmetic decides by how much — which is how
`CAB_CHECKS_FORMAT.md` read Q9 all along. `cabinet_run_distribution` is that step, and the tests
below that were `xfail(strict=True)` are now assertions.

`filler_distribution` is untouched and still abstains. Its snapshot is content-addressed and a
recorded finding cites the text that judged it, so the tests pinning its behaviour stay exactly as
they were: the residual it hands the reviewer is the same 6" the deck derives, which is the evidence
that step one was right and only the ownership of step two was ever in question.

**The bounds here are inputs, not defaults.** `FILLER_WIDTH_MIN`/`MAX` and the per-type cabinet bounds
are not settled — the email said 1"/2", the 2026-08-25 call said 3-4", these examples use 2"/3", and
all three are illustrative (CLIENT_FACTS Q21, and question 1 of the four sent back to Raj on
2026-09-04). Every bound below is passed explicitly per case for that reason; nothing here may become
a default.

Source: `docs/decisions/CAB_CHECKS_FORMAT.md` · client facts Q8, Q9, Q21
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

import pytest

from units.measurement import Measurement, Unit
from verdict.operations.distribution import (
    DISTRIBUTION_SPECS,
    CabinetType,
    DistributionCondition,
    cabinet_run_distribution,
    filler_distribution,
)
from verdict.outcomes import Outcome
from verdict.registry import Arity, RuleAuthoringError, validate_operands


def _inches(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, None)


@dataclass(frozen=True, slots=True)
class DistributionExample:
    """One worked example from the deck, exactly as `CAB_CHECKS_FORMAT.md` records it.

    The layout row order is `filler | CAB_REGULAR | CAB_EQUIP | CAB_REGULAR | filler`. `equip` is a
    fixed per-cabinet input taken from the equipment spec — the distributor must never resize it, and
    it is stored here so a reader can check the arithmetic without opening the deck.
    """

    case_id: str
    arch_width: int
    site_width: int
    arch_layout: tuple[int, int, int, int, int]
    expected_site_layout: tuple[int, int, int, int, int]
    filler_min: int
    filler_max: int
    equip: int
    #: What the regular cabinets must absorb once the fillers have done all they can.
    cabinet_residual: int

    @property
    def arch_fillers(self) -> list[Measurement]:
        return [_inches(self.arch_layout[0]), _inches(self.arch_layout[-1])]

    @property
    def site_fillers(self) -> list[Measurement]:
        return [_inches(self.expected_site_layout[0]), _inches(self.expected_site_layout[-1])]


#: Scenario 1, slide 4: the site is 8" smaller. Fillers 3->2 give 2"; cabinets 24->21 give 6".
CAB_DIST_1 = DistributionExample(
    case_id="CAB-DIST-1",
    arch_width=90,
    site_width=82,
    arch_layout=(3, 24, 36, 24, 3),
    expected_site_layout=(2, 21, 36, 21, 2),
    filler_min=2,
    filler_max=3,
    equip=36,
    cabinet_residual=-6,
)

#: Scenario 2, slide 6: the site is 8" larger. Fillers 2->3 give 2"; cabinets 24->27 take 6".
CAB_DIST_2 = DistributionExample(
    case_id="CAB-DIST-2",
    arch_width=88,
    site_width=96,
    arch_layout=(2, 24, 36, 24, 2),
    expected_site_layout=(3, 27, 36, 27, 3),
    filler_min=2,
    filler_max=3,
    equip=36,
    cabinet_residual=6,
)

EXAMPLES = (CAB_DIST_1, CAB_DIST_2)

# Question 4 to Raj (2026-09-04) is still open: both examples divide evenly between the two regular
# cabinets, and the rounding rule for a split that does not — nearest 1/8" or 1/4", and which cabinet
# takes the remainder — is unanswered. Under exact match (V1_VERDICT_MODEL) a guess there would not
# be a rounding preference but a wrong PASS or FAIL. #676 therefore abstains on such a run rather
# than inferring a rule from these two cases; `test_an_uneven_split_is_not_decided_by_this_rule`
# pins that. Replace the abstention with the real rule when the answer lands.


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example.case_id)
def test_the_deck_arithmetic_is_self_consistent(example: DistributionExample) -> None:
    """The example as recorded adds up, checked before it is used to judge any code.

    A worked example transcribed from a slide is a claim like any other. If the layouts did not sum
    to their stated widths, every assertion below would be measuring the transcription rather than
    the implementation.
    """
    assert sum(example.arch_layout) == example.arch_width
    assert sum(example.expected_site_layout) == example.site_width

    filler_change = (example.expected_site_layout[0] + example.expected_site_layout[-1]) - (
        example.arch_layout[0] + example.arch_layout[-1]
    )
    total_change = example.site_width - example.arch_width
    assert total_change - filler_change == example.cabinet_residual, (
        "the residual recorded for this case is not what the layouts imply, so either the deck was "
        "transcribed wrongly or the derivation in CAB_CHECKS_FORMAT.md does not hold"
    )


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example.case_id)
def test_the_equipment_cabinet_is_never_resized(example: DistributionExample) -> None:
    """`CAB_EQUIP` is fixed from the equipment spec — the one width no scenario may move.

    This was asserted on the recorded data alone while no code reached step two. #676 is that code,
    so it now checks both: the deck's own layouts leave the equipment cabinet alone, and so does the
    operation. Slide 3 gives the reason — *"the equipment cabinet dimensions should not be reduced
    otherwise equipment will not fit"* — and it binds in both directions, because scenario 2 grows
    the run and an opening too wide for the appliance is as wrong as one too narrow.
    """
    assert example.arch_layout[2] == example.equip
    assert example.expected_site_layout[2] == example.equip

    facts = dict(cabinet_run_distribution(**_distribution_arguments(example)).intermediates)  # type: ignore[arg-type]

    computed = facts["expected_cabinets"]
    assert isinstance(computed, tuple)
    assert computed[1] == _inches(example.equip)
    assert facts["equipment_cabinets"] == (1,)


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example.case_id)
def test_fillers_alone_cannot_absorb_the_difference(example: DistributionExample) -> None:
    """Both examples reach step two, which is the whole reason they are worth having.

    If a future edit made either case absorbable by fillers alone, it would still pass the operation
    tests below while quietly no longer testing the two-step precedence at all.
    """
    assert example.cabinet_residual != 0


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example.case_id)
def test_step_one_computes_exactly_the_residual_the_deck_derives(
    example: DistributionExample,
) -> None:
    """**What the shipped operation does today, and it agrees with the deck to the inch.**

    `filler_distribution` abstains here rather than producing a layout, but the number it hands the
    reviewer is exactly the 6" the deck says the regular cabinets must absorb. That is worth pinning:
    it means the disagreement with `CAB_CHECKS_FORMAT.md` is about who performs step two, not about
    the arithmetic of step one.
    """
    result = filler_distribution(
        field_width=_inches(example.site_width),
        design_width=_inches(example.arch_width),
        design_fillers=example.arch_fillers,
        proposed_fillers=example.site_fillers,
        # Passed per case, never defaulted: these values are illustrative and unsettled.
        filler_min=_inches(example.filler_min),
        filler_max=_inches(example.filler_max),
        allow_asymmetric=0,
    )
    intermediates = dict(result.intermediates)

    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert (
        intermediates["condition"] == DistributionCondition.CABINET_SELECTION_REQUIRED.value
    ), "the operation stopped for some reason other than needing a cabinet moved"

    remaining = intermediates["remaining_difference"]
    assert isinstance(remaining, Measurement)
    assert remaining.exact == Fraction(example.cabinet_residual), (
        f'{example.case_id}: the deck derives {example.cabinet_residual}" for the regular cabinets '
        f'and the operation computed {remaining.exact}"'
    )


#: Wide enough that no case is decided by a bound unless it sets its own. The real values are
#: unsettled (CLIENT_FACTS Q21, question 1 to Raj) and must never acquire a default, here or in the
#: operation — a case that needs a bound states it.
WIDE_TYPE_BOUNDS: dict[str, Measurement] = {
    f"{kind.value}_cab_width_{edge}": _inches(1 if edge == "min" else 96)
    for kind in CabinetType
    if not kind.is_equipment
    for edge in ("min", "max")
}


def _distribution_arguments(
    example: DistributionExample,
    *,
    regular_type: CabinetType = CabinetType.DOUBLE_DOOR,
) -> dict[str, object]:
    """One example as operands for `cabinet_run_distribution`.

    The layout row is `filler | CAB_REGULAR | CAB_EQUIP | CAB_REGULAR | filler`, so the cabinets are
    the middle three and the equipment cabinet is the one whose width equals `equip`.

    **`regular_type` is a parameter because the deck does not say.** Slides 4 and 8 draw two 24"
    regular cabinets without naming them single-door, double-door or drawer. With the bounds wide
    the choice cannot change the arithmetic, and `test_the_regular_cabinet_type_does_not_change_the
    _arithmetic` proves that rather than asserting it — so the default here is a placeholder, not a
    reading of the deck.
    """
    cabinets = example.arch_layout[1:-1]
    types = tuple(
        CabinetType.EQUIPMENT if width == example.equip else regular_type for width in cabinets
    )
    return {
        "field_width": _inches(example.site_width),
        "design_width": _inches(example.arch_width),
        "design_fillers": example.arch_fillers,
        "proposed_fillers": example.site_fillers,
        "design_cabinets": [_inches(width) for width in cabinets],
        "proposed_cabinets": [_inches(width) for width in example.expected_site_layout[1:-1]],
        "cabinet_type": [kind.value for kind in types],
        **WIDE_TYPE_BOUNDS,
        "filler_min": _inches(example.filler_min),
        "filler_max": _inches(example.filler_max),
    }


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example.case_id)
def test_the_deck_expects_a_computed_layout_rather_than_an_abstention(
    example: DistributionExample,
) -> None:
    """The target from the deck, now reached.

    **This was `xfail(strict=True)` until #676**, with the reason *"step two is not implemented…
    which reading of Q9 holds is question 2 of four sent back to Raj on 2026-09-04"*. The
    2026-09-21 deck answers it: slide 11 has the reviewer *classify* a cabinet — *"User should be
    able to draw a bounding box around a cabinet and categorize that as a particular equipment
    cabinet"* — and the program then compute. Reviewer picks what may move; arithmetic decides by
    how much. `CAB_CHECKS_FORMAT.md` had read Q9 that way all along.

    The earlier test deliberately asserted the outcome alone, because *"inventing operand names
    here would encode a guess about an interface nobody has designed"*. The interface now exists,
    so this asserts the computed layout too.
    """
    result = cabinet_run_distribution(**_distribution_arguments(example))  # type: ignore[arg-type]

    facts = dict(result.intermediates)
    assert result.outcome is Outcome.PASS
    assert facts["expected_fillers"] == tuple(example.site_fillers)
    assert facts["expected_cabinets"] == tuple(
        _inches(width) for width in example.expected_site_layout[1:-1]
    )


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example.case_id)
def test_the_residual_the_deck_names_is_what_the_cabinets_absorb(
    example: DistributionExample,
) -> None:
    """`cabinet_residual` is the deck's own figure. Two regular cabinets split it equally."""
    facts = dict(cabinet_run_distribution(**_distribution_arguments(example)).intermediates)  # type: ignore[arg-type]

    assert facts["remainder_after_fillers"] == _inches(example.cabinet_residual)
    assert facts["share_per_regular_cabinet"] == _inches(Fraction(example.cabinet_residual, 2))


def test_a_shop_drawing_that_disagrees_with_the_distribution_fails() -> None:
    """Disagreeing with a computed expectation is a FAIL, not an abstention.

    There is nothing uncertain about 20 not being 21 — the uncertainty the abstention outcomes
    exist for is about what the drawing *says*, not about arithmetic on what it says.
    """
    arguments = _distribution_arguments(CAB_DIST_1)
    arguments["proposed_cabinets"] = [_inches(20), _inches(36), _inches(22)]

    assert cabinet_run_distribution(**arguments).outcome is Outcome.FAIL  # type: ignore[arg-type]


def test_a_regular_cabinet_is_never_taken_past_its_own_type_bound() -> None:
    """Slide 12, scenario 4. Floor the double-door minimum at 23" and the 6" cannot be absorbed.

    *"The program should not force a fix. It should flag 'cannot be resolved, RFI to architect.'"*
    The finding names the cabinet, the bound and the operand that set it, because an RFI that does
    not say which limit was hit costs a second round trip.
    """
    arguments = _distribution_arguments(CAB_DIST_1, regular_type=CabinetType.DOUBLE_DOOR)
    arguments["double_door_cab_width_min"] = _inches(23)

    result = cabinet_run_distribution(**arguments)  # type: ignore[arg-type]

    facts = dict(result.intermediates)
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert facts["condition"] == DistributionCondition.CANNOT_BE_RESOLVED.value
    assert "RFI" in str(facts["reviewer_action"])
    assert facts["unabsorbed_difference"] == _inches(6)

    # Reviewer-facing prose since #682 renders it into the explanation: cabinets are counted from
    # one the way a person counts them, the category is words rather than an identifier, and a
    # width is written the way a drawing writes it.
    blocked_by = str(facts["blocked_by"])
    assert "cabinet 1" in blocked_by
    assert "double door" in blocked_by
    assert '23"' in blocked_by
    assert "minimum" in blocked_by


def test_only_the_type_of_the_cabinet_that_moved_can_block_it() -> None:
    """A tight bound on a type no cabinet in the run has does not touch the result.

    Without a per-type lookup the six bounds would collapse into one, and a drawer minimum would
    silently constrain a double-door cabinet.
    """
    arguments = _distribution_arguments(CAB_DIST_1, regular_type=CabinetType.DOUBLE_DOOR)
    arguments["drawer_cab_width_min"] = _inches(30)

    assert cabinet_run_distribution(**arguments).outcome is Outcome.PASS  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "regular_type", [kind for kind in CabinetType if not kind.is_equipment], ids=lambda k: k.value
)
def test_the_regular_cabinet_type_does_not_change_the_arithmetic(
    regular_type: CabinetType,
) -> None:
    """The deck never names the type of its two 24" cabinets, and with wide bounds it need not.

    This is what lets `_distribution_arguments` pick one: the type selects which bound is consulted
    and nothing else, so while no bound binds, all three give the same layout.
    """
    result = cabinet_run_distribution(  # type: ignore[arg-type]
        **_distribution_arguments(CAB_DIST_1, regular_type=regular_type)
    )

    facts = dict(result.intermediates)
    assert result.outcome is Outcome.PASS
    assert facts["expected_cabinets"] == (_inches(21), _inches(36), _inches(21))
    assert facts["cabinet_types"] == (regular_type.value, "equipment", regular_type.value)


def test_a_run_of_only_equipment_cabinets_has_nothing_that_may_move() -> None:
    """Every cabinet pinned, fillers at their bound: there is no distribution to propose."""
    arguments = _distribution_arguments(CAB_DIST_1)
    arguments["cabinet_type"] = [CabinetType.EQUIPMENT.value] * 3

    result = cabinet_run_distribution(**arguments)  # type: ignore[arg-type]

    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert dict(result.intermediates)["condition"] == (
        DistributionCondition.CANNOT_BE_RESOLVED.value
    )


def test_fillers_alone_absorb_a_small_difference() -> None:
    """Slide 12, scenario 3: *"the fillers alone can absorb it and no cabinet changes."*"""
    arguments = _distribution_arguments(CAB_DIST_1)
    arguments["field_width"] = _inches(88)  # 2" smaller; the fillers cover it
    arguments["proposed_fillers"] = [_inches(2), _inches(2)]
    arguments["proposed_cabinets"] = [_inches(24), _inches(36), _inches(24)]

    result = cabinet_run_distribution(**arguments)  # type: ignore[arg-type]

    facts = dict(result.intermediates)
    assert result.outcome is Outcome.PASS
    assert facts["condition"] == DistributionCondition.FILLERS_ABSORB.value
    assert facts["share_per_regular_cabinet"] is None
    # Slide 12, outcome 3: "mark the cabinets with green checks and change only the fillers".
    assert facts["cabinets_retained"] is True
    assert facts["expected_cabinets"] == (_inches(24), _inches(36), _inches(24))


def test_a_drawing_that_already_matches_the_site_passes() -> None:
    """Slide 12, scenario 5: *"the shop drawing already matches the site… report a pass."*"""
    arguments = _distribution_arguments(CAB_DIST_1)
    arguments["field_width"] = _inches(90)
    arguments["proposed_fillers"] = [_inches(3), _inches(3)]
    arguments["proposed_cabinets"] = [_inches(24), _inches(36), _inches(24)]

    result = cabinet_run_distribution(**arguments)  # type: ignore[arg-type]

    assert result.outcome is Outcome.PASS
    assert dict(result.intermediates)["condition"] == (
        DistributionCondition.NO_CHANGE_REQUIRED.value
    )


def test_a_longer_run_divides_across_every_regular_cabinet() -> None:
    """Slide 12 names runs of more than three cabinets and equipment at the end.

    `filler_distribution` raises `RuleAuthoringError` on any run that is not exactly two, which
    turns one of these drawings into a rule-authoring failure (#673). This operation does not
    impose an arity: 6" over three regulars is 2" each.
    """
    result = cabinet_run_distribution(
        field_width=_inches(106),
        design_width=_inches(114),
        design_fillers=[_inches(3), _inches(3)],
        proposed_fillers=[_inches(2), _inches(2)],
        design_cabinets=[_inches(24), _inches(24), _inches(24), _inches(36)],
        proposed_cabinets=[_inches(22), _inches(22), _inches(22), _inches(36)],
        cabinet_type=["drawer", "drawer", "drawer", "equipment"],
        **WIDE_TYPE_BOUNDS,
        filler_min=_inches(2),
        filler_max=_inches(3),
    )

    facts = dict(result.intermediates)
    assert result.outcome is Outcome.PASS
    assert facts["share_per_regular_cabinet"] == _inches(-2)
    assert facts["regular_cabinets"] == (0, 1, 2)


def test_an_uneven_split_is_not_decided_by_this_rule() -> None:
    """6" over three regular cabinets is 2" each; 4" over three is not a width anyone can draw.

    The deck's rule underdetermines this run, so the operation abstains and hands the reviewer the
    exact share it computed. It does not FAIL the drawing — a shop drawing carrying 22 5/8, 22 5/8
    and 22 3/4 may well be correct, and calling that a FAIL would be this system inventing the
    rounding rule question 4 asks Raj for.
    """
    result = cabinet_run_distribution(
        field_width=_inches(108),
        design_width=_inches(114),
        design_fillers=[_inches(3), _inches(3)],
        proposed_fillers=[_inches(2), _inches(2)],
        design_cabinets=[_inches(24), _inches(24), _inches(24), _inches(36)],
        proposed_cabinets=[
            _inches(Fraction(181, 8)),
            _inches(Fraction(181, 8)),
            _inches(Fraction(91, 4)),
            _inches(36),
        ],
        cabinet_type=["drawer", "drawer", "drawer", "equipment"],
        **WIDE_TYPE_BOUNDS,
        filler_min=_inches(2),
        filler_max=_inches(3),
    )

    facts = dict(result.intermediates)
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert facts["condition"] == DistributionCondition.SHARE_DOES_NOT_DIVIDE.value
    assert facts["share_per_regular_cabinet"] == _inches(Fraction(-4, 3))


def test_an_uneven_filler_split_abstains_under_its_own_name() -> None:
    """Step one can land on an undrawable width too, and it is reported as a filler share.

    Three fillers sharing 7" is 2 1/3" each. The key says `share_per_filler`, not
    `share_per_regular_cabinet`: a reviewer reading the finding has to be able to tell which half of
    the calculation stopped, and here the cabinets were never reached — the remainder is zero.
    """
    result = cabinet_run_distribution(
        field_width=_inches(67),
        design_width=_inches(66),
        design_fillers=[_inches(2), _inches(2), _inches(2)],
        proposed_fillers=[_inches(2), _inches(2), _inches(3)],
        design_cabinets=[_inches(24), _inches(36)],
        proposed_cabinets=[_inches(24), _inches(36)],
        cabinet_type=["single_door", "equipment"],
        **WIDE_TYPE_BOUNDS,
        filler_min=_inches(1),
        filler_max=_inches(3),
    )

    facts = dict(result.intermediates)
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert facts["condition"] == DistributionCondition.SHARE_DOES_NOT_DIVIDE.value
    assert facts["share_per_filler"] == _inches(Fraction(7, 3))
    assert "share_per_regular_cabinet" not in facts
    assert facts["remainder_after_fillers"] == _inches(0)


def test_a_classification_that_misses_a_cabinet_abstains() -> None:
    """Both counts come from the drawing and the reviewer, so a mismatch is data (#673).

    This raised `RuleAuthoringError` when #676 shipped, on the reading that the rule must have been
    wired wrongly. But the cabinets are read off a sheet and the categories are entered by a
    person, so the two can disagree on a perfectly well-authored rule — and raising stopped every
    other check on the package.
    """
    arguments = _distribution_arguments(CAB_DIST_1)
    arguments["cabinet_type"] = [CabinetType.DOUBLE_DOOR.value, CabinetType.EQUIPMENT.value]

    result = cabinet_run_distribution(**arguments)  # type: ignore[arg-type]

    facts = dict(result.intermediates)
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert facts["condition"] == DistributionCondition.RUN_SHAPE_UNSUPPORTED.value
    assert "2 cabinet(s)" in str(facts["shape_found"])
    assert "3" in str(facts["shape_found"])


def test_a_category_the_deck_does_not_name_is_refused() -> None:
    """The set is closed, and an unknown category is never treated as a regular cabinet.

    Abstaining rather than raising (#673) — it is a reviewer's input, not the rule's text — but the
    safety property is the one that matters and it is unchanged: an unrecognised word must never
    fall through to "regular", because a misspelt equipment cabinet would then be resized, which is
    the one failure slide 3 exists to prevent.
    """
    arguments = _distribution_arguments(CAB_DIST_1)
    arguments["cabinet_type"] = ["double_door", "appliance", "double_door"]

    result = cabinet_run_distribution(**arguments)  # type: ignore[arg-type]

    facts = dict(result.intermediates)
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert facts["condition"] == DistributionCondition.RUN_SHAPE_UNSUPPORTED.value
    assert "appliance" in str(facts["shape_found"])
    # No width was proposed for anything: the run was never compared.
    assert "expected_cabinets" not in facts


def test_a_shop_run_of_a_different_length_abstains_rather_than_raising() -> None:
    """Two drawings that describe different parts cannot have their widths compared.

    That is a real pair of drawings — a shop drawing with a cabinet the architect did not draw —
    and it must not take the package down with it.
    """
    arguments = _distribution_arguments(CAB_DIST_1)
    arguments["proposed_cabinets"] = [_inches(21), _inches(36)]

    result = cabinet_run_distribution(**arguments)  # type: ignore[arg-type]

    facts = dict(result.intermediates)
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert facts["condition"] == DistributionCondition.RUN_SHAPE_UNSUPPORTED.value
    assert "proposed_cabinets" in str(facts["shape_found"])


def test_an_empty_run_abstains_because_nothing_was_read() -> None:
    """No fillers read is a statement about the reading, never about the rule."""
    arguments = _distribution_arguments(CAB_DIST_1)
    arguments["design_fillers"] = []
    arguments["proposed_fillers"] = []

    result = cabinet_run_distribution(**arguments)  # type: ignore[arg-type]

    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert dict(result.intermediates)["condition"] == (
        DistributionCondition.RUN_SHAPE_UNSUPPORTED.value
    )


def test_a_genuinely_malformed_rule_still_stops_the_run() -> None:
    """#673 must not have turned `RuleAuthoringError` into a catch-all.

    A bound above its own maximum, and a measurement that is not one, can only come from the rule
    text or the registry — no drawing produces them — so they still reach `verdict/engine.py` and
    fail loudly rather than quietly judging nothing.
    """
    inverted = _distribution_arguments(CAB_DIST_1)
    inverted["double_door_cab_width_min"] = _inches(40)
    inverted["double_door_cab_width_max"] = _inches(20)
    with pytest.raises(RuleAuthoringError, match="must not exceed"):
        cabinet_run_distribution(**inverted)  # type: ignore[arg-type]

    wrong_kind = _distribution_arguments(CAB_DIST_1)
    wrong_kind["field_width"] = 82
    with pytest.raises(RuleAuthoringError, match="must be a Measurement"):
        cabinet_run_distribution(**wrong_kind)  # type: ignore[arg-type]

    not_a_list = _distribution_arguments(CAB_DIST_1)
    not_a_list["design_cabinets"] = _inches(24)
    with pytest.raises(RuleAuthoringError, match="must have list arity"):
        cabinet_run_distribution(**not_a_list)  # type: ignore[arg-type]


def test_every_type_bound_is_a_required_operand_so_a_missing_one_is_not_found() -> None:
    """No bound may acquire a default, in the operation or in a rule.

    The engine answers NOT_FOUND when a required operand cannot be resolved — *"a missing
    intermediate or parameter is not zero"* (`verdict/engine.py`) — and that is the whole mechanism
    behind the acceptance criterion. It only holds while all six are declared, which is what this
    pins: an optional bound would reach `cabinet_run_distribution` as `None` and there is no honest
    value to put in its place. CLIENT_FACTS Q21 says the numbers are still unsettled.
    """
    (spec,) = [s for s in DISTRIBUTION_SPECS if s.name == "cabinet_run_distribution"]

    for kind in CabinetType:
        if kind.is_equipment:
            continue
        for edge in ("min", "max"):
            assert spec.operands[f"{kind.value}_cab_width_{edge}"] is Arity.SCALAR

    with pytest.raises(RuleAuthoringError, match="single_door_cab_width_min"):
        validate_operands(
            spec,
            {
                name: value
                for name, value in _distribution_arguments(CAB_DIST_1).items()
                if name != "single_door_cab_width_min"
            },
        )


def test_unequal_fillers_that_must_move_are_not_apportioned_by_this_rule() -> None:
    """Slide 12 names unequal fillers; slides 3 and 7 never say how a change is shared.

    2" and 4" losing 2" between them could be 1"+3", 2"+2" or 1 1/2"+2 1/2", all inside the bound.
    Picking one would be this system inventing the rule, and under exact match the pick *is* the
    verdict — so the finding carries the total the fillers must reach and leaves the split open.
    """
    result = cabinet_run_distribution(
        field_width=_inches(58),
        design_width=_inches(60),
        design_fillers=[_inches(2), _inches(4)],
        proposed_fillers=[_inches(1), _inches(3)],
        design_cabinets=[_inches(18), _inches(36)],
        proposed_cabinets=[_inches(18), _inches(36)],
        cabinet_type=["single_door", "equipment"],
        **WIDE_TYPE_BOUNDS,
        filler_min=_inches(1),
        filler_max=_inches(4),
    )

    facts = dict(result.intermediates)
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert facts["condition"] == (DistributionCondition.FILLER_APPORTIONMENT_NOT_DETERMINED.value)
    assert facts["expected_filler_total"] == _inches(4)
    assert facts["filler_width_bounds"] == (_inches(2), _inches(8))


def test_unequal_fillers_are_still_judged_when_nothing_has_to_move() -> None:
    """The abstention is about sharing a *change*, not about unequal fillers as such.

    The site matches the architectural drawing here, so the expected layout is the design layout —
    2" and 4", unequal and untouched. The shop drawing evened them to 3" and 3", a change the site
    never justified, and that is a FAIL rather than a review: nothing about it is uncertain.
    """
    result = cabinet_run_distribution(
        field_width=_inches(60),
        design_width=_inches(60),
        design_fillers=[_inches(2), _inches(4)],
        proposed_fillers=[_inches(3), _inches(3)],
        design_cabinets=[_inches(18), _inches(36)],
        proposed_cabinets=[_inches(18), _inches(36)],
        cabinet_type=["single_door", "equipment"],
        **WIDE_TYPE_BOUNDS,
        filler_min=_inches(1),
        filler_max=_inches(4),
    )

    facts = dict(result.intermediates)
    assert result.outcome is Outcome.FAIL
    assert facts["condition"] == DistributionCondition.NO_CHANGE_REQUIRED.value
    assert facts["expected_fillers"] == (_inches(2), _inches(4))
    assert facts["cabinets_retained"] is True


def test_a_drawing_that_closes_but_distributes_wrongly_reports_a_real_delta() -> None:
    """A FAIL whose delta is zero tells the reviewer the opposite of what the finding says.

    This drawing totals the site width exactly and still puts the inches on the wrong cabinets, so
    the delta is the distance from the expectation — 1" moved off each regular — not from the wall.
    """
    arguments = _distribution_arguments(CAB_DIST_1)
    arguments["proposed_cabinets"] = [_inches(20), _inches(36), _inches(22)]

    result = cabinet_run_distribution(**arguments)  # type: ignore[arg-type]

    assert result.outcome is Outcome.FAIL
    assert dict(result.intermediates)["proposed_run_total"] == _inches(CAB_DIST_1.site_width)
    assert result.delta == _inches(2)
