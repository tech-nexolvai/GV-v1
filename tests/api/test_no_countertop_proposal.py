"""The simple review's "no countertop" proposal and its wording (#1394). Synthetic values only.

**Safety.** The one-click "These N pages have no countertop" proposal holds only pages where every
reader positively answered "no countertop on the sheet" (`row-kind:` recorded as `no_countertop`).
An older reading with no kind, whose 0 may have meant "a countertop none of the boxes measures", is
asked on its own ("Decide this page"), with both answers and nothing proposed; a reading that says a
countertop exists but is not boxed is never a "no countertop" page at all. The group confirm's
`expected_keys` cover only the proposed pages.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.review.review_items import plain_architect_note
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    from app.db.session import session_factory

    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.mark.parametrize(
    "reason",
    [
        (
            "The architect's dimensions on this sheet measure fixture centres and blocking, not "
            "the countertop. No dimension pairs with this row."
        ),
        "Not compared: the architect's spans run to a fixture centre line and hatched blocking.",
    ],
)
def test_the_architect_note_is_plain_words(reason: str) -> None:
    assert plain_architect_note(reason) == (
        "The architect gives no width for this countertop "
        "(their numbers measure to fixtures or walls)."
    )


def test_a_plain_architect_note_is_kept() -> None:
    reason = "Not compared with the architect's drawing here."
    assert plain_architect_note(reason) == reason


# --- QA fixes on the same screen (#1394) ----------------------------------------------------------


def test_a_whole_page_box_a_hair_past_the_edge_is_put_back_on_the_page() -> None:
    """A "no countertop" page's whole-page box can end a hair outside 0..1 after conversion; it is
    put on the edge so its mark is drawn. A box really outside the page is kept, and refused."""
    import json
    from decimal import Decimal
    from uuid import uuid4

    from app.review.reviewer_marks import _reference
    from evidence.coordinates import StoredPoint
    from evidence.polygon import Polygon

    near = [[Decimal("-0.0000004"), Decimal(0)], [Decimal("1.0002"), Decimal(0)]]
    near += [[Decimal("1.0002"), Decimal("1.000001")], [Decimal(0), Decimal("1.000001")]]
    polygon = json.loads(_reference(uuid4(), 0, near))["polygon"]
    assert polygon == [["0", "0"], ["1", "0"], ["1", "1"], ["0", "1"]]
    document = uuid4()
    Polygon(tuple(StoredPoint(Decimal(x), Decimal(y)) for x, y in polygon), "stored", document, 0)

    far = [[Decimal(0), Decimal(0)], [Decimal("1.5"), Decimal(0)], [Decimal("1.5"), Decimal(1)]]
    assert json.loads(_reference(uuid4(), 0, far))["polygon"][1] == ["1.5", "0"]
