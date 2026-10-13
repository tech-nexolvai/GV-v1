"""Reading a stored run for Pariksha, read-only (#1219).

Verification for: `eval/pariksha/stored.py`. A synthetic stored run on the test database: invented
questions, invented answers, an invented page hash. No model call.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from eval.pariksha.stored import StoredRunError, load_architect_run, load_stored_run

pytest_plugins = ("tests.app.postgres_fixture",)

PAGE_HASH = "1" * 64  # `_scaffold`'s page content hash
OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"


@pytest.fixture
def engine(postgres_engine: Engine) -> Engine:
    from alembic import command
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return postgres_engine


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    from app.db.session import session_factory

    opened = session_factory(engine)()
    try:
        yield opened
    finally:
        opened.close()


def span_answer(text_: str, belongs: str = "yes") -> str:
    return json.dumps(
        {
            "belongs": belongs,
            "text": text_,
            "stacked": False,
            "combined": False,
            "readable": True,
            "no_dimension": False,
        }
    )


def row_answer(row: int, kind: str = "row") -> str:
    return json.dumps({"row": row, "kind": kind, "also": [], "why": "invented"})


def _packet(
    version_id: UUID,
    question_id: str,
    prompt_id: str,
    *,
    candidate_ids: list[str],
    reused_from: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "question_id": question_id,
        "document_version_id": str(version_id),
        "page_index": 0,
        "source_page_sha256": PAGE_HASH,
        "prompt_id": prompt_id,
        "candidate_ids": candidate_ids,
        "page_transform": {"dpi": 300},
        "images": {"close_up": {"sha256": "2" * 64, "storage_key": None}},
    }
    if prompt_id.startswith("slot-row-choice"):
        body["candidate_count"] = len(candidate_ids)
    body["packet_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if reused_from is not None:
        body["reused_from"] = reused_from
    return body


def add_answer(
    session: Session,
    run_id: UUID,
    version_id: UUID,
    *,
    question_id: str,
    prompt_id: str,
    model_id: str,
    raw: str | None,
    outcome: str = "ok",
    cost_micros: int | None = 1000,
    candidate_ids: list[str] | None = None,
    reused_from: str | None = None,
) -> None:
    from app.models.runs import ModelInvocation

    session.add(
        ModelInvocation(
            extraction_run_id=run_id,
            model_id=model_id,
            prompt_id=prompt_id,
            template_id=prompt_id,
            input_tokens=10,
            output_tokens=5,
            cost_micros=cost_micros,
            latency_ms=1,
            outcome=outcome,
            rejection_reason=None if outcome == "ok" else "invented failure",
            private_raw_response=raw,
            reader_page_index=0,
            reader_attempt_number=1,
            reader_question_packet=_packet(
                version_id,
                question_id,
                prompt_id,
                candidate_ids=candidate_ids or [str(uuid4())],
                reused_from=reused_from,
            ),
        )
    )
    session.flush()


def add_slot_candidate(session: Session, version: Any, page: Any, run: Any, box: str) -> str:
    from app.models.evidence import ObservationCandidate

    candidate = ObservationCandidate(
        document_version_id=version.id,
        page_id=page.id,
        extraction_run_id=run.id,
        raw_text="invented",
        polygon=[[0, 0], [1, 1]],
        ambiguity_flags=["slot-reader", "slot:0", f"slot-box:{box}"],
    )
    session.add(candidate)
    session.flush()
    return str(candidate.id)


def synthetic_run(session: Session, answers: Mapping[str, tuple[str, str]]) -> tuple[Any, Any]:
    """One stored run: each label question id → (first reader's text, second reader's text)."""
    from tests.workflow.test_slot_reader import _scaffold

    _revision, version, page, run = _scaffold(session)
    candidate = add_slot_candidate(session, version, page, run, "10,20,30,40")
    for question_id, (first, second) in answers.items():
        for model, said in ((OPUS, first), (SONNET, second)):
            add_answer(
                session,
                run.id,
                version.id,
                question_id=question_id,
                prompt_id="claude-slot-span-v4",
                model_id=model,
                raw=span_answer(said),
                candidate_ids=[candidate],
            )
    add_answer(
        session,
        run.id,
        version.id,
        question_id="p0:row-choice",
        prompt_id="slot-row-choice-v3",
        model_id=OPUS,
        raw=row_answer(1),
        candidate_ids=[str(uuid4()), str(uuid4())],
    )
    session.commit()
    return version, run


def test_a_stored_run_reads_each_question_with_its_identity_and_every_attempt(
    session: Session,
) -> None:
    version, run = synthetic_run(session, {"p0:slot0:0": ('3/4"', '3/4"')})
    add_answer(
        session,
        run.id,
        version.id,
        question_id="p0:slot0:0",
        prompt_id="claude-slot-span-v4",
        model_id=SONNET,
        raw=None,
        outcome="timeout",
        cost_micros=None,
    )
    session.commit()

    stored = load_stored_run(session, agent="akshara", database="synthetic")

    assert stored.extraction_run_id == str(run.id)
    (question,) = stored.questions
    assert question.identity.family == "claude-slot-span"
    assert question.identity.page_sha256 == PAGE_HASH
    assert question.identity.question_id == "p0:slot0:0"
    assert question.slot_box == "10,20,30,40"
    assert sorted((a.model_id, a.outcome) for a in question.attempts) == [
        (OPUS, "ok"),
        (SONNET, "ok"),
        (SONNET, "timeout"),
    ]
    rows = load_stored_run(session, agent="rekha", database="synthetic")
    assert [q.identity.question_id for q in rows.questions] == ["p0:row-choice"]


def test_the_stored_run_is_read_in_a_read_only_transaction(session: Session) -> None:
    synthetic_run(session, {"p0:slot0:0": ('3/4"', '3/4"')})

    load_stored_run(session, agent="akshara", database="synthetic")

    with pytest.raises(DBAPIError, match="read-only"):
        session.execute(text("CREATE TABLE pariksha_must_not_exist (id int)"))
    session.rollback()


def test_a_database_holding_several_runs_needs_the_run_named(session: Session) -> None:
    from tests.workflow.test_reader_reuse import _new_run

    version, run = synthetic_run(session, {"p0:slot0:0": ('3/4"', '3/4"')})
    revision_id = session.execute(
        text(
            "SELECT wr.package_revision_id FROM extraction_runs er "
            "JOIN task_runs tr ON tr.id = er.task_run_id "
            "JOIN workflow_runs wr ON wr.id = tr.workflow_run_id WHERE er.id = :run"
        ),
        {"run": run.id},
    ).scalar_one()
    rerun = _new_run(session, revision_id)
    add_answer(
        session,
        rerun.id,
        version.id,
        question_id="p0:slot0:0",
        prompt_id="claude-slot-span-v4",
        model_id=OPUS,
        raw=span_answer('3/4"'),
        cost_micros=0,
        reused_from=str(uuid4()),
    )
    session.commit()

    with pytest.raises(StoredRunError, match="2 extraction runs"):
        load_stored_run(session, agent="akshara", database="synthetic")
    session.rollback()

    chosen = load_stored_run(
        session, agent="akshara", database="synthetic", extraction_run_id=rerun.id
    )
    (question,) = chosen.questions
    assert [attempt.reused for attempt in question.attempts] == [True]


def test_a_run_without_questions_for_the_agent_is_refused(session: Session) -> None:
    from tests.workflow.test_slot_reader import _scaffold

    _scaffold(session)
    session.commit()

    with pytest.raises(StoredRunError, match="no stored"):
        load_stored_run(session, agent="akshara", database="synthetic")


def test_the_architect_run_is_read_as_spans_per_page(session: Session) -> None:
    from fractions import Fraction

    from app.models.evidence import ObservationCandidate
    from tests.workflow.test_slot_reader import _scaffold

    _revision, version, page, run = _scaffold(session)
    for raw_text, value, reason in (("2'-0\"", 24, None), ("3'-0\" VIF", None, "not firm (vif)")):
        session.add(
            ObservationCandidate(
                document_version_id=version.id,
                page_id=page.id,
                extraction_run_id=run.id,
                raw_text=raw_text,
                value_numerator=value,
                value_denominator=None if value is None else 1,
                unit=None if value is None else "in",
                polygon=[[0, 0], [1, 1]],
                ambiguity_flags=["architect-reader", "arch-ticks-on-outline:yes"],
                review_reason=reason,
            )
        )
    session.commit()

    stored = load_architect_run(session, database="synthetic")

    assert stored.document_sha256 == "1" * 64
    spans = stored.pages[1]
    assert [(s.text, s.inches, s.printed_inches, s.held_reason) for s in spans] == [
        ("2'-0\"", Fraction(24), Fraction(24), None),
        ("3'-0\" VIF", None, Fraction(36), "not firm (vif)"),
    ]
    assert spans[0].on_outline is True
