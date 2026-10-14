"""Code makes a countertop item with no click only when two judgments agree (#1244).

Verification for `workflow/parts.py:confirm_part_by_code` and `release_contradicted_code_part`, the
two judgments in `workflow/bala_items.py` (`WouldBeAutomatic`, `confirm_automatic_countertops`),
their call in `DatabaseStages.match` behind `GV_BALA_AUTO_ITEMS`, and the Measure page's listing of
code's items (`app/evidence/parts.py`). The ADR-0021 amendment of 2026-10-14.

**Every value here is synthetic**, built with `tests/workflow/test_bala_items.py`'s revision: a row
of three pieces under a printed overall, both readers naming it, every width sealed, inside one
drawing a person confirmed as the vendor's. That row qualifies; each test takes one thing away.

**The outcomes that matter most:** a person always wins, code takes back only its own item, a piece
is never automatic, and no automatic item reaches a check (`countertop_scopes` stays `None`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.audit.events import AuditCategory, AuditEvent
from app.db.session import session_factory
from app.evidence.parts import (
    PartRefusalReason,
    PartRefused,
    add_part,
    confirm_listed_part,
    revision_parts,
    withdraw_listed_part,
)
from app.models import (
    DrawingItem,
    DrawingView,
    ItemIdentifier,
    ObservationCandidate,
    PartConfirmation,
    PartProposal,
    SlotRowReviewDecision,
    ViewRole,
)
from app.models.runs import ExtractionRun
from tests.workflow.test_bala_items import _links, _Revision
from tests.workflow.test_part_proposals_route import _upgrade
from vocabulary.part_kinds import PartKind
from vocabulary.semantic_types import SemanticType
from workflow.bala_items import PIECES_STAY_ONE_CLICK, plan_items, propose_items
from workflow.part_operands import countertop_scopes, manual_run_pages, reviewer_owned_pages
from workflow.parts import (
    BALA_CONFIRMER,
    confirm_part,
    confirm_part_by_code,
    current_decision,
    live_part_item_ids,
    release_contradicted_code_part,
    withdraw_part,
)
from workflow.stages import DatabaseStages
from workflow.view_roles import confirm_view_role

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


def _vendors(session: Session, built: _Revision, role: ViewRole = ViewRole.SHOP) -> None:
    """A person confirms whose drawing each view is, as the Measure page records it."""
    for view in built.views:
        confirm_view_role(session, view=view, role=role, actor=PERSON)


def _qualifying(session: Session, **row: object) -> tuple[_Revision, UUID]:
    built = _Revision(session, role=None)
    _vendors(session, built)
    anchor = built.row(built.run(), pieces=3, **row)  # type: ignore[arg-type]
    return built, anchor


def _match(session: Session, built: _Revision, *, on: bool = True) -> dict[str, object]:
    result = DatabaseStages(bala_auto_items=on).match(session, built.revision.id)
    bala = result["bala"]
    assert isinstance(bala, dict)
    return bala


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _live_items(session: Session) -> list[DrawingItem]:
    return list(
        session.scalars(select(DrawingItem).where(DrawingItem.id.in_(live_part_item_ids())))
    )


def _countertop(session: Session, anchor: UUID) -> PartProposal:
    return _links(session)[(anchor, None)]


# -- both judgments agree: code makes the countertop, and nothing else ----------------------------


def test_two_judgments_agreeing_make_the_countertop_item_by_code(session: Session) -> None:
    built, anchor = _qualifying(session)

    bala = _match(session, built)

    assert bala["items_written"] == 1
    automatic = bala["automatic"]
    assert isinstance(automatic, dict)
    assert automatic["switched_on"] is True
    assert automatic["confirmed"] == ["Countertop row on page 1"]
    countertop = _countertop(session, anchor)
    decision = current_decision(session, countertop)
    assert decision is not None
    assert decision.confirmed_by == BALA_CONFIRMER
    assert decision.kind == PartKind.COUNTERTOP.value and decision.code_as_printed is None
    (item,) = _live_items(session)
    assert item.id == decision.drawing_item_id
    assert item.item_type == SemanticType.COUNTERTOP_OVERALL_WIDTH.value
    assert item.extent == countertop.extent
    # Never corroborated, and no identifier: the confirmation is the authority.
    assert item.corroborated is False
    assert _count(session, ItemIdentifier) == 0
    # Code's decision is evidence qualification, never a review action.
    event = session.scalars(select(AuditEvent).where(AuditEvent.target_id == decision.id)).one()
    assert event.category == AuditCategory.EVIDENCE_QUALIFICATION.value
    assert event.actor == BALA_CONFIRMER
    # Pieces stay a person's click.
    pieces = [proposal for (row, slot), proposal in _links(session).items() if slot is not None]
    assert len(pieces) == 3
    assert all(current_decision(session, piece) is None for piece in pieces)
    # Asked again: nothing more.
    again = _match(session, built)
    assert again["items_written"] == 0
    assert _count(session, PartConfirmation) == 1


def test_switched_off_code_makes_no_item(session: Session) -> None:
    built, _anchor = _qualifying(session)

    bala = _match(session, built, on=False)

    assert bala["items_written"] == 0
    assert bala["automatic"] == {
        "switched_on": False,
        "countertops_confirmed": 0,
        "confirmed": [],
        "left_to_a_person": [],
        "released": [],
    }
    # It qualifies, and is still only reported.
    assert bala["would_be_automatic"] == {"countertops": 1, "pieces": 0}
    assert _count(session, DrawingItem) == 0
    # The default stages are off.
    DatabaseStages().match(session, built.revision.id)
    assert _count(session, DrawingItem) == 0


def test_an_automatic_item_never_reaches_a_check(session: Session) -> None:
    """Why code's items cannot create a PASS: the countertop checks read items only once a person
    confirms a countertop's run, and code writes no run. So the findings are those of the legacy
    path, unchanged (the $0 proof compares them byte for byte)."""
    built, _anchor = _qualifying(session)
    assert countertop_scopes(session, built.revision.id) is None

    _match(session, built)

    assert len(_live_items(session)) == 1
    assert countertop_scopes(session, built.revision.id) is None
    assert reviewer_owned_pages(session, built.revision.id) == set()
    assert manual_run_pages(session, built.revision.id) == set()


# -- a person always wins ---------------------------------------------------------------------------


def test_code_does_not_decide_a_suggestion_a_person_decided(session: Session) -> None:
    built, anchor = _qualifying(session)
    propose_items(session, built.revision.id)
    withdrawn = withdraw_listed_part(
        session,
        package_revision_id=built.revision.id,
        proposal_id=_countertop(session, anchor).id,
        actor=PERSON,
    )
    assert isinstance(withdrawn, PartConfirmation)

    bala = _match(session, built)

    assert bala["items_written"] == 0
    assert _count(session, DrawingItem) == 0
    automatic = bala["automatic"]
    assert isinstance(automatic, dict)
    assert automatic["left_to_a_person"] == [
        "Countertop row on page 1: a decision already stands on it"
    ]


def test_code_does_not_confirm_where_a_person_added_the_countertop(session: Session) -> None:
    built, _anchor = _qualifying(session)
    added = add_part(
        session,
        package_revision_id=built.revision.id,
        view_id=built.views[0].id,
        kind=PartKind.COUNTERTOP,
        code=None,
        ends=(("0.2", "0.44"), ("0.6", "0.44")),
        actor=PERSON,
    )
    assert isinstance(added, PartConfirmation)

    bala = _match(session, built)

    assert bala["items_written"] == 0
    assert [item.id for item in _live_items(session)] == [added.drawing_item_id]


def test_undo_stands_on_this_reading_and_the_next(session: Session) -> None:
    """A person's Undo is a withdrawal; code never makes the same countertop again, on the same
    reading or a re-read that draws it a little elsewhere."""
    built, anchor = _qualifying(session)
    _match(session, built)
    undone = withdraw_listed_part(
        session,
        package_revision_id=built.revision.id,
        proposal_id=_countertop(session, anchor).id,
        actor=PERSON,
    )
    assert isinstance(undone, PartConfirmation)
    assert _live_items(session) == []

    _match(session, built)
    assert _live_items(session) == []

    reread = built.row(built.run(), pieces=3, shift=60)
    bala = _match(session, built)
    assert (reread, None) in _links(session)  # suggested again, for a person
    assert bala["items_written"] == 0
    assert _live_items(session) == []


def test_a_persons_correction_of_codes_item_is_never_taken_back(session: Session) -> None:
    built, anchor = _qualifying(session)
    _match(session, built)
    corrected = confirm_listed_part(
        session,
        package_revision_id=built.revision.id,
        proposal_id=_countertop(session, anchor).id,
        kind=PartKind.COUNTERTOP,
        code=None,
        actor=PERSON,
    )
    assert isinstance(corrected, PartConfirmation)

    built.row(built.run(), pieces=3, extra=("row-hold:field-cut",))
    bala = _match(session, built)

    automatic = bala["automatic"]
    assert isinstance(automatic, dict) and automatic["released"] == []
    current = current_decision(session, _countertop(session, anchor))
    assert current is not None and current.id == corrected.id


# -- code takes back only its own -------------------------------------------------------------------


def test_a_newer_contradicting_read_withdraws_only_codes_item(session: Session) -> None:
    built, anchor = _qualifying(session)
    _match(session, built)
    piece = _links(session)[(anchor, 0)]
    persons = confirm_listed_part(
        session,
        package_revision_id=built.revision.id,
        proposal_id=piece.id,
        kind=PartKind.CABINET,
        code=None,
        actor=PERSON,
    )
    assert isinstance(persons, PartConfirmation)
    code_decision = current_decision(session, _countertop(session, anchor))
    assert code_decision is not None

    # Re-read: the row is now held, so the two judgments no longer agree.
    built.row(built.run(), pieces=3, extra=("row-hold:field-cut",))
    bala = _match(session, built)

    automatic = bala["automatic"]
    assert isinstance(automatic, dict)
    assert automatic["released"] == [
        (
            "Countertop row on page 1 (an earlier reading): a newer reading of the countertop "
            "rows replaced it"
        )
    ]
    taken_back = current_decision(session, _countertop(session, anchor))
    assert taken_back is not None and taken_back.supersedes_id == code_decision.id
    assert taken_back.confirmed_by == BALA_CONFIRMER and taken_back.decision == "withdrawn"
    event = session.scalars(select(AuditEvent).where(AuditEvent.target_id == taken_back.id)).one()
    assert event.category == AuditCategory.EVIDENCE_QUALIFICATION.value
    # The person's piece is untouched, and the held row makes no new item.
    assert [item.id for item in _live_items(session)] == [persons.drawing_item_id]


def test_the_same_read_no_longer_agreeing_withdraws_codes_item_even_switched_off(
    session: Session,
) -> None:
    built, _anchor = _qualifying(session)
    _match(session, built)
    # A person now says the drawing is the architect's: code's second judgment is gone.
    confirm_view_role(session, view=built.views[0], role=ViewRole.ARCH, actor=PERSON)

    bala = _match(session, built, on=False)

    automatic = bala["automatic"]
    assert isinstance(automatic, dict)
    (released,) = automatic["released"]  # type: ignore[misc]
    assert released.startswith("Countertop row on page 1: ")
    assert "no confirmation says its drawing is the vendor's" in released
    assert _live_items(session) == []


def test_a_newer_reading_that_agrees_replaces_codes_item_with_a_new_one(session: Session) -> None:
    built, first = _qualifying(session)
    _match(session, built)
    second = built.row(built.run(), pieces=3)

    bala = _match(session, built)

    automatic = bala["automatic"]
    assert isinstance(automatic, dict)
    assert len(automatic["released"]) == 1  # type: ignore[arg-type]
    assert automatic["confirmed"] == ["Countertop row on page 1"]
    (item,) = _live_items(session)
    decision = current_decision(session, _countertop(session, second))
    assert decision is not None and decision.drawing_item_id == item.id
    # The older reading's suggestion, decided only by code, is no longer listed.
    listed = {
        part.proposal.id
        for drawing in revision_parts(session, built.revision.id)
        for part in drawing.parts
    }
    assert _countertop(session, first).id not in listed
    assert _countertop(session, second).id in listed


def test_release_touches_nothing_but_codes_current_confirmation(session: Session) -> None:
    built, anchor = _qualifying(session)
    propose_items(session, built.revision.id)
    countertop = _countertop(session, anchor)

    assert (
        release_contradicted_code_part(session, proposal=countertop, judgments_agree=False) is None
    )
    confirm_listed_part(
        session,
        package_revision_id=built.revision.id,
        proposal_id=countertop.id,
        kind=PartKind.COUNTERTOP,
        code=None,
        actor=PERSON,
    )
    assert (
        release_contradicted_code_part(session, proposal=countertop, judgments_agree=False) is None
    )
    assert len(_live_items(session)) == 1


def test_codes_own_withdrawal_is_shown_as_undecided(session: Session) -> None:
    """Code taking back its own item is no person's word: the card is a person's one click again."""
    built, anchor = _qualifying(session)
    _match(session, built)
    confirm_view_role(session, view=built.views[0], role=ViewRole.ARCH, actor=PERSON)
    _match(session, built)
    confirm_view_role(session, view=built.views[0], role=ViewRole.SHOP, actor=PERSON)

    shown = {
        part.proposal.id: part.decision
        for drawing in revision_parts(session, built.revision.id)
        for part in drawing.parts
    }
    assert shown[_countertop(session, anchor).id] is None
    # And code does not decide it again: a decision already stands on it.
    bala = _match(session, built)
    assert bala["items_written"] == 0


# -- pieces stay a person's click -------------------------------------------------------------------


def test_a_piece_is_never_automatic_named_or_not(session: Session) -> None:
    built, anchor = _qualifying(session, kinds={1: ("filler", 'printed word "FILLER"')})

    _match(session, built)

    (row,) = plan_items(session, built.revision.id).rows
    assert row.automatic.countertop is True
    assert row.automatic.pieces == (False, False, False)
    assert all(PIECES_STAY_ONE_CLICK in why for why in row.automatic.pieces_why_not)
    links = _links(session)
    for index in range(3):
        assert current_decision(session, links[(anchor, index)]) is None
    with pytest.raises(ValueError, match="only a countertop"):
        confirm_part_by_code(
            session, proposal=links[(anchor, 1)], kind=PartKind.FILLER, reason="both agree"
        )
    with pytest.raises(ValueError, match="only a suggestion of a countertop"):
        confirm_part_by_code(
            session, proposal=links[(anchor, 0)], kind=PartKind.COUNTERTOP, reason="both agree"
        )


# -- each judgment missing means not automatic ------------------------------------------------------


def _held(extra: str) -> Callable[[Session], tuple[_Revision, UUID]]:
    return lambda session: _qualifying(session, extra=(extra,))


def _code_chose(session: Session) -> tuple[_Revision, UUID]:
    return _qualifying(session, chosen=False)


def _no_overall(session: Session) -> tuple[_Revision, UUID]:
    return _qualifying(session, overall=False)


def _unsealed(session: Session) -> tuple[_Revision, UUID]:
    return _qualifying(session, sealed=False)


def _gaps(session: Session) -> tuple[_Revision, UUID]:
    return _qualifying(session, gap=5)


def _typed_widths(session: Session) -> tuple[_Revision, UUID]:
    built, anchor = _qualifying(session)
    session.add(
        SlotRowReviewDecision(
            row_candidate_id=anchor,
            measurements={"piece_1": {"numerator": 20, "denominator": 1, "unit": "in"}},
            confirmed_by=PERSON,
        )
    )
    session.flush()
    return built, anchor


def _second_reading_of_a_piece(session: Session) -> tuple[_Revision, UUID]:
    built, anchor = _qualifying(session)
    first = session.get_one(ObservationCandidate, anchor)
    session.add(
        ObservationCandidate(
            document_version_id=first.document_version_id,
            page_id=first.page_id,
            extraction_run_id=first.extraction_run_id,
            raw_text="20",
            value_numerator=20,
            value_denominator=1,
            unit="in",
            polygon=first.polygon,
            coordinate_space="image",
            ambiguity_flags=[flag for flag in first.ambiguity_flags or () if flag != "slot:0"]
            + ["slot:1"],
            corroboration_status="CORROBORATED",
            corroboration_lane="SECOND_READER",
        )
    )
    session.flush()
    return built, anchor


def _split_page(session: Session) -> tuple[_Revision, UUID]:
    built, anchor = _qualifying(session)
    run = session.get_one(ObservationCandidate, anchor).extraction_run_id
    built.split_page(session.get_one(ExtractionRun, run), page=0)
    return built, anchor


def _role_without_a_confirmation(session: Session) -> tuple[_Revision, UUID]:
    built = _Revision(session, role="shop")
    return built, built.row(built.run(), pieces=3)


def _architects_drawing(session: Session) -> tuple[_Revision, UUID]:
    built = _Revision(session, role=None)
    _vendors(session, built, ViewRole.ARCH)
    return built, built.row(built.run(), pieces=3)


def _two_drawings(session: Session) -> tuple[_Revision, UUID]:
    """A second drawing over the pieces' line (not the overall's): the row is in two drawings."""
    built, anchor = _qualifying(session)
    session.add(
        DrawingView(
            page_id=built.pages[0].id,
            tag="panel-2",
            region={
                "space": "stored",
                "points": [["0.05", "0.47"], ["0.95", "0.47"], ["0.95", "0.95"], ["0.05", "0.95"]],
            },
            role=None,
        )
    )
    session.flush()
    return built, anchor


def _crosses_the_frame(session: Session) -> tuple[_Revision, UUID]:
    """The row's line lies in the drawing, but its printed overall reaches over the frame."""
    built, anchor = _qualifying(session)
    built.views[0].region = {
        "space": "stored",
        "points": [["0.05", "0.43"], ["0.95", "0.43"], ["0.95", "0.95"], ["0.05", "0.95"]],
    }
    session.flush()
    return built, anchor


MISSING: dict[str, tuple[Callable[[Session], tuple[_Revision, UUID]], str]] = {
    # Judgment 1, the AI readers.
    "code-chose-the-row": (_code_chose, "the AI readers did not both name this row"),
    "split-page": (_split_page, "the AI readers split on this page"),
    "row-hold": (_held("row-hold:field-cut"), "the row is held"),
    "row-ambiguous": (_held("row-ambiguous"), "the row is held"),
    "missing-widths": (_held("row-partial"), "the row waits for the reviewer"),
    "reviewer-markup": (_held("reviewer-markup"), "GV's markup is drawn over the row"),
    "check-hold": (_held("check-hold:stone-short-of-ends"), "the row waits for the reviewer"),
    "no-stone": (_held("no-stone:opus"), "a reader said no stone top is drawn over this row"),
    "typed-widths": (_typed_widths, "a reviewer typed widths into this row"),
    # Judgment 2, code.
    "no-printed-overall": (_no_overall, "no overall length is printed"),
    "pieces-do-not-tile": (
        _held("row-finding:pieces do not tile the overall"),
        "its pieces do not tile its printed overall",
    ),
    "boxes-do-not-meet": (_gaps, "its pieces do not run end to end"),
    "two-readings-of-a-piece": (_second_reading_of_a_piece, "exactly one reading for each"),
    "unsealed-width": (_unsealed, "not sealed by both readers"),
    "role-without-a-confirmation": (
        _role_without_a_confirmation,
        "no confirmation says its drawing is the vendor's",
    ),
    "architects-drawing": (_architects_drawing, "no confirmation says its drawing is the vendor's"),
    "two-drawings-hold-it": (_two_drawings, "inside exactly one drawing"),
    "crosses-the-frame": (_crosses_the_frame, "inside exactly one drawing"),
}


@pytest.mark.parametrize(("build", "why"), list(MISSING.values()), ids=list(MISSING))
def test_each_judgment_missing_means_not_automatic(
    session: Session, build: Callable[[Session], tuple[_Revision, UUID]], why: str
) -> None:
    built, _anchor = build(session)

    bala = _match(session, built)

    (row,) = plan_items(session, built.revision.id).rows
    assert row.automatic.countertop is False
    assert any(
        why in reason for reason in row.automatic.countertop_why_not
    ), row.automatic.countertop_why_not
    assert bala["items_written"] == 0
    assert _count(session, DrawingItem) == 0


def test_the_qualifying_row_is_the_baseline(session: Session) -> None:
    """Each case above takes one thing from this row, which qualifies."""
    built, _anchor = _qualifying(session)
    (row,) = plan_items(session, built.revision.id).rows
    assert row.automatic.countertop is True, row.automatic.countertop_why_not


def test_a_role_code_confirmed_on_two_judgments_counts(session: Session) -> None:
    """The drawing's role may come from code's own two judgments (`confirm_view_role_by_code`)."""
    from workflow.view_roles import confirm_view_role_by_code

    built = _Revision(session, role=None)
    for view in built.views:
        confirm_view_role_by_code(
            session, view=view, role=ViewRole.SHOP, reason="heading and content agree"
        )
    built.row(built.run(), pieces=3)

    (row,) = plan_items(session, built.revision.id).rows
    assert row.automatic.countertop is True, row.automatic.countertop_why_not


# -- only code's one name, and only through its one door --------------------------------------------


def test_confirm_part_refuses_any_other_code_name(session: Session) -> None:
    built, anchor = _qualifying(session)
    propose_items(session, built.revision.id)
    countertop = _countertop(session, anchor)

    with pytest.raises(ValueError, match="code confirms a part only as"):
        confirm_part(
            session,
            proposal=countertop,
            kind=PartKind.COUNTERTOP,
            code=None,
            actor="code:something-else/v1",
        )
    with pytest.raises(ValueError, match="code withdraws a part only as"):
        withdraw_part(session, proposal=countertop, actor="code:something-else/v1")


def test_the_measure_page_refuses_a_decision_made_in_codes_name(session: Session) -> None:
    built, anchor = _qualifying(session)
    propose_items(session, built.revision.id)
    countertop = _countertop(session, anchor)

    outcomes = [
        confirm_listed_part(
            session,
            package_revision_id=built.revision.id,
            proposal_id=countertop.id,
            kind=PartKind.COUNTERTOP,
            code=None,
            actor=BALA_CONFIRMER,
        ),
        withdraw_listed_part(
            session,
            package_revision_id=built.revision.id,
            proposal_id=countertop.id,
            actor="code:anything",
        ),
        add_part(
            session,
            package_revision_id=built.revision.id,
            view_id=built.views[0].id,
            kind=PartKind.COUNTERTOP,
            code=None,
            ends=(("0.2", "0.44"), ("0.6", "0.44")),
            actor=BALA_CONFIRMER,
        ),
    ]

    assert all(
        isinstance(outcome, PartRefused) and outcome.reason is PartRefusalReason.CODE_ACTOR
        for outcome in outcomes
    )
    assert _count(session, PartConfirmation) == 0


def test_a_code_confirmation_needs_its_reason(session: Session) -> None:
    built, anchor = _qualifying(session)
    propose_items(session, built.revision.id)

    with pytest.raises(ValueError, match="reason"):
        confirm_part_by_code(
            session, proposal=_countertop(session, anchor), kind=PartKind.COUNTERTOP, reason=" "
        )


def test_the_stage_refuses_a_switch_that_is_not_a_bool() -> None:
    with pytest.raises(TypeError, match="bala_auto_items"):
        DatabaseStages(bala_auto_items="1")  # type: ignore[arg-type]
