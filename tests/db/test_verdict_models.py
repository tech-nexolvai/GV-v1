"""Database contract for check runs, sealed operands, findings and evidence (#199, C1.9).

This is the plane a client or an auditor is shown, so the story's real acceptance criterion is
recomputability: read a finding's operands back and the arithmetic has to come out the same. The
tests here are that, plus the refusals that keep it true — above all that only qualified evidence can
be an operand, and that an operand is an exact rational rather than a rounded one.
"""

from __future__ import annotations

from fractions import Fraction
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, Float, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.base import Base, Immutable
from app.db.session import session_factory, unit_of_work
from app.models import (
    CheckRun,
    Finding,
    FindingEvidence,
    PackageRevision,
    RuleDefinition,
    RuleSnapshot,
    VerdictInput,
)
from units.measurement import Unit
from verdict.operands import QUALIFIED_STATUSES, EvidenceStatus
from verdict.outcomes import Outcome, Severity

pytest_plugins = ("tests.app.postgres_fixture",)

VERDICT_TABLES = {"check_runs", "verdict_inputs", "findings", "finding_evidence"}
CANONICAL = '{"id":"CT-WIDTH-001","version":"1.0.0"}'


def _upgrade(engine: Engine) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


def _revision(session: Session) -> PackageRevision:
    from app.models import Package, PackageState, Project

    project = Project(name=f"GV Verdict Test {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    return revision


def _snapshot(session: Session) -> RuleSnapshot:
    import hashlib

    definition = RuleDefinition(rule_id=f"CT-WIDTH-{uuid4().hex[:6]}")
    session.add(definition)
    session.flush()
    body = CANONICAL.replace("1.0.0", uuid4().hex[:5])
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(body.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=body,
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _run(session: Session) -> CheckRun:
    run = CheckRun(
        package_revision_id=_revision(session).id,
        rule_snapshot_id=_snapshot(session).id,
        engine_version="verdict-1.2.3",
    )
    session.add(run)
    session.flush()
    return run


def _operand(run: CheckRun, name: str, value: str, status: EvidenceStatus) -> VerdictInput:
    exact = Fraction(value)
    return VerdictInput(
        check_run_id=run.id,
        operand_name=name,
        value_numerator=exact.numerator,
        value_denominator=exact.denominator,
        unit=Unit.MM.value,
        evidence_status=status.value,
    )


# ---------------------------------------------------------------------------
# Shape, without a database
# ---------------------------------------------------------------------------


def test_all_four_tables_are_registered() -> None:
    assert VERDICT_TABLES <= set(Base.metadata.tables)


def test_a_finding_and_its_inputs_are_immutable() -> None:
    """A re-run produces a new finding against a new check run. Editing one would change what a
    reviewer signed off on, retrospectively and silently."""
    for model in (VerdictInput, Finding, FindingEvidence):
        assert issubclass(model, Immutable)
    assert not issubclass(CheckRun, Immutable), "a run is a process record and may be annotated"


def test_no_float_column_exists_anywhere_in_this_plane() -> None:
    """A golden-rule violation if one did. ADR-0001: a rounded operand recomputes to a different
    answer, and the stored inputs would then disagree with the stored outcome."""
    for name in VERDICT_TABLES:
        for column in Base.metadata.tables[name].columns:
            assert not isinstance(column.type, Float), f"{name}.{column.name} is a float"


def test_a_finding_cites_the_snapshot_not_the_rule() -> None:
    """A rule id says "the width check"; a snapshot id says which version, with what tolerance, from
    what content hash. Reconstruction needs the second."""
    columns = Base.metadata.tables["check_runs"].columns
    assert "rule_snapshot_id" in columns
    assert "rule_id" not in columns


def test_only_qualified_evidence_may_be_an_operand() -> None:
    """`verdict/operands.py` admits two of five statuses into a verdict. The constraint mirrors it,
    because a RAW_CANDIDATE here would be one unverified extraction carrying the weight of
    corroborated evidence."""
    constraint = next(
        c
        for c in Base.metadata.tables["verdict_inputs"].constraints
        if getattr(c, "name", "") == "ck_verdict_inputs_verdict_input_status_qualified"
    )
    expression = str(constraint.sqltext)  # type: ignore[attr-defined]
    for status in QUALIFIED_STATUSES:
        assert f"'{status.value}'" in expression
    for excluded in set(EvidenceStatus) - QUALIFIED_STATUSES:
        assert f"'{excluded.value}'" not in expression


def test_the_migration_vocabularies_match_the_live_enums() -> None:
    """The migration spells the values out so it keeps saying what it said. This catches drift."""
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0011_verdict_plane.py"
    ).read_text(encoding="utf-8")
    for member in (*Outcome, *Severity, *Unit, *QUALIFIED_STATUSES):
        assert f"'{member.value}'" in migration, f"{member.value} missing from the migration"


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


def test_a_finding_recomputes_from_its_own_stored_operands(postgres_engine: Engine) -> None:
    """**The story's real acceptance criterion.** Read the operands back and the arithmetic comes out
    the same — exactly, as `Fraction`. A finding that cannot be reproduced from its own stored inputs
    is an audit trail in name only."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        run = _run(session)
        session.add_all(
            (
                _operand(run, "cabinet_a", "1219/1", EvidenceStatus.CORROBORATED),
                _operand(run, "cabinet_b", "1219/1", EvidenceStatus.CORROBORATED),
                _operand(run, "filler", "127/2", EvidenceStatus.HUMAN_CONFIRMED),
            )
        )
        session.flush()
        session.add(
            Finding(
                check_run_id=run.id,
                outcome=Outcome.PASS.value,
                severity=Severity.CRITICAL.value,
                trace={"sum": "2501.5", "unit": "mm"},
                parameter_set_versions={"countertop": "1.0.0"},
            )
        )

    with unit_of_work(factory) as session:
        operands = session.scalars(select(VerdictInput)).all()
        total = sum(
            (Fraction(o.value_numerator, o.value_denominator) for o in operands), Fraction(0)
        )
        assert total == Fraction("1219") + Fraction("1219") + Fraction("127/2")
        assert total == Fraction("5003/2")
        finding = session.scalars(select(Finding)).one()
        assert Fraction(finding.trace["sum"]) == total


def test_a_repeating_fraction_survives_exactly(postgres_engine: Engine) -> None:
    """1/3 as a numerator and denominator, not 0.333333333. The pair is why this plane can be
    recomputed at all."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        run = _run(session)
        session.add(_operand(run, "third", "1/3", EvidenceStatus.CORROBORATED))
    with unit_of_work(factory) as session:
        stored = session.scalars(select(VerdictInput)).one()
        assert Fraction(stored.value_numerator, stored.value_denominator) == Fraction(1, 3)


@pytest.mark.parametrize(
    "status",
    sorted(set(EvidenceStatus) - QUALIFIED_STATUSES, key=lambda s: s.value),
)
def test_unqualified_evidence_cannot_be_an_operand(
    postgres_engine: Engine, status: EvidenceStatus
) -> None:
    """The gate, in the database. A RAW_CANDIDATE, CONFLICTING or REJECTED reading is precisely why a
    check abstains — writing one here would let it decide instead."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.add(_operand(_run(session), "width", "1219/1", status))


def test_a_zero_denominator_is_refused(postgres_engine: Engine) -> None:
    """Not a number. A rational with no denominator cannot be recomputed, only guessed at."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        run = _run(session)
        session.add(
            VerdictInput(
                check_run_id=run.id,
                operand_name="width",
                value_numerator=1,
                value_denominator=0,
                unit=Unit.MM.value,
                evidence_status=EvidenceStatus.CORROBORATED.value,
            )
        )


def test_one_operand_name_per_run(postgres_engine: Engine) -> None:
    """The same number in a different slot is a different calculation, so a name may appear once."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        run = _run(session)
        session.add(_operand(run, "width", "1219/1", EvidenceStatus.CORROBORATED))
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        existing = session.scalars(select(CheckRun)).one()
        session.add(_operand(existing, "width", "1220/1", EvidenceStatus.CORROBORATED))


def test_an_operand_may_have_no_observation(postgres_engine: Engine) -> None:
    """A literal lives in the rule and a user input is what somebody typed. Neither has an
    observation, and a non-null column would force one to be invented."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        run = _run(session)
        session.add(_operand(run, "field_cut", "254/10", EvidenceStatus.HUMAN_CONFIRMED))
    with unit_of_work(factory) as session:
        assert session.scalars(select(VerdictInput)).one().canonical_observation_id is None


def test_one_finding_per_check_run(postgres_engine: Engine) -> None:
    """Two would leave "what did this check decide?" with two answers."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)

    def finding(run_id, outcome: Outcome) -> Finding:
        return Finding(
            check_run_id=run_id,
            outcome=outcome.value,
            severity=Severity.CRITICAL.value,
            trace={},
            parameter_set_versions={},
        )

    with unit_of_work(factory) as session:
        session.add(finding(_run(session).id, Outcome.PASS))
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        existing = session.scalars(select(CheckRun)).one()
        session.add(finding(existing.id, Outcome.FAIL))


def test_an_unknown_outcome_is_refused(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.add(
            Finding(
                check_run_id=_run(session).id,
                outcome="PROBABLY_FINE",
                severity=Severity.CRITICAL.value,
                trace={},
                parameter_set_versions={},
            )
        )


def test_a_snapshot_cannot_be_deleted_while_a_run_cites_it(postgres_engine: Engine) -> None:
    """RESTRICT. A finding citing a deleted snapshot is a decision nobody can reconstruct."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        _run(session)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.delete(session.scalars(select(RuleSnapshot)).one())


def test_a_re_run_produces_a_new_finding_rather_than_editing_one(postgres_engine: Engine) -> None:
    """Immutability, end to end: two runs of the same rule against the same revision keep both
    findings, so a reviewer can see what changed."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        for outcome in (Outcome.FAIL, Outcome.PASS):
            run = _run(session)
            session.add(
                Finding(
                    check_run_id=run.id,
                    outcome=outcome.value,
                    severity=Severity.CRITICAL.value,
                    trace={},
                    parameter_set_versions={},
                )
            )
    with unit_of_work(factory) as session:
        assert {f.outcome for f in session.scalars(select(Finding))} == {"FAIL", "PASS"}
