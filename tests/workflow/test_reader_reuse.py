"""A re-run reuses the readers' stored answers to identical questions (#1112).

Verification for `workflow/reader_reuse.py` and its wiring in `workflow/slot_reader.py` and
`workflow/stages.py`. The first run is read by fake Claude readers and recorded exactly as the
worker records it; later runs are read with readers that would answer differently, so any question
asked again shows. Synthetic sheets and values only; no network.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.config import Settings
from evidence.coordinates import PageTransform
from extraction.form_reader.bedrock import AttemptUsage
from extraction.slot_reader.bedrock import CropJob, replay_stored_answer
from tests.workflow.test_slot_reader import (
    DPI,
    FRACTION_ENV,
    OPUS,
    SONNET,
    TEXTS,
    FakeReaders,
    _scaffold,
    claude_crops_to_texts,
    claude_runtime,
    named_sheet,
    runtime,
    slot_page,
)
from workflow.reader_reuse import StoredReaderAnswers, question_identity
from workflow.slot_reader import (
    PageSlotResult,
    SlotPage,
    SlotReaderRuntime,
    configured_slot_reader,
    read_slot_pages,
)

pytest_plugins = ("tests.app.postgres_fixture",)

TRANSFORM = PageTransform(
    dpi=DPI,
    rotation=0,
    media_box=(Decimal(0), Decimal(0), Decimal(612), Decimal(792)),
    crop_box=(Decimal(0), Decimal(0), Decimal(612), Decimal(792)),
)
#: Made-up prices, so a real call has a cost a reused answer must not add to.
PRICE = SimpleNamespace(input_per_1k_tokens=Decimal("0.01"), output_per_1k_tokens=Decimal("0.05"))
OTHER_TEXTS: Mapping[int | None, str] = {0: '7"', 1: '9"', 2: '7"', None: '23"'}
NEW_SONNET = "anthropic.claude-sonnet-9-9"


class PricedRates:
    def rate_for(self, model_id: str) -> object | None:
        return PRICE


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: BytesIO, *, content_type: str) -> SimpleNamespace:
        content = data.read()
        self.objects[key] = content
        return SimpleNamespace(sha256=hashlib.sha256(content).hexdigest())


@pytest.fixture
def session(postgres_engine: Any) -> Iterator[Any]:
    from alembic import command
    from app.db.session import session_factory
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _page(version_id: UUID, page_id: UUID, sheet: bytes | None = None) -> SlotPage:
    return replace(
        slot_page(named_sheet() if sheet is None else sheet),
        page_id=page_id,
        document_version_id=version_id,
        transform=TRANSFORM,
    )


def _readers(page: SlotPage, texts: Mapping[int | None, str] = TEXTS, **kwargs: Any) -> FakeReaders:
    lookup = claude_crops_to_texts(page, texts)
    return FakeReaders(lambda _model, png: lookup.get(png, '2"'), **kwargs)


def _different_readers(page: SlotPage) -> FakeReaders:
    """Readers whose every answer differs from the first run's: any question asked shows."""
    return _readers(
        page,
        OTHER_TEXTS,
        walls=lambda _model: {"left": "yes", "right": "yes", "behind": "yes", "view": "plan"},
        row=lambda _model: {"row": 0, "why": "no countertop row"},
        counter_break=lambda _model: {
            "contains_tall_appliance": True,
            "why": "a tall unit is drawn",
        },
    )


def _calls(readers: FakeReaders) -> int:
    return (
        len(readers.requests)
        + len(readers.wall_requests)
        + len(readers.row_requests)
        + len(readers.counter_break_requests)
    )


def _runtime(readers: FakeReaders, **changes: Any) -> SlotReaderRuntime:
    second = changes.pop("second_reader", SONNET)
    configured = claude_runtime(readers, question_packets=True, **changes)
    return replace(
        configured,
        form=replace(
            configured.form,
            reader_ids=(OPUS, second),
            calls_per_minute={OPUS: 6000, second: 6000},
            rates=PricedRates(),  # type: ignore[arg-type]
        ),
    )


def _new_run(session: Any, revision_id: UUID) -> Any:
    """Another extraction run of the same drawing, under `revision_id` (a re-run)."""
    from app.models.runs import ExtractionRun, TaskRun, WorkflowRun

    workflow_run = WorkflowRun(package_revision_id=revision_id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    task_run = TaskRun(
        workflow_run_id=workflow_run.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task_run)
    session.flush()
    run = ExtractionRun(
        task_run_id=task_run.id,
        extractor="extraction.form_reader",
        extractor_version="slot-reader-v1-ink-v1",
        config_hash="test",
    )
    session.add(run)
    session.flush()
    return run


def _second_revision(session: Any, version: Any) -> UUID:
    """A later revision of the same package that carries the same drawing (ADR-0018)."""
    from app.models import Document, PackageRevision, PackageRevisionDocument, PackageState

    document = session.get(Document, version.document_id)
    revision = PackageRevision(
        package_id=document.package_id, revision_number=2, state=PackageState.NEEDS_INPUT
    )
    session.add(revision)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision.id,
            package_id=document.package_id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    session.flush()
    return revision.id


def _read_and_record(
    session: Any,
    page: SlotPage,
    readers: FakeReaders,
    run: Any,
    *,
    stored: bool = True,
    **changes: Any,
) -> tuple[PageSlotResult, list[AttemptUsage], Any]:
    """Read the page as the worker does and record every attempt with the worker's own code."""
    from workflow.stages import DatabaseStages, _SpendMeter

    configured = _runtime(readers, **changes)
    attempts: list[AttemptUsage] = []
    (result,) = read_slot_pages(
        [page],
        runtime=configured,
        record_attempt=attempts.append,
        store=MemoryStore(),  # type: ignore[arg-type]
        stored_answers=StoredReaderAnswers(session) if stored else None,
    )
    stages = object.__new__(DatabaseStages)
    stages._form_reader = configured.form
    stages._meter = _SpendMeter(cap_micros=10_000_000)
    stages._record_reader_attempts(session, run.id, attempts)
    session.flush()
    return result, attempts, stages._meter


def _readings(result: PageSlotResult) -> tuple[object, ...]:
    return (
        result.row_choice_number,
        result.row_choice_picks,
        tuple(item.outcome for item in result.slots),
        None if result.overall is None else result.overall.outcome,
        result.mapping,
        result.row_hold,
        result.check_hold,
        result.vetoed,
        None if result.walls is None else (result.walls.answers, result.walls.outcome),
    )


def _rows(session: Any, run: Any) -> list[Any]:
    from app.models.runs import ModelInvocation

    return list(
        session.scalars(select(ModelInvocation).where(ModelInvocation.extraction_run_id == run.id))
    )


def _packet_hash(packet: Mapping[str, object]) -> str:
    body = {k: v for k, v in packet.items() if k not in ("packet_sha256", "reused_from")}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------------------------
# The identical question: reused, free, recorded, and the same readings
# ---------------------------------------------------------------------------------------------


def test_a_rerun_of_identical_questions_makes_no_call_and_gives_identical_readings(
    session: Any,
) -> None:
    from app.budget.attribution import usage_by_package
    from app.budget.ceiling import recorded_usage

    first_revision, version, page_row, first_run = _scaffold(session)
    first_readers = _readers(_page(version.id, page_row.id))
    first, first_attempts, first_meter = _read_and_record(
        session, _page(version.id, page_row.id), first_readers, first_run
    )
    asked = _calls(first_readers)
    assert asked and first_meter.spent_micros > 0
    originals = {row.id: row for row in _rows(session, first_run)}
    assert all(row.outcome == "ok" for row in originals.values())

    # A later revision of the same drawing, read again by readers that would answer differently.
    second_revision = _second_revision(session, version)
    second_run = _new_run(session, second_revision)
    page = _page(version.id, page_row.id)
    second_readers = _different_readers(page)
    second, second_attempts, second_meter = _read_and_record(
        session, page, second_readers, second_run
    )

    assert _calls(second_readers) == 0, "no question is asked again"
    assert _readings(second) == _readings(first)
    assert second.mapping.proposals, "the reused answers still make the proposals"
    assert len(second_attempts) == len(first_attempts) == asked
    assert second_meter.spent_micros == 0 and second_meter.unpriced_calls == 0

    reused = _rows(session, second_run)
    assert len(reused) == asked
    new_candidates = {str(value) for value in second.owner_candidate_ids.values()}
    new_candidates |= {str(value) for value in second.row_candidate_ids}
    if second.wall_candidate_id is not None:
        new_candidates.add(str(second.wall_candidate_id))
    for row in reused:
        packet = row.reader_question_packet
        source = originals[UUID(packet["reused_from"])]
        assert (row.input_tokens, row.output_tokens, row.cost_micros) == (0, 0, 0)
        assert row.outcome == "ok" and row.private_raw_response == source.private_raw_response
        assert (row.model_id, row.prompt_id) == (source.model_id, source.prompt_id)
        # The packet is this run's own question (its fresh candidate ids), still hash-checked.
        assert set(packet["candidate_ids"]) <= new_candidates
        assert packet["packet_sha256"] == _packet_hash(packet)
        assert question_identity(packet) == question_identity(source.reader_question_packet)

    # Nothing counts a reused answer as a call or as spend.
    assert recorded_usage(session, second_revision).model_calls == 0
    assert recorded_usage(session, first_revision.id).model_calls == asked
    by_package = usage_by_package(session, timedelta(days=1), as_of=datetime.now(UTC))
    assert second_revision not in by_package
    assert by_package[first_revision.id].invocation_count == asked

    # A third run reuses again, and still points at the calls that were actually made.
    third_run = _new_run(session, second_revision)
    third_readers = _different_readers(page)
    third, _, _ = _read_and_record(session, page, third_readers, third_run)
    assert _calls(third_readers) == 0
    assert _readings(third) == _readings(first)
    assert {
        UUID(row.reader_question_packet["reused_from"]) for row in _rows(session, third_run)
    } <= set(originals)


def test_a_changed_picture_is_asked_again(session: Any) -> None:
    _revision, version, page_row, first_run = _scaffold(session)
    _read_and_record(
        session,
        _page(version.id, page_row.id),
        _readers(_page(version.id, page_row.id)),
        first_run,
    )
    from tests.extraction.slot_reader import sheets

    # The same document version and page, but every picture differs by one far-off word.
    changed = _page(
        version.id,
        page_row.id,
        named_sheet(sheets.text(110, 690, "NOTE")),
    )
    readers = _readers(changed)
    _result, attempts, _meter = _read_and_record(
        session, changed, readers, _new_run(session, _revision.id)
    )

    assert _calls(readers) == len(attempts) > 0
    assert all(attempt.reused_from is None for attempt in attempts)


def test_a_changed_prompt_id_asks_that_question_again_and_reuses_the_rest(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from extraction.slot_reader import bedrock
    from workflow import slot_reader

    revision, version, page_row, first_run = _scaffold(session)
    page = _page(version.id, page_row.id)
    first_readers = _readers(page)
    _read_and_record(session, page, first_readers, first_run)
    span_questions = len(first_readers.requests)
    assert span_questions

    # A reworded label question gets a new prompt id; its stored answers are no longer its own.
    monkeypatch.setattr(bedrock, "CLAUDE_SPAN_PROMPT_ID", "claude-slot-span-v99")
    monkeypatch.setattr(slot_reader, "CLAUDE_SPAN_PROMPT_ID", "claude-slot-span-v99")
    readers = _readers(page)
    _result, attempts, _meter = _read_and_record(
        session, page, readers, _new_run(session, revision.id)
    )

    assert len(readers.requests) == span_questions, "every label question is asked again"
    assert not readers.row_requests and not readers.wall_requests
    assert not readers.counter_break_requests, "the unchanged questions are reused"
    assert {a.prompt_id for a in attempts if a.reused_from is None} == {"claude-slot-span-v99"}


def test_a_changed_model_finds_no_stored_answer(session: Any) -> None:
    """The reader pair is fixed in code, so a changed model is checked where answers are found."""
    from extraction.slot_reader.bedrock import CLAUDE_SPAN_PROMPT_ID

    _revision, version, page_row, first_run = _scaffold(session)
    page = _page(version.id, page_row.id)
    _read_and_record(session, page, _readers(page), first_run)
    png = _tiny_png()
    spans = [
        row
        for row in _rows(session, first_run)
        if row.model_id == SONNET and row.prompt_id == CLAUDE_SPAN_PROMPT_ID
    ]
    assert spans

    def jobs(model: str) -> list[CropJob]:
        return [
            CropJob(
                f"q{index}",
                model,
                0,
                png,
                png,
                grounded_claude=True,
                question_packet=row.reader_question_packet,
            )
            for index, row in enumerate(spans)
        ]

    stored = StoredReaderAnswers(session)
    found = stored.find(jobs(SONNET), product=None)
    assert {answer.invocation_id for answer in found.values()} == {row.id for row in spans}
    assert stored.find(jobs(NEW_SONNET), product=None) == {}
    # Opus was shown the same questions: it gets its own answers, never Sonnet's.
    opus_found = stored.find(jobs(OPUS), product=None)
    opus_rows = {row.id: row.model_id for row in _rows(session, first_run)}
    assert opus_found and {opus_rows[a.invocation_id] for a in opus_found.values()} == {OPUS}


def test_a_changed_effort_is_asked_again(session: Any) -> None:
    revision, version, page_row, first_run = _scaffold(session)
    page = _page(version.id, page_row.id)
    first_readers = _readers(page)
    _read_and_record(session, page, first_readers, first_run)

    readers = _readers(page)
    _read_and_record(session, page, readers, _new_run(session, revision.id), claude_effort="max")

    assert _calls(readers) == _calls(first_readers)


# ---------------------------------------------------------------------------------------------
# Switched off, and answers that were never accepted
# ---------------------------------------------------------------------------------------------


def test_switched_off_every_question_is_asked_again(session: Any) -> None:
    revision, version, page_row, first_run = _scaffold(session)
    page = _page(version.id, page_row.id)
    first_readers = _readers(page)
    _read_and_record(session, page, first_readers, first_run)

    readers = _readers(page)
    _result, attempts, meter = _read_and_record(
        session, page, readers, _new_run(session, revision.id), reuse_answers=False
    )

    assert _calls(readers) == _calls(first_readers)
    assert all(attempt.reused_from is None for attempt in attempts)
    assert meter.spent_micros > 0


def test_the_setting_is_on_by_default_and_reaches_the_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "postgresql+psycopg://x@localhost/x"
    assert Settings(_env_file=None, database_url=url)  # type: ignore[call-arg].reader_reuse_answers is True
    monkeypatch.setenv("GV_READER_REUSE_ANSWERS", "false")
    assert Settings(_env_file=None, database_url=url)  # type: ignore[call-arg].reader_reuse_answers is False

    form = runtime(FakeReaders(lambda _m, _p: "")).form
    for value in (True, False):
        settings = SimpleNamespace(
            slot_reader_enabled=True, claude_reader_enabled=False, reader_reuse_answers=value
        )
        configured = configured_slot_reader(settings, form, environ=FRACTION_ENV)
        assert configured is not None and configured.reuse_answers is value


class SonnetSpansMalformed(FakeReaders):
    """Sonnet's every label answer is not JSON: rejected, re-asked once, rejected, abstained."""

    def _answer(self, **kwargs: Any) -> Mapping[str, Any]:
        before = len(self.requests)
        reply = super()._answer(**kwargs)
        if kwargs["modelId"] == SONNET and len(self.requests) > before:
            return {
                "output": {"message": {"content": [{"text": "the label says twelve"}]}},
                "usage": {"inputTokens": 10, "outputTokens": 5},
            }
        return reply


def test_a_rejected_or_failed_answer_is_never_reused(session: Any) -> None:
    from app.runs.invocations import record
    from extraction.models.invocations import InvocationRecord

    revision, version, page_row, first_run = _scaffold(session)
    page = _page(version.id, page_row.id)
    lookup = claude_crops_to_texts(page, TEXTS)
    first_readers = SonnetSpansMalformed(lambda _model, png: lookup.get(png, '2"'))
    _read_and_record(session, page, first_readers, first_run)
    rejected = [row for row in _rows(session, first_run) if row.outcome == "rejected"]
    assert rejected and all(row.private_raw_response for row in rejected)
    # The same questions also recorded as failed, timed-out and refused calls that kept a reply.
    for row, outcome in zip(rejected, ("failed", "timeout", "refused"), strict=False):
        record(
            session,
            InvocationRecord(
                extraction_run_id=first_run.id,
                model_id=row.model_id,
                prompt_id=row.prompt_id,
                template_id=row.template_id,
                crop_artifact_id=None,
                input_tokens=1,
                output_tokens=1,
                cost_micros=1,
                latency_ms=1,
                outcome=outcome,
                private_raw_response=json.dumps(
                    {
                        "text": '12"',
                        "stacked": False,
                        "combined": False,
                        "readable": True,
                        "no_dimension": False,
                        "belongs": "yes",
                    }
                ),
                reader_page_index=0,
                reader_attempt_number=1,
                reader_question_packet=row.reader_question_packet,
            ),
        )
    sonnet_spans = sum(1 for model, _png in first_readers.requests if model == SONNET) // 2

    readers = _readers(page)
    _result, attempts, _meter = _read_and_record(
        session, page, readers, _new_run(session, revision.id)
    )

    asked = [model for model, _png in readers.requests]
    assert asked and set(asked) == {SONNET} and len(asked) == sonnet_spans
    assert not readers.row_requests and not readers.wall_requests
    assert all(a.reused_from is not None for a in attempts if a.model_id == OPUS)


# ---------------------------------------------------------------------------------------------
# The parts: what makes two questions identical, and how a stored reply is read
# ---------------------------------------------------------------------------------------------


def _packet(**changes: Any) -> dict[str, object]:
    packet: dict[str, object] = {
        "question_id": "p0:s1:0",
        "document_version_id": "00000000-0000-0000-0000-000000000001",
        "page_index": 0,
        "source_page_sha256": "1" * 64,
        "prompt_id": "claude-slot-span-v3",
        "candidate_ids": [str(uuid4())],
        "page_transform": {"dpi": 150, "rotation": 0},
        "images": {
            "full_view": {"sha256": "a" * 64, "storage_key": "k/full"},
            "close_up": {"sha256": "b" * 64, "storage_key": "k/close"},
        },
        "effort": "high",
        "packet_sha256": "f" * 64,
    }
    packet.update(changes)
    return packet


def test_the_question_identity_ignores_only_what_each_run_mints_fresh() -> None:
    base = question_identity(_packet())
    assert base is not None
    assert question_identity(_packet(candidate_ids=[str(uuid4())], packet_sha256="e" * 64)) == base
    assert question_identity(_packet(reused_from=str(uuid4()))) == base
    moved = _packet()
    moved["images"] = {
        "full_view": {"sha256": "a" * 64, "storage_key": "elsewhere/full"},
        "close_up": {"sha256": "b" * 64, "storage_key": "elsewhere/close"},
    }
    assert question_identity(moved) == base
    for changed in (
        _packet(prompt_id="claude-slot-span-v4"),
        _packet(effort="max"),
        _packet(document_version_id="00000000-0000-0000-0000-000000000002"),
        _packet(page_index=1),
        _packet(question_id="p0:s2:0"),
        _packet(source_page_sha256="2" * 64),
        _packet(candidate_ids=[str(uuid4()), str(uuid4())]),
        _packet(
            images={
                "full_view": {"sha256": "a" * 64, "storage_key": None},
                "close_up": {"sha256": "c" * 64, "storage_key": None},
            }
        ),
        _packet(
            images={
                "full_view": {"sha256": "a" * 64, "storage_key": None},
                "close_up": {"sha256": "b" * 64, "storage_key": None},
                "upright_close_up": {"sha256": "d" * 64, "storage_key": None},
            }
        ),
    ):
        assert question_identity(changed) not in (None, base)


@pytest.mark.parametrize(
    "packet",
    [
        _packet(images={}),
        _packet(images={"close_up": {"storage_key": "k"}}),
        {key: value for key, value in _packet().items() if key != "document_version_id"},
        {key: value for key, value in _packet().items() if key != "prompt_id"},
    ],
)
def test_a_packet_that_cannot_be_compared_is_never_matched(packet: Mapping[str, object]) -> None:
    assert question_identity(packet) is None


def _tiny_png() -> bytes:
    from evidence.crop import encode_png

    return encode_png(2, 2, bytes(12))


def _jobs() -> Sequence[CropJob]:
    png = _tiny_png()
    return (
        CropJob("p0:s0:0", OPUS, 0, png, png, grounded_claude=True, question_packet=_packet()),
        CropJob(
            "p0:row-choice", OPUS, 0, png, row_question=True, candidate_count=2, question_packet={}
        ),
    )


def test_a_stored_reply_is_read_by_the_live_code_and_a_bad_one_is_asked_again() -> None:
    span, row = _jobs()
    answer = {
        "text": '12"',
        "stacked": False,
        "combined": True,
        "readable": True,
        "no_dimension": False,
        "belongs": "yes",
    }
    replayed = replay_stored_answer(span, json.dumps(answer), reused_from="x", max_tokens=400)
    assert replayed is not None
    reading, attempt = replayed
    assert reading.text == '12"' and reading.combined is True  # type: ignore[union-attr]
    assert attempt.reused_from == "x" and attempt.raw_response_text == json.dumps(answer)
    assert (attempt.input_tokens, attempt.output_tokens, attempt.latency_ms) == (0, 0, 0)
    assert attempt.attempt_number == 1 and not attempt.malformed

    assert replay_stored_answer(span, "twelve inches", reused_from="x", max_tokens=400) is None
    assert (
        replay_stored_answer(span, json.dumps({"text": '12"'}), reused_from="x", max_tokens=400)
        is None
    )
    # An answer the live code would refuse for this question — a row beyond the numbered boxes.
    out_of_range = json.dumps({"row": 3, "kind": "row", "also": [], "why": "the third box"})
    assert replay_stored_answer(row, out_of_range, reused_from="x", max_tokens=400) is None
    in_range = replay_stored_answer(
        row,
        json.dumps({"row": 2, "kind": "row", "also": [], "why": "the second box"}),
        reused_from="x",
        max_tokens=400,
    )
    assert in_range is not None and in_range[0].row == 2  # type: ignore[union-attr]
    # A reply in an older question's shape is not an answer to the live question, so it is asked
    # again: span v4 needs "yes"/"no"/"unsure" (#1110), row v3 needs its kind (#1108).
    old_span = json.dumps(answer | {"belongs": True})
    assert replay_stored_answer(span, old_span, reused_from="x", max_tokens=400) is None
    old_row = json.dumps({"row": 2, "why": "the second box"})
    assert replay_stored_answer(row, old_row, reused_from="x", max_tokens=400) is None
