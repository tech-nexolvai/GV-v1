"""Only human-labelled types are a valid score denominator."""

from __future__ import annotations

from eval.semantic_typing import TypingLabel, TypingPrediction, score_typing
from vocabulary.semantic_types import SemanticType


def test_typing_score_keeps_abstention_separate_from_wrong_label() -> None:
    score = score_typing(
        (
            TypingLabel("left", SemanticType.FILLER_WIDTH),
            TypingLabel("middle", SemanticType.CABINET_WIDTH),
        ),
        (
            TypingPrediction("left", SemanticType.FILLER_WIDTH),
            TypingPrediction("middle", None),
        ),
    )

    assert score.eligible == 2
    assert score.predicted == 1
    assert score.correct == 1
    assert score.abstained == 1
    assert score.coverage == 0.5
    assert score.accuracy_when_typed == 1.0


def test_empty_prediction_set_has_no_misleading_zero_accuracy() -> None:
    score = score_typing((TypingLabel("left", SemanticType.FILLER_WIDTH),), ())

    assert score.coverage == 0.0
    assert score.accuracy_when_typed is None
