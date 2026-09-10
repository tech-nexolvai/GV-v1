"""Honest metrics for the semantic-typing gate.

Only human-confirmed labels belong in this denominator.  A case whose type is itself marked
heuristic-unconfirmed cannot prove an automatic typer correct; scoring it would grade a guess against
the same class of guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from vocabulary.semantic_types import SemanticType

__all__ = ["SemanticTypingScore", "TypingLabel", "TypingPrediction", "score_typing"]


@dataclass(frozen=True, slots=True)
class TypingLabel:
    case_id: str
    semantic_type: SemanticType


@dataclass(frozen=True, slots=True)
class TypingPrediction:
    case_id: str
    semantic_type: SemanticType | None


@dataclass(frozen=True, slots=True)
class SemanticTypingScore:
    eligible: int
    predicted: int
    correct: int
    abstained: int

    @property
    def coverage(self) -> float:
        return 0.0 if self.eligible == 0 else self.predicted / self.eligible

    @property
    def accuracy_when_typed(self) -> float | None:
        return None if self.predicted == 0 else self.correct / self.predicted


def score_typing(
    labels: tuple[TypingLabel, ...], predictions: tuple[TypingPrediction, ...]
) -> SemanticTypingScore:
    """Score exact labels; missing/unknown predictions are abstentions, never false positives."""

    by_case = {label.case_id: label for label in labels}
    if len(by_case) != len(labels):
        raise ValueError("human-confirmed typing labels must have unique case ids")
    predicted_by_case = {prediction.case_id: prediction for prediction in predictions}
    if len(predicted_by_case) != len(predictions):
        raise ValueError("typing predictions must have unique case ids")
    unknown = sorted(set(predicted_by_case) - set(by_case))
    if unknown:
        raise ValueError(f"typing predictions have no human-confirmed label: {', '.join(unknown)}")

    predicted = 0
    correct = 0
    for case_id, label in by_case.items():
        prediction = predicted_by_case.get(case_id)
        if prediction is None or prediction.semantic_type is None:
            continue
        predicted += 1
        correct += prediction.semantic_type is label.semantic_type
    return SemanticTypingScore(
        eligible=len(labels),
        predicted=predicted,
        correct=correct,
        abstained=len(labels) - predicted,
    )
