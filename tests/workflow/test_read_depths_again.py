"""Read depths again: the depth and sink reader on a drawing set that was already read (#1308).

Verification for `workflow/depth_again.py`, `DatabaseStages.read_depths_again`, its outbox work in
`workflow/sutradhar.consume`, the parking of a refused request (`workflow/outbox.py`) and the
sign-off hold on a depth reading newer than the checks (`app/review/approval.py`).

The reading itself is made up (`tests/workflow/test_depth_reader._fake_reading`'s way): no picture
is drawn and no model is asked. Every client here refuses or records; none reaches a network.

Every value here is made up.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.evidence.confirm import confirm_candidate_type
from app.models import ObservationCandidate, OutboxEntry, PackageState
from app.models.runs import ExtractionRun, ModelInvocation, TaskRun
from app.models.verdicts import CheckRun
from extraction.depth.prompts import (
    VIEW_KIND_PROMPT,
    VIEW_KIND_PROMPT_ID,
    VIEW_KIND_SCHEMA,
    DepthQuestion,
)
from extraction.depth.read import Asked, DepthReading
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.extraction.slot_reader.test_bedrock import PNG
from tests.workflow.test_depth_evidence import _Set
from tests.workflow.test_depth_reader import _match_stage_run
from workflow.ai_budget import AiBudget
from workflow.depth_again import (
    AGAIN_MARK,
    BUDGET_USED_UP,
    CLOSED,
    MAX_TRIES,
    NEEDS_CLAUDE,
    NEEDS_REUSE,
    READ_DEPTHS_AGAIN_WORKFLOW,
    SWITCHED_OFF,
    UNPRICED,
    DepthsAgainRefused,
    SpendLimits,
    estimate,
    last_request,
    request_cap_usd,
    set_budget_left,
)
from workflow.depth_evidence import CLAUDE_PAIR, CONFIRM_ONLY, DEPTH_EXTRACTOR
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

ACTOR = "reviewer@example.com"
CT010 = "CT010"


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


# -- made-up clients and readings -------------------------------------------------------------------


class _Answering:
    """A client that answers every view-kind question "plan", and counts the calls it was asked."""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def for_current_thread(self) -> _Answering:
        return self

    def converse(self, **_: object) -> Mapping[str, Any]:
        with self._lock:
            self.calls += 1
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": json.dumps({"kind": "plan", "why": "made up"})}],
                }
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }


class _MustNotBeCalled:
    """A client for a run that must pay for nothing: any call is counted, and refused."""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def for_current_thread(self) -> _MustNotBeCalled:
        return self

    def converse(self, **_: object) -> Mapping[str, Any]:
        with self._lock:
            self.calls += 1
        raise AssertionError("a run that reuses stored answers made a paid call")


def _stages(
    store: LocalStore, clients: object, *, ceiling: Decimal | None = None
) -> DatabaseStages:
    """The Claude stages of `tests/workflow/test_depth_reader._claude_stages`, with `clients`."""
    from app.runs.rates import load_model_rates
    from tests.workflow.test_slot_reader import FRACTION_ENV
    from workflow.form_reader import FormReaderRuntime
    from workflow.slot_reader import (
        E2_CROP_SETTINGS,
        MEASURED_SETTINGS,
        SlotReaderRuntime,
        fraction_bar_from_environment,
    )

    form = FormReaderRuntime(
        reader_ids=CLAUDE_PAIR,
        clients=clients,  # type: ignore[arg-type]
        rates=load_model_rates(
            Path(__file__).resolve().parents[2] / "deploy" / "model_rates.us-east-1.json"
        ),
        calls_per_minute=dict.fromkeys(CLAUDE_PAIR, 6000),
        max_concurrent_calls=1,
        max_tokens=3000,
        max_throttle_retries=0,
        retry_backoff_seconds=0.001,
        prompt=None,  # type: ignore[arg-type]
    )
    slot = SlotReaderRuntime(
        form=form,
        crop_settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
        fraction_bar=fraction_bar_from_environment(FRACTION_ENV),
        allow_stacked=True,
        claude_row_reader=True,
        spend_cap_usd=ceiling,
    )
    return DatabaseStages(store, form_reader=form, slot_reader=slot, depth_reader=True)


def _read_set(session: Session, store: LocalStore) -> _Set:
    """A set whose reading handed over to the reviewer, with its match stage run recorded."""
    sheet = _Set(session, store)
    _match_stage_run(session, sheet)
    sheet.revision.state = PackageState.AWAITING_REVIEW.value
    session.flush()
    return sheet


def _view_kind_reading(monkeypatch: pytest.MonkeyPatch, sheet: _Set, views: int = 2) -> list[int]:
    """`read_depth` asks one real view-kind question per view, of both readers, with a real signed
    packet (so a stored answer can be found again), and reads nothing. Returns how often it ran."""
    import extraction.depth.read as read_module

    ran: list[int] = []

    def prepare(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(views=[])

    def read(pages: object, *, ask: Any, **_: object) -> DepthReading:
        ran.append(1)
        question = DepthQuestion(VIEW_KIND_PROMPT_ID, (PNG,), VIEW_KIND_PROMPT, VIEW_KIND_SCHEMA)
        page = SimpleNamespace(
            document_version_id=str(sheet.page.document_version_id),
            page_index=0,
            page_sha256=sheet.page.content_hash,
        )
        jobs = [
            Asked(
                f"p0:v1.{number}:kind",
                model,
                0,
                question,
                read_module.question_packet(page, f"p0:v1.{number}:kind", question, "high"),  # type: ignore[arg-type]
            )
            for number in range(1, views + 1)
            for model in CLAUDE_PAIR
        ]
        ask(jobs)
        return DepthReading(views=(), questions_asked=len(jobs))

    monkeypatch.setattr(read_module, "prepare_pdf_page", prepare)
    monkeypatch.setattr(read_module, "read_depth", read)
    return ran


def _again_runs(session: Session, sheet: _Set) -> list[ExtractionRun]:
    return list(
        session.scalars(
            select(ExtractionRun)
            .where(
                ExtractionRun.extractor == DEPTH_EXTRACTOR,
                ExtractionRun.config_hash.contains(AGAIN_MARK),
            )
            .order_by(ExtractionRun.created_at)
        )
    )


def _paid_calls(session: Session) -> int:
    """Calls a model was really asked: a reused answer's row says where it came from."""
    rows = session.scalars(select(ModelInvocation.reader_question_packet)).all()
    return sum(1 for packet in rows if isinstance(packet, dict) and "reused_from" not in packet)


# -- refused, with a plain reason ---------------------------------------------------------------------


def test_refused_with_a_plain_reason_when_the_switch_is_off(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _read_set(session, store)
    ran = _view_kind_reading(monkeypatch, sheet)

    with pytest.raises(DepthsAgainRefused) as refused:
        DatabaseStages(store).read_depths_again(session, sheet.revision.id, str(uuid4()))

    assert refused.value.plain_reason == SWITCHED_OFF
    assert "GV_DEPTH_READER" in SWITCHED_OFF
    assert ran == [] and _again_runs(session, sheet) == []


def test_refused_without_the_claude_readers(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _read_set(session, store)
    ran = _view_kind_reading(monkeypatch, sheet)

    with pytest.raises(DepthsAgainRefused) as refused:
        DatabaseStages(store, depth_reader=True).read_depths_again(
            session, sheet.revision.id, str(uuid4())
        )

    assert refused.value.plain_reason == NEEDS_CLAUDE and ran == []


def test_refused_without_stored_answers_reused(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    sheet = _read_set(session, store)
    ran = _view_kind_reading(monkeypatch, sheet)
    stages = _stages(store, _MustNotBeCalled())
    stages._slot_reader = replace(stages._slot_reader, reuse_answers=False)  # type: ignore[type-var]

    with pytest.raises(DepthsAgainRefused) as refused:
        stages.read_depths_again(session, sheet.revision.id, str(uuid4()))

    assert refused.value.plain_reason == NEEDS_REUSE and ran == []


@pytest.mark.parametrize("state", [PackageState.MATCHING, PackageState.RUNNING_CHECKS])
def test_refused_while_the_set_is_being_read_or_checked(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch, state: PackageState
) -> None:
    sheet = _read_set(session, store)
    sheet.revision.state = state.value
    session.flush()
    ran = _view_kind_reading(monkeypatch, sheet)

    with pytest.raises(DepthsAgainRefused) as refused:
        _stages(store, _MustNotBeCalled()).read_depths_again(
            session, sheet.revision.id, str(uuid4())
        )

    assert "being read or checked" in refused.value.plain_reason and ran == []


def test_a_refused_request_is_parked_with_its_reason_never_redelivered(
    session: Session, store: LocalStore, postgres_engine: Engine
) -> None:
    from workflow.outbox import OutboxDispatchError, dispatch_committed, enqueue

    factory = session_factory(postgres_engine)
    with factory() as setup:
        entry = enqueue(
            setup,
            workflow=READ_DEPTHS_AGAIN_WORKFLOW,
            payload={"package_revision_id": str(uuid4())},
        )
        setup.commit()

    def refuse(**_: object) -> None:
        raise DepthsAgainRefused(SWITCHED_OFF)

    with pytest.raises(OutboxDispatchError):
        dispatch_committed(factory, refuse)

    with factory() as check:
        row = check.get_one(OutboxEntry, entry)
        assert row.abandoned_at is not None and row.abandoned_reason == SWITCHED_OFF
        check.delete(row)
        check.commit()


# -- what it runs ---------------------------------------------------------------------------------------


def test_it_runs_bala_and_the_depth_reader_only_then_queues_the_checks(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _read_set(session, store)
    ran = _view_kind_reading(monkeypatch, sheet)
    session.add(
        CheckRun(
            package_revision_id=sheet.revision.id,
            rule_snapshot_id=_any_snapshot(session),
            engine_version="synthetic",
        )
    )
    session.flush()
    bala: list[UUID] = []
    real_bala = DatabaseStages._bala_items

    def counted_bala(self: DatabaseStages, session: Session, revision_id: UUID) -> object:
        bala.append(revision_id)
        return real_bala(self, session, revision_id)

    def never(*_: object, **__: object) -> object:
        raise AssertionError("reading depths again must never read the set again")

    monkeypatch.setattr(DatabaseStages, "_bala_items", counted_bala)
    for name in ("ingest", "extract_pages", "read_unread_pages", "_run_form_reader_for_document"):
        monkeypatch.setattr(DatabaseStages, name, never)
    clients = _Answering()
    request = str(uuid4())

    result = _stages(store, clients).read_depths_again(session, sheet.revision.id, request)

    assert bala == [sheet.revision.id] and ran == [1]
    assert result["ran"] is True and result["checks_queued"] is True
    (run,) = _again_runs(session, sheet)
    assert run.config_hash.endswith(f"{AGAIN_MARK}{request}")
    assert str(run.id) == result["depth"]["run"]  # type: ignore[index]
    queued = session.scalars(select(OutboxEntry).where(OutboxEntry.workflow == "run_checks")).all()
    assert [row.payload["package_revision_id"] for row in queued] == [str(sheet.revision.id)]
    # The package's state is left where it was: the reviewer still has it.
    assert sheet.revision.state == PackageState.AWAITING_REVIEW.value
    # The earlier reading is still there (append-only).
    assert session.get(ExtractionRun, sheet.run.id) is not None


def test_the_same_request_delivered_twice_reads_once(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _read_set(session, store)
    ran = _view_kind_reading(monkeypatch, sheet)
    stages = _stages(store, _Answering())
    request = str(uuid4())

    stages.read_depths_again(session, sheet.revision.id, request)
    again = stages.read_depths_again(session, sheet.revision.id, request)

    assert ran == [1] and len(_again_runs(session, sheet)) == 1
    assert "already recorded" in str(again["depth"]["reason"])  # type: ignore[index]


def test_a_second_request_reuses_every_stored_answer_and_pays_for_nothing(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _read_set(session, store)
    _view_kind_reading(monkeypatch, sheet, views=2)
    paying = _Answering()
    _stages(store, paying).read_depths_again(session, sheet.revision.id, str(uuid4()))
    assert paying.calls == 4
    paid_before = _paid_calls(session)

    free = _MustNotBeCalled()
    result = _stages(store, free).read_depths_again(session, sheet.revision.id, str(uuid4()))

    assert free.calls == 0, "a stored answer was paid for again"
    assert _paid_calls(session) == paid_before
    assert result["ran"] is True and len(_again_runs(session, sheet)) == 2
    second = _again_runs(session, sheet)[-1]
    reused = session.scalars(
        select(ModelInvocation).where(ModelInvocation.extraction_run_id == second.id)
    ).all()
    assert len(reused) == 4
    assert all("reused_from" in (row.reader_question_packet or {}) for row in reused)
    # The status says so, in plain words.
    told = last_request(session, sheet.revision.id)
    assert told is None  # no outbox row: these were called directly
    shown = estimate(session, sheet.revision.id)
    assert (shown.questions_low, shown.questions_high, shown.stored_answers) == (4, 4, 4)


def _spent(session: Session, run: ExtractionRun, micros: int | None) -> None:
    """A recorded call of the set that cost `micros` (`None`: a call with no price)."""
    session.add(
        ModelInvocation(
            extraction_run_id=run.id,
            model_id=CLAUDE_PAIR[0],
            prompt_id="claude-slot-span-v5",
            template_id="claude-slot-span-v5",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            outcome="ok",
            cost_micros=micros,
        )
    )
    session.flush()


def _spent_reading_the_set(session: Session, sheet: _Set, micros: int) -> None:
    """The set's own reading (not a depth reading) spent `micros`."""
    workflow_run = session.get_one(TaskRun, sheet.run.task_run_id).workflow_run_id
    ledger = TaskRun(
        workflow_run_id=workflow_run,
        idempotency_key=f"synthetic-{uuid4()}",
        task_type="extract_pages",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(ledger)
    session.flush()
    slot_run = ExtractionRun(
        task_run_id=ledger.id, extractor="synthetic", extractor_version="synthetic", config_hash="x"
    )
    session.add(slot_run)
    session.flush()
    _spent(session, slot_run, micros)


def test_a_used_up_budget_is_refused_before_anything_is_asked(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _read_set(session, store)
    # No depth reading yet, so every question would be new; and everything the set may spend is
    # spent already.
    _spent_reading_the_set(session, sheet, 50_000_000)
    session.delete(sheet.run)
    session.flush()
    ran = _view_kind_reading(monkeypatch, sheet, views=2)
    clients = _Answering()

    with pytest.raises(DepthsAgainRefused) as refused:
        _stages(store, clients).read_depths_again(session, sheet.revision.id, str(uuid4()))

    assert refused.value.plain_reason == BUDGET_USED_UP
    assert ran == [] and clients.calls == 0 and _again_runs(session, sheet) == []


def test_a_used_up_budget_still_reads_when_every_answer_is_stored_and_pays_nothing(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1317: the up-front refusal is only for a question with no stored answer. A set whose budget
    is used up may still read its depths again when every question has a stored answer: it costs
    nothing, so refusing it would only take the reading away."""
    sheet = _read_set(session, store)
    ran = _view_kind_reading(monkeypatch, sheet, views=2)
    paying = _Answering()
    _stages(store, paying).read_depths_again(session, sheet.revision.id, str(uuid4()))
    assert paying.calls == 4
    # Then everything the set may spend is spent, and every question has a stored answer.
    _spent_reading_the_set(session, sheet, 50_000_000)
    limits = SpendLimits(AiBudget(), fixed_usd=None, claude_ceiling_usd=None)
    assert request_cap_usd(session, sheet.revision.id, limits) == 0
    assert set_budget_left(session, sheet.revision.id, limits) == 0
    shown = estimate(session, sheet.revision.id)
    assert (shown.questions_high, shown.stored_answers) == (4, 4)
    paid_before = _paid_calls(session)

    free = _MustNotBeCalled()
    result = _stages(store, free).read_depths_again(session, sheet.revision.id, str(uuid4()))

    assert result["ran"] is True and ran == [1, 1]
    assert free.calls == 0 and _paid_calls(session) == paid_before
    assert len(_again_runs(session, sheet)) == 2


def test_an_unpriced_call_is_refused_before_anything_is_asked(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _read_set(session, store)
    _spent(session, sheet.run, None)
    ran = _view_kind_reading(monkeypatch, sheet)

    with pytest.raises(DepthsAgainRefused) as refused:
        _stages(store, _MustNotBeCalled()).read_depths_again(
            session, sheet.revision.id, str(uuid4())
        )

    assert refused.value.plain_reason == UNPRICED and ran == []


def test_a_review_signed_off_while_its_depths_are_read_keeps_nothing_of_it(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import extraction.depth.read as read_module

    sheet = _read_set(session, store)
    _view_kind_reading(monkeypatch, sheet, views=1)
    reading = read_module.read_depth

    def signed_off_meanwhile(*args: Any, **kwargs: Any) -> DepthReading:
        found = reading(*args, **kwargs)
        sheet.revision.state = PackageState.APPROVED.value
        session.flush()
        return found

    monkeypatch.setattr(read_module, "read_depth", signed_off_meanwhile)

    with pytest.raises(DepthsAgainRefused) as refused:
        _stages(store, _Answering()).read_depths_again(session, sheet.revision.id, str(uuid4()))

    assert refused.value.plain_reason == CLOSED
    assert not session.scalars(
        select(OutboxEntry).where(OutboxEntry.workflow == "run_checks")
    ).all()


def test_a_reading_stopped_by_the_cap_never_replaces_a_complete_earlier_one(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _read_set(session, store)
    link = sheet.link()
    first = sheet.reading(
        CT010,
        24,
        CONFIRM_ONLY,
        link=link,
        view="p0:v1.1",
        confirmed_by="drawn-length-only",
        answers=False,
    )
    confirm_candidate_type(session, candidate_id=first.id, semantic_type=CT010, confirmed_by=ACTOR)
    assert sheet.operands().operands["countertop_depth"].value.exact == 24  # type: ignore[union-attr]
    _view_kind_reading(monkeypatch, sheet, views=2)
    clients = _Answering()

    # A cent is left: not refused up front, but no question fits, so the reading stops at once.
    result = _stages(store, clients, ceiling=Decimal("0.01")).read_depths_again(
        session, sheet.revision.id, str(uuid4())
    )

    assert clients.calls == 0
    depth = result["depth"]
    assert isinstance(depth, dict) and depth["stopped"] == "budget"
    assert depth["kept_earlier"] == str(sheet.run.id)
    assert "AI budget" in str(result["reason"]) and "earlier complete reading" in str(
        result["reason"]
    )
    (run,) = _again_runs(session, sheet)
    outcomes = session.scalars(
        select(ModelInvocation.outcome).where(ModelInvocation.extraction_run_id == run.id)
    ).all()
    assert "budget_refused" in outcomes
    # The earlier reading and the person's confirmation are still what the check uses.
    found = sheet.operands()
    operand = found.operands["countertop_depth"]
    assert operand.status.value == "HUMAN_CONFIRMED" and operand.value.exact == 24  # type: ignore[union-attr]
    assert any("this earlier one is used" in note for note in found.notes)
    # And the estimate counts from the complete reading, never the stopped one.
    assert estimate(session, sheet.revision.id).basis.startswith("The last complete")


def test_a_run_with_no_countertop_after_bala_is_refused_with_why(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import workflow.depth_again as again_module
    import workflow.depth_reader as reader_module

    sheet = _read_set(session, store)
    ran = _view_kind_reading(monkeypatch, sheet)
    monkeypatch.setattr(again_module, "countertops_by_drawing", lambda *_: {})
    monkeypatch.setattr(reader_module, "countertops_by_drawing", lambda *_: {})

    with pytest.raises(DepthsAgainRefused) as refused:
        _stages(store, _MustNotBeCalled()).read_depths_again(
            session, sheet.revision.id, str(uuid4())
        )

    assert "no countertop" in refused.value.plain_reason.lower() and ran == []


# -- decisions carry over --------------------------------------------------------------------------------


def test_a_person_s_confirmation_carries_over_the_new_reading(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import workflow.depth_reader as reader_module
    from workflow.depth_reader import PersistReport

    sheet = _read_set(session, store)
    link = sheet.link()
    # A confirm-only reading needs no answers on record (and stored ones here would have no price).
    first = sheet.reading(
        CT010,
        24,
        CONFIRM_ONLY,
        link=link,
        view="p0:v1.1",
        confirmed_by="drawn-length-only",
        answers=False,
    )
    confirm_candidate_type(session, candidate_id=first.id, semantic_type=CT010, confirmed_by=ACTOR)
    _view_kind_reading(monkeypatch, sheet, views=1)

    def identical(session: Session, revision_id: UUID, run: ExtractionRun, *_: object, **__: object):  # type: ignore[no-untyped-def]
        # The same dimension read again with the same number, in the new reading.
        sheet.reading(
            CT010,
            24,
            CONFIRM_ONLY,
            link=link,
            view="p0:v1.1",
            confirmed_by="drawn-length-only",
            run=run,
            answers=False,
        )
        return PersistReport()

    monkeypatch.setattr(reader_module, "persist_depth_reading", identical)

    _stages(store, _Answering()).read_depths_again(session, sheet.revision.id, str(uuid4()))

    operand = sheet.operands().operands["countertop_depth"]
    assert operand.status.value == "HUMAN_CONFIRMED" and operand.value.exact == 24  # type: ignore[union-attr]
    # Nothing earlier was changed: the first reading and the person's decision are still there.
    assert session.get(ObservationCandidate, first.id) is not None
    assert len(_again_runs(session, sheet)) == 1


# -- sign-off waits for the checks to see it ------------------------------------------------------------


def test_a_depth_reading_newer_than_the_checks_holds_sign_off(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.approval import _revisions_with_unchecked_inputs

    sheet = _read_set(session, store)
    _view_kind_reading(monkeypatch, sheet)
    session.add(
        CheckRun(
            package_revision_id=sheet.revision.id,
            rule_snapshot_id=_any_snapshot(session),
            engine_version="synthetic",
        )
    )
    session.flush()
    assert _revisions_with_unchecked_inputs(session, [sheet.revision.id]) == set()

    _stages(store, _Answering()).read_depths_again(session, sheet.revision.id, str(uuid4()))

    assert _revisions_with_unchecked_inputs(session, [sheet.revision.id]) == {sheet.revision.id}


# -- the estimate before starting ------------------------------------------------------------------------


def test_the_estimate_before_any_depth_reading_is_per_countertop(
    session: Session, store: LocalStore
) -> None:
    sheet = _read_set(session, store)
    session.delete(sheet.run)
    session.flush()

    shown = estimate(session, sheet.revision.id)

    assert (shown.pages, shown.countertops) == (1, 1)
    assert (shown.questions_low, shown.questions_high, shown.stored_answers) == (6, 12, 0)
    assert "both readers" in shown.basis and shown.basis.startswith("At least 6 to 12")


def test_the_status_of_a_request_says_what_happened(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workflow.outbox import enqueue

    sheet = _read_set(session, store)
    _view_kind_reading(monkeypatch, sheet, views=1)
    waiting = enqueue(
        session,
        workflow=READ_DEPTHS_AGAIN_WORKFLOW,
        payload={"package_revision_id": str(sheet.revision.id)},
    )
    session.flush()
    told = last_request(session, sheet.revision.id)
    assert told is not None and told.state == "waiting"

    _stages(store, _Answering()).read_depths_again(session, sheet.revision.id, str(waiting))
    row = session.get_one(OutboxEntry, waiting)
    row.attempts, row.dispatched_at = 1, func.now()
    session.flush()

    told = last_request(session, sheet.revision.id)
    assert told is not None and told.state == "read"
    assert "2 questions" in told.sentence


class _SameSessionLedger:
    """The call ledger (`workflow.reading_watch.CallLedger`) written in the test's own session.

    The real ledger writes each paid call in a transaction of its own, which needs the workflow run
    committed; the test's session never commits. This one writes the same rows, under a ledger run
    of the stage's workflow run, when the stage asks which calls the ledger holds: so the stage
    writes the $0 twin of each paid call exactly as it does in a real run (#1328)."""

    def __init__(self, stages: DatabaseStages, session: Session, task_run_id: UUID) -> None:
        self._stages = stages
        self._session = session
        self._task_run_id = task_run_id
        self._attempts: list[Any] = []
        self._ledgered: dict[int, UUID] = {}
        self._lock = threading.Lock()

    def record(self, attempt: Any) -> None:
        with self._lock:
            self._attempts.append(attempt)

    def snapshot(self) -> tuple[Any, ...]:
        with self._lock:
            return tuple(self._attempts)

    def ledgered_in(self, session: Session) -> dict[int, UUID]:
        from app.runs.invocations import record as record_model_invocation
        from workflow.reading_watch import LEDGER_EXTRACTOR, LEDGER_TASK_TYPE, ledger_key_prefix

        paid = [
            (position, attempt)
            for position, attempt in enumerate(self.snapshot())
            if attempt.reused_from is None and position not in self._ledgered
        ]
        if paid:
            stage = session.get_one(TaskRun, self._task_run_id)
            task = TaskRun(
                workflow_run_id=stage.workflow_run_id,
                idempotency_key=f"{ledger_key_prefix(stage.idempotency_key)}{uuid4().hex}",
                task_type=LEDGER_TASK_TYPE,
                attempt=1,
                outcome="recorded",
            )
            session.add(task)
            session.flush()
            run = ExtractionRun(
                task_run_id=task.id,
                extractor=LEDGER_EXTRACTOR,
                extractor_version="call-ledger-v1",
                config_hash="calls-of:synthetic",
            )
            session.add(run)
            session.flush()
            for position, attempt in paid:
                row = record_model_invocation(
                    session, self._stages._reader_invocation(run.id, attempt)
                )
                self._ledgered[position] = row.id
        return dict(self._ledgered)


def _ledgered_stages(
    store: LocalStore, clients: object, monkeypatch: pytest.MonkeyPatch
) -> DatabaseStages:
    """`_stages` whose paid calls go through the call ledger, as in a real run."""
    stages = _stages(store, clients)

    def ledger(
        self: DatabaseStages, session: Session, task_run_id: UUID, *, reader_version: str
    ) -> _SameSessionLedger:
        del reader_version
        return _SameSessionLedger(self, session, task_run_id)

    monkeypatch.setattr(DatabaseStages, "_call_ledger", ledger)
    return stages


def _asked_through_the_outbox(
    session: Session, stages: DatabaseStages, sheet: _Set
) -> tuple[UUID, dict[str, object]]:
    """A request queued, then run by the worker and handed over, as the outbox does."""
    from workflow.outbox import enqueue

    request = enqueue(
        session,
        workflow=READ_DEPTHS_AGAIN_WORKFLOW,
        payload={"package_revision_id": str(sheet.revision.id)},
    )
    session.flush()
    result = stages.read_depths_again(session, sheet.revision.id, str(request))
    row = session.get_one(OutboxEntry, request)
    row.attempts, row.dispatched_at = 1, func.now()
    session.flush()
    return request, result


def _ledger_cost(session: Session, after: OutboxEntry) -> int:
    """What the call ledger's rows written since `after` was asked cost, in micros."""
    from workflow.reading_watch import LEDGER_EXTRACTOR

    return int(
        session.scalar(
            select(func.coalesce(func.sum(ModelInvocation.cost_micros), 0))
            .join(ExtractionRun, ExtractionRun.id == ModelInvocation.extraction_run_id)
            .where(
                ExtractionRun.extractor == LEDGER_EXTRACTOR,
                ModelInvocation.created_at >= after.created_at,
            )
        )
        or 0
    )


def test_the_status_counts_a_new_paid_call_as_paid_not_as_a_stored_answer(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1328: the call ledger writes each paid call at once and the stage then writes a $0 twin
    naming it, the same shape as a reused stored answer. The status counted every twin as reused:
    a run that paid for 29 of 79 questions said all 79 were answered at no cost."""
    sheet = _read_set(session, store)
    _view_kind_reading(monkeypatch, sheet, views=2)
    first_clients = _Answering()
    _asked_through_the_outbox(session, _ledgered_stages(store, first_clients, monkeypatch), sheet)
    assert first_clients.calls == 4
    (first,) = _again_runs(session, sheet)
    twins = session.scalars(
        select(ModelInvocation).where(ModelInvocation.extraction_run_id == first.id)
    ).all()
    assert len(twins) == 4 and all(row.cost_micros == 0 for row in twins)
    assert all("reused_from" in (row.reader_question_packet or {}) for row in twins)

    told = last_request(session, sheet.revision.id)
    assert told is not None and told.state == "read"
    assert "4 questions, 0 answered from stored answers at no cost" in told.sentence
    assert "4 asked of the AI readers and paid for" in told.sentence

    # A second request asks the same four questions and two new ones: four stored answers reused
    # at no cost, two new paid calls.
    _view_kind_reading(monkeypatch, sheet, views=3)
    second_clients = _Answering()
    request, _ = _asked_through_the_outbox(
        session, _ledgered_stages(store, second_clients, monkeypatch), sheet
    )
    assert second_clients.calls == 2
    cost = _ledger_cost(session, session.get_one(OutboxEntry, request))
    assert cost > 0

    told = last_request(session, sheet.revision.id)
    assert told is not None and told.state == "read"
    from workflow.depth_again import _dollars

    assert told.sentence == (
        "Depths read again: 6 questions, 4 answered from stored answers at no cost, "
        f"2 asked of the AI readers and paid for ({_dollars(cost)})."
    )


def test_each_new_call_is_paid_once_and_a_call_made_before_the_request_is_reused(
    session: Session, store: LocalStore
) -> None:
    """`_spent_by_request` by its rows: a stage-paid row, a twin of a new call, a second question
    answered by that same new call, a stored answer from before the request, and an attempt the
    budget refused before it was sent."""
    from datetime import timedelta

    from workflow.depth_again import _spent_by_request

    sheet = _read_set(session, store)
    asked_at = sheet.run.created_at

    def row(run: ExtractionRun, *, cost: int, made: object, **extra: Any) -> ModelInvocation:
        added = ModelInvocation(
            extraction_run_id=run.id,
            model_id=CLAUDE_PAIR[0],
            prompt_id="depth-view-kind-v1",
            template_id="depth-view-kind-v1",
            input_tokens=0 if cost == 0 else 10,
            output_tokens=0 if cost == 0 else 5,
            latency_ms=0,
            outcome=extra.pop("outcome", "ok"),
            cost_micros=cost,
            created_at=made,
            **extra,
        )
        session.add(added)
        session.flush()
        return added

    before = row(sheet.run, cost=40_000, made=asked_at - timedelta(hours=1))
    new_call = row(sheet.run, cost=25_000, made=asked_at + timedelta(seconds=5))
    reading = ExtractionRun(
        task_run_id=sheet.run.task_run_id,
        extractor=DEPTH_EXTRACTOR,
        extractor_version="synthetic",
        config_hash="synthetic;again=x",
    )
    session.add(reading)
    session.flush()
    later = asked_at + timedelta(seconds=10)

    def naming(target: ModelInvocation) -> dict[str, Any]:
        return {
            "reader_question_packet": {"packet_sha256": "a" * 64, "reused_from": str(target.id)}
        }

    row(reading, cost=30_000, made=later)  # a paid call the stage wrote itself
    row(reading, cost=0, made=later, **naming(new_call))  # the twin of a new call
    row(reading, cost=0, made=later, **naming(new_call))  # the same new call answers again
    row(reading, cost=0, made=later, **naming(before))  # a stored answer from before
    row(reading, cost=0, made=later, outcome="budget_refused", rejection_reason="SpendCapExceeded")

    spent = _spent_by_request(session, reading.id, since=asked_at)

    assert (spent.asked, spent.reused, spent.paid, spent.not_sent) == (5, 2, 2, 1)
    assert spent.paid_micros == 30_000 + 25_000


def test_a_request_that_paid_for_nothing_says_so() -> None:
    from workflow.depth_again import _counted, _dollars, _Spent

    assert _counted(_Spent(asked=4, reused=4, paid=0, paid_micros=0, not_sent=0)) == (
        "4 questions, 4 answered from stored answers at no cost"
    )
    assert _counted(_Spent(asked=79, reused=50, paid=29, paid_micros=290_108, not_sent=0)) == (
        "79 questions, 50 answered from stored answers at no cost, 29 asked of the AI readers "
        "and paid for ($0.29)"
    )
    assert _counted(_Spent(asked=3, reused=1, paid=1, paid_micros=150, not_sent=1)) == (
        "3 questions, 1 answered from stored answers at no cost, 1 asked of the AI readers and "
        "paid for (less than $0.01), 1 not asked"
    )
    assert _dollars(0) == "$0.00"


def test_both_money_numbers_after_a_paid_run(session: Session, store: LocalStore) -> None:
    """#1328: "budget left" showed the per-request cap, $1.50 again after $0.29 was spent. The
    request may still spend up to the ceiling; the drawing set has $0.29 less left."""
    from workflow.set_spend import revision_page_count

    sheet = _read_set(session, store)
    limits = SpendLimits(AiBudget(), fixed_usd=None, claude_ceiling_usd=Decimal("1.50"))
    whole = AiBudget().for_pages(revision_page_count(session, sheet.revision.id))
    assert whole > Decimal("1.79")
    assert request_cap_usd(session, sheet.revision.id, limits) == Decimal("1.50")
    assert set_budget_left(session, sheet.revision.id, limits) == whole

    _spent_reading_the_set(session, sheet, 290_108)

    assert request_cap_usd(session, sheet.revision.id, limits) == Decimal("1.50")
    assert set_budget_left(session, sheet.revision.id, limits) == (
        whole - Decimal("0.290108")
    ).quantize(Decimal("0.01"), rounding="ROUND_DOWN")


def _any_snapshot(session: Session) -> UUID:
    from tests.workflow.test_stages import _publish_rulebook

    _publish_rulebook(session)
    from app.models import RuleSnapshot

    return session.scalars(select(RuleSnapshot.id).limit(1)).one()


@pytest.mark.parametrize(
    "word", ["", " ", "true", "TRUE", "on", "yes", "1", "false", "off", "no", "0", "maybe"]
)
def test_the_api_reads_the_switch_exactly_as_the_worker_does(
    word: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import Settings
    from extraction.depth.read import depth_reader_enabled

    monkeypatch.setenv("GV_DEPTH_READER", word)
    try:
        worker: bool | str = depth_reader_enabled({"GV_DEPTH_READER": word})
    except ValueError:
        worker = "refused"
    try:
        api: bool | str = Settings(
            database_url="postgresql+psycopg://gv:gv@localhost:5433/gv"
        ).depth_reader
    except ValueError:
        api = "refused"
    assert api == worker


def test_a_request_tried_again_and_again_says_so_and_stops_blocking(
    session: Session, store: LocalStore
) -> None:
    from workflow.depth_again import request_waiting
    from workflow.outbox import enqueue

    sheet = _read_set(session, store)
    entry = enqueue(
        session,
        workflow=READ_DEPTHS_AGAIN_WORKFLOW,
        payload={"package_revision_id": str(sheet.revision.id)},
    )
    session.flush()
    row = session.get_one(OutboxEntry, entry)

    row.attempts = MAX_TRIES - 1
    session.flush()
    told = last_request(session, sheet.revision.id)
    assert told is not None and told.state == "waiting" and "Tried 2 times" in told.sentence
    assert request_waiting(session, sheet.revision.id)

    row.attempts = MAX_TRIES
    session.flush()
    told = last_request(session, sheet.revision.id)
    assert told is not None and told.state == "not_read" and "Ask again" in told.sentence
    assert not request_waiting(session, sheet.revision.id)


def test_an_unexpected_failure_is_parked_with_why_never_retried_for_ever(
    session: Session,
) -> None:
    from workflow import sutradhar

    class Breaks:
        def read_depths_again(self, *_: object) -> dict[str, object]:
            raise RuntimeError("the store could not be read")

    with pytest.raises(DepthsAgainRefused) as refused:
        sutradhar.consume(
            session,
            workflow=READ_DEPTHS_AGAIN_WORKFLOW,
            payload={"package_revision_id": str(uuid4())},
            idempotency_key=str(uuid4()),
            stages_factory=lambda **_: Breaks(),
        )

    assert refused.value.plain_reason == (
        "Depths were not read again: RuntimeError: the store could not be read"
    )
