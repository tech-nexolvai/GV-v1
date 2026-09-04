"""Querying findings (#222, D1.1): what the list must never quietly leave out.

Two claims carry this story, and both fail silently when they are wrong — which is why most of what
follows is failure paths rather than a happy path.

**The default list contains the abstentions.** A `REVIEW REQUIRED` missing from a list looks exactly
like a check that passed, and a package that looks clean gets signed off. So there is a test for the
default, and a separate one proving the outcome filter can actually narrow — a "default includes
everything" test passes just as well against an endpoint that ignores the filter entirely.

**Paging never skips a finding.** The interesting case is not paging a static table; it is paging one
that is being written to. The keyset test inserts a finding that sorts *inside* an already-served
page and asserts the reviewer still sees every original row exactly once.

The PostgreSQL tests skip without `DATABASE_URL` and run on CI. Everything above them runs anywhere.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.api import findings
from app.auth import Principal, Role, authenticate, require_project_access
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import (
    CheckRun,
    Finding,
    Package,
    PackageRevision,
    PackageState,
    Project,
    ReviewAction,
    ReviewSession,
    RuleDefinition,
    RuleSnapshot,
)
from app.schemas.findings import (
    ORDERING_DESCRIPTION,
    OUTCOME_ORDER,
    SEVERITY_ORDER,
    validate_sort_orders,
)
from tests.app.postgres_fixture import alembic_config
from verdict.outcomes import ABSTAINING_OUTCOMES, Outcome, Severity

pytest_plugins = ("tests.app.postgres_fixture",)

DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"
PROJECT_A = uuid4()
PROJECT_B = uuid4()

#: A fixed instant, so the tests control the tie-break rather than the clock.
EPOCH = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(database_url=DATABASE_URL)  # type: ignore[call-arg]


def _principal(*projects: UUID) -> Principal:
    return Principal(id="anant", roles=frozenset({Role.REVIEWER}), projects=frozenset(projects))


def _app(principal: Principal, session: Session | None = None) -> FastAPI:
    """The application factory plus this story's router.

    The router is included here rather than in `app/main.py`: several route groups are being written
    in parallel, and the factory is wired once, in one pass, when they land.
    """
    app = create_app(_settings())
    app.include_router(findings.router, prefix=API_PREFIX)
    app.dependency_overrides[authenticate] = lambda: principal
    app.dependency_overrides[findings.get_session] = lambda: session
    return app


def _url(project_id: UUID, package_id: UUID) -> str:
    return f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/findings"


# ---------------------------------------------------------------------------
# The sort key, which is what makes paging safe
# ---------------------------------------------------------------------------


def test_the_documented_order_ranks_every_outcome_and_severity() -> None:
    """An unranked value shares a bucket with every other unranked one, so the sort stops being
    total — and a keyset cursor over a non-total order skips and repeats rows."""
    validate_sort_orders(SEVERITY_ORDER, OUTCOME_ORDER)
    assert set(SEVERITY_ORDER) == set(Severity)
    assert set(OUTCOME_ORDER) == set(Outcome)


def test_the_order_guard_refuses_a_key_that_misses_a_value() -> None:
    """The test above asserts today's tuples are complete, so it would still pass with the guard
    deleted. This one watches the guard fail."""
    with pytest.raises(RuntimeError, match="does not rank every value"):
        validate_sort_orders((Severity.CRITICAL,), OUTCOME_ORDER)
    with pytest.raises(RuntimeError, match="does not rank every value"):
        validate_sort_orders(SEVERITY_ORDER, (Outcome.PASS,))


def test_the_order_guard_refuses_a_value_ranked_twice() -> None:
    """Two ranks for one value means its position depends on which comparison runs first."""
    with pytest.raises(RuntimeError, match="ranked twice"):
        validate_sort_orders((*SEVERITY_ORDER, Severity.CRITICAL), OUTCOME_ORDER)


def test_failures_rank_above_abstentions_and_abstentions_above_passes() -> None:
    """`DESIGN_PRODUCT.md` §3.2 applied to a list. A REVIEW REQUIRED under forty passes is a check
    nobody reads, which is the same outcome as not reporting it."""
    position = {outcome: rank for rank, outcome in enumerate(OUTCOME_ORDER)}
    assert position[Outcome.FAIL] == 0
    for abstention in ABSTAINING_OUTCOMES:
        assert position[abstention] < position[Outcome.PASS]


def test_critical_ranks_first_among_severities() -> None:
    assert SEVERITY_ORDER[0] is Severity.CRITICAL


# ---------------------------------------------------------------------------
# The cursor
# ---------------------------------------------------------------------------


def test_a_cursor_round_trips_exactly() -> None:
    """Every component of the sort key survives, to the microsecond. A truncated timestamp would
    make the boundary ambiguous, and an ambiguous boundary is a repeated or a skipped row."""
    original = findings.Cursor(
        severity=Severity.MAJOR,
        outcome=Outcome.REVIEW_REQUIRED,
        created_at=EPOCH + timedelta(microseconds=7),
        id=uuid4(),
    )
    assert findings.decode_cursor(findings.encode_cursor(original)) == original


def test_a_cursor_carries_no_padding_that_a_query_string_would_mangle() -> None:
    encoded = findings.encode_cursor(
        findings.Cursor(severity=Severity.MINOR, outcome=Outcome.PASS, created_at=EPOCH, id=uuid4())
    )
    assert "=" not in encoded


def _tampered(payload: dict[str, str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


# A fixed identifier, not `uuid4()`. Called inside `parametrize`, a fresh UUID is baked into the
# test id at collection, so the ids differ every run: a failure cannot be re-run by node id, and
# parallel workers each collect a different set and refuse to run at all. The value is irrelevant to
# what these cases assert — that a cursor we did not issue is refused — so it only has to be a
# well-formed UUID.
TAMPERED_CURSOR_ID = "852497a4-2931-44fd-9268-f0a6232b917f"


@pytest.mark.parametrize(
    "raw",
    [
        "not-base64-at-all!!",
        base64.urlsafe_b64encode(b"{not json").decode().rstrip("="),
        _tampered({"s": "CRITICAL"}),
        _tampered({"s": "SEVERE", "o": "FAIL", "t": EPOCH.isoformat(), "i": TAMPERED_CURSOR_ID}),
        _tampered({"s": "CRITICAL", "o": "CLEAN", "t": EPOCH.isoformat(), "i": TAMPERED_CURSOR_ID}),
        _tampered({"s": "CRITICAL", "o": "FAIL", "t": "the day before", "i": TAMPERED_CURSOR_ID}),
        _tampered({"s": "CRITICAL", "o": "FAIL", "t": EPOCH.isoformat(), "i": "not-a-uuid"}),
    ],
)
def test_a_cursor_we_did_not_issue_is_refused_rather_than_reset(raw: str) -> None:
    """Falling back to page one would turn a client bug into an endless loop over the first page,
    with every request succeeding and the data looking plausible."""
    with pytest.raises(HTTPException) as refusal:
        findings.decode_cursor(raw)
    assert refusal.value.status_code == 422


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def _walk(dependant: Any) -> list[Any]:
    found: list[Any] = []
    for dependency in dependant.dependencies:
        if dependency.call is not None:
            found.append(dependency.call)
        found.extend(_walk(dependency))
    return found


def test_the_route_carries_the_shared_project_dependency() -> None:
    """The project boundary, asserted on this router directly.

    Read off `findings.router.routes` rather than off a built application on purpose. Current
    FastAPI does not flatten an included router into `app.routes`; it appends one opaque
    `_IncludedRouter` and resolves the children at match time. So an enumeration that walks
    `app.routes` — which is what `tests/api/test_authorisation.py` does — cannot see a route added
    by `include_router` at all, and would report this one as neither guarded nor unguarded. That is
    flagged for the wiring pass; meanwhile the check is made here, where it can actually fail.
    """
    route = next(
        r
        for r in findings.router.routes
        if isinstance(r, APIRoute) and r.path.endswith("/findings")
    )
    assert "{project_id}" in route.path
    assert require_project_access in _walk(route.dependant)


def test_a_caller_outside_the_project_is_told_it_does_not_exist() -> None:
    """404, never 403. A 403 confirms the project exists, which is what the boundary is for."""
    client = TestClient(_app(_principal(PROJECT_A)), raise_server_exceptions=False)
    assert client.get(_url(PROJECT_B, uuid4())).status_code == 404


def test_a_refusal_names_neither_the_project_nor_the_reason() -> None:
    """A message that explained itself would hand back exactly what the 404 was chosen to hide."""
    client = TestClient(_app(_principal(PROJECT_A)), raise_server_exceptions=False)
    response = client.get(_url(PROJECT_B, uuid4()))
    assert str(PROJECT_B) not in response.text
    assert "forbidden" not in response.text.lower()
    assert "reviewer" not in response.text.lower()
    assert "project" not in response.json()["message"].lower()


def test_the_refusal_uses_the_shared_error_envelope() -> None:
    """One error shape for every failure — a client's error handling is written once."""
    client = TestClient(_app(_principal(PROJECT_A)), raise_server_exceptions=False)
    body = client.get(_url(PROJECT_B, uuid4())).json()
    assert set(body) == {"error", "message", "request_id"}


# ---------------------------------------------------------------------------
# Bad arguments, refused before anything is read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        {"outcome": "MAYBE"},
        {"outcome": "pass"},  # the stored vocabulary is upper case; a near-miss is still a miss
        {"severity": "URGENT"},
        {"limit": "0"},
        {"limit": str(findings.MAX_PAGE_SIZE + 1)},
        {"limit": "all"},
    ],
)
def test_a_filter_value_outside_the_vocabulary_is_refused(query: dict[str, str]) -> None:
    """Refused rather than ignored. A misspelt filter that silently matched everything would show a
    reviewer more than they asked for; one that silently matched nothing would show them less, and
    an empty list is the dangerous direction."""
    client = TestClient(_app(_principal(PROJECT_A)), raise_server_exceptions=False)
    response = client.get(_url(PROJECT_A, uuid4()), params=query)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


def test_a_malformed_cursor_is_refused_without_reading_anything() -> None:
    """No session is wired into this app, so touching the database would raise instead of answering
    — which is the point: an argument we can reject on sight costs no round trip."""
    client = TestClient(_app(_principal(PROJECT_A)), raise_server_exceptions=False)
    assert client.get(_url(PROJECT_A, uuid4()), params={"cursor": "nonsense!!"}).status_code == 422


def test_the_submitted_filter_values_are_not_echoed_back() -> None:
    """`AGENTS.md` §6 keeps drawing content out of anything logged or forwarded, and a rejected
    request may carry a value read off a client's drawing."""
    client = TestClient(_app(_principal(PROJECT_A)), raise_server_exceptions=False)
    response = client.get(_url(PROJECT_A, uuid4()), params={"outcome": "SECRET-VENDOR-CODE"})
    assert "SECRET-VENDOR-CODE" not in response.text


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    """A migrated, throwaway schema and one session over it.

    The same session is handed to the endpoint, so a row written here is visible to the request
    without a commit — the tests are about ordering and paging, not about transaction visibility.
    """
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _project(session: Session, project_id: UUID) -> Project:
    project = Project(id=project_id, name=f"GV project {project_id}")
    session.add(project)
    session.flush()
    return project


def _package(session: Session, project_id: UUID) -> Package:
    package = Package(project_id=project_id, vendor=None)
    session.add(package)
    session.flush()
    return package


def _revision(session: Session, package_id: UUID, number: int = 1) -> PackageRevision:
    revision = PackageRevision(
        package_id=package_id, revision_number=number, state=PackageState.RUNNING_CHECKS
    )
    session.add(revision)
    session.flush()
    return revision


def _snapshot(
    session: Session, *, check_type: str = "internal", product_type: str = "countertop"
) -> RuleSnapshot:
    definition = RuleDefinition(rule_id=f"CT-WIDTH-{uuid4().hex[:8]}")
    session.add(definition)
    session.flush()
    canonical = json.dumps({"id": definition.rule_id, "version": "1.0.0"}, separators=(",", ":"))
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=canonical,
        product_type=product_type,
        check_type=check_type,
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _finding(
    session: Session,
    revision: PackageRevision,
    snapshot: RuleSnapshot,
    outcome: Outcome,
    severity: Severity = Severity.CRITICAL,
    *,
    created_at: datetime | None = None,
) -> Finding:
    """One finding and the check run it came from.

    A run per finding, because `findings.check_run_id` is unique — one finding per run, or "what did
    this check decide?" would have two answers.
    """
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version="verdict-1.2.3",
    )
    session.add(run)
    session.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=outcome.value,
        severity=severity.value,
        trace={},
        parameter_set_versions={"countertop": "3"},
        created_at=created_at if created_at is not None else EPOCH,
    )
    session.add(finding)
    session.flush()
    return finding


def _client(session: Session, *projects: UUID) -> TestClient:
    return TestClient(_app(_principal(*projects), session=session), raise_server_exceptions=False)


def _outcomes(body: dict[str, Any]) -> list[str]:
    return [item["outcome"] for item in body["items"]]


def _page(client: TestClient, project_id: UUID, package_id: UUID, **params: Any) -> dict[str, Any]:
    response = client.get(_url(project_id, package_id), params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_the_default_list_includes_every_abstention(session: Session) -> None:
    """**The finding this story exists to prevent.** A default that omitted REVIEW REQUIRED would
    let a package look clean because the reviewer never saw what the system declined to judge."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    snapshot = _snapshot(session)
    for offset, outcome in enumerate(Outcome):
        _finding(session, revision, snapshot, outcome, created_at=EPOCH + timedelta(minutes=offset))

    body = _page(_client(session, PROJECT_A), PROJECT_A, package.id)
    assert set(_outcomes(body)) == {outcome.value for outcome in Outcome}


def test_the_outcome_filter_can_actually_narrow(session: Session) -> None:
    """The test above passes just as well against an endpoint that ignores the filter. This one
    fails unless the filter is wired, which is what makes the pair meaningful."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    snapshot = _snapshot(session)
    for offset, outcome in enumerate(Outcome):
        _finding(session, revision, snapshot, outcome, created_at=EPOCH + timedelta(minutes=offset))

    body = _page(
        _client(session, PROJECT_A),
        PROJECT_A,
        package.id,
        outcome=[Outcome.REVIEW_REQUIRED.value, Outcome.NOT_FOUND.value],
    )
    assert sorted(_outcomes(body)) == sorted(
        [Outcome.NOT_FOUND.value, Outcome.REVIEW_REQUIRED.value]
    )


def test_critical_failures_come_first(session: Session) -> None:
    """The acceptance criterion. Written oldest-worst-last on purpose, so an endpoint that returned
    insertion order would fail."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    snapshot = _snapshot(session)
    written = [
        (Outcome.PASS, Severity.ADVISORY),
        (Outcome.PASS, Severity.CRITICAL),
        (Outcome.REVIEW_REQUIRED, Severity.MAJOR),
        (Outcome.FAIL, Severity.MINOR),
        (Outcome.REVIEW_REQUIRED, Severity.CRITICAL),
        (Outcome.FAIL, Severity.CRITICAL),
    ]
    for offset, (outcome, severity) in enumerate(written):
        _finding(
            session,
            revision,
            snapshot,
            outcome,
            severity,
            created_at=EPOCH + timedelta(minutes=offset),
        )

    body = _page(_client(session, PROJECT_A), PROJECT_A, package.id)
    returned = [(item["severity"], item["outcome"]) for item in body["items"]]
    assert returned == [
        (Severity.CRITICAL.value, Outcome.FAIL.value),
        (Severity.CRITICAL.value, Outcome.REVIEW_REQUIRED.value),
        (Severity.CRITICAL.value, Outcome.PASS.value),
        (Severity.MAJOR.value, Outcome.REVIEW_REQUIRED.value),
        (Severity.MINOR.value, Outcome.FAIL.value),
        (Severity.ADVISORY.value, Outcome.PASS.value),
    ]


def test_a_critical_pass_never_outranks_a_critical_failure(session: Session) -> None:
    """Severity alone does not deliver "critical failures first": a critical PASS and a critical
    FAIL tie on severity, and the tie-break would be whichever was written first."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    snapshot = _snapshot(session)
    _finding(session, revision, snapshot, Outcome.PASS, Severity.CRITICAL, created_at=EPOCH)
    _finding(
        session,
        revision,
        snapshot,
        Outcome.FAIL,
        Severity.CRITICAL,
        created_at=EPOCH + timedelta(hours=1),
    )

    body = _page(_client(session, PROJECT_A), PROJECT_A, package.id)
    assert _outcomes(body)[0] == Outcome.FAIL.value


def test_the_ordering_is_described_in_every_response(session: Session) -> None:
    """The acceptance asks for the ordering to be documented. An ordering a client has to infer is
    one they will infer wrongly."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    body = _page(_client(session, PROJECT_A), PROJECT_A, package.id)
    assert body["ordering"] == ORDERING_DESCRIPTION


def test_paging_returns_every_finding_exactly_once(session: Session) -> None:
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    snapshot = _snapshot(session)
    written = [
        _finding(
            session,
            revision,
            snapshot,
            Outcome.FAIL if index % 2 else Outcome.REVIEW_REQUIRED,
            SEVERITY_ORDER[index % len(SEVERITY_ORDER)],
            created_at=EPOCH + timedelta(minutes=index),
        ).id
        for index in range(11)
    ]

    client = _client(session, PROJECT_A)
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # a bound, so a broken cursor loops forever in the test rather than in CI
        body = _page(
            client, PROJECT_A, package.id, limit=3, **({"cursor": cursor} if cursor else {})
        )
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert cursor is None, "paging did not terminate"
    assert len(seen) == len(set(seen)), "a finding was returned on two pages"
    assert set(seen) == {str(identifier) for identifier in written}


def test_the_last_page_carries_no_cursor(session: Session) -> None:
    """A page shorter than `limit` is not the signal — `next_cursor` is. Issuing a cursor that leads
    to an empty page makes every client walk one extra round trip to find the end."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    snapshot = _snapshot(session)
    for index in range(3):
        _finding(
            session,
            revision,
            snapshot,
            Outcome.FAIL,
            created_at=EPOCH + timedelta(minutes=index),
        )

    body = _page(_client(session, PROJECT_A), PROJECT_A, package.id, limit=3)
    assert len(body["items"]) == 3
    assert body["next_cursor"] is None


def test_a_finding_written_mid_paging_never_hides_an_existing_one(session: Session) -> None:
    """**The acceptance criterion, and the reason paging is keyset rather than offset.**

    The new finding sorts *inside* the page already served. Under `OFFSET`, every later row shifts
    down by one and the row on the boundary is never returned — the reviewer's list is short by one
    and nothing says so.
    """
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    snapshot = _snapshot(session)
    original = [
        _finding(
            session,
            revision,
            snapshot,
            Outcome.FAIL,
            Severity.CRITICAL,
            created_at=EPOCH + timedelta(minutes=index),
        ).id
        for index in range(6)
    ]

    client = _client(session, PROJECT_A)
    first = _page(client, PROJECT_A, package.id, limit=3)
    seen = [item["id"] for item in first["items"]]

    # Written between the requests, and sorting into the middle of the page just served.
    _finding(
        session,
        revision,
        snapshot,
        Outcome.FAIL,
        Severity.CRITICAL,
        created_at=EPOCH + timedelta(seconds=30),
    )

    cursor = first["next_cursor"]
    while cursor is not None:
        body = _page(client, PROJECT_A, package.id, limit=3, cursor=cursor)
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]

    assert len(seen) == len(set(seen))
    assert {str(identifier) for identifier in original} <= set(seen)


def test_a_package_in_another_project_is_not_found(session: Session) -> None:
    """The caller belongs to project A and names project A in the path, but the package is B's.
    Answering with B's findings would make the path segment decorative."""
    _project(session, PROJECT_A)
    _project(session, PROJECT_B)
    other = _package(session, PROJECT_B)
    revision = _revision(session, other.id)
    _finding(session, revision, _snapshot(session), Outcome.FAIL)

    response = _client(session, PROJECT_A).get(_url(PROJECT_A, other.id))
    assert response.status_code == 404
    assert str(other.id) not in response.text


def test_a_package_that_does_not_exist_is_refused_in_the_same_words(session: Session) -> None:
    """ "No such package" and "not your package" must be indistinguishable, or the difference between
    them is the disclosure the 404 was chosen to avoid."""
    _project(session, PROJECT_A)
    _project(session, PROJECT_B)
    other = _package(session, PROJECT_B)
    client = _client(session, PROJECT_A)

    absent = client.get(_url(PROJECT_A, uuid4()))
    foreign = client.get(_url(PROJECT_A, other.id))
    assert absent.status_code == foreign.status_code == 404
    assert absent.json()["message"] == foreign.json()["message"]


def test_a_package_with_no_findings_is_an_empty_page_not_an_error(session: Session) -> None:
    """Nothing to report is a legitimate answer, and a 404 here would be indistinguishable from the
    refusal above — a client could not tell "checks have not run yet" from "not yours"."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)

    body = _page(_client(session, PROJECT_A), PROJECT_A, package.id)
    assert body["items"] == []
    assert body["next_cursor"] is None


def test_a_filter_that_matches_nothing_is_an_empty_page(session: Session) -> None:
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    _finding(session, revision, _snapshot(session), Outcome.FAIL, Severity.CRITICAL)

    body = _page(
        _client(session, PROJECT_A),
        PROJECT_A,
        package.id,
        severity=[Severity.ADVISORY.value],
    )
    assert body["items"] == []


def test_findings_from_another_package_never_appear(session: Session) -> None:
    """The `WHERE` clause, not just the dependency. A membership check says which project; it does
    not say which package's rows the query returned."""
    _project(session, PROJECT_A)
    mine = _package(session, PROJECT_A)
    theirs = _package(session, PROJECT_A)
    snapshot = _snapshot(session)
    kept = _finding(session, _revision(session, mine.id), snapshot, Outcome.FAIL)
    _finding(session, _revision(session, theirs.id), snapshot, Outcome.FAIL)

    body = _page(_client(session, PROJECT_A), PROJECT_A, mine.id)
    assert [item["id"] for item in body["items"]] == [str(kept.id)]


def test_the_check_type_and_product_type_filters_narrow(session: Session) -> None:
    """Both live on the rule snapshot, so this also proves the join reaches it."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    internal = _snapshot(session, check_type="internal", product_type="countertop")
    against = _snapshot(session, check_type="against_approved", product_type="cabinet")
    kept = _finding(session, revision, internal, Outcome.FAIL)
    _finding(session, revision, against, Outcome.FAIL, created_at=EPOCH + timedelta(minutes=1))

    client = _client(session, PROJECT_A)
    by_check = _page(client, PROJECT_A, package.id, check_type=["internal"])
    assert [item["id"] for item in by_check["items"]] == [str(kept.id)]

    by_product = _page(client, PROJECT_A, package.id, product_type=["cabinet"])
    assert [item["check_type"] for item in by_product["items"]] == ["against_approved"]


def test_a_finding_comes_back_with_the_versions_that_explain_it(session: Session) -> None:
    """`AGENTS.md` §2.7. A finding that cannot be attributed to the versions that produced it is an
    assertion rather than a record — and the snapshot *hash* is the part that survives a mistaken
    republish, which a version string alone does not."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id, number=4)
    snapshot = _snapshot(session)
    _finding(session, revision, snapshot, Outcome.FAIL)

    item = _page(_client(session, PROJECT_A), PROJECT_A, package.id)["items"][0]
    assert item["rule_snapshot_hash"] == snapshot.snapshot_id
    assert item["rule_snapshot_id"] == str(snapshot.id)
    assert item["rule_version"] == snapshot.version
    assert item["engine_version"] == "verdict-1.2.3"
    assert item["parameter_set_versions"] == {"countertop": "3"}
    assert item["revision_number"] == 4
    assert item["package_revision_id"] == str(revision.id)


def test_the_list_does_not_carry_calculation_traces(session: Session) -> None:
    """Reconstructing a finding from its own operands is a per-finding request
    (`DESIGN_PRODUCT.md` §3.1). A list carrying every trace would be enormous and would still not be
    the recompute that section asks for."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    _finding(session, revision, _snapshot(session), Outcome.FAIL)

    item = _page(_client(session, PROJECT_A), PROJECT_A, package.id)["items"][0]
    assert "trace" not in item


# ---------------------------------------------------------------------------
# The summary — what a package list shows before anyone opens a finding
# ---------------------------------------------------------------------------


def _summary(client: TestClient, project_id: UUID, package_id: UUID) -> dict[str, Any]:
    response = client.get(f"{_url(project_id, package_id)}/summary")
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_every_outcome_is_counted_and_they_sum_to_the_total(session: Session) -> None:
    """**Abstentions are counted, not left as a remainder.**

    A summary reporting only passes and failures would leave `REVIEW_REQUIRED`, `NOT_FOUND` and
    `NO_APPLICABLE_RULE` in neither column, and a reader would take what is left for passes. Under
    V1's exact-match rule those abstentions are the expected bulk of a run.
    """
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    snapshot = _snapshot(session)
    for offset, outcome in enumerate(Outcome):
        _finding(session, revision, snapshot, outcome, created_at=EPOCH + timedelta(minutes=offset))

    body = _summary(_client(session, PROJECT_A), PROJECT_A, package.id)

    assert body["total"] == len(list(Outcome))
    assert body["passed"] == 1
    assert body["failed"] == 1
    assert body["review_required"] == 1
    assert body["not_found"] == 1
    assert body["no_applicable_rule"] == 1
    counted = (
        body["passed"]
        + body["failed"]
        + body["review_required"]
        + body["not_found"]
        + body["no_applicable_rule"]
    )
    assert (
        counted == body["total"]
    ), "the parts do not add up to the whole, so something is uncounted"


def test_critical_failures_are_counted_separately(session: Session) -> None:
    """The primary safety metric counts these, so the reviewer's list leads with them rather than
    leaving them to be spotted among the rest."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    snapshot = _snapshot(session)
    _finding(session, revision, snapshot, Outcome.FAIL, Severity.CRITICAL, created_at=EPOCH)
    _finding(
        session,
        revision,
        snapshot,
        Outcome.FAIL,
        Severity.ADVISORY,
        created_at=EPOCH + timedelta(minutes=1),
    )
    _finding(
        session,
        revision,
        snapshot,
        Outcome.PASS,
        Severity.CRITICAL,
        created_at=EPOCH + timedelta(minutes=2),
    )

    body = _summary(_client(session, PROJECT_A), PROJECT_A, package.id)

    assert body["failed"] == 2
    assert body["critical_failed"] == 1, "an advisory failure is not a critical one"


def test_a_package_with_no_findings_summarises_to_zero(session: Session) -> None:
    """Zero is a real answer here and must not be an error — a package whose checks have not run yet
    is an ordinary state, and the list still has to render it."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    _revision(session, package.id)

    body = _summary(_client(session, PROJECT_A), PROJECT_A, package.id)
    assert body["total"] == 0 and body["critical_failed"] == 0


def test_another_projects_package_does_not_summarise(session: Session) -> None:
    """404, not an empty summary. An empty summary would confirm the package exists, which is what
    the boundary is for."""
    _project(session, PROJECT_A)
    _project(session, PROJECT_B)
    package = _package(session, PROJECT_B)
    revision = _revision(session, package.id)
    _finding(session, revision, _snapshot(session), Outcome.FAIL, created_at=EPOCH)

    response = _client(session, PROJECT_A).get(f"{_url(PROJECT_A, package.id)}/summary")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Reading a reviewer's decisions back (#472)
# ---------------------------------------------------------------------------


def _session_and_action(
    session: Session,
    revision: PackageRevision,
    finding: Finding,
    action: str,
    *,
    actor: str = "anant",
    at: datetime | None = None,
    note: str | None = None,
    action_id: UUID | None = None,
) -> ReviewAction:
    """One sitting and one thing done in it.

    `action_id` is settable so a test can make insertion order and id order disagree, which is the
    only way to pin a tie-break deterministically.
    """
    sitting = ReviewSession(package_revision_id=revision.id, reviewer=actor)
    session.add(sitting)
    session.flush()
    recorded = ReviewAction(
        **({"id": action_id} if action_id is not None else {}),
        review_session_id=sitting.id,
        finding_id=finding.id,
        package_revision_id=revision.id,
        action=action,
        actor=actor,
        note=note,
        created_at=at if at is not None else EPOCH,
    )
    session.add(recorded)
    session.flush()
    return recorded


def test_a_finding_nobody_has_touched_reports_no_action(session: Session) -> None:
    """`None`, and it must stay reachable — an inner join would have dropped every untouched finding
    and shown a reviewer only the work already done."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    _finding(session, revision, _snapshot(session), Outcome.FAIL)

    body = _page(_client(session, PROJECT_A), PROJECT_A, package.id)

    assert len(body["items"]) == 1
    assert body["items"][0]["reviewer_action"] is None


def test_a_recorded_decision_comes_back_with_the_finding(session: Session) -> None:
    """**The defect this closes.**

    The action was written to the ledger and never returned, so the workspace showed every finding as
    untouched after a refresh — and a reviewer could record the same decision twice because the first
    was invisible. That happened in testing, on this exact path.
    """
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    finding = _finding(session, revision, _snapshot(session), Outcome.FAIL)
    _session_and_action(session, revision, finding, "confirm", note="matches the cut sheet")

    body = _page(_client(session, PROJECT_A), PROJECT_A, package.id)
    recorded = body["items"][0]["reviewer_action"]

    assert recorded["action"] == "confirm"
    assert recorded["actor"] == "anant"
    assert recorded["note"] == "matches the cut sheet"
    assert recorded["at"] is not None


def test_a_changed_mind_reports_the_later_decision(session: Session) -> None:
    """The ledger is append-only, so a reviewer who changes their mind leaves two rows. The screen
    must show the second — reporting the first would tell them their correction did not take."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    finding = _finding(session, revision, _snapshot(session), Outcome.FAIL)
    _session_and_action(session, revision, finding, "confirm", at=EPOCH)
    _session_and_action(session, revision, finding, "dismiss", at=EPOCH + timedelta(minutes=5))

    body = _page(_client(session, PROJECT_A), PROJECT_A, package.id)

    assert body["items"][0]["reviewer_action"]["action"] == "dismiss"


def test_a_colleagues_decision_is_shown_with_their_name(session: Session) -> None:
    """A finding's disposition belongs to the package, not to whoever is looking.

    Showing somebody else's confirmation as outstanding would invite a second opinion recorded as a
    first. `actor` is what lets the screen say it was not you.
    """
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    finding = _finding(session, revision, _snapshot(session), Outcome.FAIL)
    _session_and_action(session, revision, finding, "except", actor="priya")

    body = _page(_client(session, PROJECT_A), PROJECT_A, package.id)
    recorded = body["items"][0]["reviewer_action"]

    assert recorded["action"] == "except"
    assert recorded["actor"] == "priya"


def test_an_action_on_one_finding_does_not_attach_to_another(session: Session) -> None:
    """Two findings, one actioned. The join must not smear it across the package — a reviewer would
    see work they never did and sign off on it."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    snapshot = _snapshot(session)
    first = _finding(session, revision, snapshot, Outcome.FAIL)
    _finding(session, revision, snapshot, Outcome.REVIEW_REQUIRED)
    _session_and_action(session, revision, first, "confirm")

    body = _page(_client(session, PROJECT_A), PROJECT_A, package.id)
    by_id = {item["id"]: item["reviewer_action"] for item in body["items"]}

    assert by_id[str(first.id)]["action"] == "confirm"
    assert [a for a in by_id.values() if a is None], "the untouched finding lost its None"
    assert sum(1 for a in by_id.values() if a is not None) == 1


def test_the_action_does_not_multiply_the_findings(session: Session) -> None:
    """A join returning several action rows per finding would duplicate the finding itself.

    Three actions on one finding, and the list must still hold one row — otherwise a package with a
    much-revised finding reports more work than exists, and paging walks the same finding repeatedly.
    """
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    finding = _finding(session, revision, _snapshot(session), Outcome.FAIL)
    for minute, verb in enumerate(("confirm", "dismiss", "confirm")):
        _session_and_action(session, revision, finding, verb, at=EPOCH + timedelta(minutes=minute))

    body = _page(_client(session, PROJECT_A), PROJECT_A, package.id)

    assert len(body["items"]) == 1
    assert body["items"][0]["reviewer_action"]["action"] == "confirm"


# ---------------------------------------------------------------------------
# Superseded runs (#477)
# ---------------------------------------------------------------------------


def test_a_superseded_run_disappears_from_the_list(session: Session) -> None:
    """**The reviewer must see one answer per check, and the endpoint is where that is decided.**

    Findings are immutable, so re-running the checks adds a second set rather than replacing the
    first. Both stay in the table — that is the audit trail — but a list showing both would put a
    PASS and a FAIL for the same check in front of a reviewer with equal standing.

    Asserted through the endpoint rather than against a hand-written query. A test that did its own
    join would pass while the endpoint returned everything, which is exactly what happened: the first
    version of this change was mutation-tested by deleting the filter from `_base_query`, and every
    test still passed because they all filtered for themselves.
    """
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    snapshot = _snapshot(session)

    stale = _finding(session, revision, snapshot, Outcome.PASS)
    session.get(CheckRun, stale.check_run_id).superseded_at = datetime(2026, 9, 1, tzinfo=UTC)
    _finding(session, revision, snapshot, Outcome.FAIL)
    session.flush()

    body = _page(_client(session, PROJECT_A), PROJECT_A, package.id)

    assert _outcomes(body) == [
        "FAIL"
    ], "the superseded PASS is still listed; a reviewer would see two answers for one check"


def test_the_summary_counts_only_live_findings(session: Session) -> None:
    """The counts feed the packages table and the usage page. Counting superseded runs would report
    twice the work and inflate the failure count on every re-run."""
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    snapshot = _snapshot(session)

    stale = _finding(session, revision, snapshot, Outcome.FAIL)
    session.get(CheckRun, stale.check_run_id).superseded_at = datetime(2026, 9, 1, tzinfo=UTC)
    _finding(session, revision, snapshot, Outcome.PASS)
    session.flush()

    client = _client(session, PROJECT_A)
    response = client.get(f"{_url(PROJECT_A, package.id)}/summary")

    assert response.status_code == 200, response.text
    counts = response.json()
    assert counts["total"] == 1
    assert counts["failed"] == 0
    assert counts["passed"] == 1


def test_two_actions_in_the_same_instant_resolve_by_id(session: Session) -> None:
    """**The tie-break I documented and did not test.**

    `_latest_action` orders by `created_at` then `id`, and the second half is the whole point: two
    actions written in one transaction share a timestamp, and without a total order the finding reads
    `confirm` on one request and `dismiss` on the next. Nothing about that looks like a bug — it looks
    like a reviewer's own decision being flaky.

    **The ids are fixed, and the arrangement was measured rather than guessed.** With random uuids a
    mutant that deleted the tie-break passed four runs in five, and the first fixed arrangement I
    tried let it pass five in five — the broken order happened to agree with the right answer. So the
    two arrangements were run against the mutant to find the one where they differ: with the *later*
    decision carrying the higher id, the ordering that ignores id returns the earlier one.

    Raised by review, which is where it should have been caught: the docstring already claimed the
    property.
    """
    _project(session, PROJECT_A)
    package = _package(session, PROJECT_A)
    revision = _revision(session, package.id)
    finding = _finding(session, revision, _snapshot(session), Outcome.FAIL)

    instant = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    low = UUID("00000000-0000-4000-8000-000000000000")
    high = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
    _session_and_action(session, revision, finding, "confirm", at=instant, action_id=low)
    _session_and_action(session, revision, finding, "dismiss", at=instant, action_id=high)

    body = _page(_client(session, PROJECT_A), PROJECT_A, package.id)

    assert body["items"][0]["reviewer_action"]["action"] == "dismiss", (
        "the tie was broken by something other than id, so which decision a reviewer sees depends on "
        "the query plan"
    )
