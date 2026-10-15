"""Depth and sink readings answered from the simple review screen (#1360). Synthetic values only.

A depth or sink value the two AI readers agree on, but only its drawn length checks, waits for a
person (`app/review/depth_readings.py`, state `to_confirm`). The simple review list shows one item
per such reading, with the value a Confirm records, and the action endpoint answers it:

* **Confirm** goes through the held-reading evidence decision (`confirm_candidate_reading`, #1339):
  the same records as that decision made directly, the same HUMAN_CONFIRMED value the advanced
  depth card's confirm writes, one coalesced re-check. A depth reading is confirmed once only, and
  only through its own countertop's result (#1294, #1296).
* **Not this** records that the check could not use the reading: a `dismiss` with a fixed note,
  printed NOT CHECKED, exactly as clearing a "Not checked" result. Nothing is written on the reading.
"""

from __future__ import annotations

from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.measurements import RUN_CHECKS_WORKFLOW
from app.auth import Principal, Role
from app.db.session import session_factory
from app.models import (
    CanonicalObservation,
    DrawingView,
    ObservationCandidate,
    PackageState,
    ViewRole,
)
from app.models.evidence import EvidenceSupportingCandidate
from app.models.outbox import OutboxEntry
from app.models.review import ReviewAction, ReviewSession
from app.review import review_item_actions
from app.review.evidence_actions import confirm_candidate_reading
from app.review.review_items import ReviewItemOut, ReviewItemsOut, review_items_for_revision
from app.review.session import open_session
from app.review.signed_record import ReviewDisposition
from storage.local import LocalStore
from tests.api.test_depth_readings import _base, _client, _crop
from tests.evidence.test_bridge import _upgrade
from tests.workflow.test_depth_evidence import ACTOR, CT008, CT010, _Set
from tests.workflow.test_reading_parts import _confirmed
from vocabulary.part_kinds import PartKind
from workflow.depth_evidence import CONFIRM_ONLY
from workflow.view_roles import confirm_view_role

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
    return LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only")


def _waiting(
    session: Session, store: LocalStore, field: str = CT010, inches: int = 25
) -> tuple[_Set, ObservationCandidate]:
    """One countertop alone on its drawing, a value only its drawn length checks, with its picture;
    the checks have run with the depth reader on, so the value waits for a person."""
    sheet = _Set(session, store)
    row = sheet.reading(
        field, inches, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only"
    )
    _crop(session, store, row)
    sheet.check(store, on=True)
    _awaiting_review(sheet)
    return sheet, row


def _awaiting_review(sheet: _Set) -> None:
    """The revision read and checked, as a person finds it (a revision still being read is left
    alone by the re-check: its reading queues its own run)."""
    sheet.revision.state = PackageState.AWAITING_REVIEW.value
    sheet.session.commit()


def _items(client: Any, sheet: _Set) -> Any:
    response = client.get(f"{_base(sheet)}/review-items")
    assert response.status_code == 200, response.text
    return response.json()


#: The check each synthetic reading is asked for here. One reading may be read by several checks
#: (a countertop depth also by the back offset minimum): each lists it, and one Confirm answers all.
WHAT = {CT010: "Countertop depth", CT008: "Sink cut-out depth"}


def _all_depth_items(client: Any, sheet: _Set) -> list[dict[str, Any]]:
    return [item for item in _items(client, sheet)["items"] if item["kind"] == "depth_reading"]


def _depth_items(client: Any, sheet: _Set, field: str = CT010) -> list[dict[str, Any]]:
    return [item for item in _all_depth_items(client, sheet) if item["what"] == WHAT[field]]


def _act(client: Any, sheet: _Set, key: str, action: str, body: dict[str, object]) -> Any:
    return client.post(f"{_base(sheet)}/review-items/{key}/{action}", json=body)


def _confirm(client: Any, sheet: _Set, item: dict[str, Any], shown: str | None = None) -> Any:
    return _act(
        client,
        sheet,
        str(item["key"]),
        "confirm",
        {
            "expected_finding_id": item["finding_id"],
            "shown_value": shown if shown is not None else item["best_guess"]["text"],
        },
    )


def _records(session: Session, candidate_id: UUID) -> dict[str, Any]:
    """What an answer about one reading wrote, without ids or times: the observations citing the
    reading, and each review action on its findings (kind, note, from what to what)."""
    observations = list(
        session.scalars(
            select(CanonicalObservation)
            .join(
                EvidenceSupportingCandidate,
                EvidenceSupportingCandidate.canonical_observation_id == CanonicalObservation.id,
            )
            .where(EvidenceSupportingCandidate.candidate_id == candidate_id)
            .order_by(CanonicalObservation.created_at, CanonicalObservation.id)
        )
    )
    by_id = {observation.id: observation for observation in observations}
    actions = list(
        session.scalars(
            select(ReviewAction)
            .where(
                ReviewAction.original_observation_id.in_(list(by_id))
                | ReviewAction.resulting_observation_id.in_(list(by_id))
            )
            .order_by(ReviewAction.created_at, ReviewAction.id)
        )
    )

    def shape(observation: CanonicalObservation | None) -> tuple[object, ...] | None:
        if observation is None:
            return None
        return (
            observation.status,
            observation.semantic_type,
            Fraction(observation.value_numerator, observation.value_denominator),
            observation.unit,
            observation.document_role,
        )

    return {
        "observations": [shape(observation) for observation in observations],
        "actions": [
            (
                action.action,
                action.actor,
                action.note,
                shape(by_id.get(action.original_observation_id)),  # type: ignore[arg-type]
                shape(by_id.get(action.resulting_observation_id)),  # type: ignore[arg-type]
            )
            for action in actions
        ],
    }


def _person_values(session: Session, candidate_id: UUID) -> list[tuple[object, ...]]:
    """The HUMAN_CONFIRMED values citing the reading: what the depth check reads (person wins)."""
    return [
        shape
        for shape in _records(session, candidate_id)["observations"]
        if shape is not None and shape[0] == "HUMAN_CONFIRMED"
    ]


def _queued(session: Session, sheet: _Set) -> list[OutboxEntry]:
    return list(
        session.scalars(
            select(OutboxEntry).where(
                OutboxEntry.workflow == RUN_CHECKS_WORKFLOW,
                OutboxEntry.dispatched_at.is_(None),
                OutboxEntry.payload["package_revision_id"].astext == str(sheet.revision.id),
            )
        )
    )


# --- the list -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "rule", "what"),
    [
        (CT010, "CT-DEPTH-001", "Countertop depth"),
        (CT008, "CT-SINK-CUTOUT-DEPTH-001", "Sink cut-out depth"),
    ],
)
def test_each_reading_to_confirm_is_one_item_with_the_value_a_confirm_records(
    session: Session, store: LocalStore, field: str, rule: str, what: str
) -> None:
    sheet, row = _waiting(session, store, field)
    client = _client(session, store, sheet)

    (item,) = [item for item in _all_depth_items(client, sheet) if item["what"] == what]

    assert item["candidate_id"] == str(row.id)
    assert item["key"] == f"depth:{item['finding_id']}:{row.id}"
    assert item["best_guess"]["text"] == '25"'
    assert item["actions"] == ["confirm", "not_this"]
    assert item["status"] == "open"
    assert item["page"] == 1
    assert rule  # the rule is named by `what`, the list's plain name for it


# --- Confirm --------------------------------------------------------------------------------------


def _expected(field: str) -> dict[str, Any]:
    """The held-reading evidence decision's records for one confirmed 25" reading (#1339): the
    AI's reading as an observation, the person's HUMAN_CONFIRMED copy citing the reading, and one
    `confirm` on the result from the one to the other, with no note (it decides nothing there)."""
    read = ("RAW_CANDIDATE", field, Fraction(25), "in", "SHOP")
    person = ("HUMAN_CONFIRMED", field, Fraction(25), "in", "SHOP")
    return {"observations": [read, person], "actions": [("confirm", ACTOR, None, read, person)]}


@pytest.mark.parametrize("field", [CT010, CT008])
@pytest.mark.parametrize("path", ["simple review", "evidence decision"])
def test_confirm_writes_exactly_what_the_evidence_decision_writes(
    session: Session, store: LocalStore, field: str, path: str
) -> None:
    """The same records whichever way the reading is confirmed: through the simple review item, or
    the held-reading evidence decision made directly (`confirm_candidate_reading`)."""
    sheet, row = _waiting(session, store, field)
    client = _client(session, store, sheet)
    (item,) = _depth_items(client, sheet, field)

    if path == "simple review":
        response = _confirm(client, sheet, item)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["recorded"].startswith("Confirmed")
        assert body["rechecking"] is True
        assert len(_queued(session, sheet)) == 1
    else:
        sitting = open_session(session, package_revision_id=sheet.revision.id, reviewer=ACTOR)
        confirm_candidate_reading(
            session,
            principal=Principal(id=ACTOR, roles=frozenset({Role.REVIEWER})),
            review_session_id=sitting.id,
            finding_id=UUID(item["finding_id"]),
            candidate_id=row.id,
            expected_value=Fraction(25),
        )
        session.commit()

    assert _records(session, row.id) == _expected(field)


@pytest.mark.parametrize("field", [CT010, CT008])
def test_the_advanced_depth_card_writes_the_same_value_the_check_reads(
    session: Session, store: LocalStore, field: str
) -> None:
    """The advanced depth card's confirm writes the person's value alone (no review action): the
    same HUMAN_CONFIRMED value as the simple path, which is what the depth check reads."""
    sheet, row = _waiting(session, store, field)
    card = _client(session, store, sheet).post(
        f"{_base(sheet)}/candidates/{row.id}/confirm", json={"semantic_type": field}
    )
    assert card.status_code == 201, card.text

    assert _person_values(session, row.id) == [
        shape for shape in _expected(field)["observations"] if shape[0] == "HUMAN_CONFIRMED"
    ]


def test_after_a_confirm_the_item_waits_for_the_recheck_and_is_never_not_checked(
    session: Session, store: LocalStore
) -> None:
    sheet, _row = _waiting(session, store)
    client = _client(session, store, sheet)
    (item,) = _depth_items(client, sheet)

    assert _confirm(client, sheet, item).status_code == 201

    # Every check that reads the reading waits for the re-check (the countertop depth and the back
    # offset minimum): one item each, none offered, none "Not checked".
    listed = _items(client, sheet)
    after = [entry for entry in listed["items"] if entry["kind"] == "depth_reading"]
    assert sorted((entry["what"], entry["status"], entry["actions"]) for entry in after) == [
        ("Back offset minimum", "rechecking", []),
        ("Countertop depth", "rechecking", []),
    ]
    assert item["finding_id"] in {entry["finding_id"] for entry in after}
    assert {entry["finding_id"] for entry in after}.isdisjoint(
        {entry["finding_id"] for entry in listed["not_checked"]}
    )
    assert listed["unaccounted"] == []


def test_a_confirm_names_the_value_shown(session: Session, store: LocalStore) -> None:
    sheet, row = _waiting(session, store)
    client = _client(session, store, sheet)
    (item,) = _depth_items(client, sheet)

    missing = _act(
        client, sheet, str(item["key"]), "confirm", {"expected_finding_id": item["finding_id"]}
    )
    other = _confirm(client, sheet, item, shown='24"')

    assert (missing.status_code, other.status_code) == (422, 409)
    assert _records(session, row.id) == {"observations": [], "actions": []}


def test_a_second_confirm_is_refused_and_writes_nothing(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet, row = _waiting(session, store)
    client = _client(session, store, sheet)
    (item,) = _depth_items(client, sheet)
    assert _confirm(client, sheet, item).status_code == 201
    written = _records(session, row.id)

    # The item is no longer offered: the same click again is stale.
    again = _confirm(client, sheet, item)
    assert again.status_code == 409, again.text

    # Even a screen that still offers it (the list as it was): the evidence decision refuses it.
    _list_as_before(monkeypatch, item)
    forced = _confirm(client, sheet, item)
    assert forced.status_code == 409, forced.text
    assert "only be confirmed once" in forced.json()["message"]
    assert _records(session, row.id) == written


def test_a_reading_the_advanced_card_confirmed_is_not_confirmed_again(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet, row = _waiting(session, store)
    client = _client(session, store, sheet)
    (item,) = _depth_items(client, sheet)
    card = client.post(f"{_base(sheet)}/candidates/{row.id}/confirm", json={"semantic_type": CT010})
    assert card.status_code == 201, card.text
    written = _records(session, row.id)

    assert _confirm(client, sheet, item).status_code == 409
    _list_as_before(monkeypatch, item)
    assert _confirm(client, sheet, item).status_code == 409
    assert _records(session, row.id) == written


def test_another_countertops_reading_is_never_confirmed_for_this_item(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two countertops, each alone on its own drawing; the reading is tied to the first. The second
    countertop's result never lists it, and a Confirm naming it through that result is refused."""
    sheet = _Set(session, store)
    row = sheet.reading(
        CT010, 25, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only"
    )
    _crop(session, store, row)
    other_view = DrawingView(
        page_id=sheet.page.id,
        tag="OTHER",
        region={
            "space": "stored",
            "points": [["0", "0"], ["0.05", "0"], ["0.05", "0.05"], ["0", "0.05"]],
        },
    )
    session.add(other_view)
    session.flush()
    confirm_view_role(session, view=other_view, role=ViewRole.SHOP, actor=ACTOR)
    _other_proposal, other_item = _confirmed(
        session, other_view, PartKind.COUNTERTOP, "0.01", "0.04"
    )
    findings = sheet.check(store, on=True)
    _awaiting_review(sheet)
    (other,) = [f for f in findings["CT-DEPTH-001"] if f.scope_item_id == other_item]
    client = _client(session, store, sheet)

    listed = _items(client, sheet)
    depth = [entry for entry in listed["items"] if entry["kind"] == "depth_reading"]
    assert {entry["candidate_id"] for entry in depth} == {str(row.id)}
    assert all(entry["finding_id"] != str(other.id) for entry in depth)

    # A key naming the other countertop's result with this reading is not listed: stale, nothing.
    forged = {
        **depth[0],
        "key": f"depth:{other.id}:{row.id}",
        "finding_id": str(other.id),
    }
    assert _confirm(client, sheet, forged).status_code == 409

    # Even a list that offered it there: the evidence decision refuses another countertop's reading.
    _list_with(monkeypatch, forged)
    refused = _confirm(client, sheet, forged)
    assert refused.status_code == 422, refused.text
    assert "own countertop" in refused.json()["message"]
    assert _records(session, row.id) == {"observations": [], "actions": []}
    assert _queued(session, sheet) == []


def test_a_reading_not_offered_for_confirming_is_refused_here(
    session: Session, store: LocalStore
) -> None:
    """Without its picture the advanced card does not offer a reading either (`can_confirm`): the
    list offers only Not this, and a Confirm is refused before anything is written."""
    sheet = _Set(session, store)
    row = sheet.reading(
        CT010, 25, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only"
    )
    sheet.check(store, on=True)
    _awaiting_review(sheet)
    client = _client(session, store, sheet)
    (item,) = _depth_items(client, sheet)
    assert item["actions"] == ["not_this"]

    refused = _confirm(client, sheet, item)

    assert refused.status_code == 422, refused.text
    assert _records(session, row.id) == {"observations": [], "actions": []}


# --- Not this -------------------------------------------------------------------------------------


def test_not_this_records_the_check_as_not_checked_and_nothing_on_the_reading(
    session: Session, store: LocalStore
) -> None:
    sheet, row = _waiting(session, store)
    client = _client(session, store, sheet)
    (item,) = _depth_items(client, sheet)

    response = _act(
        client,
        sheet,
        str(item["key"]),
        "answer",
        {"expected_finding_id": item["finding_id"], "answer": "not_this"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["rechecking"] is False
    (action,) = session.scalars(
        select(ReviewAction).where(ReviewAction.finding_id == UUID(item["finding_id"]))
    ).all()
    assert (action.action, action.actor, action.note) == (
        "dismiss",
        ACTOR,
        review_item_actions.DEPTH_NOT_THIS_NOTE,
    )
    assert action.original_observation_id is None and action.resulting_observation_id is None
    sitting = session.get_one(ReviewSession, action.review_session_id)
    assert (sitting.reviewer, sitting.package_revision_id) == (ACTOR, sheet.revision.id)
    # Printed NOT CHECKED on the signed record and the cloud PDF, with the plain reason.
    words = ReviewDisposition(
        action_id=action.id,
        action="dismiss",
        reviewer=ACTOR,
        at=action.created_at,
        note=action.note,
    ).wording("REVIEW_REQUIRED")
    assert words.startswith("Not checked:")
    # Nothing on the reading, and nothing a check reads changed: no re-check.
    assert _records(session, row.id) == {"observations": [], "actions": []}
    assert _queued(session, sheet) == []

    (after,) = _depth_items(client, sheet)
    assert (after["status"], after["actions"], after["decided_by"]) == ("done", [], ACTOR)
    # Answered once: a Confirm afterwards is refused, and writes nothing.
    assert _confirm(client, sheet, item).status_code == 409
    assert _records(session, row.id) == {"observations": [], "actions": []}


def test_no_answer_on_a_depth_or_sink_item_is_501_any_more(
    session: Session, store: LocalStore
) -> None:
    assert not hasattr(review_item_actions, "DEPTH_NOT_YET")
    sheet, _row = _waiting(session, store, CT008)
    client = _client(session, store, sheet)
    (item,) = _depth_items(client, sheet, CT008)

    for action, body in (
        ("confirm", {"shown_value": '25"'}),
        ("answer", {"answer": "not_this"}),
    ):
        response = _act(
            client,
            sheet,
            str(item["key"]),
            action,
            {"expected_finding_id": item["finding_id"], **body},
        )
        assert response.status_code != 501, response.text


# --- helpers --------------------------------------------------------------------------------------


def _list_as_before(monkeypatch: pytest.MonkeyPatch, item: dict[str, Any]) -> None:
    """The list as a stale screen saw it: the item still offered, open."""
    _list_with(monkeypatch, item)


def _list_with(monkeypatch: pytest.MonkeyPatch, item: dict[str, Any]) -> None:
    real = review_items_for_revision
    offered = ReviewItemOut.model_validate({**item, "status": "open"})

    def patched(*args: object, **kwargs: object) -> ReviewItemsOut:
        listed = real(*args, **kwargs)  # type: ignore[arg-type]
        listed.items = [entry for entry in listed.items if entry.key != offered.key] + [offered]
        return listed

    monkeypatch.setattr(review_item_actions, "review_items_for_revision", patched)
