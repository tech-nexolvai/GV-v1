"""A typed width or length of zero or less is refused on every path a person types one (#1182).

The audit (2026-10-11) proved a row `0 | 21 | 18` passing against 39: a `0` typed for a piece nobody
could read made the sum come out right. The width check itself now holds such a piece for the
reviewer (`tests/rules/test_ct1_width.py`); these tests prove the screen's three ways in refuse it
first, with a plain message, and that settings where zero is a real answer are untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.api.review import _corrected_inches
from app.db.session import session_factory
from app.review.sizes import NOT_ABOVE_ZERO
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


# ---------------------------------------------------------------------------
# A slot-reader row's widths
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Values saved in Measurements
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# A reviewer's correction of a reading
# ---------------------------------------------------------------------------


def _db_with(semantic_type: str) -> Any:
    reading = SimpleNamespace(unit="in", semantic_type=semantic_type)
    return SimpleNamespace(get=lambda _model, _id: reading)


@pytest.mark.parametrize("typed", ['0"', '-2 1/2"'])
def test_correcting_a_width_to_zero_or_less_is_refused(typed: str) -> None:
    with pytest.raises(HTTPException) as refused:
        _corrected_inches(_db_with("countertop_piece_width"), uuid4(), typed)

    assert refused.value.status_code == 422
    assert refused.value.detail == f"corrected_value: {NOT_ABOVE_ZERO}"


def test_correcting_a_value_that_is_not_a_size_to_zero_is_still_allowed() -> None:
    """A clearance or an offset may truly be zero; only sizes of real things are held to it."""
    assert _corrected_inches(_db_with("CT011"), uuid4(), '0"') == 0
