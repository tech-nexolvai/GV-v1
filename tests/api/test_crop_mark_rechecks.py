"""Every screen that shows the "shows GV markup" flag shows the re-checked one (#1141).

A crop cut before #1078 keeps its stored flag (the row is append-only); a re-check row written by
`workflow/crop_mark_rechecks.py` is what a reviewer must be told. Three readers show the flag: the
candidate list, the vendor-only view (both routes), and the finding chain. Each must agree, and a
crop with no re-check must still show its own flag.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

import workflow.stages as stages_module
from app.db.session import session_factory
from app.main import API_PREFIX
from app.models import (
    CanonicalObservation,
    EvidenceArtifact,
    EvidenceMarkRecheck,
    Package,
    Page,
)
from storage.local import LocalStore
from tests.api.test_drawing_views import _client
from tests.api.test_finding_chain import _client as _chain_client
from tests.api.test_finding_chain import _seed
from tests.api.test_vendor_only_region import _base, _candidate, _confirmed
from tests.extraction.test_reader import MISSING_SPACE
from tests.workflow.test_crop_mark_rechecks import RUN_BY, _old_check
from tests.workflow.test_evidence_crop_gv_marks import REVIEWED
from tests.workflow.test_pipeline_end_to_end import _revision, _upgrade, store
from workflow.crop_mark_rechecks import recheck_crop_marks
from workflow.stages import DatabaseStages

__all__ = ["store"]  # the fixture, re-exported so pytest finds it here

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _cut_before_1078(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> tuple[UUID, UUID]:
    """The sheet read and its crops cut with the old check; vendor-only pictures made."""
    revision = _revision(session, store, data=REVIEWED)
    stages = DatabaseStages(store, missing_space=MISSING_SPACE)
    stages.extract_pages(session, revision.id)
    with monkeypatch.context() as patched:
        patched.setattr(stages_module, "crop_mark_state", _old_check)
        stages.validate_evidence(session, revision.id)
    stages.render_vendor_page_pictures(session, revision.id)
    session.commit()
    package = session.get_one(Package, revision.package_id)
    return package.project_id, package.id


def _recheck(session: Session, store: LocalStore) -> None:
    stages = DatabaseStages(store, missing_space=MISSING_SPACE)
    outcome = recheck_crop_marks(session, stages, run_by=RUN_BY, dry_run=False)
    session.commit()
    assert outcome.changed > 0, outcome.as_dict()


def _listed(client: Any, project_id: UUID, package_id: UUID) -> dict[str, bool | None]:
    response = client.get(f"{_base(project_id, package_id)}/candidates", params={"page_number": 1})
    assert response.status_code == 200, response.text
    return {item["raw_text"]: item["crop_shows_gv_mark"] for item in response.json()["candidates"]}


def test_the_candidate_list_shows_the_rechecked_flag(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, package_id = _cut_before_1078(session, store, monkeypatch)
    client = _client(session, store, project_id)
    before = _listed(client, project_id, package_id)
    covered = [text for text in before if text.startswith("648")]
    far = [text for text in before if text.startswith("100")]
    assert covered and far, before
    assert {before[text] for text in covered} == {False}, "the old flag was not reproduced"

    _recheck(session, store)

    after = _listed(client, project_id, package_id)
    assert {after[text] for text in covered} == {True}
    assert {after[text] for text in far} == {False}


@pytest.mark.parametrize("route", ["evidence", "candidates"])
def test_the_vendor_only_view_shows_the_rechecked_flag(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    project_id, package_id = _cut_before_1078(session, store, monkeypatch)
    candidate = _candidate(session, "648")
    reading = candidate.id if route == "candidates" else _confirmed(session, candidate).id
    client = _client(session, store, project_id)
    url = f"{_base(project_id, package_id)}/{route}/{reading}/vendor-only"
    assert client.get(url).json()["crop_shows_gv_mark"] is False

    _recheck(session, store)

    body = client.get(url).json()
    assert body["crop_shows_gv_mark"] is True
    assert body["available"] is True


def _chain_marks(
    session: Session, project_id: UUID, package_id: UUID, finding_id: UUID
) -> tuple[str, dict[str, bool | None]]:
    response = _chain_client(session, project_id).get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/findings/{finding_id}/chain"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["outcome"], {
        operand["evidence"]["document_role"]: operand["evidence"]["crop_shows_gv_mark"]
        for operand in body["operands"]
    }


@pytest.mark.parametrize(("stored", "rechecked"), [(False, True), (None, True), (False, None)])
def test_the_finding_chain_shows_the_newest_recheck(
    session: Session, stored: bool | None, rechecked: bool | None
) -> None:
    """A re-check that says "not checked" still wins: it is an answer, not an absence."""
    project_id, package_id, finding_id = _seed(session)
    shop = session.scalars(
        select(CanonicalObservation).where(CanonicalObservation.document_role == "SHOP")
    ).one()
    page = session.get_one(Page, shop.page_id)
    crop = EvidenceArtifact(
        candidate_id=None,
        canonical_observation_id=shop.id,
        document_version_id=page.document_version_id,
        page_id=page.id,
        kind="crop",
        storage_key="evidence-crops/old.png",
        sha256="d" * 64,
        media_type="image/png",
        coordinate_space="image",
        shows_gv_marks=stored,
    )
    session.add(crop)
    session.flush()
    outcome_before, before = _chain_marks(session, project_id, package_id, finding_id)
    assert before == {"SHOP": stored, "ARCH": None}

    session.add(
        EvidenceMarkRecheck(
            crop_artifact_id=crop.id,
            shows_gv_marks=rechecked,
            check_version="test-check",
            run_id=uuid4(),
            run_by=RUN_BY,
        )
    )
    session.flush()

    outcome_after, after = _chain_marks(session, project_id, package_id, finding_id)
    assert after == {"SHOP": rechecked, "ARCH": None}
    # A flag never changes an outcome.
    assert outcome_after == outcome_before
