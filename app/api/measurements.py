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

## A setting typed from the passage the app found, without seeing its number (#866)

Where the architect's drawing states a setting, `GET .../required-inputs` points at the passage: a
page and a crop, never the number (`SettingPointerOut`). The reviewer types what they see and sends
the pointer back as the entry's `citation`. The server holds the typed number to the passage's
(`workflow.parameter_citations.confirm_typed_value`, which re-runs #849's guard first) and refuses
the whole request on any difference, so nothing is stored. A match is stored as `G.C / Client` with
a reference naming the page and the document, and the passage is kept beside the stored value in
`parameter_value_citations`. An entry with no citation is stored exactly as before.

Source: `CLIENT_FACTS` Q7 · Design: `docs/DESIGN_PLATFORM.md` §4.2 ·
Verification: `tests/api/test_measurements.py`, `tests/api/test_setting_citations.py`
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.confirmations import _candidate_crop_artifact, _verified_crop_content
from app.api.dependencies import get_artifact_store, get_session
from app.auth import Action, Principal, require_action, require_project_access
from app.models import Package, PackageRevision, PackageState
from app.models.document import (
    Document,
    DocumentVersion,
    PackageRevisionDocument,
    Page,
)
from app.models.evidence import (
    CanonicalObservation,
    EvidenceArtifact,
    EvidenceSupportingCandidate,
    ObservationCandidate,
)
from app.models.package_text import TextPhrase
from app.models.parameter_proposals import ParameterProposal
from app.models.parameter_value_citations import ParameterValueCitation, ParameterValueCitationRun
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import ParameterValue as StoredParameterValue
from app.models.parameters import from_rows, to_rows
from app.schemas.measurements import (
    AssignmentEvent,
    AssignmentStepOut,
    CheckRequest,
    ConfirmedReadingOut,
    DiscriminatorOut,
    LayoutProposalOut,
    PageProposalIn,
    ParameterEntry,
    ParameterOut,
    ProposedFieldOut,
    ProposedMeasurementsOut,
    ProposedReadingOut,
    QuantityOut,
    RequiredInputsOut,
    ReviewerEntry,
    ReviewerEntryOut,
    SettingPointerOut,
    SourceOut,
    StoredList,
    StoredValue,
)
from app.verdicts.rulebook import snapshot_store
from rules.parameter_sources import SOURCE_GUIDANCE, allowed_sources
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.required_inputs import allowed_categories_for, required_inputs
from rules.schema import Quantity, Rule
from storage.store import ArtifactStore
from units.imperial import format_inches
from units.measurement import Measurement
from units.normalise import UnitNormalisationError, normalise_to_inches
from verdict.operands import QUALIFIED_STATUSES, EvidenceStatus
from workflow.assignment_bedrock import (
    AssignmentProgress,
    configured_assignment_model,
    propose_and_guard,
)
from workflow.classifications import record_classifications
from workflow.layout_proposals import (
    confirmed_discriminators,
    record_layout_confirmation,
    stored_layout_proposals,
)
from workflow.measurements import LIST_MARKER
from workflow.outbox import enqueue
from workflow.parameter_citations import (
    CitationRefusal,
    CitationRefusalReason,
    check_proposal,
    confirm_typed_value,
    live_parameter_proposals,
)
from workflow.propose import (
    MAX_ASSIGNMENT_READINGS,
    assignment_context,
    assignment_fields,
    record_proposal,
    stored_proposal,
)

router = APIRouter(tags=["measurements"])

#: What every refusal says, matching `app/api/packages.py`: nothing about the project or the package.
NOT_FOUND_DETAIL: Final = "Not found"

#: The workflow the enqueued row asks for. Named, not free text, so a typo cannot enqueue work that
#: no consumer recognises and that then sits in the outbox looking accepted.
RUN_CHECKS_WORKFLOW: Final = "run_checks"

#: States where the Measure page should keep watching because drawing readings or filed proposals may
#: still appear. Written out rather than derived from "not terminal": review/check states, failures
#: and human handoffs are not reading states, even though some are non-terminal.
READING_STATES: Final[frozenset[PackageState]] = frozenset(
    {
        PackageState.UPLOADED,
        PackageState.INGESTING,
        PackageState.EXTRACTING,
        PackageState.MATCHING,
        PackageState.VALIDATING_EVIDENCE,
    }
)


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


def _source(name: str, given: Provenance | None) -> Provenance:
    """The source a setting is recorded with, or a 422 saying what the setting takes (#827).

    **Refused rather than defaulted.** A setting the table does not know has no honest source, so
    recording it as `Measured` — what every setting got before #827 — would be a guess dressed as a
    fact. A setting that allows one source needs no answer; one that allows several needs the
    reviewer's, because which of them is true is a fact about this job the system cannot know.
    """
    allowed = allowed_sources(name)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{name!r} is not a setting any published check uses, so it has no source to "
                "record. Check the name against the form."
            ),
        )
    choices = " or ".join(f"{source.value!r}" for source in allowed)
    if given is None:
        if len(allowed) == 1:
            return allowed[0]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Say where {name!r} came from: {choices}.",
        )
    if given not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{name!r} cannot come from {given.value!r}; it takes {choices}. "
                f"{' '.join(SOURCE_GUIDANCE[source] for source in allowed)}"
            ),
        )
    return given


@dataclass(frozen=True, slots=True)
class _CitedPassage:
    """The passage a stored value was typed from and matched, as `parameter_value_citations`
    records it (#866)."""

    proposal_id: UUID
    document_version_id: UUID
    document_sha256: str
    page_id: UUID
    candidate_ids: tuple[UUID, ...]


def _passage_page(session: Session, phrase_id: UUID) -> tuple[Page, DocumentVersion, Document]:
    """The page a passage is on, the document version it belongs to, and that version's upload.

    A phrase holds runs from one page only, so its page is the passage's.
    """
    phrase = session.get(TextPhrase, phrase_id)
    page = None if phrase is None else session.get(Page, phrase.page_id)
    version = None if page is None else session.get(DocumentVersion, page.document_version_id)
    document = None if version is None else session.get(Document, version.document_id)
    if page is None or version is None or document is None:  # pragma: no cover - foreign keys
        raise LookupError(f"passage {phrase_id} has no page, version or document")
    return page, version, document


def _cited(
    session: Session, revision: PackageRevision, entry: ParameterEntry, typed: Measurement
) -> tuple[tuple[Provenance, str], _CitedPassage]:
    """A setting typed from the passage the form showed, held to it — or a 422 saying why (#866).

    **Enforced here, not trusted to the form.** A hand-crafted request can cite any pointer id, so
    `confirm_typed_value` checks no newer pointer has replaced it, puts it through #849's guard
    again (the architect's confirmed drawing, no markup, one inch number), and compares the typed
    number with the passage's exactly. Any refusal stops the whole request before anything
    is stored. No sentence here states the passage's number.

    The source is the one the passage gives and the reference is written from the passage itself, so
    a request that sends either differently is refused rather than half-believed.
    """
    assert entry.citation is not None
    if entry.reference is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{entry.name}: a value typed from the passage the form found takes its reference "
                "from that passage. Leave the reference out, or enter the value without the "
                "citation and say where it came from."
            ),
        )
    checked = confirm_typed_value(
        session,
        package_revision_id=revision.id,
        proposal_id=entry.citation,
        setting=entry.name,
        typed=typed,
    )
    if isinstance(checked, CitationRefusal):
        if checked.reason is CitationRefusalReason.MISMATCH:
            proposal = session.get(ParameterProposal, entry.citation)
            assert proposal is not None  # a mismatch is reached only after the pointer was found
            page, _, _ = _passage_page(session, proposal.phrase_id)
            detail = (
                f"{entry.name}: the number typed is not the one in the architect's drawing on page "
                f"{page.index + 1}, so nothing was saved. Look at the passage again and type the "
                "number exactly as it is written."
            )
        else:
            detail = (
                f"{entry.name} cannot be saved from that passage: {checked.detail}. "
                "Nothing was saved."
            )
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
    if entry.source is not None and entry.source is not checked.claimed_source:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{entry.name}: a value typed from the architect's drawing comes from "
                f"{checked.claimed_source.value!r}, not {entry.source.value!r}."
            ),
        )
    page, version, document = _passage_page(session, checked.phrase_id)
    reference = (
        f"Architect's drawing, page {page.index + 1} of the {document.kind} document "
        f"{version.sha256[:12]}: typed without being shown the number, and it matched"
    )
    return (_source(entry.name, checked.claimed_source), reference), _CitedPassage(
        proposal_id=entry.citation,
        document_version_id=version.id,
        document_sha256=version.sha256,
        page_id=page.id,
        candidate_ids=checked.candidate_ids,
    )


def _record_citations(
    session: Session,
    rows: list[StoredParameterValue],
    *,
    fresh: Mapping[str, _CitedPassage],
    carried_from: Mapping[str, UUID],
) -> None:
    """Store each new value's passage beside it, and copy an earlier value's with it when carried.

    `carried_from` names the earlier row each carried value was copied from (#799). Its citation is
    copied too, so the version the checks read still says where the number came from.
    """
    earlier: dict[UUID, _CitedPassage] = {}
    if carried_from:
        for citation in session.scalars(
            select(ParameterValueCitation).where(
                ParameterValueCitation.parameter_value_id.in_(list(carried_from.values()))
            )
        ):
            earlier[citation.parameter_value_id] = _CitedPassage(
                proposal_id=citation.parameter_proposal_id,
                document_version_id=citation.document_version_id,
                document_sha256=citation.document_sha256,
                page_id=citation.page_id,
                candidate_ids=tuple(
                    session.scalars(
                        select(ParameterValueCitationRun.candidate_id)
                        .where(ParameterValueCitationRun.citation_id == citation.id)
                        .order_by(ParameterValueCitationRun.position)
                    )
                ),
            )
    pending: list[tuple[ParameterValueCitation, tuple[UUID, ...]]] = []
    for row in rows:
        if row.name in fresh:
            passage = fresh[row.name]
        elif row.name in carried_from and carried_from[row.name] in earlier:
            passage = earlier[carried_from[row.name]]
        else:
            continue
        citation = ParameterValueCitation(
            parameter_value_id=row.id,
            parameter_proposal_id=passage.proposal_id,
            document_version_id=passage.document_version_id,
            document_sha256=passage.document_sha256,
            page_id=passage.page_id,
        )
        pending.append((citation, passage.candidate_ids))
    if not pending:
        return
    # No relationship ties these tables to `parameter_values`, so the unit of work does not order
    # their inserts by foreign key: each row is flushed before the rows that point at it.
    session.flush()
    session.add_all(citation for citation, _ in pending)
    session.flush()
    session.add_all(
        ParameterValueCitationRun(citation_id=citation.id, candidate_id=candidate_id, position=at)
        for citation, candidate_ids in pending
        for at, candidate_id in enumerate(candidate_ids)
    )


def _passage_crop(
    session: Session, revision: PackageRevision, proposal: ParameterProposal
) -> EvidenceArtifact | None:
    """The stored crop of the first of the number's runs that has one, or `None`.

    The number's runs only, as `parameter_proposals` stores the span "so a reviewer is shown the
    number's own runs, not the whole line"; each crop shows a margin of the page around its run.
    """
    citation = check_proposal(session, proposal)
    if isinstance(citation, CitationRefusal):
        return None
    for candidate_id in citation.candidate_ids:
        artifact = _candidate_crop_artifact(session, revision, candidate_id)
        if artifact is not None:
            return artifact
    return None


def _setting_pointers(session: Session, revision: PackageRevision) -> dict[str, SettingPointerOut]:
    """Each setting whose newest pointer passes the guard now, as a page and a crop: never the
    number (#866)."""
    pointers: dict[str, SettingPointerOut] = {}
    for setting, proposal in live_parameter_proposals(session, revision.id).items():
        page, _, document = _passage_page(session, proposal.phrase_id)
        pointers[setting] = SettingPointerOut(
            proposal_id=proposal.id,
            page_index=page.index,
            document_kind=document.kind,
            has_crop=_passage_crop(session, revision, proposal) is not None,
        )
    return pointers


def _store(
    session: Session,
    *,
    project_id: UUID | None,
    layer: ParameterLayer,
    values: dict[str, Measurement],
    typed: dict[str, str],
    actor: str,
    carry_forward: bool,
    package_revision_id: UUID | None = None,
    provenance: Provenance = Provenance.MEASURED,
    sources: Mapping[str, tuple[Provenance, str | None]] | None = None,
    citations: Mapping[str, _CitedPassage] | None = None,
) -> tuple[int | None, tuple[StoredValue, ...]]:
    """Persist one layer's values, reusing an identical set rather than minting a second.

    **With `carry_forward`, the new version keeps every earlier value this request does not set**
    (#799). The checks read only the latest version of a layer, and the form sends only the fields
    it has filled, so a version holding only what was sent erased every other setting: a reviewer
    who corrected the side thickness on Tuesday lost Monday's depth and overhang. A carried value is
    the earlier `ParameterValue` itself — its who, when and provenance unchanged — so the record
    still says who set each number. A RUN set belongs to one package revision (#801), so carrying it
    forward stays within that package: `package_revision_id` names it, and is required for RUN.

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

    **`citations` are the passages values were typed from and matched (#866)**, stored beside them.
    A carried value takes its earlier citation with it.
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
    carried: dict[str, ParameterValue] = {}
    carried_from: dict[str, UUID] = {}
    if carry_forward:
        previous = session.execute(
            select(StoredParameterSet)
            .where(
                StoredParameterSet.project_id == project_id,
                StoredParameterSet.layer == layer.value,
                # Within one review for RUN (#801); `IS NULL` for the other layers, which name none.
                (
                    StoredParameterSet.package_revision_id.is_(None)
                    if package_revision_id is None
                    else StoredParameterSet.package_revision_id == package_revision_id
                ),
            )
            .order_by(StoredParameterSet.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if previous is not None:
            rows = list(
                session.execute(
                    select(StoredParameterValue).where(
                        StoredParameterValue.parameter_set_id == previous.id
                    )
                ).scalars()
            )
            carried = {
                name: value
                for name, value in from_rows(previous, rows).parameters.items()
                if name not in values
            }
            carried_from = {row.name: row.id for row in rows if row.name in carried}

    chosen = dict(sources or {})
    cited = dict(citations or {})
    parameters = ParameterSet(
        # `None` for the company layer (#812), which belongs to no project.
        project_id=None if project_id is None else str(project_id),
        layer=layer,
        version=next_version,
        parameters={
            **carried,
            **{
                name: ParameterValue(
                    value=Quantity(value=measurement.exact, unit=measurement.unit),
                    # The source the reviewer named for a setting (#827), checked against what the
                    # setting allows before it reached here; otherwise `provenance` — MEASURED for a
                    # dimension a person read, COMPANY_STANDARD for the company layer (#812). Every
                    # member of `Provenance` is a person's or the rulebook's; none is one a model could
                    # claim, which is what keeps a model's number out of here — not a check in this
                    # module.
                    provenance=chosen.get(name, (provenance, None))[0],
                    set_by=actor,
                    set_at=now,
                    reference=chosen.get(name, (provenance, None))[1],
                )
                for name, measurement in values.items()
            },
        },
    )

    existing = session.execute(
        select(StoredParameterSet).where(StoredParameterSet.set_id == parameters.set_id)
    ).scalar_one_or_none()
    if existing is not None:
        version = existing.version
    else:
        stored, rows = to_rows(parameters, package_revision_id=package_revision_id)
        session.add(stored)
        for row in rows:
            session.add(row)
        _record_citations(session, rows, fresh=cited, carried_from=carried_from)
        version = next_version

    return version, tuple(
        StoredValue(
            name=name,
            numerator=str(measurement.exact.numerator),
            denominator=str(measurement.exact.denominator),
            unit=measurement.unit.value,
            as_typed=typed[name],
            source=chosen[name][0].value if name in chosen else None,
            reference=chosen[name][1] if name in chosen else None,
            citation=cited[name].proposal_id if name in cited else None,
        )
        for name, measurement in sorted(values.items())
    )


def _stored_proposal_out(
    session: Session, revision: PackageRevision, *, page_number: int | None = None
) -> tuple[ProposedFieldOut, ...]:
    """The filed proposal for this revision, rendered for the form.

    **Read, never computed.** The proposal was made when the drawings were read; asking the model
    again here would put a network call and a cost on a page load, and would let two loads of the
    same unchanged package disagree with each other.

    The value comes from the candidate's own exact numerator and denominator rather than from
    anything stored beside the proposal. `measurement_proposals` records which reading fills which
    slot and nothing else, so there is no second copy of a number here to drift from the first.
    """
    rows = stored_proposal(session, revision.id, page_number=page_number)
    if not rows:
        return ()

    candidates = {
        candidate.id: (candidate, page_index)
        for candidate, page_index in session.execute(
            select(ObservationCandidate, Page.index)
            .join(Page, Page.id == ObservationCandidate.page_id)
            .where(ObservationCandidate.id.in_([row.candidate_id for row in rows]))
        ).all()
    }

    fields, _rules = assignment_fields(session)
    by_key = {field.key: field for field in fields}

    grouped: dict[str, list[ProposedReadingOut]] = {}
    verified: dict[str, bool] = {}
    for row in rows:
        found = candidates.get(row.candidate_id)
        if found is None or row.field_key not in by_key:
            # A proposal naming a reading this revision no longer has, or a field the published
            # rulebook has stopped asking for. Both mean the rulebook or the read moved on since the
            # proposal was filed, and a stale row must not put a value in front of a reviewer.
            continue
        candidate, page_index = found
        if candidate.value_numerator is None or candidate.value_denominator is None:
            continue
        verified[row.field_key] = row.placement_verified
        grouped.setdefault(row.field_key, []).append(
            ProposedReadingOut(
                candidate_id=candidate.id,
                value=(
                    f"{format_inches(Fraction(candidate.value_numerator, candidate.value_denominator))} "
                    f"{candidate.unit}"
                ),
                page_index=page_index,
            )
        )

    return tuple(
        ProposedFieldOut(
            field_key=key,
            name=by_key[key].name,
            source=by_key[key].source,
            many=by_key[key].many,
            placement_verified=verified[key],
            values=tuple(values),
        )
        for key, values in sorted(grouped.items())
    )


def _stored_layout_proposal_out(
    session: Session, revision: PackageRevision
) -> dict[str, LayoutProposalOut]:
    """Current layout proposals, keyed by discriminator name for the required-inputs response."""
    confirmed = confirmed_discriminators(session, revision.id)
    return {
        proposal.discriminator_name: LayoutProposalOut(
            value=proposal.proposed_value,
            crop_artifact_id=proposal.crop_artifact_id,
            model_id=proposal.model_id,
            prompt_id=proposal.prompt_id,
            confirmed=confirmed.get(proposal.discriminator_name) == proposal.proposed_value,
        )
        for proposal in stored_layout_proposals(session, revision.id)
    }


def _published_rules(session: Session) -> list[Rule]:
    """The rulebook as published, which is what `run_checks` reads.

    Shared by the form and by the submission that answers it, so a category the form offered cannot
    be one the submission refuses.
    """
    store = snapshot_store(session)
    return [
        snapshot.rule
        for snapshot in (store.latest(rule_id) for rule_id in store.rule_ids())
        if snapshot is not None
    ]


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
    page_number: Annotated[int | None, Query(ge=1)] = None,
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

    rules = _published_rules(session)
    needs = required_inputs(rules)
    layout_proposals = _stored_layout_proposal_out(session, revision)
    found = _setting_pointers(session, revision)

    # The Confirm screen is the human gate.  Once a reviewer has confirmed both what the drawing
    # says and what it means, asking them to type that exact value again is pure transcription risk.
    # Scope through the supporting candidate and revision membership: a canonical observation from a
    # different package (or an older revision containing different documents) must never prefill this
    # review.  Page/candidate ordering makes a MANY field deterministic without assigning meaning.
    confirmed_query = (
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
    )
    if page_number is not None:
        confirmed_query = confirmed_query.where(Page.index == page_number - 1)
    confirmed_rows = session.execute(confirmed_query).all()

    seen_observations: set[UUID] = set()
    confirmed_readings_list: list[ConfirmedReadingOut] = []
    # One canonical observation can have a primary candidate plus corroborating candidates.  It is
    # still one qualified reading, especially for a many-valued rule input.
    for observation, page_index, _created_at in confirmed_rows:
        if observation.id in seen_observations:
            continue
        seen_observations.add(observation.id)
        confirmed_readings_list.append(
            ConfirmedReadingOut(
                key=f"{observation.document_role}:{observation.semantic_type}",
                source=observation.document_role,
                semantic_type=observation.semantic_type,
                page_index=page_index,
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
        )
    confirmed_readings = tuple(confirmed_readings_list)

    page_numbers = tuple(
        page_index + 1
        for page_index in session.scalars(
            select(Page.index)
            .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.document_version_id == DocumentVersion.id,
            )
            .where(PackageRevisionDocument.package_revision_id == revision.id)
            .distinct()
            .order_by(Page.index)
        )
    )

    return RequiredInputsOut(
        page_numbers=page_numbers,
        proposed_readings=_stored_proposal_out(session, revision, page_number=page_number),
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
                categories=quantity.categories,
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
                sources=tuple(
                    SourceOut(value=source.value, guidance=SOURCE_GUIDANCE[source])
                    for source in allowed_sources(parameter.name)
                ),
                # A blocked setting is one nobody may supply, so it is offered no passage either.
                found=None if parameter.blocked else found.get(parameter.name),
            )
            for parameter in needs.parameters
        ),
        discriminators=tuple(
            DiscriminatorOut(
                name=discriminator.name,
                rule_ids=discriminator.rule_ids,
                choices=discriminator.choices,
                proposal=layout_proposals.get(discriminator.name),
            )
            for discriminator in needs.discriminators
        ),
        rules_published=len(rules),
        revision_state=revision.state,
        still_reading=PackageState(revision.state) in READING_STATES,
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/parameter-proposals/{proposal_id}/crop",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
            "description": "The integrity-checked crop of the passage a setting was found in.",
        }
    },
    summary="View the passage in the architect's drawing that states a setting",
)
def setting_passage_crop(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
    proposal_id: UUID,
) -> Response:
    """The picture a reviewer reads a setting off before typing it, while the form offers it.

    **The pixels, never the number** (#866). The reviewer must read the value themselves, so this
    returns a stored crop of the number's runs and nothing parsed from them. Only a pointer
    `GET .../required-inputs` would offer is served, so a pointer withdrawn since, because its
    drawing turned out to be the vendor's, shows nothing. The digest is checked before bytes leave,
    as for every other crop.
    """
    revision = _revision(session, project_id, package_id)
    proposal = next(
        (
            row
            for row in live_parameter_proposals(session, revision.id).values()
            if row.id == proposal_id
        ),
        None,
    )
    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no passage stating a setting is pointed to under that id in this package",
        )
    artifact = _passage_crop(session, revision, proposal)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no crop is stored for this passage; read it on the page instead",
        )
    content = _verified_crop_content(store, artifact)
    return Response(
        content=content, media_type=artifact.media_type, headers={"Cache-Control": "no-store"}
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/measurements",
    response_model=ReviewerEntryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Enter reviewer-supplied parameters and measurements",
)
def enter_measurements(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.ENTER_VALUES))],
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

    A setting sent with a `citation` is held to the passage it names before anything is stored, and
    a number that differs from the passage's stores nothing at all (#866).
    """
    revision = _revision(session, project_id, package_id)

    # **Split by scope, because a layer is a claim about how long a value stays true.** A project
    # setting applies to every review of the job; a run value was true for this one. Filing the sink
    # from today's cut sheet as a project setting would make it look authoritative next time.
    project_values: dict[str, Measurement] = {}
    project_typed: dict[str, str] = {}
    run_values: dict[str, Measurement] = {}
    run_typed: dict[str, str] = {}
    project_sources: dict[str, tuple[Provenance, str | None]] = {}
    run_sources: dict[str, tuple[Provenance, str | None]] = {}
    project_citations: dict[str, _CitedPassage] = {}
    run_citations: dict[str, _CitedPassage] = {}
    for entry in body.parameters:
        parsed = _parse(entry.value, field=entry.name)
        # A value typed from a passage is held to it before anything is stored, and a mismatch
        # refuses the whole request (#866). An entry with no citation is recorded as it always was.
        source: tuple[Provenance, str | None]
        citation: _CitedPassage | None = None
        if entry.citation is None:
            source = (_source(entry.name, entry.source), entry.reference)
        else:
            source, citation = _cited(session, revision, entry, parsed)
        if entry.scope == "run":
            run_values[entry.name] = parsed
            run_typed[entry.name] = entry.value
            run_sources[entry.name] = source
        else:
            project_values[entry.name] = parsed
            project_typed[entry.name] = entry.value
            project_sources[entry.name] = source
        # A setting sent twice keeps its last entry, as it always has, and only that entry's passage.
        citations = run_citations if entry.scope == "run" else project_citations
        citations.pop(entry.name, None)
        if citation is not None:
            citations[entry.name] = citation

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

    # **A classification is checked against the rulebook, not against the client's list.** The form
    # offers the choices a rule's semantic type allows; a submission naming something else is a
    # client out of step with the rulebook, and accepting it would store a category the operation
    # will later refuse — turning a correctable 422 into an abstention on a package the reviewer
    # thought they had finished.
    published = _published_rules(session) if body.classifications else []
    for classification in body.classifications:
        allowed = allowed_categories_for(published, classification.rule_id, classification.name)
        if allowed is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{classification.rule_id}.{classification.name} is not an input that takes a "
                    "category. Send a dimension as a measurement."
                ),
            )
        for position, category in enumerate(classification.categories):
            if category not in allowed:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"{classification.rule_id}.{classification.name}[{position}]: "
                        f"{category!r} is not one of {', '.join(sorted(allowed))}."
                    ),
                )
        record_classifications(
            session,
            package_revision_id=revision.id,
            rule_id=classification.rule_id,
            input_name=classification.name,
            categories=classification.categories,
            confirmed_by=principal.id,
        )

    parameter_version, stored_project = _store(
        session,
        project_id=project_id,
        layer=ParameterLayer.PROJECT,
        values=project_values,
        typed=project_typed,
        actor=principal.id,
        carry_forward=True,
        sources=project_sources,
        citations=project_citations,
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
        # Safe since #801: the set is this package revision's alone, so a carried value is one this
        # package's reviewer typed, never another package's.
        carry_forward=True,
        package_revision_id=revision.id,
        sources=run_sources,
        citations=run_citations,
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
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.ENTER_VALUES))],
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
    for name, value in stated.items():
        record_layout_confirmation(
            session,
            package_revision_id=revision.id,
            discriminator_name=name,
            value=value,
            actor=principal.id,
        )
    confirmed = confirmed_discriminators(session, revision.id)
    _check_discriminators(session, confirmed)
    accepted = enqueue(
        session,
        workflow=RUN_CHECKS_WORKFLOW,
        payload={"package_revision_id": str(revision.id), "discriminators": confirmed},
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
    _action: Annotated[Principal, Depends(require_action(Action.ENTER_VALUES))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    body: PageProposalIn,
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

    page_number = body.page_number
    page_exists = session.execute(
        select(Page.id)
        .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == DocumentVersion.id,
        )
        .where(
            PackageRevisionDocument.package_revision_id == revision.id,
            Page.index == page_number - 1,
        )
        .limit(1)
    ).scalar_one_or_none()
    if page_exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"page {page_number} is not part of this package revision",
        )

    context, rules_published = assignment_context(session, revision, page_number=page_number)
    fields, readings = context.fields, context.readings

    if len(readings) > MAX_ASSIGNMENT_READINGS:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"page {page_number} has {len(readings)} eligible unconfirmed readings, more than "
                f"the {MAX_ASSIGNMENT_READINGS} this step will propose over. Nothing is proposed "
                "rather than a partial page; review or confirm some readings first."
            ),
        )

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

        from workflow.timing import timing_recorder_from_environment

        timings = timing_recorder_from_environment()
        if timings is None:
            proposed, unverified = propose_and_guard(context, model, observer=observe)
        else:
            with timings.measure(
                "review.fill_with_ai_proposal",
                run_id=str(revision.id),
                page_index=page_number - 1,
            ):
                proposed, unverified = propose_and_guard(context, model, observer=observe)
        for event in pending:
            yield send(event)

        # **Filed, exactly as the pipeline files its own.** The button used to produce an answer
        # that lived in one browser tab: reload the page and the form was empty again, and nothing
        # recorded that a model had ever proposed anything. One writer, one reader, so "what is the
        # current proposal" has a single answer whichever path produced it.
        if proposed and model is not None:
            record_proposal(
                session,
                package_revision_id=revision.id,
                page_number=page_number,
                assignments=proposed,
                model_id=model.config.model_id,
                unverified_placement=unverified,
            )
            session.commit()

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
                    page_number=page_number,
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
