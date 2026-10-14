"""A person's "not a part" answer keeps its reason (#1246, with phase 5 decision 6).

Verification for `workflow/parts.py:withdraw_part`'s `reason`, `app/evidence/parts.py:
withdraw_listed_part`, and `part_confirmations.not_part_reason` (migration 0097).

On one of Bala's pieces the Measure page asks "what is it?". "Appliance space", "Side panel" and
"Something else" each say it is not a part, and each now keeps which one the person chose, so the
page can show it after a reload. "Not a part" and Undo keep none, and code never gives one.

**Every value here is synthetic**, built with `tests/workflow/test_bala_items.py`'s revision.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.evidence.parts import revision_parts, withdraw_listed_part
from app.models import NotPartReason, PartConfirmation, PartProposal
from tests.workflow.test_bala_items import _links, _Revision
from tests.workflow.test_part_proposals_route import _upgrade
from vocabulary.part_kinds import PartKind
from workflow.bala_items import propose_items
from workflow.parts import (
    BALA_CONFIRMER,
    confirm_part,
    confirm_part_by_code,
    current_decision,
    withdraw_part,
)

pytest_plugins = ("tests.app.postgres_fixture",)

PERSON = "reviewer@example.com"


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _pieces(session: Session, count: int = 3) -> tuple[_Revision, list[PartProposal]]:
    """A revision whose one countertop row has `count` pieces, as Bala suggested them."""
    built = _Revision(session)
    anchor = built.row(built.run(), pieces=count)
    propose_items(session, built.revision.id)
    links = _links(session)
    return built, [links[(anchor, index)] for index in range(count)]


def _listed_reasons(session: Session, built: _Revision) -> dict[object, str | None]:
    """What the Measure page's list says each decided part's reason is, read afresh."""
    session.expire_all()
    return {
        part.proposal.id: part.decision.not_part_reason
        for drawing in revision_parts(session, built.revision.id)
        for part in drawing.parts
        if part.decision is not None
    }


@pytest.mark.parametrize("reason", list(NotPartReason))
def test_each_answer_keeps_its_reason_after_a_reload(
    session: Session, reason: NotPartReason
) -> None:
    built, (piece, *_) = _pieces(session)

    outcome = withdraw_listed_part(
        session,
        package_revision_id=built.revision.id,
        proposal_id=piece.id,
        actor=PERSON,
        reason=reason,
    )
    assert isinstance(outcome, PartConfirmation)
    session.commit()

    assert _listed_reasons(session, built) == {piece.id: reason.value}
    stored = session.scalars(select(PartConfirmation)).one()
    assert stored.decision == "withdrawn" and stored.not_part_reason == reason.value


def test_the_three_answers_stay_apart(session: Session) -> None:
    """Appliance space, side panel and something else on three pieces: three different answers."""
    built, pieces = _pieces(session)
    for piece, reason in zip(pieces, NotPartReason, strict=True):
        withdraw_part(session, proposal=piece, actor=PERSON, reason=reason)
    session.commit()

    assert _listed_reasons(session, built) == {
        pieces[0].id: "appliance-space",
        pieces[1].id: "side-panel",
        pieces[2].id: "other",
    }


def test_not_a_part_with_no_reason_keeps_none(session: Session) -> None:
    built, (piece, *_) = _pieces(session)
    withdraw_part(session, proposal=piece, actor=PERSON)
    session.commit()

    assert _listed_reasons(session, built) == {piece.id: None}


def test_a_later_decision_replaces_the_reason(session: Session) -> None:
    """A person changing their mind: the newest decision is the one listed, every one kept."""
    built, (piece, *_) = _pieces(session)
    withdraw_part(session, proposal=piece, actor=PERSON, reason=NotPartReason.APPLIANCE_SPACE)
    withdraw_part(session, proposal=piece, actor=PERSON, reason=NotPartReason.SIDE_PANEL)
    session.commit()

    assert _listed_reasons(session, built) == {piece.id: "side-panel"}
    kept = session.scalars(
        select(PartConfirmation.not_part_reason).order_by(PartConfirmation.created_at)
    ).all()
    assert kept == ["appliance-space", "side-panel"]

    confirm_part(session, proposal=piece, kind=PartKind.CABINET, code=None, actor=PERSON)
    session.commit()
    assert _listed_reasons(session, built) == {piece.id: None}


def test_a_reason_is_refused_on_a_confirmation(session: Session) -> None:
    """A confirmation says what a part is; the database refuses one that also says why it is not."""
    _, (piece, *_) = _pieces(session)
    confirm_part(session, proposal=piece, kind=PartKind.CABINET, code=None, actor=PERSON)
    session.commit()
    confirmed = session.scalars(select(PartConfirmation)).one()

    with pytest.raises(IntegrityError, match="part_confirmation_not_part_reason"):
        session.execute(
            text(
                "INSERT INTO part_confirmations (id, created_at, part_proposal_id, supersedes_id, "
                "decision, kind, drawing_item_id, confirmed_by, not_part_reason) "
                "SELECT gen_random_uuid(), now(), part_proposal_id, id, 'confirmed', kind, "
                "drawing_item_id, confirmed_by, 'side-panel' FROM part_confirmations "
                "WHERE id = :id"
            ),
            {"id": confirmed.id},
        )
    session.rollback()


def test_an_unknown_reason_is_refused(session: Session) -> None:
    _, (piece, *_) = _pieces(session)
    with pytest.raises(ValueError):
        withdraw_part(session, proposal=piece, actor=PERSON, reason="a-cupboard")  # type: ignore[arg-type]
    session.rollback()
    _, (piece, *_) = _pieces(session)
    session.add(
        PartConfirmation(
            part_proposal_id=piece.id,
            decision="withdrawn",
            confirmed_by=PERSON,
            not_part_reason="a-cupboard",
        )
    )
    with pytest.raises(IntegrityError, match="part_confirmation_not_part_reason"):
        session.flush()
    session.rollback()


def test_code_never_gives_a_reason(session: Session) -> None:
    """Code takes back only its own countertop, and it cannot know what a piece is."""
    built = _Revision(session)
    anchor = built.row(built.run(), pieces=1)
    propose_items(session, built.revision.id)
    countertop = _links(session)[(anchor, None)]
    assert confirm_part_by_code(
        session, proposal=countertop, kind=PartKind.COUNTERTOP, reason="both agree"
    )

    with pytest.raises(ValueError, match="reason"):
        withdraw_part(
            session, proposal=countertop, actor=BALA_CONFIRMER, reason=NotPartReason.OTHER
        )

    # And the database refuses one too, from any writer.
    replaced = current_decision(session, countertop)
    assert replaced is not None
    session.add(
        PartConfirmation(
            part_proposal_id=countertop.id,
            supersedes_id=replaced.id,
            decision="withdrawn",
            confirmed_by=BALA_CONFIRMER,
            not_part_reason="other",
        )
    )
    with pytest.raises(IntegrityError, match="part_confirmation_not_part_reason"):
        session.flush()
    session.rollback()


def test_the_reason_is_append_only(session: Session) -> None:
    _, (piece, *_) = _pieces(session)
    withdraw_part(session, proposal=piece, actor=PERSON, reason=NotPartReason.SIDE_PANEL)
    session.commit()

    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(text("UPDATE part_confirmations SET not_part_reason = 'other'"))
    session.rollback()
