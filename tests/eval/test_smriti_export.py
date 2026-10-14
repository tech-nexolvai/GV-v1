"""Smriti: reviewer decisions become candidate Pariksha key cases, read-only (#1232).

Verification for: `eval/smriti/export.py`, `eval/smriti/candidates.py`. Synthetic decisions on every
decision table of the test database: invented pages, invented answers, invented people. No model
call.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from eval.smriti.candidates import NEEDS_CONFIRMATION, Candidate, agent_counts, table_counts
from eval.smriti.export import export_candidates

pytest_plugins = ("tests.app.postgres_fixture",)

OPUS = "anthropic.claude-opus-5-5"
PICTURE = "7" * 64
REVIEWER = "reviewer-a"


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


@dataclass(frozen=True, slots=True)
class World:
    """Every decision table holding one or more invented reviewer decisions."""

    page_hash: str
    slot_boxes: dict[str, str]
    """`slot:<n>` / `slot:overall` → the slot box of that span."""
    ledger_slot: UUID
    ledger_other: UUID
    row_decision_first: UUID
    row_decision_latest: UUID
    pairing: UUID
    view_match: UUID
    no_countertop_reader: UUID
    no_countertop_reader_old: UUID
    no_countertop_code: UUID
    no_countertop_budget: UUID
    split_action: UUID


def _packet(
    version_id: UUID,
    page_index: int,
    page_hash: str,
    question_id: str,
    prompt_id: str,
    candidate_ids: list[str],
    picture: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "question_id": question_id,
        "document_version_id": str(version_id),
        "page_index": page_index,
        "source_page_sha256": page_hash,
        "prompt_id": prompt_id,
        "candidate_ids": candidate_ids,
        "page_transform": {"dpi": 300},
        "images": {"view": {"sha256": picture, "storage_key": None}},
    }
    body["packet_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


def _ask(
    session: Session,
    run_id: UUID,
    version_id: UUID,
    page_hash: str,
    question_id: str,
    prompt_id: str,
    candidate_ids: list[str],
    *,
    picture: str = "2" * 64,
) -> None:
    from app.models.runs import ModelInvocation

    session.add(
        ModelInvocation(
            extraction_run_id=run_id,
            model_id=OPUS,
            prompt_id=prompt_id,
            template_id=prompt_id,
            input_tokens=10,
            output_tokens=5,
            cost_micros=1000,
            latency_ms=1,
            outcome="ok",
            private_raw_response="{}",
            reader_page_index=0,
            reader_attempt_number=1,
            reader_question_packet=_packet(
                version_id, 0, page_hash, question_id, prompt_id, candidate_ids, picture
            ),
        )
    )
    session.flush()


def _run(session: Session, revision_id: UUID, extractor: str) -> Any:
    from app.models.runs import ExtractionRun, TaskRun, WorkflowRun

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
        task_run_id=task.id, extractor=extractor, extractor_version="1", config_hash="test"
    )
    session.add(run)
    session.flush()
    return run


def _candidate(
    session: Session,
    version_id: UUID,
    page_id: UUID,
    run_id: UUID,
    flags: list[str],
    *,
    raw_text: str = "",
) -> UUID:
    from app.models.evidence import ObservationCandidate

    candidate = ObservationCandidate(
        document_version_id=version_id,
        page_id=page_id,
        extraction_run_id=run_id,
        raw_text=raw_text,
        polygon=[[0, 0], [1, 1]],
        ambiguity_flags=flags,
    )
    session.add(candidate)
    session.flush()
    return candidate.id


def _ledger(session: Session, scenario: Any, observation_id: UUID) -> UUID:
    from app.models.review import CorrectionLedgerEntry, ReviewAction

    action = ReviewAction(
        review_session_id=scenario.review_session_id,
        finding_id=scenario.finding_id,
        package_revision_id=scenario.package_revision_id,
        action="correct",
        actor=REVIEWER,
    )
    session.add(action)
    session.flush()
    entry = CorrectionLedgerEntry(
        review_action_id=action.id,
        canonical_observation_id=observation_id,
        original_value='{"semantic_type":"x","unit":"in","value":"1/3"}',
        corrected_value='{"semantic_type":"x","unit":"in","value":"5/2"}',
    )
    session.add(entry)
    session.flush()
    return entry.id


def _slot_observation(session: Session, scenario: Any, primary: UUID, revision_id: UUID) -> UUID:
    """A corroborated reading whose primary candidate is the slot reader's span."""
    from app.models.evidence import CanonicalObservation, EvidenceSupportingCandidate
    from evidence.canonical import Authority
    from rules.semantic_types import DocumentRole, SemanticType
    from units.measurement import Unit
    from verdict.operands import EvidenceStatus

    other_run = _run(session, revision_id, "pdfplumber")
    other = _candidate(
        session, scenario.document_version_id, scenario.page_id, other_run.id, [], raw_text="x"
    )
    observation = CanonicalObservation(
        document_version_id=scenario.document_version_id,
        page_id=scenario.page_id,
        document_role=DocumentRole.SHOP,
        polygon=[["0.1", "0.1"], ["0.2", "0.1"], ["0.2", "0.2"]],
        coordinate_space="stored",
        semantic_type=SemanticType.CT001,
        value_numerator=1,
        value_denominator=3,
        unit=Unit.INCH,
        status=EvidenceStatus.CORROBORATED,
        authority=Authority.AUTHORITATIVE,
        evidence_crop_uri=None,
    )
    session.add(observation)
    session.flush()
    session.add_all(
        (
            EvidenceSupportingCandidate(
                canonical_observation_id=observation.id, candidate_id=primary, role="primary"
            ),
            EvidenceSupportingCandidate(
                canonical_observation_id=observation.id, candidate_id=other, role="corroborating"
            ),
        )
    )
    session.flush()
    return observation.id


def build_world(session: Session) -> World:
    """One revision, one vendor page, and a reviewer decision in every decision table."""
    from app.models.evidence import (
        ArchitectPairingRecord,
        ArchitectViewMatchRecord,
        NoCountertopAnswer,
        SlotRowReviewDecision,
    )
    from app.models.review import ReviewAction
    from app.models.verdicts import CheckRun, Finding
    from tests.review.test_ledger import PAGE_HASH, _scenario

    scenario = _scenario(session, media_box=["0", "0", "612", "792"])
    revision_id = scenario.package_revision_id
    version_id = scenario.document_version_id
    page_id = scenario.page_id
    run = _run(session, revision_id, "extraction.form_reader")

    # One countertop row: two pieces and the overall, each with one label question.
    boxes = {"slot:0": "10,20,30,40", "slot:1": "40,20,60,40", "slot:overall": "10,50,60,70"}
    slots: dict[str, UUID] = {}
    for slot, box in boxes.items():
        slots[slot] = _candidate(
            session,
            version_id,
            page_id,
            run.id,
            ["slot-reader", slot, f"slot-box:{box}", "row-rank:1"],
        )
        number = slot.removeprefix("slot:")
        question = "p0:overall:0" if number == "overall" else f"p0:slot{number}:0"
        _ask(
            session,
            run.id,
            version_id,
            PAGE_HASH,
            question,
            "claude-slot-span-v4",
            [str(slots[slot])],
        )
    # The same row's slot 1 was also read in a second, unrelated row rank: never mixed in.
    _candidate(
        session,
        version_id,
        page_id,
        run.id,
        ["slot-reader", "slot:1", "slot-box:1,1,2,2", "row-rank:2"],
    )

    # correction_ledger: a corrected slot-reader reading, and one read some other way.
    ledger_slot = _ledger(
        session, scenario, _slot_observation(session, scenario, slots["slot:0"], revision_id)
    )
    ledger_other = _ledger(session, scenario, scenario.observation_id)

    # slot_row_review_decisions: walls chosen, then a width typed; the first is superseded.
    first = SlotRowReviewDecision(
        row_candidate_id=slots["slot:0"],
        wall_config="back_only",
        measurements={},
        confirmed_by=REVIEWER,
    )
    session.add(first)
    session.flush()
    latest = SlotRowReviewDecision(
        row_candidate_id=slots["slot:0"],
        supersedes_id=first.id,
        wall_config="back_only",
        measurements={
            "piece_widths:1": {
                "numerator": 25,
                "denominator": 2,
                "unit": "in",
                "display": '12 1/2"',
            }
        },
        confirmed_by=REVIEWER,
    )
    session.add(latest)
    session.flush()

    # architect_pairing_records and architect_view_matches: a reviewer's pick each.
    pairing = ArchitectPairingRecord(
        package_revision_id=revision_id,
        page_id=page_id,
        row_anchor_candidate_id=slots["slot:0"],
        extraction_run_id=None,
        source="reviewer",
        status="reviewer",
        pairs=[],
        details={},
        decided_by=REVIEWER,
    )
    view_match = ArchitectViewMatchRecord(
        package_revision_id=revision_id,
        vendor_page_id=page_id,
        row_anchor_candidate_id=slots["slot:0"],
        extraction_run_id=None,
        source="reviewer",
        status="none_matches",
        ai_picks=[],
        candidates=[],
        vendor_references=[],
        reasons=[],
        details={},
        decided_by=REVIEWER,
    )
    session.add_all([pairing, view_match])
    session.flush()

    # no_countertop_answers on three kinds of listed page.
    rows = [str(uuid4()), str(uuid4())]
    reader_record = _candidate(
        session,
        version_id,
        page_id,
        run.id,
        [
            "slot-reader-row-choice",
            "row-choice:0",
            "row-pick:opus-5-5:none",
            "row-kind:opus-5-5:no_countertop",
            *(f"row-candidate:{i}:{row}" for i, row in enumerate(rows, start=1)),
        ],
    )
    _ask(
        session,
        run.id,
        version_id,
        PAGE_HASH,
        "p0:row-choice",
        "slot-row-choice-v3",
        rows,
        picture=PICTURE,
    )
    code_record = _candidate(
        session,
        version_id,
        page_id,
        run.id,
        ["slot-reader-row-choice", "row-choice:0", "no-row:reference-only"],
    )
    budget_record = _candidate(
        session,
        version_id,
        page_id,
        run.id,
        ["slot-reader-row-choice", "row-choice:0", "no-row:budget-reached"],
    )

    def answer(record: UUID, said: str) -> UUID:
        row = NoCountertopAnswer(
            package_revision_id=revision_id,
            row_choice_candidate_id=record,
            page_id=page_id,
            page_number=1,
            page_content_hash=PAGE_HASH,
            reading_answer="row-pick:opus-5-5:none",
            answer=said,
            answered_by=REVIEWER,
        )
        session.add(row)
        session.flush()
        return row.id

    old = answer(reader_record, "countertop")
    reader_answer = answer(reader_record, "no_countertop")
    code_answer = answer(code_record, "countertop")
    budget_answer = answer(budget_record, "no_countertop")

    # A split page's result, decided by the reviewer on its finding.
    split_record = _candidate(
        session,
        version_id,
        page_id,
        run.id,
        ["slot-reader-row-choice", "row-choice:0", "row-pick:opus-5-5:1", "row-pick:sonnet-5-5:2"],
    )
    snapshot_id = session.scalar(
        select(CheckRun.rule_snapshot_id)
        .join(Finding, Finding.check_run_id == CheckRun.id)
        .where(Finding.id == scenario.finding_id)
    )
    check = CheckRun(
        package_revision_id=revision_id, rule_snapshot_id=snapshot_id, engine_version="v-test"
    )
    session.add(check)
    session.flush()
    finding = Finding(
        check_run_id=check.id,
        package_revision_id=revision_id,
        outcome="REVIEW_REQUIRED",
        severity="CRITICAL",
        trace={},
        parameter_set_versions={},
        scope_row_candidate_id=split_record,
        scope_label="Countertop row on page 1",
    )
    session.add(finding)
    session.flush()
    split_action = ReviewAction(
        review_session_id=scenario.review_session_id,
        finding_id=finding.id,
        package_revision_id=revision_id,
        action="dismiss",
        actor=REVIEWER,
    )
    session.add(split_action)
    session.commit()
    return World(
        page_hash=PAGE_HASH,
        slot_boxes=boxes,
        ledger_slot=ledger_slot,
        ledger_other=ledger_other,
        row_decision_first=first.id,
        row_decision_latest=latest.id,
        pairing=pairing.id,
        view_match=view_match.id,
        no_countertop_reader=reader_answer,
        no_countertop_reader_old=old,
        no_countertop_code=code_answer,
        no_countertop_budget=budget_answer,
        split_action=split_action.id,
    )


def _by_id(candidates: tuple[Candidate, ...]) -> dict[str, Candidate]:
    return {candidate.candidate_id: candidate for candidate in candidates}


def _export(session: Session) -> dict[str, Candidate]:
    try:
        return _by_id(export_candidates(session, database="synthetic").candidates)
    finally:
        session.rollback()


def test_a_corrected_slot_reader_value_becomes_an_akshara_label_case_whose_text_a_person_states(
    session: Session,
) -> None:
    world = build_world(session)
    found = _export(session)[f"correction_ledger:{world.ledger_slot}"]
    assert found.agent == "akshara"
    assert found.kind == "corrected"
    assert found.status == NEEDS_CONFIRMATION
    assert found.question == {
        "family": "claude-slot-span",
        "page_sha256": world.page_hash,
        "question_id": "p0:slot0:0",
        "slot_box": world.slot_boxes["slot:0"],
    }
    # The ledger keeps a value; the key compares printed text, so the text is never made up.
    assert found.answer is None
    assert found.answer_needed is not None and "printed text" in found.answer_needed
    assert found.convertible
    assert found.recorded["corrected"] == '{"semantic_type":"x","unit":"in","value":"5/2"}'


def test_a_correction_of_a_reading_no_label_question_asked_is_listed_as_not_convertible(
    session: Session,
) -> None:
    world = build_world(session)
    found = _export(session)[f"correction_ledger:{world.ledger_other}"]
    assert found.agent == "akshara"
    assert not found.convertible
    assert found.question is None
    assert "slot reader" in (found.not_convertible or "")


def test_walls_and_a_typed_width_split_into_rekha_and_akshara_and_only_the_latest_counts(
    session: Session,
) -> None:
    world = build_world(session)
    found = _export(session)
    walls = found[f"slot_row_review_decisions:{world.row_decision_latest}:walls"]
    assert walls.agent == "rekha"
    assert walls.kind == "decided"
    assert walls.recorded == {"wall_config": "back_only"}
    assert not walls.convertible and "walls" in (walls.not_convertible or "")

    width = found[f"slot_row_review_decisions:{world.row_decision_latest}:piece_widths:1"]
    assert width.agent == "akshara"
    assert width.kind == "filled"
    assert width.question == {
        "family": "claude-slot-span",
        "page_sha256": world.page_hash,
        "question_id": "p0:slot1:0",
        "slot_box": world.slot_boxes["slot:1"],
    }
    assert width.answer is None and width.convertible
    assert width.recorded == {"key": "piece_widths:1", "inches": "25/2", "typed": '12 1/2"'}

    earlier = found[f"slot_row_review_decisions:{world.row_decision_first}:walls"]
    assert earlier.kind == "superseded"
    assert not earlier.convertible


def test_reviewer_pairings_and_view_matches_are_milans_and_wait_for_a_milan_key(
    session: Session,
) -> None:
    world = build_world(session)
    found = _export(session)
    pairing = found[f"architect_pairing_records:{world.pairing}"]
    view = found[f"architect_view_matches:{world.view_match}"]
    assert (pairing.agent, view.agent) == ("milan", "milan")
    assert pairing.recorded == {"pairs": [], "nothing_comparable": True}
    assert view.recorded["status"] == "none_matches"
    for candidate in (pairing, view):
        assert not candidate.convertible
        assert "Milan" in (candidate.not_convertible or "")
        assert candidate.page_sha256 == world.page_hash


def test_a_confirmed_no_countertop_page_becomes_a_rekha_row_case_with_its_picture(
    session: Session,
) -> None:
    world = build_world(session)
    found = _export(session)
    case = found[f"no_countertop_answers:{world.no_countertop_reader}"]
    assert case.agent == "rekha"
    assert case.kind == "confirmed"
    assert case.question == {
        "family": "slot-row-choice",
        "page_sha256": world.page_hash,
        "question_id": "p0:row-choice",
        "picture_sha256": PICTURE,
    }
    assert case.answer == {"kind": "no_countertop"}
    assert case.convertible and case.answer_needed is None

    older = found[f"no_countertop_answers:{world.no_countertop_reader_old}"]
    assert older.kind == "superseded" and not older.convertible


def test_no_countertop_answers_on_code_listed_and_budget_stopped_pages_name_the_right_agent(
    session: Session,
) -> None:
    world = build_world(session)
    found = _export(session)
    code = found[f"no_countertop_answers:{world.no_countertop_code}"]
    assert code.agent == "bala"
    assert code.kind == "corrected"
    assert not code.convertible

    budget = found[f"no_countertop_answers:{world.no_countertop_budget}"]
    assert budget.agent == "none"
    assert not budget.convertible and "budget" in (budget.not_convertible or "")


def test_a_decision_on_a_split_pages_result_is_rekhas_but_names_no_box(session: Session) -> None:
    world = build_world(session)
    found = _export(session)[f"review_actions:{world.split_action}"]
    assert found.agent == "rekha"
    assert found.recorded == {"action": "dismiss"}
    assert not found.convertible and "box" in (found.not_convertible or "")


def test_a_row_choice_question_asked_with_two_pictures_is_not_guessed(session: Session) -> None:
    from app.models.evidence import NoCountertopAnswer, ObservationCandidate
    from app.models.runs import ModelInvocation

    world = build_world(session)
    record = session.scalar(
        select(NoCountertopAnswer.row_choice_candidate_id).where(
            NoCountertopAnswer.id == world.no_countertop_reader
        )
    )
    candidate = session.get(ObservationCandidate, record)
    assert candidate is not None
    packet = session.scalars(
        select(ModelInvocation.reader_question_packet).where(
            ModelInvocation.prompt_id == "slot-row-choice-v3"
        )
    ).one()
    assert packet is not None and isinstance(packet["candidate_ids"], list)
    _ask(
        session,
        candidate.extraction_run_id,
        candidate.document_version_id,
        world.page_hash,
        "p0:row-choice",
        "slot-row-choice-v3",
        [str(row) for row in packet["candidate_ids"]],
        picture="8" * 64,
    )
    session.commit()
    found = _export(session)[f"no_countertop_answers:{world.no_countertop_reader}"]
    assert not found.convertible
    assert found.question is None
    assert "2 different" in (found.not_convertible or "")


def test_every_candidate_needs_confirmation_and_the_counts_add_up(session: Session) -> None:
    build_world(session)
    try:
        exported = export_candidates(session, database="synthetic")
    finally:
        session.rollback()
    assert {candidate.status for candidate in exported.candidates} == {NEEDS_CONFIRMATION}
    counts = agent_counts(exported.candidates)
    assert counts["akshara"].decisions == 3
    assert counts["akshara"].convertible == 2
    assert counts["akshara"].ready == 0
    assert counts["rekha"].decisions == 3  # walls, the no-countertop page, the split page
    assert counts["rekha"].ready == 1
    assert counts["rekha"].earlier == 2  # the superseded walls and no-countertop answer
    assert counts["milan"].decisions == 2
    assert counts["bala"].decisions == 1
    assert counts["none"].decisions == 1
    tables = table_counts(exported.candidates)
    assert tables["correction_ledger"].decisions == 2
    assert tables["no_countertop_answers"].decisions == 3
    assert tables["no_countertop_answers"].earlier == 1


def test_the_export_is_read_only(session: Session) -> None:
    from app.models.review import CorrectionLedgerEntry

    build_world(session)
    before = session.scalar(select(func.count()).select_from(CorrectionLedgerEntry))
    session.rollback()
    export_candidates(session, database="synthetic")
    with pytest.raises(DBAPIError, match="read-only"):
        session.execute(text("CREATE TEMPORARY TABLE smriti_probe (x int)"))
    session.rollback()
    assert session.scalar(select(func.count()).select_from(CorrectionLedgerEntry)) == before


def test_an_empty_database_exports_no_candidates(session: Session) -> None:
    try:
        exported = export_candidates(session, database="synthetic")
    finally:
        session.rollback()
    assert exported.candidates == ()
    assert agent_counts(exported.candidates) == {}
