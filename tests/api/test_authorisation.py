"""Authorisation, and the enumerating test that keeps it applied (#204, C2.2).

**Scope filtering that lives in each endpoint is scope filtering that will eventually be left out of
one.** The dependency makes it easy to apply; this file is what makes it stay applied, by walking
every route and failing on any that carries no authorisation. As the API grows from two operational
endpoints to six groups, that test is the only version of this guarantee that remains true.

The other half is what a refusal looks like. Project scope is an isolation boundary, so a caller
outside a project is told the thing does not exist — a 403 confirms it does, and confirming it is
what the boundary exists to prevent.
"""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.auth import (
    AUTHORISATION_MARKER,
    PERMISSIONS,
    UNSCOPED_ROUTES,
    Action,
    AuthenticationNotConfigured,
    Principal,
    Role,
    authenticate,
    require_action,
    require_project_access,
    require_role,
)
from app.config import Settings
from app.main import create_app

DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"
PROJECT_A = uuid4()
PROJECT_B = uuid4()


def _settings() -> Settings:
    return Settings(database_url=DATABASE_URL)  # type: ignore[call-arg]


def _principal(*roles: Role, projects: frozenset | None = None) -> Principal:
    return Principal(
        id="anant",
        roles=frozenset(roles),
        projects=projects if projects is not None else frozenset({PROJECT_A}),
    )


def _impostor(project_id: str) -> str:
    """Named to look like the real check, and carrying no marker.

    Defined at module level because `from __future__ import annotations` makes the `Depends(...)`
    inside an `Annotated[...]` a *string*, resolved from module globals — a dependency defined inside
    a test function is never seen, and FastAPI silently resolves the real name instead. That is what
    made the first version of this test pass for the wrong reason.
    """
    return "not a check"


_impostor.__name__ = "require_project_access"


def _layer_one(principal: Annotated[Principal, Depends(require_project_access)]) -> Principal:
    return principal


def _layer_two(principal: Annotated[Principal, Depends(_layer_one)]) -> Principal:
    return principal


def _app_with(principal: Principal) -> FastAPI:
    app = create_app(_settings())
    app.dependency_overrides[authenticate] = lambda: principal
    return app


# ---------------------------------------------------------------------------
# The table, not scattered conditions
# ---------------------------------------------------------------------------


def test_every_action_has_roles_assigned() -> None:
    """An unassigned action is one no check can evaluate, and the safe reading is not obvious."""
    assert set(PERMISSIONS) == set(Action)


def test_confirming_evidence_and_publishing_a_rule_are_different_rights() -> None:
    """The acceptance asks for this explicitly. The person who decides what "correct" means and the
    person who certifies a drawing meets it should be able to be different people."""
    reviewer = _principal(Role.REVIEWER)
    rule_admin = _principal(Role.RULE_ADMIN)

    assert reviewer.may(Action.CONFIRM_EVIDENCE) and not reviewer.may(Action.PUBLISH_RULE)
    assert rule_admin.may(Action.PUBLISH_RULE) and not rule_admin.may(Action.CONFIRM_EVIDENCE)


def test_a_reviewer_cannot_approve_a_project_they_do_not_belong_to() -> None:
    """Role and membership answer different questions, and holding one is not holding the other."""
    reviewer = _principal(Role.REVIEWER, projects=frozenset({PROJECT_A}))
    assert reviewer.may(Action.APPROVE_PACKAGE)
    assert not reviewer.belongs_to(PROJECT_B)


def test_admin_is_listed_explicitly_rather_than_short_circuited() -> None:
    """A wildcard would mean the table no longer answers "who may publish a rule?" on its own, which
    is the entire point of having one."""
    for action, roles in PERMISSIONS.items():
        assert Role.ADMIN in roles, f"{action.value} does not name admin"


# ---------------------------------------------------------------------------
# Authentication fails closed
# ---------------------------------------------------------------------------


def test_authentication_refuses_rather_than_returning_anonymous() -> None:
    """An anonymous default would make every authorisation check below pass silently, and the failure
    would surface as a data leak rather than as a missing configuration."""
    app = create_app(_settings())
    router = APIRouter()

    @router.get("/guarded")
    async def guarded(principal: Annotated[Principal, Depends(authenticate)]) -> dict[str, str]:
        return {"id": principal.id}  # pragma: no cover - never reached

    app.include_router(router)
    with pytest.raises(AuthenticationNotConfigured):
        TestClient(app, raise_server_exceptions=True).get("/guarded")


# ---------------------------------------------------------------------------
# A boundary, not a filter
# ---------------------------------------------------------------------------


def test_another_projects_data_is_indistinguishable_from_absent() -> None:
    """**The finding this story exists to prevent.** A 403 confirms the thing exists, and confirming
    it is what the boundary is for. `DESIGN_PLATFORM.md` §4.3 names the 404-versus-403 difference.
    """
    app = _app_with(_principal(Role.REVIEWER, projects=frozenset({PROJECT_A})))

    @app.get("/projects/{project_id}/packages")
    async def packages(
        principal: Annotated[Principal, Depends(require_project_access)],
    ) -> dict[str, str]:  # pragma: no cover - exercised via HTTP
        return {"id": principal.id}

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get(f"/projects/{PROJECT_A}/packages").status_code == 200

    refused = client.get(f"/projects/{PROJECT_B}/packages")
    assert refused.status_code == 404, "403 would confirm project B exists"
    assert "forbidden" not in refused.text.lower()


def test_a_refusal_body_says_nothing_about_what_was_asked_for() -> None:
    """ "Not found" and nothing else. A message naming the project, the role required, or the reason
    would leak exactly what the 404 was chosen to hide."""
    app = _app_with(_principal(Role.REVIEWER))

    @app.get("/projects/{project_id}/secret")
    async def secret(
        principal: Annotated[Principal, Depends(require_project_access)],
    ) -> dict[str, str]:  # pragma: no cover - exercised via HTTP
        return {"id": principal.id}

    response = TestClient(app, raise_server_exceptions=False).get(f"/projects/{PROJECT_B}/secret")
    assert str(PROJECT_B) not in response.text
    assert "reviewer" not in response.text.lower()


def test_a_refusal_is_logged_with_actor_action_and_target(caplog: pytest.LogCaptureFixture) -> None:
    """The acceptance criterion, and the only record that an attempt happened at all — the response
    is deliberately shaped to look like an absence."""
    app = _app_with(_principal(Role.REVIEWER))

    @app.get("/projects/{project_id}/thing")
    async def thing(
        principal: Annotated[Principal, Depends(require_project_access)],
    ) -> dict[str, str]:  # pragma: no cover - exercised via HTTP
        return {"id": principal.id}

    with caplog.at_level("WARNING", logger="gv.auth"):
        TestClient(app, raise_server_exceptions=False).get(f"/projects/{PROJECT_B}/thing")

    record = next(r for r in caplog.records if r.name == "gv.auth")
    assert record.actor == "anant"  # type: ignore[attr-defined]
    assert record.action == "read_project"  # type: ignore[attr-defined]
    assert record.target == str(PROJECT_B)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Role checks
# ---------------------------------------------------------------------------


def test_a_reviewer_cannot_publish_a_rule() -> None:
    app = _app_with(_principal(Role.REVIEWER))

    @app.post("/rules/publish")
    async def publish(
        principal: Annotated[Principal, Depends(require_action(Action.PUBLISH_RULE))],
    ) -> dict[str, str]:  # pragma: no cover - exercised via HTTP
        return {"id": principal.id}

    assert TestClient(app, raise_server_exceptions=False).post("/rules/publish").status_code == 404


def test_a_rule_admin_can_publish_a_rule() -> None:
    """The check has to be able to say yes, or the refusals prove nothing."""
    app = _app_with(_principal(Role.RULE_ADMIN))

    @app.post("/rules/publish")
    async def publish(
        principal: Annotated[Principal, Depends(require_action(Action.PUBLISH_RULE))],
    ) -> dict[str, str]:  # pragma: no cover - exercised via HTTP
        return {"id": principal.id}

    assert TestClient(app, raise_server_exceptions=False).post("/rules/publish").status_code == 200


def test_require_role_and_require_action_agree() -> None:
    """`require_action` reads the table; `require_role` names roles at the call site. They must not
    diverge, or the table stops being the answer to "who may do this?"."""
    for action, roles in PERMISSIONS.items():
        for role in Role:
            holder = _principal(role)
            assert holder.may(action) == (role in roles), f"{role.value} vs {action.value}"


# ---------------------------------------------------------------------------
# The enumerating test — what keeps this true as the API grows
# ---------------------------------------------------------------------------


def _walk(dependant: object) -> list[object]:
    """Every callable in a route's dependency graph, at any depth.

    Recursive rather than two levels deep. The first version looked at the route's dependencies and
    their immediate children, so a legitimate authorisation dependency nested any deeper read as
    absent — and the audit would have reported a properly guarded route as unguarded, which is the
    failure that gets a guard deleted rather than fixed.
    """
    found: list[object] = []
    for dependency in getattr(dependant, "dependencies", []):
        if dependency.call is not None:
            found.append(dependency.call)
        found.extend(_walk(dependency))
    return found


def _enforcers(route: APIRoute) -> list[object]:
    """The callables that actually enforce authorisation, recognised by mark rather than by name.

    `require_role` and `require_action` both return a closure called `dependency`, so matching on
    `__name__` meant *any* callable with that name satisfied the audit — including one enforcing
    nothing. The marker is set by the module that does the enforcing.
    """
    return [c for c in _walk(route.dependant) if getattr(c, AUTHORISATION_MARKER, False)]


def _guarded(route: APIRoute) -> bool:
    """Whether a route carries any authorisation at all."""
    return bool(_enforcers(route))


def _project_scoped(route: APIRoute) -> bool:
    """Whether a route carries the *project* boundary specifically, by identity.

    Separate from `_guarded`, because the first version conflated them: any dependency counted, so a
    route under `/projects/{project_id}/...` carrying only a role check read as guarded while having
    no project scope at all. A reviewer holding the right role could then reach another project's
    data — the exact failure this story exists to prevent, passing its own enumerating test.

    Compared by identity, not by name: a function called `require_project_access` that checked
    nothing would otherwise satisfy the audit.
    """
    return any(callable_ is require_project_access for callable_ in _walk(route.dependant))


def test_every_route_is_guarded_or_explicitly_exempt() -> None:
    """The test the design names, and the reason this holds as the surface grows.

    An endpoint added without authorisation fails here rather than shipping open. Exempting one is a
    visible edit to `UNSCOPED_ROUTES` — an omission somebody has to write down, rather than one
    nobody sees.
    """
    app = create_app(_settings())
    unguarded = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and route.path not in UNSCOPED_ROUTES and not _guarded(route)
    ]
    assert not unguarded, (
        f"routes with no authorisation and no exemption: {unguarded}. Add a dependency, or add the "
        "path to UNSCOPED_ROUTES with a reason."
    )


def test_a_project_route_must_carry_the_project_boundary_specifically() -> None:
    """A role check is not a scope check. `require_role` says what may be done; only
    `require_project_access` says to whose data — and a route that has the first and not the second
    is reachable across projects by anyone holding the role."""
    app = create_app(_settings())

    @app.get("/projects/{project_id}/role-checked-only")
    async def role_only(
        project_id: str,
        principal: Annotated[Principal, Depends(require_role(Role.REVIEWER))],
    ) -> dict[str, str]:  # pragma: no cover - never called
        return {"project": project_id}

    offenders = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and "{project_id}" in route.path
        and not _project_scoped(route)
    ]
    assert "/projects/{project_id}/role-checked-only" in offenders


def test_every_project_route_carries_the_project_boundary() -> None:
    """The enumerating test, sharpened. It is not enough that a project route has *a* dependency."""
    app = create_app(_settings())
    unscoped = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and "{project_id}" in route.path
        and not _project_scoped(route)
    ]
    assert not unscoped, (
        f"project routes without require_project_access: {unscoped}. A role check says what may be "
        "done, not to whose data."
    )


def test_an_action_mapped_to_no_roles_is_refused_at_import() -> None:
    """An empty set passes a presence check, reads as deliberate, and means nobody may ever take the
    action — indistinguishable from a broken endpoint."""
    import app.auth.roles as roles_module

    for action, allowed in roles_module.PERMISSIONS.items():
        assert allowed, f"{action.value} is mapped to an empty role set"


def test_the_exemptions_are_only_the_operational_endpoints() -> None:
    """A growing exemption list is how this guarantee erodes: each entry is defensible on its own and
    the set stops meaning anything. Health and readiness carry no project data."""
    assert UNSCOPED_ROUTES == {
        "/health",
        "/ready",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }


def test_an_unguarded_route_is_actually_caught() -> None:
    """The enumerating test asserting nothing would look identical on a green run. This proves it
    fails when it should."""
    app = create_app(_settings())

    @app.get("/projects/{project_id}/forgot-the-guard")
    async def forgot(project_id: str) -> dict[str, str]:  # pragma: no cover - never called
        return {"project": project_id}

    unguarded = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and route.path not in UNSCOPED_ROUTES and not _guarded(route)
    ]
    assert "/projects/{project_id}/forgot-the-guard" in unguarded


def test_a_lookalike_dependency_does_not_satisfy_the_audit() -> None:
    """Matching on `__name__` meant any callable with the right name counted. This one enforces
    nothing and is named to look like it does."""
    app = create_app(_settings())

    # `_impostor` is defined at module level and named `require_project_access`. It has to be
    # module-level: `from __future__ import annotations` makes the `Depends(...)` inside an
    # `Annotated[...]` a string resolved from module globals, so a shadow defined here would be
    # invisible and FastAPI would resolve the real function — which is what made the first version
    # of this test pass for the wrong reason.
    @app.get("/projects/{project_id}/impostor")
    async def impostor(
        _: Annotated[str, Depends(_impostor)],
    ) -> dict[str, str]:  # pragma: no cover - never called
        return {}

    offenders = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and "{project_id}" in route.path
        and not _project_scoped(route)
    ]
    assert "/projects/{project_id}/impostor" in offenders


def test_a_deeply_nested_authorisation_dependency_is_still_found() -> None:
    """The audit walks the whole graph. A two-level look would report a properly guarded route as
    unguarded — the failure that gets a guard deleted rather than fixed."""
    app = create_app(_settings())

    @app.get("/projects/{project_id}/deep")
    async def deep(
        principal: Annotated[Principal, Depends(_layer_two)],
    ) -> dict[str, str]:  # pragma: no cover - never called
        return {"id": principal.id}

    route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path.endswith("/deep"))
    assert _project_scoped(route) and _guarded(route)


def test_the_permission_validator_refuses_an_empty_role_set() -> None:
    """The validator itself, not the shipped mapping. The previous test asserted only that today's
    table is fine, so it would have passed with the validator deleted."""
    from app.auth.roles import validate_permissions

    with pytest.raises(RuntimeError, match="no roles assigned"):
        validate_permissions({action: frozenset() for action in Action})


def test_the_permission_validator_refuses_a_missing_action() -> None:
    from app.auth.roles import validate_permissions

    partial = {action: frozenset({Role.ADMIN}) for action in Action}
    partial.pop(Action.PUBLISH_RULE)
    with pytest.raises(RuntimeError, match="publish_rule"):
        validate_permissions(partial)


def test_the_validator_accepts_the_shipped_table() -> None:
    """It has to be able to say yes, or the refusals prove nothing."""
    from app.auth.roles import validate_permissions

    validate_permissions(PERMISSIONS)
