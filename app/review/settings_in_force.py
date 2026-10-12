"""Which value each setting has right now for one package, and where it comes from (#1181).

Measurements showed a setting blank whenever nothing had been typed for the project, although a
check would run on GV's default or on the sink card: the reviewer could not see what a run would use.
This answers it, setting by setting, in the order a run resolves it — the rulebook's own default,
then GV's default (the company layer), then the project's value (the sink card's, for the sink's
size), then a value typed for this package — so the screen can say "In use: 30" — from the sink
card" instead of an empty box.

Read-only; the newest version of each layer, the same versions a run reads.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import ParameterValue as StoredParameterValue
from rules.parameters import ParameterLayer
from rules.schema import Rule
from units.imperial import format_inches
from units.measurement import Unit

__all__ = ["SINK_SETTINGS", "InForce", "Origin", "settings_in_force"]

#: The settings the project's sink card saves.
SINK_SETTINGS: Final[frozenset[str]] = frozenset({"sink_interior_width", "sink_interior_depth"})

Origin = Literal["rulebook", "gv_default", "sink_card", "project", "package"]


@dataclass(frozen=True, slots=True)
class InForce:
    """One setting's value now, as a run for this package would read it."""

    text: str
    origin: Origin
    set_by: str | None
    set_at: datetime | None
    source: str | None


def _text(row: StoredParameterValue) -> str:
    exact = row.exact_value
    return f'{format_inches(exact)}"' if row.unit == Unit.INCH.value else f"{exact} {row.unit}"


def _newest(session: Session, *conditions: object) -> UUID | None:
    return session.execute(
        select(StoredParameterSet.id)
        .where(*conditions)  # type: ignore[arg-type]
        .order_by(StoredParameterSet.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def settings_in_force(
    session: Session, project_id: UUID, revision_id: UUID, rules: Iterable[Rule]
) -> dict[str, InForce]:
    """Every setting the rules declare that has a value now, with where the value comes from.

    A setting no layer holds and the rulebook gives no default is absent: it is missing, never zero.
    """
    declared = {name: spec for rule in rules for name, spec in rule.parameters.items()}
    found: dict[str, InForce] = {}
    for name, spec in declared.items():
        if spec.default is not None:
            found[name] = InForce(
                text=(
                    f'{format_inches(spec.default.exact_value)}"'
                    if spec.default.unit == Unit.INCH
                    else f"{spec.default.exact_value} {spec.default.unit}"
                ),
                origin="rulebook",
                set_by=None,
                set_at=None,
                source=None,
            )
    layers: tuple[tuple[UUID | None, ParameterLayer], ...] = (
        (
            _newest(
                session,
                StoredParameterSet.layer == ParameterLayer.GLOBAL.value,
                StoredParameterSet.project_id.is_(None),
            ),
            ParameterLayer.GLOBAL,
        ),
        (
            _newest(
                session,
                StoredParameterSet.layer == ParameterLayer.PROJECT.value,
                StoredParameterSet.project_id == project_id,
            ),
            ParameterLayer.PROJECT,
        ),
        (
            _newest(
                session,
                StoredParameterSet.layer == ParameterLayer.RUN.value,
                StoredParameterSet.package_revision_id == revision_id,
            ),
            ParameterLayer.RUN,
        ),
    )
    ids = [set_id for set_id, _layer in layers if set_id is not None]
    rows: dict[UUID, dict[str, StoredParameterValue]] = {}
    for value in session.scalars(
        select(StoredParameterValue).where(
            StoredParameterValue.parameter_set_id.in_(ids),
            StoredParameterValue.name.in_(list(declared)),
        )
    ):
        rows.setdefault(value.parameter_set_id, {})[value.name] = value
    for set_id, layer in layers:
        if set_id is None:
            continue
        for name, row in rows.get(set_id, {}).items():
            origin: Origin
            if layer is ParameterLayer.GLOBAL:
                origin = "gv_default"
            elif layer is ParameterLayer.PROJECT:
                origin = "sink_card" if name in SINK_SETTINGS else "project"
            else:
                origin = "package"
            found[name] = InForce(
                text=_text(row),
                origin=origin,
                set_by=row.set_by,
                set_at=row.set_at,
                source=(
                    row.provenance
                    if row.source_reference is None
                    else f"{row.provenance}: {row.source_reference}"
                ),
            )
    return found
