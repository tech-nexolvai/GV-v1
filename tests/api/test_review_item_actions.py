"""One action endpoint per review item (#1351, simple review W3). Synthetic values only.

Each action writes only through a path that already exists (a review-session finding action, the
wall review, a no-countertop answer, the architect pairing), opens the review session itself, is
refused with 409 when the item the screen showed is stale, and asks for one coalesced re-check when
the answer changes what a check reads. A held number is never settled from another number's answer.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.api.measurements import RUN_CHECKS_WORKFLOW, queue_recheck
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import (
    ArchitectPairingRecord,
    Finding,
    NoCountertopAnswer,
    ObservationCandidate,
    PackageRevision,
    PackageState,
    SlotRowReviewDecision,
)
from app.models.outbox import OutboxEntry
from app.models.review import ReviewAction, ReviewSession
from app.review import review_item_actions
from app.review.review_items import ReviewItemsOut, review_items_for_revision
from tests.api.test_slot_rows import _package_rows, _run_current_checks, _save_all_row_widths
from tests.api.test_wall_cases_api import _sealed
from tests.app.postgres_fixture import alembic_config
from tests.review.test_no_countertop_answers import _listed_page

pytest_plugins = ("tests.app.postgres_fixture",)

REVIEWER = "reviewer"


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


def _client(session: Session, project_id: UUID) -> TestClient:
    app = create_app(Settings(database_url="postgresql+psycopg://gv:gv@localhost:5434/unused"))
    app.dependency_overrides[authenticate] = lambda: Principal(
        id=REVIEWER, roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _items(client: TestClient, project_id: UUID, package_id: UUID) -> Any:
    response = client.get(f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/review-items")
    assert response.status_code == 200, response.text
    return response.json()


def _act(
    client: TestClient,
    project_id: UUID,
    package_id: UUID,
    key: str,
    action: str,
    body: dict[str, object] | None = None,
) -> Any:
    return client.post(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/review-items/{key}/{action}",
        json=body or {},
    )


def _revision(session: Session, package_id: UUID) -> PackageRevision:
    return session.query(PackageRevision).filter_by(package_id=package_id).one()


def _actions(session: Session, finding_id: str | UUID) -> list[ReviewAction]:
    return list(
        session.scalars(
            select(ReviewAction).where(ReviewAction.finding_id == UUID(str(finding_id)))
        )
    )


def _queued_runs(session: Session, revision_id: UUID) -> list[OutboxEntry]:
    return list(
        session.scalars(
            select(OutboxEntry).where(
                OutboxEntry.workflow == RUN_CHECKS_WORKFLOW,
                OutboxEntry.dispatched_at.is_(None),
                OutboxEntry.payload["package_revision_id"].astext == str(revision_id),
            )
        )
    )


def _of_kind(body: Any, kind: str) -> list[dict[str, Any]]:
    return [item for item in body["items"] if item["kind"] == kind]


def _held_page(session: Session, tmp_path: Path) -> tuple[UUID, UUID]:
    """Page 1's countertop is held as a whole: one countertop to check by hand."""
    project_id, package_id, _anchors = _package_rows(session, held_page=0)
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    return project_id, package_id


def _first_by_hand(body: Any) -> dict[str, Any]:
    return next(item for item in _of_kind(body, "countertop_by_hand") if item["page"] == 1)


# --- checked by hand ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "named", "recorded", "note"),
    [
        # The screen's shape: the URL action `answer`, the answer's name in the body.
        ("answer", {"answer": "checked_right"}, "confirm", review_item_actions.CHECKED_RIGHT_NOTE),
        ("answer", {"answer": "checked_wrong"}, "dismiss", review_item_actions.CHECKED_WRONG_NOTE),
        # The W1 list's own name directly in the URL is the same answer.
        ("checked-wrong", {}, "dismiss", review_item_actions.CHECKED_WRONG_NOTE),
    ],
)
def test_checked_by_hand_records_the_finding_action_with_a_fixed_note_in_a_session_it_opens(
    session: Session,
    tmp_path: Path,
    action: str,
    named: dict[str, object],
    recorded: str,
    note: str,
) -> None:
    project_id, package_id = _held_page(session, tmp_path)
    client = _client(session, project_id)
    item = _first_by_hand(_items(client, project_id, package_id))
    assert session.scalars(select(ReviewSession)).first() is None

    response = _act(
        client,
        project_id,
        package_id,
        str(item["key"]),
        action,
        {"expected_finding_id": item["finding_id"], **named},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    (written,) = _actions(session, str(item["finding_id"]))
    assert (written.action, written.actor, written.note) == (recorded, REVIEWER, note)
    opened = session.get(ReviewSession, written.review_session_id)
    assert opened is not None and opened.reviewer == REVIEWER and opened.completed_at is None
    assert body["review_session_id"] == str(opened.id)
    # A finding decision changes nothing a check reads: no re-check is asked for.
    assert body["rechecking"] is False
    assert _queued_runs(session, _revision(session, package_id).id) == []
    # The list moved: the item is done, by this person, and counted as checked.
    after = _first_by_hand(body["review_items"])
    assert (after["status"], after["decided_by"], after["actions"]) == ("done", REVIEWER, [])
    assert body["review_items"]["header"]["checked_by_you"] == 1

    # A second answer to the same item records nothing and says why (it is no longer open).
    again = _act(
        client,
        project_id,
        package_id,
        str(item["key"]),
        action,
        {"expected_finding_id": item["finding_id"], **named},
    )
    assert again.status_code == 409, again.text
    assert len(_actions(session, str(item["finding_id"]))) == 1

    # The next action reuses the person's open session. (Page 2's countertop is not held: its one
    # open question is its walls, a wall choice with no right or wrong, #1358.)
    assert [entry["page"] for entry in _of_kind(body["review_items"], "countertop_by_hand")] == [1]
    other = body["review_items"]["not_checked"][0]
    second = _act(
        client,
        project_id,
        package_id,
        str(other["key"]),
        "clear-not-checked",
        {"expected_finding_id": other["finding_id"]},
    )
    assert second.status_code == 201, second.text
    assert second.json()["review_session_id"] == str(opened.id)


# --- stale, unknown, not offered --------------------------------------------------------------------


def test_a_stale_expected_finding_is_409_and_records_nothing(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id = _held_page(session, tmp_path)
    client = _client(session, project_id)
    item = _first_by_hand(_items(client, project_id, package_id))

    wrong = _act(
        client,
        project_id,
        package_id,
        str(item["key"]),
        "checked_right",
        {"expected_finding_id": str(uuid4())},
    )
    assert wrong.status_code == 409, wrong.text
    missing = _act(client, project_id, package_id, str(item["key"]), "checked_right", {})
    assert missing.status_code == 409, missing.text

    # The checks ran again since the screen loaded: the item's result was replaced.
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    replaced = _act(
        client,
        project_id,
        package_id,
        str(item["key"]),
        "checked_right",
        {"expected_finding_id": item["finding_id"]},
    )
    assert replaced.status_code == 409, replaced.text
    assert _actions(session, str(item["finding_id"])) == []
    assert session.scalars(select(ReviewAction)).first() is None


def test_an_unknown_item_or_package_is_404_and_an_action_not_offered_is_422(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id = _held_page(session, tmp_path)
    client = _client(session, project_id)
    item = _first_by_hand(_items(client, project_id, package_id))

    unknown = _act(
        client,
        project_id,
        package_id,
        f"countertop:{uuid4()}",
        "checked_right",
        {"expected_finding_id": str(uuid4())},
    )
    assert unknown.status_code == 404, unknown.text
    no_package = _act(client, project_id, uuid4(), str(item["key"]), "checked_right", {})
    assert no_package.status_code == 404, no_package.text
    not_offered = _act(
        client,
        project_id,
        package_id,
        str(item["key"]),
        "choose_wall",
        {"expected_finding_id": item["finding_id"], "wall_config": "back_left_right"},
    )
    assert not_offered.status_code == 422, not_offered.text
    assert session.scalars(select(ReviewAction)).first() is None


# --- numbers --------------------------------------------------------------------------------------


def _mixed(page_index: int, _row: int, slot: str) -> list[str]:
    """Page 1: piece 1's readers differ (a guess); piece 2 sits under GV's markup (no guess)."""
    if page_index == 0 and slot == "0":
        return ["reader:model-a:20", "reader:model-b:28", "readers-differ"]
    if page_index == 0 and slot == "1":
        return ["reviewer-markup"]
    return []


def test_a_number_is_501_until_confirming_a_number_arrives(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, _anchors = _package_rows(
        session, unsealed_all=True, piece_count=2, slot_flags=_mixed
    )
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    client = _client(session, project_id)
    number = next(
        item
        for item in _of_kind(_items(client, project_id, package_id), "number")
        if item["what"] == "Piece 1 width"
    )
    assert number["best_guess"]["text"] == '20"'

    # Confirm names the value the person saw (#1358): missing is 422, another value is 409.
    for shown, code in ((None, 422), ('28"', 409), ("not a width", 409)):
        refused = _act(
            client,
            project_id,
            package_id,
            str(number["key"]),
            "confirm",
            {"expected_finding_id": number["finding_id"], "shown_value": shown},
        )
        assert refused.status_code == code, refused.text

    for action, body in (("confirm", {"shown_value": '20"'}), ("fix", {"value": '21"'})):
        response = _act(
            client,
            project_id,
            package_id,
            str(number["key"]),
            action,
            {"expected_finding_id": number["finding_id"], **body},
        )
        assert response.status_code == 501, response.text
        assert response.json()["message"] == review_item_actions.NUMBERS_NOT_YET
    assert session.scalars(select(ReviewAction)).first() is None


def test_a_result_held_on_several_numbers_is_never_settled_from_one_of_them(
    session: Session, tmp_path: Path
) -> None:
    """The countertop's numbers without a guess are checked by hand, but the same result also waits
    on numbers with a guess: the by-hand answer would mark every one of them done, so it waits."""
    project_id, package_id, _anchors = _package_rows(
        session, unsealed_all=True, piece_count=2, slot_flags=_mixed
    )
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    client = _client(session, project_id)
    body = _items(client, project_id, package_id)
    by_hand = _first_by_hand(body)
    numbers = [
        item for item in _of_kind(body, "number") if item["finding_id"] == by_hand["finding_id"]
    ]
    assert numbers, "the fixture must hold one result on numbers with and without a guess"

    response = _act(
        client,
        project_id,
        package_id,
        str(by_hand["key"]),
        "checked_right",
        {"expected_finding_id": by_hand["finding_id"]},
    )
    assert response.status_code == 409, response.text
    assert "number" in response.json()["message"]
    assert _actions(session, str(by_hand["finding_id"])) == []
    after = _items(client, project_id, package_id)
    assert all(
        item["status"] == "open"
        for item in after["items"]
        if item["finding_id"] == by_hand["finding_id"]
    )


def test_numbers_answered_reads_the_per_number_confirmations(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once every number of the result has its own confirmation, the by-hand answer goes through."""
    project_id, package_id, _anchors = _package_rows(
        session, unsealed_all=True, piece_count=2, slot_flags=_mixed
    )
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    client = _client(session, project_id)
    body = _items(client, project_id, package_id)
    by_hand = _first_by_hand(body)
    held = {
        UUID(str(item["candidate_id"]))
        for item in _of_kind(body, "number")
        if item["finding_id"] == by_hand["finding_id"]
    }
    assert review_item_actions.numbers_answered(session, held) == set()
    monkeypatch.setattr(review_item_actions, "numbers_answered", lambda _session, ids: set(ids))

    response = _act(
        client,
        project_id,
        package_id,
        str(by_hand["key"]),
        "checked_right",
        {"expected_finding_id": by_hand["finding_id"]},
    )
    assert response.status_code == 201, response.text
    assert [action.action for action in _actions(session, str(by_hand["finding_id"]))] == [
        "confirm"
    ]


# --- failures and not checked ---------------------------------------------------------------------


def test_agreeing_with_a_failure_confirms_it_with_the_fixed_note(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="readers"
    )
    _save_all_row_widths(session, project_id, package_id, anchors[0])
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    client = _client(session, project_id)
    failure = _items(client, project_id, package_id)["failures"][0]

    response = _act(
        client,
        project_id,
        package_id,
        failure["key"],
        "agree",
        {"expected_finding_id": failure["finding_id"]},
    )

    assert response.status_code == 201, response.text
    (written,) = _actions(session, failure["finding_id"])
    assert (written.action, written.note) == ("confirm", review_item_actions.AGREE_NOTE)
    after = next(
        entry
        for entry in response.json()["review_items"]["failures"]
        if entry["key"] == failure["key"]
    )
    assert after["status"] == "done"


def test_clear_not_checked_dismisses_the_whole_group_in_one_action(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id = _held_page(session, tmp_path)
    client = _client(session, project_id)
    body = _items(client, project_id, package_id)
    group = body["not_checked"]
    assert group, "the fixture must give depth and sink checks that did not run"
    keys = [entry["key"] for entry in group]

    stale = _act(
        client,
        project_id,
        package_id,
        "not-checked",
        "clear-not-checked",
        {"expected_keys": keys[:-1]},
    )
    assert stale.status_code == 409, stale.text
    assert session.scalars(select(ReviewAction)).first() is None

    response = _act(
        client, project_id, package_id, "not-checked", "clear-not-checked", {"expected_keys": keys}
    )
    assert response.status_code == 201, response.text
    written = list(session.scalars(select(ReviewAction)))
    assert sorted(str(action.finding_id) for action in written) == sorted(
        entry["finding_id"] for entry in group
    )
    assert {(action.action, action.note) for action in written} == {
        ("dismiss", review_item_actions.NOT_CHECKED_FIXED_NOTE)
    }
    assert len({action.review_session_id for action in written}) == 1
    after = response.json()["review_items"]
    assert all(entry["status"] == "done" for entry in after["not_checked"])

    # One not-checked result on its own is cleared the same way, once.
    again = _act(
        client,
        project_id,
        package_id,
        keys[0],
        "clear-not-checked",
        {"expected_finding_id": group[0]["finding_id"]},
    )
    assert again.status_code == 409, again.text


# --- pages listed with no countertop ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "answer", "rechecks"),
    [
        ("confirm_no_countertop", "no_countertop", False),
        ("there_is_a_countertop", "countertop", True),
    ],
)
def test_a_page_with_no_countertop_is_answered_through_the_existing_answer(
    session: Session, tmp_path: Path, action: str, answer: str, rechecks: bool
) -> None:
    project_id, package_id, revision, record_id = _listed_page(session, tmp_path)
    client = _client(session, project_id)
    (page,) = _of_kind(_items(client, project_id, package_id), "no_countertop_page")
    assert page["row_id"] == str(record_id)

    response = _act(client, project_id, package_id, str(page["key"]), "answer", {"answer": action})

    assert response.status_code == 201, response.text
    (written,) = session.scalars(select(NoCountertopAnswer))
    assert (written.answer, written.answered_by, written.row_choice_candidate_id) == (
        answer,
        REVIEWER,
        record_id,
    )
    assert response.json()["rechecking"] is rechecks
    assert len(_queued_runs(session, revision.id)) == (1 if rechecks else 0)


def test_confirm_every_listed_page_uses_the_batch_confirm(session: Session, tmp_path: Path) -> None:
    project_id, package_id, _revision_row, record_id = _listed_page(session, tmp_path)
    client = _client(session, project_id)
    (page,) = _of_kind(_items(client, project_id, package_id), "no_countertop_page")

    response = _act(
        client,
        project_id,
        package_id,
        "no-countertop-pages",
        "answer",
        {"answer": "confirm_no_countertop", "expected_keys": [page["key"]]},
    )

    assert response.status_code == 201, response.text
    (written,) = session.scalars(select(NoCountertopAnswer))
    assert written.answer == "no_countertop" and written.batch_id is not None
    assert written.row_choice_candidate_id == record_id


# --- walls and the architect: the existing POSTs, then one re-check ---------------------------------


def _pretend_kind(monkeypatch: pytest.MonkeyPatch, key: str, kind: str, actions: list[str]) -> None:
    """Show one real countertop's result as another kind of item (the fixtures cannot draw it)."""
    real = review_items_for_revision

    def patched(*args: object, **kwargs: object) -> ReviewItemsOut:
        listed = real(*args, **kwargs)  # type: ignore[arg-type]
        for index, item in enumerate(listed.items):
            if item.key == key and item.status == "open":
                listed.items[index] = item.model_copy(update={"kind": kind, "actions": actions})
        return listed

    monkeypatch.setattr(review_item_actions, "review_items_for_revision", patched)


def test_a_wall_choice_goes_through_the_wall_review_and_queues_one_recheck(
    session: Session, tmp_path: Path
) -> None:
    """The real wall-only row (#1358): its `wall_choice` item is answered with one of the layouts
    it offers, through the existing wall review, and waits for the re-check."""
    project_id, package_id, anchors = _sealed(session, wall_source="readers", field_cuts=1)
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    revision = _revision(session, package_id)
    client = _client(session, project_id)
    item = next(
        entry
        for entry in _of_kind(_items(client, project_id, package_id), "wall_choice")
        if entry["row_id"] == str(anchors[0])
    )
    assert item["actions"] == ["choose_wall"]

    for refused_body in ({}, {"wall_config": "a garage"}):
        refused = _act(
            client,
            project_id,
            package_id,
            str(item["key"]),
            "answer",
            {"expected_finding_id": item["finding_id"], "answer": "choose_wall", **refused_body},
        )
        assert refused.status_code == 422, refused.text
    # No right or wrong on a wall choice.
    for not_offered in ("checked_right", "checked_wrong"):
        refused = _act(
            client,
            project_id,
            package_id,
            str(item["key"]),
            "answer",
            {"expected_finding_id": item["finding_id"], "answer": not_offered},
        )
        assert refused.status_code == 422, refused.text
    assert session.scalars(select(SlotRowReviewDecision)).first() is None

    response = _act(
        client,
        project_id,
        package_id,
        str(item["key"]),
        "answer",
        {
            "expected_finding_id": item["finding_id"],
            "answer": "choose_wall",
            "wall_config": "back_and_left",
        },
    )

    assert response.status_code == 201, response.text
    (decision,) = session.scalars(select(SlotRowReviewDecision))
    assert (decision.row_candidate_id, decision.wall_config, decision.confirmed_by) == (
        anchors[0],
        "back_and_left",
        REVIEWER,
    )
    assert decision.measurements == {}
    assert response.json()["rechecking"] is True
    assert len(_queued_runs(session, revision.id)) == 1
    # The wall answer is the row's, not a finding decision.
    assert session.scalars(select(ReviewAction)).first() is None
    # The item now waits for the re-check that reads the answer; nothing more is asked.
    after = response.json()["review_items"]
    (waiting,) = [entry for entry in after["items"] if entry["key"] == item["key"]]
    assert (waiting["status"], waiting["actions"]) == ("rechecking", [])
    assert after["unaccounted"] == []


def test_an_architect_answer_goes_through_the_pairing_post_and_rechecks(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, package_id, anchors = _package_rows(session, wall_source="readers")
    _run_current_checks(session, package_id, tmp_path)
    anchor = session.get(ObservationCandidate, anchors[0])
    assert anchor is not None
    # Code's own pairing of the row: nothing on the architect's drawing fitted.
    session.add(
        ArchitectPairingRecord(
            package_revision_id=_revision(session, package_id).id,
            page_id=anchor.page_id,
            row_anchor_candidate_id=anchor.id,
            extraction_run_id=anchor.extraction_run_id,
            source="code",
            status="no_fit",
            pairs=[],
            details={"reasons": ["synthetic: nothing fitted"]},
        )
    )
    session.commit()
    client = _client(session, project_id)
    item = next(
        entry
        for entry in _items(client, project_id, package_id)["items"]
        if entry["row_id"] == str(anchors[0]) and entry["finding_id"] is not None
    )
    _pretend_kind(monkeypatch, str(item["key"]), "architect", ["answer"])

    neither = _act(
        client,
        project_id,
        package_id,
        str(item["key"]),
        "answer",
        {"expected_finding_id": item["finding_id"]},
    )
    assert neither.status_code == 422, neither.text

    response = _act(
        client,
        project_id,
        package_id,
        str(item["key"]),
        "answer",
        {"expected_finding_id": item["finding_id"], "architect_pairing": {"pairs": []}},
    )

    assert response.status_code == 201, response.text
    record = session.scalars(
        select(ArchitectPairingRecord).where(ArchitectPairingRecord.source == "reviewer")
    ).one()
    assert (record.row_anchor_candidate_id, record.source, record.decided_by) == (
        anchors[0],
        "reviewer",
        REVIEWER,
    )
    assert response.json()["rechecking"] is True


def test_a_depth_reading_is_501_until_it_is_wired(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, package_id = _held_page(session, tmp_path)
    client = _client(session, project_id)
    item = _first_by_hand(_items(client, project_id, package_id))
    _pretend_kind(monkeypatch, str(item["key"]), "depth_reading", ["confirm", "not_this"])

    response = _act(
        client,
        project_id,
        package_id,
        str(item["key"]),
        "confirm",
        {"expected_finding_id": item["finding_id"]},
    )
    assert response.status_code == 501, response.text


# --- the coalesced re-check -------------------------------------------------------------------------


def test_rechecks_coalesce_to_one_waiting_run_and_never_refuse(
    session: Session, tmp_path: Path, postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, package_id, _anchors = _package_rows(session, wall_source="readers")
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    revision = _revision(session, package_id)

    # Nothing waiting: one run is queued. Asked again before the worker takes it: the same run.
    assert queue_recheck(session, revision) is True
    session.commit()
    assert queue_recheck(session, revision) is False
    session.commit()
    (waiting,) = _queued_runs(session, revision.id)

    # The worker has taken that run (its row is locked while it runs, and only stamped delivered
    # when it finishes): it may have read the values before this answer, so one follow-up waits.
    worker = session_factory(postgres_engine)()
    try:
        worker.execute(
            select(OutboxEntry.id).where(OutboxEntry.id == waiting.id).with_for_update()
        ).one()
        assert queue_recheck(session, revision) is True
        session.commit()
        # A third answer while the first runs and the follow-up waits: still the one follow-up.
        assert queue_recheck(session, revision) is False
        session.commit()
    finally:
        worker.rollback()
        worker.close()
    assert len(_queued_runs(session, revision.id)) == 2

    # Through the endpoint: a wall answer on each countertop, each 201 and never 409; both join
    # the runs already waiting, so no run is added.
    client = _client(session, project_id)
    walls = _of_kind(_items(client, project_id, package_id), "wall_choice")
    assert len(walls) == 2, "each countertop's one open question is its walls (#1358)"
    for item in walls:
        response = _act(
            client,
            project_id,
            package_id,
            str(item["key"]),
            "choose_wall",
            {"expected_finding_id": item["finding_id"], "wall_config": "back_left_right"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["rechecking"] is True
    assert len(_queued_runs(session, revision.id)) == 2


def test_a_recheck_while_checks_run_queues_one_follow_up(session: Session, tmp_path: Path) -> None:
    _project_id, package_id, _anchors = _package_rows(session, wall_source="readers")
    _run_current_checks(session, package_id, tmp_path)
    revision = _revision(session, package_id)
    revision.state = PackageState.RUNNING_CHECKS.value
    session.commit()

    assert queue_recheck(session, revision) is True
    session.commit()
    assert queue_recheck(session, revision) is False
    session.commit()
    assert len(_queued_runs(session, revision.id)) == 1
    assert session.scalars(select(Finding)).first() is not None


# --- #1358: groups name what was seen; nothing new is not "Confirmed"; settings; no asserts --------


@pytest.mark.parametrize(
    ("group", "action", "answer"),
    [
        ("not-checked", "clear-not-checked", None),
        ("no-countertop-pages", "answer", "confirm_no_countertop"),
    ],
)
def test_a_group_click_without_the_keys_seen_is_refused_and_records_nothing(
    session: Session, tmp_path: Path, group: str, action: str, answer: str | None
) -> None:
    project_id, package_id = (
        _held_page(session, tmp_path)
        if group == "not-checked"
        else _listed_page(session, tmp_path)[:2]
    )
    client = _client(session, project_id)
    body = {} if answer is None else {"answer": answer}
    actions_before = len(list(session.scalars(select(ReviewAction))))

    response = _act(client, project_id, package_id, group, action, body)

    assert response.status_code == 422, response.text
    assert "expected_keys" in response.json()["message"]
    assert len(list(session.scalars(select(ReviewAction)))) == actions_before
    assert session.scalars(select(NoCountertopAnswer)).first() is None


def _keep_open(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    """Show an item as still open (the screen loaded before another answer landed)."""
    real = review_items_for_revision

    def patched(*args: object, **kwargs: object) -> ReviewItemsOut:
        listed = real(*args, **kwargs)  # type: ignore[arg-type]
        for index, item in enumerate(listed.items):
            if item.key == key:
                listed.items[index] = item.model_copy(
                    update={
                        "status": "open",
                        "actions": ["confirm_no_countertop", "there_is_a_countertop"],
                    }
                )
        return listed

    monkeypatch.setattr(review_item_actions, "review_items_for_revision", patched)


def test_a_page_answer_already_recorded_is_nothing_new_not_confirmed(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, package_id, _revision_row, _record = _listed_page(session, tmp_path)
    client = _client(session, project_id)
    (page,) = _of_kind(_items(client, project_id, package_id), "no_countertop_page")
    first = _act(
        client,
        project_id,
        package_id,
        str(page["key"]),
        "answer",
        {"answer": "confirm_no_countertop"},
    )
    assert first.status_code == 201, first.text
    _keep_open(monkeypatch, str(page["key"]))

    again = _act(
        client,
        project_id,
        package_id,
        str(page["key"]),
        "answer",
        {"answer": "confirm_no_countertop"},
    )

    assert again.status_code == 200, again.text
    assert again.json()["recorded"].startswith("Nothing new")
    assert len(list(session.scalars(select(NoCountertopAnswer)))) == 1

    group = _act(
        client,
        project_id,
        package_id,
        "no-countertop-pages",
        "answer",
        {"answer": "confirm_no_countertop", "expected_keys": [page["key"]]},
    )
    assert group.status_code == 200, group.text
    assert group.json()["recorded"].startswith("Nothing new")
    assert len(list(session.scalars(select(NoCountertopAnswer)))) == 1


def test_a_recheck_runs_the_same_settings_check_as_run_checks(
    session: Session, tmp_path: Path
) -> None:
    """A confirmed discriminator the rulebook does not offer is refused (422), as `POST .../checks`
    refuses it, rather than queued to abstain silently."""
    from fastapi import HTTPException

    from workflow.layout_proposals import record_layout_confirmation

    _project_id, package_id = _held_page(session, tmp_path)
    revision = _revision(session, package_id)
    record_layout_confirmation(
        session,
        package_revision_id=revision.id,
        discriminator_name="wall_config",
        value="a garage",
        actor=REVIEWER,
    )

    with pytest.raises(HTTPException) as refused:
        queue_recheck(session, revision)

    assert refused.value.status_code == 422
    assert _queued_runs(session, revision.id) == []


def test_an_item_with_nothing_to_answer_on_is_422_not_a_crash(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route's former `assert`s are plain 422s: a finding answer on an item with no result, and
    a row answer on an item with no row."""
    project_id, package_id = _held_page(session, tmp_path)
    client = _client(session, project_id)
    item = _first_by_hand(_items(client, project_id, package_id))
    real = review_items_for_revision

    def patched(*args: object, **kwargs: object) -> ReviewItemsOut:
        listed = real(*args, **kwargs)  # type: ignore[arg-type]
        for index, entry in enumerate(listed.items):
            if entry.key == item["key"]:
                listed.items[index] = entry.model_copy(
                    update={
                        "kind": "wall_choice",
                        "actions": ["choose_wall"],
                        "row_id": None,
                    }
                )
        return listed

    monkeypatch.setattr(review_item_actions, "review_items_for_revision", patched)
    no_row = _act(
        client,
        project_id,
        package_id,
        str(item["key"]),
        "answer",
        {
            "expected_finding_id": item["finding_id"],
            "answer": "choose_wall",
            "wall_config": "back_left_right",
        },
    )
    assert no_row.status_code == 422, no_row.text

    def no_result(*args: object, **kwargs: object) -> ReviewItemsOut:
        listed = real(*args, **kwargs)  # type: ignore[arg-type]
        for index, entry in enumerate(listed.items):
            if entry.key == item["key"]:
                listed.items[index] = entry.model_copy(update={"finding_id": None})
        return listed

    monkeypatch.setattr(review_item_actions, "review_items_for_revision", no_result)
    no_finding = _act(client, project_id, package_id, str(item["key"]), "checked_right", {})
    assert no_finding.status_code == 422, no_finding.text
    assert session.scalars(select(ReviewAction)).first() is None
    assert session.scalars(select(SlotRowReviewDecision)).first() is None
