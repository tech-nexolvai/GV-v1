"""Carry a person's answers from revision N to revision N+1 where the thing answered is unchanged (#1180).

A vendor's resubmission is usually a small change to a large set. Asking the reviewer every wall
question and every drawing role again, on drawings that did not change, is work the review already
did. This module copies those answers forward, **only where the record proves the thing they were
about is identical**, and says on every copy where it came from.

**What is carried, and on what identity.**

* **Run-scope job values** (`carry_run_values`): the RUN-layer settings a reviewer typed for this job
  that are not read off the drawing — a sink's cut-sheet size and the like (names with no `rule_id:`
  prefix). They go into revision N+1's first RUN set when it starts, each value the earlier row's
  number, unit, person, time and provenance unchanged.
  **Measurements typed off the drawing (`rule_id:name`) are never carried**: the drawing changed, and a
  stale width checked against a changed drawing is a PASS nobody measured. Classifications are drawing
  answers too, and stay behind.
* **Wall answers and typed missing widths** (`SlotRowReviewDecision`), once revision N+1 has been read:
  onto a countertop row whose stable identity — **the page's content hash, the page number, the row
  rank** — piece count, reader version and every value read equal a row of revision N that has an
  answer, and the row's line sits within a point of it (`row_identity`, `row_y_pt`). A page whose
  content is unknown (no hash, or the hash of no bytes) never matches, and a row two earlier rows
  could be is asked again. The page hash rather than the file's: the vendor re-issues the whole PDF, so a
  file hash never matches, while a byte-identical page read to the same numbers is the same row. An
  unchanged file is the same document version, so through #1112 it is read from the same stored
  answers and its rows match by construction.
* **A person's drawing-role confirmation**, once read: onto a view of a re-issued drawing whose page is
  byte-identical to the earlier one (same document, page index, page content hash, view tag). An
  unchanged drawing needs nothing: its pages, views and roles are the same rows (ADR-0018).
  Code-made roles are not copied; code makes them again from the page itself.

**Not here, on purpose.** Architect-view picks carry through #1166's own rule (D3), which already
walks `supersedes_id`. Reviewer decisions on results do not cross revisions (`app/review/carry_over.py`
fingerprints include the revision); within revision N+1 they carry through the normal carry-over after
its first check run.

**Honest record.** Every carried record is a new append-only row naming the original
(`carried_from_id`, migration 0089); the person it names is the person who gave the answer, and the
audit event names this module as the one that copied it. Running it twice changes nothing: a row or
view that already has an answer is skipped.

Verification: `tests/api/test_revisions.py`
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.events import AuditCategory, emit
from app.models import (
    DocumentVersion,
    DrawingView,
    ObservationCandidate,
    PackageRevision,
    PackageRevisionDocument,
    Page,
    SlotRowReviewDecision,
    ViewRoleConfirmation,
)
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import ParameterValue as StoredParameterValue
from app.models.parameters import from_rows, to_rows
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from rules.parameters import ParameterLayer
from rules.parameters import ParameterSet as InMemoryParameterSet
from workflow.architect_match_records import VENDOR_ROW_Y_SLACK_PT
from workflow.slot_row_scope import SlotRow, slot_rows
from workflow.view_roles import CODE_CONFIRMERS, revision_views

__all__ = [
    "CARRY_FORWARD_ACTOR",
    "CarriedOrigin",
    "CarryForwardReport",
    "carried_answers_driving_results",
    "carry_after_reading",
    "carry_run_values",
    "role_origins",
    "row_identity",
    "wall_answer_origins",
]

#: Who the audit record names as having copied an answer forward. Never a person: the person is on
#: the carried record itself, as the one who gave the answer.
CARRY_FORWARD_ACTOR: Final = "revision carry-forward (#1180)"

#: How a RUN-layer name that belongs to a rule input is written (`workflow/measurements.py`).
_RULE_INPUT_SEPARATOR: Final = ":"


@dataclass(frozen=True, slots=True)
class CarryForwardReport:
    """What one pass carried, in counts a person can read back."""

    wall_answers: int = 0
    drawing_roles: int = 0

    def as_payload(self) -> dict[str, int]:
        return {"wall_answers": self.wall_answers, "drawing_roles": self.drawing_roles}


# --- run-scope job values ------------------------------------------------------------------------


def carry_run_values(
    session: Session, *, project_id: UUID, prior_revision_id: UUID, new_revision_id: UUID
) -> int:
    """Copy the prior revision's run-scope job values into the new revision's first RUN set.

    Returns how many values were carried; `0` when the prior revision has none, or the new one
    already holds a RUN set (nothing is ever merged into a set a person saved). Commits nothing.
    """
    if (
        session.scalar(
            select(StoredParameterSet.id)
            .where(
                StoredParameterSet.layer == ParameterLayer.RUN.value,
                StoredParameterSet.package_revision_id == new_revision_id,
            )
            .limit(1)
        )
        is not None
    ):
        return 0
    previous = session.scalar(
        select(StoredParameterSet)
        .where(
            StoredParameterSet.project_id == project_id,
            StoredParameterSet.layer == ParameterLayer.RUN.value,
            StoredParameterSet.package_revision_id == prior_revision_id,
        )
        .order_by(StoredParameterSet.version.desc())
        .limit(1)
    )
    if previous is None:
        return 0
    rows = list(
        session.scalars(
            select(StoredParameterValue).where(StoredParameterValue.parameter_set_id == previous.id)
        )
    )
    plain = {
        name: value
        for name, value in from_rows(previous, rows).parameters.items()
        if _RULE_INPUT_SEPARATOR not in name
    }
    if not plain:
        return 0
    next_version = (
        session.scalar(
            select(StoredParameterSet.version)
            .where(
                StoredParameterSet.project_id == project_id,
                StoredParameterSet.layer == ParameterLayer.RUN.value,
            )
            .order_by(StoredParameterSet.version.desc())
            .limit(1)
        )
        or 0
    ) + 1
    carried = InMemoryParameterSet(
        project_id=str(project_id),
        layer=ParameterLayer.RUN,
        version=next_version,
        parameters=plain,
    )
    stored, values = to_rows(carried, package_revision_id=new_revision_id)
    session.add(stored)
    session.flush()
    for value in values:
        session.add(value)
    session.flush()
    return len(values)


# --- after the new revision is read --------------------------------------------------------------


def _rank(row: SlotRow) -> str | None:
    return next(
        (
            flag.removeprefix("row-rank:")
            for flag in row.anchor.ambiguity_flags or ()
            if flag.startswith("row-rank:")
        ),
        None,
    )


def _read_values(row: SlotRow) -> tuple[tuple[str, int | None, int | None, str | None], ...]:
    """Every slot the row read, with its exact value: `(slot, numerator, denominator, unit)`."""
    values = []
    for candidate in row.candidates:
        slot = next(
            (flag for flag in candidate.ambiguity_flags or () if flag.startswith("slot:")), None
        )
        if slot is None:
            continue
        values.append(
            (slot, candidate.value_numerator, candidate.value_denominator, candidate.unit)
        )
    return tuple(sorted(values, key=lambda value: (value[0], str(value[1:]))))


RowIdentity = tuple[
    str, int, str, int, str, tuple[tuple[str, int | None, int | None, str | None], ...]
]

#: The SHA-256 of no bytes at all: what a page whose content could not be read hashes to
#: (`extraction/reader.py` hashes `b""` on a failure). Two such pages are not "the same page".
EMPTY_CONTENT_HASH: Final = hashlib.sha256(b"").hexdigest()

#: Two reads of one countertop row put its line this close, in page points (as #1166's D3 rule,
#: `workflow/architect_match_records.VENDOR_ROW_Y_SLACK_PT`).
ROW_Y_SLACK_PT: Final = VENDOR_ROW_Y_SLACK_PT


def known_content(content_hash: str | None) -> bool:
    """A page content hash that identifies real content: present, and not the hash of nothing."""
    return bool(content_hash) and content_hash != EMPTY_CONTENT_HASH


def row_identity(
    row: SlotRow, page_content_hash: str | None, reader_version: str | None = None
) -> RowIdentity | None:
    """The same countertop row on another read: the same page bytes (its content hash) at the same
    page number, the same row rank, the same piece count, the same reader version and the same
    values read in every slot. The row's line must also sit within `ROW_Y_SLACK_PT` (`row_y_pt`).

    `None` when the row carries no rank or the page's content is unknown: nothing then identifies
    it across reads, and it is asked again.

    The page's content hash rather than the whole file's: a vendor re-issues the whole PDF, so a
    file hash would never match and every unchanged page would be asked again. A byte-identical page
    read by the same reader to the same row with the same numbers is the same countertop.
    """
    rank = _rank(row)
    if rank is None or not known_content(page_content_hash) or reader_version is None:
        return None
    assert page_content_hash is not None
    return (
        page_content_hash,
        row.page_number,
        rank,
        row.piece_count,
        reader_version,
        _read_values(row),
    )


def row_y_pt(row: SlotRow, dpi: int | None) -> Decimal | None:
    """Where the row's line sits on the page, in points: the middle of its first piece's box."""
    polygon = row.anchor.polygon
    if not dpi or not isinstance(polygon, list) or not polygon:
        return None
    try:
        ys = [Decimal(str(point[1])) for point in polygon]
    except (TypeError, IndexError, ArithmeticError):
        return None
    return (sum(ys, Decimal(0)) / Decimal(len(ys))) * Decimal(72) / Decimal(dpi)


def _page_hashes(session: Session, rows: Iterable[SlotRow]) -> dict[UUID, str]:
    page_ids = {row.anchor.page_id for row in rows}
    if not page_ids:
        return {}
    return {
        page_id: content_hash
        for page_id, content_hash in session.execute(
            select(Page.id, Page.content_hash).where(Page.id.in_(page_ids))
        )
    }


def _readers(session: Session, rows: Iterable[SlotRow]) -> dict[UUID, tuple[str, int | None]]:
    """`{extraction run: (reader version, dpi)}` for the runs that read these rows."""
    run_ids = {row.anchor.extraction_run_id for row in rows}
    if not run_ids:
        return {}
    return {
        run_id: (version, dpi)
        for run_id, version, dpi in session.execute(
            select(ExtractionRun.id, ExtractionRun.extractor_version, ExtractionRun.dpi).where(
                ExtractionRun.id.in_(run_ids)
            )
        )
    }


def _carry_wall_answers(session: Session, prior_id: UUID, revision_id: UUID) -> int:
    earlier = slot_rows(session, prior_id)
    answered = [row for row in earlier if row.decision is not None]
    if not answered:
        return 0
    current = slot_rows(session, revision_id)
    hashes = _page_hashes(session, (*answered, *current))
    readers = _readers(session, (*answered, *current))

    def placed(row: SlotRow) -> tuple[RowIdentity | None, Decimal | None]:
        version, dpi = readers.get(row.anchor.extraction_run_id, (None, None))
        return (
            row_identity(row, hashes.get(row.anchor.page_id), version),
            row_y_pt(row, dpi),
        )

    remembered: list[tuple[RowIdentity, Decimal, SlotRowReviewDecision]] = []
    for row in answered:
        key, y = placed(row)
        if key is not None and y is not None and row.decision is not None:
            remembered.append((key, y, row.decision))
    carried = 0
    for row in current:
        if row.decision is not None:
            continue
        key, y = placed(row)
        if key is None or y is None:
            continue
        matches = [
            decision
            for earlier_key, earlier_y, decision in remembered
            if earlier_key == key and abs(earlier_y - y) <= ROW_Y_SLACK_PT
        ]
        # Two earlier rows this row could be is no identity at all: it is asked again.
        if len(matches) != 1:
            continue
        source = matches[0]
        record = SlotRowReviewDecision(
            row_candidate_id=row.anchor.id,
            supersedes_id=None,
            wall_config=source.wall_config,
            measurements=dict(source.measurements),
            confirmed_by=source.confirmed_by,
            carried_from_id=source.id,
        )
        session.add(record)
        session.flush()
        emit(
            session,
            category=AuditCategory.REVIEW_ACTION,
            actor=CARRY_FORWARD_ACTOR,
            target_id=record.id,
            target_type="slot_row_review_decision",
        )
        carried += 1
    return carried


def _view_key(
    session: Session, views: Iterable[DrawingView]
) -> dict[UUID, tuple[UUID, int, str, str]]:
    """`{view id: (document, page index, page content hash, tag)}`."""
    page_ids = {view.page_id for view in views}
    if not page_ids:
        return {}
    pages = {
        page_id: (document_id, index, content_hash)
        for page_id, document_id, index, content_hash in session.execute(
            select(Page.id, DocumentVersion.document_id, Page.index, Page.content_hash)
            .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
            .where(Page.id.in_(page_ids))
        )
    }
    return {view.id: (*pages[view.page_id], view.tag) for view in views if view.page_id in pages}


def _latest_confirmations(
    session: Session, view_ids: Iterable[UUID]
) -> dict[UUID, ViewRoleConfirmation]:
    ids = list(set(view_ids))
    if not ids:
        return {}
    latest: dict[UUID, ViewRoleConfirmation] = {}
    for confirmation in session.scalars(
        select(ViewRoleConfirmation)
        .where(ViewRoleConfirmation.drawing_view_id.in_(ids))
        .order_by(ViewRoleConfirmation.created_at.desc(), ViewRoleConfirmation.id.desc())
    ):
        latest.setdefault(confirmation.drawing_view_id, confirmation)
    return latest


def _carry_drawing_roles(session: Session, prior_id: UUID, revision_id: UUID) -> int:
    earlier = [entry.view for entry in revision_views(session, prior_id)]
    current = [entry.view for entry in revision_views(session, revision_id)]
    earlier_ids = {view.id for view in earlier}
    # A view both revisions share is the same row, and its role is already the same.
    fresh = [view for view in current if view.id not in earlier_ids]
    if not fresh or not earlier:
        return 0
    keys = _view_key(session, (*earlier, *fresh))
    confirmed = _latest_confirmations(session, earlier_ids)
    remembered: dict[tuple[UUID, int, str, str], ViewRoleConfirmation] = {}
    for view in earlier:
        confirmation = confirmed.get(view.id)
        if confirmation is None or confirmation.confirmed_by in CODE_CONFIRMERS:
            continue
        # A page whose content is unknown is never "the same page" (#1180 review).
        if view.id in keys and known_content(keys[view.id][2]):
            remembered[keys[view.id]] = confirmation
    if not remembered:
        return 0
    already = _latest_confirmations(session, (view.id for view in fresh))
    carried = 0
    for view in fresh:
        source = remembered.get(keys.get(view.id, (UUID(int=0), -1, "", "")))
        if source is None or view.id in already or view.role is not None:
            continue
        record = ViewRoleConfirmation(
            drawing_view_id=view.id,
            role=source.role,
            confirmed_by=source.confirmed_by,
            carried_from_id=source.id,
        )
        session.add(record)
        view.role = source.role
        session.flush()
        emit(
            session,
            category=AuditCategory.REVIEW_ACTION,
            actor=CARRY_FORWARD_ACTOR,
            target_id=record.id,
            target_type="view_role_confirmation",
        )
        carried += 1
    return carried


def carry_after_reading(session: Session, revision_id: UUID) -> CarryForwardReport:
    """Carry wall answers and drawing roles from the revision this one superseded. Commits nothing.

    A revision that supersedes nothing (revision 1) carries nothing. Idempotent.
    """
    revision = session.get(PackageRevision, revision_id)
    if revision is None or revision.supersedes_id is None:
        return CarryForwardReport()
    roles = _carry_drawing_roles(session, revision.supersedes_id, revision_id)
    walls = _carry_wall_answers(session, revision.supersedes_id, revision_id)
    return CarryForwardReport(wall_answers=walls, drawing_roles=roles)


# --- where a carried answer came from (for the screens and the signed PDF) -----------------------


@dataclass(frozen=True, slots=True)
class CarriedOrigin:
    """The answer a carried record copies: the revision it was given on, by whom, and when."""

    revision_number: int
    given_by: str
    given_at: datetime


def _roots(session: Session, model: type[Any], records: Iterable[Any]) -> dict[UUID, Any]:
    """`{carried record id: the original record}`, following `carried_from_id` to the first one."""
    roots: dict[UUID, Any] = {}
    for record in records:
        if record.carried_from_id is None:
            continue
        root = record
        seen: set[UUID] = set()
        while root.carried_from_id is not None and root.id not in seen:
            seen.add(root.id)
            earlier = session.get(model, root.carried_from_id)
            if earlier is None:
                break
            root = earlier
        roots[record.id] = root
    return roots


def wall_answer_origins(
    session: Session, decisions: Iterable[SlotRowReviewDecision | None]
) -> dict[UUID, CarriedOrigin]:
    """For each carried wall answer: the revision the person gave it on, who, and when."""
    roots = _roots(session, SlotRowReviewDecision, (d for d in decisions if d is not None))
    if not roots:
        return {}
    anchors = {root.row_candidate_id for root in roots.values()}
    numbers = {
        candidate_id: number
        for candidate_id, number in session.execute(
            select(ObservationCandidate.id, PackageRevision.revision_number)
            .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
            .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
            .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
            .join(PackageRevision, PackageRevision.id == WorkflowRun.package_revision_id)
            .where(ObservationCandidate.id.in_(anchors))
        )
    }
    return {
        carried_id: CarriedOrigin(
            revision_number=numbers[root.row_candidate_id],
            given_by=root.confirmed_by,
            given_at=root.created_at,
        )
        for carried_id, root in roots.items()
        if root.row_candidate_id in numbers
    }


def role_origins(
    session: Session, confirmations: Iterable[ViewRoleConfirmation | None]
) -> dict[UUID, CarriedOrigin]:
    """For each carried drawing-role confirmation: the revision the person gave it on (the first
    revision holding that drawing version), who, and when."""
    roots = _roots(session, ViewRoleConfirmation, (c for c in confirmations if c is not None))
    if not roots:
        return {}
    views = {root.drawing_view_id for root in roots.values()}
    numbers: dict[UUID, int] = {}
    for view_id, number in session.execute(
        select(DrawingView.id, PackageRevision.revision_number)
        .join(Page, Page.id == DrawingView.page_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == Page.document_version_id,
        )
        .join(PackageRevision, PackageRevision.id == PackageRevisionDocument.package_revision_id)
        .where(DrawingView.id.in_(views))
    ):
        numbers[view_id] = min(number, numbers.get(view_id, number))
    return {
        carried_id: CarriedOrigin(
            revision_number=numbers[root.drawing_view_id],
            given_by=root.confirmed_by,
            given_at=root.created_at,
        )
        for carried_id, root in roots.items()
        if root.drawing_view_id in numbers
    }


#: What the audit trail calls a reviewer's confirmation that carried answers still apply (#1180).
CARRIED_CONFIRMATION_TARGET: Final = "carried_answers_confirmation"


def carried_answers_driving_results(
    session: Session, revision_id: UUID
) -> tuple[tuple[int, str], ...]:
    """Every answer carried from an earlier revision that a live result of this revision rests on,
    as `(the revision it was given on, words)` (#1180): a wall answer on a countertop row that has a live finding, and a drawing role
    carried onto this revision's pages while any live finding exists. Empty when nothing carried.

    Sign-off asks a person to confirm these still apply: an answer given on another revision is
    still a person's answer, but not one given while looking at these drawings.
    """
    from app.models.verdicts import CheckRun, Finding

    live = list(
        session.execute(
            select(Finding.scope_row_candidate_id)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .where(Finding.package_revision_id == revision_id, CheckRun.superseded_at.is_(None))
        )
    )
    if not live:
        return ()
    checked_rows = {row_id for (row_id,) in live if row_id is not None}
    rows = [row for row in slot_rows(session, revision_id) if row.anchor.id in checked_rows]
    walls = wall_answer_origins(session, (row.decision for row in rows))
    described: list[tuple[int, str]] = []
    for row in rows:
        origin = None if row.decision is None else walls.get(row.decision.id)
        if origin is not None:
            described.append(
                (
                    origin.revision_number,
                    (
                        f"{row.label}: wall answer from revision {origin.revision_number} "
                        f"({origin.given_by})"
                    ),
                )
            )
    views = [entry for entry in revision_views(session, revision_id)]
    latest = _latest_confirmations(session, (entry.view.id for entry in views))
    roles = role_origins(session, latest.values())
    for entry in views:
        confirmation = latest.get(entry.view.id)
        origin = None if confirmation is None else roles.get(confirmation.id)
        if origin is not None:
            described.append(
                (
                    origin.revision_number,
                    (
                        f"Drawing {entry.view.tag} on page {entry.page_index + 1}: role from "
                        f"revision {origin.revision_number} ({origin.given_by})"
                    ),
                )
            )
    return tuple(described)
