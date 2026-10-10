"""What the countertop results say about each row's architect view (#1168), read-only.

When the architect's drawings are their own file and that file was indexed this revision (#1166),
every vendor countertop row is matched with one view of it, automatically or by the reviewer. This
module turns the stored match records into the screen's and the report's words
(`ArchitectMatchOut`), and names the view a recorded result was compared with.

Nothing is decided here. The match that counts is #1166's effective record
(`effective_architect_matches`) and the picks waiting for a run are #1167's
(`architect_matches_waiting_for_run`), both read once per revision by the countertop results. A
view is described exactly as the reviewer picker describes it (`app/api/architect_matches.py`).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Package
from app.models.evidence import ArchitectViewMatchRecord
from app.review.architect_views_out import view_out, views_by_id
from app.review.row_location import RowLocation
from app.schemas.architect_matches import ArchitectViewRefOut
from app.schemas.visual_ui import ArchitectAiPickOut, ArchitectMatchOut
from workflow.architect_match_contract import MATCHED_STATUSES, EffectiveMatch
from workflow.architect_match_contract import compared_with_text as contract_compared_with_text

__all__ = ["SeparateFileMatches", "compared_view", "match_out", "separate_file_views"]

_ANSWERS: Final = frozenset({"view", "none", "unsure", "no_answer"})

#: Whose judgments a match rests on, in the words the screen and the report use.
_JUDGMENTS: Final[dict[str, str]] = {
    "automatic": "code and both AIs",
    "reviewer": "reviewer",
    "carried": "carried over from the earlier revision",
}


@dataclass(frozen=True, slots=True)
class SeparateFileMatches:
    """Every row's match on a revision whose architect file was indexed, read once."""

    matches: Mapping[UUID, EffectiveMatch | None]
    waiting: frozenset[UUID]
    """Match records recorded after the live check run (#1167): their rows wait for the next."""
    records: Mapping[UUID, ArchitectViewMatchRecord]
    """The effective records themselves, by record id (code's verdict, the AIs' picks, the time)."""
    views: Mapping[UUID, ArchitectViewRefOut]
    """Every view a record names (matched, code's pick), by view id."""
    own_view_pages: frozenset[UUID] = frozenset()
    """Vendor pages holding their own architect drawing, asked for the rows with no match."""


def separate_file_views(
    session: Session,
    package_id: UUID,
    matches: Mapping[UUID, EffectiveMatch | None],
    waiting: frozenset[UUID],
    *,
    own_view_pages: frozenset[UUID] = frozenset(),
) -> SeparateFileMatches:
    """The effective records and the views they name, in at most three statements whatever the
    number of rows: the records, the views (`views_by_id`), and the package's project (for the
    picture links), the last two only when a view is named."""
    record_ids = {match.record_id for match in matches.values() if match is not None}
    records = (
        {
            record.id: record
            for record in session.scalars(
                select(ArchitectViewMatchRecord).where(
                    ArchitectViewMatchRecord.id.in_(tuple(record_ids))
                )
            )
        }
        if record_ids
        else {}
    )
    view_ids = {match.matched.view_id for match in matches.values() if match and match.matched}
    view_ids |= {r.code_pick_view_id for r in records.values() if r.code_pick_view_id is not None}
    found = views_by_id(session, view_ids)
    views: dict[UUID, ArchitectViewRefOut] = {}
    if found:
        project_id = session.scalar(select(Package.project_id).where(Package.id == package_id))
        assert project_id is not None
        views = {
            view_id: view_out(
                entry, page_number, document_id, project_id=project_id, package_id=package_id
            )
            for view_id, (entry, page_number, document_id) in found.items()
        }
    return SeparateFileMatches(
        matches=matches,
        waiting=waiting,
        records=records,
        views=views,
        own_view_pages=own_view_pages,
    )


def _ai_picks(record: ArchitectViewMatchRecord | None) -> tuple[ArchitectAiPickOut, ...]:
    picks: list[ArchitectAiPickOut] = []
    for raw in () if record is None else record.ai_picks or ():
        if not isinstance(raw, dict):
            continue
        answer = raw.get("answer")
        view_id: UUID | None
        try:
            view_id = None if raw.get("view_id") is None else UUID(str(raw.get("view_id")))
        except ValueError:
            view_id = None
        picks.append(
            ArchitectAiPickOut(
                model_label=str(raw.get("model_id") or "an AI"),
                answer=cast(
                    Literal["view", "none", "unsure", "no_answer"],
                    answer if answer in _ANSWERS else "no_answer",
                ),
                view_id=view_id,
                why=str(raw.get("why") or ""),
            )
        )
    return tuple(picks)


def match_out(found: SeparateFileMatches, row_anchor_id: UUID) -> ArchitectMatchOut:
    """The row's match in the screen's words; `not_matched_yet` when it has no record."""
    match = found.matches.get(row_anchor_id)
    if match is None:
        return ArchitectMatchOut(
            record_id=None,
            status="not_matched_yet",
            source=None,
            judgments=None,
            code_verdict=None,
            code_pick_view_id=None,
            matched_view=None,
            needs_decision=False,
            reason=None,
        )
    record = found.records.get(match.record_id)
    waiting = match.record_id in found.waiting
    named = match.status in MATCHED_STATUSES or match.status in ("not_separated", "none_matches")
    return ArchitectMatchOut(
        record_id=match.record_id,
        status=match.status,
        source=match.source,
        judgments=_JUDGMENTS.get(match.source) if named else None,
        code_verdict=None if record is None else record.code_verdict,
        code_pick_view_id=None if record is None else record.code_pick_view_id,
        ai_picks=_ai_picks(record),
        matched_view=(None if match.matched is None else found.views.get(match.matched.view_id)),
        needs_decision=match.status == "needs_reviewer" and not waiting,
        reason="; ".join(reason for reason in match.reasons if reason.strip()) or None,
        waits_for_run=waiting,
    )


def compared_view(
    found: SeparateFileMatches,
    row_anchor_id: UUID,
    *,
    locations: Collection[RowLocation | None],
    finding_created_at: datetime,
) -> tuple[ArchitectViewRefOut | None, str | None]:
    """The view this row's own recorded result was compared with, and `compared with …`.

    Only for a result that compared something (`locations` holds one entry per compared pair),
    against a view the row is matched with, and only when the row's own result shows it used that
    view: a compared architect dimension stored on the view's page of the view's file; or, where no
    compared dimension has a stored position, a match recorded before the row's own result. A pick
    waiting for the next run is never named.
    """
    match = found.matches.get(row_anchor_id)
    if (
        not locations
        or match is None
        or match.matched is None
        or match.status not in MATCHED_STATUSES
        or match.record_id in found.waiting
    ):
        return None, None
    view = match.matched
    placed = [location for location in locations if location is not None]
    if placed:
        used = any(
            location.document_version_id == view.document_version_id
            and location.page_number == view.page_number
            for location in placed
        )
    else:
        record = found.records.get(match.record_id)
        used = record is not None and record.created_at <= finding_created_at
    if not used:
        return None, None
    return found.views.get(view.view_id), contract_compared_with_text(view)
