"""The width check for every wall case, read back for the screens and reports (#1202).

The reviewer's one-click wall answer shows each choice with the overall it needs and its result, and
the "i" explanation shows the row's own worked example. Both come from the row's **recorded**
width result (`workflow/wall_cases.py` stored every case in its trace): nothing here works a
number out. Where there is no such result, the cases are empty and a plain reason says why.

Also `current_field_cut`: the field cut per wall end a run would use now, for the explanation's
"1" is this project's field cut now"; and `field_cut_changed`: when it differs from the one a
recorded result used, that result's choices are out of date and are not shown (#1202 review).

Source: issue #1202 · Verification: `tests/api/test_wall_cases_api.py`
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from fractions import Fraction
from typing import Any, Final
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.package import Package, PackageRevision
from app.models.parameters import ParameterSet, ParameterValue
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.verdicts import CheckRun, Finding
from app.schemas.visual_ui import ExactValueOut, WallCaseOut, WallCasesOut
from rules.parameters import ParameterLayer
from units.imperial import format_inches, parse_imperial
from vocabulary.wall_layouts import wall_case_words
from workflow.wall_cases import (
    is_every_wall_case,
    read_wall_cases,
    scale_inputs,
    stored_wall_cases,
)

__all__ = [
    "FIELD_CUT_CHANGED",
    "NOT_ALL_SEALED",
    "NO_CHECK_RUN",
    "SAVED_AFTER_RUN",
    "WALLS_ALREADY_USED",
    "current_field_cut",
    "field_cut_changed",
    "field_cut_text",
    "latest_width_findings",
    "recorded_field_cut",
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
FIELD_CUT: Final = "field_cut"
FIELD_CUT_CHANGED: Final = (
    "The project's field cut changed since the last check run; run the checks to see each "
    "choice's result."
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
    """Every wall case of a recorded every-case result; otherwise empty with `unavailable_reason`,
    or with why the recorded cases could not be read back (never silently empty)."""
    if unavailable_reason is not None or trace is None:
        return WallCasesOut(unavailable_reason=unavailable_reason)
    cases, unreadable = read_wall_cases(trace)
    if unreadable is not None or not cases:
        return WallCasesOut(unavailable_reason=unreadable)
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


def unavailable_reason(
    finding: Finding | None, *, saved_after: bool, field_cut_changed: bool = False
) -> str | None:
    """Why a row's choices carry no result; `None` when its every-case result can be shown."""
    if finding is None:
        return NO_CHECK_RUN
    if saved_after:
        return SAVED_AFTER_RUN
    if field_cut_changed:
        return FIELD_CUT_CHANGED
    if is_every_wall_case(finding.trace):
        return None
    return WALLS_ALREADY_USED if finding.variant else NOT_ALL_SEALED


def current_field_cut(
    session: Session, revision_id: UUID, *, project_id: UUID | None = None
) -> Fraction | None:
    """The field cut per wall end a check run would use now, in inches (#1202 review).

    The newest set of each stored layer, the run's over the project's over GV's default, as a run
    resolves them; with none holding it, the published rulebook's own default. One statement, and
    one more only when no stored layer sets it. `None` when nothing sets it (or not in inches).
    Without `project_id` the revision's own project is found inside the same statement.
    """
    project = (
        project_id
        if project_id is not None
        else (
            select(Package.project_id)
            .join(PackageRevision, PackageRevision.package_id == Package.id)
            .where(PackageRevision.id == revision_id)
            .scalar_subquery()
        )
    )
    rows = session.execute(
        select(
            ParameterSet.layer,
            ParameterSet.version,
            ParameterValue.numerator,
            ParameterValue.denominator,
            ParameterValue.unit,
        )
        .outerjoin(
            ParameterValue,
            and_(
                ParameterValue.parameter_set_id == ParameterSet.id,
                ParameterValue.name == FIELD_CUT,
            ),
        )
        .where(
            or_(
                and_(
                    ParameterSet.layer == ParameterLayer.GLOBAL.value,
                    ParameterSet.project_id.is_(None),
                ),
                and_(
                    ParameterSet.layer == ParameterLayer.PROJECT.value,
                    ParameterSet.project_id == project,
                ),
                and_(
                    ParameterSet.layer == ParameterLayer.RUN.value,
                    ParameterSet.package_revision_id == revision_id,
                ),
            )
        )
    ).all()
    newest: dict[str, tuple[int, Fraction | None]] = {}
    for layer, version, numerator, denominator, unit in rows:
        value = (
            Fraction(numerator, denominator)
            if numerator is not None and denominator and unit == "in"
            else None
        )
        if layer not in newest or version > newest[layer][0]:
            newest[layer] = (version, value)
    for layer in (ParameterLayer.RUN, ParameterLayer.PROJECT, ParameterLayer.GLOBAL):
        found = newest.get(layer.value)
        if found is not None and found[1] is not None:
            return found[1]
    return _rulebook_field_cut(session)


def _rulebook_field_cut(session: Session) -> Fraction | None:
    """The published width rule's own default field cut, read from its newest snapshot's text."""
    snapshots = session.execute(
        select(RuleSnapshot.version, RuleSnapshot.canonical_json)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(RuleDefinition.rule_id == "CT-WIDTH-001")
    ).all()

    def order(version: str) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in version.split("."))
        except ValueError:
            return ()

    if not snapshots:
        return None
    _version, text = max(snapshots, key=lambda row: order(row[0]))
    try:
        default = json.loads(text)["parameters"][FIELD_CUT]["default"]
        if default.get("unit") != "in":
            return None
        return Fraction(str(default["value"]))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def field_cut_text(value: Fraction | None) -> str | None:
    return None if value is None else f'{format_inches(value)}"'


def recorded_field_cut(finding: Finding | None) -> Fraction | None:
    """The field cut per wall end a recorded width result used, from its own trace."""
    trace = None if finding is None else finding.trace
    if not isinstance(trace, Mapping):
        return None
    per_end = next(
        (case.field_cut_per_end for case in stored_wall_cases(trace) if case.field_cut_per_end),
        None,
    )
    if per_end is not None:
        return per_end
    pairs: Any = trace.get("intermediates") or ()
    raw = next(
        (
            pair[1]
            for pair in pairs
            if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] == "field_cut_total"
        ),
        None,
    )
    return scale_inputs(raw)[0]


def field_cut_changed(finding: Finding | None, current: Fraction | None) -> bool:
    """Whether the project's field cut changed since this result was recorded: its numbers are
    then out of date until the checks run again (#1202 review)."""
    recorded = recorded_field_cut(finding)
    return recorded is not None and current is not None and recorded != current
