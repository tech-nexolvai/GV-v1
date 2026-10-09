"""Documents table: search, sort and the decision split on `packages-summary` (#1065).

Synthetic vendors and findings only. Every test reads the summary through the HTTP API, and every
expectation is computed from the rows the test wrote, never from the endpoint's own answer.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import (
    CheckRun,
    Finding,
    Package,
    PackageRevision,
    Project,
)
from app.models.review import Approval, ReviewAction, ReviewActionKind
from app.review.approval import approval_readiness, approval_readiness_many
from app.review.session import open_session, record_action
from tests.app.postgres_fixture import alembic_config
from tests.review.test_session import _finding

pytest_plugins = ("tests.app.postgres_fixture",)

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


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
    app = create_app(Settings(database_url="postgresql+psycopg://gv:gv@localhost:5433/gv"))
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _add_finding(session: Session, revision: PackageRevision, outcome: str) -> Finding:
    finding = _finding(session, revision)
    if outcome != "FAIL":
        run = session.get(CheckRun, finding.check_run_id)
        assert run is not None
        other = CheckRun(
            package_revision_id=revision.id,
            rule_snapshot_id=run.rule_snapshot_id,
            engine_version=run.engine_version,
        )
        session.add(other)
        session.flush()
        replacement = Finding(
            check_run_id=other.id,
            package_revision_id=revision.id,
            outcome=outcome,
            severity="FLAG",
            trace={},
            parameter_set_versions={},
            reason="Synthetic",
        )
        run.superseded_at = BASE_TIME
        session.add(replacement)
        session.flush()
        return replacement
    return finding


def _package(
    session: Session,
    project: Project,
    *,
    vendor: str | None,
    product_type: str | None = "countertop",
    minutes: int,
    outcomes: tuple[str, ...] = (),
) -> Package:
    package = Package(project_id=project.id, vendor=vendor, product_type=product_type)
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id,
        revision_number=1,
        state="AWAITING_REVIEW",
        created_at=BASE_TIME + timedelta(minutes=minutes),
    )
    session.add(revision)
    session.flush()
    for outcome in outcomes:
        _add_finding(session, revision, outcome)
    return package


def _project(session: Session) -> Project:
    project = Project(name="documents summary tests")
    session.add(project)
    session.flush()
    return project


def _seed(session: Session) -> Project:
    """Eight packages: ties on time, on vendor and on decisions, and two without a vendor."""
    project = _project(session)
    _package(session, project, vendor="Beta Stone", minutes=5, outcomes=("FAIL", "NOT_FOUND"))
    _package(session, project, vendor="alpha marble", minutes=5, outcomes=("PASS",))
    _package(session, project, vendor="Gamma Works", minutes=1, outcomes=("REVIEW_REQUIRED",))
    _package(session, project, vendor=None, minutes=7, outcomes=("FAIL",))
    _package(session, project, vendor="  ", minutes=2)
    _package(session, project, vendor="beta stone", minutes=3, outcomes=("FAIL", "FAIL"))
    _package(
        session, project, vendor="Delta", product_type="cabinet", minutes=4, outcomes=("PASS",)
    )
    _package(session, project, vendor="ALPHA Marble", minutes=6, outcomes=("NOT_FOUND",))
    # A package in another project: never listed, whatever the search.
    other = _project(session)
    _package(session, other, vendor="Beta Stone", minutes=9, outcomes=("FAIL",))
    session.commit()
    return project


def _walk(client: TestClient, project: Project, limit: int, **params: str) -> list[dict[str, Any]]:
    """Every page, following `next_cursor` verbatim, refusing to loop."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        query = {"limit": str(limit), **params}
        if cursor is not None:
            query["cursor"] = cursor
        response = client.get(f"{API_PREFIX}/projects/{project.id}/packages-summary", params=query)
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) <= limit
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return items
        assert cursor not in seen
        seen.add(cursor)


def _expected(session: Session, project: Project, sort: str, q: str | None) -> list[UUID]:
    rows = session.execute(
        select(Package, PackageRevision)
        .join(PackageRevision, PackageRevision.package_id == Package.id)
        .where(Package.project_id == project.id)
    ).all()
    labels = {"countertop": "countertop", "cabinet": "cabinets"}
    if q is not None:
        needle = q.lower()
        rows = [
            row
            for row in rows
            if (row.Package.vendor is not None and needle in row.Package.vendor.lower())
            or needle in labels.get(row.Package.product_type or "", "")
        ]
    readiness = approval_readiness_many(session, [row.PackageRevision.id for row in rows])

    def unnamed(row: Any) -> bool:
        return row.Package.vendor is None or not row.Package.vendor.strip()

    if sort == "updated":
        ordered = sorted(
            rows,
            key=lambda row: (row.PackageRevision.created_at, row.Package.id),
            reverse=True,
        )
    elif sort == "vendor":
        ordered = sorted(
            rows,
            key=lambda row: (
                unnamed(row),
                "" if unnamed(row) else row.Package.vendor.strip().lower(),
                row.Package.id,
            ),
        )
    else:
        ordered = sorted(
            rows,
            key=lambda row: (
                readiness[row.PackageRevision.id].blocking_findings,
                row.PackageRevision.created_at,
                row.Package.id,
            ),
            reverse=True,
        )
    return [row.Package.id for row in ordered]


@pytest.mark.parametrize("sort", ["updated", "vendor", "needs_decision"])
@pytest.mark.parametrize("q", [None, "BETA", "alpha", "stone", "cabinets", "zzz"])
@pytest.mark.parametrize("limit", [1, 3])
def test_every_sort_and_search_pages_without_repeats_or_skips(
    session: Session, sort: str, q: str | None, limit: int
) -> None:
    project = _seed(session)
    client = _client(session, project.id)
    params = {"sort": sort} if q is None else {"sort": sort, "q": q}
    walked = [UUID(item["package_id"]) for item in _walk(client, project, limit, **params)]
    assert walked == _expected(session, project, sort, q)
    assert len(walked) == len(set(walked))


def test_default_sort_is_updated_newest_first(session: Session) -> None:
    project = _seed(session)
    client = _client(session, project.id)
    walked = [UUID(item["package_id"]) for item in _walk(client, project, 2)]
    assert walked == _expected(session, project, "updated", None)


def test_vendor_sort_puts_unnamed_last(session: Session) -> None:
    project = _seed(session)
    items = _walk(_client(session, project.id), project, 50, sort="vendor")
    names = [item["vendor"] for item in items]
    assert names[-2:] in ([None, "  "], ["  ", None])
    assert [name.lower() for name in names[:-2]] == sorted(name.lower() for name in names[:-2])


def test_search_is_case_insensitive_and_project_scoped(session: Session) -> None:
    project = _seed(session)
    items = _walk(_client(session, project.id), project, 50, q="beta STONE")
    assert sorted(item["vendor"] for item in items) == ["Beta Stone", "beta stone"]


def test_search_treats_like_wildcards_as_text(session: Session) -> None:
    project = _seed(session)
    _package(session, project, vendor="100% Quartz_Co", minutes=8)
    session.commit()
    client = _client(session, project.id)
    assert [i["vendor"] for i in _walk(client, project, 50, q="%")] == ["100% Quartz_Co"]
    assert [i["vendor"] for i in _walk(client, project, 50, q="_")] == ["100% Quartz_Co"]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"sort": "vendor"}, {"sort": "updated"}),
        ({"sort": "vendor"}, {"sort": "needs_decision"}),
        ({"sort": "needs_decision"}, {}),
        ({"q": "beta"}, {"q": "alpha"}),
        ({"q": "beta"}, {}),
        ({}, {"q": "beta"}),
    ],
)
def test_a_cursor_reused_with_a_different_sort_or_search_is_refused(
    session: Session, first: dict[str, str], second: dict[str, str]
) -> None:
    project = _seed(session)
    client = _client(session, project.id)
    url = f"{API_PREFIX}/projects/{project.id}/packages-summary"
    page = client.get(url, params={"limit": "1", **first}).json()
    assert page["next_cursor"] is not None
    refused = client.get(url, params={"limit": "1", "cursor": page["next_cursor"], **second})
    assert refused.status_code == 422, refused.text
    assert "cursor" in refused.text.lower()


def test_an_unknown_sort_and_a_forged_cursor_are_refused(session: Session) -> None:
    project = _seed(session)
    client = _client(session, project.id)
    url = f"{API_PREFIX}/projects/{project.id}/packages-summary"
    assert client.get(url, params={"sort": "outcome"}).status_code == 422
    assert client.get(url, params={"cursor": "not-a-cursor"}).status_code == 422


def test_another_project_is_not_found_with_search_and_sort(session: Session) -> None:
    project = _seed(session)
    other = uuid4()
    response = _client(session, project.id).get(
        f"{API_PREFIX}/projects/{other}/packages-summary",
        params={"q": "beta", "sort": "needs_decision"},
    )
    assert response.status_code == 404


def test_needs_decision_by_outcome_adds_up_and_follows_readiness(session: Session) -> None:
    project = _seed(session)
    # A reviewer decision takes a finding out of every count, exactly as readiness says.
    revision = session.scalars(
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.project_id == project.id, Package.vendor == "Beta Stone")
    ).one()
    not_found = session.scalars(
        select(Finding).where(
            Finding.package_revision_id == revision.id, Finding.outcome == "NOT_FOUND"
        )
    ).one()
    review = open_session(session, package_revision_id=revision.id, reviewer="reviewer")
    record_action(
        session,
        review_session_id=review.id,
        finding_id=not_found.id,
        action=ReviewActionKind.DISMISS,
        actor="reviewer",
        note="Synthetic: not checkable",
    )
    session.commit()
    items = _walk(_client(session, project.id), project, 50)
    seen_outcomes: set[str] = set()
    for item in items:
        split = item["needs_decision_by_outcome"]
        assert sum(split.values()) == item["needs_decision"]
        readiness = approval_readiness(session, UUID(item["revision_id"]))
        assert item["needs_decision"] == readiness.blocking_findings
        outcomes = session.scalars(
            select(Finding.outcome).where(Finding.id.in_(readiness.blocking_finding_ids))
        ).all()
        assert split == {
            "fail": outcomes.count("FAIL"),
            "review": outcomes.count("REVIEW_REQUIRED"),
            "not_found": outcomes.count("NOT_FOUND"),
            "other": len(outcomes)
            - outcomes.count("FAIL")
            - outcomes.count("REVIEW_REQUIRED")
            - outcomes.count("NOT_FOUND"),
        }
        seen_outcomes.update(key for key, value in split.items() if value)
    assert {"fail", "review", "not_found"} <= seen_outcomes
    beta = next(item for item in items if item["revision_id"] == str(revision.id))
    assert beta["needs_decision_by_outcome"] == {
        "fail": 1,
        "review": 0,
        "not_found": 0,
        "other": 0,
    }


def test_a_corrected_pass_is_split_honestly_as_other(session: Session) -> None:
    """A `correct` blocks until a re-run whatever the recorded outcome; it is never called FAIL."""
    project = _project(session)
    package = _package(session, project, vendor="Synthetic", minutes=1, outcomes=("PASS",))
    revision = session.scalars(
        select(PackageRevision).where(PackageRevision.package_id == package.id)
    ).one()
    passed = session.scalars(
        select(Finding).where(Finding.package_revision_id == revision.id, Finding.outcome == "PASS")
    ).one()
    review = open_session(session, package_revision_id=revision.id, reviewer="reviewer")
    record_action(
        session,
        review_session_id=review.id,
        finding_id=passed.id,
        action=ReviewActionKind.CORRECT,
        actor="reviewer",
        note="Synthetic correction",
    )
    session.commit()
    (item,) = _walk(_client(session, project.id), project, 50)
    assert item["needs_decision"] == approval_readiness(session, revision.id).blocking_findings == 1
    assert item["needs_decision_by_outcome"] == {"fail": 0, "review": 0, "not_found": 0, "other": 1}


def test_reading_the_summary_changes_no_outcome_readiness_or_approval(session: Session) -> None:
    project = _seed(session)
    revisions = session.scalars(
        select(PackageRevision.id)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.project_id == project.id)
    ).all()

    def snapshot() -> tuple[object, ...]:
        return (
            sorted(
                (str(f.id), f.outcome)
                for f in session.scalars(
                    select(Finding).where(Finding.package_revision_id.in_(revisions))
                )
            ),
            {
                str(k): (v.can_approve, v.blocking_finding_ids)
                for k, v in approval_readiness_many(session, list(revisions)).items()
            },
            session.scalars(select(Approval.id)).all(),
            session.scalars(select(ReviewAction.id)).all(),
            sorted((str(r.id), r.state) for r in session.scalars(select(PackageRevision))),
        )

    before = snapshot()
    client = _client(session, project.id)
    for sort in ("updated", "vendor", "needs_decision"):
        _walk(client, project, 2, sort=sort, q="a")
    session.expire_all()
    assert snapshot() == before


def test_needs_decision_sort_keeps_a_bounded_number_of_queries(session: Session) -> None:
    from sqlalchemy import event

    project = _seed(session)
    for minutes in range(20):
        _package(
            session, project, vendor=f"Bulk {minutes}", minutes=10 + minutes, outcomes=("FAIL",)
        )
    session.commit()
    statements: list[str] = []

    def count(*args: object) -> None:
        statements.append(str(args[2]))

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        response = _client(session, project.id).get(
            f"{API_PREFIX}/projects/{project.id}/packages-summary",
            params={"sort": "needs_decision", "limit": "5"},
        )
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert response.status_code == 200, response.text
    assert len(statements) <= 12, statements
