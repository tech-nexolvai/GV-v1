"""What the countertop results say about each row's architect view (#1168), read-only.

When the architect's drawings are their own file and that file was indexed this revision (#1166),
every vendor countertop row is matched with one view of it, automatically or by the reviewer. This
module turns the stored match records into the screen's and the report's words:

* `ArchitectMatchOut`: the row's match state, whose judgments it rests on, what each AI answered,
  the matched view, and whether a reviewer's pick waits for the checks to run again;
* `ArchitectViewRefOut`: one view, with its page, sheet, title, frame and picture link.

Nothing is decided here: the match that counts is #1166's effective record
(`workflow.architect_match_records.effective_architect_matches`), read once per revision. A
combined sheet (the architect's drawing on the vendor's sheet) never reaches this module's queries:
`separate_file_matches` answers `None` after the one question the countertop results already ask.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DocumentVersion, Package, Page
from app.models.evidence import ArchitectViewIndexEntry, ArchitectViewMatchRecord
from app.review.row_location import RowLocation
from app.schemas.visual_ui import ArchitectAiPickOut, ArchitectMatchOut, ArchitectViewRefOut
from workflow.architect_match_contract import MATCHED_STATUSES, EffectiveMatch, MatchedView
from workflow.architect_match_contract import compared_with_text as contract_compared_with_text
from workflow.architect_pairing_records import MatchesLookup
from workflow.architect_row_plan import architect_file_indexed

__all__ = [
    "PICTURE_PATH",
    "SeparateFileMatches",
    "compared_view",
    "match_out",
    "separate_file_matches",
    "stored_matches",
]

#: Where a view's stored picture is served (#1166's reviewer API), under the API prefix.
PICTURE_PATH: Final = (
    "/api/v1/projects/{project_id}/packages/{package_id}/architect-views/{view_id}/picture"
)

_ANSWERS: Final = frozenset({"view", "none", "unsure", "no_answer"})

#: Whose judgments a match rests on, in the words the screen and the report use.
_JUDGMENTS: Final[dict[str, str]] = {
    "automatic": "code and both AIs",
    "reviewer": "reviewer",
    "carried": "carried over from the earlier revision",
}


def stored_matches(
    session: Session, row_anchor_ids: Collection[UUID]
) -> Mapping[UUID, EffectiveMatch | None]:
    """#1166's effective match per row (`effective_architect_matches`), read in one batch."""
    from workflow.architect_match_records import effective_architect_matches

    return effective_architect_matches(session, row_anchor_ids)


@dataclass(frozen=True, slots=True)
class SeparateFileMatches:
    """Every row's match on a revision whose architect file was indexed, read once."""

    matches: Mapping[UUID, EffectiveMatch | None]
    records: Mapping[UUID, ArchitectViewMatchRecord]
    """The effective records themselves, by record id (code's verdict, the AIs' picks, the time)."""
    views: Mapping[UUID, ArchitectViewRefOut]
    """Every view a record names (matched, code's pick, an AI's pick), by view id."""


def separate_file_matches(
    session: Session,
    revision_id: UUID,
    row_anchor_ids: Collection[UUID],
    *,
    separate_file: bool,
    package_id: UUID,
    lookup: MatchesLookup | None = None,
) -> SeparateFileMatches | None:
    """The rows' matches, or `None` when there is nothing to match against: a combined sheet
    (`separate_file` false: no statement at all), or a separate file not indexed this revision (one
    statement). Otherwise a fixed number of statements, whatever the number of rows: the index
    check, the lookup's own (at most two), the records, and the views."""
    if not separate_file or not row_anchor_ids:
        return None
    if not architect_file_indexed(session, revision_id):
        return None
    matches = (lookup or stored_matches)(session, row_anchor_ids)
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
    view_ids: set[UUID] = set()
    for match in matches.values():
        if match is not None and match.matched is not None:
            view_ids.add(match.matched.view_id)
    for record in records.values():
        if record.code_pick_view_id is not None:
            view_ids.add(record.code_pick_view_id)
    file_names = {
        match.matched.view_id: match.matched.file_name
        for match in matches.values()
        if match is not None and match.matched is not None
    }
    return SeparateFileMatches(
        matches=matches,
        records=records,
        views=_views(session, view_ids, file_names=file_names, package_id=package_id),
    )


def _views(
    session: Session,
    view_ids: Collection[UUID],
    *,
    file_names: Mapping[UUID, str],
    package_id: UUID,
) -> dict[UUID, ArchitectViewRefOut]:
    """The named views with their page, document and the package's project (for the picture
    link), in one statement."""
    if not view_ids:
        return {}
    found: dict[UUID, ArchitectViewRefOut] = {}
    for entry, page_index, document_id, project_id in session.execute(
        select(ArchitectViewIndexEntry, Page.index, DocumentVersion.document_id, Package.project_id)
        .join(Page, Page.id == ArchitectViewIndexEntry.page_id)
        .join(DocumentVersion, DocumentVersion.id == ArchitectViewIndexEntry.document_version_id)
        .join(Package, Package.id == package_id)
        .where(ArchitectViewIndexEntry.id.in_(tuple(view_ids)))
    ):
        found[entry.id] = view_ref(
            entry,
            page_index + 1,
            document_id,
            file_name=file_names.get(entry.id, _FALLBACK_FILE_NAME),
            project_id=project_id,
            package_id=package_id,
        )
    return found


#: How a view the effective match does not name (code's or an AI's pick) names its file: as #1166
#: names it (the upload keeps no file name, only its kind).
_FALLBACK_FILE_NAME: Final = "the architect's drawings"


def view_ref(
    entry: ArchitectViewIndexEntry,
    page_number: int,
    document_id: UUID,
    *,
    file_name: str,
    project_id: UUID,
    package_id: UUID,
) -> ArchitectViewRefOut:
    """One indexed view as the screen names it; its frame only from its stored points."""
    label = f"Page {page_number}, view {entry.view_number}"
    if entry.title:
        label += f": {entry.title}"
    if entry.sheet_number:
        label += f" (sheet {entry.sheet_number})"
    return ArchitectViewRefOut(
        view_id=entry.id,
        document_id=document_id,
        document_version_id=entry.document_version_id,
        file_name=file_name,
        page_number=page_number,
        sheet_number=entry.sheet_number,
        bubble=entry.bubble,
        title=entry.title,
        scale_note=entry.scale_note,
        label=label,
        region=_region(entry, page_number),
        picture_url=(
            None
            if entry.picture_storage_key is None
            else PICTURE_PATH.format(project_id=project_id, package_id=package_id, view_id=entry.id)
        ),
        separated=entry.separated,
    )


def _region(entry: ArchitectViewIndexEntry, page_number: int) -> RowLocation | None:
    """The view's stored frame, or `None` when its stored points are missing or malformed."""
    points = entry.extent.get("stored_points") if isinstance(entry.extent, dict) else None
    if not isinstance(points, list) or len(points) < 3:
        return None
    try:
        polygon = [[str(point[0]), str(point[1])] for point in points]
    except (IndexError, TypeError):
        return None
    return RowLocation(
        page_id=entry.page_id,
        document_version_id=entry.document_version_id,
        page_number=page_number,
        polygon=polygon,
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


def waits_for_run(record: ArchitectViewMatchRecord | None, checked_at: datetime | None) -> bool:
    """A reviewer's pick recorded after the live check run (or with no run yet): only a new run
    uses it. The same rule sign-off applies to every reviewer input (`app/review/approval.py`)."""
    if record is None or record.source != "reviewer":
        return False
    return checked_at is None or record.created_at > checked_at


def match_out(
    found: SeparateFileMatches, row_anchor_id: UUID, *, checked_at: datetime | None
) -> ArchitectMatchOut:
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
    waiting = waits_for_run(record, checked_at)
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
    compared: bool,
    checked_at: datetime | None,
) -> tuple[ArchitectViewRefOut | None, str | None]:
    """The view this row's recorded result was compared with, and `compared with …`: only for a
    result that compared something, against a matched view the result was made with (a reviewer's
    later pick is not it)."""
    match = found.matches.get(row_anchor_id)
    if (
        not compared
        or match is None
        or match.matched is None
        or match.status not in MATCHED_STATUSES
        or waits_for_run(found.records.get(match.record_id), checked_at)
    ):
        return None, None
    view: MatchedView = match.matched
    return found.views.get(view.view_id), contract_compared_with_text(view)
