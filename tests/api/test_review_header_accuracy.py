"""The simple review header: totals, the AI's share, and its accuracy from this set's answers (#1386).

Line 1 says how many numbers, how many both AIs settled (with a whole percent) and how many need a
person; the two groups add up to the total by construction. Line 2 says how many AI readings a
person checked on this revision and how many the AI had right, counted exactly as the correction
rate counts them (`eval/correction_rate.py`); nothing checked gives no percentage. A settled number
a person corrected shows in the split of settled numbers checked.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.review.review_items import whole_percent
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


@pytest.mark.parametrize(
    ("part", "whole", "percent"),
    [
        (23, 51, 45),
        (11, 12, 92),
        (1, 2, 50),
        (0, 5, 0),
        (5, 5, 100),
        (199, 200, 99),  # never "100%" unless every one
        (1, 201, 1),  # never "0%" unless none
        (0, 0, None),  # nothing to divide: no percentage
    ],
)
def test_whole_percent_is_exact_and_never_rounds_to_all_or_none(
    part: int, whole: int, percent: int | None
) -> None:
    assert whole_percent(part, whole) == percent
