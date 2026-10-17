"""Which hosts and pages may talk to this API (security audit 2026-10-18).

The API listens on 127.0.0.1 and trusts the development identity, so the address it listens on is
not enough: a web page the reviewer has open elsewhere could point its own domain at 127.0.0.1
("DNS rebinding") and read the drawings through the browser, or make the browser send a POST that
starts a paid chat. Two checks close that:

- **Host.** A request is served only when its `Host` header names this machine (`localhost`,
  `127.0.0.1`, `::1`), Starlette's test client (`testserver`, which no DNS name can be), or a host
  the deployment lists in `GV_TRUSTED_HOSTS`. A rebound domain arrives with its own name and is
  refused. The screen's development proxy (`frontend/main/vite.config.ts`, `changeOrigin`) sends
  `127.0.0.1:8000`.
- **Origin, on writes.** A POST, PUT, PATCH or DELETE that a browser sends from a page carries an
  `Origin`; it is served only when that page's host is one of the same hosts. A request without an
  `Origin` (curl, scripts, tests) is not a browser page's and is served.

Refused requests get the API's one error shape (`app/errors.py`), 400 for a host and 403 for an
origin.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Final
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response, status

from app.errors import _envelope

__all__ = ["LOCAL_HOSTS", "allowed_hosts", "host_name", "install_host_guard", "origin_host"]

#: This machine, and Starlette's test client.
LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1", "testserver"})
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


def allowed_hosts(extra: Iterable[str] = ()) -> frozenset[str]:
    """The local hosts and the deployment's own (`GV_TRUSTED_HOSTS`), lower-case."""
    return LOCAL_HOSTS | {name.strip().lower().rstrip(".") for name in extra if name.strip()}


def host_name(header: str | None) -> str | None:
    """The host of a `Host` header without its port (`[::1]:8000` gives `::1`), lower-case."""
    if not header:
        return None
    value = header.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else None
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def origin_host(origin: str) -> str | None:
    """The host of an `Origin` header (`http://localhost:5173` gives `localhost`); `None` for
    `null` or anything that is not a web origin."""
    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.hostname.lower()


def install_host_guard(app: FastAPI, extra_hosts: Iterable[str] = ()) -> None:
    """Refuse a request for a host this API is not, and a browser write from another site's page."""
    hosts = allowed_hosts(extra_hosts)

    @app.middleware("http")
    async def _host_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if host_name(request.headers.get("host")) not in hosts:
            return _envelope(
                request,
                "host_not_allowed",
                "This API answers only on this machine's own address.",
                status.HTTP_400_BAD_REQUEST,
            )
        origin = request.headers.get("origin")
        if (
            request.method not in _SAFE_METHODS
            and origin is not None
            and origin_host(origin) not in hosts
        ):
            return _envelope(
                request,
                "origin_not_allowed",
                "This API takes changes only from GV Review's own screen.",
                status.HTTP_403_FORBIDDEN,
            )
        return await call_next(request)
