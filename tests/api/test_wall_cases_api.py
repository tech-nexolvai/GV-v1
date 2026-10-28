"""Each wall choice's result on the review API, the results line, and the field cut (#1202).

Synthetic rows only (`tests/api/test_slot_rows.py`'s builder): page 1's row has pieces of 20" and
21" (41") and a 42" overall, so with GV's 1" field cut it is right with a wall at one end (42") and
wrong with no end walls (41") or walls at both ends (43").
"""

from __future__ import annotations

from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import ObservationCandidate, PackageRevision
from app.review.countertop_results import _countertop_results_for_revision
from tests.api.test_slot_rows import _package_rows, _reader_support, _run_current_checks
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _sealed(session: Session, **options: object) -> tuple[UUID, UUID, dict[int, UUID]]:
    project_id, package_id, anchors = _package_rows(
        session, piece_count=2, widths_add_up=True, **options  # type: ignore[arg-type]
    )
    for candidate in session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ).all():
        _reader_support(session, candidate)
    return project_id, package_id, anchors


def _result(session: Session, package_id: UUID, row_id: UUID):  # type: ignore[no-untyped-def]
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    results = _countertop_results_for_revision(session, package_id, revision)
    return next(item for item in results.items if item.row_id == row_id)


def test_a_fail_on_automatic_walls_stays_automatic(session: Session, tmp_path: Path) -> None:
    """A FAIL is a flag the reviewer can dismiss; only a PASS on vendor-read walls waits."""
    _project_id, package_id, anchors = _sealed(
        session, wall_source="code-ink-and-both-ais", overall_override=50
    )
    findings = _run_current_checks(session, package_id, tmp_path)
    finding = next(item for item in findings if item.scope_row_candidate_id == anchors[0])
    assert finding.outcome == "FAIL", finding.reason
    assert finding.variant == "back_left_right"


def test_an_engine_error_on_the_wall_cases_leaves_the_walls_needed_and_says_why(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.engine import EngineError

    def broken(*_args: object, **_kwargs: object) -> object:
        raise EngineError("synthetic engine refusal")

    monkeypatch.setattr("workflow.stages.wall_free_finding", broken)
    _project_id, package_id, anchors = _sealed(session, wall_source="readers", field_cuts=1)
    findings = _run_current_checks(session, package_id, tmp_path)
    finding = next(item for item in findings if item.scope_row_candidate_id == anchors[0])
    assert finding.outcome == "REVIEW_REQUIRED"
    assert "could not be worked out for every wall case: synthetic engine refusal" in (
        finding.reason or ""
    )


def _shown(value):  # type: ignore[no-untyped-def]
    return None if value is None else value.display


def test_wrong_for_every_wall_case_records_the_needed_range_and_the_closest_gap(
    session: Session, tmp_path: Path
) -> None:
    """#1329: the results table shows what was needed and how far off, with no walls set. Every
    number is the recorded cases' own: 41", 42" or 43" needed, the vendor wrote 50"."""
    _project_id, package_id, anchors = _sealed(
        session, wall_source="readers", field_cuts=1, overall_override=50
    )
    _run_current_checks(session, package_id, tmp_path)
    result = _result(session, package_id, anchors[0])
    assert result.expected_total is None and result.delta is None, "no one wall case applied"
    cases = result.wall_cases
    assert [(_shown(case.expected_total), _shown(case.difference)) for case in cases.cases] == [
        ('43"', '7"'),
        ('42"', '8"'),
        ('42"', '8"'),
        ('41"', '9"'),
        ('41"', '9"'),
    ]
    # The differences are the recorded numbers' own: the vendor's overall minus each case's needed.
    vendor = Fraction(int(cases.vendor_wrote.numerator), int(cases.vendor_wrote.denominator))
    for case in cases.cases:
        needed = Fraction(int(case.expected_total.numerator), int(case.expected_total.denominator))
        difference = Fraction(int(case.difference.numerator), int(case.difference.denominator))
        assert difference == vendor - needed
    assert (_shown(cases.needed_least), _shown(cases.needed_most)) == ('41"', '43"')
    assert _shown(cases.closest_gap) == '7"'


def test_mixed_wall_cases_record_each_cases_difference(session: Session, tmp_path: Path) -> None:
    """Right with a wall at one end (42"), wrong otherwise: each case keeps its own signed
    difference (short below zero), and the closest gap is nothing."""
    _project_id, package_id, anchors = _sealed(session, wall_source="readers", field_cuts=1)
    _run_current_checks(session, package_id, tmp_path)
    cases = _result(session, package_id, anchors[0]).wall_cases
    assert [(case.outcome, _shown(case.difference)) for case in cases.cases] == [
        ("FAIL", '-1"'),
        ("PASS", '0"'),
        ("PASS", '0"'),
        ("FAIL", '1"'),
        ("FAIL", '1"'),
    ]
    assert (_shown(cases.needed_least), _shown(cases.needed_most)) == ('41"', '43"')
    assert _shown(cases.closest_gap) == '0"'
