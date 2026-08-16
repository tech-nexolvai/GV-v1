"""Evidence localisation as a measurable release gate (#172).

`AGENTS.md` §9 requires a finding's page and polygon to meet a threshold. The gate existed in prose
and nothing measured it.

Four properties matter:

- **page and box are judged separately** — they fail for completely different reasons, and one
  number hides which
- **both must clear the threshold** to pass — a right page with a wrong box sends a reviewer to the
  wrong part of the right page, which is no better than the wrong page
- **an unannotated prediction raises** — the cases nobody annotated are the ones most likely to be
  wrong, so omitting them inflates the score
- **the arithmetic is exact** — a release decision is made from it
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from eval.gold_set.schema import GoldObservation
from eval.localisation import (
    ALL_CHECKS,
    DEFAULT_IOU_THRESHOLD,
    Box,
    DegenerateBox,
    LocalisationResult,
    MissingAnnotation,
    area,
    intersection_over_union,
    measure,
    report,
)
from rules.semantic_types import OperandSource, SemanticType
from units.measurement import Measurement, Unit

HALF = Fraction(1, 2)


def _obs(
    item: str = "cab-1",
    *,
    page: int = 1,
    box: Box = (0, 0, 10, 10),
    semantic: SemanticType = SemanticType.CT001,
) -> GoldObservation:
    return GoldObservation(
        semantic_type=semantic,
        source=OperandSource.SHOP,
        value=Measurement(exact=Fraction(984), unit=Unit.MM, raw_text="984"),
        page=page,
        polygon=box,
        item_id=item,
    )


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def test_identical_boxes_overlap_completely() -> None:
    assert intersection_over_union((0, 0, 10, 10), (0, 0, 10, 10)) == 1


def test_disjoint_boxes_do_not_overlap() -> None:
    assert intersection_over_union((0, 0, 10, 10), (50, 50, 60, 60)) == 0


def test_a_half_shifted_box_scores_one_third() -> None:
    """Hand-computed: overlap 50, union 150. The classic IoU result for a 50% shift, and a useful
    reminder that IoU is harsher than it feels."""
    assert intersection_over_union((0, 0, 10, 10), (5, 0, 15, 10)) == Fraction(1, 3)


def test_the_result_is_an_exact_fraction() -> None:
    """A rounded IoU would put a float behind a release gate, and two runs that localised
    identically could then compare unequal."""
    value = intersection_over_union((0, 0, 3, 3), (1, 1, 4, 4))
    assert isinstance(value, Fraction)
    assert value == Fraction(4, 14)


def test_a_zero_area_box_is_an_error_not_a_miss() -> None:
    """A zero-area annotation is a bad annotation, not a failed prediction. Scoring it 0 would
    blame the extractor for the annotator's slip."""
    with pytest.raises(DegenerateBox, match="no area"):
        intersection_over_union((0, 0, 10, 10), (5, 5, 5, 5))


def test_area_of_an_inverted_box_is_zero_not_negative() -> None:
    assert area((10, 10, 0, 0)) == 0


# ---------------------------------------------------------------------------
# Page and box are judged separately
# ---------------------------------------------------------------------------


def test_a_right_page_with_a_wrong_box_does_not_pass() -> None:
    """The property the whole metric exists for. A reviewer sent to the wrong part of the right
    page still cannot verify the finding at a glance."""
    result = measure(
        [_obs(page=2, box=(0, 0, 10, 10))],
        [_obs(page=2, box=(500, 500, 510, 510))],
        threshold=HALF,
    )[ALL_CHECKS]
    assert result.page_accuracy == 1
    assert result.box_accuracy == 0
    assert result.passed is False


def test_a_right_box_on_the_wrong_page_does_not_pass() -> None:
    result = measure(
        [_obs(page=1, box=(0, 0, 10, 10))],
        [_obs(page=7, box=(0, 0, 10, 10))],
        threshold=HALF,
    )[ALL_CHECKS]
    assert result.page_accuracy == 0
    assert result.box_accuracy == 1
    assert result.passed is False


def test_both_correct_passes() -> None:
    result = measure([_obs()], [_obs()], threshold=HALF)[ALL_CHECKS]
    assert result.passed is True
    assert result.mean_iou == 1


def test_the_two_are_reported_separately_not_as_one_number() -> None:
    """One number hides which half is broken, and a wrong page and a wrong box have completely
    different causes — classification versus geometry."""
    result = measure(
        [_obs(page=1, box=(0, 0, 10, 10))],
        [_obs(page=9, box=(0, 0, 10, 10))],
        threshold=HALF,
    )[ALL_CHECKS]
    assert result.page_accuracy != result.box_accuracy


# ---------------------------------------------------------------------------
# The threshold
# ---------------------------------------------------------------------------


def test_the_threshold_is_required_not_defaulted() -> None:
    """A default would be a magic number deciding a release gate."""
    with pytest.raises(TypeError):
        measure([_obs()], [_obs()])  # type: ignore[call-arg]


def test_a_box_just_under_the_threshold_fails() -> None:
    """IoU 1/3 against a 1/2 threshold."""
    result = measure([_obs(box=(0, 0, 10, 10))], [_obs(box=(5, 0, 15, 10))], threshold=HALF)[
        ALL_CHECKS
    ]
    assert result.mean_iou == Fraction(1, 3)
    assert result.box_accuracy == 0


def test_a_lower_threshold_accepts_the_same_box() -> None:
    """The threshold is the decision, not the geometry — the same overlap passes or fails depending
    on what somebody declared."""
    result = measure(
        [_obs(box=(0, 0, 10, 10))], [_obs(box=(5, 0, 15, 10))], threshold=Fraction(1, 4)
    )[ALL_CHECKS]
    assert result.box_accuracy == 1


def test_the_default_threshold_is_a_named_constant() -> None:
    """Greppable and arguable, rather than buried in a call."""
    assert DEFAULT_IOU_THRESHOLD == Fraction(1, 2)


# ---------------------------------------------------------------------------
# Unannotated predictions
# ---------------------------------------------------------------------------


def test_an_unannotated_prediction_raises_rather_than_being_skipped() -> None:
    """Scoring only what happens to have an annotation inflates the result: the unannotated cases
    are the ones most likely to be wrong."""
    with pytest.raises(MissingAnnotation, match="no annotation to compare against"):
        measure([_obs("cab-1"), _obs("cab-999")], [_obs("cab-1")], threshold=HALF)


def test_the_error_names_the_offending_cases() -> None:
    with pytest.raises(MissingAnnotation) as err:
        measure([_obs("cab-999")], [_obs("cab-1")], threshold=HALF)
    assert "cab-999" in str(err.value)


def test_nothing_compared_is_not_measured_rather_than_zero() -> None:
    """Zero reads as total failure; the truth is that nobody measured it."""
    result = measure([], [], threshold=HALF)[ALL_CHECKS]
    assert not result.measured
    assert result.passed is None
    assert result.page_accuracy is None


# ---------------------------------------------------------------------------
# Per check type
# ---------------------------------------------------------------------------


def test_results_are_grouped_by_check_type_when_a_grouping_is_supplied() -> None:
    """The gate is per check type, so the report is too."""
    predicted = [_obs("a", semantic=SemanticType.CT001), _obs("b", semantic=SemanticType.CT010)]
    annotated = [_obs("a", semantic=SemanticType.CT001), _obs("b", semantic=SemanticType.CT010)]
    results = measure(
        predicted,
        annotated,
        threshold=HALF,
        check_type_of=lambda obs: "width" if obs.semantic_type is SemanticType.CT001 else "depth",
    )
    assert set(results) == {"width", "depth"}


def test_without_a_grouping_everything_falls_under_a_named_bucket() -> None:
    """Named rather than blank, so a report never implies a breakdown it does not have. A
    GoldObservation carries a semantic type and not a check type, so the mapping has to come from
    the caller rather than be invented here."""
    assert set(measure([_obs()], [_obs()], threshold=HALF)) == {ALL_CHECKS}


# ---------------------------------------------------------------------------
# The report, and the metric that consumes this
# ---------------------------------------------------------------------------


def test_the_report_shows_page_and_box_and_the_threshold() -> None:
    text = report(measure([_obs()], [_obs()], threshold=HALF))
    assert "page" in text and "box" in text and "IoU" in text


def test_the_report_says_when_a_check_type_was_not_measured() -> None:
    text = report({ALL_CHECKS: LocalisationResult(ALL_CHECKS, 0, 0, 0, None, HALF)})
    assert "NOT MEASURED" in text
    assert "An unmeasured gate is not a passed one" in text


def test_the_headline_metric_delegates_here() -> None:
    """`eval/metrics.py` originally required exact polygon equality — unusably strict, since no
    extraction lane reproduces a hand-drawn box to the pixel. It would have read zero forever."""
    from eval.metrics import evidence_localisation_rate

    near_miss = measure([_obs(box=(0, 0, 10, 10))], [_obs(box=(1, 1, 11, 11))], threshold=HALF)
    assert near_miss[ALL_CHECKS].box_accuracy == 1

    result = evidence_localisation_rate([_obs(box=(0, 0, 10, 10))], [_obs(box=(1, 1, 11, 11))])
    assert result.value == 1, "a near miss should score, not read as a total failure"


def test_the_headline_metric_reports_not_measured_on_a_missing_annotation() -> None:
    """The gate is unmeasurable, not failed, and those are different facts."""
    from eval.metrics import evidence_localisation_rate

    result = evidence_localisation_rate([_obs("cab-999")], [_obs("cab-1")])
    assert result.value is None
    assert "no annotation" in result.note
