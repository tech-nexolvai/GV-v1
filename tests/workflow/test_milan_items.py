"""Each live countertop's architect comparison, looked up by the countertop (#1273, Milan by item).

Verification for `workflow/milan_items.py`: a countertop reports "not compared" with the reason
its row's stored pairing gives, "waits for the reviewer", or the compared pairs with each vendor
piece named by the piece item a person confirmed; it reads only, asks a fixed number of statements
for any number of countertops, and no check imports it.

The countertops and pieces are made the way a reviewer makes them: Bala suggests them from the
countertop row the slot reader read, and a person confirms each with one click. Every value is
made up.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import (
    ArchitectPairingRecord,
    CheckRun,
    DrawingItem,
    Finding,
    ObservationCandidate,
    PartConfirmation,
    PartProposal,
    PartProposalRow,
)
from tests.workflow.test_bala_items import _Revision
from tests.workflow.test_item_plan import _confirm_bala_countertop, _unlinked_countertop
from tests.workflow.test_part_proposals_route import _upgrade
from vocabulary.part_kinds import PartKind
from workflow.architect_pairing_contract import EffectivePair, EffectivePairing
from workflow.architect_row_plan import PAIR_BY_REVIEWER
from workflow.bala_items import propose_items
from workflow.milan_items import (
    NO_ROW_FOR_ARCHITECT,
    architect_status_for_item,
    architect_statuses_for_revision,
)
from workflow.parts import confirm_part

pytest_plugins = ("tests.app.postgres_fixture",)

PERSON = "reviewer@example.com"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _confirm_bala_piece(session: Session, anchor: UUID, slot: int, kind: PartKind) -> UUID:
    """A person confirms Bala's suggestion for piece `slot` of the row anchored at `anchor`."""
    proposal = session.scalars(
        select(PartProposal)
        .join(PartProposalRow, PartProposalRow.part_proposal_id == PartProposal.id)
        .where(PartProposalRow.row_candidate_id == anchor, PartProposalRow.slot_index == slot)
    ).one()
    confirmation = confirm_part(session, proposal=proposal, kind=kind, code=None, actor=PERSON)
    assert confirmation.drawing_item_id is not None
    return confirmation.drawing_item_id


def _nothing_comparable(session: Session, built: _Revision, anchor: UUID, reason: str) -> None:
    """The row's stored pairing: the architect prints nothing comparable, and why."""
    row = session.get_one(ObservationCandidate, anchor)
    session.add(
        ArchitectPairingRecord(
            package_revision_id=built.revision.id,
            page_id=row.page_id,
            row_anchor_candidate_id=anchor,
            extraction_run_id=row.extraction_run_id,
            source="none",
            status="nothing_comparable",
            pairs=[],
            details={"reasons": [reason]},
        )
    )
    session.flush()


def _one_countertop(session: Session, *, pieces: int = 3) -> tuple[_Revision, UUID, UUID]:
    built = _Revision(session)
    anchor = built.row(built.run(), page=0, pieces=pieces)
    propose_items(session, built.revision.id)
    item = _confirm_bala_countertop(session, anchor)
    return built, anchor, item


def test_a_countertop_whose_row_has_nothing_comparable_says_so_with_the_stored_reason(
    session: Session,
) -> None:
    built, anchor, item = _one_countertop(session)
    _nothing_comparable(session, built, anchor, "The architect prints only blocking on this page")

    (status,) = architect_statuses_for_revision(session, built.revision.id)

    assert status.countertop.item_id == item
    assert status.state == "not_compared"
    assert status.reason == "The architect prints only blocking on this page."
    assert status.pairs == ()
    assert status.pairing_source == "none"
    assert architect_status_for_item(session, item) == status


def test_a_countertop_whose_row_was_never_paired_says_nothing_is_paired(session: Session) -> None:
    _built, _anchor, item = _one_countertop(session)

    status = architect_status_for_item(session, item)

    assert status is not None
    assert (status.state, status.reason) == (
        "not_compared",
        "No architect dimension is paired with this row.",
    )
    assert status.pairing_source is None


def test_a_countertop_with_no_row_says_so(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), page=0)
    propose_items(session, built.revision.id)
    item = _unlinked_countertop(session, built, page=1)

    (status,) = architect_statuses_for_revision(session, built.revision.id)

    assert status.countertop.item_id == item
    assert (status.state, status.reason, status.pairs) == ("not_compared", NO_ROW_FOR_ARCHITECT, ())


def test_compared_pairs_name_the_piece_item_confirmed_for_the_same_place(
    session: Session,
) -> None:
    """Overall and piece 2 are paired: piece 2's pair names the cabinet a person confirmed from
    Bala's suggestion for that row and place; the overall names no piece."""
    _built, anchor, item = _one_countertop(session, pieces=3)
    cabinet = _confirm_bala_piece(session, anchor, 1, PartKind.CABINET)
    _confirm_bala_piece(session, anchor, 2, PartKind.FILLER)  # not paired: not listed
    overall_span, piece_span = uuid4(), uuid4()
    pairing = EffectivePairing(
        record_id=uuid4(),
        source="code+ais",
        status="paired",
        pairs=(
            EffectivePair("piece", piece_span, (1,)),
            EffectivePair("overall", overall_span, ()),
        ),
        reasons=(),
    )

    def lookup(_session: Session, row_anchor_id: UUID) -> EffectivePairing | None:
        return pairing if row_anchor_id == anchor else None

    status = architect_status_for_item(session, item, pairing_lookup=lookup)

    assert status is not None
    assert (status.state, status.reason) == ("compared", None)
    assert (status.pairing_source, status.pairing_judgments) == ("code+ais", "code and both AIs")
    assert [
        (pair.label, pair.vendor_slot, pair.piece_item_id, pair.piece_item_type)
        for pair in status.pairs
    ] == [
        ("overall", None, None, None),
        ("piece 2", 1, cabinet, "cabinet_width"),
    ]
    assert [pair.architect_candidate_id for pair in status.pairs] == [overall_span, piece_span]


def test_a_paired_piece_nobody_confirmed_names_no_item(session: Session) -> None:
    _built, _anchor, item = _one_countertop(session, pieces=2)
    pairing = EffectivePairing(
        record_id=uuid4(),
        source="reviewer",
        status="reviewer",
        pairs=(EffectivePair("piece", uuid4(), (0,)),),
        reasons=(),
    )

    status = architect_status_for_item(session, item, pairing_lookup=lambda _s, _a: pairing)

    assert status is not None
    (pair,) = status.pairs
    assert (pair.label, pair.piece_item_id) == ("piece 1", None)


def test_a_disagreement_waits_for_the_reviewer(session: Session) -> None:
    _built, _anchor, item = _one_countertop(session)
    pairing = EffectivePairing(
        record_id=uuid4(), source="none", status="ais-disagree", pairs=(), reasons=()
    )

    status = architect_status_for_item(session, item, pairing_lookup=lambda _s, _a: pairing)

    assert status is not None
    assert (status.state, status.reason, status.pairs) == (
        "waits_for_reviewer",
        PAIR_BY_REVIEWER,
        (),
    )


def test_not_a_live_countertop_has_no_status(session: Session) -> None:
    _built, anchor, _item = _one_countertop(session)
    piece = _confirm_bala_piece(session, anchor, 0, PartKind.CABINET)

    assert architect_status_for_item(session, piece) is None
    assert architect_status_for_item(session, uuid4()) is None


def _counts(session: Session) -> tuple[int, ...]:
    return tuple(
        session.scalar(select(func.count()).select_from(model)) or 0
        for model in (Finding, CheckRun, ArchitectPairingRecord, PartConfirmation, DrawingItem)
    )


def test_it_reads_only(session: Session) -> None:
    built, anchor, _item = _one_countertop(session)
    _nothing_comparable(session, built, anchor, "Nothing comparable")
    session.commit()
    before = _counts(session)

    architect_statuses_for_revision(session, built.revision.id)

    assert not session.new and not session.dirty and not session.deleted
    assert _counts(session) == before


def _statements(session: Session, pages: int) -> int:
    built = _Revision(session, pages=pages)
    run = built.run()
    anchors = [built.row(run, page=page) for page in range(pages)]
    propose_items(session, built.revision.id)
    for anchor in anchors:
        _confirm_bala_countertop(session, anchor)
        _confirm_bala_piece(session, anchor, 0, PartKind.CABINET)
        _nothing_comparable(session, built, anchor, "Nothing comparable")
    session.commit()
    session.expire_all()
    seen: list[object] = []

    def count(*_args: object) -> None:
        seen.append(None)

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", count)
    try:
        statuses = architect_statuses_for_revision(session, built.revision.id)
    finally:
        event.remove(bind, "before_cursor_execute", count)
    assert [status.state for status in statuses] == ["not_compared"] * pages
    return len(seen)


def test_the_statement_count_does_not_grow_with_the_countertops(session: Session) -> None:
    assert _statements(session, 1) == _statements(session, 4)


#: The only module outside the tests that may import the lookup: the read endpoint.
_ALLOWED_IMPORTERS = {"app/api/item_architect.py"}


def test_no_check_rule_or_evidence_step_reads_it() -> None:
    """Read-only by construction: nothing but the read endpoint imports `workflow.milan_items`, so
    no check, rule, evidence step or stage can take an operand from it (#1273)."""
    importers: set[str] = set()
    for path in REPO.rglob("*.py"):
        relative = path.relative_to(REPO).as_posix()
        if relative.startswith(("tests/", ".venv/", "frontend/", "node_modules/")):
            continue
        if "milan_items" not in path.read_text(encoding="utf-8", errors="ignore"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            )
            if any(name == "workflow.milan_items" for name in names):
                importers.add(relative)
    assert importers == _ALLOWED_IMPORTERS
