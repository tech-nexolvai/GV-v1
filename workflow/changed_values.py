"""Read the changed-values report from the same immutable inputs as a live check run.

No current setting or rule is consulted.  A missing or inconsistent citation is not a report.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import ParameterValue as StoredParameterValue
from app.models.parameters import from_rows
from app.models.rules import RuleSnapshot as StoredRuleSnapshot
from app.models.verdicts import CheckRun, Finding
from app.verdicts.rulebook import from_row
from rules.overrides import override_report
from rules.parameters import (
    ParameterLayer,
    ParameterSet,
    ParameterValue,
    Provenance,
    ResolvedParameter,
    is_rulebook_default,
    resolve_all,
)
from rules.schema import Quantity, Rule
from units.imperial import format_inches
from units.measurement import Unit
from vocabulary.setting_names import setting_name

UNAVAILABLE = "Not available for this check run; re-run the checks to see it"
NOT_RUN = "Run checks to see changed values"


@dataclass(frozen=True, slots=True)
class ChangedValues:
    status: str
    message: str | None
    company_standards_displaced: tuple[str, ...]
    outstanding: tuple[str, ...]
    entered: tuple[UsedValue, ...] = ()
    """Every setting a person entered for this job or package that set no GV standard aside (#1181):
    a sink's size, an overhang with no company default."""
    defaults_used: tuple[UsedValue, ...] = ()
    """Every GV default (a company value an admin set) that stood in for a check because the job set
    none (#1181). The rulebook's own defaults are not listed: they are the rules as published."""


@dataclass(frozen=True, slots=True)
class UsedValue:
    """One setting a run used, said in plain words (#1181)."""

    name: str
    text: str
    where: str
    """`this job`, `this package` or `GV default`."""
    source: str
    set_by: str
    set_at: datetime

    def line(self) -> str:
        """For the reviewer and the signed report: what, where from, who and when."""
        return (
            f"{setting_name(self.name)}: {self.text} ({self.where}; {self.source}; "
            f"set by {self.set_by} on {self.set_at.strftime('%d %b %Y')})"
        )

    def plain(self) -> str:
        """For the vendor's redline: the setting and its value only, never a person or a note."""
        return f"{setting_name(self.name)}: {self.text}"


def _unavailable() -> ChangedValues:
    return ChangedValues("unavailable", UNAVAILABLE, (), ())


def _defaults(canonical_json: str, cited_hash: str) -> ParameterSet:
    if "sha256:" + hashlib.sha256(canonical_json.encode("utf-8")).hexdigest() != cited_hash:
        raise ValueError("the recorded defaults hash does not match its stored bytes")
    data = json.loads(canonical_json)
    if not isinstance(data, dict) or data.get("layer") != "global":
        raise ValueError("the recorded defaults are not a GLOBAL parameter set")
    parameters = {
        name: ParameterValue(
            value=Quantity(value=Fraction(value["value"]), unit=Unit(value["unit"])),
            provenance=Provenance(value["provenance"]),
            set_by=value["set_by"],
            set_at=datetime.fromisoformat(value["set_at"]),
            reference=value.get("reference"),
        )
        for name, value in data["parameters"].items()
    }
    result = ParameterSet(
        project_id=data["project_id"],
        layer=ParameterLayer(data["layer"]),
        version=data["version"],
        parameters=parameters,
    )
    if result.canonical_json() != canonical_json or result.set_id != cited_hash:
        raise ValueError("the stored defaults do not reconstruct to their cited content hash")
    return result


def _stored_layer(session: Session, set_id: str) -> ParameterSet:
    row = session.scalar(select(StoredParameterSet).where(StoredParameterSet.set_id == set_id))
    if row is None:
        raise ValueError("a cited setting version is not stored")
    values = list(
        session.scalars(
            select(StoredParameterValue).where(StoredParameterValue.parameter_set_id == row.id)
        )
    )
    layer = from_rows(row, values)
    if layer.set_id != set_id:
        raise ValueError("a cited setting version failed its content hash")
    return layer


def layered_parameter_sets(
    defaults: ParameterSet, stored: tuple[ParameterSet, ...]
) -> tuple[ParameterSet, ...]:
    """The same by-name company/default merge used for the check, before PROJECT/RUN."""
    company = next((layer for layer in stored if layer.layer is ParameterLayer.GLOBAL), None)
    if company is None:
        return (defaults, *stored)
    merged = ParameterSet(
        project_id=company.project_id,
        layer=company.layer,
        version=company.version,
        parameters={**defaults.parameters, **company.parameters},
    )
    return tuple(merged if layer is company else layer for layer in stored)


def changed_values_for_revision(
    session: Session, revision_id: UUID, *, finding_ids: tuple[UUID, ...] | None = None
) -> ChangedValues:
    """Resolve only current findings; mismatched batches or missing citations abstain."""
    rows = session.execute(
        select(Finding, CheckRun, StoredRuleSnapshot)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(StoredRuleSnapshot, StoredRuleSnapshot.id == CheckRun.rule_snapshot_id)
        .where(
            Finding.package_revision_id == revision_id,
            (
                CheckRun.superseded_at.is_(None)
                if finding_ids is None
                else Finding.id.in_(finding_ids)
            ),
        )
    ).all()
    if not rows:
        return ChangedValues("not_run", NOT_RUN, (), ())
    first_finding, first_run, _ = rows[0]
    if first_run.defaults_set_id is None or first_run.defaults_canonical_json is None:
        return _unavailable()
    citations = first_finding.parameter_set_versions
    try:
        defaults = _defaults(first_run.defaults_canonical_json, first_run.defaults_set_id)
        snapshots = {}
        for finding, run, snapshot in rows:
            if (
                run.defaults_set_id != first_run.defaults_set_id
                or run.defaults_canonical_json != first_run.defaults_canonical_json
                or finding.parameter_set_versions != citations
            ):
                return _unavailable()
            verified = from_row(snapshot)
            snapshots[verified.snapshot_id] = verified.rule
        if not isinstance(citations, dict):
            return _unavailable()
        # The GLOBAL citation names a stored company set when one exists; otherwise the
        # synthetic defaults must be the very hash recorded on the check run.
        layers: list[ParameterSet] = []
        for layer_name, set_id in citations.items():
            layer_kind = ParameterLayer(layer_name)
            if layer_kind is ParameterLayer.GLOBAL and set_id == defaults.set_id:
                continue
            stored = _stored_layer(session, set_id)
            if stored.layer is not layer_kind:
                return _unavailable()
            layers.append(stored)
        if "global" not in citations or not isinstance(citations["global"], str):
            return _unavailable()
        layered = layered_parameter_sets(defaults, tuple(layers))
        report = override_report(snapshots.values(), *layered)
        entered, defaults_used = _entered(snapshots.values(), layered)
    except (KeyError, TypeError, ValueError):
        return _unavailable()
    return ChangedValues(
        "available",
        None,
        tuple(item.explain() for item in report.company_standards_displaced),
        report.outstanding,
        entered,
        defaults_used,
    )


def _used(name: str, resolved: ResolvedParameter, where: str) -> UsedValue:
    value = resolved.value
    exact = value.value.exact_value
    unit = value.value.unit
    return UsedValue(
        name=name,
        text=f'{format_inches(exact)}"' if unit is Unit.INCH else f"{exact} {unit.value}",
        where=where,
        source=value.source_text,
        set_by=value.set_by,
        set_at=value.set_at,
    )


def _entered(
    rules: Iterable[Rule], layered: tuple[ParameterSet, ...]
) -> tuple[tuple[UsedValue, ...], tuple[UsedValue, ...]]:
    """Settings a person entered that displaced no GV standard, and GV defaults that stood in (#1181).

    The run's own settings, resolved as the check resolved them. A dimension typed off a drawing is
    stored beside the settings (`rule_id:name`) and is not a setting, so only names a rule declares as
    a parameter count. A value that set a GV standard aside is already in the report's first list.
    """
    declared = {name for rule in rules for name in rule.parameters}
    entered: list[UsedValue] = []
    defaults: list[UsedValue] = []
    for name, resolved in sorted(resolve_all(*layered).items()):
        if name not in declared:
            continue
        if resolved.layer is ParameterLayer.GLOBAL:
            if not is_rulebook_default(resolved.value):
                defaults.append(_used(name, resolved, "GV default"))
            continue
        if resolved.overrides_a_company_standard:
            continue
        where = "this package" if resolved.layer is ParameterLayer.RUN else "this job"
        entered.append(_used(name, resolved, where))
    return tuple(entered), tuple(defaults)
