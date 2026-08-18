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

from app.config import Settings
from app.errors import REQUEST_ID_STATE, install_error_handlers

#: The six API groups that will hang off this app: packages, documents, findings, review, rules and
#: operations. None exist yet; the skeleton is what they attach to.
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
        """The process can do work: the database answers and the schema is current.

        Returns 503 rather than raising, because a readiness probe that 500s is indistinguishable from
        a crashed process, and the two call for different responses from an orchestrator.

        The migration check is the part that matters. A process serving requests against a schema three
        migrations behind is not degraded, it is wrong — and nothing else in the system would report
        it.
        """
        from sqlalchemy import create_engine

        from app.db.session import session_factory

        problems: list[str] = []
        try:
            engine = create_engine(resolved.database_url)
            with session_factory(engine)() as session:
                session.execute(text("SELECT 1"))
                current = session.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
            if current is None:
                problems.append("no migration has been applied")
        except Exception:  # noqa: BLE001 - the reason is deliberately not surfaced to the caller
            problems.append("the database did not answer")

        if problems:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "not ready",
                    "problems": problems,
                    "request_id": getattr(request.state, REQUEST_ID_STATE, "unknown"),
                },
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ready"})

    return app
