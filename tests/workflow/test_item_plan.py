"""Each live countertop item, its plan, its line in the run check, and `match` reporting it (#1245).

Verification for `workflow/item_plan.py` (`check_plan_for`, `live_countertops`,
`countertops_by_row`), the run-check line "every live countertop item has a width finding"
(`workflow/pariksha_run_check.py`) and the items in `DatabaseStages.match`'s summary.

The countertops here are made the way a reviewer makes them: Bala suggests one from the countertop
row the slot reader read (`workflow/bala_items.py`), and a person confirms it with one click
(`workflow/parts.py:confirm_part`). Every value is made up.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import Finding, Package, PartProposal, PartProposalRow
from app.verdicts.rulebook import snapshot_store
from rules.semantic_types import ProductType
from storage.local import LocalStore
from tests.workflow.test_bala_items import _Revision
from tests.workflow.test_part_proposals_route import _upgrade
from tests.workflow.test_stages import _publish_rulebook
from vocabulary.part_kinds import PartKind
from workflow.bala_items import propose_items
from workflow.item_plan import (
    NO_ROW_LINKED,
    check_plan_for,
    countertops_by_row,
    live_countertops,
)
from workflow.pariksha_run_check import run_check
from workflow.parts import confirm_part, record_part_proposal, withdraw_part
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

PERSON = "reviewer@example.com"
WIDTH = "CT-WIDTH-001"


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


def _confirm_bala_countertop(session: Session, anchor: UUID) -> UUID:
    """A person confirms Bala's countertop suggestion for the row anchored at `anchor`."""
    proposal = session.scalars(
        select(PartProposal)
        .join(PartProposalRow, PartProposalRow.part_proposal_id == PartProposal.id)
        .where(PartProposalRow.row_candidate_id == anchor, PartProposalRow.slot_index.is_(None))
    ).one()
    confirmation = confirm_part(
        session, proposal=proposal, kind=PartKind.COUNTERTOP, code=None, actor=PERSON
    )
    assert confirmation.drawing_item_id is not None
    return confirmation.drawing_item_id


def _unlinked_countertop(session: Session, built: _Revision, page: int) -> UUID:
    """A countertop a person confirmed from a suggestion that is not Bala's: it has no row."""
    box = [
        (Decimal("0.10"), Decimal("0.20")),
        (Decimal("0.30"), Decimal("0.20")),
        (Decimal("0.30"), Decimal("0.25")),
        (Decimal("0.10"), Decimal("0.25")),
    ]
    proposal = record_part_proposal(
        session,
        drawing_view_id=built.views[page].id,
        kind=PartKind.COUNTERTOP,
        extent=box,
        defining_line=(box[0], box[1]),
        code_as_printed=None,
        code_candidate_id=None,
        reason="a dimension on the vendor's drawing",
        source="test",
        source_version="1",
    )
    confirmation = confirm_part(
        session, proposal=proposal, kind=PartKind.COUNTERTOP, code=None, actor=PERSON
    )
    assert confirmation.drawing_item_id is not None
    return confirmation.drawing_item_id


def _checked(session: Session, store: LocalStore, built: _Revision) -> None:
    _publish_rulebook(session)
    DatabaseStages(store).run_checks(session, built.revision.id)


def _countertop_rules(session: Session) -> set[str]:
    store = snapshot_store(session)
    return {
        rule_id
        for rule_id in store.rule_ids()
        if (latest := store.latest(rule_id)) is not None
        and latest.rule.product_type is ProductType.COUNTERTOP
    }


def row_width_variant(session: Session, finding_id: UUID) -> str | None:
    return session.get_one(Finding, finding_id).variant


def test_no_items_no_plan(session: Session, store: LocalStore) -> None:
    """Rows read, suggestions filed, nothing confirmed: there is no countertop item, so no plan."""
    built = _Revision(session)
    built.row(built.run())
    propose_items(session, built.revision.id)
    _checked(session, store, built)

    assert live_countertops(session, built.revision.id) == ()
    assert check_plan_for(session, built.revision.id).items == ()


def test_the_plan_lists_the_rules_its_row_s_results_and_what_they_wait_on(
    session: Session, store: LocalStore
) -> None:
    """One countertop confirmed from Bala's suggestion: its plan lists every published countertop
    rule, the width result of its row (still keyed by the row), and nothing it does not own."""
    built = _Revision(session)
    run = built.run()
    anchor = built.row(run, page=0)
    other = built.row(run, page=1)
    propose_items(session, built.revision.id)
    item = _confirm_bala_countertop(session, anchor)
    _checked(session, store, built)

    plan = check_plan_for(session, built.revision.id)

    (entry,) = plan.items
    assert entry.countertop.item_id == item
    assert entry.countertop.row_candidate_id == anchor
    assert entry.countertop.label == "Countertop 1 on page 1"
    assert entry.note is None
    assert {rule.rule_id for rule in entry.rules} == _countertop_rules(session)
    assert {"CT-WIDTH-001", "CT-DEPTH-001"} <= {rule.rule_id for rule in entry.rules}
    width = next(rule for rule in entry.rules if rule.rule_id == WIDTH)
    (row_width,) = width.findings
    assert row_width.scope == "row"
    # The other row's result is not this countertop's.
    assert all(finding.scope == "row" for finding in entry.findings)
    assert countertops_by_row(session, built.revision.id) == {anchor: entry.countertop}
    assert other not in countertops_by_row(session, built.revision.id)
    # A rule whose whole-package result waits on values lists them; a rule that does not, none.
    depth = next(rule for rule in entry.rules if rule.rule_id == "CT-DEPTH-001")
    assert depth.resolved and depth.findings == ()
    assert "countertop_overhang" in {missing.key for missing in depth.missing}
    assert width.missing == ()
    # The width rule's variant is chosen per row from that row's walls: the plan takes the version
    # and the variant from the row's own result, and does not say it waits for the walls.
    assert width.resolved and width.waits_for is None
    assert width.version is not None
    assert width.variant == row_width_variant(session, row_width.finding_id)


def test_a_countertop_with_no_row_says_so_and_lists_no_row_s_results(
    session: Session, store: LocalStore
) -> None:
    built = _Revision(session)
    built.row(built.run(), page=0)
    propose_items(session, built.revision.id)
    item = _unlinked_countertop(session, built, page=1)
    _checked(session, store, built)

    (entry,) = check_plan_for(session, built.revision.id).items

    assert entry.countertop.item_id == item
    assert entry.countertop.row_candidate_id is None
    assert entry.note == NO_ROW_LINKED
    assert entry.findings == ()
    assert {rule.rule_id for rule in entry.rules} == _countertop_rules(session)


def test_a_withdrawn_countertop_has_no_plan(session: Session, store: LocalStore) -> None:
    built = _Revision(session)
    anchor = built.row(built.run())
    propose_items(session, built.revision.id)
    _confirm_bala_countertop(session, anchor)
    proposal = session.scalars(
        select(PartProposal)
        .join(PartProposalRow, PartProposalRow.part_proposal_id == PartProposal.id)
        .where(PartProposalRow.row_candidate_id == anchor, PartProposalRow.slot_index.is_(None))
    ).one()
    withdraw_part(session, proposal=proposal, actor=PERSON)

    assert check_plan_for(session, built.revision.id).items == ()


# -- the run-check line: every live countertop item has a width finding ----------------------------


def _codes(session: Session, revision: UUID) -> list[tuple[str, tuple[tuple[str, str], ...]]]:
    return [(item.code, item.ids) for item in run_check(session, revision).items]


def test_the_run_check_says_when_a_countertop_has_no_width_finding(
    session: Session, store: LocalStore
) -> None:
    """A countertop with no row has no width result: the run check says so, naming the countertop,
    for a person (report only: no result is held or changed)."""
    built = _Revision(session)
    built.row(built.run(), page=0)
    propose_items(session, built.revision.id)
    item = _unlinked_countertop(session, built, page=1)
    _checked(session, store, built)

    found = [
        ids
        for code, ids in _codes(session, built.revision.id)
        if code == "countertop-item-without-width-result"
    ]

    assert found == [(("item", str(item)), ("page", "2"))]
    report = run_check(session, built.revision.id)
    line = next(i for i in report.items if i.code == "countertop-item-without-width-result")
    assert line.found == (
        "Countertop 1 on page 2 has no live width result: no countertop row's width check, and "
        "no width check of its own, belongs to it."
    )
    assert dict(report.examined)["live countertop items"] == 1


def test_the_run_check_line_stays_quiet_when_the_row_has_its_width_finding(
    session: Session, store: LocalStore
) -> None:
    built = _Revision(session)
    anchor = built.row(built.run(), page=0)
    propose_items(session, built.revision.id)
    _confirm_bala_countertop(session, anchor)
    _checked(session, store, built)

    codes = [code for code, _ids in _codes(session, built.revision.id)]

    assert "countertop-item-without-width-result" not in codes


# -- match reports the items -----------------------------------------------------------------------


def test_match_reports_the_items_each_unmatched_with_its_reason(
    session: Session, store: LocalStore
) -> None:
    """`match` now sees the countertops a person confirmed: it counts them and lists each one it
    could not match, with why (a countertop carries no tag the matcher uses)."""
    built = _Revision(session)
    anchor = built.row(built.run(), page=0)
    propose_items(session, built.revision.id)
    item = _confirm_bala_countertop(session, anchor)

    summary = DatabaseStages(store).match(session, built.revision.id)

    report = summary["live_items"]
    assert isinstance(report, dict)
    assert report["count"] == 1
    assert report["countertops"] == 1
    (unmatched,) = report["unmatched"]
    assert unmatched["item_id"] == str(item)
    assert unmatched["kind"] == PartKind.COUNTERTOP.value
    assert unmatched["reason"]


def test_match_with_no_items_reports_zero(session: Session, store: LocalStore) -> None:
    built = _Revision(session)
    built.row(built.run(), page=0)

    summary = DatabaseStages(store).match(session, built.revision.id)

    assert summary["live_items"] == {"count": 0, "countertops": 0, "unmatched": []}


# -- a re-read: Bala does not suggest a decided countertop again, so the newest row has no link ---


def test_after_a_re_read_the_countertop_follows_its_row_by_drawing_and_outline(
    session: Session, store: LocalStore
) -> None:
    """Review of #1243: a re-read makes new rows, and Bala skips the countertop a person already
    decided (same drawing, same outline), so the newest row has no link of its own. The countertop
    still owns that row's results: it is followed to it by drawing and outline, and the run check
    finds its width result."""
    built = _Revision(session)
    first = built.row(built.run(), page=0)
    propose_items(session, built.revision.id)
    item = _confirm_bala_countertop(session, first)

    newest = built.row(built.run(), page=0)
    outcome = propose_items(session, built.revision.id)
    assert outcome.already_decided >= 1
    assert not session.scalars(
        select(PartProposalRow).where(
            PartProposalRow.row_candidate_id == newest, PartProposalRow.slot_index.is_(None)
        )
    ).all()
    _checked(session, store, built)

    (entry,) = check_plan_for(session, built.revision.id).items
    assert entry.countertop.item_id == item
    assert entry.countertop.row_candidate_id == newest
    assert entry.countertop.row_followed
    width = next(rule for rule in entry.rules if rule.rule_id == WIDTH)
    assert [finding.scope for finding in width.findings] == ["row"]
    assert set(countertops_by_row(session, built.revision.id)) == {newest}
    codes = [code for code, _ids in _codes(session, built.revision.id)]
    assert "countertop-item-without-width-result" not in codes


def test_after_a_re_read_a_row_on_another_drawing_is_never_followed_to(
    session: Session, store: LocalStore
) -> None:
    """The newest reading has its row on the other page's drawing only: nothing on the countertop's
    own drawing matches, so it stays unlinked, its plan says so, and the run check reports it."""
    built = _Revision(session)
    first = built.row(built.run(), page=0)
    propose_items(session, built.revision.id)
    item = _confirm_bala_countertop(session, first)

    built.row(built.run(), page=1)
    propose_items(session, built.revision.id)
    _checked(session, store, built)

    (entry,) = check_plan_for(session, built.revision.id).items
    assert entry.countertop.item_id == item
    assert entry.countertop.row_candidate_id is None
    assert entry.note == NO_ROW_LINKED
    codes = [code for code, _ids in _codes(session, built.revision.id)]
    assert "countertop-item-without-width-result" in codes


def _reread_rows(session: Session, built: _Revision, pages: int) -> list[UUID]:
    """Read every page, confirm each page's countertop, then read every page again: Bala skips each
    decided countertop, so every newest row has no link of its own."""
    first_run = built.run()
    firsts = [built.row(first_run, page=page) for page in range(pages)]
    propose_items(session, built.revision.id)
    for anchor in firsts:
        _confirm_bala_countertop(session, anchor)
    newest_run = built.run()
    newest = [built.row(newest_run, page=page) for page in range(pages)]
    outcome = propose_items(session, built.revision.id)
    assert outcome.already_decided >= pages
    return newest


def test_after_a_re_read_a_row_skipped_for_a_withdrawn_suggestion_is_never_followed_to(
    session: Session, store: LocalStore
) -> None:
    """Review of #1247 (M1), the reviewer's case. Run 1: countertop A is confirmed. Run 2: the reader
    picks a different row on the same page, and the person says it is not a countertop. Run 3: the
    reader picks that row again, so Bala skips it, because of the withdrawn decision, not A's. The
    row is not A's: A stays unplaced and the run check names it."""
    built = _Revision(session)
    first = built.row(built.run(), page=0)
    propose_items(session, built.revision.id)
    item = _confirm_bala_countertop(session, first)

    other = built.row(built.run(), page=0, shift=40)
    filed = propose_items(session, built.revision.id)
    assert filed.written >= 1
    rejected = session.scalars(
        select(PartProposal)
        .join(PartProposalRow, PartProposalRow.part_proposal_id == PartProposal.id)
        .where(PartProposalRow.row_candidate_id == other, PartProposalRow.slot_index.is_(None))
    ).one()
    withdraw_part(session, proposal=rejected, actor=PERSON)

    again = built.row(built.run(), page=0, shift=40)
    skipped = propose_items(session, built.revision.id)
    assert skipped.already_decided >= 1
    _checked(session, store, built)

    (entry,) = check_plan_for(session, built.revision.id).items
    assert entry.countertop.item_id == item
    assert entry.countertop.row_candidate_id is None
    assert not entry.countertop.row_followed
    assert entry.note == NO_ROW_LINKED
    assert again not in countertops_by_row(session, built.revision.id)
    codes = [code for code, _ids in _codes(session, built.revision.id)]
    assert "countertop-item-without-width-result" in codes


def test_after_a_re_read_a_row_two_decided_countertops_could_own_is_followed_by_neither(
    session: Session, store: LocalStore
) -> None:
    """Two countertops decided on the same page, and the newest reading's row skipped: nothing says
    which decision the skip was for, so neither countertop takes it (unique both ways)."""
    built = _Revision(session)
    first = built.row(built.run(), page=0)
    propose_items(session, built.revision.id)
    _confirm_bala_countertop(session, first)
    second = built.row(built.run(), page=0, shift=40)
    propose_items(session, built.revision.id)
    _confirm_bala_countertop(session, second)

    again = built.row(built.run(), page=0, shift=40)
    assert propose_items(session, built.revision.id).already_decided >= 1
    _checked(session, store, built)

    countertops = live_countertops(session, built.revision.id)
    assert len(countertops) == 2
    assert all(c.row_candidate_id is None and not c.row_followed for c in countertops)
    assert again not in countertops_by_row(session, built.revision.id)


def test_after_a_re_read_a_row_whose_outline_does_not_overlap_is_never_followed_to(
    session: Session, store: LocalStore
) -> None:
    """Bala's tolerance can be wide enough to skip a row whose outline lies elsewhere on the drawing:
    the countertop is followed only to a row whose outline overlaps its own along and across."""
    built = _Revision(session)
    first = built.row(built.run(), page=0, pieces=1)
    propose_items(session, built.revision.id)
    _confirm_bala_countertop(session, first)

    far = built.row(built.run(), page=0, pieces=1, shift=700)
    assert (
        propose_items(session, built.revision.id, same_outline_tolerance=Decimal(1)).already_decided
        >= 1
    )
    _checked(session, store, built)

    (countertop,) = live_countertops(session, built.revision.id)
    assert countertop.row_candidate_id is None
    assert far not in countertops_by_row(session, built.revision.id)


def _statements(session: Session, call: object) -> int:
    bind = session.get_bind()
    seen: list[str] = []

    def count(*_args: object) -> None:
        seen.append("statement")

    event.listen(bind, "before_cursor_execute", count)
    try:
        assert callable(call)
        call()
    finally:
        event.remove(bind, "before_cursor_execute", count)
    return len(seen)


@pytest.mark.parametrize("pages", [2, 6])
def test_following_after_a_re_read_asks_the_database_a_fixed_number_of_times(
    session: Session, pages: int
) -> None:
    """Review of #1247 (M3): after a re-read, following every countertop to its newest row costs the
    same number of statements whatever the number of rows (it measured 13 with 2 rows and 21 with
    6). Both sizes must match the budget."""
    built = _Revision(session, pages=pages)
    newest = _reread_rows(session, built, pages)
    session.expire_all()

    statements = _statements(session, lambda: countertops_by_row(session, built.revision.id))

    assert set(countertops_by_row(session, built.revision.id)) == set(newest)
    assert statements == FOLLOW_STATEMENTS


#: What following after a re-read costs, whatever the number of rows.
FOLLOW_STATEMENTS = 11


def test_the_plan_lists_only_the_package_s_product_s_rules(
    session: Session, store: LocalStore
) -> None:
    """Review of #1247 (L4): on a cabinet package the countertop's plan lists the cabinet rules the
    package is checked by, not the countertop rules, whose checks never run there."""
    built = _Revision(session)
    anchor = built.row(built.run(), page=0)
    propose_items(session, built.revision.id)
    _confirm_bala_countertop(session, anchor)
    package = session.get_one(Package, built.revision.package_id)
    package.product_type = ProductType.CABINET.value
    session.flush()
    _publish_rulebook(session)

    (entry,) = check_plan_for(session, built.revision.id).items

    store_ = snapshot_store(session)
    cabinet = {
        rule_id
        for rule_id in store_.rule_ids()
        if (latest := store_.latest(rule_id)) is not None
        and latest.rule.product_type is ProductType.CABINET
    }
    assert cabinet
    assert {rule.rule_id for rule in entry.rules} == cabinet
    assert not {rule.rule_id for rule in entry.rules} & _countertop_rules(session)


def test_the_run_check_line_is_silent_on_a_cabinet_package(
    session: Session, store: LocalStore
) -> None:
    """Review of #1247 (L5): on a cabinet package the countertop width check is out of scope, so a
    confirmed countertop there has no width result by design and the line says nothing."""
    built = _Revision(session)
    built.row(built.run(), page=0)
    propose_items(session, built.revision.id)
    _unlinked_countertop(session, built, page=1)
    package = session.get_one(Package, built.revision.package_id)
    package.product_type = ProductType.CABINET.value
    session.flush()
    _checked(session, store, built)

    codes = [code for code, _ids in _codes(session, built.revision.id)]

    assert "countertop-item-without-width-result" not in codes
