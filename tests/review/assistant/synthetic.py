"""A made-up review for the assistant's tests (#1128). Synthetic values only, no client data.

Page 3: no countertop. Page 4: a countertop that needs correction (printed 84 1/2", needed 85").
Page 5: another check waiting on a value. Page 7: a countertop held because the AIs picked different
lines. Page 9: a countertop that looks right, with a decision carried over, and a second countertop
row that was not checked.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from uuid import UUID, uuid4

from app.review.assistant.records import ReviewSnapshot, build_snapshot
from app.schemas.visual_ui import (
    AgreementFactsOut,
    ArchitectResultOut,
    CountertopPieceOut,
    CountertopResultOut,
    CountertopResultsOut,
    ExactValueOut,
    HoldOut,
    PageWithoutCountertopOut,
    ReviewerDecisionOut,
    RowNotCheckedOut,
    WallLayoutOut,
)
from units.imperial import format_inches
from verdict.outcomes import Outcome
from workflow.findings_composer import ComposerFinding

PACKAGE = UUID("00000000-0000-4000-8000-000000000001")
REVISION = UUID("00000000-0000-4000-8000-000000000002")
ROW_FAIL = UUID("00000000-0000-4000-8000-0000000000a4")
ROW_HELD = UUID("00000000-0000-4000-8000-0000000000a7")
ROW_PASS = UUID("00000000-0000-4000-8000-0000000000a9")
FINDING_FAIL = UUID("00000000-0000-4000-8000-0000000000f4")
FINDING_HELD = UUID("00000000-0000-4000-8000-0000000000f7")
FINDING_PASS = UUID("00000000-0000-4000-8000-0000000000f9")
FINDING_SINK = UUID("00000000-0000-4000-8000-0000000000f5")

HOLD_REASON = "The two AIs picked different countertop lines on this page, so nothing was read."
FAIL_REASON = "The printed overall does not match the pieces below it plus the field cut."
SINK_REASON = "The sink centre line is not given on this sheet, so the check could not run."
NO_COUNTERTOP_REASON = "Both AIs found no stone countertop line on this sheet."
SECOND_ROW_REASON = "An AI found a second countertop on this page; it was not checked."
READINESS_REASON = (
    "3 findings still need a valid reviewer decision or a check rerun after correction. "
    "Add a note when required."
)


def exact(value: Fraction) -> ExactValueOut:
    return ExactValueOut(
        numerator=str(value.numerator),
        denominator=str(value.denominator),
        display=f'{format_inches(value)}"',
    )


def _agreement() -> AgreementFactsOut:
    return AgreementFactsOut(
        both_readers_agreed_on_row=True, values_agreed=(), code_clue_used=False
    )


def _piece(index: int, value: Fraction) -> CountertopPieceOut:
    return CountertopPieceOut(index=index, value=exact(value), source="sealed", kind="cabinet")


def countertop_results() -> CountertopResultsOut:
    fail = CountertopResultOut(
        finding_id=FINDING_FAIL,
        row_id=ROW_FAIL,
        page_number=4,
        label="Sample run A",
        row_location=None,
        outcome=Outcome.FAIL,
        reviewer_decision=None,
        needs_decision=True,
        printed_overall=exact(Fraction(169, 2)),
        pieces=(_piece(0, Fraction(30)), _piece(1, Fraction(36)), _piece(2, Fraction(18))),
        field_cut_per_end=exact(Fraction(1, 2)),
        field_cut_count=2,
        expected_total=exact(Fraction(85)),
        delta=exact(Fraction(-1, 2)),
        wall_layout=WallLayoutOut(
            config="back_left_right", label="back wall and both ends", source="both readers"
        ),
        agreement=_agreement(),
        hold=None,
    )
    held = CountertopResultOut(
        finding_id=FINDING_HELD,
        row_id=ROW_HELD,
        page_number=7,
        label="Sample run B",
        row_location=None,
        outcome=Outcome.REVIEW_REQUIRED,
        reviewer_decision=None,
        needs_decision=True,
        printed_overall=None,
        pieces=(),
        field_cut_per_end=None,
        field_cut_count=None,
        expected_total=None,
        delta=None,
        wall_layout=WallLayoutOut(config=None, label=None, source="not established"),
        agreement=_agreement(),
        hold=HoldOut(code="row-choice-split", reason=HOLD_REASON),
        architect=ArchitectResultOut(not_compared_reason="Not compared: no countertop line."),
    )
    passed = CountertopResultOut(
        finding_id=FINDING_PASS,
        row_id=ROW_PASS,
        page_number=9,
        label="Sample run C",
        row_location=None,
        outcome=Outcome.PASS,
        reviewer_decision=ReviewerDecisionOut(
            action="confirm",
            note=None,
            actor="reviewer-one",
            time=datetime(2026, 10, 1, tzinfo=UTC),
            carried_over=True,
            carried_from_finding_id=uuid4(),
        ),
        needs_decision=False,
        printed_overall=exact(Fraction(96)),
        pieces=(_piece(0, Fraction(30)), _piece(1, Fraction(36)), _piece(2, Fraction(30))),
        field_cut_per_end=None,
        field_cut_count=0,
        expected_total=exact(Fraction(96)),
        delta=exact(Fraction(0)),
        wall_layout=WallLayoutOut(config="island", label="island; no wall ends", source="reviewer"),
        agreement=_agreement(),
        hold=None,
    )
    return CountertopResultsOut(
        package_id=PACKAGE,
        revision_id=REVISION,
        items=(fail, held, passed),
        pages_without_countertop=(
            PageWithoutCountertopOut(page_number=3, reason=NO_COUNTERTOP_REASON),
        ),
        rows_not_checked=(RowNotCheckedOut(page_number=9, reason=SECOND_ROW_REASON),),
    )


def composer(
    key: UUID,
    check: str,
    name: str,
    outcome: str,
    reason: str,
    *,
    pages: tuple[str, ...] = (),
    tolerance: str | None = None,
) -> ComposerFinding:
    return ComposerFinding(
        key=str(key),
        check=check,
        check_name=name,
        outcome=outcome,
        severity="critical",
        reason=reason,
        comparison=None,
        difference=None,
        tolerance=tolerance,
        arithmetic_unit=None,
        operands=(),
        evidence_pages=pages,
        notes=(),
    )


def findings() -> tuple[ComposerFinding, ...]:
    return (
        composer(
            FINDING_FAIL,
            "CT-WIDTH-001",
            "Countertop width",
            "FAIL",
            FAIL_REASON,
            pages=("4",),
            tolerance='0"',
        ),
        composer(FINDING_HELD, "CT-WIDTH-001", "Countertop width", "REVIEW_REQUIRED", HOLD_REASON),
        composer(FINDING_PASS, "CT-WIDTH-001", "Countertop width", "PASS", "It matches."),
        composer(
            FINDING_SINK, "CT-SINK-001", "Sink centre line", "NOT_FOUND", SINK_REASON, pages=("5",)
        ),
    )


@dataclass(frozen=True)
class Readiness:
    can_approve: bool = False
    blocking_findings: int = 3
    blocking_finding_ids: tuple[UUID, ...] = (FINDING_FAIL, FINDING_HELD, FINDING_SINK)
    reason: str | None = READINESS_REASON


@dataclass(frozen=True)
class _Action:
    action: str
    note: str | None = None


@dataclass(frozen=True)
class Decision:
    action: _Action
    carried_from: UUID | None = None

    @property
    def carried_over(self) -> bool:
        return self.carried_from is not None


@dataclass(frozen=True)
class Inputs:
    countertops: CountertopResultsOut = field(default_factory=countertop_results)
    readiness: Readiness = field(default_factory=Readiness)
    findings: tuple[ComposerFinding, ...] = field(default_factory=findings)
    decisions: Mapping[UUID, Decision] = field(default_factory=dict)
    checks_have_run: bool = True


def snapshot(inputs: Inputs | None = None) -> ReviewSnapshot:
    given = inputs or Inputs()
    return build_snapshot(
        given.countertops,
        given.readiness,
        given.findings,
        given.decisions,
        checks_have_run=given.checks_have_run,
    )


def empty_snapshot() -> ReviewSnapshot:
    """A package nothing has checked yet."""
    return snapshot(
        Inputs(
            countertops=CountertopResultsOut(package_id=PACKAGE, revision_id=REVISION, items=()),
            readiness=Readiness(
                can_approve=False,
                blocking_findings=0,
                blocking_finding_ids=(),
                reason="There are no findings to sign off. Run the checks first.",
            ),
            findings=(),
            checks_have_run=False,
        )
    )
