"""An upload keeps the file name the reviewer uploaded, cleaned, and never logs it (#1174).

The real confirm route against a real database: the name sent with the confirmation is cleaned
and stored on the upload's version, comes back in the answer, is optional (an older client sends
none), does not change on a repeat confirmation, and appears in no log record. Every name is made
up.
"""

from __future__ import annotations

import hashlib
import io
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import DocumentKind, DocumentVersion
from storage.local import LocalStore
from tests.api.test_packages import (
    PROJECT,
    _client,
    _new_package,
    _register,
    _upgrade,
)

pytest_plugins = ("tests.app.postgres_fixture",)


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


def _upload(
    session: Session,
    store: LocalStore,
    payload: bytes,
    *,
    file_name: str | None,
    kind: DocumentKind = DocumentKind.ARCHITECTURAL,
) -> tuple[str, Any]:
    """Register one file in a new package, write its bytes as a client would, then confirm with
    `file_name` (or with none, as an older client does)."""
    client = _client(session, store)
    digest = hashlib.sha256(payload).hexdigest()
    package_id = _new_package(session)
    registered = _register(client, digest, package_id, kind=kind)
    assert registered.status_code == 201, registered.text
    document_id = registered.json()["document_id"]
    store.put(registered.json()["storage_key"], io.BytesIO(payload), content_type="application/pdf")
    body: dict[str, object] = {"sha256": digest, "page_count": 2}
    if file_name is not None:
        body["file_name"] = file_name
    confirmed = client.post(
        f"/api/v1/projects/{PROJECT}/documents/{document_id}/confirm", json=body
    )
    return document_id, confirmed


def _stored_name(session: Session, document_id: str) -> str | None:
    session.expire_all()
    return session.scalars(
        select(DocumentVersion.file_name).where(DocumentVersion.document_id == document_id)
    ).one()


def test_an_upload_stores_the_cleaned_name(session: Session, store: LocalStore) -> None:
    document_id, confirmed = _upload(
        session, store, b"%PDF-1.7 named\n", file_name="C:\\fakepath\\Synthetic Plans\x00 B.pdf"
    )

    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["file_name"] == "Synthetic Plans B.pdf"
    assert _stored_name(session, document_id) == "Synthetic Plans B.pdf"


def test_a_long_odd_path_like_name_is_cleaned(session: Session, store: LocalStore) -> None:
    raw = "/tmp/upload/\u202e<i>Plänë & 図面<i>\n" + "x" * 400 + ".pdf"

    document_id, confirmed = _upload(session, store, b"%PDF-1.7 long\n", file_name=raw)

    assert confirmed.status_code == 201, confirmed.text
    stored = _stored_name(session, document_id)
    assert stored is not None
    assert len(stored) == 255 and stored.endswith("….pdf")
    assert stored.startswith("<i>Plänë & 図面<i>xxx")
    assert "\n" not in stored and "\u202e" not in stored and "/" not in stored


def test_an_upload_without_a_name_stores_none(session: Session, store: LocalStore) -> None:
    """An older client sends no name; a name that cleans to nothing is no name either."""
    unnamed, first = _upload(session, store, b"%PDF-1.7 unnamed\n", file_name=None)
    blank, second = _upload(session, store, b"%PDF-1.7 blank\n", file_name=" \x07 ")

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["file_name"] is None and second.json()["file_name"] is None
    assert _stored_name(session, unnamed) is None
    assert _stored_name(session, blank) is None


def test_a_repeat_confirmation_keeps_the_first_name(session: Session, store: LocalStore) -> None:
    payload = b"%PDF-1.7 repeat\n"
    document_id, first = _upload(session, store, payload, file_name="first-sample.pdf")

    again = _client(session, store).post(
        f"/api/v1/projects/{PROJECT}/documents/{document_id}/confirm",
        json={
            "sha256": hashlib.sha256(payload).hexdigest(),
            "page_count": 2,
            "file_name": "renamed-sample.pdf",
        },
    )

    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["file_name"] == "first-sample.pdf"
    assert _stored_name(session, document_id) == "first-sample.pdf"


def test_the_name_is_never_logged(
    session: Session, store: LocalStore, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "Synthetic-Job-Name-Never-Logged.pdf"

    with caplog.at_level(logging.DEBUG):
        _, confirmed = _upload(session, store, b"%PDF-1.7 quiet\n", file_name=secret)

    assert confirmed.status_code == 201, confirmed.text
    assert not [record for record in caplog.records if "Never-Logged" in record.getMessage()]
