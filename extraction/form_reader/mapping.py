"""Deterministic, reviewer-owned form-field proposals from paired form readings."""

from __future__ import annotations

from dataclasses import dataclass

from extraction.form_reader.agreement import ComparedReading
from extraction.form_reader.schema import PageFormAnswer


@dataclass(frozen=True, slots=True)
class FormFieldProposal:
    """A candidate-to-form-slot suggestion. It contains no operand value."""

    field_key: str
    position: int
    reading: ComparedReading


@dataclass(frozen=True, slots=True)
class FormReviewQuestion:
    """A candidate that should be shown for human review, not assigned to a field."""

    reading: ComparedReading
    review_reason: str


@dataclass(frozen=True, slots=True)
class FormMapping:
    proposals: tuple[FormFieldProposal, ...]
    questions: tuple[FormReviewQuestion, ...]


def map_page_to_fields(
    first: PageFormAnswer,
    second: PageFormAnswer,
    readings: tuple[ComparedReading, ...],
) -> FormMapping:
    """Map only corroborated one-countertop values; all other readings stay questions.

    Field keys are the published required-input keys used by the measurement form. Chain order is
    the two readers' common structural order, never inferred from model box coordinates.
    """
    single_countertop = len(first.countertops) == len(second.countertops) == 1
    proposals: list[FormFieldProposal] = []
    questions: list[FormReviewQuestion] = []
    cabinet_position = 0
    filler_position = 0
    for reading in sorted(
        readings,
        key=lambda item: (
            item.countertop_index,
            -1 if item.chain_index is None else item.chain_index,
            item.slot,
        ),
    ):
        left = reading.first_dimension
        right = reading.qwen_dimension
        reason = _review_reason(reading)
        field_key: str | None = None
        position = 0
        if single_countertop and reading.state == "corroborated" and left and right:
            if reading.slot == "overall":
                first_form = first.countertops[0]
                second_form = second.countertops[0]
                no_appliance = not any(
                    dimension.kind == "appliance_space"
                    for dimension in (*first_form.chain, *second_form.chain)
                )
                if first_form.overall_scope == second_form.overall_scope == "run" and no_appliance:
                    field_key = "SHOP:countertop_overall_width"
            elif left.kind == right.kind == "cabinet":
                field_key = "SHOP:cabinet_width"
                position = cabinet_position
                cabinet_position += 1
            elif left.kind == right.kind == "filler":
                field_key = "SHOP:filler_width"
                position = filler_position
                filler_position += 1
        if field_key is None:
            if not single_countertop:
                reason = "multiple countertops on this page; review each reading"
            questions.append(FormReviewQuestion(reading=reading, review_reason=reason))
        else:
            proposals.append(
                FormFieldProposal(field_key=field_key, position=position, reading=reading)
            )
    return FormMapping(tuple(proposals), tuple(questions))


def _review_reason(reading: ComparedReading) -> str:
    reasons = {
        "readers-differ": "readers differ",
        "one-reader-missing": "only one reader",
        "combined": "combined label",
        "stacked": "stacked fraction",
        "unreadable": "unreadable dimension text",
        "scope-not-run": "whole-wall overall, not a run width",
        "reader-topology-differs": "readers describe different drawing parts",
        "appliance-span": "overall includes an appliance space",
    }
    if reading.reason in reasons:
        return reasons[reading.reason]
    left, right = reading.first_dimension, reading.qwen_dimension
    if left is not None and right is not None and left.kind != right.kind:
        return "readers differ on the part type"
    if left is not None and left.kind == "cabinets_equal":
        return "pieces don't add up; review the value"
    if left is not None and left.kind == "end_panel":
        return "end panel; review the value"
    if reading.state == "corroborated":
        return "part type is not eligible for automatic form mapping"
    return "review this reading before assigning it"
