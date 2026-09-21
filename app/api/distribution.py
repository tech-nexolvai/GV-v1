"""Reviewer-facing filler distribution calculator for Q9/Q21.

The arithmetic is the reviewed ``filler_distribution`` operation. This route wraps it in the
product shape Raj asked for: the reviewer supplies the site field width and, only when fillers
cannot absorb the difference, the cabinet they are willing to adjust. The route never chooses that
cabinet itself.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import Principal, require_project_access
from app.schemas.distribution import (
    CabinetProposalOut,
    FillerDistributionRequest,
    FillerDistributionResponse,
    FillerProposalOut,
    OperandTraceOut,
    QuantityOut,
)
from units.imperial import format_inches
from units.measurement import Measurement, Unit
from units.normalise import UnitNormalisationError, normalise_to_inches
from verdict.operations.distribution import DistributionCondition, filler_distribution
from verdict.outcomes import Outcome

router = APIRouter(tags=["distribution"])


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


def _design_width(
    cabinets: tuple[Measurement, ...], fillers: tuple[Measurement, Measurement]
) -> Measurement:
    total = sum((measurement.exact for measurement in cabinets), Fraction(0))
    total += fillers[0].exact + fillers[1].exact
    return _derived(total)


def _equal_fillers(total: Measurement) -> tuple[Measurement, Measurement]:
    each = _derived(total.exact * Fraction(1, 2), total.unit)
    return (each, each)


@router.post(
    "/projects/{project_id}/filler-distribution",
    response_model=FillerDistributionResponse,
    summary="Calculate a reviewer-confirmed filler/cabinet distribution",
)
def calculate_filler_distribution(
    project_id: UUID,
    payload: FillerDistributionRequest,
    principal: Annotated[Principal, Depends(require_project_access)],
) -> FillerDistributionResponse:
    """Return the filler-first proposal, abstaining when reviewer input is missing."""

    del principal

    cabinets = tuple(
        _parse(cabinet.width, field=f"assembly.cabinets[{index}].width")
        for index, cabinet in enumerate(payload.assembly.cabinets)
    )
    design_fillers = (
        _parse(payload.assembly.fillers[0].width, field="assembly.fillers[0].width"),
        _parse(payload.assembly.fillers[1].width, field="assembly.fillers[1].width"),
    )
    filler_min = _parse(payload.filler_min, field="filler_min")
    filler_max = _parse(payload.filler_max, field="filler_max")
    if filler_min.exact < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="filler_min must not be negative",
        )
    if filler_min.exact > filler_max.exact:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="filler_min must not exceed filler_max",
        )
    cabinet_ids = tuple(cabinet.id for cabinet in payload.assembly.cabinets)
    if len(set(cabinet_ids)) != len(cabinet_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="cabinet ids must be unique so a reviewer selection identifies one cabinet",
        )
    design_width = _design_width(cabinets, design_fillers)

    original_filler_out = (
        FillerProposalOut(
            id=payload.assembly.fillers[0].id,
            original=_quantity(design_fillers[0]),
            proposed=_quantity(design_fillers[0]),
        ),
        FillerProposalOut(
            id=payload.assembly.fillers[1].id,
            original=_quantity(design_fillers[1]),
            proposed=_quantity(design_fillers[1]),
        ),
    )
    original_cabinet_out = tuple(
        CabinetProposalOut(
            id=cabinet.id,
            original=_quantity(width),
            proposed=_quantity(width),
            adjustable=False,
        )
        for cabinet, width in zip(payload.assembly.cabinets, cabinets, strict=True)
    )

    if payload.field_width is None:
        return FillerDistributionResponse(
            outcome="NOT_FOUND",
            condition="field_width_missing",
            message=(
                "The on-site field dimension was not supplied, so the distribution cannot be "
                "calculated."
            ),
            design_width=_quantity(design_width),
            site_difference=None,
            field_dimension=_operand("field_width", "USER_INPUT", None),
            selected_adjustable_cabinet_id=payload.adjustable_cabinet_id,
            fillers=original_filler_out,
            cabinets=original_cabinet_out,
            operands=(
                _operand("field_width", "USER_INPUT", None),
                _operand("design_fillers", "ARCH", None),
                _operand("filler_bounds", "LITERAL", None),
            ),
            calculation="field width missing; no arithmetic run",
        )

    field_width = _parse(payload.field_width, field="field_width")
    site_difference = _derived(field_width.exact - design_width.exact)
    design_total = _derived(design_fillers[0].exact + design_fillers[1].exact)
    requested_filler_total = _derived(design_total.exact + site_difference.exact)
    lower_total = _derived(filler_min.exact * 2)
    upper_total = _derived(filler_max.exact * 2)

    if lower_total.exact <= requested_filler_total.exact <= upper_total.exact:
        proposed_fillers = _equal_fillers(requested_filler_total)
        operation = filler_distribution(
            field_width=field_width,
            design_width=design_width,
            design_fillers=design_fillers,
            proposed_fillers=proposed_fillers,
            filler_min=filler_min,
            filler_max=filler_max,
            allow_asymmetric=0,
        )
        return FillerDistributionResponse(
            outcome="PASS",
            condition=DistributionCondition.FILLERS_ABSORB.value,
            message="The fillers can absorb the site difference; no cabinet adjustment is needed.",
            design_width=_quantity(design_width),
            site_difference=_quantity(site_difference),
            field_dimension=_operand("field_width", "USER_INPUT", field_width),
            selected_adjustable_cabinet_id=None,
            fillers=(
                FillerProposalOut(
                    id=payload.assembly.fillers[0].id,
                    original=_quantity(design_fillers[0]),
                    proposed=_quantity(proposed_fillers[0]),
                ),
                FillerProposalOut(
                    id=payload.assembly.fillers[1].id,
                    original=_quantity(design_fillers[1]),
                    proposed=_quantity(proposed_fillers[1]),
                ),
            ),
            cabinets=original_cabinet_out,
            operands=(
                _operand("field_width", "USER_INPUT", field_width),
                _operand("design_width", "ARCH", design_width),
                _operand("design_fillers", "ARCH", design_total),
                _operand("filler_bounds", "LITERAL", _derived(filler_min.exact)),
            ),
            calculation=operation.comparison,
        )

    bounded_total = lower_total if requested_filler_total.exact < lower_total.exact else upper_total
    bounded_fillers = _equal_fillers(bounded_total)
    operation = filler_distribution(
        field_width=field_width,
        design_width=design_width,
        design_fillers=design_fillers,
        proposed_fillers=bounded_fillers,
        filler_min=filler_min,
        filler_max=filler_max,
        allow_asymmetric=0,
    )
    if operation.outcome is not Outcome.REVIEW_REQUIRED:
        raise RuntimeError("filler_distribution did not abstain when filler bounds were exceeded")

    if payload.adjustable_cabinet_id is None:
        return FillerDistributionResponse(
            outcome="REVIEW_REQUIRED",
            condition=DistributionCondition.CABINET_SELECTION_REQUIRED.value,
            message=(
                "Fillers cannot absorb the site difference within their bounds. A reviewer must "
                "choose the adjustable cabinet; the system did not pick one."
            ),
            design_width=_quantity(design_width),
            site_difference=_quantity(site_difference),
            field_dimension=_operand("field_width", "USER_INPUT", field_width),
            selected_adjustable_cabinet_id=None,
            fillers=(
                FillerProposalOut(
                    id=payload.assembly.fillers[0].id,
                    original=_quantity(design_fillers[0]),
                    proposed=_quantity(bounded_fillers[0]),
                ),
                FillerProposalOut(
                    id=payload.assembly.fillers[1].id,
                    original=_quantity(design_fillers[1]),
                    proposed=_quantity(bounded_fillers[1]),
                ),
            ),
            cabinets=original_cabinet_out,
            operands=(
                _operand("field_width", "USER_INPUT", field_width),
                _operand("design_width", "ARCH", design_width),
                _operand("design_fillers", "ARCH", design_total),
                _operand("filler_bounds", "LITERAL", _derived(filler_min.exact)),
            ),
            calculation=operation.comparison,
        )

    try:
        selected_index = cabinet_ids.index(payload.adjustable_cabinet_id)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="adjustable_cabinet_id must name one of the supplied cabinets",
        ) from error

    cabinet_delta = _derived(requested_filler_total.exact - bounded_total.exact)
    proposed_cabinets = tuple(
        CabinetProposalOut(
            id=cabinet.id,
            original=_quantity(width),
            proposed=_quantity(
                _derived(width.exact + cabinet_delta.exact) if index == selected_index else width
            ),
            adjustable=index == selected_index,
        )
        for index, (cabinet, width) in enumerate(
            zip(payload.assembly.cabinets, cabinets, strict=True)
        )
    )
    return FillerDistributionResponse(
        outcome="PASS",
        condition="cabinet_adjusted_by_reviewer_selection",
        message=(
            "Fillers were set to their bounds and the remaining difference was assigned to the "
            "reviewer-chosen adjustable cabinet."
        ),
        design_width=_quantity(design_width),
        site_difference=_quantity(site_difference),
        field_dimension=_operand("field_width", "USER_INPUT", field_width),
        selected_adjustable_cabinet_id=payload.adjustable_cabinet_id,
        fillers=(
            FillerProposalOut(
                id=payload.assembly.fillers[0].id,
                original=_quantity(design_fillers[0]),
                proposed=_quantity(bounded_fillers[0]),
            ),
            FillerProposalOut(
                id=payload.assembly.fillers[1].id,
                original=_quantity(design_fillers[1]),
                proposed=_quantity(bounded_fillers[1]),
            ),
        ),
        cabinets=proposed_cabinets,
        operands=(
            _operand("field_width", "USER_INPUT", field_width),
            _operand("design_width", "ARCH", design_width),
            _operand("design_fillers", "ARCH", design_total),
            _operand("adjustable_cabinet", "USER_INPUT", cabinets[selected_index]),
        ),
        calculation=(
            f"{operation.comparison}; reviewer selected {payload.adjustable_cabinet_id}, so the "
            f'remaining {format_inches(cabinet_delta.exact)}" is applied to that cabinet'
        ),
    )


__all__ = ["calculate_filler_distribution", "router"]
