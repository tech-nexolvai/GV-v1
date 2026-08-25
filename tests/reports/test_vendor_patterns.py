"""Vendor patterns: countable, drillable, and never a rule key.

Source: issue #241; ADR-0006. Verification: ``reports/vendor_patterns.py``.

Three things carry this report, and each fails quietly if it is wrong. A count with no findings
behind it is a claim the vendor cannot check. A window that ignores *when* things happened cannot
tell "improving" from "always been like this". And a report that counts only failures misses the
check that passes every time because somebody corrects the reading first.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.session import session_factory, unit_of_work
from app.models.package import Package, PackageRevision, PackageState, Project
from app.models.review import ReviewAction, ReviewActionKind, ReviewSession
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.verdicts import CheckRun, Finding
from reports.vendor_patterns import vendor_patterns
from verdict.outcomes import Outcome, Severity

pytest_plugins = ("tests.app.postgres_fixture",)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
WINDOW = timedelta(days=90)
VENDOR = "Vicentia Millwork"
OTHER_VENDOR = "Someone Else"


@pytest.fixture
def sessions(postgres_engine: Engine) -> sessionmaker[Session]:
    Base.metadata.create_all(postgres_engine)
    return session_factory(postgres_engine)


def _revision(session: Session, vendor: str | None) -> PackageRevision:
    project = Project(name=f"P {uuid4().hex[:6]}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=vendor)
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.RUNNING_CHECKS
    )
    session.add(revision)
    session.flush()
    return revision


def _snapshot(session: Session) -> RuleSnapshot:
    definition = RuleDefinition(rule_id=f"CT-{uuid4().hex[:8]}")
    session.add(definition)
    session.flush()
    canonical = json.dumps({"id": definition.rule_id}, separators=(",", ":"))
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=canonical,
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _finding(
    session: Session,
    revision: PackageRevision,
    snapshot: RuleSnapshot,
    outcome: Outcome,
    when: datetime,
) -> Finding:
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version="verdict-1.2.3",
    )
    session.add(run)
    session.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=outcome.value,
        severity=Severity.CRITICAL.value,
        trace={},
        parameter_set_versions={},
        created_at=when,
    )
    session.add(finding)
    session.flush()
    return finding


def _correct(session: Session, finding: Finding, revision: PackageRevision, when: datetime) -> None:
    review = ReviewSession(package_revision_id=revision.id, reviewer="anant")
    session.add(review)
    session.flush()
    action = ReviewAction(
        review_session_id=review.id,
        finding_id=finding.id,
        package_revision_id=revision.id,
        action=ReviewActionKind.CORRECT.value,
        actor="anant",
        created_at=when,
    )
    session.add(action)
    session.flush()


# ---------------------------------------------------------------------------
# Drillable, not just countable
# ---------------------------------------------------------------------------


def test_a_recurring_failure_carries_the_findings_that_prove_it(
    sessions: sessionmaker[Session],
) -> None:
    """Input: three failures of one check. Outcome: the three finding ids, not just a count.

    "You get this wrong a lot" invites an argument. "These three findings, here they are" invites a
    look — and the vendor can check it.
    """
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        expected = {
            _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=d)).id
            for d in (5, 10, 15)
        }

    with unit_of_work(sessions) as session:
        report = vendor_patterns(session, VENDOR, WINDOW, now=NOW)

    assert report.has_findings
    (found,) = report.recurring_failures.values()
    assert set(found) == expected


def test_passes_are_not_counted_as_failures(sessions: sessionmaker[Session]) -> None:
    """A report that counted every finding would make a working vendor look like a problem."""
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        _finding(session, revision, snapshot, Outcome.PASS, NOW - timedelta(days=1))
        _finding(session, revision, snapshot, Outcome.REVIEW_REQUIRED, NOW - timedelta(days=1))

    with unit_of_work(sessions) as session:
        report = vendor_patterns(session, VENDOR, WINDOW, now=NOW)

    assert report.recurring_failures == {}


def test_failures_are_grouped_by_the_exact_rule_version(
    sessions: sessionmaker[Session],
) -> None:
    """Keyed by snapshot, not rule id: a rule whose tolerance changed mid-window is two checks.

    Pooling them would attribute failures against the old limit to the new one, which is the
    argument a vendor would rightly win.
    """
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        first, second = _snapshot(session), _snapshot(session)
        _finding(session, revision, first, Outcome.FAIL, NOW - timedelta(days=2))
        _finding(session, revision, second, Outcome.FAIL, NOW - timedelta(days=2))

    with unit_of_work(sessions) as session:
        report = vendor_patterns(session, VENDOR, WINDOW, now=NOW)

    assert len(report.recurring_failures) == 2


# ---------------------------------------------------------------------------
# Corrections are a signal too
# ---------------------------------------------------------------------------


def test_a_corrected_reading_is_reported_even_though_the_check_passed(
    sessions: sessionmaker[Session],
) -> None:
    """The case a failure count misses entirely.

    A check that passes only because a reviewer fixes the reading every time is not a check that
    works — it is one whose input is unreliable, and the failure count says nothing about it.
    """
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        passed = _finding(session, revision, snapshot, Outcome.PASS, NOW - timedelta(days=3))
        _correct(session, passed, revision, NOW - timedelta(days=3))

    with unit_of_work(sessions) as session:
        report = vendor_patterns(session, VENDOR, WINDOW, now=NOW)

    assert report.recurring_failures == {}
    (corrected,) = report.correction_hotspots.values()
    assert corrected == (passed.id,)


# ---------------------------------------------------------------------------
# The window, and the boundary where a windowed report is usually wrong
# ---------------------------------------------------------------------------


def test_a_finding_older_than_the_window_is_excluded(
    sessions: sessionmaker[Session],
) -> None:
    """Otherwise "always been like this" and "fixed it months ago" read identically."""
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=200))

    with unit_of_work(sessions) as session:
        report = vendor_patterns(session, VENDOR, WINDOW, now=NOW)

    assert report.recurring_failures == {}
    assert not report.has_findings


def test_the_window_boundary_is_inclusive_at_the_start_and_exclusive_at_the_end(
    sessions: sessionmaker[Session],
) -> None:
    """Boundary-exact, both sides. A report whose edges are guesswork cannot be reconciled twice."""
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        _finding(session, revision, snapshot, Outcome.FAIL, NOW - WINDOW)  # exactly `since`
        _finding(session, revision, snapshot, Outcome.FAIL, NOW)  # exactly `until`

    with unit_of_work(sessions) as session:
        report = vendor_patterns(session, VENDOR, WINDOW, now=NOW)

    assert sum(len(v) for v in report.recurring_failures.values()) == 1


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------


def test_a_vendor_who_has_stopped_failing_reads_as_improving(
    sessions: sessionmaker[Session],
) -> None:
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        for day in (80, 78, 76, 74):
            _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=day))

    with unit_of_work(sessions) as session:
        assert vendor_patterns(session, VENDOR, WINDOW, now=NOW).trend == "improving"


def test_a_vendor_failing_more_recently_reads_as_worsening(
    sessions: sessionmaker[Session],
) -> None:
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        for day in (10, 8, 6, 4):
            _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=day))

    with unit_of_work(sessions) as session:
        assert vendor_patterns(session, VENDOR, WINDOW, now=NOW).trend == "worsening"


def test_too_few_findings_is_steady_rather_than_a_direction(
    sessions: sessionmaker[Session],
) -> None:
    """With one failure either side, a change of one is noise.

    Reporting it as a direction invites a conversation the data does not support — and this report
    exists to make those conversations evidenced.
    """
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=80))
        _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=5))

    with unit_of_work(sessions) as session:
        assert vendor_patterns(session, VENDOR, WINDOW, now=NOW).trend == "steady"


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


def test_one_vendors_findings_never_appear_in_anothers_report(
    sessions: sessionmaker[Session],
) -> None:
    """The whole point of the report is attribution; getting this wrong accuses the wrong company."""
    with unit_of_work(sessions) as session:
        snapshot = _snapshot(session)
        mine = _revision(session, VENDOR)
        theirs = _revision(session, OTHER_VENDOR)
        _finding(session, mine, snapshot, Outcome.FAIL, NOW - timedelta(days=2))
        for day in (3, 4, 5):
            _finding(session, theirs, snapshot, Outcome.FAIL, NOW - timedelta(days=day))

    with unit_of_work(sessions) as session:
        report = vendor_patterns(session, VENDOR, WINDOW, now=NOW)

    assert sum(len(v) for v in report.recurring_failures.values()) == 1


def test_a_package_with_no_vendor_is_not_attributed_to_anyone(
    sessions: sessionmaker[Session],
) -> None:
    """`Package.vendor` is nullable, and an unrecorded vendor must not become somebody's record."""
    with unit_of_work(sessions) as session:
        revision = _revision(session, None)
        snapshot = _snapshot(session)
        _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=2))

    with unit_of_work(sessions) as session:
        assert vendor_patterns(session, VENDOR, WINDOW, now=NOW).recurring_failures == {}


def test_an_empty_vendor_is_refused(sessions: sessionmaker[Session]) -> None:
    """It would pool every package whose vendor is unrecorded and attribute it to nobody."""
    with unit_of_work(sessions) as session, pytest.raises(ValueError, match="must be named"):
        vendor_patterns(session, "  ", WINDOW, now=NOW)


def test_an_empty_report_is_distinguishable_from_a_clean_record(
    sessions: sessionmaker[Session],
) -> None:
    """`has_findings` exists because empty aggregates and a good vendor look identical otherwise."""
    with unit_of_work(sessions) as session:
        report = vendor_patterns(session, "Nobody At All", WINDOW, now=NOW)

    assert not report.has_findings
    assert report.trend == "steady"


def test_the_report_states_the_window_it_covers(sessions: sessionmaker[Session]) -> None:
    """A report passed on without its window is a number nobody can reproduce."""
    with unit_of_work(sessions) as session:
        report = vendor_patterns(session, VENDOR, WINDOW, now=NOW)

    assert report.until == NOW
    assert report.since == NOW - WINDOW
    assert report.vendor == VENDOR
