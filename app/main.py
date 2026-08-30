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

**The factory refuses to build an unsafe API.** Once the routes are declared, `assert_no_verdict_fields`
walks all of them and raises if any would accept a PASS/FAIL, a tolerance or a measurement from its
caller (#207, C2.5). A startup failure is the right shape for this: the alternative is a process that
serves such a route and produces findings nobody can tell apart from computed ones.

Source: `docs/DESIGN_PLATFORM.md` §4.1, §4.2 · Verification: `tests/api/test_app.py`,
`tests/api/test_no_client_verdict.py`
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Final
from uuid import uuid4

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import StringConstraints, TypeAdapter, ValidationError
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.api.guards import assert_no_verdict_fields
from app.auth import authenticate
from app.auth.development import principal_from_environment
from app.config import Settings
from app.errors import REQUEST_ID_STATE, _envelope, install_error_handlers
from app.telemetry.tracing import (
    TRACE_ID_HEADER,
    TRACE_ID_STATE,
    current_trace_id,
    incoming_context,
    traced,
)

#: The six API groups that hang off this app: packages, documents, findings, review, rules and
#: operations. All but review are wired; review attaches the same way.
API_PREFIX = "/api/v1"

#: The longest caller-supplied request id this app will keep.
#:
#: **100 and not 128, for a reason worth writing down.** The accepted value goes onto a span, where
#: `app/telemetry/tracing.py` refuses an unbroken run of 120 or more base64-shaped characters. My first
#: attempt allowed 128, so a perfectly ordinary opaque token — 126 alphanumeric characters — satisfied the
#: boundary and was then refused inside the middleware, turning a valid request into a 500. Two limits that
#: have to agree, and they did not. `test_a_maximum_length_request_id_is_actually_usable` keeps them
#: agreeing: it probes for the longest accepted value rather than naming one, so widening this past the
#: span guard's threshold fails a test.
#:
#: A UUID is 36 characters and a `sha256:`-prefixed digest is 71, so this is clear of every real id while
#: far too small to smuggle content through.
REQUEST_ID_MAX_LENGTH: Final = 100

#: The request id contract: ids, hex, digests and prefixed forms, and nothing else.
#:
#: **A Pydantic contract rather than a local regular expression**, because `AGENTS.md` says Pydantic v2 for
#: every boundary contract and an inbound header is exactly that. Review's point, and the right one: the
#: constraint now reads the same way as every other boundary in this codebase instead of being a private
#: convention in one module.
#:
#: Deliberately narrow. The value is echoed in a response header, written to logs and put on a span, and
#: before any of this it was taken from the header with no constraint at all — so a caller could choose a
#: 200 KB value, embed a data URI, or include control characters. None of that is a plausible request id and
#: all of it ends up somewhere it should not.
RequestId = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Za-z0-9._:-]+$", min_length=1, max_length=REQUEST_ID_MAX_LENGTH
    ),
]

_REQUEST_ID = TypeAdapter(RequestId)


def _accepted_request_id(supplied: str | None) -> str:
    """The caller's request id if it is one, otherwise a fresh one.

    **Replaced rather than rejected.** A malformed `X-Request-ID` is not worth failing a request over — the
    caller gets a working response and an id we generated, which is exactly what happens when they send
    none. Refusing would turn a cosmetic client bug into an outage.
    """
    try:
        return _REQUEST_ID.validate_python(supplied)
    except ValidationError:
        return str(uuid4())


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

    # A development identity, if one was asked for and this is a laptop. `authenticate` refuses by
    # default so that a missing identity provider cannot become an anonymous principal — which is
    # right, and also means nothing can call this API locally. `app/auth/development.py` is the
    # local answer and refuses to exist anywhere else; the override is installed here, in the
    # factory, rather than inside the dependency, so the decision is visible where the app is built.
    development_principal = principal_from_environment(resolved.environment)
    if development_principal is not None:
        app.dependency_overrides[authenticate] = lambda: development_principal

    # Imported here rather than at module scope: the routers pull in the ORM and the query layer, and
    # a module-level import would make `app.main` drag the database into anything that merely wants
    # `create_app` — including the isolation tests, whose whole job is to prove what does not import
    # what.
    from app.api import (
        background,
        documents,
        finding_chain,
        finding_export,
        findings,
        operations,
        packages,
        review,
        rules,
    )

    app.include_router(packages.router, prefix=API_PREFIX)
    app.include_router(documents.router, prefix=API_PREFIX)
    app.include_router(findings.router, prefix=API_PREFIX)
    app.include_router(finding_chain.router, prefix=API_PREFIX)
    # The versioned export downstream consumers read (#224, D1.3). Same prefix as the rest, so the shape a
    # report or spreadsheet pins is served from the path the API documents.
    app.include_router(finding_export.router, prefix=API_PREFIX)
    # The handle for work the API accepted rather than did (#208, C2.6). Mounted under the same prefix,
    # which is what makes the `status_url` handed to a client a path this service actually serves.
    app.include_router(background.router, prefix=API_PREFIX)

    # The rulebook and the operation registry (#206, C2.4). Read-only apart from publish, which
    # delegates to D6.
    app.include_router(rules.router, prefix=API_PREFIX)
    app.include_router(operations.router, prefix=API_PREFIX)

    # Review sessions and the actions a reviewer takes (#229). The service has existed since
    # D4.1; this is what lets the workspace reach it.
    app.include_router(review.router, prefix=API_PREFIX)

    @app.middleware("http")
    async def _request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Bind one id and one trace to the request, and put both back on the response.

        Read from the inbound header when the caller supplied one: an id that changes at our boundary
        makes a distributed trace two traces, and the reviewer's report cites whichever half we
        happened to log.

        **The span is here and not in each handler**, so no route can forget it and no route can invent
        its own convention — `app/telemetry/tracing.py` explains why there is exactly one. The trace id
        goes back on the response for the same reason the request id does: an id nobody outside the
        process can see cannot appear in a bug report.

        The only attribute set here is `request_id`, which is the one this layer actually knows. A
        request knows its path, not its `package_id` — the rest of `SPAN_ATTRS` belongs on the spans the
        handlers and workers open inside this one, which is #259's remaining work.
        """
        header = resolved.request_id_header
        request_id = _accepted_request_id(request.headers.get(header))
        setattr(request.state, REQUEST_ID_STATE, request_id)

        # A caller's own trace is continued rather than restarted; `None` means it sent none, and the
        # span then starts a fresh trace.
        #
        # Opened through `traced()` and not the tracer directly. The first version did the latter and set
        # `gv.request_id` with `span.set_attribute`, so the one span every request creates was the one span
        # that never met the checks in `app/telemetry/tracing.py` — including on a value the caller
        # controls. Going through the helper is what makes that guard mean something.
        # **The name is the method alone, never the requested path.** `request.url.path` is caller-
        # controlled and `_checked` guards attributes, not the span name — so a request to an unmatched
        # path carrying encoded drawing content would put that content in the trace before routing
        # rejected it, which is precisely what AGENTS.md §6 forbids. It would also give every junk URL its
        # own span name, which is how a tracing backend's cardinality gets away from you.
        #
        # OpenTelemetry's HTTP convention is `{method} {route}` with the *template*, falling back to the
        # method alone when the route is unknown. That is what happens here: the template is not resolved
        # until routing has run, so the span starts as the method and is renamed below once the route is
        # known — from our own route table, not from the caller.
        with traced(
            request.method,
            parent=incoming_context(request.headers),
            # Correlated deliberately: the request id is what a person quotes and the trace id is what a
            # backend indexes, so each has to be findable from the other.
            request_id=request_id,
        ) as span:
            trace_id = current_trace_id()
            # Parked on the request, not just used here: an unhandled exception is handled outside this
            # middleware, by which point the span has ended. `app/errors.py` stamps the header from state
            # so the error case is not the one case without a trace id.
            if trace_id is not None:
                setattr(request.state, TRACE_ID_STATE, trace_id)
            response = await call_next(request)

            # `scope["route"]` is populated by the router, so it only exists once `call_next` has run, and
            # stays absent for a path that matched nothing. A template we declared is safe to name a span
            # with; the string the caller sent is not.
            template = getattr(request.scope.get("route"), "path", None)
            if isinstance(template, str):
                span.update_name(f"{request.method} {template}")

        response.headers[header] = request_id
        if trace_id is not None:
            response.headers[TRACE_ID_HEADER] = trace_id
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

    # Last, once every route is declared — the audit walks what the app will actually serve, and a
    # route added after the check would never be seen by it. Raises rather than warns: a process that
    # boots with a route accepting a client-supplied PASS/FAIL serves it, and the findings it produces
    # are indistinguishable from ones the engine computed. `app/api/guards.py` explains the rule.
    assert_no_verdict_fields(app)

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
