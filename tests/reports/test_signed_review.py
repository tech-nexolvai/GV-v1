"""The reviewer record adds no new verdict (#953)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from reports.signed_review import ReviewDisposition, SignedReview, SignedReviewFinding


def test_pdf_and_workbook_include_the_same_record() -> None:
    from io import BytesIO

    from openpyxl import load_workbook
    from pypdf import PdfReader

    from reports.findings_pdf import FindingsPdfInput, write_findings_pdf
    from reports.spreadsheet import StoredFinding, write_stored_workbook

    at = datetime(2026, 1, 1, tzinfo=UTC)
    finding = SignedReviewFinding(
        finding_id=uuid4(),
        rule_id="CHECK-1",
        outcome="NOT_FOUND",
        actions=(
            ReviewDisposition(
                action_id=uuid4(),
                action="dismiss",
                reviewer="reviewer",
                at=at,
                note="Source missing",
            ),
        ),
    )
    review = SignedReview(
        approval_id=uuid4(), approved_by="reviewer", approved_at=at, findings=(finding,)
    )
    stored = StoredFinding(
        rule_id="CHECK-1",
        outcome="NOT_FOUND",
        severity="FLAG",
        snapshot_id="snapshot",
        engine_version="test",
        trace={},
    )
    pdf = write_findings_pdf(
        FindingsPdfInput(
            package_revision_id=uuid4(),
            revision_number=1,
            vendor=None,
            findings=(stored,),
            signed_review=review,
        )
    )
    text = " ".join(" ".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages).split())
    assert "Not checked: dismissed by reviewer reviewer" in text
    assert "Source missing" in text
    assert str(review.approval_id) in text
    book = load_workbook(BytesIO(write_stored_workbook((stored,), signed_review=review)))
    text = " ".join(str(c.value) for sheet in book for row in sheet for c in row if c.value)
    assert finding.wording in text
    assert str(review.approval_id) in text
    assert book["Findings"]["B2"].value == "NOT_FOUND"


def test_dismissal_is_not_a_pass_or_a_checked_result() -> None:
    action = ReviewDisposition(
        action_id=uuid4(),
        action="dismiss",
        reviewer="reviewer",
        at=datetime(2026, 1, 1, tzinfo=UTC),
        note="Required source not supplied.",
    )
    finding = SignedReviewFinding(
        finding_id=uuid4(), rule_id="CHECK-1", outcome="NOT_FOUND", actions=(action,)
    )
    assert finding.wording == (
        "Not checked: dismissed by reviewer reviewer on 2026-01-01T00:00:00+00:00. "
        "Reason: Required source not supplied."
    )
    assert finding.outcome == "NOT_FOUND"
    assert "PASS" not in finding.wording


@pytest.mark.parametrize(
    "action,phrase",
    [
        ("confirm", "confirmed"),
        ("correct", "corrected"),
        ("except", "exception recorded"),
        ("dismiss", "dismissed"),
    ],
)
def test_every_action_is_preserved(action: str, phrase: str) -> None:
    disposition = ReviewDisposition(
        action_id=uuid4(),
        action=action,
        reviewer="reviewer",
        at=datetime(2026, 1, 1, tzinfo=UTC),
        note="Recorded reason",
    )
    finding = SignedReviewFinding(
        finding_id=uuid4(), rule_id="CHECK-1", outcome="FAIL", actions=(disposition,)
    )
    assert phrase in finding.wording
    assert finding.outcome == "FAIL"
    assert "PASS" not in finding.wording


def test_unsigned_action_is_not_invented() -> None:
    finding = SignedReviewFinding(
        finding_id=uuid4(), rule_id="CHECK-1", outcome="NOT_FOUND", actions=()
    )
    assert "no per-finding action recorded" in finding.wording


def test_equal_timestamp_actions_are_refused() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    actions = tuple(
        ReviewDisposition(action_id=uuid4(), action=action, reviewer="reviewer", at=at)
        for action in ("confirm", "dismiss")
    )
    with pytest.raises(ValueError, match="ambiguous"):
        SignedReviewFinding(finding_id=uuid4(), rule_id="CHECK-1", outcome="FAIL", actions=actions)


def test_signoff_names_the_record_and_coverage() -> None:
    finding = SignedReviewFinding(finding_id=uuid4(), rule_id="CHECK-1", outcome="FAIL", actions=())
    record = SignedReview(
        approval_id=uuid4(),
        approved_by="reviewer",
        approved_at=datetime(2026, 1, 1, tzinfo=UTC),
        findings=(finding,),
    )
    assert "Signed off by reviewer" in record.signoff
    assert str(record.approval_id) in record.signoff
    assert "1 findings covered" in record.signoff
