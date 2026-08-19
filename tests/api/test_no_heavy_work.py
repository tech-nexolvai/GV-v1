"""The control plane does no heavy work, and the import walk that keeps it that way (#208, C2.6).

Backend §4.1, via `docs/DESIGN_PLATFORM.md` §4.2: anything CPU-heavy is a background task. On one 8 GB
VM, rendering inside a request competes with PostgreSQL and OCR for the same memory — so an endpoint
that merely *accepts* a drawing must not be able to reach the code that reads it. A route module can
acquire that reach without anybody deciding to: one convenience import three modules away is enough.

So this walks it. Every module under `app/api/`, transitively, and it fails if any of them can reach
OCR, rendering, vision, model or extraction code. Same idea as `tests/test_verdict_isolation.py` and
the same idea as the two route-enumerating guards before it: the boundary holds because something
checks the whole surface, not because each author remembered.

**Why the walker here is module-level rather than reusing the package-level one.**
`tests/test_verdict_isolation.py` resolves imports to their top-level package, so `from app.models
import Package` records `app` and the walk then covers everything under `app/` — including
`app/runs/invocations.py`, which imports `extraction` perfectly legitimately. At that granularity this
guard could not tell `app/api/` from `app/runs/`, so it would have been either vacuous or wrong about
code doing nothing wrong. This resolves the dotted module, and shares the vocabulary rather than the
function. Folding the two walkers together is worth doing and is flagged on the PR, not done here.

**What the walk found, which is half of why this story mattered.** `app/api/` could reach
`retrieval.candidate` — through `app.models` → `app.models.matching`, which imported `Lane` from it.
That also broke `DESIGN_PLATFORM.md` §2, whose table says `app/models/` must never import
`retrieval/`. `Lane` now lives in `vocabulary/lanes.py`, so the guard needs no exemption at all.

Source: backend proposal §4.1, §9.2 · Design: `docs/DESIGN_PLATFORM.md` §4.2, §2 ·
Verification: this file
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.api import background
from app.api.background import (
    ACCEPTED_WORK_PATH,
    STATUS_DESCRIPTIONS,
    AcceptedOut,
    PayloadMissingProject,
    WorkStatus,
    enqueue_and_respond,
    status_url_for,
)
from app.auth import Principal, Role, authenticate, require_project_access
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import OutboxEntry
from vocabulary.lanes import Lane
from workflow.outbox import enqueue

pytest_plugins = ("tests.app.postgres_fixture",)

REPO_ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"
PROJECT_A = uuid4()
PROJECT_B = uuid4()
WORKFLOW = "ingest_document"

#: Every top-level package in this repo, so the walk knows what to descend into.
PROJECT_PACKAGES = {
    "app",
    "eval",
    "evidence",
    "extraction",
    "reports",
    "retrieval",
    "rules",
    "units",
    "verdict",
    "vocabulary",
    "workflow",
}

#: Our own packages the control plane must not be able to reach. `DESIGN_PLATFORM.md` §2, the
#: `app/api/` row: never `extraction/`, never `retrieval/`, never OCR or rendering.
#:
#: `reports/` is here for the same reason as rendering — it draws redlines with reportlab, which is
#: exactly the "rendering inside a request" §4.2 names.
FORBIDDEN_PACKAGES = frozenset({"extraction", "retrieval", "reports"})

#: Third-party libraries whose *presence in the process* is the cost. Not an exhaustive list of every
#: heavy library — the specific capabilities §4.2 says belong in a background task.
FORBIDDEN_LIBRARIES = frozenset(
    {
        # OCR and vision
        "paddleocr",
        "doctr",
        "cv2",
        "pytesseract",
        # PDF reading and rasterising
        "pdfplumber",
        "pypdfium2",
        "pikepdf",
        # rendering
        "reportlab",
        "pypdf",
        # models
        "torch",
        "transformers",
        "sentence_transformers",
        "onnxruntime",
    }
)

#: Reached, allowed, and written down rather than left to be rediscovered.
#:
#: `shapely` is a GEOS C extension and a base dependency, and the control plane loads it through
#: `app.models.evidence` → `evidence.canonical` → `evidence.polygon`. That path is design-legal — §2
#: permits `app/models/` → `evidence/` — and reading evidence back needs the polygon types. It is not
#: free, and it is the one thing here worth revisiting if the control plane's memory ever becomes the
#: binding constraint; it is nothing like the cost of loading an OCR engine, which is what this guard
#: is for. Anant's call, recorded on the #208 PR.
TOLERATED_LIBRARIES = frozenset({"shapely"})


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


def _module_name(path: Path, root: Path) -> str:
    """Dotted name for a file, so `app/api/documents.py` is `app.api.documents`."""
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _file_for(module: str, root: Path) -> Path | None:
    """The file a dotted module name refers to, package `__init__` included."""
    base = root / Path(*module.split("."))
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    candidate = base.with_suffix(".py")
    return candidate if candidate.exists() else None


def _imports_in(path: Path) -> set[str]:
    """Dotted module names one file imports.

    `from app.models import Package` yields both `app.models` and `app.models.Package`; the second is
    a class rather than a module, and `_file_for` simply finds no file for it. Emitting both is what
    makes `from x.y import z` work whether `z` is a submodule or a name inside one — resolving that
    properly would mean importing the package, and importing it is the thing this test must not do.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
    return found


def reachable_from(package_dir: str, root: Path = REPO_ROOT) -> dict[str, list[str]]:
    """Every module reachable from the modules in one directory, with the chain that reached it.

    Breadth-first so a reported chain is a shortest one — the most useful thing to hand somebody who
    has to break it. The chain is the whole value of the failure message: "app/api reaches cv2" sends
    an author looking through seven files, while naming the hop that did it is a fix.

    `root` is a parameter rather than the module constant so the failure tests below can walk a fake
    tree in `tmp_path`. Planting a real offending module inside `app/api/` would leave the repository
    failing its own guard if the test died before cleaning up.
    """
    start = [_module_name(f, root) for f in sorted((root / package_dir).rglob("*.py"))]
    chains: dict[str, list[str]] = {}
    queue: list[tuple[str, list[str]]] = [(module, [module]) for module in start]
    seen: set[str] = set()

    while queue:
        module, chain = queue.pop(0)
        if module in seen:
            continue
        seen.add(module)
        file = _file_for(module, root)
        if file is None:
            continue
        for imported in sorted(_imports_in(file)):
            chains.setdefault(imported, [*chain, imported])
            if imported.split(".")[0] in PROJECT_PACKAGES and imported not in seen:
                queue.append((imported, [*chain, imported]))
    return chains


def _fake_api_module(tmp_path: Path, source: str) -> Path:
    """An `app/api/` containing one offending module, in a throwaway tree."""
    api = tmp_path / "app" / "api"
    api.mkdir(parents=True, exist_ok=True)
    (api / "__init__.py").write_text("", encoding="utf-8")
    (api / "offender.py").write_text(source, encoding="utf-8")
    return tmp_path


def _offenders(chains: dict[str, list[str]]) -> list[str]:
    """Reached modules that are forbidden, each with the chain that reached it."""
    return [
        f"{module}  (via {' -> '.join(chain)})"
        for module, chain in sorted(chains.items())
        if module.split(".")[0] in FORBIDDEN_PACKAGES | FORBIDDEN_LIBRARIES
    ]


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_the_control_plane_reaches_no_heavy_work() -> None:
    """The property the story asks for, over the whole of `app/api/`."""
    offenders = _offenders(reachable_from("app/api"))
    assert not offenders, (
        "app/api/ can reach code that does heavy work:\n  "
        + "\n  ".join(offenders)
        + "\n\nBackend §4.1: anything CPU-heavy is a background task. On one 8 GB VM the control "
        "plane must not compete with OCR and PostgreSQL for memory, and an import is enough to load "
        "it — the module does not have to be called. Enqueue the work instead: see "
        "`app/api/background.py`."
    )


def test_the_walk_is_not_vacuous() -> None:
    """A guard over an empty set passes and proves nothing.

    This is the failure mode the C2.5 guard nearly shipped with, so it is asserted directly: the walk
    has to reach the modules `app/api/` obviously does import, and has to have descended several hops
    to find them.
    """
    chains = reachable_from("app/api")

    assert "app.models" in chains, "the walk cannot see app/api's own imports"
    assert "workflow.outbox" in chains
    assert "evidence.polygon" in chains, "the walk is not descending through app.models"
    assert max(len(chain) for chain in chains.values()) >= 4, "the walk is not going deep enough"


def test_a_route_module_importing_extraction_is_caught(tmp_path: Path) -> None:
    """The guard has to be able to fail, and this is the exact case the acceptance names."""
    root = _fake_api_module(
        tmp_path, "from extraction.models.invocations import InvocationRecord\n"
    )
    offenders = _offenders(reachable_from("app/api", root))
    assert any("extraction" in offender for offender in offenders), offenders


def test_an_import_two_hops_away_is_caught(tmp_path: Path) -> None:
    """The realistic shape. Nobody writes `import cv2` in a route module; they import a helper that
    imports a reader that imports cv2, and every file on the way looks reasonable on its own."""
    root = _fake_api_module(tmp_path, "from app.helpers import prepare\n")
    helpers = tmp_path / "app" / "helpers.py"
    helpers.write_text("from extraction.reader import read\n", encoding="utf-8")
    (tmp_path / "extraction").mkdir()
    (tmp_path / "extraction" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "extraction" / "reader.py").write_text("import cv2\n", encoding="utf-8")

    offenders = _offenders(reachable_from("app/api", root))
    assert any("cv2" in offender for offender in offenders), offenders
    chains = reachable_from("app/api", root)
    assert chains["cv2"] == ["app.api.offender", "app.helpers", "extraction.reader", "cv2"]


@pytest.mark.parametrize("library", sorted(FORBIDDEN_LIBRARIES))
def test_a_heavy_library_would_be_caught(library: str, tmp_path: Path) -> None:
    """Every name on the list actually trips the guard.

    A frozenset nothing checks is a list of good intentions: a typo in one entry — `pdfplumberr` —
    would silently permit the library it was written to forbid, and the guard would still be green.
    """
    root = _fake_api_module(tmp_path, f"import {library}\n")
    assert any(library in offender for offender in _offenders(reachable_from("app/api", root)))


def test_a_harmless_import_is_not_flagged(tmp_path: Path) -> None:
    """The guard has to be able to say yes. One that flagged `uuid` would be switched off in a week."""
    root = _fake_api_module(tmp_path, "from uuid import UUID\nimport json\n")
    assert _offenders(reachable_from("app/api", root)) == []


def test_shapely_is_reached_and_that_is_a_recorded_decision() -> None:
    """**Tolerated, not unnoticed.** The control plane does load GEOS, by this exact path.

    Asserted rather than merely commented, so the day the path changes this test says so instead of a
    stale docstring quietly describing something that is no longer true.
    """
    chains = reachable_from("app/api")
    assert "shapely.geometry" in chains, "the shapely path changed; revisit the recorded decision"

    chain = chains["shapely.geometry"]
    # The tail is the decision. Which module under `app/api/` the walk happens to start from is an
    # artefact of iteration order — every one of them reaches `app.models` — so asserting the first
    # hop would make this fail whenever a route module is added or renamed, which teaches nobody
    # anything. What was actually decided is that the chain runs through the evidence types.
    assert chain[0].startswith("app.api.")
    assert chain[-4:] == [
        "app.models.evidence",
        "evidence.canonical",
        "evidence.polygon",
        "shapely.geometry",
    ], chain
    assert "shapely" in TOLERATED_LIBRARIES
    assert "shapely" not in FORBIDDEN_LIBRARIES, "tolerated and forbidden are contradictory"


# ---------------------------------------------------------------------------
# The retrieval import this story removed
# ---------------------------------------------------------------------------


def test_the_control_plane_no_longer_reaches_retrieval() -> None:
    """The finding this story turned up. `app/models/matching.py` imported `Lane` from
    `retrieval.candidate`, which put every module reaching `app.models` — the whole of `app/api/` —
    one hop from the retrieval package, and broke §2's table into the bargain."""
    chains = reachable_from("app/api")
    reached = [module for module in chains if module.split(".")[0] == "retrieval"]
    assert not reached, f"app/api/ reaches retrieval again: {reached}"


def test_app_models_does_not_import_retrieval() -> None:
    """§2's row, directly: `app/models/` may import `units/`, `rules/` and `evidence/`, never
    `retrieval/`. Asserted on `app/models/` rather than only through `app/api/`, because the table is
    a statement about that package whoever happens to import it."""
    chains = reachable_from("app/models")
    reached = [module for module in chains if module.split(".")[0] == "retrieval"]
    assert not reached, f"app/models/ imports retrieval: {reached}"


def test_moving_lane_changed_no_stored_value() -> None:
    """The move is safe only if the members and their values are untouched — the SQL `CHECK` on
    `match_candidates.lane` is derived from them, and a changed value would need a migration."""
    from app.models.matching import LANE_VALUES

    assert [member.value for member in Lane] == [
        "exact",
        "alias",
        "metadata",
        "geometry",
        "trigram",
        "lexical",
        "dense",
        "fusion",
    ]
    assert (
        LANE_VALUES
        == "'exact', 'alias', 'metadata', 'geometry', 'trigram', 'lexical', 'dense', 'fusion'"
    )


def test_the_old_import_path_still_works() -> None:
    """`retrieval.candidate.Lane` is re-exported, so retrieval's own callers are untouched and there
    is one definition rather than two that agree. `PageType` moved this way first."""
    from retrieval.candidate import Lane as ReExported

    assert ReExported is Lane


def test_the_lane_vocabulary_imports_nothing_from_the_project() -> None:
    """What makes `vocabulary/` safe for anything to import. A vocabulary that grew a dependency would
    carry it into every package that names the concept — which is precisely how the retrieval import
    got into the control plane in the first place."""
    chains = reachable_from("vocabulary")
    reached = [
        module
        for module in chains
        if module.split(".")[0] in PROJECT_PACKAGES and module.split(".")[0] != "vocabulary"
    ]
    assert not reached, f"vocabulary/ reaches {reached}"


# ---------------------------------------------------------------------------
# Accepting work without doing it
# ---------------------------------------------------------------------------


class _FakeSession:
    """Records what was added, so `enqueue_and_respond` can be tested without a database."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def test_accepted_work_is_queued_and_says_so() -> None:
    """The honest status at the moment of accepting: recorded, not started."""
    session = _FakeSession()
    accepted = enqueue_and_respond(
        session,  # type: ignore[arg-type]
        workflow=WORKFLOW,
        payload={"project_id": str(PROJECT_A), "document_id": str(uuid4())},
    )

    assert accepted.status == WorkStatus.QUEUED
    assert "Nothing has started yet" in accepted.what_it_means
    assert accepted.accepted_work_id == session.added[0].id


def test_the_handle_is_the_outbox_entry_not_a_run_id() -> None:
    """**The dishonesty this avoided.** No workflow run exists at enqueue time — `enqueue` starts
    nothing, and `engine_run_id` is assigned by the engine after commit. A `workflow_run_id` here
    would be an invented value that never matches the run, and a client correlating engine logs by it
    would find nothing."""
    session = _FakeSession()
    accepted = enqueue_and_respond(
        session,  # type: ignore[arg-type]
        workflow=WORKFLOW,
        payload={"project_id": str(PROJECT_A)},
    )

    assert "workflow_run_id" not in AcceptedOut.model_fields
    assert "accepted_work_id" in AcceptedOut.model_fields
    assert isinstance(session.added[0], OutboxEntry)
    assert accepted.accepted_work_id == session.added[0].id


def test_a_payload_with_no_project_is_refused() -> None:
    """The handle is scoped by `payload->>'project_id'`, so a payload without one could only be given
    an unscoped URL or a URL that 404s for ever. Both are worse than refusing."""
    with pytest.raises(PayloadMissingProject, match="project_id"):
        enqueue_and_respond(
            _FakeSession(),  # type: ignore[arg-type]
            workflow=WORKFLOW,
            payload={"document_id": str(uuid4())},
        )


@pytest.mark.parametrize("value", ["", "   ", None])
def test_a_blank_project_is_refused_too(value: str | None) -> None:
    """An empty string is present and useless — it would build a URL matching no row."""
    with pytest.raises(PayloadMissingProject):
        enqueue_and_respond(
            _FakeSession(),  # type: ignore[arg-type]
            workflow=WORKFLOW,
            payload={"project_id": value},
        )


def test_nothing_is_committed_or_started() -> None:
    """It adds a row to the caller's transaction and nothing else. A commit here would break the
    outbox: the point is that the business change and the intent land together."""
    session = _FakeSession()
    enqueue_and_respond(
        session,  # type: ignore[arg-type]
        workflow=WORKFLOW,
        payload={"project_id": str(PROJECT_A)},
    )

    assert len(session.added) == 1
    assert not hasattr(session, "committed")
    assert session.added[0].dispatched_at is None
    assert session.added[0].attempts == 0


def test_enqueue_returns_the_id_of_the_row_it_added() -> None:
    """The change to `workflow/outbox.py` this needed. No flush: `app/db/base.py` assigns identity in
    `__init__` precisely so an id exists before the row reaches the database."""
    session = _FakeSession()
    returned = enqueue(session, workflow=WORKFLOW, payload={"project_id": str(PROJECT_A)})  # type: ignore[arg-type]

    assert returned == session.added[0].id
    assert isinstance(returned, UUID)


# ---------------------------------------------------------------------------
# The status URL cannot drift from the route
# ---------------------------------------------------------------------------


def test_the_status_url_is_the_path_the_app_serves() -> None:
    """A handed-out URL that does not match the mounted route is a 404 the client cannot explain.
    Both are built from `ACCEPTED_WORK_PATH`, and this is what proves they stayed that way."""
    served = [
        path
        for path in _served_paths(create_app(_settings()))
        if path.endswith("/accepted-work/{accepted_work_id}")
    ]
    assert served == [API_PREFIX + ACCEPTED_WORK_PATH]


def test_the_url_carries_no_host() -> None:
    """A path, not an absolute URL. Whatever sits in front of this service decides the host, and
    building one from request headers is how a link ends up pointing at an internal address."""
    url = status_url_for(PROJECT_A, uuid4())
    assert url.startswith("/projects/")
    assert "http" not in url


def test_the_returned_url_includes_the_mount_prefix() -> None:
    """`enqueue_and_respond` is called from a router mounted under a prefix, so the path it returns
    has to include it or the client requests a path nobody serves."""
    accepted = enqueue_and_respond(
        _FakeSession(),  # type: ignore[arg-type]
        workflow=WORKFLOW,
        payload={"project_id": str(PROJECT_A)},
        prefix=API_PREFIX,
    )
    assert accepted.status_url.startswith(f"{API_PREFIX}/projects/{PROJECT_A}/accepted-work/")


# ---------------------------------------------------------------------------
# What the status endpoint says, and to whom
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(database_url=DATABASE_URL)  # type: ignore[call-arg]


def _principal(*projects: UUID) -> Principal:
    return Principal(id="anant", roles=frozenset({Role.REVIEWER}), projects=frozenset(projects))


def _served_paths(app: FastAPI) -> list[str]:
    """Every path the app serves, prefixes applied, including behind `include_router`.

    Current FastAPI appends one opaque object per `include_router` rather than flattening the
    children, so filtering `app.routes` for `APIRoute` sees nothing from a wired router — the trap
    `tests/api/test_authorisation.py` documents at length.
    """
    found: list[str] = []

    def descend(routes: list[Any], prefix: str) -> None:
        for route in routes:
            context = getattr(route, "include_context", None)
            if context is not None:
                inner = getattr(route, "original_router", None)
                descend(list(getattr(inner, "routes", [])), prefix + getattr(context, "prefix", ""))
            elif isinstance(route, APIRoute):
                found.append(prefix + route.path)

    descend(list(app.routes), "")
    return found


def _walk(dependant: Any) -> list[Any]:
    found: list[Any] = []
    for dependency in getattr(dependant, "dependencies", []):
        if dependency.call is not None:
            found.append(dependency.call)
        found.extend(_walk(dependency))
    return found


def test_the_status_route_carries_the_project_boundary() -> None:
    """Project scope is an isolation boundary, not a filter (ADR-0006). The row itself has no
    project column, so this dependency and the payload match are both load-bearing."""
    route = next(
        r
        for r in background.router.routes
        if isinstance(r, APIRoute) and r.path.endswith("/accepted-work/{accepted_work_id}")
    )
    assert "{project_id}" in route.path
    assert require_project_access in _walk(route.dependant)


@pytest.mark.parametrize(
    ("dispatched", "attempts", "expected"),
    [
        (False, 0, WorkStatus.QUEUED),
        (False, 3, WorkStatus.RETRYING),
        (True, 1, WorkStatus.STARTED),
    ],
    ids=["not-picked-up", "hand-off-failing", "handed-to-the-engine"],
)
def test_the_status_reads_the_two_columns_honestly(
    dispatched: bool, attempts: int, expected: str
) -> None:
    """`dispatched_at` is stamped only after the engine accepted the start, so it is the only
    evidence anything began. `attempts` above zero without it is the difference between "waiting its
    turn" and "wedged", which is worth telling somebody."""
    entry = OutboxEntry(workflow=WORKFLOW, payload={}, attempts=attempts)
    entry.dispatched_at = datetime(2026, 1, 1, tzinfo=UTC) if dispatched else None

    assert background._status_of(entry) == expected


def test_started_does_not_claim_the_work_finished() -> None:
    """**The honesty the acceptance criterion is about.** The outbox knows the workflow was handed to
    the engine and nothing more. A client reading `started` as completion would report a drawing as
    reviewed on the strength of the request having been accepted."""
    description = STATUS_DESCRIPTIONS[WorkStatus.STARTED]
    assert "nothing about whether" in description
    assert "finished" in description
    for state, text in STATUS_DESCRIPTIONS.items():
        assert text.strip(), state


def test_every_status_has_a_plain_english_description() -> None:
    """A status a client has to look up is one they will guess at."""
    assert set(STATUS_DESCRIPTIONS) == {
        WorkStatus.QUEUED,
        WorkStatus.RETRYING,
        WorkStatus.STARTED,
    }


# ---------------------------------------------------------------------------
# The scope filter, checked as SQL without needing a database
# ---------------------------------------------------------------------------


def _compiled(project_id: UUID, work_id: UUID) -> str:
    from sqlalchemy.dialects import postgresql

    from app.api.background import scoped_query

    return str(
        scoped_query(project_id, work_id).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_the_scope_filter_is_in_the_sql() -> None:
    """**The isolation boundary, asserted on the statement itself.**

    The database-backed tests below prove it behaves; this proves the clause is *there*, and it runs
    everywhere — including where no PostgreSQL is available, which is where this would otherwise go
    unverified until CI. A `->>` comparison against a mistyped key matches nothing while looking
    exactly right, and the failure mode is a boundary that silently 404s everything rather than one
    that leaks, so it would be found late and by the wrong person.
    """
    sql = _compiled(PROJECT_A, uuid4())

    assert "payload ->> 'project_id'" in sql, sql
    assert str(PROJECT_A) in sql
    assert "outbox_entries.id = " in sql


def test_the_filter_pins_the_row_as_well_as_the_project() -> None:
    """Two clauses, not one. The id alone would let a caller in project A read project B's row; the
    project alone would return somebody else's work for this project."""
    work_id = uuid4()
    sql = _compiled(PROJECT_A, work_id)

    assert sql.count(" AND ") >= 1
    assert str(work_id) in sql


# ---------------------------------------------------------------------------
# ...and against a real database, where the JSONB comparison actually runs
# ---------------------------------------------------------------------------


@pytest.fixture
def factory(postgres_engine: Engine) -> sessionmaker[Session]:
    """A session factory against a database migrated to head."""
    config = Config("alembic.ini")
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return session_factory(postgres_engine)


def _app(principal: Principal, session: Session) -> FastAPI:
    app = create_app(_settings())
    app.dependency_overrides[authenticate] = lambda: principal
    app.dependency_overrides[background.get_session] = lambda: session
    return app


def _url(project_id: UUID, work_id: UUID) -> str:
    return API_PREFIX + status_url_for(project_id, work_id)


def test_accepted_work_can_be_polled_back(factory: sessionmaker[Session]) -> None:
    """The round trip, against PostgreSQL: enqueue, commit, and read the handle back.

    Against a real database because the scope filter is `payload->>'project_id'` — real SQL that a
    stubbed session would not execute, so a mistake in it would pass a mocked test and leak in
    production.
    """
    with factory() as session:
        work_id = enqueue(
            session, workflow=WORKFLOW, payload={"project_id": str(PROJECT_A), "n": 1}
        )
        session.commit()

    with factory() as session:
        client = TestClient(_app(_principal(PROJECT_A), session), raise_server_exceptions=False)
        response = client.get(_url(PROJECT_A, work_id))

    assert response.status_code == 200
    body = response.json()
    assert body["accepted_work_id"] == str(work_id)
    assert body["status"] == WorkStatus.QUEUED


def test_another_projects_work_is_indistinguishable_from_absent(
    factory: sessionmaker[Session],
) -> None:
    """**The isolation property.** A 403 would confirm the work exists, which is what the boundary is
    for. The caller belongs to project B and asks for project B's URL, so the dependency lets them
    through — the payload match is the only thing standing between them and project A's row."""
    with factory() as session:
        work_id = enqueue(session, workflow=WORKFLOW, payload={"project_id": str(PROJECT_A)})
        session.commit()

    with factory() as session:
        client = TestClient(_app(_principal(PROJECT_B), session), raise_server_exceptions=False)
        response = client.get(_url(PROJECT_B, work_id))

    assert response.status_code == 404
    assert str(PROJECT_A) not in response.text
    assert "forbidden" not in response.text.lower()


def test_work_that_does_not_exist_is_the_same_404(factory: sessionmaker[Session]) -> None:
    """The two answers have to be identical, or the difference between them is the leak."""
    with factory() as session:
        client = TestClient(_app(_principal(PROJECT_A), session), raise_server_exceptions=False)
        missing = client.get(_url(PROJECT_A, uuid4()))

    assert missing.status_code == 404
    assert missing.json()["message"] == background.NOT_FOUND_DETAIL


def test_a_dispatched_row_reports_started(factory: sessionmaker[Session]) -> None:
    """The other end of the lifecycle, read back through the endpoint rather than the helper."""
    with factory() as session:
        work_id = enqueue(session, workflow=WORKFLOW, payload={"project_id": str(PROJECT_A)})
        session.flush()
        entry = session.get(OutboxEntry, work_id)
        assert entry is not None
        entry.attempts = 1
        entry.dispatched_at = datetime.now(UTC)
        session.commit()

    with factory() as session:
        client = TestClient(_app(_principal(PROJECT_A), session), raise_server_exceptions=False)
        body = client.get(_url(PROJECT_A, work_id)).json()

    assert body["status"] == WorkStatus.STARTED
    assert "nothing about whether it finished" in body["what_it_means"]


def test_the_route_is_wired_into_the_factory() -> None:
    """A router nobody included serves nothing, and every test above that uses `background.router`
    directly would still pass. This is the one that fails if the `include_router` line is dropped.
    """
    assert API_PREFIX + ACCEPTED_WORK_PATH in _served_paths(create_app(_settings()))


def test_the_dependency_is_the_shared_one() -> None:
    """`app/api/dependencies.get_session`, not a private copy — three copies of an engine-caching
    dependency is three places to build a connection pool per request."""
    from app.api.dependencies import get_session

    assert background.get_session is get_session
