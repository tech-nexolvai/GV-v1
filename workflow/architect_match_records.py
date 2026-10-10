"""The architect view match records, read and appended without reading a drawing (#1166).

`workflow/architect_matching.py` makes a match (code, then both AIs) and so imports the drawing
readers. Everything here only reads and appends the stored records, so the control plane may import
it: the reviewer picks a view with one click (`app/api/architect_matches.py`) and the countertop
results read every row's match. `tests/api/test_no_heavy_work.py` keeps it that way. Imports:
SQLAlchemy, `app.models`, the match contract and the standard library only.

* `persist_architect_matches`: one `automatic` (or `carried`) record per vendor row the run matched;
* `effective_architect_match` / `effective_architect_matches`: the record that counts for a row —
  its chain's newest record, a reviewer's pick above all because it supersedes — with the view it
  names; the batched form answers any number of rows in three statements;
* `matched_architect_view`: the view a row is compared against, only when the match is
  `auto_matched`, `reviewer_confirmed` or `carried_over`;
* `record_reviewer_match`: a reviewer's pick or "none of these", superseding the latest record,
  refused when stale (`ReviewerMatchStale`, 409) or not allowed (`ReviewerMatchRefused`, 422);
* `remembered_matches`: what a person decided for this revision and the ones it supersedes, for
  carrying a decision to the next run of the identical vendor item (decision D3: never pre-selected
  otherwise).

Source: issue #1166 · Verification: `tests/workflow/test_architect_match_records.py`,
`tests/api/test_architect_matches.py`
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Final, Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DocumentVersion, ObservationCandidate, PackageRevision, Page
from app.models.evidence import ArchitectViewIndexEntry, ArchitectViewMatchRecord
from app.models.runs import ModelInvocation
from workflow.architect_match_contract import (
    MATCHED_STATUSES,
    EffectiveMatch,
    MatchedView,
    MatchSource,
    MatchStatus,
    RowMatch,
)

__all__ = [
    "ARCHITECT_FILE_NAME",
    "VENDOR_ROW_Y_SLACK_PT",
    "RememberedMatch",
    "ReviewerMatchRefused",
    "ReviewerMatchStale",
    "effective_architect_match",
    "effective_architect_matches",
    "latest_match_record",
    "matched_architect_view",
    "matched_views",
    "persist_architect_matches",
    "record_reviewer_match",
    "remembered_matches",
    "same_vendor_item",
]

#: How a result names the architect's file. The upload keeps no file name, only its kind, so the
#: architect's file is named by what it is.
ARCHITECT_FILE_NAME: Final = "the architect's drawings"

#: Two readings of a vendor row are the same item's row when their dimension lines are this close,
#: in page points (decision D3's "row line y within 1 pt").
VENDOR_ROW_Y_SLACK_PT: Final = Decimal(1)

_NOTE_LIMIT: Final = 500
_DECIDED: Final = frozenset({"reviewer", "carried"})


class ReviewerMatchRefused(ValueError):
    """A reviewer's pick that may not be recorded, with the reason in plain words (422)."""


class ReviewerMatchStale(ValueError):
    """The row's match changed after the reviewer looked at it: reload, then pick again (409)."""


class _MatchedResult(Protocol):
    """What persisting reads of a `workflow.slot_reader.PageSlotResult`."""

    @property
    def page_id(self) -> UUID: ...

    @property
    def owner_candidate_ids(self) -> Mapping[str, UUID]: ...

    @property
    def architect_match(self) -> RowMatch | None: ...


# --- reading -------------------------------------------------------------------------------------


def matched_views(session: Session, view_ids: Collection[UUID]) -> dict[UUID, MatchedView]:
    """The architect views named, as a person and the report name them, in one statement."""
    if not view_ids:
        return {}
    rows = session.execute(
        select(ArchitectViewIndexEntry, Page.index)
        .join(Page, Page.id == ArchitectViewIndexEntry.page_id)
        .where(ArchitectViewIndexEntry.id.in_(tuple(view_ids)))
    ).all()
    return {entry.id: _matched_view(entry, index) for entry, index in rows}


def _matched_view(entry: ArchitectViewIndexEntry, page_index: int) -> MatchedView:
    return MatchedView(
        view_id=entry.id,
        document_version_id=entry.document_version_id,
        page_id=entry.page_id,
        page_number=page_index + 1,
        view_number=entry.view_number,
        view_tag=entry.view_tag,
        title=entry.title,
        bubble=entry.bubble,
        sheet_number=entry.sheet_number,
        scale_note=entry.scale_note,
        file_name=ARCHITECT_FILE_NAME,
        separated=entry.separated,
    )


def latest_match_record(session: Session, row_anchor_id: UUID) -> ArchitectViewMatchRecord | None:
    """The newest record of the row's chain: its tip (each record is superseded at most once and
    a row has one root, so the newest is the tip)."""
    return session.execute(
        select(ArchitectViewMatchRecord)
        .where(ArchitectViewMatchRecord.row_anchor_candidate_id == row_anchor_id)
        .order_by(ArchitectViewMatchRecord.created_at.desc(), ArchitectViewMatchRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _effective(record: ArchitectViewMatchRecord, view: MatchedView | None) -> EffectiveMatch:
    reasons = record.reasons if isinstance(record.reasons, list) else []
    return EffectiveMatch(
        record_id=record.id,
        status=cast(MatchStatus, record.status),
        source=cast(MatchSource, record.source),
        matched=view,
        needs_reviewer=record.status == "needs_reviewer",
        reasons=tuple(str(reason) for reason in reasons),
        decided_by=record.decided_by,
    )


def effective_architect_matches(
    session: Session, row_anchor_ids: Collection[UUID]
) -> dict[UUID, EffectiveMatch | None]:
    """`effective_architect_match` for many rows at once, in at most two statements."""
    answers: dict[UUID, EffectiveMatch | None] = dict.fromkeys(row_anchor_ids)
    if not answers:
        return answers
    newest: dict[UUID, ArchitectViewMatchRecord] = {}
    for record in session.scalars(
        select(ArchitectViewMatchRecord)
        .where(ArchitectViewMatchRecord.row_anchor_candidate_id.in_(tuple(answers)))
        .order_by(ArchitectViewMatchRecord.created_at.desc(), ArchitectViewMatchRecord.id.desc())
    ):
        newest.setdefault(record.row_anchor_candidate_id, record)
    views = matched_views(
        session,
        {record.matched_view_id for record in newest.values() if record.matched_view_id},
    )
    for anchor_id, record in newest.items():
        answers[anchor_id] = _effective(
            record, None if record.matched_view_id is None else views.get(record.matched_view_id)
        )
    return answers


def effective_architect_match(session: Session, row_anchor_id: UUID) -> EffectiveMatch | None:
    """The match that counts for one vendor row, or `None` when the row was never matched (a
    combined sheet, or a run from before the architect file was indexed)."""
    return effective_architect_matches(session, (row_anchor_id,))[row_anchor_id]


def matched_architect_view(session: Session, row_anchor_id: UUID) -> MatchedView | None:
    """The view a row is compared against: only for `auto_matched`, `reviewer_confirmed` and
    `carried_over`. `not_separated` names a view whose dimensions were not read: `None`."""
    effective = effective_architect_match(session, row_anchor_id)
    if effective is None or effective.status not in MATCHED_STATUSES:
        return None
    return effective.matched


# --- storing a run's matches ---------------------------------------------------------------------


def _invocations(session: Session, extraction_run_id: UUID) -> dict[str, dict[str, str]]:
    """The answering attempt of each reader for each match question in the run, by packet hash
    then model."""
    found: dict[str, dict[str, str]] = {}
    rows = session.execute(
        select(ModelInvocation.id, ModelInvocation.model_id, ModelInvocation.reader_question_packet)
        .where(
            ModelInvocation.extraction_run_id == extraction_run_id,
            ModelInvocation.outcome == "ok",
            ModelInvocation.reader_question_packet["question_id"].astext.like("%:arch-match"),
        )
        .order_by(ModelInvocation.reader_attempt_number)
    ).all()
    for invocation_id, model_id, packet in rows:
        digest = packet.get("packet_sha256") if isinstance(packet, dict) else None
        if isinstance(digest, str):
            found.setdefault(digest, {})[model_id] = str(invocation_id)
    return found


def _uuid(value: object) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def persist_architect_matches(
    session: Session,
    *,
    package_revision_id: UUID,
    extraction_run_id: UUID,
    results: Sequence[_MatchedResult],
) -> int:
    """One record per vendor row the run matched (`automatic`, or `carried` from a person's earlier
    decision). Append-only; each AI pick gets the id of the call that answered it."""
    session.flush()
    invocations = _invocations(session, extraction_run_id)
    count = 0
    for result in results:
        match = result.architect_match
        anchor = result.owner_candidate_ids.get("slot:0")
        if match is None or anchor is None:
            continue
        packet = match.question_packet
        packet_sha = None if packet is None else packet.get("packet_sha256")
        answered = invocations.get(packet_sha, {}) if isinstance(packet_sha, str) else {}
        ai_picks = [
            {**pick, "invocation_id": answered.get(str(pick.get("model_id")))}
            for pick in match.ai_picks
        ]
        details = dict(match.details or {})
        if packet is not None:
            details["packet_sha256"] = packet_sha
            images = packet.get("images")
            if isinstance(images, Mapping):
                details["picture"] = images.get("arch_match_view")
            details["common_scale"] = packet.get("common_scale")
        carried_from = _uuid(details.get("carried_from_id")) if match.source == "carried" else None
        session.add(
            ArchitectViewMatchRecord(
                package_revision_id=package_revision_id,
                vendor_page_id=result.page_id,
                row_anchor_candidate_id=anchor,
                extraction_run_id=extraction_run_id,
                source=match.source,
                status=match.status,
                matched_view_id=None if match.chosen is None else match.chosen.view_id,
                code_verdict=str(match.code.verdict),
                code_pick_view_id=_uuid(match.code.pick),
                ai_picks=ai_picks,
                candidates=[dict(candidate) for candidate in match.candidate_json],
                vendor_title=_text(details.pop("vendor_title", None), 300),
                vendor_references=_references(details.pop("vendor_references", None)),
                reasons=list(match.reasons),
                details=details,
                supersedes_id=None,
                carried_from_id=carried_from,
                decided_by=None,
                note=None,
            )
        )
        count += 1
    session.flush()
    return count


def _references(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, (list, tuple)) else []


def _text(value: object, limit: int) -> str | None:
    return None if not isinstance(value, str) or not value.strip() else value[:limit]


# --- the reviewer's pick -------------------------------------------------------------------------


def record_reviewer_match(
    session: Session,
    *,
    anchor: ObservationCandidate,
    package_revision_id: UUID,
    view_id: UUID | None,
    none_of_these: bool,
    note: str | None,
    actor: str,
    expected_record_id: UUID | None,
) -> ArchitectViewMatchRecord:
    """Append a reviewer's pick for one row, superseding its latest record (not committed).

    Exactly one of `view_id` and `none_of_these`. A view must be one of the row's candidates (a
    view of this revision's architect file); a view not clearly apart from its neighbour is
    recorded `not_separated` (it names the view, and nothing in it was read). Refused when stale:
    `expected_record_id` given and the row's latest record is another one.
    """
    current = latest_match_record(session, anchor.id)
    if expected_record_id is not None and (current is None or current.id != expected_record_id):
        raise ReviewerMatchStale(
            "This row's architect view match changed after you opened it. Reload it before "
            "choosing again."
        )
    if current is None:
        raise ReviewerMatchRefused(
            "This row has not been matched with the architect's drawings yet. Run the checks "
            "again, then choose."
        )
    if current.package_revision_id != package_revision_id:
        raise ReviewerMatchRefused("This row belongs to another revision of the package.")
    if (view_id is None) == (not none_of_these):
        raise ReviewerMatchRefused("Choose one of the architect's views, or 'none of these'.")
    if not actor.strip():
        raise ReviewerMatchRefused("A reviewer's choice must name the reviewer.")
    view: ArchitectViewIndexEntry | None = None
    if view_id is not None:
        offered = {
            str(candidate.get("view_id"))
            for candidate in current.candidates
            if isinstance(candidate, dict)
        }
        if str(view_id) not in offered:
            raise ReviewerMatchRefused(
                "That view is not one of the architect's views offered for this countertop."
            )
        view = session.get(ArchitectViewIndexEntry, view_id)
        if view is None:
            raise ReviewerMatchRefused("That architect view no longer exists.")
    if view is None:
        status = "none_matches"
        reasons = [
            "The reviewer found no view in the architect's drawings that shows this countertop."
        ]
    elif not view.separated:
        status = "not_separated"
        reasons = [
            (
                "The reviewer chose an architect view that is not clearly apart from its neighbour, "
                "so its dimensions were not read."
            )
        ]
    else:
        status = "reviewer_confirmed"
        reasons = ["The reviewer chose this architect view."]
    record = ArchitectViewMatchRecord(
        package_revision_id=package_revision_id,
        vendor_page_id=current.vendor_page_id,
        row_anchor_candidate_id=anchor.id,
        extraction_run_id=None,
        source="reviewer",
        status=status,
        matched_view_id=None if view is None else view.id,
        code_verdict=current.code_verdict,
        code_pick_view_id=current.code_pick_view_id,
        ai_picks=list(current.ai_picks),
        candidates=list(current.candidates),
        vendor_title=current.vendor_title,
        vendor_references=list(current.vendor_references),
        reasons=reasons,
        details={**current.details, "decided_on": str(current.id)},
        supersedes_id=current.id,
        carried_from_id=None,
        decided_by=actor,
        note=None if note is None else note[:_NOTE_LIMIT],
    )
    session.add(record)
    return record


# --- remembered for the next run (D3) ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RememberedMatch:
    """A person's decision (or one carried from it) for a vendor item, to carry to its next run."""

    record_id: UUID
    package_revision_id: UUID
    revision_number: int
    status: str
    vendor_item_key: Mapping[str, object]
    """`{page_content_hash, page_index, pieces, row_y_pt}` of the vendor row it was made on."""
    view_key: tuple[str, str, str] | None
    """`(architect document sha256, architect page content hash, view tag)` of the view it names;
    `None` for "none of these"."""
    created_at: datetime


def remembered_matches(session: Session, package_revision_id: UUID) -> tuple[RememberedMatch, ...]:
    """Every standing person's decision on this revision and the revisions it supersedes, newest
    revision first, newest decision first: the tip of each row's chain, when a person made it
    (`reviewer`) or it was carried from one (`carried`)."""
    chain: list[tuple[UUID, int]] = []
    seen: set[UUID] = set()
    current: UUID | None = package_revision_id
    while current is not None and current not in seen:
        seen.add(current)
        revision = session.get(PackageRevision, current)
        if revision is None:
            break
        chain.append((revision.id, revision.revision_number))
        current = revision.supersedes_id
    numbers = dict(chain)
    records = session.scalars(
        select(ArchitectViewMatchRecord)
        .where(ArchitectViewMatchRecord.package_revision_id.in_(tuple(numbers)))
        .order_by(ArchitectViewMatchRecord.created_at.desc(), ArchitectViewMatchRecord.id.desc())
    ).all()
    tips: dict[UUID, ArchitectViewMatchRecord] = {}
    for record in records:
        tips.setdefault(record.row_anchor_candidate_id, record)
    decided = [record for record in tips.values() if record.source in _DECIDED]
    view_ids = {record.matched_view_id for record in decided if record.matched_view_id}
    keys: dict[UUID, tuple[str, str, str]] = {}
    if view_ids:
        for entry_id, sha, content_hash, tag in session.execute(
            select(
                ArchitectViewIndexEntry.id,
                DocumentVersion.sha256,
                Page.content_hash,
                ArchitectViewIndexEntry.view_tag,
            )
            .join(
                DocumentVersion, DocumentVersion.id == ArchitectViewIndexEntry.document_version_id
            )
            .join(Page, Page.id == ArchitectViewIndexEntry.page_id)
            .where(ArchitectViewIndexEntry.id.in_(tuple(view_ids)))
        ):
            keys[entry_id] = (sha, content_hash, tag)
    order = {revision_id: position for position, (revision_id, _n) in enumerate(chain)}
    remembered = [
        RememberedMatch(
            record_id=record.id,
            package_revision_id=record.package_revision_id,
            revision_number=numbers[record.package_revision_id],
            status=record.status,
            vendor_item_key=_item_key(record.details.get("vendor_item_key")),
            view_key=None if record.matched_view_id is None else keys.get(record.matched_view_id),
            created_at=record.created_at,
        )
        for record in decided
        if isinstance(record.details.get("vendor_item_key"), dict)
        and (record.matched_view_id is None or record.matched_view_id in keys)
    ]
    remembered.sort(
        key=lambda item: (order[item.package_revision_id], -item.created_at.timestamp())
    )
    return tuple(remembered)


def _item_key(value: object) -> dict[str, object]:
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def same_vendor_item(
    remembered: Mapping[str, object], current: Mapping[str, object]
) -> tuple[bool, bool]:
    """`(the same item, identical)`: the same item when the page is the same (its content, or its
    place in the vendor's file) with the same number of pieces and the row's line within
    `VENDOR_ROW_Y_SLACK_PT`; identical when the page's content is also exactly the same."""
    same_page = remembered.get("page_content_hash") == current.get(
        "page_content_hash"
    ) or remembered.get("page_index") == current.get("page_index")
    then, now = _decimal(remembered.get("row_y_pt")), _decimal(current.get("row_y_pt"))
    same_row = (
        remembered.get("pieces") == current.get("pieces")
        and then is not None
        and now is not None
        and abs(then - now) <= VENDOR_ROW_Y_SLACK_PT
    )
    same = same_page and same_row
    identical = same and remembered.get("page_content_hash") == current.get("page_content_hash")
    return same, identical
