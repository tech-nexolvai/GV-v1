"""The countertop-level scorecard, the honest metric (#1321).

Verification for: `eval/pariksha/countertops.py`. For each keyed countertop: did the app decide it
automatically (a PASS or FAIL before any person acted), and was that decision right by the keys?
Every page, picture, line, number and finding here is invented; no model is called.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from eval.pariksha.countertops import (
    CountertopFinding,
    FindingNumber,
    load_countertop_findings,
    render_countertops,
    score_countertops,
)
from eval.pariksha.keys import KEY_FORMAT, AnswerKey, load_key
from eval.pariksha.row_geometry import ROW_BOXES_FORMAT, RowBoxes, load_row_boxes

pytest_plugins = ("tests.app.postgres_fixture",)

PAGE_A = "a" * 64
PAGE_B = "b" * 64
PAGE_C = "c" * 64
PAGE_D = "d" * 64
PICTURE_A = "3" * 64
PICTURE_B = "4" * 64
LINE = {"y": 412.5, "x0": 100, "x1": 400}
CABINETS = {"y": 430.25, "x0": 110, "x1": 390}
SPAN = "10,20,30,40"


def _rows_key(name: str = "rows", **truths: dict[str, Any]) -> AnswerKey:
    pages = {"A": PAGE_A, "B": PAGE_B, "C": PAGE_C, "D": PAGE_D}
    geometry = any("row_at" in answer for answer in truths.values())
    key = load_key(
        {
            "format": KEY_FORMAT,
            "agent": "rekha",
            "name": name,
            "version": "1",
            **({"row_tolerance_pt": 1} if geometry else {}),
            "cases": [
                {
                    "family": "slot-row-choice",
                    "page_sha256": pages[page],
                    "question_id": "p1:row-choice",
                    "answer": answer,
                }
                for page, answer in truths.items()
            ],
        }
    )
    assert isinstance(key, AnswerKey)
    return key


def _label_key(texts: list[str], page: str = PAGE_A) -> AnswerKey:
    key = load_key(
        {
            "format": KEY_FORMAT,
            "agent": "akshara",
            "name": "labels",
            "version": "1",
            "cases": [
                {
                    "family": "claude-slot-span",
                    "page_sha256": page,
                    "question_id": "p1:slot0:0",
                    "slot_box": SPAN,
                    "answer": {"texts": texts},
                }
            ],
        }
    )
    assert isinstance(key, AnswerKey)
    return key


def _boxes() -> RowBoxes:
    return load_row_boxes(
        {
            "format": ROW_BOXES_FORMAT,
            "pictures": [
                {
                    "picture_sha256": picture,
                    "page_sha256": page,
                    "boxes": [CABINETS, LINE],
                    "how": "invented",
                }
                for picture, page in ((PICTURE_A, PAGE_A), (PICTURE_B, PAGE_B))
            ],
        }
    )


def _finding(
    page: str = PAGE_A,
    outcome: str = "FAIL",
    *,
    box: int | None = 2,
    picture: str = PICTURE_A,
    numbers: tuple[FindingNumber, ...] = (),
) -> CountertopFinding:
    return CountertopFinding(
        page_sha256=page,
        label="Countertop row on page 1",
        outcome=outcome,
        reason="invented",
        row_choice=box,
        pictures=(picture,),
        numbers=numbers,
    )


def _line(card: Any, page: str) -> Any:
    (line,) = [line for line in card.lines if line.page_sha256 == page]
    return line


KEYED_SPAN = (FindingNumber(operand="piece_widths[0]", inches=Fraction(3, 4), slot_box=SPAN),)


def test_an_automatic_decision_on_the_keyed_line_is_right_and_a_held_one_is_not_decided() -> None:
    card = score_countertops(
        [_rows_key(A={"row_at": LINE}, B={"row_at": LINE})],
        [_finding(numbers=KEYED_SPAN), _finding(PAGE_B, "REVIEW_REQUIRED", picture=PICTURE_B)],
        row_boxes=_boxes(),
        label_keys=[_label_key(['3/4"'])],
    )

    decided, held = _line(card, PAGE_A), _line(card, PAGE_B)
    assert (decided.automatic, decided.line, decided.right) == (True, "right", True)
    assert (held.automatic, held.right) == (False, None)
    assert "REVIEW_REQUIRED" in held.why
    assert card.totals == {
        "keyed_countertops": 2,
        "automatic": 1,
        "automatic_right": 1,
        "automatic_right_line_numbers_not_keyed": 0,
        "automatic_wrong": 0,
        "automatic_unknown": 0,
        "keyed_no_countertop": 0,
        "no_countertop_decided": 0,
        "keyed_not_in_run": 0,
    }


def test_keyed_pages_another_drawing_set_holds_are_counted_apart_not_as_undecided() -> None:
    card = score_countertops(
        [_rows_key(A={"row_at": LINE}, B={"row_at": LINE}, C={"kind": "no_countertop"})],
        [_finding()],
        row_boxes=_boxes(),
        read_pages={PAGE_C},
    )

    assert [line.page_sha256 for line in card.lines] == [PAGE_A, PAGE_C]
    assert card.totals["keyed_countertops"] == 1
    assert card.totals["keyed_not_in_run"] == 1
    assert "did not read" in render_countertops(card)


def test_an_automatic_decision_on_another_line_is_wrong() -> None:
    card = score_countertops([_rows_key(A={"row_at": LINE})], [_finding(box=1)], row_boxes=_boxes())

    line = _line(card, PAGE_A)
    assert (line.automatic, line.line, line.right) == (True, "wrong", False)
    assert card.totals["automatic_wrong"] == 1


def test_without_the_pictures_box_geometry_the_line_is_unknown_never_right() -> None:
    card = score_countertops(
        [_rows_key(A={"row_at": LINE})], [_finding(picture="9" * 64)], row_boxes=_boxes()
    )

    line = _line(card, PAGE_A)
    assert (line.line, line.right) == ("unknown", None)
    assert "no box geometry" in line.why
    assert card.totals["automatic_unknown"] == 1


def test_a_keyed_number_that_differs_makes_the_decision_wrong_and_unkeyed_ones_are_counted() -> (
    None
):
    numbers = (
        FindingNumber(operand="piece_widths[0]", inches=Fraction(3, 4), slot_box=SPAN),
        FindingNumber(operand="countertop_width", inches=Fraction(36), slot_box="1,2,3,4"),
    )
    right = score_countertops(
        [_rows_key(A={"row_at": LINE})],
        [_finding(numbers=numbers)],
        row_boxes=_boxes(),
        label_keys=[_label_key(['3/4"'])],
    )
    wrong = score_countertops(
        [_rows_key(A={"row_at": LINE})],
        [_finding(numbers=numbers)],
        row_boxes=_boxes(),
        label_keys=[_label_key(['1 3/4"'])],
    )

    assert (_line(right, PAGE_A).numbers_keyed, _line(right, PAGE_A).numbers_wrong) == (1, 0)
    assert _line(right, PAGE_A).numbers_total == 2
    # The right line and no keyed number wrong, but one number has no key: not called right.
    assert (_line(right, PAGE_A).line, _line(right, PAGE_A).right) == ("right", None)
    assert "1 of 2 number(s) not keyed" in _line(right, PAGE_A).why
    assert right.totals["automatic_right_line_numbers_not_keyed"] == 1
    assert right.totals["automatic_right"] == 0
    assert (_line(wrong, PAGE_A).numbers_wrong, _line(wrong, PAGE_A).right) == (1, False)
    assert "piece_widths[0]" in _line(wrong, PAGE_A).why


def test_a_page_keyed_no_countertop_that_gets_an_automatic_decision_is_wrong() -> None:
    card = score_countertops(
        [_rows_key(C={"kind": "no_countertop"}, D={"kind": "no_countertop"})],
        [_finding(PAGE_C, "PASS")],
        row_boxes=_boxes(),
    )

    assert card.totals["keyed_no_countertop"] == 2
    assert card.totals["no_countertop_decided"] == 1
    assert _line(card, PAGE_C).right is False
    assert _line(card, PAGE_D).right is None


def test_a_keyed_countertop_with_no_finding_says_so() -> None:
    card = score_countertops([_rows_key(A={"row_at": LINE})], [], row_boxes=_boxes())

    line = _line(card, PAGE_A)
    assert (line.automatic, line.right) == (False, None)
    assert "no countertop finding" in line.why


def test_two_keys_giving_one_countertop_different_lines_are_refused() -> None:
    other = {"row_at": CABINETS}
    with pytest.raises(ValueError, match="different"):
        score_countertops(
            [_rows_key(A={"row_at": LINE}), _rows_key("other", A=other)],
            [_finding()],
            row_boxes=_boxes(),
        )
    # The same answer in two keys is one countertop, named by both.
    card = score_countertops(
        [_rows_key(A={"row_at": LINE}), _rows_key("copy", A={"row_at": LINE})],
        [_finding()],
        row_boxes=_boxes(),
    )
    assert _line(card, PAGE_A).keys == ("copy", "rows")


def test_the_rendered_scorecard_states_the_honest_metric_in_plain_words() -> None:
    card = score_countertops(
        [_rows_key(A={"row_at": LINE}, B={"row_at": LINE})],
        [_finding(), _finding(PAGE_B, "REVIEW_REQUIRED", picture=PICTURE_B)],
        row_boxes=_boxes(),
    )

    text = render_countertops(card)
    assert "decided automatically: 1 of 2" in text
    assert "right (right line, every number keyed and right): 0" in text
    assert (
        "right line, numbers not keyed (whether the PASS or FAIL is right is NOT checked): 1"
        in (text)
    )
    assert "whether this FAIL is right is not checked" in text
    assert "wrong: 0" in text


def test_a_finding_with_no_single_picture_of_its_own_cannot_be_told() -> None:
    for pictures in ((), (PICTURE_A, PICTURE_B)):
        finding = CountertopFinding(
            page_sha256=PAGE_A,
            label="Countertop row on page 1",
            outcome="FAIL",
            reason="invented",
            row_choice=2,
            pictures=pictures,
            numbers=KEYED_SPAN,
        )
        card = score_countertops(
            [_rows_key(A={"row_at": LINE})],
            [finding],
            row_boxes=_boxes(),
            label_keys=[_label_key(['3/4"'])],
        )
        assert (_line(card, PAGE_A).line, _line(card, PAGE_A).right) == ("unknown", None)
        assert "row picture" in _line(card, PAGE_A).why


# --- read from a stored run ------------------------------------------------------------------


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


def _width_rule(session: Session) -> Any:
    import hashlib

    from app.models.rules import RuleDefinition, RuleSnapshot

    definition = RuleDefinition(rule_id="CT-WIDTH-001")
    session.add(definition)
    session.flush()
    body = '{"id":"CT-WIDTH-001","invented":true}'
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(body.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=body,
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _row_candidate(session: Session, version: Any, page: Any, run: Any, flags: list[str]) -> Any:
    from app.models.evidence import ObservationCandidate

    candidate = ObservationCandidate(
        document_version_id=version.id,
        page_id=page.id,
        extraction_run_id=run.id,
        raw_text="invented",
        polygon=[[0, 0], [1, 1]],
        ambiguity_flags=flags,
    )
    session.add(candidate)
    session.flush()
    return candidate


def _add_finding(
    session: Session, revision: Any, snapshot: Any, row: Any, outcome: str, at: datetime
) -> Any:
    from app.models.verdicts import CheckRun, Finding

    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version="verdict-test",
        created_at=at,
    )
    session.add(run)
    session.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=outcome,
        severity="CRITICAL",
        trace={},
        parameter_set_versions={},
        reason=f"invented {outcome}",
        scope_row_candidate_id=row.id,
        scope_label="Countertop row on page 1",
        created_at=at,
    )
    session.add(finding)
    session.flush()
    return run


def _add_number(
    session: Session, check_run: Any, version: Any, page: Any, run: Any, inches: Fraction
) -> None:
    from app.models.evidence import CanonicalObservation, EvidenceSupportingCandidate
    from app.models.verdicts import VerdictInput

    # Two readers' candidates for the same span: a corroborated value needs both.
    candidates = [
        _row_candidate(session, version, page, run, ["slot-reader", "slot:0", f"slot-box:{SPAN}"])
        for _ in range(2)
    ]
    observation = CanonicalObservation(
        document_version_id=version.id,
        page_id=page.id,
        document_role="SHOP",
        polygon=[["0", "0"], ["1", "1"]],
        coordinate_space="stored",
        semantic_type="countertop_piece_width",
        value_numerator=inches.numerator,
        value_denominator=inches.denominator,
        unit="in",
        status="CORROBORATED",
        authority="AUTHORITATIVE",
    )
    session.add(observation)
    session.flush()
    for candidate, role in zip(candidates, ("primary", "corroborating"), strict=True):
        session.add(
            EvidenceSupportingCandidate(
                canonical_observation_id=observation.id, candidate_id=candidate.id, role=role
            )
        )
    session.add(
        VerdictInput(
            check_run_id=check_run.id,
            operand_name="piece_widths[0]",
            value_numerator=inches.numerator,
            value_denominator=inches.denominator,
            unit="in",
            evidence_status="CORROBORATED",
            canonical_observation_id=observation.id,
        )
    )
    session.flush()


def _add_row_question(
    session: Session, run: Any, version: Any, picture: str, candidates: list[str] | None = None
) -> None:
    from app.models.runs import ModelInvocation

    session.add(
        ModelInvocation(
            extraction_run_id=run.id,
            model_id="reader-one",
            prompt_id="slot-row-choice-v5",
            template_id="slot-row-choice-v5",
            input_tokens=10,
            output_tokens=5,
            cost_micros=0,
            latency_ms=1,
            outcome="ok",
            private_raw_response=json.dumps({"row": 2, "kind": "row", "also": [], "why": "x"}),
            reader_page_index=0,
            reader_attempt_number=1,
            reader_question_packet={
                "question_id": "p0:row-choice",
                "document_version_id": str(version.id),
                "page_index": 0,
                "source_page_sha256": "1" * 64,
                "prompt_id": "slot-row-choice-v5",
                "candidate_ids": candidates or [str(uuid4()), str(uuid4())],
                "candidate_count": 2,
                "packet_sha256": "p" * 8,
                "images": {"numbered_vendor_view": {"sha256": picture, "storage_key": None}},
            },
        )
    )
    session.flush()


def test_the_stored_run_gives_each_row_its_first_width_finding_before_any_person_acted(
    session: Session,
) -> None:
    from tests.workflow.test_slot_reader import _scaffold

    revision, version, page, run = _scaffold(session)  # its page hash is "1" * 64
    snapshot = _width_rule(session)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    asked = [str(uuid4()), str(uuid4())]
    first_row = _row_candidate(
        session,
        version,
        page,
        run,
        ["slot-reader", "row-choice:2", f"row-choice-candidate:{asked[1]}"],
    )
    second_row = _row_candidate(session, version, page, run, ["slot-reader", "row-rank:1"])
    # The first row was held, then passed once a person answered: only the first counts.
    _add_finding(session, revision, snapshot, first_row, "REVIEW_REQUIRED", start)
    _add_finding(session, revision, snapshot, first_row, "PASS", start + timedelta(seconds=9))
    failed = _add_finding(session, revision, snapshot, second_row, "FAIL", start)
    _add_number(session, failed, version, page, run, Fraction(3, 4))
    _add_row_question(session, run, version, PICTURE_A, asked)
    # Another revision asked about a page with the same hash on another picture: never used.
    _other_revision, other_version, _other_page, other_run = _scaffold(session)
    _add_row_question(session, other_run, other_version, PICTURE_B)
    session.commit()

    findings = load_countertop_findings(session)

    assert sorted((f.outcome, f.row_choice) for f in findings) == [
        ("FAIL", None),
        ("REVIEW_REQUIRED", 2),
    ]
    assert {f.page_sha256 for f in findings} == {"1" * 64}
    assert {f.pictures for f in findings} == {(PICTURE_A,)}
    (failed_finding,) = [f for f in findings if f.outcome == "FAIL"]
    assert failed_finding.numbers == (
        FindingNumber(operand="piece_widths[0]", inches=Fraction(3, 4), slot_box=SPAN),
    )


def test_reading_findings_is_read_only_and_refuses_to_mix_revisions(session: Session) -> None:
    from tests.workflow.test_slot_reader import _scaffold

    snapshot = _width_rule(session)
    for _ in range(2):
        revision, version, page, run = _scaffold(session)
        row = _row_candidate(session, version, page, run, ["slot-reader"])
        _add_finding(session, revision, snapshot, row, "FAIL", datetime(2026, 1, 1, tzinfo=UTC))
    session.commit()

    with pytest.raises(ValueError, match="2 package revisions"):
        load_countertop_findings(session)
    assert len(load_countertop_findings(session, package_revision_id=revision.id)) == 1
