"""The depth and sink reader's readings on the screens (#1295).

Verification for `app/review/depth_readings.py` (`depth_readings` on the findings list, the views
tied to each countertop on the parts list, the vendor's sink note on `sink-in-use`) and for the
settings a countertop's own depth or sink result asks for (`app/review/waiting_values.py`).

**The bar.** Each waiting depth or sink check says, per countertop and per value, what the reader
read and how code checked it, a value waiting for a person (confirmed through the existing confirm
path), or why a value was not used. With the reader off (no readings) every screen is exactly as
before. Listing writes nothing, decides nothing and costs a fixed number of queries whatever the
number of countertops. The vendor's sink note is a hint and is never filled into a setting.

Every value, name and note here is made up.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import (
    DrawingView,
    ExtractionRun,
    ItemViewLink,
    ObservationCandidate,
    Package,
    Page,
    ParameterValue,
    RuleDefinition,
    RuleSnapshot,
    TaskRun,
    ViewRole,
)
from app.models.evidence import EvidenceArtifact
from app.review.depth_readings import (
    VENDOR_TEXT_EXTRACTORS,
    depth_readings_for,
    linked_views_by_proposal,
    vendor_sink_notes,
)
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.workflow.test_depth_evidence import ACTOR, _Set
from tests.workflow.test_reading_parts import REGION, _confirmed
from tests.workflow.test_stages import _publish_rulebook
from verdict.outcomes import Outcome
from vocabulary.part_kinds import PartKind
from workflow.depth_evidence import (
    CONFIRM_ONLY,
    DEPTH_RULE_IDS,
    QUALIFIED,
)
from workflow.stages import _PRINTED_CODE_EXTRACTORS
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)

CT010, CT012, CT008, CT007 = "CT010", "CT012", "CT008", "CT007"


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only")


def _client(session: Session, store: LocalStore, sheet: _Set) -> Any:
    from fastapi.testclient import TestClient

    from app.auth import Principal, Role, authenticate

    project = session.get_one(Package, sheet.revision.package_id).project_id
    app = create_app(
        Settings(database_url="postgresql+psycopg://unused@localhost/unused", environment="test")
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[authenticate] = lambda: Principal(
        id=ACTOR, roles=frozenset({Role("reviewer")}), projects=frozenset({project})
    )
    return TestClient(app, raise_server_exceptions=False)


def _base(sheet: _Set) -> str:
    package = sheet.session.get_one(Package, sheet.revision.package_id)
    return f"/api/v1/projects/{package.project_id}/packages/{package.id}"


def _crop(session: Session, store: LocalStore, row: ObservationCandidate) -> None:
    """The reading's picture, as the evidence stage cuts it."""
    content = b"synthetic crop " + str(row.id).encode()
    key = f"evidence/crops/{row.id}.png"
    store.put(key, io.BytesIO(content), content_type="image/png")
    session.add(
        EvidenceArtifact(
            candidate_id=row.id,
            document_version_id=row.document_version_id,
            page_id=row.page_id,
            kind="crop",
            storage_key=key,
            sha256=hashlib.sha256(content).hexdigest(),
            media_type="image/png",
            coordinate_space="image",
            shows_gv_marks=False,
        )
    )
    session.flush()


# -- the reader never read the set: every screen as before -----------------------------------------


# -- one line per value, per countertop -----------------------------------------------------------


# -- the settings a countertop's own result still waits on -----------------------------------------


def test_the_per_countertop_rules_are_the_depth_reader_s_five() -> None:
    from app.review.waiting_values import PER_COUNTERTOP_RULES

    assert PER_COUNTERTOP_RULES == DEPTH_RULE_IDS


# -- read-only, and a fixed number of queries -----------------------------------------------------


@dataclass(frozen=True)
class _Item:
    id: UUID
    package_revision_id: UUID
    rule_id: str
    rule_snapshot_id: UUID
    outcome: Outcome
    scope_item_id: UUID | None
    scope_row_candidate_id: UUID | None = None


def _snapshots(session: Session) -> dict[str, UUID]:
    return dict(
        session.execute(
            select(RuleDefinition.rule_id, RuleSnapshot.id).join(
                RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id
            )
        ).all()
    )


def _countertop(sheet: _Set, index: int) -> tuple[UUID, UUID, Page]:
    """Another countertop of the set, alone on a page of its own."""
    session = sheet.session
    page = Page(
        document_version_id=sheet.page.document_version_id,
        index=index,
        content_hash=f"{index:x}" * 64,
        width_pt=400,
        height_pt=100,
        rotation=0,
        has_vector_text=True,
        render_failed=False,
        media_box=["0", "0", "400", "100"],
        crop_box=["0", "0", "400", "100"],
    )
    session.add(page)
    session.flush()
    view = DrawingView(page_id=page.id, tag=f"SYNTHETIC {index}", region=REGION)
    session.add(view)
    session.flush()
    confirm_view_role(session, view=view, role=ViewRole.SHOP, actor=ACTOR)
    proposal, item = _confirmed(session, view, PartKind.COUNTERTOP, "0.20", "0.80")
    return proposal, item, page


def _read_and_list(
    sheet: _Set, store: LocalStore, proposal: UUID, item: UUID, page: Page
) -> list[_Item]:
    """A depth to use and a front offset to confirm on the countertop's page, and its five
    waiting results."""
    sheet.page = page
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link(proposal))
    row = sheet.reading(
        CT007, 4, CONFIRM_ONLY, link=sheet.link(proposal), confirmed_by="drawn-length-only"
    )
    _crop(sheet.session, store, row)
    snapshots = _snapshots(sheet.session)
    return [
        _Item(uuid4(), sheet.revision.id, rule_id, snapshots[rule_id], Outcome.NOT_FOUND, item)
        for rule_id in DEPTH_RULE_IDS
    ]


def _statements(session: Session, items: list[_Item]) -> tuple[int, dict[UUID, Any]]:
    """How many statements one listing sends, and what it answers."""
    engine = session.get_bind()
    counted: list[str] = []

    def count(*args: Any) -> None:
        counted.append(args[2])

    event.listen(engine, "before_cursor_execute", count)
    try:
        answers = depth_readings_for(session, items)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    return len(counted), answers


def test_listing_costs_the_same_queries_whatever_the_number_of_countertops(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    _publish_rulebook(session)
    snapshots = _snapshots(session)
    whole = [
        _Item(uuid4(), sheet.revision.id, rule_id, snapshots[rule_id], Outcome.NOT_FOUND, None)
        for rule_id in DEPTH_RULE_IDS
    ]
    items = whole + _read_and_list(sheet, store, sheet.proposal, sheet.item, sheet.page)
    session.commit()
    one, answers = _statements(session, items)

    for index in (1, 2):
        proposal, item, page = _countertop(sheet, index)
        items += _read_and_list(sheet, store, proposal, item, page)
    session.commit()
    three, answers = _statements(session, items)

    assert one == three, (one, three)
    # Each countertop's own results show its own readings; the whole-package depth shows all three.
    assert all(answers[item.id] for item in items if item.scope_item_id is not None)
    depth = next(i for i in whole if i.rule_id == "CT-DEPTH-001")
    assert [line["countertop"] for line in (a.model_dump() for a in answers[depth.id])] == [
        "Countertop 1 on page 1",
        "Countertop 1 on page 2",
        "Countertop 1 on page 3",
    ]


# -- the views tied to each countertop (the Measure page's chip) ----------------------------------


def test_a_person_s_correction_moves_the_view_to_the_other_countertop(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    other, _item = _confirmed(session, sheet.view, PartKind.COUNTERTOP, "0.10", "0.15")
    first = sheet.link(kind="plan")
    session.add(
        ItemViewLink(
            part_proposal_id=other,
            page_id=first.page_id,
            region=first.region,
            view_kind="section",
            heading_text=None,
            judged_by="person",
            extraction_run_id=None,
            supersedes_id=first.id,
            linked_by=ACTOR,
        )
    )
    session.flush()

    views = linked_views_by_proposal(session, sheet.revision.id)

    assert sheet.proposal not in views
    assert [(v.view, v.by_person) for v in views[other]] == [("section", True)]


# -- the vendor's sink note: a hint, never a setting ------------------------------------------------


def _text(sheet: _Set, extractor: str, text: str) -> None:
    session = sheet.session
    task = session.get_one(TaskRun, sheet.run.task_run_id)
    run = ExtractionRun(
        task_run_id=task.id, extractor=extractor, extractor_version="1", config_hash="t", dpi=300
    )
    session.add(run)
    session.flush()
    session.add(
        ObservationCandidate(
            document_version_id=sheet.page.document_version_id,
            page_id=sheet.page.id,
            extraction_run_id=run.id,
            raw_text=text,
            polygon=[[700, 150], [800, 150], [800, 200], [700, 200]],
            coordinate_space="image",
            ambiguity_flags=[],
        )
    )
    session.flush()


def test_the_vendor_s_sink_note_is_shown_as_a_hint_and_never_saved(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    _text(sheet, "pdfplumber", "ACME-100 undermount sink by Synthetic Co")
    _text(sheet, "pdfplumber", "SINK")  # a lone label names no sink
    _text(sheet, "rapidocr", "undermount sink")  # inside the note above: one note
    _text(sheet, "extraction.annotations", "GV: sink is wrong")  # GV's markup is never read
    session.commit()
    settings_before = session.scalar(select(func.count()).select_from(ParameterValue))

    sink = _client(session, store, sheet).get(f"{_base(sheet)}/sink-in-use").json()

    assert sink["vendor_notes"] == [
        {"text": "ACME-100 undermount sink by Synthetic Co", "page_number": 1}
    ]
    assert sink["inside_width"] is None and sink["inside_depth"] is None
    assert session.scalar(select(func.count()).select_from(ParameterValue)) == settings_before


def test_no_sink_note_without_a_depth_reading(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    _text(sheet, "pdfplumber", "ACME-100 undermount sink")

    assert vendor_sink_notes(session, sheet.revision.id) == []


def test_the_sink_note_reads_the_vendor_s_own_text_layers_only() -> None:
    assert VENDOR_TEXT_EXTRACTORS == _PRINTED_CODE_EXTRACTORS


# -- review of #1296: a person's confirmation of one countertop's depth never passes the package ---


# -- a countertop row's own held result (#1201) ---------------------------------------------------


def test_a_countertop_row_s_own_result_lists_what_was_read_for_that_row_only(
    session: Session, store: LocalStore
) -> None:
    """Since #1201 a countertop row gets its own held depth result. It lists what the reader read
    for the countertop suggested from that row, as the whole-package result did, and never offers
    to confirm it there. Another row's result lists nothing of it."""
    from app.models import PartProposalRow
    from tests.workflow.test_several_countertops import _blank_page, _slot_rows

    sheet = _Set(session, store)
    other_page = _blank_page(sheet, 1)
    _slot_rows(sheet, [sheet.page, other_page])
    anchors = {
        row.page_id: row
        for row in session.scalars(
            select(ObservationCandidate).where(
                ObservationCandidate.ambiguity_flags.contains(["slot:0"])
            )
        )
    }
    mine, theirs = anchors[sheet.page.id], anchors[other_page.id]
    session.add(
        PartProposalRow(
            part_proposal_id=sheet.proposal,
            row_candidate_id=mine.id,
            slot_index=None,
            extraction_run_id=mine.extraction_run_id,
            kind_named=True,
        )
    )
    session.flush()
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    _publish_rulebook(session)
    snapshots = _snapshots(session)
    items = [
        _Item(uuid4(), sheet.revision.id, rule_id, snapshots[rule_id], outcome, None, row.id)
        for rule_id in ("CT-DEPTH-001", "CT-SINK-CUTOUT-WIDTH-001")
        for row, outcome in ((mine, Outcome.REVIEW_REQUIRED), (theirs, Outcome.REVIEW_REQUIRED))
    ]

    answers = depth_readings_for(session, items)

    depth_mine, depth_theirs, sink_mine, sink_theirs = items
    lines = [line.model_dump() for line in answers[depth_mine.id]]
    assert [(line["countertop"], line["state"], line["value"]) for line in lines] == [
        ("Countertop 1 on page 1", "read", '24"')
    ]
    assert not any(line.get("can_confirm") for line in lines)
    # Only what was read: nothing for the sink; and nothing of it on the other row's result.
    assert depth_theirs.id not in answers and sink_mine.id not in answers
    assert sink_theirs.id not in answers
