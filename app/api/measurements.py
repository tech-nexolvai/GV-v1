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
from app.schemas.measurements import ReviewerEntry, ReviewerEntryOut, StoredValue
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.schema import Quantity
from units.measurement import Measurement
from units.normalise import UnitNormalisationError, normalise_to_inches
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

    parameters = {entry.name: _parse(entry.value, field=entry.name) for entry in body.parameters}
    parameter_typed = {entry.name: entry.value for entry in body.parameters}
    # Keyed `rule_id:name`, because two rules may each declare an input called `width` and they are
    # not the same reading. The drain splits this back apart when it builds the operand map.
    measurements = {
        f"{entry.rule_id}:{entry.name}": _parse(entry.value, field=f"{entry.rule_id}.{entry.name}")
        for entry in body.measurements
    }
    measurement_typed = {
        f"{entry.rule_id}:{entry.name}": entry.value for entry in body.measurements
    }

    parameter_version, stored_parameters = _store(
        session,
        project_id=project_id,
        layer=ParameterLayer.PROJECT,
        values=parameters,
        typed=parameter_typed,
        actor=principal.id,
    )
    measurement_version, stored_measurements = _store(
        session,
        project_id=project_id,
        layer=ParameterLayer.RUN,
        values=measurements,
        typed=measurement_typed,
        actor=principal.id,
    )

    try:
        session.commit()
    except Exception:
        session.rollback()
        raise

    return ReviewerEntryOut(
        parameter_set_version=parameter_version,
        measurement_set_version=measurement_version,
        parameters=stored_parameters,
        measurements=stored_measurements,
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
    accepted = enqueue(
        session,
        workflow=RUN_CHECKS_WORKFLOW,
        payload={"package_revision_id": str(revision.id)},
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise

    return {"accepted_id": str(accepted), "package_revision_id": str(revision.id)}
