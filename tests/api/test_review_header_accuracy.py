"""The simple review header: totals, the AI's share, and its accuracy from this set's answers (#1386).

Line 1 says how many numbers, how many both AIs settled (with a whole percent) and how many need a
person; the two groups add up to the total by construction. Line 2 says how many AI readings a
person checked on this revision and how many the AI had right, counted exactly as the correction
rate counts them (`eval/correction_rate.py`); nothing checked gives no percentage. A settled number
a person corrected shows in the split of settled numbers checked.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.main import API_PREFIX
from app.models import CanonicalObservation, EvidenceSupportingCandidate, ObservationCandidate
from app.models.review import ReviewAction
from app.models.verdicts import FindingEvidence, VerdictInput
from app.review.evidence_actions import (
    confirm_candidate_reading,
    correct_candidate_reading,
    correct_evidence,
)
from app.review.review_items import whole_percent
from eval.correction_rate import correction_rate, reviewed_readings
from tests.api.test_review_item_actions import _act, _items, _save_all_row_widths
from tests.api.test_review_items import _client
from tests.api.test_slot_rows import _package_rows, _run_current_checks
from tests.app.postgres_fixture import alembic_config
from tests.review.test_held_reading_actions import (
    _held_rows,
    _review_session_id,
    _reviewer,
    _revision,
    _width_finding,
)
from workflow.slot_row_scope import candidate_is_sealed

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


def _header(session: Session, project_id: UUID, package_id: UUID) -> dict[str, Any]:
    response = _client(session, project_id).get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/review-items"
    )
    assert response.status_code == 200, response.text
    header: dict[str, Any] = response.json()["header"]
    return header


def test_settled_and_need_you_add_up_and_nothing_checked_has_no_accuracy(
    session: Session, tmp_path: Path
) -> None:
    """Input: two rows, four numbers, one held. Outcome: 4 numbers, 3 settled by both AIs (75%),
    1 needs you; nothing checked yet, so no accuracy percentage at all (never 0% or 100%)."""
    project_id, package_id, anchors = _held_rows(session)
    _width_finding(session, package_id, anchors[0], tmp_path)
    session.commit()

    header = _header(session, project_id, package_id)

    assert (header["numbers"], header["confirmed_by_ai"], header["need_you"]) == (4, 3, 1)
    assert header["confirmed_by_ai"] + header["need_you"] == header["numbers"]
    assert header["settled_percent"] == 75
    assert (header["readings_checked"], header["ai_right"], header["ai_right_percent"]) == (
        0,
        0,
        None,
    )
    assert (header["settled_checked"], header["settled_right"]) == (0, 0)


def test_accuracy_counts_confirms_and_fixes_exactly_as_the_correction_rate(
    session: Session, tmp_path: Path
) -> None:
    """Input: the held 20" fixed to 21", the check re-run, then the AI's 20" confirmed (both
    tagged held-reading answers or ledgered fixes). Outcome: 2 checked, AI right on 1 (50%), the
    same pair the correction rate counts; neither was a settled number."""
    project_id, package_id, anchors = _held_rows(session)
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    sitting = _review_session_id(session, package_id)
    correct_candidate_reading(
        session,
        principal=_reviewer(project_id),
        review_session_id=sitting,
        finding_id=finding.id,
        candidate_id=anchors[0],
        corrected_value='21"',
    )
    session.commit()
    decided = _width_finding(session, package_id, anchors[0], tmp_path)
    confirm_candidate_reading(
        session,
        principal=_reviewer(project_id),
        review_session_id=sitting,
        finding_id=decided.id,
        candidate_id=anchors[0],
        expected_value=Fraction(20),
    )
    session.commit()

    header = _header(session, project_id, package_id)

    (rate,) = correction_rate(session, window=timedelta(days=1)).values()
    assert header["readings_checked"] == rate.denominator == 2
    assert header["readings_checked"] - header["ai_right"] == rate.numerator == 1
    assert header["ai_right_percent"] == 50
    assert (header["settled_checked"], header["settled_right"]) == (0, 0)
    assert header["confirmed_by_ai"] + header["need_you"] == header["numbers"]


def test_a_settled_number_a_person_corrected_shows_in_the_settled_split(
    session: Session, tmp_path: Path
) -> None:
    """Input: a number both AIs settled, which a person corrects on the check's evidence. Outcome:
    it counts as checked and wrong, and the settled split says 0 right of 1: the safety number."""
    project_id, package_id, anchors = _held_rows(session)
    finding = _width_finding(session, package_id, anchors[1], tmp_path)
    # The piece width both AIs settled, as the check used it: its evidence cites each reader's own
    # reading of the sealed label (`supports:<label>`), never the label itself.
    used = session.scalars(
        select(VerdictInput).where(
            VerdictInput.check_run_id == finding.check_run_id,
            VerdictInput.operand_name == "piece_widths[0]",
        )
    ).one()
    observation = session.get(CanonicalObservation, used.canonical_observation_id)
    assert observation is not None
    readers = session.scalars(
        select(ObservationCandidate)
        .join(
            EvidenceSupportingCandidate,
            EvidenceSupportingCandidate.candidate_id == ObservationCandidate.id,
        )
        .where(EvidenceSupportingCandidate.canonical_observation_id == observation.id)
    ).all()
    assert readers and not any(candidate_is_sealed(reader) for reader in readers)
    # The advanced view corrects a finding's linked evidence; link it as that view would show it.
    session.add(
        FindingEvidence(
            finding_id=finding.id, canonical_observation_id=observation.id, role="piece_widths[0]"
        )
    )
    session.flush()
    correct_evidence(
        session,
        principal=_reviewer(project_id),
        review_session_id=_review_session_id(session, package_id),
        finding_id=finding.id,
        observation_id=observation.id,
        corrected_value=Fraction(observation.value_numerator + 1, observation.value_denominator),
    )
    session.commit()

    header = _header(session, project_id, package_id)

    assert (header["readings_checked"], header["ai_right"], header["ai_right_percent"]) == (
        1,
        0,
        0,
    )
    assert (header["settled_checked"], header["settled_right"]) == (1, 0)
    (reading,) = reviewed_readings(session, revision_id=_revision(session, package_id).id)
    assert reading.corrected


def test_answers_on_another_revision_are_never_counted(session: Session, tmp_path: Path) -> None:
    """Input: a confirmation on this revision. Outcome: counted here, and nowhere else."""
    project_id, package_id, anchors = _held_rows(session)
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    confirm_candidate_reading(
        session,
        principal=_reviewer(project_id),
        review_session_id=_review_session_id(session, package_id),
        finding_id=finding.id,
        candidate_id=anchors[0],
        expected_value=Fraction(20),
    )
    session.commit()

    assert len(reviewed_readings(session, revision_id=_revision(session, package_id).id)) == 1
    assert reviewed_readings(session, revision_id=uuid4()) == []
    assert _header(session, project_id, package_id)["readings_checked"] == 1


def test_an_agree_on_a_failure_is_a_decision_never_an_answer_on_a_reading(
    session: Session, tmp_path: Path
) -> None:
    """Input: a person agrees with a FAIL, and says a by-hand result is right. Outcome: both are
    decisions on a result, not answers about one AI reading, so the accuracy counts neither."""
    project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="readers"
    )
    _save_all_row_widths(session, project_id, package_id, anchors[0])
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    client = _client(session, project_id)
    listed = _items(client, project_id, package_id)
    failure = listed["failures"][0]
    agreed = _act(
        client,
        project_id,
        package_id,
        failure["key"],
        "agree",
        {"expected_finding_id": failure["finding_id"]},
    )
    assert agreed.status_code == 201, agreed.text
    by_hand = [
        item
        for item in listed["items"]
        if "checked_right" in item["actions"] and item["finding_id"] is not None
    ]
    for item in by_hand[:1]:
        answered = _act(
            client,
            project_id,
            package_id,
            item["key"],
            "answer",
            {"answer": "checked_right", "expected_finding_id": item["finding_id"]},
        )
        assert answered.status_code == 201, answered.text
    session.commit()

    assert session.query(ReviewAction).filter_by(action="confirm").count() == 1 + len(by_hand[:1])
    header = _header(session, project_id, package_id)
    assert (header["readings_checked"], header["ai_right"], header["ai_right_percent"]) == (
        0,
        0,
        None,
    )


@pytest.mark.parametrize(
    ("part", "whole", "percent"),
    [
        (23, 51, 45),
        (11, 12, 92),
        (1, 2, 50),
        (0, 5, 0),
        (5, 5, 100),
        (199, 200, 99),  # never "100%" unless every one
        (1, 201, 1),  # never "0%" unless none
        (0, 0, None),  # nothing to divide: no percentage
    ],
)
def test_whole_percent_is_exact_and_never_rounds_to_all_or_none(
    part: int, whole: int, percent: int | None
) -> None:
    assert whole_percent(part, whole) == percent
