"""The six depth and sink checks give one result per countertop, not one per package (#1201).

Verification for `workflow/several_countertops.py` (`countertop_holds`, the waiting sentences) and
its caller in `workflow/stages.py`.

**The bar.** On a package with several countertops, every countertop gets its own result for each
of CT-DEPTH-001, CT-SINK-CUTOUT-WIDTH-001, CT-SINK-CUTOUT-DEPTH-001, CT-SINK-OFFSET-FRONT-001,
CT-BACK-OFFSET-MIN-001 and CT-SINK-CABINET-WIDTH-001, in both depth-reader modes:

* a countertop the check ran for (reader on, run decided) keeps its own result, decided only on its
  own readings;
* any other countertop gets a held result of its own that says in plain words what it waits for;
* a value from one countertop, or from the whole package, never decides another countertop;
* a countertop that cannot carry a result of its own (a second box named on a page, with no record)
  keeps one held whole-package result naming it, so nothing goes silent;
* a package with one countertop is exactly as before.

Every value, name and note here is made up.
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

from app.db.session import session_factory
from storage.local import LocalStore
from tests.api.test_depth_readings import _countertop
from tests.evidence.test_bridge import _upgrade
from tests.workflow.test_depth_evidence import ACTOR, _findings, _Set
from tests.workflow.test_several_countertops import (
    _blank_page,
    _label,
    _project_depth_parameters,
    _run,
    _slot_rows,
    _typed,
)
from tests.workflow.test_stages import _publish_rulebook
from verdict.outcomes import Outcome
from workflow.countertop_runs import withdraw_countertop_run
from workflow.depth_evidence import DEPTH_RULE_IDS, QUALIFIED
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

CT010 = "CT010"
SINK_CABINET = "CT-SINK-CABINET-WIDTH-001"
SIX = sorted({*DEPTH_RULE_IDS, SINK_CABINET})
DECIDED = {Outcome.PASS.value, Outcome.FAIL.value}

#: What each check waits for, said on each countertop's own held result.
WAITS_FOR = {
    "CT-DEPTH-001": "Waiting for the countertop depth of this countertop.",
    "CT-SINK-CUTOUT-WIDTH-001": "Waiting for the sink cut-out width of this countertop, if it has a sink.",
    "CT-SINK-CUTOUT-DEPTH-001": "Waiting for the sink cut-out depth of this countertop, if it has a sink.",
    "CT-SINK-OFFSET-FRONT-001": (
        "Waiting for the front offset (the strip between the cabinet front and the sink cut-out) "
        "of this countertop, if it has a sink."
    ),
    "CT-BACK-OFFSET-MIN-001": (
        "Waiting for the countertop depth, the front offset and the sink cut-out depth of this "
        "countertop, if it has a sink."
    ),
    SINK_CABINET: (
        "Waiting for the wall layout, the sink cabinet width, the sink cut-out width and the "
        "clearance either side of the cut-out of this countertop, if it has a sink."
    ),
}
READER_OFF = (
    "The depth and sink reader is switched off, so nothing reads this countertop's own values: "
    "check them on its drawing and decide this result."
)
RUN_NOT_DECIDED = (
    "It clears when this countertop is confirmed and its run decided on Measurements: this check "
    "then runs for it from the depth and sink reader's readings."
)
NEVER = (
    "This check never runs per countertop, so nothing reads this countertop's own values: check "
    "them on its drawing and decide this result."
)


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


def _scoped(found: dict[str, list[Any]], rule_id: str) -> list[Any]:
    return [
        f
        for f in found.get(rule_id, [])
        if f.scope_item_id is not None or f.scope_row_candidate_id is not None
    ]


def _unscoped(found: dict[str, list[Any]], rule_id: str) -> list[Any]:
    return [
        f
        for f in found.get(rule_id, [])
        if f.scope_item_id is None and f.scope_row_candidate_id is None
    ]


def _why(rule_id: str, *, on: bool) -> str:
    if rule_id == SINK_CABINET:
        return NEVER
    return RUN_NOT_DECIDED if on else READER_OFF


def _two(session: Session, store: LocalStore) -> tuple[_Set, UUID, UUID, Any]:
    """Two countertops a person confirmed, each alone on its own page, with the job's settings."""
    sheet = _Set(session, store)
    proposal, item, page = _countertop(sheet, 1)
    _project_depth_parameters(session, sheet.revision)
    _publish_rulebook(session)
    return sheet, proposal, item, page


# -- 1. one result per countertop, each saying what it waits for ---------------------------------


@pytest.mark.parametrize("on", [False, True], ids=["reader-off", "reader-on-no-run-decided"])
def test_each_countertop_gets_its_own_result_for_every_check(
    session: Session, store: LocalStore, on: bool
) -> None:
    sheet, _proposal, item, _page = _two(session, store)

    found = _run(sheet, store, on=on)

    for rule_id in SIX:
        own = _scoped(found, rule_id)
        assert sorted((f.scope_item_id, f.scope_label) for f in own) == sorted(
            [(sheet.item, "Countertop 1 on page 1"), (item, "Countertop 1 on page 2")]
        ), rule_id
        for finding in own:
            assert finding.outcome == Outcome.REVIEW_REQUIRED.value, finding.reason
            assert finding.reason == f"{WAITS_FOR[rule_id]} {_why(rule_id, on=on)}", rule_id
        # Nothing was given for the whole package: no whole-package result to decide as well.
        assert _unscoped(found, rule_id) == [], rule_id


def test_countertop_rows_and_a_split_page_get_their_own_results(
    session: Session, store: LocalStore
) -> None:
    """No countertop confirmed yet: each row the slot reader found, and a page whose row the
    readers did not agree on, get their own results, scoped to their own record."""
    from tests.workflow.test_bala_items import _Revision

    made = _Revision(session, pages=3)
    run = made.run()
    first = made.row(run, page=0)
    second = made.row(run, page=1)
    made.split_page(run, page=2)
    _project_depth_parameters(session, made.revision)
    _publish_rulebook(session)

    DatabaseStages(store).run_checks(session, made.revision.id)
    session.flush()
    found = _findings(session, made.revision)

    for rule_id in SIX:
        own = _scoped(found, rule_id)
        assert len(own) == 3, rule_id
        labels = {f.scope_row_candidate_id: f.scope_label for f in own}
        assert labels[first] == "Countertop row on page 1"
        assert labels[second] == "Countertop row on page 2"
        # The split page's result is scoped to its own "row not chosen" record.
        (split,) = [label for anchor, label in labels.items() if anchor not in (first, second)]
        assert "page 3" in split, split
        assert all(f.outcome == Outcome.REVIEW_REQUIRED.value for f in own), rule_id
        assert all(f.reason.startswith(WAITS_FOR[rule_id]) for f in own), rule_id
        assert _unscoped(found, rule_id) == [], rule_id


def test_a_second_box_with_no_record_keeps_one_whole_package_hold_naming_it(
    session: Session, store: LocalStore
) -> None:
    """A reader named a second countertop's box on page 1, but nothing on record stands for it, so
    it cannot carry a result of its own: one held whole-package result names it (never silent)."""
    from tests.workflow.test_bala_items import _Revision

    made = _Revision(session, pages=1)
    anchor = made.row(made.run(), page=0, extra=("row-also:opus:2",))
    _project_depth_parameters(session, made.revision)
    _publish_rulebook(session)

    DatabaseStages(store).run_checks(session, made.revision.id)
    session.flush()
    found = _findings(session, made.revision)

    for rule_id in SIX:
        (own,) = _scoped(found, rule_id)
        assert own.scope_row_candidate_id == anchor
        (held,) = _unscoped(found, rule_id)
        assert held.outcome == Outcome.REVIEW_REQUIRED.value, held.reason
        assert (
            "Countertops with no result of their own for this check: another countertop row on "
            "page 1 (box 2)." in held.notes
        ), held.notes


def test_reader_on_a_countertop_with_no_run_decided_gets_its_own_held_result(
    session: Session, store: LocalStore
) -> None:
    """Countertop A's run is decided, countertop B is only a row on page 2. A has its own five
    results; B gets its own held results, scoped to its row, never one shared whole-package one."""
    sheet = _Set(session, store)
    _project_depth_parameters(session, sheet.revision)
    _publish_rulebook(session)
    _slot_rows(sheet, [sheet.page, _blank_page(sheet, 1)])
    withdraw_countertop_run(session, countertop_item_id=sheet.item, actor=ACTOR)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())

    found = _run(sheet, store, on=True)

    for rule_id in DEPTH_RULE_IDS:
        own = _scoped(found, rule_id)
        mine = [f for f in own if f.scope_item_id == sheet.item]
        theirs = [f for f in own if f.scope_row_candidate_id is not None]
        assert len(mine) == 1 and len(theirs) == 1, rule_id
        assert theirs[0].scope_label == "Countertop row on page 2"
        assert theirs[0].outcome == Outcome.REVIEW_REQUIRED.value
        assert theirs[0].reason == f"{WAITS_FOR[rule_id]} {RUN_NOT_DECIDED}"
        assert _unscoped(found, rule_id) == [], rule_id
    cabinet = _scoped(found, SINK_CABINET)
    assert len(cabinet) == 2 and all(f.reason.endswith(NEVER) for f in cabinet)
    assert _unscoped(found, SINK_CABINET) == []


# -- 2. a value only counts for the countertop it belongs to -------------------------------------


def test_a_value_from_countertop_a_never_decides_countertop_b(
    session: Session, store: LocalStore
) -> None:
    """Reader on, both runs decided: A's own qualified depth (25 1/2" = 24" + 1 1/2") PASSes A
    alone. B read nothing: B is not decided, and its result carries none of A's values."""
    sheet, _proposal, item, _page = _two(session, store)
    for countertop in (sheet.item, item):
        withdraw_countertop_run(session, countertop_item_id=countertop, actor=ACTOR)
    sheet.reading(CT010, Fraction(51, 2), QUALIFIED, link=sheet.link())

    found = _run(sheet, store, on=True)

    (mine,) = [f for f in found["CT-DEPTH-001"] if f.scope_item_id == sheet.item]
    (theirs,) = [f for f in found["CT-DEPTH-001"] if f.scope_item_id == item]
    assert mine.outcome == Outcome.PASS.value, mine.reason
    assert theirs.outcome not in DECIDED, theirs.reason
    for rule_id in SIX:
        assert {f.scope_item_id for f in _scoped(found, rule_id)} == {sheet.item, item}, rule_id
        assert all(
            f.outcome not in DECIDED for f in found[rule_id] if f.scope_item_id != sheet.item
        ), rule_id


@pytest.mark.parametrize("on", [False, True], ids=["reader-off", "reader-on-no-run-decided"])
@pytest.mark.parametrize("source", ["typed", "confirmed-label"])
def test_a_whole_package_value_decides_no_countertop(
    session: Session, store: LocalStore, on: bool, source: str
) -> None:
    """A depth typed on the form, or an ordinary label a person confirmed on countertop A's page,
    is one value for the whole package: it decides no countertop. Each countertop holds on its
    own, and the whole-package result that shows the value is held too (#1297)."""
    sheet, _proposal, item, _page = _two(session, store)
    typed = None
    if source == "typed":
        typed = _typed("CT-DEPTH-001", countertop_depth=Fraction(51, 2))
    else:
        _label(sheet, Fraction(51, 2))

    found = _run(sheet, store, on=on, typed=typed)

    assert all(f.outcome not in DECIDED for rule_id in SIX for f in found.get(rule_id, []))
    own = _scoped(found, "CT-DEPTH-001")
    assert {f.scope_item_id for f in own} == {sheet.item, item}
    assert all(f.reason.startswith(WAITS_FOR["CT-DEPTH-001"]) for f in own)
    (whole,) = _unscoped(found, "CT-DEPTH-001")
    assert whole.outcome == Outcome.REVIEW_REQUIRED.value
    assert whole.reason.startswith("This package has 2 countertops"), whole.reason


# -- 3. one countertop: exactly as before ---------------------------------------------------------


@pytest.mark.parametrize("on", [False, True], ids=["reader-off", "reader-on"])
def test_one_countertop_is_unchanged(session: Session, store: LocalStore, on: bool) -> None:
    sheet = _Set(session, store)
    _project_depth_parameters(session, sheet.revision)
    _publish_rulebook(session)

    found = _run(
        sheet, store, on=on, typed=_typed("CT-DEPTH-001", countertop_depth=Fraction(51, 2))
    )

    (depth,) = found["CT-DEPTH-001"]
    assert depth.outcome == Outcome.PASS.value and depth.scope_item_id is None
    for rule_id in SIX:
        assert _scoped(found, rule_id) == [], rule_id
        assert len(_unscoped(found, rule_id)) == 1, rule_id
        assert all(not f.reason.startswith("Waiting for") for f in found[rule_id])


def test_one_countertop_row_is_unchanged(session: Session, store: LocalStore) -> None:
    from tests.workflow.test_bala_items import _Revision

    made = _Revision(session, pages=1)
    made.row(made.run(), page=0)
    _project_depth_parameters(session, made.revision)
    _publish_rulebook(session)

    DatabaseStages(store).run_checks(session, made.revision.id)
    session.flush()
    found = _findings(session, made.revision)

    for rule_id in SIX:
        assert _scoped(found, rule_id) == [], rule_id
        (whole,) = _unscoped(found, rule_id)
        assert not whole.reason.startswith(("Waiting for", "This package has")), whole.reason


# -- 4. a decision carries over an unchanged re-run, and over confirming the countertop -----------


def _blocking_own(session: Session, revision_id: UUID) -> tuple[int, int]:
    from app.models import CheckRun
    from app.models.verdicts import Finding as FindingRow
    from app.review.approval import approval_readiness

    own = set(
        session.scalars(
            select(FindingRow.id)
            .join(CheckRun, CheckRun.id == FindingRow.check_run_id)
            .where(
                FindingRow.package_revision_id == revision_id,
                CheckRun.superseded_at.is_(None),
                FindingRow.scope_row_candidate_id.is_not(None),
                FindingRow.reason.startswith("Waiting for"),
            )
        ).all()
    )
    blocking = set(approval_readiness(session, revision_id).blocking_finding_ids)
    return len(own), len(own & blocking)


def test_a_decision_on_a_countertop_s_result_carries_over(
    session: Session, store: LocalStore
) -> None:
    from tests.review.test_carry_over import _decide_everything
    from tests.workflow.test_bala_items import _Revision
    from tests.workflow.test_item_plan import _confirm_bala_countertop
    from workflow.bala_items import propose_items

    made = _Revision(session, pages=2)
    run = made.run()
    anchors = [made.row(run, page=0), made.row(run, page=1)]
    propose_items(session, made.revision.id)
    _publish_rulebook(session)
    stages = DatabaseStages(store)
    stages.run_checks(session, made.revision.id)
    assert _blocking_own(session, made.revision.id) == (12, 12)
    _decide_everything(session, made.revision.id)
    session.flush()

    stages.run_checks(session, made.revision.id)
    session.flush()
    assert _blocking_own(session, made.revision.id) == (12, 0), "an unchanged re-run asked again"

    # The rows become confirmed countertops: the same two countertops, each still on its row.
    for anchor in anchors:
        _confirm_bala_countertop(session, anchor)
    stages.run_checks(session, made.revision.id)
    session.flush()
    assert _blocking_own(session, made.revision.id) == (12, 0), "confirming them asked again"


# -- 5. the words ---------------------------------------------------------------------------------


def test_the_waiting_sentences_name_no_countertop_and_never_decide() -> None:
    from workflow.several_countertops import waiting_reason

    for rule_id in SIX:
        for on in (False, True):
            reason = waiting_reason(rule_id, depth_reader=on)
            assert reason == f"{WAITS_FOR[rule_id]} {_why(rule_id, on=on)}"
            assert "page" not in reason and "Countertop " not in reason
