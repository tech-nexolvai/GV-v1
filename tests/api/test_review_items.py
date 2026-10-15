"""The simple review list: every blocking result once, real guesses only, read-only (#1338)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import CheckRun, Finding, ObservationCandidate, PackageRevision
from app.models.rules import RuleDefinition, RuleSnapshot
from app.review.approval import approval_readiness
from tests.api.test_slot_rows import _package_rows, _run_current_checks
from tests.api.test_wall_cases_api import _sealed
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

BLOCKING = {"FAIL", "REVIEW_REQUIRED", "NOT_FOUND"}
DEPTH_AND_SINK = {
    "CT-DEPTH-001",
    "CT-SINK-CUTOUT-WIDTH-001",
    "CT-SINK-CUTOUT-DEPTH-001",
    "CT-SINK-OFFSET-FRONT-001",
    "CT-BACK-OFFSET-MIN-001",
    "CT-SINK-CABINET-WIDTH-001",
}


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


def _client(session: Session, project_id: UUID) -> TestClient:
    app = create_app(Settings(database_url="postgresql+psycopg://gv:gv@localhost:5434/unused"))
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _readers_differ(page_index: int, _row_offset: int, slot: str) -> list[str]:
    """Page 1's first piece: two readers wrote different numbers (made-up values)."""
    if page_index == 0 and slot == "0":
        return ["reader:model-a:20", "reader:model-b:28", "readers-differ"]
    return []


def _live(session: Session, package_id: UUID) -> list[tuple[Finding, str]]:
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    return [
        (finding, rule_id)
        for finding, rule_id in session.execute(
            select(Finding, RuleDefinition.rule_id)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
            .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
            .where(Finding.package_revision_id == revision.id, CheckRun.superseded_at.is_(None))
        ).all()
    ]


def test_every_blocking_result_is_listed_exactly_once(session: Session, tmp_path: Path) -> None:
    project_id, package_id, anchors = _package_rows(
        session, unsealed_page=0, piece_count=2, slot_flags=_readers_differ
    )
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    live = _live(session, package_id)
    blocking = {finding.id for finding, _rule in live if finding.outcome in BLOCKING}
    assert blocking, "the fixture must give blocking results to account for"

    response = _client(session, project_id).get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/review-items"
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["unaccounted"] == []
    groups: dict[str, list[str]] = {}
    for name in ("items", "failures", "not_checked"):
        ids = {entry["finding_id"] for entry in body[name] if entry["finding_id"] is not None}
        for finding_id in ids:
            groups.setdefault(finding_id, []).append(name)
    assert set(groups) == {str(finding_id) for finding_id in blocking}
    assert all(len(placed) == 1 for placed in groups.values()), groups
    # failures and not_checked list each result once.
    for name in ("failures", "not_checked"):
        listed = [entry["finding_id"] for entry in body[name]]
        assert len(listed) == len(set(listed))

    outcomes = {str(finding.id): finding.outcome for finding, _rule in live}
    assert all(outcomes[entry["finding_id"]] == "FAIL" for entry in body["failures"])
    # With the depth and sink reader off, every depth and sink check waiting is "not checked".
    assert {entry["finding_id"] for entry in body["not_checked"]} == {
        str(finding.id)
        for finding, rule in live
        if rule in DEPTH_AND_SINK and finding.outcome in BLOCKING
    }
    assert body["not_checked"], "the fixture must give depth and sink checks waiting"
    assert all(entry["status"] == "open" for entry in body["not_checked"])

    # The held number on page 1: one item of its own, with the stored suggestion as the guess and
    # each reader's text, the readers' disagreement in plain words, and Confirm/Fix while open.
    numbers = [item for item in body["items"] if item["kind"] == "number"]
    assert len(numbers) == 1
    number = numbers[0]
    assert number["page"] == 1
    assert number["candidate_id"] == str(anchors[0])
    assert number["what"] == "Piece 1 width"
    assert number["best_guess"]["source"] == "suggestion"
    assert number["best_guess"]["value"]["display"] == '20"'
    assert [reader["text"] for reader in number["readers"]] == ["20", "28"]
    assert number["actions"] == ["confirm", "fix"]
    assert number["status"] == "open"
    assert number["finding_id"] in groups

    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    readiness = approval_readiness(session, revision.id)
    header = body["header"]
    assert header["can_finalise"] is readiness.can_approve
    assert header["blocked_reason"] == readiness.reason
    assert header["failures"] == len(body["failures"])
    assert header["not_checked"] == len(body["not_checked"])
    assert header["numbers"] == 6
    assert header["confirmed_by_ai"] == 5


def test_reading_the_list_writes_nothing(session: Session, tmp_path: Path) -> None:
    project_id, package_id, _anchors = _package_rows(session, unsealed_page=0)
    _run_current_checks(session, package_id, tmp_path)
    session.commit()
    writes: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = _client(session, project_id).get(
            f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/review-items"
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    assert writes == []


def test_an_unknown_package_is_not_found(session: Session) -> None:
    project_id = uuid4()
    response = _client(session, project_id).get(
        f"{API_PREFIX}/projects/{project_id}/packages/{uuid4()}/review-items"
    )
    assert response.status_code == 404


def _candidate(flags: list[str], value: tuple[int, int] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        ambiguity_flags=flags,
        value_numerator=None if value is None else value[0],
        value_denominator=None if value is None else value[1],
        unit=None if value is None else "in",
    )


def test_best_guess_is_exactly_the_stored_value_with_every_reading_beside_it() -> None:
    """#1358: the guess is the value a Confirm records (the stored one), never the PDF text or one
    reader's text when they differ; every reading is listed so the disagreement is in sight."""
    from app.review.review_items import _readers, best_guess

    held = _candidate(
        [
            "ink:vendor",
            "reader:pdf-text-layer:21",
            "reader:model-a:21",
            "reader:model-b:20",
            "readers-differ",
        ],
        (20, 1),
    )
    guess = best_guess(held)
    assert guess is not None and guess.text == '20"' and guess.source == "suggestion"
    assert guess.value is not None and guess.value.display == '20"'
    assert [(reader.reader, reader.text) for reader in _readers(held)] == [
        ("pdf-text-layer", "21"),
        ("model-a", "21"),
        ("model-b", "20"),
    ]

    # Who wrote the stored value: the PDF's own text, else both readers identically, else neither.
    both = ["ink:vendor", "reader:model-a:12 1/4", "reader:model-b:12 1/4"]
    pdf = best_guess(_candidate(["reader:pdf-text-layer:12 1/4", *both], (49, 4)))
    assert pdf is not None and pdf.source == "pdf_text" and pdf.text == '12 1/4"'
    agreed = best_guess(_candidate([*both, "odd-fraction"], (49, 4)))
    assert agreed is not None and agreed.source == "agreed_text" and agreed.text == '12 1/4"'
    other_pdf = best_guess(_candidate(["reader:pdf-text-layer:12 3/4", *both], (49, 4)))
    assert other_pdf is not None and other_pdf.text == '12 1/4"'
    assert other_pdf.source == "agreed_text"

    # Nothing stored, or not in inches: no guess (checked by hand), whatever the texts say.
    assert best_guess(_candidate([*both, "odd-fraction"])) is None
    in_mm = _candidate(both, (311, 1))
    in_mm.unit = "mm"
    assert best_guess(in_mm) is None


def test_gv_markup_is_never_a_guess_or_a_reading() -> None:
    from app.review.review_items import _readers, best_guess, plain_why

    for markup in (["ink:vendor", "reviewer-markup"], ["ink:vendor", "gv-tick"], ["ink:reviewer"]):
        held = _candidate(
            [*markup, "reader:pdf-text-layer:30", "reader:model-a:30", "reader:model-b:30"],
            (30, 1),
        )
        assert best_guess(held) is None
        assert _readers(held) == []
    # The hold's "reviewer wrote X" is GV's own writing: never shown.
    why = plain_why(
        "Covered by reviewer markup; reviewer wrote 31 1/2", "Check the vendor's own number."
    )
    assert "31" not in why and "wrote" not in why


def test_a_second_label_or_unchecked_ink_is_never_a_guess_or_a_reading() -> None:
    """A piece with two labels keeps every label's reader texts on one record with no ink flag:
    typically the vendor's number and GV's correction beside it (made-up values). Only a record
    positively checked as the vendor's one label gives a guess or reader texts."""
    from app.review.review_items import _readers, best_guess

    texts = ["reader:model-a:30", "reader:model-b:30", "reader:model-a:31 1/2"]
    for flags in (
        ["two-labels", *texts],  # two labels, one of them GV's ink: no ink flag at all
        ["two-labels", "ink:vendor", *texts],
        ["ink-unchecked", "reader:model-a:30", "reader:model-b:30"],
        ["reader:model-a:30", "reader:model-b:30"],  # ink never recorded
        ["ink:vendor", "ink:reviewer", "reader:model-a:30", "reader:model-b:30"],
    ):
        held = _candidate([*flags, "reader:pdf-text-layer:30"], (30, 1))
        assert best_guess(held) is None, flags
        assert _readers(held) == [], flags


def test_reviewer_wrote_is_removed_to_the_end_of_its_clause() -> None:
    from app.review.review_items import plain_why

    fallback = "Check the vendor's own number."
    assert plain_why("Covered by reviewer markup; reviewer wrote 42.5", fallback) == (
        "Covered by reviewer markup"
    )
    kept = plain_why("Covered by reviewer markup; reviewer wrote 3 1/2. Box 2; edge", fallback)
    assert "3 1/2" not in kept and "Box" not in kept and kept.endswith("edge")
    assert plain_why("reviewer wrote 42.5", fallback) == fallback


def test_a_held_number_with_no_place_on_the_row_is_checked_by_hand(
    session: Session, tmp_path: Path
) -> None:
    """A held record with no `slot:` flag is never skipped: it is listed to check by hand."""
    project_id, package_id, anchors = _package_rows(session, unsealed_page=0)
    anchor = session.get(ObservationCandidate, anchors[0])
    assert anchor is not None
    unplaced = ObservationCandidate(
        document_version_id=anchor.document_version_id,
        page_id=anchor.page_id,
        extraction_run_id=anchor.extraction_run_id,
        raw_text="17",
        value_numerator=17,
        value_denominator=1,
        unit="in",
        polygon=[[0, 0], [10, 0], [10, 10], [0, 10]],
        coordinate_space="image",
        ambiguity_flags=["slot-reader", "row-rank:1", "ink:vendor", "reader:model-a:17"],
        review_reason="made-up hold",
    )
    session.add(unplaced)
    session.flush()
    _run_current_checks(session, package_id, tmp_path)
    session.commit()

    body = (
        _client(session, project_id)
        .get(f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/review-items")
        .json()
    )
    assert body["unaccounted"] == []
    assert all(item["candidate_id"] != str(unplaced.id) for item in body["items"])
    by_hand = [
        item for item in body["items"] if item["kind"] == "countertop_by_hand" and item["page"] == 1
    ]
    assert len(by_hand) == 1
    assert "place on the row was not recorded" in by_hand[0]["what"]


def test_the_accounting_names_a_result_or_page_left_out_or_listed_twice() -> None:
    from app.review.review_items import (
        FailureOut,
        NotCheckedOut,
        ReviewItemOut,
        account,
    )

    kept, dropped, doubled = uuid4(), uuid4(), uuid4()

    def item(finding_id: UUID | None, *, kind: str = "number", page: int = 1) -> ReviewItemOut:
        return ReviewItemOut(
            key=str(uuid4()),
            kind=kind,  # type: ignore[arg-type]
            page=page,
            what="made-up",
            why="made-up",
            actions=[],
            status="open",
            finding_id=finding_id,
            row_id=uuid4(),
        )

    failure = FailureOut(
        key="f", finding_id=doubled, rule_id="R", why="made-up", actions=[], status="open"
    )
    waiting = NotCheckedOut(
        key="n", finding_id=doubled, rule_id="R", check="made-up", why="made-up", status="open"
    )
    found = account(
        blocking_findings=[kept, dropped, doubled],
        blocking_pages=[4, 5],
        # Two questions on one result are still one group.
        items=[item(kept), item(kept), item(None, kind="no_countertop_page", page=4)],
        failures=[failure],
        not_checked=[waiting],
    )
    by_id = {entry.finding_id: entry.why for entry in found if entry.kind == "finding"}
    assert by_id == {
        dropped: "in no group",
        doubled: "listed more than once: failures, not_checked",
    }
    assert [(entry.page, entry.why) for entry in found if entry.kind == "page"] == [
        (5, "in no item")
    ]


# --- walls: a countertop whose only open question is its walls (#1358) ----------------------------


def test_a_wall_only_row_is_one_wall_choice_with_each_layouts_result_even_with_a_proposal(
    session: Session, tmp_path: Path
) -> None:
    """Every number sealed, nothing held, the readers proposed walls at both ends, and the walls
    change the result: one `wall_choice`, never checked by hand (whose "It is right" would confirm
    a width resting on walls nobody chose). Pieces 20" + 21", overall 42", 1" field cut."""
    project_id, package_id, anchors = _sealed(session, wall_source="readers", field_cuts=1)
    _run_current_checks(session, package_id, tmp_path)
    session.commit()

    body = (
        _client(session, project_id)
        .get(f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/review-items")
        .json()
    )

    assert body["unaccounted"] == []
    assert [item for item in body["items"] if item["kind"] == "countertop_by_hand"] == []
    walls = next(item for item in body["items"] if item["row_id"] == str(anchors[0]))
    assert walls["kind"] == "wall_choice"
    assert walls["key"] == f"walls:{walls['finding_id']}"
    assert walls["actions"] == ["choose_wall"], "no right or wrong on a wall choice"
    assert walls["status"] == "open"
    assert walls["best_guess"] is None
    assert walls["wall_results_note"] is None
    assert walls["vendor_wrote"]["display"] == '42"'
    choices = {
        choice["layout"]: (choice["proposed"], choice["result"], choice["needed"]["display"])
        for choice in walls["wall_choices"]
    }
    assert choices == {
        "back_left_right": (True, "FAIL", '43"'),
        "back_and_left": (False, "PASS", '42"'),
        "back_and_right": (False, "PASS", '42"'),
        "back_only": (False, "FAIL", '41"'),
        "island": (False, "FAIL", '41"'),
    }
    assert walls["wall_choices"][0]["words"] == "walls at both ends"


def test_a_wall_only_row_with_no_recorded_cases_offers_every_layout_and_says_why(
    session: Session, tmp_path: Path
) -> None:
    """The check could not work out every wall case (the numbers did not seal for it): the walls
    are still the open question. Every published layout is offered, the proposal pre-selected, and
    the item says why no choice shows a result."""
    from app.review.wall_cases import NOT_ALL_SEALED

    project_id, package_id, anchors = _package_rows(session, wall_source="readers")
    _run_current_checks(session, package_id, tmp_path)
    session.commit()

    body = (
        _client(session, project_id)
        .get(f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/review-items")
        .json()
    )

    assert body["unaccounted"] == []
    walls = next(item for item in body["items"] if item["row_id"] == str(anchors[0]))
    assert walls["kind"] == "wall_choice" and walls["actions"] == ["choose_wall"]
    assert [
        (choice["layout"], choice["proposed"], choice["result"]) for choice in walls["wall_choices"]
    ] == [
        ("back_left_right", True, None),
        ("back_and_left", False, None),
        ("back_and_right", False, None),
        ("back_only", False, None),
        ("island", False, None),
    ]
    assert walls["wall_results_note"] == NOT_ALL_SEALED


def test_walls_a_person_chose_before_the_run_are_no_wall_question(
    session: Session, tmp_path: Path
) -> None:
    """Page 1's walls were chosen by the reviewer before the run, so its result used them: no wall
    choice there. Page 2's walls are still open: its one wall choice."""
    project_id, package_id, anchors = _sealed(
        session, wall_source="readers", field_cuts=1, walls_confirmed=(0,)
    )
    _run_current_checks(session, package_id, tmp_path)
    session.commit()

    body = (
        _client(session, project_id)
        .get(f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/review-items")
        .json()
    )

    assert body["unaccounted"] == []
    walls = [item for item in body["items"] if item["kind"] == "wall_choice"]
    assert [item["row_id"] for item in walls] == [str(anchors[1])]


def test_walls_the_readers_did_not_agree_on_propose_nothing() -> None:
    """`walls-held:` (the readers differ), or a person's earlier answer asked again: every layout
    is offered with nothing pre-selected, as the wall review shows it."""
    from app.review.review_items import _readers_proposal

    def result(config: str | None, chosen: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            wall_layout=SimpleNamespace(config=config, chosen_by_reviewer=chosen)
        )

    def row(flags: list[str], wall_hold: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            wall_candidate=SimpleNamespace(ambiguity_flags=flags), wall_hold=wall_hold
        )

    sealed = ["walls-sealed:back_left_right", "wall-source:readers"]
    assert _readers_proposal(result("back_left_right"), row(sealed)) == "back_left_right"
    held = row([*sealed, "walls-held:readers differ"])
    assert _readers_proposal(result("back_left_right"), held) is None
    asked_again = row(sealed, wall_hold="an earlier answer could not be carried")
    assert _readers_proposal(result("back_left_right"), asked_again) is None
    assert _readers_proposal(result("back_and_left", chosen=True), row(sealed)) is None
