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


def test_the_aggregates_cannot_be_edited_by_a_caller(
    sessions: sessionmaker[Session],
) -> None:
    """`VendorReport` is frozen, and a frozen dataclass holding a plain dict is not.

    A caller editing an aggregate would change the report underneath whoever else is holding it —
    the same defect `ProjectScope` was fixed for.
    """
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        failed = _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=2))
        _correct(session, failed, revision, NOW - timedelta(days=2))

    with unit_of_work(sessions) as session:
        report = vendor_patterns(session, VENDOR, WINDOW, now=NOW)

    for aggregate in (report.recurring_failures, report.correction_hotspots):
        # Non-empty first. An empty immutable mapping raises on assignment just as a populated one
        # does, so without this the test would keep passing if the report stopped finding anything.
        assert aggregate, "the seeded data should appear in both aggregates"
        with pytest.raises(TypeError):
            aggregate["anything"] = ()  # type: ignore[index]


def test_the_same_data_produces_the_same_report_twice(
    sessions: sessionmaker[Session],
) -> None:
    """The query has no ORDER BY, so row order is whatever the database returns.

    A report listing the same findings in a different order each run looks like it changed when it
    did not — and this one exists to be reconciled against a previous copy.
    """
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        # Several snapshots, so key order is exercised as well as the ids inside each.
        snapshots = [_snapshot(session) for _ in range(3)]
        for day, snapshot in zip((2, 4, 6, 8, 10, 12), snapshots * 2, strict=False):
            _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=day))

    with unit_of_work(sessions) as session:
        first = vendor_patterns(session, VENDOR, WINDOW, now=NOW)
        second = vendor_patterns(session, VENDOR, WINDOW, now=NOW)

    assert first.recurring_failures, "the seeded failures should appear, or this asserts nothing"
    # Compared as item lists, not as dicts: `==` on two dicts ignores key order, which is the one
    # property this test exists to check.
    assert list(first.recurring_failures.items()) == list(second.recurring_failures.items())
    for report in (first, second):
        keys = list(report.recurring_failures)
        assert keys == sorted(keys), "snapshot keys are not in a stable order"
        for ids in report.recurring_failures.values():
            assert list(ids) == sorted(ids, key=str), "finding ids are not in a stable order"


@pytest.mark.parametrize("window", [timedelta(0), timedelta(days=-1)])
def test_a_non_positive_window_is_refused(
    window: timedelta, sessions: sessionmaker[Session]
) -> None:
    """Refused rather than answered.

    A zero or negative window would otherwise return an empty report, which is indistinguishable
    from a vendor with a clean record — so the caller learns nothing and believes something.
    """
    with unit_of_work(sessions) as session, pytest.raises(ValueError, match="must be positive"):
        vendor_patterns(session, VENDOR, window, now=NOW)


def test_no_failures_then_several_is_reported_as_worsening(
    sessions: sessionmaker[Session],
) -> None:
    """Either half reaching the threshold is enough, and this is why.

    Requiring *both* halves to be busy would report a vendor who went from nothing to five failures
    as steady — the exact case most worth raising.
    """
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        for day in (10, 8, 6, 4, 2):
            _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=day))

    with unit_of_work(sessions) as session:
        assert vendor_patterns(session, VENDOR, WINDOW, now=NOW).trend == "worsening"


# ---------------------------------------------------------------------------
# Person-confirmed only (#1272): a FAIL nobody confirmed may rest on an AI misread
# ---------------------------------------------------------------------------

PERSON = "reviewer-a"
STRANGER = "ui script"
CODE_ACTOR = "code:invented-checker/v1"
#: The local real-reviewer list, as a test gives it: invented names only.
LIST = frozenset({"  Reviewer-A "})


def _act(
    session: Session,
    finding: Finding,
    revision: PackageRevision,
    action: ReviewActionKind,
    when: datetime,
    *,
    actor: str = PERSON,
) -> ReviewAction:
    review = ReviewSession(package_revision_id=revision.id, reviewer=actor)
    session.add(review)
    session.flush()
    row = ReviewAction(
        review_session_id=review.id,
        finding_id=finding.id,
        package_revision_id=revision.id,
        action=action.value,
        actor=actor,
        created_at=when,
    )
    session.add(row)
    session.flush()
    return row


def _sign(
    session: Session, revision: PackageRevision, findings: list[Finding], *, by: str = PERSON
) -> None:
    from app.models.review import Approval, ApprovedFinding

    approval = Approval(package_revision_id=revision.id, approved_by=by)
    session.add(approval)
    session.flush()
    session.add_all(
        ApprovedFinding(
            approval_id=approval.id, finding_id=finding.id, package_revision_id=revision.id
        )
        for finding in findings
    )
    session.flush()


def _failed_ids(report: object) -> set[object]:
    return {
        finding
        for findings in report.recurring_failures.values()  # type: ignore[attr-defined]
        for finding in findings
    }


def test_an_unconfirmed_fail_is_not_a_vendor_pattern_when_only_confirmed_ones_count(
    sessions: sessionmaker[Session],
) -> None:
    """A FAIL nobody looked at may rest on a reading the AI got wrong: it is the AI's mistake as
    likely as the vendor's, so the person-confirmed report leaves it out. The default report is
    unchanged."""
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=3))

    with unit_of_work(sessions) as session:
        confirmed = vendor_patterns(
            session, VENDOR, WINDOW, now=NOW, confirmed_only=True, reviewers=LIST
        )
        every = vendor_patterns(session, VENDOR, WINDOW, now=NOW)

    assert confirmed.recurring_failures == {}
    assert confirmed.confirmed_only is True
    assert sum(len(v) for v in every.recurring_failures.values()) == 1
    assert every.confirmed_only is False


def test_a_fail_a_person_confirmed_or_signed_counts(sessions: sessionmaker[Session]) -> None:
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        confirmed = _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=4))
        _act(session, confirmed, revision, ReviewActionKind.CONFIRM, NOW - timedelta(days=3))
        signed = _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=4))
        _act(session, signed, revision, ReviewActionKind.EXCEPT, NOW - timedelta(days=3))
        _sign(session, revision, [confirmed, signed])

    with unit_of_work(sessions) as session:
        report = vendor_patterns(
            session, VENDOR, WINDOW, now=NOW, confirmed_only=True, reviewers=LIST
        )

    assert _failed_ids(report) == {confirmed.id, signed.id}


def test_a_fail_the_person_dismissed_or_corrected_is_not_counted_even_when_signed(
    sessions: sessionmaker[Session],
) -> None:
    """Sign-off covers every finding of the revision, including the ones the reviewer dismissed:
    signing is not confirming those. The newest decision counts: a later dismiss replaces an
    earlier confirm."""
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        dismissed = _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=4))
        _act(session, dismissed, revision, ReviewActionKind.CONFIRM, NOW - timedelta(days=3))
        _act(session, dismissed, revision, ReviewActionKind.DISMISS, NOW - timedelta(days=2))
        corrected = _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=4))
        _act(session, corrected, revision, ReviewActionKind.CORRECT, NOW - timedelta(days=3))
        _sign(session, revision, [dismissed, corrected])

    with unit_of_work(sessions) as session:
        report = vendor_patterns(
            session, VENDOR, WINDOW, now=NOW, confirmed_only=True, reviewers=LIST
        )

    assert report.recurring_failures == {}


def test_code_confirming_or_signing_a_fail_is_not_a_person_confirming_it(
    sessions: sessionmaker[Session],
) -> None:
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        failed = _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=4))
        _act(
            session,
            failed,
            revision,
            ReviewActionKind.CONFIRM,
            NOW - timedelta(days=3),
            actor=CODE_ACTOR,
        )
        _sign(session, revision, [failed], by=CODE_ACTOR)

    with unit_of_work(sessions) as session:
        report = vendor_patterns(
            session, VENDOR, WINDOW, now=NOW, confirmed_only=True, reviewers=LIST
        )

    assert report.recurring_failures == {}


@pytest.mark.parametrize("rerun_signed", [False, True])
def test_a_failure_counts_once_across_its_carried_copies(
    sessions: sessionmaker[Session], rerun_signed: bool
) -> None:
    """A re-run's FAIL that inherits the earlier confirm is a copy: the original FAIL counts once,
    and the copy is not a second vendor failure, even when the re-run's revision is signed."""
    from app.models.review import FindingDecisionCarryover

    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        original = _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=4))
        action = _act(
            session, original, revision, ReviewActionKind.CONFIRM, NOW - timedelta(days=3)
        )
        rerun = _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=2))
        session.add(
            FindingDecisionCarryover(
                package_revision_id=revision.id,
                new_finding_id=rerun.id,
                from_finding_id=original.id,
                review_action_id=action.id,
                action=ReviewActionKind.CONFIRM.value,
                matched_on_hash="a" * 64,
            )
        )
        session.flush()
        if rerun_signed:
            _sign(session, revision, [rerun])

    with unit_of_work(sessions) as session:
        report = vendor_patterns(
            session, VENDOR, WINDOW, now=NOW, confirmed_only=True, reviewers=LIST
        )

    assert _failed_ids(report) == {original.id}


def test_with_no_reviewer_list_no_fail_counts_and_the_report_says_why(
    sessions: sessionmaker[Session],
) -> None:
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        failed = _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=4))
        _act(session, failed, revision, ReviewActionKind.CONFIRM, NOW - timedelta(days=3))
        _sign(session, revision, [failed])

    with unit_of_work(sessions) as session:
        report = vendor_patterns(session, VENDOR, WINDOW, now=NOW, confirmed_only=True)

    assert report.recurring_failures == {}
    assert report.not_counted is not None and "reviewer list" in report.not_counted


def test_a_name_off_the_reviewer_list_confirming_or_signing_does_not_count(
    sessions: sessionmaker[Session],
) -> None:
    """Most "person" rows in our copies were written by test scripts."""
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        failed = _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=4))
        _act(
            session,
            failed,
            revision,
            ReviewActionKind.CONFIRM,
            NOW - timedelta(days=3),
            actor=STRANGER,
        )
        _sign(session, revision, [failed], by=STRANGER)

    with unit_of_work(sessions) as session:
        report = vendor_patterns(
            session, VENDOR, WINDOW, now=NOW, confirmed_only=True, reviewers=LIST
        )

    assert report.recurring_failures == {}
    assert report.not_counted is None


def test_at_the_same_instant_a_dismiss_wins_over_a_confirm(
    sessions: sessionmaker[Session],
) -> None:
    """A tie is decided the cautious way, never by which id happens to sort last."""
    when = NOW - timedelta(days=3)
    with unit_of_work(sessions) as session:
        revision = _revision(session, VENDOR)
        snapshot = _snapshot(session)
        failed = _finding(session, revision, snapshot, Outcome.FAIL, NOW - timedelta(days=4))
        _act(session, failed, revision, ReviewActionKind.DISMISS, when)
        _act(session, failed, revision, ReviewActionKind.CONFIRM, when)
        _sign(session, revision, [failed])

    with unit_of_work(sessions) as session:
        report = vendor_patterns(
            session, VENDOR, WINDOW, now=NOW, confirmed_only=True, reviewers=LIST
        )

    assert report.recurring_failures == {}
