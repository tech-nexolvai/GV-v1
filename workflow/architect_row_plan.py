"""Whether one vendor countertop row has anything to compare with the architect (#1054).

The light half of the vendor-vs-architect check (CT-ARCH-WIDTH-001): it reads the row's effective
pairing (#1053) through `workflow/architect_pairing_contract.py` and decides, without reading any
value, what the check does with the row:

* **nothing comparable** — no pairing, the architect prints nothing comparable, the scales could not
  be measured, or no pair is one to one: **no finding**, and a plain reason shown beside the row.
  This never creates a PASS (decision flagged for Anant in #1054: it saves a reviewer click on every
  page where the architect prints nothing comparable). Where no row of a revision is compared, one
  revision-wide NO_APPLICABLE_RULE line says so (`NOTHING_PAIRED_ON_REVISION`): seen to have run,
  never a pass, blocking nothing;
* **unresolved** — the two AIs disagreed or refused, or code could not decide while the architect
  does print a usable dimension on drawn casework: a REVIEW_REQUIRED finding, "pair it (one click)";
* **compare** — the one-to-one pairs, overall with overall and one architect span with one vendor
  piece. A bay the vendor splits into several pieces is left out and said so (V1 compares no sums).

Imports nothing that reads a drawing, so the countertop results (`app/api/visual_countertops.py`)
can ask it too (`tests/api/test_no_heavy_work.py`).

**The join seam with #1053.** `effective_architect_pairing` imports `workflow.architect_pairing`
lazily and answers "no pairing" while that module does not exist on this branch.
`DatabaseStages(architect_pairing=...)` and `countertop_results_for_revision(pairing_lookup=...)`
take any callable of the same shape, which is how the tests give fake pairings.

Source: issue #1054 · Verification: `tests/workflow/test_architect_row_evidence.py`
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.sides import ReadingSides
from app.models.evidence import ObservationCandidate
from app.models.runs import ExtractionRun
from workflow.architect_pairing_contract import EffectivePairing, PairingSource
from workflow.slot_row_scope import SlotRow

__all__ = [
    "ARCHITECT_CHECK_RULE_ID",
    "ARCHITECT_TEXT_ROUTE",
    "CONFIRM_AI_PAIRING",
    "NOTHING_PAIRED_ON_REVISION",
    "PAIR_BY_REVIEWER",
    "ArchitectRowPlan",
    "ComparedPair",
    "Disposition",
    "PairingLookup",
    "effective_architect_pairing",
    "pair_label",
    "pairing_source_from_notes",
    "pairing_source_note",
    "plan_architect_row",
]

#: The route the architect reader records its values under — `workflow.architect_reader.
#: ARCHITECT_EXTRACTOR`, restated because that module reads PDFs and the API may not import it.
#: `tests/workflow/test_architect_row_evidence.py` fails if the two part.
ARCHITECT_TEXT_ROUTE: Final = "architect-text"

ARCHITECT_CHECK_RULE_ID: Final = "CT-ARCH-WIDTH-001"

type PairingLookup = Callable[[Session, UUID], EffectivePairing | None]
"""`latest_architect_pairing(session, row_anchor_id)` from #1053, or anything shaped like it."""

#: The pairing statuses that mean "the architect prints nothing to compare here": no finding.
_NOTHING_COMPARABLE: Final = frozenset({"nothing_comparable", "no_scale", "none"})
#: Statuses where the two AIs were asked and did not settle it: the reviewer pairs (one click).
_AIS_UNSETTLED: Final = frozenset({"ais-disagree", "ais-refused"})
#: Statuses where code could not decide: held only when the architect prints something comparable.
_CODE_UNDECIDED: Final = frozenset({"ambiguous", "no_fit"})
_DECIDED: Final = frozenset({"paired", "reviewer"})

_SOURCE_WORDS: Final = {
    "code": "decided by code from where both drawings are drawn",
    "both-ais": "both AIs gave the identical pairing (not yet confirmed by a person)",
    "reviewer": "chosen by the reviewer",
    "none": "nobody has paired this row",
}

NOTHING_PAIRED_ON_REVISION: Final = (
    "Compared row by row with the architect's drawing: no countertop row on this revision has an "
    "architect dimension paired one to one with it, so nothing was compared. This is not a pass; "
    "each row's results say why it was not compared."
)
PAIR_BY_REVIEWER: Final = "Pair the architect's dimension with the vendor's (one click)."
CONFIRM_AI_PAIRING: Final = (
    "The AIs paired these; confirm the pairing. The numbers match, but a match that rests on an "
    "AI-only pairing is not a pass until a person confirms which dimensions belong together."
)


def effective_architect_pairing(session: Session, row_anchor_id: UUID) -> EffectivePairing | None:
    """The pairing that counts for this vendor row: the join seam with #1053.

    Until `workflow/architect_pairing.py` exists on this branch there is no pairing, so the check
    compares nothing and says so. Only that module's own absence is "no pairing": any other import
    error inside it is raised, so a broken pairing module can never look like an empty one.
    """
    try:
        from workflow.architect_pairing import (  # type: ignore[import-not-found]
            latest_architect_pairing,
        )
    except ModuleNotFoundError as error:
        if error.name != "workflow.architect_pairing":
            raise
        return None
    pairing = latest_architect_pairing(session, row_anchor_id)
    assert pairing is None or isinstance(pairing, EffectivePairing)
    return pairing


class Disposition(StrEnum):
    """What the check does with one row."""

    NOT_COMPARED = "not_compared"
    """Nothing comparable: no finding; the reason is shown beside the row."""
    UNRESOLVED = "unresolved"
    """Something comparable that nobody could pair: REVIEW_REQUIRED."""
    COMPARE = "compare"
    """At least one pair to compare: the rule's arithmetic decides."""


@dataclass(frozen=True, slots=True)
class ComparedPair:
    """One width both drawings print for the same thing, compared exactly."""

    kind: Literal["piece", "overall"]
    vendor_slot: int | None
    """The vendor row's `slot:<i>`; `None` for the overall."""
    architect_candidate_id: UUID

    @property
    def architect_name(self) -> str:
        return (
            "architect_overall"
            if self.vendor_slot is None
            else f"architect_piece[{self.vendor_slot}]"
        )

    @property
    def vendor_name(self) -> str:
        return "vendor_overall" if self.vendor_slot is None else f"vendor_piece[{self.vendor_slot}]"


def pair_label(kind: str, vendor_slot: int | None) -> str:
    """Plain words for one pair: `overall` or `piece 2` (counted from one, as the row shows it)."""
    return "overall" if kind == "overall" or vendor_slot is None else f"piece {vendor_slot + 1}"


def pairing_source_note(pairing: EffectivePairing) -> str:
    """The finding's note saying who paired the row. `app/api/visual_countertops.py` reads it back.

    Fixed wording on purpose: the countertop results take the pairing source from the finding that
    used it, not from whatever pairing exists when the page is opened.
    """
    words = _SOURCE_WORDS.get(pairing.source, pairing.source)
    return f"Pairing source: {pairing.source} — {words} (pairing record {pairing.record_id})."


def pairing_source_from_notes(notes: list[str] | tuple[str, ...] | None) -> PairingSource | None:
    """The pairing source a finding recorded in its notes (`pairing_source_note`), or `None`."""
    for note in notes or ():
        if not isinstance(note, str) or not note.startswith("Pairing source: "):
            continue
        word = note.removeprefix("Pairing source: ").split(" ", 1)[0]
        if word == "code":
            return "code"
        if word == "both-ais":
            return "both-ais"
        if word == "reviewer":
            return "reviewer"
        if word == "none":
            return "none"
    return None


@dataclass(frozen=True, slots=True)
class ArchitectRowPlan:
    """What to do with one row, before any value is read."""

    disposition: Disposition
    reason: str | None
    """Why nothing is compared, or why the reviewer must pair; `None` when there are pairs."""
    pairs: tuple[ComparedPair, ...]
    notes: tuple[str, ...]
    """Plain English for the finding: who paired, what was compared, what was left out and why."""
    pairing: EffectivePairing | None


def _joined(reasons: tuple[str, ...] | list[str], fallback: str) -> str:
    said = [reason.strip().rstrip(".") for reason in reasons if reason and reason.strip()]
    return ("; ".join(said) + ".") if said else fallback


def _eligible_architect_spans(session: Session, row: SlotRow) -> bool:
    """Whether the architect prints, on this row's page, a usable dimension on drawn casework."""
    for candidate in session.scalars(
        select(ObservationCandidate)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .where(
            ObservationCandidate.page_id == row.anchor.page_id,
            ObservationCandidate.document_version_id == row.anchor.document_version_id,
            ExtractionRun.extractor == ARCHITECT_TEXT_ROUTE,
            ObservationCandidate.value_numerator.is_not(None),
        )
    ):
        flags = set(candidate.ambiguity_flags or ())
        if "arch-ticks-on-outline:yes" in flags and not any(
            flag.startswith("arch-held:") for flag in flags
        ):
            return True
    return False


def plan_architect_row(
    session: Session,
    row: SlotRow,
    pairing: EffectivePairing | None,
    *,
    sides: ReadingSides | None = None,
) -> ArchitectRowPlan:
    """Decide whether this row has anything to compare with the architect, and which pairs.

    Never a pass or a fail. "Nothing comparable" is never turned into a PASS: it is no finding, with
    the reason shown beside the row (a decision flagged for Anant in #1054). `sides` lets a caller
    asking about many rows share one `ReadingSides` and its per-document answers.
    """
    if pairing is None:
        return ArchitectRowPlan(
            Disposition.NOT_COMPARED,
            "No architect dimension is paired with this row.",
            (),
            (),
            None,
        )
    if (sides or ReadingSides(session)).same_file_as_both_sides(row.anchor.document_version_id):
        return ArchitectRowPlan(
            Disposition.NOT_COMPARED,
            "The same file was uploaded as both the architect's and the vendor's drawing, so it "
            "has no separate architect side to compare with.",
            (),
            (),
            pairing,
        )
    source_note = pairing_source_note(pairing)
    pairing_notes = tuple(f"Pairing: {reason}" for reason in pairing.reasons if reason.strip())
    status = pairing.status
    if pairing.source == "none" or status in _NOTHING_COMPARABLE:
        fallback = {
            "no_scale": "The drawings' scales could not be measured, so nothing was paired.",
            "nothing_comparable": "The architect prints nothing comparable for this row.",
        }.get(status, "No architect dimension is paired with this row.")
        return ArchitectRowPlan(
            Disposition.NOT_COMPARED,
            _joined(pairing.reasons, fallback),
            (),
            (source_note, *pairing_notes),
            pairing,
        )
    if status in _AIS_UNSETTLED:
        return ArchitectRowPlan(
            Disposition.UNRESOLVED,
            PAIR_BY_REVIEWER,
            (),
            (source_note, *pairing_notes),
            pairing,
        )
    if status in _CODE_UNDECIDED:
        if not _eligible_architect_spans(session, row):
            return ArchitectRowPlan(
                Disposition.NOT_COMPARED,
                _joined(
                    pairing.reasons,
                    "The architect prints no usable dimension on drawn casework on this page.",
                ),
                (),
                (source_note, *pairing_notes),
                pairing,
            )
        return ArchitectRowPlan(
            Disposition.UNRESOLVED,
            PAIR_BY_REVIEWER,
            (),
            (source_note, *pairing_notes),
            pairing,
        )
    if status not in _DECIDED:
        return ArchitectRowPlan(
            Disposition.UNRESOLVED,
            f"The pairing's status {status!r} is not one this check knows. {PAIR_BY_REVIEWER}",
            (),
            (source_note, *pairing_notes),
            pairing,
        )
    return _plan_pairs(row, pairing, source_note, pairing_notes)


def _plan_pairs(
    row: SlotRow,
    pairing: EffectivePairing,
    source_note: str,
    pairing_notes: tuple[str, ...],
) -> ArchitectRowPlan:
    compared: list[ComparedPair] = []
    left_out: list[str] = []
    inconsistent: list[str] = []
    for pair in pairing.pairs:
        slots = pair.vendor_slot_indices
        if pair.kind == "overall":
            if slots:
                inconsistent.append("an overall pair names vendor pieces")
                continue
            compared.append(ComparedPair("overall", None, pair.architect_candidate_id))
            continue
        if pair.kind != "piece":
            inconsistent.append(f"a pair of unknown kind {pair.kind!r}")
            continue
        if not slots:
            inconsistent.append("a piece pair names no vendor piece")
            continue
        if any(slot < 0 or slot >= row.piece_count for slot in slots):
            inconsistent.append("a pair names a vendor piece this row does not have")
            continue
        if len(slots) > 1:
            left_out.append(
                f"pieces {', '.join(str(slot + 1) for slot in slots)}: the vendor splits this bay "
                f"into {len(slots)} pieces; their sum is not compared in V1"
            )
            continue
        compared.append(ComparedPair("piece", slots[0], pair.architect_candidate_id))
    keys = [pair.vendor_slot for pair in compared]
    architects = [pair.architect_candidate_id for pair in compared]
    if len(set(keys)) != len(keys):
        inconsistent.append("one vendor width is paired twice")
    if len(set(architects)) != len(architects):
        inconsistent.append("one architect dimension is paired twice")
    if inconsistent:
        return ArchitectRowPlan(
            Disposition.UNRESOLVED,
            f"The pairing record is inconsistent ({'; '.join(inconsistent)}). {PAIR_BY_REVIEWER}",
            (),
            (source_note, *pairing_notes),
            pairing,
        )
    left_notes = tuple(f"Not compared: {reason}." for reason in left_out)
    if not compared:
        return ArchitectRowPlan(
            Disposition.NOT_COMPARED,
            _joined(
                [*left_out, *pairing.reasons],
                "No architect dimension is paired one to one with this row.",
            ),
            (),
            (source_note, *pairing_notes, *left_notes),
            pairing,
        )
    ordered = sorted(
        compared, key=lambda pair: -1 if pair.vendor_slot is None else pair.vendor_slot
    )
    compared_notes = tuple(
        f"Compared: the vendor's {pair_label(pair.kind, pair.vendor_slot)} with the architect's "
        "dimension paired with it."
        for pair in ordered
    )
    return ArchitectRowPlan(
        Disposition.COMPARE,
        None,
        tuple(ordered),
        (source_note, *compared_notes, *left_notes, *pairing_notes),
        pairing,
    )
