"""Exact filler-first distribution for one ordered left/right cabinet run.

The operation calculates a derived expectation for a reviewer. It never selects a cabinet or
issues a fabrication instruction. When the two fillers cannot absorb the site difference inside
their configured bounds, it returns ``REVIEW_REQUIRED`` with the remaining difference recorded.

Source: issue #61; Cabinet_Checks.xlsx H18-H25 and N18-N22; client facts Q8, Q9 and Q21.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from fractions import Fraction

from units.measurement import Measurement, Unit
from units.policy import require_same_unit
from verdict.outcomes import Outcome
from verdict.registry import Arity, OperationResult, OperationSpec, RuleAuthoringError, register


class DistributionCondition(StrEnum):
    """Why the operation decided or abstained."""

    FILLERS_ABSORB = "fillers_absorb"
    CABINET_SELECTION_REQUIRED = "cabinet_selection_required"
    #: Slide 12, scenario 5: the shop drawing already matches the site.
    NO_CHANGE_REQUIRED = "no_change_required"
    #: Step two ran: the fillers reached their bound and the regular cabinets took the rest.
    CABINETS_ABSORB_REMAINDER = "cabinets_absorb_remainder"
    #: Slide 12, scenario 4. Named for what the reviewer must do next, not for what the code could
    #: not do — the difference is unresolvable in the drawing, and the next action is an RFI.
    CANNOT_BE_RESOLVED = "cannot_be_resolved"
    #: Ours, not the deck's: an equal split landed on a width no imperial drawing can carry.
    #: See `_is_a_writable_width`.
    SHARE_DOES_NOT_DIVIDE = "share_does_not_divide"
    #: Ours, not the deck's: unequal fillers must move and no rule says how. See
    #: `_apportionment_not_determined`.
    FILLER_APPORTIONMENT_NOT_DETERMINED = "filler_apportionment_not_determined"


class CabinetType(StrEnum):
    """The cabinet categories the deck names, and no others.

    Three regular types, each with its own width bound on slides 3 and 7 —
    `SINGLE_DOOR_CAB_WIDTH_MIN`, `DOUBLE_DOOR_CAB_WIDTH_MIN`, `DRAWER_CAB_WIDTH_MIN` — plus
    `CAB_EQUIP`, which has no bound here because it never moves.

    The value strings are recorded in a finding's trace, so they are part of the data contract.
    """

    SINGLE_DOOR = "single_door"
    DOUBLE_DOOR = "double_door"
    DRAWER = "drawer"
    EQUIPMENT = "equipment"

    @property
    def is_equipment(self) -> bool:
        return self is CabinetType.EQUIPMENT


def _pair(values: Sequence[Measurement], name: str) -> tuple[Measurement, Measurement]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise RuleAuthoringError(f"{name} must have list arity")
    if len(values) != 2:
        raise RuleAuthoringError(
            f"{name} must contain exactly two ordered values: left filler, then right filler"
        )
    left, right = values
    if not isinstance(left, Measurement) or not isinstance(right, Measurement):
        raise RuleAuthoringError(f"{name} values must be Measurements")
    return left, right


def _measurement(value: object, name: str) -> Measurement:
    if not isinstance(value, Measurement):
        raise RuleAuthoringError(f"{name} must be a Measurement")
    return value


def _sequence(values: object, name: str) -> tuple[Measurement, ...]:
    """An ordered run of measurements of any length.

    Unlike `_pair`, no arity is imposed here. Raj's deck names runs of more than three cabinets,
    two equipment cabinets and an equipment cabinet at the end; a length check would turn each of
    those drawings into a rule-authoring failure (#673).
    """
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise RuleAuthoringError(f"{name} must have list arity")
    if not values:
        raise RuleAuthoringError(f"{name} must not be empty")
    for value in values:
        if not isinstance(value, Measurement):
            raise RuleAuthoringError(f"{name} values must be Measurements")
    return tuple(values)


def _types(values: object, name: str, *, length: int) -> tuple[CabinetType, ...]:
    """The reviewer's per-cabinet classification, as the deck's own categories.

    Slide 11 puts this with the reviewer — *"User should be able to draw a bounding box around a
    cabinet and categorize that as a particular equipment cabinet and confirm width should be
    changed or cannot be changed"* — so it arrives as an operand and is never inferred here. The
    set is closed: an unrecognised category is a rule-authoring fault, not a cabinet to guess at.
    """
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise RuleAuthoringError(f"{name} must have list arity")
    if len(values) != length:
        raise RuleAuthoringError(
            f"{name} must carry one value per cabinet: got {len(values)} for {length} cabinet(s)"
        )
    classified: list[CabinetType] = []
    for value in values:
        try:
            classified.append(CabinetType(value))
        except ValueError:
            raise RuleAuthoringError(
                f"{name} values must be one of "
                f"{', '.join(sorted(member.value for member in CabinetType))}: got {value!r}"
            ) from None
    return tuple(classified)


def filler_distribution(
    *,
    field_width: Measurement,
    design_width: Measurement,
    design_fillers: Sequence[Measurement],
    proposed_fillers: Sequence[Measurement],
    filler_min: Measurement,
    filler_max: Measurement,
    allow_asymmetric: int,
) -> OperationResult:
    """Check an exact, filler-first response to a changed site width.

    Both filler sequences are ordered ``(left, right)``. The expected filler total is the
    architectural filler total plus ``field_width - design_width``. In the ordinary U.N.O. branch
    the expected total is divided equally. A reviewer-established noted branch may be asymmetric,
    but its two values must still sum exactly and each remain within the configured bounds.

    If the fillers cannot absorb the difference, the operation abstains and records the unabsorbed
    amount. It deliberately does not accept cabinet operands: Q9 assigns cabinet selection to the
    reviewer, so no code path here can move a non-adjustable cabinet.
    """

    field_width = _measurement(field_width, "field_width")
    design_width = _measurement(design_width, "design_width")
    filler_min = _measurement(filler_min, "filler_min")
    filler_max = _measurement(filler_max, "filler_max")
    design_left, design_right = _pair(design_fillers, "design_fillers")
    proposed_left, proposed_right = _pair(proposed_fillers, "proposed_fillers")
    if type(allow_asymmetric) is not int or allow_asymmetric not in (0, 1):
        raise RuleAuthoringError("allow_asymmetric must be the reviewed integer 0 or 1")

    unit = require_same_unit(
        field_width,
        design_width,
        design_left,
        design_right,
        proposed_left,
        proposed_right,
        filler_min,
        filler_max,
    )
    if filler_min.exact < 0:
        raise RuleAuthoringError("filler_min must not be negative")
    if filler_min.exact > filler_max.exact:
        raise RuleAuthoringError("filler_min must not exceed filler_max")

    site_difference = Measurement(field_width.exact - design_width.exact, unit, None)
    design_total = Measurement(design_left.exact + design_right.exact, unit, None)
    expected_total = Measurement(design_total.exact + site_difference.exact, unit, None)
    lower_total = Measurement(filler_min.exact * 2, unit, None)
    upper_total = Measurement(filler_max.exact * 2, unit, None)
    proposed_total = Measurement(proposed_left.exact + proposed_right.exact, unit, None)

    common_intermediates: tuple[tuple[str, object], ...] = (
        ("ordered_design_fillers", (design_left, design_right)),
        ("ordered_proposed_fillers", (proposed_left, proposed_right)),
        ("site_difference", site_difference),
        ("design_filler_total", design_total),
        ("expected_filler_total", expected_total),
        ("allowed_filler_total", (lower_total, upper_total)),
    )

    if expected_total.exact < lower_total.exact or expected_total.exact > upper_total.exact:
        boundary = lower_total if expected_total.exact < lower_total.exact else upper_total
        remaining = Measurement(expected_total.exact - boundary.exact, unit, None)
        direction = "reduce" if remaining.exact < 0 else "increase"
        return OperationResult(
            outcome=Outcome.REVIEW_REQUIRED,
            delta=Measurement(abs(remaining.exact), unit, None),
            intermediates=(
                *common_intermediates,
                ("condition", DistributionCondition.CABINET_SELECTION_REQUIRED.value),
                ("bounded_filler_total", boundary),
                ("remaining_difference", remaining),
                ("cabinet_adjustment", "reviewer_selection_required; no cabinet selected"),
            ),
            comparison=(
                f"fillers cannot absorb the site difference within {filler_min.exact}.."
                f"{filler_max.exact} {unit.value}; reviewer must select an adjustable cabinet "
                f"to {direction} by {abs(remaining.exact)} {unit.value}"
            ),
            tolerance=None,
        )

    bounds_ok = all(
        filler_min.exact <= filler.exact <= filler_max.exact
        for filler in (proposed_left, proposed_right)
    )
    total_ok = proposed_total.exact == expected_total.exact
    expected_pair: tuple[Measurement, Measurement] | None = None
    symmetry_ok = True
    if not allow_asymmetric:
        each = Measurement(expected_total.exact * Fraction(1, 2), unit, None)
        expected_pair = (each, each)
        symmetry_ok = proposed_left.exact == each.exact and proposed_right.exact == each.exact

    passed = bounds_ok and total_ok and symmetry_ok
    mismatch = Measurement(abs(proposed_total.exact - expected_total.exact), unit, None)
    return OperationResult(
        outcome=Outcome.PASS if passed else Outcome.FAIL,
        delta=mismatch,
        intermediates=(
            *common_intermediates,
            ("condition", DistributionCondition.FILLERS_ABSORB.value),
            ("expected_fillers", expected_pair),
            ("proposed_filler_total", proposed_total),
            ("each_filler_within_bounds", bounds_ok),
            ("asymmetric_note_applied", bool(allow_asymmetric)),
            ("cabinet_adjustment", "not_required; no cabinet selected"),
        ),
        comparison=(
            f"proposed fillers {'satisfy' if passed else 'do not satisfy'} exact total "
            f"{expected_total.exact} {unit.value}, individual bounds {filler_min.exact}.."
            f"{filler_max.exact}, and "
            f"{'the reviewer-noted asymmetric layout' if allow_asymmetric else 'equal U.N.O. split'}"
        ),
        tolerance=None,
    )


def cabinet_run_distribution(
    *,
    field_width: Measurement,
    design_width: Measurement,
    design_fillers: Sequence[Measurement],
    proposed_fillers: Sequence[Measurement],
    design_cabinets: Sequence[Measurement],
    proposed_cabinets: Sequence[Measurement],
    cabinet_type: Sequence[str],
    single_door_cab_width_min: Measurement,
    single_door_cab_width_max: Measurement,
    double_door_cab_width_min: Measurement,
    double_door_cab_width_max: Measurement,
    drawer_cab_width_min: Measurement,
    drawer_cab_width_max: Measurement,
    filler_min: Measurement,
    filler_max: Measurement,
) -> OperationResult:
    """Raj's two-step distribution, computed exactly and compared against the shop drawing.

    **The order is his, and it is not an optimisation.** Fillers move first, to their bound; only
    what the fillers cannot absorb reaches the cabinets; and of the cabinets, only the regular ones
    move. From the 2026-09-21 deck, slide 3, verbatim: *"the dimension adjustment should be made
    only on the regular cabinet (CAB_REGULAR) because the equipment cabinet dimensions should not be
    reduced otherwise equipment will not fit."* Slide 7 is the mirror for a site wider than the
    drawing. The remainder then divides equally between the regular cabinets — 90-82 leaves 42 over
    two regulars at 21 each, 88-96 leaves 54 at 27 each.

    **What this computes is an expectation, not an instruction.** It derives the widths the shop
    drawing should carry and reports whether the drawing carries them. It cuts nothing and selects
    nothing: which cabinets may move arrives in `cabinet_type`, from the reviewer.

    **The six type bounds have no defaults, on purpose.** A package that has not supplied
    `SINGLE_DOOR_CAB_WIDTH_MIN` and its five siblings gets NOT_FOUND from the engine before this
    runs, because every operand here is required. CLIENT_FACTS Q21 is explicit that the numbers
    themselves are unsettled — the email said 1"/2", the 2026-08-25 call said 3-4" — so a default
    would be this system inventing the one input the whole step-two calculation turns on.

    **His five outcomes, and two abstentions for his silences.** No change needed (slide 12,
    scenario 5) is a PASS. Fillers absorbing the whole difference, and cabinets absorbing the
    remainder, are expectations to compare. When the fillers are at their bound, the equipment
    cabinets cannot move and a regular cabinet would pass its own limit, the deck is explicit that
    the program *"should not force a fix"* — it reports that it cannot be resolved so the reviewer
    can raise an RFI.

    The other two are not his, and are marked as ours wherever they appear. Slide 12 names layouts
    the worked examples do not cover — more than three cabinets, unequal fillers — and for two of
    them the deck states no rule: how a remainder that does not divide equally is apportioned, and
    how *unequal* fillers move when they must. Both abstain rather than pick, because under exact
    match (Q2) a picked width is a verdict, not a preference.
    """

    field_width = _measurement(field_width, "field_width")
    design_width = _measurement(design_width, "design_width")
    filler_min = _measurement(filler_min, "filler_min")
    filler_max = _measurement(filler_max, "filler_max")
    bounds: dict[CabinetType, tuple[Measurement, Measurement]] = {
        CabinetType.SINGLE_DOOR: (
            _measurement(single_door_cab_width_min, "single_door_cab_width_min"),
            _measurement(single_door_cab_width_max, "single_door_cab_width_max"),
        ),
        CabinetType.DOUBLE_DOOR: (
            _measurement(double_door_cab_width_min, "double_door_cab_width_min"),
            _measurement(double_door_cab_width_max, "double_door_cab_width_max"),
        ),
        CabinetType.DRAWER: (
            _measurement(drawer_cab_width_min, "drawer_cab_width_min"),
            _measurement(drawer_cab_width_max, "drawer_cab_width_max"),
        ),
    }
    design_filler_run = _sequence(design_fillers, "design_fillers")
    proposed_filler_run = _sequence(proposed_fillers, "proposed_fillers")
    design_cabinet_run = _sequence(design_cabinets, "design_cabinets")
    proposed_cabinet_run = _sequence(proposed_cabinets, "proposed_cabinets")
    types = _types(cabinet_type, "cabinet_type", length=len(design_cabinet_run))

    for name, run in (
        ("proposed_fillers", proposed_filler_run),
        ("proposed_cabinets", proposed_cabinet_run),
    ):
        expected_length = (
            len(design_filler_run) if name == "proposed_fillers" else len(design_cabinet_run)
        )
        if len(run) != expected_length:
            raise RuleAuthoringError(
                f"{name} must be the same length as its design run: "
                f"got {len(run)} for {expected_length}"
            )

    unit = require_same_unit(
        field_width,
        design_width,
        filler_min,
        filler_max,
        *design_filler_run,
        *proposed_filler_run,
        *design_cabinet_run,
        *proposed_cabinet_run,
        *(bound for pair in bounds.values() for bound in pair),
    )
    if filler_min.exact < 0:
        raise RuleAuthoringError("filler_min must not be negative")
    if filler_min.exact > filler_max.exact:
        raise RuleAuthoringError("filler_min must not exceed filler_max")
    for cabinet_kind, (low, high) in bounds.items():
        if low.exact < 0:
            raise RuleAuthoringError(f"{cabinet_kind.value}_cab_width_min must not be negative")
        if low.exact > high.exact:
            raise RuleAuthoringError(
                f"{cabinet_kind.value}_cab_width_min must not exceed "
                f"{cabinet_kind.value}_cab_width_max"
            )

    def measure(value: Fraction) -> Measurement:
        return Measurement(value, unit, None)

    difference = field_width.exact - design_width.exact
    design_filler_total = sum((f.exact for f in design_filler_run), Fraction(0))
    design_cabinet_total = sum((c.exact for c in design_cabinet_run), Fraction(0))
    filler_floor = filler_min.exact * len(design_filler_run)
    filler_ceiling = filler_max.exact * len(design_filler_run)

    # Step 1. The fillers take as much of the difference as their bounds allow, and no more.
    wanted_filler_total = design_filler_total + difference
    filler_total = min(max(wanted_filler_total, filler_floor), filler_ceiling)
    remainder = difference - (filler_total - design_filler_total)

    facts: tuple[tuple[str, object], ...] = (
        ("site_difference", measure(difference)),
        ("design_filler_total", measure(design_filler_total)),
        ("design_cabinet_total", measure(design_cabinet_total)),
        ("expected_filler_total", measure(filler_total)),
        ("allowed_filler_total", (measure(filler_floor), measure(filler_ceiling))),
        ("cabinet_types", tuple(kind.value for kind in types)),
        (
            "equipment_cabinets",
            tuple(index for index, kind in enumerate(types) if kind.is_equipment),
        ),
        ("remainder_after_fillers", measure(remainder)),
    )

    regulars = tuple(index for index, kind in enumerate(types) if not kind.is_equipment)

    # Step 2. Only what the fillers could not absorb, and only across the regular cabinets.
    expected_cabinets = list(design_cabinet_run)
    share: Fraction | None = None
    if remainder != 0:
        if not regulars:
            return _cannot_resolve(
                facts,
                remainder=remainder,
                unit=unit,
                why="every cabinet in the run is an equipment cabinet, so nothing may move",
            )
        share = Fraction(remainder, 1) / len(regulars)
        for index in regulars:
            expected_cabinets[index] = measure(design_cabinet_run[index].exact + share)
        # Both of the deck's examples divide exactly — 6 over two cabinets. It never says what to do
        # when they do not, so this abstains rather than choosing. See `_is_a_writable_width`.
        #
        # **Before the bounds check, deliberately.** A width of 22 2/3" is not the answer this run
        # has; it is the sign that the rule does not determine one. Testing a bound against it would
        # report "cannot be resolved" on a run where 22 5/8 and 22 3/4 both fit.
        unwritable = tuple(
            index for index in regulars if not _is_a_writable_width(expected_cabinets[index].exact)
        )
        if unwritable:
            return _share_does_not_divide(
                facts,
                share=share,
                members=regulars,
                subject="regular_cabinet",
                widths=tuple(expected_cabinets[index].exact for index in unwritable),
                unit=unit,
            )
        for index in regulars:
            width = expected_cabinets[index].exact
            low, high = bounds[types[index]]
            if width < low.exact or width > high.exact:
                bound = "minimum" if width < low.exact else "maximum"
                limit = low.exact if width < low.exact else high.exact
                return _cannot_resolve(
                    facts,
                    remainder=remainder,
                    unit=unit,
                    why=(
                        f"cabinet {index} ({types[index].value}) would become {width} "
                        f"{unit.value}, past its {bound} of {limit} {unit.value} "
                        f"({types[index].value}_cab_width_"
                        f"{'min' if bound == 'minimum' else 'max'})"
                    ),
                )

    # Both worked examples start from equal fillers and end with equal fillers, which is the
    # evidence for splitting a moved total equally. Slide 12 also names unequal fillers as a layout
    # in scope, and for those the deck says only that each must honour its bound — never how the
    # change is shared out. Ours, not his: expect nothing per filler, and abstain.
    expected_fillers: tuple[Measurement, ...] = design_filler_run
    if filler_total != design_filler_total:
        if len({filler.exact for filler in design_filler_run}) != 1:
            return _apportionment_not_determined(
                facts,
                expected_total=measure(filler_total),
                floor=measure(filler_floor),
                ceiling=measure(filler_ceiling),
                unit=unit,
            )
        filler_share = Fraction(filler_total, 1) / len(design_filler_run)
        if not _is_a_writable_width(filler_share):
            return _share_does_not_divide(
                facts,
                share=filler_share,
                members=tuple(range(len(design_filler_run))),
                subject="filler",
                widths=(filler_share,),
                unit=unit,
            )
        expected_fillers = tuple(measure(filler_share) for _ in design_filler_run)
    matched = _runs_match(proposed_filler_run, expected_fillers) and _runs_match(
        proposed_cabinet_run, tuple(expected_cabinets)
    )
    proposed_total = sum(
        (m.exact for m in (*proposed_filler_run, *proposed_cabinet_run)), Fraction(0)
    )
    # The distance from the expectation, not from the wall. A drawing can total the field width
    # exactly and still put the inches on the wrong cabinet, and a delta of zero beside a FAIL
    # would tell the reviewer the opposite of what the finding says.
    deviation = sum(
        (
            abs(proposed.exact - expected.exact)
            for proposed, expected in zip(
                (*proposed_filler_run, *proposed_cabinet_run),
                (*expected_fillers, *expected_cabinets),
            )
        ),
        Fraction(0),
    )
    # Scenario 5 is not a special case with its own exit — it is this comparison with an expected
    # layout that happens to equal the design. Giving it an early return once meant a shop drawing
    # that moved widths the site never justified came back labelled "the fillers absorbed it".
    if difference == 0:
        condition = DistributionCondition.NO_CHANGE_REQUIRED
    elif remainder == 0:
        condition = DistributionCondition.FILLERS_ABSORB
    else:
        condition = DistributionCondition.CABINETS_ABSORB_REMAINDER
    return OperationResult(
        outcome=Outcome.PASS if matched else Outcome.FAIL,
        delta=measure(deviation),
        intermediates=(
            *facts,
            ("condition", condition.value),
            ("expected_fillers", expected_fillers),
            ("expected_cabinets", tuple(expected_cabinets)),
            ("share_per_regular_cabinet", None if share is None else measure(share)),
            ("regular_cabinets", regulars),
            # Slide 12, outcome 3: "mark the cabinets with green checks and change only the
            # fillers". The reviewer's screen needs to know that, so it is a recorded fact.
            ("cabinets_retained", share is None),
            ("proposed_run_total", measure(proposed_total)),
        ),
        comparison=(
            f"shop drawing {'matches' if matched else 'does not match'} the exact distribution: "
            f"fillers to {filler_total} {unit.value} total"
            + (
                ""
                if share is None
                else f", then {share} {unit.value} to each of {len(regulars)} regular cabinet(s)"
            )
        ),
        tolerance=None,
    )


def _is_a_writable_width(value: Fraction) -> bool:
    """Whether an exact width could be dimensioned on an imperial drawing at all.

    An imperial drawing carries binary fractions — halves, quarters, eighths, sixteenths. A width of
    22 2/3" is not a width anybody draws or cuts, so an equal split that produces one has not found
    the answer; it has found that the deck's rule does not determine the answer for this run.

    **This is a representability test, not a rounding rule.** It does not pick 22 5/8 over 22 3/4 —
    that choice is question 4 to Raj, still open, and under exact match (Q2) guessing it would be a
    wrong PASS or FAIL rather than a rounding preference. A denominator that is a power of two is a
    property of the imperial system; a tolerance or a nearest-increment would be our invention.
    """
    return value.denominator & (value.denominator - 1) == 0


def _apportionment_not_determined(
    facts: tuple[tuple[str, object], ...],
    *,
    expected_total: Measurement,
    floor: Measurement,
    ceiling: Measurement,
    unit: Unit,
) -> OperationResult:
    """Abstain when unequal fillers have to move and the deck does not say how.

    **This is ours, not Raj's.** Slide 12 lists unequal fillers as a layout in scope and slides 3
    and 7 say only that each filler honours its own bound. Two fillers of 2" and 4" that must lose
    2" between them could become 1"+3", 2"+2" or 1.5"+2.5", and nothing in the deck chooses. The
    finding therefore carries what *is* determined — the total the fillers must reach, and the
    bound each must stay inside — so the reviewer confirms an apportionment instead of typing one.
    """
    return OperationResult(
        outcome=Outcome.REVIEW_REQUIRED,
        delta=expected_total,
        intermediates=(
            *facts,
            ("condition", DistributionCondition.FILLER_APPORTIONMENT_NOT_DETERMINED.value),
            ("expected_filler_total", expected_total),
            ("filler_width_bounds", (floor, ceiling)),
            (
                "reviewer_action",
                (
                    "the fillers started unequal and must change; confirm how the difference is "
                    "shared between them"
                ),
            ),
        ),
        comparison=(
            f"the fillers must total {expected_total.exact} {unit.value}, but they started unequal "
            "and the deck states no rule for sharing a change between unequal fillers"
        ),
        tolerance=None,
    )


def _share_does_not_divide(
    facts: tuple[tuple[str, object], ...],
    *,
    share: Fraction,
    members: tuple[int, ...],
    subject: str,
    widths: tuple[Fraction, ...],
    unit: Unit,
) -> OperationResult:
    """Abstain when an equal split lands on a width no drawing can carry.

    The exact share travels with the finding so the reviewer sees what the rule produced, and can
    say how the difference was really apportioned — which is the answer the open question needs.
    `subject` names what was being split, so the recorded key is `share_per_regular_cabinet` or
    `share_per_filler` and never claims to be the other one.
    """
    return OperationResult(
        outcome=Outcome.REVIEW_REQUIRED,
        delta=Measurement(abs(share), unit, None),
        intermediates=(
            *facts,
            ("condition", DistributionCondition.SHARE_DOES_NOT_DIVIDE.value),
            (f"share_per_{subject}", Measurement(share, unit, None)),
            (f"{subject}s", members),
            (
                "reviewer_action",
                (
                    "an equal split does not land on a drawable width; confirm how the "
                    "difference is apportioned"
                ),
            ),
        ),
        comparison=(
            f"an equal split gives {share} {unit.value} to each of {len(members)} "
            f"{subject.replace('_', ' ')}(s), making "
            + ", ".join(f"{width} {unit.value}" for width in widths)
            + " — not a width an imperial drawing can carry"
        ),
        tolerance=None,
    )


def _runs_match(left: Sequence[Measurement], right: Sequence[Measurement]) -> bool:
    """Exact equality, position by position. Q2 leaves no tolerance band to round into."""
    return len(left) == len(right) and all(a.exact == b.exact for a, b in zip(left, right))


def _cannot_resolve(
    facts: tuple[tuple[str, object], ...],
    *,
    remainder: Fraction,
    unit: Unit,
    why: str,
) -> OperationResult:
    """Slide 12, scenario 4: report it and stop.

    *"The program should not force a fix. It should flag 'cannot be resolved, RFI to architect.'"*
    The unabsorbed amount and the constraint that blocked it both travel with the finding, because
    an RFI that does not say how far short the drawing falls costs a second round trip.
    """
    unabsorbed = Measurement(abs(remainder), unit, None)
    return OperationResult(
        outcome=Outcome.REVIEW_REQUIRED,
        delta=unabsorbed,
        intermediates=(
            *facts,
            ("condition", DistributionCondition.CANNOT_BE_RESOLVED.value),
            ("unabsorbed_difference", unabsorbed),
            ("blocked_by", why),
            (
                "reviewer_action",
                "cannot be resolved by distribution; raise an RFI to the architect",
            ),
        ),
        comparison=(
            f"the site difference cannot be absorbed within the stated limits: {why}. "
            f"{abs(remainder)} {unit.value} remains unabsorbed"
        ),
        tolerance=None,
    )


DISTRIBUTION_SPECS: tuple[OperationSpec, ...] = (
    OperationSpec(
        "filler_distribution",
        "1.0.0",
        {
            "field_width": Arity.SCALAR,
            "design_width": Arity.SCALAR,
            "design_fillers": Arity.LIST,
            "proposed_fillers": Arity.LIST,
            "filler_min": Arity.SCALAR,
            "filler_max": Arity.SCALAR,
            "allow_asymmetric": Arity.SCALAR,
        },
        filler_distribution,
    ),
    OperationSpec(
        "cabinet_run_distribution",
        "1.0.0",
        {
            "field_width": Arity.SCALAR,
            "design_width": Arity.SCALAR,
            "design_fillers": Arity.LIST,
            "proposed_fillers": Arity.LIST,
            "design_cabinets": Arity.LIST,
            "proposed_cabinets": Arity.LIST,
            "cabinet_type": Arity.LIST,
            "single_door_cab_width_min": Arity.SCALAR,
            "single_door_cab_width_max": Arity.SCALAR,
            "double_door_cab_width_min": Arity.SCALAR,
            "double_door_cab_width_max": Arity.SCALAR,
            "drawer_cab_width_min": Arity.SCALAR,
            "drawer_cab_width_max": Arity.SCALAR,
            "filler_min": Arity.SCALAR,
            "filler_max": Arity.SCALAR,
        },
        cabinet_run_distribution,
    ),
)


def register_distribution_operations() -> None:
    """Register the reviewed distribution operations.

    `filler_distribution` stays exactly as published. Its snapshot is content-addressed and a
    finding cites the text that judged it, so changing its operand set would make an already
    recorded verdict claim to have used a signature that no longer exists.
    """

    for spec in DISTRIBUTION_SPECS:
        register(spec)
