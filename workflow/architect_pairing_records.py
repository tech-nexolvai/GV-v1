"""The architect pairing records, read and appended without reading a drawing (#1053, #1054).

`workflow/architect_pairing.py` makes a pairing (code by drawn position, then both AIs) and so
imports the drawing readers. Everything here only reads and appends the stored records, so the
control plane may import it: the countertop results ask every row's pairing
(`app/api/visual_countertops.py`, through `workflow/architect_row_plan.py`) and the reviewer pairs
with one click (`app/api/slot_rows.py`). `tests/api/test_no_heavy_work.py` keeps it that way.
Imports: SQLAlchemy, `app.models`, the pairing contract, `units` and the standard library only.

* `latest_architect_pairing` / `latest_architect_pairings`: the pairing that counts for a row
  (the reviewer's latest, else the latest automatic record), every pair re-checked against the
  architect candidate as it stands now. The batched form answers many rows in a fixed number of
  statements;
* `architect_spans_for_row` / `record_reviewer_pairing`: what the reviewer is offered (nothing for
  a record made under a match that no longer stands, #1167) and the
  append-only record of the reviewer's choice;
* `record_code_pairing_for_view` (#1167): after a reviewer chose which view of the architect's own
  file shows the row (#1166), code's pairing against that view, as the matcher computed it, becomes
  the row's pairing (one judgment: the reviewer confirms it before a result on it counts).

**Which records count (#1167).** A record made against a view of the architect's own file names it
(`details.architect_view_id`) and counts only while that view is the row's effective match
(`auto_matched`, `reviewer_confirmed` or `carried_over`) AND was made under that very match
record (`details.match_record_id`); its pairs must lie in that view (its page,
its file version and `arch-view:<n>`). A record that names no view is a combined sheet's: its pairs
must lie on the row's own page, exactly as before, and no match is ever looked up for it.

`ARCHITECT_EXTRACTOR` lives here, the one home of the route the architect reader records its values
under; `workflow/architect_reader.py` and `workflow/architect_row_plan.py` import it from here.

Source: issues #1053, #1054 · Verification: `tests/workflow/test_architect_pairing_records.py`,
`tests/api/test_architect_pairing.py`, `tests/api/test_no_heavy_work.py`
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.models import DrawingView, ViewRole
from app.models.document import Page
from app.models.evidence import (
    ArchitectPairingRecord,
    ArchitectViewIndexEntry,
    ArchitectViewMatchRecord,
    ObservationCandidate,
)
from app.models.runs import ExtractionRun
from units.measurement import Unit
from workflow.architect_match_contract import MATCHED_STATUSES, EffectiveMatch
from workflow.architect_pairing_contract import EffectivePair, EffectivePairing

__all__ = [
    "ARCHITECT_EXTRACTOR",
    "DecidedPair",
    "EligibleSpan",
    "MatchesLookup",
    "ReviewerPairingRefused",
    "ReviewerPairingStale",
    "architect_spans_for_row",
    "architect_views",
    "latest_architect_pairing",
    "latest_architect_pairings",
    "latest_record",
    "record_code_pairing_for_view",
    "record_reviewer_pairing",
    "record_view",
]

#: The route the architect's values are recorded under. Text read by code: never a model.
ARCHITECT_EXTRACTOR: Final = "architect-text"

_AUTOMATIC: Final = ("code+ais", "code", "both-ais", "none")
_SOURCES: Final = (*_AUTOMATIC, "reviewer")
_REASON_LIMIT: Final = 500
#: Code's own pairing statuses (`extraction.architect.pairing.PairingStatus`), restated: this module
#: reads no drawing code.
_CODE_STATUSES: Final = frozenset(
    {"paired", "ambiguous", "no_fit", "no_scale", "nothing_comparable"}
)
#: What a record made against a view of the architect's own file says about the view (#1167).
_VIEW_KEYS: Final = (
    "architect_view_id",
    "architect_document_version_id",
    "architect_page_id",
    "architect_page_index",
    "architect_view_number",
    "architect_view_tag",
    "match_record_id",
)

type MatchesLookup = Callable[[Session, Collection[UUID]], Mapping[UUID, EffectiveMatch | None]]
"""`effective_architect_matches(session, row_anchor_ids)` from #1166, or anything shaped like it."""
type MatchLookup = Callable[[Session, UUID], EffectiveMatch | None]


def _stored_matches(
    session: Session, row_anchor_ids: Collection[UUID]
) -> Mapping[UUID, EffectiveMatch | None]:
    """The rows' effective matches as #1166 stores them, read only for records that name a view."""
    from workflow.architect_match_records import effective_architect_matches

    return effective_architect_matches(session, row_anchor_ids)


def _stored_match(session: Session, row_anchor_id: UUID) -> EffectiveMatch | None:
    return _stored_matches(session, (row_anchor_id,)).get(row_anchor_id)


def record_view(record: ArchitectPairingRecord) -> UUID | None:
    """The architect view (`architect_view_index` id) a record was made against (#1167), or `None`
    for a combined sheet's record, made on the row's own page."""
    stored = (record.details or {}).get("architect_view_id")
    if stored is None:
        return None
    try:
        return UUID(str(stored))
    except ValueError:
        # Names a view this code cannot read: never the row's match, so it never counts.
        return UUID(int=0)


def _matched_view_id(match: EffectiveMatch | None) -> UUID | None:
    """The view the row is compared against now, or `None` (no match, or not a compared state)."""
    if match is None or match.status not in MATCHED_STATUSES or match.matched is None:
        return None
    return match.matched.view_id


def _counts(record: ArchitectPairingRecord, match: EffectiveMatch | None) -> bool:
    """A record counts when it names no view (a combined sheet's), or when it was made under the
    row's match as it stands now: the same view AND the same match record (`match_record_id`).

    The view alone is not enough: after a reviewer moves the match away and back to the same view,
    a pairing made under the earlier match must not come back to life; the new match brings its
    own pairing (`record_code_pairing_for_view`). A view record with no match record counts never.
    """
    view = record_view(record)
    if view is None:
        return True
    if match is None or view != _matched_view_id(match):
        return False
    return str((record.details or {}).get("match_record_id")) == str(match.record_id)


@dataclass(frozen=True, slots=True)
class _Where:
    """Where a record's architect dimensions may lie: the row's own page, or one matched view."""

    page_id: UUID
    document_version_id: UUID
    view_number: int | None
    """`arch-view:<n>` for a matched view; `None` on the row's own page (any confirmed drawing)."""


def _view_where(details: Mapping[str, object]) -> _Where | None:
    """The matched view a record names, from its details; `None` when it names none or is
    unreadable (then nothing in it counts)."""
    try:
        return _Where(
            page_id=UUID(str(details["architect_page_id"])),
            document_version_id=UUID(str(details["architect_document_version_id"])),
            view_number=int(str(details["architect_view_number"])),
        )
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class DecidedPair:
    kind: Literal["piece", "overall"]
    architect_candidate_id: UUID
    vendor_slot_indices: tuple[int, ...]
    """Contiguous `slot:<i>` indices; empty for the overall."""

    def as_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "architect_candidate_id": str(self.architect_candidate_id),
            "vendor_slot_indices": list(self.vendor_slot_indices),
        }


def _contiguous(indices: Sequence[int]) -> bool:
    ordered = sorted(indices)
    return all(right == left + 1 for left, right in pairwise(ordered))


def _flag(flags: Iterable[str], prefix: str) -> str | None:
    return next((flag.removeprefix(prefix) for flag in flags if flag.startswith(prefix)), None)


def _span_key(flags: Sequence[str]) -> tuple[int, int, int] | None:
    parts = [_flag(flags, prefix) for prefix in ("arch-view:", "arch-row:", "arch-slot:")]
    if any(part is None or not part.isdigit() for part in parts):
        return None
    view, rank, slot = (int(part) for part in parts if part is not None)
    return view, rank, slot


def _panel_number(tag: str) -> int | None:
    number = tag.removeprefix("panel-")
    return int(number) if tag.startswith("panel-") and number.isdigit() else None


def _content_number(tag: str) -> int | None:
    number = tag.removeprefix("view-")
    return int(number) if tag.startswith("view-") and number.isdigit() else None


def architect_views(session: Session, page_id: UUID) -> set[int]:
    """The annotation indices of the page's pasted drawings whose role is now the architect's, and
    the numbers of its views drawn as the page's own content whose role is (#1163)."""
    return _architect_views_by_page(session, (page_id,)).get(page_id, set())


def _architect_views_by_page(session: Session, page_ids: Collection[UUID]) -> dict[UUID, set[int]]:
    found: dict[UUID, set[int]] = {}
    if not page_ids:
        return found
    panels: dict[UUID, set[int]] = {}
    contents: dict[UUID, set[int]] = {}
    for page_id, tag, role in session.execute(
        select(DrawingView.page_id, DrawingView.tag, DrawingView.role).where(
            DrawingView.page_id.in_(tuple(page_ids))
        )
    ):
        panel = _panel_number(tag)
        content = _content_number(tag)
        if panel is not None:
            panels.setdefault(page_id, set()).add(panel)
            if role == ViewRole.ARCH.value:
                found.setdefault(page_id, set()).add(panel)
        elif content is not None and role == ViewRole.ARCH.value:
            contents.setdefault(page_id, set()).add(content)
    # A view drawn as the page's own content (#1163) shares the `arch-view:<n>` key with a pasted
    # drawing's annotation index. The reader never gives one page both, but should a page hold a
    # `panel-<n>` and a `view-<n>` at once, neither number counts: which drawing a value came from
    # would be a guess.
    for page_id, numbers in contents.items():
        clash = numbers & panels.get(page_id, set())
        found.setdefault(page_id, set()).update(numbers - clash)
        if clash:
            found[page_id] -= clash
    return found


# --- the read path and the reviewer ----------------------------------------------------------------


def latest_record(
    session: Session, row_anchor_id: UUID, *, sources: Collection[str] | None = None
) -> ArchitectPairingRecord | None:
    """The newest record for this row, optionally only of these sources."""
    query = select(ArchitectPairingRecord).where(
        ArchitectPairingRecord.row_anchor_candidate_id == row_anchor_id
    )
    if sources is not None:
        query = query.where(ArchitectPairingRecord.source.in_(tuple(sources)))
    return session.execute(
        query.order_by(
            ArchitectPairingRecord.created_at.desc(), ArchitectPairingRecord.id.desc()
        ).limit(1)
    ).scalar_one_or_none()


def _chain_tip(session: Session, row_anchor_id: UUID) -> ArchitectPairingRecord | None:
    later = aliased(ArchitectPairingRecord)
    return session.execute(
        select(ArchitectPairingRecord)
        .where(
            ArchitectPairingRecord.row_anchor_candidate_id == row_anchor_id,
            ~exists().where(later.supersedes_id == ArchitectPairingRecord.id),
        )
        .order_by(ArchitectPairingRecord.created_at.desc(), ArchitectPairingRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class EligibleSpan:
    """One architect span on the row's page, as the reviewer is offered it."""

    candidate: ObservationCandidate
    row: int | None
    slot: int | None
    on_outline: bool | None
    held_reason: str | None
    inches: Fraction | None

    @property
    def comparable(self) -> bool:
        return self.on_outline is True and self.held_reason is None and self.inches is not None

    def refusal(self) -> str:
        if self.on_outline is False:
            return "it runs to a fixture's centre line, so it never measures a cabinet"
        if self.on_outline is None:
            return "its ends are not known to sit on the casework outline"
        return f"its value is held: {self.held_reason or 'no value was stored'}"


def _eligible(candidate: ObservationCandidate, views: Collection[int]) -> EligibleSpan:
    flags = candidate.ambiguity_flags or []
    outline = _flag(flags, "arch-ticks-on-outline:")
    key = _span_key(flags)
    held = _flag(flags, "arch-held:")
    if held is None and (key is None or key[0] not in views):
        held = "this drawing is no longer confirmed as the architect's"
    inches = (
        None
        if candidate.value_numerator is None
        or candidate.value_denominator is None
        or candidate.value_denominator <= 0
        or candidate.unit != Unit.INCH.value
        else Fraction(candidate.value_numerator, candidate.value_denominator)
    )
    if held is None and inches is None:
        held = "no value was stored"
    return EligibleSpan(
        candidate=candidate,
        row=None if key is None else key[1],
        slot=None if key is None else key[2],
        on_outline={"yes": True, "no": False}.get(outline or ""),
        held_reason=held,
        inches=inches,
    )


def architect_spans_for_row(
    session: Session,
    anchor: ObservationCandidate,
    record: ArchitectPairingRecord | None,
    *,
    match_lookup: MatchLookup | None = None,
) -> list[EligibleSpan]:
    """Every architect span the architect reader stored on the row's page, in the run the row's
    automatic pairing used. Empty when the row has no automatic pairing (the reader was off).

    For a record made against a view of the architect's own file (#1167): only that view's spans,
    and none at all when the record no longer counts (the row's match moved since; `match_lookup`,
    by default #1166's stored match)."""
    if (
        record is not None
        and record_view(record) is not None
        and not _counts(record, (match_lookup or _stored_match)(session, anchor.id))
    ):
        return []
    automatic = record
    while automatic is not None and automatic.source == "reviewer":
        if automatic.supersedes_id is None:
            automatic = None
            break
        automatic = session.get(ArchitectPairingRecord, automatic.supersedes_id)
    if automatic is None:
        return []
    run_id = automatic.details.get("architect_run_id")
    if not isinstance(run_id, str):
        return []
    run = session.get(ExtractionRun, UUID(run_id))
    if run is None or run.extractor != ARCHITECT_EXTRACTOR:
        return []
    where = _Where(anchor.page_id, anchor.document_version_id, None)
    if record_view(automatic) is not None:
        # Paired against a view of the architect's own file (#1167): only that view's spans.
        named = _view_where(automatic.details)
        if named is None:
            return []
        where = named
    views = architect_views(session, where.page_id)
    query = select(ObservationCandidate).where(
        ObservationCandidate.extraction_run_id == run.id,
        ObservationCandidate.page_id == where.page_id,
    )
    if where.view_number is not None:
        query = query.where(ObservationCandidate.document_version_id == where.document_version_id)
    candidates = [
        candidate
        for candidate in session.scalars(
            query.order_by(ObservationCandidate.created_at, ObservationCandidate.id)
        )
        if _in_view(candidate, where.view_number)
    ]
    spans = [_eligible(candidate, views) for candidate in candidates]
    return sorted(spans, key=lambda span: (span.row or 0, span.slot or 0))


def _in_view(candidate: ObservationCandidate, view_number: int | None) -> bool:
    """`True` on the row's own page (`view_number` `None`); else only `arch-view:<view_number>`."""
    if view_number is None:
        return True
    key = _span_key(candidate.ambiguity_flags or ())
    return key is not None and key[0] == view_number


class ReviewerPairingRefused(ValueError):
    """A reviewer's pairing that may not be recorded, with the reason in plain words."""


class ReviewerPairingStale(ValueError):
    """The row's pairing changed after the reviewer looked at it (#1088): reload, then pair again."""


def record_reviewer_pairing(
    session: Session,
    *,
    anchor: ObservationCandidate,
    package_revision_id: UUID,
    piece_count: int,
    pairs: Sequence[DecidedPair],
    note: str | None,
    actor: str,
    expected_record_id: UUID | None = None,
    match_lookup: MatchLookup | None = None,
) -> ArchitectPairingRecord:
    """Append a reviewer's pairing for one row, superseding the latest record (not committed).

    `pairs` empty means the reviewer states that nothing on the architect's drawing is comparable.
    Refused (`ReviewerPairingRefused`): a span not stored on this row's page in its pairing's run, a
    held span, a centre-line or unknown-outline span, a vendor split that is not contiguous or not on
    this row, a span or a vendor piece used twice, more than one overall, or a row with no pairing.

    `expected_record_id` is the record the reviewer was looking at (#1088). When given and the row's
    latest record is another one, nothing is recorded (`ReviewerPairingStale`): without it, a
    reviewer confirming what they saw would silently supersede a colleague's newer decision.

    A row paired against a view of the architect's own file (#1167) is paired again only while that
    view is still its match (`match_lookup`, by default #1166's stored match): the reviewer's
    record names the same view, and only that view's spans may be paired.
    """
    current = _chain_tip(session, anchor.id)
    if expected_record_id is not None and (current is None or current.id != expected_record_id):
        raise ReviewerPairingStale(
            "This row's pairing changed after you opened it. Reload it before pairing again."
        )
    view_details: dict[str, object] = {}
    match: EffectiveMatch | None = None
    if current is not None and record_view(current) is not None:
        match = (match_lookup or _stored_match)(session, anchor.id)
        if match is None or not _counts(current, match):
            raise ReviewerPairingRefused(
                "The architect view matched with this row has changed since this pairing was "
                "made, so it cannot be paired from it."
            )
        view_details = {key: current.details[key] for key in _VIEW_KEYS if key in current.details}
        view_details["match_record_id"] = str(match.record_id)
    found = architect_spans_for_row(
        session, anchor, current, match_lookup=lambda _session, _anchor: match
    )
    spans = {span.candidate.id: span for span in found}
    if current is None or not spans and pairs:
        raise ReviewerPairingRefused(
            "This row has no dimension of the architect's view matched with it to pair with."
            if view_details
            else "This row has no architect reading to pair with; the architect reader did not "
            "read it."
        )
    used_spans: set[UUID] = set()
    used_pieces: set[int] = set()
    overall = 0
    for pair in pairs:
        span = spans.get(pair.architect_candidate_id)
        if span is None:
            raise ReviewerPairingRefused(
                "That architect dimension is not in the architect's view matched with this row."
                if view_details
                else "That architect dimension is not on this row's page of this drawing set."
            )
        if not span.comparable:
            raise ReviewerPairingRefused(
                f"The architect's {span.candidate.raw_text} cannot be paired: {span.refusal()}."
            )
        if span.candidate.id in used_spans:
            raise ReviewerPairingRefused("Each architect dimension can be paired only once.")
        used_spans.add(span.candidate.id)
        if pair.kind == "overall":
            overall += 1
            if pair.vendor_slot_indices:
                raise ReviewerPairingRefused("The overall pairs with the whole row, not pieces.")
            continue
        indices = pair.vendor_slot_indices
        if not indices or any(index < 0 or index >= piece_count for index in indices):
            raise ReviewerPairingRefused("Choose one or more of this row's own pieces.")
        if not _contiguous(indices) or len(set(indices)) != len(indices):
            raise ReviewerPairingRefused(
                "Pieces paired with one architect dimension must be next to each other."
            )
        if used_pieces & set(indices):
            raise ReviewerPairingRefused("Each vendor piece can be paired only once.")
        used_pieces.update(indices)
    if overall > 1:
        raise ReviewerPairingRefused("Only one architect dimension can pair with the overall.")
    record = ArchitectPairingRecord(
        package_revision_id=package_revision_id,
        page_id=anchor.page_id,
        row_anchor_candidate_id=anchor.id,
        extraction_run_id=None,
        source="reviewer",
        status="reviewer",
        pairs=[
            DecidedPair(
                pair.kind, pair.architect_candidate_id, tuple(sorted(pair.vendor_slot_indices))
            ).as_json()
            for pair in pairs
        ],
        details={
            **view_details,
            "note": None if note is None else note[:_REASON_LIMIT],
            "architect_run_id": _run_of(current),
            "reasons": [
                (
                    "A reviewer paired the architect's dimensions with this row."
                    if pairs
                    else "A reviewer states that nothing on the architect's drawing is "
                    "comparable with this row."
                )
            ],
        },
        supersedes_id=current.id,
        decided_by=actor,
    )
    session.add(record)
    return record


def _run_of(record: ArchitectPairingRecord) -> object:
    return record.details.get("architect_run_id")


def record_code_pairing_for_view(
    session: Session,
    *,
    anchor: ObservationCandidate,
    package_revision_id: UUID,
    match_record: ArchitectViewMatchRecord,
) -> ArchitectPairingRecord | None:
    """After a reviewer chose the architect view that shows this row (#1166), append code's pairing
    against that view as the row's pairing (not committed), superseding the latest record.

    The pairing is the one the matcher computed for that candidate view in the run
    (`candidates[].code_pairing`, code's drawn-position maths only), so it is one judgment: `code`
    when it pairs, else `none`. A PASS or FAIL never rests on it before a reviewer confirms the
    pairing (`AUTOMATIC_SOURCES`), and the read path re-checks every pair against the view. `None`,
    and nothing appended, when the record names no compared view or holds no readable pairing for
    it: the row then says it waits for a re-run.
    """
    if match_record.row_anchor_candidate_id != anchor.id:
        raise ValueError("the match record belongs to another row")
    if match_record.status not in MATCHED_STATUSES or match_record.matched_view_id is None:
        return None
    view = session.get(ArchitectViewIndexEntry, match_record.matched_view_id)
    page = None if view is None else session.get(Page, view.page_id)
    if view is None or page is None:
        return None
    stored = next(
        (
            candidate.get("code_pairing")
            for candidate in match_record.candidates or ()
            if isinstance(candidate, dict) and str(candidate.get("view_id")) == str(view.id)
        ),
        None,
    )
    if not isinstance(stored, dict):
        return None
    status, raw_pairs, raw_details = (
        stored.get("status"),
        stored.get("pairs"),
        stored.get("details"),
    )
    if status not in _CODE_STATUSES or not isinstance(raw_pairs, list):
        return None
    pairs: list[dict[str, object]] = []
    for raw in raw_pairs:
        item = _parsed(raw) if isinstance(raw, dict) else "unreadable"
        if isinstance(item, str):
            return None
        kind, candidate_id, indices = item
        pairs.append(DecidedPair(kind, candidate_id, tuple(sorted(indices))).as_json())
    source = "code" if status == "paired" and pairs else "none"
    details: dict[str, object] = dict(raw_details) if isinstance(raw_details, dict) else {}
    details.setdefault("architect_run_id", str(view.extraction_run_id))
    stored_reasons = details.get("reasons")
    reasons = [str(reason) for reason in stored_reasons] if isinstance(stored_reasons, list) else []
    reasons.append(
        "Code paired this row against the architect's view the reviewer chose; a reviewer "
        "confirms the pairing before any result on it counts."
        if source == "code"
        else "Code found nothing to pair in the architect's view the reviewer chose."
    )
    current = _chain_tip(session, anchor.id)
    record = ArchitectPairingRecord(
        package_revision_id=package_revision_id,
        page_id=anchor.page_id,
        row_anchor_candidate_id=anchor.id,
        extraction_run_id=anchor.extraction_run_id,
        source=source,
        status=status,
        pairs=pairs if source == "code" else [],
        details={
            **details,
            "architect_view_id": str(view.id),
            "architect_document_version_id": str(view.document_version_id),
            "architect_page_id": str(view.page_id),
            "architect_page_index": page.index,
            "architect_view_number": view.view_number,
            "architect_view_tag": view.view_tag,
            "match_record_id": str(match_record.id),
            "reasons": reasons,
        },
        supersedes_id=None if current is None else current.id,
        decided_by=None,
    )
    session.add(record)
    return record


def latest_architect_pairing(
    session: Session, row_anchor_id: UUID, *, matches: MatchesLookup | None = None
) -> EffectivePairing | None:
    """The pairing that counts for one vendor countertop row (its `slot:0` candidate), or `None`.

    The reviewer's latest record wins; otherwise the latest automatic record (code and both AIs,
    code alone, both AIs alone, or nobody). Its pairs are re-checked against the architect
    candidates as they stand: only a span that is stored on the row's page, unheld, has a value,
    sits on the drawn outline and is in a drawing still confirmed as the architect's is returned;
    any other pair is left out with the reason. `vendor_slot_indices` is empty for the overall.
    Only `code+ais` (two independent judgments) or `reviewer` stands on its own: a result resting
    on `code` or `both-ais` needs a reviewer's confirmation of the pairing (the rule's job, T3).
    `architect_measures` is what both AIs agreed each architect dimension on the page measures
    (an automatic record only).

    A record made against a view of the architect's own file (#1167) counts only while that view
    is the row's match (`matches`, by default #1166's stored matches); its pairs must lie in that
    view. When no record counts (the match changed since), the answer is `None`.
    """
    return latest_architect_pairings(session, (row_anchor_id,), matches=matches)[row_anchor_id]


type _Parsed = tuple[Literal["piece", "overall"], UUID, tuple[int, ...]]


def _parsed(raw: dict[str, object]) -> _Parsed | str:
    """One stored pair, or the reason it is left out before any candidate is looked at."""
    try:
        candidate_id = UUID(str(raw["architect_candidate_id"]))
        kind = raw["kind"]
        stored_indices = raw.get("vendor_slot_indices")
        if not isinstance(stored_indices, list):
            raise TypeError("vendor_slot_indices is not a list")
        indices = tuple(int(index) for index in stored_indices)
    except (KeyError, TypeError, ValueError):
        return "A stored pair could not be read and is left out."
    if kind == "piece":
        return "piece", candidate_id, indices
    if kind == "overall":
        return "overall", candidate_id, indices
    return "A stored pair has no kind and is left out."


def latest_architect_pairings(
    session: Session,
    row_anchor_ids: Collection[UUID],
    *,
    matches: MatchesLookup | None = None,
) -> dict[UUID, EffectivePairing | None]:
    """`latest_architect_pairing` for many rows at once, in at most five statements, plus the
    match lookup's own only when a record names a view of the architect's own file (#1167).

    Same answer, row by row, as asking each row on its own: the countertop results ask every row
    of a revision and must stay within their statement bound (`tests/api/test_visual_ui.py`).
    """
    answers: dict[UUID, EffectivePairing | None] = dict.fromkeys(row_anchor_ids)
    if not answers:
        return answers
    records = list(
        session.scalars(
            select(ArchitectPairingRecord)
            .where(ArchitectPairingRecord.row_anchor_candidate_id.in_(tuple(answers)))
            .order_by(ArchitectPairingRecord.created_at.desc(), ArchitectPairingRecord.id.desc())
        )
    )
    viewed = {
        record.row_anchor_candidate_id for record in records if record_view(record) is not None
    }
    found_matches = (matches or _stored_matches)(session, viewed) if viewed else {}
    matched_views = {
        anchor_id: match.matched
        for anchor_id in viewed
        if (match := found_matches.get(anchor_id)) is not None
        and _matched_view_id(match) is not None
        and match.matched is not None
    }
    reviewer: dict[UUID, ArchitectPairingRecord] = {}
    automatic: dict[UUID, ArchitectPairingRecord] = {}
    for record in records:
        if not _counts(record, found_matches.get(record.row_anchor_candidate_id)):
            continue
        if record.source == "reviewer":
            reviewer.setdefault(record.row_anchor_candidate_id, record)
        elif record.source in _AUTOMATIC:
            automatic.setdefault(record.row_anchor_candidate_id, record)
    chosen = {
        anchor_id: found
        for anchor_id in answers
        if (found := reviewer.get(anchor_id) or automatic.get(anchor_id)) is not None
    }
    if not chosen:
        return answers
    anchors = {
        candidate.id: candidate
        for candidate in session.scalars(
            select(ObservationCandidate).where(ObservationCandidate.id.in_(tuple(chosen)))
        )
    }
    views = _architect_views_by_page(
        session,
        {anchor.page_id for anchor in anchors.values()}
        | {view.page_id for view in matched_views.values()},
    )
    parsed = {
        anchor_id: [_parsed(raw) for raw in record.pairs] for anchor_id, record in chosen.items()
    }
    named = {item[1] for items in parsed.values() for item in items if not isinstance(item, str)}
    candidates = (
        {
            candidate.id: candidate
            for candidate in session.scalars(
                select(ObservationCandidate).where(ObservationCandidate.id.in_(tuple(named)))
            )
        }
        if named
        else {}
    )
    run_ids = {candidate.extraction_run_id for candidate in candidates.values()}
    runs = (
        {
            run.id: run
            for run in session.scalars(
                select(ExtractionRun).where(ExtractionRun.id.in_(tuple(run_ids)))
            )
        }
        if run_ids
        else {}
    )
    for anchor_id, record in chosen.items():
        anchor = anchors.get(anchor_id)
        where: _Where | None = (
            None if anchor is None else _Where(anchor.page_id, anchor.document_version_id, None)
        )
        if record_view(record) is not None:
            stated = _view_where(record.details)
            view = matched_views.get(anchor_id)
            where = (
                stated
                if stated is not None
                and view is not None
                and (stated.page_id, stated.document_version_id, stated.view_number)
                == (view.page_id, view.document_version_id, view.view_number)
                else None
            )
        answers[anchor_id] = _effective(record, where, views, parsed[anchor_id], candidates, runs)
    return answers


def _effective(
    record: ArchitectPairingRecord,
    where: _Where | None,
    views_by_page: Mapping[UUID, set[int]],
    parsed: Sequence[_Parsed | str],
    candidates: Mapping[UUID, ObservationCandidate],
    runs: Mapping[UUID, ExtractionRun],
) -> EffectivePairing | None:
    """One record re-checked: every pair must lie `where` (the row's own page, or the matched view
    the record names, #1167) and still be a usable architect value. `where` is `None` when the
    row's anchor is gone or the view the record names is not the row's match: nothing counts."""
    views = set() if where is None else views_by_page.get(where.page_id, set())
    stored_reasons = record.details.get("reasons")
    reasons = [str(reason) for reason in stored_reasons] if isinstance(stored_reasons, list) else []
    pairs: list[EffectivePair] = []
    for item in parsed:
        if isinstance(item, str):
            reasons.append(item)
            continue
        kind, candidate_id, indices = item
        candidate = candidates.get(candidate_id)
        run = None if candidate is None else runs.get(candidate.extraction_run_id)
        if where is not None and where.view_number is not None:
            if (
                candidate is None
                or candidate.page_id != where.page_id
                or candidate.document_version_id != where.document_version_id
                or not _in_view(candidate, where.view_number)
                or run is None
                or run.extractor != ARCHITECT_EXTRACTOR
            ):
                reasons.append(
                    "A paired architect dimension is not in the architect's view matched with "
                    "this row; left out."
                )
                continue
        elif (
            candidate is None
            or where is None
            or candidate.page_id != where.page_id
            or run is None
            or run.extractor != ARCHITECT_EXTRACTOR
        ):
            reasons.append("A paired architect dimension is not on this row's page; left out.")
            continue
        span = _eligible(candidate, views)
        if not span.comparable:
            reasons.append(f"The architect's {candidate.raw_text} is left out: {span.refusal()}.")
            continue
        if kind == "piece" and (not indices or not _contiguous(indices)):
            reasons.append("A paired vendor split is not contiguous; left out.")
            continue
        pairs.append(
            EffectivePair(
                kind=kind,
                architect_candidate_id=candidate_id,
                vendor_slot_indices=() if kind == "overall" else indices,
            )
        )
    source = record.source
    if source not in _SOURCES:
        return None
    return EffectivePairing(
        record_id=record.id,
        source=source,  # type: ignore[arg-type]
        status=record.status,
        pairs=tuple(pairs),
        reasons=tuple(reasons),
        architect_measures=_measures(record.details.get("architect_measures")),
    )


def _measures(stored: object) -> tuple[tuple[UUID, str], ...]:
    """The stored `{candidate id: measure}` the two AIs agreed on, in a fixed order; anything that
    cannot be read is left out."""
    found: list[tuple[UUID, str]] = []
    for key, measure in stored.items() if isinstance(stored, dict) else ():
        try:
            candidate_id = UUID(str(key))
        except ValueError:
            continue
        if isinstance(measure, str):
            found.append((candidate_id, measure))
    return tuple(sorted(found, key=lambda item: str(item[0])))
