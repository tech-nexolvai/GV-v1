"""The Measure page's parts of each drawing: listed, then confirmed one at a time by a person (#882).

Verification for `app/api/drawing_parts.py` and `app/evidence/parts.py`, and for the match stage
reading only parts a person confirmed and has not taken back.

The sheet is `tests/workflow/test_part_proposals_route.py`'s: one vendor drawing, labelled as the
vendor's but not yet confirmed, on which the page stage suggests two cabinets (the first under the
invented code `XQ24`) and a countertop. **Every code here is invented.**

**The outcome that matters most is a count of zero**: listing the suggestions, however often, makes
no part. A suggestion becomes a part only through a person's confirmation (#852).
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.audit.events import AuditEvent
from app.db.session import session_factory
from app.evidence.parts import ADDED_BY_A_PERSON
from app.models import (
    DocumentKind,
    DrawingItem,
    DrawingView,
    ItemIdentifier,
    Package,
    PackageRevision,
    PartConfirmation,
    PartProposal,
    ViewRole,
)
from storage.local import LocalStore
from tests.api.test_drawing_views import _client
from tests.workflow.test_part_proposals_route import _extract, _upgrade
from tests.workflow.test_view_roles import _document_version, _item
from tests.workflow.test_view_roles import _revision as _bare_revision
from workflow import parts as workflow_parts
from workflow.stages import DatabaseStages, _matchable_items
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)

#: An invented code, corrected as a person might read it, with the spacing they typed.
CORRECTED_CODE = " xq-24/B "


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _sheet(session: Session, store: LocalStore) -> tuple[PackageRevision, UUID, UUID]:
    """The extracted sheet: its revision, its project and its package."""
    revision, _ = _extract(session, store)
    package = session.get_one(Package, revision.package_id)
    return revision, package.project_id, package.id


def _base(project_id: UUID, package_id: UUID) -> str:
    return f"/api/v1/projects/{project_id}/packages/{package_id}"


def _drawing(client: Any, project_id: UUID, package_id: UUID) -> dict[str, Any]:
    response = client.get(f"{_base(project_id, package_id)}/parts")
    assert response.status_code == 200, response.text
    drawings: list[dict[str, Any]] = response.json()["drawings"]
    assert len(drawings) == 1
    return drawings[0]


def _vendors(session: Session, role: ViewRole = ViewRole.SHOP) -> DrawingView:
    view = session.scalars(select(DrawingView)).one()
    confirm_view_role(session, view=view, role=role, actor="reviewer@example.com")
    session.commit()
    return view


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _nothing_decided(session: Session) -> None:
    assert _count(session, DrawingItem) == 0
    assert _count(session, ItemIdentifier) == 0
    assert _count(session, PartConfirmation) == 0


# -- listing -------------------------------------------------------------------------------------


def test_a_suggestion_never_becomes_a_part_without_a_confirmation(
    session: Session, store: LocalStore
) -> None:
    """**Done when, 1, and the list-endpoint mutation.** Listing twice, on an unconfirmed drawing and
    then on one confirmed as the vendor's, writes no item, no code and no decision."""
    _, project_id, package_id = _sheet(session, store)
    client = _client(session, store, project_id)

    drawing = _drawing(client, project_id, package_id)
    _vendors(session)
    _drawing(client, project_id, package_id)

    assert [part["suggested_kind"] for part in drawing["parts"]] == [
        "cabinet",
        "countertop",
        "cabinet",
    ]
    assert all(part["decision"] is None for part in drawing["parts"])
    _nothing_decided(session)


def test_the_suggestions_are_listed_left_to_right_with_what_each_one_needs(
    session: Session, store: LocalStore
) -> None:
    """Left end first: the first cabinet and the countertop share theirs, and the cabinet ends
    sooner. Each says why it was suggested, and the code read over the first cabinet comes too."""
    _, project_id, package_id = _sheet(session, store)

    drawing = _drawing(_client(session, store, project_id), project_id, package_id)

    parts = drawing["parts"]
    lefts = [Decimal(part["left_end"]["x"]) for part in parts]
    rights = [Decimal(part["right_end"]["x"]) for part in parts]
    assert [part["position"] for part in parts] == [1, 2, 3]
    assert lefts[0] == lefts[1] < lefts[2]
    assert rights[0] < rights[1]
    assert rights[0] == lefts[2] and rights[1] == rights[2]
    assert [part["suggested_code"] for part in parts] == ["XQ24", None, None]
    assert all(part["reason"].strip() for part in parts)
    assert not any(part["added_by_a_person"] for part in parts)
    # Nothing has cut the pictures yet, so none is offered.
    assert [part["has_crop"] for part in parts] == [False, False, False]
    # The label suggests the vendor's drawing; nobody has said so, so nothing can be confirmed yet.
    assert drawing["role"] is None
    assert drawing["can_confirm"] is False
    assert "Confirm it is the vendor's drawing first" in drawing["why_not"]


def test_the_picture_is_the_stored_crop_of_the_code_reading(
    session: Session, store: LocalStore
) -> None:
    """Once the evidence stage has cut every reading's crop, the coded cabinet has a picture and the
    others say they have none rather than showing some other region."""
    revision, project_id, package_id = _sheet(session, store)
    DatabaseStages(store=store, dpi=150).validate_evidence(session, revision.id)
    session.commit()
    client = _client(session, store, project_id)

    parts = _drawing(client, project_id, package_id)["parts"]
    crops = [
        client.get(f"{_base(project_id, package_id)}/parts/{p['proposal_id']}/crop") for p in parts
    ]

    assert [part["has_crop"] for part in parts] == [True, False, False]
    assert crops[0].status_code == 200, crops[0].text
    assert crops[0].headers["content-type"] == "image/png"
    assert crops[0].content.startswith(b"\x89PNG\r\n\x1a\n")
    assert [response.status_code for response in crops[1:]] == [404, 404]


# -- which drawings may have parts -----------------------------------------------------------------


def test_a_part_on_an_unconfirmed_drawing_is_refused(session: Session, store: LocalStore) -> None:
    """**Done when, 2.** The label suggests the vendor's drawing, which aims a suggestion but makes
    no part: confirming and adding are both refused, and nothing is written."""
    _, project_id, package_id = _sheet(session, store)
    client = _client(session, store, project_id)
    drawing = _drawing(client, project_id, package_id)
    first = drawing["parts"][0]

    confirmed = client.post(
        f"{_base(project_id, package_id)}/parts/{first['proposal_id']}/confirm",
        json={"kind": "cabinet", "code": "XQ24"},
    )
    added = client.post(
        f"{_base(project_id, package_id)}/views/{drawing['view_id']}/parts",
        json={"kind": "filler", "ends": [first["left_end"], first["right_end"]]},
    )

    assert confirmed.status_code == 409
    assert "Confirm it is the vendor's drawing first" in confirmed.json()["message"]
    assert added.status_code == 409
    _nothing_decided(session)
    assert _count(session, PartProposal) == 3


def test_a_part_on_the_architects_drawing_is_refused_but_can_still_be_withdrawn(
    session: Session, store: LocalStore
) -> None:
    """**Done when, 2.** A person said this is the architect's drawing. Its old suggestions cannot be
    confirmed, nor a part added; saying one is not a part still works, because it makes nothing."""
    _, project_id, package_id = _sheet(session, store)
    _vendors(session, ViewRole.ARCH)
    client = _client(session, store, project_id)
    drawing = _drawing(client, project_id, package_id)
    first = drawing["parts"][0]

    confirmed = client.post(
        f"{_base(project_id, package_id)}/parts/{first['proposal_id']}/confirm",
        json={"kind": "cabinet"},
    )
    added = client.post(
        f"{_base(project_id, package_id)}/views/{drawing['view_id']}/parts",
        json={"kind": "cabinet", "ends": [first["left_end"], first["right_end"]]},
    )
    withdrawn = client.post(
        f"{_base(project_id, package_id)}/parts/{first['proposal_id']}/withdraw"
    )

    assert drawing["can_confirm"] is False
    assert "architect's drawing" in drawing["why_not"]
    assert confirmed.status_code == 409
    assert "architect's drawing" in confirmed.json()["message"]
    assert added.status_code == 409
    assert withdrawn.status_code == 201, withdrawn.text
    assert withdrawn.json()["decision"]["decision"] == "withdrawn"
    assert _count(session, DrawingItem) == 0


# -- deciding ------------------------------------------------------------------------------------


def test_a_reviewer_confirms_one_suggestion_and_that_alone_makes_a_part(
    session: Session, store: LocalStore
) -> None:
    """**Done when, 6.** A reviewer may confirm. One suggestion, one item, of the kind the person
    chose rather than the one suggested, and an audit event naming them."""
    _, project_id, package_id = _sheet(session, store)
    _vendors(session)
    client = _client(session, store, project_id, "reviewer")
    third = _drawing(client, project_id, package_id)["parts"][2]

    response = client.post(
        f"{_base(project_id, package_id)}/parts/{third['proposal_id']}/confirm",
        json={"kind": "filler", "code": None},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["proposal_id"] == third["proposal_id"]
    assert body["suggested_kind"] == "cabinet"
    assert body["decision"]["decision"] == "confirmed"
    assert body["decision"]["kind"] == "filler"
    assert body["decision"]["code"] is None
    assert body["decision"]["decided_by"] == "reviewer@example.com"
    item = session.scalars(select(DrawingItem)).one()
    assert item.item_type == "filler_width"
    assert _count(session, ItemIdentifier) == 0
    confirmation = session.scalars(select(PartConfirmation)).one()
    assert confirmation.drawing_item_id == item.id
    audited = session.scalars(
        select(AuditEvent).where(AuditEvent.target_id == confirmation.id)
    ).one()
    assert (audited.actor, audited.target_type) == ("reviewer@example.com", "part_confirmation")


def test_a_corrected_code_is_stored_verbatim(session: Session, store: LocalStore) -> None:
    """**Done when, 4.** The person's code, with the spacing they typed, is the code kept: on the
    decision and on the item's identifier. What the computer read stays on the suggestion."""
    _, project_id, package_id = _sheet(session, store)
    _vendors(session)
    client = _client(session, store, project_id)
    first = _drawing(client, project_id, package_id)["parts"][0]

    response = client.post(
        f"{_base(project_id, package_id)}/parts/{first['proposal_id']}/confirm",
        json={"kind": "cabinet", "code": CORRECTED_CODE},
    )

    assert response.status_code == 201, response.text
    assert response.json()["decision"]["code"] == CORRECTED_CODE
    assert session.scalars(select(PartConfirmation.code_as_printed)).one() == CORRECTED_CODE
    identifier = session.scalars(select(ItemIdentifier)).one()
    assert (identifier.kind, identifier.value_as_printed) == ("catalogue", CORRECTED_CODE)
    proposal = session.get_one(PartProposal, UUID(first["proposal_id"]))
    assert proposal.code_as_printed == "XQ24"


def test_a_blank_code_is_refused(session: Session, store: LocalStore) -> None:
    """A code of spaces is no code; the person leaves it out instead."""
    _, project_id, package_id = _sheet(session, store)
    _vendors(session)
    client = _client(session, store, project_id)
    first = _drawing(client, project_id, package_id)["parts"][0]

    response = client.post(
        f"{_base(project_id, package_id)}/parts/{first['proposal_id']}/confirm",
        json={"kind": "cabinet", "code": "   "},
    )

    assert response.status_code == 422
    assert "Leave it out if none is printed" in response.json()["message"]
    _nothing_decided(session)


def test_a_withdrawn_or_corrected_part_is_not_read_by_matching(
    session: Session, store: LocalStore
) -> None:
    """**Done when, 3.** The match stage reads the part while it stands, nothing once it is
    withdrawn, and only the new item once a person corrects it. The old items keep their rows.

    Once withdrawn, the stage's own report counts no item either, and says no part is confirmed:
    counting the withdrawn one would send a person to confirm the architect's drawing instead."""
    revision, project_id, package_id = _sheet(session, store)
    _vendors(session)
    client = _client(session, store, project_id)
    first = _drawing(client, project_id, package_id)["parts"][0]
    url = f"{_base(project_id, package_id)}/parts/{first['proposal_id']}"

    def read_by_matching() -> set[UUID]:
        return {item.item_id for item, _ in _matchable_items(session, revision.id)}

    assert client.post(f"{url}/confirm", json={"kind": "cabinet"}).status_code == 201
    standing = read_by_matching()
    assert client.post(f"{url}/withdraw").status_code == 201
    withdrawn = read_by_matching()
    reported = DatabaseStages().match(session, revision.id)
    assert client.post(f"{url}/confirm", json={"kind": "filler"}).status_code == 201
    corrected = read_by_matching()

    items = {row.id: row.item_type for row in session.scalars(select(DrawingItem))}
    assert len(items) == 2
    assert len(standing) == 1 and withdrawn == set()
    assert (reported["items"], reported["candidates"]) == (0, 0)
    assert "no confirmed parts exist" in str(reported["reason"])
    assert len(corrected) == 1 and corrected != standing
    assert items[corrected.pop()] == "filler_width"


def test_two_decisions_at_the_same_moment_are_not_both_recorded(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second of two people deciding at once is told to reload, not shown an error page.

    Simulated by hiding the first decision from the second, as a concurrent request would not see
    it: the database's one-current-decision rule refuses the second."""
    _, project_id, package_id = _sheet(session, store)
    _vendors(session)
    client = _client(session, store, project_id)
    first = _drawing(client, project_id, package_id)["parts"][0]
    url = f"{_base(project_id, package_id)}/parts/{first['proposal_id']}/confirm"
    assert client.post(url, json={"kind": "cabinet"}).status_code == 201

    monkeypatch.setattr(workflow_parts, "current_decision", lambda *_: None)
    response = client.post(url, json={"kind": "filler"})

    assert response.status_code == 409
    assert "Reload the page" in response.json()["message"]
    assert _count(session, PartConfirmation) == 1


# -- adding --------------------------------------------------------------------------------------


def test_a_person_adds_a_missing_part_by_its_two_ends(session: Session, store: LocalStore) -> None:
    """Ends sent right first are kept left first. The part is the person's own suggestion, decided
    at once, with their code on the decision; its outline is the line between the ends."""
    _, project_id, package_id = _sheet(session, store)
    _vendors(session)
    client = _client(session, store, project_id)
    drawing = _drawing(client, project_id, package_id)
    first, _, second = drawing["parts"]

    response = client.post(
        f"{_base(project_id, package_id)}/views/{drawing['view_id']}/parts",
        json={"kind": "filler", "code": "F3", "ends": [second["right_end"], first["right_end"]]},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["added_by_a_person"] is True
    assert (body["left_end"], body["right_end"]) == (first["right_end"], second["right_end"])
    assert body["decision"]["kind"] == "filler" and body["decision"]["code"] == "F3"
    added = session.get_one(PartProposal, UUID(body["proposal_id"]))
    assert added.source == ADDED_BY_A_PERSON
    assert added.code_as_printed is None and added.code_candidate_id is None
    assert added.extent["points"] == [
        [first["right_end"]["x"], first["right_end"]["y"]],
        [second["right_end"]["x"], second["right_end"]["y"]],
    ]
    assert _count(session, DrawingItem) == 1
    after = _drawing(client, project_id, package_id)["parts"]
    assert [part["proposal_id"] for part in after].count(body["proposal_id"]) == 1


def _off_the_page(left: dict[str, str], top: dict[str, str]) -> list[dict[str, str]]:
    return [{"x": "2", "y": left["y"]}, left]


def _one_above_the_other(left: dict[str, str], top: dict[str, str]) -> list[dict[str, str]]:
    return [left, {"x": left["x"], "y": top["y"]}]


def _not_a_number(left: dict[str, str], top: dict[str, str]) -> list[dict[str, str]]:
    return [{"x": "a quarter", "y": left["y"]}, left]


def _nan(left: dict[str, str], top: dict[str, str]) -> list[dict[str, str]]:
    return [{"x": "NaN", "y": left["y"]}, left]


@pytest.mark.parametrize(
    ("ends", "said"),
    [
        (_off_the_page, "not on this drawing"),
        (_one_above_the_other, "one above the other"),
        (_not_a_number, "not on this drawing"),
        (_nan, "not on this drawing"),
    ],
    ids=["off-the-page", "one-above-the-other", "not-a-number", "nan"],
)
def test_ends_that_do_not_span_a_part_on_the_drawing_are_refused(
    session: Session, store: LocalStore, ends: Any, said: str
) -> None:
    """Each built from points the drawing really has, so each fails for its own reason only: the
    first cabinet's left end, and the height of the countertop's line, which is on the drawing."""
    _, project_id, package_id = _sheet(session, store)
    _vendors(session)
    client = _client(session, store, project_id)
    drawing = _drawing(client, project_id, package_id)
    first, top, _ = drawing["parts"]

    response = client.post(
        f"{_base(project_id, package_id)}/views/{drawing['view_id']}/parts",
        json={"kind": "cabinet", "ends": ends(first["left_end"], top["left_end"])},
    )

    assert response.status_code == 422
    assert said in response.json()["message"]
    _nothing_decided(session)
    assert _count(session, PartProposal) == 3


def test_a_drawing_with_no_outline_on_the_page_takes_no_added_part(
    session: Session, store: LocalStore
) -> None:
    """A vendor's drawing whose region is not kept as stored points has no box to place an end in,
    so adding a part to it is refused, rather than the request failing."""
    revision = _bare_revision(session)
    version = _document_version(session, revision, document_kind=DocumentKind.SHOP)
    view_id = session.get_one(
        DrawingItem, _item(session, version, view_role=ViewRole.SHOP, tag="S").id
    ).drawing_view_id
    session.commit()
    project_id = session.get_one(Package, revision.package_id).project_id

    response = _client(session, store, project_id).post(
        f"{_base(project_id, revision.package_id)}/views/{view_id}/parts",
        json={"kind": "cabinet", "ends": [{"x": "0.2", "y": "0.5"}, {"x": "0.4", "y": "0.5"}]},
    )

    assert response.status_code == 422
    assert "outline is not recorded on the page" in response.json()["message"]
    assert _count(session, DrawingItem) == 1


# -- who may ---------------------------------------------------------------------------------------


def test_someone_who_cannot_confirm_evidence_cannot_decide_a_part(
    session: Session, store: LocalStore
) -> None:
    """**Done when, 6.** A rule administrator may read the list but not decide; the refusal looks
    like an absence, as every refusal here does."""
    _, project_id, package_id = _sheet(session, store)
    view = _vendors(session)
    client = _client(session, store, project_id, "rule_admin")
    first = _drawing(client, project_id, package_id)["parts"][0]
    base = _base(project_id, package_id)

    responses = [
        client.post(f"{base}/parts/{first['proposal_id']}/confirm", json={"kind": "cabinet"}),
        client.post(f"{base}/parts/{first['proposal_id']}/withdraw"),
        client.post(
            f"{base}/views/{view.id}/parts",
            json={"kind": "cabinet", "ends": [first["left_end"], first["right_end"]]},
        ),
    ]

    assert [response.status_code for response in responses] == [404, 404, 404]
    _nothing_decided(session)


def test_another_projects_part_is_not_found(session: Session, store: LocalStore) -> None:
    """**Done when, 6.** A part of project A, asked for through project B's own package, is absent:
    in the list, its picture, every decision, and adding to A's drawing through B."""
    _, project_a, package_a = _sheet(session, store)
    _vendors(session)
    proposal_id = _drawing(_client(session, store, project_a), project_a, package_a)["parts"][0][
        "proposal_id"
    ]
    view_a = session.scalars(select(DrawingView)).one()
    _, project_b, package_b = _sheet(session, store)
    client = _client(session, store, project_b)
    base = _base(project_b, package_b)

    responses = [
        client.get(f"{base}/parts/{proposal_id}/crop"),
        client.post(f"{base}/parts/{proposal_id}/confirm", json={"kind": "cabinet"}),
        client.post(f"{base}/parts/{proposal_id}/withdraw"),
        client.post(
            f"{base}/views/{view_a.id}/parts",
            json={"kind": "cabinet", "ends": [{"x": "0.2", "y": "0.5"}, {"x": "0.4", "y": "0.5"}]},
        ),
        client.get(f"{_base(project_a, package_a)}/parts"),
    ]

    assert [response.status_code for response in responses] == [404, 404, 404, 404, 404]
    assert proposal_id not in {
        part["proposal_id"] for part in _drawing(client, project_b, package_b)["parts"]
    }
    _nothing_decided(session)


def test_an_unknown_part_is_not_found(session: Session, store: LocalStore) -> None:
    _, project_id, package_id = _sheet(session, store)
    _vendors(session)

    response = _client(session, store, project_id).post(
        f"{_base(project_id, package_id)}/parts/{uuid4()}/confirm", json={"kind": "cabinet"}
    )

    assert response.status_code == 404
