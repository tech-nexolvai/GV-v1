"""Every reviewer question is optional (owner decision 2026-10-16).

"It should be according to the reviewer: if he wants to add, he can; otherwise it just runs."

* Only real system states hold sign-off: no results yet, a corrected result waiting for its
  re-run, a page waiting for a re-run, inputs changed after the last run, not awaiting review.
  An open person-question (a held number, a countertop to check by hand, a page to decide, a FAIL
  nobody agreed with) does not.
* What nobody answered is printed, never dropped: a grey "Not confirmed by a person" mark at its
  spot when it has one, and a "Left for a person (N)" cover section grouped by kind, on the draft
  and on the final (signed) cloud PDF (snapshot format 5). A FAIL stays a FAIL cloud.

Every value here is synthetic.
"""

from __future__ import annotations

import io
from uuid import uuid4

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.roles import Principal, Role
from app.models import Finding
from app.models.signed_exports import ApprovalExportSnapshot
from app.review.approval import approval_readiness, approve_package
from app.review.left_for_person import (
    NOT_CONFIRMED,
    FrozenLeftForPerson,
    FrozenLeftItem,
    cover_groups,
    left_for_person_marks,
)
from app.review.session import open_session
from app.review.signed_exports import SNAPSHOT_FORMAT_VERSION, ExportSnapshot, load_snapshot
from reports.redline import REVIEWER_MARK_GLYPHS, ReviewerMarkKind
from reports.signed_review import ReviewCover, SignedReview, review_pdf
from storage.local import LocalStore
from tests.api.test_draft_cloud import _checked
from tests.api.test_draft_cloud import session as session_fixture
from tests.api.test_draft_cloud import store as store_fixture

session = session_fixture
store = store_fixture

DISAGREED = "The two AIs did not agree on this line."


def _text(content: bytes) -> str:
    return " ".join(
        " ".join((page.extract_text() or "").split())
        for page in PdfReader(io.BytesIO(content)).pages
    )


def test_open_person_questions_no_longer_hold_sign_off(session: Session, store: LocalStore) -> None:
    """The FAIL nobody agreed with and the held result nobody answered stay listed, but the
    package can be signed: nothing a person has not answered blocks sign-off any more."""
    _, revision = _checked(session, store)
    fail = session.scalars(select(Finding).where(Finding.outcome == "FAIL")).one()
    held = session.scalars(select(Finding).where(Finding.reason == DISAGREED)).one()

    readiness = approval_readiness(session, revision.id)

    assert readiness.can_approve is True
    assert readiness.reason is None
    # Still listed as open for a person (optional): the screen and the summary count them.
    assert set(readiness.blocking_finding_ids) == {fail.id, held.id}


def test_a_system_state_still_holds_sign_off(session: Session, store: LocalStore) -> None:
    _, revision = _checked(session, store)
    revision.state = "EXTRACTING"
    session.commit()

    readiness = approval_readiness(session, revision.id)

    assert readiness.can_approve is False
    assert readiness.reason == "The package is not awaiting review."


def test_signing_with_questions_left_prints_them_as_not_confirmed(
    session: Session, store: LocalStore
) -> None:
    """Sign-off goes through with the questions unanswered; the snapshot (format 5) freezes each
    one, and the signed cover lists them under "Left for a person"."""
    _, revision = _checked(session, store)
    review = open_session(session, package_revision_id=revision.id, reviewer="ana@example.com")
    session.commit()

    decision = approve_package(
        session,
        principal=Principal("ana@example.com", frozenset({Role.REVIEWER})),
        review_session_id=review.id,
    )
    session.commit()

    stored = session.scalars(
        select(ApprovalExportSnapshot).where(
            ApprovalExportSnapshot.approval_id == decision.approval.id
        )
    ).one()
    payload = load_snapshot(session, stored)
    assert payload.format_version == SNAPSHOT_FORMAT_VERSION == 5
    assert payload.left_for_person is not None
    kinds = {item.kind for item in payload.left_for_person.items}
    assert "failure" in kinds, "the FAIL nobody agreed with is printed as not agreed"
    assert len(payload.left_for_person.items) >= 2
    for item in payload.left_for_person.items:
        assert item.words.startswith("Not confirmed by a person")
        assert "(AI: " in item.words


def test_an_older_snapshot_has_no_left_for_person_section() -> None:
    """Formats 1-4 never carry the section, and format 5 always does."""
    import pytest
    from pydantic import ValidationError

    fields = ExportSnapshot.model_fields
    assert "left_for_person" in fields
    with pytest.raises(ValidationError):
        ExportSnapshot.model_validate(
            {
                "format_version": 4,
                "package_revision_id": str(uuid4()),
                "revision_number": 1,
                "vendor": None,
                "review": {
                    "approval_id": str(uuid4()),
                    "approved_by": "a",
                    "approved_at": "2026-10-16T00:00:00Z",
                    "findings": [],
                },
                "facts": [],
                "changed_values": {"items": []},
                "reviewer_marks": {"marks": [], "not_checked_finding_ids": []},
                "old_compare_off": True,
                "left_for_person": {"items": []},
            }
        )


def _item(kind: str, words: str, place: str | None = None) -> FrozenLeftItem:
    return FrozenLeftItem(
        kind=kind,
        kind_words={
            "number": "Numbers to confirm",
            "no_countertop_page": "Pages to decide",
            "failure": "Clouded as wrong, not agreed by a person",
        }[kind],
        what="Width 2 on Countertop 1",
        page=3,
        words=words,
        place=place,
        unplaced_reason=None if place else "no recorded place on the drawing",
    )


def test_marks_are_grey_not_confirmed_and_a_fail_keeps_its_own_cloud() -> None:
    frozen = FrozenLeftForPerson(
        items=(
            _item("number", 'Not confirmed by a person — Width 2 (AI: 24 1/2")', place='{"p":1}'),
            _item("no_countertop_page", "Not confirmed by a person — Decide this page (AI: x)"),
            _item("failure", "Not confirmed by a person — Overall (AI: Vendor wrote 72 in)"),
        )
    )

    marks = left_for_person_marks(frozen)

    assert [mark.kind for mark in marks] == [ReviewerMarkKind.NOT_CONFIRMED] * 2
    assert marks[0].evidence_ref == '{"p":1}'
    assert marks[1].unplaced_reason == "no recorded place on the drawing"
    assert REVIEWER_MARK_GLYPHS[ReviewerMarkKind.NOT_CONFIRMED]
    assert NOT_CONFIRMED == "Not confirmed by a person"


def test_the_cover_lists_what_was_left_for_a_person_grouped_by_kind() -> None:
    frozen = FrozenLeftForPerson(
        items=(
            _item("number", 'Not confirmed by a person — Width 2 (AI: 24 1/2")'),
            _item("number", 'Not confirmed by a person — Width 3 (AI: 30")'),
            _item("no_countertop_page", "Not confirmed by a person — Decide this page (AI: x)"),
        )
    )
    groups = cover_groups(frozen)
    assert [(words, len(lines)) for words, lines in groups] == [
        ("Numbers to confirm", 2),
        ("Pages to decide", 1),
    ]
    review = SignedReview.model_validate(
        {
            "approval_id": str(uuid4()),
            "approved_by": "ana@example.com",
            "approved_at": "2026-10-16T00:00:00Z",
            "findings": [
                {
                    "finding_id": str(uuid4()),
                    "rule_id": "CT-MADE-UP",
                    "outcome": "PASS",
                    "actions": [],
                }
            ],
        }
    )

    text = _text(review_pdf(review, ReviewCover(left_for_person=groups)))

    assert "Left for a person (3)" in text
    assert "Numbers to confirm (2)" in text
    assert "Pages to decide (1)" in text
    assert "Width 3" in text
    # A cover with nothing left for a person (or an older one) is drawn as before.
    assert "Left for a person" not in _text(review_pdf(review, ReviewCover()))


def test_the_draft_and_the_signed_cloud_pdf_print_what_was_left(
    session: Session, store: LocalStore
) -> None:
    """End to end on the synthetic package: the draft's front page and the signed cloud PDF both
    say "Left for a person" with each question "Not confirmed by a person"; the FAIL is still
    listed as clouded as wrong."""
    from app.models import OutputArtifact
    from workflow.draft_cloud_render import render_draft_cloud
    from workflow.signed_outputs import generate_signed_outputs

    _, revision = _checked(session, store)
    drawn = render_draft_cloud(session, store, revision.id, uuid4(), requested_by="ana@example.com")
    draft = session.get(OutputArtifact, drawn["draft_id"])
    assert draft is not None
    draft_text = _text(store.get(draft.storage_key).read())
    assert "Left for a person (" in draft_text
    assert "Not confirmed by a person" in draft_text

    review = open_session(session, package_revision_id=revision.id, reviewer="ana@example.com")
    session.commit()
    decision = approve_package(
        session,
        principal=Principal("ana@example.com", frozenset({Role.REVIEWER})),
        review_session_id=review.id,
    )
    session.commit()
    bundle = generate_signed_outputs(session, store, decision.approval.id)
    session.commit()
    redline = session.get(OutputArtifact, bundle.redline_id)
    assert redline is not None
    signed_text = _text(store.get(redline.storage_key).read())
    assert "Left for a person (" in signed_text
    assert "Not confirmed by a person" in signed_text
    assert "Will be clouded as wrong (1)" in signed_text
