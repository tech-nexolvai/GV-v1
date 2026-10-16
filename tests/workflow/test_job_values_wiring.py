"""The job values reader wired into the worker (#1409). Mocked readers only: no model is called.

The reader runs once per revision in the match stage, only when `GV_JOB_VALUES_READER` is on; its
results are written append-only to `job_value_readings`, its calls through the call ledger, and a
request for a set already read reads only the job values. Synthetic values throughout.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models.job_values import JobValueReading
from app.models.outbox import OutboxEntry
from app.models.runs import ExtractionRun, ModelInvocation
from extraction.form_reader.bedrock import AttemptUsage
from extraction.geometry.rows import Box
from extraction.job_values.decide import JobValueResult, Outcome, ReaderReading
from extraction.job_values.read import JOB_VALUES_PROMPT_ID, JobValuesReading
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.workflow.test_depth_evidence import _Set
from tests.workflow.test_depth_reader import _claude_stages, _match_stage_run
from workflow.job_values import (
    JOB_VALUES_EXTRACTOR,
    READ_JOB_VALUES_WORKFLOW,
    latest_job_values,
    stored_answers,
)
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

PAIR = ("anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only")


def _results(version: str) -> dict[str, JobValueResult]:
    readings = (
        ReaderReading(PAIR[0], "printed", '3/4"'),
        ReaderReading(PAIR[1], "printed", '3/4"'),
    )
    return {
        "cabinet_depth": JobValueResult("cabinet_depth", Outcome.NOTHING, reasons=("not printed",)),
        "countertop_overhang": JobValueResult(
            "countertop_overhang",
            Outcome.SUGGESTION,
            value=Fraction(3, 2),
            page_index=0,
            document_version_id=version,
            box=Box(Decimal(40), Decimal(10), Decimal(80), Decimal(20)),
            readings=readings,
            source="ai-pair",
            reasons=("the readers disagree",),
        ),
        "backsplash_thickness": JobValueResult(
            "backsplash_thickness",
            Outcome.AUTO,
            value=Fraction(3, 4),
            page_index=0,
            document_version_id=version,
            box=Box(Decimal(100), Decimal(50), Decimal(140), Decimal(60)),
            readings=readings,
            source="ai-pair",
        ),
        "cabinet_side_thickness": JobValueResult(
            "cabinet_side_thickness", Outcome.REFUSED, reasons=("millimetres only",)
        ),
    }


def _fake_reader(monkeypatch: pytest.MonkeyPatch, sheet: _Set) -> list[dict[str, Any]]:
    """Code 'prepares' one drawing and the 'readers' answer once each; returns each call's kwargs."""
    import extraction.job_values.read as read_module

    calls: list[dict[str, Any]] = []
    version = str(sheet.page.document_version_id)

    def prepare(*_: object, **__: object) -> list[object]:
        return []

    def read(panels: object, **kwargs: Any) -> JobValuesReading:
        calls.append(kwargs)
        for model in kwargs["models"]:
            kwargs["record_attempt"](
                AttemptUsage(
                    model,
                    JOB_VALUES_PROMPT_ID,
                    JOB_VALUES_PROMPT_ID,
                    100,
                    10,
                    5,
                    False,
                    page_index=0,
                    raw_response_text="{}",
                )
            )
        return JobValuesReading(results=_results(version), asked=1, not_read=["p.2 drawing 1: x"])

    monkeypatch.setattr(read_module, "prepare_pdf_panels", prepare)
    monkeypatch.setattr(read_module, "read_job_values", read)
    return calls


def _on(store: LocalStore) -> DatabaseStages:
    stages = _claude_stages(store)
    stages._job_values_reader = True
    return stages


def test_match_reads_job_values_only_when_switched_on(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _Set(session, store)
    calls: list[UUID] = []

    def counted(self: DatabaseStages, session: Session, revision_id: UUID) -> dict[str, object]:
        calls.append(revision_id)
        return {"ran": True}

    monkeypatch.setattr(DatabaseStages, "_job_values_pass", counted)

    off = DatabaseStages(store).match(session, sheet.revision.id)
    assert calls == [] and "job_values" not in off

    on = DatabaseStages(store, job_values_reader=True).match(session, sheet.revision.id)
    assert calls == [sheet.revision.id] and on["job_values"] == {"ran": True}


def test_the_switch_must_be_a_yes_or_no() -> None:
    with pytest.raises(TypeError):
        DatabaseStages(job_values_reader="yes")  # type: ignore[arg-type]


def test_switched_on_without_the_claude_readers_it_reads_nothing_and_says_why(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)

    result = DatabaseStages(store, job_values_reader=True)._job_values_pass(
        session, sheet.revision.id
    )

    assert result["ran"] is False and "Claude" in str(result["reason"])
    assert session.scalar(select(func.count()).select_from(JobValueReading)) == 0


def test_a_reading_is_recorded_once_with_its_calls_budget_and_stored_answers(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _Set(session, store)
    _match_stage_run(session, sheet)
    calls = _fake_reader(monkeypatch, sheet)
    stages = _on(store)

    first = stages._job_values_pass(session, sheet.revision.id)

    assert first["ran"] is True, first
    assert first["values"] == {
        "cabinet_depth": "nothing",
        "countertop_overhang": "suggestion",
        "backsplash_thickness": "auto",
        "cabinet_side_thickness": "refused",
    }
    assert len(calls) == 1
    assert calls[0]["spend_guard"] is not None, "one spend guard, capped by the set's budget"
    assert calls[0]["stored"] is not None, "a stored answer is replayed, never paid twice"
    assert tuple(calls[0]["models"]) == PAIR
    run = session.get_one(ExtractionRun, UUID(str(first["run"])))
    assert run.extractor == JOB_VALUES_EXTRACTOR
    invocations = session.scalars(
        select(ModelInvocation).where(ModelInvocation.extraction_run_id == run.id)
    ).all()
    assert {row.model_id for row in invocations} == set(PAIR)
    rows = session.scalars(
        select(JobValueReading).where(JobValueReading.extraction_run_id == run.id)
    ).all()
    assert {row.setting: row.outcome for row in rows} == first["values"]
    assert all(row.not_read == ["p.2 drawing 1: x"] for row in rows), "never silent"

    # Once per revision: a second pass reads nothing again.
    again = stages._job_values_pass(session, sheet.revision.id)
    assert again["run"] == first["run"] and len(calls) == 1

    # A request of its own reads once more, and only once.
    asked = stages.read_job_values_again(session, sheet.revision.id, "request-1")
    assert asked["run"] != first["run"] and len(calls) == 2
    stages.read_job_values_again(session, sheet.revision.id, "request-1")
    assert len(calls) == 2


def test_a_request_is_refused_when_the_switch_is_off(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    with pytest.raises(ValueError, match="GV_JOB_VALUES_READER"):
        DatabaseStages(store).read_job_values_again(session, sheet.revision.id, "request-1")


def test_a_failed_reading_never_stops_match(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import extraction.job_values.read as read_module

    sheet = _Set(session, store)
    _match_stage_run(session, sheet)
    _fake_reader(monkeypatch, sheet)

    def broken(*_: object, **__: object) -> JobValuesReading:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(read_module, "read_job_values", broken)

    result = _on(store)._job_values_pass(session, sheet.revision.id)

    assert result["ran"] is False and "synthetic failure" in str(result["reason"])
    assert session.scalar(select(func.count()).select_from(JobValueReading)) == 0


def test_the_form_gets_the_newest_auto_and_suggested_values_with_where_to_ring_them(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _Set(session, store)
    _match_stage_run(session, sheet)
    _fake_reader(monkeypatch, sheet)
    _on(store)._job_values_pass(session, sheet.revision.id)

    found = latest_job_values(session, sheet.revision.id)

    assert set(found) == {"countertop_overhang", "backsplash_thickness"}, "nothing/refused: none"
    auto = found["backsplash_thickness"]
    assert (auto.value, auto.page, auto.auto, auto.source) == ('3/4"', 1, True, "two AIs")
    assert auto.page_id == str(sheet.page.id)
    # The box (100..140 x 50..60 pt) on the synthetic 400 x 100 pt page, in stored space (0-1).
    xs = [Decimal(point[0]) for point in auto.polygon]
    ys = [Decimal(point[1]) for point in auto.polygon]
    near = Decimal("0.005")  # one rendered pixel
    assert abs(min(xs) - Decimal("0.25")) < near and abs(max(xs) - Decimal("0.35")) < near
    assert abs(min(ys) - Decimal("0.5")) < near and abs(max(ys) - Decimal("0.6")) < near
    suggestion = found["countertop_overhang"]
    assert (suggestion.value, suggestion.auto) == ('1 1/2"', False), "a suggestion never pre-fills"


def test_a_stored_answer_to_the_identical_question_is_found(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    packet: dict[str, object] = {
        "reader": "job-values-reader",
        "document_version_id": str(sheet.page.document_version_id),
        "prompt_id": JOB_VALUES_PROMPT_ID,
        "images": {"picture_1": {"sha256": "a" * 64}},
    }
    session.add(
        ModelInvocation(
            extraction_run_id=sheet.run.id,
            model_id=PAIR[0],
            prompt_id=JOB_VALUES_PROMPT_ID,
            template_id=JOB_VALUES_PROMPT_ID,
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            outcome="ok",
            cost_micros=1,
            private_raw_response=json.dumps({"stored": True}),
            reader_question_packet=packet,
        )
    )
    session.flush()
    find = stored_answers(session)

    assert find(PAIR[0], packet) == json.dumps({"stored": True})
    assert find(PAIR[1], packet) is None
    other: Mapping[str, object] = {**packet, "images": {"picture_1": {"sha256": "b" * 64}}}
    assert find(PAIR[0], other) is None


def test_the_worker_consumes_a_request_and_the_script_queues_one(
    session: Session, store: LocalStore
) -> None:
    from scripts.job_values_queue import queue, revisions_to_queue
    from workflow.sutradhar import consume

    sheet = _Set(session, store)
    wanted = revisions_to_queue(session, [sheet.revision.package_id], [])
    assert wanted == [sheet.revision.id]
    [entry_id] = queue(session, wanted)
    session.flush()
    entry = session.get_one(OutboxEntry, entry_id)
    assert entry.workflow == READ_JOB_VALUES_WORKFLOW

    asked: list[tuple[UUID, str]] = []

    class _Stages:
        def read_job_values_again(
            self, session: Session, revision_id: UUID, request: str
        ) -> dict[str, object]:
            asked.append((revision_id, request))
            return {"ran": True, "run": "synthetic"}

    out = consume(
        session,
        workflow=READ_JOB_VALUES_WORKFLOW,
        payload=dict(entry.payload),
        idempotency_key="request-1",
        stages_factory=lambda **_: _Stages(),
    )
    assert out == {"ran": True, "run": "synthetic"}
    assert asked == [(sheet.revision.id, "request-1")]
