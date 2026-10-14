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
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

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
    CheckRun,
    DrawingItem,
    DrawingView,
    ItemIdentifier,
    ObservationCandidate,
    PackageRevision,
    PackageRevisionDocument,
    Page,
    PartConfirmation,
    PartProposal,
    RuleSnapshot,
    SlotRowReviewDecision,
    ViewRole,
)
from app.models.runs import ExtractionRun, WorkflowRun
from app.models.verdicts import Finding
from app.verdicts.rulebook import snapshot_store
from storage.local import LocalStore
from tests.workflow.test_bala_items import _links, _Revision
from tests.workflow.test_part_operands import Assembly, form_widths
from tests.workflow.test_part_proposals_route import _upgrade
from tests.workflow.test_reading_parts import REGION as PARTS_REGION
from tests.workflow.test_reading_parts import _box as _parts_box
from tests.workflow.test_stages import _publish_rulebook
from vocabulary.part_kinds import PartKind
from vocabulary.semantic_types import SemanticType
from workflow.bala_items import (
    AUTOMATIC_REASON,
    BALA_SOURCE,
    BALA_VERSION,
    FROM_THE_COUNTERTOP_LINE,
    PIECES_STAY_ONE_CLICK,
    plan_items,
    propose_items,
)
from workflow.countertop_runs import confirm_countertop_run
from workflow.part_operands import countertop_scopes, manual_run_pages, reviewer_owned_pages
from workflow.parts import (
    BALA_CONFIRMER,
    confirm_part,
    confirm_part_by_code,
    current_decision,
    live_part_item_ids,
    record_part_proposal,
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
        "kept": [],
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


# -- review of #1248: code's item changes no finding, even beside a person's run (H1) ----------------


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only")


def _codes_countertop_on_a_second_page(session: Session, assembly: Assembly) -> PartConfirmation:
    """A countertop code confirmed on two judgments, on a second vendor page of the revision."""
    page = Page(
        document_version_id=assembly.page.document_version_id,
        index=1,
        content_hash="c" * 64,
        width_pt=400,
        height_pt=100,
        rotation=0,
        has_vector_text=True,
        render_failed=False,
    )
    session.add(page)
    session.flush()
    view = DrawingView(page_id=page.id, tag="SYNTHETIC CODE", region=PARTS_REGION)
    session.add(view)
    session.flush()
    confirm_view_role(session, view=view, role=ViewRole.SHOP, actor=PERSON)
    extent = _parts_box("0.20", "0.80", "0.42", "0.70")
    proposal = record_part_proposal(
        session,
        drawing_view_id=view.id,
        kind=PartKind.COUNTERTOP,
        extent=extent,
        defining_line=(extent[0], extent[1]),
        code_as_printed=None,
        code_candidate_id=None,
        reason=FROM_THE_COUNTERTOP_LINE,
        source=BALA_SOURCE,
        source_version=BALA_VERSION,
        find_existing=False,
    )
    made = confirm_part_by_code(
        session, proposal=proposal, kind=PartKind.COUNTERTOP, reason=AUTOMATIC_REASON
    )
    assert made is not None
    return made


def _live_findings(session: Session, revision_id: UUID) -> list[tuple[str, ...]]:
    rows = session.execute(
        select(
            RuleSnapshot.snapshot_id,
            Finding.scope_label,
            Finding.outcome,
            Finding.severity,
            Finding.reason,
            Finding.delta_numerator,
            Finding.delta_denominator,
        )
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .where(Finding.package_revision_id == revision_id, CheckRun.superseded_at.is_(None))
    ).all()
    return sorted(tuple(str(value) for value in row) for row in rows)


def test_codes_item_changes_no_finding_beside_a_persons_run(
    session: Session, store: LocalStore
) -> None:
    """The reviewer's case: a person confirmed a countertop's run on page 1, and code made a
    countertop item on page 2. The findings are exactly those of the same revision without code's
    item: code's item is no countertop the checks are scoped to, and its page is no person's."""
    without = Assembly(session, store)
    with_code = Assembly(session, store)
    _codes_countertop_on_a_second_page(session, with_code)

    for assembly in (without, with_code):
        scopes = countertop_scopes(session, assembly.revision.id)
        assert scopes is not None and [scope.page_index for scope in scopes] == [0]
        assert reviewer_owned_pages(session, assembly.revision.id) == {0}
        DatabaseStages(
            store,
            discriminators={"wall_config": "back_left_right"},
            operands={"CT-WIDTH-001": form_widths()},
        ).run_checks(session, assembly.revision.id)

    assert _live_findings(session, with_code.revision.id) == _live_findings(
        session, without.revision.id
    )


def test_a_persons_run_on_codes_item_makes_it_a_checked_countertop(
    session: Session, store: LocalStore
) -> None:
    """Code's item is checked once a person has confirmed a run beneath it: that run is a person's
    decision about this countertop."""
    assembly = Assembly(session, store, run=False)
    persons = session.get_one(PartProposal, assembly.proposals[0])
    # Code's countertop over the same members (written directly: the test needs the item, not
    # the two judgments), then the person's own countertop taken back.
    extent = _parts_box("0.20", "0.80", "0.42", "0.70")
    codes = confirm_part(
        session,
        proposal=record_part_proposal(
            session,
            drawing_view_id=assembly.view.id,
            kind=PartKind.COUNTERTOP,
            extent=extent,
            defining_line=(extent[0], extent[1]),
            code_as_printed=None,
            code_candidate_id=None,
            reason=FROM_THE_COUNTERTOP_LINE,
            source=BALA_SOURCE,
            source_version=BALA_VERSION,
            find_existing=False,
        ),
        kind=PartKind.COUNTERTOP,
        code=None,
        actor=BALA_CONFIRMER,
    )
    assert codes.drawing_item_id is not None
    withdraw_part(session, proposal=persons, actor=PERSON)
    assert countertop_scopes(session, assembly.revision.id) is None

    confirm_countertop_run(
        session,
        countertop_item_id=codes.drawing_item_id,
        member_item_ids=assembly.parts[1:],
        edge_tolerance=Decimal(0),
        actor=PERSON,
        wall_config="back_left_right",
    )

    scopes = countertop_scopes(session, assembly.revision.id)
    assert scopes is not None and [scope.item_id for scope in scopes] == [codes.drawing_item_id]


# -- review of #1248: a person's countertop over code's replaces it (M1) ----------------------------


def test_a_person_adding_a_countertop_over_codes_item_replaces_it(session: Session) -> None:
    built, anchor = _qualifying(session)
    _match(session, built)
    codes = current_decision(session, _countertop(session, anchor))
    assert codes is not None and codes.confirmed_by == BALA_CONFIRMER
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

    bala = _match(session, built, on=False)

    automatic = bala["automatic"]
    assert isinstance(automatic, dict)
    assert automatic["released"] == [
        "Countertop row on page 1: a person decided a countertop at the same place"
    ]
    assert [item.id for item in _live_items(session)] == [added.drawing_item_id]


def test_a_person_deciding_an_older_suggestion_wins_over_codes_newer_item(
    session: Session,
) -> None:
    """The race: a person decides an older reading's countertop on a page they opened before the
    re-read, while match confirms the newer reading's overlapping one. The next match takes code's
    item back."""
    built, first = _qualifying(session)
    propose_items(session, built.revision.id)
    second = built.row(built.run(), pieces=3, shift=60)
    _match(session, built)
    codes = current_decision(session, _countertop(session, second))
    assert codes is not None and codes.confirmed_by == BALA_CONFIRMER
    persons = confirm_listed_part(
        session,
        package_revision_id=built.revision.id,
        proposal_id=_countertop(session, first).id,
        kind=PartKind.COUNTERTOP,
        code=None,
        actor=PERSON,
    )
    assert isinstance(persons, PartConfirmation)

    bala = _match(session, built)

    automatic = bala["automatic"]
    assert isinstance(automatic, dict)
    assert automatic["released"] == [
        "Countertop row on page 1: a person decided a countertop at the same place"
    ]
    assert automatic["confirmed"] == []
    assert [item.id for item in _live_items(session)] == [persons.drawing_item_id]


# -- review of #1248: never across revisions (M2) ---------------------------------------------------


def _next_revision(session: Session, built: _Revision) -> PackageRevision:
    """Revision 2 of the same package, carrying the same document at the same version (as
    `app/lifecycle/supersede.py` carries an unchanged one), with its own workflow; `built` now
    reads and writes revision 2. Returns revision 1."""
    first = built.revision
    carried = session.scalars(
        select(PackageRevisionDocument).where(
            PackageRevisionDocument.package_revision_id == first.id
        )
    ).one()
    second = PackageRevision(package_id=first.package_id, revision_number=2, state="MATCHING")
    session.add(second)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=second.id,
            package_id=carried.package_id,
            document_id=carried.document_id,
            document_version_id=carried.document_version_id,
        )
    )
    built.workflow = WorkflowRun(package_revision_id=second.id, engine_run_id=str(uuid4()))
    session.add(built.workflow)
    session.flush()
    built.revision = second
    return first


def test_a_new_revision_never_writes_on_a_signed_off_revisions_item(session: Session) -> None:
    built, first_anchor = _qualifying(session)
    _match(session, built)
    signed = current_decision(session, _countertop(session, first_anchor))
    assert signed is not None and signed.confirmed_by == BALA_CONFIRMER
    first = _next_revision(session, built)
    first.state = "APPROVED"
    session.flush()
    before = _count(session, PartConfirmation)
    second_anchor = built.row(built.run(), pieces=3, shift=60)

    bala = _match(session, built)

    # Revision 1 gains no row: its item is not revision 2's to take back.
    assert _count(session, PartConfirmation) == before
    still = current_decision(session, _countertop(session, first_anchor))
    assert still is not None and still.id == signed.id
    # Revision 2 makes nothing over it, and says why.
    automatic = bala["automatic"]
    assert isinstance(automatic, dict)
    assert automatic["released"] == []
    assert automatic["confirmed"] == []
    assert automatic["left_to_a_person"] == [
        "Countertop row on page 1: an earlier revision's countertop stands here"
    ]
    assert current_decision(session, _countertop(session, second_anchor)) is None
    assert [item.id for item in _live_items(session)] == [signed.drawing_item_id]
    # Revision 2's Measure page lists the live item, with code's decision, so a person can Undo it.
    shown = {
        part.proposal.id: part.decision
        for drawing in revision_parts(session, built.revision.id)
        for part in drawing.parts
    }
    assert shown[_countertop(session, first_anchor).id] is not None
    assert shown[_countertop(session, first_anchor).id].id == signed.id  # type: ignore[union-attr]
    assert shown[_countertop(session, second_anchor).id] is None
    undone = withdraw_listed_part(
        session,
        package_revision_id=built.revision.id,
        proposal_id=_countertop(session, first_anchor).id,
        actor=PERSON,
    )
    assert isinstance(undone, PartConfirmation)
    assert _live_items(session) == []


def test_code_confirms_nothing_over_any_live_countertop(session: Session) -> None:
    """Whoever made it: here code's own item from another revision's reading."""
    built, _anchor = _qualifying(session)
    _match(session, built)
    _next_revision(session, built)
    reread = built.row(built.run(), pieces=3, shift=60)
    propose_items(session, built.revision.id)

    assert (
        confirm_part_by_code(
            session,
            proposal=_countertop(session, reread),
            kind=PartKind.COUNTERTOP,
            reason=AUTOMATIC_REASON,
        )
        is None
    )
    assert len(_live_items(session)) == 1


# -- review of #1248: code withdraws only its own current item (L2) ----------------------------------


def test_code_withdraws_nothing_but_its_own_current_confirmation(session: Session) -> None:
    built, anchor = _qualifying(session)
    propose_items(session, built.revision.id)
    countertop = _countertop(session, anchor)

    with pytest.raises(ValueError, match="only its own"):
        withdraw_part(session, proposal=countertop, actor=BALA_CONFIRMER)
    confirm_listed_part(
        session,
        package_revision_id=built.revision.id,
        proposal_id=countertop.id,
        kind=PartKind.COUNTERTOP,
        code=None,
        actor=PERSON,
    )
    with pytest.raises(ValueError, match="only its own"):
        withdraw_part(session, proposal=countertop, actor=BALA_CONFIRMER)
    assert len(_live_items(session)) == 1


# -- re-review of #1248: a person's run beneath code's item keeps it ---------------------------------


def _persons_run_on_codes_item(session: Session) -> tuple[_Revision, UUID, PartConfirmation]:
    """Code made the countertop; a person confirmed its three pieces and the run beneath it."""
    if snapshot_store(session).latest("CT-WIDTH-001") is None:
        _publish_rulebook(session)
    built, anchor = _qualifying(session)
    _match(session, built)
    codes = current_decision(session, _countertop(session, anchor))
    assert codes is not None and codes.confirmed_by == BALA_CONFIRMER
    assert codes.drawing_item_id is not None
    members: list[UUID] = []
    for slot in range(3):
        made = confirm_listed_part(
            session,
            package_revision_id=built.revision.id,
            proposal_id=_links(session)[(anchor, slot)].id,
            kind=PartKind.CABINET,
            code=None,
            actor=PERSON,
        )
        assert isinstance(made, PartConfirmation) and made.drawing_item_id is not None
        members.append(made.drawing_item_id)
    confirm_countertop_run(
        session,
        countertop_item_id=codes.drawing_item_id,
        member_item_ids=members,
        edge_tolerance=Decimal(0),
        actor=PERSON,
        wall_config="back_left_right",
    )
    return built, anchor, codes


def _scoped(session: Session, built: _Revision) -> list[UUID] | None:
    scopes = countertop_scopes(session, built.revision.id)
    return None if scopes is None else [scope.item_id for scope in scopes]


def _countertop_findings(
    session: Session, store: LocalStore, built: _Revision
) -> list[tuple[str, ...]]:
    """The checks' results about the countertop item, after a check run."""
    DatabaseStages(store, operands={}).run_checks(session, built.revision.id)
    return [
        finding
        for finding in _live_findings(session, built.revision.id)
        if finding[1].startswith("Countertop on page")
    ]


@pytest.mark.parametrize("on", [True, False], ids=["switched-on", "switched-off"])
def test_a_newer_reading_keeps_codes_item_beneath_a_persons_run(
    session: Session, store: LocalStore, on: bool
) -> None:
    built, anchor, codes = _persons_run_on_codes_item(session)
    assert _scoped(session, built) == [codes.drawing_item_id]
    before = _countertop_findings(session, store, built)
    assert before and all("No live confirmed countertop run" not in f[4] for f in before)

    reread = built.row(built.run(), pieces=3)
    bala = _match(session, built, on=on)

    automatic = bala["automatic"]
    assert isinstance(automatic, dict)
    assert automatic["released"] == []
    assert automatic["kept"] == [
        (
            "Countertop row on page 1 (an earlier reading): kept: a person confirmed a run "
            "beneath it (a newer reading of the countertop rows replaced it)"
        )
    ]
    assert automatic["confirmed"] == []
    if on:
        assert automatic["left_to_a_person"] == [
            "Countertop row on page 1: a person confirmed a run beneath the countertop here"
        ]
    assert current_decision(session, _countertop(session, reread)) is None
    still = current_decision(session, _countertop(session, anchor))
    assert still is not None and still.id == codes.id
    assert _scoped(session, built) == [codes.drawing_item_id]
    assert _countertop_findings(session, store, built) == before


def test_a_row_that_stops_agreeing_keeps_codes_item_beneath_a_persons_run(
    session: Session,
) -> None:
    built, anchor, codes = _persons_run_on_codes_item(session)
    session.add(
        SlotRowReviewDecision(
            row_candidate_id=anchor,
            measurements={"piece_1": {"numerator": 20, "denominator": 1, "unit": "in"}},
            confirmed_by=PERSON,
        )
    )
    session.flush()

    bala = _match(session, built, on=False)

    automatic = bala["automatic"]
    assert isinstance(automatic, dict)
    assert automatic["released"] == []
    (kept,) = automatic["kept"]  # type: ignore[misc]
    assert kept.startswith("Countertop row on page 1: kept: a person confirmed a run beneath it")
    assert _scoped(session, built) == [codes.drawing_item_id]


def test_code_never_withdraws_its_item_beneath_a_persons_run(session: Session) -> None:
    built, anchor, _codes = _persons_run_on_codes_item(session)
    countertop = _countertop(session, anchor)

    assert (
        release_contradicted_code_part(session, proposal=countertop, judgments_agree=False) is None
    )
    with pytest.raises(ValueError, match="run beneath"):
        withdraw_part(session, proposal=countertop, actor=BALA_CONFIRMER)
    # A person's Undo is still a person's decision.
    undone = withdraw_listed_part(
        session, package_revision_id=built.revision.id, proposal_id=countertop.id, actor=PERSON
    )
    assert isinstance(undone, PartConfirmation)
