"""The application factory: request ids, the error contract, and two different health questions.

**A factory, not a module-level `app`.** Tests build isolated instances with their own settings, and a
module-level singleton would make every test share one configuration — including the database URL,
which is the one thing a test most needs to control.

**`/health` and `/ready` answer different questions, and conflating them is the point of having both.**
`/health` says the process is alive. `/ready` says it can do work: the database answers, and the
migrations are current. An orchestrator that only checks liveness will route traffic to a process that
is running against a schema three migrations behind, and every request it serves will be wrong in a
way nothing reports.

**Request ids are accepted, not only generated.** A reviewer's report, our logs and the caller's trace
should all say the same id. If the caller supplied one we keep it; otherwise we make one.

Source: `docs/DESIGN_PLATFORM.md` §4.1 · Verification: `tests/api/test_app.py`
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.errors import REQUEST_ID_STATE, _envelope, install_error_handlers

#: The six API groups that will hang off this app: packages, documents, findings, review, rules and
#: operations. Findings is the first one wired; the rest attach the same way.
API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an application.

    `settings` defaults to reading the environment, and constructing it *here* is what makes a missing
    setting a startup failure. Passing one in is how a test avoids needing the environment at all.
    """
    resolved = settings or Settings()  # type: ignore[call-arg]  # values come from the environment
    app = FastAPI(
        title="Graniti Vicentia V1",
        description=(
            "AI-assisted shop drawing review. The AI reads; deterministic Python decides; a "
            "reviewer signs off."
        ),
        version="0.1.0",
    )
    app.state.settings = resolved
    install_error_handlers(app)

    # Imported here rather than at module scope: the router pulls in the ORM and the query layer, and
    # a module-level import would make `app.main` drag the database into anything that merely wants
    # `create_app` — including the isolation tests, whose whole job is to prove what does not import
    # what.
    from app.api import findings

    app.include_router(findings.router, prefix=API_PREFIX)

    @app.middleware("http")
    async def _request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Bind one id to the request, and put it back on the response.

        Read from the inbound header when the caller supplied one: an id that changes at our boundary
        makes a distributed trace two traces, and the reviewer's report cites whichever half we
        happened to log.
        """
        header = resolved.request_id_header
        request_id = request.headers.get(header) or str(uuid4())
        setattr(request.state, REQUEST_ID_STATE, request_id)
        response = await call_next(request)
        response.headers[header] = request_id
        return response

    @app.get("/health", tags=["operations"])
    async def health() -> dict[str, str]:
        """The process is alive. Says nothing about whether it can work — see `/ready`."""
        return {"status": "alive", "environment": resolved.environment}

    @app.get("/ready", tags=["operations"])
    async def ready(request: Request) -> JSONResponse:
        """The process can do work: the database answers and the schema is **at head**.

        Returns 503 rather than raising, because a readiness probe that 500s is indistinguishable from
        a crashed process, and the two call for different responses from an orchestrator.

        The migration check is the part that matters, and the first version did not do what this
        docstring claimed. It asked only whether `alembic_version` was non-empty, so a database three
        migrations behind answered `200 ready` — exactly the failure the endpoint exists to catch. It
        now compares the applied revision against the head revision in `alembic/versions`.

        The work runs in a worker thread: `create_engine` and a synchronous session block the event
        loop, and a readiness probe that stalls it makes every other request slow while reporting on
        health.
        """
        problems = await run_in_threadpool(_readiness_problems, resolved.database_url)
        if problems:
            # The same envelope as every other failure. "One error shape" would be a claim this
            # endpoint quietly broke, and a client handling errors through ErrorEnvelope could not
            # parse a bespoke body.
            return _envelope(
                request, "not_ready", "; ".join(problems), status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ready"})

    return app


def _readiness_problems(database_url: str) -> list[str]:
    """Everything wrong with the database right now, in plain English. Synchronous by design.

    Returns a list rather than raising on the first, because an operator reading a probe wants both
    problems at once rather than one per restart.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine

    from app.db.session import session_factory

    try:
        engine = create_engine(database_url)
        with session_factory(engine)() as session:
            session.execute(text("SELECT 1"))
            applied = session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - the reason is deliberately not surfaced to the caller
        return ["the database did not answer"]

    expected = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    if applied is None:
        return ["no migration has been applied"]
    if applied != expected:
        problem = (
            f"the schema is at {applied} but the code expects {expected}. A process serving "
            "requests against an outdated schema is not degraded, it is wrong."
        )
        return [problem]
    return []
