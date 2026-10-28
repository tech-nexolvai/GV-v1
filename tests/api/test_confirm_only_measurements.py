"""A number drawn on the drawing is confirmed from a reading, never typed (#1251).

GV's rule, decided 2026-10-12 (#1181): people confirm readings; they do not type numbers drawn on the
vendor's or the architect's drawing. The waiting card already offers no box for them. This holds the
Measurements endpoint to the same rule, so a typed countertop depth can no longer reach the checks
and beat a reading.

What stays typable: the reviewer's own entries (`USER_INPUT`, e.g. the site width), the typed lists
(cabinet runs, #1205), and the settings, including the sink's spec-sheet size.

Values typed before this change stay in the database (append-only); nothing new of that kind can be
typed. Since #1303 they no longer count (`tests/api/test_typed_drawn_stops_counting.py`).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
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


# **Only what a published check declares, in the shape it declares, can be typed** (#1253 review).
#
# The refusal above matches the exact (rule, input) pair, so a name the rulebook never declared
# walked past it: `countertop_depth#0` was stored as a one-item list for `countertop_depth`, which
# outranks any reading and then crashed the checks for the whole revision on the scalar's arity.
# Lists are sent as `values` under the plain input name; the server alone writes `#0`, `#1`, ... and
# the `rule:` prefix, so a name carrying either is never a legitimate entry.


def test_the_waiting_card_and_the_form_say_where_a_drawn_value_is_read_the_same_way() -> None:
    """#1262: one sentence for both screens; a vendor's view is never named for the architect's."""
    from app.review.waiting_values import DRAWING_GUIDE, where_drawn

    assert where_drawn("SHOP", "CT010") == DRAWING_GUIDE["CT010"].where_to_find
    assert where_drawn("ARCH", "CT010") == "The architect's drawing."
    assert where_drawn("ARCH", "CT001") == "The architect's drawing."
    for text in (where_drawn("SHOP", "CT010"), where_drawn("ARCH", "CT001")):
        assert "not supplied" not in text
