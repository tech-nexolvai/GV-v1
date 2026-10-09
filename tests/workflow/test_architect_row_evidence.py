"""The vendor-vs-architect check per countertop row (CT-ARCH-WIDTH-001, #1054), through the database.

The pairing (#1053) is built in parallel, so every pairing here is a fake `EffectivePairing` handed
to `DatabaseStages(architect_pairing=...)`. The architect's values are stored exactly as the
architect reader stores them (#1052, `workflow/architect_reader.py`): one candidate per printed span
from the `architect-text` route, with its flags, inside a drawing whose role code confirmed.

Every value is invented. The vendor row is `tests/api/test_slot_rows._package_rows`: on the first
page pieces of 20" and 21" and an overall of 43"; on the second 21", 22" and 45".

Verification for: `workflow/architect_row_plan.py`, `workflow/architect_row_evidence.py`, the
architect block of `workflow/stages.DatabaseStages.run_checks`, and the row guard in
`app/verdicts/record.py`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import (
    CanonicalObservation,
    Document,
    DocumentVersion,
    EvidenceSupportingCandidate,
    ObservationCandidate,
    PackageRevision,
    PackageRevisionDocument,
    SourceArtifact,
    ViewRole,
)
from app.models.document import Page
from app.models.evidence import EvidenceCorroborationLane
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.runs import ExtractionRun, TaskRun
from app.models.verdicts import CheckRun, Finding, VerdictInput
from app.review.approval import approval_readiness
from app.verdicts.record import EvidenceMissing, record_finding
from app.verdicts.rulebook import snapshot_store
from extraction.architect.labels import read_label
from storage.local import LocalStore
from tests.api.test_slot_rows import _package_rows, _reader_support
from tests.app.postgres_fixture import alembic_config
from verdict.finding import Finding as DomainFinding
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.outcomes import Outcome
from workflow.architect_pairing_contract import EffectivePair, EffectivePairing
from workflow.architect_reader import ARCHITECT_EXTRACTOR, ARCHITECT_EXTRACTOR_VERSION
from workflow.architect_row_evidence import architect_row_operands
from workflow.architect_row_plan import (
    ARCHITECT_TEXT_ROUTE,
    CONFIRM_AI_PAIRING,
    NOTHING_PAIRED_ON_REVISION,
    PAIR_BY_REVIEWER,
    Disposition,
    effective_architect_pairing,
    plan_architect_row,
)
from workflow.review import ENGINE_VERSION
from workflow.slot_row_scope import SlotRow, slot_rows
from workflow.stages import DatabaseStages
from workflow.view_roles import confirm_view_role, confirm_view_role_by_code, record_panel_view

pytest_plugins = ("tests.app.postgres_fixture",)

ARCH_RULE = "CT-ARCH-WIDTH-001"
#: The architect's drawing: the top of the sheet, in the stored space (0..1 of the page).
ARCH_REGION = ((Decimal("0.1"), Decimal("0.05")), (Decimal("0.9"), Decimal("0.05")),
               (Decimal("0.9"), Decimal("0.4")), (Decimal("0.1"), Decimal("0.4")))  # fmt: skip


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


# ---------------------------------------------------------------------------
# The sheet: a sealed vendor row and the architect's drawing above it
# ---------------------------------------------------------------------------


def _sealed_rows(session: Session) -> tuple[UUID, dict[int, UUID]]:
    """Two vendor rows whose widths add up and whose walls come from drawing clues: both PASS."""
    _project_id, package_id, anchors = _package_rows(
        session, piece_count=2, widths_add_up=True, wall_source="vendor-drawing-clues"
    )
    for candidate in session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ).all():
        _reader_support(session, candidate)
    return package_id, anchors


def _page_of(session: Session, anchor: UUID) -> Page:
    candidate = session.get_one(ObservationCandidate, anchor)
    return session.get_one(Page, candidate.page_id)


def _architect_drawing(
    session: Session, anchor: UUID, *, role: ViewRole = ViewRole.ARCH
) -> ExtractionRun:
    """The architect's drawing on the row's page, its role confirmed by code, and the reader's run."""
    page = _page_of(session, anchor)
    view = record_panel_view(
        session,
        page_id=page.id,
        annotation_index=1,
        stored_points=ARCH_REGION,
        proposed_role=role.value,
        heading="ID SET ELEVATION",
        reason="synthetic heading",
    )
    confirm_view_role_by_code(session, view=view, role=role, reason="synthetic: both agree")
    slot_run = session.get_one(
        ExtractionRun, session.get_one(ObservationCandidate, anchor).extraction_run_id
    )
    slot_task = session.get_one(TaskRun, slot_run.task_run_id)
    task = TaskRun(
        workflow_run_id=slot_task.workflow_run_id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task)
    session.flush()
    run = ExtractionRun(
        task_run_id=task.id,
        extractor=ARCHITECT_EXTRACTOR,
        extractor_version=ARCHITECT_EXTRACTOR_VERSION,
        config_hash="synthetic-architect",
        dpi=150,
    )
    session.add(run)
    session.flush()
    return run


def _architect_value(
    session: Session,
    run: ExtractionRun,
    anchor: UUID,
    text: str,
    *,
    slot: int = 0,
    held: str | None = None,
    outline: Literal["yes", "no", "unknown"] = "yes",
    extra_flags: tuple[str, ...] = (),
    box: tuple[int, int, int, int] = (300, 200, 400, 230),
    unit: str = "in",
) -> ObservationCandidate:
    """One printed architect span, stored as `persist_architect_pages` stores it."""
    page = _page_of(session, anchor)
    reading = read_label(text)
    value = None if held is not None else reading.inches
    left, top, right, bottom = box
    flags = [
        "architect-reader",
        "arch-view:1",
        "arch-row:1",
        f"arch-slot:{slot}",
        "arch-ticks:110:170",
        "arch-scale:1.5000",
        f"arch-ticks-on-outline:{outline}",
        *extra_flags,
    ]
    if held is not None:
        flags.append(f"arch-held:{held}")
    candidate = ObservationCandidate(
        document_version_id=page.document_version_id,
        page_id=page.id,
        extraction_run_id=run.id,
        raw_text=text,
        value_numerator=None if value is None else value.numerator,
        value_denominator=None if value is None else value.denominator,
        unit=None if value is None else unit,
        polygon=[[left, top], [right, top], [right, bottom], [left, bottom]],
        coordinate_space="image",
        ambiguity_flags=flags,
        review_reason=held,
    )
    session.add(candidate)
    session.flush()
    return candidate


def _pairing(
    *pairs: EffectivePair,
    source: Literal["code", "both-ais", "reviewer", "none"] = "code",
    status: str = "paired",
    reasons: tuple[str, ...] = ("synthetic pairing",),
) -> EffectivePairing:
    return EffectivePairing(
        record_id=uuid4(), source=source, status=status, pairs=pairs, reasons=reasons
    )


def _overall(candidate: ObservationCandidate) -> EffectivePair:
    return EffectivePair(
        kind="overall", architect_candidate_id=candidate.id, vendor_slot_indices=()
    )


def _piece(candidate: ObservationCandidate, *slots: int) -> EffectivePair:
    return EffectivePair(
        kind="piece", architect_candidate_id=candidate.id, vendor_slot_indices=slots
    )


def _run(
    session: Session,
    package_id: UUID,
    tmp_path: Path,
    pairings: Mapping[UUID, EffectivePairing] | None = None,
) -> dict[str, dict[UUID, Finding]]:
    """Run the checks with these fake pairings; the live row findings by rule, then by row."""
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    found = dict(pairings or {})
    stages = DatabaseStages(
        store=LocalStore(root=tmp_path / "store", ticket_secret=b"synthetic-test"),
        architect_pairing=lambda _session, anchor: found.get(anchor),
    )
    stages.run_checks(session, revision.id)
    rows = session.execute(
        select(Finding, RuleDefinition.rule_id)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(Finding.package_revision_id == revision.id, CheckRun.superseded_at.is_(None))
    ).all()
    by_rule: dict[str, dict[UUID, Finding]] = {}
    for finding, rule_id in rows:
        if finding.scope_row_candidate_id is not None:
            by_rule.setdefault(rule_id, {})[finding.scope_row_candidate_id] = finding
        else:
            by_rule.setdefault(rule_id, {})[UUID(int=0)] = finding
    return by_rule


def _inputs(session: Session, finding: Finding) -> dict[str, VerdictInput]:
    return {
        row.operand_name: row
        for row in session.scalars(
            select(VerdictInput).where(VerdictInput.check_run_id == finding.check_run_id)
        )
    }


def _only_the_revision_line(findings: dict[str, dict[UUID, Finding]]) -> None:
    """No row-scoped architect finding: only the revision's NO_APPLICABLE_RULE line."""
    architect = findings[ARCH_RULE]
    assert set(architect) == {UUID(int=0)}
    assert architect[UUID(int=0)].outcome == "NO_APPLICABLE_RULE"


def _row(session: Session, package_id: UUID, anchor: UUID) -> SlotRow:
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    return next(row for row in slot_rows(session, revision.id) if row.anchor.id == anchor)


# ---------------------------------------------------------------------------
# The seam and the route name
# ---------------------------------------------------------------------------


def test_the_route_name_is_the_architect_readers_own() -> None:
    assert ARCHITECT_TEXT_ROUTE == ARCHITECT_EXTRACTOR


def test_without_the_pairing_module_there_is_no_pairing(session: Session) -> None:
    """The join seam: until #1053's module is on this branch, every row has no pairing."""
    assert effective_architect_pairing(session, uuid4()) is None


def test_the_labels_used_here_read_exactly() -> None:
    assert read_label("3' - 7\"").inches == 43
    assert read_label("3' - 6 15/16\"").inches == Fraction(687, 16)
    assert read_label("1' - 8\"").inches == 20


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def test_equal_widths_paired_by_code_pass_beside_the_width_check(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")

    findings = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})

    finding = findings[ARCH_RULE][anchors[0]]
    assert finding.outcome == "PASS", finding.reason
    assert finding.scope_row_candidate_id == anchors[0]
    width = findings["CT-WIDTH-001"][anchors[0]]
    assert finding.scope_label == f"{width.scope_label} · architect"
    assert any(note.startswith("Pairing source: code") for note in finding.notes)
    inputs = _inputs(session, finding)
    assert {"architect_overall", "vendor_overall"} <= set(inputs)
    architect = inputs["architect_overall"]
    assert Fraction(architect.value_numerator, architect.value_denominator) == 43
    assert architect.evidence_status == EvidenceStatus.CORROBORATED.value
    observation = session.get_one(CanonicalObservation, architect.canonical_observation_id)
    assert observation.document_role == "ARCH"
    lanes = set(
        session.scalars(
            select(EvidenceCorroborationLane.lane).where(
                EvidenceCorroborationLane.canonical_observation_id == observation.id
            )
        )
    )
    assert lanes == {"DRAWN_LENGTH"}
    vendor = inputs["vendor_overall"]
    assert vendor.canonical_observation_id is not None
    assert session.get_one(CanonicalObservation, vendor.canonical_observation_id).document_role == (
        "SHOP"
    )
    # The other row has no pairing: no architect finding for it, and no revision-wide line either.
    assert set(findings[ARCH_RULE]) == {anchors[0]}


def test_a_sixteenth_apart_fails(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 6 15/16\"")

    finding = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})[
        ARCH_RULE
    ][anchors[0]]

    assert finding.outcome == "FAIL"
    assert Fraction(finding.delta_numerator, finding.delta_denominator) == Fraction(1, 16)


def test_one_to_one_pieces_are_compared_each_against_its_own_architect_span(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    first = _architect_value(session, run, anchors[0], "1' - 8\"", slot=0)
    second = _architect_value(
        session, run, anchors[0], "1' - 10\"", slot=1, box=(420, 200, 520, 230)
    )

    finding = _run(
        session,
        package_id,
        tmp_path,
        {anchors[0]: _pairing(_piece(first, 0), _piece(second, 1))},
    )[ARCH_RULE][anchors[0]]

    # 20 = 20 and 22 != 21: the second piece fails.
    assert finding.outcome == "FAIL"
    inputs = _inputs(session, finding)
    assert {"architect_piece[0]", "vendor_piece[0]", "architect_piece[1]", "vendor_piece[1]"} <= (
        set(inputs)
    )
    assert Fraction(
        inputs["vendor_piece[1]"].value_numerator, inputs["vendor_piece[1]"].value_denominator
    ) == Fraction(21)


def test_a_pass_resting_on_an_ai_only_pairing_waits_for_the_reviewer(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")

    finding = _run(
        session,
        package_id,
        tmp_path,
        {anchors[0]: _pairing(_overall(overall), source="both-ais")},
    )[ARCH_RULE][anchors[0]]

    assert finding.outcome == "REVIEW_REQUIRED"
    assert finding.reason == CONFIRM_AI_PAIRING
    # Nothing on record reads as a PASS: the stored trace is the abstention, not the calculation.
    assert finding.trace["outcome"] == "REVIEW_REQUIRED"
    assert "PASS" not in str(finding.trace)


def test_a_fail_on_an_ai_only_pairing_stands(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 6\"")

    finding = _run(
        session,
        package_id,
        tmp_path,
        {anchors[0]: _pairing(_overall(overall), source="both-ais")},
    )[ARCH_RULE][anchors[0]]

    assert finding.outcome == "FAIL"


def test_a_reviewers_pairing_may_pass(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")

    finding = _run(
        session,
        package_id,
        tmp_path,
        {anchors[0]: _pairing(_overall(overall), source="reviewer", status="reviewer")},
    )[ARCH_RULE][anchors[0]]

    assert finding.outcome == "PASS"


@pytest.mark.parametrize("status", ["ais-disagree", "ais-refused"])
def test_an_unsettled_ai_pairing_asks_the_reviewer_to_pair(
    session: Session, tmp_path: Path, status: str
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    _architect_value(session, run, anchors[0], "3' - 7\"")

    finding = _run(
        session, package_id, tmp_path, {anchors[0]: _pairing(source="both-ais", status=status)}
    )[ARCH_RULE][anchors[0]]

    assert finding.outcome == "REVIEW_REQUIRED"
    assert finding.reason == PAIR_BY_REVIEWER
    assert _inputs(session, finding) == {}


def test_code_undecided_with_a_usable_architect_dimension_asks_the_reviewer(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    _architect_value(session, run, anchors[0], "3' - 7\"")

    findings = _run(session, package_id, tmp_path, {anchors[0]: _pairing(status="ambiguous")})

    assert findings[ARCH_RULE][anchors[0]].outcome == "REVIEW_REQUIRED"
    assert findings[ARCH_RULE][anchors[0]].reason == PAIR_BY_REVIEWER


def test_code_undecided_with_only_centre_lines_or_held_spans_is_not_compared(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    _architect_value(session, run, anchors[0], "1' - 5\"", outline="no")
    _architect_value(session, run, anchors[0], "2' - 7\"", held="the drawn length disagrees")

    findings = _run(session, package_id, tmp_path, {anchors[0]: _pairing(status="ambiguous")})

    _only_the_revision_line(findings)


@pytest.mark.parametrize("status", ["nothing_comparable", "no_scale"])
def test_nothing_comparable_makes_no_finding_and_says_why(
    session: Session, tmp_path: Path, status: str
) -> None:
    package_id, anchors = _sealed_rows(session)
    pairing = _pairing(
        status=status, reasons=("every architect span ends on a fixture's centre line",)
    )

    findings = _run(session, package_id, tmp_path, {anchors[0]: pairing})

    _only_the_revision_line(findings)
    plan = plan_architect_row(session, _row(session, package_id, anchors[0]), pairing)
    assert plan.disposition is Disposition.NOT_COMPARED
    assert plan.reason == "every architect span ends on a fixture's centre line."


def test_no_pairing_at_all_makes_no_row_finding_and_one_line_that_blocks_nothing(
    session: Session, tmp_path: Path
) -> None:
    """Seen to have run, never a pass, and no click: one NO_APPLICABLE_RULE line for the revision."""
    package_id, anchors = _sealed_rows(session)
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()

    DatabaseStages(
        store=LocalStore(root=tmp_path / "store", ticket_secret=b"synthetic-test")
    ).run_checks(session, revision.id)

    rows = session.execute(
        select(Finding, RuleDefinition.rule_id)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(Finding.package_revision_id == revision.id, CheckRun.superseded_at.is_(None))
    ).all()
    architect = [finding for finding, rule_id in rows if rule_id == ARCH_RULE]
    assert len(architect) == 1
    (line,) = architect
    assert line.outcome == "NO_APPLICABLE_RULE"
    assert line.scope_row_candidate_id is None
    assert line.reason == NOTHING_PAIRED_ON_REVISION
    assert line.id not in approval_readiness(session, revision.id).blocking_finding_ids
    assert {rule_id for _finding, rule_id in rows} >= {"CT-WIDTH-001"}
    assert set(anchors.values()) <= {
        finding.scope_row_candidate_id for finding, rule_id in rows if rule_id == "CT-WIDTH-001"
    }


def test_no_revision_line_once_a_row_is_compared(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")

    findings = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})

    assert set(findings[ARCH_RULE]) == {anchors[0]}


def test_a_bay_the_vendor_splits_is_left_out_and_said_so(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    bay = _architect_value(session, run, anchors[0], "3' - 5\"")
    overall = _architect_value(session, run, anchors[0], "3' - 7\"", box=(300, 260, 400, 290))

    split_only = _pairing(_piece(bay, 0, 1))
    _only_the_revision_line(_run(session, package_id, tmp_path, {anchors[0]: split_only}))
    plan = plan_architect_row(session, _row(session, package_id, anchors[0]), split_only)
    assert plan.disposition is Disposition.NOT_COMPARED
    assert plan.reason is not None
    assert "the vendor splits this bay into 2 pieces; their sum is not compared in V1" in (
        plan.reason
    )

    both = _pairing(_piece(bay, 0, 1), _overall(overall))
    finding = _run(session, package_id, tmp_path, {anchors[0]: both})[ARCH_RULE][anchors[0]]
    assert finding.outcome == "PASS"
    assert any("splits this bay into 2 pieces" in note for note in finding.notes)
    assert not any(name.startswith("architect_piece") for name in _inputs(session, finding))


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"held": "the architect marks this number as not firm (VIF)"}, "held"),
        ({"outline": "no"}, "casework"),
        ({"outline": "unknown"}, "casework"),
        ({"extra_flags": ("arch-qualifier:EQ",)}, "EQ"),
        ({"extra_flags": ("reviewer-markup",)}, "black text"),
        ({"unit": "mm"}, "inches"),
    ],
)
def test_an_architect_value_the_check_may_not_use_goes_to_the_reviewer(
    session: Session, tmp_path: Path, kwargs: dict[str, Any], why: str
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"", **kwargs)

    finding = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})[
        ARCH_RULE
    ][anchors[0]]

    assert finding.outcome == "REVIEW_REQUIRED"
    assert why in (finding.reason or "")
    assert _inputs(session, finding) == {}
    assert (
        session.scalar(
            select(CanonicalObservation.id)
            .join(
                EvidenceSupportingCandidate,
                EvidenceSupportingCandidate.canonical_observation_id == CanonicalObservation.id,
            )
            .where(EvidenceSupportingCandidate.candidate_id == overall.id)
        )
        is None
    )


def test_a_reviewer_markup_value_is_never_an_architect_operand(
    session: Session, tmp_path: Path
) -> None:
    """GV's coloured markup has its own route; a pairing naming it compares nothing."""
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    markup_run = session.get_one(ExtractionRun, run.id)
    markup_run.extractor = "extraction.annotations"
    session.flush()
    marked = _architect_value(session, run, anchors[0], "3' - 7\"")

    finding = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(marked))})[
        ARCH_RULE
    ][anchors[0]]

    assert finding.outcome == "REVIEW_REQUIRED"
    assert "architect's own text" in (finding.reason or "")
    assert _inputs(session, finding) == {}


def test_a_value_in_a_drawing_confirmed_as_the_vendors_is_never_the_architects(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0], role=ViewRole.SHOP)
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")

    finding = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})[
        ARCH_RULE
    ][anchors[0]]

    assert finding.outcome == "REVIEW_REQUIRED"
    assert "confirmed as the architect's" in (finding.reason or "")


def test_a_person_confirming_the_drawing_as_the_vendors_later_wins(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")
    from app.models import DrawingView

    view = session.scalars(select(DrawingView)).one()
    confirm_view_role(session, view=view, role=ViewRole.SHOP, actor="synthetic reviewer")

    finding = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})[
        ARCH_RULE
    ][anchors[0]]

    assert finding.outcome == "REVIEW_REQUIRED"


def test_an_architect_value_on_another_page_is_refused(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[1])
    elsewhere = _architect_value(session, run, anchors[1], "3' - 7\"")

    finding = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(elsewhere))})[
        ARCH_RULE
    ][anchors[0]]

    assert finding.outcome == "REVIEW_REQUIRED"
    assert "different sheet" in (finding.reason or "")


def test_an_unsealed_vendor_row_is_not_compared_automatically(
    session: Session, tmp_path: Path
) -> None:
    """The vendor's side is exactly what the width check would seal: here, nothing yet."""
    _project_id, package_id, anchors = _package_rows(
        session, piece_count=2, widths_add_up=True, wall_source="vendor-drawing-clues"
    )
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")

    findings = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})

    finding = findings[ARCH_RULE][anchors[0]]
    assert finding.outcome == "REVIEW_REQUIRED"
    assert "vendor's row is not ready" in (finding.reason or "")


def test_a_wall_layout_waiting_for_the_reviewer_does_not_hold_the_architect_check(
    session: Session, tmp_path: Path
) -> None:
    """Walls decide the width check's field cut; they say nothing about the architect's widths."""
    _project_id, package_id, anchors = _package_rows(
        session, piece_count=2, widths_add_up=True, wall_source="readers"
    )
    for candidate in session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ).all():
        _reader_support(session, candidate)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")

    findings = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})

    assert findings["CT-WIDTH-001"][anchors[0]].outcome == "REVIEW_REQUIRED"
    assert findings[ARCH_RULE][anchors[0]].outcome == "PASS"


def test_an_inconsistent_pairing_record_goes_to_the_reviewer(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    span = _architect_value(session, run, anchors[0], "1' - 8\"")

    finding = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_piece(span, 7))})[
        ARCH_RULE
    ][anchors[0]]

    assert finding.outcome == "REVIEW_REQUIRED"
    assert "inconsistent" in (finding.reason or "")


def test_a_same_file_package_compares_nothing(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    shop = session.get_one(
        DocumentVersion, session.get_one(ObservationCandidate, anchors[0]).document_version_id
    )
    architectural = Document(package_id=package_id, kind="architectural")
    session.add(architectural)
    session.flush()
    artifact = SourceArtifact(storage_key=f"synthetic/{uuid4()}", sha256=shop.sha256, size=1)
    session.add(artifact)
    session.flush()
    twin = DocumentVersion(
        document_id=architectural.id,
        source_artifact_id=artifact.id,
        sha256=shop.sha256,
        page_count=2,
    )
    session.add(twin)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision.id,
            package_id=package_id,
            document_id=architectural.id,
            document_version_id=twin.id,
        )
    )
    session.flush()
    pairing = _pairing(_overall(overall))

    findings = _run(session, package_id, tmp_path, {anchors[0]: pairing})

    _only_the_revision_line(findings)
    plan = plan_architect_row(session, _row(session, package_id, anchors[0]), pairing)
    assert plan.reason is not None and "same file" in plan.reason


def test_the_width_checks_results_do_not_change(session: Session, tmp_path: Path) -> None:
    """CT-WIDTH-001 byte for byte, with and without the architect check beside it."""
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 6\"")

    def width_facts(findings: dict[str, dict[UUID, Finding]]) -> dict[UUID, tuple[object, ...]]:
        return {
            anchor: (
                finding.outcome,
                finding.reason,
                finding.trace,
                finding.notes,
                finding.delta_numerator,
                finding.delta_denominator,
                finding.scope_label,
                tuple(
                    sorted(
                        (name, item.value_numerator, item.value_denominator, item.evidence_status)
                        for name, item in _inputs(session, finding).items()
                    )
                ),
            )
            for anchor, finding in findings["CT-WIDTH-001"].items()
        }

    before = width_facts(_run(session, package_id, tmp_path))
    with_architect = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})
    after = width_facts(with_architect)

    assert with_architect[ARCH_RULE][anchors[0]].outcome == "FAIL"
    assert after == before
    assert {facts[0] for facts in before.values()} == {"PASS"}


def test_the_operands_are_the_paired_values_in_the_pairings_order(
    session: Session,
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    second = _architect_value(session, run, anchors[0], "1' - 9\"", slot=1)
    overall = _architect_value(session, run, anchors[0], "3' - 7\"", box=(300, 260, 400, 290))
    row = _row(session, package_id, anchors[0])
    plan = plan_architect_row(session, row, _pairing(_piece(second, 1), _overall(overall)))

    built = architect_row_operands(session, row, plan)

    assert built.eligible, built.reason
    architect = built.operands["architect_widths"].value
    vendor = built.operands["vendor_widths"].value
    assert isinstance(architect, tuple) and isinstance(vendor, tuple)
    assert [value.exact for value in architect] == [Fraction(43), Fraction(21)]
    assert [value.exact for value in vendor] == [Fraction(43), Fraction(21)]
    assert built.operands["architect_widths"].source == "ARCH"
    assert built.operands["vendor_widths"].source == "SHOP"


# ---------------------------------------------------------------------------
# The row guard in app/verdicts/record.py
# ---------------------------------------------------------------------------


def _decided(session: Session) -> DomainFinding:
    snapshot = snapshot_store(session).latest(ARCH_RULE)
    assert snapshot is not None
    return DomainFinding(
        rule_id=ARCH_RULE,
        outcome=Outcome.REVIEW_REQUIRED,
        severity=snapshot.rule.severity,
        reason="synthetic guard test",
        snapshot_id=snapshot.snapshot_id,
        engine_version=ENGINE_VERSION,
    )


def _sealed_architect_operand(
    session: Session, package_id: UUID, anchor: UUID, candidate: ObservationCandidate
) -> tuple[SlotRow, EffectivePairing, VerdictOperand]:
    pairing = _pairing(_overall(candidate))
    row = _row(session, package_id, anchor)
    built = architect_row_operands(session, row, plan_architect_row(session, row, pairing))
    assert built.eligible, built.reason
    return row, pairing, built.operands["architect_overall"]


def _record(
    session: Session,
    package_id: UUID,
    row: SlotRow,
    operand: VerdictOperand,
    pairing: EffectivePairing | None,
) -> None:
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    record_finding(
        session,
        package_revision_id=revision.id,
        finding=_decided(session),
        operands={operand.name: operand},
        parameter_set_ids={},
        scope_row_candidate_id=row.anchor.id,
        scope_label="synthetic row · architect",
        architect_pairing=pairing,
    )


def test_the_guard_admits_the_architect_value_the_rows_pairing_names(session: Session) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")
    row, pairing, operand = _sealed_architect_operand(session, package_id, anchors[0], overall)

    _record(session, package_id, row, operand, pairing)


def test_the_guard_refuses_an_architect_value_without_the_rows_pairing(session: Session) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")
    row, _pairing_used, operand = _sealed_architect_operand(
        session, package_id, anchors[0], overall
    )

    with pytest.raises(EvidenceMissing, match="pairing"):
        _record(session, package_id, row, operand, None)


def test_the_guard_refuses_an_architect_value_the_pairing_does_not_name(
    session: Session,
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")
    other = _architect_value(session, run, anchors[0], "1' - 8\"", box=(300, 260, 400, 290))
    row, _used, operand = _sealed_architect_operand(session, package_id, anchors[0], overall)

    with pytest.raises(EvidenceMissing, match="pairing names"):
        _record(session, package_id, row, operand, _pairing(_overall(other)))


def test_the_guard_refuses_an_architect_value_from_another_rows_page(session: Session) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[1])
    elsewhere = _architect_value(session, run, anchors[1], "3' - 9\"")
    other_row, pairing, operand = _sealed_architect_operand(
        session, package_id, anchors[1], elsewhere
    )
    assert other_row.anchor.id == anchors[1]

    with pytest.raises(EvidenceMissing, match="different row or page"):
        _record(session, package_id, _row(session, package_id, anchors[0]), operand, pairing)


def test_the_guard_refuses_once_a_person_says_the_drawing_is_the_vendors(
    session: Session,
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")
    row, pairing, operand = _sealed_architect_operand(session, package_id, anchors[0], overall)
    from app.models import DrawingView

    view = session.scalars(select(DrawingView)).one()
    confirm_view_role(session, view=view, role=ViewRole.SHOP, actor="synthetic reviewer")

    with pytest.raises(EvidenceMissing, match="architect"):
        _record(session, package_id, row, operand, pairing)


def test_a_pairing_without_a_row_is_refused(session: Session) -> None:
    package_id, _anchors = _sealed_rows(session)
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()

    with pytest.raises(EvidenceMissing, match="one countertop row"):
        record_finding(
            session,
            package_revision_id=revision.id,
            finding=_decided(session),
            operands={},
            parameter_set_ids={},
            architect_pairing=_pairing(),
        )
