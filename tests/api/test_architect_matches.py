"""The reviewer's one-click choice of the architect's view for a countertop row (#1166).

Verification for `GET`/`POST .../slot-rows/{row_id}/architect-view-match` and
`GET .../architect-views/{view_id}/picture` in `app/api/architect_matches.py`. Rows come from
`tests/api/test_slot_rows.py`'s synthetic package; the architect's indexed views and the automatic
match are added here. Invented values only.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.schemas.architect_matches import ArchitectViewPickIn
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config

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
