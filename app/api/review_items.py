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
* a number: W2's confirm and correct (#1339; 501 until it lands).

An answer that changes what a check reads (a wall, an architect pick, "There is a countertop", a
number) queues one coalesced re-check (`app/api/measurements.py:queue_recheck`) in the same
transaction: never a 409 to the person. `app/review/review_item_actions.py` says how keys, stale
items and numbers are decided.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.architect_matches import pick_architect_view
from app.api.dependencies import get_session
from app.api.measurements import checks_pending, queue_recheck
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
from app.errors import _envelope
from app.models import Package, PackageRevision
from app.review import review_item_actions as actions
from app.review.review_items import ReviewItemOut, ReviewItemsOut, review_items_for_revision
from app.schemas.architect_matches import ArchitectViewPickIn

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
    del principal
    revision = session.execute(
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return review_items_for_revision(
        session, project_id=project_id, package_id=package_id, revision=revision
    )


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
        501: {"description": "This kind of answer cannot be saved yet (numbers: W2)."},
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
    request: Request,
) -> actions.ReviewItemActionOut | JSONResponse:
    """One answer. URL actions: `confirm | fix | answer | agree | clear-not-checked`.

    `answer` carries the answer's own name in the body: `{"answer": "checked_right"}`,
    `{"answer": "checked_wrong"}`, `{"answer": "choose_wall", "wall_config": "..."}`,
    `{"answer": "confirm_no_countertop"}`, `{"answer": "there_is_a_countertop"}`, or for an
    architect item `{"architect_view": {...}}` / `{"architect_pairing": {...}}`.

    Group keys: `not-checked` with `clear-not-checked` dismisses every open "Not checked" result in
    one transaction; `no-countertop-pages` with `answer` `confirm_no_countertop` confirms every
    listed page at once. Both take `expected_keys` (409 when the group changed).
    """
    try:
        revision, listed = actions.current_items(session, project_id, package_id)
        if key in (actions.NOT_CHECKED_GROUP, actions.PAGES_GROUP):
            name = actions.answer_name(action, body, "group")
            return _answer_group(
                session, principal, project_id, package_id, revision, listed, key, name, body
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

    if kind in ("number", "depth_reading"):
        # Not yet, said plainly: a raised 5xx would hide the reason behind the generic sentence.
        session.rollback()
        return _envelope(
            request,
            "not_implemented",
            actions.NUMBERS_NOT_YET if kind == "number" else actions.DEPTH_NOT_YET,
            status.HTTP_501_NOT_IMPLEMENTED,
        )

    sitting_id: UUID | None = None
    if name in actions.FINDING_ANSWERS:
        assert entry.finding_id is not None
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
        assert isinstance(entry, ReviewItemOut) and entry.row_id is not None
        # Queued first, in the same transaction: the existing route's commit lands both, and its
        # rollback (refused, or nothing new to record) drops both.
        if name in _RECHECKS:
            queue_recheck(session, revision)
        recorded = _answer_through_existing_post(
            session, principal, project_id, package_id, entry, name, body
        )

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


def _answer_through_existing_post(
    session: Session,
    principal: Principal,
    project_id: UUID,
    package_id: UUID,
    entry: ReviewItemOut,
    name: str,
    body: actions.ReviewItemActionIn,
) -> str:
    """Call the existing POST for this answer, exactly as its own route would be called."""
    row_id = entry.row_id
    assert row_id is not None
    if name == "choose_wall":
        if not body.wall_config:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Choose one of the published wall layouts (`wall_config`).",
            )
        review_slot_row(
            principal,
            principal,
            session,
            project_id,
            package_id,
            row_id,
            SlotRowReviewIn(wall_config=body.wall_config),
        )
        return f"Wall layout chosen for this countertop: {body.wall_config}."
    if name in ("confirm_no_countertop", "there_is_a_countertop"):
        answer: Literal["no_countertop", "countertop"] = (
            "no_countertop" if name == "confirm_no_countertop" else "countertop"
        )
        answer_no_countertop_page(
            principal,
            principal,
            session,
            project_id,
            package_id,
            row_id,
            NoCountertopAnswerIn(answer=answer),
            Response(),
        )
        return (
            "Confirmed: no countertop on this page."
            if answer == "no_countertop"
            else "Recorded: there is a countertop on this page. Its result comes with the re-check."
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
            return "Architect's view chosen for this countertop."
        pairing = ArchitectPairingIn.model_validate(body.architect_pairing)
    except ValidationError as error:
        session.rollback()
        raise _invalid(error) from error
    pair_architect_dimensions(
        principal, principal, session, project_id, package_id, row_id, pairing
    )
    return "Architect's dimensions paired with this countertop."


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
) -> actions.ReviewItemActionOut:
    """A whole group in one action and one transaction."""
    sitting_id: UUID | None = None
    if key == actions.NOT_CHECKED_GROUP:
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
        confirm_no_countertop_pages(
            principal,
            principal,
            session,
            project_id,
            package_id,
            NoCountertopPagesIn.model_validate({"pages": pages}),
            Response(),
        )
        words = f"{len(pages)} pages confirmed with no countertop."
    session.refresh(revision)
    return actions.ReviewItemActionOut(
        key=key,
        action=name,
        recorded=words,
        review_session_id=sitting_id,
        rechecking=checks_pending(session, revision),
        review_items=review_items_for_revision(
            session, project_id=project_id, package_id=package_id, revision=revision
        ),
    )


__all__ = ["router"]
