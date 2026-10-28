"""Structured, read-only countertop results for the visual reviewer."""

from __future__ import annotations

import hashlib
from typing import Annotated, Final, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.signed_off import open_revision
from app.audit.events import AuditCategory, emit
from app.auth import Action, Principal, require_action, require_project_access
from app.models import (
    NoCountertopAnswer,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    Page,
)
from app.models.evidence import NO_COUNTERTOP_NOTE_MAX_LENGTH
from app.review.countertop_results import (  # noqa: F401  (moved there; names kept for this module's own users)
    _ADMISSIBLE_EVIDENCE,
    _ARCHITECT_INPUT,
    NO_ROW_CHOSEN,
    NOT_CHECKED_YET,
    _architect_block,
    _architect_positions,
    _batched_pairings,
    _blank_page_out,
    _confirmation_out,
    _countertop_ref,
    _countertop_results_for_revision,
    _decision_out,
    _drawn_length_note,
    _exact,
    _intermediate,
    _pair_outcomes,
    _recorded_row_width,
    _row_values,
    _rows_not_checked,
    _scale_inputs,
    _source_and_kind,
    _split_page_item,
    _stored_crops,
    _StoredCrops,
    _supported_candidate,
    _trace_value,
    _wall,
)
from app.review.no_countertop import (
    PageAnswer,
    answered_pages,
    pages_words,
    reading_answer,
)
from app.schemas.visual_ui import (
    CheckPlanOut,
    CountertopItemRefOut,
    CountertopResultsOut,
    ItemPlanOut,
    PlannedFindingOut,
    PlannedRuleOut,
)

# The batch form of `architect_views` (#1167), private to its module: the countertop results ask
# it once per revision instead of once per row.
from workflow.item_plan import PlannedFinding, check_plan_for
from workflow.part_operands import reviewer_owned_pages
from workflow.slot_row_scope import (
    UnchosenRowPage,
    slot_rows_and_unchosen_pages,
    unchosen_page,
)

router = APIRouter(tags=["visual reviewer"])


@router.get(
    "/projects/{project_id}/packages/{package_id}/countertop-results",
    response_model=CountertopResultsOut,
    summary="Read structured countertop results for the live check run",
)
def countertop_results(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.READ_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    revision_number: Annotated[
        int | None,
        Query(
            alias="revision",
            ge=1,
            description=(
                "An earlier revision's number, to read its results as they were (#1180). "
                "Default: the newest revision."
            ),
        ),
    ] = None,
) -> CountertopResultsOut:
    del principal
    # The package is loaded with its revision, so its product (#1205) costs no statement of its own.
    chosen = select(PackageRevision, Package).join(
        Package, Package.id == PackageRevision.package_id
    )
    chosen = chosen.where(Package.id == package_id, Package.project_id == project_id)
    if revision_number is not None:
        chosen = chosen.where(PackageRevision.revision_number == revision_number)
    found = session.execute(
        chosen.order_by(PackageRevision.revision_number.desc()).limit(1)
    ).first()
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _countertop_results_for_revision(session, package_id, found[0])


@router.get(
    "/projects/{project_id}/packages/{package_id}/countertop-items/plan",
    response_model=CheckPlanOut,
    summary="Read each confirmed countertop's check plan: its rules, its results, what they wait on",
)
def countertop_item_plan(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.READ_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    revision_number: Annotated[
        int | None,
        Query(
            alias="revision",
            ge=1,
            description="An earlier revision's number. Default: the newest revision.",
        ),
    ] = None,
) -> CheckPlanOut:
    """For each live countertop item (one whose current decision confirms it), the published rules
    for the package's product and where each stands, the results its row or the item itself has,
    and what each rule's whole-package result is still waiting on (#1245, `workflow/item_plan.py`).

    **Read-only, and it decides nothing**: no check runs, no result changes. A countertop with no
    countertop row linked to it says so in `note`.
    """
    del principal
    chosen = (
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
    )
    if revision_number is not None:
        chosen = chosen.where(PackageRevision.revision_number == revision_number)
    revision = session.scalars(
        chosen.order_by(PackageRevision.revision_number.desc()).limit(1)
    ).first()
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    plan = check_plan_for(session, revision.id)

    def found(entry: PlannedFinding) -> PlannedFindingOut:
        return PlannedFindingOut(
            finding_id=entry.finding_id,
            rule_id=entry.rule_id,
            outcome=entry.outcome,
            scope="item" if entry.scope == "item" else "row",
        )

    return CheckPlanOut(
        package_id=package_id,
        revision_id=revision.id,
        items=tuple(
            ItemPlanOut(
                countertop=CountertopItemRefOut(
                    item_id=item.countertop.item_id,
                    page_number=item.countertop.page_number,
                    number=item.countertop.number,
                    label=item.countertop.label,
                ),
                row_id=item.countertop.row_candidate_id,
                rules=tuple(
                    PlannedRuleOut(
                        rule_id=rule.rule_id,
                        resolved=rule.resolved,
                        waits_for=rule.waits_for,
                        version=rule.version,
                        variant=rule.variant,
                        findings=tuple(found(entry) for entry in rule.findings),
                        missing=rule.missing,
                    )
                    for rule in item.rules
                ),
                findings=tuple(found(entry) for entry in item.findings),
                note=item.note,
            )
            for item in plan.items
        ),
    )


def _answer_lock_key(revision_id: UUID) -> int:
    """A stable signed 64-bit key for PostgreSQL's advisory lock on one revision's page answers."""
    digest = hashlib.sha256(f"no-countertop-answer:{revision_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _note(text: str | None) -> str | None:
    """A note as stored: trimmed, and none at all when only spaces were typed."""
    trimmed = (text or "").strip()
    return trimmed or None


class NoCountertopAnswerIn(BaseModel):
    """One reviewer click on a page listed with no countertop (#1191). Nothing is pre-selected."""

    model_config = ConfigDict(extra="forbid")

    answer: Literal["no_countertop", "countertop"] = Field(
        description=(
            '"no_countertop": "Confirm: no countertop here". "countertop": "There is a '
            'countertop": the page then needs the reviewer like a split page, and its result comes '
            "with the next check run."
        )
    )
    note: str | None = Field(
        default=None,
        max_length=NO_COUNTERTOP_NOTE_MAX_LENGTH,
        description="An optional note kept with the answer (#1195).",
    )


class NoCountertopPageIn(BaseModel):
    """One page the reviewer confirms in "Confirm these N pages" (#1195): its number and the
    reading record the screen showed for it, so a page read again since is refused, not guessed."""

    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(ge=1)
    expected_record_id: UUID = Field(
        description="The page's `record_id` as the screen showed it (`pages_without_countertop`)."
    )


class NoCountertopPagesIn(BaseModel):
    """ "Confirm these N pages" (#1195): "Confirm: no countertop here" on each page named, exactly
    as if each were clicked on its own, with one optional note for all of them. Pages left out
    (unticked) are not answered and keep waiting."""

    model_config = ConfigDict(extra="forbid")

    pages: list[NoCountertopPageIn] = Field(min_length=1, max_length=500)
    note: str | None = Field(
        default=None,
        max_length=NO_COUNTERTOP_NOTE_MAX_LENGTH,
        description="An optional note kept with each page's answer.",
    )

    @model_validator(mode="after")
    def _each_page_once(self) -> NoCountertopPagesIn:
        records = [page.expected_record_id for page in self.pages]
        if len(set(records)) != len(records):
            raise ValueError("each page may be named only once")
        return self


#: Said when the screen answers a page that is no longer listed (read again, or already answered).
PAGE_NOT_LISTED: Final = (
    "This page is no longer listed with no countertop: it was read again or already answered. "
    "Reload the results."
)


def _pages_not_on_list(numbers: list[int]) -> str:
    verb = "is" if len(numbers) == 1 else "are"
    words = pages_words(numbers)
    return (
        f"{words[0].upper()}{words[1:]} {verb} not on the list of pages not checked. Only pages "
        "listed there can be confirmed."
    )


def stale_pages_sentence(numbers: list[int]) -> str:
    """Said when "Confirm these N pages" names pages the list no longer shows as the screen did."""
    words = pages_words(numbers)
    return (
        f"The list of pages not checked changed since you opened it ({words}: read again or "
        "already answered). Nothing was saved. Reload the results and confirm again."
    )


def _newest_revision(session: Session, project_id: UUID, package_id: UUID) -> PackageRevision:
    revision = session.execute(
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return revision


def _listed_now(
    session: Session, revision: PackageRevision
) -> tuple[tuple[UnchosenRowPage, ...], dict[UUID, PageAnswer]]:
    """The revision's pages as listed right now, with the answers in force, read under the
    revision's answer lock (taken here), so two answers never cross."""
    session.execute(select(func.pg_advisory_xact_lock(_answer_lock_key(revision.id))))
    _rows, unchosen = slot_rows_and_unchosen_pages(session, revision.id)
    owned = reviewer_owned_pages(session, revision.id) if unchosen else set()
    return answered_pages(
        session, revision, tuple(page for page in unchosen if page.page_number - 1 not in owned)
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/no-countertop-pages/{record_id}/answer",
    response_model=CountertopResultsOut,
    status_code=status.HTTP_201_CREATED,
    summary="Confirm that a listed page has no countertop, or say there is one",
    dependencies=[Depends(open_revision)],
)
def answer_no_countertop_page(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    record_id: UUID,
    body: NoCountertopAnswerIn,
    response: Response,
) -> CountertopResultsOut:
    """Record one reviewer answer on a page every AI said has no countertop (#1191).

    The page must be listed right now on the newest revision (409 otherwise, with a plain sentence).
    The answer is a new append-only record with who and when; the newest one counts. Answers the
    updated countertop results, so the screen shows the page confirmed, or as a blocking item.

    **One answer at a time per revision.** The newest answer is read under a transaction-scoped
    advisory lock, so a "Confirm" sent at the same moment as a "There is a countertop" waits for it,
    finds the page no longer listed and is refused, instead of silently overriding it. A second
    "Confirm" on a page already confirmed records nothing and answers 200 with the first person and
    time (the answer was already given).
    """
    revision = _newest_revision(session, project_id, package_id)
    listed_now, in_force = _listed_now(session, revision)
    page = next(
        (page for page in listed_now if page.record.id == record_id and not page.split), None
    )
    if page is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=PAGE_NOT_LISTED)
    standing = in_force.get(page.record.id)
    if standing is not None and standing.answer == body.answer:
        # Already confirmed: nothing new to record; the first person and time stand.
        session.rollback()
        response.status_code = status.HTTP_200_OK
        return _countertop_results_for_revision(session, package_id, revision)
    answer = NoCountertopAnswer(
        package_revision_id=revision.id,
        row_choice_candidate_id=page.record.id,
        page_id=page.record.page_id,
        page_number=page.page_number,
        page_content_hash=page.page_content_hash or "",
        reading_answer=reading_answer(page),
        answer=body.answer,
        answered_by=principal.id,
        note=_note(body.note),
    )
    session.add(answer)
    session.flush()
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=principal.id,
        target_id=answer.id,
        target_type="no_countertop_answer",
    )
    session.commit()
    return _countertop_results_for_revision(session, package_id, revision)


#: The audit target type of one "Confirm these N pages" action (#1195); its id is the `batch_id`
#: on each answer it recorded.
NO_COUNTERTOP_BATCH_TARGET: Final = "no_countertop_answer_batch"


@router.post(
    "/projects/{project_id}/packages/{package_id}/no-countertop-pages/confirm",
    response_model=CountertopResultsOut,
    status_code=status.HTTP_201_CREATED,
    summary="Confirm that each named listed page has no countertop, in one action",
    dependencies=[Depends(open_revision)],
)
def confirm_no_countertop_pages(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    body: NoCountertopPagesIn,
    response: Response,
) -> CountertopResultsOut:
    """ "Confirm these N pages" (#1195): "Confirm: no countertop here" on every page named.

    The same permission, lock and record as the single answer (`answer_no_countertop_page`): each
    page gets its own answer row (who, when, the optional note) and its own audit event; the rows
    share a batch id, which has an audit event of its own.

    **All or nothing.** The request names each page and the reading record the screen showed for it.
    A record that was never a listed page of this revision is refused with 422. If any named page is
    no longer listed as the screen showed it (read again, answered "There is a countertop", or no
    longer listed), nothing is recorded and the request is refused with 409: the screen reloads.
    A page already confirmed records nothing new (its first person and time stand); when every named
    page was already confirmed the answer is 200, else 201. Pages not named keep waiting.
    """
    revision = _newest_revision(session, project_id, package_id)
    listed_now, in_force = _listed_now(session, revision)
    named = {page.expected_record_id: page.page_number for page in body.pages}
    # 422: a record the list never held for that page (not a listed reading of this revision).
    records = {
        record.id: (page_index + 1, unchosen_page(page_index, record).listed)
        for record, page_index in session.execute(
            select(ObservationCandidate, Page.index)
            .join(Page, Page.id == ObservationCandidate.page_id)
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.document_version_id == Page.document_version_id,
            )
            .where(
                ObservationCandidate.id.in_(named),
                PackageRevisionDocument.package_revision_id == revision.id,
                ObservationCandidate.ambiguity_flags.contains(["slot-reader-row-choice"]),
            )
        ).all()
    }
    unknown = sorted(
        {number for record, number in named.items() if records.get(record) != (number, True)}
    )
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_pages_not_on_list(unknown)
        )
    # 409: the list changed since the screen loaded it.
    current = {page.record.id: page for page in listed_now if page.listed}
    stale = sorted({number for record, number in named.items() if record not in current})
    if stale:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=stale_pages_sentence(stale)
        )
    batch_id = uuid4()
    note = _note(body.note)
    recorded = 0
    for record_id in sorted(named, key=lambda record: (named[record], str(record))):
        page = current[record_id]
        standing = in_force.get(record_id)
        if standing is not None and standing.answer == "no_countertop":
            continue  # already confirmed: the first person and time stand
        answer = NoCountertopAnswer(
            package_revision_id=revision.id,
            row_choice_candidate_id=record_id,
            page_id=page.record.page_id,
            page_number=page.page_number,
            page_content_hash=page.page_content_hash or "",
            reading_answer=reading_answer(page),
            answer="no_countertop",
            answered_by=principal.id,
            note=note,
            batch_id=batch_id,
        )
        session.add(answer)
        session.flush()
        emit(
            session,
            category=AuditCategory.REVIEW_ACTION,
            actor=principal.id,
            target_id=answer.id,
            target_type="no_countertop_answer",
        )
        recorded += 1
    if not recorded:
        session.rollback()
        response.status_code = status.HTTP_200_OK
        return _countertop_results_for_revision(session, package_id, revision)
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=principal.id,
        target_id=batch_id,
        target_type=NO_COUNTERTOP_BATCH_TARGET,
    )
    session.commit()
    return _countertop_results_for_revision(session, package_id, revision)


__all__ = [
    "NO_COUNTERTOP_BATCH_TARGET",
    "_countertop_results_for_revision",
    "answer_no_countertop_page",
    "confirm_no_countertop_pages",
    "countertop_results",
    "router",
]
