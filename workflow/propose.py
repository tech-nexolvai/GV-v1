"""Which reading fills which field, for one package revision — asked once and kept.

**The proposal used to be something a reviewer had to ask for.** #591 put it behind a button, which
meant the form was empty every time it was opened, the model was paid for again on every reload, and
the answer to "was this filled by AI?" lasted exactly as long as the browser tab. This module is the
same step moved to where the facts arrive: the worker runs it when the drawings have been read, and
a reviewer opening the form finds it filled, marked and editable.

Two callers, deliberately one implementation:

* `scripts/drain_outbox.py` runs it after extraction, which is what "filled on upload" means.
* `POST .../measurements/propose` runs it again on demand, for a reviewer who wants another answer
  after confirming a reading or correcting one.

Both persist through `record_proposal`, so the button and the pipeline cannot disagree about what
the current proposal is.

**Nothing here decides anything.** `workflow/assignment.py` is the checking half and says why the
checking is the point; this assembles what it checks and files what survived. A proposal is not a
measurement: no value is stored, only which candidate fills which slot, and the reviewer saving the
form is still what records a number — as `Provenance.MEASURED`, with their name on it.

Source: issue #596 · Verification: `tests/workflow/test_propose.py`
"""

from __future__ import annotations

from fractions import Fraction
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import (
    Document,
    DocumentKind,
    DocumentVersion,
    PackageRevisionDocument,
    Page,
)
from app.models.evidence import (
    EvidenceSupportingCandidate,
    MeasurementProposal,
    ObservationAssociation,
    ObservationCandidate,
    line_key,
)
from app.models.package import PackageRevision
from app.verdicts.rulebook import snapshot_store
from rules.required_inputs import required_inputs
from units.imperial import format_inches
from vocabulary.semantic_types import CLIENT_CODES
from workflow.assignment import AssignmentContext, Field, ProposedAssignment, Reading
from workflow.assignment_bedrock import (
    PROMPT_ID,
    AssignmentProgress,
    BedrockAssignmentModel,
    propose_and_guard,
)

__all__ = [
    "MAX_ASSIGNMENT_READINGS",
    "assignment_context",
    "assignment_fields",
    "assignment_readings",
    "propose_for_revision",
    "quantity_description",
    "record_proposal",
    "stored_proposal",
]

#: Above this, nothing is proposed. A page with hundreds of readings is one where the reader has
#: picked up the title block and the scale bar, and asking a model to sort that out would spend a
#: large prompt to produce a proposal the guard is likely to refuse whole.
MAX_ASSIGNMENT_READINGS = 200


def document_source(document_kind: str | None) -> str | None:
    """Normalise a document kind to the document-role vocabulary rule inputs use.

    Only the two drawings a rule can read from map to anything. Anything else is `None`, and a
    reading off it can fill no field — which is a fact about the rulebook, not a shortcoming.
    """
    if document_kind is None:
        return None
    kind = str(document_kind)
    if kind == DocumentKind.ARCHITECTURAL.value:
        return "ARCH"
    if kind == DocumentKind.SHOP.value:
        return "SHOP"
    return None


def quantity_description(semantic_type: str) -> str | None:
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


def assignment_fields(session: Session) -> tuple[tuple[Field, ...], int]:
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
            description=quantity_description(quantity.semantic_type),
        )
        for quantity in needs.quantities
    )
    return fields, len(rules)


def assignment_readings(session: Session, revision: PackageRevision) -> tuple[Reading, ...]:
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


def assignment_context(
    session: Session, revision: PackageRevision
) -> tuple[AssignmentContext, int]:
    """Everything a model is given for this revision, and how many rules published the fields."""
    fields, rules_published = assignment_fields(session)
    readings = assignment_readings(session, revision)
    return AssignmentContext(fields=fields, readings=readings), rules_published


def record_proposal(
    session: Session,
    *,
    package_revision_id: UUID,
    assignments: tuple[ProposedAssignment, ...],
    model_id: str,
) -> UUID | None:
    """File an accepted proposal, or file nothing. Returns the proposal id, or `None`.

    **Only accepted proposals are rows.** `guard_assignment` refuses a batch whole, and a refusal
    leaves the fields empty for the reviewer — which is exactly what an absent row already says. A
    refusal row would be a second way of saying nothing, and the reason belongs in the caller's log
    where somebody diagnosing it will look.

    Appends rather than replaces, because the table is append-only: a second proposal is a second
    set of rows beside the first, and `stored_proposal` reads the newest. The older set is the
    record of what it replaced.
    """
    if not assignments:
        return None
    proposal_id = uuid4()
    for assignment in assignments:
        for position, candidate_id in enumerate(assignment.candidate_ids):
            session.add(
                MeasurementProposal(
                    package_revision_id=package_revision_id,
                    proposal_id=proposal_id,
                    field_key=assignment.field_key,
                    position=position,
                    candidate_id=UUID(candidate_id),
                    model_id=model_id,
                    prompt_id=PROMPT_ID,
                )
            )
    session.flush()
    return proposal_id


def stored_proposal(session: Session, package_revision_id: UUID) -> tuple[MeasurementProposal, ...]:
    """The newest proposal's rows for this revision, in field and position order.

    Newest by `created_at`, then by `proposal_id` so two written in the same recorded instant still
    order deterministically — a form whose fields reorder between loads is one a reviewer loses
    their place in.
    """
    rows = list(
        session.execute(
            select(MeasurementProposal)
            .where(MeasurementProposal.package_revision_id == package_revision_id)
            .order_by(
                MeasurementProposal.created_at.desc(),
                MeasurementProposal.proposal_id.desc(),
            )
        ).scalars()
    )
    if not rows:
        return ()
    newest = rows[0].proposal_id
    return tuple(
        sorted(
            (row for row in rows if row.proposal_id == newest),
            key=lambda row: (row.field_key, row.position),
        )
    )


def propose_for_revision(
    session: Session,
    package_revision_id: UUID,
    model: BedrockAssignmentModel | None,
) -> dict[str, object]:
    """Ask, check, and file — the whole step, for a caller that wants no stream.

    What `scripts/drain_outbox.py` runs once the drawings have been read. Returns a summary for the
    worker's log; the outcome that matters is on disk or is deliberately absent.

    **A failure here must not fail the extraction.** The drawings were read, the readings are
    recorded, and a reviewer can fill the form by hand exactly as they do today — which is what
    happens when no model is configured at all. So every failure is caught and reported, never
    raised: an unreachable provider is not a reason to lose a read drawing.
    """
    revision = session.get(PackageRevision, package_revision_id)
    if revision is None:
        return {"ran": False, "reason": "no such package revision"}
    if model is None:
        return {"ran": False, "reason": "no model is configured"}

    try:
        context, _rules = assignment_context(session, revision)
    except Exception as error:  # noqa: BLE001 - reported, never fatal to the read
        return {"ran": False, "reason": f"the context could not be assembled: {error}"}

    if len(context.readings) > MAX_ASSIGNMENT_READINGS:
        return {
            "ran": False,
            "reason": f"{len(context.readings)} readings is more than this step proposes over",
        }

    last: list[AssignmentProgress] = []
    accepted = propose_and_guard(context, model, observer=last.append)
    proposal_id = record_proposal(
        session,
        package_revision_id=package_revision_id,
        assignments=accepted,
        model_id=model.config.model_id,
    )
    refused = next(
        (
            progress.detail
            for progress in reversed(last)
            if progress.phase in {"refused", "unavailable"}
        ),
        None,
    )
    return {
        "ran": True,
        "readings": len(context.readings),
        "attached": sum(1 for reading in context.readings if reading.line_key is not None),
        "fields": len(context.fields),
        "filled": len(accepted),
        "proposal_id": None if proposal_id is None else str(proposal_id),
        **({"reason": refused} if refused and not accepted else {}),
    }
