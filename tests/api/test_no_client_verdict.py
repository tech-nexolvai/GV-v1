"""No route accepts a client-supplied verdict, and the audit that keeps it that way (#207, C2.5).

Backend §10.2, quoted in `docs/DESIGN_PLATFORM.md` §4.2: *"The API never accepts a client-provided
PASS/FAIL calculation."* This file is the half of that sentence that stays true as the API grows from
seven endpoints to six route groups.

The tests are mostly about failure. A guard whose tests only prove that today's clean app is clean
would look identical on a green run with the guard deleted — so most of what follows builds a
deliberately unsafe route and insists it is caught: in a body, in a query string, nested two models
deep, wrapped in `Optional` and `list`, behind a dependency, behind `include_router`, and named like a
verdict while typed as a `bool`.

Every request model here is defined at **module level**, and that is load-bearing rather than a style
choice. `from __future__ import annotations` makes an endpoint's annotations strings that FastAPI
resolves from module globals, so a model defined inside a test function is invisible to it — the
mistake that made the first version of the authorisation audit pass for the wrong reason.

Source: backend proposal §10.2, §11 · Design: `docs/DESIGN_PLATFORM.md` §4.2 ·
Verification: this file
"""

from __future__ import annotations

from typing import Annotated, Optional
from uuid import UUID

import pytest
from fastapi import APIRouter, Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict

from app.api.guards import (
    FORBIDDEN_FIELD_NAMES,
    FORBIDDEN_REQUEST_TYPES,
    GOLDEN_RULE,
    READ_ONLY_FILTERS,
    SAFE_METHODS,
    ClientVerdictAccepted,
    ReadOnlyFilter,
    assert_no_verdict_fields,
    mounted_routes,
    names_like_a_verdict,
    offences,
    unused_read_only_filters,
    validate_read_only_filters,
)
from app.config import Settings
from app.main import create_app
from rules.schema import Tolerance
from units.measurement import Measurement
from verdict.outcomes import Outcome, Severity

DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"

FINDINGS_PATH = "/api/v1/projects/{project_id}/packages/{package_id}/findings"


def _settings() -> Settings:
    return Settings(database_url=DATABASE_URL)  # type: ignore[call-arg]


def _app() -> FastAPI:
    return create_app(_settings())


# ---------------------------------------------------------------------------
# Request models, all at module level so FastAPI can resolve them
# ---------------------------------------------------------------------------


class SubmittedVerdict(BaseModel):
    """The thing this whole story exists to refuse: a caller stating the conclusion."""

    finding_id: UUID
    outcome: Outcome


class DeepInner(BaseModel):
    allowed: Tolerance


class DeepMiddle(BaseModel):
    inner: DeepInner


class DeepOuter(BaseModel):
    """A `Tolerance` two models down. A top-level-only check would call this clean."""

    middle: DeepMiddle


class OptionalOutcome(BaseModel):
    """The old spelling on purpose, which is why the lint is silenced rather than obeyed.

    `Optional[Outcome]` is a `typing.Union` and `Outcome | None` is a `types.UnionType` — different
    runtime objects, so a walk that handles one is not proof it handles the other. Both are covered:
    this model, and the `Outcome | None` query parameters further down.
    """

    decided: Optional[Outcome] = None  # noqa: UP045 - see the docstring


class NewStyleOptionalOutcome(BaseModel):
    decided: Outcome | None = None


class ListOfOutcomes(BaseModel):
    decided: list[Outcome] = []


class MappedSeverities(BaseModel):
    by_rule: dict[str, Severity] = {}


class MeasuredValue(BaseModel):
    """`Measurement` is a frozen dataclass rather than a model, so the walk has to handle both."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    measured: Measurement


class ProposedTolerance(Tolerance):
    """A subclass of a forbidden type. It is the forbidden type with a different name on it."""


class SubclassedTolerance(BaseModel):
    """**The hole an identity-only check leaves open.**

    `ProposedTolerance` is not `Tolerance` by identity, so a check comparing classes for equality
    would walk its *fields* instead — `value` and `unit`, neither of which is named like a verdict —
    and report the route as clean.
    """

    allowed: ProposedTolerance


class InnocentlyTypedVerdict(BaseModel):
    """No forbidden type anywhere. `passed: bool` is a client-supplied PASS/FAIL regardless."""

    finding_id: UUID
    passed: bool


class Comment(BaseModel):
    """Self-referential, so the walk has to stop itself rather than run out of stack."""

    body: str
    replies: list[Comment] = []


#: The self-reference cannot be resolved while the class body is running, so pydantic leaves the
#: annotation as a forward reference until asked. Resolved here, because the audit reads
#: `model_fields` and an unresolved reference would make it skip the very field under test.
Comment.model_rebuild()


class Lookalikes(BaseModel):
    """Names that contain the forbidden letters and mean nothing of the kind.

    A guard that flags these is one somebody switches off, and then it is protecting nothing.
    """

    bypassed: bool = False
    password: str = ""
    passenger_count: int = 0


class LocalOutcome(BaseModel):
    """Named exactly like the real thing and carrying whatever the client sent.

    The forbidden types are matched by identity, so this must not satisfy the audit by having the
    right `__name__` — and must not be *refused* either, since a name check is not what is protecting
    us here. It is clean, and the audit has to say so for the right reason.
    """

    value: int


LocalOutcome.__name__ = "Outcome"


class ReviewActionIn(BaseModel):
    """What a reviewer action is allowed to look like: identifiers of server-side rows.

    `docs/DESIGN_PLATFORM.md` §4.2 and `app/models/review.py`: an action names the finding revision it
    is about, and the outcome is looked up rather than accepted. The acceptance criterion for this
    story is that reviewer actions reference server-side finding revisions and never a submitted
    result — so this model passing is as much a part of the guard as the bad ones failing.
    """

    finding_id: UUID
    package_revision_id: UUID
    action: str
    note: str | None = None


# ---------------------------------------------------------------------------
# Dependencies, also module level — a `Depends` inside an `Annotated` is resolved from globals
# ---------------------------------------------------------------------------


def _shared_outcome_filter(
    outcome: Annotated[list[Outcome] | None, Query()] = None,
) -> list[Outcome] | None:
    """A filter factored into a shared dependency — the realistic way an unsafe field spreads."""
    return outcome


def _harmless_dependency(limit: Annotated[int, Query(ge=1)] = 10) -> int:
    return limit


# ---------------------------------------------------------------------------
# The shipped API
# ---------------------------------------------------------------------------


def test_the_shipped_api_accepts_no_verdict() -> None:
    """The property the story asks for, on the real routes."""
    assert_no_verdict_fields(_app())


def test_the_factory_would_refuse_to_build_an_unsafe_api() -> None:
    """**The startup half of the guard, proven by making the real app unsafe.**

    With the exemptions removed, the shipped findings filters are offences — so if `create_app` did
    not call the guard, this would pass silently and the whole check would be test-only. Rewriting the
    exemption list rather than asserting the call is present is the difference between checking the
    mechanism runs and checking it is spelled correctly.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("app.api.guards.READ_ONLY_FILTERS", ())
        with pytest.raises(ClientVerdictAccepted):
            _app()


def test_the_audit_can_see_the_wired_routers() -> None:
    """A vacuous audit and a passing audit look identical. This is what separates them.

    On this FastAPI, `include_router` appends one opaque object rather than flattening children into
    `app.routes`, so an enumeration that filtered `app.routes` for `APIRoute` would inspect an empty
    set and pass. The routes have to appear under the prefix they are actually served on, too — a
    route audited without its prefix is exempted under a path nobody serves.
    """
    paths = [mounted.path for mounted in mounted_routes(_app())]

    assert FINDINGS_PATH in paths, "the audit cannot see routes behind include_router"
    assert sum(path.startswith("/api/v1/") for path in paths) >= 5


# ---------------------------------------------------------------------------
# The exemptions are load-bearing, and cannot be stretched
# ---------------------------------------------------------------------------


def test_the_findings_filters_would_be_refused_without_their_exemption() -> None:
    """Proof the two entries in `READ_ONLY_FILTERS` are doing real work.

    If these fields were not offences in the first place, the exemptions would be decoration and a
    later reader would conclude the strict default is not really strict.

    The app is built *before* the exemptions are removed, because building it is what runs the startup
    assertion — patch first and the factory raises before the audit can be inspected.
    """
    app = _app()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("app.api.guards.READ_ONLY_FILTERS", ())
        refused = {(o.path, o.field) for o in offences(app)}

    assert (FINDINGS_PATH, "outcome") in refused
    assert (FINDINGS_PATH, "severity") in refused


def test_every_shipped_exemption_excuses_something_that_exists() -> None:
    """A stale exemption reads as a considered decision about a route that has gone, and the next
    author renaming a filter finds a list that appears to have thought about their case."""
    assert unused_read_only_filters(_app()) == []


def test_a_stale_exemption_is_reported() -> None:
    """The check above has to be able to fail, or it is asserting that a list is a list."""
    stale = ReadOnlyFilter(
        method="GET", path="/api/v1/gone", field="outcome", reason="route was removed"
    )
    app = _app()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("app.api.guards.READ_ONLY_FILTERS", (stale,))
        assert unused_read_only_filters(app) == [stale]


def test_an_exemption_does_not_cover_the_same_field_on_a_write() -> None:
    """**The hole an exemption keyed on the path alone would open.** The findings filter is exempt on
    `GET`. A `POST` to the same path taking the same query field is a caller supplying a verdict, and
    the exemption must not reach it."""
    app = _app()
    router = APIRouter()

    @router.post(FINDINGS_PATH.removeprefix("/api/v1"))
    async def submit(
        outcome: Annotated[Outcome | None, Query()] = None,
    ) -> dict[str, str]:  # pragma: no cover - never called
        return {}

    app.include_router(router, prefix="/api/v1")

    refused = [o for o in offences(app) if o.method == "POST" and o.path == FINDINGS_PATH]
    assert refused, "a write reusing an exempted read's field slipped through"


def test_a_body_field_is_never_exempt() -> None:
    """An exemption excuses reading a value back out. Nothing arriving in a body is being read back,
    so the same field name on the same exempted route is still refused when it is in the body."""
    app = _app()
    router = APIRouter()

    @router.get(FINDINGS_PATH.removeprefix("/api/v1"))
    async def read_with_body(
        outcome: SubmittedVerdict,
    ) -> dict[str, str]:  # pragma: no cover - never called
        return {}

    app.include_router(router, prefix="/api/v1")

    assert [o for o in offences(app) if o.location == "body" and o.path == FINDINGS_PATH]


def test_only_a_safe_method_may_be_exempted() -> None:
    """Refused at import, so the module will not load with an exemption that could excuse a write."""
    unsafe = ReadOnlyFilter(method="POST", path="/x", field="outcome", reason="filters a list")
    with pytest.raises(RuntimeError, match="unsafe method"):
        validate_read_only_filters([unsafe])


def test_an_exemption_without_a_reason_is_refused() -> None:
    """An exemption nobody justified is one nobody can review, and it is the first of many."""
    with pytest.raises(RuntimeError, match="no reason"):
        validate_read_only_filters(
            [ReadOnlyFilter(method="GET", path="/x", field="outcome", reason="   ")]
        )


def test_the_same_field_cannot_be_exempted_twice() -> None:
    """Two reasons for one field means the one that applies depends on list order."""
    entry = ReadOnlyFilter(method="GET", path="/x", field="outcome", reason="first reason")
    twice = ReadOnlyFilter(method="GET", path="/x", field="outcome", reason="second reason")
    with pytest.raises(RuntimeError, match="exempted twice"):
        validate_read_only_filters([entry, twice])


def test_the_validator_accepts_the_shipped_exemptions() -> None:
    """It has to be able to say yes, or the refusals above prove nothing."""
    validate_read_only_filters(READ_ONLY_FILTERS)


def test_the_exemptions_are_only_the_findings_filters() -> None:
    """A growing list is how this erodes — each entry defensible alone until the set means nothing.
    Two read-only filters today; adding a third should be a decision somebody sees in review."""
    assert {(f.method, f.path, f.field) for f in READ_ONLY_FILTERS} == {
        ("GET", FINDINGS_PATH, "outcome"),
        ("GET", FINDINGS_PATH, "severity"),
    }


# ---------------------------------------------------------------------------
# A forbidden type, in every wrapper it can hide in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (SubmittedVerdict, "payload.outcome"),
        (DeepOuter, "payload.middle.inner.allowed"),
        (SubclassedTolerance, "payload.allowed"),
        (OptionalOutcome, "payload.decided"),
        (NewStyleOptionalOutcome, "payload.decided"),
        (ListOfOutcomes, "payload.decided"),
        (MappedSeverities, "payload.by_rule"),
        (MeasuredValue, "payload.measured"),
        (InnocentlyTypedVerdict, "payload.passed"),
    ],
    ids=[
        "a-top-level-outcome",
        "a-tolerance-two-models-down",
        "a-subclass-of-a-forbidden-type",
        "an-optional-outcome-typing-union",
        "an-optional-outcome-pep-604",
        "a-list-of-outcomes",
        "a-dict-of-severities",
        "a-measurement-dataclass",
        "a-bool-named-passed",
    ],
)
def test_an_unsafe_body_field_is_caught(model: type[BaseModel], expected: str) -> None:
    """Every wrapper a verdict can arrive in, caught under the dotted path a fix would need.

    `Optional`, `list` and `dict` are the cases a top-level `isinstance` check misses, and nesting is
    the case a one-level walk misses. A caller does not care which wrapper carried the value.
    """
    app = _app()
    router = APIRouter()
    router.add_api_route(
        "/deliberately-unsafe",
        _endpoint_taking(model),
        methods=["POST"],
    )
    app.include_router(router)

    refused = {o.field for o in offences(app) if o.path == "/deliberately-unsafe"}
    assert expected in refused, refused


def _endpoint_taking(model: type[BaseModel]) -> object:
    """An endpoint whose only parameter is `payload`, typed as the model under test.

    Built with a real annotation rather than a decorator per case, so the parametrised test above
    exercises FastAPI's own parameter analysis — the same code path a hand-written endpoint takes.
    """

    async def endpoint(payload: model) -> dict[str, str]:  # type: ignore[valid-type]
        return {}  # pragma: no cover - never called

    endpoint.__annotations__["payload"] = model
    return endpoint


def test_a_self_referential_model_does_not_recurse_for_ever() -> None:
    """`replies: list[Comment]` inside `Comment`. Without cycle protection the walk never returns,
    and the guard takes the process down at startup instead of protecting it."""
    app = _app()
    router = APIRouter()
    router.add_api_route("/comments", _endpoint_taking(Comment), methods=["POST"])
    app.include_router(router)

    assert not [o for o in offences(app) if o.path == "/comments"]


def test_a_forbidden_type_is_matched_by_identity_not_by_name() -> None:
    """A local class called `Outcome` carrying an `int` is not the verdict enum, and flagging it would
    teach authors that the guard is about naming. It is about which type the value came from."""
    app = _app()
    router = APIRouter()
    router.add_api_route("/local-outcome", _endpoint_taking(LocalOutcome), methods=["POST"])
    app.include_router(router)

    assert not [o for o in offences(app) if o.path == "/local-outcome"]


def test_names_that_merely_look_similar_are_not_flagged() -> None:
    """`bypassed`, `password`, `passenger_count`. A guard that cries wolf gets switched off."""
    app = _app()
    router = APIRouter()
    router.add_api_route("/lookalikes", _endpoint_taking(Lookalikes), methods=["POST"])
    app.include_router(router)

    assert not [o for o in offences(app) if o.path == "/lookalikes"]


@pytest.mark.parametrize(
    "name", ["outcome", "verdict", "passed", "tolerance", "expected_value", "expected_value_mm"]
)
def test_a_verdict_shaped_name_is_recognised(name: str) -> None:
    assert names_like_a_verdict(name)


@pytest.mark.parametrize("name", ["bypassed", "password", "passenger", "value_expected_by_nobody"])
def test_an_unrelated_name_is_not(name: str) -> None:
    assert not names_like_a_verdict(name)


def test_a_prefixed_verdict_name_is_still_a_verdict_name() -> None:
    """The realistic way this arrives: not `outcome` but `client_outcome` or `verdict_outcome`."""
    assert names_like_a_verdict("client_outcome")
    assert names_like_a_verdict("verdict_outcome")
    assert names_like_a_verdict("suggested_tolerance")


# ---------------------------------------------------------------------------
# Where the field is declared, rather than what it is
# ---------------------------------------------------------------------------


def test_an_unsafe_query_field_on_a_write_is_caught() -> None:
    """A `POST` taking `?outcome=PASS` accepts a client verdict as completely as one taking a body."""
    app = _app()
    router = APIRouter()

    @router.post("/query-verdict")
    async def query_verdict(
        outcome: Annotated[Outcome | None, Query()] = None,
    ) -> dict[str, str]:  # pragma: no cover - never called
        return {}

    app.include_router(router)

    assert [o for o in offences(app) if o.path == "/query-verdict" and o.location == "query"]


def test_an_unsafe_field_behind_a_dependency_is_caught() -> None:
    """A shared dependency is exactly where a filter gets factored out to, and one unsafe dependency
    adds the same field to every endpoint using it. Looking only at a route's own parameters would
    miss all of them at once."""
    app = _app()
    router = APIRouter()

    @router.post("/via-dependency")
    async def via_dependency(
        chosen: Annotated[list[Outcome] | None, Depends(_shared_outcome_filter)] = None,
    ) -> dict[str, str]:  # pragma: no cover - never called
        return {}

    app.include_router(router)

    assert [o for o in offences(app) if o.path == "/via-dependency"]


def test_an_unsafe_field_on_a_router_level_dependency_is_caught() -> None:
    """`include_router(dependencies=[...])` parameters do not appear in the child route's own
    dependant, so a router guarded — or compromised — at include time reads as having no fields."""
    app = _app()
    router = APIRouter()

    @router.post("/router-level")
    async def router_level() -> dict[str, str]:  # pragma: no cover - never called
        return {}

    app.include_router(router, dependencies=[Depends(_shared_outcome_filter)])

    assert [o for o in offences(app) if o.path == "/router-level"]


def test_a_route_behind_include_router_is_caught() -> None:
    """The failure mode that would make everything above vacuous: an unsafe route arriving by the one
    path the enumeration could not see."""
    app = _app()
    inner, outer = APIRouter(), APIRouter()
    inner.add_api_route("/nested-unsafe", _endpoint_taking(SubmittedVerdict), methods=["POST"])
    outer.include_router(inner, prefix="/inner")
    app.include_router(outer, prefix="/api/v1")

    refused = {o.path for o in offences(app)}
    assert "/api/v1/inner/nested-unsafe" in refused


def test_a_harmless_dependency_is_left_alone() -> None:
    """The audit has to be able to say yes about the ordinary case, or it says nothing about any."""
    app = _app()
    router = APIRouter()

    @router.post("/paged")
    async def paged(
        limit: Annotated[int, Depends(_harmless_dependency)] = 10,
    ) -> dict[str, str]:  # pragma: no cover - never called
        return {}

    app.include_router(router)

    assert not [o for o in offences(app) if o.path == "/paged"]


# ---------------------------------------------------------------------------
# Reviewer actions
# ---------------------------------------------------------------------------


def test_a_reviewer_action_may_reference_a_finding_revision() -> None:
    """The acceptance criterion, in its allowed form. `finding_id` plus `package_revision_id` names a
    server-side row and the outcome is looked up — `app/models/review.py` is built the same way."""
    app = _app()
    router = APIRouter()
    router.add_api_route("/review/actions", _endpoint_taking(ReviewActionIn), methods=["POST"])
    app.include_router(router)

    assert not [o for o in offences(app) if o.path == "/review/actions"]


def test_a_reviewer_action_may_not_submit_a_result() -> None:
    """The same endpoint with the conclusion supplied instead of referenced. This is the shape the
    criterion rules out, and the only difference is which of the two models it takes."""
    app = _app()
    router = APIRouter()
    router.add_api_route("/review/actions", _endpoint_taking(SubmittedVerdict), methods=["POST"])
    app.include_router(router)

    refused = [o for o in offences(app) if o.path == "/review/actions"]
    assert refused and refused[0].field == "payload.outcome"


def test_a_reviewer_action_may_not_submit_a_bare_boolean_either() -> None:
    """`passed: bool` carries no forbidden type at all, and is a submitted result all the same."""
    app = _app()
    router = APIRouter()
    router.add_api_route(
        "/review/approvals", _endpoint_taking(InnocentlyTypedVerdict), methods=["POST"]
    )
    app.include_router(router)

    assert [o for o in offences(app) if o.path == "/review/approvals"]


# ---------------------------------------------------------------------------
# What the failure says
# ---------------------------------------------------------------------------


def test_the_failure_explains_the_golden_rule_not_the_schema() -> None:
    """The acceptance criterion, and the reason it is one. The obvious fix for "forbidden type in
    request model" is to rename the field or loosen the type, and that fix leaves the hole open while
    making the guard quiet. The message has to say what the rule is protecting."""
    app = _app()
    router = APIRouter()
    router.add_api_route("/unsafe", _endpoint_taking(SubmittedVerdict), methods=["POST"])
    app.include_router(router)

    with pytest.raises(ClientVerdictAccepted) as raised:
        assert_no_verdict_fields(app)

    message = str(raised.value)
    assert "deterministic Python decides" in message
    assert "reviewer signs off" in message
    assert "never a value a caller hands us" in message


def test_the_failure_names_the_route_the_method_and_the_field() -> None:
    """An author reading this has to know where to go. A count of violations sends them looking."""
    app = _app()
    router = APIRouter()
    router.add_api_route("/unsafe-again", _endpoint_taking(DeepOuter), methods=["PATCH"])
    app.include_router(router)

    with pytest.raises(ClientVerdictAccepted) as raised:
        assert_no_verdict_fields(app)

    message = str(raised.value)
    assert "PATCH" in message
    assert "/unsafe-again" in message
    assert "payload.middle.inner.allowed" in message, "the nested path is what a fix needs"


def test_the_failure_says_how_to_exempt_a_genuine_filter() -> None:
    """Without this, the only discoverable way past the guard is to weaken it."""
    app = _app()
    router = APIRouter()
    router.add_api_route("/still-unsafe", _endpoint_taking(SubmittedVerdict), methods=["POST"])
    app.include_router(router)

    with pytest.raises(ClientVerdictAccepted, match="READ_ONLY_FILTERS"):
        assert_no_verdict_fields(app)


def test_every_offence_is_reported_not_only_the_first() -> None:
    """An author who has to rerun the process per field fixes one and learns nothing about the rest."""
    app = _app()
    router = APIRouter()
    router.add_api_route("/two-a", _endpoint_taking(SubmittedVerdict), methods=["POST"])
    router.add_api_route("/two-b", _endpoint_taking(MappedSeverities), methods=["POST"])
    app.include_router(router)

    with pytest.raises(ClientVerdictAccepted) as raised:
        assert_no_verdict_fields(app)

    assert "/two-a" in str(raised.value) and "/two-b" in str(raised.value)


# ---------------------------------------------------------------------------
# The lists themselves
# ---------------------------------------------------------------------------


def test_the_forbidden_types_are_the_verdict_plane_ones() -> None:
    """Named here so a removal is a visible edit rather than a quiet narrowing of the guard."""
    assert set(FORBIDDEN_REQUEST_TYPES) == {Outcome, Severity, Tolerance, Measurement}


def test_the_forbidden_names_cover_the_untyped_ways_to_say_pass() -> None:
    assert FORBIDDEN_FIELD_NAMES >= {"outcome", "verdict", "passed", "tolerance", "expected_value"}


def test_only_methods_that_cannot_change_anything_are_safe() -> None:
    """`POST`, `PUT`, `PATCH` and `DELETE` must never be in here — an exemption on any of them is a
    write accepting a verdict."""
    assert SAFE_METHODS == {"GET", "HEAD"}


def test_the_golden_rule_text_is_the_rule_not_a_summary() -> None:
    """It is quoted into every failure, so it is worth asserting it still says the whole thing."""
    for phrase in ("AI reads", "evidence qualifies", "deterministic Python decides", "signs off"):
        assert phrase in GOLDEN_RULE
