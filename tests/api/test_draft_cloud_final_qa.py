"""The draft cloud PDF after the final QA (2026-10-16).

* A result a person checked by hand and found wrong is counted under "Will be clouded as wrong",
  read from the signed record's own mark (`ReviewDisposition.checked_wrong_by_hand`).
* While the old row-by-row architect compare is switched off (`GV_ARCH_COMPARE_RECORDS`), its
  leftover "nothing was compared" row is neither printed in the appendix nor counted in the results
  in force; the front page says, once, that it was left out and why (never silent).
"""

from __future__ import annotations

import io

import pytest
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Finding
from app.models.review import ReviewAction, ReviewSession
from app.review.signed_record import CHECKED_WRONG_NOTE
from storage.local import LocalStore
from tests.api.test_draft_cloud import _checked
from tests.api.test_draft_cloud import session as session_fixture
from tests.api.test_draft_cloud import store as store_fixture
from tests.api.test_draft_cloud_qa_fixes import NOTHING_COMPARED, _old_compare_row
from workflow.draft_cloud_render import _live_rows, open_items, render_draft_cloud

session = session_fixture
store = store_fixture

DISAGREED = "The two AIs did not agree on this line."


def _pages(content: bytes) -> list[str]:
    return [
        " ".join((page.extract_text() or "").split())
        for page in PdfReader(io.BytesIO(content)).pages
    ]


def _checked_wrong_by_hand(db: Session, finding: Finding) -> None:
    sitting = ReviewSession(package_revision_id=finding.package_revision_id, reviewer="Made Up")
    db.add(sitting)
    db.flush()
    db.add(
        ReviewAction(
            review_session_id=sitting.id,
            finding_id=finding.id,
            package_revision_id=finding.package_revision_id,
            action="dismiss",
            actor="Made Up",
            note=CHECKED_WRONG_NOTE,
        )
    )
    db.commit()


def test_a_result_checked_by_hand_as_wrong_is_counted_as_clouded_wrong(
    session: Session, store: LocalStore
) -> None:
    _, revision = _checked(session, store)
    held = session.scalars(select(Finding).where(Finding.reason == DISAGREED)).one()
    _checked_wrong_by_hand(session, held)

    items = open_items(session, revision.id, _live_rows(session, revision.id))

    assert len(items.failures) == 2, "the FAIL and the one checked by hand as wrong"
    wrong = items.failures[-1]
    assert wrong.reason.startswith("Checked by hand: WRONG by Made Up.")
    assert all(DISAGREED not in item.reason for item in items.needs_a_person)


@pytest.mark.parametrize(("setting", "listed"), [("0", False), ("1", True)])
def test_the_old_compare_row_is_left_out_of_the_draft_while_the_old_compare_is_off(
    session: Session,
    store: LocalStore,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    listed: bool,
) -> None:
    from workflow.draft_cloud import request_draft_cloud

    monkeypatch.setenv("GV_ARCH_COMPARE_RECORDS", setting)
    _, revision = _checked(session, store)
    _old_compare_row(session, revision)
    request = request_draft_cloud(session, revision.id, requested_by="ana@example.com")
    session.commit()

    drawn = render_draft_cloud(session, store, revision.id, request, requested_by="a reviewer")

    from app.models import OutputArtifact

    artifact = session.get(OutputArtifact, drawn["draft_id"])
    assert artifact is not None
    pages = _pages(store.get(artifact.storage_key).read())
    front, rest = pages[0], " ".join(pages[1:])
    assert drawn["findings"] == (5 if listed else 4)
    assert (f"{5 if listed else 4} result(s) in force" in front) is True
    assert ("CT-ARCH-WIDTH-001" in rest) is listed
    assert ("nothing was compared" in rest) is listed
    # (The front page wraps at hyphens, so the rule id may be split across lines.)
    assert ("left out: the old architect check" in front) is not listed
    assert ("is switched off, so nothing was compared by it" in front) is not listed
    assert NOTHING_COMPARED  # the row the helper writes


def test_the_draft_banner_is_a_thin_strip_above_the_pages_content(
    session: Session, store: LocalStore
) -> None:
    """The banner covered side notes (QA 2026-10-16): it now sits in a thin strip added above
    each page's visible top, so nothing of the page is under it."""
    import pdfplumber

    from app.models import OutputArtifact
    from workflow.draft_cloud import request_draft_cloud
    from workflow.draft_cloud_render import BANNER_STRIP

    _, revision = _checked(session, store)
    request = request_draft_cloud(session, revision.id, requested_by="ana@example.com")
    session.commit()
    drawn = render_draft_cloud(session, store, revision.id, request, requested_by="a reviewer")
    artifact = session.get(OutputArtifact, drawn["draft_id"])
    assert artifact is not None
    content = store.get(artifact.storage_key).read()

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        assert len(pdf.pages) >= 2
        for page in pdf.pages:
            banner = [char for char in page.chars if char["fontname"].endswith("Helvetica-Bold")]
            words = "".join(
                char["text"] for char in banner if char["bottom"] <= float(BANNER_STRIP) + 0.5
            )
            assert "DRAFT" in words, "the banner is in the strip at the very top"
            below = [
                char
                for char in page.chars
                if char["top"] < float(BANNER_STRIP) - 0.5 and char not in banner
            ]
            assert not below, "nothing of the page itself is in the banner's strip"


def test_a_check_that_could_not_run_no_longer_holds_sign_off(
    session: Session, store: LocalStore
) -> None:
    """Decision 2026-10-16 ("nothing from the reviewer"): a check waiting for values nothing has
    read is printed NOT CHECKED by itself and no longer blocks sign-off. It is still listed as an
    open NOT CHECKED item on the draft, never as settled by a person."""
    from app.review.approval import approval_readiness
    from tests.api.test_draft_cloud import WAITING

    _, revision = _checked(session, store)
    waiting = session.scalars(select(Finding).where(Finding.reason == WAITING)).one()

    readiness = approval_readiness(session, revision.id)

    assert waiting.id not in readiness.blocking_finding_ids
    assert readiness.not_checked_finding_ids == (waiting.id,)
    assert readiness.blocking_findings == 2, "the FAIL and the disagreement still wait"
    items = open_items(session, revision.id, _live_rows(session, revision.id))
    assert [item.reason for item in items.not_checked] == [WAITING]
    assert items.settled == ()
