"""Storing the architect's values from a combined sheet, and the role code decides (#1052).

Verification for: `workflow/architect_reader.py`, `workflow/view_roles.confirm_view_role_by_code`
and the `extract_pages` hook behind `GV_ARCHITECT_READER_ENABLED`. The sheet is the invented one in
`tests/extraction/architect/combined_sheet.py`; no client value appears here.
"""

from __future__ import annotations

import io
import tempfile
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.documents import storage_key
from app.audit.events import AuditEvent
from app.db.session import session_factory
from app.evidence.sides import ReadingSides
from app.models import (
    Document,
    DocumentVersion,
    DrawingView,
    PackageRevision,
    PackageRevisionDocument,
    SourceArtifact,
    ViewRole,
    ViewRoleConfirmation,
)
from app.models.evidence import ObservationCandidate
from app.models.runs import ExtractionRun
from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.architect.combined_sheet import combined_sheet
from tests.extraction.test_reader import MISSING_SPACE
from vocabulary.semantic_types import DocumentRole
from workflow.architect_reader import ARCHITECT_EXTRACTOR
from workflow.stages import DatabaseStages, configured_architect_reader
from workflow.view_roles import CODE_CONFIRMER, confirm_view_role, confirm_view_role_by_code

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


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _stages(store: LocalStore, *, enabled: bool = True) -> DatabaseStages:
    from tests.workflow.test_markup_route import _SilentOcr

    return DatabaseStages(
        store,
        dpi=150,
        ocr_engine=_SilentOcr(),  # type: ignore[arg-type]
        missing_space=MISSING_SPACE,
        architect_reader=MEASURED_ARCHITECT_SETTINGS if enabled else None,
    )


def _upload(session: Session, store: LocalStore, data: bytes) -> PackageRevision:
    from tests.workflow.test_association import _revision as _stored_revision

    revision = _stored_revision(session, store, data=data)
    session.commit()
    return revision


def _extract(
    session: Session, store: LocalStore, revision: PackageRevision, *, enabled: bool = True
) -> None:
    _stages(store, enabled=enabled).extract_pages(session, revision.id)
    session.commit()


def _architect_rows(session: Session) -> list[ObservationCandidate]:
    return list(
        session.scalars(
            select(ObservationCandidate)
            .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
            .where(ExtractionRun.extractor == ARCHITECT_EXTRACTOR)
            .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
        )
    )


def _by_text(rows: list[ObservationCandidate]) -> dict[str, ObservationCandidate]:
    return {row.raw_text.replace("’", "'"): row for row in rows}


def _views(session: Session) -> dict[str, DrawingView]:
    return {view.tag: view for view in session.scalars(select(DrawingView))}


@pytest.mark.parametrize(("enabled", "expected"), [(False, None), (True, "measured")])
def test_the_setting_switches_the_reader_on_and_off(enabled: bool, expected: str | None) -> None:
    """The typed `GV_ARCHITECT_READER_ENABLED` field: on gives the measured settings, off none."""
    from app.config import Settings

    settings = Settings(
        database_url="postgresql+psycopg://x@localhost/x", architect_reader_enabled=enabled
    )

    reader = configured_architect_reader(settings)
    assert reader is (MEASURED_ARCHITECT_SETTINGS if expected else None)


def test_the_setting_is_off_by_default_and_read_from_its_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import Settings

    monkeypatch.delenv("GV_ARCHITECT_READER_ENABLED", raising=False)
    assert Settings.model_fields["architect_reader_enabled"].default is False
    monkeypatch.setenv("GV_ARCHITECT_READER_ENABLED", "true")
    on = Settings(database_url="postgresql+psycopg://x@localhost/x")
    assert on.architect_reader_enabled is True
    assert configured_architect_reader(on) is MEASURED_ARCHITECT_SETTINGS


def test_off_nothing_is_read_and_no_role_is_set(session: Session, store: LocalStore) -> None:
    _extract(session, store, _upload(session, store, combined_sheet()), enabled=False)

    assert _architect_rows(session) == []
    assert session.scalars(select(ViewRoleConfirmation)).all() == []


def test_agreeing_judgments_confirm_both_roles_by_code(session: Session, store: LocalStore) -> None:
    _extract(session, store, _upload(session, store, combined_sheet()))

    views = _views(session)
    assert views["panel-1"].role == ViewRole.ARCH.value
    assert views["panel-3"].role == ViewRole.SHOP.value
    confirmations = session.scalars(select(ViewRoleConfirmation)).all()
    assert {row.confirmed_by for row in confirmations} == {CODE_CONFIRMER}
    audited = session.scalars(
        select(AuditEvent).where(AuditEvent.target_id.in_([row.id for row in confirmations]))
    ).all()
    assert {event.actor for event in audited} == {CODE_CONFIRMER}


def test_architect_values_are_stored_as_the_architects_with_their_flags(
    session: Session, store: LocalStore
) -> None:
    _extract(session, store, _upload(session, store, combined_sheet()))

    rows = _by_text(_architect_rows(session))
    cabinet = rows["3' - 4\""]
    assert Fraction(cabinet.value_numerator, cabinet.value_denominator) == 40  # type: ignore[arg-type]
    assert cabinet.unit == "in"
    flags = set(cabinet.ambiguity_flags)
    assert "arch-ticks-on-outline:yes" in flags
    assert "arch-slot:0" in flags
    assert any(flag.startswith("arch-row:") for flag in flags)
    assert any(flag.startswith("arch-scale:1.5") for flag in flags)
    assert any(flag.startswith("arch-ticks:110") for flag in flags)
    assert "arch-ticks-on-outline:no" in rows["1' - 5\""].ambiguity_flags

    assert ReadingSides(session).of(cabinet) is DocumentRole.ARCH


def test_a_held_span_is_stored_with_its_reason_and_no_value(
    session: Session, store: LocalStore
) -> None:
    _extract(session, store, _upload(session, store, combined_sheet()))

    held = _by_text(_architect_rows(session))["2' - 7\""]
    assert held.value_numerator is None and held.value_denominator is None
    assert held.review_reason is not None and "drawn length" in held.review_reason
    assert any(flag.startswith("arch-held:") for flag in held.ambiguity_flags)


def test_coloured_text_is_never_stored(session: Session, store: LocalStore) -> None:
    _extract(session, store, _upload(session, store, combined_sheet()))

    rows = _architect_rows(session)
    assert rows
    assert all("9' - 9" not in row.raw_text.replace("’", "'") for row in rows)
    assert all(row.value_numerator != 117 for row in rows)


def test_a_sheet_with_no_heading_decides_no_role_and_stores_no_value(
    session: Session, store: LocalStore
) -> None:
    _extract(session, store, _upload(session, store, combined_sheet(headings=False)))

    assert session.scalars(select(ViewRoleConfirmation)).all() == []
    assert _architect_rows(session) == []


def test_a_same_file_package_gets_no_architect_values(session: Session, store: LocalStore) -> None:
    """The same bytes uploaded as both drawings are one combined set (#963): nothing is read."""
    revision = _upload(session, store, combined_sheet())
    shop = session.scalars(select(DocumentVersion)).one()
    architectural = Document(package_id=revision.package_id, kind="architectural")
    session.add(architectural)
    session.flush()
    data = combined_sheet()
    key = storage_key(architectural.id, shop.sha256)
    artifact = SourceArtifact(storage_key=key, sha256=shop.sha256, size=len(data))
    session.add(artifact)
    session.flush()
    store.put(key, io.BytesIO(data), content_type="application/pdf")
    twin = DocumentVersion(
        document_id=architectural.id,
        source_artifact_id=artifact.id,
        sha256=shop.sha256,
        page_count=1,
    )
    session.add(twin)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision.id,
            package_id=revision.package_id,
            document_id=architectural.id,
            document_version_id=twin.id,
        )
    )
    session.commit()

    _extract(session, store, revision)

    assert _architect_rows(session) == []
    assert session.scalars(select(ViewRoleConfirmation)).all() == []


def test_a_person_confirming_later_wins_and_a_rerun_neither_overrides_nor_repeats(
    session: Session, store: LocalStore
) -> None:
    revision = _upload(session, store, combined_sheet())
    _extract(session, store, revision)
    view = _views(session)["panel-1"]
    stored = len(_architect_rows(session))

    confirm_view_role(session, view=view, role=ViewRole.SHOP, actor="reviewer-1")
    session.commit()
    _extract(session, store, revision)

    assert session.get_one(DrawingView, view.id).role == ViewRole.SHOP.value
    assert len(_architect_rows(session)) == stored
    latest = session.scalars(
        select(ViewRoleConfirmation)
        .where(ViewRoleConfirmation.drawing_view_id == view.id)
        .order_by(ViewRoleConfirmation.created_at.desc())
    ).first()
    assert latest is not None and latest.confirmed_by == "reviewer-1"


def test_code_never_confirms_over_a_persons_earlier_confirmation(
    session: Session, store: LocalStore
) -> None:
    _extract(session, store, _upload(session, store, combined_sheet()), enabled=False)
    view = _views(session)["panel-1"]
    confirm_view_role(session, view=view, role=ViewRole.SHOP, actor="reviewer-1")

    written = confirm_view_role_by_code(
        session, view=view, role=ViewRole.ARCH, reason="heading and content agree"
    )

    assert written is None
    assert view.role == ViewRole.SHOP.value
