"""The draft cloud PDF's front page says what the review screen says (QA 2026-10-16, problem 4).

* The AI page review's differences are clouded by themselves and no person answers them (decision
  2026-10-16), so the front page lists them under "Compared with the architect (AI read)", with
  their count; "Needs a person" counts only real person items and does not claim nothing is
  waiting in words that contradict listed differences.
* The old row-by-row architect check's leftover "nothing was compared" row (CT-ARCH-WIDTH-001) is
  not an open item while the old compare is switched off (`GV_ARCH_COMPARE_RECORDS`, the default),
  as on the screen; switched on, it is counted as before.

Every value here is made up.
"""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime

import pytest
from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.models import CheckRun, Finding
from app.models.package import PackageRevision
from app.models.rules import RuleDefinition, RuleSnapshot
from reports.redline import AiReviewCounts, ArchitectCompare, ArchitectDifference
from storage.local import LocalStore
from tests.api.test_draft_cloud import _checked
from tests.api.test_draft_cloud import session as session_fixture
from tests.api.test_draft_cloud import store as store_fixture
from workflow.draft_cloud_render import OpenItems, _front_page, _live_rows, open_items

session = session_fixture
store = store_fixture

NOTHING_COMPARED = (
    "Compared row by row with the architect's drawing: no countertop row on this revision has an "
    "architect dimension paired one to one with it, so nothing was compared."
)


def _text(content: bytes) -> str:
    return " ".join(
        " ".join((page.extract_text() or "").split())
        for page in PdfReader(io.BytesIO(content)).pages
    )


def _ai_compare() -> ArchitectCompare:
    return ArchitectCompare(
        counts=AiReviewCounts(same=3, different=2, not_shown=1, unsure=0),
        different=(
            ArchitectDifference(
                architect_text='36"',
                vendor_text='35 1/2"',
                subject="Page 2",
                evidence_ref=None,
                unplaced_reason="made up",
                measures="overall width",
                ai_read=True,
            ),
            ArchitectDifference(
                architect_text='2\'-9" (33")',
                vendor_text='34"',
                subject="Page 4",
                evidence_ref=None,
                unplaced_reason="made up",
                measures="floor to top of counter",
                ai_read=True,
            ),
        ),
    )


def _front(architect: ArchitectCompare | None) -> str:
    return _text(
        _front_page(
            OpenItems(failures=(), needs_a_person=(), not_checked=(), not_checked_ids=frozenset()),
            revision_number=1,
            vendor="Made-up Vendor",
            requested_by="a reviewer",
            made_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
            results=5,
            architect=architect,
        )
    )


def test_the_front_page_lists_the_ai_differences_apart_from_needs_a_person() -> None:
    text = _front(_ai_compare())

    assert "Compared with the architect (AI read) (2 different)" in text
    assert "Compared with the architect (AI read): 3 same · 2 different · 1 not shown" in text
    assert 'A1 Needs: 36" (per architect: overall width) — Vendor drew: 35 1/2" — Page 2' in text
    assert 'A2 Needs: 2\'-9" (33") (per architect: floor to top of counter)' in text
    assert "Needs a person (0)" in text
    assert "Nothing is waiting for a person." not in text, "it would contradict the AI list"


def test_with_no_ai_differences_the_front_page_reads_as_before() -> None:
    text = _front(None)

    assert "Needs a person (0) None. Nothing is waiting for a person." in text
    assert "(AI read)" not in text


def _old_compare_row(db: Session, revision: PackageRevision) -> Finding:
    """The old architect check's "nothing was compared" result (NO_APPLICABLE_RULE)."""
    definition = RuleDefinition(rule_id="CT-ARCH-WIDTH-001")
    db.add(definition)
    db.flush()
    body = '{"id":"CT-ARCH-WIDTH-001"}'
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(body.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=body,
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=0,
    )
    db.add(snapshot)
    db.flush()
    run = CheckRun(
        package_revision_id=revision.id, rule_snapshot_id=snapshot.id, engine_version="verdict-1"
    )
    db.add(run)
    db.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome="NO_APPLICABLE_RULE",
        severity="FLAG",
        trace={},
        parameter_set_versions={},
        reason=NOTHING_COMPARED,
    )
    db.add(finding)
    db.commit()
    return finding


@pytest.mark.parametrize(("setting", "counted"), [("0", False), ("1", True)])
def test_the_old_compare_row_is_an_open_item_only_while_the_old_compare_is_on(
    session: Session,
    store: LocalStore,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    counted: bool,
) -> None:
    monkeypatch.setenv("GV_ARCH_COMPARE_RECORDS", setting)
    _, revision = _checked(session, store)
    _old_compare_row(session, revision)

    items = open_items(session, revision.id, _live_rows(session, revision.id))

    reasons = [item.reason for item in items.not_checked]
    assert (NOTHING_COMPARED in reasons) is counted
    assert len(items.failures) == 1, "the other results are sorted as before"
