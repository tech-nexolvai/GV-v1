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
from rules.parameters import ParameterLayer, ParameterSet
from units.measurement import Measurement, Unit
from verdict.operands import EvidenceStatus, VerdictOperand

__all__ = ["SOURCE", "operands_for", "run_parameters_for"]

#: The operand source recorded against a reviewer's reading.
#:
#: `USER_INPUT` rather than `SHOP`: the reviewer read it off the shop drawing, but the *system* did
#: not, and a finding that claimed `SHOP` would say extraction had produced it. `verdict/operands.py`
#: names this source for exactly this case.
SOURCE = "USER_INPUT"


#: How a many-valued measurement's position is written into its stored name.
#:
#: `CT-WIDTH-001:cabinet_widths#2` is the third cabinet. A separator the rulebook cannot produce, so
#: an ordinary input name can never be mistaken for an indexed one — rule ids and input names are
#: identifiers and carry no `#`.
LIST_MARKER = "#"


def _latest_run_set(
    session: Session, package_revision_id: UUID
) -> tuple[StoredParameterSet | None, ParameterSet | None]:
    """The newest RUN set for this package's project, and its values.

    **The newest, because a reviewer who corrects a typo submits again.** Running against an earlier
    set would judge the package on a number the reviewer had already replaced, and it would look
    entirely normal on the findings list. Earlier versions stay: a finding cites the version that
    judged it (ADR-0016), and superseding a value is not deleting it.
    """
    project_id = session.execute(
        select(Package.project_id)
        .join(PackageRevision, PackageRevision.package_id == Package.id)
        .where(PackageRevision.id == package_revision_id)
    ).scalar_one_or_none()
    if project_id is None:
        return None, None

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
        return None, None

    values = list(
        session.execute(
            select(StoredParameterValue).where(StoredParameterValue.parameter_set_id == stored.id)
        ).scalars()
    )
    return stored, from_rows(stored, values)


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
    stored, parameters = _latest_run_set(session, package_revision_id)
    if stored is None or parameters is None:
        return {}

    # **A many-valued input arrives as several rows and leaves as one tuple.**
    #
    # `parameter_values` holds one number per name, so a run of four cabinets is stored as
    # `CT-WIDTH-001:cabinet_widths#0` through `#3` — one row per measurement, each carrying its own
    # provenance, which is what a reviewer actually did. They are regrouped here because
    # `VerdictOperand` takes a tuple of `Measurement` for a many-valued operand and the engine checks
    # arity: a single value handed to `sum_within_tolerance` would compare a total against one
    # cabinet.
    #
    # Sorted by index rather than by insertion, because the order is the layout left to right and
    # `CAB-ARCH-VS-SHOP-001` compares two runs position by position.
    scalars: dict[tuple[str, str], tuple[Measurement, str]] = {}
    lists: dict[tuple[str, str], dict[int, tuple[Measurement, str]]] = {}
    for key, value in parameters.parameters.items():
        rule_id, _, remainder = key.partition(":")
        if not remainder:
            # Not a measurement. A RUN-layer entry with no `rule_id:` prefix is a run-scope parameter
            # — `sink_interior_width` and the like — and belongs to `run_parameters_for`, not here.
            continue
        name, marker, index_text = remainder.partition(LIST_MARKER)
        measurement = Measurement(value.value.value, Unit(value.value.unit), None)
        # Names the person and the set, so a finding's operand can be traced to who supplied it and
        # which version they supplied.
        reference = f"reviewer:{value.set_by}:{stored.set_id}"
        if marker:
            try:
                index = int(index_text)
            except ValueError:
                # A malformed index is dropped rather than guessed into position 0, where it would
                # silently reorder a cabinet run. Nothing this module writes produces one.
                continue
            lists.setdefault((rule_id, name), {})[index] = (measurement, reference)
        else:
            scalars[(rule_id, name)] = (measurement, reference)

    operands: dict[str, dict[str, VerdictOperand]] = {}
    for (rule_id, name), (measurement, reference) in scalars.items():
        operands.setdefault(rule_id, {})[name] = VerdictOperand(
            name=name,
            value=measurement,
            status=EvidenceStatus.HUMAN_CONFIRMED,
            source=SOURCE,
            evidence_ref=reference,
        )
    for (rule_id, name), indexed in lists.items():
        ordered = [indexed[position] for position in sorted(indexed)]
        operands.setdefault(rule_id, {})[name] = VerdictOperand(
            name=name,
            value=tuple(measurement for measurement, _ in ordered),
            status=EvidenceStatus.HUMAN_CONFIRMED,
            source=SOURCE,
            evidence_ref=ordered[0][1],
        )
    return operands


def run_parameters_for(session: Session, package_revision_id: UUID) -> ParameterSet | None:
    """The run-scope parameters a reviewer supplied, as a layer the resolver can take.

    **This exists because nothing else loads the RUN layer.** `app/models/parameters.py:
    load_parameter_sets` covers GLOBAL and PROJECT and says so — "RUN sets are supplied per review and
    are not loaded here" — so `sink_interior_depth` and `sink_interior_width` reached no resolver and
    both sink-cutout checks abstained however carefully the reviewer typed the cut sheet.

    Measurement keys are excluded. They live in the same stored set because both are per-review, but
    they are operands rather than parameters, and handing `CT-WIDTH-001:cabinet_widths#0` to the
    resolver as a parameter name would put a measurement in the effective parameter set a reviewer
    reads.
    """
    stored, parameters = _latest_run_set(session, package_revision_id)
    if stored is None or parameters is None:
        return None
    plain = {name: value for name, value in parameters.parameters.items() if ":" not in name}
    if not plain:
        return None
    return ParameterSet(
        project_id=parameters.project_id,
        layer=ParameterLayer.RUN,
        version=parameters.version,
        parameters=plain,
    )
