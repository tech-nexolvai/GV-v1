"""The width check on a real database: pieces from the form, the company field cut, a project override.

Verification for #991 through `DatabaseStages.run_checks`, the path a review actually takes: the
rulebook published by `scripts/run_checks.py`, values stored as the Measure form stores them, and
the field cut resolved through the same layers (`declared_defaults` → company → project) a live run
uses. Made-up numbers with the shape of the first real verdict; no client value.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import yaml
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models import CheckRun, Finding, PackageRevision, RuleDefinition, RuleSnapshot
from app.verdicts.rulebook import from_row, snapshot_store
from rules.parameters import ParameterLayer
from rules.schema import Rule
from rules.snapshot import publish
from scripts.run_checks import _publish_rulebook
from tests.workflow.test_full_coverage import RULEBOOK, _revision, _store, _upgrade
from workflow.measurements import operands_for
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

#: 3 + 15 3/8 + 3/4 + 18 1/4 + 3 = 40 3/8; walls both ends add 1 + 1 = 42 3/8. The vendor wrote 41 1/4.
PIECES = ('3"', '15 3/8"', '3/4"', '18 1/4"', '3"')
WRONG_OVERALL = '41 1/4"'
WALLS_BOTH_ENDS = {"wall_config": "back_left_right"}


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    from app.db.session import session_factory

    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _width_finding(session: Session, revision: PackageRevision) -> Finding:
    rows = session.execute(
        select(Finding, RuleSnapshot)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .where(Finding.package_revision_id == revision.id)
    ).all()
    found = [finding for finding, snapshot in rows if from_row(snapshot).rule.id == "CT-WIDTH-001"]
    assert len(found) == 1
    return found[0]


def _check(session: Session, revision: PackageRevision) -> Finding:
    DatabaseStages(
        operands=operands_for(session, revision.id), discriminators=WALLS_BOTH_ENDS
    ).run_checks(session, revision.id)
    return _width_finding(session, revision)


def test_form_pieces_and_the_company_field_cut_fail_the_wrong_overall(session: Session) -> None:
    """No project value for the field cut: the company standard 1 in is used, and the finding says
    so. Output: FAIL, 41 1/4 against 42 3/8, with no piece's kind ever entered."""
    revision = _revision(session)
    _publish_rulebook(session)
    _store(
        session,
        revision,
        ParameterLayer.RUN,
        {"CT-WIDTH-001:countertop_width": WRONG_OVERALL, "CT-WIDTH-001:piece_widths": PIECES},
    )

    finding = _check(session, revision)

    assert finding.outcome == "FAIL"
    assert finding.reason == "41 1/4 in != 42 3/8 in"
    notes = " | ".join(finding.notes or ())
    assert "field_cut: company standard 1 in" in notes
    assert "field_cut = 1 in (global, Company standard" in notes


def test_a_project_field_cut_replaces_the_company_standard(session: Session) -> None:
    """A 5/8 in field cut for this job: 40 3/8 + 5/8 + 5/8 = 41 5/8. The finding names the override."""
    revision = _revision(session)
    _publish_rulebook(session)
    _store(session, revision, ParameterLayer.PROJECT, {"field_cut": '5/8"'})
    _store(
        session,
        revision,
        ParameterLayer.RUN,
        {"CT-WIDTH-001:countertop_width": '41 5/8"', "CT-WIDTH-001:piece_widths": PIECES},
    )

    finding = _check(session, revision)

    assert finding.outcome == "PASS"
    notes = " | ".join(finding.notes or ())
    assert "field_cut: company standard" not in notes
    assert "field_cut overrides a company standard" in notes


def test_publishing_a_bumped_rule_adds_the_version_beside_the_old_one(session: Session) -> None:
    """A database holding 1.0.1 gets 1.1.0 as a further snapshot; 1.0.1 stays as it was."""
    current = Rule.model_validate(
        yaml.safe_load((RULEBOOK / "ct_width_001.yaml").read_text(encoding="utf-8"))
    )
    older = publish(current.model_copy(update={"version": "1.0.1"}))
    definition = RuleDefinition(rule_id=current.id)
    session.add(definition)
    session.flush()
    session.add(
        RuleSnapshot(
            rule_definition_id=definition.id,
            snapshot_id=older.snapshot_id,
            version="1.0.1",
            canonical_json=older.canonical_json,
            product_type=current.product_type.value,
            check_type=current.check_type.value,
            unconfirmed_tolerance_count=0,
        )
    )
    session.flush()

    _publish_rulebook(session)
    again = _publish_rulebook(session)

    store = snapshot_store(session)
    assert sorted(snapshot.version for snapshot in store.versions_of("CT-WIDTH-001")) == [
        "1.0.1",
        "1.1.0",
    ]
    latest = store.latest("CT-WIDTH-001")
    assert latest is not None and latest.version == current.version
    assert store.get(older.snapshot_id).version == "1.0.1"
    assert again == 0
