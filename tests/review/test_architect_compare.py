"""Compared with the architect: what code decides for each stored pairing, and a person's answers (#1418).

Verification for: `app/review/architect_compare.py`, `app/models/architect_compare.py` (migration
0102), the W1 `architect_compare` field and the sign-off readiness. Synthetic rows only: the slot
readings come from `tests/api/test_slot_rows._package_rows` (pages 612 x 792 pt, read at 150 dpi),
and the compare records are written here by hand at the same spots, in page points.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import PackageRevision, Page
from app.models.architect_compare import ArchitectCompareAnswer, ArchitectCompareRecord
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from app.review.approval import approval_readiness
from app.review.architect_compare import (
    NOT_COMPARED_GROUP,
    CompareRefused,
    architect_compare_for_revision,
    record_compare_answers,
)
from tests.api.test_slot_rows import _package_rows, _run_current_checks
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

# Where the fixture's slots are read, in pixels at 150 dpi, and the same box in page points.
SLOT_PX = {"0": (100, 100, 140, 120), "overall": (300, 100, 340, 120)}


def _pt(px: tuple[int, int, int, int]) -> list[str]:
    return [str(Decimal(value * 72) / Decimal(150)) for value in px]


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


def _boxes(page_index: int, _row: int, slot: str) -> list[str]:
    x0, y0, x1, y1 = SLOT_PX[slot]
    return [f"slot-box:{x0},{y0},{x1},{y1}"]


def _compare_run(session: Session, revision_id: UUID) -> ExtractionRun:
    workflow = WorkflowRun(package_revision_id=revision_id, engine_run_id=str(uuid4()))
    session.add(workflow)
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task)
    session.flush()
    run = ExtractionRun(
        task_run_id=task.id,
        extractor="architect-compare",
        extractor_version="architect-compare-v1",
        config_hash="synthetic",
        dpi=150,
    )
    session.add(run)
    session.flush()
    return run


def _record(
    run: ExtractionRun,
    revision_id: UUID,
    page: Page,
    number: int,
    inches: int,
    vendor_label: list[str] | None,
    *,
    reason: str = "the only vendor vertical dimension line drawn at the same place and length",
) -> ArchitectCompareRecord:
    return ArchitectCompareRecord(
        extraction_run_id=run.id,
        package_revision_id=revision_id,
        page_id=page.id,
        architect_view=0,
        view_title=None,
        architect_number=number,
        architect_text=f'{inches}"',
        architect_numerator=inches,
        architect_denominator=1,
        axis="vertical",
        pairing="paired" if vendor_label is not None else "unpaired",
        reason=reason if vendor_label is not None else "the vendor draws no labelled line here",
        architect_box=["400", "400", "420", "500"],
        vendor_view=1 if vendor_label is not None else None,
        vendor_label_box=vendor_label,
        vendor_line_box=None if vendor_label is None else ["70", "40", "70", "60"],
        vendor_drawn=None if vendor_label is None else str(inches),
        paired_by=None if vendor_label is None else "geometry",
    )


def _setup(session: Session) -> tuple[UUID, UUID, list[ArchitectCompareRecord]]:
    """Page 1: the slot reads 20 (sealed) and the overall 40 (sealed); page 2's slot is unsealed.
    Records: 20 at page 1's slot (same), 41 at its overall (different), one unpaired, one at a
    spot no reading covers (unread), and one at page 2's unsealed slot (unread)."""
    _project, package_id, _anchors = _package_rows(session, slot_flags=_boxes, unsealed_page=1)
    revision = session.scalars(select(PackageRevision).filter_by(package_id=package_id)).one()
    pages = {page.index: page for page in session.scalars(select(Page))}
    run = _compare_run(session, revision.id)
    records = [
        _record(run, revision.id, pages[0], 1, 20, _pt(SLOT_PX["0"])),
        _record(run, revision.id, pages[0], 2, 41, _pt(SLOT_PX["overall"])),
        _record(run, revision.id, pages[0], 3, 30, None),
        _record(run, revision.id, pages[0], 4, 12, ["500", "700", "520", "710"]),
        _record(run, revision.id, pages[1], 1, 21, _pt(SLOT_PX["0"])),
    ]
    session.add_all(records)
    session.flush()
    return package_id, revision.id, records


def test_every_architect_dimension_is_listed_once_with_what_code_decided(
    session: Session,
) -> None:
    _package, revision_id, records = _setup(session)

    items, counts = architect_compare_for_revision(session, revision_id)

    assert sorted(item.key for item in items) == sorted(f"arch:{r.id}" for r in records)
    by_key = {item.key: item for item in items}
    results = [by_key[f"arch:{r.id}"].result for r in records]
    assert results == ["same", "different", "unpaired", "unread", "unread"]
    different = by_key[f"arch:{records[1].id}"]
    assert (different.architect_inches, different.vendor_inches) == ("41", "40")
    assert different.clouded and different.needs_person and different.status == "open"
    assert different.actions == ["agree", "not_this"]
    assert different.vendor_ref is not None and different.vendor_ref.page_number == 1
    assert "agree" in by_key[f"arch:{records[4].id}"].why or "did not agree" in (
        by_key[f"arch:{records[4].id}"].why
    )
    assert by_key[f"arch:{records[0].id}"].status == "done"
    assert (counts.same, counts.different, counts.unpaired, counts.unread) == (1, 1, 1, 2)
    assert counts.needs_person == 4


def test_a_number_a_published_rule_already_checked_shows_that_rules_result(
    session: Session, tmp_path: Path
) -> None:
    package_id, revision_id, records = _setup(session)
    _run_current_checks(session, package_id, tmp_path)

    items, counts = architect_compare_for_revision(session, revision_id)

    by_key = {item.key: item for item in items}
    routed = by_key[f"arch:{records[0].id}"]
    assert routed.result == "rule"
    assert routed.rule_id is not None and routed.finding_id is not None
    assert by_key[f"arch:{records[1].id}"].result == "rule", "the row's width rule covers it too"
    assert counts.rule == 2


def test_a_reading_not_positively_the_vendors_ink_is_never_used(session: Session) -> None:
    _project, package_id, _anchors = _package_rows(
        session,
        slot_flags=lambda page, row, slot: [*_boxes(page, row, slot), "-ink:vendor", "ink:gv"],
    )
    revision = session.scalars(select(PackageRevision).filter_by(package_id=package_id)).one()
    page = session.scalars(select(Page).filter_by(index=0)).first()
    assert page is not None
    run = _compare_run(session, revision.id)
    record = _record(run, revision.id, page, 1, 20, _pt(SLOT_PX["0"]))
    session.add(record)
    session.flush()

    (item,), _counts = architect_compare_for_revision(session, revision.id)

    assert item.result == "unread"
    assert "vendor's own ink" in item.why
    assert item.vendor_inches is None


def test_a_persons_answer_is_its_own_row_and_sign_off_waits_until_each_is_answered(
    session: Session,
) -> None:
    _package, revision_id, records = _setup(session)
    reason = approval_readiness(session, revision_id).reason or ""
    # The fixture's own results block first; the compare is counted in the readiness helper.
    from app.review.approval import _compare_open

    assert _compare_open(session, revision_id) == 4

    assert (
        record_compare_answers(
            session,
            revision_id=revision_id,
            key=f"arch:{records[1].id}",
            answer="agree",
            actor="tester",
        )
        == 1
    )
    with pytest.raises(CompareRefused) as wrong_kind:
        record_compare_answers(
            session,
            revision_id=revision_id,
            key=f"arch:{records[2].id}",
            answer="agree",
            actor="tester",
        )
    assert wrong_kind.value.status == 422
    with pytest.raises(CompareRefused) as stale:
        record_compare_answers(
            session,
            revision_id=revision_id,
            key=NOT_COMPARED_GROUP,
            answer="clear_not_compared",
            actor="tester",
            expected_keys=[f"arch:{records[2].id}"],
        )
    assert stale.value.status == 409
    open_keys = [f"arch:{r.id}" for r in records[2:]]
    assert (
        record_compare_answers(
            session,
            revision_id=revision_id,
            key=NOT_COMPARED_GROUP,
            answer="clear_not_compared",
            actor="tester",
            expected_keys=open_keys,
        )
        == 3
    )

    items, counts = architect_compare_for_revision(session, revision_id)
    by_key = {item.key: item for item in items}
    agreed = by_key[f"arch:{records[1].id}"]
    assert (agreed.answer, agreed.answered_by, agreed.clouded) == ("vendor_wrong", "tester", True)
    assert counts.needs_person == 0
    assert _compare_open(session, revision_id) == 0
    assert "compared with the architect" not in (
        approval_readiness(session, revision_id).reason or ""
    )
    assert reason is not None
    # Append-only: the database refuses an edit of an answer or a record.
    session.commit()
    with pytest.raises(DBAPIError):
        session.execute(text("UPDATE architect_compare_answers SET answer = 'checked_right'"))
    session.rollback()
    with pytest.raises(DBAPIError):
        session.execute(text("DELETE FROM architect_compare_records"))
    session.rollback()
    assert session.scalars(select(ArchitectCompareAnswer)).all()


def test_sign_off_says_the_compared_numbers_need_a_person() -> None:
    from app.review.approval import _readiness_reason
    from app.review.no_countertop import PagesBlockingSignOff

    reason = _readiness_reason(
        has_findings=True,
        blocked=(),
        pages=PagesBlockingSignOff(),
        inputs_changed=False,
        revision=None,
        compare_open=2,
    )

    assert reason is not None and "2 numbers compared with the architect" in reason


def test_the_worker_stores_every_architect_dimension_once_and_never_twice(
    session: Session,
) -> None:
    from app.models import DocumentVersion
    from tests.extraction.architect.combined_sheet import combined_sheet
    from workflow.architect_compare import persist_architect_compare

    _project, package_id, _anchors = _package_rows(session)
    revision = session.scalars(select(PackageRevision).filter_by(package_id=package_id)).one()
    version = session.scalars(select(DocumentVersion)).one()
    task = _compare_run(session, revision.id).task_run_id

    first = persist_architect_compare(
        session,
        package_revision_id=revision.id,
        version_id=version.id,
        data=combined_sheet(),
        task_run_id=task,
        dpi=150,
    )
    again = persist_architect_compare(
        session,
        package_revision_id=revision.id,
        version_id=version.id,
        data=combined_sheet(),
        task_run_id=task,
        dpi=150,
    )

    assert (first, again) == (4, 0)
    items, _counts = architect_compare_for_revision(session, revision.id)
    assert len(items) == 4
    assert {item.result for item in items} <= {"unpaired", "unread", "same", "different", "rule"}


def test_the_review_list_and_the_answer_route_carry_the_compare(session: Session) -> None:
    from app.models import Package
    from tests.api.test_review_item_actions import _act, _client, _items

    package_id, _revision_id, records = _setup(session)
    package = session.get(Package, package_id)
    assert package is not None
    session.commit()
    client = _client(session, package.project_id)

    body = _items(client, package.project_id, package_id)
    assert len(body["architect_compare"]) == len(records)
    assert body["header"]["architect_compare_counts"]["different"] == 1
    different = f"arch:{records[1].id}"

    agreed = _act(client, package.project_id, package_id, different, "agree")
    assert agreed.status_code == 201, agreed.text
    again = _act(client, package.project_id, package_id, different, "agree")
    assert again.status_code == 200, again.text
    stale = _act(
        client,
        package.project_id,
        package_id,
        NOT_COMPARED_GROUP,
        "answer",
        {"answer": "clear_not_compared", "expected_keys": []},
    )
    assert stale.status_code == 409, stale.text
    by_hand = _act(
        client,
        package.project_id,
        package_id,
        f"arch:{records[2].id}",
        "answer",
        {"answer": "checked_wrong"},
    )
    assert by_hand.status_code == 201, by_hand.text
    listed = {e["key"]: e for e in by_hand.json()["review_items"]["architect_compare"]}
    assert listed[different]["answer"] == "vendor_wrong"
    assert listed[f"arch:{records[2].id}"]["status"] == "done"
