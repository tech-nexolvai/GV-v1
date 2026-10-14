"""Smriti's learner report: per agent, what the AIs answered and what people decided (#1272).

Verification for: `eval/smriti/report.py`. Invented candidates, invented calls, invented stops and
invented people; the database tests use an empty or invented test database. No model call.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from eval.correction_rate import CorrectionRateKey
from eval.metrics import MetricResult
from eval.smriti.actors import Reviewers
from eval.smriti.candidates import Candidate, CandidateExport
from eval.smriti.report import (
    NEEDLESS,
    NOT_MEASURED,
    UNDETERMINED,
    USEFUL,
    WAITING,
    StoppedItem,
    build_report,
    classify_stop,
    learner_report,
    render_report,
)

pytest_plugins = ("tests.app.postgres_fixture",)

PERSON = "reviewer-a"
STRANGER = "someone-else"
PAGE = "a" * 64
OTHER_PAGE = "b" * 64
STOPPED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LATER = (STOPPED_AT + timedelta(hours=1)).isoformat()
EARLIER = (STOPPED_AT - timedelta(hours=1)).isoformat()


def _candidate(**changes: Any) -> Candidate:
    values: dict[str, Any] = {
        "candidate_id": f"correction_ledger:{uuid4()}",
        "table": "correction_ledger",
        "decision_id": str(uuid4()),
        "agent": "akshara",
        "corrects": "a value a reader read",
        "kind": "corrected",
        "decided_by": PERSON,
        "decided_at": LATER,
        "page_sha256": PAGE,
        "page_number": 1,
        "recorded": {},
        "question": {
            "family": "claude-slot-span",
            "page_sha256": PAGE,
            "question_id": "p0:slot0:0",
            "slot_box": "1,2,3,4",
        },
        "answer": None,
        "answer_needed": "the printed text",
        "not_convertible": None,
        "actor_status": "reviewer",
    }
    values.update(changes)
    return Candidate(**values)


def _export(*candidates: Candidate, skipped: dict[str, dict[str, int]] | None = None) -> Any:
    return CandidateExport(
        database="invented",
        exported_at=STOPPED_AT.isoformat(),
        code_version="test",
        candidates=tuple(candidates),
        skipped=skipped or {},
        reviewer_list="test",
    )


def _stop(item: str = "p0:slot0:0", agent: str = "akshara", **changes: Any) -> StoppedItem:
    values: dict[str, Any] = {
        "reading_id": str(uuid4()),
        "document_version_id": str(uuid4()),
        "page_number": 1,
        "page_sha256": PAGE,
        "agent": agent,
        "item": item,
        "code": "readers-differ",
        "stopped_at": STOPPED_AT,
    }
    values.update(changes)
    return StoppedItem(**values)


def _agent(report: Any, name: str) -> Any:
    (found,) = [agent for agent in report.agents if agent.agent == name]
    return found


def _no_zero_percent(text: str) -> None:
    assert not re.search(r"(?<![\d.])0(\.0+)?\s*%", text), "a 0 denominator must never read as 0%"


# --- NOT MEASURED, never 0% ---------------------------------------------------------------------


def test_a_zero_denominator_reads_not_measured_never_zero_percent() -> None:
    report = build_report(export=_export(), invocations=[], stops=[], reviewed={}, window="test")
    akshara = _agent(report, "akshara")
    assert akshara.correction_rate.denominator == 0
    assert akshara.correction_rate.show().startswith(NOT_MEASURED)
    assert akshara.gate_usefulness.show().startswith(NOT_MEASURED)
    text = render_report(report)
    assert NOT_MEASURED in text
    _no_zero_percent(text)
    # Every agent is listed, so an agent with nothing is seen as NOT MEASURED, not left out.
    for name in ("bala", "rekha", "akshara", "vastu", "milan", "kautilya", "khoj", "vani"):
        assert name in {agent.agent for agent in report.agents}


def test_ai_answers_without_any_person_decision_are_still_not_measured() -> None:
    """148 calls and no decision: the denominator of a correction rate is decisions, not calls."""
    report = build_report(
        export=_export(),
        invocations=[("claude-slot-span-v2", "ok", 84), ("slot-row-choice-v2", "ok", 32)],
        stops=[],
        reviewed={},
        window="test",
    )
    akshara = _agent(report, "akshara")
    assert akshara.ai.calls == 84
    assert akshara.ai.answered == 84
    assert _agent(report, "rekha").ai.calls == 32
    assert akshara.correction_rate.show().startswith(NOT_MEASURED)
    _no_zero_percent(render_report(report))


def test_a_family_no_agent_is_named_for_is_listed_not_dropped() -> None:
    report = build_report(
        export=_export(),
        invocations=[("invented-prompt-v1", "ok", 3), ("invented-prompt-v1", "failed", 1)],
        stops=[],
        reviewed={},
        window="test",
    )
    assert report.unmapped_families["invented-prompt"].calls == 4
    assert report.unmapped_families["invented-prompt"].answered == 3
    assert "invented-prompt" in render_report(report)


def test_a_measured_rate_shows_its_numerator_and_denominator() -> None:
    report = build_report(
        export=_export(
            _candidate(kind="corrected"),
            _candidate(kind="confirmed", candidate_id="review_actions:x", table="review_actions"),
            _candidate(kind="confirmed", candidate_id="review_actions:y", table="review_actions"),
        ),
        invocations=[],
        stops=[],
        reviewed={},
        window="test",
    )
    rate = _agent(report, "akshara").correction_rate
    assert (rate.numerator, rate.denominator) == (1, 3)
    assert "1 of 3" in rate.show()


# --- corrections: AI misread or vendor drew it --------------------------------------------------


def test_corrections_split_into_misread_vendor_and_not_yet_answered() -> None:
    report = build_report(
        export=_export(
            _candidate(mistake_needed="ai_misread"),
            _candidate(mistake_needed="vendor_drew_it"),
            _candidate(mistake_needed="unanswered"),
            _candidate(
                agent="rekha",
                table="no_countertop_answers",
                candidate_id="no_countertop_answers:z",
                mistake_needed=None,
            ),
        ),
        invocations=[],
        stops=[],
        reviewed={},
        window="test",
    )
    akshara = _agent(report, "akshara")
    assert akshara.corrections.ai_misread == 1
    assert akshara.corrections.vendor_drew_it == 1
    assert akshara.corrections.unanswered == 1
    assert akshara.corrections.total == akshara.decisions.corrected == 3
    # One correction still waits for its answer, so the misread rate is not knowable exactly.
    assert akshara.misread_rate.show().startswith(NOT_MEASURED)
    assert _agent(report, "rekha").corrections.not_asked == 1


def test_the_misread_rate_is_measured_once_every_correction_is_answered() -> None:
    report = build_report(
        export=_export(
            _candidate(mistake_needed="ai_misread"),
            _candidate(mistake_needed="vendor_drew_it"),
            _candidate(kind="confirmed", candidate_id="review_actions:c", table="review_actions"),
        ),
        invocations=[],
        stops=[],
        reviewed={},
        window="test",
    )
    rate = _agent(report, "akshara").misread_rate
    assert (rate.numerator, rate.denominator) == (1, 3)


# --- code is never a person ---------------------------------------------------------------------


def test_code_decisions_are_never_counted_as_a_persons() -> None:
    """Even in a candidates file that holds one (the exporter itself never writes them)."""
    report = build_report(
        export=_export(
            _candidate(decided_by="code:bala-two-judgments/v1", agent="bala", kind="confirmed"),
            _candidate(decided_by="CODE:invented/v1", kind="corrected"),
            skipped={"view_role_confirmations": {"code": 28}},
        ),
        invocations=[],
        stops=[_stop()],
        reviewed={},
        window="test",
    )
    assert _agent(report, "bala").decisions.decisions == 0
    assert _agent(report, "akshara").decisions.decisions == 0
    assert _agent(report, "akshara").correction_rate.denominator == 0
    # A code decision on the stopped item does not judge the stop either.
    (outcome,) = report.gates
    assert outcome.verdict == WAITING
    text = render_report(report)
    assert "view_role_confirmations: code 28" in text


def test_decisions_by_names_off_the_reviewer_list_are_counted_apart() -> None:
    report = build_report(
        export=_export(
            _candidate(actor_status="reviewer"),
            _candidate(decided_by=STRANGER, actor_status="unknown"),
        ),
        invocations=[],
        stops=[],
        reviewed={},
        window="test",
    )
    akshara = _agent(report, "akshara")
    assert akshara.decisions.decisions == 2
    assert akshara.by_reviewer == 1


def test_overturned_automatic_results_are_counted_per_agent_and_listed() -> None:
    overturned = {
        "what": "an item code made by itself",
        "by": "code:x",
        "answer": {},
        "record": "r",
    }
    report = build_report(
        export=_export(
            _candidate(
                agent="bala",
                table="part_confirmations",
                candidate_id="part_confirmations:o",
                overturned=overturned,
            ),
        ),
        invocations=[],
        stops=[],
        reviewed={},
        window="test",
    )
    assert _agent(report, "bala").decisions.overturned == 1
    assert "an item code made by itself" in render_report(report)


# --- eval/correction_rate.py, mapped to agents --------------------------------------------------


def test_reviewed_reading_rates_are_mapped_to_their_agents() -> None:
    slot = CorrectionRateKey("internal", "extraction.form_reader", "slot-reader-v1")
    architect = CorrectionRateKey("type1", "architect-text", "architect-text-v1")
    other = CorrectionRateKey("internal", "pdfplumber", "extraction.reader/1")

    def result(corrected: int, reviewed: int) -> MetricResult:
        return MetricResult(
            key="reviewer_correction_rate",
            value=Fraction(corrected, reviewed),
            numerator=corrected,
            denominator=reviewed,
        )

    report = build_report(
        export=_export(),
        invocations=[],
        stops=[],
        reviewed={slot: result(1, 4), architect: result(0, 2), other: result(1, 1)},
        window="test",
    )
    assert [key for key, _ in _agent(report, "akshara").reviewed] == [slot]
    assert [key for key, _ in _agent(report, "vastu").reviewed] == [architect]
    assert [key for key, _ in report.unmapped_readers] == [other]
    text = render_report(report)
    assert "pdfplumber" in text


# --- gate usefulness ----------------------------------------------------------------------------


def test_a_stop_the_person_later_corrected_was_useful() -> None:
    outcome = classify_stop(_stop(), [_candidate(kind="corrected")])
    assert outcome.verdict == USEFUL


def test_a_stop_where_the_person_later_confirmed_the_ai_was_needless() -> None:
    confirm = _candidate(kind="confirmed", candidate_id="review_actions:c", table="review_actions")
    assert classify_stop(_stop(), [confirm]).verdict == NEEDLESS


def test_a_typed_value_does_not_say_whether_the_ai_was_right() -> None:
    typed = _candidate(kind="filled", table="slot_row_review_decisions")
    assert classify_stop(_stop(), [typed]).verdict == UNDETERMINED


def test_a_stop_with_no_later_person_decision_is_waiting() -> None:
    assert classify_stop(_stop(), []).verdict == WAITING
    # A decision made before the stop is not what the person did about it.
    assert classify_stop(_stop(), [_candidate(decided_at=EARLIER)]).verdict == WAITING
    # A decision on another item or another page is not about this one.
    elsewhere = _candidate(
        question={"family": "claude-slot-span", "page_sha256": PAGE, "question_id": "p0:slot1:0"}
    )
    assert classify_stop(_stop(), [elsewhere]).verdict == WAITING
    assert classify_stop(_stop(page_sha256=OTHER_PAGE), [_candidate()]).verdict == WAITING


def test_only_the_newest_current_decision_judges_the_stop() -> None:
    """A later correction replaces an earlier confirm; a superseded or carried record never counts."""
    confirm = _candidate(
        kind="confirmed",
        candidate_id="review_actions:c",
        table="review_actions",
        decided_at=LATER,
    )
    correction = _candidate(
        kind="corrected", decided_at=(STOPPED_AT + timedelta(hours=2)).isoformat()
    )
    assert classify_stop(_stop(), [confirm, correction]).verdict == USEFUL
    superseded = _candidate(kind="superseded")
    assert classify_stop(_stop(), [superseded]).verdict == WAITING
    carried = _candidate(kind="carried")
    assert classify_stop(_stop(), [carried]).verdict == WAITING


def test_a_page_stop_is_judged_by_the_persons_answer_about_that_page() -> None:
    """The line finder's row-choice stop and the page sorter's unread-page stop are answered by
    the reviewer's no-countertop answer on the same page."""
    there_is_one = _candidate(
        agent="rekha",
        table="no_countertop_answers",
        candidate_id="no_countertop_answers:a",
        kind="corrected",
        question=None,
    )
    none_there = _candidate(
        agent="bala",
        table="no_countertop_answers",
        candidate_id="no_countertop_answers:b",
        kind="confirmed",
        question=None,
    )
    assert classify_stop(_stop("p0:row-choice", "rekha"), [there_is_one]).verdict == USEFUL
    assert classify_stop(_stop("p0:page", "bala"), [none_there]).verdict == NEEDLESS
    # A finding decision on the same page is not an answer about the page's countertop.
    finding = _candidate(
        agent="kautilya",
        table="review_actions",
        candidate_id="review_actions:f",
        kind="decided",
        question=None,
    )
    assert classify_stop(_stop("p0:page", "bala"), [finding]).verdict == WAITING


def test_gate_usefulness_is_useful_over_useful_or_needless_per_agent() -> None:
    confirm = _candidate(kind="confirmed", candidate_id="review_actions:c", table="review_actions")
    other_item = _candidate(
        kind="corrected",
        question={"family": "claude-slot-span", "page_sha256": PAGE, "question_id": "p0:slot1:0"},
    )
    report = build_report(
        export=_export(confirm, other_item),
        invocations=[],
        stops=[_stop(), _stop("p0:slot1:0"), _stop("p0:slot2:0")],
        reviewed={},
        window="test",
    )
    akshara = _agent(report, "akshara")
    assert akshara.gates == {USEFUL: 1, NEEDLESS: 1, WAITING: 1}
    rate = akshara.gate_usefulness
    assert (rate.numerator, rate.denominator) == (1, 2)
    assert _agent(report, "rekha").gate_usefulness.show().startswith(NOT_MEASURED)


# --- read from a database, read-only ------------------------------------------------------------


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


PEOPLE = Reviewers(names=frozenset({"reviewer-a"}), source="test")


def test_an_empty_database_reads_not_measured_everywhere(session: Session) -> None:
    try:
        report = learner_report(session, database="invented", reviewers=PEOPLE)
    finally:
        session.rollback()
    text = render_report(report)
    assert all(agent.decisions.decisions == 0 for agent in report.agents)
    assert all(agent.correction_rate.denominator == 0 for agent in report.agents)
    assert report.gates == ()
    assert NOT_MEASURED in text
    _no_zero_percent(text)


def test_the_report_reads_only(session: Session) -> None:
    from sqlalchemy import text

    try:
        learner_report(session, database="invented", reviewers=PEOPLE)
        assert session.execute(text("SHOW transaction_read_only")).scalar() == "on"
    finally:
        session.rollback()


def test_a_stopped_item_the_person_later_corrected_is_a_useful_stop(session: Session) -> None:
    """End to end on an invented database: the number reader's inspector stopped a label, and the
    reviewer later corrected it in the ledger; the line finder stopped a page whose no-countertop
    listing the reviewer later confirmed."""
    from app.models.document import Page
    from app.models.runs import ReadingEvent
    from tests.eval.test_smriti_export import build_world

    world = build_world(session)
    page = session.scalars(select(Page).where(Page.content_hash == world.page_hash)).one()
    reading = uuid4()
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    events = [
        ("Akshara", "p0:slot0:0", "stopped", "readers-differ"),
        ("Rekha", "p0:row-choice", "stopped", "no-countertop"),
        ("Akshara", "p0:slot1:0", "passed", "sealed"),
    ]
    for position, (agent, item, kind, code) in enumerate(events):
        session.add(
            ReadingEvent(
                package_revision_id=uuid4(),
                reading_id=reading,
                position=position,
                document_version_id=page.document_version_id,
                page_number=page.index + 1,
                agent=agent,
                item=item,
                kind=kind,
                code=code,
                created_at=long_ago,
            )
        )
    session.add(
        ReadingEvent(
            package_revision_id=uuid4(),
            reading_id=reading,
            position=len(events),
            kind="finished",
            created_at=long_ago,
        )
    )
    session.commit()
    try:
        report = learner_report(session, database="invented", reviewers=PEOPLE)
    finally:
        session.rollback()
    verdicts = {
        (outcome.stop.agent, outcome.stop.item): outcome.verdict for outcome in report.gates
    }
    assert verdicts == {("akshara", "p0:slot0:0"): USEFUL, ("rekha", "p0:row-choice"): NEEDLESS}
    assert _agent(report, "akshara").decisions.corrected >= 1
    assert "p0:slot0:0 (readers-differ): useful" in render_report(report)


def test_a_stop_in_a_reading_that_never_finished_is_not_judged(session: Session) -> None:
    """A reading that failed is never shown as if it were the result, so its stops asked nothing."""
    from app.models.runs import ReadingEvent

    session.add(
        ReadingEvent(
            package_revision_id=uuid4(),
            reading_id=uuid4(),
            position=0,
            document_version_id=uuid4(),
            page_number=1,
            agent="Akshara",
            item="p0:slot0:0",
            kind="stopped",
            code="readers-differ",
        )
    )
    session.commit()
    try:
        report = learner_report(session, database="invented", reviewers=PEOPLE)
    finally:
        session.rollback()
    assert report.gates == ()
    assert report.unfinished_stops == 1
