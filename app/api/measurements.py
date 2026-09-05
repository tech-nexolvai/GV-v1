"""Reviewer-supplied values, and asking for the checks to be run.

`CLIENT_FACTS` Q7: *"the reviewer types the values into input fields for that drawing set"*. This is
those fields' endpoint. It exists because nothing in the pipeline produces a verdict operand yet — a
candidate has no semantic type, `evidence/gate.py:seal` needs a canonical observation, and nothing
mints one — so the reading half of the product is a person, and that is the sanctioned arrangement
rather than a stand-in: a reviewer's own reading is HUMAN_CONFIRMED, which the evidence gate accepts.

## Where the values go, and why not somewhere new

**Project settings land in the PROJECT layer; measurements land in the RUN layer.** Both are
`parameter_sets`, which is `rules/parameters.py`'s own answer: `user_input_set` exists for exactly
this and says why the layer matters — *"RUN rather than PROJECT because these are measured for a
single review: the room was that width on the day somebody stood in it. Recording them as project
settings would imply they apply to every later review."*

So no new table. A measurement is not a canonical observation and must not be filed as one: it has no
page, no polygon and no crop, and inventing those would put a reading on a drawing region nobody
looked at.

## Running the checks is not this module's job, and the boundary is enforced

`POST .../checks` **enqueues**. It does not run anything, and it cannot: `tests/api/test_no_heavy_work.py`
walks every import under `app/api/` and fails if the control plane can reach extraction or rendering
code. Since `workflow/stages.py` gained the OCR route it reaches `extraction.reader`, so importing
`DatabaseStages` here fails that walk with a trace — verified, not assumed:

    app.api.packages -> workflow.stages -> extraction.reader -> extraction.geometry.containment

That guard is right. `DESIGN_PLATFORM.md` §4.2 puts CPU-heavy work in a background task so an endpoint
that merely accepts a drawing cannot reach the code that reads it. So the row and the intent commit
together in one transaction, and something outside this process does the work —
`scripts/drain_outbox.py` today, a registered worker when Phase 6 lands.

Source: `CLIENT_FACTS` Q7 · Design: `docs/DESIGN_PLATFORM.md` §4.2 ·
Verification: `tests/api/test_measurements.py`
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.auth import Action, Principal, require_action, require_project_access
from app.models import Package, PackageRevision
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import to_rows
from app.schemas.measurements import (
    CheckRequest,
    DiscriminatorOut,
    ParameterOut,
    QuantityOut,
    RequiredInputsOut,
    ReviewerEntry,
    ReviewerEntryOut,
    StoredList,
    StoredValue,
)
from app.verdicts.rulebook import snapshot_store
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.required_inputs import required_inputs
from rules.schema import Quantity
from units.measurement import Measurement
from units.normalise import UnitNormalisationError, normalise_to_inches
from workflow.measurements import LIST_MARKER
from workflow.outbox import enqueue

router = APIRouter(tags=["measurements"])

#: What every refusal says, matching `app/api/packages.py`: nothing about the project or the package.
NOT_FOUND_DETAIL: Final = "Not found"

#: The workflow the enqueued row asks for. Named, not free text, so a typo cannot enqueue work that
#: no consumer recognises and that then sits in the outbox looking accepted.
RUN_CHECKS_WORKFLOW: Final = "run_checks"


def _parse(value: str, *, field: str) -> Measurement:
    """One typed token as an exact measurement in inches, or a 422 saying what was wrong.

    `normalise_to_inches` refuses a token with no unit, and that refusal is inherited on purpose: a
    bare `984` was once stored as 984 inches because tokenisation had removed its `mm` (#483). A
    reviewer who omits the mark is told, rather than having one chosen for them.
    """
    try:
        return normalise_to_inches(value)
    except UnitNormalisationError as refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f'{field}: {refused}. Give the value with its unit — 25 1/2", 610 mm — because a '
                "number with no unit cannot be converted and must not be guessed at."
            ),
        ) from refused


def _revision(session: Session, project_id: UUID, package_id: UUID) -> PackageRevision:
    """This package's current revision, or 404 in the same words for absent and forbidden."""
    revision = session.execute(
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return revision


def _store(
    session: Session,
    *,
    project_id: UUID,
    layer: ParameterLayer,
    values: dict[str, Measurement],
    typed: dict[str, str],
    actor: str,
) -> tuple[int | None, tuple[StoredValue, ...]]:
    """Persist one layer's values, reusing an identical set rather than minting a second.

    **A re-submission mints a new version, and that is by design rather than a shortcoming.**
    `ParameterSet.set_id` puts `set_at` *inside* the content hash deliberately —
    `rules/parameters.py` explains why: "two sets recording the same number measured on different
    days are genuinely different records, and collapsing them would lose the distinction a reviewer
    needs." So the same numbers typed twice are two records, not one, and a finding cites the version
    that judged it (ADR-0016). Rewriting a set in place would change what an already-recorded finding
    claims to have used.

    The `set_id` lookup below is therefore a narrow guard, not the normal path: it catches a repeat
    that lands within the same recorded instant, which a fast client retry can produce. Saying so
    plainly because a comment claiming it deduplicates ordinary re-submissions would be false — they
    are supposed to become new versions.
    """
    if not values:
        return None, ()

    now = datetime.now(UTC)
    next_version = (
        session.execute(
            select(func.coalesce(func.max(StoredParameterSet.version), 0)).where(
                StoredParameterSet.project_id == project_id,
                StoredParameterSet.layer == layer.value,
            )
        ).scalar_one()
        + 1
    )
    parameters = ParameterSet(
        project_id=str(project_id),
        layer=layer,
        version=next_version,
        parameters={
            name: ParameterValue(
                value=Quantity(value=measurement.exact, unit=measurement.unit),
                # MEASURED: a person measured or read it. `HUMAN_PROVENANCES` is a closed set with no
                # member a model could claim, which is what keeps a model's number out of here — not
                # a check in this module.
                provenance=Provenance.MEASURED,
                set_by=actor,
                set_at=now,
            )
            for name, measurement in values.items()
        },
    )

    existing = session.execute(
        select(StoredParameterSet).where(StoredParameterSet.set_id == parameters.set_id)
    ).scalar_one_or_none()
    if existing is not None:
        version = existing.version
    else:
        stored, rows = to_rows(parameters)
        session.add(stored)
        for row in rows:
            session.add(row)
        version = next_version

    return version, tuple(
        StoredValue(
            name=name,
            numerator=str(measurement.exact.numerator),
            denominator=str(measurement.exact.denominator),
            unit=measurement.unit.value,
            as_typed=typed[name],
        )
        for name, measurement in sorted(values.items())
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/required-inputs",
    response_model=RequiredInputsOut,
    summary="What the published rulebook needs before it can decide anything",
)
def read_required_inputs(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> RequiredInputsOut:
    """The fields a reviewer must fill, derived from the rules themselves.

    **This exists so a form cannot omit a field.** A hand-written list is right the day it is written
    and silently wrong the first time a rule gains an input — and the check then abstains for a reason
    the reviewer cannot act on, which looks exactly like a genuine missing dimension.

    Grouped by physical quantity rather than by rule input, because three rules read the sink's front
    offset and one of them calls the same measurement by a different name. A reviewer measures it
    once and is asked once; the `consumers` say which inputs the value feeds.

    Reads the published rulebook from the database, which is what `run_checks` reads. Nothing
    published means an empty form and `rules_published: 0` — a different situation from a rulebook
    that wants nothing, and the caller can tell them apart.
    """
    _revision(session, project_id, package_id)

    store = snapshot_store(session)
    rules = [
        snapshot.rule
        for snapshot in (store.latest(rule_id) for rule_id in store.rule_ids())
        if snapshot is not None
    ]
    needs = required_inputs(rules)

    return RequiredInputsOut(
        quantities=tuple(
            QuantityOut(
                key=quantity.key,
                semantic_type=quantity.semantic_type,
                source=quantity.source,
                many=quantity.many,
                consumers=tuple(
                    {"rule_id": consumer.rule_id, "input_name": consumer.input_name}
                    for consumer in quantity.consumers
                ),
            )
            for quantity in needs.quantities
        ),
        parameters=tuple(
            ParameterOut(
                name=parameter.name,
                scope=parameter.scope,
                rule_ids=parameter.rule_ids,
                declared_default=parameter.declared_default,
                blocked=parameter.blocked,
            )
            for parameter in needs.parameters
        ),
        discriminators=tuple(
            DiscriminatorOut(
                name=discriminator.name,
                rule_ids=discriminator.rule_ids,
                choices=discriminator.choices,
            )
            for discriminator in needs.discriminators
        ),
        rules_published=len(rules),
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/measurements",
    response_model=ReviewerEntryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Enter reviewer-supplied parameters and measurements",
)
def enter_measurements(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.MANAGE_PROJECT))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    body: ReviewerEntry,
) -> ReviewerEntryOut:
    """Store what the reviewer typed, exactly, and say back what the system understood.

    Two checks for two questions, as `create_package` does: `require_project_access` says the caller
    may see this project, `require_action` says entering values is something their role may do.

    Nothing is run here. A submission records values; asking for the checks is a separate call, so a
    reviewer can correct a typo without a verdict being computed from the first attempt.
    """
    _revision(session, project_id, package_id)

    # **Split by scope, because a layer is a claim about how long a value stays true.** A project
    # setting applies to every review of the job; a run value was true for this one. Filing the sink
    # from today's cut sheet as a project setting would make it look authoritative next time.
    project_values: dict[str, Measurement] = {}
    project_typed: dict[str, str] = {}
    run_values: dict[str, Measurement] = {}
    run_typed: dict[str, str] = {}
    for entry in body.parameters:
        parsed = _parse(entry.value, field=entry.name)
        if entry.scope == "run":
            run_values[entry.name] = parsed
            run_typed[entry.name] = entry.value
        else:
            project_values[entry.name] = parsed
            project_typed[entry.name] = entry.value

    # Keyed `rule_id:name`, because two rules may each declare an input called `width` and they are
    # not the same reading. A many-valued input becomes one row per measurement, `#0` upward, in the
    # order given — that order is the layout left to right, and `CAB-ARCH-VS-SHOP-001` compares two
    # runs position by position, so reordering here would compare the wrong pair of cabinets.
    measurement_keys: set[str] = set()
    for measurement in body.measurements:
        label = f"{measurement.rule_id}.{measurement.name}"
        if measurement.values is not None:
            for index, raw in enumerate(measurement.values):
                key = f"{measurement.rule_id}:{measurement.name}{LIST_MARKER}{index}"
                run_values[key] = _parse(raw, field=f"{label}[{index}]")
                run_typed[key] = raw
                measurement_keys.add(key)
        else:
            key = f"{measurement.rule_id}:{measurement.name}"
            if measurement.value is None:  # pragma: no cover - the schema refuses neither form
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{label}: neither a value nor values was given.",
                )
            run_values[key] = _parse(measurement.value, field=label)
            run_typed[key] = measurement.value
            measurement_keys.add(key)

    parameter_version, stored_project = _store(
        session,
        project_id=project_id,
        layer=ParameterLayer.PROJECT,
        values=project_values,
        typed=project_typed,
        actor=principal.id,
    )
    # **Run parameters and measurements share one stored set.** `rules/parameters.py` refuses two sets
    # in one layer, so they cannot be stored separately — and they belong together anyway, being the
    # two halves of what a reviewer supplied for this review. `workflow/measurements.py` tells them
    # apart again by the `rule_id:` prefix.
    measurement_version, stored_run = _store(
        session,
        project_id=project_id,
        layer=ParameterLayer.RUN,
        values=run_values,
        typed=run_typed,
        actor=principal.id,
    )
    stored_measurements = tuple(v for v in stored_run if v.name in measurement_keys)
    stored_parameters = (
        *stored_project,
        *(v for v in stored_run if v.name not in measurement_keys),
    )

    try:
        session.commit()
    except Exception:
        session.rollback()
        raise

    grouped: dict[str, list[StoredValue]] = {}
    for value in stored_measurements:
        if LIST_MARKER in value.name:
            grouped.setdefault(value.name.split(LIST_MARKER)[0], []).append(value)

    return ReviewerEntryOut(
        parameter_set_version=parameter_version,
        measurement_set_version=measurement_version,
        parameters=stored_parameters,
        measurements=tuple(v for v in stored_measurements if LIST_MARKER not in v.name),
        # Grouped back into runs, so a client shows four cabinets rather than `cabinet_widths#0`
        # through `#3`. Sorted by index rather than by insertion: the order is the layout.
        lists=tuple(
            StoredList(
                name=name,
                values=tuple(sorted(values, key=lambda v: int(v.name.split(LIST_MARKER)[1]))),
            )
            for name, values in sorted(grouped.items())
        ),
    )


def _check_discriminators(session: Session, stated: dict[str, str]) -> None:
    """Refuse a discriminator the rulebook does not declare, or a value it does not offer.

    **A misspelling would not fail — it would abstain**, and the two are not the same to a reviewer.
    The resolver matches the stated value against the declared variants and finds nothing, so the
    rule reports NO_APPLICABLE_RULE: "this check does not apply to this package". A reviewer reading
    that has no reason to suspect a typo, and the check they meant to run silently did not.

    So the vocabulary is closed here, where the mistake can still be corrected, and the message names
    what was offered.
    """
    if not stated:
        return
    store = snapshot_store(session)
    rules = [
        snapshot.rule
        for snapshot in (store.latest(rule_id) for rule_id in store.rule_ids())
        if snapshot is not None
    ]
    declared = {
        discriminator.name: discriminator.choices
        for discriminator in required_inputs(rules).discriminators
    }
    for name, value in stated.items():
        if name not in declared:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"no published rule uses a discriminator called {name!r}. "
                    f"The rulebook declares: {sorted(declared) or 'none'}."
                ),
            )
        if value not in declared[name]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{name}={value!r} is not one of the variants the rulebook declares "
                    f"({list(declared[name])}). An unrecognised value does not fail the check — it "
                    "makes the rule report that it does not apply, which reads as a deliberate "
                    "exclusion rather than a typo."
                ),
            )


@router.post(
    "/projects/{project_id}/packages/{package_id}/checks",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask for the checks to be run against this package",
)
def request_checks(
    _access: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.MANAGE_PROJECT))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    body: CheckRequest | None = None,
) -> dict[str, str]:
    """Record the intent to run the checks. Starts nothing, and cannot.

    **202, not 200**, and the body carries an `accepted` id rather than a run id. Nothing has started:
    `workflow/outbox.py:enqueue` says the id "names the enqueued work, not a workflow run", and
    calling it a run id here would have a client poll for something that does not exist yet.

    The reason this is not simply `run_checks(...)` is a guard, not a preference.
    `tests/api/test_no_heavy_work.py` walks every import under `app/api/` and refuses any path to
    extraction or rendering — and `workflow/stages.py` reaches `extraction.reader` since the OCR
    route landed. So this module may not import the thing that does the work, which is exactly the
    separation `DESIGN_PLATFORM.md` §4.2 asks for.
    """
    revision = _revision(session, project_id, package_id)
    stated = dict((body.discriminators if body else {}) or {})
    _check_discriminators(session, stated)
    accepted = enqueue(
        session,
        workflow=RUN_CHECKS_WORKFLOW,
        payload={"package_revision_id": str(revision.id), "discriminators": stated},
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise

    return {"accepted_id": str(accepted), "package_revision_id": str(revision.id)}
