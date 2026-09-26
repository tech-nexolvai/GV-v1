"""Request and response models for the filler distribution calculator."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The four categories Raj's deck names. Three regular types, each with its own width bound, plus
#: the equipment cabinet, which has no bound here because the distribution never moves it.
CabinetTypeName = Literal["single_door", "double_door", "drawer", "equipment"]


class CabinetInput(BaseModel):
    """One cabinet in the ordered run the reviewer is checking."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    width: str = Field(
        min_length=1,
        max_length=100,
        description='Authored dimension with its unit, e.g. 30" or 762 mm.',
    )
    type: CabinetTypeName = Field(
        description=(
            "The reviewer's classification of this cabinet. Slide 11 of the 2026-09-21 deck puts "
            "this with the reviewer — they categorise a cabinet and confirm whether its width may "
            "change — so it is a required input and the server never infers it."
        )
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
    #: Any number, ordered left to right. Raj's examples show two, and slide 12 names layouts with
    #: a wall on only one side, so an arity of exactly two would refuse a run the deck describes.
    fillers: tuple[FillerInput, ...] = Field(min_length=1)


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
    #: The three regular types' bounds, all required and **none defaulted**. CLIENT_FACTS Q21 is
    #: explicit that the values are unsettled — the email said 1"/2", the 2026-08-25 call said
    #: 3-4" — so an absent bound is a missing reviewer input, never a number this service chooses.
    single_door_cab_width_min: str = Field(min_length=1, max_length=100)
    single_door_cab_width_max: str = Field(min_length=1, max_length=100)
    double_door_cab_width_min: str = Field(min_length=1, max_length=100)
    double_door_cab_width_max: str = Field(min_length=1, max_length=100)
    drawer_cab_width_min: str = Field(min_length=1, max_length=100)
    drawer_cab_width_max: str = Field(min_length=1, max_length=100)


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
    type: CabinetTypeName
    original: QuantityOut
    proposed: QuantityOut
    #: Whether this cabinet's width may change at all — true for every regular cabinet, false for
    #: an equipment cabinet. It is no longer "the one cabinet the reviewer picked": the remainder
    #: divides equally across all of them, so more than one can be true.
    adjustable: bool


class FillerDistributionResponse(BaseModel):
    """A deterministic proposal, or an honest abstention."""

    outcome: Literal["PASS", "REVIEW_REQUIRED", "NOT_FOUND"]
    #: The operation's own condition, passed through rather than translated, so the reviewer's
    #: screen and a stored finding use one vocabulary.
    condition: str
    #: The full explanation, in the shape Raj's slides 5 and 9 ask for: the two widths, what the
    #: fillers could absorb, and what the cabinets take. Assembled from the exact numbers, never by
    #: a model.
    message: str
    #: One line for a list of findings, before a reviewer opens one.
    summary: str
    design_width: QuantityOut
    site_difference: QuantityOut | None
    field_dimension: OperandTraceOut
    fillers: tuple[FillerProposalOut, ...]
    cabinets: tuple[CabinetProposalOut, ...]
    #: True when the fillers absorbed the whole difference — slide 12, outcome 3: "mark the
    #: cabinets with green checks and change only the fillers".
    cabinets_retained: bool
    #: What the reviewer must do when the calculation abstained, in their words. Null otherwise.
    reviewer_action: str | None = None
    operands: tuple[OperandTraceOut, ...]
    calculation: str
