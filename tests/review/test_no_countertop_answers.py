"""A page every AI said has no countertop is never skipped silently (#1191).

Anant decided (2026-10-12, option c): an older reading that does not say what its "no line" meant is
blocking (`tests/api/test_unchosen_row_pages.py`), and every page still listed with no countertop
needs one reviewer click before sign-off: "Confirm: no countertop here", or "There is a countertop",
which makes the page a blocking item like a split page. The click is recorded with who and when,
carried only onto an identical page, frozen at sign-off and printed in the signed files.
Synthetic values only.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.auth import Principal, Role
from app.db.session import session_factory
from app.models import (
    Document,
    DocumentKind,
    DocumentVersion,
    NoCountertopAnswer,
    ObservationCandidate,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    SourceArtifact,
)
from app.models.document import Page
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from app.review.approval import (
    approve_package,
)
from app.review.countertop_results import _countertop_results_for_revision
from app.review.no_countertop import (
    pages_blocking_sign_off,
    reading_answer,
)
from app.review.session import open_session
from storage.local import LocalStore
from tests.api.test_unchosen_row_pages import (
    NO_COUNTERTOP_KINDS,
    NONE_PICKS,
    NONE_WHY,
    _live,
)
from tests.app.postgres_fixture import alembic_config
from workflow.slot_row_scope import unchosen_page

pytest_plugins = ("tests.app.postgres_fixture",)

LISTED = NONE_PICKS + NO_COUNTERTOP_KINDS
REVIEWER = "synthetic-reviewer"


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


def _review_session(session: Session, revision_id: UUID) -> UUID:
    review = open_session(session, package_revision_id=revision_id, reviewer=REVIEWER)
    session.commit()
    return review.id


# --- sign-off waits for every listed page ---------------------------------------------------------


# --- repeat clicks --------------------------------------------------------------------------------


# --- carried only onto an identical page ----------------------------------------------------------


def _reread(session: Session, revision_id: UUID) -> UUID:
    """A newer slot-reader run on the revision: only its records count from now on."""
    workflow_run = WorkflowRun(package_revision_id=revision_id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow_run.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task)
    session.flush()
    run = ExtractionRun(
        task_run_id=task.id,
        extractor="extraction.form_reader",
        extractor_version="slot-reader-v1-test",
        config_hash="synthetic-test",
        dpi=150,
    )
    session.add(run)
    session.flush()
    return run.id


def _record_on(session: Session, run_id: UUID, page: Page, flags: tuple[str, ...]) -> UUID:
    record = ObservationCandidate(
        document_version_id=page.document_version_id,
        page_id=page.id,
        extraction_run_id=run_id,
        raw_text="",
        polygon=[[0, 0], [612, 0], [612, 792], [0, 792]],
        coordinate_space="image",
        ambiguity_flags=["slot-reader-row-choice", "row-choice:0", *flags],
        review_reason=NONE_WHY,
    )
    session.add(record)
    session.flush()
    return record.id


def _two_revision_pages(
    session: Session, hashes: tuple[tuple[str, str], ...]
) -> tuple[PackageRevision, PackageRevision, list[Page], list[Page]]:
    """Revision 1 and 2 of one package, each with its own issue of one shop drawing whose pages
    have these content hashes (revision 1's, revision 2's)."""
    from tests.api.test_packages import _new_package

    package_id = _new_package(session)
    one = session.scalars(
        select(PackageRevision).where(PackageRevision.package_id == package_id)
    ).one()
    document = Document(package_id=package_id, kind=DocumentKind.SHOP.value)
    session.add(document)
    session.flush()
    two = PackageRevision(
        package_id=package_id, revision_number=2, state="UPLOADED", supersedes_id=one.id
    )
    session.add(two)
    session.flush()
    pages: list[list[Page]] = []
    for position, revision in enumerate((one, two)):
        digest = hashlib.sha256(f"synthetic issue {position}".encode()).hexdigest()
        artifact = SourceArtifact(storage_key=f"synthetic/{uuid4()}", sha256=digest, size=1)
        session.add(artifact)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            source_artifact_id=artifact.id,
            sha256=digest,
            page_count=len(hashes),
        )
        session.add(version)
        session.flush()
        session.add(
            PackageRevisionDocument(
                package_revision_id=revision.id,
                package_id=package_id,
                document_id=document.id,
                document_version_id=version.id,
            )
        )
        issue = [
            Page(
                document_version_id=version.id,
                index=index,
                content_hash=pair[position],
                width_pt=Decimal(612),
                height_pt=Decimal(792),
                rotation=0,
                has_vector_text=True,
            )
            for index, pair in enumerate(hashes)
        ]
        session.add_all(issue)
        session.flush()
        pages.append(issue)
    return one, two, pages[0], pages[1]


def test_revision_two_keeps_a_confirmation_only_on_an_identical_page_with_a_known_hash(
    session: Session,
) -> None:
    empty = hashlib.sha256(b"").hexdigest()
    one, two, old_pages, new_pages = _two_revision_pages(
        session,
        (
            ("1" * 64, "1" * 64),  # page 1: byte-identical on revision 2
            ("2" * 64, "3" * 64),  # page 2: changed on revision 2
            (empty, empty),  # page 3: content unknown on both
        ),
    )
    run_one = _reread(session, one.id)
    for page in old_pages:
        record = session.get(ObservationCandidate, _record_on(session, run_one, page, LISTED))
        assert record is not None
        listed = unchosen_page(page.index, record, page.content_hash)
        session.add(
            NoCountertopAnswer(
                package_revision_id=one.id,
                row_choice_candidate_id=record.id,
                page_id=page.id,
                page_number=page.index + 1,
                page_content_hash=page.content_hash,
                reading_answer=reading_answer(listed),
                answer="no_countertop",
                answered_by=REVIEWER,
            )
        )
    run_two = _reread(session, two.id)
    for page in new_pages:
        _record_on(session, run_two, page, LISTED)
    session.commit()

    results = _countertop_results_for_revision(session, two.package_id, two)
    confirmations = {
        page.page_number: page.confirmation for page in results.pages_without_countertop
    }
    assert set(confirmations) == {1, 2, 3}
    carried = confirmations[1]
    assert carried is not None
    assert (carried.from_revision, carried.confirmed_by) == (1, REVIEWER)
    assert confirmations[2] is None  # the page changed
    assert confirmations[3] is None  # an unknown page is never "the same page"
    assert pages_blocking_sign_off(session, [two.id])[two.id].unconfirmed == (2, 3)
    # Revision 1 shows its own answers, given there, with no "from revision".
    earlier = _countertop_results_for_revision(session, one.package_id, one)
    assert [
        (page.page_number, page.confirmation is not None and page.confirmation.from_revision)
        for page in earlier.pages_without_countertop
    ] == [(1, None), (2, None), (3, None)]


def test_an_answer_given_on_revision_two_never_reaches_revision_one(session: Session) -> None:
    one, two, old_pages, new_pages = _two_revision_pages(session, (("1" * 64, "1" * 64),))
    _record_on(session, _reread(session, one.id), old_pages[0], LISTED)
    record = session.get(
        ObservationCandidate, _record_on(session, _reread(session, two.id), new_pages[0], LISTED)
    )
    assert record is not None
    session.add(
        NoCountertopAnswer(
            package_revision_id=two.id,
            row_choice_candidate_id=record.id,
            page_id=new_pages[0].id,
            page_number=1,
            page_content_hash="1" * 64,
            reading_answer=reading_answer(unchosen_page(0, record, "1" * 64)),
            answer="no_countertop",
            answered_by=REVIEWER,
        )
    )
    session.commit()

    assert pages_blocking_sign_off(session, [one.id, two.id]) == {
        one.id: pages_blocking_sign_off(session, [one.id])[one.id],
        two.id: pages_blocking_sign_off(session, [two.id])[two.id],
    }
    assert pages_blocking_sign_off(session, [one.id])[one.id].unconfirmed == (1,)
    assert pages_blocking_sign_off(session, [two.id])[two.id].unconfirmed == ()


# --- who may answer, and when ---------------------------------------------------------------------


# --- frozen at sign-off and printed ---------------------------------------------------------------


# --- carried to revision 2 ------------------------------------------------------------------------


def _carried_to_revision_two(
    session: Session, answer: str
) -> tuple[PackageRevision, PackageRevision, UUID]:
    """Revision 1 answered page 1 (`answer`); revision 2's page 1 is byte-identical and was read to
    the same answer. Returns both revisions and revision 2's record."""
    one, two, old_pages, new_pages = _two_revision_pages(session, (("1" * 64, "1" * 64),))
    record = session.get(
        ObservationCandidate, _record_on(session, _reread(session, one.id), old_pages[0], LISTED)
    )
    assert record is not None
    session.add(
        NoCountertopAnswer(
            package_revision_id=one.id,
            row_choice_candidate_id=record.id,
            page_id=old_pages[0].id,
            page_number=1,
            page_content_hash="1" * 64,
            reading_answer=reading_answer(unchosen_page(0, record, "1" * 64)),
            answer=answer,
            answered_by=REVIEWER,
        )
    )
    new_record = _record_on(session, _reread(session, two.id), new_pages[0], LISTED)
    one.state = PackageState.SUPERSEDED.value
    session.commit()
    return one, two, new_record


def test_a_confirmation_carried_to_revision_two_needs_the_signers_still_applies_tick(
    session: Session,
) -> None:
    """Review fix (C5 reversed): like #1180's carried wall answers, a confirmation given on revision
    1's identical page is listed at sign-off, and the signer must say it still applies."""
    from app.review.approval import CarriedAnswersUnconfirmed
    from tests.api.test_revisions import _scoped_finding

    _one, two, _record = _carried_to_revision_two(session, "no_countertop")
    pages = pages_blocking_sign_off(session, [two.id])[two.id]
    assert pages.unconfirmed == ()
    assert pages.carried == ((1, f"Page 1: no countertop confirmed on revision 1 ({REVIEWER})"),)
    # A result to sign, as a checked revision 2 would have.
    _scoped_finding(session, two, _record)
    two.state = PackageState.AWAITING_REVIEW.value
    session.commit()
    review_id = _review_session(session, two.id)
    signer = Principal(
        id="synthetic-signer", roles=frozenset({Role.ADMIN}), projects=frozenset({two.package_id})
    )

    with pytest.raises(
        CarriedAnswersUnconfirmed, match=r"Page 1: no countertop confirmed on revision 1"
    ):
        approve_package(session, principal=signer, review_session_id=review_id)
    session.rollback()

    decision = approve_package(
        session, principal=signer, review_session_id=review_id, carried_answers_confirmed=True
    )
    session.commit()
    assert decision.approval.package_revision_id == two.id


def test_a_carried_there_is_a_countertop_needs_no_tick_and_gets_a_result_in_the_check_stage(
    session: Session, tmp_path: Path
) -> None:
    from tests.workflow.test_stages import _publish_rulebook
    from workflow.stages import DatabaseStages

    _publish_rulebook(session)
    _one, two, record_id = _carried_to_revision_two(session, "countertop")
    pages = pages_blocking_sign_off(session, [two.id])[two.id]
    assert (pages.unconfirmed, pages.unchecked, pages.carried) == ((), (1,), ())
    (item,) = _countertop_results_for_revision(session, two.package_id, two).items
    assert (
        item.hold is not None
        and "on revision 1 that this page has a countertop" in item.hold.reason
    )

    two.state = PackageState.NEEDS_INPUT.value
    session.commit()
    DatabaseStages(
        store=LocalStore(root=tmp_path / "store", ticket_secret=b"synthetic-test")
    ).run_checks(session, two.id)

    (finding,) = [f for f in _live(session, two.id) if f.scope_row_candidate_id == record_id]
    assert finding.outcome == "REVIEW_REQUIRED"
    assert pages_blocking_sign_off(session, [two.id])[two.id].unchecked == ()


def test_a_code_listed_page_s_answer_carries_to_revision_two_only_with_the_same_reason_code(
    session: Session,
) -> None:
    """#1193 review: a page code read no row on, answered on revision 1, keeps its answer on an
    identical page of revision 2 read for the same reason; a page whose reason changed is asked
    again."""
    from workflow.unread_pages import persist_unread_pages, unread_page

    one, two, old_pages, new_pages = _two_revision_pages(
        session, (("1" * 64, "1" * 64), ("4" * 64, "4" * 64))
    )
    run_one = _reread(session, one.id)
    persist_unread_pages(
        session,
        extraction_run_id=run_one,
        pages=[
            unread_page(page, ("no-vendor-drawing", "synthetic: no vendor drawing"))
            for page in old_pages
        ],
        dpi=150,
    )
    for record in session.scalars(
        select(ObservationCandidate).where(ObservationCandidate.extraction_run_id == run_one)
    ):
        page = session.get(Page, record.page_id)
        assert page is not None
        listed = unchosen_page(page.index, record, page.content_hash)
        session.add(
            NoCountertopAnswer(
                package_revision_id=one.id,
                row_choice_candidate_id=record.id,
                page_id=page.id,
                page_number=page.index + 1,
                page_content_hash=page.content_hash,
                reading_answer=reading_answer(listed),
                answer="no_countertop",
                answered_by=REVIEWER,
            )
        )
    run_two = _reread(session, two.id)
    persist_unread_pages(
        session,
        extraction_run_id=run_two,
        pages=[
            unread_page(new_pages[0], ("no-vendor-drawing", "synthetic: no vendor drawing")),
            unread_page(new_pages[1], ("no-row-found", "synthetic: no row found")),
        ],
        dpi=150,
    )
    session.commit()

    results = _countertop_results_for_revision(session, two.package_id, two)
    confirmations = {
        page.page_number: page.confirmation for page in results.pages_without_countertop
    }
    carried = confirmations[1]
    assert carried is not None and (carried.from_revision, carried.confirmed_by) == (1, REVIEWER)
    assert confirmations[2] is None  # same page, but code now reads it for another reason
    assert pages_blocking_sign_off(session, [two.id])[two.id].unconfirmed == (2,)
