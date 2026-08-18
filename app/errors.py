"""One error shape, for every failure.

**Including validation errors.** FastAPI answers a bad request body with its own format, so a client
would meet two different error shapes depending on where the failure happened — and the one place a
client most needs a stable contract is the error path, because that is the code they write once and
never look at again.

Every response therefore carries `error`, `message` and `request_id`:

* `error` is a stable machine code. Clients branch on it, so it does not change when the wording does.
* `message` is plain English and safe to show a reviewer. No stack traces, no SQL, no file paths —
  `AGENTS.md` §6 forbids drawing content in logs, and an error message is a log with an audience.
* `request_id` is the same id in the response, the logs and the reviewer's report, which is what makes
  "it failed at 14:32" answerable.

Source: `docs/DESIGN_PLATFORM.md` §4.1 · Verification: `tests/api/test_app.py`
"""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

#: Where the request id lives on the request state, set by the middleware in `app/main.py`.
REQUEST_ID_STATE: Final = "request_id"


class ErrorEnvelope(BaseModel):
    """The only error body this API returns."""

    error: str
    message: str
    request_id: str


def _envelope(request: Request, code: str, message: str, http_status: int) -> JSONResponse:
    """Build the error response, and put the request id in the header as well as the body.

    The header is set here rather than left to the middleware because an unhandled exception
    propagates *past* the middleware — it never gets a response to stamp — so a 500 came back with
    the id in the body and no header at all. That is the one case where a caller most needs it, and
    the two disagreeing is worse than either alone: a reviewer quotes one and we search for the other.
    """
    request_id = getattr(request.state, REQUEST_ID_STATE, "unknown")
    settings = getattr(request.app.state, "settings", None)
    header = getattr(settings, "request_id_header", "X-Request-ID")
    return JSONResponse(
        status_code=http_status,
        content=ErrorEnvelope(error=code, message=message, request_id=request_id).model_dump(),
        headers={header: request_id},
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's own shape, replaced.

    The detail is deliberately not echoed back. Pydantic's error list contains the submitted values,
    and a rejected request may carry a dimension read off a client's drawing — `AGENTS.md` §6 keeps
    drawing content out of anything that gets logged or forwarded.
    """
    return _envelope(
        request,
        "invalid_request",
        "The request body or parameters did not validate. Check the API reference for the "
        "expected shape; the submitted values are deliberately not echoed back.",
        status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    """A raised `HTTPException`, with its detail forwarded only when the status says it is for the
    caller.

    A 4xx detail is written by us to tell a client what they got wrong — "no such package" — and is
    safe. A 5xx detail is written for us: it may name a host, a table or a driver error, and
    forwarding it hands an internal detail to whoever asked. The first version forwarded both.
    """
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        return _envelope(
            request,
            "internal_error",
            "Something went wrong on our side. Quote the request id when reporting it.",
            exc.status_code,
        )
    return _envelope(request, "http_error", str(exc.detail), exc.status_code)


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """Anything unforeseen, without telling the caller what broke.

    A stack trace or a database message in a response body is how an internal detail becomes a
    client's problem to interpret — and occasionally a security finding. The request id is the handle
    for going and reading the log.
    """
    return _envelope(
        request,
        "internal_error",
        "Something went wrong on our side. Quote the request id when reporting it.",
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers. Called by the factory, never at import."""
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled_error)
