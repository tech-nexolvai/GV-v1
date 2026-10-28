"""#1339 (simple review W2): a person confirms or fixes an AI reading the checks held back.

Fail-first tests for the safety zone:

- a held reading a person confirms is used by the width check, which decides PASS or FAIL by code;
  without the person the row still waits;
- a fix writes all four records (the AI's reading as REJECTED, the person's value as
  HUMAN_CONFIRMED, a CORRECT review action on the finding, a correction-ledger row), and the check
  uses the person's value; the newest answer wins;
- a reading of another row, or of another countertop, is refused, and another row's confirmation
  never fills this row;
- a width can no longer be typed for a row;
- on a combined sheet the reading's side comes from `ReadingSides`: the vendor's drawing only.

All synthetic: invented values and names, no model call.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.auth.roles import Principal, Role
from app.db.session import session_factory
from app.models import (
    CanonicalObservation,
    CorrectionLedgerEntry,
    DrawingView,
    EvidenceSupportingCandidate,
    ObservationCandidate,
    PackageRevision,
    ReviewAction,
    ViewRole,
)
from app.models.verdicts import Finding, VerdictInput
from app.review.approval import _revisions_with_unchecked_inputs
from app.review.carry_over import decision_holds, decision_records
from app.review.evidence_actions import (
    HeldReadingRefusal,
    HeldReadingRefused,
    confirm_candidate_reading,
    correct_candidate_reading,
)
from app.review.session import open_session
from eval.correction_rate import correction_rate
from evidence.canonical import Authority, EvidenceStatus
from storage.local import LocalStore
from tests.api.test_slot_rows import _package_rows, _reader_support, _run_current_checks
from tests.app.postgres_fixture import alembic_config
from vocabulary.semantic_types import DocumentRole, SemanticType
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)

REVIEWER = "synthetic reviewer"


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


def _claude_partial(page_index: int, _row_offset: int, _slot: str) -> list[str]:
    """Page 1's row as the Claude-mode reader stores it when it holds one label: every label of the
    row carries `row-partial`, and the held one has no form proposal."""
    return ["row-partial"] if page_index == 0 else []


def _held_rows(session: Session, **options: object) -> tuple[UUID, UUID, dict[int, UUID]]:
    """Two rows whose widths add up and whose walls a person chose. Page 1's piece 1 (20") is held
    ("readers differ"); everything else is sealed by both readers, their answers on record."""
    settings: dict[str, object] = {
        "unsealed_page": 0,
        "unsealed_proposal": False,
        "slot_flags": _claude_partial,
        "widths_add_up": True,
        "walls_confirmed": True,
        **options,
    }
    ids = _package_rows(session, **settings)  # type: ignore[arg-type]
    for candidate in session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ).all():
        if candidate.corroboration_status == "CORROBORATED":
            _reader_support(session, candidate)
    return ids


def _reviewer(project_id: UUID) -> Principal:
    return Principal(
        id=REVIEWER, roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )


def _revision(session: Session, package_id: UUID) -> PackageRevision:
    return session.query(PackageRevision).filter_by(package_id=package_id).one()


def _width_finding(session: Session, package_id: UUID, row_id: UUID, root: Path) -> Finding:
    from app.review.wall_cases import latest_width_findings

    _run_current_checks(session, package_id, root)
    revision = _revision(session, package_id)
    return latest_width_findings(session, revision.id, [row_id])[row_id]


def _review_session_id(session: Session, package_id: UUID) -> UUID:
    return open_session(
        session, package_revision_id=_revision(session, package_id).id, reviewer=REVIEWER
    ).id


# -- confirm ---------------------------------------------------------------------------------------


def test_a_held_reading_waits_until_a_person_confirms_it_then_code_decides(
    session: Session, tmp_path: Path
) -> None:
    """Input: a row with one held label (the AI's best guess 20"). Outcome: the width check waits;
    once a person confirms the guess, the check runs on it and code decides PASS; the operand is
    the person's confirmation, linked as the finding's evidence."""
    project_id, package_id, anchors = _held_rows(session)
    held = anchors[0]

    before = _width_finding(session, package_id, held, tmp_path)
    assert before.outcome == "REVIEW_REQUIRED"

    decision = confirm_candidate_reading(
        session,
        principal=_reviewer(project_id),
        review_session_id=_review_session_id(session, package_id),
        finding_id=before.id,
        candidate_id=held,
        expected_value=Fraction(20),
    )
    session.commit()  # the database's deferred provenance checks run here

    assert decision.resulting.status == EvidenceStatus.HUMAN_CONFIRMED.value
    assert decision.resulting.document_role == DocumentRole.SHOP.value
    assert decision.resulting.semantic_type == SemanticType.COUNTERTOP_PIECE_WIDTH.value
    assert Fraction(decision.resulting.value_numerator, decision.resulting.value_denominator) == 20
    cited = session.scalars(
        select(EvidenceSupportingCandidate.candidate_id).where(
            EvidenceSupportingCandidate.canonical_observation_id == decision.resulting.id
        )
    ).all()
    assert cited == [held]
    # A confirm review action from the AI's reading (RAW_CANDIDATE, citing the label) to the
    # person's value, with no note and no ledger row: it is counted by the correction rate, and it
    # decides nothing on the finding (no note on a waiting result, so sign-off still waits).
    action = decision.action
    assert action is not None
    assert (action.action, action.finding_id, action.actor, action.note) == (
        "confirm",
        before.id,
        REVIEWER,
        None,
    )
    assert action.original_observation_id == decision.original.id
    assert action.resulting_observation_id == decision.resulting.id
    # An answer about a held reading, never a decision on the finding (#1367).
    assert action.tags == ["held-reading-answer"]
    assert decision.original.status == EvidenceStatus.RAW_CANDIDATE.value
    assert session.query(ReviewAction).count() == 1
    assert session.query(CorrectionLedgerEntry).count() == 0
    records = decision_records(session, [before.id])
    assert not decision_holds(before, records, when=datetime.now(UTC))
    # The person's value is a new input the checks have not used: sign-off waits for the re-run.
    revision = _revision(session, package_id)
    assert revision.id in _revisions_with_unchecked_inputs(session, [revision.id])
    (rate,) = correction_rate(session, window=timedelta(days=1)).values()
    assert (rate.numerator, rate.denominator) == (0, 1)

    after = _width_finding(session, package_id, held, tmp_path)
    assert after.outcome == "PASS", after.reason
    (used,) = session.scalars(
        select(VerdictInput).where(
            VerdictInput.check_run_id == after.check_run_id,
            VerdictInput.operand_name == "piece_widths[0]",
        )
    ).all()
    assert used.canonical_observation_id == decision.resulting.id
    assert used.evidence_status == EvidenceStatus.HUMAN_CONFIRMED.value
    assert used.slot_row_review_decision_id is None


def test_without_the_person_the_ai_guess_never_fills_the_row(
    session: Session, tmp_path: Path
) -> None:
    """Input: the held row checked twice, nobody confirming. Outcome: REVIEW_REQUIRED both times;
    the AI's stored guess is never an operand."""
    _project_id, package_id, anchors = _held_rows(session)

    first = _width_finding(session, package_id, anchors[0], tmp_path)
    second = _width_finding(session, package_id, anchors[0], tmp_path)

    assert first.outcome == second.outcome == "REVIEW_REQUIRED"
    assert (
        session.query(CanonicalObservation)
        .filter(CanonicalObservation.status == EvidenceStatus.HUMAN_CONFIRMED.value)
        .count()
        == 0
    )


def test_a_reading_both_ais_sealed_is_not_a_held_reading(session: Session, tmp_path: Path) -> None:
    """Input: confirm the overall, which both readers sealed. Outcome: refused, nothing written."""
    project_id, package_id, anchors = _held_rows(session)
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    overall = _slot_candidate(session, anchors[0], "overall")

    with pytest.raises(HeldReadingRefused) as refused:
        confirm_candidate_reading(
            session,
            principal=_reviewer(project_id),
            review_session_id=_review_session_id(session, package_id),
            finding_id=finding.id,
            candidate_id=overall.id,
            expected_value=Fraction(1),
        )
    assert refused.value.reason is HeldReadingRefusal.ALREADY_COUNTS
    assert session.query(CanonicalObservation).filter_by(status="HUMAN_CONFIRMED").count() == 0


def test_a_held_label_with_no_ai_reading_cannot_be_confirmed_or_fixed(
    session: Session, tmp_path: Path
) -> None:
    """Input: a held label the AI read no number for. Outcome: both refused (checked by hand)."""
    project_id, package_id, anchors = _held_rows(session)
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    blank = _copy_candidate(session, anchors[0], slot="0", value=None)
    # The original held label stays too, so this position now has two: either way, nothing is
    # usable, and both actions are refused.
    sitting = _review_session_id(session, package_id)
    for act in (confirm_candidate_reading, correct_candidate_reading):
        kwargs = _answer_kwargs(act)
        with pytest.raises(HeldReadingRefused):
            act(
                session,
                principal=_reviewer(project_id),
                review_session_id=sitting,
                finding_id=finding.id,
                candidate_id=blank.id,
                **kwargs,
            )
    assert session.query(CanonicalObservation).filter_by(status="HUMAN_CONFIRMED").count() == 0


def test_confirming_twice_is_refused(session: Session, tmp_path: Path) -> None:
    project_id, package_id, anchors = _held_rows(session)
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    sitting = _review_session_id(session, package_id)
    confirm_candidate_reading(
        session,
        principal=_reviewer(project_id),
        review_session_id=sitting,
        finding_id=finding.id,
        candidate_id=anchors[0],
        expected_value=Fraction(20),
    )
    with pytest.raises(HeldReadingRefused) as refused:
        confirm_candidate_reading(
            session,
            principal=_reviewer(project_id),
            review_session_id=sitting,
            finding_id=finding.id,
            candidate_id=anchors[0],
            expected_value=Fraction(20),
        )
    assert refused.value.reason is HeldReadingRefusal.ALREADY_ANSWERED


def test_nothing_is_answered_on_a_signed_off_revision(session: Session, tmp_path: Path) -> None:
    """Input: the held row's revision is signed off. Outcome: confirm and fix are both refused."""
    project_id, package_id, anchors = _held_rows(session)
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    sitting = _review_session_id(session, package_id)
    _revision(session, package_id).state = "APPROVED"
    session.flush()

    for act in (confirm_candidate_reading, correct_candidate_reading):
        kwargs = _answer_kwargs(act)
        with pytest.raises(HeldReadingRefused) as refused:
            act(
                session,
                principal=_reviewer(project_id),
                review_session_id=sitting,
                finding_id=finding.id,
                candidate_id=anchors[0],
                **kwargs,
            )
        assert refused.value.reason is HeldReadingRefusal.REVISION_CLOSED
    assert session.query(CanonicalObservation).filter_by(status="HUMAN_CONFIRMED").count() == 0


# -- correct ---------------------------------------------------------------------------------------


def test_a_fix_writes_all_four_records_and_the_check_uses_the_persons_value(
    session: Session, tmp_path: Path
) -> None:
    """Input: the person fixes the held 20" to 21" with the note "AI misread". Outcome: the AI's
    reading as REJECTED, the person's 21" as HUMAN_CONFIRMED citing the same held candidate, a
    CORRECT review action on the finding (original -> resulting), one ledger row; the re-run decides
    on 21": the pieces then exceed the overall, so FAIL by code."""
    project_id, package_id, anchors = _held_rows(session)
    finding = _width_finding(session, package_id, anchors[0], tmp_path)

    decision = correct_candidate_reading(
        session,
        principal=_reviewer(project_id),
        review_session_id=_review_session_id(session, package_id),
        finding_id=finding.id,
        candidate_id=anchors[0],
        corrected_value='21"',
        note="AI misread",
    )
    session.commit()  # the database's deferred provenance checks run here

    assert decision.original is not None and decision.action is not None
    assert decision.original.status == EvidenceStatus.REJECTED.value
    assert Fraction(decision.original.value_numerator, decision.original.value_denominator) == 20
    assert decision.resulting.status == EvidenceStatus.HUMAN_CONFIRMED.value
    assert Fraction(decision.resulting.value_numerator, decision.resulting.value_denominator) == 21
    for observation in (decision.original, decision.resulting):
        cited = session.scalars(
            select(EvidenceSupportingCandidate.candidate_id).where(
                EvidenceSupportingCandidate.canonical_observation_id == observation.id
            )
        ).all()
        assert cited == [anchors[0]]
    action = decision.action
    assert action.action == "correct"
    assert action.finding_id == finding.id
    assert action.actor == REVIEWER
    assert action.note == "AI misread"
    assert action.original_observation_id == decision.original.id
    assert action.resulting_observation_id == decision.resulting.id
    (ledger,) = session.scalars(select(CorrectionLedgerEntry)).all()
    assert ledger.review_action_id == action.id
    assert ledger.canonical_observation_id == decision.original.id
    assert '"value":"20/1"' in ledger.original_value
    assert '"value":"21/1"' in ledger.corrected_value

    after = _width_finding(session, package_id, anchors[0], tmp_path)
    assert after.outcome == "FAIL", after.reason


def test_the_newest_answer_wins(session: Session, tmp_path: Path) -> None:
    """Input: fixed to 21", then, while the result still waits, the person says the AI's 20" was
    right after all. Outcome: fixing to the AI's own value is refused (that is a confirmation), the
    confirmation is accepted, and the check uses the newest answer: PASS. Every answer stays on
    record, both answers hang off one AI reading (one chain), and Smriti skips the replaced fix."""
    from eval.smriti.export import export_candidates

    project_id, package_id, anchors = _held_rows(session)
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    sitting = _review_session_id(session, package_id)
    fixed = correct_candidate_reading(
        session,
        principal=_reviewer(project_id),
        review_session_id=sitting,
        finding_id=finding.id,
        candidate_id=anchors[0],
        corrected_value='21"',
    )
    session.commit()
    with pytest.raises(HeldReadingRefused) as same:
        correct_candidate_reading(
            session,
            principal=_reviewer(project_id),
            review_session_id=sitting,
            finding_id=finding.id,
            candidate_id=anchors[0],
            corrected_value='20"',
        )
    assert same.value.reason is HeldReadingRefusal.SAME_AS_READ
    with pytest.raises(HeldReadingRefused) as again:
        correct_candidate_reading(
            session,
            principal=_reviewer(project_id),
            review_session_id=sitting,
            finding_id=finding.id,
            candidate_id=anchors[0],
            corrected_value='21"',
        )
    assert again.value.reason is HeldReadingRefusal.ALREADY_ANSWERED

    confirmed = confirm_candidate_reading(
        session,
        principal=_reviewer(project_id),
        review_session_id=sitting,
        finding_id=finding.id,
        candidate_id=anchors[0],
        expected_value=Fraction(20),
    )
    session.commit()

    assert confirmed.original.id == fixed.original.id
    assert _width_finding(session, package_id, anchors[0], tmp_path).outcome == "PASS"
    assert session.query(CanonicalObservation).filter_by(status="HUMAN_CONFIRMED").count() == 2
    assert session.query(CorrectionLedgerEntry).count() == 1
    assert session.query(ReviewAction).count() == 2
    session.commit()  # the export opens its own read-only transaction
    exported = export_candidates(session, database="synthetic").candidates
    session.rollback()
    by_table = {
        candidate.candidate_id: candidate
        for candidate in exported
        if candidate.table in {"correction_ledger", "review_actions"}
    }
    (ledger,) = [c for c in by_table.values() if c.table == "correction_ledger"]
    assert ledger.kind == "superseded"
    assert confirmed.action is not None
    assert by_table[f"review_actions:{confirmed.action.id}"].kind == "confirmed"


def test_taking_back_a_fix_after_code_decided_on_it(session: Session, tmp_path: Path) -> None:
    """Input: the held label fixed to 21", the check re-run (code decides FAIL on 21"), then the
    person confirms the AI's 20" through that FAIL. Outcome: accepted as the newest answer. Its
    `confirm` review action on the decided FAIL is tagged `held-reading-answer` (#1367): the
    correction rate counts it, and it never stands behind the FAIL; sign-off waits for the re-run,
    which decides PASS; Smriti skips the replaced fix."""
    from eval.smriti.export import export_candidates

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
    assert decided.outcome == "FAIL"
    actions = session.query(ReviewAction).count()

    taken_back = confirm_candidate_reading(
        session,
        principal=_reviewer(project_id),
        review_session_id=sitting,
        finding_id=decided.id,
        candidate_id=anchors[0],
        expected_value=Fraction(20),
    )
    session.commit()

    assert session.query(ReviewAction).count() == actions + 1
    assert (taken_back.action.action, taken_back.action.finding_id, taken_back.action.note) == (
        "confirm",
        decided.id,
        None,
    )
    assert taken_back.action.tags == ["held-reading-answer"]
    assert not decision_holds(
        decided, decision_records(session, [decided.id]), when=datetime.now(UTC)
    )
    (rate,) = correction_rate(session, window=timedelta(days=1)).values()
    assert (rate.numerator, rate.denominator) == (1, 2)  # the fix and this confirmation
    revision = _revision(session, package_id)
    assert revision.id in _revisions_with_unchecked_inputs(session, [revision.id])
    exported = export_candidates(session, database="synthetic").candidates
    session.rollback()
    (ledger,) = [c for c in exported if c.table == "correction_ledger"]
    assert ledger.kind == "superseded"
    assert _width_finding(session, package_id, anchors[0], tmp_path).outcome == "PASS"


def test_confirm_records_only_the_value_the_person_was_shown(
    session: Session, tmp_path: Path
) -> None:
    """Input: the held label's PDF text and one reader say 21", the other reader 20", and the stored
    reading is 20". The screen showed 21". Outcome: Confirm with the shown 21" is refused and
    nothing is written (it would record 20" as the person's answer); Confirm with 20" is accepted.
    """
    project_id, package_id, anchors = _held_rows(
        session,
        slot_flags=_held_slot_flags(
            'reader:pdf-text-layer:21"', 'reader:reader-a:21"', 'reader:reader-b:20"'
        ),
    )
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    sitting = _review_session_id(session, package_id)

    with pytest.raises(HeldReadingRefused) as refused:
        confirm_candidate_reading(
            session,
            principal=_reviewer(project_id),
            review_session_id=sitting,
            finding_id=finding.id,
            candidate_id=anchors[0],
            expected_value=Fraction(21),
        )
    assert refused.value.reason is HeldReadingRefusal.SHOWN_VALUE_DIFFERS
    assert "use Fix" in refused.value.detail
    assert _person_answers(session) == 0
    assert session.query(ReviewAction).count() == 0

    decision = confirm_candidate_reading(
        session,
        principal=_reviewer(project_id),
        review_session_id=sitting,
        finding_id=finding.id,
        candidate_id=anchors[0],
        expected_value=Fraction(20),
    )
    assert Fraction(decision.resulting.value_numerator, decision.resulting.value_denominator) == 20


@pytest.mark.parametrize("typed", ["21", "21 cm", '0"', '-1"', '163/8"', "about 21"])
def test_a_fix_takes_inches_only_through_the_review_parser(
    session: Session, tmp_path: Path, typed: str
) -> None:
    """Input: a fix typed without a unit, in an unknown unit, zero, negative, squeezed or not a
    number.
    Outcome: refused with the reason, and nothing is left behind."""
    project_id, package_id, anchors = _held_rows(session)
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    sitting = _review_session_id(session, package_id)
    canonicals = session.query(CanonicalObservation).count()

    with pytest.raises(HeldReadingRefused) as refused:
        correct_candidate_reading(
            session,
            principal=_reviewer(project_id),
            review_session_id=sitting,
            finding_id=finding.id,
            candidate_id=anchors[0],
            corrected_value=typed,
        )
    assert refused.value.reason is HeldReadingRefusal.BAD_VALUE
    assert refused.value.detail
    assert session.query(CanonicalObservation).count() == canonicals
    assert session.query(ReviewAction).count() == 0


@pytest.mark.parametrize("typed", ["520 mm", "520mm", "520 MM", "520 millimetres"])
def test_a_fix_in_millimetres_is_refused(session: Session, tmp_path: Path, typed: str) -> None:
    """Input: a fix typed in millimetres. Outcome: refused as not inches (never converted into a
    decision: 520 mm would become 2600/127 in), and nothing is left behind."""
    project_id, package_id, anchors = _held_rows(session)
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    sitting = _review_session_id(session, package_id)

    with pytest.raises(HeldReadingRefused) as refused:
        correct_candidate_reading(
            session,
            principal=_reviewer(project_id),
            review_session_id=sitting,
            finding_id=finding.id,
            candidate_id=anchors[0],
            corrected_value=typed,
        )
    assert refused.value.reason is HeldReadingRefusal.NOT_INCHES
    assert _person_answers(session) == 0
    assert session.query(ReviewAction).count() == 0


# -- GV's markup and untrusted ink: W1's rule ------------------------------------------------------


@pytest.mark.parametrize(
    ("added", "removed"),
    [
        (["reviewer-markup"], []),
        (["gv-tick"], []),
        (["ink:gv"], ["ink:vendor"]),
        (["ink-unchecked"], []),
        (["two-labels"], []),
        ([], ["ink:vendor"]),
    ],
    ids=["gv-markup", "gv-tick", "gv-ink", "ink-unchecked", "two-labels", "ink-never-checked"],
)
def test_a_value_not_positively_the_vendors_is_never_confirmed_or_fixed(
    session: Session, tmp_path: Path, added: list[str], removed: list[str]
) -> None:
    """Input: the held label under GV's markup, with GV's ink, with unchecked ink, with a second
    label, or with no ink check at all. Outcome: confirm and fix are both refused (check it by
    hand), and nothing is written. The rule is W1's `vendor_ink_only`, not a copy of it."""
    project_id, package_id, anchors = _held_rows(
        session, slot_flags=_held_slot_flags(*added, *(f"-{flag}" for flag in removed))
    )
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    sitting = _review_session_id(session, package_id)

    for act in (confirm_candidate_reading, correct_candidate_reading):
        with pytest.raises(HeldReadingRefused) as refused:
            act(
                session,
                principal=_reviewer(project_id),
                review_session_id=sitting,
                finding_id=finding.id,
                candidate_id=anchors[0],
                **_answer_kwargs(act),
            )
        assert refused.value.reason is HeldReadingRefusal.NO_AI_READING
        assert "by hand" in refused.value.detail
    assert _person_answers(session) == 0
    assert session.query(ReviewAction).count() == 0


def test_an_answer_on_a_label_under_gv_markup_never_fills_the_row(
    session: Session, tmp_path: Path
) -> None:
    """Input: a HUMAN_CONFIRMED 20" citing the row's own held label, written by another path (the
    older type confirmation), on a label flagged as GV's markup. Outcome: the row still waits: the
    width check's own reader re-checks W1's rule on the cited label (defence in depth)."""
    from app.evidence.confirm import observation_from_candidate

    _project_id, package_id, anchors = _held_rows(
        session, slot_flags=_held_slot_flags("reviewer-markup")
    )
    held = session.get_one(ObservationCandidate, anchors[0])
    made = observation_from_candidate(
        session,
        held,
        semantic_type=SemanticType.COUNTERTOP_PIECE_WIDTH.value,
        status=EvidenceStatus.HUMAN_CONFIRMED,
        actor=REVIEWER,
    )
    assert isinstance(made, CanonicalObservation)
    session.commit()

    assert _width_finding(session, package_id, anchors[0], tmp_path).outcome == "REVIEW_REQUIRED"


def test_a_refusal_to_place_the_reading_names_its_own_reason(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input: the reading's page has no recorded transform, or it cannot be normalised. Outcome:
    refused with that reason (not "not the vendor's drawing")."""
    import app.review.evidence_actions as actions
    from app.evidence.confirm import ConfirmationRefused, RefusalReason

    project_id, package_id, anchors = _held_rows(session)
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    sitting = _review_session_id(session, package_id)
    for why, expected in (
        (RefusalReason.NO_TRANSFORM, HeldReadingRefusal.CANNOT_PLACE),
        (RefusalReason.NOT_NORMALISABLE, HeldReadingRefusal.NOT_NORMALISABLE),
    ):
        monkeypatch.setattr(
            actions,
            "observation_from_candidate",
            lambda *_a, _why=why, **_k: ConfirmationRefused(_why, "synthetic refusal"),
        )
        with pytest.raises(HeldReadingRefused) as refused:
            confirm_candidate_reading(
                session,
                principal=_reviewer(project_id),
                review_session_id=sitting,
                finding_id=finding.id,
                candidate_id=anchors[0],
                expected_value=Fraction(20),
            )
        assert refused.value.reason is expected


# -- ownership -------------------------------------------------------------------------------------


def test_another_rows_reading_is_refused(session: Session, tmp_path: Path) -> None:
    """Input: page 1's finding with page 2's (sealed or held) label. Outcome: both refused."""
    project_id, package_id, anchors = _held_rows(session)
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    sitting = _review_session_id(session, package_id)
    other = _copy_candidate(session, anchors[1], slot="0", value=Fraction(30), sealed=False)

    for candidate_id in (anchors[1], other.id):
        for act in (confirm_candidate_reading, correct_candidate_reading):
            kwargs = _answer_kwargs(act)
            with pytest.raises(HeldReadingRefused) as refused:
                act(
                    session,
                    principal=_reviewer(project_id),
                    review_session_id=sitting,
                    finding_id=finding.id,
                    candidate_id=candidate_id,
                    **kwargs,
                )
            assert refused.value.reason is HeldReadingRefusal.NOT_THIS_ITEM
    assert session.query(CanonicalObservation).filter_by(status="HUMAN_CONFIRMED").count() == 0


def test_another_rows_confirmation_never_fills_this_row(session: Session, tmp_path: Path) -> None:
    """Input: a HUMAN_CONFIRMED 20" piece width citing page 2's label, on page 1's page. Outcome:
    page 1's held row still waits; only its own label's confirmation could fill it."""
    _project_id, package_id, anchors = _held_rows(session)
    other_row_label = session.get_one(ObservationCandidate, anchors[1])
    held = session.get_one(ObservationCandidate, anchors[0])
    stray = CanonicalObservation(
        document_version_id=held.document_version_id,
        page_id=held.page_id,
        document_role=DocumentRole.SHOP.value,
        polygon=[["0", "0"], ["0.01", "0"], ["0.01", "0.01"], ["0", "0.01"]],
        coordinate_space="stored",
        semantic_type=SemanticType.COUNTERTOP_PIECE_WIDTH.value,
        value_numerator=20,
        value_denominator=1,
        unit="in",
        status=EvidenceStatus.HUMAN_CONFIRMED.value,
        authority=Authority.AUTHORITATIVE.value,
    )
    session.add(stray)
    session.flush()
    session.add(
        EvidenceSupportingCandidate(
            canonical_observation_id=stray.id, candidate_id=other_row_label.id, role="primary"
        )
    )
    session.flush()

    assert _width_finding(session, package_id, anchors[0], tmp_path).outcome == "REVIEW_REQUIRED"


def test_a_row_with_another_hold_stays_held_after_its_readings_are_confirmed(
    session: Session, tmp_path: Path
) -> None:
    """Input: the held row also carries a line-finder hold (`row-hold:`). Outcome: confirming its
    held label does not release the row: only the missing labels' hold is resolved by people."""
    project_id, package_id, anchors = _held_rows(
        session,
        slot_flags=lambda page, _row, _slot: (
            ["row-partial", "row-hold:counter-break"] if page == 0 else []
        ),
    )
    finding = _width_finding(session, package_id, anchors[0], tmp_path)
    confirm_candidate_reading(
        session,
        principal=_reviewer(project_id),
        review_session_id=_review_session_id(session, package_id),
        finding_id=finding.id,
        candidate_id=anchors[0],
        expected_value=Fraction(20),
    )
    session.flush()

    assert _width_finding(session, package_id, anchors[0], tmp_path).outcome == "REVIEW_REQUIRED"


# -- no typed widths -------------------------------------------------------------------------------


# -- combined sheets: the side comes from ReadingSides ---------------------------------------------


@pytest.mark.parametrize(
    ("holding_role", "confirmed"),
    [(ViewRole.SHOP, True), (ViewRole.ARCH, False), (None, False)],
)
def test_on_a_combined_sheet_only_a_reading_on_the_vendors_drawing_is_confirmed(
    session: Session, tmp_path: Path, holding_role: ViewRole | None, confirmed: bool
) -> None:
    """Input: page 1 holds two drawings side by side; the held label lies in the left one, whose
    role is the vendor's, the architect's, or not confirmed. Outcome: confirmed (as the vendor's
    reading) only in the vendor's drawing; otherwise refused and nothing written."""
    project_id, package_id, anchors = _held_rows(session)
    held = session.get_one(ObservationCandidate, anchors[0])
    left = DrawingView(page_id=held.page_id, tag="LEFT", region=_region("0.00", "0.50"))
    right = DrawingView(page_id=held.page_id, tag="RIGHT", region=_region("0.50", "1.00"))
    session.add_all((left, right))
    session.flush()
    if holding_role is not None:
        confirm_view_role(session, view=left, role=holding_role, actor=REVIEWER)
    other = ViewRole.ARCH if holding_role is ViewRole.SHOP else ViewRole.SHOP
    confirm_view_role(session, view=right, role=other, actor=REVIEWER)
    finding = _width_finding(session, package_id, anchors[0], tmp_path)

    def act() -> object:
        return confirm_candidate_reading(
            session,
            principal=_reviewer(project_id),
            review_session_id=_review_session_id(session, package_id),
            finding_id=finding.id,
            candidate_id=anchors[0],
            expected_value=Fraction(20),
        )

    if confirmed:
        decision = act()
        assert decision.resulting.document_role == DocumentRole.SHOP.value  # type: ignore[attr-defined]
    else:
        with pytest.raises(HeldReadingRefused) as refused:
            act()
        assert refused.value.reason is HeldReadingRefusal.NOT_THE_VENDORS_DRAWING
        assert session.query(CanonicalObservation).filter_by(status="HUMAN_CONFIRMED").count() == 0


# -- a countertop's depth reading ------------------------------------------------------------------


def test_a_countertops_depth_reading_is_confirmed_only_for_its_own_countertop(
    session: Session, tmp_path: Path
) -> None:
    """Input: a depth read by drawn length alone (waiting for a person) on one countertop, and a
    second countertop on the same drawing. Outcome: the confirmation is refused through the other
    countertop's result and accepted through its own; the depth check then uses it."""
    from tests.workflow.test_depth_evidence import CT010, _findings, _Set
    from tests.workflow.test_reading_parts import _confirmed
    from vocabulary.part_kinds import PartKind
    from workflow.depth_evidence import CONFIRM_ONLY
    from workflow.stages import DatabaseStages

    store = LocalStore(root=tmp_path / "store", ticket_secret=b"synthetic-test")
    sheet = _Set(session, store)
    reading = sheet.reading(
        CT010, 25, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only"
    )
    # A second countertop on its own small drawing beside it (two on one drawing withhold both).
    other_view = DrawingView(
        page_id=sheet.page.id,
        tag="OTHER",
        region={
            "space": "stored",
            "points": [["0", "0"], ["0.05", "0"], ["0.05", "0.05"], ["0", "0.05"]],
        },
    )
    session.add(other_view)
    session.flush()
    confirm_view_role(session, view=other_view, role=ViewRole.SHOP, actor=REVIEWER)
    _other_proposal, other_item = _confirmed(
        session, other_view, PartKind.COUNTERTOP, "0.01", "0.04"
    )
    findings = sheet.check(store, on=True)
    (own,) = [f for f in findings["CT-DEPTH-001"] if f.scope_item_id == sheet.item]
    others = [f for f in findings["CT-DEPTH-001"] if f.scope_item_id == other_item]
    assert own.outcome == "REVIEW_REQUIRED"
    assert others, "the second countertop has its own depth result"
    principal = Principal(id=REVIEWER, roles=frozenset({Role.REVIEWER}))
    sitting = open_session(session, package_revision_id=sheet.revision.id, reviewer=REVIEWER).id

    for finding in others:
        with pytest.raises(HeldReadingRefused) as refused:
            confirm_candidate_reading(
                session,
                principal=principal,
                review_session_id=sitting,
                finding_id=finding.id,
                candidate_id=reading.id,
                expected_value=Fraction(25),
            )
        assert refused.value.reason is HeldReadingRefusal.NOT_THIS_ITEM

    # A fix of a depth reading is never taken here (the depth check would hold for good).
    with pytest.raises(HeldReadingRefused) as fixed:
        correct_candidate_reading(
            session,
            principal=principal,
            review_session_id=sitting,
            finding_id=own.id,
            candidate_id=reading.id,
            corrected_value='24"',
        )
    assert fixed.value.reason is HeldReadingRefusal.DEPTH_CONFIRM_ONCE
    assert "advanced depth card" in fixed.value.detail
    assert _person_answers(session) == 0

    decision = confirm_candidate_reading(
        session,
        principal=principal,
        review_session_id=sitting,
        finding_id=own.id,
        candidate_id=reading.id,
        expected_value=Fraction(25),
    )
    assert decision.resulting.semantic_type == CT010
    assert decision.action is not None and decision.action.action == "confirm"
    # One answer only: a second confirmation or a fix after it is refused, nothing more written.
    answers = session.query(CanonicalObservation).count()
    for act, extra in (
        (confirm_candidate_reading, {"expected_value": Fraction(25)}),
        (correct_candidate_reading, {"corrected_value": '24"'}),
    ):
        with pytest.raises(HeldReadingRefused) as again:
            act(
                session,
                principal=principal,
                review_session_id=sitting,
                finding_id=own.id,
                candidate_id=reading.id,
                **extra,
            )
        assert again.value.reason is HeldReadingRefusal.DEPTH_CONFIRM_ONCE
    assert session.query(CanonicalObservation).count() == answers
    session.commit()
    operand = sheet.operands().operands["countertop_depth"]
    assert operand.status is EvidenceStatus.HUMAN_CONFIRMED
    # The checks again (the rulebook and the countertop's run decision are already in place), with
    # the project's cabinet depth and overhang: 24" + 1" is the confirmed 25".
    _project_settings(session, sheet.revision, cabinet_depth=24, countertop_overhang=1)
    DatabaseStages(
        store, discriminators={"wall_config": "back_left_right"}, depth_reader=True
    ).run_checks(session, sheet.revision.id)
    (after,) = [
        f
        for f in _findings(session, sheet.revision)["CT-DEPTH-001"]
        if f.scope_item_id == sheet.item
    ]
    assert after.outcome == "PASS", after.reason


# -- helpers ---------------------------------------------------------------------------------------


def _project_settings(session: Session, revision: PackageRevision, **inches: int) -> None:
    """Project settings a person typed, in inches (invented values)."""
    from sqlalchemy import func

    from app.models import Package
    from app.models.parameters import ParameterSet as StoredSet
    from app.models.parameters import to_rows
    from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
    from rules.schema import Quantity
    from units.measurement import Unit

    package = session.get_one(Package, revision.package_id)
    newest = session.scalar(
        select(func.max(StoredSet.version)).where(
            StoredSet.project_id == package.project_id,
            StoredSet.layer == ParameterLayer.PROJECT.value,
        )
    )
    stored, rows = to_rows(
        ParameterSet(
            project_id=str(package.project_id),
            layer=ParameterLayer.PROJECT,
            version=(newest or 0) + 1,
            parameters={
                name: ParameterValue(
                    value=Quantity(value=Fraction(value), unit=Unit.INCH),
                    provenance=Provenance.GC_CLIENT,
                    set_by=REVIEWER,
                    set_at=datetime(2026, 10, 3, tzinfo=UTC),
                )
                for name, value in inches.items()
            },
        ),
        package_revision_id=None,
    )
    session.add(stored)
    session.add_all(rows)
    session.flush()


def _person_answers(session: Session) -> int:
    """Observations a person's answer wrote: theirs, and the AI's reading it is recorded against."""
    return (
        session.query(CanonicalObservation)
        .filter(
            CanonicalObservation.status.in_(
                [
                    EvidenceStatus.HUMAN_CONFIRMED.value,
                    EvidenceStatus.RAW_CANDIDATE.value,
                    EvidenceStatus.REJECTED.value,
                ]
            )
        )
        .count()
    )


def _held_slot_flags(*extra: str) -> Callable[[int, int, str], list[str]]:
    """`_claude_partial`, plus `extra` on the held label (page 1's piece 1) only."""

    def flags(page_index: int, row_offset: int, slot: str) -> list[str]:
        own = list(extra) if page_index == 0 and slot == "0" else []
        return [*_claude_partial(page_index, row_offset, slot), *own]

    return flags


def _answer_kwargs(act: object, *, fix: str = '21"') -> dict[str, Any]:
    """The extra keyword each action takes: the value shown (confirm) or the fix typed (fix)."""
    if act is correct_candidate_reading:
        return {"corrected_value": fix}
    return {"expected_value": Fraction(20)}


def _region(left: str, right: str) -> dict[str, object]:
    return {
        "space": "stored",
        "points": [[left, "0.00"], [right, "0.00"], [right, "1.00"], [left, "1.00"]],
    }


def _slot_candidate(session: Session, anchor_id: UUID, slot: str) -> ObservationCandidate:
    anchor = session.get_one(ObservationCandidate, anchor_id)
    rank = next(flag for flag in anchor.ambiguity_flags if flag.startswith("row-rank:"))
    return next(
        candidate
        for candidate in session.scalars(
            select(ObservationCandidate).where(
                ObservationCandidate.extraction_run_id == anchor.extraction_run_id,
                ObservationCandidate.page_id == anchor.page_id,
            )
        )
        if rank in candidate.ambiguity_flags and f"slot:{slot}" in candidate.ambiguity_flags
    )


def _copy_candidate(
    session: Session,
    anchor_id: UUID,
    *,
    slot: str,
    value: Fraction | None,
    sealed: bool = False,
) -> ObservationCandidate:
    """Another label of the same row as `anchor_id`, held, with `value` or none."""
    anchor = session.get_one(ObservationCandidate, anchor_id)
    flags = [
        flag
        for flag in anchor.ambiguity_flags
        if not flag.startswith("slot:") and not flag.startswith("reader-id:")
    ]
    copy = ObservationCandidate(
        document_version_id=anchor.document_version_id,
        page_id=anchor.page_id,
        extraction_run_id=anchor.extraction_run_id,
        raw_text="" if value is None else f"{value} inch",
        value_numerator=None if value is None else value.numerator,
        value_denominator=None if value is None else value.denominator,
        unit=None if value is None else "in",
        polygon=anchor.polygon,
        coordinate_space="image",
        ambiguity_flags=[*flags, f"slot:{slot}"],
        corroboration_status="CORROBORATED" if sealed else None,
        corroboration_lane="SECOND_READER" if sealed else None,
    )
    session.add(copy)
    session.flush()
    return copy
