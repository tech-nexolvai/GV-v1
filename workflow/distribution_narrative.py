"""Raj's own explanation of a cabinet distribution, rendered from the exact numbers.

Slides 5 and 9 of the 2026-09-21 deck write out the wanted output twice, and say why:

    "It would be better if the program identifies the variables first and provides the logic as
    below. This explanation will help the shop drawing reviewer to understand how the program is
    correcting the drawing."

**A template, not a model.** Every figure in his paragraph is already an intermediate of
`cabinet_run_distribution`, so the sentence is fully determined by the arithmetic. Composing it with
a language model would put generated text beside a number the reviewer is being asked to trust, for
no gain, and a generated sentence can disagree with the verdict it explains. A template cannot.

**It never states a number the operation did not compute.** Everything below is read out of
`intermediates`; nothing is re-derived here, because a second implementation of the arithmetic is a
second chance to get it wrong. Where a fact is absent the sentence is left out rather than guessed.

This module is deliberately outside `verdict/`: that service decides, and presentation is not its
job. One renderer, used by the reviewer's calculator and by the composed finding, so the same
sentence reaches a reviewer wherever they meet it.

Source: issue #682. Verification: tests/workflow/test_distribution_narrative.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from fractions import Fraction

from units.imperial import format_inches
from units.measurement import Measurement, Unit
from verdict.operations.distribution import CabinetType, DistributionCondition

__all__ = ["explain_distribution"]


def _inches(value: Measurement | Fraction) -> str:
    """One exact value the way a drawing writes it: `21 1/2"`, never `21.5`."""
    exact = value.exact if isinstance(value, Measurement) else value
    suffix = '"' if not isinstance(value, Measurement) or value.unit is Unit.INCH else ""
    rendered = f"{format_inches(exact)}{suffix}"
    if isinstance(value, Measurement) and value.unit is not Unit.INCH:
        return f"{format_inches(exact)} {value.unit.value}"
    return rendered


def _magnitude(value: Measurement | Fraction) -> str:
    """The size of a change without its sign — the direction is said in words."""
    exact = value.exact if isinstance(value, Measurement) else value
    return _inches(abs(exact))


def _run(values: Sequence[Measurement]) -> str:
    return ", ".join(_inches(value) for value in values)


def _distinct(values: Sequence[Measurement]) -> Measurement | None:
    """The one value a run holds, or None when it holds more than one.

    Raj writes "reduced from 24" to 21" each" because both regular cabinets were 24". When they
    differ there is no "each", so the caller lists them instead of inventing a single number.
    """
    if not values:
        return None
    first = values[0]
    return first if all(value.exact == first.exact for value in values) else None


def explain_distribution(facts: Mapping[str, object]) -> str:
    """The correction explained the way slides 5 and 9 explain it.

    Takes the operation's intermediates rather than its `OperationResult`, because the two callers
    hold different things: the reviewer's calculator has the result in hand, and a composed finding
    has the trace that was persisted with it. One renderer either way, so the sentence a reviewer
    reads on the screen is the sentence in the report.

    Returns a paragraph for any condition the operation can produce, including the abstentions —
    those need it most, because "cannot be resolved" is what a reviewer turns into an RFI and an RFI
    that does not say how far short the drawing falls costs a second round trip. A condition it does
    not recognise returns nothing, so a caller shows its own summary rather than a half-sentence.
    """
    facts = dict(facts)
    condition = str(facts.get("condition", ""))

    if condition not in {member.value for member in DistributionCondition}:
        return ""

    if condition == DistributionCondition.RUN_SHAPE_UNSUPPORTED.value:
        return (
            f"This run cannot be checked automatically: {facts.get('shape_found')}, and "
            f"{facts.get('shape_supported')}. Check it by hand."
        )

    sentences = [_the_site_against_the_drawing(facts)]

    if condition == DistributionCondition.NO_CHANGE_REQUIRED.value:
        sentences.append(
            "Nothing needs to change: the shop drawing already shows the architectural widths."
        )
        return " ".join(part for part in sentences if part)

    sentences.append(_what_the_fillers_can_do(facts))

    if condition == DistributionCondition.FILLER_APPORTIONMENT_NOT_DETERMINED.value:
        total = facts.get("expected_filler_total")
        bounds = facts.get("filler_width_bounds")
        wanted = _inches(total) if isinstance(total, Measurement) else "the required total"
        within = (
            f", and each one has to stay within {_inches(bounds[0])} to {_inches(bounds[1])}"
            if isinstance(bounds, tuple)
            and len(bounds) == 2
            and all(isinstance(bound, Measurement) for bound in bounds)
            else ""
        )
        sentences.append(
            "The fillers were not equal to begin with, so how the change is shared between them "
            f"is not settled by the rule. They have to total {wanted} between them{within}. "
            "Confirm the split."
        )
        return " ".join(part for part in sentences if part)

    if condition == DistributionCondition.FILLERS_ABSORB.value:
        sentences.append(
            "The fillers absorb all of it, so no cabinet changes and every cabinet width on the "
            "drawing stands."
        )
        return " ".join(part for part in sentences if part)

    if condition == DistributionCondition.CANNOT_BE_RESOLVED.value:
        sentences.append(_cannot_be_resolved(facts))
        return " ".join(part for part in sentences if part)

    if condition == DistributionCondition.SHARE_DOES_NOT_DIVIDE.value:
        sentences.append(_does_not_divide(facts))
        return " ".join(part for part in sentences if part)

    sentences.append(_what_the_cabinets_take(facts))
    return " ".join(part for part in sentences if part)


def _the_site_against_the_drawing(facts: dict[str, object]) -> str:
    """Raj's first sentence: the two widths, and how much has to move."""
    design = facts.get("design_width")
    difference = facts.get("site_difference")
    if not isinstance(design, Measurement) or not isinstance(difference, Measurement):
        return ""

    site = Measurement(design.exact + difference.exact, design.unit, None)
    if difference.exact == 0:
        return (
            f"Wall to wall width in the architectural drawing = {_inches(design)}, and the site "
            f"measures the same."
        )
    direction = "reduced" if difference.exact < 0 else "increased"
    return (
        f"Wall to wall width in the architectural drawing = {_inches(design)}. Wall to wall width "
        f"as per site dimensions = {_inches(site)}. So {_magnitude(difference)} needs to be "
        f"{direction} in the shop drawing cabinet elevation."
    )


def _what_the_fillers_can_do(facts: dict[str, object]) -> str:
    """Raj's second sentence: the filler bound, what they absorb, what is left."""
    design_fillers = facts.get("design_fillers")
    expected = facts.get("expected_fillers")
    design_total = facts.get("design_filler_total")
    expected_total = facts.get("expected_filler_total")
    remainder = facts.get("remainder_after_fillers")
    if not (
        isinstance(design_fillers, tuple)
        and isinstance(design_total, Measurement)
        and isinstance(expected_total, Measurement)
        and isinstance(remainder, Measurement)
    ):
        return ""

    absorbed = expected_total.exact - design_total.exact
    if absorbed == 0:
        return "The fillers are already at their limit, so they cannot absorb any of it."

    was = _distinct(design_fillers)
    now = _distinct(expected) if isinstance(expected, tuple) else None
    if was is not None and now is not None:
        moved = (
            f"the fillers can only be {'reduced' if absorbed < 0 else 'increased'} from "
            f"{_inches(was)} to {_inches(now)} on each side"
        )
        limit = (
            f"Since the {'minimum' if absorbed < 0 else 'maximum'} width of the filler is "
            f"{_inches(now)} per side, "
        )
    else:
        moved = f"the fillers can only reach {_inches(expected_total)} in total"
        limit = "Within their limits, "

    tail = (
        f"That absorbs {_magnitude(Measurement(absorbed, expected_total.unit, None))} in total"
        + (
            "."
            if remainder.exact == 0
            else (f", so {_magnitude(remainder)} still needs to be adjusted in the cabinets.")
        )
    )
    return f"{limit}{moved}. {tail}"


def _what_the_cabinets_take(facts: dict[str, object]) -> str:
    """Raj's third sentence: the equipment cabinet holds, the regular ones split the rest."""
    design = facts.get("design_cabinets")
    expected = facts.get("expected_cabinets")
    regulars = facts.get("regular_cabinets")
    remainder = facts.get("remainder_after_fillers")
    types = facts.get("cabinet_types")
    if not (
        isinstance(design, tuple)
        and isinstance(expected, tuple)
        and isinstance(regulars, tuple)
        and isinstance(remainder, Measurement)
    ):
        return ""

    equipment = (
        [
            design[index]
            for index, name in enumerate(types)
            if name == CabinetType.EQUIPMENT.value and index < len(design)
        ]
        if isinstance(types, tuple)
        else []
    )
    held = _distinct(equipment)
    direction = "less" if remainder.exact < 0 else "more"
    held_sentence = (
        (
            f"The equipment cabinet width cannot be {direction} than {_inches(held)}, so the full "
            f"{_magnitude(remainder)} is taken by the other cabinets. "
        )
        if held is not None
        else ""
    )

    was = _distinct([design[index] for index in regulars])
    now = _distinct([expected[index] for index in regulars])
    verb = "reduced" if remainder.exact < 0 else "increased"
    if was is not None and now is not None:
        return f"{held_sentence}They are {verb} from {_inches(was)} to {_inches(now)} each."
    return f"{held_sentence}The regular cabinets become {_run([expected[i] for i in regulars])}."


def _cannot_be_resolved(facts: dict[str, object]) -> str:
    """Slide 12, scenario 4 — and the sentence a reviewer turns into an RFI."""
    unabsorbed = facts.get("unabsorbed_difference")
    blocked_by = facts.get("blocked_by")
    return (
        f"It cannot be absorbed within the stated limits: {blocked_by}. "
        f"{_magnitude(unabsorbed) if isinstance(unabsorbed, Measurement) else 'The difference'} "
        "is left over. The program will not force a fix — this needs an RFI to the architect."
    )


def _does_not_divide(facts: dict[str, object]) -> str:
    """Ours, not his: the deck's two examples both divide evenly and it never says what else to do."""
    share = facts.get("share_per_regular_cabinet") or facts.get("share_per_filler")
    worked_out = ""
    if isinstance(share, Measurement):
        direction = "off" if share.exact < 0 else "onto"
        worked_out = f" — it works out at {_magnitude(share)} {direction} each"
    return (
        "Dividing that equally does not land on a width a drawing can carry"
        + worked_out
        + ". The rule does not say how to round it or which part takes the remainder, so confirm "
        "how it is apportioned."
    )
