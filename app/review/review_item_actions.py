"""What one answer on the simple review list means, and which existing path records it (#1351, W3).

`POST .../review-items/{key}/{action}` (`app/api/review_items.py`) answers one item of the W1 list
(`app/review/review_items.py`). This module holds the decisions that are not HTTP: which item a key
names now, whether the screen's view of it is stale, which answers each kind takes, the fixed notes,
and the per-number rule. It writes through existing functions only (`app/review/session.py`); the
route calls the other existing POSTs (wall review, no-countertop answers, architect pairing).

**The URL actions are the screen's five** (`confirm | fix | answer | agree | clear-not-checked`).
Every other one-click answer the list offers is sent as `answer`, with its own name in the body
(`{"answer": "checked_right"}`); the W1 names are also accepted directly in the URL.

**Stale is 409, unknown is 404, not yet is 501.** Each call names the result the screen showed
(`expected_finding_id`). A key that is no longer listed, or now names another result, is stale when
it, or the expected result, belongs to this package; otherwise it is unknown.

**A result held on several numbers is settled number by number.** Its numbers with a guess are
answered one by one, each with its own confirmation (W2, #1339; until then 501), and the next check
run decides the result. Its numbers checked by hand are answered with one finding action, which
would mark every number of the result done: so that answer waits until each numbered item of the
same result has its own confirmation (`numbers_answered`), and is refused with 409 before.

**What the person saw (#1358).** A group click names the open keys it was shown (`expected_keys`,
required; 409 when the group changed), and a Confirm on a number names the value shown
(`shown_value`, required; 409 when the stored value differs now). "Checked by hand: wrong" is a
DISMISS with the shared note `app.review.signed_record.CHECKED_WRONG_NOTE`, printed as WRONG.

**A cabinet run's marks (#648).** `mark_cabinets` names one kind per cabinet shown
(`cabinet_kinds`, left to right): 422 without them or with a kind not offered, 409 when the run now
has another number of cabinets. They are recorded through the existing classification path
(`workflow/classifications.py:record_classifications`, what `CAB-FILLER-001` reads), never a new one.

Source: issues #1351, #1358, #648. Verification: `tests/api/test_review_item_actions.py`,
`tests/api/test_review_item_cabinet_kinds.py`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Finding, ObservationCandidate, Package, PackageRevision
from app.models.document import PackageRevisionDocument
from app.models.evidence import CanonicalObservation, EvidenceSupportingCandidate
from app.models.review import ReviewAction, ReviewActionKind, ReviewSession
from app.review.review_items import (
    CABINET_RULE,
    CABINET_TYPE_INPUT,
    FailureOut,
    NotCheckedOut,
    ReviewItemOut,
    ReviewItemsOut,
    parsed_inches,
    review_items_for_revision,
)
from app.review.session import open_session, record_action
from app.review.signed_record import CHECKED_WRONG_NOTE
from verdict.operands import EvidenceStatus
from workflow.classifications import record_classifications

__all__ = [
    "AGREE_NOTE",
    "CHECKED_RIGHT_NOTE",
    "CHECKED_WRONG_NOTE",
    "NOT_CHECKED_FIXED_NOTE",
    "NOT_CHECKED_GROUP",
    "NUMBERS_NOT_YET",
    "PAGES_GROUP",
    "ItemRefused",
    "ReviewItemActionIn",
    "ReviewItemActionOut",
    "answer_name",
    "check_shown_value",
    "current_items",
    "find_entry",
    "numbers_answered",
    "record_cabinet_marks",
    "record_finding_actions",
    "reviewer_session",
]

#: The fixed notes each finding action carries. A person never types one here. "Checked by hand:
#: wrong" is the shared `app.review.signed_record.CHECKED_WRONG_NOTE`, which the signed record and
#: the cloud PDF read (#1358).
CHECKED_RIGHT_NOTE: Final = "Checked by hand: right (simple review)."
AGREE_NOTE: Final = "Agreed: this is wrong on the drawing and will be clouded (simple review)."
NOT_CHECKED_FIXED_NOTE: Final = "Not checked: the values were not read"

NUMBERS_NOT_YET: Final = "Confirming a number arrives with W2"
DEPTH_NOT_YET: Final = (
    "Confirming a depth or sink reading is not wired into the simple review yet. Use the depth "
    "card in the advanced view."
)

#: Group keys: the whole "Not checked" group, and every page listed with no countertop.
NOT_CHECKED_GROUP: Final = "not-checked"
PAGES_GROUP: Final = "no-countertop-pages"

#: Which answers each kind takes (the W1 list's own names, `app/review/review_items.py`), plus
#: `clear_not_checked`, the screen's name for settling a "Not checked" result.
_TAKES: Final[dict[str, frozenset[str]]] = {
    "number": frozenset({"confirm", "fix"}),
    "countertop_by_hand": frozenset({"checked_right", "checked_wrong"}),
    "other_result": frozenset({"checked_right", "checked_wrong"}),
    "wall_choice": frozenset({"choose_wall"}),
    "no_countertop_page": frozenset({"confirm_no_countertop", "there_is_a_countertop"}),
    "architect": frozenset({"answer"}),
    "depth_reading": frozenset({"confirm", "not_this"}),
    "cabinet_kinds": frozenset({"mark_cabinets"}),
    "failure": frozenset({"agree"}),
    "not_checked": frozenset({"clear_not_checked", "settle_group"}),
}

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

Entry = ReviewItemOut | FailureOut | NotCheckedOut


class ItemRefused(Exception):
    """An answer refused with a status code and a plain sentence."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ReviewItemActionIn(BaseModel):
    """One answer. Only the fields the answer needs are read; a person never types a note here."""

    model_config = ConfigDict(extra="forbid")

    expected_finding_id: UUID | None = Field(
        default=None,
        description=(
            "The `finding_id` of the item as the screen showed it. Required for an item with a "
            "result; 409 when the item now names another result or is no longer listed. Null for "
            "a page listed with no countertop and for a group key."
        ),
    )
    answer: str | None = Field(
        default=None,
        description=(
            "With the URL action `answer`: which answer, by the W1 list's own action name "
            "(`checked_right`, `checked_wrong`, `choose_wall`, `confirm_no_countertop`, "
            "`there_is_a_countertop`, `answer`, `not_this`). Defaults to `answer` for an "
            "architect item."
        ),
        max_length=64,
    )
    value: str | None = Field(
        default=None,
        max_length=64,
        description="With `fix`: the reading the person picked, with its unit (W2, #1339).",
    )
    shown_value: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "With `confirm` on a number: the value the screen showed (`best_guess.text`), e.g. "
            '`20"`. Required; 409 when the stored value differs now (#1358). Confirm records '
            "exactly the stored value, never another reading."
        ),
    )
    wall_config: str | None = Field(
        default=None,
        max_length=64,
        description="With `choose_wall`: one of the published wall layouts.",
    )
    architect_view: dict[str, object] | None = Field(
        default=None,
        description=(
            "With an architect `answer`: the body of `POST .../architect-view-match` "
            "(`view_id` or `none_of_these`, `expected_record_id`, optional `note`)."
        ),
    )
    architect_pairing: dict[str, object] | None = Field(
        default=None,
        description=(
            "With an architect `answer`: the body of `POST .../architect-pairing` (`pairs`, "
            "optional `expected_record_id` and `note`); `pairs: []` says nothing is comparable."
        ),
    )
    cabinet_kinds: list[str] | None = Field(
        default=None,
        max_length=200,
        description=(
            "With `mark_cabinets`: one kind per cabinet shown, left to right (a "
            "`cabinet_choices` category). Required; 409 when the run's cabinets changed."
        ),
    )
    expected_keys: list[str] | None = Field(
        default=None,
        max_length=1000,
        description=(
            "With a group key (`not-checked`, `no-countertop-pages`): the open keys the screen "
            "showed. Required for a group (422 without); 409 when the group differs now (#1358)."
        ),
    )


class ReviewItemActionOut(BaseModel):
    key: str
    action: str = Field(description="The answer recorded, by the W1 list's action name.")
    recorded: str = Field(description="What was recorded, in plain words.")
    review_session_id: UUID | None = Field(
        default=None, description="The review session a finding action was recorded in."
    )
    rechecking: bool = Field(
        description="True while the checks run again or wait to (this answer is read by them)."
    )
    review_items: ReviewItemsOut = Field(description="The list as it stands after the answer.")


def answer_name(url_action: str, body: ReviewItemActionIn, kind: str) -> str:
    """The answer's own name: the URL action, or with `answer` the body's name."""
    action = url_action.strip().lower().replace("-", "_")
    if action == "answer":
        if body.answer is not None:
            return body.answer.strip().lower().replace("-", "_")
        if kind == "architect":
            return "answer"
        raise ItemRefused(422, 'Say which answer in the body, e.g. {"answer": "checked_right"}.')
    return action


def current_items(
    session: Session, project_id: UUID, package_id: UUID
) -> tuple[PackageRevision, ReviewItemsOut]:
    revision = session.execute(
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        raise ItemRefused(404, "Not found")
    return revision, review_items_for_revision(
        session, project_id=project_id, package_id=package_id, revision=revision
    )


def _kind(entry: Entry) -> str:
    if isinstance(entry, FailureOut):
        return "failure"
    if isinstance(entry, NotCheckedOut):
        return "not_checked"
    return entry.kind


def _open_actions(entry: Entry) -> frozenset[str]:
    if isinstance(entry, NotCheckedOut):
        return _TAKES["not_checked"] if entry.status == "open" else frozenset()
    return frozenset(entry.actions)


def _belongs(session: Session, package_id: UUID, ids: Iterable[UUID]) -> bool:
    """Whether any id is a result or a reading of this package (any revision)."""
    named = list(ids)
    if not named:
        return False
    finding = session.execute(
        select(Finding.id)
        .join(PackageRevision, PackageRevision.id == Finding.package_revision_id)
        .where(Finding.id.in_(named), PackageRevision.package_id == package_id)
        .limit(1)
    ).first()
    if finding is not None:
        return True
    reading = session.execute(
        select(ObservationCandidate.id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == ObservationCandidate.document_version_id,
        )
        .where(ObservationCandidate.id.in_(named), PackageRevisionDocument.package_id == package_id)
        .limit(1)
    ).first()
    return reading is not None


def find_entry(
    session: Session,
    listed: ReviewItemsOut,
    *,
    package_id: UUID,
    key: str,
    action: str,
    expected_finding_id: UUID | None,
) -> tuple[Entry, str]:
    """The entry the key names now and its kind, refused when unknown, stale or not offered."""
    entries: list[Entry] = [*listed.items, *listed.failures, *listed.not_checked]
    entry = next((candidate for candidate in entries if candidate.key == key), None)
    if entry is None:
        named = [UUID(found) for found in _UUID.findall(key.lower())]
        if expected_finding_id is not None:
            named.append(expected_finding_id)
        if _belongs(session, package_id, named):
            raise ItemRefused(
                409,
                "This item changed since you opened it: the checks ran again or it was answered. "
                "Nothing was saved. Reload the list.",
            )
        raise ItemRefused(404, "Not found")
    kind = _kind(entry)
    if action not in _TAKES[kind]:
        offered = ", ".join(sorted(_TAKES[kind]))
        raise ItemRefused(422, f"This item does not take '{action}'. It takes: {offered}.")
    finding_id = entry.finding_id
    if finding_id is not None and expected_finding_id != finding_id:
        raise ItemRefused(
            409,
            "This item now belongs to a newer result than the one you saw. Nothing was saved. "
            "Reload the list.",
        )
    if action not in _open_actions(entry):
        why = (
            "It is waiting for the checks to run again."
            if entry.status == "rechecking"
            else "It was already answered."
        )
        raise ItemRefused(409, f"This item is not open. {why} Nothing was saved.")
    return entry, kind


def check_shown_value(entry: ReviewItemOut, shown_value: str | None) -> None:
    """A Confirm on a number names the value the person saw: 422 without it, 409 when the stored
    value shown now differs (#1358). Exact inches, so `20"` and `20 in` are the same value."""
    if shown_value is None or not shown_value.strip():
        raise ItemRefused(
            422,
            "Say which value you confirm (`shown_value`, the value shown). Nothing was saved.",
        )
    guess = entry.best_guess
    shown = None if guess is None or guess.value is None else guess.value
    seen = parsed_inches(shown_value)
    if (
        shown is None
        or seen is None
        or Fraction(int(shown.numerator), int(shown.denominator)) != seen
    ):
        raise ItemRefused(
            409,
            "The value shown for this number changed since you opened it. Nothing was saved. "
            "Reload the list.",
        )


def numbers_answered(session: Session, candidate_ids: Iterable[UUID]) -> set[UUID]:
    """The held numbers a person has already answered, each with its own confirmation: a
    HUMAN_CONFIRMED canonical observation citing the reading (W2's confirm and fix, #1339)."""
    ids = list(candidate_ids)
    if not ids:
        return set()
    return set(
        session.scalars(
            select(EvidenceSupportingCandidate.candidate_id)
            .join(
                CanonicalObservation,
                CanonicalObservation.id == EvidenceSupportingCandidate.canonical_observation_id,
            )
            .where(
                EvidenceSupportingCandidate.candidate_id.in_(ids),
                CanonicalObservation.status == EvidenceStatus.HUMAN_CONFIRMED.value,
            )
        )
    )


def refuse_until_numbers_answered(
    session: Session, listed: ReviewItemsOut, entry: ReviewItemOut
) -> None:
    """409 while the same result still waits on a number with its own answer to give."""
    waiting = {
        item.candidate_id
        for item in listed.items
        if item.kind == "number"
        and item.finding_id == entry.finding_id
        and item.key != entry.key
        and item.status == "open"
        and item.candidate_id is not None
    }
    open_numbers = waiting - numbers_answered(session, waiting)
    if open_numbers:
        count = len(open_numbers)
        raise ItemRefused(
            409,
            f"Answer the {count} other number{'s' if count != 1 else ''} on this countertop first: "
            "this answer would settle the whole countertop. Nothing was saved.",
        )


def reviewer_session(session: Session, revision_id: UUID, reviewer: str) -> ReviewSession:
    """The person's open review session on this revision, opened when there is none."""
    found = session.scalars(
        select(ReviewSession)
        .where(
            ReviewSession.package_revision_id == revision_id,
            ReviewSession.reviewer == reviewer,
            ReviewSession.completed_at.is_(None),
        )
        .order_by(ReviewSession.created_at.desc(), ReviewSession.id.desc())
        .limit(1)
    ).first()
    if found is not None:
        return found
    return open_session(session, package_revision_id=revision_id, reviewer=reviewer)


@dataclass(frozen=True, slots=True)
class FindingDecision:
    finding_id: UUID
    kind: ReviewActionKind
    note: str


#: The finding action each answer records, with its fixed note.
FINDING_ANSWERS: Final[dict[str, tuple[ReviewActionKind, str]]] = {
    "checked_right": (ReviewActionKind.CONFIRM, CHECKED_RIGHT_NOTE),
    "checked_wrong": (ReviewActionKind.DISMISS, CHECKED_WRONG_NOTE),
    "agree": (ReviewActionKind.CONFIRM, AGREE_NOTE),
    "clear_not_checked": (ReviewActionKind.DISMISS, NOT_CHECKED_FIXED_NOTE),
    "settle_group": (ReviewActionKind.DISMISS, NOT_CHECKED_FIXED_NOTE),
}


def record_finding_actions(
    session: Session,
    *,
    revision_id: UUID,
    actor: str,
    decisions: Sequence[FindingDecision],
) -> tuple[ReviewSession, list[ReviewAction]]:
    """The review-session finding actions, in the person's session, flushed (the caller commits)."""
    sitting = reviewer_session(session, revision_id, actor)
    recorded = [
        record_action(
            session,
            review_session_id=sitting.id,
            finding_id=decision.finding_id,
            action=decision.kind,
            actor=actor,
            note=decision.note,
        )
        for decision in decisions
    ]
    return sitting, recorded


Group = Literal["not-checked", "no-countertop-pages"]


def group_keys(listed: ReviewItemsOut, group: Group) -> list[str]:
    """The open keys a group key answers, in the list's order."""
    if group == NOT_CHECKED_GROUP:
        return [entry.key for entry in listed.not_checked if entry.status == "open"]
    return [
        item.key
        for item in listed.items
        if item.kind == "no_countertop_page" and item.status == "open"
    ]


def check_group(listed: ReviewItemsOut, group: Group, expected: list[str] | None) -> list[str]:
    """The group's open keys, only when they are exactly the ones the person saw (#1358): a group
    click names what was seen, so one result that arrived since is never answered unseen."""
    if expected is None:
        raise ItemRefused(
            422,
            "Say which items you saw in this group (`expected_keys`). Nothing was saved.",
        )
    keys = group_keys(listed, group)
    if not keys:
        raise ItemRefused(409, "Nothing in this group is waiting for an answer. Nothing was saved.")
    if sorted(expected) != sorted(keys):
        raise ItemRefused(
            409,
            "This group changed since you opened it. Nothing was saved. Reload the list.",
        )
    return keys


def record_cabinet_marks(
    session: Session,
    *,
    revision_id: UUID,
    entry: ReviewItemOut,
    kinds: list[str] | None,
    actor: str,
) -> tuple[str, ...]:
    """Record one kind per cabinet shown through the existing classification path (flushed, the
    caller commits): exactly the cabinets on the screen, each one of the kinds offered."""
    if not kinds:
        raise ItemRefused(
            422, "Mark every cabinet (`cabinet_kinds`, left to right). Nothing was saved."
        )
    if len(kinds) != len(entry.cabinets):
        raise ItemRefused(
            409,
            f"This run now has {len(entry.cabinets)} cabinets, not {len(kinds)}. Nothing was saved. "
            "Reload the list.",
        )
    offered = {choice.category for choice in entry.cabinet_choices}
    for position, kind in enumerate(kinds, start=1):
        if kind not in offered:
            raise ItemRefused(
                422,
                f"Cabinet {position}: {kind!r} is not one of {', '.join(sorted(offered))}. "
                "Nothing was saved.",
            )
    record_classifications(
        session,
        package_revision_id=revision_id,
        rule_id=CABINET_RULE,
        input_name=CABINET_TYPE_INPUT,
        categories=kinds,
        confirmed_by=actor,
    )
    session.flush()
    return tuple(kinds)
