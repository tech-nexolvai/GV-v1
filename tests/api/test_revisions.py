"""Revision 2 of a job through the API (#1180): start it, keep revision 1 read-only, carry forward
what is unchanged, and say what changed.

The real routes against a real database. Every file, vendor and value here is made up; no paid call
is made (nothing reads a drawing: `/extract` only queues the work).
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.auth import Principal, Role, authenticate
from app.db.session import session_factory
from app.lifecycle.carry_forward import carry_after_reading
from app.lifecycle.side_states import enter_needs_input
from app.lifecycle.states import transition
from app.main import create_app
from app.models import (
    Approval,
    Document,
    DocumentKind,
    DocumentVersion,
    DrawingView,
    ObservationCandidate,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Page,
    SlotRowReviewDecision,
    SourceArtifact,
    ViewRoleConfirmation,
)
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import ParameterValue as StoredParameterValue
from app.models.parameters import to_rows
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from rules.parameters import ParameterLayer, ParameterValue, Provenance
from rules.parameters import ParameterSet as InMemoryParameterSet
from rules.schema import Quantity
from storage.local import LocalStore
from tests.api.test_packages import PROJECT, _new_package, _settings, _upgrade
from units.measurement import Unit
from workflow.architect_match_records import remembered_matches
from workflow.slot_row_scope import slot_rows
from workflow.view_roles import CODE_CONFIRMER

pytest_plugins = ("tests.app.postgres_fixture",)

ARCH_BYTES = b"%PDF-1.7 synthetic architect set\n"
SHOP_ONE = b"%PDF-1.7 synthetic shop drawing, first issue\n"
SHOP_TWO = b"%PDF-1.7 synthetic shop drawing, second issue\n"
SHOP_THREE = b"%PDF-1.7 synthetic shop drawing, third issue\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(root=tmp_path / "artifacts", ticket_secret=b"synthetic-test-secret")


def _client(session: Session, store: LocalStore, *, roles: frozenset[Role] | None = None) -> Any:
    app = create_app(_settings())
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="synthetic-reviewer",
        roles=roles or frozenset({Role.ADMIN}),
        projects=frozenset({PROJECT}),
    )
    return TestClient(app, raise_server_exceptions=False)


def _base(package_id: UUID) -> str:
    return f"/api/v1/projects/{PROJECT}/packages/{package_id}"


def _upload(
    client: Any, store: LocalStore, package_id: UUID, kind: DocumentKind, data: bytes
) -> str:
    registered = client.post(
        f"{_base(package_id)}/documents", json={"kind": kind.value, "sha256": _sha(data)}
    )
    assert registered.status_code == 201, registered.text
    ticket = registered.json()
    store.put(ticket["storage_key"], io.BytesIO(data), content_type="application/pdf")
    confirmed = client.post(
        f"/api/v1/projects/{PROJECT}/documents/{ticket['document_id']}/confirm",
        json={"sha256": _sha(data), "page_count": 2, "file_name": f"synthetic-{kind.value}.pdf"},
    )
    assert confirmed.status_code in (200, 201), confirmed.text
    return str(ticket["document_id"])


def _finish_reading(session: Session, revision_id: UUID) -> None:
    """Walk a revision from UPLOADED to waiting for the reviewer, as the worker does (no reading)."""
    for state in (
        PackageState.INGESTING,
        PackageState.EXTRACTING,
        PackageState.MATCHING,
        PackageState.VALIDATING_EVIDENCE,
    ):
        transition(session, revision_id, state, actor="synthetic worker")
    enter_needs_input(session, revision_id, actor="synthetic worker", needed="synthetic values")
    session.commit()


def _revision(session: Session, package_id: UUID, number: int) -> PackageRevision:
    session.expire_all()
    found = session.scalar(
        select(PackageRevision).where(
            PackageRevision.package_id == package_id, PackageRevision.revision_number == number
        )
    )
    assert found is not None
    return found


def _read_review(client: Any, session: Session, store: LocalStore) -> tuple[UUID, str, str]:
    """A package whose revision 1 holds an architect set and a shop drawing, read and waiting."""
    package_id = _new_package(session)
    arch = _upload(client, store, package_id, DocumentKind.ARCHITECTURAL, ARCH_BYTES)
    shop = _upload(client, store, package_id, DocumentKind.SHOP, SHOP_ONE)
    assert client.post(f"{_base(package_id)}/extract").status_code == 202
    _finish_reading(session, _revision(session, package_id, 1).id)
    return package_id, arch, shop


def _members(session: Session, revision_id: UUID) -> dict[UUID, UUID]:
    session.expire_all()
    return {
        document: version
        for document, version in session.execute(
            select(
                PackageRevisionDocument.document_id, PackageRevisionDocument.document_version_id
            ).where(PackageRevisionDocument.package_revision_id == revision_id)
        )
    }


def _start(
    client: Any,
    package_id: UUID,
    shop: bytes,
    arch: bytes | None = None,
    *,
    expected: int = 1,
    headers: dict[str, str] | None = None,
) -> Any:
    body: dict[str, object] = {"expected_revision": expected, "shop": {"sha256": _sha(shop)}}
    if arch is not None:
        body["architectural"] = {"sha256": _sha(arch)}
    return client.post(f"{_base(package_id)}/revisions", json=body, headers=headers or {})


def _send_and_confirm(client: Any, store: LocalStore, upload: dict[str, Any], data: bytes) -> None:
    store.put(upload["ticket"]["storage_key"], io.BytesIO(data), content_type="application/pdf")
    confirmed = client.post(
        f"/api/v1/projects/{PROJECT}/documents/{upload['document_id']}/confirm",
        json={"sha256": _sha(data), "page_count": 2, "file_name": "synthetic-reissue.pdf"},
    )
    assert confirmed.status_code == 201, confirmed.text


# --- starting a revision ----------------------------------------------------------------------------


def test_starting_a_revision_opens_revision_two_and_closes_revision_one(
    session: Session, store: LocalStore
) -> None:
    client = _client(session, store)
    package_id, arch, shop = _read_review(client, session, store)
    one = _revision(session, package_id, 1)
    before = _members(session, one.id)

    started = _start(client, package_id, SHOP_TWO)

    assert started.status_code == 201, started.text
    body = started.json()
    assert body["revision"]["revision_number"] == 2
    assert body["revision"]["state"] == "UPLOADING"
    assert body["revision"]["supersedes_revision_number"] == 1
    assert [(u["kind"], u["document_id"], u["status"]) for u in body["uploads"]] == [
        ("shop", shop, "upload")
    ]
    assert body["uploads"][0]["ticket"]["document_id"] == shop
    assert _revision(session, package_id, 1).state == PackageState.SUPERSEDED
    # Revision 1's drawings are untouched; revision 2 starts holding the same versions.
    assert _members(session, one.id) == before
    two = _revision(session, package_id, 2)
    assert _members(session, two.id) == before

    # The existing upload flow finishes it: the confirmed re-issue replaces the carried shop version.
    _send_and_confirm(client, store, body["uploads"][0], SHOP_TWO)
    after = _members(session, two.id)
    assert after[UUID(arch)] == before[UUID(arch)]
    assert after[UUID(shop)] != before[UUID(shop)]
    assert _members(session, one.id) == before
    assert client.post(f"{_base(package_id)}/extract").status_code == 202
    assert _revision(session, package_id, 2).state == PackageState.UPLOADED


def test_a_revision_with_nothing_new_is_not_read(session: Session, store: LocalStore) -> None:
    client = _client(session, store)
    package_id, _arch, _shop = _read_review(client, session, store)
    assert _start(client, package_id, SHOP_TWO).status_code == 201

    refused = client.post(f"{_base(package_id)}/extract")

    assert refused.status_code == 422
    assert "re-issued drawing" in refused.json()["message"]


def test_the_same_shop_file_is_not_a_revision(session: Session, store: LocalStore) -> None:
    client = _client(session, store)
    package_id, _arch, _shop = _read_review(client, session, store)

    refused = _start(client, package_id, SHOP_ONE)

    assert refused.status_code == 422
    assert "same shop drawing file as revision 1" in refused.json()["message"]
    assert _revision(session, package_id, 1).state == PackageState.NEEDS_INPUT
    assert (
        session.scalar(
            select(PackageRevision.id).where(
                PackageRevision.package_id == package_id, PackageRevision.revision_number == 2
            )
        )
        is None
    )


def test_one_file_cannot_be_both_drawings(session: Session, store: LocalStore) -> None:
    client = _client(session, store)
    package_id, _arch, _shop = _read_review(client, session, store)

    refused = _start(client, package_id, ARCH_BYTES)

    assert refused.status_code == 409
    assert "combined set" in refused.json()["message"]


def test_a_revision_cannot_start_while_the_drawings_are_still_uploading(
    session: Session, store: LocalStore
) -> None:
    client = _client(session, store)
    package_id = _new_package(session)
    _upload(client, store, package_id, DocumentKind.SHOP, SHOP_ONE)

    refused = _start(client, package_id, SHOP_TWO)

    assert refused.status_code == 409
    assert "still being uploaded" in refused.json()["message"]


def test_a_new_architect_drawing_gets_its_own_ticket(session: Session, store: LocalStore) -> None:
    client = _client(session, store)
    package_id, arch, shop = _read_review(client, session, store)

    started = _start(client, package_id, SHOP_TWO, arch=b"%PDF-1.7 synthetic architect, v2\n")

    assert started.status_code == 201, started.text
    statuses = {u["kind"]: (u["document_id"], u["status"]) for u in started.json()["uploads"]}
    assert statuses == {"shop": (shop, "upload"), "architectural": (arch, "upload")}


def test_the_same_architect_file_is_reused(session: Session, store: LocalStore) -> None:
    client = _client(session, store)
    package_id, arch, _shop = _read_review(client, session, store)

    started = _start(client, package_id, SHOP_TWO, arch=ARCH_BYTES)

    uploads = {u["kind"]: u for u in started.json()["uploads"]}
    assert {kind: u["status"] for kind, u in uploads.items()} == {
        "shop": "upload",
        "architectural": "unchanged",
    }
    assert uploads["architectural"]["document_id"] == arch
    assert uploads["architectural"]["ticket"] is None


def test_bytes_uploaded_before_are_reused_without_sending_them_again(
    session: Session, store: LocalStore
) -> None:
    client = _client(session, store)
    package_id, _arch, shop = _read_review(client, session, store)
    first_version = _members(session, _revision(session, package_id, 1).id)[UUID(shop)]
    started = _start(client, package_id, SHOP_TWO).json()
    _send_and_confirm(client, store, started["uploads"][0], SHOP_TWO)
    assert client.post(f"{_base(package_id)}/extract").status_code == 202
    _finish_reading(session, _revision(session, package_id, 2).id)

    # Revision 3 goes back to the first issue: those bytes are already stored for this drawing.
    third = _start(client, package_id, SHOP_ONE, expected=2)

    assert third.status_code == 201, third.text
    assert third.json()["uploads"][0]["status"] == "already_stored"
    assert third.json()["uploads"][0]["ticket"] is None
    assert _members(session, _revision(session, package_id, 3).id)[UUID(shop)] == first_version
    assert client.post(f"{_base(package_id)}/extract").status_code == 202


# --- read-only revision 1 -----------------------------------------------------------------------------


def test_revision_one_is_read_only_after_revision_two_starts(
    session: Session, store: LocalStore
) -> None:
    client = _client(session, store)
    package_id, _arch, _shop = _read_review(client, session, store)
    one = _revision(session, package_id, 1)
    opened = client.post(
        f"{_base(package_id)}/review-sessions", json={"package_revision_id": str(one.id)}
    )
    assert opened.status_code == 201, opened.text
    assert _start(client, package_id, SHOP_TWO).status_code == 201

    refused = client.post(
        f"/api/v1/projects/{PROJECT}/review-sessions/{opened.json()['id']}/actions",
        json={"finding_id": str(uuid4()), "action": "confirm"},
    )

    assert refused.status_code == 409
    # #1182's closed-revision guard answers first, in its own words for a replaced revision.
    assert "A newer revision replaced this one" in refused.json()["message"]
    reopened = client.post(
        f"{_base(package_id)}/review-sessions", json={"package_revision_id": str(one.id)}
    )
    assert reopened.status_code == 409


def test_an_unfinished_start_is_resumed_not_refused(session: Session, store: LocalStore) -> None:
    """The start was abandoned before the drawing was sent: asking again hands out the tickets for
    the same revision 2, opens nothing new and closes nothing more (the choice made for #1180)."""
    client = _client(session, store)
    package_id, _arch, shop = _read_review(client, session, store)
    assert _start(client, package_id, SHOP_TWO).status_code == 201

    # Abandoned: revision 1 is closed, revision 2 waits for its drawing, and the list says so.
    listed = client.get(f"{_base(package_id)}/revisions").json()
    assert listed["unfinished_revision"] == 2 and listed["can_start_revision"] is True
    assert _revision(session, package_id, 1).state == PackageState.SUPERSEDED

    resumed = _start(client, package_id, SHOP_THREE, expected=2)

    assert resumed.status_code == 201, resumed.text
    body = resumed.json()
    assert body["resumed"] is True and body["revision"]["revision_number"] == 2
    assert [(u["document_id"], u["status"]) for u in body["uploads"]] == [(shop, "upload")]
    assert "was started but not read yet" in body["next_step"]
    assert (
        session.scalar(
            select(PackageRevision.id).where(
                PackageRevision.package_id == package_id, PackageRevision.revision_number == 3
            )
        )
        is None
    )
    _send_and_confirm(client, store, body["uploads"][0], SHOP_THREE)
    assert client.post(f"{_base(package_id)}/extract").status_code == 202
    assert client.get(f"{_base(package_id)}/revisions").json()["unfinished_revision"] is None


def test_a_resumed_start_still_refuses_the_previous_shop_file(
    session: Session, store: LocalStore
) -> None:
    client = _client(session, store)
    package_id, _arch, _shop = _read_review(client, session, store)
    _start(client, package_id, SHOP_TWO)

    refused = _start(client, package_id, SHOP_ONE, expected=2)

    assert refused.status_code == 422
    assert "same shop drawing file as revision 1" in refused.json()["message"]


def test_two_simultaneous_starts_open_one_revision(session: Session, store: LocalStore) -> None:
    client = _client(session, store)
    package_id, _arch, _shop = _read_review(client, session, store)

    first = _start(client, package_id, SHOP_TWO)
    second = _start(client, package_id, SHOP_THREE)

    assert first.status_code == 201
    assert second.status_code == 409
    assert "just started by someone else" in second.json()["message"]
    assert "revision 2" in second.json()["message"]


def test_a_revision_cannot_start_while_checks_are_queued(
    session: Session, store: LocalStore
) -> None:
    from workflow.outbox import enqueue

    client = _client(session, store)
    package_id, _arch, _shop = _read_review(client, session, store)
    one = _revision(session, package_id, 1)
    enqueue(
        session,
        workflow="run_checks",
        payload={"package_revision_id": str(one.id), "project_id": str(PROJECT)},
    )
    session.commit()

    refused = _start(client, package_id, SHOP_TWO)

    assert refused.status_code == 409
    assert "Checks are running on revision 1" in refused.json()["message"]
    assert _revision(session, package_id, 1).state == PackageState.NEEDS_INPUT


def test_an_architect_only_change_is_not_a_revision_yet(
    session: Session, store: LocalStore
) -> None:
    client = _client(session, store)
    package_id, _arch, _shop = _read_review(client, session, store)

    refused = _start(client, package_id, SHOP_ONE, arch=b"%PDF-1.7 synthetic architect, v2\n")

    assert refused.status_code == 422
    assert "Only the architect's drawings changed" in refused.json()["message"]


def test_a_write_from_a_screen_showing_revision_one_is_refused(
    session: Session, store: LocalStore
) -> None:
    client = _client(session, store)
    package_id, _arch, _shop = _read_review(client, session, store)
    assert _start(client, package_id, SHOP_TWO).status_code == 201

    stale = client.post(f"{_base(package_id)}/extract", headers={"X-GV-Revision": "1"})
    current = client.post(f"{_base(package_id)}/extract", headers={"X-GV-Revision": "2"})
    read = client.get(f"{_base(package_id)}/revisions", headers={"X-GV-Revision": "1"})

    assert stale.status_code == 409
    assert "You are looking at revision 1, which is read-only" in stale.json()["message"]
    # The newest revision's screen reaches the route (which refuses an unchanged set on its own).
    assert current.status_code == 422
    assert read.status_code == 200


def test_evidence_decisions_and_exceptions_on_revision_one_are_refused(
    session: Session, store: LocalStore
) -> None:
    from fractions import Fraction as Exact

    from app.review.evidence_actions import correct_evidence
    from app.review.session import RevisionSuperseded

    client = _client(session, store)
    package_id, _arch, _shop = _read_review(client, session, store)
    one = _revision(session, package_id, 1)
    sitting = client.post(
        f"{_base(package_id)}/review-sessions", json={"package_revision_id": str(one.id)}
    ).json()["id"]
    assert _start(client, package_id, SHOP_TWO).status_code == 201
    base = f"/api/v1/projects/{PROJECT}/review-sessions/{sitting}"

    confirm = client.post(
        f"{base}/evidence",
        json={"finding_id": str(uuid4()), "observation_id": str(uuid4()), "action": "confirm"},
    )
    exception = client.post(
        f"{base}/exceptions",
        json={
            "finding_id": str(uuid4()),
            "scope": "package",
            "scope_id": str(one.id),
            "reason": "synthetic",
            "expires_at": "2099-01-01T00:00:00Z",
        },
    )

    replaced = "A newer revision replaced this one"
    assert confirm.status_code == 409 and replaced in confirm.json()["message"]
    assert exception.status_code == 409 and replaced in exception.json()["message"]
    with pytest.raises(RevisionSuperseded):
        correct_evidence(
            session,
            principal=Principal(
                id="synthetic-reviewer",
                roles=frozenset({Role.ADMIN}),
                projects=frozenset({PROJECT}),
            ),
            review_session_id=UUID(sitting),
            finding_id=uuid4(),
            observation_id=uuid4(),
            corrected_value=Exact(30),
        )


# --- listing ---------------------------------------------------------------------------------------


def test_revisions_are_listed_oldest_first_with_what_each_holds(
    session: Session, store: LocalStore
) -> None:
    client = _client(session, store)
    package_id, arch, shop = _read_review(client, session, store)
    one = _revision(session, package_id, 1)
    session.add(Approval(package_revision_id=one.id, approved_by="synthetic-signer"))
    session.commit()
    _start(client, package_id, SHOP_TWO)

    listed = client.get(f"{_base(package_id)}/revisions")

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["current_revision_number"] == 2
    first, second = body["items"]
    assert (first["revision_number"], first["read_only"], first["current"]) == (1, True, False)
    assert (second["revision_number"], second["read_only"], second["current"]) == (2, False, True)
    assert first["state"] == "SUPERSEDED" and first["superseded_by_revision_number"] == 2
    assert first["signed_off_by"] == "synthetic-signer" and first["signed_off_at"] is not None
    assert second["signed_off_at"] is None
    carried = {d["document_id"]: d["same_as_revision"] for d in second["drawings"]}
    # Until the re-issue is confirmed, both drawings are revision 1's own files.
    assert carried == {arch: 1, shop: 1}
    assert all(d["same_as_revision"] is None for d in first["drawings"])


# --- auth and isolation --------------------------------------------------------------------------------


def test_starting_a_revision_needs_the_upload_permission(
    session: Session, store: LocalStore
) -> None:
    package_id, _arch, _shop = _read_review(_client(session, store), session, store)
    # Starting a revision is starting a review (#1182 START_REVIEW): reviewers and admins only.
    rule_admin = _client(session, store, roles=frozenset({Role.RULE_ADMIN}))

    # A role that may not upload is refused like everything else here: as if nothing were there.
    assert _start(rule_admin, package_id, SHOP_TWO).status_code == 404
    assert _revision(session, package_id, 1).state == PackageState.NEEDS_INPUT
    assert rule_admin.get(f"{_base(package_id)}/revisions").status_code == 200

    reviewer = _client(session, store, roles=frozenset({Role.REVIEWER}))
    assert _start(reviewer, package_id, SHOP_TWO).status_code == 201


def test_another_projects_package_is_not_found(session: Session, store: LocalStore) -> None:
    client = _client(session, store)
    elsewhere = _new_package(session, project_id=uuid4())

    for response in (
        _start(client, elsewhere, SHOP_TWO),
        client.get(f"{_base(elsewhere)}/revisions"),
        client.get(f"{_base(elsewhere)}/revisions/compare"),
    ):
        assert response.status_code == 404
        assert response.json()["message"] == "Not found"


# --- run-scope values carried, measurements not ---------------------------------------------------------


def test_the_jobs_own_values_are_carried_but_drawing_measurements_are_not(
    session: Session, store: LocalStore
) -> None:
    client = _client(session, store)
    package_id, _arch, _shop = _read_review(client, session, store)
    one = _revision(session, package_id, 1)
    values = InMemoryParameterSet(
        project_id=str(PROJECT),
        layer=ParameterLayer.RUN,
        version=1,
        parameters={
            "sink_interior_width": ParameterValue(
                value=Quantity(value=Fraction(30), unit=Unit.INCH),
                provenance=Provenance.MEASURED,
                set_by="synthetic-reviewer",
                set_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
            "CT-WIDTH-001:countertop_width": ParameterValue(
                value=Quantity(value=Fraction(96), unit=Unit.INCH),
                provenance=Provenance.MEASURED,
                set_by="synthetic-reviewer",
                set_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        },
    )
    stored, rows = to_rows(values, package_revision_id=one.id)
    session.add(stored)
    session.flush()
    session.add_all(rows)
    session.commit()

    started = _start(client, package_id, SHOP_TWO)

    assert started.json()["carried_values"] == 1
    two = _revision(session, package_id, 2)
    carried = session.scalars(
        select(StoredParameterValue)
        .join(StoredParameterSet, StoredParameterSet.id == StoredParameterValue.parameter_set_id)
        .where(StoredParameterSet.package_revision_id == two.id)
    ).all()
    assert [(value.name, value.set_by) for value in carried] == [
        ("sink_interior_width", "synthetic-reviewer")
    ]


# --- carry forward after reading: walls and roles -------------------------------------------------------


def _version(session: Session, document: Document, data: bytes) -> DocumentVersion:
    digest = _sha(data)
    artifact = SourceArtifact(storage_key=f"synthetic/{uuid4()}", sha256=digest, size=len(data))
    session.add(artifact)
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256=digest, page_count=2
    )
    session.add(version)
    session.flush()
    return version


def _pages(session: Session, version: DocumentVersion, hashes: tuple[str, ...]) -> list[Page]:
    pages = [
        Page(
            document_version_id=version.id,
            index=index,
            content_hash=content_hash,
            width_pt=Decimal(612),
            height_pt=Decimal(792),
            rotation=0,
            has_vector_text=True,
        )
        for index, content_hash in enumerate(hashes)
    ]
    session.add_all(pages)
    session.flush()
    return pages


def _reader_run(session: Session, revision_id: UUID, version: str = "slot-reader-v1-test") -> UUID:
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
        extractor_version=version,
        config_hash="synthetic-test",
        dpi=150,
    )
    session.add(run)
    session.flush()
    return run.id


def _row(
    session: Session,
    run_id: UUID,
    page: Page,
    *,
    rank: str,
    widths: tuple[int, ...],
    top: int = 0,
) -> UUID:
    anchor: UUID | None = None
    slots = [(str(i), width) for i, width in enumerate(widths)] + [("overall", sum(widths))]
    for slot, numerator in slots:
        candidate = ObservationCandidate(
            document_version_id=page.document_version_id,
            page_id=page.id,
            extraction_run_id=run_id,
            raw_text=f"{numerator} inch",
            value_numerator=numerator,
            value_denominator=1,
            unit="in",
            polygon=[[0, top], [10, top], [10, top + 10], [0, top + 10]],
            coordinate_space="image",
            ambiguity_flags=[
                "slot-reader",
                f"slot:{slot}",
                f"row-rank:{rank}",
                f"row-slot-count:{len(widths)}",
            ],
        )
        session.add(candidate)
        session.flush()
        if slot == "0":
            anchor = candidate.id
    assert anchor is not None
    return anchor


def _two_revisions(session: Session) -> tuple[PackageRevision, PackageRevision, Document]:
    """Revision 1 and its successor, each holding its own issue of one shop drawing whose first
    page is byte-identical and whose second page changed."""
    package_id = _new_package(session)
    one = _revision(session, package_id, 1)
    document = Document(package_id=package_id, kind=DocumentKind.SHOP.value)
    session.add(document)
    session.flush()
    first = _version(session, document, SHOP_ONE)
    second = _version(session, document, SHOP_TWO)
    session.add(
        PackageRevisionDocument(
            package_revision_id=one.id,
            package_id=package_id,
            document_id=document.id,
            document_version_id=first.id,
        )
    )
    session.flush()
    one.state = PackageState.NEEDS_INPUT.value
    two = PackageRevision(
        package_id=package_id, revision_number=2, state="UPLOADED", supersedes_id=one.id
    )
    session.add(two)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=two.id,
            package_id=package_id,
            document_id=document.id,
            document_version_id=second.id,
        )
    )
    session.flush()
    return one, two, document


def test_wall_answers_carry_only_to_the_same_row_on_an_unchanged_page(session: Session) -> None:
    one, two, document = _two_revisions(session)
    versions = {
        version.id: version
        for version in session.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id == document.id)
        )
    }
    first, second = (
        next(v for v in versions.values() if v.sha256 == _sha(SHOP_ONE)),
        next(v for v in versions.values() if v.sha256 == _sha(SHOP_TWO)),
    )
    old_pages = _pages(session, first, ("1" * 64, "2" * 64))
    new_pages = _pages(session, second, ("1" * 64, "3" * 64))
    run_one = _reader_run(session, one.id)
    same_old = _row(session, run_one, old_pages[0], rank="1", widths=(30, 40))
    moved_old = _row(session, run_one, old_pages[1], rank="2", widths=(30, 40))
    for anchor, wall in ((same_old, "back_only"), (moved_old, "island")):
        session.add(
            SlotRowReviewDecision(
                row_candidate_id=anchor,
                wall_config=wall,
                measurements={"countertop_width": "70"},
                confirmed_by="synthetic-reviewer",
            )
        )
    run_two = _reader_run(session, two.id)
    same_new = _row(session, run_two, new_pages[0], rank="1", widths=(30, 40))
    changed_new = _row(session, run_two, new_pages[1], rank="2", widths=(30, 40))
    session.commit()

    report = carry_after_reading(session, two.id)
    again = carry_after_reading(session, two.id)

    assert report.wall_answers == 1 and again.wall_answers == 0
    rows = {row.anchor.id: row for row in slot_rows(session, two.id)}
    carried = rows[same_new].decision
    assert carried is not None
    assert (carried.wall_config, carried.confirmed_by) == ("back_only", "synthetic-reviewer")
    assert carried.carried_from_id is not None
    assert rows[changed_new].decision is None


def test_a_row_read_differently_is_asked_again(session: Session) -> None:
    one, two, document = _two_revisions(session)
    first, second = session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.created_at)
    ).all()
    old_page = _pages(session, first, ("1" * 64,))[0]
    new_page = _pages(session, second, ("1" * 64,))[0]
    anchor = _row(session, _reader_run(session, one.id), old_page, rank="1", widths=(30, 40))
    session.add(
        SlotRowReviewDecision(
            row_candidate_id=anchor,
            wall_config="back_only",
            measurements={},
            confirmed_by="synthetic-reviewer",
        )
    )
    _row(session, _reader_run(session, two.id), new_page, rank="1", widths=(30, 41))
    session.commit()

    assert carry_after_reading(session, two.id).wall_answers == 0


def test_a_persons_drawing_role_carries_to_the_identical_page_only(session: Session) -> None:
    _one, two, document = _two_revisions(session)
    first, second = session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.created_at)
    ).all()
    old_pages = _pages(session, first, ("1" * 64, "2" * 64))
    new_pages = _pages(session, second, ("1" * 64, "3" * 64))
    old_views = [
        DrawingView(page_id=page.id, tag="A", region={}, role="arch") for page in old_pages
    ]
    coded = DrawingView(page_id=old_pages[0].id, tag="B", region={}, role="shop")
    session.add_all((*old_views, coded))
    session.flush()
    session.add_all(
        [
            ViewRoleConfirmation(
                drawing_view_id=view.id, role="arch", confirmed_by="synthetic-reviewer"
            )
            for view in old_views
        ]
        + [ViewRoleConfirmation(drawing_view_id=coded.id, role="shop", confirmed_by=CODE_CONFIRMER)]
    )
    new_views = [DrawingView(page_id=page.id, tag="A", region={}) for page in new_pages]
    new_coded = DrawingView(page_id=new_pages[0].id, tag="B", region={})
    session.add_all((*new_views, new_coded))
    session.commit()

    report = carry_after_reading(session, two.id)

    assert report.drawing_roles == 1
    session.expire_all()
    assert session.get(DrawingView, new_views[0].id).role == "arch"  # type: ignore[union-attr]
    assert session.get(DrawingView, new_views[1].id).role is None  # type: ignore[union-attr]
    assert session.get(DrawingView, new_coded.id).role is None  # type: ignore[union-attr]
    carried = session.scalars(
        select(ViewRoleConfirmation).where(ViewRoleConfirmation.drawing_view_id == new_views[0].id)
    ).one()
    assert carried.confirmed_by == "synthetic-reviewer" and carried.carried_from_id is not None


def test_an_unchanged_drawing_keeps_its_views_and_roles(
    session: Session, store: LocalStore
) -> None:
    """A carried drawing is the same version, so its pages and views are the same rows."""
    client = _client(session, store)
    package_id, arch, _shop = _read_review(client, session, store)
    one = _revision(session, package_id, 1)
    version = _members(session, one.id)[UUID(arch)]
    page = _pages(session, session.get(DocumentVersion, version), ("4" * 64,))[0]  # type: ignore[arg-type]
    session.add(DrawingView(page_id=page.id, tag="A", region={}, role="arch"))
    session.commit()

    _start(client, package_id, SHOP_TWO)

    two = _revision(session, package_id, 2)
    assert _members(session, two.id)[UUID(arch)] == version
    from workflow.view_roles import revision_views

    assert [entry.view.role for entry in revision_views(session, two.id)] == ["arch"]


def test_a_persons_architect_view_pick_on_revision_one_is_remembered_on_revision_two(
    session: Session,
) -> None:
    """#1166's own carry (D3) walks `supersedes_id`: a reviewer's pick on revision 1 is what
    revision 2's matching remembers for the same vendor item."""
    from app.models import ArchitectViewMatchRecord

    one, two, document = _two_revisions(session)
    first = session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.created_at)
    ).first()
    assert first is not None
    page = _pages(session, first, ("1" * 64,))[0]
    anchor = _row(session, _reader_run(session, one.id), page, rank="1", widths=(30, 40))
    item_key = {"page_content_hash": "1" * 64, "page_index": 0, "pieces": 2, "row_y_pt": "2.4"}
    pick = ArchitectViewMatchRecord(
        package_revision_id=one.id,
        vendor_page_id=page.id,
        row_anchor_candidate_id=anchor,
        source="reviewer",
        status="none_matches",
        ai_picks=[],
        candidates=[],
        vendor_references=[],
        reasons=[],
        details={"vendor_item_key": item_key, "architect_files": ["b" * 64]},
        decided_by="synthetic-reviewer",
    )
    session.add(pick)
    session.commit()

    remembered = remembered_matches(session, two.id)

    assert [(m.record_id, m.revision_number, m.status) for m in remembered] == [
        (pick.id, 1, "none_matches")
    ]
    assert remembered[0].vendor_item_key["page_content_hash"] == "1" * 64


# --- what changed ------------------------------------------------------------------------------------


def test_what_changed_lists_pages_and_countertops(session: Session, store: LocalStore) -> None:
    client = _client(session, store)
    package_id, arch, shop = _read_review(client, session, store)
    one = _revision(session, package_id, 1)
    started = _start(client, package_id, SHOP_TWO).json()
    _send_and_confirm(client, store, started["uploads"][0], SHOP_TWO)
    two = _revision(session, package_id, 2)
    old_members, new_members = _members(session, one.id), _members(session, two.id)
    arch_version = session.get(DocumentVersion, old_members[UUID(arch)])
    old_shop = session.get(DocumentVersion, old_members[UUID(shop)])
    new_shop = session.get(DocumentVersion, new_members[UUID(shop)])
    assert arch_version is not None and old_shop is not None and new_shop is not None

    unread = client.get(f"{_base(package_id)}/revisions/compare")
    assert unread.status_code == 200, unread.text
    shop_change = next(d for d in unread.json()["drawings"] if d["kind"] == "shop")
    assert shop_change["compared"] is False and "Not read yet" in shop_change["reason"]

    _pages(session, arch_version, ("a" * 64, "b" * 64))
    old_pages = _pages(session, old_shop, ("1" * 64, "2" * 64))
    new_pages = _pages(session, new_shop, ("1" * 64, "3" * 64, "4" * 64))
    old_run, new_run = _reader_run(session, one.id), _reader_run(session, two.id)
    _row(session, old_run, old_pages[0], rank="1", widths=(30, 40))
    _row(session, old_run, old_pages[1], rank="2", widths=(30, 40))
    _row(session, new_run, new_pages[0], rank="1", widths=(30, 40))
    _row(session, new_run, new_pages[2], rank="3", widths=(50,))
    session.commit()

    compared = client.get(f"{_base(package_id)}/revisions/compare?before=1&after=2")

    assert compared.status_code == 200, compared.text
    body = compared.json()
    assert (body["before_revision_number"], body["after_revision_number"]) == (1, 2)
    assert (body["pages_added"], body["pages_removed"], body["pages_changed"]) == (1, 0, 1)
    assert body["pages_unchanged"] == 3
    shop_pages = {
        page["page_number"]: page["change"]
        for drawing in body["drawings"]
        if drawing["kind"] == "shop"
        for page in drawing["pages"]
    }
    assert shop_pages == {1: "unchanged", 2: "changed", 3: "added"}
    architect = next(d for d in body["drawings"] if d["kind"] == "architectural")
    assert architect["same_file"] is True
    changes = {(row["page_number"], row["change"]) for row in body["countertops"]}
    assert changes == {(1, "same"), (2, "gone"), (3, "new")}
    assert body["countertops_checked"] is False


def test_what_changed_refuses_comparing_backwards(session: Session, store: LocalStore) -> None:
    client = _client(session, store)
    package_id, _arch, _shop = _read_review(client, session, store)
    _start(client, package_id, SHOP_TWO)

    assert client.get(f"{_base(package_id)}/revisions/compare?before=2&after=1").status_code == 422
    assert client.get(f"{_base(package_id)}/revisions/compare?before=1&after=9").status_code == 404


def test_an_earlier_revisions_countertop_results_can_be_read(
    session: Session, store: LocalStore
) -> None:
    client = _client(session, store)
    package_id, _arch, _shop = _read_review(client, session, store)
    one = _revision(session, package_id, 1)
    _start(client, package_id, SHOP_TWO)

    earlier = client.get(f"{_base(package_id)}/countertop-results?revision=1")
    newest = client.get(f"{_base(package_id)}/countertop-results")
    missing = client.get(f"{_base(package_id)}/countertop-results?revision=7")

    assert earlier.status_code == 200 and earlier.json()["revision_id"] == str(one.id)
    assert newest.json()["revision_id"] == str(_revision(session, package_id, 2).id)
    assert missing.status_code == 404


def test_the_carry_forward_migration_is_the_single_head() -> None:
    from alembic.script import ScriptDirectory

    from tests.app.postgres_fixture import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    found = script.get_revision("0089_revision_carry_forward")
    assert found is not None and found.down_revision == "0088_outbox_abandoned"
    # #1191 added 0090 on top, #1193 0091, #1195 0092, #1206 0093 and #1215 0094; 0089 is still
    # in the chain below them. Later migrations (#1238's 0096) may follow, on one line of history.
    (head,) = script.get_heads()
    assert "0094_invocation_failure_reason" in {
        rev.revision for rev in script.walk_revisions("base", head)
    }
    above = script.get_revision("0090_no_countertop_answers")
    assert above is not None and above.down_revision == found.revision
    reference = script.get_revision("0091_reference_view_suggestion")
    assert reference is not None and reference.down_revision == above.revision
    batches = script.get_revision("0092_no_countertop_batches")
    assert batches is not None and batches.down_revision == reference.revision
    refusals = script.get_revision("0093_model_invocation_refusals")
    assert refusals is not None and refusals.down_revision == batches.revision
    top = script.get_revision("0094_invocation_failure_reason")
    assert top is not None and top.down_revision == refusals.revision


# --- carry-forward: the identity is strict ----------------------------------------------------------


def _versions(session: Session, document: Document) -> tuple[DocumentVersion, DocumentVersion]:
    first, second = session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.created_at)
    ).all()
    return first, second


def _answer(session: Session, anchor: UUID, wall: str = "back_only") -> None:
    session.add(
        SlotRowReviewDecision(
            row_candidate_id=anchor,
            wall_config=wall,
            measurements={},
            confirmed_by="synthetic-reviewer",
        )
    )


def test_two_identical_earlier_rows_carry_nothing(session: Session) -> None:
    one, two, document = _two_revisions(session)
    first, second = _versions(session, document)
    old_page = _pages(session, first, ("1" * 64,))[0]
    new_page = _pages(session, second, ("1" * 64,))[0]
    run_one = _reader_run(session, one.id)
    # Two earlier rows indistinguishable by every part of the identity.
    _answer(session, _row(session, run_one, old_page, rank="1", widths=(30, 40)), "back_only")
    _answer(session, _row(session, run_one, old_page, rank="1", widths=(30, 40)), "island")
    _row(session, _reader_run(session, two.id), new_page, rank="1", widths=(30, 40))
    session.commit()

    assert carry_after_reading(session, two.id).wall_answers == 0


@pytest.mark.parametrize("unknown", ["", hashlib.sha256(b"").hexdigest()])
def test_a_page_whose_content_is_unknown_never_carries(session: Session, unknown: str) -> None:
    from app.lifecycle.carry_forward import known_content, row_identity

    one, two, document = _two_revisions(session)
    first, second = _versions(session, document)
    empty = hashlib.sha256(b"").hexdigest()
    old_page = _pages(session, first, (empty,))[0]
    new_page = _pages(session, second, (empty,))[0]
    _answer(
        session,
        _row(session, _reader_run(session, one.id), old_page, rank="1", widths=(30, 40)),
    )
    _row(session, _reader_run(session, two.id), new_page, rank="1", widths=(30, 40))
    session.commit()

    assert carry_after_reading(session, two.id).wall_answers == 0
    assert not known_content(unknown)
    assert row_identity(slot_rows(session, two.id)[0], unknown, "slot-reader-v1-test") is None


def test_another_reader_or_another_line_carries_nothing(session: Session) -> None:
    one, two, document = _two_revisions(session)
    first, second = _versions(session, document)
    old_pages = _pages(session, first, ("1" * 64, "2" * 64))
    new_pages = _pages(session, second, ("1" * 64, "2" * 64))
    run_one = _reader_run(session, one.id)
    _answer(session, _row(session, run_one, old_pages[0], rank="1", widths=(30, 40)))
    _answer(session, _row(session, run_one, old_pages[1], rank="2", widths=(30, 40), top=100))
    # Page 1 read by another reader version; page 2's row line 50 px (24 pt at 150 dpi) lower.
    _row(
        session,
        _reader_run(session, two.id, version="slot-reader-v2-test"),
        new_pages[0],
        rank="1",
        widths=(30, 40),
    )
    _row(session, _reader_run(session, two.id), new_pages[1], rank="2", widths=(30, 40), top=150)
    session.commit()

    assert carry_after_reading(session, two.id).wall_answers == 0


# --- carried answers are visible ----------------------------------------------------------------------


def test_carried_answers_name_their_revision_everywhere(
    session: Session, store: LocalStore
) -> None:
    from reports.findings_pdf import _wall_source

    one, two, document = _two_revisions(session)
    first, second = _versions(session, document)
    old_page = _pages(session, first, ("1" * 64,))[0]
    new_page = _pages(session, second, ("1" * 64,))[0]
    _answer(
        session,
        _row(session, _reader_run(session, one.id), old_page, rank="1", widths=(30, 40)),
    )
    _row(session, _reader_run(session, two.id), new_page, rank="1", widths=(30, 40))
    views = [
        DrawingView(page_id=old_page.id, tag="A", region={}, role="arch"),
        DrawingView(page_id=new_page.id, tag="A", region={}),
    ]
    session.add_all(views)
    session.flush()
    session.add(
        ViewRoleConfirmation(
            drawing_view_id=views[0].id, role="arch", confirmed_by="synthetic-reviewer"
        )
    )
    session.commit()
    assert carry_after_reading(session, two.id).wall_answers == 1
    session.commit()
    client = _client(session, store)
    base = _base(one.package_id)

    rows = client.get(f"{base}/slot-rows").json()["rows"]
    results = client.get(f"{base}/countertop-results").json()["items"]
    roles = client.get(f"{base}/views").json()["views"]

    assert [(r["carried_from_revision"], r["confirmed_by"]) for r in rows] == [
        (1, "synthetic-reviewer")
    ]
    assert rows[0]["carried_given_at"] is not None
    wall = results[0]["wall_layout"]
    assert (wall["source"], wall["carried_from_revision"], wall["carried_by"]) == (
        "reviewer",
        1,
        "synthetic-reviewer",
    )
    carried_view = next(v for v in roles if v["role"] == "arch")
    assert carried_view["role_carried_from_revision"] == 1
    from app.api.visual_countertops import _countertop_results_for_revision

    item = _countertop_results_for_revision(session, one.package_id, two).items[0]
    assert _wall_source(item) == "reviewer (revision 1) · synthetic-reviewer"


def test_a_carried_job_value_says_which_revision_it_came_from(
    session: Session, store: LocalStore
) -> None:
    from app.api.measurements import _carried_from

    client = _client(session, store)
    package_id, _arch, _shop = _read_review(client, session, store)
    _start(client, package_id, SHOP_TWO)
    one, two = _revision(session, package_id, 1), _revision(session, package_id, 2)
    number = _carried_from(session, two)

    assert number(one.created_at) == 1
    assert number(two.created_at) is None
    assert _carried_from(session, one)(one.created_at) is None


# --- the worker hook ------------------------------------------------------------------------------------


def test_the_worker_carries_answers_right_after_reading_the_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.drain_outbox as worker
    import workflow.review as review_module
    from app.lifecycle import side_states
    from workflow import sutradhar

    order: list[str] = []

    class Outcome:
        def __init__(self) -> None:
            self.payload: dict[str, object] = {}

    def run_stage(_session: object, *, stage: str, **_kwargs: object) -> Outcome:
        order.append(stage)
        return Outcome()

    monkeypatch.setattr(worker, "_stages", lambda **_kwargs: object())
    # The flow's steps live in `workflow/sutradhar.py` since #1227, so they are replaced there.
    monkeypatch.setattr(sutradhar, "ensure_workflow_run", lambda *args: None)
    monkeypatch.setattr(review_module, "run_stage", run_stage)
    monkeypatch.setattr(
        sutradhar, "carry_forward", lambda *_args: order.append("carry") or {"wall_answers": 0}
    )
    monkeypatch.setattr(sutradhar, "build_package_text", lambda *_args: {})
    monkeypatch.setattr(sutradhar, "hunt_package_values", lambda *_args: {})
    monkeypatch.setattr(sutradhar, "propose_measurements", lambda *_args: {})
    monkeypatch.setattr(side_states, "enter_needs_input", lambda *_args, **_kwargs: None)
    from app.runs import reading_progress

    monkeypatch.setattr(reading_progress, "side_engine", lambda *_args: None)
    monkeypatch.setattr(reading_progress, "record_progress", lambda *_args, **_kwargs: None)

    class StageSession:
        """Each stage commits as it ends (#1178); the commits are recorded in the same order."""

        def commit(self) -> None:
            order.append("commit")

        def rollback(self) -> None:
            order.append("rollback")

        def get_bind(self) -> None:
            return None

    results = worker._extract_package(StageSession(), uuid4(), str(uuid4()))

    # Carried inside the reading's own transaction: the reading and its carried answers commit
    # together, so a crash cannot leave a read revision without the answers it should have taken.
    assert order == [
        "commit",
        "ingest",
        "commit",
        "extract_pages",
        "carry",
        "commit",
        "match",
        "commit",
        "validate_evidence",
    ]
    assert results["carry_forward"] == {"wall_answers": 0}


def test_a_failed_carry_never_fails_the_reading(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.lifecycle.carry_forward as carry
    import scripts.drain_outbox as worker

    def broken(_session: object, _revision_id: UUID) -> None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(carry, "carry_after_reading", broken)
    one, two, _document = _two_revisions(session)

    reported = worker._carry_forward(session, two.id)

    assert reported == {"ran": False, "reason": "carrying answers forward failed: RuntimeError"}
    # The session is still usable: the savepoint was rolled back, not the reading.
    assert session.get(PackageRevision, one.id) is not None


# --- what changed: pairing -------------------------------------------------------------------------------


def test_pages_pair_by_content_then_sheet_then_place() -> None:
    from app.api.revisions import _PageFacts, pair_pages

    empty = hashlib.sha256(b"").hexdigest()
    old = [
        _PageFacts(0, "a" * 64, "A-1"),
        _PageFacts(1, "b" * 64, "A-2"),
        _PageFacts(2, "c" * 64, None),
        _PageFacts(3, empty, None),
    ]
    new = [
        _PageFacts(0, "f" * 64, None),  # a cover sheet inserted in front
        _PageFacts(1, "a" * 64, "A-1"),  # moved down one
        _PageFacts(2, "d" * 64, "A-2"),  # same sheet, new content
        _PageFacts(3, empty, None),  # unknown content: never "the same"
    ]

    pairs = {
        (None if b is None else b.index, None if a is None else a.index): change
        for b, a, change in pair_pages(old, new)
    }

    assert pairs[(0, 1)] == "moved"
    assert pairs[(1, 2)] == "changed"
    assert pairs[(3, 3)] == "changed"
    assert pairs[(None, 0)] == "added"
    assert pairs[(2, None)] == "removed"


def test_what_changed_waits_for_the_new_revision_to_be_read(
    session: Session, store: LocalStore
) -> None:
    client = _client(session, store)
    package_id, _arch, shop = _read_review(client, session, store)
    one = _revision(session, package_id, 1)
    old_shop = session.get(DocumentVersion, _members(session, one.id)[UUID(shop)])
    assert old_shop is not None
    page = _pages(session, old_shop, ("1" * 64,))[0]
    _row(session, _reader_run(session, one.id), page, rank="1", widths=(30, 40))
    session.commit()
    _start(client, package_id, SHOP_TWO)

    compared = client.get(f"{_base(package_id)}/revisions/compare").json()

    assert compared["countertops_read"] is False
    assert compared["countertops"] == []


# --- sign-off: carried answers are confirmed by the signer, on the server ----------------------------


def _scoped_finding(session: Session, revision: PackageRevision, row_id: UUID) -> None:
    from app.models import CheckRun, Finding, RuleDefinition, RuleSnapshot

    definition = RuleDefinition(rule_id=f"CT-{uuid4().hex[:6]}")
    session.add(definition)
    session.flush()
    body = f'{{"id":"{definition.rule_id}"}}'
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(body.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=body,
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    run = CheckRun(
        package_revision_id=revision.id, rule_snapshot_id=snapshot.id, engine_version="test"
    )
    session.add(run)
    session.flush()
    session.add(
        Finding(
            check_run_id=run.id,
            package_revision_id=revision.id,
            outcome="PASS",
            severity="FLAG",
            trace={},
            parameter_set_versions={},
            scope_row_candidate_id=row_id,
            scope_label="Synthetic countertop row",
        )
    )
    session.flush()


def _ready_to_sign(session: Session, *, carried: bool) -> tuple[PackageRevision, UUID]:
    """Revision 2, checked and waiting for sign-off; its one countertop result rests on a wall
    answer carried from revision 1 (or, with `carried=False`, on one given on revision 2)."""
    from app.review.session import open_session

    one, two, document = _two_revisions(session)
    first, second = _versions(session, document)
    old_page = _pages(session, first, ("1" * 64,))[0]
    new_page = _pages(session, second, ("1" * 64,))[0]
    old_row = _row(session, _reader_run(session, one.id), old_page, rank="1", widths=(30, 40))
    new_row = _row(session, _reader_run(session, two.id), new_page, rank="1", widths=(30, 40))
    _answer(session, old_row if carried else new_row)
    session.commit()
    carry_after_reading(session, two.id)
    _scoped_finding(session, two, new_row)
    two.state = PackageState.AWAITING_REVIEW.value
    session.flush()
    review = open_session(session, package_revision_id=two.id, reviewer="synthetic-reviewer")
    session.commit()
    return two, review.id


def _signer() -> Principal:
    return Principal(
        id="synthetic-signer", roles=frozenset({Role.ADMIN}), projects=frozenset({PROJECT})
    )


def test_sign_off_is_refused_until_carried_answers_are_confirmed(
    session: Session, store: LocalStore
) -> None:
    _two, review_id = _ready_to_sign(session, carried=True)
    client = _client(session, store)

    refused = client.post(f"/api/v1/projects/{PROJECT}/review-sessions/{review_id}/approve")

    assert refused.status_code == 409
    message = refused.json()["message"]
    assert "Answers carried from revision 1 must be confirmed before sign-off" in message
    assert "wall answer from revision 1 (synthetic-reviewer)" in message


def test_a_confirmed_sign_off_records_who_confirmed_in_the_signed_record(
    session: Session,
) -> None:
    from app.models.signed_exports import ApprovalExportSnapshot
    from app.review.approval import approve_package
    from app.review.signed_exports import load_snapshot

    two, review_id = _ready_to_sign(session, carried=True)

    decision = approve_package(
        session, principal=_signer(), review_session_id=review_id, carried_answers_confirmed=True
    )
    session.commit()

    snapshot = session.scalars(
        select(ApprovalExportSnapshot).where(
            ApprovalExportSnapshot.approval_id == decision.approval.id
        )
    ).one()
    review = load_snapshot(session, snapshot).review
    assert review.carried_answers_confirmation is not None
    assert "confirmed to still apply by synthetic-signer" in review.carried_answers_confirmation
    assert review.carried_answers_confirmation in review.signoff
    assert session.get(PackageRevision, two.id).state == PackageState.APPROVED  # type: ignore[union-attr]


def test_nothing_carried_needs_no_confirmation(session: Session) -> None:
    from app.models.signed_exports import ApprovalExportSnapshot
    from app.review.approval import approve_package
    from app.review.signed_exports import load_snapshot

    _two, review_id = _ready_to_sign(session, carried=False)

    decision = approve_package(session, principal=_signer(), review_session_id=review_id)
    session.commit()

    snapshot = session.scalars(
        select(ApprovalExportSnapshot).where(
            ApprovalExportSnapshot.approval_id == decision.approval.id
        )
    ).one()
    assert load_snapshot(session, snapshot).review.carried_answers_confirmation is None
