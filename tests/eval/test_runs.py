"""Persisting an evaluation run (#262).

The tests that matter are about **exactness across a round trip**. `eval/metrics.py` computes
metrics as exact `Fraction`s because a release decision is made from them, and the `value` column is
`NUMERIC(18, 9)` — `1/3` does not fit in it.

If a metric were read back from `value`, two runs that computed the identical rate could compare
unequal, and a regression check would report a change that never happened. That is why
`numerator`/`denominator` are the authoritative stored form.

These need a live PostgreSQL and skip without `DATABASE_URL`, so the exactness argument is also
asserted directly against the reconstruction function, which runs everywhere.
"""

from __future__ import annotations

from fractions import Fraction

import pytest
from sqlalchemy.orm import Session

from app.models.document import Document, DocumentKind, DocumentVersion, SourceArtifact
from app.models.evaluation import GoldCase, GoldSet
from app.models.evaluation import MetricResult as MetricRow
from app.models.package import Package, PackageRevision, PackageState, Project
from eval.metrics import MetricResult as ComputedMetric
from eval.runs import VALUE_SCALE, _approximate, load_metric

# ---------------------------------------------------------------------------
# Exactness — no database needed, so these always run
# ---------------------------------------------------------------------------


def _row(numerator: int, denominator: int, note: str = "") -> MetricRow:
    return MetricRow(
        metric="critical_false_pass_rate",
        check_type="all",
        value=_approximate(Fraction(numerator, denominator) if denominator else None),
        numerator=numerator,
        denominator=denominator,
        note=note or None,
    )


def test_a_repeating_fraction_survives_the_round_trip_exactly() -> None:
    """The case the design exists for. 1/3 stored in NUMERIC(18,9) is 0.333333333, a different
    number — reconstructing from it would make two identical runs compare unequal."""
    restored = load_metric(_row(1, 3))
    assert restored.value == Fraction(1, 3)
    assert restored.value != Fraction(333333333, 1000000000)


def test_the_convenience_column_is_lossy_and_that_is_expected() -> None:
    """Asserted rather than assumed, so nobody later 'fixes' the reconstruction to read `value`."""
    approximate = _approximate(Fraction(1, 3))
    assert approximate is not None
    assert Fraction(approximate) != Fraction(1, 3)
    assert approximate.as_tuple().exponent == -VALUE_SCALE


@pytest.mark.parametrize(
    "numerator, denominator",
    [(1, 3), (2, 7), (1, 1), (0, 5), (17, 100), (999999, 1000000)],
)
def test_reconstruction_is_exact_for_any_rational(numerator: int, denominator: int) -> None:
    restored = load_metric(_row(numerator, denominator))
    assert restored.value == Fraction(numerator, denominator)


def test_a_zero_denominator_reconstructs_as_not_measured_not_as_zero() -> None:
    """The distinction the whole metrics layer is built around. A rate of 0 over 0 cases renders as
    a perfect score; it means nobody measured anything."""
    restored = load_metric(_row(0, 0, note="no gold set"))
    assert restored.value is None
    assert not restored.measured
    assert restored.note == "no gold set"


def test_an_unmeasured_metric_stores_a_null_value() -> None:
    assert _approximate(None) is None


def test_the_reconstructed_metric_is_the_same_type_the_metrics_layer_produces() -> None:
    """So a stored run and a fresh computation can be compared without a conversion step, which is
    where a rounding would sneak back in."""
    assert isinstance(load_metric(_row(1, 3)), ComputedMetric)


# ---------------------------------------------------------------------------
# Persistence — needs a database
# ---------------------------------------------------------------------------


# The postgres fixture is registered in tests/conftest.py so every test package can use
# it. A bare plugin name only resolves for tests in the same directory as the fixture.
@pytest.fixture
def session(postgres_engine):  # type: ignore[no-untyped-def]
    from app.db.base import Base
    from app.db.session import session_factory

    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    with factory() as session:
        yield session


def _gold_set(session, version: str = "1.0"):  # type: ignore[no-untyped-def]
    from app.models.evaluation import GoldSet

    gold_set = GoldSet(name="countertops", version=version)
    session.add(gold_set)
    session.flush()
    return gold_set


def _computed() -> dict[str, ComputedMetric]:
    return {
        "critical_false_pass_rate": ComputedMetric(
            "critical_false_pass_rate", Fraction(1, 3), 1, 3
        ),
        "reviewer_minutes": ComputedMetric("reviewer_minutes", None, 0, 0, "needs C1.10"),
    }


def test_a_run_records_every_version_that_could_explain_a_difference(session) -> None:  # type: ignore[no-untyped-def]
    from eval.runs import record_run

    run = record_run(
        session,
        gold_set=_gold_set(session),
        code_version="abc1234",
        rule_snapshot_ids=["snap-1", "snap-2"],
        extractor_versions={"pdfplumber": "0.11.0"},
        results=_computed(),
    )
    assert run.code_version == "abc1234"
    assert run.rule_snapshot_ids == ["snap-1", "snap-2"]
    assert run.extractor_versions == {"pdfplumber": "0.11.0"}
    assert run.gold_set_version == "1.0"


def test_a_run_without_provenance_is_refused(session) -> None:  # type: ignore[no-untyped-def]
    from eval.runs import MissingProvenanceError, record_run

    with pytest.raises(MissingProvenanceError, match="cannot be attributed"):
        record_run(
            session,
            gold_set=_gold_set(session),
            code_version="  ",
            rule_snapshot_ids=["snap-1"],
            extractor_versions={},
            results=_computed(),
        )


def test_a_run_with_no_rule_snapshots_is_refused(session) -> None:  # type: ignore[no-untyped-def]
    from eval.runs import MissingProvenanceError, record_run

    with pytest.raises(MissingProvenanceError, match="rule_snapshot_ids"):
        record_run(
            session,
            gold_set=_gold_set(session),
            code_version="abc",
            rule_snapshot_ids=[],
            extractor_versions={},
            results=_computed(),
        )


def test_metrics_survive_the_database_exactly(session) -> None:  # type: ignore[no-untyped-def]
    """The round trip that matters, through real PostgreSQL rather than a constructed row."""
    from eval.runs import metrics_for, record_run

    run = record_run(
        session,
        gold_set=_gold_set(session),
        code_version="abc",
        rule_snapshot_ids=["s1"],
        extractor_versions={},
        results=_computed(),
    )
    restored = metrics_for(session, run)
    assert restored["critical_false_pass_rate"].value == Fraction(1, 3)
    assert restored["reviewer_minutes"].value is None


def test_runs_are_queryable_as_a_series_with_their_versions(session) -> None:  # type: ignore[no-untyped-def]
    """A number without the versions that produced it cannot answer *what changed*, which is the
    only reason to look at a series."""
    from eval.runs import record_run, series

    gold_set = _gold_set(session)
    for code_version in ("v1", "v2", "v3"):
        record_run(
            session,
            gold_set=gold_set,
            code_version=code_version,
            rule_snapshot_ids=["s1"],
            extractor_versions={},
            results=_computed(),
        )
    points = series(session, metric="critical_false_pass_rate")
    assert [run.code_version for run, _ in points] == ["v1", "v2", "v3"]
    assert all(metric.value == Fraction(1, 3) for _, metric in points)


def test_a_baseline_is_scoped_to_its_gold_set_version(session) -> None:  # type: ignore[no-untyped-def]
    """A baseline from a different gold set is not a baseline — the cases changed, so a difference
    says nothing about the code."""
    from eval.runs import baseline, record_run

    record_run(
        session,
        gold_set=_gold_set(session, version="1.0"),
        code_version="v1",
        rule_snapshot_ids=["s1"],
        extractor_versions={},
        results=_computed(),
        is_baseline=True,
    )
    assert baseline(session, gold_set_version="1.0") is not None
    assert baseline(session, gold_set_version="2.0") is None


# ---------------------------------------------------------------------------
# Per-case results (#315)
# ---------------------------------------------------------------------------


def _gold_case(session: Session, gold_set: GoldSet) -> GoldCase:
    """A gold case needs a document version, which needs the package aggregate above it.

    Built from `tests/db/test_evidence_models.py` rather than from memory: the first version of this
    helper invented a `revision_label` field that `PackageRevision` does not have — it is
    `revision_number`, an integer — and the two tests using it failed on CI, where the database that
    would have caught it actually exists.

    Flushed in stages, and **not** because the ids need populating — `TimestampedUUID` assigns those
    in a construction listener, so they are available immediately. The flushes order the inserts.
    These models are wired with plain `ForeignKey` columns rather than ORM `relationship()`s, so
    SQLAlchemy has no dependency graph to sort a single `add_all` by, and `documents` can be inserted
    before the `package_revisions` row it points at. Collapsing them to one flush passed locally,
    where the database tests skip, and failed on CI with a foreign-key violation.
    """
    digest = "b" * 64
    project = Project(name="GV Case Results Test")
    package = Package(project_id=project.id, vendor=None)
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    artifact = SourceArtifact(
        storage_key=f"originals/{project.id}/drawing.pdf",
        sha256=digest,
        size=100,
        backend_version_id=None,
    )
    document = Document(package_revision_id=revision.id, kind=DocumentKind.SHOP)
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=artifact.id,
        # Equal to the artifact's on purpose: the composite foreign key requires both to match.
        sha256=digest,
        page_count=1,
    )
    case = GoldCase(
        gold_set_id=gold_set.id,
        document_version_id=version.id,
        content_hash=digest,
        annotations={},
        annotated_by="anant",
    )
    session.add(project)
    session.flush()
    session.add(package)
    session.flush()
    session.add(revision)
    session.flush()
    session.add_all((artifact, document))
    session.flush()
    session.add(version)
    session.flush()
    session.add(case)
    session.flush()
    return case


def test_a_run_records_how_each_case_fared(session: Session) -> None:
    """`metric_results` says a rate moved; this says which cases moved."""
    from app.models.evaluation import CaseResult
    from eval.runs import record_run
    from verdict.outcomes import Outcome

    gold_set = _gold_set(session)
    case = _gold_case(session, gold_set)

    run = record_run(
        session,
        gold_set=gold_set,
        code_version="abc1234",
        rule_snapshot_ids=["snap-1"],
        extractor_versions={"pdfplumber": "0.11.0"},
        results=_computed(),
        case_outcomes={case.id: {"CT-1": (Outcome.FAIL, Outcome.PASS)}},
    )

    rows = session.query(CaseResult).filter(CaseResult.evaluation_run_id == run.id).all()
    assert len(rows) == 1
    assert (rows[0].check, rows[0].outcome, rows[0].expected) == ("CT-1", "FAIL", "PASS")


def test_a_run_may_be_recorded_without_case_results(session: Session) -> None:
    """Scoring only aggregate metrics is legitimate. What must not happen is such a run later
    reading as though its cases were compared and found identical — `compare_cases` reports that as
    'not compared' rather than as no change."""
    from app.models.evaluation import CaseResult
    from eval.runs import record_run

    run = record_run(
        session,
        gold_set=_gold_set(session),
        code_version="abc1234",
        rule_snapshot_ids=["snap-1"],
        extractor_versions={"pdfplumber": "0.11.0"},
        results=_computed(),
    )
    assert session.query(CaseResult).filter(CaseResult.evaluation_run_id == run.id).count() == 0


def test_a_case_result_is_immutable() -> None:
    """Like every other evaluation record. One edited after the fact silently changes what a
    historical comparison meant."""
    from app.db.base import Immutable
    from app.models.evaluation import CaseResult

    assert issubclass(CaseResult, Immutable)


def test_one_case_and_check_is_recorded_once_per_run(session: Session) -> None:
    """Two rows for one `(run, case, check)` would make "did this case pass?" have two answers."""
    from sqlalchemy.exc import IntegrityError

    from app.models.evaluation import CaseResult
    from eval.runs import record_run
    from verdict.outcomes import Outcome

    gold_set = _gold_set(session)
    case = _gold_case(session, gold_set)
    run = record_run(
        session,
        gold_set=gold_set,
        code_version="abc1234",
        rule_snapshot_ids=["snap-1"],
        extractor_versions={"pdfplumber": "0.11.0"},
        results=_computed(),
        case_outcomes={case.id: {"CT-1": (Outcome.PASS, Outcome.PASS)}},
    )
    session.add(
        CaseResult(
            evaluation_run_id=run.id,
            gold_case_id=case.id,
            check="CT-1",
            outcome="FAIL",
            expected="PASS",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
