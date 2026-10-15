"""#1367: a person's answer about a held reading is a tagged `confirm`, never a decision.

Anant decided (2026-10-16, "Separate tag"): "the drawn length disagreed" on a confirmed AI reading is
a tag on the confirm review action, shown in the history, never the note (a note on a confirm is
what sign-off reads as the person deciding the result). And an answer through a result already
decided (PASS or FAIL) is written as a `confirm` too, so the correction rate counts it, while every
reader of review actions as decisions skips it.

Fail-first, one test per reader:

- the database: tags only from the known set, only on a `confirm` of a reading tagged
  `held-reading-answer`; the migration writes out the model's CHECK;
- `evidence_actions.confirm_candidate_reading`: the tags, never a note;
- `carry_over.decision_records` / `decision_holds` (sign-off, readiness, the findings list, the
  review list): an answer never stands, never hides the decision before it, never displaces a
  carried one; a FAIL is never "decided" by it;
- `reports/vendor_patterns.py` (Smriti's person-confirmed FAILs): never counted;
- `reviewer_marks` (the cloud PDF's "checked by hand" marks): the decision before it stays shown;
- `eval/correction_rate.py`: counted, through a FAIL too; Smriti's export: a confirmed reading;
- the history: the tags are returned.

All synthetic: invented values and names, no model call.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.api.review import list_finding_actions
from app.db.session import session_factory
from app.models import ReviewAction
from app.models.review import TAGS_CHECK, ReviewActionKind, ReviewActionTag
from app.models.verdicts import Finding
from app.review.approval import _unaddressed
from app.review.carry_over import (
    DecisionRecords,
    EffectiveDecision,
    decision_holds,
    decision_records,
)
from app.review.evidence_actions import ReadingDecision, confirm_candidate_reading
from app.review.requirements import needs_note
from app.review.reviewer_marks import reviewer_marks_for
from app.review.session import record_action
from app.review.signed_record import ReviewDisposition, SignedReviewFinding
from eval.correction_rate import correction_rate
from reports.vendor_patterns import _person_confirmed
from tests.app.postgres_fixture import alembic_config
from tests.review.test_held_reading_actions import (
    REVIEWER,
    _held_rows,
    _held_slot_flags,
    _review_session_id,
    _reviewer,
    _revision,
    _width_finding,
)

pytest_plugins = ("tests.app.postgres_fixture",)

REVISION = "0100_review_action_tags"
BEFORE = "0099_draft_redline_output"
HELD = ReviewActionTag.HELD_READING_ANSWER.value
DRAWN = ReviewActionTag.DRAWN_LENGTH_DISAGREED.value
NOTE = "TEST synthetic reason"


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


def _now() -> datetime:
    return datetime.now(UTC)


def _confirm(
    session: Session, project_id: UUID, package_id: UUID, finding: Finding, label: UUID
) -> ReadingDecision:
    decision = confirm_candidate_reading(
        session,
        principal=_reviewer(project_id),
        review_session_id=_review_session_id(session, package_id),
        finding_id=finding.id,
        candidate_id=label,
        expected_value=Fraction(20),
    )
    session.commit()
    return decision


def _decide(
    session: Session, package_id: UUID, finding: Finding, kind: ReviewActionKind
) -> ReviewAction:
    action = record_action(
        session,
        review_session_id=_review_session_id(session, package_id),
        finding_id=finding.id,
        action=kind,
        actor=REVIEWER,
        note=NOTE,
    )
    session.commit()
    return action


def _fail_with_a_taken_back_fix(
    session: Session, tmp_path: Path
) -> tuple[UUID, UUID, dict[int, UUID], Finding]:
    """The held label fixed to 21", the checks re-run: code decides FAIL. Nothing answered yet."""
    from app.review.evidence_actions import correct_candidate_reading

    project_id, package_id, anchors = _held_rows(session)
    waiting = _width_finding(session, package_id, anchors[0], tmp_path)
    correct_candidate_reading(
        session,
        principal=_reviewer(project_id),
        review_session_id=_review_session_id(session, package_id),
        finding_id=waiting.id,
        candidate_id=anchors[0],
        corrected_value='21"',
    )
    session.commit()
    decided = _width_finding(session, package_id, anchors[0], tmp_path)
    assert decided.outcome == "FAIL"
    return project_id, package_id, anchors, decided


# -- the database -----------------------------------------------------------------------------------


def test_the_migration_follows_the_draft_cloud_and_writes_out_the_models_check() -> None:
    script = ScriptDirectory.from_config(alembic_config())
    found = script.get_revision(REVISION)
    assert found is not None and found.down_revision == BEFORE
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.TAGS_CHECK == TAGS_CHECK


@pytest.mark.parametrize(
    ("kind", "tags", "with_observations"),
    [
        (ReviewActionKind.DISMISS, [HELD], False),  # a decision never carries a tag
        (ReviewActionKind.CONFIRM, [HELD], False),  # only on a confirm of a reading
        (ReviewActionKind.CONFIRM, [DRAWN], True),  # never without held-reading-answer
        (ReviewActionKind.CONFIRM, [HELD, "decided"], True),  # only the known tags
        (ReviewActionKind.CONFIRM, {"tag": HELD}, True),  # an array
    ],
)
def test_the_database_refuses_a_tag_off_an_answer_about_a_held_reading(
    session: Session, tmp_path: Path, kind: ReviewActionKind, tags: object, with_observations: bool
) -> None:
    project_id, package_id, anchors = _held_rows(session)
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    answer = _confirm(session, project_id, package_id, finding, anchors[0]).action
    session.add(
        ReviewAction(
            review_session_id=answer.review_session_id,
            finding_id=finding.id,
            package_revision_id=finding.package_revision_id,
            action=kind.value,
            actor=REVIEWER,
            note=NOTE,
            original_observation_id=answer.original_observation_id if with_observations else None,
            resulting_observation_id=answer.resulting_observation_id if with_observations else None,
            tags=tags,
        )
    )
    with pytest.raises(IntegrityError, match="review_action_tags"):
        session.flush()
    session.rollback()


# -- item 1: the drawn length disagreed -----------------------------------------------------------


def test_a_confirmed_reading_whose_drawn_length_disagreed_is_tagged_never_noted(
    session: Session, tmp_path: Path
) -> None:
    """Input: the held label carries the reader's `drawn-length` hold (both AIs read 20", the drawn
    length disagreed); a person confirms 20". Outcome: the confirm action is tagged
    `held-reading-answer` and `drawn-length-disagreed`, with no note; the history returns both tags;
    nothing is decided: the result still blocks sign-off and its decision does not hold."""
    project_id, package_id, anchors = _held_rows(
        session, slot_flags=_held_slot_flags("drawn-length")
    )
    waiting = _width_finding(session, package_id, anchors[0], tmp_path)
    assert waiting.outcome == "REVIEW_REQUIRED"

    action = _confirm(session, project_id, package_id, waiting, anchors[0]).action

    assert (action.action, action.note, action.tags) == ("confirm", None, [HELD, DRAWN])
    assert needs_note(waiting.outcome, action.action)  # the note rule itself is unchanged
    records = decision_records(session, [waiting.id])
    assert waiting.id not in records.decisions
    assert not decision_holds(waiting, records, when=_now())
    assert waiting.id in _unaddressed(session, (waiting,))
    reviewer = _reviewer(project_id)
    (shown,) = list_finding_actions(
        reviewer, reviewer, session, project_id, package_id, waiting.id
    ).items
    assert (shown.action, shown.note, shown.tags) == ("confirm", None, [HELD, DRAWN])


def test_an_answer_never_hides_the_decision_before_it(session: Session, tmp_path: Path) -> None:
    """Input: a waiting result the person settled ("not checkable", with a note), then a person
    confirms a held reading on it. Outcome: the settled decision still stands and holds; the answer
    is in the history only."""
    project_id, package_id, anchors = _held_rows(session)
    waiting = _width_finding(session, package_id, anchors[0], tmp_path)
    settled = _decide(session, package_id, waiting, ReviewActionKind.DISMISS)

    answer = _confirm(session, project_id, package_id, waiting, anchors[0]).action

    records = decision_records(session, [waiting.id])
    assert records.decisions[waiting.id].action.id == settled.id
    assert decision_holds(waiting, records, when=_now())
    assert [a.id for a in records.own_actions[waiting.id]] == [settled.id, answer.id]


def test_an_answer_never_displaces_a_carried_decision(session: Session, tmp_path: Path) -> None:
    """Input: a waiting result settled with a note, carried over an unchanged re-run; then a person
    confirms a held reading on the new result. Outcome: the carried decision still stands."""
    project_id, package_id, anchors = _held_rows(session)
    first = _width_finding(session, package_id, anchors[0], tmp_path)
    settled = _decide(session, package_id, first, ReviewActionKind.DISMISS)
    again = _width_finding(session, package_id, anchors[0], tmp_path)
    assert again.id != first.id
    assert decision_records(session, [again.id]).carried[again.id].action.id == settled.id

    _confirm(session, project_id, package_id, again, anchors[0])

    records = decision_records(session, [again.id])
    standing = records.decisions[again.id]
    assert (standing.action.id, standing.carried_over) == (settled.id, True)
    assert decision_holds(again, records, when=_now())


# -- item 2: an answer through a decided result ----------------------------------------------------


def test_an_answer_through_a_fail_is_counted_and_never_stands_behind_the_fail(
    session: Session, tmp_path: Path
) -> None:
    """Input: a FAIL code decided on a person's fix; the person takes the fix back by confirming the
    AI's reading through that FAIL. Outcome: a tagged confirm is written; the correction rate counts
    it; it does not decide the FAIL (sign-off still waits); Smriti's person-confirmed FAILs leave the
    FAIL out; Smriti's export calls it a confirmed reading, not a decision on the finding."""
    from eval.smriti.export import export_candidates

    project_id, package_id, anchors, decided = _fail_with_a_taken_back_fix(session, tmp_path)

    answer = _confirm(session, project_id, package_id, decided, anchors[0]).action

    assert (answer.action, answer.finding_id, answer.note, answer.tags) == (
        "confirm",
        decided.id,
        None,
        [HELD],
    )
    (rate,) = correction_rate(session, window=timedelta(days=1)).values()
    assert (rate.numerator, rate.denominator) == (1, 2)
    records = decision_records(session, [decided.id])
    assert decided.id not in records.decisions
    assert not decision_holds(decided, records, when=_now())
    assert decided.id in _unaddressed(session, (decided,))
    assert _person_confirmed(
        session, [(decided.id, decided.created_at)], frozenset({REVIEWER})
    ) == (set())
    exported = export_candidates(session, database="synthetic").candidates
    session.rollback()
    (candidate,) = [c for c in exported if c.candidate_id == f"review_actions:{answer.id}"]
    assert (candidate.kind, candidate.agent) != ("decided", "kautilya")
    assert candidate.kind == "confirmed"


def test_the_decision_before_an_answer_through_a_fail_stays_the_one_in_force(
    session: Session, tmp_path: Path
) -> None:
    """Input: a FAIL settled by the person (dismissed, with a note), then the person confirms the
    AI's reading through it. Outcome: the dismissal stays the decision in force, and the cloud PDF's
    "checked by hand" mark is the dismissal, never "FAIL agreed"."""
    project_id, package_id, anchors, decided = _fail_with_a_taken_back_fix(session, tmp_path)
    dismissed = _decide(session, package_id, decided, ReviewActionKind.DISMISS)
    answer = _confirm(session, project_id, package_id, decided, anchors[0]).action

    records = decision_records(session, [decided.id])
    assert records.decisions[decided.id].action.id == dismissed.id
    assert decision_holds(decided, records, when=_now())

    history = tuple(
        ReviewDisposition(
            action_id=action.id,
            action=action.action,  # type: ignore[arg-type]
            reviewer=action.actor,
            at=action.created_at,
            note=action.note,
        )
        for action in (dismissed, answer)
    )
    marks = reviewer_marks_for(
        session,
        _revision(session, package_id).id,
        findings=[(decided, "CT-WIDTH-001")],
        decisions=[
            SignedReviewFinding(
                finding_id=decided.id, rule_id="CT-WIDTH-001", outcome="FAIL", actions=history
            )
        ],
        until=None,
    ).marks
    words = [mark.words for mark in marks]
    assert not any("FAIL agreed" in said for said in words), words
    assert any("Dismissed by" in said for said in words), words


@pytest.mark.parametrize(
    ("outcome", "note"), [("FAIL", None), ("PASS", None), ("REVIEW_REQUIRED", NOTE)]
)
def test_decision_holds_never_counts_an_answer_even_handed_one(
    outcome: str, note: str | None
) -> None:
    """`decision_holds` itself, without the database: records that (wrongly) name a tagged answer as
    the decision never hold, even where an untagged confirm with the same note would."""
    revision, finding_id = uuid4(), uuid4()
    finding = Finding(id=finding_id, package_revision_id=revision, outcome=outcome)

    def confirm(tags: list[str]) -> ReviewAction:
        return ReviewAction(
            id=uuid4(),
            finding_id=finding_id,
            package_revision_id=revision,
            action="confirm",
            actor=REVIEWER,
            note=note,
            tags=tags,
        )

    def holds(action: ReviewAction) -> bool:
        records = DecisionRecords(
            decisions={finding_id: EffectiveDecision(action, None)},
            carried={},
            corrected=frozenset(),
            own_actions={finding_id: (action,)},
            grants={},
        )
        return decision_holds(finding, records, when=_now())

    assert holds(confirm([]))
    assert not holds(confirm([HELD]))
    assert not holds(confirm([HELD, DRAWN]))
