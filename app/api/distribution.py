"""Reviewer-facing cabinet distribution calculator for Q8/Q9/Q21.

The arithmetic is the reviewed ``cabinet_run_distribution`` operation and none of it is repeated
here. This route only shapes that operation's result into the product form: parse the authored
dimensions, hand them over, and render what came back.

**The operation judges a drawing; this route asks it what the drawing should say.** They are the
same calculation read two ways. The reviewer has no corrected shop drawing yet, so the run they
hold — the architectural one — is submitted as the proposal, and the operation's *expectation* is
the answer they wanted. Its verdict on that submission is therefore not the reviewer's verdict:
a FAIL means only "the architectural layout is not the site-corrected one", which is exactly the
case where there is a proposal worth showing. What travels to the screen unchanged is the
operation's ``condition``, so the route and a stored finding speak one vocabulary.

**Nothing here decides which cabinet may move.** That is the reviewer's classification, arriving
per cabinet as ``type`` (slide 11 of the 2026-09-21 deck). The route never infers it, and the
remainder divides equally across every regular cabinet rather than landing on one.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import Principal, require_project_access
from app.schemas.distribution import (
    CabinetProposalOut,
    CabinetTypeName,
    FillerDistributionRequest,
    FillerDistributionResponse,
    FillerProposalOut,
    OperandTraceOut,
    QuantityOut,
)
from units.imperial import format_inches
from units.measurement import Measurement, Unit
from units.normalise import UnitNormalisationError, normalise_to_inches
from verdict.operations.distribution import DistributionCondition, cabinet_run_distribution
from verdict.outcomes import Outcome
from verdict.registry import RuleAuthoringError
from workflow.distribution_narrative import explain_distribution

router = APIRouter(tags=["distribution"])

#: A one-line summary of each condition, kept beside the full explanation rather than replaced by
#: it: `workflow.distribution_narrative` writes the paragraph a reviewer reads, and this is the
#: label a list of findings shows before they open one. Keyed by the operation's own vocabulary so a
#: condition it gains without a line here fails loudly in tests rather than reaching a screen
#: unexplained.
_MESSAGES: dict[str, str] = {
    DistributionCondition.NO_CHANGE_REQUIRED.value: (
        "The site matches the architectural drawing, so no width needs to change."
    ),
    DistributionCondition.FILLERS_ABSORB.value: (
        "The fillers can absorb the site difference on their own; no cabinet changes."
    ),
    DistributionCondition.CABINETS_ABSORB_REMAINDER.value: (
        "The fillers reached their limit, so the rest is divided equally between the regular "
        "cabinets. The equipment cabinets keep their width."
    ),
    DistributionCondition.CANNOT_BE_RESOLVED.value: (
        "The site difference cannot be absorbed within the stated limits. This needs an RFI to "
        "the architect rather than a forced fix."
    ),
    DistributionCondition.SHARE_DOES_NOT_DIVIDE.value: (
        "Dividing the difference equally does not land on a width a drawing can carry, so the "
        "split is not settled by the rule. Confirm how it is apportioned."
    ),
    DistributionCondition.RUN_SHAPE_UNSUPPORTED.value: (
        "This run is a shape the check cannot compare — the parts on the two drawings do not line "
        "up, or a cabinet has no classification. Check it by hand."
    ),
    DistributionCondition.FILLER_APPORTIONMENT_NOT_DETERMINED.value: (
        "The fillers started unequal and have to change, and no rule says how the change is "
        "shared between them. Confirm the split."
    ),
}


def _parse(value: str, *, field: str) -> Measurement:
    """Parse one API dimension into the exact inch value the operation consumes."""

    try:
        return normalise_to_inches(value)
    except UnitNormalisationError as refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{field}: {refused}. Give the value with its unit, for example "
                '25 1/2" or 648 mm.'
            ),
        ) from refused


def _derived(value: Fraction, unit: Unit = Unit.INCH) -> Measurement:
    """A computed exact measurement with no authored token."""

    return Measurement(value, unit, None)


def _quantity(measurement: Measurement) -> QuantityOut:
    display = (
        f'{format_inches(measurement.exact)}"'
        if measurement.unit is Unit.INCH
        else f"{measurement.exact} {measurement.unit.value}"
    )
    return QuantityOut(
        numerator=str(measurement.exact.numerator),
        denominator=str(measurement.exact.denominator),
        unit=measurement.unit.value,
        display=display,
        as_typed=measurement.raw_text,
    )


def _nullable_quantity(measurement: Measurement | None) -> QuantityOut | None:
    if measurement is None:
        return None
    return _quantity(measurement)


def _operand(
    name: str,
    source: Literal["ARCH", "SHOP", "USER_INPUT", "LITERAL"],
    value: Measurement | None,
) -> OperandTraceOut:
    return OperandTraceOut(
        name=name,
        source=source,
        status="HUMAN_CONFIRMED",
        value=_nullable_quantity(value),
    )


def _total(measurements: tuple[Measurement, ...]) -> Measurement:
    return _derived(sum((measurement.exact for measurement in measurements), Fraction(0)))


@router.post(
    "/projects/{project_id}/filler-distribution",
    response_model=FillerDistributionResponse,
    summary="Calculate the site-corrected layout for one cabinet run",
)
def calculate_filler_distribution(
    project_id: UUID,
    payload: FillerDistributionRequest,
    principal: Annotated[Principal, Depends(require_project_access)],
) -> FillerDistributionResponse:
    """Return the two-step proposal, abstaining when reviewer input is missing."""

    del principal, project_id

    cabinets = tuple(
        _parse(cabinet.width, field=f"assembly.cabinets[{index}].width")
        for index, cabinet in enumerate(payload.assembly.cabinets)
    )
    design_fillers = tuple(
        _parse(filler.width, field=f"assembly.fillers[{index}].width")
        for index, filler in enumerate(payload.assembly.fillers)
    )
    types: tuple[CabinetTypeName, ...] = tuple(
        cabinet.type for cabinet in payload.assembly.cabinets
    )
    bounds = {
        name: _parse(getattr(payload, name), field=name)
        for name in (
            "filler_min",
            "filler_max",
            "single_door_cab_width_min",
            "single_door_cab_width_max",
            "double_door_cab_width_min",
            "double_door_cab_width_max",
            "drawer_cab_width_min",
            "drawer_cab_width_max",
        )
    }

    cabinet_ids = tuple(cabinet.id for cabinet in payload.assembly.cabinets)
    if len(set(cabinet_ids)) != len(cabinet_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="cabinet ids must be unique so the proposal names one cabinet each",
        )

    design_width = _derived(_total(cabinets).exact + _total(design_fillers).exact)
    design_filler_total = _total(design_fillers)

    def respond(
        *,
        outcome: Literal["PASS", "REVIEW_REQUIRED", "NOT_FOUND"],
        condition: str,
        message: str,
        site_difference: Measurement | None,
        field_width: Measurement | None,
        proposed_fillers: tuple[Measurement, ...],
        proposed_cabinets: tuple[Measurement, ...],
        cabinets_retained: bool,
        reviewer_action: str | None,
        calculation: str,
        summary: str,
    ) -> FillerDistributionResponse:
        return FillerDistributionResponse(
            outcome=outcome,
            condition=condition,
            message=message,
            design_width=_quantity(design_width),
            site_difference=_nullable_quantity(site_difference),
            field_dimension=_operand("field_width", "USER_INPUT", field_width),
            fillers=tuple(
                FillerProposalOut(
                    id=filler.id,
                    original=_quantity(original),
                    proposed=_quantity(proposed),
                )
                for filler, original, proposed in zip(
                    payload.assembly.fillers, design_fillers, proposed_fillers, strict=True
                )
            ),
            cabinets=tuple(
                CabinetProposalOut(
                    id=cabinet.id,
                    type=cabinet.type,
                    original=_quantity(original),
                    proposed=_quantity(proposed),
                    adjustable=cabinet.type != "equipment",
                )
                for cabinet, original, proposed in zip(
                    payload.assembly.cabinets, cabinets, proposed_cabinets, strict=True
                )
            ),
            cabinets_retained=cabinets_retained,
            reviewer_action=reviewer_action,
            summary=summary,
            operands=(
                _operand("field_width", "USER_INPUT", field_width),
                _operand("design_width", "ARCH", design_width),
                _operand("design_fillers", "ARCH", design_filler_total),
                _operand("filler_bounds", "LITERAL", bounds["filler_min"]),
            ),
            calculation=calculation,
        )

    if payload.field_width is None:
        return respond(
            outcome="NOT_FOUND",
            condition="field_width_missing",
            message=(
                "The on-site field dimension was not supplied, so the distribution cannot be "
                "calculated."
            ),
            summary="the site field width is missing",
            site_difference=None,
            field_width=None,
            proposed_fillers=design_fillers,
            proposed_cabinets=cabinets,
            cabinets_retained=False,
            reviewer_action="enter the site field width",
            calculation="field width missing; no arithmetic run",
        )

    field_width = _parse(payload.field_width, field="field_width")

    # The architectural run is submitted as the proposal because it is the run the reviewer holds.
    # What is wanted back is the operation's expectation, not its verdict on that submission.
    try:
        result = cabinet_run_distribution(
            field_width=field_width,
            design_width=design_width,
            design_fillers=list(design_fillers),
            proposed_fillers=list(design_fillers),
            design_cabinets=list(cabinets),
            proposed_cabinets=list(cabinets),
            cabinet_type=list(types),
            **bounds,
        )
    except RuleAuthoringError as refused:
        # Bounds that contradict each other are a reviewer's input mistake here, not a rule wired
        # wrongly, so they come back as 422 rather than reaching the client as a server fault.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(refused)
        ) from refused

    facts = dict(result.intermediates)
    condition = str(facts["condition"])
    # An operation that refused the run's shape abstained before any arithmetic, so it reports no
    # site difference. Every other path computes one.
    site_difference = facts.get("site_difference")
    assert site_difference is None or isinstance(site_difference, Measurement)

    if condition not in _MESSAGES:
        raise RuntimeError(
            f"cabinet_run_distribution returned condition {condition!r}, which this route has no "
            "reviewer-facing message for"
        )

    if result.outcome is Outcome.REVIEW_REQUIRED:
        return respond(
            outcome="REVIEW_REQUIRED",
            condition=condition,
            message=explain_distribution(facts),
            summary=_MESSAGES[condition],
            site_difference=site_difference,
            field_width=field_width,
            proposed_fillers=design_fillers,
            proposed_cabinets=cabinets,
            # False on every abstention, whatever the reason. The flag drives green checks on the
            # cabinets, and a calculation that declined to reach an answer has not cleared them.
            cabinets_retained=False,
            reviewer_action=str(facts.get("reviewer_action", "")) or None,
            calculation=result.comparison,
        )

    expected_fillers = facts["expected_fillers"]
    expected_cabinets = facts["expected_cabinets"]
    assert isinstance(expected_fillers, tuple)
    assert isinstance(expected_cabinets, tuple)
    return respond(
        # PASS reports that the calculation reached an answer, not that a drawing was approved.
        # The operation's FAIL on the architectural run is what makes the proposal non-trivial.
        outcome="PASS",
        condition=condition,
        message=explain_distribution(facts),
        summary=_MESSAGES[condition],
        site_difference=site_difference,
        field_width=field_width,
        proposed_fillers=expected_fillers,
        proposed_cabinets=expected_cabinets,
        cabinets_retained=bool(facts["cabinets_retained"]),
        reviewer_action=None,
        calculation=result.comparison,
    )


__all__ = ["calculate_filler_distribution", "router"]
