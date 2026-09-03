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

So this file asserts two different things. The passing tests pin what the operation actually does —
including that its residual is exactly the 6" the deck's derivation names, which is the evidence that
step one is right and only the ownership of step two is in question. The `xfail` tests state the
target from the deck, so the examples live in the repository as data rather than in a document, and
turn green when step two lands rather than needing to be written then.

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
from verdict.operations.distribution import DistributionCondition, filler_distribution
from verdict.outcomes import Outcome


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

# TODO(#274-adjacent, question 4 to Raj 2026-09-04): both examples divide evenly between the two
# regular cabinets. No case here covers an uneven split, because the rounding rule — nearest 1/8" or
# 1/4", and which cabinet takes the remainder — is unanswered, and the verdict is exact-match
# (V1_VERDICT_MODEL), so a guess would not be a rounding preference but a wrong PASS or FAIL. Add the
# case when the answer lands; do not infer one from these two.


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

    Asserted on the recorded data rather than on code, because no code reaches step two yet. When it
    does, this is the property that must survive: the equipment has to fit.
    """
    assert example.arch_layout[2] == example.equip
    assert example.expected_site_layout[2] == example.equip


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


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example.case_id)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "step two is not implemented. filler_distribution deliberately refuses cabinet operands, "
        "citing Q9 as assigning cabinet selection to the reviewer; CAB_CHECKS_FORMAT.md reads Q9 as "
        "'only regular cabinets move' and makes the calculation the system's. Which reading holds is "
        "question 2 of four sent back to Raj on 2026-09-04. strict, so this fails loudly the day the "
        "behaviour changes rather than passing unnoticed."
    ),
)
def test_the_deck_expects_a_computed_layout_rather_than_an_abstention(
    example: DistributionExample,
) -> None:
    """The target from the deck, asserted on the outcome alone.

    Deliberately not asserting a shape for the computed cabinet widths: no API for step two exists,
    and inventing operand names here would encode a guess about an interface nobody has designed —
    the failure this repository keeps meeting. What the deck is unambiguous about is that a site
    layout the rules permit is a result, not a referral.
    """
    result = filler_distribution(
        field_width=_inches(example.site_width),
        design_width=_inches(example.arch_width),
        design_fillers=example.arch_fillers,
        proposed_fillers=example.site_fillers,
        filler_min=_inches(example.filler_min),
        filler_max=_inches(example.filler_max),
        allow_asymmetric=0,
    )

    assert result.outcome is Outcome.PASS
