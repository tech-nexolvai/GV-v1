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
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.models import NoCountertopAnswer
from app.review.review_items import plain_architect_note
from tests.api.test_review_item_actions import (
    _act,
    _client,
    _held_page,
    _items,
    _of_kind,
)
from tests.api.test_unchosen_row_pages import (
    NONE_PICKS,
    NOT_BOXED_KINDS,
    NOT_BOXED_WHY,
    _unchosen_record,
)
from tests.app.postgres_fixture import alembic_config
from tests.review.test_no_countertop_answers import _listed_page

pytest_plugins = ("tests.app.postgres_fixture",)

#: An older reading (no `row-kind:`), whose AI reason says a countertop is drawn but not boxed.
OLDER_WHY = (
    "synthetic: the front view shows a countertop dimension chain, but no numbered box measures it"
)


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


def _three_pages(
    session: Session, tmp_path: Path
) -> tuple[TestClient, UUID, UUID, dict[int, UUID]]:
    """Page 3: every AI said "no countertop" (with kinds). Page 4: an older all-0 reading with no
    kind. Page 5: both AIs said a countertop exists that no numbered box measures."""
    project_id, package_id, revision, positive = _listed_page(session, tmp_path)
    older = _unchosen_record(session, revision.id, page_index=3, flags=NONE_PICKS, reason=OLDER_WHY)
    not_boxed = _unchosen_record(
        session, revision.id, page_index=4, flags=NONE_PICKS + NOT_BOXED_KINDS, reason=NOT_BOXED_WHY
    )
    session.commit()
    return (
        _client(session, project_id),
        project_id,
        package_id,
        {3: positive, 4: older, 5: not_boxed},
    )


def _pages(body: Any) -> dict[int, dict[str, Any]]:
    return {item["page"]: item for item in _of_kind(body, "no_countertop_page")}


def test_only_pages_every_reader_said_none_are_proposed(session: Session, tmp_path: Path) -> None:
    client, project_id, package_id, records = _three_pages(session, tmp_path)
    pages = _pages(_items(client, project_id, package_id))

    # The positive "none" page is the one-click proposal.
    assert pages[3]["row_id"] == str(records[3])
    assert pages[3]["proposed_no_countertop"] is True
    # The older reading is asked on its own: no proposal, both answers offered.
    assert pages[4]["row_id"] == str(records[4])
    assert pages[4]["proposed_no_countertop"] is False
    assert pages[4]["what"] == "Decide this page"
    assert pages[4]["actions"] == ["confirm_no_countertop", "there_is_a_countertop"]
    # "A countertop no box measures" is never a no-countertop page.
    assert 5 not in pages


def test_the_group_confirm_covers_only_the_proposed_pages(session: Session, tmp_path: Path) -> None:
    client, project_id, package_id, records = _three_pages(session, tmp_path)
    pages = _pages(_items(client, project_id, package_id))

    # Naming the older page in the group is refused: it is not in the group.
    refused = _act(
        client,
        project_id,
        package_id,
        "no-countertop-pages",
        "answer",
        {
            "answer": "confirm_no_countertop",
            "expected_keys": [pages[3]["key"], pages[4]["key"]],
        },
    )
    assert refused.status_code == 409, refused.text
    assert list(session.scalars(select(NoCountertopAnswer))) == []

    response = _act(
        client,
        project_id,
        package_id,
        "no-countertop-pages",
        "answer",
        {"answer": "confirm_no_countertop", "expected_keys": [pages[3]["key"]]},
    )
    assert response.status_code == 201, response.text
    (written,) = session.scalars(select(NoCountertopAnswer))
    assert written.row_choice_candidate_id == records[3]

    # The older page still waits for its own answer.
    after = _pages(_items(client, project_id, package_id))
    assert after[4]["status"] == "open"


def test_a_countertop_checked_by_hand_is_named_by_its_page(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id = _held_page(session, tmp_path)
    client = _client(session, project_id)
    body = _items(client, project_id, package_id)

    named = [item["what"] for item in _of_kind(body, "countertop_by_hand")]
    assert named
    assert "This countertop" not in named
    assert "Countertop on page 1 — check the widths by hand" in named


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


def test_an_answer_naming_no_result_is_422_not_newer_result(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id = _held_page(session, tmp_path)
    client = _client(session, project_id)
    item = next(
        entry
        for entry in _of_kind(_items(client, project_id, package_id), "countertop_by_hand")
        if entry["finding_id"]
    )
    response = _act(client, project_id, package_id, str(item["key"]), "checked_right", {})
    assert response.status_code == 422, response.text
    assert "expected_finding_id is required" in response.text
