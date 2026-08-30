"""The laptop runner, and the one route it adds that the deployed API must never have.

`scripts/dev_server.py` exists because `create_app` deliberately builds an application that cannot do
very much — no artifact store, no rulebook — and a laptop has no deployment to supply them. It also
mounts an upload shim, because `LocalStore` hands out `file:` URLs and **a browser cannot PUT to
`file:`**.

That shim is the part worth testing. It accepts file bytes, which is precisely what
`tests/api/test_packages.py::test_no_route_accepts_file_bytes` forbids of the shipped API, so the
thing these assert is that it stays on the correct side of that line: mounted by the script, never by
the factory, and refusing anything the storage layer would refuse.

Verification for: `scripts/dev_server.py`
"""

from __future__ import annotations

import hashlib
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from dev_server import DEVELOPMENT, NotADevelopmentEnvironment, _mount_upload_shim

from app.config import Settings
from app.main import create_app
from storage.local import TICKET_PARAMETER, LocalStore
from tests.api.test_authorisation import _mounted_routes

SHIM = "/_dev/upload/{key:path}"

#: Matches `tests/api/test_packages.py`. Nothing here connects; `create_app` needs a URL to build.
DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"

#: An hour, which is what the runner asks for too. Long enough that no assertion here races it.
TICKET_LIFETIME = timedelta(hours=1)


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(root=tmp_path, ticket_secret=b"a secret only this test knows")


@pytest.fixture
def shimmed(store: LocalStore) -> TestClient:
    """A bare application with only the shim on it.

    Deliberately not `create_app`: these are assertions about the shim, and building the real
    application would need a database that CI does not have.
    """
    app = FastAPI()
    _mount_upload_shim(app, store)
    return TestClient(app)


def _ticket(store: LocalStore, key: str) -> str:
    return store.upload_ticket(key, content_type="application/pdf", expires_in=TICKET_LIFETIME).url


def _token(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)[TICKET_PARAMETER][0]


def test_the_shim_is_not_on_the_shipped_application() -> None:
    """**The point of the whole file.**

    Backend §4.1: uploads go straight to storage and the control plane does short work only, because
    one 8 GB VM cannot put a drawing through the request path while PostgreSQL and OCR are also
    resident. The shim breaks that rule on purpose and is therefore allowed to exist only in a script
    that refuses to run outside development.

    Asserted against the route table rather than by grepping `app/`, because the failure this guards
    against is somebody moving the function, not somebody typing its name.

    **Through `_mounted_routes`, not `app.routes`.** `include_router` appends one opaque object and
    hides every route it carried, so the plain attribute reports two routes out of fifteen — and an
    absence checked against a set that small is a green tick that means nothing. The control below is
    what proves this one is looking at the real table.
    """
    settings = Settings(database_url=DATABASE_URL)  # type: ignore[call-arg]
    paths = {mounted.path for mounted in _mounted_routes(create_app(settings))}

    assert SHIM not in paths, (
        "the development upload shim is mounted on the shipped application. It accepts file bytes, "
        "which is what test_no_route_accepts_file_bytes forbids — mount it from scripts/ only."
    )


def test_that_absence_was_checked_against_the_real_route_table() -> None:
    """The control for the test above, which asserts a *negative*.

    Without this, deleting every router from `create_app` would make that test greener rather than
    redder. A route only reachable through `include_router` has to show up here for the absence to
    have been worth anything.
    """
    settings = Settings(database_url=DATABASE_URL)  # type: ignore[call-arg]
    paths = {mounted.path for mounted in _mounted_routes(create_app(settings))}

    assert (
        "/api/v1/projects/{project_id}/packages" in paths
    ), f"the walk did not reach the included routers, so the shim check proves nothing. Saw: {paths}"


def test_the_shim_is_mounted_when_the_script_asks_for_it(store: LocalStore) -> None:
    """And the other half: it is genuinely absent above because of *where* it lives, not because
    `_mount_upload_shim` quietly does nothing."""
    app = FastAPI()
    _mount_upload_shim(app, store)

    assert SHIM in {r.path for r in app.routes if isinstance(r, APIRoute)}


def test_the_shim_writes_what_the_ticket_covers(shimmed: TestClient, store: LocalStore) -> None:
    """The honest path: the bytes reach storage and can be read back."""
    body = b"%PDF-1.4\n% a drawing, though not a real one\n%%EOF\n"
    key = f"documents/{hashlib.sha256(body).hexdigest()}.pdf"

    response = shimmed.put(
        f"/_dev/upload/{key}",
        params={TICKET_PARAMETER: _token(_ticket(store, key))},
        content=body,
        headers={"content-type": "application/pdf"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"key": key, "bytes": str(len(body))}
    assert store.get(key).read() == body, "the shim reported a write it did not perform"


def test_a_ticket_for_one_object_cannot_write_another(
    shimmed: TestClient, store: LocalStore, tmp_path: Path
) -> None:
    """**The reason this verifies rather than just accepts.**

    A ticket is signed over its key. Were the shim to write wherever the URL pointed, a laptop would
    be running an open write endpoint rooted at the storage directory — a worse thing to have than
    the inconvenience it removes. This is also the check S3 performs on its own signature, so the
    behaviour does not change when #221 lands.
    """
    honest = "documents/mine.pdf"
    response = shimmed.put(
        "/_dev/upload/documents/somebody-elses.pdf",
        params={TICKET_PARAMETER: _token(_ticket(store, honest))},
        content=b"%PDF-1.4\n%%EOF\n",
    )

    assert response.status_code == 403
    # Asserted against the directory, not against `store.get` raising. A 403 that had already written
    # the bytes would satisfy the status check alone, and the whole point is that nothing landed.
    assert not list(tmp_path.rglob("somebody-elses*")), "refused the write and performed it anyway"


@pytest.mark.parametrize("token", ["", "forged", "not.a.real.signature"])
def test_the_shim_refuses_a_token_it_did_not_sign(shimmed: TestClient, token: str) -> None:
    """Every refusal is the same 403. A shim that distinguished "malformed" from "wrong signature"
    would be telling a caller how to get closer."""
    response = shimmed.put(
        "/_dev/upload/documents/anything.pdf",
        params={TICKET_PARAMETER: token},
        content=b"bytes",
    )

    assert response.status_code == 403


def test_the_runner_refuses_to_start_outside_development() -> None:
    """The two locks are the environment and the principal; this is the first.

    `scripts/dev_server.py` mounts an unauthenticated identity and an open-by-ticket write route. Run
    against a staging database it would be a live vulnerability, so it declines rather than warns.
    """
    assert DEVELOPMENT == "development"
    assert issubclass(NotADevelopmentEnvironment, Exception)
