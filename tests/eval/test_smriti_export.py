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

from eval.smriti.actors import Reviewers
from eval.smriti.candidates import NEEDS_CONFIRMATION, Candidate, agent_counts, table_counts
from eval.smriti.export import export_candidates

pytest_plugins = ("tests.app.postgres_fixture",)

OPUS = "anthropic.claude-opus-5-5"
PICTURE = "7" * 64
REVIEWER = "reviewer-a"
#: The local real-reviewer list, as a test gives it: invented names only.
PEOPLE = Reviewers(names=frozenset({REVIEWER}), source="test")


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


def _export(session: Session, reviewers: Reviewers = PEOPLE) -> dict[str, Candidate]:
    try:
        return _by_id(
            export_candidates(session, database="synthetic", reviewers=reviewers).candidates
        )
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
    # A walls key exists since #1250: the reason is now that no wall question names this row.
    assert not walls.convertible and "wall question" in (walls.not_convertible or "")
    assert "no walls answer" not in (walls.not_convertible or "")
    assert "walls key" not in (walls.not_convertible or "")

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
        exported = export_candidates(session, database="synthetic", reviewers=PEOPLE)
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
    export_candidates(session, database="synthetic", reviewers=PEOPLE)
    with pytest.raises(DBAPIError, match="read-only"):
        session.execute(text("CREATE TEMPORARY TABLE smriti_probe (x int)"))
    session.rollback()
    assert session.scalar(select(func.count()).select_from(CorrectionLedgerEntry)) == before


def test_an_empty_database_exports_no_candidates(session: Session) -> None:
    try:
        exported = export_candidates(session, database="synthetic", reviewers=PEOPLE)
    finally:
        session.rollback()
    assert exported.candidates == ()
    assert agent_counts(exported.candidates) == {}


# --- Smriti v2 (#1267): every person decision, nothing code made -------------------------------

WALL_BOX = "5,5,95,80"


@dataclass(frozen=True, slots=True)
class V2World:
    """The newer decision tables: code's decisions, a person's, an unknown name's, carried copies,
    a batch click, and a person overturning what code or both AIs decided by themselves."""

    page_hash: str
    code_role_only: UUID
    code_role: UUID
    person_role: UUID
    code_countertop: UUID
    person_withdrawal: UUID
    person_piece: UUID
    stranger_piece: UUID
    original_row: UUID
    carried_row: UUID
    batch_answer: UUID
    countertop_answer: UUID
    confirm_action: UUID
    walls_decision: UUID
    dismiss: UUID
    code_pairing: UUID
    person_pairing: UUID


def _row_record(
    session: Session,
    run_id: UUID,
    version_id: UUID,
    page_id: UUID,
    page_hash: str,
    kinds: list[str],
) -> UUID:
    """A reader's row-choice record on a page every reader said has no countertop, and its question."""
    rows = [str(uuid4()), str(uuid4())]
    record = _candidate(
        session,
        version_id,
        page_id,
        run_id,
        [
            "slot-reader-row-choice",
            "row-choice:0",
            *(f"row-pick:{model}:none" for model in kinds),
            *(f"row-kind:{model}:no_countertop" for model in kinds),
            *(f"row-candidate:{i}:{row}" for i, row in enumerate(rows, start=1)),
        ],
    )
    _ask(
        session,
        run_id,
        version_id,
        page_hash,
        "p0:row-choice",
        "slot-row-choice-v3",
        rows,
        picture=PICTURE,
    )
    return record


def build_v2_world(session: Session) -> V2World:
    from app.models.drawing import (
        DrawingItem,
        DrawingView,
        PartConfirmation,
        PartProposal,
        PartProposalRow,
        ViewRoleConfirmation,
    )
    from app.models.evidence import (
        ArchitectPairingRecord,
        NoCountertopAnswer,
        ObservationCandidate,
        SlotRowReviewDecision,
    )
    from app.models.review import ReviewAction
    from tests.review.test_ledger import PAGE_HASH, _scenario
    from vocabulary.part_kinds import PartKind
    from workflow.parts import BALA_CONFIRMER
    from workflow.view_roles import CODE_CONFIRMER

    scenario = _scenario(session, media_box=["0", "0", "612", "792"])
    revision_id = scenario.package_revision_id
    version_id = scenario.document_version_id
    page_id = scenario.page_id
    run = _run(session, revision_id, "extraction.form_reader")

    # One countertop row of two pieces and its overall, each span asked once.
    spans: dict[str, UUID] = {}
    for slot, box, printed in (
        ("slot:0", "10,20,30,40", '2"'),
        ("slot:1", "40,20,60,40", '12 1/2"'),
        ("slot:overall", "10,50,60,70", '14 1/2"'),
    ):
        spans[slot] = _candidate(
            session,
            version_id,
            page_id,
            run.id,
            ["slot-reader", slot, f"slot-box:{box}", "row-rank:1", "row-slot-count:2"],
            raw_text=printed,
        )
        number = slot.removeprefix("slot:")
        _ask(
            session,
            run.id,
            version_id,
            PAGE_HASH,
            "p0:overall:0" if number == "overall" else f"p0:slot{number}:0",
            "claude-slot-span-v4",
            [str(spans[slot])],
        )

    # The row's wall question: code's ink and both AIs agreed on a layout by themselves.
    wall = ObservationCandidate(
        document_version_id=version_id,
        page_id=page_id,
        extraction_run_id=run.id,
        raw_text="walls: back_only",
        polygon=[[5, 5], [95, 5], [95, 80], [5, 80]],
        ambiguity_flags=[
            "wall-reader",
            "row-rank:1",
            "walls-sealed:back_only",
            "wall-ink:left=False,right=False",
            "wall-source:code-ink-and-both-ais",
        ],
    )
    session.add(wall)
    session.flush()
    _ask(session, run.id, version_id, PAGE_HASH, "p0:walls", "slot-walls-v3", [str(wall.id)])
    walls_decision = SlotRowReviewDecision(
        row_candidate_id=spans["slot:0"],
        wall_config="back_and_left",
        measurements={},
        confirmed_by=REVIEWER,
    )
    session.add(walls_decision)
    session.flush()

    # A decision carried from an earlier revision's identical row: the original is its own case.
    earlier_run = _run(session, revision_id, "extraction.form_reader")
    later_run = _run(session, revision_id, "extraction.form_reader")
    earlier_anchor = _candidate(
        session, version_id, page_id, earlier_run.id, ["slot-reader", "slot:0", "row-rank:1"]
    )
    later_anchor = _candidate(
        session, version_id, page_id, later_run.id, ["slot-reader", "slot:0", "row-rank:1"]
    )
    original_row = SlotRowReviewDecision(
        row_candidate_id=earlier_anchor,
        wall_config="island",
        measurements={},
        confirmed_by=REVIEWER,
    )
    session.add(original_row)
    session.flush()
    carried_row = SlotRowReviewDecision(
        row_candidate_id=later_anchor,
        wall_config="island",
        measurements={},
        confirmed_by=REVIEWER,
        carried_from_id=original_row.id,
    )
    session.add(carried_row)
    session.flush()

    # View roles: code set one view alone, and set another that a person then changed.
    region = {"space": "stored", "points": [["0", "0"], ["1", "1"]]}
    view = DrawingView(page_id=page_id, tag="A", region=region)
    other_view = DrawingView(page_id=page_id, tag="B", region=region)
    session.add_all([view, other_view])
    session.flush()
    code_role_only = ViewRoleConfirmation(
        drawing_view_id=other_view.id, role="shop", confirmed_by=CODE_CONFIRMER
    )
    code_role = ViewRoleConfirmation(
        drawing_view_id=view.id, role="shop", confirmed_by=CODE_CONFIRMER
    )
    session.add_all([code_role_only, code_role])
    session.flush()
    person_role = ViewRoleConfirmation(drawing_view_id=view.id, role="arch", confirmed_by=REVIEWER)
    session.add(person_role)
    session.flush()

    # Bala's parts of the row: code made the countertop by itself and a person took it back; a
    # person named piece 1 a cabinet; a name nobody listed named piece 0 a filler.
    extent = {"space": "stored", "points": [["0", "0"], ["1", "1"]]}

    def propose(kind: str, slot_index: int | None) -> UUID:
        proposal = PartProposal(
            drawing_view_id=view.id,
            kind=kind,
            extent=extent,
            source="bala",
            source_version="1",
            reason="invented",
        )
        session.add(proposal)
        session.flush()
        session.add(
            PartProposalRow(
                part_proposal_id=proposal.id,
                row_candidate_id=spans["slot:0"],
                slot_index=slot_index,
                extraction_run_id=run.id,
                kind_named=slot_index is None,
            )
        )
        session.flush()
        return proposal.id

    def decide(
        proposal: UUID, actor: str, kind: str | None, supersedes: UUID | None = None
    ) -> UUID:
        item_id = None
        if kind is not None:
            item = DrawingItem(
                drawing_view_id=view.id, item_type=PartKind(kind).item_type.value, extent=extent
            )
            session.add(item)
            session.flush()
            item_id = item.id
        decision = PartConfirmation(
            part_proposal_id=proposal,
            supersedes_id=supersedes,
            decision="withdrawn" if kind is None else "confirmed",
            kind=kind,
            drawing_item_id=item_id,
            confirmed_by=actor,
        )
        session.add(decision)
        session.flush()
        return decision.id

    countertop = propose("countertop", None)
    code_countertop = decide(countertop, BALA_CONFIRMER, "countertop")
    person_withdrawal = decide(countertop, REVIEWER, None, supersedes=code_countertop)
    person_piece = decide(propose("filler", 1), REVIEWER, "cabinet")
    stranger_piece = decide(propose("filler", 0), "ui reviewer", "filler")

    # Two pages every reader listed with no countertop: one confirmed in a batch click, and one
    # where the person said both AIs were wrong.
    batch_record = _row_record(
        session, run.id, version_id, page_id, PAGE_HASH, ["opus-5-5", "sonnet-5-5"]
    )
    agreed_record = _row_record(
        session, run.id, version_id, page_id, PAGE_HASH, ["opus-5-5", "sonnet-5-5"]
    )

    def answer(record: UUID, said: str, batch: UUID | None) -> UUID:
        row = NoCountertopAnswer(
            package_revision_id=revision_id,
            row_choice_candidate_id=record,
            page_id=page_id,
            page_number=1,
            page_content_hash=PAGE_HASH,
            reading_answer="row-pick:opus-5-5:none",
            answer=said,
            answered_by=REVIEWER,
            batch_id=batch,
        )
        session.add(row)
        session.flush()
        return row.id

    batch_answer = answer(batch_record, "no_countertop", uuid4())
    countertop_answer = answer(agreed_record, "countertop", None)

    # A person confirmed a reading was right, and dismissed a check's result.
    confirm = ReviewAction(
        review_session_id=scenario.review_session_id,
        finding_id=scenario.finding_id,
        package_revision_id=revision_id,
        action="confirm",
        actor=REVIEWER,
        original_observation_id=_slot_observation(session, scenario, spans["slot:1"], revision_id),
        resulting_observation_id=scenario.observation_id,
    )
    dismiss = ReviewAction(
        review_session_id=scenario.review_session_id,
        finding_id=scenario.finding_id,
        package_revision_id=revision_id,
        action="dismiss",
        actor=REVIEWER,
    )
    session.add_all([confirm, dismiss])
    session.flush()

    # Code and both AIs paired the row by themselves; a person replaced the pairing.
    code_pairing = ArchitectPairingRecord(
        package_revision_id=revision_id,
        page_id=page_id,
        row_anchor_candidate_id=spans["slot:0"],
        extraction_run_id=run.id,
        source="code+ais",
        status="paired",
        pairs=[
            {"kind": "overall", "architect_candidate_id": str(uuid4()), "vendor_slot_indices": [0]}
        ],
        details={},
    )
    session.add(code_pairing)
    session.flush()
    person_pairing = ArchitectPairingRecord(
        package_revision_id=revision_id,
        page_id=page_id,
        row_anchor_candidate_id=spans["slot:0"],
        extraction_run_id=None,
        source="reviewer",
        status="reviewer",
        pairs=[],
        details={},
        supersedes_id=code_pairing.id,
        decided_by=REVIEWER,
    )
    session.add(person_pairing)
    session.commit()
    return V2World(
        page_hash=PAGE_HASH,
        code_role_only=code_role_only.id,
        code_role=code_role.id,
        person_role=person_role.id,
        code_countertop=code_countertop,
        person_withdrawal=person_withdrawal,
        person_piece=person_piece,
        stranger_piece=stranger_piece,
        original_row=original_row.id,
        carried_row=carried_row.id,
        batch_answer=batch_answer,
        countertop_answer=countertop_answer,
        confirm_action=confirm.id,
        walls_decision=walls_decision.id,
        dismiss=dismiss.id,
        code_pairing=code_pairing.id,
        person_pairing=person_pairing.id,
    )


def test_a_code_part_or_view_role_is_never_a_candidate_and_is_counted_as_skipped(
    session: Session,
) -> None:
    world = build_v2_world(session)
    try:
        exported = export_candidates(session, database="synthetic", reviewers=PEOPLE)
    finally:
        session.rollback()
    found = _by_id(exported.candidates)
    for code_made in (
        f"part_confirmations:{world.code_countertop}",
        f"view_role_confirmations:{world.code_role}",
        f"view_role_confirmations:{world.code_role_only}",
        f"architect_pairing_records:{world.code_pairing}",
    ):
        assert code_made not in found
    assert not any(candidate.decided_by.startswith("code:") for candidate in exported.candidates)
    # Never silent: what was left out is counted, with why.
    assert exported.skipped["part_confirmations"] == {"code": 1}
    assert exported.skipped["view_role_confirmations"] == {"code": 2}
    assert exported.skipped["architect_pairing_records"] == {"machine source": 1}


def test_carried_and_batch_decisions_are_never_ready(session: Session) -> None:
    world = build_v2_world(session)
    found = _export(session)
    carried = found[f"slot_row_review_decisions:{world.carried_row}:walls"]
    assert carried.kind == "carried"
    assert not carried.convertible and carried.answer is None
    assert str(world.original_row) in (carried.not_convertible or "")
    # The original decision is its own candidate.
    assert found[f"slot_row_review_decisions:{world.original_row}:walls"].kind == "decided"

    batch = found[f"no_countertop_answers:{world.batch_answer}"]
    assert batch.question is not None and batch.question["family"] == "slot-row-choice"
    assert batch.answer is None, "a batch click is never a ready answer"
    assert batch.answer_needed is not None and "own look" in batch.answer_needed
    assert batch.recorded["batch"] is True


def test_an_unknown_actor_is_not_convertible(session: Session) -> None:
    world = build_v2_world(session)
    found = _export(session)
    stranger = found[f"part_confirmations:{world.stranger_piece}"]
    assert stranger.actor_status == "unknown"
    assert not stranger.convertible
    assert "not on the local list" in (stranger.not_convertible or "")
    assert found[f"part_confirmations:{world.person_piece}"].actor_status == "reviewer"

    # With no list given, nobody is a known reviewer: nothing is convertible.
    nobody = _export(session, Reviewers(names=None, source=None))
    assert nobody, "the decisions are still listed"
    assert not any(candidate.convertible for candidate in nobody.values())
    assert {candidate.actor_status for candidate in nobody.values()} == {"unknown"}


def test_a_confirm_action_gives_a_positive_label_case(session: Session) -> None:
    world = build_v2_world(session)
    found = _export(session)[f"review_actions:{world.confirm_action}"]
    assert found.agent == "akshara"
    assert found.kind == "confirmed"
    assert found.question == {
        "family": "claude-slot-span",
        "page_sha256": world.page_hash,
        "question_id": "p0:slot1:0",
        "slot_box": "40,20,60,40",
    }
    # The answer is the stored reading's text, which the person sees and checks.
    assert found.answer == {"texts": ['12 1/2"']}
    assert found.convertible and found.mistake_needed is None


def test_a_person_confirming_a_piece_kind_gives_a_bala_kinds_case(session: Session) -> None:
    world = build_v2_world(session)
    found = _export(session)[f"part_confirmations:{world.person_piece}"]
    assert found.agent == "bala"
    assert found.kind == "corrected"  # suggested a filler, the person said cabinet
    assert found.question == {
        "family": "piece-kind",
        "page_sha256": world.page_hash,
        "question_id": "p0:piece1",
        "pieces": 2,
    }
    assert found.answer == {"kinds": ["cabinet"]}
    assert found.convertible and found.overturned is None


def test_a_person_replacing_a_code_item_is_flagged_overturned(session: Session) -> None:
    from workflow.parts import BALA_CONFIRMER
    from workflow.view_roles import CODE_CONFIRMER

    world = build_v2_world(session)
    found = _export(session)
    withdrawal = found[f"part_confirmations:{world.person_withdrawal}"]
    assert withdrawal.overturned is not None
    assert withdrawal.overturned["by"] == BALA_CONFIRMER
    assert withdrawal.overturned["record"] == f"part_confirmations:{world.code_countertop}"
    assert withdrawal.kind == "corrected"

    role = found[f"view_role_confirmations:{world.person_role}"]
    assert role.agent == "bala"
    assert role.overturned is not None and role.overturned["by"] == CODE_CONFIRMER
    assert role.overturned["answer"] == "shop"
    assert not role.convertible

    pairing = found[f"architect_pairing_records:{world.person_pairing}"]
    assert pairing.overturned is not None and pairing.overturned["by"] == "code+ais"

    walls = found[f"slot_row_review_decisions:{world.walls_decision}:walls"]
    assert walls.overturned is not None
    assert walls.overturned["answer"] == "back_only"
    assert walls.overturned["by"] == "wall-source:code-ink-and-both-ais"

    countertop = found[f"no_countertop_answers:{world.countertop_answer}"]
    assert countertop.overturned is not None and countertop.overturned["by"] == "both AIs"


def test_a_wall_choice_names_its_walls_question_and_the_person_states_the_drawn_ends(
    session: Session,
) -> None:
    world = build_v2_world(session)
    walls = _export(session)[f"slot_row_review_decisions:{world.walls_decision}:walls"]
    assert walls.question == {
        "family": "slot-walls",
        "page_sha256": world.page_hash,
        "question_id": "p0:walls",
        "row_box": WALL_BOX,
    }
    assert walls.recorded == {"wall_config": "back_and_left"}
    assert walls.answer is None and walls.convertible
    assert walls.answer_needed is not None and "drawn" in walls.answer_needed


def test_there_is_a_countertop_asks_the_person_for_the_box(session: Session) -> None:
    world = build_v2_world(session)
    found = _export(session)[f"no_countertop_answers:{world.countertop_answer}"]
    assert found.agent == "rekha" and found.kind == "corrected"
    assert found.question is not None and found.question["picture_sha256"] == PICTURE
    assert found.answer is None and found.convertible
    assert found.answer_needed is not None and "box" in found.answer_needed
    assert "not_among_boxes" in found.answer_needed


def test_a_correction_asks_misread_or_vendor_and_it_starts_unanswered(session: Session) -> None:
    world = build_world(session)
    found = _export(session)
    assert found[f"correction_ledger:{world.ledger_slot}"].mistake_needed == "unanswered"
    typed = found[f"slot_row_review_decisions:{world.row_decision_latest}:piece_widths:1"]
    assert typed.mistake_needed == "unanswered"
    assert found[f"no_countertop_answers:{world.no_countertop_reader}"].mistake_needed is None


def test_a_dismiss_is_counted_for_the_findings_and_never_convertible(session: Session) -> None:
    world = build_v2_world(session)
    found = _export(session)[f"review_actions:{world.dismiss}"]
    assert found.agent == "kautilya"
    assert found.recorded == {"action": "dismiss"}
    assert not found.convertible and "finding" in (found.not_convertible or "")


def test_the_overturned_listing_and_the_v2_file_round_trip(session: Session, tmp_path: Any) -> None:
    from eval.smriti.candidates import (
        CANDIDATES_FORMAT,
        overturned_listing,
        read_candidates,
        render_counts,
        write_candidates,
    )

    world = build_v2_world(session)
    try:
        exported = export_candidates(session, database="synthetic", reviewers=PEOPLE)
    finally:
        session.rollback()
    listed = overturned_listing(exported)
    assert {candidate.candidate_id for candidate in listed} >= {
        f"part_confirmations:{world.person_withdrawal}",
        f"view_role_confirmations:{world.person_role}",
        f"no_countertop_answers:{world.countertop_answer}",
    }
    printed = render_counts(exported)
    assert "overturned" in printed and "report only" in printed

    path = tmp_path / "candidates.json"
    write_candidates(exported, path)
    assert json.loads(path.read_text())["format"] == CANDIDATES_FORMAT == "smriti-candidates/2"
    again = read_candidates(path)
    assert again.candidates == exported.candidates
    assert again.skipped == exported.skipped


def test_a_v1_candidates_file_is_still_read_and_its_people_are_unchecked(tmp_path: Any) -> None:
    from eval.smriti.candidates import _digest, read_candidates

    v1_entry = {
        "candidate_id": "no_countertop_answers:1",
        "table": "no_countertop_answers",
        "decision_id": "1",
        "agent": "rekha",
        "corrects": "a page listed as having no countertop",
        "kind": "confirmed",
        "decided_by": REVIEWER,
        "decided_at": "2026-10-14T00:00:00+00:00",
        "page_sha256": "a" * 64,
        "page_number": 1,
        "recorded": {"answer": "no_countertop"},
        "question": None,
        "answer": None,
        "answer_needed": None,
        "not_convertible": "invented",
        "status": NEEDS_CONFIRMATION,
    }
    document: dict[str, Any] = {
        "format": "smriti-candidates/1",
        "database": "synthetic",
        "exported_at": "2026-10-14T00:00:00+00:00",
        "code_version": "test",
        "candidates": [v1_entry],
        "counts": {},
    }
    document["sha256"] = _digest(document)
    path = tmp_path / "v1.json"
    path.write_text(json.dumps(document))
    (only,) = read_candidates(path).candidates
    assert only.actor_status == "unchecked"
    assert only.overturned is None and only.mistake_needed is None
