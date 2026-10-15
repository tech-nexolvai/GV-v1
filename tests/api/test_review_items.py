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
from app.models import CheckRun, Finding, PackageRevision
from app.models.rules import RuleDefinition, RuleSnapshot
from app.review.approval import approval_readiness
from tests.api.test_slot_rows import _package_rows, _run_current_checks
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


def test_best_guess_takes_the_file_text_then_agreed_text_then_the_suggestion() -> None:
    from app.review.review_items import best_guess

    both = ["reader:model-a:12 1/2", "reader:model-b:12 1/2"]
    pdf = best_guess(_candidate(["reader:pdf-text-layer:12 3/4", *both], (49, 4)))
    assert pdf is not None and pdf.source == "pdf_text" and pdf.text == "12 3/4"
    assert pdf.value is not None and pdf.value.display == '12 3/4"'

    agreed = best_guess(_candidate([*both, "odd-fraction"], (49, 4)))
    assert agreed is not None and agreed.source == "agreed_text" and agreed.text == "12 1/2"

    suggested = best_guess(_candidate(["reader:model-a:12", "reader:model-b:13"], (49, 4)))
    assert suggested is not None and suggested.source == "suggestion"
    assert suggested.text == '12 1/4"'

    assert best_guess(_candidate(["reader:model-a:12", "reader:model-b:13"])) is None


def test_gv_markup_is_never_a_guess_or_a_reading() -> None:
    from app.review.review_items import _readers, best_guess, plain_why

    for markup in (["reviewer-markup"], ["gv-tick"], ["ink:reviewer"]):
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
