"""Whole-package depth and sink checks on a package with several countertops (#1297).

Verification for `workflow/several_countertops.py` and its two callers: `workflow/stages.py`
(the hold on every whole-package result of the five depth and sink checks and
CT-SINK-CABINET-WIDTH-001) and `workflow/evidence_operands.py` (the depth reader's hold, worded
truthfully, once, naming each countertop from its view's current link).

**The bar.** On a package with two countertops, one value never decides a check run once for the
whole package, whatever its source: a number typed before #1253, an ordinary drawn label a person
confirmed, or the depth reader's reading a person confirmed. Such a result is `REVIEW_REQUIRED`, and
its reason says what clears it. A result scoped to one countertop, and a package with one countertop,
are exactly as before. The change can only hold, never decide.

Every value, name and note here is made up.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.evidence.confirm import confirm_candidate_type
from app.models import ItemViewLink, ObservationCandidate
from evidence.canonical import EvidenceStatus
from storage.local import LocalStore
from tests.api.test_depth_readings import _countertop
from tests.evidence.test_bridge import _upgrade
from tests.workflow.test_depth_evidence import ACTOR, _findings, _rule, _Set
from tests.workflow.test_stages import _publish_rulebook
from units.measurement import Measurement, Unit
from verdict.operands import VerdictOperand
from verdict.outcomes import Outcome
from workflow.countertop_runs import withdraw_countertop_run
from workflow.depth_evidence import CONFIRM_ONLY, DEPTH_RULE_IDS, QUALIFIED
from workflow.evidence_operands import evidence_operands
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

CT010, CT012, CT008, CT007 = "CT010", "CT012", "CT008", "CT007"
SINK_CABINET = "CT-SINK-CABINET-WIDTH-001"
TWO = "This package has 2 countertops, and this check ran once for the whole package"
NAMED = "Countertops in this package: Countertop 1 on page 1 and Countertop 1 on page 2."
READER_OFF = "The depth and sink reader is switched off"
NEVER = "This check never runs per countertop"
PER_COUNTERTOP = "It clears when each countertop is confirmed and its run decided on Measurements"


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


def _typed(rule_id: str, **values: Fraction) -> dict[str, dict[str, VerdictOperand]]:
    """Numbers a person typed on the form before #1253, as `operands_for` returns them."""
    return {
        rule_id: {
            name: VerdictOperand(
                name=name,
                value=Measurement(value, Unit.INCH, str(value)),
                status=EvidenceStatus.HUMAN_CONFIRMED,
                source="SHOP",
                evidence_ref="reviewer:typed",
            )
            for name, value in values.items()
        }
    }


def _project_depth_parameters(session: Session, revision: Any) -> None:
    """The job's cabinet depth (24") and overhang (1 1/2"), as the next project settings."""
    from app.models import Package
    from app.models.parameters import ParameterSet as StoredSet
    from app.models.parameters import to_rows
    from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
    from rules.schema import Quantity

    project = session.get_one(Package, revision.package_id).project_id
    versions = session.scalars(
        select(StoredSet.version).where(
            StoredSet.project_id == project, StoredSet.layer == ParameterLayer.PROJECT.value
        )
    ).all()
    when = datetime(2026, 10, 1, tzinfo=UTC)
    stored, values = to_rows(
        ParameterSet(
            project_id=str(project),
            layer=ParameterLayer.PROJECT,
            version=max(versions, default=0) + 1,
            parameters={
                name: ParameterValue(
                    value=Quantity(value=value, unit=Unit.INCH),
                    provenance=Provenance.MEASURED,
                    set_by="test reviewer",
                    set_at=when,
                )
                for name, value in (
                    ("cabinet_depth", Fraction(24)),
                    ("countertop_overhang", Fraction(3, 2)),
                    ("cabinet_side_thickness", Fraction(1)),
                )
            },
        )
    )
    session.add(stored)
    session.add_all(values)
    session.flush()


def _two(session: Session, store: LocalStore) -> tuple[_Set, UUID, UUID]:
    """Two countertops a person confirmed, each alone on its own page; the job's cabinet depth (24")
    and overhang (1 1/2"). Returns the set, the second countertop's suggestion and its item."""
    sheet = _Set(session, store)
    proposal, item, _page = _countertop(sheet, 1)
    _project_depth_parameters(session, sheet.revision)
    _publish_rulebook(session)
    return sheet, proposal, item


def _run(
    sheet: _Set,
    store: LocalStore,
    *,
    on: bool = False,
    typed: dict[str, dict[str, VerdictOperand]] | None = None,
) -> dict[str, list[Any]]:
    DatabaseStages(
        store,
        operands=typed or {},
        discriminators={"wall_config": "back_left_right"},
        depth_reader=on,
    ).run_checks(sheet.session, sheet.revision.id)
    sheet.session.flush()
    return _findings(sheet.session, sheet.revision)


def _whole(found: dict[str, list[Any]], rule_id: str) -> Any:
    (finding,) = [
        f for f in found[rule_id] if f.scope_item_id is None and f.scope_row_candidate_id is None
    ]
    return finding


def _label(sheet: _Set, value: Fraction, field: str = CT010) -> ObservationCandidate:
    """An ordinary drawn label (not the depth reader's), confirmed by a person as `field`."""
    row = ObservationCandidate(
        document_version_id=sheet.page.document_version_id,
        page_id=sheet.page.id,
        extraction_run_id=sheet.run.id,
        raw_text=f'{value}"',
        value_numerator=value.numerator,
        value_denominator=value.denominator,
        unit="in",
        polygon=[[700, 150], [800, 150], [800, 200], [700, 200]],
        coordinate_space="image",
        ambiguity_flags=[],
    )
    sheet.session.add(row)
    sheet.session.flush()
    confirmed = confirm_candidate_type(
        sheet.session, candidate_id=row.id, semantic_type=field, confirmed_by=ACTOR
    )
    assert not hasattr(confirmed, "reason"), confirmed
    return row


# -- 1. one value never decides a whole-package check on a package with two countertops -----------


def test_a_typed_depth_never_passes_a_package_with_two_countertops(
    session: Session, store: LocalStore
) -> None:
    sheet, _proposal, _item = _two(session, store)

    found = _run(sheet, store, typed=_typed("CT-DEPTH-001", countertop_depth=Fraction(51, 2)))

    depth = _whole(found, "CT-DEPTH-001")
    assert depth.outcome == Outcome.REVIEW_REQUIRED.value, depth.reason
    assert depth.reason.startswith(TWO), depth.reason
    assert READER_OFF in depth.reason
    # The names are in the notes, never the reason: a decision carries over while they change.
    assert NAMED in depth.notes and "Countertop 1 on page" not in depth.reason


def test_a_typed_depth_that_is_wrong_is_held_too_never_decided(
    session: Session, store: LocalStore
) -> None:
    sheet, _proposal, _item = _two(session, store)

    found = _run(sheet, store, typed=_typed("CT-DEPTH-001", countertop_depth=Fraction(101, 4)))

    assert _whole(found, "CT-DEPTH-001").outcome == Outcome.REVIEW_REQUIRED.value


def test_a_confirmed_ordinary_label_never_passes_a_package_with_two_countertops(
    session: Session, store: LocalStore
) -> None:
    sheet, _proposal, _item = _two(session, store)
    _label(sheet, Fraction(51, 2))

    found = _run(sheet, store)

    depth = _whole(found, "CT-DEPTH-001")
    assert depth.outcome == Outcome.REVIEW_REQUIRED.value, depth.reason
    assert depth.reason.startswith(TWO)


def test_a_missing_value_is_held_and_still_says_what_is_missing(
    session: Session, store: LocalStore
) -> None:
    sheet, _proposal, _item = _two(session, store)

    found = _run(sheet, store)

    for rule_id in DEPTH_RULE_IDS:
        finding = _whole(found, rule_id)
        assert finding.outcome == Outcome.REVIEW_REQUIRED.value, rule_id
        assert finding.reason.startswith(TWO), finding.reason
        assert " Also: " in finding.reason, finding.reason


def test_the_sink_cabinet_check_is_held_even_with_the_reader_on(
    session: Session, store: LocalStore
) -> None:
    """With the reader on and both countertops' runs decided, the five checks run per countertop and
    are not touched; the sink cabinet check still runs once for the whole package, and holds."""
    sheet, _proposal, item = _two(session, store)
    for countertop in (sheet.item, item):
        withdraw_countertop_run(session, countertop_item_id=countertop, actor=ACTOR)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    typed = _typed(
        SINK_CABINET,
        sink_cabinet_width=Fraction(36),
        clearance_left=Fraction(1),
        cutout_width=Fraction(32),
        clearance_right=Fraction(1),
    )

    found = _run(sheet, store, on=True, typed=typed)

    cabinet = _whole(found, SINK_CABINET)
    assert cabinet.outcome == Outcome.REVIEW_REQUIRED.value, cabinet.reason
    assert cabinet.reason.startswith(TWO) and NEVER in cabinet.reason
    for rule_id in DEPTH_RULE_IDS:
        scoped = found[rule_id]
        assert {f.scope_item_id for f in scoped} == {sheet.item, item}, rule_id
        assert all("This package has" not in (f.reason or "") for f in scoped), rule_id
    # The first countertop's qualified depth still decides its own result (24 vs 24 + 1 1/2: FAIL).
    (mine,) = [f for f in found["CT-DEPTH-001"] if f.scope_item_id == sheet.item]
    assert mine.outcome == Outcome.FAIL.value, mine.reason


def test_the_depth_reader_s_confirmed_reading_holds_with_one_reason(
    session: Session, store: LocalStore
) -> None:
    """The reviewer's case of #1296: whole-package, a person confirmed countertop 1's depth."""
    sheet, _proposal, _item = _two(session, store)
    first = sheet.reading(
        CT010, 25, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only"
    )
    confirm_candidate_type(session, candidate_id=first.id, semantic_type=CT010, confirmed_by=ACTOR)

    found = _run(sheet, store, on=True)

    depth = _whole(found, "CT-DEPTH-001")
    assert depth.outcome == Outcome.REVIEW_REQUIRED.value
    assert depth.reason.startswith(TWO)
    assert depth.reason.count(PER_COUNTERTOP) == 1, depth.reason


# -- one countertop: exactly as before ------------------------------------------------------------


def test_one_countertop_is_unchanged(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    _project_depth_parameters(session, sheet.revision)
    _publish_rulebook(session)

    found = _run(sheet, store, typed=_typed("CT-DEPTH-001", countertop_depth=Fraction(51, 2)))

    assert _whole(found, "CT-DEPTH-001").outcome == Outcome.PASS.value
    assert all("This package has" not in (f.reason or "") for fs in found.values() for f in fs)


# -- 2. the depth reader's hold, worded truthfully ------------------------------------------------


def _held_reason(sheet: _Set, rule_id: str, *, on: bool) -> str:
    held = evidence_operands(
        sheet.session, sheet.revision.id, [_rule(rule_id)], depth_reader=on
    ).ambiguous.get(rule_id, {})
    reasons = set(held.values())
    assert len(reasons) == 1, reasons
    return reasons.pop()


def _confirmed_reading(sheet: _Set, field: str, value: int, link: ItemViewLink) -> None:
    row = sheet.reading(field, value, CONFIRM_ONLY, link=link, confirmed_by="drawn-length-only")
    confirm_candidate_type(
        sheet.session, candidate_id=row.id, semantic_type=field, confirmed_by=ACTOR
    )


def test_the_hold_says_the_reader_is_off_when_it_is(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    _confirmed_reading(sheet, CT010, 25, sheet.link())

    off = _held_reason(sheet, "CT-DEPTH-001", on=False)
    on = _held_reason(sheet, "CT-DEPTH-001", on=True)

    assert "Countertop 1 on page 1" in off
    assert READER_OFF in off and PER_COUNTERTOP not in off
    assert PER_COUNTERTOP in on and READER_OFF not in on


def test_the_sink_cabinet_hold_never_promises_a_per_countertop_check(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    _confirmed_reading(sheet, CT012, 32, sheet.link())

    for on in (False, True):
        reason = _held_reason(sheet, SINK_CABINET, on=on)
        assert NEVER in reason and PER_COUNTERTOP not in reason, reason


def test_one_reason_lists_every_countertop(session: Session, store: LocalStore) -> None:
    """Two values of one check confirmed on two countertops' pages: one sentence, both named."""
    sheet, proposal, _item = _two(session, store)
    first_page = sheet.page
    _confirmed_reading(sheet, CT010, 25, sheet.link())
    sheet.page = session.get_one(type(first_page), _page_of(session, proposal))
    _confirmed_reading(sheet, CT007, 4, sheet.link(proposal))

    reason = _held_reason(sheet, "CT-BACK-OFFSET-MIN-001", on=True)

    assert "Countertop 1 on page 1 and Countertop 1 on page 2" in reason, reason


def test_the_countertop_named_is_the_one_the_view_is_tied_to_now(
    session: Session, store: LocalStore
) -> None:
    """A person moved the view's link to the other countertop: the reason names that one."""
    sheet, proposal, _item = _two(session, store)
    link = sheet.link()
    _confirmed_reading(sheet, CT010, 25, link)
    session.add(
        ItemViewLink(
            part_proposal_id=proposal,
            page_id=link.page_id,
            region=link.region,
            view_kind="plan",
            heading_text=None,
            judged_by="person",
            extraction_run_id=None,
            supersedes_id=link.id,
            linked_by=ACTOR,
        )
    )
    session.flush()

    reason = _held_reason(sheet, "CT-DEPTH-001", on=True)

    assert "for Countertop 1 on page 2." in reason, reason
    assert "Countertop 1 on page 1" not in reason, reason


def _page_of(session: Session, proposal: UUID) -> UUID:
    from app.models import DrawingView, PartProposal

    view = session.scalar(select(PartProposal.drawing_view_id).where(PartProposal.id == proposal))
    return session.get_one(DrawingView, view).page_id


# -- it can only hold ------------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", list(Outcome))
@pytest.mark.parametrize("rule_id", [*sorted(DEPTH_RULE_IDS), SINK_CABINET, "CT-WIDTH-001"])
@pytest.mark.parametrize("count", [0, 1, 2, 3])
@pytest.mark.parametrize("on", [False, True])
def test_the_hold_only_ever_holds(outcome: Outcome, rule_id: str, count: int, on: bool) -> None:
    from verdict.finding import Finding
    from verdict.outcomes import Severity
    from verdict.trace import CalculationTrace
    from workflow.several_countertops import (
        CountedCountertop,
        PackageCountertops,
        hold_for_several,
    )

    decided = outcome in (Outcome.PASS, Outcome.FAIL)
    trace = (
        CalculationTrace(
            operation="equals",
            operands=(),
            intermediates=(),
            comparison="a synthetic comparison",
            tolerance=None,
            arithmetic_unit=Unit.INCH,
            outcome=outcome,
            engine_version="1",
            operation_version="1",
        )
        if decided
        else None
    )
    before = Finding(
        rule_id=rule_id,
        outcome=outcome,
        severity=Severity.FLAG,
        reason="the check's own reason",
        snapshot_id="s" * 20,
        engine_version="1",
        trace=trace,
    )
    countertops = PackageCountertops(
        tuple(CountedCountertop(f"Countertop {n}") for n in range(1, count + 1))
    )

    after = hold_for_several(before, countertops, depth_reader=on)

    if count > 1 and rule_id != "CT-WIDTH-001":
        assert after.outcome is Outcome.REVIEW_REQUIRED
        assert after.trace is None and after.delta is None
        assert after.reason.startswith(f"This package has {count} countertops, and ")
        assert "Countertop 1" not in after.reason
        assert after.notes[-1].startswith("Countertops in this package: Countertop 1")
    else:
        assert after is before
    assert after.outcome not in (Outcome.PASS, Outcome.FAIL) or after is before


def test_the_reason_names_no_countertop_and_the_notes_name_them_all() -> None:
    from verdict.finding import Finding
    from verdict.outcomes import Severity
    from workflow.several_countertops import (
        CountedCountertop,
        PackageCountertops,
        hold_for_several,
    )

    names = tuple(f"Countertop row on page {n}" for n in range(1, 11))
    before = Finding(
        rule_id="CT-DEPTH-001",
        outcome=Outcome.NOT_FOUND,
        severity=Severity.FLAG,
        reason="a value is missing.",
        snapshot_id="s" * 20,
        engine_version="1",
        notes=("an earlier note",),
    )

    after = hold_for_several(
        before,
        PackageCountertops(tuple(CountedCountertop(name) for name in names)),
        depth_reader=False,
    )

    assert after.reason.startswith("This package has 10 countertops, and "), after.reason
    assert "Countertop row" not in after.reason
    assert after.reason.endswith("Also: a value is missing.")
    assert after.notes == (
        "an earlier note",
        f"Countertops in this package: {', '.join(names[:-1])} and {names[-1]}.",
    )


# -- how many countertops: rows count when no countertop is confirmed yet --------------------------


def test_two_countertop_rows_hold_a_typed_depth_with_no_countertop_confirmed(
    session: Session, store: LocalStore
) -> None:
    from tests.workflow.test_bala_items import _Revision

    made = _Revision(session, pages=2)
    run = made.run()
    made.row(run, page=0)
    made.row(run, page=1)
    revision = made.revision
    _project_depth_parameters(session, revision)
    _publish_rulebook(session)

    DatabaseStages(
        store, operands=_typed("CT-DEPTH-001", countertop_depth=Fraction(51, 2))
    ).run_checks(session, revision.id)
    session.flush()

    depth = _whole(_findings(session, revision), "CT-DEPTH-001")
    assert depth.outcome == Outcome.REVIEW_REQUIRED.value, depth.reason
    assert depth.reason.startswith(TWO), depth.reason
    assert (
        "Countertops in this package: Countertop row on page 1 and Countertop row on page 2."
        in depth.notes
    )


def test_a_second_countertop_named_on_one_page_counts(session: Session) -> None:
    from tests.workflow.test_bala_items import _Revision
    from workflow.several_countertops import package_countertops

    made = _Revision(session, pages=2)
    run = made.run()
    made.row(run, page=0, extra=("row-also:opus:2", "row-also:sonnet:2"))
    made.split_page(run, page=1)

    found = package_countertops(session, made.revision.id)

    assert found.names == (
        "Countertop row on page 1",
        "another countertop row on page 1 (box 2)",
        "Countertop row on page 2",
    )


def test_one_row_and_its_confirmed_countertop_are_one_countertop(session: Session) -> None:
    from tests.workflow.test_bala_items import _Revision
    from tests.workflow.test_item_plan import _confirm_bala_countertop
    from workflow.bala_items import propose_items
    from workflow.several_countertops import package_countertops

    made = _Revision(session, pages=1)
    anchor = made.row(made.run(), page=0)
    propose_items(session, made.revision.id)
    item = _confirm_bala_countertop(session, anchor)

    found = package_countertops(session, made.revision.id)

    assert [(c.name, c.item_id) for c in found.countertops] == [("Countertop 1 on page 1", item)]


def test_an_item_and_a_row_on_different_pages_are_two_countertops(
    session: Session, store: LocalStore
) -> None:
    """The review's probe: countertop A a person confirmed on page 1 (no row read there), countertop
    B only a row on page 2. One each is two countertops, never one, and a typed depth holds."""
    from tests.workflow.test_bala_items import _Revision
    from tests.workflow.test_item_plan import _unlinked_countertop

    made = _Revision(session, pages=2)
    made.row(made.run(), page=1)
    _unlinked_countertop(session, made, 0)
    _project_depth_parameters(session, made.revision)
    _publish_rulebook(session)

    from workflow.several_countertops import package_countertops

    assert package_countertops(session, made.revision.id).names == (
        "Countertop 1 on page 1",
        "Countertop row on page 2",
    )
    DatabaseStages(
        store, operands=_typed("CT-DEPTH-001", countertop_depth=Fraction(51, 2))
    ).run_checks(session, made.revision.id)
    session.flush()

    depth = _whole(_findings(session, made.revision), "CT-DEPTH-001")
    assert depth.outcome == Outcome.REVIEW_REQUIRED.value, depth.reason


def test_an_unlinked_item_and_the_one_row_on_its_page_are_one_countertop(
    session: Session,
) -> None:
    from tests.workflow.test_bala_items import _Revision
    from tests.workflow.test_item_plan import _unlinked_countertop
    from workflow.several_countertops import package_countertops

    made = _Revision(session, pages=1)
    made.row(made.run(), page=0)
    _unlinked_countertop(session, made, 0)

    assert package_countertops(session, made.revision.id).names == ("Countertop 1 on page 1",)


def test_when_unsure_an_item_and_two_rows_on_its_page_are_three(session: Session) -> None:
    """One unlinked item beside a row and a second countertop's box on the same page: which row it
    is cannot be told, so all three count (fail closed)."""
    from tests.workflow.test_bala_items import _Revision
    from tests.workflow.test_item_plan import _unlinked_countertop
    from workflow.several_countertops import package_countertops

    made = _Revision(session, pages=1)
    made.row(made.run(), page=0, extra=("row-also:opus:2",))
    _unlinked_countertop(session, made, 0)

    assert package_countertops(session, made.revision.id).count == 3


def test_a_set_read_before_rows_or_countertops_counts_nothing(
    session: Session,
) -> None:
    from tests.workflow.test_bala_items import _Revision
    from workflow.several_countertops import package_countertops

    made = _Revision(session, pages=2)
    assert package_countertops(session, made.revision.id).count == 0


# -- a depth reading linked as a part's width: refused in plain words ----------------------------


def test_a_width_linked_to_a_depth_reading_is_refused_in_plain_words(
    session: Session, store: LocalStore
) -> None:
    from app.models import EvidenceSupportingCandidate, ExtractionRun, TaskRun, WorkflowRun
    from tests.workflow.test_identifier_pairing import _rules
    from tests.workflow.test_part_operands import RULE, Assembly
    from workflow.depth_evidence import DEPTH_EXTRACTOR, DEPTH_READER_FLAG, DEPTH_READER_VERSION

    assembly = Assembly(session, store)
    workflow = session.scalars(
        select(WorkflowRun).where(WorkflowRun.package_revision_id == assembly.revision.id)
    ).first()
    assert workflow is not None
    task = TaskRun(
        workflow_run_id=workflow.id,
        idempotency_key="synthetic-depth-task",
        task_type="match",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task)
    session.flush()
    run = ExtractionRun(
        task_run_id=task.id,
        extractor=DEPTH_EXTRACTOR,
        extractor_version=DEPTH_READER_VERSION,
        config_hash="synthetic",
        dpi=300,
    )
    session.add(run)
    session.flush()
    depth = ObservationCandidate(
        document_version_id=assembly.page.document_version_id,
        page_id=assembly.page.id,
        extraction_run_id=run.id,
        raw_text='43"',
        value_numerator=43,
        value_denominator=1,
        unit="in",
        polygon=[[700, 150], [800, 150], [800, 200], [700, 200]],
        coordinate_space="image",
        ambiguity_flags=[DEPTH_READER_FLAG, "state:confirm-only", f"field:{CT010}"],
    )
    session.add(depth)
    session.flush()
    # The countertop's linked width reading stands on the depth reader's reading.
    session.add(
        EvidenceSupportingCandidate(
            canonical_observation_id=assembly.readings[0],
            candidate_id=depth.id,
            role="primary",
        )
    )
    session.flush()

    reason = evidence_operands(session, assembly.revision.id, _rules()).missing[RULE]

    assert "linked to a reading from the depth and sink reader" in reason, reason
    assert "never a part's width" in reason
    assert "cannot be rebuilt" not in reason


# -- reader on: a countertop with no result of its own is never silent ---------------------------


def _slot_rows(sheet: _Set, pages: list[Any]) -> None:
    """One countertop row per page, as the slot reader records it (made-up widths)."""
    from app.models import ExtractionRun, TaskRun, WorkflowRun

    workflow = WorkflowRun(package_revision_id=sheet.revision.id, engine_run_id="slot-synthetic")
    sheet.session.add(workflow)
    sheet.session.flush()
    task = TaskRun(
        workflow_run_id=workflow.id,
        idempotency_key="slot-synthetic-task",
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    sheet.session.add(task)
    sheet.session.flush()
    run = ExtractionRun(
        task_run_id=task.id,
        extractor="extraction.form_reader",
        extractor_version="slot-reader-v1-test",
        config_hash="synthetic-test",
        dpi=150,
    )
    sheet.session.add(run)
    sheet.session.flush()
    for page in pages:
        for slot in ("0", "1", "overall"):
            left = 400 if slot == "1" else 200
            box = (190, 700, 610, 760) if slot == "overall" else (left, 800, left + 200, 860)
            sheet.session.add(
                ObservationCandidate(
                    document_version_id=page.document_version_id,
                    page_id=page.id,
                    extraction_run_id=run.id,
                    raw_text="20",
                    value_numerator=20,
                    value_denominator=1,
                    unit="in",
                    polygon=[
                        [box[0], box[1]],
                        [box[2], box[1]],
                        [box[2], box[3]],
                        [box[0], box[3]],
                    ],
                    coordinate_space="image",
                    ambiguity_flags=[
                        "slot-reader",
                        f"slot:{slot}",
                        f"slot-box:{box[0]},{box[1]},{box[2]},{box[3]}",
                        "row-rank:1",
                        "row-slot-count:2",
                        "row-choice:1",
                    ],
                    corroboration_status="CORROBORATED",
                    corroboration_lane="SECOND_READER",
                )
            )
    sheet.session.flush()


def _blank_page(sheet: _Set, index: int) -> Any:
    from app.models.document import Page

    page = Page(
        document_version_id=sheet.page.document_version_id,
        index=index,
        content_hash=f"{index + 3:x}" * 64,
        width_pt=400,
        height_pt=100,
        rotation=0,
        has_vector_text=True,
        render_failed=False,
        media_box=["0", "0", "400", "100"],
        crop_box=["0", "0", "400", "100"],
    )
    sheet.session.add(page)
    sheet.session.flush()
    return page


def test_reader_on_a_countertop_with_no_run_decided_gets_a_held_result(
    session: Session, store: LocalStore
) -> None:
    """The review's probe: countertop A's run is decided, countertop B is only a row on page 2.
    A has its own five results; B is never silent: since #1201 it gets a held result of its own
    for each check, scoped to its row (`test_one_result_per_countertop.py` has the wording)."""
    sheet = _Set(session, store)
    _project_depth_parameters(session, sheet.revision)
    _publish_rulebook(session)
    _slot_rows(sheet, [sheet.page, _blank_page(sheet, 1)])
    withdraw_countertop_run(session, countertop_item_id=sheet.item, actor=ACTOR)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())

    found = _run(sheet, store, on=True)

    for rule_id in DEPTH_RULE_IDS:
        mine = [f for f in found[rule_id] if f.scope_item_id == sheet.item]
        assert len(mine) == 1, rule_id
        (held,) = [f for f in found[rule_id] if f.scope_row_candidate_id is not None]
        assert held.outcome == Outcome.REVIEW_REQUIRED.value, held.reason
        assert held.scope_label == "Countertop row on page 2"
        assert "this countertop is confirmed and its run decided on Measurements" in held.reason
    assert all(f.outcome == Outcome.REVIEW_REQUIRED.value for f in found[SINK_CABINET])
    assert len(found[SINK_CABINET]) == 2


def test_reader_on_every_countertop_with_its_own_result_adds_nothing(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    _project_depth_parameters(session, sheet.revision)
    _publish_rulebook(session)
    _slot_rows(sheet, [sheet.page])
    withdraw_countertop_run(session, countertop_item_id=sheet.item, actor=ACTOR)

    found = _run(sheet, store, on=True)

    for rule_id in DEPTH_RULE_IDS:
        assert [f.scope_item_id for f in found[rule_id]] == [sheet.item], rule_id


# -- a decision carries over while the countertops stay the same ---------------------------------


def _own_holds(session: Session, revision_id: UUID) -> set[UUID]:
    """Each countertop's own held results of the six checks (#1201)."""
    from app.models import CheckRun
    from app.models.verdicts import Finding as FindingRow

    return set(
        session.scalars(
            select(FindingRow.id)
            .join(CheckRun, CheckRun.id == FindingRow.check_run_id)
            .where(
                FindingRow.package_revision_id == revision_id,
                CheckRun.superseded_at.is_(None),
                FindingRow.reason.startswith("Waiting for "),
            )
        ).all()
    )


def _whole_package_holds(session: Session, revision_id: UUID) -> set[UUID]:
    from app.models import CheckRun
    from app.models.verdicts import Finding as FindingRow

    return set(
        session.scalars(
            select(FindingRow.id)
            .join(CheckRun, CheckRun.id == FindingRow.check_run_id)
            .where(
                FindingRow.package_revision_id == revision_id,
                CheckRun.superseded_at.is_(None),
                FindingRow.scope_item_id.is_(None),
                FindingRow.scope_row_candidate_id.is_(None),
                FindingRow.reason.startswith("This package has"),
            )
        ).all()
    )


def test_confirming_the_countertops_asks_no_decision_again_and_a_new_one_does(
    session: Session, store: LocalStore
) -> None:
    from app.review.approval import approval_readiness
    from tests.review.test_carry_over import _decide_everything
    from tests.workflow.test_bala_items import _Revision
    from tests.workflow.test_item_plan import _confirm_bala_countertop, _unlinked_countertop
    from workflow.bala_items import propose_items

    made = _Revision(session, pages=3)
    run = made.run()
    anchors = [made.row(run, page=0), made.row(run, page=1)]
    propose_items(session, made.revision.id)
    _publish_rulebook(session)
    stages = DatabaseStages(store)
    stages.run_checks(session, made.revision.id)
    assert len(_own_holds(session, made.revision.id)) == 12
    _decide_everything(session, made.revision.id)
    session.flush()

    # The rows become confirmed countertops: new names, the same two countertops.
    for anchor in anchors:
        _confirm_bala_countertop(session, anchor)
    stages.run_checks(session, made.revision.id)
    session.flush()
    holds = _own_holds(session, made.revision.id)
    blocking = set(approval_readiness(session, made.revision.id).blocking_finding_ids)
    assert len(holds) == 12 and not holds & blocking, "the same two countertops asked again"

    # A third countertop, confirmed from no row (#1201 review): it has no result of its own, so the
    # six whole-package holds name it and ask; the first two's own results are about those two
    # countertops alone, so a decision on them stands.
    _unlinked_countertop(session, made, 2)
    stages.run_checks(session, made.revision.id)
    session.flush()
    holds = _own_holds(session, made.revision.id)
    blocking = set(approval_readiness(session, made.revision.id).blocking_finding_ids)
    assert len(holds) == 12 and not holds & blocking
    whole = _whole_package_holds(session, made.revision.id)
    assert len(whole) == 6 and whole <= blocking


def test_the_screens_find_the_notes_by_the_server_s_own_opening_words() -> None:
    from workflow.several_countertops import COUNTERTOPS_NOTE, UNCHECKED_NOTE

    shown = (Path(__file__).parents[2] / "frontend/main/src/lib/countertop-notes.ts").read_text()
    for note in (COUNTERTOPS_NOTE, UNCHECKED_NOTE):
        assert f"'{note.split('{names}')[0]}'" in shown, note
