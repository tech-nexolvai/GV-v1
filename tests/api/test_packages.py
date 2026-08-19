"""Packages, documents, upload tickets and confirmation (#205, C2.3).

Four guarantees, and the three that matter most are the ones a green CI run says nothing about:

**The API never receives file bytes.** Asserted by walking every route's signature rather than by
reviewing each new endpoint — `DESIGN_PLATFORM.md` §4.2: "a guard that each author must remember is a
guard that will eventually be forgotten." The test below is written so that *adding* a file body makes
it fail, not so that today's routes make it pass.

**The version and the ingestion request commit together.** The hard part is proving it rather than
observing it: a test that confirms both rows appear on the happy path passes just as well against an
implementation that commits twice. So the failure is induced *between* them, and the assertion is that
**neither** survives.

**An upload ticket is scoped, single-purpose and expiring.** Each of the three is a separate refusal,
because a ticket that is only two of the three is the interesting bug.

The fourth — re-uploading makes a new version rather than replacing one — is `AGENTS.md` §2.7, and
`DocumentVersion` is `Immutable` so the database refuses the alternative outright.
"""

from __future__ import annotations

import hashlib
import inspect
import io
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi import FastAPI, UploadFile
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_artifact_store, get_session
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models.document import DocumentKind, DocumentVersion
from app.models.package import Package, PackageRevision, PackageState, Project
from storage.local import LocalStore
from storage.signing import CapabilityInvalid, sign_capability, verify_capability
from storage.store import UPLOAD_PURPOSE, UploadTicket

pytest_plugins = ("tests.app.postgres_fixture",)

DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"
SECRET = b"a-test-signing-secret-that-is-long-enough"


def _settings() -> Settings:
    return Settings(database_url=DATABASE_URL)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# The API never receives file bytes
# ---------------------------------------------------------------------------


def _request_annotations(app: FastAPI) -> list[tuple[str, str, Any]]:
    """Every parameter of every route endpoint, with the path it belongs to.

    Walks the mounted route table rather than a list of modules, so a router added later is covered
    without anybody remembering to add it here — `include_router` hides its children from
    `app.routes`, which is why this goes through the same enumeration the authorisation audit uses.
    """
    from tests.api.test_authorisation import _mounted_routes

    found: list[tuple[str, str, Any]] = []
    for mounted in _mounted_routes(app):
        signature = inspect.signature(mounted.route.endpoint)
        for name, parameter in signature.parameters.items():
            found.append((mounted.path, name, parameter.annotation))
    return found


def _mentions_bytes(annotation: Any) -> bool:
    """Whether an annotation is, or contains, a file or raw-bytes body.

    Rendered to text on purpose. `Annotated[UploadFile, File()]`, `list[UploadFile]`,
    `UploadFile | None` and a bare `bytes` are all different objects and all the same mistake, and
    matching the spelling catches shapes this test's author did not think of.
    """
    rendered = str(annotation)
    return any(
        marker in rendered
        for marker in ("UploadFile", "bytes", "StreamingBody", "File(", "SpooledTemporaryFile")
    )


def test_no_route_accepts_file_bytes() -> None:
    """The acceptance criterion, as a guard over the whole route table.

    Backend §4.1: the control plane does short work only, and uploads go straight to storage. A route
    that accepts bytes puts one 8 GB VM's request path in competition with PostgreSQL and OCR for
    memory — and it will be added by somebody who has not read §4.1, which is the whole reason this is
    a test and not a convention.
    """
    offenders = [
        f"{path}({name}: {annotation})"
        for path, name, annotation in _request_annotations(create_app(_settings()))
        if _mentions_bytes(annotation)
    ]
    assert not offenders, (
        f"these routes accept file bytes: {offenders}. Uploads go directly to storage with a ticket; "
        "the API must never be in the byte path."
    )


def test_the_no_file_bytes_guard_actually_catches_one() -> None:
    """**Without this, the test above is a green tick that proves nothing.**

    An enumeration asserting an empty list looks identical whether it is working or looking in the
    wrong place — which is exactly how the authorisation audit in this same directory came to be
    auditing an empty set for a whole afternoon.
    """
    app = create_app(_settings())

    @app.post("/projects/{project_id}/sneaky-upload")
    async def sneaky(
        project_id: str,
        upload: UploadFile,
    ) -> dict[str, str]:  # pragma: no cover - never called
        return {"project": project_id}

    offenders = [
        path for path, _, annotation in _request_annotations(app) if _mentions_bytes(annotation)
    ]
    assert "/projects/{project_id}/sneaky-upload" in offenders


# ---------------------------------------------------------------------------
# The upload ticket: scoped, single-purpose, expiring
# ---------------------------------------------------------------------------


def _ticket(key: str = "documents/abc/deadbeef", **overrides: Any) -> UploadTicket:
    defaults: dict[str, Any] = {
        "key": key,
        "url": f"http://local/upload/{key}",
        "method": "PUT",
        "expires_at": datetime.now(UTC) + timedelta(minutes=15),
        "required_headers": {"Content-Type": "application/pdf"},
    }
    return UploadTicket(**{**defaults, **overrides})


def _token(key: str = "documents/one/aaa", *, minutes: int = 5) -> str:
    return sign_capability(
        secret=SECRET,
        purpose=UPLOAD_PURPOSE,
        key=key,
        expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
    )


NOW = datetime.now(UTC)


def test_a_token_is_refused_for_a_different_key() -> None:
    """Scoped. A capability that can be re-aimed is a write token for the whole store."""
    with pytest.raises(CapabilityInvalid):
        verify_capability(
            _token("documents/one/aaa"),
            secret=SECRET,
            purpose=UPLOAD_PURPOSE,
            key="documents/two/bbb",
            now=NOW,
        )


def test_a_token_is_refused_after_it_expires() -> None:
    """Expiring, judged at a stated instant. `verify_capability` takes `now` rather than reading the
    clock, which is the only way to test a boundary without moving the machine's time."""
    token = _token(minutes=5)
    verify_capability(
        token, secret=SECRET, purpose=UPLOAD_PURPOSE, key="documents/one/aaa", now=NOW
    )

    with pytest.raises(CapabilityInvalid):
        verify_capability(
            token,
            secret=SECRET,
            purpose=UPLOAD_PURPOSE,
            key="documents/one/aaa",
            now=NOW + timedelta(minutes=6),
        )


def test_a_token_is_refused_for_a_different_purpose() -> None:
    """Single-purpose. Reading is a separate issuance with its own audit trail (#254), so an upload
    capability must not answer a download question."""
    with pytest.raises(CapabilityInvalid):
        verify_capability(
            _token(), secret=SECRET, purpose="download", key="documents/one/aaa", now=NOW
        )


def test_a_token_is_refused_under_a_different_secret() -> None:
    """A signature nobody checks is decoration. This fails if the comparison is dropped."""
    with pytest.raises(CapabilityInvalid):
        verify_capability(
            _token(),
            secret=b"a-different-secret-entirely-and-long",
            purpose=UPLOAD_PURPOSE,
            key="documents/one/aaa",
            now=NOW,
        )


def test_a_tampered_token_is_refused() -> None:
    """Editing the payload must not change what it authorises — otherwise the expiry and the key are
    both advisory and the signature is a decoration."""
    payload, _, signature = _token().rpartition(".")
    with pytest.raises(CapabilityInvalid):
        verify_capability(
            f"{payload}x.{signature}",
            secret=SECRET,
            purpose=UPLOAD_PURPOSE,
            key="documents/one/aaa",
            now=NOW,
        )


def test_the_ticket_type_refuses_a_download_purpose() -> None:
    """**The type had no validation at all when I found it.** Its docstring claimed scoped,
    single-purpose and expiring, and checked none of the three — a promise to whoever reads the prose
    and nothing to whoever constructs one. Every storage backend constructs these, including ones not
    written yet (#221)."""
    with pytest.raises(ValueError, match="purpose"):
        _ticket(purpose="download")


def test_the_ticket_type_refuses_a_naive_expiry() -> None:
    """A naive instant is compared against a different clock than it was written by, and that mistake
    runs one way: a ticket that outlives its lifetime."""
    with pytest.raises(ValueError, match="timezone-aware"):
        _ticket(expires_at=datetime.now())  # noqa: DTZ005 - the point of the test


def test_the_ticket_type_refuses_an_empty_key() -> None:
    with pytest.raises(ValueError, match="key"):
        _ticket(key="   ")


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


def _upgrade(engine: Engine) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store(tmp_path: Any) -> LocalStore:
    """A store that can sign tickets. `ticket_secret` has no default and `LocalStore` refuses to
    issue or verify without one — a generated key stops verifying at the next restart and a
    hard-coded one is a shared secret in the repository."""
    return LocalStore(root=tmp_path / "artifacts", ticket_secret=SECRET)


def _client(session: Session, store: LocalStore) -> Any:
    from fastapi.testclient import TestClient

    from app.auth import authenticate

    app = create_app(_settings())
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[authenticate] = _principal
    return TestClient(app, raise_server_exceptions=False)


def _principal() -> Any:
    from app.auth import Principal, Role

    return Principal(id="anant", roles=frozenset({Role.ADMIN}), projects=frozenset({PROJECT}))


PROJECT = uuid4()


def _new_package(session: Session, *, project_id: UUID | None = None) -> UUID:
    """A package with its first revision, committed so the endpoint's own reads can see it.

    Real column names, taken from the models rather than remembered: `revision_number`, not
    `revision_label`. Inventing a field name here has broken CI four times on this project.
    """
    resolved = project_id or PROJECT
    if session.get(Project, resolved) is None:
        # The project has to exist first: `packages.project_id` is a real foreign key, and a column
        # name verified against the model says nothing about the row it points at. This is the shape
        # of failure that has cost this project several CI runs.
        session.add(Project(id=resolved, name=f"project-{resolved}"))
        session.flush()
    package = Package(project_id=resolved)
    session.add(package)
    session.flush()
    session.add(
        PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED.value)
    )
    session.commit()
    return package.id


def _register(client: Any, digest: str, package_id: UUID) -> Any:
    return client.post(
        f"/api/v1/projects/{PROJECT}/packages/{package_id}/documents",
        json={"kind": DocumentKind.SHOP.value, "sha256": digest},
    )


def _upload_and_confirm(
    client: Any, store: LocalStore, package_id: UUID, payload: bytes, *, pages: int = 3
) -> tuple[str, Any]:
    """Register, write the bytes straight to storage as a client would, then confirm."""
    digest = hashlib.sha256(payload).hexdigest()
    registered = _register(client, digest, package_id)
    assert registered.status_code == 201, registered.text
    document_id = registered.json()["document_id"]

    ticket = client.post(
        f"/api/v1/projects/{PROJECT}/documents/{document_id}/uploads",
        json={"sha256": digest},
    )
    assert ticket.status_code == 201, ticket.text
    store.put(ticket.json()["storage_key"], io.BytesIO(payload), content_type="application/pdf")

    confirmed = client.post(
        f"/api/v1/projects/{PROJECT}/documents/{document_id}/confirm",
        json={"sha256": digest, "page_count": pages},
    )
    return document_id, confirmed


def _outbox_rows(session: Session) -> list[Any]:
    from app.models.outbox import OutboxEntry

    return list(session.execute(select(OutboxEntry)).scalars())


def test_the_version_and_the_ingestion_request_commit_together(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The acceptance criterion, proved rather than observed.**

    The failure is induced *between* the version and the outbox row. A test that confirms both rows
    appear on the happy path passes identically against an implementation that commits twice — so it
    would not notice the bug it exists to catch, which is a document version whose ingestion was never
    requested. That version sits there looking successfully uploaded and is never processed.
    """
    from app.api import documents as documents_module

    package_id = _new_package(session)
    payload = b"%PDF-1.7 induced failure\n"

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the outbox write failed")

    monkeypatch.setattr(documents_module, "enqueue", explode)

    client = _client(session, store)
    _, confirmed = _upload_and_confirm(client, store, package_id, payload)

    assert confirmed.status_code >= 500, "an outbox failure must not read as success"
    session.rollback()
    assert _outbox_rows(session) == []
    assert (
        list(session.execute(select(DocumentVersion)).scalars()) == []
    ), "the version survived a failed outbox write, so ingestion will never be requested for it"


def test_a_confirmed_upload_writes_the_version_and_one_outbox_row(
    session: Session, store: LocalStore
) -> None:
    """The happy path, and the other half of the atomicity claim: together means both, not either."""
    package_id = _new_package(session)
    client = _client(session, store)
    _, confirmed = _upload_and_confirm(client, store, package_id, b"%PDF-1.7 good\n")

    assert confirmed.status_code == 201, confirmed.text
    assert len(list(session.execute(select(DocumentVersion)).scalars())) == 1
    rows = _outbox_rows(session)
    assert len(rows) == 1
    assert rows[0].payload["sha256"] == confirmed.json()["sha256"]


def test_a_hash_mismatch_is_refused_and_writes_nothing(session: Session, store: LocalStore) -> None:
    """Taking the client's word for the hash would make §2.7's byte-exact pin a restatement of their
    claim rather than a check of it."""
    package_id = _new_package(session)
    client = _client(session, store)

    declared = hashlib.sha256(b"what I say I sent").hexdigest()
    registered = _register(client, declared, package_id)
    document_id = registered.json()["document_id"]
    ticket = client.post(
        f"/api/v1/projects/{PROJECT}/documents/{document_id}/uploads",
        json={"sha256": declared},
    )
    # Different bytes at the key the declared hash named.
    store.put(ticket.json()["storage_key"], io.BytesIO(b"what I actually sent"), content_type="a/b")

    refused = client.post(
        f"/api/v1/projects/{PROJECT}/documents/{document_id}/confirm",
        json={"sha256": declared, "page_count": 2},
    )
    assert refused.status_code == 422
    assert list(session.execute(select(DocumentVersion)).scalars()) == []
    assert _outbox_rows(session) == []


def test_re_confirming_the_same_bytes_is_a_no_op(session: Session, store: LocalStore) -> None:
    """Ingestion must not run twice. A second outbox row would re-extract the same drawing and, worse,
    make the count of ingestions a number nobody can reason about."""
    package_id = _new_package(session)
    client = _client(session, store)
    payload = b"%PDF-1.7 idempotent\n"

    document_id, first = _upload_and_confirm(client, store, package_id, payload)
    digest = hashlib.sha256(payload).hexdigest()
    again = client.post(
        f"/api/v1/projects/{PROJECT}/documents/{document_id}/confirm",
        json={"sha256": digest, "page_count": 3},
    )

    assert first.status_code == 201
    assert again.status_code == 200, "a repeat is a no-op, not a conflict and not a second create"
    assert again.json()["id"] == first.json()["id"]
    assert len(_outbox_rows(session)) == 1, "ingestion was requested twice for one upload"


def test_re_uploading_different_bytes_makes_a_new_version(
    session: Session, store: LocalStore
) -> None:
    """§2.7: a revision is a new version, never an edit of one. `DocumentVersion` is `Immutable`, so
    the database refuses the alternative outright — this asserts the API takes the other road."""
    package_id = _new_package(session)
    client = _client(session, store)

    first_id, first = _upload_and_confirm(client, store, package_id, b"%PDF-1.7 rev A\n")
    second_digest = hashlib.sha256(b"%PDF-1.7 rev B\n").hexdigest()
    ticket = client.post(
        f"/api/v1/projects/{PROJECT}/documents/{first_id}/uploads",
        json={"sha256": second_digest},
    )
    store.put(
        ticket.json()["storage_key"],
        io.BytesIO(b"%PDF-1.7 rev B\n"),
        content_type="application/pdf",
    )
    second = client.post(
        f"/api/v1/projects/{PROJECT}/documents/{first_id}/confirm",
        json={"sha256": second_digest, "page_count": 4},
    )

    assert second.status_code == 201
    versions = list(session.execute(select(DocumentVersion)).scalars())
    assert len(versions) == 2, "the second upload replaced the first instead of adding a version"
    assert {v.sha256 for v in versions} == {first.json()["sha256"], second_digest}


def test_another_projects_package_is_indistinguishable_from_absent(
    session: Session, store: LocalStore
) -> None:
    """A 404 that differs from the not-found 404 confirms the package exists, which is what the
    boundary is for."""
    package_id = _new_package(session, project_id=uuid4())
    client = _client(session, store)

    other_project = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}")
    never_existed = client.get(f"/api/v1/projects/{PROJECT}/packages/{uuid4()}")

    assert other_project.status_code == never_existed.status_code == 404

    # Everything but `request_id`, which is per-request by design and is the one field that *should*
    # differ. Comparing the whole body was my first version and it failed on exactly that — a
    # reminder that "identical" has to mean identical in the parts a caller could learn from.
    def _comparable(response: Any) -> dict[str, Any]:
        return {k: v for k, v in response.json().items() if k != "request_id"}

    assert _comparable(other_project) == _comparable(
        never_existed
    ), "the two refusals differ, so the response tells a caller which packages exist"
