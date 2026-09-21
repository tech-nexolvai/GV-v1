"""Request and response models for the filler distribution calculator."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CabinetInput(BaseModel):
    """One cabinet in the ordered run the reviewer is checking."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    width: str = Field(
        min_length=1,
        max_length=100,
        description='Authored dimension with its unit, e.g. 30" or 762 mm.',
    )


class FillerInput(BaseModel):
    """One filler, ordered left then right in the request."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    width: str = Field(
        min_length=1,
        max_length=100,
        description='Authored dimension with its unit, e.g. 2" or 51 mm.',
    )


class AssemblyInput(BaseModel):
    """The ordered cabinet/filler assembly used to derive the design width."""

    model_config = ConfigDict(extra="forbid")

    cabinets: tuple[CabinetInput, ...] = Field(min_length=1)
    fillers: tuple[FillerInput, FillerInput]


class FillerDistributionRequest(BaseModel):
    """Inputs for the deterministic filler-first distribution proposal.

    ``field_width`` is nullable on purpose: a missing on-site field dimension is a business
    abstention (NOT_FOUND), not a guessed zero or a selected default.
    """

    model_config = ConfigDict(extra="forbid")

    assembly: AssemblyInput
    field_width: str | None = Field(
        default=None,
        max_length=100,
        description='Reviewer-entered site dimension with its unit, e.g. 96 1/2".',
    )
    filler_min: str = Field(min_length=1, max_length=100)
    filler_max: str = Field(min_length=1, max_length=100)
    adjustable_cabinet_id: str | None = Field(default=None, min_length=1, max_length=100)


class QuantityOut(BaseModel):
    """One exact dimension, rendered without a JSON float."""

    numerator: str
    denominator: str
    unit: Literal["in", "mm"]
    display: str
    as_typed: str | None = None


class OperandTraceOut(BaseModel):
    """The source record the response gives for an input value."""

    name: str
    source: Literal["ARCH", "SHOP", "USER_INPUT", "LITERAL"]
    status: Literal["HUMAN_CONFIRMED"]
    value: QuantityOut | None


class FillerProposalOut(BaseModel):
    """One filler before and after the distribution calculation."""

    id: str
    original: QuantityOut
    proposed: QuantityOut


class CabinetProposalOut(BaseModel):
    """One cabinet before and after the distribution calculation."""

    id: str
    original: QuantityOut
    proposed: QuantityOut
    adjustable: bool


class FillerDistributionResponse(BaseModel):
    """A deterministic proposal, or an honest abstention."""

    outcome: Literal["PASS", "REVIEW_REQUIRED", "NOT_FOUND"]
    condition: str
    message: str
    design_width: QuantityOut
    site_difference: QuantityOut | None
    field_dimension: OperandTraceOut
    selected_adjustable_cabinet_id: str | None
    fillers: tuple[FillerProposalOut, FillerProposalOut]
    cabinets: tuple[CabinetProposalOut, ...]
    operands: tuple[OperandTraceOut, ...]
    calculation: str
