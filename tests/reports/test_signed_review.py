"""The reviewer record adds no new verdict (#953)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from reports.signed_review import ReviewDisposition, SignedReview, SignedReviewFinding


@pytest.mark.parametrize("outcome", ["NOT_FOUND", "FAIL"])
def test_pdf_and_workbook_include_the_same_record(outcome: str) -> None:
    from io import BytesIO

    from openpyxl import load_workbook
    from pypdf import PdfReader

    from reports.findings_pdf import FindingsPdfInput, write_findings_pdf
    from reports.spreadsheet import StoredFinding, write_stored_workbook

    at = datetime(2026, 1, 1, tzinfo=UTC)
    finding = SignedReviewFinding(
        finding_id=uuid4(),
        rule_id="CHECK-1",
        outcome=outcome,
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
        outcome=outcome,
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
    assert finding.wording in text
    assert "Source missing" in text
    assert str(review.approval_id) in text
    book = load_workbook(BytesIO(write_stored_workbook((stored,), signed_review=review)))
    text = " ".join(str(c.value) for sheet in book for row in sheet for c in row if c.value)
    assert finding.wording in text
    assert str(review.approval_id) in text
    assert book["Findings"]["B2"].value == outcome


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
        "Not checked: dismissed as not checkable by reviewer reviewer on 2026-01-01T00:00:00+00:00. "
        "Recorded check remains NOT_FOUND. "
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


@pytest.mark.parametrize("outcome", ["REVIEW_REQUIRED", "NOT_FOUND"])
def test_checked_by_hand_wrong_prints_wrong_never_ok_or_accepted(outcome: str) -> None:
    """A person's "checked by hand: wrong" (#1358) is a DISMISS with the one shared note: printed as
    WRONG, a problem for the vendor, with the recorded check unchanged; and marked for the cloud PDF
    without matching its words."""
    from app.review.signed_record import CHECKED_WRONG_NOTE

    wrong = ReviewDisposition(
        action_id=uuid4(),
        action="dismiss",
        reviewer="reviewer",
        at=datetime(2026, 1, 1, tzinfo=UTC),
        note=CHECKED_WRONG_NOTE,
    )
    finding = SignedReviewFinding(
        finding_id=uuid4(), rule_id="CHECK-1", outcome=outcome, actions=(wrong,)
    )
    assert finding.wording == (
        "Checked by reviewer: WRONG — problem for the vendor, by reviewer reviewer on "
        f"2026-01-01T00:00:00+00:00. Recorded check remains {outcome}."
    )
    for never in ("OK", "accepted", "dismissed", "PASS"):
        assert never not in finding.wording
    assert wrong.checked_wrong_by_hand is True
    # Any other dismissal keeps its own words and is not marked wrong.
    other = wrong.model_copy(update={"note": "Required source not supplied."})
    assert other.checked_wrong_by_hand is False
    assert other.wording(outcome).startswith("Not checked: dismissed as not checkable")
    confirmed = wrong.model_copy(update={"action": "confirm"})
    assert confirmed.checked_wrong_by_hand is False


def _cover_review() -> SignedReview:
    from app.review.signed_record import CHECKED_WRONG_NOTE

    at = datetime(2026, 1, 1, tzinfo=UTC)

    def finding(rule_id: str, outcome: str, note: str | None) -> SignedReviewFinding:
        actions = (
            ()
            if note is None
            else (
                ReviewDisposition(
                    action_id=uuid4(), action="dismiss", reviewer="Made Up", at=at, note=note
                ),
            )
        )
        return SignedReviewFinding(
            finding_id=uuid4(), rule_id=rule_id, outcome=outcome, actions=actions
        )

    return SignedReview(
        approval_id=uuid4(),
        approved_by="Made Up",
        approved_at=at,
        findings=(
            finding("CHECK-FAIL", "FAIL", None),
            finding("CHECK-HAND", "REVIEW_REQUIRED", CHECKED_WRONG_NOTE),
            finding("CHECK-PASS", "PASS", None),
            finding("CT-ARCH-WIDTH-001", "NO_APPLICABLE_RULE", None),
        ),
    )


def _cover_text(content: bytes) -> str:
    from io import BytesIO

    from pypdf import PdfReader

    return " ".join(
        " ".join((page.extract_text() or "") for page in PdfReader(BytesIO(content)).pages).split()
    )


def test_an_older_snapshots_cover_is_drawn_exactly_as_before() -> None:
    from reports.signed_review import review_pdf

    review = _cover_review()
    assert review_pdf(review) == review_pdf(review, None)
    text = _cover_text(review_pdf(review))
    assert "Will be clouded as wrong" not in text
    assert "CT-ARCH-WIDTH-001" in text


def test_a_format_3_cover_counts_wrong_by_hand_and_lists_the_architect_compare() -> None:
    """Final QA 2026-10-16: the final cover counts a result checked by hand as wrong under "Will
    be clouded as wrong", lists the compare with every difference as the draft does, and leaves the
    old compare's row out (said once) while it was switched off."""
    from reports.redline import (
        AiReviewCounts,
        ArchitectCompare,
        ArchitectDifference,
        review_cover_for,
    )
    from reports.signed_review import review_pdf

    compare = ArchitectCompare(
        counts=AiReviewCounts(same=3, different=1, not_shown=2, unsure=0),
        different=(
            ArchitectDifference(
                architect_text='36"',
                vendor_text='35 1/2"',
                subject="Page 2",
                unplaced_reason="made up",
                measures="overall width",
                ai_read=True,
            ),
        ),
    )
    cover = review_cover_for(
        compare,
        left_out_rules=frozenset({"CT-ARCH-WIDTH-001"}),
        left_out_note="1 result(s) of the old architect check left out: it is switched off.",
    )
    text = _cover_text(review_pdf(_cover_review(), cover))

    assert "Will be clouded as wrong (2)" in text
    assert "CHECK-FAIL" in text and "CHECK-HAND: Checked by reviewer: WRONG" in text
    assert "Compared with the architect (AI read) (1 different)" in text
    assert (
        "Compared with the architect (AI read): 3 same · 1 different · 2 not shown · 0 unsure"
        in text
    )
    assert 'A1 Needs: 36" (per architect: overall width) — Vendor drew: 35 1/2" — Page 2' in text
    assert "CT-ARCH-WIDTH-001" not in text
    assert "left out: it is switched off" in text


def _many_not_checked(count: int) -> SignedReview:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    return SignedReview(
        approval_id=uuid4(),
        approved_by="Made Up",
        approved_at=at,
        findings=(
            SignedReviewFinding(
                finding_id=uuid4(), rule_id="CHECK-FAIL", outcome="FAIL", actions=()
            ),
            *(
                SignedReviewFinding(
                    finding_id=uuid4(),
                    rule_id="CT-DEPTH-001",
                    outcome="REVIEW_REQUIRED",
                    scope_label=f"Countertop {n}",
                    actions=(),
                )
                for n in range(count)
            ),
        ),
    )


def test_a_format_4_cover_groups_the_not_checked_results_by_reason() -> None:
    """Set 3 QA: the final cover listed 174 not-checked results one by one ("REVIEW_REQUIRED: no
    per-finding action recorded" with raw ids, 28 pages). A format-4 cover groups them by reason,
    as the draft's front page does."""
    from reports.signed_review import ReviewCover, review_pdf

    review = _many_not_checked(60)
    ids = frozenset(f.finding_id for f in review.findings if f.rule_id == "CT-DEPTH-001")
    labels = tuple(f"Countertop {n} — CT-DEPTH-001" for n in range(60))
    cover = ReviewCover(
        not_checked=(("Waiting for values nothing has read yet.", labels),),
        not_checked_ids=ids,
    )
    content = review_pdf(review, cover)
    text = _cover_text(content)

    assert "NOT CHECKED (60): grouped by reason" in text
    assert "60 check(s): Waiting for values nothing has read yet." in text
    # The FAIL only: once under "Will be clouded as wrong", once on its own.
    assert text.count("no per-finding action recorded") == 2
    assert all(str(finding_id) not in text for finding_id in ids)
    from io import BytesIO

    from pypdf import PdfReader

    assert len(PdfReader(BytesIO(content)).pages) <= 3


def test_a_format_3_cover_without_groups_is_drawn_as_before() -> None:
    from reports.signed_review import ReviewCover, review_pdf

    review = _many_not_checked(3)
    text = _cover_text(review_pdf(review, ReviewCover()))
    assert "NOT CHECKED (" not in text
    assert text.count("no per-finding action recorded") == 5  # the FAIL twice, each result once


def test_the_signed_outputs_group_only_unanswered_not_checked_results() -> None:
    from types import SimpleNamespace

    from workflow.signed_outputs import not_checked_groups

    review = _many_not_checked(2)
    depth = [f for f in review.findings if f.rule_id == "CT-DEPTH-001"]
    rows = [
        (
            SimpleNamespace(
                id=f.finding_id, reason="Waiting for values.", trace={}, scope_label=f.scope_label
            ),
            None,
            None,
            SimpleNamespace(rule_id=f.rule_id),
        )
        for f in review.findings
    ]
    groups, ids = not_checked_groups(rows, review, frozenset(f.finding_id for f in depth))
    assert groups == (
        ("Waiting for values.", ("Countertop 0 — CT-DEPTH-001", "Countertop 1 — CT-DEPTH-001")),
    )
    assert ids == frozenset(f.finding_id for f in depth)
