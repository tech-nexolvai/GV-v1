"""The AI page review as the review shows it, a person's answers, sign-off, and the old compare off (decision 2026-10-16).

Verification for `app/review/ai_review.py`, `app/models/ai_review.py` (migration 0103),
`workflow/ai_page_review.py` (with a mocked AI: no paid call), the W1 `ai_review` field, the W3
answers on `air:<id>` and sign-off readiness. Synthetic numbers only; pages come from
`tests/api/test_slot_rows._package_rows` (612 x 792 pt).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from fractions import Fraction
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import DocumentVersion, PackageRevision, Page
from app.models.ai_review import AiPageReview, AiPageReviewAnswer, AiPageReviewRow
from app.models.runs import ExtractionRun, ModelInvocation, TaskRun, WorkflowRun
from app.review.ai_review import (
    AiReviewRefused,
    ai_review_for_revision,
    ai_review_open_many,
    record_ai_review_answer,
)
from tests.api.test_slot_rows import _package_rows
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


def _task(session: Session, revision_id: UUID) -> TaskRun:
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
    return task


def _review(session: Session) -> tuple[UUID, UUID, list[AiPageReviewRow]]:
    """Page 1 reviewed: rows same, different, not_shown, unsure, unreadable; page 2 failed."""
    _project, package_id, _anchors = _package_rows(session)
    revision = session.scalars(select(PackageRevision).filter_by(package_id=package_id)).one()
    pages = {page.index: page for page in session.scalars(select(Page))}
    run = ExtractionRun(
        task_run_id=_task(session, revision.id).id,
        extractor="ai-page-review",
        extractor_version="ai-page-review-v1",
        config_hash="synthetic",
        dpi=150,
    )
    session.add(run)
    session.flush()
    review = AiPageReview(
        extraction_run_id=run.id,
        package_revision_id=revision.id,
        page_id=pages[0].id,
        status="done",
        model_id="anthropic.claude-opus-5-5",
        prompt_id="ai-page-review-v1",
        cost_micros=250_000,
        vendor_dpi=150,
        vendor_crop_px=[0, 0, 1000, 1000],
        dropped=[],
    )
    failed = AiPageReview(
        extraction_run_id=run.id,
        package_revision_id=revision.id,
        page_id=pages[1].id,
        status="failed",
        reason="the package's AI budget was reached",
        model_id="anthropic.claude-opus-5-5",
        prompt_id="ai-page-review-v1",
        dropped=[],
    )
    session.add_all([review, failed])
    session.flush()

    def row(position: int, result: str, architect: int, vendor: int | None) -> AiPageReviewRow:
        return AiPageReviewRow(
            review_id=review.id,
            package_revision_id=revision.id,
            position=position,
            architect_text=f'{architect}"',
            architect_numerator=architect,
            architect_denominator=1,
            architect_box=["400", "400", "420", "500"],
            measures="floor to counter top",
            vendor_text=None if vendor is None else f"{vendor * 25} [{vendor}]",
            vendor_numerator=vendor,
            vendor_denominator=None if vendor is None else 1,
            result=result,
            ai_result=result.upper(),
            why="synthetic",
            vendor_box_px=[100, 100, 140, 120] if vendor is not None else None,
        )

    rows = [
        row(1, "same", 31, 31),
        row(2, "different", 33, 34),
        row(3, "not_shown", 12, None),
        row(4, "unsure", 20, None),
        row(5, "unreadable", 8, None),
    ]
    session.add_all(rows)
    session.flush()
    return package_id, revision.id, rows


def test_each_page_and_row_is_shown_with_what_code_decided(session: Session) -> None:
    _package, revision_id, rows = _review(session)

    review = ai_review_for_revision(session, revision_id)

    assert [(p.page, p.status) for p in review.pages] == [(1, "done"), (2, "failed")]
    assert review.pages[1].reason == "the package's AI budget was reached"
    assert review.pages[0].cost_usd == "0.25"
    listed = review.pages[0].rows
    assert [r.key for r in listed] == [f"air:{r.id}" for r in rows]
    assert [r.result for r in listed] == ["same", "different", "not_shown", "unsure", "unreadable"]
    different = listed[1]
    assert (different.architect_inches, different.vendor_inches) == ("33", "34")
    # No person answers it (decision 2026-10-16): clouded by itself, nothing offered, nothing open.
    assert different.status == "done" and different.actions == []
    assert different.clouded
    assert different.vendor_box is not None and different.vendor_box.page_number == 1
    assert different.architect_ref is not None
    assert all(
        r.status == "done" and not r.actions and not r.clouded for r in listed if r is not different
    )
    counts = review.counts
    assert (counts.same, counts.different, counts.not_shown, counts.unsure, counts.unreadable) == (
        1,
        1,
        1,
        1,
        1,
    )
    assert counts.needs_person == 0
    assert ai_review_for_revision(session, uuid4()).pages == []


def test_old_answers_stay_append_only_and_change_nothing(session: Session) -> None:
    """The answers table and code stay (append-only, unused by the screen); a different row is
    clouded whatever was answered, and never waits for a person."""
    _package, revision_id, rows = _review(session)
    assert ai_review_open_many(session, [revision_id, uuid4()]) == {}

    with pytest.raises(AiReviewRefused) as refused:
        record_ai_review_answer(
            session, revision_id=revision_id, key=f"air:{rows[0].id}", answer="agree", actor="t"
        )
    assert refused.value.status == 422
    with pytest.raises(AiReviewRefused):
        record_ai_review_answer(
            session, revision_id=revision_id, key=f"air:{uuid4()}", answer="agree", actor="t"
        )

    key = f"air:{rows[1].id}"
    assert (
        record_ai_review_answer(
            session, revision_id=revision_id, key=key, answer="agree", actor="tester"
        )
        == 1
    )
    assert (
        record_ai_review_answer(
            session, revision_id=revision_id, key=key, answer="agree", actor="tester"
        )
        == 0
    )
    row = ai_review_for_revision(session, revision_id).pages[0].rows[1]
    assert (row.status, row.clouded, row.actions) == ("done", True, [])
    assert ai_review_open_many(session, [revision_id]) == {}

    assert (
        record_ai_review_answer(
            session, revision_id=revision_id, key=key, answer="not_this", actor="tester"
        )
        == 1
    )
    row = ai_review_for_revision(session, revision_id).pages[0].rows[1]
    assert (row.status, row.clouded, row.actions) == ("done", True, [])
    assert len(session.scalars(select(AiPageReviewAnswer)).all()) == 2

    session.commit()
    for statement in (
        "UPDATE ai_page_review_answers SET answer = 'vendor_wrong'",
        "DELETE FROM ai_page_review_rows",
        "UPDATE ai_page_reviews SET status = 'done'",
    ):
        with pytest.raises(DBAPIError):
            session.execute(text(statement))
        session.rollback()


def test_sign_off_waits_on_open_different_rows() -> None:
    from app.review.approval import _readiness_reason
    from app.review.no_countertop import PagesBlockingSignOff

    reason = _readiness_reason(
        has_findings=True,
        blocked=(),
        pages=PagesBlockingSignOff(),
        inputs_changed=False,
        revision=None,
        compare_open=1,
    )
    assert reason is not None and "1 number compared with the architect" in reason


def test_readiness_ignores_the_ai_review_different_rows(session: Session) -> None:
    from app.review.approval import _compare_open

    _package, revision_id, rows = _review(session)
    assert rows[1].result == "different"
    assert _compare_open(session, [revision_id]) == {}
    record_ai_review_answer(
        session, revision_id=revision_id, key=f"air:{rows[1].id}", answer="not_this", actor="t"
    )
    assert _compare_open(session, [revision_id]) == {}


def test_the_review_list_and_the_answer_route_carry_the_ai_review(session: Session) -> None:
    from app.models import Package
    from tests.api.test_review_item_actions import _act, _client, _items

    package_id, _revision_id, rows = _review(session)
    package = session.get(Package, package_id)
    assert package is not None
    session.commit()
    client = _client(session, package.project_id)

    body = _items(client, package.project_id, package_id)
    assert [p["page"] for p in body["ai_review"]["pages"]] == [1, 2]
    assert body["ai_review"]["pages"][1]["reason"]
    assert body["ai_review"]["counts"]["needs_person"] == 0
    assert body["ai_review"]["pages"][0]["rows"][1]["actions"] == []
    assert body["ai_review"]["pages"][0]["rows"][1]["clouded"] is True
    assert body["architect_compare"] == []
    key = f"air:{rows[1].id}"

    agreed = _act(client, package.project_id, package_id, key, "agree")
    assert agreed.status_code == 201, agreed.text
    again = _act(client, package.project_id, package_id, key, "agree")
    assert again.status_code == 200, again.text
    not_this = _act(client, package.project_id, package_id, key, "answer", {"answer": "not_this"})
    assert not_this.status_code == 201, not_this.text
    listed = not_this.json()["review_items"]["ai_review"]["pages"][0]["rows"][1]
    assert listed["answer"] == "not_this"
    refused = _act(client, package.project_id, package_id, f"air:{rows[0].id}", "agree")
    assert refused.status_code == 422, refused.text


def test_the_old_direct_compare_is_off_by_default_and_its_records_stay(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.architect_compare import ArchitectCompareRecord
    from app.review.architect_compare import architect_compare_for_revision
    from tests.review.test_architect_compare import _setup

    monkeypatch.delenv("GV_ARCH_COMPARE_RECORDS", raising=False)
    _package, revision_id, records = _setup(session)

    assert architect_compare_for_revision(session, revision_id)[0] == []
    from app.review.approval import _compare_open

    assert _compare_open(session, [revision_id]) == {}
    assert len(session.scalars(select(ArchitectCompareRecord)).all()) == len(records)

    monkeypatch.setenv("GV_ARCH_COMPARE_RECORDS", "1")
    assert len(architect_compare_for_revision(session, revision_id)[0]) == len(records)


# --- the worker's stage, with a mocked AI ---------------------------------------------------------


class _Client:
    def __init__(self, text_: str) -> None:
        self.text = text_
        self.calls = 0

    def converse(self, **_request: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": self.text}]}},
            "usage": {"inputTokens": 10_000, "outputTokens": 2_000},
        }


def _fake_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """One sheet (page 1) with two architect dimensions; pictures are tiny blank PNGs."""
    import io

    from PIL import Image

    import workflow.ai_page_review as module
    from extraction.ai_review.checks import ArchitectListed
    from extraction.ai_review.reader import AI_REVIEW_PROMPT, Half, ListedDimension, PageQuestion
    from extraction.geometry.rows import Box

    out = io.BytesIO()
    Image.new("RGB", (100, 80), (255, 255, 255)).save(out, format="PNG")
    box = Box(Decimal(10), Decimal(10), Decimal(30), Decimal(20))
    half = Half(out.getvalue(), 150, (0, 0, 500, 400), box)

    class _Page:
        page_index = 0

    def question(_data: bytes, page: Any, _settings: Any) -> PageQuestion:
        dims = (
            ListedDimension(ArchitectListed("A1", '33"', Fraction(33)), box),
            ListedDimension(ArchitectListed("A2", '12"', Fraction(12)), box),
        )
        return PageQuestion(page.page_index, "0" * 64, half, half, dims, AI_REVIEW_PROMPT + "A1/A2")

    monkeypatch.setattr("extraction.compare.pair.compare_pdf", lambda *_a, **_k: ((_Page(), ()),))
    monkeypatch.setattr(module, "page_question", question)


ANSWER = {
    "vendor_dimensions": [],
    "rows": [
        {
            "architect_id": "A1",
            "architect_text": '33"',
            "measures": "floor to counter top",
            "vendor_text": '34"',
            "result": "DIFFERENT",
            "why": "the vendor's counter is higher",
            "vendor_box": [100, 100, 200, 150],
        },
        {
            "architect_id": "A2",
            "architect_text": '12"',
            "measures": "wall to outlet",
            "vendor_text": "",
            "result": "NOT_SHOWN",
            "why": "no vendor dimension",
            "vendor_box": [],
        },
    ],
}


def _rates() -> Any:
    from app.runs.rates import load_model_rates

    return load_model_rates("deploy/model_rates.us-east-1.json")


def _run(session: Session, client: Any, guard: Any) -> tuple[UUID, dict[str, int], UUID]:
    from workflow.ai_page_review import persist_ai_page_review

    _project, package_id, _anchors = _package_rows(session)
    revision = session.scalars(select(PackageRevision).filter_by(package_id=package_id)).one()
    version = session.scalars(select(DocumentVersion)).one()
    task = _task(session, revision.id)
    counts = persist_ai_page_review(
        session,
        package_revision_id=revision.id,
        version_id=version.id,
        data=b"%PDF synthetic",
        task_run_id=task.id,
        dpi=150,
        client=client,
        rates=_rates(),
        guard=guard,
    )
    return revision.id, counts, task.id


def test_the_worker_reviews_a_sheet_records_its_cost_and_reuses_the_stored_answer(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workflow.ai_page_review import persist_ai_page_review, spend_guard

    _fake_question(monkeypatch)
    client = _Client(json.dumps(ANSWER))
    revision_id, counts, task_id = _run(session, client, spend_guard(_rates(), Decimal(6)))

    assert counts == {"done": 1} and client.calls == 1
    page = ai_review_for_revision(session, revision_id).pages[0]
    assert [r.result for r in page.rows] == ["different", "not_shown"]
    assert page.rows[0].vendor_inches == "34" and page.rows[0].vendor_box is not None
    (call,) = session.scalars(select(ModelInvocation)).all()
    assert call.cost_micros == 80_000  # 10k in at $4/M + 2k out at $20/M, from the price file
    assert page.cost_usd == "0.08"

    # The same task again stores nothing (a redelivered stage); a new run reuses the stored answer.
    version = session.scalars(select(DocumentVersion)).one()
    again = persist_ai_page_review(
        session,
        package_revision_id=revision_id,
        version_id=version.id,
        data=b"%PDF synthetic",
        task_run_id=task_id,
        dpi=150,
        client=client,
        rates=_rates(),
        guard=None,
    )
    assert again == {}
    fresh = persist_ai_page_review(
        session,
        package_revision_id=revision_id,
        version_id=version.id,
        data=b"%PDF synthetic",
        task_run_id=_task(session, revision_id).id,
        dpi=150,
        client=client,
        rates=_rates(),
        guard=None,
    )
    assert fresh == {"done": 1} and client.calls == 1
    assert sorted(c.cost_micros or 0 for c in session.scalars(select(ModelInvocation))) == [
        0,
        80_000,
    ]


def test_an_unparseable_answer_or_the_budget_marks_the_page_failed_never_stops(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workflow.ai_page_review import spend_guard

    _fake_question(monkeypatch)
    client = _Client("this is not json")
    revision_id, counts, _task_id = _run(session, client, spend_guard(_rates(), Decimal(6)))
    assert counts == {"failed": 1} and client.calls == 2
    (page,) = ai_review_for_revision(session, revision_id).pages
    assert page.status == "failed" and page.reason and "malformed" in page.reason
    assert page.rows == []


def test_a_budget_too_small_for_one_call_asks_nothing(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workflow.ai_page_review import spend_guard

    _fake_question(monkeypatch)
    client = _Client(json.dumps(ANSWER))
    revision_id, counts, _task_id = _run(session, client, spend_guard(_rates(), Decimal("0.01")))
    assert counts == {"failed": 1} and client.calls == 0
    (page,) = ai_review_for_revision(session, revision_id).pages
    assert page.reason == "the package's AI budget was reached"


def test_the_stage_is_off_unless_switched_on_and_never_raises(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workflow.ai_page_review import run_in_stage

    _project, package_id, _anchors = _package_rows(session)
    revision = session.scalars(select(PackageRevision).filter_by(package_id=package_id)).one()
    task = _task(session, revision.id)
    off = run_in_stage(
        session,
        package_revision_id=revision.id,
        documents=[(uuid4(), b"x")],
        task_run_id=task.id,
        dpi=150,
        environ={},
    )
    assert off == {}

    def broken(*_a: object, **_k: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("workflow.ai_page_review.persist_ai_page_review", broken)
    on = run_in_stage(
        session,
        package_revision_id=revision.id,
        documents=[(uuid4(), b"x")],
        task_run_id=task.id,
        dpi=150,
        environ={"GV_AI_PAGE_REVIEW": "1"},
        client=_Client("{}"),
    )
    assert on == {}
    assert session.scalars(select(AiPageReview)).all() == []
