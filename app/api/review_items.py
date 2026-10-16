"""`GET .../review-items`: one read-only list of what a person must check (#1338, simple review W1),
and `POST .../review-items/{key}/{action}`: one answer to one item of it (#1351, W3).

The GET only finds the package's newest revision and hands it to `app/review/review_items.py`,
which builds the list from the existing projections. It writes nothing, queues nothing and calls no
model.

The POST writes ONLY through paths that already exist, and holds no truth path of its own:

* checked by hand, FAIL "Agree" and "Not checked": the review-session finding actions
  (`app/review/session.py:record_action`), with a fixed note, in the person's own session (opened
  here when there is none);
* a wall choice: `POST .../slot-rows/{row}/review`, with the wall choice only;
* a page listed with no countertop: the existing answer, or "Confirm these N pages";
* an architect question: the existing view-match or pairing POST;
* a number: W2's confirm and correct (#1339);
* a depth or sink reading (#1360): Confirm through W2's same confirm (once only, its own countertop
  only); "Not this" as a finding action with a fixed note, printed NOT CHECKED;
* a cabinet run's marks (#648): the reviewer's classification of each cabinet
  (`workflow/classifications.py:record_classifications`, what `CAB-FILLER-001` reads).

An answer that changes what a check reads (a wall, an architect pick, "There is a countertop", a
number) queues one coalesced re-check (`app/api/measurements.py:queue_recheck`) in the same
transaction: never a 409 to the person. `app/review/review_item_actions.py` says how keys, stale
items and numbers are decided.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.architect_matches import pick_architect_view
from app.api.company_settings import ProjectValueIn, may_save_project, store_project_values
from app.api.confirmations import _candidate_crop_artifact, _verified_crop_content
from app.api.dependencies import get_artifact_store, get_session
from app.api.measurements import (
    _declared_inputs,
    _published_rules,
    _store,
    checks_pending,
    queue_recheck,
)
from app.api.review import _refuse
from app.api.signed_off import open_revision
from app.api.slot_rows import (
    ArchitectPairingIn,
    SlotRowReviewIn,
    pair_architect_dimensions,
    review_slot_row,
)
from app.api.visual_countertops import (
    NoCountertopAnswerIn,
    NoCountertopPagesIn,
    answer_no_countertop_page,
    confirm_no_countertop_pages,
)
from app.auth import Action, Principal, require_action, require_project_access
from app.models import Package, PackageRevision
from app.models.review import ReviewActionKind
from app.review import ai_review
from app.review import architect_compare as compare
from app.review import review_item_actions as actions
from app.review.cabinet_run_answer import CabinetRunRefused, cabinet_run_answer
from app.review.evidence_actions import (
    HeldReadingRefusal,
    HeldReadingRefused,
    confirm_candidate_reading,
    correct_candidate_reading,
)
from app.review.review_items import (
    CABINET_RULE,
    CABINET_TYPE_INPUT,
    ReviewItemOut,
    ReviewItemsOut,
    parsed_inches,
    review_items_for_revision,
)
from app.schemas.architect_matches import ArchitectViewPickIn
from rules.parameters import ParameterLayer, Provenance
from storage.store import ArtifactStore
from workflow.classifications import record_classifications

router = APIRouter(tags=["simple review"])


@router.get(
    "/projects/{project_id}/packages/{package_id}/review-items",
    response_model=ReviewItemsOut,
    summary="List exactly what a person must check on the newest revision, read-only",
)
def review_items(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.READ_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> ReviewItemsOut:
    """Items to answer, FAIL results to agree with, and the checks that could not run.

    Every blocking result is in exactly one of `items`, `failures` and `not_checked`, and every
    page that blocks sign-off is one item; `unaccounted` names anything that is not, and must be
    empty. Nothing here decides or records anything.
    """
    revision = session.execute(
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    listed = review_items_for_revision(
        session, project_id=project_id, package_id=package_id, revision=revision
    )
    listed.can_save_job_values = may_save_project(principal, project_id)
    return listed


#: Answers whose record a check reads: the checks run again after them (coalesced).
_RECHECKS: frozenset[str] = frozenset(
    {"choose_wall", "there_is_a_countertop", "answer", "confirm", "fix"}
)


def _http(error: actions.ItemRefused) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail)


def _invalid(error: ValidationError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="; ".join(str(problem.get("msg")) for problem in error.errors()) or str(error),
    )


def _artifact_store_when_needed(request: Request) -> Callable[[], ArtifactStore]:
    """The artifact store, fetched only by an answer that needs it (a depth or sink reading's
    picture, #1360), so every other answer works where no store is configured. An override of
    `get_artifact_store` is honoured."""
    provider = request.app.dependency_overrides.get(get_artifact_store)
    if provider is None:
        return lambda: get_artifact_store(request)
    return lambda: provider()


@router.post(
    "/projects/{project_id}/packages/{package_id}/review-items/{key}/{action}",
    response_model=actions.ReviewItemActionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Answer one item of the review list, through the existing decision paths",
    dependencies=[Depends(open_revision)],
    responses={
        404: {"description": "No such package, or no such item."},
        409: {"description": "The item is stale (`expected_finding_id`) or no longer open."},
        422: {"description": "The item does not take this answer, or the answer is incomplete."},
    },
)
def answer_review_item(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    key: str,
    action: str,
    body: actions.ReviewItemActionIn,
    response: Response,
    store: Annotated[Callable[[], ArtifactStore], Depends(_artifact_store_when_needed)],
) -> actions.ReviewItemActionOut | JSONResponse:
    """One answer. URL actions: `confirm | fix | answer | agree | clear-not-checked`.

    `answer` carries the answer's own name in the body: `{"answer": "checked_right"}`,
    `{"answer": "checked_wrong"}`, `{"answer": "choose_wall", "wall_config": "..."}`,
    `{"answer": "confirm_no_countertop"}`, `{"answer": "there_is_a_countertop"}`, or for an
    architect item `{"architect_view": {...}}` / `{"architect_pairing": {...}}`. `confirm` on a
    number names the value shown (`shown_value`).

    Group keys: `not-checked` with `clear-not-checked` dismisses every open "Not checked" result in
    one transaction; `no-countertop-pages` with `answer` `confirm_no_countertop` confirms every
    listed page at once. Both require `expected_keys` (409 when the group changed).
    `not-checked` with `answer` `job_values` and `job_values: [{name, value}]` saves this job's
    values through the project's settings (Company settings → Projects) and queues one coalesced
    re-check (#1403); it needs no `expected_keys`, because the values belong to the project.

    201 when something was recorded; 200 when the answer was already recorded (nothing new).
    """
    try:
        revision, listed = actions.current_items(session, project_id, package_id)
        if key.startswith(ai_review.AI_KEY_PREFIX):
            return _answer_ai_review(
                session, principal, project_id, package_id, revision, key, action, body, response
            )
        if key.startswith(compare.KEY_PREFIX) or key == compare.NOT_COMPARED_GROUP:
            return _answer_compare(
                session, principal, project_id, package_id, revision, key, action, body, response
            )
        if key in (actions.NOT_CHECKED_GROUP, actions.PAGES_GROUP):
            name = actions.answer_name(action, body, "group")
            return _answer_group(
                session,
                principal,
                project_id,
                package_id,
                revision,
                listed,
                key,
                name,
                body,
                response,
            )
        kind = (
            "architect"
            if any(item.key == key and item.kind == "architect" for item in listed.items)
            else ""
        )
        name = actions.answer_name(action, body, kind)
        entry, kind = actions.find_entry(
            session,
            listed,
            package_id=package_id,
            key=key,
            action=name,
            expected_finding_id=body.expected_finding_id,
        )
    except actions.ItemRefused as refused:
        raise _http(refused) from refused

    sitting_id: UUID | None = None
    if kind == "cabinet_run":
        if not isinstance(entry, ReviewItemOut):  # pragma: no cover - the kind is an item's
            raise _unprocessable("This item has no cabinet run to use.")
        recorded = _answer_cabinet_run(session, principal, project_id, revision, entry, body)
    elif kind == "cabinet_kinds":
        if not isinstance(entry, ReviewItemOut):  # pragma: no cover - the kind is an item's
            raise _unprocessable("This item has no cabinets to mark.")
        recorded = _answer_cabinets(session, principal, revision, entry, body)
    elif kind == "number" or (kind == "depth_reading" and name == "confirm"):
        # A depth or sink reading's Confirm is the same held-reading evidence decision as a
        # number's (#1360): once only, its own countertop only, the value shown.
        if not isinstance(entry, ReviewItemOut) or name not in ("confirm", "fix"):
            raise _unprocessable("A number takes Confirm or Fix.")
        if kind == "depth_reading":
            _check_depth_picture(session, store(), revision, entry)
        sitting_id, recorded = _answer_number(session, principal, revision, entry, name, body)
    elif kind == "depth_reading" and name == "not_this":
        # "Not this" = Not checked (Anant, 2026-10-16): the whole result is recorded as not
        # checked, so it is offered only on one countertop's own result with this one reading
        # open (the list's actions, held by `find_entry`). Nothing is written on the reading.
        if not isinstance(entry, ReviewItemOut) or entry.finding_id is None:
            raise _unprocessable("This item has no result to record the answer on.")
        note = actions.depth_not_this_note(entry)
        try:
            sitting, _recorded = actions.record_finding_actions(
                session,
                revision_id=revision.id,
                actor=principal.id,
                decisions=[
                    actions.FindingDecision(entry.finding_id, ReviewActionKind.DISMISS, note)
                ],
            )
            sitting_id = sitting.id
            session.commit()
        except Exception as error:
            session.rollback()
            raise _refuse(error) from error
        recorded = f"Recorded on the result: {note}"
    elif name in actions.FINDING_ANSWERS:
        if entry.finding_id is None:
            raise _unprocessable("This item has no result to record the answer on.")
        if isinstance(entry, ReviewItemOut):
            try:
                actions.refuse_until_numbers_answered(session, listed, entry)
            except actions.ItemRefused as refused:
                raise _http(refused) from refused
        kind_of, note = actions.FINDING_ANSWERS[name]
        try:
            sitting, _recorded = actions.record_finding_actions(
                session,
                revision_id=revision.id,
                actor=principal.id,
                decisions=[actions.FindingDecision(entry.finding_id, kind_of, note)],
            )
            sitting_id = sitting.id
            session.commit()
        except Exception as error:
            session.rollback()
            raise _refuse(error) from error
        recorded = f"Recorded on the result: {note}"
    else:
        if not isinstance(entry, ReviewItemOut) or entry.row_id is None:
            raise _unprocessable("This item has no countertop row or page to answer on.")
        if name == "choose_wall":
            _check_wall_choice(session, entry, body)
        # Queued first, in the same transaction: the existing route's commit lands both, and its
        # rollback (refused, or nothing new to record) drops both.
        if name in _RECHECKS:
            try:
                queue_recheck(session, revision)
            except HTTPException:
                session.rollback()
                raise
        recorded, nothing_new = _answer_through_existing_post(
            session, principal, project_id, package_id, entry, entry.row_id, name, body
        )
        if nothing_new:
            response.status_code = status.HTTP_200_OK

    session.refresh(revision)
    return actions.ReviewItemActionOut(
        key=key,
        action=name,
        recorded=recorded,
        review_session_id=sitting_id,
        rechecking=checks_pending(session, revision),
        review_items=review_items_for_revision(
            session, project_id=project_id, package_id=package_id, revision=revision
        ),
    )


def _answer_compare(
    session: Session,
    principal: Principal,
    project_id: UUID,
    package_id: UUID,
    revision: PackageRevision,
    key: str,
    action: str,
    body: actions.ReviewItemActionIn,
    response: Response,
) -> actions.ReviewItemActionOut:
    """An answer on a number compared with the architect (#1418): `agree` (the vendor is wrong) or
    `not_this` (not a difference) on a different one, `checked_right` / `checked_wrong` on one with
    no match or not read, or on the group `architect-not-compared` `clear_not_compared` with the
    `expected_keys` shown. Its own append-only row; the compare's record is never edited."""
    name = action.strip().lower().replace("-", "_")
    if name == "answer":
        if body.answer is None:
            raise _unprocessable('Say which answer in the body, e.g. {"answer": "checked_right"}.')
        name = body.answer.strip().lower().replace("-", "_")
    try:
        written = compare.record_compare_answers(
            session,
            revision_id=revision.id,
            key=key,
            answer=name,
            actor=principal.id,
            expected_keys=body.expected_keys,
        )
        session.commit()
    except compare.CompareRefused as refused:
        session.rollback()
        raise HTTPException(status_code=refused.status, detail=refused.detail) from refused
    if not written:
        response.status_code = status.HTTP_200_OK
    session.refresh(revision)
    stored = compare.SENT_AS[name]
    return actions.ReviewItemActionOut(
        key=key,
        action=name,
        recorded=compare.ANSWER_NOTES[stored],
        review_session_id=None,
        rechecking=checks_pending(session, revision),
        review_items=review_items_for_revision(
            session, project_id=project_id, package_id=package_id, revision=revision
        ),
    )


def _answer_ai_review(
    session: Session,
    principal: Principal,
    project_id: UUID,
    package_id: UUID,
    revision: PackageRevision,
    key: str,
    action: str,
    body: actions.ReviewItemActionIn,
    response: Response,
) -> actions.ReviewItemActionOut:
    """An answer on an AI page review row (`air:<id>`, decision 2026-10-16): `agree` (the vendor
    is wrong) or `not_this` (not a difference) on a different row, as the URL action or as
    `answer` with the name in the body. Its own append-only row; the review row is never edited."""
    name = action.strip().lower().replace("-", "_")
    if name == "answer":
        if body.answer is None:
            raise _unprocessable('Say which answer in the body, e.g. {"answer": "not_this"}.')
        name = body.answer.strip().lower().replace("-", "_")
    try:
        written = ai_review.record_ai_review_answer(
            session, revision_id=revision.id, key=key, answer=name, actor=principal.id
        )
        session.commit()
    except ai_review.AiReviewRefused as refused:
        session.rollback()
        raise HTTPException(status_code=refused.status, detail=refused.detail) from refused
    if not written:
        response.status_code = status.HTTP_200_OK
    session.refresh(revision)
    return actions.ReviewItemActionOut(
        key=key,
        action=name,
        recorded=ai_review.AI_REVIEW_ANSWER_NOTES[ai_review.AI_REVIEW_SENT_AS[name]],
        review_session_id=None,
        rechecking=checks_pending(session, revision),
        review_items=review_items_for_revision(
            session, project_id=project_id, package_id=package_id, revision=revision
        ),
    )


#: W2 refusals that mean "the item moved on since you looked": 409, reload. The rest are 422.
_NUMBER_CONFLICTS: frozenset[HeldReadingRefusal] = frozenset(
    {
        HeldReadingRefusal.ALREADY_ANSWERED,
        HeldReadingRefusal.ALREADY_COUNTS,
        HeldReadingRefusal.SHOWN_VALUE_DIFFERS,
        HeldReadingRefusal.REVISION_CLOSED,
        HeldReadingRefusal.DEPTH_CONFIRM_ONCE,
    }
)


def _answer_number(
    session: Session,
    principal: Principal,
    revision: PackageRevision,
    entry: ReviewItemOut,
    name: str,
    body: actions.ReviewItemActionIn,
) -> tuple[UUID, str]:
    """Confirm or Fix one held AI number through W2 (#1339), with one coalesced re-check.

    Confirm records exactly the stored reading, and only when it is the value the screen showed
    (`shown_value`); Fix records the person's value as a correction. Both, and the re-check, commit
    together or not at all.
    """
    if entry.finding_id is None or entry.candidate_id is None:
        raise _unprocessable("This number has no AI reading to confirm or fix; check it by hand.")
    try:
        if name == "confirm":
            actions.check_shown_value(entry, body.shown_value)
    except actions.ItemRefused as refused:
        session.rollback()
        raise _http(refused) from refused
    if name == "fix" and (body.value is None or not body.value.strip()):
        raise _unprocessable("Type the value the drawing shows, in inches. Nothing was saved.")
    try:
        sitting = actions.reviewer_session(session, revision.id, principal.id)
        if name == "confirm":
            shown = parsed_inches(body.shown_value or "")
            assert shown is not None  # check_shown_value refused anything else
            confirm_candidate_reading(
                session,
                principal=principal,
                review_session_id=sitting.id,
                finding_id=entry.finding_id,
                candidate_id=entry.candidate_id,
                expected_value=shown,
            )
        else:
            correct_candidate_reading(
                session,
                principal=principal,
                review_session_id=sitting.id,
                finding_id=entry.finding_id,
                candidate_id=entry.candidate_id,
                corrected_value=body.value or "",
            )
        queue_recheck(session, revision)
        sitting_id = sitting.id
        session.commit()
    except HeldReadingRefused as refused:
        session.rollback()
        code = (
            status.HTTP_409_CONFLICT
            if refused.reason in _NUMBER_CONFLICTS
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise HTTPException(
            status_code=code, detail=f"{refused.detail}. Nothing was saved."
        ) from refused
    except HTTPException:
        session.rollback()
        raise
    except Exception as error:
        session.rollback()
        raise _refuse(error) from error
    if name == "confirm":
        return sitting_id, "Confirmed: the checks will use this reading."
    return (
        sitting_id,
        "Fixed: recorded as a correction of the AI's reading; the checks will use your value.",
    )


def _answer_cabinets(
    session: Session,
    principal: Principal,
    revision: PackageRevision,
    entry: ReviewItemOut,
    body: actions.ReviewItemActionIn,
) -> str:
    """The marks and one coalesced re-check, committed together or not at all (#648)."""
    try:
        marks = actions.record_cabinet_marks(
            session,
            revision_id=revision.id,
            entry=entry,
            kinds=body.cabinet_kinds,
            actor=principal.id,
        )
        queue_recheck(session, revision)
        session.commit()
    except actions.ItemRefused as refused:
        session.rollback()
        raise _http(refused) from refused
    except HTTPException:
        session.rollback()
        raise
    except Exception as error:
        session.rollback()
        raise _refuse(error) from error
    keeps = sum(
        1
        for mark in marks
        if any(c.category == mark and c.keeps_width for c in entry.cabinet_choices)
    )
    return (
        f"Marked {len(marks)} cabinets: {keeps} keep their width, {len(marks) - keeps} can be "
        "resized. The cabinet check runs again with them."
    )


def _answer_cabinet_run(
    session: Session,
    principal: Principal,
    project_id: UUID,
    revision: PackageRevision,
    entry: ReviewItemOut,
    body: actions.ReviewItemActionIn,
) -> str:
    """A proposed cabinet run used (cabinets e2e): the vendor's fillers and cabinets as two AIs read
    them, each cabinet's mark and the site wall-to-wall, stored where `CAB-FILLER-001` reads them
    (the RUN set Measurements stores, `app/api/measurements.py:_store`; the marks through
    `workflow/classifications.py`), and one coalesced re-check. All or nothing."""
    published = _published_rules(session)
    try:
        answer = cabinet_run_answer(
            entry.cabinet_proposal,
            body.cabinet_run,
            offered=[choice.category for choice in entry.cabinet_choices],
            declared=set(_declared_inputs(published)),
        )
    except CabinetRunRefused as refused:
        raise HTTPException(status_code=refused.status_code, detail=refused.detail) from refused
    try:
        _store(
            session,
            project_id=project_id,
            layer=ParameterLayer.RUN,
            values=answer.values,
            typed=answer.typed,
            actor=principal.id,
            carry_forward=True,
            package_revision_id=revision.id,
            sources={
                key: (Provenance.MEASURED, reference)
                for key, reference in answer.references.items()
            },
        )
        record_classifications(
            session,
            package_revision_id=revision.id,
            rule_id=CABINET_RULE,
            input_name=CABINET_TYPE_INPUT,
            categories=answer.categories,
            confirmed_by=principal.id,
        )
        session.flush()
        queue_recheck(session, revision)
        session.commit()
    except HTTPException:
        session.rollback()
        raise
    except Exception as error:
        session.rollback()
        raise _refuse(error) from error
    return answer.summary


def _check_depth_picture(
    session: Session, store: ArtifactStore, revision: PackageRevision, entry: ReviewItemOut
) -> None:
    """A depth or sink reading is confirmed only with its own picture, whose stored bytes still
    match their digest: the same guard `POST .../candidates/{id}/confirm` applies (#1360, defence in
    depth behind the list's `can_confirm`). 409 otherwise; nothing is saved."""
    artifact = (
        None
        if entry.candidate_id is None
        else _candidate_crop_artifact(session, revision, entry.candidate_id)
    )
    if artifact is None:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This reading has no picture yet, so it cannot be confirmed here. Nothing was "
            "saved.",
        )
    try:
        _verified_crop_content(store, artifact)
    except HTTPException:
        session.rollback()
        raise


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _check_wall_choice(
    session: Session, entry: ReviewItemOut, body: actions.ReviewItemActionIn
) -> None:
    """A wall choice names one of the layouts the item offered (422 otherwise; nothing saved)."""
    offered = [choice.layout for choice in entry.wall_choices]
    if not body.wall_config or (offered and body.wall_config not in offered):
        session.rollback()
        raise _unprocessable(
            "Choose one of the published wall layouts (`wall_config`)"
            + (f": {', '.join(offered)}." if offered else ".")
        )


def _answer_through_existing_post(
    session: Session,
    principal: Principal,
    project_id: UUID,
    package_id: UUID,
    entry: ReviewItemOut,
    row_id: UUID,
    name: str,
    body: actions.ReviewItemActionIn,
) -> tuple[str, bool]:
    """Call the existing POST for this answer, exactly as its own route would be called. Returns
    what was recorded, in words, and whether it was nothing new (the answer already stood)."""
    if name == "choose_wall":
        review_slot_row(
            principal,
            principal,
            session,
            project_id,
            package_id,
            row_id,
            SlotRowReviewIn(wall_config=body.wall_config),
        )
        return f"Wall layout chosen for this countertop: {body.wall_config}.", False
    if name in ("confirm_no_countertop", "there_is_a_countertop"):
        answer: Literal["no_countertop", "countertop"] = (
            "no_countertop" if name == "confirm_no_countertop" else "countertop"
        )
        answered = Response(status_code=status.HTTP_201_CREATED)
        answer_no_countertop_page(
            principal,
            principal,
            session,
            project_id,
            package_id,
            row_id,
            NoCountertopAnswerIn(answer=answer),
            answered,
        )
        if answered.status_code == status.HTTP_200_OK:
            # The existing route's "nothing new": the first person and time stand.
            return "Nothing new: this page's answer was already recorded.", True
        return (
            (
                "Confirmed: no countertop on this page."
                if answer == "no_countertop"
                else "Recorded: there is a countertop on this page. Its result comes with the "
                "re-check."
            ),
            False,
        )
    # The architect question: exactly one of a view pick and a pairing.
    if (body.architect_view is None) == (body.architect_pairing is None):
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Send exactly one of `architect_view` and `architect_pairing`.",
        )
    try:
        if body.architect_view is not None:
            pick = ArchitectViewPickIn.model_validate(body.architect_view)
            pick_architect_view(principal, principal, session, project_id, package_id, row_id, pick)
            return "Architect's view chosen for this countertop.", False
        pairing = ArchitectPairingIn.model_validate(body.architect_pairing)
    except ValidationError as error:
        session.rollback()
        raise _invalid(error) from error
    pair_architect_dimensions(
        principal, principal, session, project_id, package_id, row_id, pairing
    )
    return "Architect's dimensions paired with this countertop.", False


def _answer_group(
    session: Session,
    principal: Principal,
    project_id: UUID,
    package_id: UUID,
    revision: PackageRevision,
    listed: ReviewItemsOut,
    key: str,
    name: str,
    body: actions.ReviewItemActionIn,
    response: Response,
) -> actions.ReviewItemActionOut:
    """A whole group in one action and one transaction, only for exactly the keys the person saw
    (`expected_keys`, required)."""
    sitting_id: UUID | None = None
    if key == actions.NOT_CHECKED_GROUP and name == "job_values":
        words = _save_job_values(session, principal, project_id, revision, body)
    elif key == actions.NOT_CHECKED_GROUP:
        if name not in ("clear_not_checked", "settle_group"):
            raise actions.ItemRefused(422, "The 'Not checked' group takes 'clear-not-checked'.")
        keys = actions.check_group(listed, actions.NOT_CHECKED_GROUP, body.expected_keys)
        chosen = set(keys)
        kind_of, note = actions.FINDING_ANSWERS["clear_not_checked"]
        try:
            sitting, recorded = actions.record_finding_actions(
                session,
                revision_id=revision.id,
                actor=principal.id,
                decisions=[
                    actions.FindingDecision(entry.finding_id, kind_of, note)
                    for entry in listed.not_checked
                    if entry.key in chosen
                ],
            )
            sitting_id = sitting.id
            session.commit()
        except Exception as error:
            session.rollback()
            raise _refuse(error) from error
        words = f"{len(recorded)} checks recorded as {note}."
    else:
        if name != "confirm_no_countertop":
            raise actions.ItemRefused(
                422, "The pages group takes the answer 'confirm_no_countertop'."
            )
        keys = actions.check_group(listed, actions.PAGES_GROUP, body.expected_keys)
        chosen = set(keys)
        pages = [
            {"page_number": item.page, "expected_record_id": str(item.row_id)}
            for item in listed.items
            if item.key in chosen
        ]
        answered = Response(status_code=status.HTTP_201_CREATED)
        confirm_no_countertop_pages(
            principal,
            principal,
            session,
            project_id,
            package_id,
            NoCountertopPagesIn.model_validate({"pages": pages}),
            answered,
        )
        if answered.status_code == status.HTTP_200_OK:
            response.status_code = status.HTTP_200_OK
            words = "Nothing new: every page's answer was already recorded."
        else:
            words = f"{len(pages)} pages confirmed with no countertop."
    session.refresh(revision)
    listed_now = review_items_for_revision(
        session, project_id=project_id, package_id=package_id, revision=revision
    )
    listed_now.can_save_job_values = may_save_project(principal, project_id)
    return actions.ReviewItemActionOut(
        key=key,
        action=name,
        recorded=words,
        review_session_id=sitting_id,
        rechecking=checks_pending(session, revision),
        review_items=listed_now,
    )


def _save_job_values(
    session: Session,
    principal: Principal,
    project_id: UUID,
    revision: PackageRevision,
    body: actions.ReviewItemActionIn,
) -> str:
    """This job's values (#1403), saved through the project's settings: the path Company settings
    → Projects saves by (`store_project_values`), never a new one. Then one coalesced re-check, in
    the same transaction: a refused value saves nothing and queues nothing."""
    if not may_save_project(principal, project_id):
        raise actions.ItemRefused(
            403, "Only this project's reviewers may enter its values. Nothing was saved."
        )
    entries = body.job_values or []
    try:
        store_project_values(
            session,
            principal,
            project_id,
            [ProjectValueIn(name=entry.name, value=entry.value) for entry in entries],
        )
        queue_recheck(session, revision)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return f"{len(entries)} of this job's values saved on the project; the checks run again."


__all__ = ["router"]
