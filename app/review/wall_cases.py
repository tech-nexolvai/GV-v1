"""The width check for every wall case, read back for the screens and reports (#1202).

The reviewer's one-click wall answer shows each choice with the overall it needs and its result, and
the "i" explanation shows the row's own worked example. Both come from the row's **recorded**
width result (`workflow/wall_cases.py` stored every case in its trace): nothing here works a
number out. Where there is no such result, the cases are empty and a plain reason says why.

Also `field_cut_in_force`: the field cut per wall end the newest width results used (the
project's value as the last run read it), for the explanation's "1" is this project's field cut".

Source: issue #1202 · Verification: `tests/api/test_wall_cases_api.py`
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from fractions import Fraction
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.verdicts import CheckRun, Finding
from app.schemas.visual_ui import ExactValueOut, WallCaseOut, WallCasesOut
from units.imperial import format_inches, parse_imperial
from vocabulary.wall_layouts import wall_case_words
from workflow.wall_cases import is_every_wall_case, scale_inputs, stored_wall_cases

__all__ = [
    "NOT_ALL_SEALED",
    "NO_CHECK_RUN",
    "SAVED_AFTER_RUN",
    "WALLS_ALREADY_USED",
    "field_cut_in_force",
    "latest_width_findings",
    "unavailable_reason",
    "wall_cases_out",
]

NO_CHECK_RUN: Final = "Run the checks to see each choice's result."
NOT_ALL_SEALED: Final = (
    "Each choice's result shows once every width on this row is sealed and nothing holds it."
)
WALLS_ALREADY_USED: Final = (
    "This row's result already used its walls; run the checks again after changing them."
)
SAVED_AFTER_RUN: Final = (
    "A wall answer or a width was saved after the last check run; run the checks to see each "
    "choice's result."
)


def _exact(value: Fraction | None) -> ExactValueOut | None:
    if value is None:
        return None
    return ExactValueOut(
        numerator=str(value.numerator),
        denominator=str(value.denominator),
        display=f'{format_inches(value)}"',
    )


def _operand(trace: Mapping[str, Any], name: str) -> Fraction | None:
    for operand in trace.get("operands", ()) or ():
        if isinstance(operand, Mapping) and operand.get("name") == name:
            raw = operand.get("value")
            if isinstance(raw, str):
                try:
                    return parse_imperial(raw.removesuffix(" in").removesuffix('"'))
                except (ValueError, TypeError):
                    return None
    return None


def wall_cases_out(
    trace: Mapping[str, Any] | None,
    *,
    between_panels: bool = False,
    unavailable_reason: str | None = None,
) -> WallCasesOut:
    """Every wall case of a recorded every-case result; otherwise empty with `unavailable_reason`."""
    cases = stored_wall_cases(trace) if unavailable_reason is None else ()
    if not cases or trace is None:
        return WallCasesOut(unavailable_reason=unavailable_reason)
    per_end = next((case.field_cut_per_end for case in cases if case.field_cut_per_end), None)
    row_width = next((case.row_width for case in cases if case.row_width is not None), None)
    return WallCasesOut(
        cases=tuple(
            WallCaseOut(
                layout=case.layout,
                words=wall_case_words(case.layout, between_panels=between_panels),
                field_cut_count=case.field_cut_count,
                field_cut_total=_exact(case.field_cut_total),
                expected_total=_exact(case.expected),
                outcome=case.outcome,
            )
            for case in cases
        ),
        vendor_wrote=_exact(_operand(trace, "countertop_width")),
        pieces_add_up_to=_exact(row_width),
        field_cut_per_end=_exact(per_end),
    )


def latest_width_findings(
    session: Session, revision_id: UUID, row_ids: Collection[UUID]
) -> dict[UUID, Finding]:
    """Each row's live width result (CT-WIDTH-001, not superseded), in one statement."""
    if not row_ids:
        return {}
    found = session.scalars(
        select(Finding)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(
            Finding.package_revision_id == revision_id,
            CheckRun.superseded_at.is_(None),
            Finding.scope_row_candidate_id.in_(list(row_ids)),
            RuleDefinition.rule_id == "CT-WIDTH-001",
        )
    ).all()
    return {
        finding.scope_row_candidate_id: finding
        for finding in found
        if finding.scope_row_candidate_id is not None
    }


def unavailable_reason(finding: Finding | None, *, saved_after: bool) -> str | None:
    """Why a row's choices carry no result; `None` when its every-case result can be shown."""
    if finding is None:
        return NO_CHECK_RUN
    if saved_after:
        return SAVED_AFTER_RUN
    if is_every_wall_case(finding.trace):
        return None
    return WALLS_ALREADY_USED if finding.variant else NOT_ALL_SEALED


def field_cut_in_force(findings: Iterable[Finding]) -> str | None:
    """The field cut per wall end the newest width results used, as text (`1"`), or `None`.

    Read from the recorded results already loaded (no statement of its own): a row's own result
    names it, and every row of a revision is checked with the same project setting. Before any
    check has run there is none, and the explanation then names no number.
    """
    for finding in findings:
        trace = finding.trace
        if not isinstance(trace, Mapping):
            continue
        per_end = next(
            (case.field_cut_per_end for case in stored_wall_cases(trace) if case.field_cut_per_end),
            None,
        )
        if per_end is None:
            pairs: Any = trace.get("intermediates") or ()
            raw = next(
                (
                    pair[1]
                    for pair in pairs
                    if isinstance(pair, (list, tuple))
                    and len(pair) == 2
                    and pair[0] == "field_cut_total"
                ),
                None,
            )
            per_end = scale_inputs(raw)[0]
        if per_end is not None:
            return f'{format_inches(per_end)}"'
    return None
