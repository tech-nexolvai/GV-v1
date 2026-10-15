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
    ArchitectPairingRecord,
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
from workflow.slot_row_scope import SlotRow, slot_rows, slot_rows_and_unchosen_pages
from workflow.view_roles import CODE_CONFIRMERS, revision_views

__all__ = [
    "CARRY_FORWARD_ACTOR",
    "CarriedOrigin",
    "CarryForwardReport",
    "WithinRevisionCarry",
    "carried_answers_driving_results",
    "carry_after_reading",
    "carry_row_answers_within_revision",
    "carry_run_values",
    "role_origins",
    "row_identity",
    "wall_answer_origins",
]

#: Who the audit record names as having copied an answer forward. Never a person: the person is on
#: the carried record itself, as the one who gave the answer.
CARRY_FORWARD_ACTOR: Final = "revision carry-forward (#1180)"

#: Who the audit record names as having carried an answer to a newer reading of the same revision
#: (#1206). Never a person: the person is on the carried record itself.
CARRY_WITHIN_REVISION_ACTOR: Final = "read the unread pages (#1206)"

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


def _without_rank(key: RowIdentity) -> tuple[object, ...]:
    """The row's identity without its rank (#1210): the same page bytes, page number, piece count,
    reader version and values read. With the line within `ROW_Y_SLACK_PT` it is the same physical
    row, whatever rank the reading gave it (the readers chose another of the page's ranked rows, or
    code ranked them differently)."""
    return (*key[:2], *key[3:])


#: Where each piece of a row sits across the page, in points (`row_xs_pt`): per slot (`slot:<i>`
#: or `slot:overall`), its slot box's left and right ends and its label's middle.
RowXs = tuple[tuple[str, Decimal | None, Decimal | None, Decimal], ...]

#: Where a row sits on a page: its identity, its line's height and its pieces across the page.
Placement = tuple[RowIdentity | None, Decimal | None, RowXs | None]


def _same_xs(mine: RowXs | None, other: RowXs | None) -> bool:
    """The same pieces at the same places across the page: every slot's box ends and label middle
    within `ROW_Y_SLACK_PT` (#1210 review: an identical run drawn beside the paired one, at the
    same height, is another line). Unknown positions never match."""
    if mine is None or other is None or [x[0] for x in mine] != [x[0] for x in other]:
        return False
    for (_slot, *here), (_same, *there) in zip(mine, other, strict=True):
        for this, that in zip(here, there, strict=True):
            if this is None and that is None:
                continue
            if this is None or that is None or abs(this - that) > ROW_Y_SLACK_PT:
                return False
    return True


def _same_row(
    where: Placement,
    others: Iterable[Placement],
    *,
    within_revision: bool = False,
) -> list[int]:
    """Which of `others` (by index) are the row placed at `where`: the same identity, the line
    within `ROW_Y_SLACK_PT`.

    `within_revision` (#1210), for a newer reading of the same revision: every piece must also sit
    at the same place across the page (`_same_xs`), and, when no row has the same rank, the same
    physical row at another rank counts (`_without_rank`)."""
    key, y, xs = where
    if key is None or y is None:
        return []
    placed = list(others)

    def near(other_y: Decimal | None, other_xs: RowXs | None) -> bool:
        return (
            other_y is not None
            and abs(other_y - y) <= ROW_Y_SLACK_PT
            and (not within_revision or _same_xs(xs, other_xs))
        )

    exact = [i for i, (other, oy, oxs) in enumerate(placed) if other == key and near(oy, oxs)]
    if exact or not within_revision:
        return exact
    physical = _without_rank(key)
    return [
        i
        for i, (other, oy, oxs) in enumerate(placed)
        if other is not None and _without_rank(other) == physical and near(oy, oxs)
    ]


def _box_ends(flags: Iterable[str], name: str) -> tuple[Decimal, Decimal] | None:
    """The left and right ends of a `<name>:left,top,right,bottom` box flag, in pixels."""
    for flag in flags:
        if flag.startswith(f"{name}:"):
            parts = flag.removeprefix(f"{name}:").split(",")
            try:
                return Decimal(parts[0]), Decimal(parts[2])
            except (IndexError, ArithmeticError):
                return None
    return None


def row_xs_pt(row: SlotRow, dpi: int | None) -> RowXs | None:
    """Where each piece of the row sits across the page, in points (`RowXs`); `None` when a piece's
    place is unknown."""
    if not dpi:
        return None
    scale = Decimal(72) / Decimal(dpi)
    found: dict[str, tuple[Decimal | None, Decimal | None, Decimal]] = {}
    for candidate in row.candidates:
        flags = candidate.ambiguity_flags or ()
        slot = next((flag for flag in flags if flag.startswith("slot:")), None)
        if slot is None:
            continue
        polygon = candidate.polygon
        if slot in found or not isinstance(polygon, list) or not polygon:
            return None
        try:
            middle = sum((Decimal(str(point[0])) for point in polygon), Decimal(0)) / len(polygon)
        except (TypeError, IndexError, ArithmeticError):
            return None
        ends = _box_ends(flags, "slot-box")
        found[slot] = (
            None if ends is None else ends[0] * scale,
            None if ends is None else ends[1] * scale,
            middle * scale,
        )
    return tuple((slot, *found[slot]) for slot in sorted(found)) or None


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
    return _carry_row_answers(
        session, slot_rows(session, prior_id), slot_rows(session, revision_id)
    )


@dataclass(frozen=True, slots=True)
class WithinRevisionCarry:
    """What "Read the unread pages" carried from the earlier reading of the revision (#1206)."""

    decisions: int
    """Wall answers and typed widths (`SlotRowReviewDecision`) carried to their rows."""
    pairings: int
    """Reviewer architect pairings carried to their rows, each still the pairing that counts."""
    pairings_held: int
    """Reviewer pairings that could not be placed on exactly one new row: that row's pairing is
    held, naming the earlier decision, so it never silently reverts to the automatic one. When the
    new reading chose another line of the page (#1210), the row it chose there is held."""
    pairings_not_placed: int = 0
    """Reviewer pairings on a page where the new reading chose no row left to hold (#1210): the page
    has no countertop row now, so it is listed for the reviewer's own answer (a row-choice split
    or a page with no countertop row, each blocking sign-off until answered)."""


def carry_row_answers_within_revision(
    session: Session, revision_id: UUID, earlier_run_id: UUID
) -> WithinRevisionCarry:
    """Carry a reviewer's row answers from an earlier reading of this revision to its newest one
    (#1206): "Read the unread pages" writes a new reading of every page, so every row is a new
    record, and the rows of pages already read must keep what the reviewer said about them.

    * **Wall answers and typed widths** (`SlotRowReviewDecision`): the same identity as between
      revisions (page content hash, page number, row rank, piece count, reader version, every value
      read, the line within `ROW_Y_SLACK_PT`), and every piece at the same place across the page
      (`_same_xs`: an identical run beside it, at the same height, is another line). When no row
      has the same rank, the same physical row at another rank counts (#1210, `_same_row`). A row
      with no single match is asked again, and an answer left with no row is audited with the
      reason (`_audit_not_carried`).
    * **Architect pairings a reviewer made** (`ArchitectPairingRecord`, source `reviewer`): by the
      same rule; the copy outranks the new automatic pairing exactly as before. One that cannot be
      placed on exactly one new row, or would no longer count there, holds that row's pairing with
      the earlier decision named (`_hold_pairing`): never a silent return to the automatic one.
      When the new reading chose another line of the page (no row left at the paired rank, #1210),
      the page's chosen row is held, saying the reading picked a different line. With no row on
      the page at all, the pairing is audited with the reason and the page is listed for the
      reviewer. A row is held once per pass.
    * **Holds stay in force** (#1210 review): a hold standing for a reviewer's pairing (source
      `none`, `earlier_reviewer_record_id`) is never let go by a later reading. Its row, or the
      row chosen in its place, is held again with the hold's own words, naming the original
      decision, until a person pairs it.

    Each copy names the original (`carried_from_id`, in the pairing's `details`) and is audited.
    """
    earlier, _unchosen = slot_rows_and_unchosen_pages(session, revision_id, run_id=earlier_run_id)
    current = slot_rows(session, revision_id)
    decisions = _carry_row_answers(session, earlier, current, within_revision=True)
    pairings, held, not_placed = _carry_reviewer_pairings(session, revision_id, earlier, current)
    return WithinRevisionCarry(
        decisions=decisions,
        pairings=pairings,
        pairings_held=held,
        pairings_not_placed=not_placed,
    )


def _carry_reviewer_pairings(
    session: Session,
    revision_id: UUID,
    earlier: Iterable[SlotRow],
    current: Iterable[SlotRow],
) -> tuple[int, int, int]:
    """`(carried, held, not placed)`: every reviewer pairing in force on an earlier row, on its new
    row, or holding the row the new reading chose instead."""
    from workflow.architect_match_records import effective_architect_matches
    from workflow.architect_pairing_records import latest_architect_pairings, record_view

    earlier = tuple(earlier)
    current = tuple(current)
    # A reviewer's pairing, or a hold that stands for one (#1210 review): a hold is never let go by
    # a later reading; it holds the row again, naming the reviewer's original decision.
    in_force: dict[UUID, tuple[Any, ArchitectPairingRecord, list[str] | None]] = {}
    for anchor_id, pairing in latest_architect_pairings(
        session, [row.anchor.id for row in earlier]
    ).items():
        if pairing is None:
            continue
        if pairing.source == "reviewer":
            in_force[anchor_id] = (
                pairing,
                session.get_one(ArchitectPairingRecord, pairing.record_id),
                None,
            )
        elif pairing.source == "none":
            hold = session.get_one(ArchitectPairingRecord, pairing.record_id)
            named = _held_for(session, hold)
            if named is not None:
                said = hold.details.get("reasons")
                in_force[anchor_id] = (
                    pairing,
                    named,
                    [str(reason) for reason in said] if isinstance(said, list) else [],
                )
    if not in_force:
        return 0, 0, 0
    hashes = _page_hashes(session, (*earlier, *current))
    readers = _readers(session, (*earlier, *current))

    def placed(row: SlotRow) -> Placement:
        version, dpi = readers.get(row.anchor.extraction_run_id, (None, None))
        return (
            row_identity(row, hashes.get(row.anchor.page_id), version),
            row_y_pt(row, dpi),
            row_xs_pt(row, dpi),
        )

    current_placed = [placed(other) for other in current]
    carried = held = 0
    carried_to: set[UUID] = set()
    held_rows: set[UUID] = set()
    other_line: list[tuple[SlotRow, ArchitectPairingRecord, list[str] | None]] = []
    for row in earlier:
        entry = in_force.get(row.anchor.id)
        if entry is None:
            continue
        pairing, original, hold_reasons = entry
        targets = [current[i] for i in _same_row(placed(row), current_placed, within_revision=True)]
        if hold_reasons is not None:
            # Still held: the same row, or the rows it could be, are held again; never carried.
            suspects = targets or _same_rank(row, current)
            if suspects:
                held += _hold_pairing(
                    session, revision_id, suspects, original, held=held_rows, reasons=hold_reasons
                )
            else:
                other_line.append((row, original, hold_reasons))
            continue
        if len(targets) == 1:
            target = targets[0]
            details: dict[str, object] = {**original.details, "carried_from_id": str(original.id)}
            ok = True
            if record_view(original) is not None:
                # Made against a view of the architect's own file (#1167): it counts only under the
                # row's match as it stands now, which the new reading re-made for the same view.
                match = effective_architect_matches(session, [target.anchor.id]).get(
                    target.anchor.id
                )
                view = None if match is None else match.matched
                ok = (
                    view is not None
                    and str(view.page_id) == str(details.get("architect_page_id"))
                    and str(view.document_version_id)
                    == str(details.get("architect_document_version_id"))
                    and view.view_tag == details.get("architect_view_tag")
                )
                if ok:
                    assert match is not None and view is not None
                    details["architect_view_id"] = str(view.view_id)
                    details["match_record_id"] = str(match.record_id)
            if ok and _copy_counts(session, target, original, details, pairing):
                carried += 1
                carried_to.add(target.anchor.id)
                continue
            held += _hold_pairing(session, revision_id, [target], original, held=held_rows)
            continue
        # Two new rows it could be, or none with the same identity: the rows on the same page at the
        # same rank are held, naming the decision, rather than taking the automatic pairing.
        suspects = targets or _same_rank(row, current)
        if suspects:
            held += _hold_pairing(session, revision_id, suspects, original, held=held_rows)
        else:
            other_line.append((row, original, None))
    # No row left at the paired rank: the new reading chose another line of the page (#1210). The
    # row it chose there is held, never left on its automatic pairing; a row that just took a
    # reviewer's own pairing keeps it. Done after every carry, so no carried pairing is undone.
    not_placed = 0
    for row, original, hold_reasons in other_line:
        chosen = [
            other
            for other in current
            if other.anchor.page_id == row.anchor.page_id and other.anchor.id not in carried_to
        ]
        if not chosen:
            # No countertop row on the page now: the page itself is listed for the reviewer, and
            # the audit trail says why this pairing went nowhere.
            not_placed += 1
            _audit_not_carried(
                session,
                original.id,
                "architect_pairing_record",
                f"architect pairing not carried: no countertop row on page {row.page_number} "
                "in the new reading; the page is listed for the reviewer",
            )
            continue
        held += _hold_pairing(
            session,
            revision_id,
            chosen,
            original,
            other_line_on_page=row.page_number,
            held=held_rows,
            reasons=hold_reasons,
        )
    return carried, held, not_placed


def _same_rank(row: SlotRow, current: Iterable[SlotRow]) -> list[SlotRow]:
    """The new rows on the same page at the same rank as `row`."""
    rank = _rank(row)
    return [
        other
        for other in current
        if other.anchor.page_id == row.anchor.page_id and _rank(other) == rank
    ]


def _held_for(session: Session, hold: ArchitectPairingRecord) -> ArchitectPairingRecord | None:
    """The reviewer's pairing a hold stands for (`earlier_reviewer_record_id`), or `None` for a
    record that is no such hold (code's own "no pairing"). A hold whose named record cannot be read
    stands for itself: it is still held, never let go."""
    named = (hold.details or {}).get("earlier_reviewer_record_id")
    if named is None:
        return None
    try:
        found = session.get(ArchitectPairingRecord, UUID(str(named)))
    except ValueError:
        found = None
    return hold if found is None else found


def _audit_not_carried(session: Session, target_id: UUID, target_type: str, reason: str) -> None:
    """Record an answer that could not be carried, with the plain reason (#1210 review). The audit
    record has no reason field (and this change adds no migration), so the reason rides on the
    actor: who did it, then what happened."""
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=f"{CARRY_WITHIN_REVISION_ACTOR}: {reason}"[:200],
        target_id=target_id,
        target_type=target_type,
    )


def _tip(session: Session, row_anchor_id: UUID) -> UUID | None:
    """The newest record of a row's pairing chain (the one nothing supersedes yet)."""
    from sqlalchemy import exists
    from sqlalchemy.orm import aliased

    later = aliased(ArchitectPairingRecord)
    return session.scalar(
        select(ArchitectPairingRecord.id)
        .where(
            ArchitectPairingRecord.row_anchor_candidate_id == row_anchor_id,
            ~exists().where(later.supersedes_id == ArchitectPairingRecord.id),
        )
        .order_by(ArchitectPairingRecord.created_at.desc(), ArchitectPairingRecord.id.desc())
        .limit(1)
    )


def _copy_counts(
    session: Session,
    target: SlotRow,
    original: ArchitectPairingRecord,
    details: dict[str, object],
    pairing: Any,
) -> bool:
    """Append the reviewer's pairing on the new row and keep it only when it is now the pairing that
    counts there, with the same pairs; otherwise take it back (a savepoint) for a hold."""
    from workflow.architect_pairing_records import latest_architect_pairing

    reasons = details.get("reasons")
    details["reasons"] = [
        *(reasons if isinstance(reasons, list) else []),
        "Carried from the reviewer's pairing on the earlier reading of this revision.",
    ]
    savepoint = session.begin_nested()
    record = ArchitectPairingRecord(
        package_revision_id=original.package_revision_id,
        page_id=target.anchor.page_id,
        row_anchor_candidate_id=target.anchor.id,
        extraction_run_id=None,
        source="reviewer",
        status="reviewer",
        pairs=list(original.pairs),
        details=details,
        supersedes_id=_tip(session, target.anchor.id),
        decided_by=original.decided_by,
    )
    session.add(record)
    session.flush()
    now = latest_architect_pairing(session, target.anchor.id)
    if (
        now is None
        or now.record_id != record.id
        or {pair.architect_candidate_id for pair in now.pairs}
        != {pair.architect_candidate_id for pair in pairing.pairs}
    ):
        savepoint.rollback()
        return False
    savepoint.commit()
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=CARRY_WITHIN_REVISION_ACTOR,
        target_id=record.id,
        target_type="architect_pairing_record",
    )
    return True


def _hold_pairing(
    session: Session,
    revision_id: UUID,
    rows: Iterable[SlotRow],
    original: ArchitectPairingRecord,
    *,
    other_line_on_page: int | None = None,
    held: set[UUID] | None = None,
    reasons: list[str] | None = None,
) -> int:
    """Hold each row's pairing with the earlier reviewer decision named: a record of no pairing
    that outranks the new automatic one, so a person pairs the row again. `other_line_on_page`:
    the page whose paired row the new reading did not choose (#1210); the words say so. `held`:
    rows already held in this pass, skipped and added to. `reasons`: an earlier hold's own words,
    kept when a later reading holds its row again."""
    said = (
        "nothing on the architect's drawing is comparable"
        if not original.pairs
        else f"{len(original.pairs)} architect dimension(s) paired"
    )
    reason = (
        (
            f"{original.decided_by} paired this row on the earlier reading ({said}), "
            "but the new reading could not be matched to that row for certain. Pair "
            "it again."
        )
        if other_line_on_page is None
        else (
            f"{original.decided_by} paired the countertop row on page {other_line_on_page} on "
            f"the earlier reading ({said}), but the new reading picked a different line on this "
            "page, so that pairing could not be carried. Pair this row again."
        )
    )
    count = 0
    for row in rows:
        if held is not None:
            if row.anchor.id in held:
                continue
            held.add(row.anchor.id)
        record = ArchitectPairingRecord(
            package_revision_id=revision_id,
            page_id=row.anchor.page_id,
            row_anchor_candidate_id=row.anchor.id,
            extraction_run_id=row.anchor.extraction_run_id,
            source="none",
            status="ambiguous",
            pairs=[],
            details={
                "earlier_reviewer_record_id": str(original.id),
                "reasons": reasons or [reason],
            },
            supersedes_id=_tip(session, row.anchor.id),
            decided_by=None,
        )
        session.add(record)
        session.flush()
        emit(
            session,
            category=AuditCategory.REVIEW_ACTION,
            actor=CARRY_WITHIN_REVISION_ACTOR,
            target_id=record.id,
            target_type="architect_pairing_record",
        )
        count += 1
    return count


def _carry_row_answers(
    session: Session,
    earlier: Iterable[SlotRow],
    current: Iterable[SlotRow],
    *,
    within_revision: bool = False,
) -> int:
    answered = [row for row in earlier if row.decision is not None]
    if not answered:
        return 0
    current = tuple(current)
    hashes = _page_hashes(session, (*answered, *current))
    readers = _readers(session, (*answered, *current))

    def placed(row: SlotRow) -> Placement:
        version, dpi = readers.get(row.anchor.extraction_run_id, (None, None))
        return (
            row_identity(row, hashes.get(row.anchor.page_id), version),
            row_y_pt(row, dpi),
            row_xs_pt(row, dpi),
        )

    remembered: list[tuple[Placement, SlotRowReviewDecision]] = []
    for row in answered:
        where = placed(row)
        if where[0] is not None and where[1] is not None and row.decision is not None:
            remembered.append((where, row.decision))
    carried = 0
    used: set[UUID] = set()
    for row in current:
        if row.decision is not None:
            continue
        matches = [
            remembered[i][1]
            for i in _same_row(
                placed(row),
                (where for where, _ in remembered),
                within_revision=within_revision,
            )
        ]
        # Two earlier rows this row could be is no identity at all: it is asked again.
        if len(matches) != 1:
            continue
        source = matches[0]
        used.add(source.id)
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
    if within_revision:
        # Never silent (#1210 review): an answer the newer reading has no row for is audited with
        # the reason. (A wall hold the screen shows is a follow-up; the row's walls are decided
        # again, asked unless code and both AIs agree.)
        pages_now = {row.anchor.page_id for row in current}
        for row in answered:
            assert row.decision is not None
            if row.decision.id in used:
                continue
            why = (
                f"the new reading's row on page {row.page_number} is not the row it was given on"
                if row.anchor.page_id in pages_now
                else f"no countertop row on page {row.page_number} in the new reading"
            )
            _audit_not_carried(
                session,
                row.decision.id,
                "slot_row_review_decision",
                f"wall answer not carried: {why}; the row's walls are asked again",
            )
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
    from app.review.no_countertop import held_depth_and_sink_runs

    live = list(
        session.execute(
            select(Finding.scope_row_candidate_id)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .where(Finding.package_revision_id == revision_id, CheckRun.superseded_at.is_(None))
        )
    )
    # A row's own depth and sink holds (#1201) rest on no wall answer: only the row's other
    # results make its carried wall answer one a result rests on, as before them.
    walled = set(
        session.scalars(
            select(Finding.scope_row_candidate_id)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .where(
                Finding.package_revision_id == revision_id,
                CheckRun.superseded_at.is_(None),
                CheckRun.id.not_in(held_depth_and_sink_runs()),
            )
        )
    )
    if not live:
        return ()
    checked_rows = {row_id for row_id in walled if row_id is not None}
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
