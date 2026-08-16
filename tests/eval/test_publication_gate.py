"""The gold-set regression that must pass before publication (#238).

`AGENTS.md` §9 requires *"human approval + full gold-set regression"*. `D6.3` enforced the approval
and left the regression as a required argument with no default — a hole that could not be filled by
forgetting. This is what fills it.

It lives in `eval/` rather than `rules/governance/` as #238's plan said: the gate needs a database
session, and putting it in `rules/` let `verdict/` transitively reach SQLAlchemy. The isolation guard
caught that on the first run.

The tests are mostly refusals, because a gate that runs is easy and a gate that cannot be talked
around is the point. Two of them matter more than the rest:

- an **empty gold set** is refused, not passed — a check with nothing to check against reports a
  green gate for an unmeasured change, and that is worse than no check because it is believed
- a run compared against **itself** is refused — it would always pass, and look exactly like a
  genuine clean result
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import pytest

from eval.gold_set.schema import (
    ExpectedFinding,
    GoldCase,
    GoldManifest,
    GroundTruth,
    Provenance,
)
from eval.metrics import MetricResult as ComputedMetric
from eval.publication_gate import (
    NoGoldSet,
    RegressionUnavailable,
    as_regression_check,
    gate,
)
from rules.governance.proposal import propose
from rules.schema import CheckType, GlobalApplicability, InputSelector, OperationRef, Rule
from rules.semantic_types import OperandSource, ProductType, SemanticType
from units.measurement import Unit
from verdict.operations.aggregate import AGGREGATE_SPECS
from verdict.operations.scalar import SCALAR_SPECS
from verdict.outcomes import Outcome, Severity
from verdict.registry import REGISTRY, register

PRIMARY = "critical_false_pass_rate"


@pytest.fixture(autouse=True)
def _registry() -> None:
    for spec in (*SCALAR_SPECS, *AGGREGATE_SPECS):
        if spec.name not in REGISTRY:
            register(spec)


def _rule(rule_id: str = "CT-WIDTH-001", version: str = "1.0.0") -> Rule:
    return Rule(
        id=rule_id,
        version=version,
        product_type=ProductType.COUNTERTOP,
        check_type=CheckType.INTERNAL,
        severity=Severity.CRITICAL,
        arithmetic_unit=Unit.MM,
        inputs={
            "width": InputSelector(source=OperandSource.SHOP, semantic_type=SemanticType.CT001)
        },
        applicability=GlobalApplicability(scope="global"),
        operation=OperationRef(type="exists", operands={"value": "width"}),
    )


def _proposal(rule: Rule | None = None):  # type: ignore[no-untyped-def]
    return propose(rule or _rule(), author="keyur", rationale="tighten the width check")


def _case(case_id: str = "GC-001") -> GoldCase:
    return GoldCase(
        id=case_id,
        product_type=ProductType.COUNTERTOP,
        arch=Path("data/drawings/a.pdf"),
        shop=Path("data/drawings/s.pdf"),
        ground_truth=GroundTruth(
            observations=(),
            matches=(),
            expected_findings=(
                ExpectedFinding(
                    check="CT-WIDTH-001",
                    outcome=Outcome.FAIL,
                    reason="width is 3mm over the run beneath it",
                ),
            ),
        ),
        provenance=Provenance(
            annotator="anant",
            annotated_on=date(2026, 8, 16),
            document_version_id=uuid4(),
            content_hash="a" * 64,
        ),
    )


def _manifest(cases: int = 1) -> GoldManifest:
    return GoldManifest(version=1, cases=tuple(_case(f"GC-{n:03d}") for n in range(cases)))


def _metrics(false_pass: str) -> dict[str, ComputedMetric]:
    frac = Fraction(false_pass)
    return {PRIMARY: ComputedMetric(PRIMARY, frac, frac.numerator, frac.denominator)}


def _scorer(false_pass: str):  # type: ignore[no-untyped-def]
    def score(snapshot, manifest):  # type: ignore[no-untyped-def]
        return _metrics(false_pass)

    return score


# ---------------------------------------------------------------------------
# The refusals that do not need a database
# ---------------------------------------------------------------------------


def test_an_empty_gold_set_is_refused_not_passed() -> None:
    """The state the project is actually in today: `eval/gold_set/cases/` is empty. Refusing every
    publication until real cases exist is the correct behaviour, not a limitation."""
    with pytest.raises(NoGoldSet, match="nothing to regress against"):
        gate(
            _proposal(),
            session=None,  # type: ignore[arg-type]
            gold_set=None,  # type: ignore[arg-type]
            manifest=_manifest(cases=0),
            scorer=_scorer("0"),
            code_version="abc",
            extractor_versions={},
        )


def test_the_refusal_names_what_would_unblock_it() -> None:
    """A bare refusal invites someone to work around the gate. Naming #274 and #188 points at the
    actual constraint."""
    with pytest.raises(NoGoldSet) as err:
        gate(
            _proposal(),
            session=None,  # type: ignore[arg-type]
            gold_set=None,  # type: ignore[arg-type]
            manifest=_manifest(cases=0),
            scorer=_scorer("0"),
            code_version="abc",
            extractor_versions={},
        )
    assert "#274" in str(err.value) and "#188" in str(err.value)


def test_an_invalid_proposal_is_not_scored() -> None:
    """Measuring a rule that cannot run wastes a gold-set pass and reports a number about nothing.
    `RegressionUnavailable`, not a failed regression — the rule may be fine once it validates."""
    bad = propose(
        Rule(
            id="CT-X",
            version="1.0.0",
            product_type=ProductType.COUNTERTOP,
            check_type=CheckType.INTERNAL,
            severity=Severity.CRITICAL,
            arithmetic_unit=Unit.MM,
            inputs={
                "w": InputSelector(source=OperandSource.SHOP, semantic_type=SemanticType.CT001)
            },
            applicability=GlobalApplicability(scope="global"),
            operation=OperationRef(type="not_registered", operands={"value": "w"}),
        ),
        author="keyur",
        rationale="test",
    )
    with pytest.raises(RegressionUnavailable, match="did not validate"):
        gate(
            bad,
            session=None,  # type: ignore[arg-type]
            gold_set=None,  # type: ignore[arg-type]
            manifest=_manifest(),
            scorer=_scorer("0"),
            code_version="abc",
            extractor_versions={},
        )


def test_unavailable_is_a_different_failure_from_regressed() -> None:
    """One means the change is unsafe, the other means nobody knows. Publication is refused either
    way, but the remediation is completely different."""
    assert not issubclass(RegressionUnavailable, NoGoldSet)
    assert RegressionUnavailable.__doc__ is not None
    assert "not the rule's fault" in RegressionUnavailable.__doc__


def test_gate_has_no_override_parameter() -> None:
    """An escape hatch would be used exactly once, under deadline, on the change that most needed
    the check."""
    import inspect

    names = set(inspect.signature(gate).parameters)
    assert not {"force", "override", "skip_regression", "allow_regression"} & names


# ---------------------------------------------------------------------------
# The seam publish() consumes
# ---------------------------------------------------------------------------


def test_the_adapter_matches_what_publish_expects() -> None:
    """`publish()` takes a bare Callable[[RuleProposal], RegressionOutcome] and knows nothing about
    gold sets or sessions. The adapter closes over them rather than widening that signature."""
    import inspect

    from rules.governance.publish import publish

    check = as_regression_check(
        session=None,  # type: ignore[arg-type]
        gold_set=None,  # type: ignore[arg-type]
        manifest=_manifest(cases=0),
        scorer=_scorer("0"),
        code_version="abc",
        extractor_versions={},
    )
    assert callable(check)
    assert len(inspect.signature(check).parameters) == 1
    assert "regression" in inspect.signature(publish).parameters


def test_the_adapter_propagates_a_refusal_rather_than_reporting_a_pass() -> None:
    """If it swallowed NoGoldSet and returned passed=False, publication would report a regression
    failure for a rule nobody measured — the wrong diagnosis."""
    check = as_regression_check(
        session=None,  # type: ignore[arg-type]
        gold_set=None,  # type: ignore[arg-type]
        manifest=_manifest(cases=0),
        scorer=_scorer("0"),
        code_version="abc",
        extractor_versions={},
    )
    with pytest.raises(NoGoldSet):
        check(_proposal())


# ---------------------------------------------------------------------------
# With a database
# ---------------------------------------------------------------------------


@pytest.fixture
def session(postgres_engine):  # type: ignore[no-untyped-def]
    from app.db.base import Base
    from app.db.session import session_factory

    Base.metadata.create_all(postgres_engine)
    with session_factory(postgres_engine)() as db:
        yield db


def _gold_set(session, version: str = "1.0"):  # type: ignore[no-untyped-def]
    from app.models.evaluation import GoldSet

    gold_set = GoldSet(name="countertops", version=version)
    session.add(gold_set)
    session.flush()
    return gold_set


def _baseline_run(session, gold_set, false_pass: str):  # type: ignore[no-untyped-def]
    from eval.runs import record_run

    run = record_run(
        session,
        gold_set=gold_set,
        code_version="baseline-code",
        rule_snapshot_ids=["snap-baseline"],
        extractor_versions={"pdfplumber": "0.11.0"},
        results=_metrics(false_pass),
        is_baseline=True,
    )
    session.flush()
    return run


def test_a_worse_false_pass_rate_blocks_publication(session) -> None:  # type: ignore[no-untyped-def]
    gold_set = _gold_set(session)
    _baseline_run(session, gold_set, "1/100")

    verdict = gate(
        _proposal(),
        session=session,
        gold_set=gold_set,
        manifest=_manifest(),
        scorer=_scorer("5/100"),
        code_version="new-code",
        extractor_versions={"pdfplumber": "0.11.0"},
    )
    assert not verdict.passed
    assert PRIMARY in verdict.summary
    assert verdict.report is not None and verdict.report.critical_false_pass_regressed


def test_an_improved_rate_permits_publication(session) -> None:  # type: ignore[no-untyped-def]
    gold_set = _gold_set(session)
    _baseline_run(session, gold_set, "5/100")

    verdict = gate(
        _proposal(),
        session=session,
        gold_set=gold_set,
        manifest=_manifest(),
        scorer=_scorer("1/100"),
        code_version="new-code",
        extractor_versions={"pdfplumber": "0.11.0"},
    )
    assert verdict.passed


def test_publication_is_refused_when_no_baseline_exists(session) -> None:  # type: ignore[no-untyped-def]
    """A first-ever run has nothing to regress against. Passing it would approve every change made
    before somebody remembers to designate a baseline."""
    gold_set = _gold_set(session)
    with pytest.raises(RegressionUnavailable, match="no baseline run exists"):
        gate(
            _proposal(),
            session=session,
            gold_set=gold_set,
            manifest=_manifest(),
            scorer=_scorer("1/100"),
            code_version="new-code",
            extractor_versions={},
        )


def test_the_comparison_is_against_a_stored_run_not_a_fresh_one(session) -> None:  # type: ignore[no-untyped-def]
    """Scoring twice with the same code proves only that the code is deterministic. The baseline
    has to be a run recorded earlier, under versions that may differ."""
    gold_set = _gold_set(session)
    baseline = _baseline_run(session, gold_set, "1/100")

    verdict = gate(
        _proposal(),
        session=session,
        gold_set=gold_set,
        manifest=_manifest(),
        scorer=_scorer("1/100"),
        code_version="new-code",
        extractor_versions={},
    )
    assert verdict.report is not None
    assert verdict.report.baseline_run_id == str(baseline.id)
    assert verdict.report.candidate_run_id != str(baseline.id)


def test_the_run_is_recorded_even_when_it_regresses(session) -> None:  # type: ignore[no-untyped-def]
    """A blocked publication is still evidence. Discarding the run would lose the record of what was
    attempted and why it was stopped."""
    from app.models.evaluation import EvaluationRun

    gold_set = _gold_set(session)
    _baseline_run(session, gold_set, "1/100")
    before = session.query(EvaluationRun).count()

    verdict = gate(
        _proposal(),
        session=session,
        gold_set=gold_set,
        manifest=_manifest(),
        scorer=_scorer("9/100"),
        code_version="new-code",
        extractor_versions={},
    )
    assert not verdict.passed
    assert session.query(EvaluationRun).count() == before + 1
    assert verdict.run is not None


def test_the_verdict_converts_to_what_publish_consumes(session) -> None:  # type: ignore[no-untyped-def]
    gold_set = _gold_set(session)
    _baseline_run(session, gold_set, "5/100")
    verdict = gate(
        _proposal(),
        session=session,
        gold_set=gold_set,
        manifest=_manifest(),
        scorer=_scorer("1/100"),
        code_version="new-code",
        extractor_versions={},
    )
    outcome = verdict.as_outcome()
    assert outcome.passed
    assert outcome.summary.strip()


def test_nothing_is_recorded_when_there_is_no_baseline(session) -> None:  # type: ignore[no-untyped-def]
    """Found by CodeRabbit on #317.

    `record_run` flushes rather than commits and `unit_of_work` rolls back on an exception, so a run
    recorded and then refused would vanish with the transaction. Worse than a lost row: a first run
    has no baseline, so it would raise, roll back, and leave nothing behind to become one — the gate
    would refuse forever for a reason it had itself created.

    Resolving the baseline first means nothing is scored or written on that path at all.
    """
    from app.models.evaluation import EvaluationRun

    gold_set = _gold_set(session)
    before = session.query(EvaluationRun).count()

    with pytest.raises(RegressionUnavailable, match="no baseline run exists"):
        gate(
            _proposal(),
            session=session,
            gold_set=gold_set,
            manifest=_manifest(),
            scorer=_scorer("1/100"),
            code_version="new-code",
            extractor_versions={},
        )

    assert session.query(EvaluationRun).count() == before


def test_the_scorer_is_not_run_when_there_is_no_baseline(session) -> None:  # type: ignore[no-untyped-def]
    """A gold-set pass is expensive. Paying for one whose result is discarded is waste, and on a
    real gold set it is minutes rather than milliseconds."""
    calls: list[object] = []

    def counting(snapshot, manifest):  # type: ignore[no-untyped-def]
        calls.append(snapshot)
        return _metrics("1/100")

    with pytest.raises(RegressionUnavailable):
        gate(
            _proposal(),
            session=session,
            gold_set=_gold_set(session),
            manifest=_manifest(),
            scorer=counting,
            code_version="new-code",
            extractor_versions={},
        )
    assert calls == []


def test_the_refusal_says_a_baseline_is_a_decision_not_a_side_effect(session) -> None:  # type: ignore[no-untyped-def]
    """Bootstrapping is deliberate: `record_run(..., is_baseline=True)`. Letting a failed
    publication attempt become the reference would make the first mistake the standard."""
    with pytest.raises(RegressionUnavailable) as err:
        gate(
            _proposal(),
            session=session,
            gold_set=_gold_set(session),
            manifest=_manifest(),
            scorer=_scorer("1/100"),
            code_version="new-code",
            extractor_versions={},
        )
    assert "is_baseline=True" in str(err.value)
