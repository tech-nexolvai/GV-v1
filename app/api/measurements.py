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

from collections.abc import Iterator
from datetime import UTC, datetime
from fractions import Fraction
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.confirmations import document_source
from app.api.dependencies import get_session
from app.auth import Action, Principal, require_action, require_project_access
from app.models import Package, PackageRevision
from app.models.document import (
    Document,
    DocumentVersion,
    PackageRevisionDocument,
    Page,
)
from app.models.evidence import (
    CanonicalObservation,
    EvidenceSupportingCandidate,
    ObservationAssociation,
    ObservationCandidate,
    line_key,
)
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import to_rows
from app.schemas.measurements import (
    AssignmentEvent,
    AssignmentStepOut,
    CheckRequest,
    ConfirmedReadingOut,
    DiscriminatorOut,
    ParameterOut,
    ProposedFieldOut,
    ProposedMeasurementsOut,
    ProposedReadingOut,
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
from units.imperial import format_inches
from units.measurement import Measurement
from units.normalise import UnitNormalisationError, normalise_to_inches
from verdict.operands import QUALIFIED_STATUSES, EvidenceStatus
from vocabulary.semantic_types import CLIENT_CODES
from workflow.assignment import AssignmentContext, Field, Reading
from workflow.assignment_bedrock import (
    AssignmentProgress,
    configured_assignment_model,
    propose_and_guard,
)
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
    revision = _revision(session, project_id, package_id)

    store = snapshot_store(session)
    rules = [
        snapshot.rule
        for snapshot in (store.latest(rule_id) for rule_id in store.rule_ids())
        if snapshot is not None
    ]
    needs = required_inputs(rules)

    # The Confirm screen is the human gate.  Once a reviewer has confirmed both what the drawing
    # says and what it means, asking them to type that exact value again is pure transcription risk.
    # Scope through the supporting candidate and revision membership: a canonical observation from a
    # different package (or an older revision containing different documents) must never prefill this
    # review.  Page/candidate ordering makes a MANY field deterministic without assigning meaning.
    confirmed_rows = session.execute(
        select(CanonicalObservation, Page.index, ObservationCandidate.created_at)
        .join(
            EvidenceSupportingCandidate,
            EvidenceSupportingCandidate.canonical_observation_id == CanonicalObservation.id,
        )
        .join(
            ObservationCandidate,
            ObservationCandidate.id == EvidenceSupportingCandidate.candidate_id,
        )
        .join(Page, Page.id == CanonicalObservation.page_id)
        .join(DocumentVersion, DocumentVersion.id == CanonicalObservation.document_version_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == DocumentVersion.id,
        )
        .where(
            PackageRevisionDocument.package_revision_id == revision.id,
            CanonicalObservation.status.in_([item.value for item in QUALIFIED_STATUSES]),
        )
        .order_by(Page.index, ObservationCandidate.created_at, CanonicalObservation.id)
    ).all()

    # One canonical observation can have a primary candidate plus corroborating candidates.  It is
    # still one qualified reading, especially for a many-valued rule input.
    seen_observations: set[UUID] = set()
    confirmed_readings = tuple(
        ConfirmedReadingOut(
            key=f"{observation.document_role}:{observation.semantic_type}",
            source=observation.document_role,
            semantic_type=observation.semantic_type,
            value=(
                f"{format_inches(Fraction(observation.value_numerator, observation.value_denominator))} "
                f"{observation.unit}"
            ),
            qualification=(
                "exact_vector_tag"
                if observation.status == EvidenceStatus.CORROBORATED.value
                else "reviewer_confirmed"
            ),
        )
        for observation, _page_index, _created_at in confirmed_rows
        if not (observation.id in seen_observations or seen_observations.add(observation.id))
    )

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
        confirmed_readings=confirmed_readings,
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


# ---------------------------------------------------------------------------
# Proposing which reading fills which field
# ---------------------------------------------------------------------------

#: The phases of one assignment, in the order they run. Fixed, because the percentage a reviewer
#: sees is *phases finished* and a denominator that changed under it would make the bar meaningless.
#: A retry re-sends the phase it went back to rather than adding one, so the bar holds where the
#: work actually is instead of advancing on an answer that was rejected.
ASSIGNMENT_PHASES: Final[tuple[tuple[str, str], ...]] = (
    ("rulebook", "Reading what the rulebook asks for"),
    ("readings", "Gathering what was read off the drawings"),
    ("proposing", "Asking which reading fills which field"),
    ("checking", "Checking that answer against the drawing"),
    ("filling", "Filling the form"),
)

#: Above this, nothing is proposed. A page with hundreds of readings is one where the reader has
#: picked up the title block and the scale bar, and asking a model to sort that out would spend a
#: large prompt to produce a proposal the guard is likely to refuse whole.
MAX_ASSIGNMENT_READINGS: Final = 200


class EventStreamResponse(StreamingResponse):
    """A `text/event-stream` body.

    A subclass rather than `media_type=` on the call, because the route's *declared* response class
    is what FastAPI documents: without it the generated schema lands under `application/json` and
    the OpenAPI document — which is where `frontend/main/src/api/schema.d.ts` comes from — describes
    a content type this route never returns.
    """

    media_type = "text/event-stream"


def _phase_event(name: str, detail: str, *, attempt: int = 1) -> AssignmentEvent:
    """One phase frame, with the percentage of phases *finished* before it began."""
    index = next(i for i, (phase, _) in enumerate(ASSIGNMENT_PHASES, start=1) if phase == name)
    label = ASSIGNMENT_PHASES[index - 1][1]
    return AssignmentEvent(
        event="step",
        step=AssignmentStepOut(
            index=index,
            total=len(ASSIGNMENT_PHASES),
            name=name,
            label=label,
            detail=detail,
            percent=round((index - 1) / len(ASSIGNMENT_PHASES) * 100),
            attempt=attempt,
            # Once, on the first frame. Repeating it on every frame would send the same five strings
            # five times to say something that cannot change during a run.
            sequence=tuple(prose for _, prose in ASSIGNMENT_PHASES) if index == 1 else (),
        ),
    )


def _assignment_description(semantic_type: str) -> str | None:
    """What this quantity *is*, in the client's own words, or `None` where they have not said.

    **The client's code book, deliberately, and not the rule's own description.** `Rule.description`
    is free text an author writes, and on these rules it says things like "the countertop width
    equals the sum of the cabinets and the fillers" — which is the arithmetic, and the one thing
    `workflow/assignment.py` exists to withhold: a model holding the equation could choose readings
    that make it balance, and the check would then confirm the balance on every drawing including
    one with a real error in it.

    `CLIENT_CODES` says where a quantity *is* on the drawing — "cabinet 2 width, sink cabinet
    underneath" — which is positional, carries no formula, and is exactly what makes the choice
    decidable. Anything the client has not defined is left out rather than substituted.
    """
    code = CLIENT_CODES.get(semantic_type)
    return None if code is None else code.description


def _assignment_fields(session: Session) -> tuple[tuple[Field, ...], int]:
    """The rulebook's fields as an assignment context takes them, and how many rules published them.

    The name is the rulebook's own readable one — `cabinet_widths`, not `CT004` — taken from the
    first rule that consumes the quantity, the same choice the form's labels make. Where two rules
    name one quantity differently the code is what they agree on, and it stays in `key`.
    """
    store = snapshot_store(session)
    rules = [
        snapshot.rule
        for snapshot in (store.latest(rule_id) for rule_id in store.rule_ids())
        if snapshot is not None
    ]
    needs = required_inputs(rules)
    fields = tuple(
        Field(
            key=quantity.key,
            name=next(
                (consumer.input_name for consumer in quantity.consumers if consumer.input_name),
                quantity.semantic_type,
            ),
            source=quantity.source,
            many=quantity.many,
            description=_assignment_description(quantity.semantic_type),
        )
        for quantity in needs.quantities
    )
    return fields, len(rules)


def _assignment_readings(session: Session, revision: PackageRevision) -> tuple[Reading, ...]:
    """Every reading of this package that could fill a field, with what the geometry established.

    Unconfirmed only, for the reason `list_candidates` gives: a confirmed reading already has a
    meaning and already fills its field, and offering it again would invite a second answer to a
    question a person has settled.

    **Unattached readings are included, and marked.** `guard_assignment` refuses one — an unattached
    number is a title block or a scale bar, and `text_association` declining to say what it
    annotates is a result — but `assignment_bedrock` deliberately tells the model which readings are
    unusable rather than letting it propose one and lose the whole batch to the refusal.

    The latest association wins where a candidate has more than one. `open_extraction_run` keys a run
    on its configuration, so a second row is a re-association under different thresholds rather than
    a competing opinion, and the later thresholds are the deployment's current ones.
    """
    rows = session.execute(
        select(ObservationCandidate, Page.index, Document.kind, ObservationAssociation)
        .join(Page, Page.id == ObservationCandidate.page_id)
        .join(DocumentVersion, DocumentVersion.id == ObservationCandidate.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == DocumentVersion.id,
        )
        .outerjoin(
            ObservationAssociation,
            ObservationAssociation.candidate_id == ObservationCandidate.id,
        )
        .where(
            PackageRevisionDocument.package_revision_id == revision.id,
            ObservationCandidate.value_numerator.is_not(None),
            ObservationCandidate.id.not_in(select(EvidenceSupportingCandidate.candidate_id)),
        )
        .order_by(
            Page.index,
            ObservationCandidate.created_at,
            ObservationCandidate.id,
            ObservationAssociation.created_at,
        )
    ).all()

    readings: dict[UUID, Reading] = {}
    for candidate, page_index, document_kind, association in rows:
        source = document_source(document_kind)
        if source is None or candidate.value_denominator is None:
            # Neither sheet the rules read from, or a value the parser could not make exact. It can
            # fill no field, and putting it in the context would spend prompt on a refusal.
            continue
        attached = association is not None and association.refusal_reason is None
        readings[candidate.id] = Reading(
            candidate_id=str(candidate.id),
            value=(
                f"{format_inches(Fraction(candidate.value_numerator, candidate.value_denominator))} "
                f"{candidate.unit}"
            ),
            source=source,
            page=page_index + 1,
            line_key=(
                line_key(
                    association.start_x,
                    association.start_y,
                    association.end_x,
                    association.end_y,
                )
                if attached
                else None
            ),
            chain_key=association.chain_key if attached else None,
            order=association.chain_position if attached else None,
        )
    return tuple(readings.values())


@router.post(
    "/projects/{project_id}/packages/{package_id}/measurements/propose",
    summary="Ask a model which reading fills which field, and check every answer",
    response_class=EventStreamResponse,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "A stream of `AssignmentEvent` frames: one per phase as it begins, then the "
                "result. Each frame is one `data:` line."
            ),
            # The model is declared so the generated client types are the server's own — the reason
            # `frontend/main/src/api/client.ts` regenerates from `/openapi.json` rather than hand-
            # writing an interface. The body is a stream of these, not one of them.
            "model": AssignmentEvent,
        }
    },
)
def propose_measurements(
    request: Request,
    _access: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.MANAGE_PROJECT))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> EventStreamResponse:
    """Propose which reading fills which field, and stream the phases while it happens.

    **Nothing is stored and nothing is decided.** The accepted proposal fills a form the reviewer
    then reads, edits and saves; saving is what records a value and records it as theirs. Every
    failure — no model configured, a provider that will not answer, a proposal the deterministic
    guard refuses — ends with the same thing on screen: empty fields and a person filling them,
    which is what happens today. This step can make that faster; it cannot make it worse.

    **A stream rather than one response**, because the model call is the slow part and a screen that
    names the phase it is waiting on is telling the truth about what is happening. The percentage is
    phases finished out of five, which is a number this endpoint knows; how long the model will take
    and how right its answer is are two it does not, and neither is on the bar.

    The database work is done before the stream opens, so the session is not held across it.
    """
    revision = _revision(session, project_id, package_id)

    fields, rules_published = _assignment_fields(session)
    readings = _assignment_readings(session, revision)

    if len(readings) > MAX_ASSIGNMENT_READINGS:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"this package has {len(readings)} unconfirmed readings, more than the "
                f"{MAX_ASSIGNMENT_READINGS} this step will propose over. Confirm or discard some "
                "first: a proposal across that many is one the checks are likely to refuse whole."
            ),
        )

    context = AssignmentContext(fields=fields, readings=readings)
    model = configured_assignment_model(request.app.state.settings)
    attached = sum(1 for reading in readings if reading.line_key is not None)
    by_id = {reading.candidate_id: reading for reading in readings}
    by_key = {field.key: field for field in fields}

    def frames() -> Iterator[str]:
        def send(event: AssignmentEvent) -> str:
            return f"data: {event.model_dump_json()}\n\n"

        yield send(
            _phase_event(
                "rulebook",
                f"{len(fields)} field{'' if len(fields) == 1 else 's'} across "
                f"{rules_published} published rule{'' if rules_published == 1 else 's'}",
            )
        )
        yield send(
            _phase_event(
                "readings",
                f"{len(readings)} read, {attached} attached to a dimension line",
            )
        )

        # The observer turns the phases inside `propose_and_guard` into frames. It is the only way
        # the retry is visible from out here: the return value of a refused-then-corrected call and
        # a first-time success are the same tuple.
        pending: list[AssignmentEvent] = []
        outcome: dict[str, str] = {}

        def observe(progress: AssignmentProgress) -> None:
            if progress.phase == "asking":
                pending.append(_phase_event("proposing", progress.detail, attempt=progress.attempt))
            elif progress.phase == "checking":
                pending.append(_phase_event("checking", progress.detail, attempt=progress.attempt))
            elif progress.phase in {"refused", "unavailable"}:
                outcome["unfilled_reason"] = progress.detail

        proposed = propose_and_guard(context, model, observer=observe)
        for event in pending:
            yield send(event)

        assignments = tuple(
            ProposedFieldOut(
                field_key=proposal.field_key,
                name=by_key[proposal.field_key].name,
                source=by_key[proposal.field_key].source,
                many=by_key[proposal.field_key].many,
                values=tuple(
                    ProposedReadingOut(
                        candidate_id=UUID(candidate_id),
                        value=by_id[candidate_id].value,
                        page_index=by_id[candidate_id].page - 1,
                        chain_key=by_id[candidate_id].chain_key,
                        chain_position=by_id[candidate_id].order,
                    )
                    for candidate_id in proposal.candidate_ids
                ),
            )
            for proposal in proposed
        )
        yield send(
            _phase_event(
                "filling",
                f"{len(assignments)} of {len(fields)} field"
                f"{'' if len(fields) == 1 else 's'} filled",
            )
        )
        yield send(
            AssignmentEvent(
                event="result",
                result=ProposedMeasurementsOut(
                    assignments=assignments,
                    fields_total=len(fields),
                    fields_filled=len(assignments),
                    readings_considered=len(readings),
                    readings_attached=attached,
                    model_id=None if model is None else model.config.model_id,
                    unfilled_reason=outcome.get("unfilled_reason") if not assignments else None,
                ),
            )
        )

    return EventStreamResponse(
        frames(),
        # Nothing between here and the browser may hold a frame back: a phase that arrives with the
        # result it was meant to precede is a progress display that only ever shows 100%.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
