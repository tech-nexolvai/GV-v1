"""Authorisation as dependencies, so no endpoint can forget it.

**Scope filtering that lives in each endpoint is scope filtering that will eventually be left out of
one.** These are applied at the router, so a new route inherits them by default, and
`tests/api/test_authorisation.py` enumerates every route and fails on any that does not carry them.
The test is the part that keeps this true as the surface grows; the dependency alone only makes it
easy.

**Cross-project access returns 404, never 403.** A 403 confirms the thing exists. Project scope is an
isolation boundary, and a boundary that answers "yes, but not for you" has already told the caller
what they wanted to know — `docs/DESIGN_PLATFORM.md` §4.3 names the 404-versus-403 difference
explicitly.

**Authentication is a seam, and it fails closed.** No identity provider is chosen anywhere in the
design, so `authenticate` raises rather than inventing one. A default that returned an anonymous
principal would make every one of these checks pass in development and fail in production — or worse,
pass in both.

Source: backend proposal §11 · Design: `docs/DESIGN_PLATFORM.md` §4.3
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status

from app.auth.roles import Action, Principal, Role

logger = logging.getLogger("gv.auth")

#: Routes that legitimately carry no project scope. Everything else must, and the enumerating test
#: reads this list — so exempting a route is a visible edit here rather than an omission nobody sees.
UNSCOPED_ROUTES: frozenset[str] = frozenset(
    {"/health", "/ready", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
)


class AuthenticationNotConfigured(RuntimeError):
    """No identity provider is wired, so nobody can be authenticated.

    Raised rather than defaulting to an anonymous principal. An anonymous default would make every
    authorisation check below pass silently, and the failure would surface as a data leak rather than
    as a missing configuration.
    """


def authenticate(request: Request) -> Principal:
    """Turn a request into a principal. Replaced by the deployment; refuses by default.

    Overridden with FastAPI's `dependency_overrides` in tests and by whatever the deployment wires in
    production. The default is deliberately useless.
    """
    del request
    raise AuthenticationNotConfigured(
        "no identity provider is configured. Override `authenticate` in the application factory; "
        "this refuses rather than returning an anonymous principal, because an anonymous default "
        "makes every authorisation check pass."
    )


def _refuse(principal: Principal, action: str, target: str, reason: str) -> HTTPException:
    """Log the refusal, then produce a 404 that says nothing.

    Every authorisation failure is logged with actor, action and target — that is the acceptance
    criterion, and it is also the only record that an attempt happened at all, since the response
    deliberately looks like an absence.
    """
    logger.warning(
        "authorisation refused",
        extra={"actor": principal.id, "action": action, "target": target, "reason": reason},
    )
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


#: The authenticated caller, resolved by whatever the deployment wired into `authenticate`.
#: `Annotated` rather than a `Depends()` default: the default form evaluates a call at import, which
#: ruff flags (B008) and which makes the dependency invisible to anything reading the signature.
Authenticated = Annotated[Principal, Depends(authenticate)]


def require_project_access(project_id: UUID, principal: Authenticated) -> Principal:
    """The isolation boundary. A principal outside the project is told the project does not exist."""
    if not principal.belongs_to(project_id):
        raise _refuse(principal, "read_project", str(project_id), "principal is not in the project")
    return principal


def require_role(*roles: Role) -> Callable[..., Principal]:
    """Require one of `roles`, independently of project membership.

    Separate from `require_project_access` because they answer different questions: membership says
    *which* data, role says *what may be done to it*. Collapsing them would make "a reviewer in this
    project may publish rules" expressible by accident.
    """
    allowed = frozenset(roles)

    def dependency(principal: Authenticated) -> Principal:
        if not (principal.roles & allowed):
            raise _refuse(
                principal,
                "role_check",
                ",".join(sorted(role.value for role in allowed)),
                f"principal holds {sorted(r.value for r in principal.roles)}",
            )
        return principal

    return dependency


def require_action(action: Action) -> Callable[..., Principal]:
    """Require the roles that `PERMISSIONS` grants this action.

    Preferred over `require_role` at call sites: an endpoint says what it *is* — "this approves a
    package" — and the table decides who may. Naming roles at the endpoint puts the policy back in the
    place this module exists to take it out of.
    """

    def dependency(principal: Authenticated) -> Principal:
        if not principal.may(action):
            raise _refuse(
                principal,
                action.value,
                "-",
                f"principal holds {sorted(r.value for r in principal.roles)}",
            )
        return principal

    return dependency
