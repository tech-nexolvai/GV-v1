"""The first pipeline stage that does work: running the rules and recording what they decided.

Source: the `Stages` seam in `workflow/review.py`, whose only implementation until now was `NoStages`.
Verification for: `workflow/stages.py`, `app/verdicts/record.py`, `app/verdicts/trace.py`,
`app/verdicts/rulebook.py:snapshot_store`, `app/models/parameters.py:load_parameter_sets`.

The tests that carry the weight are the two counting ones. Everything abstains today because there is
no extraction, and an abstention looks like nothing happening — so what has to be pinned is that
*every published rule produced a row*, and that a re-run leaves the reviewer with one set rather than
two. Both failures are silent, and both read as a clean package.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
import yaml
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.finding_chain import classify_trace
from app.db.session import session_factory
from app.models import (
    CheckRun,
    Finding,
    Package,
    PackageRevision,
    PackageState,
    Project,
    RuleDefinition,
    RuleSnapshot,
    VerdictInput,
)
from app.verdicts.record import EvidenceMissing, record_finding
from rules.schema import Rule
from rules.snapshot import publish
from tests.app.postgres_fixture import alembic_config
from units.measurement import Unit
from verdict.finding import Finding as DomainFinding
from verdict.outcomes import Outcome, Severity
from verdict.trace import CalculationTrace
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

RULEBOOK = pathlib.Path(__file__).resolve().parents[2] / "rules" / "rulebook"


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _revision(session: Session) -> PackageRevision:
    project = Project(name="stage test")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Apex Glass & Stone")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.RUNNING_CHECKS
    )
    session.add(revision)
    session.flush()
    return revision


def _publish_rulebook(session: Session) -> int:
    """Publish every authored rule, the way D6 would.

    Read off disk rather than hand-built, so this exercises the real rules — including their
    discriminators and derivations — and grows automatically when a rule is authored.
    """
    published = 0
    for path in sorted(RULEBOOK.glob("*.yaml")):
        rule = Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        snapshot = publish(rule)
        definition = RuleDefinition(rule_id=rule.id)
        session.add(definition)
        session.flush()
        session.add(
            RuleSnapshot(
                rule_definition_id=definition.id,
                snapshot_id=snapshot.snapshot_id,
                version=rule.version,
                canonical_json=snapshot.canonical_json,
                product_type=rule.product_type.value,
                check_type=rule.check_type.value,
                unconfirmed_tolerance_count=0,
            )
        )
        published += 1
    session.flush()
    return published


def _live_findings(session: Session, revision_id: UUID) -> list[Finding]:
    return list(
        session.execute(
            select(Finding)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .where(
                Finding.package_revision_id == revision_id,
                CheckRun.superseded_at.is_(None),
            )
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Every published rule is accounted for
# ---------------------------------------------------------------------------


def test_every_published_rule_produces_a_finding(session: Session) -> None:
    """**The invariant that matters most, and the one that fails silently.**

    A rule that never ran and a rule that passed look identical on a reviewer's list. So the check is
    not "some findings were written" but that the count matches the rulebook exactly: if a rule is
    dropped — by a product-type filter, by an unresolved discriminator, by anything — this fails.

    Both of those have already happened during development. Filtering to `countertop` silently
    skipped the two cabinet rules, and the resolver's own abstentions were counted rather than
    recorded, which lost two more.
    """
    revision = _revision(session)
    published = _publish_rulebook(session)

    result = DatabaseStages().run_checks(session, revision.id)

    assert result["ran"] is True
    assert result["findings"] == published, (
        f"{published} rules are published but {result['findings']} findings were written. A rule "
        "that produced no row is a check the reviewer cannot tell did not run."
    )
    assert len(_live_findings(session, revision.id)) == published


def test_a_rule_the_resolver_could_not_attempt_is_still_recorded(session: Session) -> None:
    """A discriminator nobody can establish means the rule never starts. That is a result.

    `CT-WIDTH-001` declares `wall_config`, and nothing reads it off a drawing yet, so applicability
    cannot pick a variant. Recording it as `REVIEW_REQUIRED` with the resolver's reason is what makes
    the gap visible; counting it would leave the reviewer with a shorter list and no idea why.
    """
    revision = _revision(session)
    _publish_rulebook(session)
    DatabaseStages().run_checks(session, revision.id)

    by_rule = _findings_by_rule(session, revision.id)

    assert by_rule["CT-WIDTH-001"].outcome == Outcome.REVIEW_REQUIRED.value
    assert by_rule["CT-WIDTH-001"].trace["cause"] == "needs_review"
    assert by_rule["CT-WIDTH-001"].trace["reason"]


def test_nothing_decides_without_operands(session: Session) -> None:
    """No extraction means no operands, so no check may reach PASS or FAIL.

    A decisive verdict here would be arithmetic performed on values nobody read — the exact thing
    every abstention path in the engine exists to prevent.
    """
    revision = _revision(session)
    _publish_rulebook(session)
    DatabaseStages().run_checks(session, revision.id)

    outcomes = {finding.outcome for finding in _live_findings(session, revision.id)}

    assert outcomes <= {Outcome.NOT_FOUND.value, Outcome.REVIEW_REQUIRED.value}
    assert Outcome.PASS.value not in outcomes
    assert Outcome.FAIL.value not in outcomes


def test_an_abstention_says_which_input_was_missing(session: Session) -> None:
    """ "NOT_FOUND" is not actionable; naming the operand is.

    The reason goes in the stored trace, so it survives to the reviewer rather than living in a log
    nobody reads during a review.
    """
    revision = _revision(session)
    _publish_rulebook(session)
    DatabaseStages().run_checks(session, revision.id)

    finding = _findings_by_rule(session, revision.id)["CT-SINK-CUTOUT-WIDTH-001"]

    assert finding.outcome == Outcome.NOT_FOUND.value
    assert "sink_interior_width" in finding.trace["reason"]


def test_an_unpublished_rulebook_says_so_rather_than_reporting_success(session: Session) -> None:
    """Zero rules and zero findings is not a clean package.

    `ran: False` with a reason is the difference between "nothing to check" and "everything checked
    out", and they must not be reported the same way.
    """
    revision = _revision(session)

    result = DatabaseStages().run_checks(session, revision.id)

    assert result["ran"] is False
    assert "no rules" in str(result["reason"])
    assert _live_findings(session, revision.id) == []


# ---------------------------------------------------------------------------
# Re-runs supersede rather than duplicate
# ---------------------------------------------------------------------------


def test_a_second_run_leaves_the_reviewer_one_set(session: Session) -> None:
    """**Findings are immutable, so a re-run adds rows rather than replacing them.**

    Both sets stay in the table — that is the audit trail — but the reviewer must see one. Two copies
    would usually agree, which is the dangerous part: it reads as duplication until the run where a
    rulebook fix changed a verdict, and the screen then shows a PASS and a FAIL for one check with
    equal standing.
    """
    revision = _revision(session)
    published = _publish_rulebook(session)

    first = DatabaseStages().run_checks(session, revision.id)
    second = DatabaseStages().run_checks(session, revision.id)

    assert first["superseded_runs"] == 0
    assert second["superseded_runs"] == published

    every = list(
        session.execute(select(Finding).where(Finding.package_revision_id == revision.id)).scalars()
    )
    assert len(every) == published * 2, "the earlier findings must still exist"
    assert len(_live_findings(session, revision.id)) == published


def test_the_superseded_run_keeps_its_findings(session: Session) -> None:
    """Nothing is deleted and nothing is edited — `findings` is append-only and would refuse either.
    Only the run is annotated."""
    revision = _revision(session)
    _publish_rulebook(session)
    DatabaseStages().run_checks(session, revision.id)
    DatabaseStages().run_checks(session, revision.id)

    superseded = list(
        session.execute(
            select(CheckRun).where(
                CheckRun.package_revision_id == revision.id,
                CheckRun.superseded_at.is_not(None),
            )
        ).scalars()
    )

    assert superseded
    for run in superseded:
        finding = session.execute(
            select(Finding).where(Finding.check_run_id == run.id)
        ).scalar_one()
        assert finding is not None


# ---------------------------------------------------------------------------
# What the writer refuses
# ---------------------------------------------------------------------------


def test_a_decided_finding_with_no_evidence_is_refused(session: Session) -> None:
    """**The invariant the schema cannot hold.**

    `app/models/verdicts.py` says a finding with no evidence cannot exist and notes that no `CHECK`
    can express it, so the writer is the enforcement. A PASS resting on nothing is not a lenient
    finding — it is a claim about a drawing nobody read, and it is the one shape that must never
    reach a vendor.

    Constructed *with* a trace, because `verdict/finding.py` already refuses a decisive finding that
    has none — a stricter guard, one layer up, which fires before this one can. What remains
    reachable, and what this covers, is the more plausible shape: a trace that looks like a
    calculation while no qualified operand was ever sealed behind it.
    """
    revision = _revision(session)
    _publish_rulebook(session)
    snapshot_id = session.execute(select(RuleSnapshot.snapshot_id)).scalars().first()

    with pytest.raises(EvidenceMissing, match="no qualified operand"):
        record_finding(
            session,
            package_revision_id=revision.id,
            finding=DomainFinding(
                rule_id="CT-WIDTH-001",
                outcome=Outcome.PASS,
                severity=Severity.CRITICAL,
                reason="invented",
                snapshot_id=str(snapshot_id),
                engine_version="1.0.0",
                trace=CalculationTrace(
                    operation="equals",
                    operands=(),
                    intermediates=(),
                    comparison="96 in == 96 in",
                    tolerance=None,
                    arithmetic_unit=Unit.INCH,
                    outcome=Outcome.PASS,
                    engine_version="1.0.0",
                    operation_version="1",
                ),
            ),
            operands={},
            parameter_set_ids={},
        )


def test_an_abstention_with_no_evidence_is_allowed(session: Session) -> None:
    """The mirror of the test above, and the reason the rule is about *decisions*.

    An abstention has no evidence because there was none. Refusing it would mean a check that could
    not run also could not be recorded — which is the silence the whole pipeline is built to avoid.
    """
    revision = _revision(session)
    _publish_rulebook(session)
    snapshot_id = session.execute(select(RuleSnapshot.snapshot_id)).scalars().first()

    row = record_finding(
        session,
        package_revision_id=revision.id,
        finding=DomainFinding(
            rule_id="CT-WIDTH-001",
            outcome=Outcome.NOT_FOUND,
            severity=Severity.CRITICAL,
            reason="nothing was read",
            snapshot_id=str(snapshot_id),
            engine_version="1.0.0",
        ),
        operands={},
        parameter_set_ids={},
    )

    assert row.outcome == Outcome.NOT_FOUND.value


def test_a_finding_citing_an_unpublished_snapshot_is_refused(session: Session) -> None:
    """A finding whose snapshot the database does not hold could never be reproduced, which is the
    one thing `AGENTS.md` §2.7 says a finding must always be."""
    revision = _revision(session)

    with pytest.raises(EvidenceMissing, match="no published snapshot"):
        record_finding(
            session,
            package_revision_id=revision.id,
            finding=DomainFinding(
                rule_id="CT-WIDTH-001",
                outcome=Outcome.NOT_FOUND,
                severity=Severity.CRITICAL,
                reason="x",
                snapshot_id=f"sha256:{'0' * 64}",
                engine_version="1.0.0",
            ),
            operands={},
            parameter_set_ids={},
        )


# ---------------------------------------------------------------------------
# What is written can be read back
# ---------------------------------------------------------------------------


def test_every_stored_trace_is_one_the_reader_recognises(session: Session) -> None:
    """**This is the first code ever to write `findings.trace`.**

    `classify_trace` has been reading that column since the chain endpoint was built, against rows
    that were only ever inserted by hand. If the writer and the reader disagree the failure is
    silent: an unrecognised trace becomes `OpaqueTraceOut` and renders as a blank panel rather than
    an error.
    """
    revision = _revision(session)
    _publish_rulebook(session)
    DatabaseStages().run_checks(session, revision.id)

    for finding in _live_findings(session, revision.id):
        # Through JSON, because that is what the column round-trips through in production.
        classified = classify_trace(json.loads(json.dumps(finding.trace)))
        assert (
            classified.kind == "abstention"
        ), f"{finding.outcome} stored a trace the reader classified as {classified.kind!r}"
        assert "kind" not in finding.trace, "the reader adds `kind`; a stored one would shadow it"


def test_no_verdict_inputs_are_written_when_nothing_was_read(session: Session) -> None:
    """Sealed operands are the values that entered the arithmetic. No arithmetic ran, so there are
    none — writing placeholder rows would claim inputs that never existed."""
    revision = _revision(session)
    _publish_rulebook(session)
    DatabaseStages().run_checks(session, revision.id)

    assert list(session.execute(select(VerdictInput)).scalars()) == []


def test_the_findings_carry_the_parameter_sets_that_judged_them(session: Session) -> None:
    """The rulebook's own declared defaults are a real layer and are recorded as one.

    Without them a rule needing only its authored `1/4` clearance would abstain for want of a value
    somebody already wrote down — an abstention caused by our wiring, wearing the appearance of a
    fact about the drawing.
    """
    revision = _revision(session)
    _publish_rulebook(session)
    DatabaseStages().run_checks(session, revision.id)

    versions = _findings_by_rule(session, revision.id)[
        "CT-SINK-CUTOUT-WIDTH-001"
    ].parameter_set_versions

    assert "global" in versions
    assert versions["global"].startswith("sha256:")


def _findings_by_rule(session: Session, revision_id: UUID) -> dict[str, Finding]:
    """Live findings keyed by the rule that produced them."""
    found: dict[str, Finding] = {}
    for finding in _live_findings(session, revision_id):
        run = session.get(CheckRun, finding.check_run_id)
        assert run is not None
        snapshot = session.get(RuleSnapshot, run.rule_snapshot_id)
        assert snapshot is not None
        definition = session.get(RuleDefinition, snapshot.rule_definition_id)
        assert definition is not None
        found[definition.rule_id] = finding
    return found


def test_a_missing_revision_is_reported_rather_than_raising(session: Session) -> None:
    """A stage is dispatched by a workflow that may be replaying an old message. An unknown revision
    is a fact to report, not a crash to retry forever."""
    result = DatabaseStages().run_checks(session, uuid4())

    assert result["ran"] is False
    assert "no such package revision" in str(result["reason"])


# ---------------------------------------------------------------------------
# The five stages that are still not built
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["ingest", "match", "validate_evidence", "generate_outputs"])
def test_the_unbuilt_stages_still_say_they_are_unbuilt(session: Session, stage: str) -> None:
    """`NoStages`' answer, kept verbatim. A stage that started returning `{}` because it was
    inherited rather than written would let a package look processed."""
    result = getattr(DatabaseStages(), stage)(session, uuid4())

    assert result == {"implemented": False, "stage": stage}


def test_extract_pages_returns_no_pages_rather_than_a_mapping(session: Session) -> None:
    """Spelled out rather than inherited or caught by a `__getattr__`.

    `tests/workflow/conftest.py` records why: a catch-all once returned a mapping here and
    `join_pages` counted a phantom page.
    """
    assert DatabaseStages().extract_pages(session, uuid4()) == ()
