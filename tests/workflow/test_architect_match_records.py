"""Architect view matches stored append-only, read back, chosen by a reviewer, remembered (#1166).

Verification for migration `0082_architect_view_matches`, `persist_architect_matches`,
`effective_architect_match(es)`, `matched_architect_view`, `record_reviewer_match`,
`remembered_matches` and `same_vendor_item` in `workflow/architect_match_records.py`. Invented
values only.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import PackageRevision
from app.models.evidence import (
    ArchitectViewIndexEntry,
    ArchitectViewMatchRecord,
    ObservationCandidate,
)
from app.models.runs import ExtractionRun, ModelInvocation
from extraction.architect.view_matching import CodeMatch, CodeVerdict
from extraction.slot_reader.bedrock import ARCH_MATCH_PROMPT_ID
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_slot_reader import _scaffold
from workflow.architect_match_contract import MatchedView, RowMatch
from workflow.architect_match_records import (
    ReviewerMatchRefused,
    ReviewerMatchStale,
    architect_fingerprint,
    effective_architect_match,
    effective_architect_matches,
    latest_match_record,
    matched_architect_view,
    persist_architect_matches,
    record_reviewer_match,
    remembered_matches,
    same_vendor_item,
    view_carry_key,
)
from workflow.architect_reader import ARCHITECT_EXTRACTOR, ARCHITECT_EXTRACTOR_VERSION

pytest_plugins = ("tests.app.postgres_fixture",)

OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
ITEM = {"page_content_hash": "1" * 64, "page_index": 0, "pieces": 3, "row_y_pt": "310"}


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


class Indexed:
    """A revision, a vendor row's anchor, and two indexed architect views (one not separated)."""

    def __init__(self, session: Session) -> None:
        self.revision, self.version, self.page, self.run = _scaffold(session)
        self.anchor = self.new_anchor(session, self.run.id)
        self.architect_run = ExtractionRun(
            task_run_id=self.run.task_run_id,
            extractor=ARCHITECT_EXTRACTOR,
            extractor_version=ARCHITECT_EXTRACTOR_VERSION,
            config_hash="test",
            dpi=150,
        )
        session.add(self.architect_run)
        session.flush()
        self.view = self.index(session, 1, separated=True)
        self.crowded = self.index(session, 2, separated=False)
        session.flush()

    def new_anchor(self, session: Session, run_id: UUID) -> ObservationCandidate:
        anchor = ObservationCandidate(
            document_version_id=self.version.id,
            page_id=self.page.id,
            extraction_run_id=run_id,
            raw_text='30"',
            polygon=[[0, 0], [1, 0], [1, 1], [0, 1]],
            coordinate_space="image",
            ambiguity_flags=["slot-reader", "slot:0", "row-rank:1", "row-slot-count:3"],
        )
        session.add(anchor)
        session.flush()
        return anchor

    def index(
        self, session: Session, number: int, *, separated: bool, run_id: UUID | None = None
    ) -> ArchitectViewIndexEntry:
        entry = ArchitectViewIndexEntry(
            extraction_run_id=run_id or self.architect_run.id,
            document_version_id=self.version.id,
            page_id=self.page.id,
            view_number=number,
            view_tag=f"view-{number}",
            sheet_number="Z-101",
            bubble=f"{number} QX 1.1",
            title="SYNTHETIC ELEVATION",
            scale_note='1/4" = 1\'-0"',
            points_per_inch="1.5",
            extent={"x0": "0", "top": "0", "x1": "10", "bottom": "10", "stored_points": []},
            separated=separated,
            role_confirmed=separated,
            row_count=1,
            reason="test",
        )
        session.add(entry)
        session.flush()
        return entry

    def matched_view(self, entry: ArchitectViewIndexEntry) -> MatchedView:
        return MatchedView(
            view_id=entry.id,
            document_version_id=entry.document_version_id,
            page_id=entry.page_id,
            page_number=1,
            view_number=entry.view_number,
            view_tag=entry.view_tag,
            title=entry.title,
            bubble=entry.bubble,
            sheet_number=entry.sheet_number,
            scale_note=entry.scale_note,
            file_name="the architect's drawings",
            separated=entry.separated,
        )

    def automatic(
        self,
        session: Session,
        *,
        status: str = "needs_reviewer",
        chosen: ArchitectViewIndexEntry | None = None,
        anchor: ObservationCandidate | None = None,
        run_id: UUID | None = None,
        revision_id: UUID | None = None,
        packet: dict[str, object] | None = None,
        source: str = "automatic",
        details: dict[str, object] | None = None,
    ) -> ArchitectViewMatchRecord:
        match = RowMatch(
            status=status,  # type: ignore[arg-type]
            source=source,  # type: ignore[arg-type]
            chosen=None if chosen is None else self.matched_view(chosen),
            code=(
                CodeMatch(CodeVerdict.GEOMETRY_CLEAR, str(chosen.id), (), ("one view fits",))
                if status == "auto_matched" and chosen is not None
                else CodeMatch(CodeVerdict.GEOMETRY_TIE, None, (), ("two views fit",))
            ),
            ai_picks=tuple(
                {"model_id": model, "answer": "view", "view_id": str(self.view.id)}
                for model in (OPUS, SONNET)
            ),
            candidate_json=tuple(
                {"view_id": str(entry.id), "rank": rank, "shown_number": rank, "remembered": False}
                for rank, entry in enumerate((self.view, self.crowded), start=1)
            ),
            reasons=("Code has no clear winner.",),
            question_packet=packet,
            details={
                "vendor_item_key": dict(ITEM),
                "vendor_title": None,
                "vendor_references": ["3/Q101"],
                **(details or {}),
            },
        )
        row = anchor or self.anchor
        result = SimpleNamespace(
            page_id=self.page.id,
            owner_candidate_ids={"slot:0": row.id},
            architect_match=match,
        )
        persist_architect_matches(
            session,
            package_revision_id=revision_id or self.revision.id,
            extraction_run_id=run_id or self.run.id,
            results=[result],
        )
        found = latest_match_record(session, row.id)
        assert found is not None
        return found

    def pick(
        self,
        session: Session,
        *,
        view: ArchitectViewIndexEntry | None = None,
        none: bool = False,
        expected: UUID | None = None,
        anchor: ObservationCandidate | None = None,
        revision_id: UUID | None = None,
    ) -> ArchitectViewMatchRecord:
        row = anchor or self.anchor
        current = latest_match_record(session, row.id)
        record = record_reviewer_match(
            session,
            anchor=row,
            package_revision_id=revision_id or self.revision.id,
            view_id=None if view is None else view.id,
            none_of_these=none,
            note="checked by hand",
            actor="reviewer-1",
            expected_record_id=(
                expected if expected is not None else (current.id if current else None)
            ),
        )
        session.flush()
        return record


# --- the migration ---------------------------------------------------------------------------------


def test_the_migration_goes_down_and_up_when_empty(
    session: Session, postgres_engine: Engine
) -> None:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)

    def tables() -> set[str]:
        with postgres_engine.connect() as connection:
            return {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
                        "AND tablename LIKE 'architect_view_%'"
                    )
                )
            }

    session.close()
    try:
        assert tables() == {"architect_view_index", "architect_view_matches"}
        command.downgrade(config, "0081_evidence_mark_rechecks")
        assert tables() == set()
        command.upgrade(config, "head")
        assert tables() == {"architect_view_index", "architect_view_matches"}
    finally:
        command.upgrade(config, "head")


def test_the_migration_refuses_to_go_down_over_stored_records(
    session: Session, postgres_engine: Engine
) -> None:
    Indexed(session)
    session.commit()
    session.close()
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    try:
        with pytest.raises(RuntimeError, match="Preserve these records"):
            command.downgrade(config, "0081_evidence_mark_rechecks")
    finally:
        command.upgrade(config, "head")


@pytest.mark.parametrize("table", ["architect_view_index", "architect_view_matches"])
def test_records_cannot_be_edited_or_deleted(session: Session, table: str) -> None:
    indexed = Indexed(session)
    indexed.automatic(session)
    session.commit()

    for statement in (f"UPDATE {table} SET created_at = created_at", f"DELETE FROM {table}"):
        with pytest.raises(DBAPIError):
            session.execute(text(statement))
        session.rollback()


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"status": "auto_matched"}, "a matched status names its view"),
        ({"status": "reviewer_confirmed"}, "an automatic record is never the reviewer's"),
        ({"source": "reviewer"}, "a reviewer record names its reviewer and has no run"),
        ({"source": "carried", "status": "carried_over"}, "a carried record says where from"),
        ({"code_verdict": "maybe"}, "code's verdict is one of five words"),
        ({"view_tag": None}, "index rows only"),
    ],
)
def test_the_database_refuses_an_inconsistent_record(
    session: Session, changes: dict[str, object], why: str
) -> None:
    indexed = Indexed(session)
    session.commit()
    values: dict[str, Any] = {
        "package_revision_id": indexed.revision.id,
        "vendor_page_id": indexed.page.id,
        "row_anchor_candidate_id": indexed.anchor.id,
        "extraction_run_id": indexed.run.id,
        "source": "automatic",
        "status": "needs_reviewer",
        "code_verdict": "geometry_tie",
        "ai_picks": [],
        "candidates": [],
        "vendor_references": [],
        "reasons": [],
        "details": {},
    }
    if "view_tag" in changes:
        session.add(
            ArchitectViewIndexEntry(
                extraction_run_id=indexed.architect_run.id,
                document_version_id=indexed.version.id,
                page_id=indexed.page.id,
                view_number=7,
                view_tag="sheet-7",
                extent={},
                separated=True,
                role_confirmed=True,
                row_count=0,
                reason=why,
            )
        )
    else:
        session.add(ArchitectViewMatchRecord(**{**values, **changes}))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


@pytest.mark.parametrize(
    "case",
    [
        "auto-without-a-code-pick",
        "auto-on-another-view-than-codes",
        "code-pick-without-a-picking-verdict",
    ],
)
def test_the_database_holds_decision_d1s_shape(session: Session, case: str) -> None:
    """An automatic match needs code's own pick of that very view (decision D1)."""
    indexed = Indexed(session)
    session.commit()
    values: dict[str, Any] = {
        "package_revision_id": indexed.revision.id,
        "vendor_page_id": indexed.page.id,
        "row_anchor_candidate_id": indexed.anchor.id,
        "extraction_run_id": indexed.run.id,
        "source": "automatic",
        "status": "auto_matched",
        "matched_view_id": indexed.view.id,
        "code_verdict": "geometry_clear",
        "code_pick_view_id": indexed.view.id,
        "ai_picks": [],
        "candidates": [],
        "vendor_references": [],
        "reasons": [],
        "details": {},
    }
    if case == "auto-without-a-code-pick":
        values.update(code_verdict="geometry_tie", code_pick_view_id=None)
    elif case == "auto-on-another-view-than-codes":
        values.update(code_pick_view_id=indexed.crowded.id)
    else:
        values.update(status="needs_reviewer", matched_view_id=None, code_verdict="geometry_tie")
    session.add(ArchitectViewMatchRecord(**values))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# --- storing and reading ---------------------------------------------------------------------------


def test_a_runs_match_is_stored_with_both_answers_linked_to_their_calls(session: Session) -> None:
    indexed = Indexed(session)
    packet = {"question_id": "p0:arch-match", "packet_sha256": "f" * 64, "images": {}}
    calls = {}
    for model in (OPUS, SONNET):
        invocation = ModelInvocation(
            extraction_run_id=indexed.run.id,
            model_id=model,
            prompt_id=ARCH_MATCH_PROMPT_ID,
            template_id=ARCH_MATCH_PROMPT_ID,
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            outcome="ok",
            reader_page_index=0,
            reader_attempt_number=1,
            reader_question_packet=packet,
        )
        session.add(invocation)
        session.flush()
        calls[model] = str(invocation.id)

    record = indexed.automatic(session, packet=packet)

    assert (record.source, record.status, record.code_verdict) == (
        "automatic",
        "needs_reviewer",
        "geometry_tie",
    )
    assert record.matched_view_id is None and record.extraction_run_id == indexed.run.id
    assert {pick["model_id"]: pick["invocation_id"] for pick in record.ai_picks} == calls
    assert record.vendor_references == ["3/Q101"]
    assert record.details["packet_sha256"] == "f" * 64
    assert record.details["vendor_item_key"] == ITEM
    effective = effective_architect_match(session, indexed.anchor.id)
    assert effective is not None
    assert (effective.status, effective.needs_reviewer, effective.matched) == (
        "needs_reviewer",
        True,
        None,
    )
    assert matched_architect_view(session, indexed.anchor.id) is None


def test_an_automatic_match_names_its_view_and_is_the_one_compared(session: Session) -> None:
    indexed = Indexed(session)
    indexed.automatic(session, status="auto_matched", chosen=indexed.view)

    view = matched_architect_view(session, indexed.anchor.id)
    assert view is not None and view.view_id == indexed.view.id and view.page_number == 1


def test_a_view_not_clearly_apart_is_named_but_never_compared(session: Session) -> None:
    indexed = Indexed(session)
    indexed.automatic(session, status="not_separated", chosen=indexed.crowded)

    effective = effective_architect_match(session, indexed.anchor.id)
    assert effective is not None and effective.matched is not None
    assert effective.matched.separated is False
    assert matched_architect_view(session, indexed.anchor.id) is None


def test_a_row_never_matched_has_no_match(session: Session) -> None:
    indexed = Indexed(session)

    assert effective_architect_match(session, indexed.anchor.id) is None
    assert effective_architect_matches(session, []) == {}


def test_many_rows_are_read_in_two_statements(session: Session, postgres_engine: Engine) -> None:
    indexed = Indexed(session)
    second = indexed.new_anchor(session, indexed.run.id)
    indexed.automatic(session, status="auto_matched", chosen=indexed.view)
    indexed.automatic(session, anchor=second)
    session.commit()
    statements: list[str] = []

    def count(*args: Any) -> None:
        statements.append(str(args[2]))

    event.listen(postgres_engine, "before_cursor_execute", count)
    try:
        found = effective_architect_matches(session, [indexed.anchor.id, second.id, uuid4()])
    finally:
        event.remove(postgres_engine, "before_cursor_execute", count)

    assert len(statements) <= 3
    first = found[indexed.anchor.id]
    assert first is not None and first.matched is not None
    assert first.matched.view_id == indexed.view.id
    other = found[second.id]
    assert other is not None and other.status == "needs_reviewer"
    assert [value for key, value in found.items() if key not in {indexed.anchor.id, second.id}] == [
        None
    ]


# --- the reviewer's pick ---------------------------------------------------------------------------


def test_a_reviewers_pick_supersedes_and_counts(session: Session) -> None:
    indexed = Indexed(session)
    automatic = indexed.automatic(session)

    record = indexed.pick(session, view=indexed.view)

    assert record.supersedes_id == automatic.id
    assert (record.source, record.status, record.decided_by, record.extraction_run_id) == (
        "reviewer",
        "reviewer_confirmed",
        "reviewer-1",
        None,
    )
    assert record.candidates == automatic.candidates and record.note == "checked by hand"
    view = matched_architect_view(session, indexed.anchor.id)
    assert view is not None and view.view_id == indexed.view.id


def test_none_of_these_and_a_crowded_view_are_recorded_as_such(session: Session) -> None:
    indexed = Indexed(session)
    indexed.automatic(session)

    none = indexed.pick(session, none=True)
    assert (none.status, none.matched_view_id) == ("none_matches", None)
    crowded = indexed.pick(session, view=indexed.crowded)
    assert (crowded.status, crowded.matched_view_id) == ("not_separated", indexed.crowded.id)
    assert matched_architect_view(session, indexed.anchor.id) is None


def test_a_stale_pick_is_refused_and_records_nothing(session: Session) -> None:
    indexed = Indexed(session)
    automatic = indexed.automatic(session)
    indexed.pick(session, view=indexed.view)

    with pytest.raises(ReviewerMatchStale):
        indexed.pick(session, none=True, expected=automatic.id)
    assert len(session.scalars(select(ArchitectViewMatchRecord)).all()) == 2


@pytest.mark.parametrize("case", ["not-offered", "both", "neither", "no-record", "other-revision"])
def test_a_pick_that_may_not_be_recorded_is_refused(session: Session, case: str) -> None:
    indexed = Indexed(session)
    stranger = indexed.index(session, 9, separated=True)
    if case != "no-record":
        indexed.automatic(session)
    current = latest_match_record(session, indexed.anchor.id)
    options: dict[str, Any] = {
        "anchor": indexed.anchor,
        "package_revision_id": indexed.revision.id,
        "view_id": indexed.view.id,
        "none_of_these": False,
        "note": None,
        "actor": "reviewer-1",
        "expected_record_id": None if current is None else current.id,
    }
    if case == "not-offered":
        options["view_id"] = stranger.id
    elif case == "both":
        options["none_of_these"] = True
    elif case == "neither":
        options["view_id"] = None
    elif case == "other-revision":
        options["package_revision_id"] = uuid4()

    with pytest.raises(ReviewerMatchRefused):
        record_reviewer_match(session, **options)


def test_two_reviewers_superseding_the_same_record_cannot_both_land(session: Session) -> None:
    indexed = Indexed(session)
    automatic = indexed.automatic(session)
    session.commit()
    for actor in ("reviewer-1", "reviewer-2"):
        session.add(
            ArchitectViewMatchRecord(
                package_revision_id=indexed.revision.id,
                vendor_page_id=indexed.page.id,
                row_anchor_candidate_id=indexed.anchor.id,
                extraction_run_id=None,
                source="reviewer",
                status="none_matches",
                ai_picks=[],
                candidates=[],
                vendor_references=[],
                reasons=[],
                details={},
                supersedes_id=automatic.id,
                decided_by=actor,
            )
        )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# --- remembered (D3) -------------------------------------------------------------------------------


def test_a_persons_standing_decisions_are_remembered_newest_revision_first(
    session: Session,
) -> None:
    indexed = Indexed(session)
    indexed.automatic(session)
    indexed.pick(session, view=indexed.view)
    untouched = indexed.new_anchor(session, indexed.run.id)
    indexed.automatic(session, anchor=untouched)
    later = PackageRevision(
        package_id=indexed.revision.package_id,
        revision_number=2,
        state=indexed.revision.state,
        supersedes_id=indexed.revision.id,
    )
    session.add(later)
    session.flush()
    on_later = indexed.new_anchor(session, indexed.run.id)
    indexed.automatic(session, anchor=on_later, revision_id=later.id)
    indexed.pick(session, none=True, anchor=on_later, revision_id=later.id)

    found = remembered_matches(session, later.id)

    assert [(item.revision_number, item.status) for item in found] == [
        (2, "none_matches"),
        (1, "reviewer_confirmed"),
    ]
    assert found[0].view_key is None
    assert found[1].view_key == ("1" * 64, "1" * 64, "view-1", "extent:0,0,10,10")
    assert found[1].vendor_item_key == ITEM
    assert remembered_matches(session, indexed.revision.id)[0].revision_number == 1


def test_a_superseded_pick_is_not_remembered(session: Session) -> None:
    indexed = Indexed(session)
    indexed.automatic(session)
    indexed.pick(session, view=indexed.view)
    indexed.pick(session, none=True)

    (only,) = remembered_matches(session, indexed.revision.id)
    assert only.status == "none_matches"


def test_a_carried_match_says_where_it_came_from(session: Session) -> None:
    indexed = Indexed(session)
    indexed.automatic(session)
    picked = indexed.pick(session, view=indexed.view)
    rerun = indexed.new_anchor(session, indexed.run.id)

    carried = indexed.automatic(
        session,
        anchor=rerun,
        status="carried_over",
        chosen=indexed.view,
        source="carried",
        details={"carried_from_id": str(picked.id), "carried_from_revision": 1},
    )

    assert (carried.source, carried.carried_from_id) == ("carried", picked.id)
    view = matched_architect_view(session, rerun.id)
    assert view is not None and view.view_id == indexed.view.id
    newest, *_rest = remembered_matches(session, indexed.revision.id)
    assert newest.record_id == carried.id


@pytest.mark.parametrize(
    ("change", "same", "identical"),
    [
        ({}, True, True),
        ({"row_y_pt": "310.9"}, True, True),
        ({"row_y_pt": "311.5"}, False, False),
        ({"pieces": 4}, False, False),
        ({"page_content_hash": "2" * 64}, True, False),
        ({"page_content_hash": "2" * 64, "page_index": 5}, False, False),
        ({"page_index": 5}, True, True),
        ({"vendor_document_sha256": "9" * 64}, True, False),
    ],
)
def test_the_same_vendor_item_and_an_identical_one(
    change: dict[str, object], same: bool, identical: bool
) -> None:
    assert same_vendor_item(ITEM, {**ITEM, **change}) == (same, identical)


def test_a_decision_remembers_the_architect_files_it_was_made_against(session: Session) -> None:
    indexed = Indexed(session)
    indexed.automatic(session, details={"architect_files": ["a" * 64]})
    indexed.pick(session, none=True)

    (only,) = remembered_matches(session, indexed.revision.id)
    assert only.architect_files == ("a" * 64,)


def test_a_views_carry_key_holds_its_picture_or_its_extent() -> None:
    by_picture = view_carry_key("1" * 64, "2" * 64, "view-1", "3" * 64, {"x0": "0"})
    other_picture = view_carry_key("1" * 64, "2" * 64, "view-1", "4" * 64, {"x0": "0"})
    by_extent = view_carry_key("1" * 64, "2" * 64, "view-1", None, {"x0": "0", "top": "1"})
    moved = view_carry_key("1" * 64, "2" * 64, "view-1", None, {"x0": "5", "top": "1"})

    assert by_picture != other_picture and by_extent != moved
    assert architect_fingerprint(["b", "a", "b"]) == ("a", "b")
