"""Synthetic funnel accounting only; no drawing material belongs in the public tests."""

from eval.gold_set.schema import GroundedCountertop
from eval.grounding_scorecard import (
    GroundingAttempt,
    PredictedLabel,
    StageResult,
    score_grounding,
)


def _target(status: str = "present") -> GroundedCountertop:
    data: dict[str, object] = {
        "target_id": "target-a",
        "page": 1,
        "status": status,
    }
    if status == "unknown":
        data["unscored_reason"] = "not independently reviewed"
    elif status == "present":
        data.update(
            {
                "object_id": "stone-a",
                "view_id": "front",
                "source_ink": "vendor",
                "stone_box": {"x0": 1, "y0": 1, "x1": 99, "y1": 20},
                "stone_ends": [[1, 10], [99, 10]],
                "acceptable_piece_rows": [
                    {
                        "row_id": "piece-row",
                        "view_id": "front",
                        "role": "pieces",
                        "box": {"x0": 1, "y0": 22, "x1": 99, "y1": 40},
                        "endpoints": [[1, 30], [99, 30]],
                        "labels": [
                            {
                                "span_id": "span-0",
                                "status": "linked",
                                "label_id": "label-0",
                                "ink": "vendor",
                                "text": '12 1/2"',
                                "box": {"x0": 40, "y0": 23, "x1": 55, "y1": 29},
                            }
                        ],
                    }
                ],
            }
        )
    return GroundedCountertop.model_validate(data)


def _attempt(**changes: object) -> GroundingAttempt:
    data: dict[str, object] = {
        "target_id": "target-a",
        "page": 1,
        "candidate_count": 2,
        "predicted_object_id": "stone-a",
        "candidate_row_ids": ("piece-row", "other-row"),
        "selected_row_id": "piece-row",
        "labels": (
            PredictedLabel(
                span_id="span-0",
                label_id="label-0",
                text='12 1/2"',
                ink="vendor",
                sealed=True,
            ),
        ),
    }
    data.update(changes)
    return GroundingAttempt.model_validate(data)


def test_funnel_scores_object_row_association_and_exact_value_separately() -> None:
    report = score_grounding((_target(),), (_attempt(),))

    assert report.object[StageResult.CORRECT] == 1
    assert report.row[StageResult.CORRECT] == 1
    assert report.association[StageResult.CORRECT] == 1
    assert report.value[StageResult.CORRECT] == 1


def test_wrong_object_stops_downstream_credit() -> None:
    report = score_grounding((_target(),), (_attempt(predicted_object_id="other-stone"),))

    assert report.object[StageResult.WRONG] == 1
    assert report.row[StageResult.HELD] == 1
    assert report.association[StageResult.HELD] == 1
    assert report.value[StageResult.HELD] == 1


def test_wrong_row_is_not_relabelled_as_a_reading_failure() -> None:
    report = score_grounding((_target(),), (_attempt(selected_row_id="other-row"),))

    assert report.row[StageResult.WRONG] == 1
    assert report.association[StageResult.HELD] == 1
    assert report.value[StageResult.HELD] == 1


def test_correct_row_held_for_person_is_coverage_not_wrongness() -> None:
    report = score_grounding(
        (_target(),),
        (_attempt(selected_row_id=None, labels=()),),
    )

    assert report.row[StageResult.HELD] == 1
    assert report.association[StageResult.HELD] == 1
    assert report.value[StageResult.HELD] == 1


def test_wrong_label_owner_and_wrong_text_are_separately_visible() -> None:
    wrong_owner = score_grounding(
        (_target(),),
        (
            _attempt(
                labels=(
                    PredictedLabel(
                        span_id="span-0",
                        label_id="neighbor",
                        text='12 1/2"',
                        ink="vendor",
                        sealed=True,
                    ),
                )
            ),
        ),
    )
    wrong_text = score_grounding(
        (_target(),),
        (
            _attempt(
                labels=(
                    PredictedLabel(
                        span_id="span-0",
                        label_id="label-0",
                        text='13 1/2"',
                        ink="vendor",
                        sealed=True,
                    ),
                )
            ),
        ),
    )

    assert wrong_owner.association[StageResult.WRONG] == 1
    assert wrong_text.association[StageResult.CORRECT] == 1
    assert wrong_text.value[StageResult.WRONG] == 1


def test_missing_candidate_and_unknown_truth_are_not_scored_as_correct() -> None:
    no_candidate = score_grounding(
        (_target(),),
        (
            _attempt(
                candidate_count=0,
                predicted_object_id=None,
                candidate_row_ids=(),
                selected_row_id=None,
                labels=(),
            ),
        ),
    )
    unknown = score_grounding((_target("unknown"),), ())

    assert no_candidate.value[StageResult.NO_CANDIDATE] == 1
    assert unknown.value[StageResult.UNMEASURED] == 1


def test_absent_object_is_a_scored_negative() -> None:
    absent = GroundedCountertop.model_validate(
        {"target_id": "target-a", "page": 1, "status": "absent"}
    )
    report = score_grounding((absent,), (_attempt(predicted_object_id=None, selected_row_id=None),))

    assert report.object[StageResult.CORRECT] == 1
