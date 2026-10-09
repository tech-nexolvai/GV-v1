"""The reviewer's one-click architect pairing on a countertop row (#1053).

Verification for `GET`/`POST .../slot-rows/{row_id}/architect-pairing` in `app/api/slot_rows.py`.
Rows come from `tests/api/test_slot_rows.py`'s synthetic package; the architect's spans and the
automatic record are added here. Invented values only.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.slot_rows import (
    ArchitectPairIn,
    ArchitectPairingIn,
    get_architect_pairing,
    pair_architect_dimensions,
    router,
)
from app.audit.events import AuditEvent
from app.auth.roles import Action, Principal, Role
from app.db.session import session_factory
from app.models import DrawingView, ObservationCandidate, Project, ViewRole
from app.models.evidence import ArchitectPairingRecord
from app.models.runs import ExtractionRun
from app.models.verdicts import Finding
from tests.api.test_slot_rows import _package_rows
from tests.app.postgres_fixture import alembic_config
from workflow.architect_reader import ARCHITECT_EXTRACTOR, ARCHITECT_EXTRACTOR_VERSION
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)


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


def _principal(project_id: UUID) -> Principal:
    return Principal(
        id="reviewer (synthetic test)",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({project_id}),
    )


class Row:
    """A synthetic row of three pieces, and three architect spans on its page: a cabinet, a
    centre-line dimension and a held one; one automatic record whose AIs disagreed."""

    def __init__(self, session: Session) -> None:
        self.project_id, self.package_id, anchors = _package_rows(session, piece_count=3)
        self.anchor = session.get_one(ObservationCandidate, anchors[0])
        slot_run = session.get_one(ExtractionRun, self.anchor.extraction_run_id)
        architect_run = ExtractionRun(
            task_run_id=slot_run.task_run_id,
            extractor=ARCHITECT_EXTRACTOR,
            extractor_version=ARCHITECT_EXTRACTOR_VERSION,
            config_hash="test",
            dpi=150,
        )
        session.add(architect_run)
        session.flush()
        view = DrawingView(
            page_id=self.anchor.page_id,
            tag="panel-1",
            region={"space": "stored", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]},
        )
        session.add(view)
        session.flush()
        confirm_view_role(session, view=view, role=ViewRole.ARCH, actor="reviewer-0")

        def span(slot: int, text: str, outline: str, held: str | None) -> ObservationCandidate:
            candidate = ObservationCandidate(
                document_version_id=self.anchor.document_version_id,
                page_id=self.anchor.page_id,
                extraction_run_id=architect_run.id,
                raw_text=text,
                value_numerator=None if held else 30,
                value_denominator=None if held else 1,
                unit=None if held else "in",
                polygon=[[0, 0], [1, 0], [1, 1], [0, 1]],
                coordinate_space="image",
                ambiguity_flags=[
                    "architect-reader",
                    "arch-view:1",
                    "arch-row:1",
                    f"arch-slot:{slot}",
                    f"arch-ticks-on-outline:{outline}",
                    *([f"arch-held:{held}"] if held else []),
                ],
            )
            session.add(candidate)
            return candidate

        self.cabinet = span(0, "2' - 6\"", "yes", None)
        self.centre_line = span(1, "1' - 5\"", "no", None)
        self.held = span(2, "2' - 7\"", "yes", "the drawn length disagrees with the label")
        session.flush()
        self.automatic = ArchitectPairingRecord(
            package_revision_id=self._revision_id(session),
            page_id=self.anchor.page_id,
            row_anchor_candidate_id=self.anchor.id,
            extraction_run_id=slot_run.id,
            source="none",
            status="ais-disagree",
            pairs=[],
            details={"architect_run_id": str(architect_run.id), "reasons": ["they differ"]},
        )
        session.add(self.automatic)
        session.commit()
        self.principal = _principal(self.project_id)

    def _revision_id(self, session: Session) -> UUID:
        from app.models import PackageRevision

        return session.scalars(
            select(PackageRevision.id).where(PackageRevision.package_id == self.package_id)
        ).one()

    def post(self, session: Session, body: ArchitectPairingIn, *, project: UUID | None = None):  # type: ignore[no-untyped-def]
        return pair_architect_dimensions(
            self.principal,
            self.principal,
            session,
            project or self.project_id,
            self.package_id,
            self.anchor.id,
            body,
        )


def _pair(candidate: ObservationCandidate, *slots: int) -> ArchitectPairIn:
    return ArchitectPairIn(
        kind="piece", architect_candidate_id=candidate.id, vendor_slot_indices=list(slots)
    )


def test_get_shows_the_record_and_every_architect_span_with_why_it_can_or_cannot_pair(
    session: Session,
) -> None:
    row = Row(session)

    shown = get_architect_pairing(
        row.principal, session, row.project_id, row.package_id, row.anchor.id
    )

    assert shown.current is not None and shown.current.status == "ais-disagree"
    assert shown.effective is not None and shown.effective.pairs == []
    spans = {span.candidate_id: span for span in shown.spans}
    assert spans[row.cabinet.id].can_pair and spans[row.cabinet.id].inches == "30 in"
    assert not spans[row.centre_line.id].can_pair
    assert "centre line" in (spans[row.centre_line.id].refusal or "")
    assert not spans[row.held.id].can_pair and spans[row.held.id].held_reason
    assert shown.piece_count == 3


def test_one_click_pairs_supersedes_and_is_audited(session: Session) -> None:
    row = Row(session)

    saved = row.post(
        session, ArchitectPairingIn(pairs=[_pair(row.cabinet, 0, 1)], note="cabinet split")
    )

    assert saved.current is not None
    assert (saved.current.source, saved.current.status) == ("reviewer", "reviewer")
    assert saved.current.supersedes_id == row.automatic.id
    assert saved.current.decided_by == "reviewer (synthetic test)"
    assert saved.current.note == "cabinet split"
    assert saved.effective is not None and not saved.effective.ai_only
    assert [pair.vendor_slot_indices for pair in saved.effective.pairs] == [[0, 1]]
    audited = session.scalars(
        select(AuditEvent).where(AuditEvent.target_id == saved.current.record_id)
    ).all()
    assert [event.actor for event in audited] == ["reviewer (synthetic test)"]


def test_nothing_comparable_is_an_explicit_reviewer_statement(session: Session) -> None:
    row = Row(session)

    saved = row.post(session, ArchitectPairingIn(pairs=[], note="only outlet spacing"))

    assert saved.current is not None and saved.current.source == "reviewer"
    assert saved.effective is not None and saved.effective.pairs == []
    assert "nothing" in " ".join(saved.current.reasons)


@pytest.mark.parametrize(
    ("which", "slots", "why"),
    [
        ("centre_line", (0,), "centre line"),
        ("held", (0,), "held"),
        ("cabinet", (0, 2), "next to each other"),
        ("cabinet", (5,), "own pieces"),
    ],
)
def test_refused_pairings_are_422_and_store_nothing(
    session: Session, which: str, slots: tuple[int, ...], why: str
) -> None:
    row = Row(session)

    with pytest.raises(HTTPException) as refused:
        row.post(session, ArchitectPairingIn(pairs=[_pair(getattr(row, which), *slots)]))

    assert refused.value.status_code == 422
    assert why in str(refused.value.detail)
    session.rollback()
    assert session.query(ArchitectPairingRecord).count() == 1


def test_a_span_from_another_page_is_refused(session: Session) -> None:
    row = Row(session)
    other = session.scalars(
        select(ObservationCandidate).where(ObservationCandidate.page_id != row.anchor.page_id)
    ).first()
    assert other is not None

    with pytest.raises(HTTPException) as refused:
        row.post(session, ArchitectPairingIn(pairs=[_pair(other, 0)]))

    assert refused.value.status_code == 422


def test_another_project_cannot_read_or_pair_this_row(session: Session) -> None:
    row = Row(session)
    stranger = Project(name="another project")
    session.add(stranger)
    session.commit()

    for call in (
        lambda: get_architect_pairing(
            row.principal, session, stranger.id, row.package_id, row.anchor.id
        ),
        lambda: row.post(session, ArchitectPairingIn(pairs=[]), project=stranger.id),
    ):
        with pytest.raises(HTTPException) as hidden:
            call()
        assert hidden.value.status_code == 404
    assert session.query(ArchitectPairingRecord).count() == 1


def test_an_unknown_row_is_404(session: Session) -> None:
    row = Row(session)

    with pytest.raises(HTTPException) as missing:
        get_architect_pairing(row.principal, session, row.project_id, row.package_id, uuid4())

    assert missing.value.status_code == 404


def test_a_race_on_the_same_record_is_409(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = Row(session)
    row.post(session, ArchitectPairingIn(pairs=[]))
    stale = session.get_one(ArchitectPairingRecord, row.automatic.id)
    monkeypatch.setattr(
        "workflow.architect_pairing_records._chain_tip", lambda _session, _anchor: stale
    )

    with pytest.raises(HTTPException) as raced:
        row.post(session, ArchitectPairingIn(pairs=[_pair(row.cabinet, 0)]))

    assert raced.value.status_code == 409


def test_pairing_never_changes_a_finding(session: Session) -> None:
    row = Row(session)
    before = [(f.id, f.outcome) for f in session.scalars(select(Finding))]

    row.post(session, ArchitectPairingIn(pairs=[_pair(row.cabinet, 0)]))

    assert [(f.id, f.outcome) for f in session.scalars(select(Finding))] == before


def test_pairing_needs_the_confirm_evidence_action() -> None:
    def actions(method: str) -> set[Action]:
        route = next(
            route
            for route in router.routes
            if isinstance(route, APIRoute)
            and route.path.endswith("/architect-pairing")
            and method in (route.methods or set())
        )
        return {
            cell.cell_contents
            for dependency in route.dependant.dependencies
            for cell in (getattr(dependency.call, "__closure__", None) or ())
            if isinstance(cell.cell_contents, Action)
        }

    assert actions("POST") == {Action.CONFIRM_EVIDENCE}
    assert actions("GET") == set(), "reading needs project access only"
