"""A person's confirms, fixes and answers are frozen into the sign-off snapshot as cloud PDF marks
(#1363), and a snapshot frozen before that draws exactly as before.

The records are built the way the review paths write them: a confirmation is a `HUMAN_CONFIRMED`
canonical observation citing the AI's reading with the person on its audit event (the #530 shape,
and W2's, #1339); a fix is the AI's observation (`REJECTED`), the person's `HUMAN_CONFIRMED` one, a
`correct` review action between them and its correction-ledger row (W2); a wall choice is a slot
row decision; a page answer a `NoCountertopAnswer`; a result checked by hand a finding action with
the simple review's fixed note. All values and names are made up.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pypdf import PdfWriter
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.audit.events import AuditCategory, emit
from app.db.session import session_factory
from app.models import (
    CheckRun,
    Finding,
    ObservationCandidate,
    PackageRevision,
    SlotRowReviewDecision,
)
from app.models.evidence import (
    CanonicalObservation,
    EvidenceSupportingCandidate,
    NoCountertopAnswer,
)
from app.models.review import Approval, ApprovedFinding, ReviewAction, ReviewSession
from app.models.signed_exports import ApprovalExportSnapshot
from app.review.ledger import record_correction
from app.review.review_item_actions import CHECKED_WRONG_NOTE
from app.review.reviewer_marks import (
    FrozenReviewerMarks,
    initials,
    is_not_checked,
    reviewer_marks_for,
)
from app.review.signed_exports import (
    ExportSnapshot,
    SignedExportRefused,
    load_snapshot,
    request_signed_exports,
)
from storage.local import LocalStore
from tests.api.test_slot_rows import _package_rows, _run_current_checks
from tests.app.postgres_fixture import alembic_config
from verdict.operands import EvidenceStatus
from vocabulary.wall_layouts import wall_layout_words
from workflow.redline_outputs import RedlineOutput
from workflow.signed_outputs import generate_signed_outputs

pytest_plugins = ("tests.app.postgres_fixture",)

ANA = "ana.bell@example.com"
WALLS = f"Walls chosen by AB: {wall_layout_words('back_only')}"
OMAR = "omar.diaz@example.com"


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


def _observation(
    db: Session, candidate: ObservationCandidate, status: EvidenceStatus, value: Fraction
) -> CanonicalObservation:
    """A canonical observation citing `candidate`, the shape a person's answer writes."""
    made = CanonicalObservation(
        document_version_id=candidate.document_version_id,
        page_id=candidate.page_id,
        document_role="SHOP",
        polygon=[["0.1", "0.2"], ["0.2", "0.2"], ["0.2", "0.25"], ["0.1", "0.25"]],
        coordinate_space="stored",
        semantic_type="countertop_piece_width",
        value_numerator=value.numerator,
        value_denominator=value.denominator,
        unit="in",
        status=status.value,
        authority="AUTHORITATIVE",
        evidence_crop_uri=None,
    )
    db.add(made)
    db.flush()
    db.add(
        EvidenceSupportingCandidate(
            canonical_observation_id=made.id, candidate_id=candidate.id, role="primary"
        )
    )
    db.flush()
    return made


def _slot(db: Session, anchor_id: UUID, slot: str) -> ObservationCandidate:
    anchor = db.get_one(ObservationCandidate, anchor_id)
    rank = next(flag for flag in anchor.ambiguity_flags if flag.startswith("row-rank:"))
    return next(
        candidate
        for candidate in db.scalars(
            select(ObservationCandidate).where(
                ObservationCandidate.extraction_run_id == anchor.extraction_run_id
            )
        )
        if rank in candidate.ambiguity_flags and f"slot:{slot}" in candidate.ambiguity_flags
    )


def _answered(
    db: Session, tmp_path: Path
) -> tuple[PackageRevision, dict[int, UUID], list[Finding]]:
    """Two checked countertop rows, then a person's confirm, fix, wall choice, page answer and a
    result checked by hand as wrong."""
    _project, package_id, anchors = _package_rows(db, widths_add_up=True)
    findings = _run_current_checks(db, package_id, tmp_path)
    revision = db.scalars(
        select(PackageRevision).where(PackageRevision.package_id == package_id)
    ).one()
    sitting = ReviewSession(package_revision_id=revision.id, reviewer=ANA)
    db.add(sitting)
    db.flush()

    # A confirmation: the AI's reading, agreed with.
    confirmed = _observation(
        db, _slot(db, anchors[0], "0"), EvidenceStatus.HUMAN_CONFIRMED, Fraction(20)
    )
    emit(
        db,
        category=AuditCategory.REVIEW_ACTION,
        actor=ANA,
        target_id=confirmed.id,
        target_type="canonical_observation",
    )
    # A fix: the AI read 21, the drawing says 21 1/2.
    read = _slot(db, anchors[1], "0")
    rejected = _observation(db, read, EvidenceStatus.REJECTED, Fraction(21))
    fixed = _observation(db, read, EvidenceStatus.HUMAN_CONFIRMED, Fraction(43, 2))
    correct = ReviewAction(
        review_session_id=sitting.id,
        finding_id=findings[0].id,
        package_revision_id=revision.id,
        action="correct",
        actor=OMAR,
        note="AI misread",
        original_observation_id=rejected.id,
        resulting_observation_id=fixed.id,
    )
    db.add(correct)
    db.flush()
    record_correction(
        db,
        review_action_id=correct.id,
        canonical_observation_id=rejected.id,
        original="21 in",
        corrected="21 1/2 in",
    )
    # Walls chosen on row 1.
    db.add(
        SlotRowReviewDecision(
            row_candidate_id=anchors[1],
            supersedes_id=None,
            wall_config="back_only",
            measurements={},
            confirmed_by=ANA,
        )
    )
    # A page answered as having no countertop (its reading record: the page's own candidate).
    page_record = db.get_one(ObservationCandidate, anchors[0])
    db.add(
        NoCountertopAnswer(
            package_revision_id=revision.id,
            row_choice_candidate_id=page_record.id,
            page_id=page_record.page_id,
            page_number=1,
            page_content_hash="0" * 64,
            reading_answer="no-row:no-drawing",
            answer="no_countertop",
            answered_by=OMAR,
        )
    )
    # A result checked by hand and found wrong (the simple review's fixed note).
    db.add(
        ReviewAction(
            review_session_id=sitting.id,
            finding_id=findings[-1].id,
            package_revision_id=revision.id,
            action="dismiss",
            actor=ANA,
            note=CHECKED_WRONG_NOTE,
        )
    )
    db.flush()
    return revision, anchors, findings


def _approve(db: Session, revision: PackageRevision) -> Approval:
    live = db.scalars(
        select(Finding)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .where(Finding.package_revision_id == revision.id, CheckRun.superseded_at.is_(None))
    ).all()
    revision.state = "APPROVED"
    approval = Approval(package_revision_id=revision.id, approved_by=ANA)
    db.add(approval)
    db.flush()
    db.add_all(
        ApprovedFinding(approval_id=approval.id, package_revision_id=revision.id, finding_id=f.id)
        for f in live
    )
    request_signed_exports(db, approval.id)
    db.commit()
    return approval


def _snapshot(db: Session, approval: Approval) -> ExportSnapshot:
    return load_snapshot(
        db,
        db.scalars(
            select(ApprovalExportSnapshot).where(ApprovalExportSnapshot.approval_id == approval.id)
        ).one(),
    )


def _by_words(marks: FrozenReviewerMarks) -> dict[str, Any]:
    return {mark.words: mark for mark in marks.marks}


def test_the_snapshot_freezes_every_persons_answer_as_a_mark(
    session: Session, tmp_path: Path
) -> None:
    revision, _anchors, _findings = _answered(session, tmp_path)
    payload = _snapshot(session, _approve(session, revision))

    assert payload.format_version == 2
    assert payload.reviewer_marks is not None
    marks = _by_words(payload.reviewer_marks)
    assert marks["Confirmed by AB"].kind == "confirmed"
    assert marks['Fixed by OD: AI read 21" → 21 1/2"'].kind == "fixed"
    assert marks[WALLS].kind == "answered"
    assert marks["No countertop on this page: confirmed by OD"].kind == "answered"
    assert marks["Checked by reviewer AB: WRONG"].kind == "checked_wrong"
    assert "Note: AI misread" in marks['Fixed by OD: AI read 21" → 21 1/2"'].details
    # Placed only at code-recorded places: the readings' stored polygons, the rows' boxes through
    # the transform recorded when they were read.
    for words in (
        "Confirmed by AB",
        'Fixed by OD: AI read 21" → 21 1/2"',
        WALLS,
        "Checked by reviewer AB: WRONG",
    ):
        place = json.loads(marks[words].place)
        assert place["space"] == "stored" and place["polygon"]


def test_a_confirmation_is_never_a_pass_mark(session: Session, tmp_path: Path) -> None:
    revision, _anchors, _findings = _answered(session, tmp_path)
    payload = _snapshot(session, _approve(session, revision))

    assert payload.reviewer_marks is not None
    for mark in payload.reviewer_marks.marks:
        assert "PASS" not in mark.words


def test_a_fixed_reading_is_drawn_once_as_fixed_not_also_as_confirmed(
    session: Session, tmp_path: Path
) -> None:
    revision, _anchors, _findings = _answered(session, tmp_path)
    payload = _snapshot(session, _approve(session, revision))

    assert payload.reviewer_marks is not None
    kinds = [mark.kind for mark in payload.reviewer_marks.marks]
    assert kinds.count("fixed") == 1 and kinds.count("confirmed") == 1


def test_an_answer_after_sign_off_is_not_frozen(session: Session, tmp_path: Path) -> None:
    revision, anchors, findings = _answered(session, tmp_path)
    approval = _approve(session, revision)
    _observation(
        session, _slot(session, anchors[1], "overall"), EvidenceStatus.HUMAN_CONFIRMED, Fraction(9)
    )
    session.commit()

    later = reviewer_marks_for(
        session,
        revision.id,
        findings=[(finding, "rule") for finding in findings],
        decisions=(),
        until=approval.created_at,
    )
    assert later.marks == tuple(
        mark
        for mark in _snapshot(session, approval).reviewer_marks.marks  # type: ignore[union-attr]
        if mark.kind != "checked_wrong"
    )


def test_a_signed_cloud_pdf_from_a_format_2_snapshot_draws_its_marks(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision, _anchors, _findings = _answered(session, tmp_path)
    approval = _approve(session, revision)
    given = _capture_redline(monkeypatch)

    generate_signed_outputs(session, LocalStore(root=tmp_path / "signed"), approval.id)

    texts = {mark.text for mark in given[0]["reviewer_marks"]}
    assert "✓ Confirmed by AB" in texts
    assert '✎ Fixed by OD: AI read 21" → 21 1/2"' in texts
    assert "✗ Checked by reviewer AB: WRONG" in texts
    # The depth and sink checks this package waits for are printed NOT CHECKED, from the snapshot.
    frozen = _snapshot(session, approval).reviewer_marks
    assert frozen is not None and frozen.not_checked_finding_ids
    assert given[0]["not_checked"] == frozenset(frozen.not_checked_finding_ids)


def test_a_snapshot_frozen_before_1363_loads_as_format_1_and_draws_no_marks(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input: a snapshot with no `format_version` and no `reviewer_marks` key, as every snapshot
    frozen before #1363. Outcome: it loads as format 1, and the cloud PDF is asked for with no marks
    and nothing printed NOT CHECKED, the renderer's defaults;
    `tests/reports/test_redline_old_format_golden.py` pins that those draw the same bytes."""
    monkeypatch.setattr("app.review.signed_exports.SNAPSHOT_FORMAT_VERSION", 1)
    monkeypatch.setattr("app.review.signed_exports.reviewer_marks_for", lambda *_a, **_k: None)
    revision, _anchors, _findings = _answered(session, tmp_path)
    approval = _approve(session, revision)
    snapshot = session.scalars(
        select(ApprovalExportSnapshot).where(ApprovalExportSnapshot.approval_id == approval.id)
    ).one()
    stored = json.loads(snapshot.canonical_json)
    del stored["format_version"], stored["reviewer_marks"]
    old = ExportSnapshot.model_validate(stored)
    assert old.format_version == 1 and old.reviewer_marks is None
    monkeypatch.undo()

    given = _capture_redline(monkeypatch)
    generate_signed_outputs(session, LocalStore(root=tmp_path / "signed"), approval.id)

    assert given[0]["reviewer_marks"] is None and given[0]["not_checked"] == frozenset()


@pytest.mark.parametrize(
    ("version", "marks"),
    [(2, None), (1, FrozenReviewerMarks(marks=(), not_checked_finding_ids=())), (3, None)],
)
def test_a_snapshot_with_the_wrong_marks_for_its_format_is_refused(
    session: Session, tmp_path: Path, version: int, marks: FrozenReviewerMarks | None
) -> None:
    revision, _anchors, _findings = _answered(session, tmp_path)
    payload = _snapshot(session, _approve(session, revision))
    stored = payload.model_dump(mode="json")
    stored["format_version"] = version
    stored["reviewer_marks"] = None if marks is None else marks.model_dump(mode="json")
    with pytest.raises(ValueError):
        ExportSnapshot.model_validate(stored)


def test_a_malformed_snapshot_is_refused_when_loaded(session: Session, tmp_path: Path) -> None:
    """The refusal reaches `load_snapshot` as a `SignedExportRefused`, never a crash."""
    revision, _anchors, _findings = _answered(session, tmp_path)
    approval = _approve(session, revision)
    snapshot = session.scalars(
        select(ApprovalExportSnapshot).where(ApprovalExportSnapshot.approval_id == approval.id)
    ).one()
    from app.review.signed_exports import canonical, digest

    stored = json.loads(snapshot.canonical_json)
    stored["reviewer_marks"] = None
    fake = ApprovalExportSnapshot(
        approval_id=approval.id,
        package_revision_id=revision.id,
        canonical_json=canonical(stored),
        sha256=digest(canonical(stored)),
        deterministic_sha256=snapshot.deterministic_sha256,
    )
    with pytest.raises(SignedExportRefused):
        load_snapshot(session, fake)


@pytest.mark.skip(
    reason="#1358 adds the 'Checked by reviewer: WRONG' wording to signed_record; enable when merged"
)
def test_the_wrong_mark_carries_the_1358_wording(session: Session, tmp_path: Path) -> None:
    revision, _anchors, _findings = _answered(session, tmp_path)
    payload = _snapshot(session, _approve(session, revision))
    assert payload.reviewer_marks is not None
    wrong = _by_words(payload.reviewer_marks)["Checked by reviewer AB: WRONG"]
    assert any(detail.startswith("Checked by reviewer: WRONG") for detail in wrong.details)


def test_initials_name_the_person_and_the_full_name_stays_in_the_details() -> None:
    assert initials("ana.bell@example.com") == "AB"
    assert initials("Ana Bell") == "AB"
    assert initials("reviewer") == "RE"
    assert initials("   ") == "?"


def test_a_check_that_could_not_run_is_not_checked() -> None:
    assert is_not_checked("NO_APPLICABLE_RULE", None)
    assert is_not_checked("REVIEW_REQUIRED", "Waiting for the depth reading.")
    assert is_not_checked("NOT_FOUND", "Waiting for the sink size.")
    assert not is_not_checked("REVIEW_REQUIRED", "The readers differ.")
    assert not is_not_checked("FAIL", "Waiting for nothing; a decided result.")


def _capture_redline(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    given: list[dict[str, Any]] = []

    def redline(_db: Session, the_store: LocalStore, **kwargs: Any) -> RedlineOutput:
        given.append(kwargs)
        page = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(page)
        page.seek(0)
        return RedlineOutput(
            the_store.put(f"redline/{uuid4()}.pdf", page, content_type="application/pdf"), None
        )

    monkeypatch.setattr("workflow.signed_outputs.render_evidence_grounded_redline", redline)
    return given


def test_the_draft_cloud_pdf_shows_the_same_marks_from_the_live_records(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workflow import draft_cloud_render

    revision, _anchors, _findings = _answered(session, tmp_path)
    session.commit()
    given: list[dict[str, Any]] = []

    def drawing(_db: Session, the_store: LocalStore, **kwargs: Any) -> RedlineOutput:
        given.append(kwargs)
        page = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(page)
        page.seek(0)
        return RedlineOutput(
            the_store.put(f"draft/{uuid4()}.pdf", page, content_type="application/pdf"), None
        )

    monkeypatch.setattr(draft_cloud_render, "render_draft_redline", drawing)
    draft_cloud_render.render_draft_cloud(
        session, LocalStore(root=tmp_path / "draft"), revision.id, uuid4(), requested_by=ANA
    )

    texts = {mark.text for mark in given[0]["reviewer_marks"]}
    assert {
        "✓ Confirmed by AB",
        '✎ Fixed by OD: AI read 21" → 21 1/2"',
        f"✍ {WALLS}",
        "✍ No countertop on this page: confirmed by OD",
        "✗ Checked by reviewer AB: WRONG",
    } <= texts
