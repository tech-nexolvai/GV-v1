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
* `architect_spans_for_row` / `record_reviewer_pairing`: what the reviewer is offered and the
  append-only record of the reviewer's choice.

`ARCHITECT_EXTRACTOR` lives here, the one home of the route the architect reader records its values
under; `workflow/architect_reader.py` and `workflow/architect_row_plan.py` import it from here.

Source: issues #1053, #1054 · Verification: `tests/workflow/test_architect_pairing_records.py`,
`tests/api/test_architect_pairing.py`, `tests/api/test_no_heavy_work.py`
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.models import DrawingView, ViewRole
from app.models.evidence import ArchitectPairingRecord, ObservationCandidate
from app.models.runs import ExtractionRun
from units.measurement import Unit
from workflow.architect_pairing_contract import EffectivePair, EffectivePairing

__all__ = [
    "ARCHITECT_EXTRACTOR",
    "DecidedPair",
    "EligibleSpan",
    "ReviewerPairingRefused",
    "ReviewerPairingStale",
    "architect_spans_for_row",
    "architect_view_tags",
    "architect_views",
    "latest_architect_pairing",
    "latest_architect_pairings",
    "latest_record",
    "record_reviewer_pairing",
]

#: The route the architect's values are recorded under. Text read by code: never a model.
ARCHITECT_EXTRACTOR: Final = "architect-text"

_AUTOMATIC: Final = ("code+ais", "code", "both-ais", "none")
_SOURCES: Final = (*_AUTOMATIC, "reviewer")
_REASON_LIMIT: Final = 500


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


def _span_tag(flags: Sequence[str]) -> str | None:
    """The tag of the view a stored span was read in: `arch-view-tag:<tag>` when the reader wrote
    one (a view drawn as page content, #1163), else the pasted drawing's `panel-<arch-view>`."""
    tag = _flag(flags, "arch-view-tag:")
    if tag is not None:
        return tag
    view = _flag(flags, "arch-view:")
    return f"panel-{view}" if view is not None and view.isdigit() else None


def architect_views(session: Session, page_id: UUID) -> set[int]:
    """The numbers of the page's drawings whose role is now the architect's: pasted drawings'
    annotation indices and views drawn as content's numbers (#1163). A number held by two views of
    which only one is the architect's counts for neither here; `architect_view_tags` tells them
    apart exactly."""
    return _architect_views_by_page(session, (page_id,)).get(page_id, set())


def architect_view_tags(session: Session, page_id: UUID) -> set[str]:
    """The tags (`panel-<n>`, `view-<n>`) of the page's drawings whose role is now the architect's."""
    return _architect_view_tags_by_page(session, (page_id,)).get(page_id, set())


def _view_roles_by_page(
    session: Session, page_ids: Collection[UUID]
) -> dict[UUID, dict[str, str | None]]:
    roles: dict[UUID, dict[str, str | None]] = {}
    if not page_ids:
        return roles
    for page_id, tag, role in session.execute(
        select(DrawingView.page_id, DrawingView.tag, DrawingView.role).where(
            DrawingView.page_id.in_(tuple(page_ids))
        )
    ):
        roles.setdefault(page_id, {})[tag] = role
    return roles


def _architect_view_tags_by_page(
    session: Session, page_ids: Collection[UUID]
) -> dict[UUID, set[str]]:
    return {
        page_id: {
            tag
            for tag, role in tags.items()
            if role == ViewRole.ARCH.value
            and (_panel_number(tag) is not None or _content_number(tag) is not None)
        }
        for page_id, tags in _view_roles_by_page(session, page_ids).items()
    }


def _architect_views_by_page(session: Session, page_ids: Collection[UUID]) -> dict[UUID, set[int]]:
    found: dict[UUID, set[int]] = {}
    for page_id, tags in _view_roles_by_page(session, page_ids).items():
        architect: set[int] = set()
        other: set[int] = set()
        for tag, role in tags.items():
            number = _panel_number(tag)
            if number is None:
                number = _content_number(tag)
            if number is None:
                continue
            (architect if role == ViewRole.ARCH.value else other).add(number)
        # `panel-<n>` and `view-<n>` share the number <n>: when only one of them is the architect's,
        # the bare number cannot say which, so it does not count (by tag it does).
        numbers = architect - other
        if numbers:
            found[page_id] = numbers
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


def _eligible(candidate: ObservationCandidate, views: Collection[str]) -> EligibleSpan:
    """`views`: the tags of the page's drawings whose role is now the architect's; a span counts
    only in the very view it was read in (`_span_tag`), never another of the same number."""
    flags = candidate.ambiguity_flags or []
    outline = _flag(flags, "arch-ticks-on-outline:")
    key = _span_key(flags)
    held = _flag(flags, "arch-held:")
    if held is None and (key is None or _span_tag(flags) not in views):
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
    session: Session, anchor: ObservationCandidate, record: ArchitectPairingRecord | None
) -> list[EligibleSpan]:
    """Every architect span the architect reader stored on the row's page, in the run the row's
    automatic pairing used. Empty when the row has no automatic pairing (the reader was off)."""
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
    views = architect_view_tags(session, anchor.page_id)
    candidates = session.scalars(
        select(ObservationCandidate)
        .where(
            ObservationCandidate.extraction_run_id == run.id,
            ObservationCandidate.page_id == anchor.page_id,
        )
        .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
    ).all()
    spans = [_eligible(candidate, views) for candidate in candidates]
    return sorted(spans, key=lambda span: (span.row or 0, span.slot or 0))


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
) -> ArchitectPairingRecord:
    """Append a reviewer's pairing for one row, superseding the latest record (not committed).

    `pairs` empty means the reviewer states that nothing on the architect's drawing is comparable.
    Refused (`ReviewerPairingRefused`): a span not stored on this row's page in its pairing's run, a
    held span, a centre-line or unknown-outline span, a vendor split that is not contiguous or not on
    this row, a span or a vendor piece used twice, more than one overall, or a row with no pairing.

    `expected_record_id` is the record the reviewer was looking at (#1088). When given and the row's
    latest record is another one, nothing is recorded (`ReviewerPairingStale`): without it, a
    reviewer confirming what they saw would silently supersede a colleague's newer decision.
    """
    current = _chain_tip(session, anchor.id)
    if expected_record_id is not None and (current is None or current.id != expected_record_id):
        raise ReviewerPairingStale(
            "This row's pairing changed after you opened it. Reload it before pairing again."
        )
    spans = {span.candidate.id: span for span in architect_spans_for_row(session, anchor, current)}
    if current is None or not spans and pairs:
        raise ReviewerPairingRefused(
            "This row has no architect reading to pair with; the architect reader did not read it."
        )
    used_spans: set[UUID] = set()
    used_pieces: set[int] = set()
    overall = 0
    for pair in pairs:
        span = spans.get(pair.architect_candidate_id)
        if span is None:
            raise ReviewerPairingRefused(
                "That architect dimension is not on this row's page of this drawing set."
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


def latest_architect_pairing(session: Session, row_anchor_id: UUID) -> EffectivePairing | None:
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
    """
    return latest_architect_pairings(session, (row_anchor_id,))[row_anchor_id]


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
    session: Session, row_anchor_ids: Collection[UUID]
) -> dict[UUID, EffectivePairing | None]:
    """`latest_architect_pairing` for many rows at once, in at most five statements.

    Same answer, row by row, as asking each row on its own: the countertop results ask every row
    of a revision and must stay within their statement bound (`tests/api/test_visual_ui.py`).
    """
    answers: dict[UUID, EffectivePairing | None] = dict.fromkeys(row_anchor_ids)
    if not answers:
        return answers
    reviewer: dict[UUID, ArchitectPairingRecord] = {}
    automatic: dict[UUID, ArchitectPairingRecord] = {}
    for record in session.scalars(
        select(ArchitectPairingRecord)
        .where(ArchitectPairingRecord.row_anchor_candidate_id.in_(tuple(answers)))
        .order_by(ArchitectPairingRecord.created_at.desc(), ArchitectPairingRecord.id.desc())
    ):
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
    views = _architect_view_tags_by_page(session, {anchor.page_id for anchor in anchors.values()})
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
        answers[anchor_id] = _effective(
            record,
            anchors.get(anchor_id),
            views,
            parsed[anchor_id],
            candidates,
            runs,
        )
    return answers


def _effective(
    record: ArchitectPairingRecord,
    anchor: ObservationCandidate | None,
    views_by_page: Mapping[UUID, set[str]],
    parsed: Sequence[_Parsed | str],
    candidates: Mapping[UUID, ObservationCandidate],
    runs: Mapping[UUID, ExtractionRun],
) -> EffectivePairing | None:
    views = set() if anchor is None else views_by_page.get(anchor.page_id, set())
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
        if (
            candidate is None
            or anchor is None
            or candidate.page_id != anchor.page_id
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
