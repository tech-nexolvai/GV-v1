"""Confirmed runs supply all widths or none (#932); a form must not hide a broken run."""

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.finding_chain import build_chain
from app.api.findings import _as_finding, _base_query
from app.db.session import session_factory
from app.models import (
    CanonicalObservation,
    CheckRun,
    CountertopRun,
    CountertopRunDecision,
    DrawingView,
    Package,
    PackageRevisionDocument,
    Page,
    PartProposal,
    ReadingPart,
    RuleDefinition,
    RuleSnapshot,
    ViewRole,
)
from app.models.parameters import to_rows
from app.models.verdicts import Finding
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.schema import Quantity
from storage.local import LocalStore
from tests.workflow.test_identifier_pairing import _finding, _rules
from tests.workflow.test_one_drawing_per_check import _drawing, _revision
from tests.workflow.test_reading_parts import REGION, _confirmed, _replace_in_review
from tests.workflow.test_stages import _publish_rulebook
from units.measurement import Measurement, Unit
from verdict.operands import EvidenceStatus, VerdictOperand
from vocabulary.part_kinds import PartKind
from workflow.countertop_runs import confirm_countertop_run, live_run_rows, withdraw_countertop_run
from workflow.evidence_operands import evidence_operands
from workflow.part_operands import part_operands
from workflow.parts import confirm_part, withdraw_part
from workflow.reading_parts import confirm_reading_part, withdraw_reading_part
from workflow.stages import DatabaseStages
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)
RULE = "CT-WIDTH-001"


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    with session_factory(postgres_engine)() as opened:
        yield opened


class Assembly:
    """Made-up widths: a 43-inch top over 17/19-inch cabinets and 2/3-inch fillers."""

    def __init__(
        self,
        session: Session,
        store: LocalStore,
        *,
        run: bool = True,
        missing: int | None = None,
        refusal: bool = False,
        six_parts: bool = False,
    ) -> None:
        self.revision = _revision(session, store, _drawing("synthetic"))
        version = session.scalars(
            select(PackageRevisionDocument).where(
                PackageRevisionDocument.package_revision_id == self.revision.id
            )
        ).one()
        self.page = Page(
            document_version_id=version.document_version_id,
            index=0,
            content_hash="a" * 64,
            width_pt=400,
            height_pt=100,
            rotation=0,
            has_vector_text=True,
            render_failed=False,
        )
        session.add(self.page)
        session.flush()
        self.view = DrawingView(page_id=self.page.id, tag="SYNTHETIC", region=REGION)
        session.add(self.view)
        session.flush()
        confirm_view_role(session, view=self.view, role=ViewRole.SHOP, actor="reviewer")
        self.parts: list[UUID] = []
        self.proposals: list[UUID] = []
        self.readings: list[UUID] = []
        specs: list[tuple[PartKind, str, str, int | Fraction]] = [
            (PartKind.COUNTERTOP, "0.20", "0.80", 43),
            (PartKind.FILLER, "0.20", "0.25", 2),
            (PartKind.CABINET, "0.25", "0.45", 17),
            (PartKind.CABINET, "0.45", "0.75", 19),
            (PartKind.FILLER, "0.75", "0.80", 3),
        ]
        if six_parts:
            # Independent synthetic fractions, not transcribed client dimensions.
            specs = [
                (PartKind.COUNTERTOP, "0.20", "0.80", Fraction(47, 2)),
                (PartKind.FILLER, "0.20", "0.25", Fraction(5, 4)),
                (PartKind.CABINET, "0.25", "0.45", Fraction(33, 4)),
                (PartKind.FILLER, "0.45", "0.50", Fraction(5, 2)),
                (PartKind.CABINET, "0.50", "0.75", Fraction(15, 2)),
                (PartKind.FILLER, "0.75", "0.80", 2),
            ]
        for index, (kind, left, right, value) in enumerate(specs):
            proposal, item = _confirmed(session, self.view, kind, left, right)
            self.proposals.append(proposal)
            self.parts.append(item)
            reading = CanonicalObservation(
                document_version_id=self.page.document_version_id,
                page_id=self.page.id,
                document_role="SHOP",
                coordinate_space="stored",
                polygon=[[left, "0.30"], [right, "0.30"], [right, "0.31"], [left, "0.31"]],
                semantic_type=kind.item_type.value,
                value_numerator=Fraction(value).numerator,
                value_denominator=Fraction(value).denominator,
                unit="in",
                status="HUMAN_CONFIRMED",
                authority="ADVISORY" if refusal and index == 2 else "AUTHORITATIVE",
            )
            session.add(reading)
            session.flush()
            self.readings.append(reading.id)
            if index != missing:
                confirm_reading_part(
                    session,
                    observation_id=reading.id,
                    item_id=item,
                    edge_tolerance=None,
                    actor="reviewer",
                )
        self.decision = None
        if run:
            self.decision = confirm_countertop_run(
                session,
                countertop_item_id=self.parts[0],
                member_item_ids=list(reversed(self.parts[1:])),
                edge_tolerance=Decimal(0),
                actor="reviewer",
            )

    def check(self, session: Session, store: LocalStore, *, form: bool = True) -> Finding:
        _publish_rulebook(session)
        DatabaseStages(
            store,
            discriminators={"wall_config": "back_left_right"},
            operands={RULE: form_widths()} if form else {},
        ).run_checks(session, self.revision.id)
        return _finding(session, self.revision, RULE)


def form_widths() -> dict[str, VerdictOperand]:
    values = {"countertop_width": 43, "cabinet_widths": (17, 19), "filler_widths": (2, 3)}
    return {
        name: VerdictOperand(
            name=name,
            value=(
                tuple(Measurement(Fraction(x), Unit.INCH, None) for x in value)
                if isinstance(value, tuple)
                else Measurement(Fraction(value), Unit.INCH, None)
            ),
            source="USER_INPUT",
            status=EvidenceStatus.HUMAN_CONFIRMED,
        )
        for name, value in values.items()
    }


def _second_complete_run(session: Session, assembly: Assembly) -> tuple[UUID, list[UUID]]:
    """Another made-up countertop on its own page, with its own linked widths."""
    page = Page(
        document_version_id=assembly.page.document_version_id,
        index=1,
        content_hash="b" * 64,
        width_pt=400,
        height_pt=100,
        rotation=0,
        has_vector_text=True,
        render_failed=False,
    )
    session.add(page)
    session.flush()
    view = DrawingView(page_id=page.id, tag="SECOND", region=REGION)
    session.add(view)
    session.flush()
    confirm_view_role(session, view=view, role=ViewRole.SHOP, actor="reviewer")
    members: list[UUID] = []
    top_id: UUID | None = None
    for kind, left, right, value in (
        (PartKind.COUNTERTOP, "0.20", "0.80", 41),
        (PartKind.FILLER, "0.20", "0.25", 2),
        (PartKind.CABINET, "0.25", "0.45", 17),
        (PartKind.CABINET, "0.45", "0.75", 19),
        (PartKind.FILLER, "0.75", "0.80", 3),
    ):
        _, item = _confirmed(session, view, kind, left, right)
        reading = CanonicalObservation(
            document_version_id=page.document_version_id,
            page_id=page.id,
            document_role="SHOP",
            coordinate_space="stored",
            polygon=[[left, "0.30"], [right, "0.30"], [right, "0.31"], [left, "0.31"]],
            semantic_type=kind.item_type.value,
            value_numerator=value,
            value_denominator=1,
            unit="in",
            status="HUMAN_CONFIRMED",
            authority="AUTHORITATIVE",
        )
        session.add(reading)
        session.flush()
        confirm_reading_part(
            session, observation_id=reading.id, item_id=item, edge_tolerance=None, actor="reviewer"
        )
        if kind is PartKind.COUNTERTOP:
            top_id = item
        else:
            members.append(item)
    assert top_id is not None
    confirm_countertop_run(
        session,
        countertop_item_id=top_id,
        member_item_ids=list(reversed(members)),
        edge_tolerance=Decimal(0),
        actor="reviewer",
    )
    return top_id, members


@pytest.mark.parametrize(
    "broken",
    [
        "member_unlinked",
        "top_unlinked",
        "withdrawn",
        "member_withdrawn",
        "top_withdrawn",
        "member_corrected",
        "top_corrected",
        "seal_refused",
    ],
)
def test_incomplete_run_blocks_passing_form_and_labels(
    session: Session, store: LocalStore, broken: str
) -> None:
    assembly = Assembly(
        session,
        store,
        missing={"member_unlinked": 2, "top_unlinked": 0}.get(broken),
        refusal=broken == "seal_refused",
    )
    if broken == "withdrawn":
        withdraw_countertop_run(session, countertop_item_id=assembly.parts[0], actor="reviewer")
    if broken in {"member_withdrawn", "top_withdrawn"}:
        index = 2 if broken == "member_withdrawn" else 0
        withdraw_part(
            session,
            proposal=session.get_one(PartProposal, assembly.proposals[index]),
            actor="reviewer",
        )
    if broken in {"member_corrected", "top_corrected"}:
        index = 2 if broken == "member_corrected" else 0
        confirm_part(
            session,
            proposal=session.get_one(PartProposal, assembly.proposals[index]),
            kind=PartKind.CABINET if index else PartKind.COUNTERTOP,
            code=None,
            actor="reviewer",
        )
    finding = assembly.check(session, store)
    assert finding.outcome == "NOT_FOUND", finding.reason
    assert "run" in (finding.reason or "").lower()
    assert not evidence_operands(session, assembly.revision.id, _rules()).operands.get(RULE)


def test_withdrawn_run_is_refused_by_each_selection_layer(
    session: Session, store: LocalStore
) -> None:
    assembly = Assembly(session, store)
    assert assembly.decision is not None
    old_run_id = assembly.decision.run_id
    assert old_run_id is not None
    withdraw_countertop_run(session, countertop_item_id=assembly.parts[0], actor="reviewer")

    selected = part_operands(
        session,
        assembly.revision.id,
        _rules(),
        {},
        scope_item_id=assembly.parts[0],
    )
    assert "withdrawn" in selected.missing[RULE]
    assert not selected.operands.get(RULE)

    rows = list(session.scalars(live_run_rows().where(CountertopRun.run_id == old_run_id)))
    assert rows == []


def test_complete_run_order_and_every_reading_provenance(
    session: Session, store: LocalStore
) -> None:
    assembly = Assembly(session, store)
    evidence = evidence_operands(session, assembly.revision.id, _rules())
    widths = evidence.operands["CAB-FILLER-001"]["shop_cabinets"].value
    assert isinstance(widths, tuple)
    assert [v.exact for v in widths if isinstance(v, Measurement)] == [17, 19]
    assert "architectural_cabinets" not in evidence.operands["CAB-FILLER-001"]
    assert "shop_cabinets" not in evidence.operands.get("CAB-ARCH-VS-SHOP-001", {})
    finding = assembly.check(session, store, form=False)
    assert finding.outcome == "PASS", finding.reason
    session.flush()
    session.expire(finding)
    notes = " ".join(finding.notes)
    for identity in [*assembly.parts, *assembly.readings, assembly.decision.run_id]:
        assert str(identity) in notes


def test_never_configured_keeps_existing_form_path(session: Session, store: LocalStore) -> None:
    assembly = Assembly(session, store, run=False)
    assert assembly.check(session, store).outcome == "PASS"


def test_multiple_runs_are_not_combined(session: Session, store: LocalStore) -> None:
    assembly = Assembly(session, store)
    _, second = _confirmed(session, assembly.view, PartKind.COUNTERTOP, "0.82", "0.94")
    confirm_countertop_run(
        session,
        countertop_item_id=second,
        member_item_ids=assembly.parts[1:],
        edge_tolerance=Decimal(0),
        actor="reviewer",
    )
    _publish_rulebook(session)
    DatabaseStages(
        store,
        discriminators={"wall_config": "back_left_right"},
        operands={RULE: form_widths()},
    ).run_checks(session, assembly.revision.id)
    findings = list(
        session.scalars(
            select(Finding)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
            .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
            .where(
                Finding.package_revision_id == assembly.revision.id,
                CheckRun.superseded_at.is_(None),
                RuleDefinition.rule_id == RULE,
            )
        )
    )
    assert len(findings) == 2
    assert {finding.scope_item_id for finding in findings} == {assembly.parts[0], second}
    assert all(finding.outcome not in {"PASS", "FAIL"} for finding in findings)


def test_one_countertop_finding_names_its_subject(session: Session, store: LocalStore) -> None:
    assembly = Assembly(session, store)
    finding = assembly.check(session, store)
    assert finding.scope_item_id == assembly.parts[0]
    assert finding.scope_label == "Countertop on page 1, item 1"
    assert finding.outcome == "PASS"
    package = session.get_one(Package, assembly.revision.package_id)
    listed = [
        _as_finding(row)
        for row in session.execute(_base_query(package.project_id, package.id))
        if row.rule_id == RULE
    ]
    assert len(listed) == 1
    assert listed[0]["scope_item_id"] == assembly.parts[0]
    assert listed[0]["scope_label"] == finding.scope_label
    run = session.get_one(CheckRun, finding.check_run_id)
    snapshot = session.get_one(RuleSnapshot, run.rule_snapshot_id)
    definition = session.get_one(RuleDefinition, snapshot.rule_definition_id)
    chain = build_chain(session, finding, run, snapshot, definition)
    assert chain.scope_item_id == assembly.parts[0]
    assert chain.scope_label == finding.scope_label


def test_two_countertops_abstain_on_layout_and_missing_run(
    session: Session, store: LocalStore
) -> None:
    assembly = Assembly(session, store)
    _, other = _confirmed(session, assembly.view, PartKind.COUNTERTOP, "0.82", "0.94")
    _publish_rulebook(session)
    DatabaseStages(
        store,
        discriminators={"wall_config": "back_left_right"},
        operands={RULE: form_widths()},
    ).run_checks(session, assembly.revision.id)
    live = list(
        session.scalars(
            select(Finding)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
            .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
            .where(
                Finding.package_revision_id == assembly.revision.id,
                CheckRun.superseded_at.is_(None),
                RuleDefinition.rule_id == RULE,
            )
        )
    )
    width = {row.scope_item_id: row for row in live if row.scope_item_id and row.outcome != "PASS"}
    assert assembly.parts[0] in width, [
        (row.scope_item_id, row.outcome, row.reason) for row in live
    ]
    assert other in width
    assert width[assembly.parts[0]].outcome == "REVIEW_REQUIRED", width[assembly.parts[0]].reason
    assert "choose the wall layout for this countertop" in width[assembly.parts[0]].reason.lower()
    assert width[other].outcome == "NOT_FOUND"
    assert "confirm this countertop's run" in width[other].reason.lower()


def test_two_complete_countertops_keep_distinct_widths_and_supersede(
    session: Session, store: LocalStore
) -> None:
    assembly = Assembly(session, store)
    second, second_members = _second_complete_run(session, assembly)
    _publish_rulebook(session)
    stages = DatabaseStages(
        store,
        discriminators={"wall_config": "back_left_right"},
        operands={RULE: form_widths()},
    )
    for attempt in range(2):
        stages.run_checks(session, assembly.revision.id)
        live = list(
            session.scalars(
                select(Finding)
                .join(CheckRun, CheckRun.id == Finding.check_run_id)
                .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
                .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
                .where(
                    Finding.package_revision_id == assembly.revision.id,
                    CheckRun.superseded_at.is_(None),
                    RuleDefinition.rule_id == RULE,
                )
            )
        )
        assert len(live) == 2
        by_subject = {finding.scope_item_id: finding for finding in live}
        assert set(by_subject) == {assembly.parts[0], second}
        assert all(finding.outcome == "REVIEW_REQUIRED" for finding in live)
        assert all("wall layout" in finding.reason.lower() for finding in live)
        first_notes = " ".join(by_subject[assembly.parts[0]].notes)
        second_notes = " ".join(by_subject[second].notes)
        assert str(assembly.readings[0]) in first_notes
        assert str(assembly.readings[0]) not in second_notes
        assert str(second_members[0]) in second_notes
        assert str(second_members[0]) not in first_notes
        filler = list(
            session.scalars(
                select(Finding)
                .join(CheckRun, CheckRun.id == Finding.check_run_id)
                .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
                .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
                .where(
                    Finding.package_revision_id == assembly.revision.id,
                    CheckRun.superseded_at.is_(None),
                    RuleDefinition.rule_id == "CAB-FILLER-001",
                )
            )
        )
        assert {finding.scope_item_id for finding in filler} == {assembly.parts[0], second}
        assert all(finding.outcome == "NOT_FOUND" for finding in filler)
        assert all("approved-side pairing" in finding.reason for finding in filler)
        if attempt == 1:
            historic = list(
                session.scalars(
                    select(Finding).where(Finding.package_revision_id == assembly.revision.id)
                )
            )
            assert len(historic) > len(live)


def test_unlink_after_confirmation_costs_the_whole_run(session: Session, store: LocalStore) -> None:
    assembly = Assembly(session, store)
    withdraw_reading_part(session, item_id=assembly.parts[2], actor="reviewer")
    assert assembly.check(session, store).outcome == "NOT_FOUND"


def _replace_run(
    session: Session, assembly: Assembly, members: list[UUID], *, gap: bool = False
) -> None:
    """Malformed/legacy storage is tested independently of the safe interactive writer."""
    assert assembly.decision is not None
    decision = CountertopRunDecision(
        countertop_item_id=assembly.parts[0],
        supersedes_id=assembly.decision.id,
        decision="confirmed",
        run_id=uuid4(),
        confirmed_by="reviewer",
    )
    session.add(decision)
    session.flush()
    # Reverse insertion to make relying on SELECT's unspecified order fail.
    for position, member in reversed(list(enumerate(members))):
        session.add(
            CountertopRun(
                run_id=decision.run_id,
                countertop_item_id=assembly.parts[0],
                position=position + (1 if gap else 0),
                member_item_id=member,
                signal="synthetic",
                proposal_source="test",
                edge_tolerance=Decimal(0),
                confirmed_by="reviewer",
            )
        )
    session.flush()
    assembly.decision = decision


def test_stored_order_not_insertion_order(session: Session, store: LocalStore) -> None:
    assembly = Assembly(session, store)
    _replace_run(session, assembly, assembly.parts[1:])
    evidence = evidence_operands(session, assembly.revision.id, _rules())
    values = evidence.operands["CAB-FILLER-001"]["shop_cabinets"].value
    assert isinstance(values, tuple)
    assert [value.exact for value in values if isinstance(value, Measurement)] == [17, 19]


@pytest.mark.parametrize(
    "problem",
    [
        "cross_drawing_member",
        "order_gap",
        "duplicate_links",
        "wrong_reading_drawing",
        "foreign_reading",
        "view_role",
    ],
)
def test_ambiguous_storage_never_uses_passing_form(
    session: Session, store: LocalStore, problem: str
) -> None:
    assembly = Assembly(session, store)
    if problem == "order_gap":
        _replace_run(session, assembly, assembly.parts[1:], gap=True)
    elif problem == "view_role":
        confirm_view_role(session, view=assembly.view, role=ViewRole.ARCH, actor="reviewer")
    elif problem == "cross_drawing_member":
        other = DrawingView(page_id=assembly.page.id, tag="OTHER", region=REGION)
        session.add(other)
        session.flush()
        _, member = _confirmed(session, other, PartKind.CABINET, "0.25", "0.45")
        _replace_run(session, assembly, [assembly.parts[1], member, *assembly.parts[3:]])
    else:
        page = assembly.page
        if problem == "foreign_reading":
            from tests.db.test_drawing_models import _page

            page = _page(session)
        reading = CanonicalObservation(
            document_version_id=page.document_version_id,
            page_id=page.id,
            document_role="SHOP",
            coordinate_space="stored",
            polygon=[["0.01", "0.01"], ["0.02", "0.01"], ["0.02", "0.02"], ["0.01", "0.02"]],
            semantic_type="cabinet_width",
            value_numerator=17,
            value_denominator=1,
            unit="in",
            status="HUMAN_CONFIRMED",
            authority="AUTHORITATIVE",
        )
        session.add(reading)
        session.flush()
        if problem != "duplicate_links":
            withdraw_reading_part(session, item_id=assembly.parts[2], actor="reviewer")
        session.add(
            ReadingPart(
                canonical_observation_id=reading.id,
                drawing_item_id=assembly.parts[2],
                signal="synthetic malformed link",
                confirmed_by="reviewer",
            )
        )
        session.flush()
    finding = assembly.check(session, store)
    assert finding.outcome in {"REVIEW_REQUIRED", "NOT_FOUND"}, finding.reason


def test_replaced_reading_requires_a_new_link(session: Session, store: LocalStore) -> None:
    assembly = Assembly(session, store)
    _replace_in_review(session, assembly.readings[2])
    evidence = evidence_operands(session, assembly.revision.id, _rules())
    assert not evidence.merge(RULE, form_widths())
    assert "replaced" in evidence.missing[RULE]


def test_field_cut_keeps_run_parameter_precedence(session: Session, store: LocalStore) -> None:
    assembly = Assembly(session, store)
    project_id = session.get_one(Package, assembly.revision.package_id).project_id
    parameter = ParameterSet(
        project_id=str(project_id),
        layer=ParameterLayer.RUN,
        version=1,
        parameters={
            "field_cut": ParameterValue(
                value=Quantity(value=Fraction(2), unit=Unit.INCH),
                provenance=Provenance.COMPANY_STANDARD,
                set_by="reviewer",
                set_at=datetime.now(UTC),
            )
        },
    )
    stored, values = to_rows(parameter, package_revision_id=assembly.revision.id)
    session.add(stored)
    session.add_all(values)
    session.flush()
    finding = assembly.check(session, store)
    assert finding.outcome == "FAIL", finding.reason
    assert "45" in finding.reason


def test_complete_run_wins_over_wrong_form_width(session: Session, store: LocalStore) -> None:
    from dataclasses import replace

    assembly = Assembly(session, store)
    evidence = evidence_operands(session, assembly.revision.id, _rules())
    form = form_widths()
    form["countertop_width"] = replace(
        form["countertop_width"], value=Measurement(Fraction(99), Unit.INCH, None)
    )
    selected = evidence.merge(RULE, form)
    assert selected["countertop_width"].value == Measurement(Fraction(43), Unit.INCH, None)
    assert selected["countertop_width"].source == "SHOP"


def test_synthetic_run_matches_the_form_result(session: Session, store: LocalStore) -> None:
    assembly = Assembly(session, store)
    # Stored project field cut is exercised by check(); compare the operand values here too.
    selected = evidence_operands(session, assembly.revision.id, _rules()).operands[RULE]
    assert {n: o.value for n, o in selected.items()} == {
        n: o.value for n, o in form_widths().items()
    }
    assert assembly.check(session, store, form=False).outcome == "PASS"


@pytest.mark.parametrize("missing", [None, 2])
def test_six_part_fraction_run_never_substitutes_form_widths(
    session: Session, store: LocalStore, missing: int | None
) -> None:
    assembly = Assembly(session, store, six_parts=True, missing=missing)
    result = assembly.check(session, store)
    if missing is not None:
        assert result.outcome == "NOT_FOUND"
        return
    selected = evidence_operands(session, assembly.revision.id, _rules()).operands[RULE]
    assert selected["countertop_width"].value == Measurement(Fraction(47, 2), Unit.INCH, None)
    assert selected["filler_widths"].value == tuple(
        Measurement(value, Unit.INCH, None)
        for value in (Fraction(5, 4), Fraction(5, 2), Fraction(2))
    )
    assert result.outcome == "PASS"
    for reading in assembly.readings:
        assert str(reading) in " ".join(result.notes)


def test_no_filler_is_not_a_fabricated_zero(session: Session, store: LocalStore) -> None:
    assembly = Assembly(session, store)
    _replace_run(session, assembly, assembly.parts[2:4])
    result = assembly.check(session, store)
    assert result.outcome == "NOT_FOUND"
    assert "no zero width" in result.reason


def test_another_revisions_run_does_not_change_the_no_run_path(
    session: Session, store: LocalStore
) -> None:
    Assembly(session, store)
    other = Assembly(session, store, run=False)
    assert other.check(session, store).outcome == "PASS"


def test_overlapping_drawings_are_ambiguous(session: Session, store: LocalStore) -> None:
    assembly = Assembly(session, store)
    session.add(DrawingView(page_id=assembly.page.id, tag="OVERLAP", region=REGION))
    session.flush()
    assert assembly.check(session, store).outcome == "REVIEW_REQUIRED"
