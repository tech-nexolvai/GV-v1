"""Independent, stage-by-stage scoring for the object-to-dimension reading funnel.

This module accepts predictions as data and compares them only with person-reviewed gold
truth. It is evaluation-only: it does not alter proposal eligibility, persist product values,
or call the verdict engine.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eval.gold_set.schema import (
    GroundedCountertop,
    GroundedLabel,
    GroundedRow,
    GroundingStatus,
)


class StageResult(StrEnum):
    """Mutually exclusive outcomes for one scored stage."""

    CORRECT = "correct"
    WRONG = "wrong"
    HELD = "held"
    NO_CANDIDATE = "no_candidate"
    UNMEASURED = "unmeasured"


class PredictedLabel(BaseModel):
    """A product-side proposed ownership/read for one known drawing span."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: str = Field(min_length=1)
    label_id: str | None = None
    text: str | None = None
    ink: str | None = None
    sealed: bool = False

    @model_validator(mode="after")
    def _sealed_prediction_is_complete(self) -> PredictedLabel:
        if self.sealed and (self.label_id is None or self.text is None):
            raise ValueError("a sealed prediction needs a label identity and printed text")
        return self


class GroundingAttempt(BaseModel):
    """One run's non-authoritative prediction for a single reviewed target."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(min_length=1)
    page: int = Field(ge=1)
    candidate_count: int = Field(ge=0)
    predicted_object_id: str | None = None
    candidate_row_ids: tuple[str, ...] = ()
    selected_row_id: str | None = None
    labels: tuple[PredictedLabel, ...] = ()

    @model_validator(mode="after")
    def _selected_row_was_a_candidate(self) -> GroundingAttempt:
        if self.selected_row_id is not None and self.selected_row_id not in self.candidate_row_ids:
            raise ValueError("selected row must be among the enumerated candidates")
        if len(self.candidate_row_ids) != len(set(self.candidate_row_ids)):
            raise ValueError("candidate row ids must be unique")
        if self.candidate_count < len(self.candidate_row_ids):
            raise ValueError("candidate_count cannot be smaller than the enumerated row ids")
        spans = [label.span_id for label in self.labels]
        if len(spans) != len(set(spans)):
            raise ValueError("one attempt may report only one prediction per span")
        return self


@dataclass(frozen=True, slots=True)
class GroundingScore:
    """Counts remain separate so coverage cannot conceal a safety error."""

    targets: int
    object: Counter[StageResult]
    row: Counter[StageResult]
    association: Counter[StageResult]
    value: Counter[StageResult]


def _normalise_printed_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("″", '"').replace("′", "'").replace("’", "'")
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def _rows(target: GroundedCountertop) -> tuple[GroundedRow, ...]:
    return (*target.acceptable_piece_rows, *target.acceptable_overall_rows)


def _truth_labels(target: GroundedCountertop) -> dict[str, GroundedLabel]:
    return {label.span_id: label for row in _rows(target) for label in row.labels}


def _correct_row(target: GroundedCountertop, row_id: str | None) -> bool:
    return row_id is not None and any(row.row_id == row_id for row in _rows(target))


def score_grounding(
    targets: tuple[GroundedCountertop, ...], attempts: tuple[GroundingAttempt, ...]
) -> GroundingScore:
    """Score each funnel stage without treating missing truth as a success or a negative."""

    attempts_by_key = {(attempt.page, attempt.target_id): attempt for attempt in attempts}
    if len(attempts_by_key) != len(attempts):
        raise ValueError("there must be at most one attempt per (page, target_id)")
    truth_keys = {(target.page, target.target_id) for target in targets}
    unexpected = set(attempts_by_key) - truth_keys
    if unexpected:
        raise ValueError(f"attempts have no corresponding reviewed target: {sorted(unexpected)}")
    object_counts: Counter[StageResult] = Counter()
    row_counts: Counter[StageResult] = Counter()
    association_counts: Counter[StageResult] = Counter()
    value_counts: Counter[StageResult] = Counter()

    for target in targets:
        attempt = attempts_by_key.get((target.page, target.target_id))
        if target.status is GroundingStatus.UNKNOWN:
            for counts in (object_counts, row_counts, association_counts, value_counts):
                counts[StageResult.UNMEASURED] += 1
            continue
        if attempt is None or (
            attempt.candidate_count == 0 and attempt.predicted_object_id is None
        ):
            missing = StageResult.NO_CANDIDATE
            object_counts[missing] += 1
            row_counts[missing] += 1
            association_counts[missing] += 1
            value_counts[missing] += 1
            continue

        if target.status is GroundingStatus.ABSENT:
            object_result = (
                StageResult.CORRECT if attempt.predicted_object_id is None else StageResult.WRONG
            )
            object_counts[object_result] += 1
            row_counts[
                StageResult.HELD if attempt.selected_row_id is None else StageResult.WRONG
            ] += 1
            association_counts[StageResult.HELD] += 1
            value_counts[StageResult.HELD] += 1
            continue

        object_ok = attempt.predicted_object_id == target.object_id
        object_counts[StageResult.CORRECT if object_ok else StageResult.WRONG] += 1
        if not object_ok:
            row_counts[StageResult.HELD] += 1
            association_counts[StageResult.HELD] += 1
            value_counts[StageResult.HELD] += 1
            continue

        if attempt.selected_row_id is None:
            row_counts[
                (
                    StageResult.HELD
                    if any(row.row_id in attempt.candidate_row_ids for row in _rows(target))
                    else StageResult.NO_CANDIDATE
                )
            ] += 1
            association_counts[StageResult.HELD] += 1
            value_counts[StageResult.HELD] += 1
            continue

        row_ok = _correct_row(target, attempt.selected_row_id)
        row_counts[StageResult.CORRECT if row_ok else StageResult.WRONG] += 1
        if not row_ok:
            association_counts[StageResult.HELD] += 1
            value_counts[StageResult.HELD] += 1
            continue

        truth_by_span = _truth_labels(target)
        prediction_by_span = {label.span_id: label for label in attempt.labels}
        relevant_spans = {
            label.span_id
            for row in _rows(target)
            if row.row_id == attempt.selected_row_id
            for label in row.labels
        }
        wrong_owner = False
        held_link = False
        wrong_value = False
        sealed_count = 0
        for span_id in relevant_spans:
            truth = truth_by_span[span_id]
            prediction = prediction_by_span.get(span_id)
            if truth.status != "linked":
                if prediction is not None and prediction.sealed:
                    wrong_owner = True
                else:
                    held_link = True
                continue
            if prediction is None or prediction.label_id is None:
                held_link = True
                continue
            if prediction.label_id != truth.label_id or prediction.ink != truth.ink.value:
                wrong_owner = True
                continue
            if prediction.sealed:
                sealed_count += 1
                if prediction.text is None or _normalise_printed_text(
                    prediction.text
                ) != _normalise_printed_text(truth.text or ""):
                    wrong_value = True
            else:
                held_link = True

        if wrong_owner:
            association_counts[StageResult.WRONG] += 1
        elif held_link:
            association_counts[StageResult.HELD] += 1
        else:
            association_counts[StageResult.CORRECT] += 1
        if wrong_value:
            value_counts[StageResult.WRONG] += 1
        elif held_link or sealed_count == 0:
            value_counts[StageResult.HELD] += 1
        else:
            value_counts[StageResult.CORRECT] += 1

    return GroundingScore(len(targets), object_counts, row_counts, association_counts, value_counts)
