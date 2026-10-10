"""The countertop results name each row's architect view match (#1168).

When the architect's drawings are their own file and it was indexed (#1166), each countertop's
architect block carries `match` (its state, whose judgments, the AIs' answers, the matched view) and,
when a recorded result compared something against the matched view, `compared_with` and the words
`compared with <file>, page N, view X …`. A reviewer's pick after the live run says it waits for the
next run. A combined sheet (no separate file) keeps every new field null and its statement count.

#1166's read path (`workflow/architect_match_records.py`) is built in parallel, so these tests pass
its effective-match lookup in, restated from its contract (the newest record of the row's chain), as
#1167's tests do. Synthetic values only.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.visual_countertops import _countertop_results_for_revision
from app.db.session import session_factory
from app.models import (
    Document,
    DocumentVersion,
    ObservationCandidate,
    PackageRevision,
    PackageRevisionDocument,
    SourceArtifact,
)
from app.models.document import Page
from app.models.evidence import ArchitectViewIndexEntry, ArchitectViewMatchRecord
from app.review.architect_view_match import (
    PICTURE_PATH,
    SeparateFileMatches,
    compared_view,
    match_out,
    waits_for_run,
)
from app.schemas.visual_ui import ArchitectViewRefOut, CountertopResultsOut
from reports.spreadsheet import architect_line
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_architect_row_evidence import _sealed_rows
from workflow.architect_match_contract import EffectiveMatch, MatchedView
from workflow.architect_row_plan import CHOOSE_ARCHITECT_VIEW, NOT_MATCHED_YET_ROW
from workflow.slot_row_scope import slot_rows
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

FILE_NAME = "the architect's drawings"


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


# --- #1166's records, written as its stage and API write them ------------------------------------


def _revision(session: Session, package_id: UUID) -> PackageRevision:
    return session.query(PackageRevision).filter_by(package_id=package_id).one()


def _architect_file(session: Session, package_id: UUID, views: int = 2) -> list[UUID]:
    """An architect file of its own (different bytes), one page, indexed with `views` views."""
    revision = _revision(session, package_id)
    document = Document(package_id=package_id, kind="architectural")
    artifact = SourceArtifact(storage_key=f"synthetic/{uuid4()}", sha256="e" * 64, size=1)
    session.add_all((document, artifact))
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256="e" * 64, page_count=2
    )
    session.add(version)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision.id,
            package_id=package_id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    page = Page(
        document_version_id=version.id,
        index=1,
        content_hash="f" * 64,
        width_pt=1224,
        height_pt=792,
        rotation=0,
        has_vector_text=True,
    )
    session.add(page)
    session.flush()
    run_id = session.scalars(select(ObservationCandidate.extraction_run_id)).first()
    assert run_id is not None
    ids: list[UUID] = []
    for number in range(1, views + 1):
        entry = ArchitectViewIndexEntry(
            extraction_run_id=run_id,
            document_version_id=version.id,
            page_id=page.id,
            view_number=number,
            view_tag=f"view-{number}",
            drawing_view_id=None,
            sheet_number="Z-9",
            bubble=str(number),
            title=f"SAMPLE ELEVATION {number}",
            scale_note='1/2" = 1\'-0"',
            points_per_inch="36",
            extent={
                "x0": "10",
                "top": "10",
                "x1": "200",
                "bottom": "120",
                "stored_points": [["0.1", "0.2"], ["0.4", "0.2"], ["0.4", "0.5"], ["0.1", "0.5"]],
            },
            separated=True,
            role_confirmed=True,
            row_count=1,
            reason="synthetic index",
            picture_sha256="a" * 64 if number == 1 else None,
            picture_storage_key=(
                f"architect-views/{version.id}/view-{number}.png" if number == 1 else None
            ),
        )
        session.add(entry)
        session.flush()
        ids.append(entry.id)
    return ids


def _record(
    session: Session,
    anchor: UUID,
    status: str,
    *,
    view: UUID | None = None,
    source: str = "automatic",
    supersedes: ArchitectViewMatchRecord | None = None,
    reasons: tuple[str, ...] = (),
    when: datetime | None = None,
) -> ArchitectViewMatchRecord:
    candidate = session.get_one(ObservationCandidate, anchor)
    record = ArchitectViewMatchRecord(
        package_revision_id=(
            supersedes.package_revision_id
            if supersedes is not None
            else session.scalars(select(PackageRevision.id)).one()
        ),
        vendor_page_id=candidate.page_id,
        row_anchor_candidate_id=anchor,
        extraction_run_id=None if source == "reviewer" else candidate.extraction_run_id,
        source=source,
        status=status,
        matched_view_id=view,
        code_verdict="geometry_tie" if status == "needs_reviewer" else None,
        ai_picks=[
            {"model_id": "reader-a", "answer": "view", "view_id": None, "why": "Synthetic: bays."},
            {"model_id": "reader-b", "answer": "unsure", "view_id": None, "why": "Synthetic."},
        ],
        candidates=[],
        vendor_references=[],
        reasons=list(reasons),
        details={},
        supersedes_id=None if supersedes is None else supersedes.id,
        decided_by="reviewer@example.com" if source == "reviewer" else None,
        **({} if when is None else {"created_at": when}),
    )
    session.add(record)
    session.flush()
    return record


def _views(session: Session, ids: Collection[UUID]) -> dict[UUID, MatchedView]:
    found: dict[UUID, MatchedView] = {}
    for entry, index in session.execute(
        select(ArchitectViewIndexEntry, Page.index)
        .join(Page, Page.id == ArchitectViewIndexEntry.page_id)
        .where(ArchitectViewIndexEntry.id.in_(tuple(ids)))
    ):
        found[entry.id] = MatchedView(
            view_id=entry.id,
            document_version_id=entry.document_version_id,
            page_id=entry.page_id,
            page_number=index + 1,
            view_number=entry.view_number,
            view_tag=entry.view_tag,
            title=entry.title,
            bubble=entry.bubble,
            sheet_number=entry.sheet_number,
            scale_note=entry.scale_note,
            file_name=FILE_NAME,
            separated=entry.separated,
        )
    return found


def stored_matches(
    session: Session, anchors: Collection[UUID]
) -> dict[UUID, EffectiveMatch | None]:
    """#1166's `effective_architect_matches`, restated: the newest record per row, two statements."""
    newest: dict[UUID, ArchitectViewMatchRecord] = {}
    for record in session.scalars(
        select(ArchitectViewMatchRecord)
        .where(ArchitectViewMatchRecord.row_anchor_candidate_id.in_(tuple(anchors)))
        .order_by(ArchitectViewMatchRecord.created_at.desc(), ArchitectViewMatchRecord.id.desc())
    ):
        newest.setdefault(record.row_anchor_candidate_id, record)
    views = _views(session, {r.matched_view_id for r in newest.values() if r.matched_view_id})
    answers: dict[UUID, EffectiveMatch | None] = dict.fromkeys(anchors)
    for anchor, record in newest.items():
        answers[anchor] = EffectiveMatch(
            record_id=record.id,
            status=record.status,  # type: ignore[arg-type]
            source=record.source,  # type: ignore[arg-type]
            matched=None if record.matched_view_id is None else views[record.matched_view_id],
            needs_reviewer=record.status == "needs_reviewer",
            reasons=tuple(record.reasons),
            decided_by=record.decided_by,
        )
    return answers


def _run(session: Session, package_id: UUID, tmp_path: Path) -> None:
    DatabaseStages(
        store=LocalStore(root=tmp_path / "store", ticket_secret=b"synthetic-test"),
        architect_pairing=lambda _session, _anchor: None,
        architect_match=lambda s, anchor: stored_matches(s, (anchor,))[anchor],
    ).run_checks(session, _revision(session, package_id).id)
    session.flush()


def _results(session: Session, package_id: UUID) -> CountertopResultsOut:
    return _countertop_results_for_revision(
        session, package_id, _revision(session, package_id), matches_lookup=stored_matches
    )


def _blocks(results: CountertopResultsOut) -> dict[UUID, Any]:
    return {item.row_id: item.architect for item in results.items}


# --- the countertop results ------------------------------------------------------------------------


def test_a_row_waiting_for_the_reviewer_and_a_matched_row(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    first, second = _architect_file(session, package_id)
    _record(session, anchors[0], "needs_reviewer")
    _record(session, anchors[1], "auto_matched", view=first, reasons=("Synthetic: reference.",))
    _run(session, package_id, tmp_path)

    blocks = _blocks(_results(session, package_id))

    waiting = blocks[anchors[0]]
    assert waiting.outcome is not None and waiting.outcome.value == "REVIEW_REQUIRED"
    assert waiting.reason == CHOOSE_ARCHITECT_VIEW
    assert waiting.needs_decision is True
    assert waiting.match is not None
    assert waiting.match.status == "needs_reviewer"
    assert waiting.match.needs_decision is True
    assert waiting.match.waits_for_run is False
    assert waiting.match.judgments is None
    assert waiting.match.code_verdict == "geometry_tie"
    assert [(p.model_label, p.answer) for p in waiting.match.ai_picks] == [
        ("reader-a", "view"),
        ("reader-b", "unsure"),
    ]
    assert waiting.compared_with is None and waiting.compared_with_text is None

    matched = blocks[anchors[1]]
    assert matched.match is not None
    assert matched.match.status == "auto_matched"
    assert matched.match.judgments == "code and both AIs"
    assert matched.match.reason == "Synthetic: reference."
    view = matched.match.matched_view
    assert isinstance(view, ArchitectViewRefOut)
    assert view.view_id == first
    assert view.page_number == 2
    assert view.file_name == FILE_NAME
    assert view.label == "Page 2, view 1: SAMPLE ELEVATION 1 (sheet Z-9)"
    assert view.region is not None and view.region.polygon[0] == ["0.1", "0.2"]
    assert view.picture_url is not None and view.picture_url.endswith(
        f"/architect-views/{first}/picture"
    )
    # Matched, but nothing paired in the view yet (#1167 asks for a run): nothing was compared,
    # so no "compared with"; the reason names the matched view.
    assert matched.reason is not None
    assert matched.reason.startswith(f"Matched with {FILE_NAME}, page 2, view 1")
    assert matched.compared == ()
    assert matched.compared_with is None and matched.compared_with_text is None
    assert second not in {view.view_id}
    # The report says the same as the screen.
    assert (
        architect_line(
            next(i for i in _results(session, package_id).items if i.row_id == anchors[0])
        )
        == f"Matches the architect: REVIEW_REQUIRED: {CHOOSE_ARCHITECT_VIEW}"
    )


def test_a_pick_after_the_run_waits_for_the_next_run(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    first, _second = _architect_file(session, package_id)
    asked = _record(session, anchors[0], "needs_reviewer")
    _run(session, package_id, tmp_path)
    session.commit()
    _record(
        session,
        anchors[0],
        "reviewer_confirmed",
        view=first,
        source="reviewer",
        supersedes=asked,
        when=datetime.now(UTC) + timedelta(minutes=5),
    )

    block = _blocks(_results(session, package_id))[anchors[0]]

    assert block.match is not None
    assert block.match.status == "reviewer_confirmed"
    assert block.match.judgments == "reviewer"
    assert block.match.waits_for_run is True
    assert block.match.needs_decision is False
    # The result on screen is still the run's ("choose"): no view was compared yet.
    assert block.reason == CHOOSE_ARCHITECT_VIEW
    assert block.compared_with is None


def test_an_indexed_file_with_no_record_is_not_matched_yet(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    _architect_file(session, package_id)
    _run(session, package_id, tmp_path)

    blocks = _blocks(_results(session, package_id))

    for anchor in anchors.values():
        block = blocks[anchor]
        assert block.match is not None
        assert block.match.status == "not_matched_yet"
        assert block.match.record_id is None
        assert block.not_compared_reason == NOT_MATCHED_YET_ROW
        assert block.compared_with is None


def test_a_combined_sheet_keeps_every_new_field_null(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    _run(session, package_id, tmp_path)

    blocks = _blocks(
        _countertop_results_for_revision(session, package_id, _revision(session, package_id))
    )

    for anchor in anchors.values():
        assert blocks[anchor].match is None
        assert blocks[anchor].compared_with is None
        assert blocks[anchor].compared_with_text is None


def test_the_statement_count_does_not_grow_with_the_rows(session: Session, tmp_path: Path) -> None:
    from tests.api.test_slot_rows import _package_rows

    def count(package_id: UUID) -> int:
        statements = 0

        def counted(*_args: object) -> None:
            nonlocal statements
            statements += 1

        revision = _revision(session, package_id)
        event.listen(session.bind, "before_cursor_execute", counted)
        try:
            _countertop_results_for_revision(
                session, package_id, revision, matches_lookup=stored_matches
            )
        finally:
            event.remove(session.bind, "before_cursor_execute", counted)
        return statements

    _project, package_id, _anchors = _package_rows(session, rows_per_page=10)
    combined = count(package_id)
    views = _architect_file(session, package_id)
    anchors = [row.anchor.id for row in slot_rows(session, _revision(session, package_id).id)]
    assert len(anchors) == 20
    for index, anchor in enumerate(anchors):
        _record(session, anchor, "auto_matched", view=views[index % 2])
    session.flush()
    separate = count(package_id)
    # The combined sheet's own bound (`tests/api/test_visual_ui.py`, 12), then the separate file's
    # fixed reads: the index check, the lookup's two, the records and the views.
    assert combined <= 12
    assert separate - combined <= 5


# --- the words, without a database ---------------------------------------------------------------


_VIEW = MatchedView(
    view_id=UUID(int=1),
    document_version_id=UUID(int=2),
    page_id=UUID(int=3),
    page_number=2,
    view_number=3,
    view_tag="view-3",
    title="SAMPLE ELEVATION",
    bubble="3",
    sheet_number="Z-9",
    scale_note=None,
    file_name=FILE_NAME,
    separated=True,
)
_REF = ArchitectViewRefOut(
    view_id=UUID(int=1),
    document_id=UUID(int=4),
    document_version_id=UUID(int=2),
    file_name=FILE_NAME,
    page_number=2,
    sheet_number="Z-9",
    bubble="3",
    title="SAMPLE ELEVATION",
    scale_note=None,
    label="Page 2, view 3: SAMPLE ELEVATION (sheet Z-9)",
    region=None,
    picture_url=None,
    separated=True,
)


class _Record:
    def __init__(self, source: str, created_at: datetime) -> None:
        self.source = source
        self.created_at = created_at
        self.code_verdict = None
        self.code_pick_view_id = None
        self.ai_picks: list[object] = [{"model_id": "x", "answer": "maybe", "why": 3}, "junk"]


def _found(source: str, status: str, created_at: datetime) -> SeparateFileMatches:
    match = EffectiveMatch(
        record_id=UUID(int=9),
        status=status,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        matched=_VIEW,
        needs_reviewer=False,
        reasons=(),
        decided_by=None,
    )
    return SeparateFileMatches(
        matches={UUID(int=7): match},
        records={UUID(int=9): _Record(source, created_at)},  # type: ignore[dict-item]
        views={_VIEW.view_id: _REF},
    )


def test_compared_with_names_the_view_only_for_a_compared_result() -> None:
    run = datetime(2026, 10, 10, 12, tzinfo=UTC)
    found = _found("automatic", "auto_matched", run - timedelta(minutes=1))
    assert compared_view(found, UUID(int=7), compared=True, checked_at=run) == (
        _REF,
        "compared with the architect's drawings, page 2, view 3 SAMPLE ELEVATION (sheet Z-9)",
    )
    assert compared_view(found, UUID(int=7), compared=False, checked_at=run) == (None, None)
    assert compared_view(found, UUID(int=8), compared=True, checked_at=run) == (None, None)
    later = _found("reviewer", "reviewer_confirmed", run + timedelta(minutes=1))
    assert compared_view(later, UUID(int=7), compared=True, checked_at=run) == (None, None)
    not_read = _found("automatic", "not_separated", run)
    assert compared_view(not_read, UUID(int=7), compared=True, checked_at=run) == (None, None)


def test_waits_for_run_only_for_a_reviewer_pick_after_the_run() -> None:
    run = datetime(2026, 10, 10, 12, tzinfo=UTC)
    after = _Record("reviewer", run + timedelta(seconds=1))
    before = _Record("reviewer", run - timedelta(seconds=1))
    automatic = _Record("automatic", run + timedelta(seconds=1))
    assert waits_for_run(after, run) is True  # type: ignore[arg-type]
    assert waits_for_run(before, run) is False  # type: ignore[arg-type]
    assert waits_for_run(automatic, run) is False  # type: ignore[arg-type]
    assert waits_for_run(after, None) is True  # type: ignore[arg-type]


def test_odd_stored_ai_answers_are_read_safely() -> None:
    found = _found("automatic", "auto_matched", datetime(2026, 10, 10, tzinfo=UTC))
    out = match_out(found, UUID(int=7), checked_at=None)
    assert [(p.model_label, p.answer, p.why) for p in out.ai_picks] == [("x", "no_answer", "3")]
    assert out.matched_view == _REF
    assert PICTURE_PATH.startswith("/api/v1/projects/")
