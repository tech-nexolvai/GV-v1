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
TWO = "This package has 2 countertops (Countertop 1 on page 1 and Countertop 1 on page 2)"
READER_OFF = "The depth and sink reader is switched off"
NEVER = "This check never runs per countertop"
PER_COUNTERTOP = "It clears when each countertop's run is decided on Measurements"


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
    (finding,) = [f for f in found[rule_id] if f.scope_item_id is None]
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
    from workflow.several_countertops import PackageCountertops, hold_for_several

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
    countertops = PackageCountertops(names=tuple(f"Countertop {n}" for n in range(1, count + 1)))

    after = hold_for_several(before, countertops, depth_reader=on)

    if count > 1 and rule_id != "CT-WIDTH-001":
        assert after.outcome is Outcome.REVIEW_REQUIRED
        assert after.trace is None and after.delta is None
        assert after.reason.startswith(f"This package has {count} countertops (")
    else:
        assert after is before
    assert after.outcome not in (Outcome.PASS, Outcome.FAIL) or after is before


def test_a_long_list_names_eight_and_counts_the_rest() -> None:
    from verdict.finding import Finding
    from verdict.outcomes import Severity
    from workflow.several_countertops import PackageCountertops, hold_for_several

    names = tuple(f"Countertop row on page {n}" for n in range(1, 11))
    before = Finding(
        rule_id="CT-DEPTH-001",
        outcome=Outcome.NOT_FOUND,
        severity=Severity.FLAG,
        reason="a value is missing.",
        snapshot_id="s" * 20,
        engine_version="1",
    )

    after = hold_for_several(before, PackageCountertops(names=names), depth_reader=False)

    assert after.reason.startswith(
        "This package has 10 countertops (Countertop row on page 1, "
    ), after.reason
    assert "Countertop row on page 8 and 2 more)" in after.reason
    assert after.reason.endswith("Also: a value is missing.")


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
    assert depth.reason.startswith(
        "This package has 2 countertops (Countertop row on page 1 and Countertop row on page 2)"
    ), depth.reason


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
    from workflow.several_countertops import package_countertops

    made = _Revision(session, pages=1)
    made.row(made.run(), page=0)

    assert package_countertops(session, made.revision.id).count == 1


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
