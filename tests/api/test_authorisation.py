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

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.utils import get_dependant
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


@dataclass(frozen=True, slots=True)
class _Mounted:
    """One route the application will actually serve, with everything that guards it."""

    path: str
    """The full served path, every enclosing prefix applied. `route.path` alone is the path *within*
    its router, so a route included under `/api/v1` would be audited under the wrong name."""

    route: APIRoute
    inherited: tuple[object, ...]
    """Callables from `include_router(dependencies=[...])`. These do **not** appear in the route's
    own dependant, so a router guarded at include time would read as completely unguarded."""


def _from_depends(depends: Sequence[object], path: str) -> list[object]:
    """Every callable reachable from a list of `Depends(...)` markers, at any depth."""
    found: list[object] = []
    for marker in depends:
        call = getattr(marker, "dependency", None)
        if call is None:
            continue
        found.append(call)
        found.extend(_walk(get_dependant(path=path, call=call)))
    return found


def _mounted_routes(app: FastAPI) -> list[_Mounted]:
    """Every `APIRoute` the app serves, including those reached through `include_router`.

    **This is the fix for a guard that would have stopped guarding the moment the API grew.** On this
    version of FastAPI, `include_router` no longer flattens the child routes into `app.routes`. It
    appends one opaque object and resolves children at match time. So the previous enumeration —
    `for route in app.routes if isinstance(route, APIRoute)` — saw *nothing* from any included
    router, and every test built on it would have passed vacuously while the API filled up with
    unaudited endpoints. Verified directly: after one `include_router`, `app.routes` contains zero
    `APIRoute` objects.

    It went unnoticed because `app/main.py` currently declares its routes with `@app.get`, which
    still produces real `APIRoute` entries. The guard works today and would have quietly stopped
    working on the first wired router — which is exactly when it starts to matter.
    """

    def descend(
        routes: Iterable[object], prefix: str, inherited: tuple[object, ...]
    ) -> list[_Mounted]:
        found: list[_Mounted] = []
        for route in routes:
            context = getattr(route, "include_context", None)
            if context is not None:
                inner = getattr(route, "original_router", None)
                found.extend(
                    descend(
                        getattr(inner, "routes", []),
                        prefix + getattr(context, "prefix", ""),
                        inherited
                        + tuple(_from_depends(getattr(context, "dependencies", []), prefix)),
                    )
                )
            elif isinstance(route, APIRoute):
                found.append(_Mounted(prefix + route.path, route, inherited))
        return found

    return descend(app.routes, "", ())


def _enforcers(mounted: _Mounted) -> list[object]:
    """The callables that actually enforce authorisation, recognised by mark rather than by name.

    `require_role` and `require_action` both return a closure called `dependency`, so matching on
    `__name__` meant *any* callable with that name satisfied the audit — including one enforcing
    nothing. The marker is set by the module that does the enforcing.
    """
    reachable = _walk(mounted.route.dependant) + list(mounted.inherited)
    return [c for c in reachable if getattr(c, AUTHORISATION_MARKER, False)]


def _guarded(mounted: _Mounted) -> bool:
    """Whether a route carries any authorisation at all."""
    return bool(_enforcers(mounted))


def _project_scoped(mounted: _Mounted) -> bool:
    """Whether a route carries the *project* boundary specifically, by identity.

    Separate from `_guarded`, because the first version conflated them: any dependency counted, so a
    route under `/projects/{project_id}/...` carrying only a role check read as guarded while having
    no project scope at all. A reviewer holding the right role could then reach another project's
    data — the exact failure this story exists to prevent, passing its own enumerating test.

    Compared by identity, not by name: a function called `require_project_access` that checked
    nothing would otherwise satisfy the audit.
    """
    reachable = _walk(mounted.route.dependant) + list(mounted.inherited)
    return any(callable_ is require_project_access for callable_ in reachable)


def test_every_route_is_guarded_or_explicitly_exempt() -> None:
    """The test the design names, and the reason this holds as the surface grows.

    An endpoint added without authorisation fails here rather than shipping open. Exempting one is a
    visible edit to `UNSCOPED_ROUTES` — an omission somebody has to write down, rather than one
    nobody sees.
    """
    app = create_app(_settings())
    unguarded = [
        mounted.path
        for mounted in _mounted_routes(app)
        if mounted.path not in UNSCOPED_ROUTES and not _guarded(mounted)
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
        mounted.path
        for mounted in _mounted_routes(app)
        if "{project_id}" in mounted.path and not _project_scoped(mounted)
    ]
    assert "/projects/{project_id}/role-checked-only" in offenders


def test_every_project_route_carries_the_project_boundary() -> None:
    """The enumerating test, sharpened. It is not enough that a project route has *a* dependency."""
    app = create_app(_settings())
    unscoped = [
        mounted.path
        for mounted in _mounted_routes(app)
        if "{project_id}" in mounted.path and not _project_scoped(mounted)
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
        mounted.path
        for mounted in _mounted_routes(app)
        if mounted.path not in UNSCOPED_ROUTES and not _guarded(mounted)
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
        mounted.path
        for mounted in _mounted_routes(app)
        if "{project_id}" in mounted.path and not _project_scoped(mounted)
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

    route = next(m for m in _mounted_routes(app) if m.path.endswith("/deep"))
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


def test_importing_the_module_is_what_runs_the_validator() -> None:
    """The three tests above call the validator by hand, so all three still pass if the call at the
    bottom of `roles.py` is deleted — and then a table with a hole in it ships silently.

    This runs the module itself, with the permission table replaced by an empty one before execution.
    The module must refuse to import. Rewriting the source rather than asserting the call is present
    is the difference between checking the mechanism works and checking it is spelled correctly:
    a call that had been commented out, moved above the table, or wrapped in a swallowed exception
    would satisfy a text search and fail here.
    """
    import ast

    source = Path("app/auth/roles.py").read_text()
    tree = ast.parse(source)
    replaced = False
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(target, ast.Name) and target.id == "PERMISSIONS":
            node.value = ast.Dict(keys=[], values=[])
            replaced = True
    assert replaced, "PERMISSIONS is no longer a module-level annotated assignment"

    with pytest.raises(RuntimeError, match="no roles assigned|has no entry"):
        exec(  # noqa: S102 - executing our own source with one literal swapped is the point
            compile(ast.fix_missing_locations(tree), "app/auth/roles.py", "exec"),
            {"__name__": "app.auth.roles_under_test"},
        )


# ---------------------------------------------------------------------------
# Routes reached through include_router — where this guard silently stopped working
# ---------------------------------------------------------------------------


def test_a_route_behind_include_router_is_visible_to_the_audit() -> None:
    """**The guard was about to stop guarding.** On this FastAPI, `include_router` no longer flattens
    child routes into `app.routes` — it appends one opaque object and resolves children at match
    time. The audit filtered `app.routes` for `APIRoute`, so an included router contributed *nothing*
    and every enumerating test below would have passed while saying nothing at all.

    It stayed hidden because `app/main.py` declares its routes with `@app.get`, which still produces
    real `APIRoute` entries, so the guard worked right up until the first router was wired — which is
    exactly when the API starts being worth guarding.
    """
    app = create_app(_settings())
    router = APIRouter()

    @router.get("/projects/{project_id}/included")
    async def included(
        principal: Annotated[Principal, Depends(require_project_access)],
    ) -> dict[str, str]:  # pragma: no cover - never called
        return {"id": principal.id}

    app.include_router(router, prefix="/api/v1")

    paths = [mounted.path for mounted in _mounted_routes(app)]
    assert (
        "/api/v1/projects/{project_id}/included" in paths
    ), "the audit cannot see routes added through include_router, so it is auditing an empty set"


def test_an_unguarded_route_behind_include_router_is_caught() -> None:
    """Visibility is not the point on its own — being *caught* is. This is the failure the whole file
    exists to prevent, arriving by the route the old enumeration could not see."""
    app = create_app(_settings())
    router = APIRouter()

    @router.get("/projects/{project_id}/forgotten")
    async def forgotten(project_id: str) -> dict[str, str]:  # pragma: no cover - never called
        return {"project": project_id}

    app.include_router(router, prefix="/api/v1")

    unguarded = [
        mounted.path
        for mounted in _mounted_routes(app)
        if mounted.path not in UNSCOPED_ROUTES and not _guarded(mounted)
    ]
    assert "/api/v1/projects/{project_id}/forgotten" in unguarded


def test_authorisation_applied_at_include_time_counts_as_guarded() -> None:
    """`include_router(dependencies=[...])` is a legitimate way to guard a whole router, and those
    dependencies do **not** appear in the child route's own dependant — I checked. Missing them would
    report a properly guarded router as wide open, and a guard that cries wolf is one somebody
    deletes rather than fixes.
    """
    app = create_app(_settings())
    router = APIRouter()

    @router.get("/projects/{project_id}/guarded-by-the-router")
    async def guarded_by_router(project_id: str) -> dict[str, str]:  # pragma: no cover
        return {"project": project_id}

    app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_project_access)])

    mounted = next(m for m in _mounted_routes(app) if m.path.endswith("/guarded-by-the-router"))
    assert _guarded(mounted)
    assert _project_scoped(mounted)


def test_a_router_nested_inside_another_router_is_still_reached() -> None:
    """Routers include routers. One level of recursion would have been the same bug one layer down."""
    app = create_app(_settings())
    inner, outer = APIRouter(), APIRouter()

    @inner.get("/projects/{project_id}/deep-include")
    async def deep(project_id: str) -> dict[str, str]:  # pragma: no cover - never called
        return {"project": project_id}

    outer.include_router(inner, prefix="/inner")
    app.include_router(outer, prefix="/api/v1")

    paths = [mounted.path for mounted in _mounted_routes(app)]
    assert "/api/v1/inner/projects/{project_id}/deep-include" in paths


def test_the_wired_api_is_audited_not_merely_present() -> None:
    """The first real router, asserted through the enumeration rather than by eye.

    This is the test that would have been impossible to write honestly before the include_router fix:
    the route below is reached through `include_router`, so the previous audit could not see it at
    all and would have reported an app with zero project routes. It now appears under the path it is
    actually served on, prefix and all, and is required to carry the project boundary.
    """
    app = create_app(_settings())
    project_routes = [m for m in _mounted_routes(app) if "{project_id}" in m.path]

    assert project_routes, "the findings router is wired, so the audit must see project routes"
    assert all(_project_scoped(m) for m in project_routes), [
        m.path for m in project_routes if not _project_scoped(m)
    ]
    assert any(
        m.path.startswith("/api/v1/") for m in project_routes
    ), "a route audited without its prefix is audited under a path nobody serves"
