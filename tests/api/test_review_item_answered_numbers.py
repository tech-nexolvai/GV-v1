"""A person's answer about a held number counts, and the number leaves the list (Set 3 QA).

A row whose overall sits under GV's markup stays checked by hand (the overall is the vendor's own
number to check), but each number a person confirmed or fixed on it is that person's second
judgment: the list stops asking for it (it waits for the re-check, then it is gone), the row shows
the person's value, and the by-hand answer for the rest no longer waits on it. A re-check gives the
row's result a new id; an answer naming the result the screen showed is still taken while the item
is open and unchanged (#1351 carry-over). Synthetic values only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from tests.api.test_review_item_actions import (
    _act,
    _client,
    _first_by_hand,
    _items,
    _mixed,
    _of_kind,
    session,  # noqa: F401  (the fixture)
)
from tests.api.test_slot_rows import _package_rows, _run_current_checks

pytest_plugins = ("tests.app.postgres_fixture",)


def _setup(session: Session, tmp_path: Path) -> tuple[Any, UUID, UUID]:  # noqa: F811
    project_id, package_id, _anchors = _package_rows(
        session, unsealed_all=True, piece_count=2, slot_flags=_mixed
    )
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    return _client(session, project_id), project_id, package_id


def _piece_one(body: Any, what: str = "Piece 1 width") -> dict[str, Any] | None:
    return next(
        (item for item in _of_kind(body, "number") if item["page"] == 1 and item["what"] == what),
        None,
    )


def _answer(
    client: Any,
    project_id: UUID,
    package_id: UUID,
    how: str,
    *,
    what: str = "Piece 1 width",
    finding_id: str | None = None,
) -> dict[str, Any]:
    """Answer one number of page 1, naming `finding_id` as the result shown (default: the current)."""
    number = _piece_one(_items(client, project_id, package_id), what)
    assert number is not None and number["status"] == "open"
    body: dict[str, object] = {"expected_finding_id": finding_id or number["finding_id"]}
    if how == "confirm":
        body["shown_value"] = number["best_guess"]["text"]
    else:
        body["value"] = '21"'
    response = _act(client, project_id, package_id, str(number["key"]), how, body)
    assert response.status_code == 201, response.text
    return number


def _countertop(client: Any, project_id: UUID, package_id: UUID) -> dict[str, Any]:
    from app.main import API_PREFIX

    response = client.get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/countertop-results"
    )
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["items"] if item["page_number"] == 1)


def test_a_confirmed_number_waits_for_the_recheck_then_leaves_the_list(
    session: Session, tmp_path: Path  # noqa: F811
) -> None:
    client, project_id, package_id = _setup(session, tmp_path)
    _answer(client, project_id, package_id, "confirm")

    # Before the re-check: never asked again (no Confirm/Fix), it waits for the checks.
    waiting = _piece_one(_items(client, project_id, package_id))
    assert waiting is not None
    assert waiting["status"] == "rechecking"
    assert waiting["actions"] == []

    # After the re-check: gone from the list; the row shows the person's value.
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    after = _items(client, project_id, package_id)
    assert _piece_one(after) is None
    piece = _countertop(client, project_id, package_id)["pieces"][0]
    assert piece["value"]["display"] == '20"'
    assert piece["source"] == "typed"
    by_hand = _first_by_hand(after)
    assert "Piece 1" not in by_hand["what"]
    assert all(
        line["position"] != 1 or line["value"] == '20"'
        for line in by_hand["what_to_check"]["pieces"]
    )


def test_a_fixed_number_counts_and_the_by_hand_answer_no_longer_waits_on_it(
    session: Session, tmp_path: Path  # noqa: F811
) -> None:
    client, project_id, package_id = _setup(session, tmp_path)
    _answer(client, project_id, package_id, "fix")
    # A fix holds the whole result until the re-check (the correction ledger); then the overall.
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    _answer(client, project_id, package_id, "confirm", what="Overall countertop width")
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    after = _items(client, project_id, package_id)
    assert _piece_one(after) is None
    assert _piece_one(after, "Overall countertop width") is None
    row = _countertop(client, project_id, package_id)
    assert row["pieces"][0]["value"]["display"] == '21"'

    # Only piece 2 (under GV's markup) is left to check by hand, and nothing waits on numbers.
    by_hand = _first_by_hand(after)
    assert by_hand["what"] == "Check by hand: Piece 2 width"
    response = _act(
        client,
        project_id,
        package_id,
        str(by_hand["key"]),
        "checked_right",
        {"expected_finding_id": by_hand["finding_id"]},
    )
    assert response.status_code == 201, response.text


def test_an_answer_naming_the_result_before_a_recheck_is_still_taken(
    session: Session, tmp_path: Path  # noqa: F811
) -> None:
    """Each answer queues a re-check that gives the row's result a new id. The next answer names the
    result the screen showed; the item is still open and unchanged, so it is taken (carry-over)."""
    client, project_id, package_id = _setup(session, tmp_path)
    shown = _first_by_hand(_items(client, project_id, package_id))
    old_finding, old_key = str(shown["finding_id"]), str(shown["key"])
    _answer(client, project_id, package_id, "confirm")
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    now = _first_by_hand(_items(client, project_id, package_id))
    assert now["finding_id"] != old_finding, "the re-check must give the result a new id"

    # A number (its key is stable) answered naming the result shown before the re-check.
    _answer(
        client,
        project_id,
        package_id,
        "confirm",
        what="Overall countertop width",
        finding_id=old_finding,
    )
    _run_current_checks(session, package_id, tmp_path)
    session.commit()

    # The by-hand item, by the key and result the screen showed first: still the same item.
    response = _act(
        client,
        project_id,
        package_id,
        old_key,
        "checked_right",
        {"expected_finding_id": old_finding},
    )
    assert response.status_code == 201, response.text


def test_a_result_whose_outcome_changed_is_still_stale(
    session: Session, tmp_path: Path  # noqa: F811
) -> None:
    """Carry-over is only for the same check, row and outcome: another result is still 409."""
    client, project_id, package_id = _setup(session, tmp_path)
    page_two = next(
        item
        for item in _of_kind(_items(client, project_id, package_id), "number")
        if item["page"] == 2
    )
    number = _piece_one(_items(client, project_id, package_id))
    assert number is not None
    response = _act(
        client,
        project_id,
        package_id,
        str(number["key"]),
        "confirm",
        {
            "expected_finding_id": page_two["finding_id"],
            "shown_value": number["best_guess"]["text"],
        },
    )
    assert response.status_code == 409, response.text
