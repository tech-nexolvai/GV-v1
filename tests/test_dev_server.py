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
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dev_server
from dev_server import (
    NotADevelopmentEnvironment,
    _mount_upload_shim,
    _no_gold_set_here,
    build_app,
)

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


def tmp_path_of(store: LocalStore) -> Path:
    """The directory a store was rooted at.

    Read back off the store rather than taking `tmp_path` as a second fixture parameter, so these
    assertions cannot drift onto a directory the store never used and quietly find nothing.
    """
    return Path(store._root)


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


@pytest.mark.parametrize("environment", ["production", "staging", "Development"])
def test_the_runner_refuses_to_start_outside_development(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """The first of the two locks, exercised rather than described.

    `scripts/dev_server.py` wires a filesystem store that signs its own upload tickets, mounts a
    route that accepts file bytes, and runs behind an identity that checks no credential. Pointed at
    a staging database that is not a development convenience, it is a live vulnerability — so it
    raises rather than warning.

    An earlier version of this test asserted that `DEVELOPMENT == "development"` and that the
    exception subclassed `Exception`, which is to say it asserted that two lines of source exist.
    It would have stayed green through the guard being deleted. `"Development"` is in the parameters
    because the comparison is exact, and a capitalised value is the plausible way to get this wrong.

    An empty environment is not in the parameters: `Settings` declares `min_length=1` and rejects it
    before this guard is reached, so asserting on it here would be asserting about pydantic.
    """
    monkeypatch.setenv("GV_ENVIRONMENT", environment)
    monkeypatch.setenv("GV_DATABASE_URL", DATABASE_URL)

    with pytest.raises(NotADevelopmentEnvironment):
        build_app()


def test_the_refusal_names_the_environment_it_saw(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whoever hits this is looking at a terminal wondering why nothing starts. "Not development" is
    not the answer; which environment it thinks it is in, is."""
    monkeypatch.setenv("GV_ENVIRONMENT", "staging")
    monkeypatch.setenv("GV_DATABASE_URL", DATABASE_URL)

    with pytest.raises(NotADevelopmentEnvironment, match="staging"):
        build_app()


def test_the_regression_gate_refuses_rather_than_being_absent() -> None:
    """**A rulebook with no regression check is not nearly configured — it is broken** (§9, #238).

    `Rulebook.regression` has no default for that reason, so the runner has to supply something. It
    supplies a check that fails and says why, because a laptop has no gold set to compare against.
    A `None` here — which is what this was until mypy objected — would have surfaced as
    `NoneType is not callable` at publish time, reading as a bug in the publish path rather than as
    the answer to the question the caller asked.
    """
    outcome = _no_gold_set_here(object())

    assert outcome.passed is False, "a laptop cannot run the gold set, so this must never pass"
    assert "development" in outcome.summary


def test_an_oversized_upload_is_refused_on_its_declared_length(
    shimmed: TestClient, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused before the body is read, when the caller was honest about its size.

    `await request.body()` accumulates the whole payload in memory. That is why this route may not
    exist in `app/` at all, and the reason does not soften on a laptop where PostgreSQL is the other
    tenant. The cap does not make the shim shippable — it makes a mistyped `curl` a 413 rather than
    a swap storm.
    """
    monkeypatch.setattr(dev_server, "MAX_UPLOAD_BYTES", 32)
    key = "documents/big.pdf"

    response = shimmed.put(
        f"/_dev/upload/{key}",
        params={TICKET_PARAMETER: _token(_ticket(store, key))},
        content=b"x" * 64,
    )

    assert response.status_code == 413
    assert not list(
        tmp_path_of(store).rglob("big.pdf")
    ), "refused the write and performed it anyway"


def test_an_oversized_upload_is_refused_even_when_the_length_was_a_lie(
    shimmed: TestClient, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The check that actually holds.**

    A caller controls `content-length` and can understate it or omit it entirely, so the cheap check
    is an optimisation and never the guarantee. Sent chunked, which is how a body arrives with no
    declared length at all.
    """
    monkeypatch.setattr(dev_server, "MAX_UPLOAD_BYTES", 32)
    key = "documents/liar.pdf"

    def chunks() -> Iterator[bytes]:
        yield b"x" * 64

    response = shimmed.put(
        f"/_dev/upload/{key}",
        params={TICKET_PARAMETER: _token(_ticket(store, key))},
        content=chunks(),
    )

    assert response.status_code == 413
    assert not list(tmp_path_of(store).rglob("liar.pdf")), "wrote a body it had already refused"


def test_the_cap_does_not_refuse_an_ordinary_drawing(
    shimmed: TestClient, store: LocalStore
) -> None:
    """The control. A ceiling low enough to reject real work would be found in production, by
    somebody who could not upload a drawing set."""
    body = b"%PDF-1.4\n" + b"\x00" * (4 * 1024 * 1024) + b"\n%%EOF\n"
    key = "documents/ordinary.pdf"

    response = shimmed.put(
        f"/_dev/upload/{key}",
        params={TICKET_PARAMETER: _token(_ticket(store, key))},
        content=body,
    )

    assert response.status_code == 200, "a 4 MB file is not large; real drawing sets are far bigger"

    # A band, not a floor. Every other assertion about the cap monkeypatches it to something tiny, so
    # the real constant is only ever exercised here — and a lower bound alone stays green while
    # somebody raises the ceiling to a number that bounds nothing, which is the same as deleting it.
    assert 64 * 1024 * 1024 <= dev_server.MAX_UPLOAD_BYTES <= 1024 * 1024 * 1024, (
        f"{dev_server.MAX_UPLOAD_BYTES} bytes is outside the range that is both usable for a real "
        "drawing set and small enough to bound a mistake on a laptop"
    )
