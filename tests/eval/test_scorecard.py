"""Scoring a run against an answer key, and the two things that must never happen.

Verification for: `eval/scorecard.py` (#553).

The two to read first are `test_an_abstention_is_never_scored_as_a_wrong_reading` and
`test_a_reading_the_pipeline_did_not_make_is_not_filled_in_from_the_key`. Both guard a way a grader
can quietly become useless: one by making the honest path look like the failing path, the other by
reporting a perfect score for a pipeline that read nothing at all.

Every fixture here is authored — no drawing, no client material, nothing loaded from `data/`.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

import pytest

from eval.gold_set.schema import (
    ExpectedFinding,
    GoldCase,
    GoldObservation,
    GroundTruth,
    Provenance,
    ReviewedDocument,
)
from eval.scorecard import render, score_package
from rules.semantic_types import OperandSource, ProductType, SemanticType
from units.measurement import Measurement, Unit
from verdict.outcomes import Outcome, Severity

DIGEST = "sha256:" + "c" * 64


@dataclass(frozen=True, slots=True)
class _Finding:
    """The three fields every metric reads. See `eval.scorecard.ScoredFinding` for why not a real
    `Finding`: that type refuses to exist without a calculation trace, and inventing one here would
    be fabricating a calculation in a test about not fabricating things."""

    rule_id: str
    outcome: Outcome
    severity: Severity


def _inches(value: Fraction | int, raw: str | None = None) -> Measurement:
    return Measurement(exact=Fraction(value), unit=Unit.INCH, raw_text=raw)


def _observation(
    semantic: SemanticType = SemanticType.CT007,
    value: Fraction | int = 4,
    *,
    item: str = "S_CAB_7",
) -> GoldObservation:
    return GoldObservation(
        semantic_type=semantic,
        source=OperandSource.SHOP,
        value=_inches(value),
        page=1,
        polygon=(10, 20, 60, 40),
        item_id=item,
    )


def _case(
    *,
    observations: tuple[GoldObservation, ...] = (),
    expected: tuple[ExpectedFinding, ...] = (),
) -> GoldCase:
    return GoldCase(
        id="authored-01",
        product_type=ProductType.COUNTERTOP,
        arch="arch.pdf",
        shop="shop.pdf",
        ground_truth=GroundTruth(observations=observations, matches=(), expected_findings=expected),
        provenance=Provenance(
            annotator="test",
            annotated_on=date(2026, 9, 8),
            documents=(
                ReviewedDocument(
                    source=OperandSource.SHOP,
                    document_version_id="11111111-1111-4111-8111-111111111111",
                    content_hash=DIGEST,
                ),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# The two invariants
# ---------------------------------------------------------------------------


def test_an_abstention_is_never_scored_as_a_wrong_reading() -> None:
    """**Input: three abstentions. Outcome: an abstention rate, and no reading marked wrong.**

    `REVIEW_REQUIRED`, `NOT_FOUND` and `NO_APPLICABLE_RULE` mean the system declined to answer, which
    under `AGENTS.md` §2 is the correct behaviour when it cannot be sure. A harness that counted them
    as errors would make the honest path look like the failing path — and the fastest way to improve
    such a score would be to guess more, which is the opposite of what this project is for.
    """
    case = _case(observations=(_observation(),))
    findings = [
        _Finding("CT-A", Outcome.REVIEW_REQUIRED, Severity.CRITICAL),
        _Finding("CT-B", Outcome.NOT_FOUND, Severity.MAJOR),
        _Finding("CT-C", Outcome.NO_APPLICABLE_RULE, Severity.MINOR),
    ]

    scorecard = score_package(case, findings)

    assert scorecard.abstention_rate == Fraction(3, 3)
    assert len(scorecard.abstained) == 3
    # The answer was never read, so it is missing — not wrong.
    assert scorecard.readings[0].missing
    assert not scorecard.readings[0].matched
    assert scorecard.reading_accuracy is None, "an unattempted answer must not count as an error"
    assert "not an error" in render(scorecard)


def test_a_reading_the_pipeline_did_not_make_is_not_filled_in_from_the_key() -> None:
    """**Input: an answer and no reading. Outcome: `nothing read`, and coverage says so.**

    A grader that supplied the answer it was grading against would report a perfect score for a
    pipeline that read nothing at all. This is the assertion that stops that: the answer is present,
    the reading is absent, and the two are reported separately.
    """
    case = _case(observations=(_observation(value=4),))

    scorecard = score_package(case, [], observations=())

    assert scorecard.readings[0].actual is None
    assert scorecard.readings[0].expected == "4 in"
    assert scorecard.coverage == Fraction(0, 1)
    assert "nothing read" in render(scorecard)


# ---------------------------------------------------------------------------
# Reading and verdict scoring
# ---------------------------------------------------------------------------


def test_a_right_reading_counts_and_a_wrong_one_does_not() -> None:
    """Input: two answers, one read correctly and one misread. Outcome: 50%.

    The wrong reading here is `3/4` for `28 3/4` — a dropped whole number, which is the failure shape
    #541 exists for and the one the synthetic fixture reproduces from the real reader.
    """
    case = _case(
        observations=(
            _observation(SemanticType.CT007, 4),
            _observation(SemanticType.CT008, Fraction(115, 4)),
        )
    )
    read = (
        _observation(SemanticType.CT007, 4),
        _observation(SemanticType.CT008, Fraction(3, 4)),
    )

    scorecard = score_package(case, [], observations=read)

    assert scorecard.reading_accuracy == Fraction(1, 2)
    assert scorecard.coverage == Fraction(1, 1)
    assert [reading.matched for reading in scorecard.readings] == [True, False]


def test_the_value_is_graded_and_the_transcription_is_not() -> None:
    """**Input: the right number, a different `raw_text`. Outcome: matched.**

    `Measurement` equality also compares `raw_text`, which is what was *printed* — so an exactly
    correct reading scored as wrong because the annotator wrote `4"` and the reader recorded `4.0"`.
    What is being graded is whether the system got the dimension right, and `(exact, unit)` is the
    dimension. Found by running the harness on the synthetic fixture and watching a correct reading
    fail.
    """
    case = _case(observations=(_observation(value=4),))
    read = (
        GoldObservation(
            semantic_type=SemanticType.CT007,
            source=OperandSource.SHOP,
            value=_inches(4, raw='4.0"'),
            page=1,
            polygon=(10, 20, 60, 40),
            item_id="S_CAB_7",
        ),
    )

    assert score_package(case, [], observations=read).reading_accuracy == Fraction(1, 1)


def test_a_nearly_right_reading_is_wrong() -> None:
    """Input: a sixteenth out. Outcome: not matched, because Q2 settled exact match for V1."""
    case = _case(observations=(_observation(value=4),))
    read = (_observation(value=Fraction(65, 16)),)

    assert score_package(case, [], observations=read).reading_accuracy == Fraction(0, 1)


def test_a_matching_verdict_counts_and_a_differing_one_does_not() -> None:
    """Input: two stated verdicts, one delivered. Outcome: 50%."""
    case = _case(
        expected=(
            ExpectedFinding(check="CT-A", outcome=Outcome.FAIL, reason="too wide"),
            ExpectedFinding(check="CT-B", outcome=Outcome.PASS, reason="fine"),
        )
    )
    findings = [
        _Finding("CT-A", Outcome.FAIL, Severity.CRITICAL),
        _Finding("CT-B", Outcome.FAIL, Severity.MAJOR),
    ]

    scorecard = score_package(case, findings)

    assert scorecard.verdict_accuracy == Fraction(1, 2)


def test_a_check_the_answer_key_says_nothing_about_is_not_scored() -> None:
    """**Input: a check with no stated expectation. Outcome: noted, not counted either way.**

    An answer key is silent, not negative. Counting an unstated check as a failure would punish a
    partially annotated case; counting it as a pass would invent a reviewer's agreement. So it is
    reported in the notes and left out of the denominator, which is what makes a partial annotation
    useful rather than misleading.
    """
    case = _case(expected=(ExpectedFinding(check="CT-A", outcome=Outcome.PASS, reason="fine"),))
    findings = [
        _Finding("CT-A", Outcome.PASS, Severity.MAJOR),
        _Finding("CT-UNSTATED", Outcome.FAIL, Severity.CRITICAL),
    ]

    scorecard = score_package(case, findings)

    assert scorecard.verdict_accuracy == Fraction(1, 1)
    assert any("says nothing about" in note for note in scorecard.notes)
    assert scorecard.critical_false_passes == ()


# ---------------------------------------------------------------------------
# The ship gate
# ---------------------------------------------------------------------------


def test_a_critical_check_wrongly_passed_is_named_not_just_counted() -> None:
    """**The ship gate.** Input: the reviewer failed a CRITICAL check the system passed.

    Named rather than counted, because the primary safety metric is not a number somebody should have
    to go hunting for the cases behind. The severity comes from the finding — the rulebook's word on
    how serious the rule is — and never from the answer key, which states only what a reviewer
    concluded.
    """
    case = _case(expected=(ExpectedFinding(check="CT-A", outcome=Outcome.FAIL, reason="too wide"),))
    findings = [_Finding("CT-A", Outcome.PASS, Severity.CRITICAL)]

    scorecard = score_package(case, findings)

    assert scorecard.critical_false_passes == ("CT-A",)
    assert "CT-A" in render(scorecard)
    assert scorecard.critical_false_pass_rate.value == Fraction(1, 1)


def test_a_major_check_wrongly_passed_is_not_a_critical_false_pass() -> None:
    """Input: the same mistake on a MAJOR rule. Outcome: not on the ship gate.

    It is still a wrong verdict and still counted in verdict accuracy. The critical rate is
    deliberately narrow: widening it would make the one number that can stop a release mean something
    else.
    """
    case = _case(expected=(ExpectedFinding(check="CT-A", outcome=Outcome.FAIL, reason="too wide"),))
    findings = [_Finding("CT-A", Outcome.PASS, Severity.MAJOR)]

    scorecard = score_package(case, findings)

    assert scorecard.critical_false_passes == ()
    assert scorecard.verdict_accuracy == Fraction(0, 1)


def test_an_unmeasurable_rate_prints_as_unmeasured_rather_than_zero() -> None:
    """**Input: nothing to measure. Outcome: the words, not a flattering number.**

    A zero false-PASS rate nobody measured reads as the best possible result, which is why
    `eval/metrics.py` returns `None` for an empty denominator and this prints it as such.
    """
    rendered = render(score_package(_case(), []))

    assert "not measured" in rendered
    assert "0.0%" not in rendered.split("readings")[0]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_answer_key_stating_one_type_twice_is_refused() -> None:
    """Input: two answers for the same quantity. Outcome: `ValueError`.

    Not a scoring edge case: readings are paired on the reviewer's semantic type, so a repeated type
    leaves the score depending on which of the two answers happened to be compared.
    """
    case = _case(
        observations=(
            _observation(SemanticType.CT007, 4),
            _observation(SemanticType.CT007, 5),
        )
    )

    with pytest.raises(ValueError, match="two answers"):
        score_package(case, [])


def test_the_scorer_writes_nothing() -> None:
    """**Outcome: the module imports no database and no storage, so it has no way to write.**

    It is an offline tool. A measurement that could steer the thing it measures is not a
    measurement, and the cheapest way to keep that true is for the scorer to have no reach into the
    live path at all.

    Asserted on the imports rather than by grepping the source for `session`, which is the mistake
    `tests/eval/test_promotion.py:test_nothing_here_types_a_reading` documents: a text guard that a
    docstring can trip gets deleted the first time it cries wolf. An import guard cannot.
    """
    import ast

    import eval.scorecard as module

    source = module.__file__
    assert source is not None
    tree = ast.parse(pathlib.Path(source).read_text(encoding="utf-8"))
    imported = {
        alias.name if isinstance(node, ast.Import) else (node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in getattr(node, "names", [])
    }

    forbidden = sorted(
        name
        for name in imported
        if name.split(".")[0] in {"app", "sqlalchemy", "storage", "alembic", "boto3", "requests"}
    )
    assert not forbidden, f"the scorer can reach the live path through {forbidden}"
