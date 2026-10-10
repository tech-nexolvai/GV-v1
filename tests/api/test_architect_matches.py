"""The reviewer's one-click choice of the architect's view for a countertop row (#1166).

Verification for `GET`/`POST .../slot-rows/{row_id}/architect-view-match` and
`GET .../architect-views/{view_id}/picture` in `app/api/architect_matches.py`. Rows come from
`tests/api/test_slot_rows.py`'s synthetic package; the architect's indexed views and the automatic
match are added here. Invented values only.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.architect_matches import (
    API_PREFIX,
    architect_view_picture,
    get_architect_view_match,
    pick_architect_view,
    router,
)
from app.audit.events import AuditEvent
from app.auth.roles import Action, Principal, Role
from app.db.session import session_factory
from app.models import ObservationCandidate, PackageRevision, Project
from app.models.evidence import ArchitectViewIndexEntry, ArchitectViewMatchRecord
from app.models.runs import ExtractionRun
from app.schemas.architect_matches import ArchitectViewPickIn
from extraction.architect.view_matching import CodeMatch, CodeVerdict
from storage.local import LocalStore
from tests.api.test_slot_rows import _package_rows
from tests.app.postgres_fixture import alembic_config
from workflow.architect_match_contract import RowMatch
from workflow.architect_match_records import persist_architect_matches
from workflow.architect_reader import ARCHITECT_EXTRACTOR, ARCHITECT_EXTRACTOR_VERSION

pytest_plugins = ("tests.app.postgres_fixture",)

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic view picture"


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


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _principal(project_id: UUID) -> Principal:
    return Principal(
        id="reviewer (synthetic test)",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({project_id}),
    )


class Row:
    """A synthetic countertop row, two indexed architect views (the second not clearly apart, the
    first with a stored picture), and one automatic match that needs the reviewer."""

    def __init__(self, session: Session, store: LocalStore, *, matched: bool = True) -> None:
        self.project_id, self.package_id, anchors = _package_rows(session, piece_count=3)
        self.anchor = session.get_one(ObservationCandidate, anchors[0])
        slot_run = session.get_one(ExtractionRun, self.anchor.extraction_run_id)
        self.revision_id = session.scalars(
            select(PackageRevision.id).where(PackageRevision.package_id == self.package_id)
        ).one()
        architect_run = ExtractionRun(
            task_run_id=slot_run.task_run_id,
            extractor=ARCHITECT_EXTRACTOR,
            extractor_version=ARCHITECT_EXTRACTOR_VERSION,
            config_hash="test",
            dpi=150,
        )
        session.add(architect_run)
        session.flush()
        key = f"architect-views/{self.anchor.document_version_id}/pages/0/view-1.png"
        store.put(key, io.BytesIO(PNG), content_type="image/png")

        def view(number: int, *, separated: bool, picture: bool) -> ArchitectViewIndexEntry:
            entry = ArchitectViewIndexEntry(
                extraction_run_id=architect_run.id,
                document_version_id=self.anchor.document_version_id,
                page_id=self.anchor.page_id,
                view_number=number,
                view_tag=f"view-{number}",
                sheet_number="Z-101",
                bubble=f"{number} QX 1.1",
                title="SYNTHETIC ELEVATION",
                scale_note='1/4" = 1\'-0"',
                points_per_inch="1.5",
                extent={
                    "x0": "0",
                    "top": "0",
                    "x1": "10",
                    "bottom": "10",
                    "stored_points": [["0", "0"], ["10", "0"], ["10", "10"], ["0", "10"]],
                },
                separated=separated,
                role_confirmed=separated,
                row_count=1,
                reason="test",
                picture_sha256=hashlib.sha256(PNG).hexdigest() if picture else None,
                picture_storage_key=key if picture else None,
            )
            session.add(entry)
            session.flush()
            return entry

        self.view = view(1, separated=True, picture=True)
        self.crowded = view(2, separated=False, picture=False)
        self.stranger = view(3, separated=True, picture=False)
        if matched:
            persist_architect_matches(
                session,
                package_revision_id=self.revision_id,
                extraction_run_id=slot_run.id,
                results=[
                    SimpleNamespace(
                        page_id=self.anchor.page_id,
                        owner_candidate_ids={"slot:0": self.anchor.id},
                        architect_match=RowMatch(
                            status="needs_reviewer",
                            source="automatic",
                            chosen=None,
                            code=CodeMatch(CodeVerdict.GEOMETRY_TIE, None, (), ("two fit",)),
                            ai_picks=(),
                            candidate_json=(
                                {
                                    "view_id": str(self.crowded.id),
                                    "rank": 2,
                                    "shown_number": 2,
                                    "score": {"fits": True, "run_length_error_display": "0.4"},
                                    "evidence": ["its run is 0.4 in off the vendor's"],
                                    "remembered": True,
                                    "ai_picked_by": [],
                                },
                                {
                                    "view_id": str(self.view.id),
                                    "rank": 1,
                                    "shown_number": 1,
                                    "score": {
                                        "fits": True,
                                        "run_length_error_display": "0.0",
                                        "bays_vendor": 3,
                                        "bays_architect": 3,
                                    },
                                    "evidence": ["its run is 0.0 in off the vendor's"],
                                    "remembered": False,
                                    "ai_picked_by": ["anthropic.claude-opus-5-5"],
                                },
                            ),
                            reasons=("Two views fit.",),
                            question_packet=None,
                            details={"vendor_item_key": {}, "vendor_references": ["3/Q101"]},
                        ),
                    )
                ],
            )
        session.commit()
        self.principal = _principal(self.project_id)

    def current_id(self, session: Session) -> UUID:
        shown = get_architect_view_match(
            self.principal, session, self.project_id, self.package_id, self.anchor.id
        )
        assert shown.current is not None
        return shown.current.record_id

    def post(self, session: Session, body: ArchitectViewPickIn, *, project: UUID | None = None):  # type: ignore[no-untyped-def]
        return pick_architect_view(
            self.principal,
            self.principal,
            session,
            project or self.project_id,
            self.package_id,
            self.anchor.id,
            body,
        )


def _records(session: Session) -> list[ArchitectViewMatchRecord]:
    return list(
        session.scalars(
            select(ArchitectViewMatchRecord).order_by(ArchitectViewMatchRecord.created_at)
        )
    )


def test_get_lists_every_candidate_ranked_with_nothing_chosen(
    session: Session, store: LocalStore
) -> None:
    row = Row(session, store)

    shown = get_architect_view_match(
        row.principal, session, row.project_id, row.package_id, row.anchor.id
    )

    assert shown.current is not None and shown.current.status == "needs_reviewer"
    assert shown.current.reasons == ["Two views fit."]
    assert [candidate.view.view_id for candidate in shown.candidates] == [
        row.view.id,
        row.crowded.id,
    ]
    first, second = shown.candidates
    assert first.rank == 1 and first.shown_to_ais and first.can_pick
    assert first.code.fits and first.code.run_length_error_display == "0.0"
    assert first.ai_picked_by == ["anthropic.claude-opus-5-5"]
    assert "3 bays against the vendor's 3" in first.score_summary
    assert first.view.label == "Page 1, view 1: SYNTHETIC ELEVATION (sheet Z-101)"
    assert first.view.picture_url == (
        f"{API_PREFIX}/projects/{row.project_id}/packages/{row.package_id}"
        f"/architect-views/{row.view.id}/picture"
    )
    assert first.view.region is not None and len(first.view.region.polygon) == 4
    assert second.remembered and not second.view.separated and second.view.picture_url is None
    assert shown.can_choose_none and shown.vendor.references == ["3/Q101"]


def test_a_row_never_matched_shows_no_record_and_no_candidates(
    session: Session, store: LocalStore
) -> None:
    row = Row(session, store, matched=False)

    shown = get_architect_view_match(
        row.principal, session, row.project_id, row.package_id, row.anchor.id
    )

    assert shown.current is None and shown.candidates == []


def test_post_records_the_pick_audits_it_and_answers_201(
    session: Session, store: LocalStore
) -> None:
    row = Row(session, store)

    shown = row.post(
        session,
        ArchitectViewPickIn(view_id=row.view.id, expected_record_id=row.current_id(session)),
    )

    assert shown.current is not None
    assert (shown.current.status, shown.current.source, shown.current.decided_by) == (
        "reviewer_confirmed",
        "reviewer",
        row.principal.id,
    )
    automatic, picked = _records(session)
    assert picked.supersedes_id == automatic.id and picked.matched_view_id == row.view.id
    (audit,) = session.scalars(
        select(AuditEvent).where(AuditEvent.target_type == "architect_view_match")
    ).all()
    assert audit.target_id == picked.id
    route = next(r for r in router.routes if isinstance(r, APIRoute) and "POST" in r.methods)
    assert route.status_code == 201


def test_none_of_these_and_a_crowded_view_are_recorded_as_such(
    session: Session, store: LocalStore
) -> None:
    row = Row(session, store)

    row.post(
        session, ArchitectViewPickIn(none_of_these=True, expected_record_id=row.current_id(session))
    )
    assert _records(session)[-1].status == "none_matches"
    row.post(
        session,
        ArchitectViewPickIn(view_id=row.crowded.id, expected_record_id=row.current_id(session)),
    )
    assert _records(session)[-1].status == "not_separated"


def test_a_pick_on_a_record_that_moved_on_is_409_and_saves_nothing(
    session: Session, store: LocalStore
) -> None:
    row = Row(session, store)
    first = row.current_id(session)
    row.post(session, ArchitectViewPickIn(view_id=row.view.id, expected_record_id=first))

    with pytest.raises(HTTPException) as stale:
        row.post(session, ArchitectViewPickIn(none_of_these=True, expected_record_id=first))

    assert stale.value.status_code == 409
    assert len(_records(session)) == 2


def test_a_view_not_offered_for_the_row_is_422(session: Session, store: LocalStore) -> None:
    row = Row(session, store)

    with pytest.raises(HTTPException) as refused:
        row.post(
            session,
            ArchitectViewPickIn(
                view_id=row.stranger.id, expected_record_id=row.current_id(session)
            ),
        )

    assert refused.value.status_code == 422
    assert len(_records(session)) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"expected_record_id": str(uuid4())},
        {"view_id": str(uuid4()), "none_of_these": True, "expected_record_id": str(uuid4())},
        {"view_id": str(uuid4())},
        {"none_of_these": True, "expected_record_id": str(uuid4()), "note": "x" * 501},
        {"none_of_these": True, "expected_record_id": str(uuid4()), "extra": 1},
    ],
    ids=["neither", "both", "no-expected-record", "long-note", "unknown-field"],
)
def test_a_malformed_body_is_refused_before_anything_is_read(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ArchitectViewPickIn.model_validate(body)


def test_another_projects_package_is_404(session: Session, store: LocalStore) -> None:
    row = Row(session, store)
    stranger = Project(name="another project")
    session.add(stranger)
    session.commit()

    with pytest.raises(HTTPException) as hidden:
        get_architect_view_match(row.principal, session, stranger.id, row.package_id, row.anchor.id)
    assert hidden.value.status_code == 404
    with pytest.raises(HTTPException) as hidden_post:
        row.post(
            session,
            ArchitectViewPickIn(none_of_these=True, expected_record_id=uuid4()),
            project=stranger.id,
        )
    assert hidden_post.value.status_code == 404
    assert len(_records(session)) == 1


def test_picking_needs_the_confirm_evidence_action_and_reading_only_access() -> None:
    def actions(path_end: str, method: str) -> set[Action]:
        route = next(
            route
            for route in router.routes
            if isinstance(route, APIRoute)
            and route.path.endswith(path_end)
            and method in (route.methods or set())
        )
        return {
            cell.cell_contents
            for dependency in route.dependant.dependencies
            for cell in (getattr(dependency.call, "__closure__", None) or ())
            if isinstance(cell.cell_contents, Action)
        }

    assert actions("/architect-view-match", "POST") == {Action.CONFIRM_EVIDENCE}
    assert actions("/architect-view-match", "GET") == set()
    assert actions("/picture", "GET") == set()


def test_the_picture_is_served_only_while_its_digest_holds(
    session: Session, store: LocalStore
) -> None:
    row = Row(session, store)

    served = architect_view_picture(
        row.principal, session, store, row.project_id, row.package_id, row.view.id
    )
    assert served.body == PNG and served.media_type == "image/png"
    for view_id in (row.crowded.id, uuid4()):
        with pytest.raises(HTTPException) as missing:
            architect_view_picture(
                row.principal, session, store, row.project_id, row.package_id, view_id
            )
        assert missing.value.status_code == 404
    wrong = ArchitectViewIndexEntry(
        extraction_run_id=row.view.extraction_run_id,
        document_version_id=row.view.document_version_id,
        page_id=row.view.page_id,
        view_number=4,
        view_tag="view-4",
        extent={},
        separated=True,
        role_confirmed=True,
        row_count=0,
        reason="a picture whose recorded digest is not its bytes",
        picture_sha256="0" * 64,
        picture_storage_key=row.view.picture_storage_key,
    )
    session.add(wrong)
    session.commit()
    with pytest.raises(HTTPException) as tampered:
        architect_view_picture(
            row.principal, session, store, row.project_id, row.package_id, wrong.id
        )
    assert tampered.value.status_code == 409


def test_the_picture_links_use_the_mounted_prefix() -> None:
    from app.main import API_PREFIX as MOUNTED

    assert API_PREFIX == MOUNTED
