"""Whether one vendor countertop row has anything to compare with the architect (#1054).

The light half of the vendor-vs-architect check (CT-ARCH-WIDTH-001): it reads the row's effective
pairing (#1053) through `workflow/architect_pairing_contract.py` and decides, without reading any
value, what the check does with the row:

* **nothing comparable** — no pairing, the architect prints nothing comparable, the scales could not
  be measured, or no pair is one to one: **no finding**, and a plain reason shown beside the row.
  This never creates a PASS (decision flagged for Anant in #1054: it saves a reviewer click on every
  page where the architect prints nothing comparable). Where no row of a revision is compared, one
  revision-wide NO_APPLICABLE_RULE line says so (`NOTHING_PAIRED_ON_REVISION`): seen to have run,
  never a pass, blocking nothing. When the architect's drawings came as their own file (#1161),
  which this version does not compare, each such row says so instead and the revision gets one
  REVIEW_REQUIRED line (`SEPARATE_ARCHITECT_FILE_NOT_COMPARED`) that the reviewer decides;
* **unresolved** — the two AIs disagreed or refused, or code could not decide while the architect
  does print a usable dimension on drawn casework: a REVIEW_REQUIRED finding, "pair it (one click)";
* **compare** — the one-to-one pairs, overall with overall and one architect span with one vendor
  piece. A bay the vendor splits into several pieces is left out and said so (V1 compares no sums).

**Two judgments for an automatic result.** The engine's PASS or FAIL stands only when the pairing
rests on two independent judgments — code's drawn position AND both AIs' reading of what each
dimension measures (`code+ais`) — or on a reviewer's decision. One judgment alone (`code` or
`both-ais`) sends the result, PASS or FAIL, to the reviewer to confirm the pairing
(`one_judgment_reason`); the engine's comparison is kept in the finding's notes.

**The architect's own file (#1167).** When the architect's drawings came as their own PDF and the
row's own page has no architect view, the row was matched with one view of that file (#1166), and
every row says plainly where it stands (`match`):

* the file produced no view index this revision: not compared, the package asks once (#1161);
* no match yet (checks run before matching existed): not compared, "run the checks again";
* `needs_reviewer`: REVIEW_REQUIRED, "choose which of the architect's views shows this countertop";
* `none_matches`: not compared, the reviewer found no view that shows it;
* `not_separated`: REVIEW_REQUIRED, compare by hand (that view's dimensions were not read);
* matched (`auto_matched`, `reviewer_confirmed`, `carried_over`): planned as on a combined sheet,
  against the pairing made for that view only, its notes starting with `compared_with_text`; when
  not compared, the reason starts "Matched with <file>, page N, view X: ".

A row with no match is planned exactly as before matching existed.

Imports nothing that reads a drawing, so the countertop results (`app/api/visual_countertops.py`)
can ask it too (`tests/api/test_no_heavy_work.py`).

**The pairing (#1053).** `effective_architect_pairing` reads the row's pairing record through the
light `workflow/architect_pairing_records.py` (never the module that makes pairings, which reads
drawings); `effective_architect_pairings` answers many rows in a fixed number of statements.
`DatabaseStages(architect_pairing=...)` and `countertop_results_for_revision(pairing_lookup=...)`
take any callable of the same shape, which is how the tests give fake pairings.

Source: issue #1054 · Verification: `tests/workflow/test_architect_row_evidence.py`
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.sides import ReadingSides
from app.models.document import Document, DocumentKind, PackageRevisionDocument
from app.models.evidence import ArchitectViewIndexEntry, ObservationCandidate
from app.models.runs import ExtractionRun
from workflow.architect_match_contract import (
    MATCHED_STATUSES,
    EffectiveMatch,
    MatchedView,
    compared_with_text,
)
from workflow.architect_pairing_contract import EffectivePairing, PairingSource
from workflow.architect_pairing_records import (
    ARCHITECT_EXTRACTOR,
    latest_architect_pairing,
    latest_architect_pairings,
)
from workflow.slot_row_scope import SlotRow

__all__ = [
    "ARCHITECT_CHECK_RULE_ID",
    "AUTOMATIC_SOURCES",
    "CHOOSE_ARCHITECT_VIEW",
    "MATCHED_VIEW_NOT_PAIRED",
    "NOTHING_PAIRED_ON_REVISION",
    "NOT_MATCHED_YET_ROW",
    "NO_ARCHITECT_VIEW_MATCHES",
    "PAIR_BY_REVIEWER",
    "SEPARATE_ARCHITECT_FILE_NOT_COMPARED",
    "SEPARATE_ARCHITECT_FILE_ROW",
    "ArchitectRowPlan",
    "ComparedPair",
    "Disposition",
    "PairingJudgments",
    "PairingLookup",
    "architect_file_indexed",
    "effective_architect_match",
    "effective_architect_pairing",
    "effective_architect_pairings",
    "engine_comparison_note",
    "matched_words",
    "measures_sentence",
    "not_separated_reason",
    "one_judgment_reason",
    "pair_label",
    "pairing_judgments",
    "pairing_source_from_notes",
    "pairing_source_note",
    "plan_architect_row",
]

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

_SOURCE_WORDS: Final[dict[str, str]] = {
    "code+ais": (
        "code's pairing from where both drawings are drawn and both AIs' identical pairing agree"
    ),
    "code": "decided by code from where both drawings are drawn; the AIs did not confirm it",
    "both-ais": "both AIs gave the identical pairing; code did not confirm it",
    "reviewer": "chosen by the reviewer",
    "none": "nobody has paired this row",
}
_SOURCES: Final[dict[str, PairingSource]] = {
    "code+ais": "code+ais",
    "code": "code",
    "both-ais": "both-ais",
    "reviewer": "reviewer",
    "none": "none",
}
#: The pairings an automatic PASS or FAIL may rest on: two independent judgments, or a person's.
AUTOMATIC_SOURCES: Final = frozenset({"code+ais", "reviewer"})
#: One judgment alone: who made it, in the reviewer's words.
_ONE_JUDGMENT: Final = {"code": "code", "both-ais": "the two AIs"}
type PairingJudgments = Literal["code and both AIs", "code only", "both AIs only", "reviewer"]
"""Whose judgments a pairing rests on, as the countertop results and reports say it."""

_JUDGMENTS: Final[dict[str, PairingJudgments]] = {
    "code+ais": "code and both AIs",
    "code": "code only",
    "both-ais": "both AIs only",
    "reviewer": "reviewer",
}
#: What an architect dimension measures (#1059's kinds), in plain words; others lose underscores.
_MEASURES_WORDS: Final = {
    "countertop": "the countertop",
    "cabinet_run": "cabinet runs",
    "blocking": "blocking",
    "fixture_centre": "fixture centres",
}

NOTHING_PAIRED_ON_REVISION: Final = (
    "Compared row by row with the architect's drawing: no countertop row on this revision has an "
    "architect dimension paired one to one with it, so nothing was compared. This is not a pass; "
    "each row's results say why it was not compared."
)
PAIR_BY_REVIEWER: Final = "Pair the architect's dimension with the vendor's (one click)."
#: A row not compared in a revision whose architect drawings came as their own file (#1161): this
#: version reads the architect only where it is pasted on the vendor's sheets, so it says so.
SEPARATE_ARCHITECT_FILE_ROW: Final = (
    "The architect's drawings were uploaded as a separate file, which this version does not "
    "compare yet. Compare this countertop with the architect's drawings by hand."
)
#: The one package-level REVIEW_REQUIRED line in that case (#1161), in place of
#: `NOTHING_PAIRED_ON_REVISION`: the comparison never ran, so the reviewer decides once.
SEPARATE_ARCHITECT_FILE_NOT_COMPARED: Final = (
    "The architect's drawings were uploaded as a separate file. This version does not compare "
    "them with the vendor's drawings yet. Compare the countertop widths by hand, then mark this "
    "checked."
)


#: A row whose revision's architect file was indexed (#1166) but which has no match record: the
#: checks ran on readings made before matching existed (#1167).
NOT_MATCHED_YET_ROW: Final = (
    "The architect's drawings were uploaded as a separate file and this countertop has not been "
    "matched with a view in it yet. Run the checks again."
)
#: `needs_reviewer`: code and both AIs did not agree on one view, so the reviewer picks (#1167).
CHOOSE_ARCHITECT_VIEW: Final = (
    "Choose which of the architect's views shows this countertop (one click)."
)
#: `none_matches`: the reviewer said no view shows this countertop (#1167).
NO_ARCHITECT_VIEW_MATCHES: Final = (
    "The reviewer found no view in the architect's drawings that shows this countertop, so "
    "nothing was compared."
)
#: A matched view with no pairing made for it (the match changed after the run, or the view had
#: nothing code could pair): the reviewer cannot pair it until the checks run again (#1167).
MATCHED_VIEW_NOT_PAIRED: Final = (
    "the architect's dimensions in this view have not been paired with this countertop yet. Run "
    "the checks again."
)


def matched_words(view: MatchedView) -> str:
    """`Matched with <file>, page N, view X`: how a row not compared names its matched view."""
    return f"Matched with {view.file_name}, page {view.page_number}, view {view.view_number}"


def not_separated_reason(view: MatchedView | None) -> str:
    """`not_separated`: the view chosen is not clearly apart, so nothing in it was read (#1167)."""
    which = "" if view is None else f" {view.view_number} on page {view.page_number}"
    return (
        f"The architect's view{which} is not clearly apart from its neighbour, so its dimensions "
        "were not read. Compare this countertop by hand, then mark it checked."
    )


def effective_architect_pairing(session: Session, row_anchor_id: UUID) -> EffectivePairing | None:
    """The pairing that counts for this vendor row (#1053), or `None` when it has none."""
    return latest_architect_pairing(session, row_anchor_id)


def effective_architect_match(session: Session, row_anchor_id: UUID) -> EffectiveMatch | None:
    """The row's match with a view of the architect's own file, as #1166 stores it, or `None`."""
    from workflow.architect_match_records import effective_architect_match as stored

    return stored(session, row_anchor_id)


def architect_file_indexed(session: Session, package_revision_id: UUID) -> bool:
    """Whether an architect-kind file of this revision produced at least one view index row (#1166):
    then its views are matched, and the package no longer asks once that the file was not compared
    (#1161). One statement."""
    found = session.scalar(
        select(ArchitectViewIndexEntry.id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id
            == ArchitectViewIndexEntry.document_version_id,
        )
        .join(Document, Document.id == PackageRevisionDocument.document_id)
        .where(
            PackageRevisionDocument.package_revision_id == package_revision_id,
            Document.kind == DocumentKind.ARCHITECTURAL.value,
        )
        .limit(1)
    )
    return found is not None


def effective_architect_pairings(
    session: Session, row_anchor_ids: Collection[UUID]
) -> dict[UUID, EffectivePairing | None]:
    """`effective_architect_pairing` for many rows, in a fixed number of statements."""
    return latest_architect_pairings(session, row_anchor_ids)


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
        if word in _SOURCES:
            return _SOURCES[word]
    return None


def pairing_judgments(source: str | None) -> PairingJudgments | None:
    """Whose judgments the pairing rests on, in plain words; `None` when nobody paired the row."""
    return None if source is None else _JUDGMENTS.get(source)


def one_judgment_reason(source: str, pairs: Sequence[tuple[str, str, str]]) -> str:
    """Why a result resting on one judgment of the pairing waits for the reviewer.

    `pairs` is (label, the architect's printed text, the vendor's value) for each compared pair, in
    the pairing's order. The label is named only when more than one pair was compared.
    """
    who = _ONE_JUDGMENT.get(source, f"the pairing source {source!r}")
    named = [
        f"the architect's {architect} and the vendor's {vendor}"
        + (f" ({label})" if len(pairs) > 1 else "")
        for label, architect, vendor in pairs
    ]
    both = ", and ".join(named) if named else "the paired dimensions"
    return f"Only {who} paired these; confirm that {both} measure the same thing."


def engine_comparison_note(outcome: str, reason: str) -> str:
    """The engine's own result, kept in the notes while the pairing waits for the reviewer."""
    return (
        "The engine's comparison, which counts only once a person confirms the pairing: "
        f"{outcome} — {reason.strip()}"
    )


def measures_sentence(pairing: EffectivePairing) -> str | None:
    """What the architect's dimensions on this sheet measure, as both AIs agreed; `None` if unsaid."""
    kinds: list[str] = []
    for _candidate, kind in pairing.architect_measures:
        word = _MEASURES_WORDS.get(kind, kind.replace("_", " ").strip())
        if word and word not in kinds:
            kinds.append(word)
    if not kinds:
        return None
    listed = kinds[0] if len(kinds) == 1 else f"{', '.join(kinds[:-1])} and {kinds[-1]}"
    sentence = f"The architect's dimensions on this sheet measure {listed}"
    if _MEASURES_WORDS["countertop"] not in kinds:
        sentence += ", not the countertop"
    return sentence + "."


def _not_compared_reason(pairing: EffectivePairing, reason: str) -> str:
    """The "not compared" reason, led by what the architect's dimensions measure when known."""
    sentence = measures_sentence(pairing)
    if sentence is None:
        return reason
    return f"{sentence} {reason[:1].upper()}{reason[1:]}"


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
    matched: MatchedView | None = None
    """The view of the architect's own file this row is compared against (#1167); `None` on a
    combined sheet. The verdict guard allows an architect value from this view only."""


def _joined(reasons: tuple[str, ...] | list[str], fallback: str) -> str:
    said = [reason.strip().rstrip(".") for reason in reasons if reason and reason.strip()]
    return ("; ".join(said) + ".") if said else fallback


def _eligible_architect_spans(
    session: Session, row: SlotRow, matched: MatchedView | None = None
) -> bool:
    """Whether the architect prints, on this row's page (or, #1167, in the architect view matched
    with it), a usable dimension on drawn casework."""
    page_id = row.anchor.page_id if matched is None else matched.page_id
    version_id = row.anchor.document_version_id if matched is None else matched.document_version_id
    for candidate in session.scalars(
        select(ObservationCandidate)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .where(
            ObservationCandidate.page_id == page_id,
            ObservationCandidate.document_version_id == version_id,
            ExtractionRun.extractor == ARCHITECT_EXTRACTOR,
            ObservationCandidate.value_numerator.is_not(None),
        )
    ):
        flags = set(candidate.ambiguity_flags or ())
        if matched is not None and f"arch-view:{matched.view_number}" not in flags:
            continue
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
    separate_architect_file: bool = False,
    match: EffectiveMatch | None = None,
    architect_file_indexed: bool = False,
) -> ArchitectRowPlan:
    """Decide whether this row has anything to compare with the architect, and which pairs.

    Never a pass or a fail. "Nothing comparable" is never turned into a PASS: it is no finding, with
    the reason shown beside the row (a decision flagged for Anant in #1054). `sides` lets a caller
    asking about many rows share one `ReadingSides` and its per-document answers.

    `separate_architect_file` (`app.evidence.sides.has_separate_architect_file` for the row's
    revision, #1161): a row that ends "not compared" says the architect's own file was not compared
    (`SEPARATE_ARCHITECT_FILE_ROW`) instead of a reason about the vendor's sheet. A row that is
    compared or waits for a pairing is unchanged.

    `match` (#1167) is the row's match with a view of the architect's own file (#1166), when it has
    one: the row is then planned by its match state (module docstring), and only against the
    pairing made for the matched view (the caller's lookup counts no other). `architect_file_indexed`
    (`architect_file_indexed`): the separate file produced a view index this revision, so a row
    with no match and no pairing says it was not matched yet instead of #1161's sentence. With
    `match=None` and the file not indexed, everything is exactly as before.
    """
    if match is not None:
        return _plan_matched(session, row, pairing, match, sides=sides)
    plan = _plan_architect_row(session, row, pairing, sides=sides)
    if separate_architect_file and plan.disposition is Disposition.NOT_COMPARED:
        if not architect_file_indexed:
            return replace(plan, reason=SEPARATE_ARCHITECT_FILE_ROW)
        if pairing is None:
            return replace(plan, reason=NOT_MATCHED_YET_ROW)
    return plan


def _plan_matched(
    session: Session,
    row: SlotRow,
    pairing: EffectivePairing | None,
    match: EffectiveMatch,
    *,
    sides: ReadingSides | None,
) -> ArchitectRowPlan:
    """A row matched (or waiting to be matched) with a view of the architect's own file (#1167)."""
    notes = tuple(f"Match: {reason}" for reason in match.reasons if reason and reason.strip())
    if match.status == "needs_reviewer":
        return ArchitectRowPlan(Disposition.UNRESOLVED, CHOOSE_ARCHITECT_VIEW, (), notes, None)
    if match.status == "none_matches":
        return ArchitectRowPlan(
            Disposition.NOT_COMPARED, NO_ARCHITECT_VIEW_MATCHES, (), notes, None
        )
    if match.status == "not_separated":
        return ArchitectRowPlan(
            Disposition.UNRESOLVED, not_separated_reason(match.matched), (), notes, None
        )
    view = match.matched
    if match.status not in MATCHED_STATUSES or view is None:
        # `no_candidates`: the architect's file has no view at all, so it was not compared; the
        # package asks once, as when the file produced no index (#1161).
        return ArchitectRowPlan(
            Disposition.NOT_COMPARED, SEPARATE_ARCHITECT_FILE_ROW, (), notes, None
        )
    if pairing is None:
        return ArchitectRowPlan(
            Disposition.UNRESOLVED,
            f"{matched_words(view)}: {MATCHED_VIEW_NOT_PAIRED}",
            (),
            (f"{matched_words(view)}.", *notes),
            None,
        )
    plan = _plan_architect_row(session, row, pairing, sides=sides, matched=view)
    if plan.disposition is Disposition.NOT_COMPARED:
        reason = plan.reason or "No architect dimension is paired with this row."
        return replace(plan, reason=f"{matched_words(view)}: {reason}", matched=view)
    if plan.disposition is Disposition.COMPARE:
        return replace(plan, notes=(compared_with_text(view), *plan.notes), matched=view)
    return replace(plan, notes=(f"{matched_words(view)}.", *plan.notes), matched=view)


def _plan_architect_row(
    session: Session,
    row: SlotRow,
    pairing: EffectivePairing | None,
    *,
    sides: ReadingSides | None,
    matched: MatchedView | None = None,
) -> ArchitectRowPlan:
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
    # **Unsettled before "nothing paired" (#1088).** `combine()` records an AI disagreement or
    # refusal, and code that could not decide, with `source="none"`: nobody paired the row.
    # The AIs are asked only when the architect prints a usable dimension on this page
    # (`_ask_the_ais`), so their disagreement means something may be comparable, and the reviewer
    # pairs it (Decision log 2026-10-09). Checked first, or it would read as "not compared".
    if status in _AIS_UNSETTLED:
        return ArchitectRowPlan(
            Disposition.UNRESOLVED,
            PAIR_BY_REVIEWER,
            (),
            (source_note, *pairing_notes),
            pairing,
        )
    if status in _CODE_UNDECIDED:
        if not _eligible_architect_spans(session, row, matched):
            return ArchitectRowPlan(
                Disposition.NOT_COMPARED,
                _not_compared_reason(
                    pairing,
                    _joined(
                        pairing.reasons,
                        "The architect prints no usable dimension on drawn casework on this page.",
                    ),
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
    if pairing.source == "none" or status in _NOTHING_COMPARABLE:
        fallback = {
            "no_scale": "The drawings' scales could not be measured, so nothing was paired.",
            "nothing_comparable": "The architect prints nothing comparable for this row.",
        }.get(status, "No architect dimension is paired with this row.")
        return ArchitectRowPlan(
            Disposition.NOT_COMPARED,
            _not_compared_reason(pairing, _joined(pairing.reasons, fallback)),
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
            _not_compared_reason(
                pairing,
                _joined(
                    [*left_out, *pairing.reasons],
                    "No architect dimension is paired one to one with this row.",
                ),
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
