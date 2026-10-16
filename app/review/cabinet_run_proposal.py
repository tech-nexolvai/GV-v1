"""A cabinet run proposed from the AI's stored width readings, for a person to confirm (cabinets e2e).

**Why.** `CAB-FILLER-001` (the cabinet run distribution) reads the vendor's run as two ordered
lists, its fillers and its cabinets, plus the reviewer's own marks and the site wall-to-wall. Until
now those lists were typed on Measurements. The countertop row reader already reads the
elevation's chain of pieces left to right, each width read by two AIs and sealed only when they
agree (`workflow/slot_reader.py`, `extraction/slot_reader/seal.py`). This module turns a chain that
passed every check below into a PROPOSED run. Nothing here saves anything: a person picks the run,
says what each piece is, and confirms (`app/api/review_items.py`, `use_cabinet_run`).

**What code checks before proposing a chain** (each a reason when it fails, never silent):

* it is the vendor's drawing (a SHOP document), never the architect's;
* the readers chose the row without a doubt or a hold (`row-ambiguous`, `row-hold:*`);
* the chain is complete: every slot from the first to the last, each exactly once;
* every piece's width was sealed by two AIs reading the same text (`candidate_is_sealed`), in
  inches, in the vendor's ink only: GV's markup is never a reading source;
* no piece's text adds two numbers (`3"+2"`): one piece, one printed width;
* the stone does not run into the walls there (the row read could be the stone's, not the run
  between the walls);
* where a printed overall was sealed too, the pieces add up to it exactly; a chain that does not
  is not proposed.

**What a piece IS is never guessed.** Only the vendor's printed word names a filler (`kind:filler`,
`extraction/slot_reader/kinds.py`). Never its width and never its place: the three answer keys show
runs with no filler at either end (three cabinets), panels at the ends, and fillers in the middle.
So the person says, piece by piece, filler or which kind of cabinet; only a printed "Filler" comes
pre-selected.

**The architect's side** is reported, not filled: on AI Sets 1-3 every architect pairing record
says nothing on the architect's sheet is comparable (`architect_pairing_records`), so the run's
architect lists stay empty and the check says what it waits for.

Read-only; no extraction imports (the API reaches this, `tests/api/test_no_heavy_work.py`).
Verification: `tests/review/test_cabinet_run_proposal.py`, `tests/api/test_review_item_cabinet_run.py`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.models.document import Document, DocumentKind, DocumentVersion
from app.models.evidence import ArchitectPairingRecord, ObservationCandidate
from app.review.cabinet_reading import ai_reading_skipped
from app.review.row_location import RowLocation, row_location
from app.schemas.visual_ui import ExactValueOut
from units.imperial import format_inches
from units.measurement import Measurement, Unit
from vocabulary.check_holds import STONE_INTO_WALLS
from workflow.measurements import operands_for
from workflow.slot_row_scope import SlotRow, candidate_is_sealed, slot_rows

__all__ = [
    "CABINET_RULE",
    "FILLER",
    "SITE_WIDTH_INPUT",
    "CabinetRunNotProposedOut",
    "CabinetRunOut",
    "CabinetRunPieceOut",
    "CabinetRunProposalOut",
    "cabinet_proposal",
    "cabinet_run_proposal_for",
    "proposed_runs",
    "saved_site_width",
]

#: The answer that names a piece a filler, beside the cabinet kinds (`cabinet_choices`).
FILLER: Final = "filler"

#: GV's ink, or ink nobody checked: never a reading source here.
_UNTRUSTED: Final = frozenset(
    {"reviewer-markup", "gv-tick", "gv-other-mark", "two-labels", "ink-unchecked"}
)

NOTHING_READ_SKIPPED: Final = (
    "No cabinet widths were read: the AI did not read this package's drawings (a cabinet package "
    "is read only with the cabinet reading setting on). Enter the run on Measurements, or read the "
    "drawings with the setting on."
)
NOTHING_READ: Final = (
    "No cabinet widths were read on the vendor's pages yet, so there is no run to propose. Enter "
    "the run on Measurements."
)
NONE_PASSED: Final = (
    "Rows were read, but none passed every check to be proposed (each is listed with its reason). "
    "Enter the run on Measurements."
)


class CabinetRunPieceOut(BaseModel):
    """One piece of a proposed run, left to right, as two AIs read its printed width."""

    position: int = Field(description="1-based place on the row, left to right.")
    width: ExactValueOut = Field(description="The sealed width, exact inches.")
    printed: str = Field(description="The label as printed, e.g. `610 [24]`.")
    candidate_id: UUID = Field(description="The sealed reading this width comes from.")
    printed_kind: str | None = Field(
        default=None,
        description=(
            "What the vendor's own printed word names it (`filler`, `cabinet`, `appliance_space`), "
            "or null when nothing printed names it. Never from its width or its place."
        ),
    )
    proposed: str | None = Field(
        default=None,
        description='Pre-selected answer: `filler` only where the vendor printed "Filler".',
    )


class CabinetRunOut(BaseModel):
    """One vendor row whose every width two AIs read alike and code checked."""

    row_id: UUID = Field(description="The row's first piece reading; sent back to use this run.")
    page: int = Field(description="1-based vendor page.")
    pieces: list[CabinetRunPieceOut]
    total: ExactValueOut = Field(description="The pieces added up: the run as the vendor drew it.")
    checks: list[str] = Field(description="What code checked, in plain words.")
    vendor_ref: RowLocation | None = None
    architect_note: str = Field(description="What the architect's drawings give for this run.")


class CabinetRunNotProposedOut(BaseModel):
    """A vendor row that was read but not proposed, and why (never silent)."""

    page: int
    row_id: UUID | None = None
    why: str


class CabinetRunProposalOut(BaseModel):
    """The runs a person may use for the cabinet check, and the rows that could not be used."""

    runs: list[CabinetRunOut] = Field(default_factory=list)
    not_proposed: list[CabinetRunNotProposedOut] = Field(default_factory=list)
    note: str | None = Field(
        default=None, description="Why nothing is proposed; null when a run is proposed."
    )
    site_width: ExactValueOut | None = Field(
        default=None,
        description=(
            "The site wall-to-wall already saved for this check (the client's F_Wall_2_Wall_Dim, "
            "measured on site); null until a person enters it."
        ),
    )


@dataclass(frozen=True, slots=True)
class _Piece:
    index: int
    value: Fraction
    candidate: ObservationCandidate
    printed_kind: str | None


def _flags(candidate: ObservationCandidate) -> list[str]:
    return [str(flag) for flag in candidate.ambiguity_flags or ()]


def _flag_value(flags: Iterable[str], prefix: str) -> str | None:
    return next((flag.removeprefix(prefix) for flag in flags if flag.startswith(prefix)), None)


def _exact(value: Fraction) -> ExactValueOut:
    return ExactValueOut(
        numerator=str(value.numerator),
        denominator=str(value.denominator),
        display=f'{format_inches(value)}"',
    )


def _value(candidate: ObservationCandidate) -> Fraction | None:
    if candidate.value_numerator is None or not candidate.value_denominator:
        return None
    return Fraction(candidate.value_numerator, candidate.value_denominator)


def _vendor_ink_only(flags: Sequence[str]) -> bool:
    inks = {flag for flag in flags if flag.startswith("ink:")}
    return inks == {"ink:vendor"} and not set(flags) & _UNTRUSTED


def _is_vendor_document(session: Session, document_version_id: UUID) -> bool:
    kind = session.scalar(
        select(Document.kind)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(DocumentVersion.id == document_version_id)
    )
    return kind == DocumentKind.SHOP.value


def _architect_note(session: Session, row_id: UUID) -> str:
    later = aliased(ArchitectPairingRecord)
    record = session.scalars(
        select(ArchitectPairingRecord)
        .where(
            ArchitectPairingRecord.row_anchor_candidate_id == row_id,
            ~exists().where(later.supersedes_id == ArchitectPairingRecord.id),
        )
        .order_by(ArchitectPairingRecord.created_at.desc())
        .limit(1)
    ).first()
    if record is None:
        return "The architect's drawings were not compared with this run."
    if record.status == "nothing_comparable":
        return (
            "The architect's drawings give no cabinet widths for this run (their numbers measure "
            "to walls, fixtures or blocking). The check waits for the architect's run."
        )
    if record.status in ("paired", "reviewer"):
        return (
            "The architect's widths for this run are compared on the architect check; they are not "
            "filled in here."
        )
    return "The architect's widths for this run could not be matched to it."


def _qualify(session: Session, row: SlotRow) -> tuple[list[_Piece], list[str]] | str:
    """The row's pieces and what code checked, or why the row is not proposed."""
    anchor = row.anchor
    if not _is_vendor_document(session, anchor.document_version_id):
        return "This row is not on the vendor's drawing."
    every = [flag for candidate in row.candidates for flag in _flags(candidate)]
    if "row-ambiguous" in every:
        return "The AIs were not sure this row is the run."
    hold = _flag_value(every, "row-hold:")
    if hold is not None:
        return f"The AIs did not read this row ({hold.replace('-', ' ')})."
    if f"check-hold:{STONE_INTO_WALLS[0]}" in every:
        # The stone runs into wall pockets here, so the row read for the countertop may be the
        # stone's (into the walls), not the run between the walls: stored readings on one page read
        # each of the two rows in different runs. Never proposed; the run is entered by hand.
        return (
            "The stone runs into the walls here, so this row may be the stone's rather than the "
            "cabinets' between the walls."
        )
    by_slot: dict[str, list[ObservationCandidate]] = {}
    for candidate in row.candidates:
        slot = _flag_value(_flags(candidate), "slot:")
        if slot is not None:
            by_slot.setdefault(slot, []).append(candidate)
    if row.piece_count < 2:
        return "The row has fewer than two pieces."
    pieces: list[_Piece] = []
    for index in range(row.piece_count):
        found = by_slot.get(str(index), [])
        if len(found) != 1:
            return f"Piece {index + 1} of {row.piece_count} has no single reading."
        candidate = found[0]
        flags = _flags(candidate)
        if not candidate_is_sealed(candidate) or candidate.unit != "in":
            return f"Piece {index + 1}: the two AIs did not read the same inches."
        if not _vendor_ink_only(flags):
            return f"Piece {index + 1}: not positively the vendor's own ink."
        if "+" in candidate.raw_text:
            return f'Piece {index + 1}: its label adds two numbers ("{candidate.raw_text}").'
        value = _value(candidate)
        if value is None or value <= 0:
            return f"Piece {index + 1}: no width above zero."
        kind = _flag_value(flags, "kind:")
        pieces.append(_Piece(index, value, candidate, None if kind in (None, "unknown") else kind))
    total = sum((piece.value for piece in pieces), Fraction(0))
    checks = [
        f"Each of the {len(pieces)} widths was read alike by two AIs, in the vendor's ink.",
        "The chain is complete: every piece from the first to the last.",
    ]
    overall = by_slot.get("overall", [])
    if len(overall) == 1 and candidate_is_sealed(overall[0]) and overall[0].unit == "in":
        printed = _value(overall[0])
        if printed != total:
            shown = "?" if printed is None else f'{format_inches(printed)}"'
            return (
                f'The pieces add up to {format_inches(total)}" but the printed overall reads '
                f"{shown}."
            )
        checks.append(f'The pieces add up to the printed overall, {format_inches(total)}".')
    elif overall:
        checks.append("A printed overall was not read alike by both AIs, so the sum is unchecked.")
    else:
        checks.append("No printed overall was read to check the sum against.")
    return pieces, checks


def proposed_runs(
    session: Session, revision_id: UUID
) -> tuple[list[CabinetRunOut], list[CabinetRunNotProposedOut]]:
    """Every row of the newest reading, proposed or listed with why it was not."""
    runs: list[CabinetRunOut] = []
    skipped: list[CabinetRunNotProposedOut] = []
    for row in slot_rows(session, revision_id):
        qualified = _qualify(session, row)
        if isinstance(qualified, str):
            skipped.append(
                CabinetRunNotProposedOut(page=row.page_number, row_id=row.anchor.id, why=qualified)
            )
            continue
        pieces, checks = qualified
        runs.append(
            CabinetRunOut(
                row_id=row.anchor.id,
                page=row.page_number,
                pieces=[
                    CabinetRunPieceOut(
                        position=piece.index + 1,
                        width=_exact(piece.value),
                        printed=piece.candidate.raw_text,
                        candidate_id=piece.candidate.id,
                        printed_kind=piece.printed_kind,
                        proposed=FILLER if piece.printed_kind == FILLER else None,
                    )
                    for piece in pieces
                ],
                total=_exact(sum((piece.value for piece in pieces), Fraction(0))),
                checks=checks,
                vendor_ref=row_location(session, row.anchor.id),
                architect_note=_architect_note(session, row.anchor.id),
            )
        )
    return runs, skipped


def cabinet_proposal(
    session: Session, revision_id: UUID, *, site_width: Fraction | None
) -> CabinetRunProposalOut:
    """The proposal a cabinet check with no run shows, with the site width already saved."""
    runs, skipped = proposed_runs(session, revision_id)
    note: str | None = None
    if not runs:
        if skipped:
            note = NONE_PASSED
        elif ai_reading_skipped(session, revision_id):
            note = NOTHING_READ_SKIPPED
        else:
            note = NOTHING_READ
    return CabinetRunProposalOut(
        runs=runs,
        not_proposed=skipped,
        note=note,
        site_width=None if site_width is None else _exact(site_width),
    )


#: The check that reads the run, and the input that is the site wall-to-wall (the client's
#: F_Wall_2_Wall_Dim, measured on site; a `USER_INPUT`, so a person types it).
CABINET_RULE: Final = "CAB-FILLER-001"
SITE_WIDTH_INPUT: Final = "field_width"


def saved_site_width(session: Session, revision_id: UUID) -> Fraction | None:
    """The site wall-to-wall the cabinet check reads now, in inches; None until it is entered."""
    operand = operands_for(session, revision_id).get(CABINET_RULE, {}).get(SITE_WIDTH_INPUT)
    value = None if operand is None else operand.value
    if not isinstance(value, Measurement) or value.unit is not Unit.INCH:
        return None
    return value.exact


def cabinet_run_proposal_for(session: Session, revision_id: UUID) -> CabinetRunProposalOut:
    """`cabinet_proposal` with the site width the check reads now."""
    return cabinet_proposal(session, revision_id, site_width=saved_site_width(session, revision_id))
