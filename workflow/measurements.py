"""Turning stored reviewer measurements back into verdict operands.

`app/api/measurements.py` stores what a reviewer typed as a RUN-layer parameter set, because that is
`rules/parameters.py`'s own home for it: `user_input_set` exists for the value behind a `USER_INPUT`
operand, and RUN rather than PROJECT because a measurement belongs to one review.

This is the other end. The engine looks an operand up by the input name a rule declares, so something
has to read those stored values and build the map — and it lives here rather than in `app/api/`
because the control plane may not import the code that runs a check.

**The status is HUMAN_CONFIRMED and that is the whole justification.** `evidence/gate.py` accepts two
statuses for a verdict operand, and a person's own reading is one of them. Nothing here promotes,
guesses, or averages: a value a reviewer typed is qualified because a reviewer typed it, and the
`set_by` recorded against it says who.

**It refuses to invent.** An input no reviewer supplied is simply absent from the map, and the engine
then abstains with NOT_FOUND — which is the correct outcome and the one the whole system is built to
produce rather than a default.

Verification: `tests/workflow/test_measurements.py`
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Package, PackageRevision
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import ParameterValue as StoredParameterValue
from app.models.parameters import from_rows
from rules.parameters import ParameterLayer
from units.measurement import Measurement, Unit
from verdict.operands import EvidenceStatus, VerdictOperand

__all__ = ["SOURCE", "operands_for"]

#: The operand source recorded against a reviewer's reading.
#:
#: `USER_INPUT` rather than `SHOP`: the reviewer read it off the shop drawing, but the *system* did
#: not, and a finding that claimed `SHOP` would say extraction had produced it. `verdict/operands.py`
#: names this source for exactly this case.
SOURCE = "USER_INPUT"


def operands_for(
    session: Session, package_revision_id: UUID
) -> dict[str, dict[str, VerdictOperand]]:
    """Every reviewer measurement for this package's project, keyed by rule then input name.

    The stored key is `rule_id:name`, because two rules may each declare an input called `width` and
    they are not the same reading. Split here rather than stored pre-split: one column that holds a
    compound key is easier to reason about than two that must agree.

    **The latest RUN set wins.** A reviewer who corrects a typo submits again, which mints a new
    version; running the checks against the earlier one would judge the package on a number the
    reviewer had already replaced. Earlier versions stay, because a finding cites the version that
    judged it (ADR-0016) and superseding a value is not deleting it.
    """
    project_id = session.execute(
        select(Package.project_id)
        .join(PackageRevision, PackageRevision.package_id == Package.id)
        .where(PackageRevision.id == package_revision_id)
    ).scalar_one_or_none()
    if project_id is None:
        return {}

    stored = session.execute(
        select(StoredParameterSet)
        .where(
            StoredParameterSet.project_id == project_id,
            StoredParameterSet.layer == ParameterLayer.RUN.value,
        )
        .order_by(StoredParameterSet.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if stored is None:
        return {}

    values = list(
        session.execute(
            select(StoredParameterValue).where(StoredParameterValue.parameter_set_id == stored.id)
        ).scalars()
    )
    parameters = from_rows(stored, values)

    operands: dict[str, dict[str, VerdictOperand]] = {}
    for key, value in parameters.parameters.items():
        rule_id, _, name = key.partition(":")
        if not name:
            # Not a measurement this module wrote. A RUN-layer parameter with no `rule_id:` prefix
            # belongs to something else — skipped rather than guessed into a rule.
            continue
        operands.setdefault(rule_id, {})[name] = VerdictOperand(
            name=name,
            value=Measurement(value.value.value, Unit(value.value.unit), None),
            status=EvidenceStatus.HUMAN_CONFIRMED,
            source=SOURCE,
            # Names the person and the set, so a finding's operand can be traced to who supplied it
            # and which version they supplied.
            evidence_ref=f"reviewer:{value.set_by}:{stored.set_id}",
        )
    return operands
