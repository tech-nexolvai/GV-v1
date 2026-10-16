"""Pages not checked: one "Confirm these N pages" action, and blank pages need no click (#1195).

Anant (2026-10-12, "yes build the confirm-all button"): every page listed under "Pages not checked"
needed its own click before sign-off (#1191). Now:

1. a page code **proved** blank from the PDF (a title or divider page: nothing drawn) is still
   listed, with its proof, but never blocks sign-off; any doubt about the proof keeps the click;
2. one reviewer action confirms the pages the screen named, each recorded exactly like a single
   answer (who, when, the optional note), with one audit event per page plus one for the batch; a
   stale list is refused with 409, a page that is not on the list with 422; unnamed pages wait.

Synthetic values only.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.visual_countertops import (
    NO_COUNTERTOP_BATCH_TARGET,
    _countertop_results_for_revision,
)
from app.audit.events import AuditEvent
from app.auth import Principal, Role
from app.db.session import session_factory
from app.models import (
    DocumentVersion,
    NoCountertopAnswer,
    ObservationCandidate,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    SourceArtifact,
)
from app.models.document import Page
from app.review.approval import approval_readiness, approve_package
from app.review.no_countertop import pages_blocking_sign_off, reading_answer
from extraction.blank_page import BlankPageFacts
from reports.spreadsheet import confirmation_words
from tests.api.test_unchosen_row_pages import (
    SPLIT_PICKS,
    _results,
    _unchosen_record,
    code_no_row_record,
)
from tests.app.postgres_fixture import alembic_config
from tests.review.test_no_countertop_answers import (
    LISTED,
    REVIEWER,
    _answer,
    _client,
    _listed_page,
    _record_on,
    _reread,
    _review_session,
    _reviewer,
)
from workflow.slot_row_scope import unchosen_page
from workflow.unread_pages import BLANK_PAGE, UnreadPage, persist_unread_pages

pytest_plugins = ("tests.app.postgres_fixture",)

#: What code found on a GV-style title page: the page frame, a white text-box background, a title.
TITLE_PAGE = BlankPageFacts(frames=1, white_paint=1, typed_texts=2, typed_characters=24)
NOTE = "TEST ONLY synthetic note: section dividers checked by eye"


# --- helpers --------------------------------------------------------------------------------------


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


def _new_page(session: Session, revision_id: UUID, page_index: int) -> Page:
    version_id = session.scalars(
        select(PackageRevisionDocument.document_version_id).where(
            PackageRevisionDocument.package_revision_id == revision_id
        )
    ).one()
    page = Page(
        document_version_id=version_id,
        index=page_index,
        content_hash=f"{page_index + 500:064x}",
        width_pt=Decimal(612),
        height_pt=Decimal(792),
        rotation=0,
        has_vector_text=True,
        media_box=["0", "0", "612", "792"],
        crop_box=["0", "0", "612", "792"],
    )
    session.add(page)
    session.flush()
    return page


def _blank_record(session: Session, revision_id: UUID, *, page_index: int) -> UUID:
    """Code's record for a page it proved blank, written as the extraction stage writes it."""
    from workflow.slot_row_scope import latest_slot_reader_run

    run_id = latest_slot_reader_run(session, revision_id)
    assert run_id is not None
    page = _new_page(session, revision_id, page_index)
    code, reason = BLANK_PAGE
    persist_unread_pages(
        session,
        extraction_run_id=run_id,
        pages=[
            UnreadPage(
                page_id=page.id,
                page_index=page.index,
                document_version_id=page.document_version_id,
                width_pt=Decimal(612),
                height_pt=Decimal(792),
                code=code,
                reason=reason,
                blank_proof=TITLE_PAGE,
            )
        ],
        dpi=150,
    )
    return session.scalars(
        select(ObservationCandidate.id).where(ObservationCandidate.page_id == page.id)
    ).one()


def _confirm_url(project_id: UUID, package_id: UUID) -> str:
    return f"/api/v1/projects/{project_id}/packages/{package_id}/no-countertop-pages/confirm"


def _body(*pages: tuple[int, UUID], note: str | None = None) -> dict[str, object]:
    return {
        "pages": [
            {"page_number": number, "expected_record_id": str(record)} for number, record in pages
        ],
        **({} if note is None else {"note": note}),
    }


def _answers(session: Session) -> list[NoCountertopAnswer]:
    return list(
        session.scalars(
            select(NoCountertopAnswer).order_by(
                NoCountertopAnswer.page_number, NoCountertopAnswer.created_at
            )
        )
    )


def _three_listed(session: Session, tmp_path: Path) -> tuple[UUID, UUID, PackageRevision, dict]:
    """Page 3 (both AIs: no countertop), page 4 (code: no vendor drawing) and page 5 (both AIs: no
    countertop), on a review whose two countertops are checked and decided."""
    project_id, package_id, revision, three = _listed_page(session, tmp_path)
    four = code_no_row_record(session, revision.id, page_index=3)
    five = _unchosen_record(
        session, revision.id, page_index=4, flags=LISTED, reason="synthetic: tall units only"
    )
    session.commit()
    return project_id, package_id, revision, {3: three, 4: four, 5: five}


# --- blank pages need no click --------------------------------------------------------------------


def test_a_page_code_proved_blank_is_listed_with_its_proof_and_never_blocks_sign_off(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, revision, three = _listed_page(session, tmp_path)
    blank = _blank_record(session, revision.id, page_index=5)
    session.commit()

    listed = {
        page.page_number: page for page in _results(session, package_id).pages_without_countertop
    }
    assert set(listed) == {3, 6}
    divider = listed[6]
    assert divider.record_id == blank and divider.confirmation is None
    assert divider.reason.startswith("Title or divider page: nothing drawn.")
    assert divider.needs_answer is False and divider.doubtful is False
    assert divider.blank_page is not None
    assert divider.blank_page.model_dump() == {
        "rule": 1,
        "stamps": 0,
        "other_annotations": 0,
        "images": 0,
        "drawn_paths": 0,
        "frames": 1,
        "white_paint": 1,
        "typed_texts": 2,
        "typed_characters": 24,
    }
    assert listed[3].needs_answer is True and listed[3].blank_page is None
    # Readiness counts only the page that needs an answer.
    readiness = approval_readiness(session, revision.id)
    assert readiness.unconfirmed_no_countertop_pages == (3,)
    # Still listed for a person, but optional (owner decision 2026-10-16): it does not lock sign-off.
    assert readiness.reason is None

    _answer(session, project_id, package_id, three, "no_countertop")

    readiness = approval_readiness(session, revision.id)
    assert readiness.unconfirmed_no_countertop_pages == ()
    assert readiness.can_approve, readiness.reason
    # The signed files say why the blank page has no answer.
    (divider,) = [
        page
        for page in _results(session, package_id).pages_without_countertop
        if page.page_number == 6
    ]
    assert confirmation_words(divider) == (
        "No answer needed: page 6 has nothing drawn (checked by code)"
    )
    approve_package(
        session,
        principal=_reviewer(project_id),
        review_session_id=_review_session(session, revision.id),
    )
    session.commit()


def test_there_is_a_countertop_on_a_blank_page_still_makes_it_blocking(
    session: Session, tmp_path: Path
) -> None:
    """The proof removes a click, never a reviewer's say: "There is a countertop" still counts."""
    project_id, package_id, revision, _three = _listed_page(session, tmp_path)
    blank = _blank_record(session, revision.id, page_index=5)
    session.commit()

    _answer(session, project_id, package_id, blank, "countertop")

    assert pages_blocking_sign_off(session, [revision.id])[revision.id].unchecked == (6,)


@pytest.mark.parametrize(
    "proof",
    [
        # One fact missing.
        ("rule=1", "stamps=0", "other-annotations=0", "images=0", "drawn-paths=0", "frames=1"),
        # Something drawn, a picture, a pasted drawing.
        tuple(
            f"{name}={value}"
            for name, value in {
                "rule": 1,
                "stamps": 0,
                "other-annotations": 0,
                "images": 0,
                "drawn-paths": 1,
                "frames": 1,
                "white-paint": 0,
                "typed-texts": 1,
                "typed-characters": 10,
            }.items()
        ),
        tuple(
            f"{name}={value}"
            for name, value in {
                "rule": 1,
                "stamps": 0,
                "other-annotations": 0,
                "images": 1,
                "drawn-paths": 0,
                "frames": 1,
                "white-paint": 0,
                "typed-texts": 1,
                "typed-characters": 10,
            }.items()
        ),
        tuple(
            f"{name}={value}"
            for name, value in {
                "rule": 1,
                "stamps": 1,
                "other-annotations": 0,
                "images": 0,
                "drawn-paths": 0,
                "frames": 1,
                "white-paint": 0,
                "typed-texts": 1,
                "typed-characters": 10,
            }.items()
        ),
        # Another rule's proof, a page of notes, a malformed fact.
        tuple(
            f"{name}={value}"
            for name, value in {
                "rule": 2,
                "stamps": 0,
                "other-annotations": 0,
                "images": 0,
                "drawn-paths": 0,
                "frames": 1,
                "white-paint": 0,
                "typed-texts": 1,
                "typed-characters": 10,
            }.items()
        ),
        tuple(
            f"{name}={value}"
            for name, value in {
                "rule": 1,
                "stamps": 0,
                "other-annotations": 0,
                "images": 0,
                "drawn-paths": 0,
                "frames": 1,
                "white-paint": 0,
                "typed-texts": 40,
                "typed-characters": 4000,
            }.items()
        ),
        (
            "rule=1",
            "stamps=0",
            "other-annotations=0",
            "images=0",
            "drawn-paths=none",
            "frames=1",
            "white-paint=0",
            "typed-texts=1",
            "typed-characters=10",
        ),
    ],
    ids=["missing-fact", "ink", "picture", "stamp", "other-rule", "page-of-notes", "malformed"],
)
def test_a_blank_proof_that_is_not_complete_and_clean_still_needs_an_answer(
    session: Session, tmp_path: Path, proof: tuple[str, ...]
) -> None:
    _project_id, package_id, revision, _three = _listed_page(session, tmp_path)
    record = _unchosen_record(
        session,
        revision.id,
        page_index=5,
        flags=("no-row:blank-page", *(f"blank-proof:{fact}" for fact in proof)),
        reason=BLANK_PAGE[1],
    )
    session.commit()

    (page,) = [
        p for p in _results(session, package_id).pages_without_countertop if p.record_id == record
    ]
    assert page.needs_answer is True and page.blank_page is None
    assert approval_readiness(session, revision.id).unconfirmed_no_countertop_pages == (3, 6)


# --- "Confirm these N pages" ----------------------------------------------------------------------


def test_confirm_these_pages_records_each_like_a_single_answer_and_leaves_unticked_pages_waiting(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, revision, records = _three_listed(session, tmp_path)
    client = _client(session, _reviewer(project_id), tmp_path)

    # Page 5 is unticked: it is not named and keeps waiting.
    response = client.post(
        _confirm_url(project_id, package_id),
        json=_body((3, records[3]), (4, records[4]), note=f"  {NOTE}  "),
    )

    assert response.status_code == 201, response.text
    stored = _answers(session)
    assert [(a.page_number, a.answer, a.answered_by, a.note) for a in stored] == [
        (3, "no_countertop", REVIEWER, NOTE),
        (4, "no_countertop", REVIEWER, NOTE),
    ]
    assert [a.row_choice_candidate_id for a in stored] == [records[3], records[4]]
    batch = {answer.batch_id for answer in stored}
    assert len(batch) == 1 and None not in batch
    # The audit trail: one event per page, plus one for the batch.
    events = session.scalars(select(AuditEvent).order_by(AuditEvent.created_at)).all()
    per_page = [e for e in events if e.target_type == "no_countertop_answer"]
    batches = [e for e in events if e.target_type == NO_COUNTERTOP_BATCH_TARGET]
    assert {e.target_id for e in per_page} == {a.id for a in stored}
    assert [(e.target_id, e.actor, e.category) for e in batches] == [
        (stored[0].batch_id, REVIEWER, "REVIEW_ACTION")
    ]
    # The screen gets the updated list: each page shows who and the note, page 5 still waits.
    listed = {page["page_number"]: page for page in response.json()["pages_without_countertop"]}
    assert listed[3]["confirmation"]["confirmed_by"] == REVIEWER
    assert listed[3]["confirmation"]["note"] == NOTE
    assert listed[5]["confirmation"] is None
    readiness = approval_readiness(session, revision.id)
    assert readiness.unconfirmed_no_countertop_pages == (5,)

    # A single answer finishes it; confirmations need no new check run (the re-run rule holds).
    _answer(session, project_id, package_id, records[5], "no_countertop")
    assert approval_readiness(session, revision.id).can_approve


def test_a_page_already_confirmed_keeps_its_first_answer(session: Session, tmp_path: Path) -> None:
    project_id, package_id, _revision, records = _three_listed(session, tmp_path)
    _answer(session, project_id, package_id, records[3], "no_countertop")
    client = _client(session, _reviewer(project_id), tmp_path)

    again = client.post(_confirm_url(project_id, package_id), json=_body((3, records[3])))
    mixed = client.post(
        _confirm_url(project_id, package_id), json=_body((3, records[3]), (5, records[5]))
    )

    assert (again.status_code, mixed.status_code) == (200, 201)
    stored = _answers(session)
    assert [(a.page_number, a.batch_id is None) for a in stored] == [(3, True), (5, False)]
    assert (
        session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.target_type == NO_COUNTERTOP_BATCH_TARGET)
        )
        == 1
    )


def test_a_list_that_changed_since_the_screen_loaded_it_is_refused_and_nothing_is_saved(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, revision, records = _three_listed(session, tmp_path)
    client = _client(session, _reviewer(project_id), tmp_path)

    # Somebody said page 4 has a countertop after the screen loaded: it is no longer listed.
    _answer(session, project_id, package_id, records[4], "countertop")
    refused = client.post(
        _confirm_url(project_id, package_id), json=_body((3, records[3]), (4, records[4]))
    )

    assert refused.status_code == 409
    assert refused.json()["message"] == (
        "The list of pages not checked changed since you opened it (page 4: read again or "
        "already answered). Nothing was saved. Reload the results and confirm again."
    )
    assert [a.page_number for a in _answers(session)] == [4]  # only the "countertop" answer

    # The pages were read again: the records the screen showed are no longer the list's.
    run_id = _reread(session, revision.id)
    for number in (3, 5):
        page = session.get(
            Page,
            session.get(ObservationCandidate, records[number]).page_id,  # type: ignore[union-attr]
        )
        assert page is not None
        _record_on(session, run_id, page, LISTED)
    session.commit()
    stale = client.post(
        _confirm_url(project_id, package_id), json=_body((3, records[3]), (5, records[5]))
    )

    assert stale.status_code == 409
    assert "(pages 3 and 5: read again or already answered)" in stale.json()["message"]
    assert [a.page_number for a in _answers(session)] == [4]


def test_a_page_that_is_not_on_the_list_is_refused_with_422(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, revision, records = _three_listed(session, tmp_path)
    split = _unchosen_record(
        session, revision.id, page_index=6, flags=SPLIT_PICKS, reason="synthetic: split"
    )
    session.commit()
    client = _client(session, _reviewer(project_id), tmp_path)
    url = _confirm_url(project_id, package_id)

    unknown = client.post(url, json=_body((3, records[3]), (9, uuid4())))
    wrong_page = client.post(url, json=_body((4, records[3])))
    disagreement = client.post(url, json=_body((7, split)))
    twice = client.post(url, json=_body((3, records[3]), (3, records[3])))
    empty = client.post(url, json={"pages": []})
    long_note = client.post(url, json=_body((3, records[3]), note="n" * 501))
    other_answer = client.post(
        url,
        json={**_body((3, records[3])), "answer": "countertop"},  # only confirmations
    )

    assert unknown.status_code == 422
    assert unknown.json()["message"] == (
        "Page 9 is not on the list of pages not checked. Only pages listed there can be confirmed."
    )
    assert (
        wrong_page.status_code == 422
        and "Page 4 is not on the list" in wrong_page.json()["message"]
    )
    assert disagreement.status_code == 422
    assert [r.status_code for r in (twice, empty, long_note, other_answer)] == [422] * 4
    assert _answers(session) == []


@pytest.mark.parametrize(
    "roles",
    [frozenset({Role.RULE_ADMIN}), frozenset()],
    ids=["rule-admin", "no-role"],
)
def test_confirm_these_pages_needs_the_same_permission_as_a_single_answer(
    session: Session, tmp_path: Path, roles: frozenset[Role]
) -> None:
    project_id, package_id, _revision, records = _three_listed(session, tmp_path)
    body = _body((3, records[3]))

    refused = _client(session, _reviewer(project_id, roles=roles), tmp_path).post(
        _confirm_url(project_id, package_id), json=body
    )
    outsider = Principal(
        id="synthetic-outsider", roles=frozenset({Role.REVIEWER}), projects=frozenset()
    )
    elsewhere = _client(session, outsider, tmp_path).post(
        _confirm_url(project_id, package_id), json=body
    )
    accepted = _client(session, _reviewer(project_id), tmp_path).post(
        _confirm_url(project_id, package_id), json=body
    )

    # A refused action answers like an absence (`app/auth/dependencies._refuse`): 404, never 403.
    assert (refused.status_code, elsewhere.status_code, accepted.status_code) == (404, 404, 201)
    assert [a.answered_by for a in _answers(session)] == [REVIEWER]


def test_confirm_these_pages_is_refused_after_sign_off(session: Session, tmp_path: Path) -> None:
    project_id, package_id, revision, records = _listed_page(session, tmp_path)
    _answer(session, project_id, package_id, records, "no_countertop")
    approve_package(
        session,
        principal=_reviewer(project_id),
        review_session_id=_review_session(session, revision.id),
    )
    session.commit()

    refused = _client(session, _reviewer(project_id), tmp_path).post(
        _confirm_url(project_id, package_id), json=_body((3, records))
    )

    assert refused.status_code == 409
    assert refused.json()["message"] == (
        "This review is signed off. Start a new revision to change it."
    )


def test_a_single_answer_keeps_its_optional_note(session: Session, tmp_path: Path) -> None:
    project_id, package_id, _revision, record = _listed_page(session, tmp_path)

    response = _client(session, _reviewer(project_id), tmp_path).post(
        f"/api/v1/projects/{project_id}/packages/{package_id}/no-countertop-pages/{record}/answer",
        json={"answer": "no_countertop", "note": NOTE},
    )

    assert response.status_code == 201
    (stored,) = _answers(session)
    assert (stored.note, stored.batch_id) == (NOTE, None)
    (page,) = response.json()["pages_without_countertop"]
    assert page["confirmation"]["note"] == NOTE


# --- carried to the next revision like a single answer --------------------------------------------


def _revision_two_with_the_same_page(
    session: Session, one: PackageRevision, record_id: UUID
) -> tuple[PackageRevision, UUID]:
    """Revision 2 of the package, whose drawing holds a byte-identical copy of the record's page,
    read to the same answer."""
    record = session.get(ObservationCandidate, record_id)
    assert record is not None
    old_page = session.get(Page, record.page_id)
    old_version = session.get(DocumentVersion, old_page.document_version_id)  # type: ignore[union-attr]
    assert old_page is not None and old_version is not None
    two = PackageRevision(
        package_id=one.package_id, revision_number=2, state="UPLOADED", supersedes_id=one.id
    )
    session.add(two)
    session.flush()
    digest = hashlib.sha256(b"synthetic issue 2").hexdigest()
    artifact = SourceArtifact(storage_key=f"synthetic/{uuid4()}", sha256=digest, size=1)
    session.add(artifact)
    session.flush()
    version = DocumentVersion(
        document_id=old_version.document_id,
        source_artifact_id=artifact.id,
        sha256=digest,
        page_count=old_page.index + 1,
    )
    session.add(version)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=two.id,
            package_id=one.package_id,
            document_id=old_version.document_id,
            document_version_id=version.id,
        )
    )
    page = Page(
        document_version_id=version.id,
        index=old_page.index,
        content_hash=old_page.content_hash,
        width_pt=Decimal(612),
        height_pt=Decimal(792),
        rotation=0,
        has_vector_text=True,
    )
    session.add(page)
    session.flush()
    new_record = _record_on(session, _reread(session, two.id), page, LISTED)
    one.state = PackageState.SUPERSEDED.value
    session.commit()
    return two, new_record


def test_a_confirmation_from_the_batch_carries_to_revision_two_like_a_single_answer(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, one, record = _listed_page(session, tmp_path)
    confirmed = _client(session, _reviewer(project_id), tmp_path).post(
        _confirm_url(project_id, package_id), json=_body((3, record))
    )
    assert confirmed.status_code == 201

    two, _new_record = _revision_two_with_the_same_page(session, one, record)

    pages = pages_blocking_sign_off(session, [two.id])[two.id]
    assert pages.unconfirmed == ()
    # Like a single answer carried over: the signer says it still applies.
    assert pages.carried == ((1, f"Page 3: no countertop confirmed on revision 1 ({REVIEWER})"),)
    (listed,) = _countertop_results_for_revision(session, package_id, two).pages_without_countertop
    assert listed.confirmation is not None and listed.confirmation.from_revision == 1


# --- review fixes (#1195 review) ------------------------------------------------------------------

#: A complete, clean proof under rule 1, as the extraction stage stores it.
PROOF_FLAGS = (
    "no-row:blank-page",
    "blank-proof:rule=1",
    "blank-proof:stamps=0",
    "blank-proof:other-annotations=0",
    "blank-proof:images=0",
    "blank-proof:drawn-paths=0",
    "blank-proof:frames=1",
    "blank-proof:white-paint=1",
    "blank-proof:typed-texts=2",
    "blank-proof:typed-characters=24",
)


def _record(*flags: str, reason: str = "synthetic reason") -> ObservationCandidate:
    """A row-choice record, never stored: enough for the page's own rules."""
    return ObservationCandidate(
        ambiguity_flags=["slot-reader-row-choice", *flags], review_reason=reason, raw_text=""
    )


@pytest.mark.parametrize(
    ("flags", "doubtful"),
    [
        (("no-row:no-row-found",), True),  # a vendor drawing code found no row on
        (("no-row:unreadable",), True),  # a page code could not read
        (("row-choice:0", "row-pick:opus-5-5:0", "row-pick:sonnet-5-5:0"), True),  # older
        (("no-row:no-drawing", "no-row-ink-found"), True),  # no pasted drawing, but ink
        (("no-row:no-vendor-drawing", "no-row-unnamed-drawing"), True),  # an unknown label
        (("no-row:no-drawing",), False),
        (("no-row:no-vendor-drawing",), False),  # only the architect's or a brand prototype
        (("no-row:reference-only",), False),
        (
            (
                "row-choice:0",
                "row-pick:opus-5-5:0",
                "row-pick:sonnet-5-5:0",
                "row-kind:opus-5-5:no_countertop",
                "row-kind:sonnet-5-5:no_countertop",
            ),
            False,
        ),  # both AIs: no countertop on the sheet
    ],
    ids=[
        "no-row-found",
        "unreadable",
        "older-reading",
        "ink-found",
        "unnamed-drawing",
        "no-drawing",
        "no-vendor-drawing",
        "reference-only",
        "ais-no-countertop",
    ],
)
def test_a_page_with_a_reason_to_doubt_starts_unticked_and_still_needs_an_answer(
    flags: tuple[str, ...], doubtful: bool
) -> None:
    """The tick "Confirm these N pages" starts with is `not doubtful` (the screen's rule)."""
    page = unchosen_page(4, _record(*flags))

    assert page.listed
    assert page.doubtful is doubtful
    assert page.needs_answer


@pytest.mark.parametrize(
    "doubt",
    [("no-row-unnamed-drawing",), ("no-row-ink-found",)],
    ids=["unnamed-drawing", "ink-found"],
)
def test_doubt_wins_over_a_blank_proof(doubt: tuple[str, ...]) -> None:
    proven = unchosen_page(4, _record(*PROOF_FLAGS))
    doubted = unchosen_page(4, _record(*PROOF_FLAGS, *doubt))
    older = replace(proven, kind="older")

    assert (proven.needs_answer, proven.doubtful) == (False, False)
    assert (doubted.needs_answer, doubted.doubtful) == (True, True)
    assert (older.needs_answer, older.doubtful) == (True, True)


def test_a_closed_revision_keeps_a_proof_made_under_an_earlier_rule(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the blank-page rule changes, an open revision asks again; a signed one keeps the proof
    it was signed with, so its words never change."""
    from workflow import slot_row_scope

    project_id, package_id, revision, three = _listed_page(session, tmp_path)
    blank = _blank_record(session, revision.id, page_index=5)
    session.commit()
    _answer(session, project_id, package_id, three, "no_countertop")
    approve_package(
        session,
        principal=_reviewer(project_id),
        review_session_id=_review_session(session, revision.id),
    )
    session.commit()

    # A newer rule (2) replaces rule 1.
    monkeypatch.setattr(slot_row_scope, "BLANK_PAGE_RULE", 2)
    monkeypatch.setitem(
        slot_row_scope.BLANK_PROOF_RULES, 2, dict(slot_row_scope.BLANK_PROOF_RULES[1])
    )

    record = session.get(ObservationCandidate, blank)
    assert record is not None
    assert unchosen_page(5, record).needs_answer  # an open revision re-asks under rule 2
    assert unchosen_page(5, record, closed=True).needs_answer is False
    session.refresh(revision)
    assert revision.state == PackageState.APPROVED.value
    signed = _countertop_results_for_revision(session, package_id, revision)
    (divider,) = [page for page in signed.pages_without_countertop if page.page_number == 6]
    assert divider.needs_answer is False and divider.blank_page is not None
    assert divider.blank_page.rule == 1
    assert pages_blocking_sign_off(session, [revision.id])[revision.id].unconfirmed == ()


def test_the_signed_files_say_no_answer_needed_once(session: Session, tmp_path: Path) -> None:
    from pypdf import PdfReader

    from reports.findings_pdf import FindingsPdfInput, write_findings_pdf
    from reports.spreadsheet import StoredFinding

    _project_id, package_id, revision, _three = _listed_page(session, tmp_path)
    _blank_record(session, revision.id, page_index=5)
    session.commit()
    listed = _results(session, package_id).pages_without_countertop
    finding = StoredFinding(
        rule_id="CT-WIDTH-001",
        outcome="PASS",
        severity="FLAG",
        snapshot_id="synthetic-snapshot",
        engine_version="0.1.0",
        trace={},
        reason="Synthetic.",
    )

    pdf = write_findings_pdf(
        FindingsPdfInput(
            package_revision_id=revision.id,
            revision_number=1,
            vendor="Synthetic vendor",
            findings=(finding,),
            pages_without_countertop=listed,
        )
    )

    text = " ".join(
        " ".join((sheet.extract_text() or "").split()) for sheet in PdfReader(io.BytesIO(pdf)).pages
    )
    assert "Title or divider page: nothing drawn." in text
    assert text.count("No answer needed") == 1


def test_a_bulk_confirm_waits_for_a_single_answer_in_flight_and_is_then_refused(
    session: Session, postgres_engine: Engine, tmp_path: Path
) -> None:
    """The bulk action takes the same per-revision lock as a single answer: sent while a "There is
    a countertop" on the same page is being written, it waits for it, then finds the page no longer
    listed (409) and records nothing."""
    import threading
    import time

    from fastapi import HTTPException, Response

    from app.api.visual_countertops import (
        NoCountertopPagesIn,
        _answer_lock_key,
        confirm_no_countertop_pages,
    )

    project_id, package_id, revision, records = _three_listed(session, tmp_path)
    first = session_factory(postgres_engine)()
    second = session_factory(postgres_engine)()
    outcome: list[object] = []

    def bulk() -> None:
        principal = _reviewer(project_id)
        try:
            outcome.append(
                confirm_no_countertop_pages(
                    principal,
                    principal,
                    second,
                    project_id,
                    package_id,
                    NoCountertopPagesIn.model_validate(
                        _body((3, records[3]), (5, records[5]))  # type: ignore[arg-type]
                    ),
                    Response(),
                )
            )
        except HTTPException as error:
            outcome.append(error)
        finally:
            second.rollback()

    try:
        # A single answer in flight: it holds the lock and has written, not committed yet.
        first.execute(select(func.pg_advisory_xact_lock(_answer_lock_key(revision.id))))
        record = first.get(ObservationCandidate, records[3])
        assert record is not None
        page = first.get(Page, record.page_id)
        assert page is not None
        first.add(
            NoCountertopAnswer(
                package_revision_id=revision.id,
                row_choice_candidate_id=record.id,
                page_id=record.page_id,
                page_number=3,
                page_content_hash=page.content_hash or "",
                reading_answer=reading_answer(unchosen_page(2, record, page.content_hash)),
                answer="countertop",
                answered_by="synthetic-first",
            )
        )
        first.flush()
        racer = threading.Thread(target=bulk)
        racer.start()
        time.sleep(0.5)
        assert racer.is_alive(), "the bulk action must wait for the lock"
        first.commit()
        racer.join(timeout=30)
    finally:
        first.close()
    second.close()

    (result,) = outcome
    assert isinstance(result, HTTPException) and result.status_code == 409
    assert [(a.page_number, a.answer) for a in _answers(session)] == [(3, "countertop")]
