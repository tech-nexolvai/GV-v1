"""The countertop results name each row's architect view match (#1168).

When the architect's drawings are their own file and it was indexed (#1166), each countertop's
architect block carries `match` (its state, whose judgments, the AIs' answers, the matched view) and,
when a recorded result compared something against the matched view, `compared_with` and the words
`compared with <file>, page N, view X …`. A reviewer's pick after the live run says it waits for the
next run. A combined sheet (no separate file) keeps every new field null and its statement count.

The match records are written as #1166's stage and API write them; everything else is the real read
path (#1166's `effective_architect_matches`, #1167's planning and waiting rule). Synthetic values only.
"""

from __future__ import annotations

from collections.abc import Iterator
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
from app.review.architect_view_match import SeparateFileMatches, compared_view, match_out
from app.review.row_location import RowLocation
from app.schemas.architect_matches import ArchitectViewRefOut
from app.schemas.visual_ui import CountertopResultsOut
from reports.spreadsheet import architect_line
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_architect_row_evidence import _sealed_rows
from workflow.architect_match_contract import EffectiveMatch, MatchedView
from workflow.architect_match_records import ARCHITECT_FILE_NAME
from workflow.architect_pairing_contract import EffectivePairing
from workflow.architect_row_plan import (
    CHOOSE_ARCHITECT_VIEW,
    NOT_MATCHED_YET_ROW,
    PAIR_BY_REVIEWER,
    no_dimensions_line_up_reason,
)
from workflow.slot_row_scope import slot_rows
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)


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
            else session.scalars(
                select(PackageRevisionDocument.package_revision_id).where(
                    PackageRevisionDocument.document_version_id == candidate.document_version_id
                )
            ).one()
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


def _run(
    session: Session,
    package_id: UUID,
    tmp_path: Path,
    pairings: dict[UUID, EffectivePairing] | None = None,
) -> None:
    found = pairings or {}
    DatabaseStages(
        store=LocalStore(root=tmp_path / "store", ticket_secret=b"synthetic-test"),
        architect_pairing=lambda _session, anchor: found.get(anchor),
    ).run_checks(session, _revision(session, package_id).id)
    session.flush()


def _results(session: Session, package_id: UUID) -> CountertopResultsOut:
    return _countertop_results_for_revision(session, package_id, _revision(session, package_id))


def _blocks(results: CountertopResultsOut) -> dict[UUID, Any]:
    return {item.row_id: item.architect for item in results.items}


# --- the countertop results ------------------------------------------------------------------------


def test_a_row_waiting_for_the_reviewer_and_a_matched_row(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    first, second = _architect_file(session, package_id)
    _record(session, anchors[0], "needs_reviewer")
    _record(session, anchors[1], "auto_matched", view=first, reasons=("Synthetic: reference.",))
    _run(session, package_id, tmp_path)

    results = _results(session, package_id)
    blocks = _blocks(results)

    waiting = blocks[anchors[0]]
    assert waiting.outcome is not None and waiting.outcome.value == "REVIEW_REQUIRED"
    assert waiting.reason == CHOOSE_ARCHITECT_VIEW
    assert waiting.needs_decision is True
    assert waiting.can_pair is False
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
    assert view.view_id == first and view.view_id != second
    assert view.page_number == 2
    assert view.file_name == ARCHITECT_FILE_NAME
    assert view.label == "Page 2, view 1: SAMPLE ELEVATION 1 (sheet Z-9)"
    assert view.region is not None and view.region.polygon[0] == ["0.1", "0.2"]
    assert view.picture_url is not None and view.picture_url.endswith(
        f"/architect-views/{first}/picture"
    )
    # Matched, but nothing in the view lines up with the row: compared by hand (no pairing to
    # offer), and nothing compared, so no "compared with".
    assert matched.reason == no_dimensions_line_up_reason(_matched_view(session, first))
    assert matched.can_pair is False
    assert matched.compared == ()
    assert matched.compared_with is None and matched.compared_with_text is None
    # The report says the same as the screen.
    item = next(i for i in results.items if i.row_id == anchors[0])
    assert (
        architect_line(item) == f"Matches the architect: REVIEW_REQUIRED: {CHOOSE_ARCHITECT_VIEW}"
    )


def test_a_pairing_is_offered_only_where_the_check_asks_for_one(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    first, _second = _architect_file(session, package_id)
    _record(session, anchors[0], "auto_matched", view=first)
    unsettled = EffectivePairing(
        record_id=uuid4(), source="none", status="ais-disagree", pairs=(), reasons=("Synthetic.",)
    )
    _run(session, package_id, tmp_path, {anchors[0]: unsettled})

    block = _blocks(_results(session, package_id))[anchors[0]]

    assert block.reason is not None and PAIR_BY_REVIEWER in block.reason
    assert block.can_pair is True


def test_no_pairing_is_offered_while_a_pick_waits_for_a_run(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    first, second = _architect_file(session, package_id)
    automatic = _record(session, anchors[0], "auto_matched", view=first)
    unsettled = EffectivePairing(
        record_id=uuid4(), source="none", status="ais-disagree", pairs=(), reasons=("Synthetic.",)
    )
    _run(session, package_id, tmp_path, {anchors[0]: unsettled})
    session.commit()
    _record(
        session,
        anchors[0],
        "reviewer_confirmed",
        view=second,
        source="reviewer",
        supersedes=automatic,
        when=datetime.now(UTC) + timedelta(minutes=5),
    )

    block = _blocks(_results(session, package_id))[anchors[0]]

    assert block.reason is not None and PAIR_BY_REVIEWER in block.reason
    assert block.match is not None and block.match.waits_for_run is True
    assert block.can_pair is False


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

    item = next(i for i in _results(session, package_id).items if i.row_id == anchors[0])
    block = item.architect

    assert block.match is not None
    assert block.match.status == "reviewer_confirmed"
    assert block.match.judgments == "reviewer"
    assert block.match.waits_for_run is True
    assert block.match.needs_decision is False
    # The result on screen is still the run's ("choose"): no view was compared yet.
    assert block.reason == CHOOSE_ARCHITECT_VIEW
    assert block.can_pair is False
    assert block.compared_with is None
    assert architect_line(item).endswith(" (waits for the next check run)")


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
        assert block.reason == NOT_MATCHED_YET_ROW
        assert block.can_pair is False
        assert block.compared_with is None


def test_a_combined_sheet_keeps_every_new_field_null(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    _run(session, package_id, tmp_path)

    blocks = _blocks(_results(session, package_id))

    for anchor in anchors.values():
        assert blocks[anchor].match is None
        assert blocks[anchor].can_pair is None
        assert blocks[anchor].compared_with is None
        assert blocks[anchor].compared_with_text is None


def _count(session: Session, package_id: UUID) -> int:
    statements = 0

    def counted(*_args: object) -> None:
        nonlocal statements
        statements += 1

    revision = _revision(session, package_id)
    event.listen(session.bind, "before_cursor_execute", counted)
    try:
        _countertop_results_for_revision(session, package_id, revision)
    finally:
        event.remove(session.bind, "before_cursor_execute", counted)
    return statements


def _separate_package(session: Session, rows_per_page: int, matched: str) -> tuple[UUID, int]:
    """A package with an indexed architect file whose rows are all matched, none, or every other."""
    from tests.api.test_slot_rows import _package_rows

    _project, package_id, _anchors = _package_rows(session, rows_per_page=rows_per_page)
    combined = _count(session, package_id)
    views = _architect_file(session, package_id)
    anchors = [row.anchor.id for row in slot_rows(session, _revision(session, package_id).id)]
    assert len(anchors) == 2 * rows_per_page
    for index, anchor in enumerate(anchors):
        if matched == "all" or (matched == "half" and index % 2 == 0):
            _record(session, anchor, "auto_matched", view=views[index % 2])
    session.flush()
    return package_id, combined


@pytest.mark.parametrize("matched", ["all", "none", "half"])
def test_the_statement_count_does_not_grow_with_the_rows(
    session: Session, matched: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tests.api.test_slot_rows as slot_rows_tests

    small, small_combined = _separate_package(session, 5, matched)
    # The second package shares the rulebook the first one published.
    monkeypatch.setattr(slot_rows_tests, "_publish_rulebook", lambda _session: None)
    large, large_combined = _separate_package(session, 10, matched)

    small_count, large_count = _count(session, small), _count(session, large)

    # The combined sheet's own bound (`tests/api/test_visual_ui.py`, 12) holds.
    assert small_combined <= 12 and large_combined <= 12
    # Flat in the number of rows, matched or not: 10 rows and 20 rows read the same.
    assert small_count - small_combined == large_count - large_combined
    # The separate file's fixed reads: the index check, the effective matches (two), the matches
    # waiting for a run, the records, the views, the package's project, and the pages with their
    # own architect drawing (for rows with no match).
    assert large_count - large_combined <= 8


def _matched_view(session: Session, view_id: UUID) -> MatchedView:
    from workflow.architect_match_records import matched_views

    return matched_views(session, {view_id})[view_id]


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
    file_name=ARCHITECT_FILE_NAME,
    separated=True,
)
_REF = ArchitectViewRefOut(
    view_id=UUID(int=1),
    document_id=UUID(int=4),
    document_version_id=UUID(int=2),
    file_name=ARCHITECT_FILE_NAME,
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
_WHEN = datetime(2026, 10, 10, 12, tzinfo=UTC)


class _Record:
    def __init__(self, created_at: datetime) -> None:
        self.created_at = created_at
        self.code_verdict = None
        self.code_pick_view_id = None
        self.ai_picks: list[object] = [{"model_id": "x", "answer": "maybe", "why": 3}, "junk"]


def _found(status: str, created_at: datetime, *, waiting: bool = False) -> SeparateFileMatches:
    match = EffectiveMatch(
        record_id=UUID(int=9),
        status=status,  # type: ignore[arg-type]
        source="automatic",
        matched=_VIEW,
        needs_reviewer=False,
        reasons=(),
        decided_by=None,
    )
    return SeparateFileMatches(
        matches={UUID(int=7): match},
        waiting=frozenset({UUID(int=9)}) if waiting else frozenset(),
        records={UUID(int=9): _Record(created_at)},  # type: ignore[dict-item]
        views={_VIEW.view_id: _REF},
    )


def _location(version: UUID, page: int) -> RowLocation:
    return RowLocation(
        page_id=uuid4(), document_version_id=version, page_number=page, polygon=[["0", "0"]]
    )


def test_compared_with_names_the_view_only_when_the_rows_own_result_used_it() -> None:
    words = "compared with the architect's drawings, page 2, view 3 SAMPLE ELEVATION (sheet Z-9)"
    earlier = _found("auto_matched", _WHEN - timedelta(minutes=1))
    on_view = [_location(UUID(int=2), 2)]
    elsewhere = [_location(UUID(int=2), 5)]

    def named(found: SeparateFileMatches, locations: list[RowLocation | None]) -> object:
        return compared_view(found, UUID(int=7), locations=locations, finding_created_at=_WHEN)

    # A compared dimension on the view's page of the view's file: named.
    assert named(earlier, on_view) == (_REF, words)
    # Compared, but against a dimension elsewhere: not this view.
    assert named(earlier, elsewhere) == (None, None)
    # No stored position: the match must predate the row's own result.
    assert named(earlier, [None]) == (_REF, words)
    later = _found("auto_matched", _WHEN + timedelta(minutes=1))
    assert named(later, [None]) == (None, None)
    # Nothing compared, a pick waiting for a run, a view not read, another row: never named.
    assert named(earlier, []) == (None, None)
    assert named(_found("reviewer_confirmed", _WHEN, waiting=True), on_view) == (None, None)
    assert named(_found("not_separated", _WHEN), on_view) == (None, None)
    assert compared_view(earlier, UUID(int=8), locations=on_view, finding_created_at=_WHEN) == (
        None,
        None,
    )


def test_waits_for_run_follows_the_waiting_records_whatever_the_source() -> None:
    assert match_out(_found("auto_matched", _WHEN, waiting=True), UUID(int=7)).waits_for_run
    assert not match_out(_found("auto_matched", _WHEN), UUID(int=7)).waits_for_run
    picked = match_out(_found("needs_reviewer", _WHEN, waiting=True), UUID(int=7))
    assert picked.waits_for_run and not picked.needs_decision


def test_odd_stored_ai_answers_are_read_safely() -> None:
    out = match_out(_found("auto_matched", _WHEN), UUID(int=7))
    assert [(p.model_label, p.answer, p.why) for p in out.ai_picks] == [("x", "no_answer", "3")]
    assert out.matched_view == _REF


class _Stored:
    def __init__(self, questions: object, code_pick: UUID | None) -> None:
        self.details = {} if questions is None else {"questions": questions}
        self.code_pick_view_id = code_pick


def test_the_picker_order_comes_from_what_the_matcher_did() -> None:
    from app.api.architect_matches import candidate_order

    asked = ["synthetic-packet-sha"]
    assert candidate_order(_Stored(asked, UUID(int=1))) == "code"  # type: ignore[arg-type]
    assert candidate_order(_Stored(asked, None)) == "ais_then_code"  # type: ignore[arg-type]
    assert candidate_order(_Stored([], None)) == "code_ais_not_asked"  # type: ignore[arg-type]
    assert candidate_order(_Stored(None, UUID(int=1))) == "code_ais_not_asked"  # type: ignore[arg-type]
