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
    CanonicalObservation,
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
from app.models.verdicts import Finding
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
from workflow.countertop_runs import withdraw_countertop_run
from workflow.depth_evidence import (
    CONFIRM_ONLY,
    DEPTH_READER_FLAG,
    DEPTH_RULE_IDS,
    HELD,
    QUALIFIED,
    VIEW_NOTE,
)
from workflow.stages import _PRINTED_CODE_EXTRACTORS, DatabaseStages
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


def _findings(client: Any, sheet: _Set) -> dict[tuple[str, str | None], dict[str, Any]]:
    response = client.get(f"{_base(sheet)}/findings?limit=200")
    assert response.status_code == 200, response.text
    return {(item["rule_id"], item["scope_item_id"]): item for item in response.json()["items"]}


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


def _checked(sheet: _Set, store: LocalStore) -> dict[tuple[str, str | None], dict[str, Any]]:
    sheet.check(store, on=True)
    sheet.session.commit()
    return _findings(_client(sheet.session, store, sheet), sheet)


def _mine(found: dict[tuple[str, str | None], dict[str, Any]], sheet: _Set, rule: str) -> Any:
    return found[(rule, str(sheet.item))]


# -- the reader never read the set: every screen as before -----------------------------------------


def test_with_no_readings_every_screen_is_as_before(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    session.delete(sheet.run)
    session.flush()
    _publish_rulebook(session)
    DatabaseStages(store, discriminators={"wall_config": "back_left_right"}).run_checks(
        session, sheet.revision.id
    )
    session.commit()
    client = _client(session, store, sheet)

    found = _findings(client, sheet)
    parts = client.get(f"{_base(sheet)}/parts").json()
    sink = client.get(f"{_base(sheet)}/sink-in-use").json()

    assert found and all(item["depth_readings"] == [] for item in found.values())
    # The waiting card still asks exactly what it asked before: the whole-package results only.
    for rule_id in DEPTH_RULE_IDS:
        (item,) = [i for (r, _), i in found.items() if r == rule_id]
        assert item["scope_item_id"] is None
        assert item["missing_inputs"], rule_id
    assert all(part["linked_views"] == [] for d in parts["drawings"] for part in d["parts"])
    assert sink["vendor_notes"] == []


def test_a_reader_that_wrote_nothing_adds_nothing(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    found = _checked(sheet, store)

    # Scoped results say why on their own reason; the card adds one "not read" line per value.
    depth = _mine(found, sheet, "CT-DEPTH-001")
    assert [line["state"] for line in depth["depth_readings"]] == ["not_read"]
    assert depth["depth_readings"][0]["reason"] == (
        "no plan or section view of its drawing was found."
    )


# -- one line per value, per countertop -----------------------------------------------------------


def test_a_qualified_reading_says_where_it_was_read_and_how_code_checked_it(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    row = sheet.reading(CT010, 24, QUALIFIED, link=sheet.link(), confirmed_by="vector-text")
    _crop(session, store, row)

    found = _checked(sheet, store)

    (line,) = _mine(found, sheet, "CT-DEPTH-001")["depth_readings"]
    assert line == {
        "countertop": "Countertop 1 on page 1",
        "field": CT010,
        "label": "Countertop depth",
        "state": "read",
        "value": '24"',
        "view": "plan",
        "page_number": 1,
        "checked_by": "printed_text",
        "candidate_id": str(row.id),
        "has_picture": True,
        "picture_shows_gv_mark": False,
        "can_confirm": False,
        "reason": None,
    }


def test_a_chain_checked_reading_says_so(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    sheet.reading(
        CT010, 24, QUALIFIED, link=sheet.link(kind="section"), confirmed_by="chain", kind="section"
    )

    (line,) = _mine(_checked(sheet, store), sheet, "CT-DEPTH-001")["depth_readings"]

    assert (line["state"], line["checked_by"], line["view"]) == ("read", "chain", "section")


def test_a_drawn_length_reading_asks_a_person_and_is_confirmed_by_the_existing_path(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    row = sheet.reading(
        CT010, 24, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only", kind="section"
    )
    _crop(session, store, row)
    found = _checked(sheet, store)
    depth = _mine(found, sheet, "CT-DEPTH-001")
    assert depth["outcome"] == Outcome.REVIEW_REQUIRED.value
    (line,) = depth["depth_readings"]
    assert (line["state"], line["value"], line["checked_by"]) == (
        "to_confirm",
        '24"',
        "drawn_length",
    )
    assert line["can_confirm"] is True

    # "Confirm this reading": the same endpoint Measurements uses, with the reading's own code.
    client = _client(session, store, sheet)
    confirmed = client.post(
        f"{_base(sheet)}/candidates/{line['candidate_id']}/confirm",
        json={"semantic_type": line["field"]},
    )
    assert confirmed.status_code == 201, confirmed.text

    (after,) = _mine(_findings(client, sheet), sheet, "CT-DEPTH-001")["depth_readings"]
    assert (after["state"], after["value"], after["can_confirm"]) == ("confirmed", '24"', False)


def test_a_reading_without_its_picture_cannot_be_confirmed_here(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only")

    (line,) = _mine(_checked(sheet, store), sheet, "CT-DEPTH-001")["depth_readings"]

    assert (line["state"], line["has_picture"], line["can_confirm"]) == ("to_confirm", False, False)


def test_a_reading_a_person_said_is_something_else_says_so(
    session: Session, store: LocalStore
) -> None:
    from app.evidence.confirm import confirm_candidate_type

    sheet = _Set(session, store)
    row = sheet.reading(
        CT010, 24, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only"
    )
    confirm_candidate_type(session, candidate_id=row.id, semantic_type=CT012, confirmed_by=ACTOR)

    (line,) = _mine(_checked(sheet, store), sheet, "CT-DEPTH-001")["depth_readings"]

    assert (line["state"], line["value"]) == ("not_this", '24"')


def test_a_held_value_gives_code_s_plain_reason(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, None, HELD, link=sheet.link(), reason="the label is cut by the edge.")

    (line,) = _mine(_checked(sheet, store), sheet, "CT-DEPTH-001")["depth_readings"]

    assert (line["state"], line["value"], line["reason"]) == (
        "held",
        None,
        "the label is cut by the edge.",
    )


def test_a_view_tied_to_no_one_countertop_is_held_with_why(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(
        CT010,
        24,
        CONFIRM_ONLY,
        unplaced=(sheet.proposal,),
        reason="the drawing holding the view holds 2 countertops.",
        confirmed_by="drawn-length-only",
    )

    (line,) = _mine(_checked(sheet, store), sheet, "CT-DEPTH-001")["depth_readings"]

    assert line["state"] == "held" and line["can_confirm"] is False
    assert line["reason"] == (
        "the view it was read on could not be tied to one countertop: the drawing holding the "
        "view holds 2 countertops."
    )


def test_a_view_that_read_nothing_says_why(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    link = sheet.link()
    session.add(
        ObservationCandidate(
            document_version_id=sheet.page.document_version_id,
            page_id=sheet.page.id,
            extraction_run_id=sheet.run.id,
            raw_text="",
            polygon=[[0, 0], [10, 0], [10, 10], [0, 10]],
            coordinate_space="image",
            ambiguity_flags=[
                DEPTH_READER_FLAG,
                VIEW_NOTE,
                "view-kind:plan",
                f"item:{sheet.proposal}",
                f"link:{link.id}",
            ],
            review_reason="the two readers named different kinds of view.",
        )
    )
    session.flush()

    lines = _mine(_checked(sheet, store), sheet, "CT-BACK-OFFSET-MIN-001")["depth_readings"]

    assert sorted(line["field"] for line in lines) == sorted([CT010, CT007, CT008])
    assert {line["state"] for line in lines} == {"not_read"}
    assert {line["reason"] for line in lines} == {"the two readers named different kinds of view."}


def test_another_countertop_s_reading_is_never_shown_on_this_one(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    other_view = DrawingView(page_id=sheet.page.id, tag="OTHER", region=REGION)
    session.add(other_view)
    session.flush()
    confirm_view_role(session, view=other_view, role=ViewRole.SHOP, actor=ACTOR)
    other, _item = _confirmed(session, other_view, PartKind.COUNTERTOP, "0.20", "0.80")
    sheet.reading(CT010, 30, QUALIFIED, link=sheet.link(other))

    (line,) = _mine(_checked(sheet, store), sheet, "CT-DEPTH-001")["depth_readings"]

    assert line["state"] == "not_read"


def test_a_decided_result_and_other_checks_get_nothing(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT007, 4, QUALIFIED, link=sheet.link())

    found = _checked(sheet, store)

    offset = _mine(found, sheet, "CT-SINK-OFFSET-FRONT-001")
    assert offset["outcome"] == Outcome.PASS.value and offset["depth_readings"] == []
    assert all(
        item["depth_readings"] == []
        for (rule, _), item in found.items()
        if rule not in DEPTH_RULE_IDS
    )


def test_a_whole_package_result_lists_what_was_read_countertop_by_countertop(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    _publish_rulebook(session)
    # No run decision on the countertop: the five checks stay one per revision.
    DatabaseStages(
        store, discriminators={"wall_config": "back_left_right"}, depth_reader=True
    ).run_checks(session, sheet.revision.id)
    session.commit()

    found = _findings(_client(session, store, sheet), sheet)

    whole = found[("CT-DEPTH-001", None)]
    assert [
        (line["countertop"], line["state"], line["value"]) for line in whole["depth_readings"]
    ] == [("Countertop 1 on page 1", "read", '24"')]
    # A whole-package result lists only what was read: nothing for the sink values.
    assert found[("CT-SINK-CUTOUT-WIDTH-001", None)]["depth_readings"] == []


# -- the settings a countertop's own result still waits on -----------------------------------------


def test_a_countertop_s_own_waiting_result_asks_its_settings_not_its_drawn_values(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())

    depth = _mine(_checked(sheet, store), sheet, "CT-DEPTH-001")

    assert depth["outcome"] == Outcome.NOT_FOUND.value
    assert [(need["key"], need["scope"]) for need in depth["missing_inputs"]] == [
        ("cabinet_depth", "project"),
        ("countertop_overhang", "project"),
    ]


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


def test_listing_writes_nothing(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    row = sheet.reading(CT007, 4, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only")
    _crop(session, store, row)
    found = _checked(sheet, store)
    tables = (CanonicalObservation, ObservationCandidate, ItemViewLink, Finding, ParameterValue)

    def counts() -> list[int]:
        return [session.scalar(select(func.count()).select_from(t)) or 0 for t in tables]

    before = counts()
    client = _client(session, store, sheet)
    again = _findings(client, sheet)
    client.get(f"{_base(sheet)}/parts")
    client.get(f"{_base(sheet)}/sink-in-use")

    assert counts() == before
    assert again == found


# -- the views tied to each countertop (the Measure page's chip) ----------------------------------


def test_each_countertop_lists_its_tied_views_with_their_kind(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.link(kind="plan")
    sheet.link(kind="elevation")
    session.commit()

    parts = _client(session, store, sheet).get(f"{_base(sheet)}/parts").json()

    (part,) = [
        p for d in parts["drawings"] for p in d["parts"] if p["proposal_id"] == str(sheet.proposal)
    ]
    assert part["linked_views"] == [
        {"view": "plan", "page_number": 1, "heading": "PLAN VIEW", "by_person": False},
        {"view": "front", "page_number": 1, "heading": "PLAN VIEW", "by_person": False},
    ]


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


def _two_countertops(
    session: Session, store: LocalStore
) -> tuple[_Set, Any, list[ObservationCandidate]]:
    """The reviewer's case: two countertops, each alone on its drawing, each with a depth only its
    drawn length checks (25" and 30"); cabinet depth 24" and overhang 1" set for the job; no
    countertop in scope, so the five checks run once for the whole package."""
    sheet = _Set(session, store)
    first = sheet.reading(
        CT010, 25, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only"
    )
    proposal, _item, page = _countertop(sheet, 1)
    sheet.page = page
    second = sheet.reading(
        CT010, 30, CONFIRM_ONLY, link=sheet.link(proposal), confirmed_by="drawn-length-only"
    )
    for row in (first, second):
        _crop(session, store, row)
    _publish_rulebook(session)
    session.commit()
    client = _client(session, store, sheet)
    saved = client.post(
        f"{_base(sheet)}/measurements",
        json={
            "parameters": [
                {"name": name, "value": value, "scope": "project", "reference": "Synthetic spec"}
                for name, value in (("cabinet_depth", '24"'), ("countertop_overhang", '1"'))
            ]
        },
    )
    assert saved.status_code == 201, saved.text
    return sheet, client, [first, second]


def _whole_package_run(sheet: _Set, store: LocalStore) -> None:
    DatabaseStages(
        store, discriminators={"wall_config": "back_left_right"}, depth_reader=True
    ).run_checks(sheet.session, sheet.revision.id)
    sheet.session.commit()


def test_confirming_one_countertop_s_depth_never_passes_the_whole_package(
    session: Session, store: LocalStore
) -> None:
    sheet, client, (first, _second) = _two_countertops(session, store)
    _whole_package_run(sheet, store)

    confirmed = client.post(
        f"{_base(sheet)}/candidates/{first.id}/confirm", json={"semantic_type": CT010}
    )
    assert confirmed.status_code == 201, confirmed.text
    _whole_package_run(sheet, store)

    whole = _findings(client, sheet)[("CT-DEPTH-001", None)]
    assert whole["outcome"] == Outcome.REVIEW_REQUIRED.value, whole
    assert "Countertop 1 on page 1" in whole["reason"], whole["reason"]
    assert "never for the whole package" in whole["reason"]


def test_confirming_from_the_measurements_list_never_passes_the_whole_package(
    session: Session, store: LocalStore
) -> None:
    sheet, client, (first, _second) = _two_countertops(session, store)
    _whole_package_run(sheet, store)

    # The Measurements page lists the reading for a person to confirm, and confirms it the same way.
    listed = client.get(f"{_base(sheet)}/candidates", params={"page_number": 1})
    assert listed.status_code == 200, listed.text
    assert str(first.id) in {c["candidate_id"] for c in listed.json()["candidates"]}
    confirmed = client.post(
        f"{_base(sheet)}/candidates/{first.id}/confirm", json={"semantic_type": CT010}
    )
    assert confirmed.status_code == 201, confirmed.text
    _whole_package_run(sheet, store)

    found = _findings(client, sheet)
    assert found[("CT-DEPTH-001", None)]["outcome"] != Outcome.PASS.value
    assert found[("CT-BACK-OFFSET-MIN-001", None)]["outcome"] != Outcome.PASS.value


def test_the_whole_package_card_never_offers_confirm(session: Session, store: LocalStore) -> None:
    sheet, client, _rows = _two_countertops(session, store)
    _whole_package_run(sheet, store)

    whole = _findings(client, sheet)[("CT-DEPTH-001", None)]

    assert [(line["value"], line["state"]) for line in whole["depth_readings"]] == [
        ('25"', "to_confirm"),
        ('30"', "to_confirm"),
    ]
    assert all(line["can_confirm"] is False for line in whole["depth_readings"])


def test_a_shared_drawing_s_reading_is_never_offered_for_confirming(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    _confirmed(session, sheet.view, PartKind.COUNTERTOP, "0.10", "0.15")
    row = sheet.reading(
        CT010, 24, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only"
    )
    _crop(session, store, row)

    lines = _mine(_checked(sheet, store), sheet, "CT-DEPTH-001")["depth_readings"]

    assert lines and all(line["can_confirm"] is False for line in lines)


def _reread(sheet: _Set, inches: int) -> ObservationCandidate:
    """The same dimension read again, by a newer reading tied to the same countertop."""
    link = sheet.session.scalars(
        select(ItemViewLink).where(ItemViewLink.part_proposal_id == sheet.proposal)
    ).first()
    sheet.rerun()
    row = sheet.reading(
        CT010,
        inches,
        CONFIRM_ONLY,
        link=link,
        confirmed_by="drawn-length-only",
        view="p0:v1.1",
    )
    return row


def test_a_person_s_confirmation_carries_over_a_re_read_on_the_card(
    session: Session, store: LocalStore
) -> None:
    from app.evidence.confirm import confirm_candidate_type

    sheet = _Set(session, store)
    first = sheet.reading(
        CT010, 24, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only", view="p0:v1.1"
    )
    confirm_candidate_type(session, candidate_id=first.id, semantic_type=CT010, confirmed_by=ACTOR)
    _reread(sheet, 24)

    (line,) = _mine(_checked(sheet, store), sheet, "CT-DEPTH-001")["depth_readings"]

    assert (line["state"], line["value"], line["can_confirm"]) == ("confirmed", '24"', False)


def test_a_re_read_with_another_number_than_a_person_confirmed_is_held_on_the_card(
    session: Session, store: LocalStore
) -> None:
    from app.evidence.confirm import confirm_candidate_type

    sheet = _Set(session, store)
    first = sheet.reading(
        CT010, 24, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only", view="p0:v1.1"
    )
    confirm_candidate_type(session, candidate_id=first.id, semantic_type=CT010, confirmed_by=ACTOR)
    _reread(sheet, 26)

    (line,) = _mine(_checked(sheet, store), sheet, "CT-DEPTH-001")["depth_readings"]

    assert (line["state"], line["can_confirm"]) == ("held", False)
    assert line["reason"] == (
        "a person confirmed 24\" for this countertop's countertop depth on an earlier reading of "
        'this view, and the new reading says 26": held for a person.'
    )


def test_the_job_s_settings_are_asked_once_not_on_every_countertop_s_card(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    _proposal, other, _page = _countertop(sheet, 1)
    _publish_rulebook(session)
    for item in (sheet.item, other):
        withdraw_countertop_run(session, countertop_item_id=item, actor=ACTOR)
    DatabaseStages(
        store, discriminators={"wall_config": "back_left_right"}, depth_reader=True
    ).run_checks(session, sheet.revision.id)
    session.commit()

    found = _findings(_client(session, store, sheet), sheet)

    asked = [
        [need["key"] for need in found[("CT-DEPTH-001", str(item))]["missing_inputs"]]
        for item in (sheet.item, other)
    ]
    assert sorted(asked) == [[], ["cabinet_depth", "countertop_overhang"]]
