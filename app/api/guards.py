"""No route accepts a client-supplied verdict, enforced by walking the whole surface (#207, C2.5).

`docs/DESIGN_PLATFORM.md` §4.2, quoting backend §10.2: *"The API never accepts a client-provided
PASS/FAIL calculation."* This module is what makes that a property of the API rather than a sentence
in a document.

**A guard each author must remember is a guard that will eventually be forgotten.** So this one does
not live in the endpoints. It walks every route the application serves, expands every request field
those routes accept, and refuses to let the process start if any of them would let a caller hand us a
conclusion. A new endpoint with an unsafe field fails at startup and in the tests, rather than
shipping and being noticed later — if at all, because the finding it produces still looks like ours.

**Why a filter is not a verdict, and why it is still on a list.** `GET …/findings?outcome=FAIL`
narrows what the engine already decided; it can never become an operand in an arithmetic comparison.
That is genuinely safe, but "safe because of how it is used" is the kind of judgement that spreads.
So the default here is the literal reading — a verdict type or a verdict-shaped field name is refused
anywhere in the request surface — and the read-only filters that are genuinely fine are named one by
one in `READ_ONLY_FILTERS`, each with its reason written down. Exempting a field is a visible edit to
this file, exactly as exempting a route from project scope is a visible edit to `UNSCOPED_ROUTES`.

Two things keep that list honest, because a growing exemption list is how this sort of guarantee
erodes: an exemption may only name a **safe** method, so it can never be used to let a write through,
and an exemption matching no live route fails `tests/api/test_no_client_verdict.py`, so the list
cannot outlive the code it was written for.

Source: backend proposal §10.2, §11; §4.1 · Design: `docs/DESIGN_PLATFORM.md` §4.2 ·
Verification: `tests/api/test_no_client_verdict.py`
"""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel

from rules.schema import Tolerance
from units.measurement import Measurement
from verdict.outcomes import Outcome, Severity

#: The verdict-plane types. A request field of any of these is a caller stating a conclusion, an
#: allowed error or a measured dimension — all three of which this service computes for itself.
#:
#: Matched by **identity**, not by name. A class called `Outcome` declared somewhere in the API layer
#: would satisfy a name check while carrying whatever the client sent, which is the failure this
#: exists to prevent wearing the guard's own clothes.
FORBIDDEN_REQUEST_TYPES: tuple[type, ...] = (Outcome, Severity, Tolerance, Measurement)

#: Field names that name a verdict whatever they are typed as. `passed: bool` is a client-supplied
#: PASS/FAIL in every sense that matters, and a plain `bool` defeats a check that only looks at types.
#:
#: Matched against the `_`-separated segments of a field name, so `verdict_outcome`, `client_outcome`
#: and `expected_value_mm` are caught as well. Segment-aligned rather than a substring search, so an
#: innocent name that merely contains the letters — `bypassed`, `passenger` — is not flagged. A guard
#: that cries wolf is one somebody deletes rather than fixes.
FORBIDDEN_FIELD_NAMES: frozenset[str] = frozenset(
    {"outcome", "verdict", "passed", "tolerance", "expected_value"}
)

#: Methods that cannot change anything. Only these may hold an exemption: a `GET` selects rows the
#: server already wrote, and there is no path from a query string on a read to a stored operand.
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

#: The golden rule, in the words that explain *why* the field is refused. Included in the error
#: because a message saying only "forbidden type in request model" tells the next author what to
#: rename, and renaming it is precisely the wrong fix.
GOLDEN_RULE = (
    "The golden rule: the AI reads, evidence qualifies, deterministic Python decides, a reviewer "
    "signs off. A PASS or a FAIL is exact arithmetic this service performs on evidence it gathered "
    "itself — it is never a value a caller hands us. An endpoint that accepts one lets the client "
    "decide the verdict, and the finding that comes out is indistinguishable from one we computed. "
    "Accept the identifier of a server-side row instead, and look the value up."
)


@dataclass(frozen=True, slots=True)
class ReadOnlyFilter:
    """One request field that may name a verdict value, and why that is safe.

    `field` is the dotted path the audit reports, so a nested field needs the path the audit prints
    rather than just its own name — an exemption cannot be broader than the thing it excuses.
    """

    method: str
    path: str
    """The full served path, every enclosing prefix applied — the path the audit reports."""

    field: str
    reason: str
    """Plain English, for the person deciding whether to add the next one."""

    @property
    def target(self) -> tuple[str, str, str]:
        """What this exemption matches, without its justification."""
        return (self.method.upper(), self.path, self.field)


#: The exemptions. Each is a field the audit would otherwise refuse, allowed because it narrows
#: results the engine already produced rather than supplying one.
#:
#: Every future read endpoint that filters on a verdict value needs its own entry here. That is the
#: cost of the strict default, and it is the point: each line is a decision somebody wrote down.
READ_ONLY_FILTERS: tuple[ReadOnlyFilter, ...] = (
    ReadOnlyFilter(
        method="GET",
        path="/api/v1/projects/{project_id}/packages/{package_id}/findings",
        field="outcome",
        reason=(
            "Narrows a list of findings the engine already decided. Read-only, and the value never "
            "reaches an arithmetic comparison — it becomes a WHERE clause over rows we wrote."
        ),
    ),
    ReadOnlyFilter(
        method="GET",
        path="/api/v1/projects/{project_id}/packages/{package_id}/findings",
        field="severity",
        reason=(
            "The same, for severity. A reviewer asking to see only the critical findings is reading "
            "our conclusions, not offering theirs."
        ),
    ),
)


class ClientVerdictAccepted(RuntimeError):
    """A route would accept a verdict from its caller. Raised at startup, and in the tests.

    A hard failure rather than a warning: a process that boots with such a route serves it, and every
    finding it produces looks exactly like one the engine computed.
    """


@dataclass(frozen=True, slots=True)
class Offence:
    """One request field that would let a caller supply a verdict."""

    method: str
    path: str
    location: str
    """Where in the request it arrives: `body`, `query`, `path`, `header` or `cookie`."""

    field: str
    """The dotted path from the top-level parameter, e.g. `payload.finding.tolerance`."""

    reason: str

    def describe(self) -> str:
        """One line, naming the route, the field and why it is refused."""
        return f"{self.method} {self.path} — {self.location} field `{self.field}`: {self.reason}"


# ---------------------------------------------------------------------------
# What the application actually serves
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MountedRoute:
    """One route the application will serve, under the path it is really served on."""

    path: str
    """`route.path` is the path *within* its router. A route included under `/api/v1` audited by its
    own path is audited — and exempted — under a path nobody serves."""

    route: APIRoute
    inherited: tuple[Dependant, ...]
    """Dependants from `include_router(dependencies=[...])`. They do not appear in the route's own
    dependant, and a router-level dependency can declare request parameters of its own."""


def _dependants_of(markers: Sequence[object], path: str) -> tuple[Dependant, ...]:
    """The dependants behind a list of `Depends(...)` markers."""
    found: list[Dependant] = []
    for marker in markers:
        call = getattr(marker, "dependency", None)
        if call is not None:
            found.append(get_dependant(path=path, call=call))
    return tuple(found)


def mounted_routes(app: FastAPI) -> list[MountedRoute]:
    """Every route the app serves, including those reached through `include_router`.

    **The enumeration is the whole guard, so getting it wrong makes the guard silently vacuous.** On
    this version of FastAPI, `include_router` does not flatten child routes into `app.routes`: it
    appends one opaque object and resolves the children at match time. Filtering `app.routes` for
    `APIRoute` therefore returns *nothing* from any wired router — verified directly, after one
    `include_router` there are zero `APIRoute` entries — and an audit built on it would pass while
    inspecting an empty set. Routers also include routers, so this recurses.

    `tests/api/test_authorisation.py` walks the route table the same way for the authorisation audit.
    The two copies are deliberate for now: that one is a test helper and this one is production code
    called at startup. Worth folding into one place, but not under this issue's scope.
    """

    def descend(
        routes: Iterable[object], prefix: str, inherited: tuple[Dependant, ...]
    ) -> list[MountedRoute]:
        found: list[MountedRoute] = []
        for route in routes:
            context = getattr(route, "include_context", None)
            if context is not None:
                inner = getattr(route, "original_router", None)
                found.extend(
                    descend(
                        getattr(inner, "routes", []),
                        prefix + getattr(context, "prefix", ""),
                        inherited
                        + _dependants_of(getattr(context, "dependencies", []), prefix or "/"),
                    )
                )
            elif isinstance(route, APIRoute):
                found.append(MountedRoute(prefix + route.path, route, inherited))
        return found

    return descend(app.routes, "", ())


# ---------------------------------------------------------------------------
# Expanding a request field to everything it can carry
# ---------------------------------------------------------------------------

#: Where request fields live on a dependant, and what to call each place in a message. All five, not
#: just the body: a `POST` taking `?outcome=PASS` in its query string accepts a client verdict just
#: as completely as one taking it in a JSON object.
_LOCATIONS: tuple[tuple[str, str], ...] = (
    ("body_params", "body"),
    ("query_params", "query"),
    ("path_params", "path"),
    ("header_params", "header"),
    ("cookie_params", "cookie"),
)


def names_like_a_verdict(name: str) -> bool:
    """Whether a field name states a conclusion, whatever it is typed as.

    Compares `_`-separated segments so `verdict_outcome` and `expected_value_mm` match while
    `bypassed` does not — a substring search would flag the second and get the guard mistrusted.
    """
    segments = tuple(part for part in name.lower().split("_") if part)
    for forbidden in FORBIDDEN_FIELD_NAMES:
        wanted = tuple(forbidden.split("_"))
        span = len(wanted)
        if any(
            segments[start : start + span] == wanted for start in range(len(segments) - span + 1)
        ):
            return True
    return False


def _members(annotation: object) -> Iterator[object]:
    """The annotation and everything nested inside it.

    Flattening this way is what makes `Outcome`, `Outcome | None`, `list[Outcome]`,
    `dict[str, Severity]` and `Annotated[list[Outcome] | None, Query()]` all one case. Checking
    top-level types only would let any of the wrappers through, and a caller does not care which one
    carried the value.
    """
    yield annotation
    for argument in typing.get_args(annotation):
        yield from _members(argument)


def _field_annotations(model: type) -> Iterable[tuple[str, object]]:
    """The declared fields of a nested model or dataclass, so the walk can keep descending.

    Dataclass annotations may be strings — `units/measurement.py` uses `from __future__ import
    annotations` — so they are resolved rather than compared as text. A field whose annotation cannot
    be resolved is skipped: refusing to start over an unresolvable third-party annotation would make
    the guard the reason the service is down.
    """
    if issubclass(model, BaseModel):
        return [(name, field.annotation) for name, field in model.model_fields.items()]
    # Re-checked here rather than trusted from the caller, so the narrowing is local and the function
    # is safe to call with any type.
    if not dataclasses.is_dataclass(model):
        return []
    try:
        hints = typing.get_type_hints(model)
    except Exception:  # noqa: BLE001 - an unresolvable annotation is skipped, not fatal
        return []
    return [(field.name, hints.get(field.name)) for field in dataclasses.fields(model)]


def _forbidden_ancestor(member: type) -> type | None:
    """The verdict-plane type this one is, or derives from — `None` if it is unrelated to all of them.

    **Subclasses count, and checking identity alone would have missed them.** `Tolerance` is a model
    and `Measurement` a dataclass, so both can be subclassed; a `class ProposedTolerance(Tolerance)`
    in a request body is the forbidden type with a different name on it. Identity matching would have
    let it through and then walked its *fields* instead, none of which are named like a verdict — so
    the guard would have reported the route as clean.

    Still identity-based in the sense that matters: relatedness is decided by the class hierarchy, not
    by what a class calls itself, so a look-alike named `Outcome` that inherits from nothing forbidden
    is correctly left alone.
    """
    for forbidden in FORBIDDEN_REQUEST_TYPES:
        try:
            if issubclass(member, forbidden):
                return forbidden
        except TypeError:  # pragma: no cover - a non-class slipping past the isinstance check
            continue
    return None


def _offences_in(
    name: str, annotation: object, *, trail: str, seen: frozenset[type]
) -> Iterator[tuple[str, str]]:
    """Everything unsafe at or beneath one field, as (dotted path, plain-English reason).

    Recursive over nested models, and `seen` is what stops a self-referential model — a comment with
    a list of replies — from recursing until the stack runs out.
    """
    field = f"{trail}.{name}" if trail else name
    if names_like_a_verdict(name):
        yield field, f"the name `{name}` states a verdict value, whatever its type"

    for member in _members(annotation):
        if not isinstance(member, type):
            continue
        forbidden = _forbidden_ancestor(member)
        if forbidden is not None:
            yield field, f"`{forbidden.__name__}` belongs to the verdict plane and is computed here"
            continue
        if member in seen:
            continue
        if issubclass(member, BaseModel) or dataclasses.is_dataclass(member):
            for nested_name, nested in _field_annotations(member):
                yield from _offences_in(nested_name, nested, trail=field, seen=seen | {member})


def _dependants(root: Dependant) -> Iterator[Dependant]:
    """A dependant and every dependant beneath it, at any depth.

    A `Depends(...)` declares request parameters of its own, and a shared dependency is exactly where
    a filter would be factored out to — so looking only at the route's own parameters would miss the
    one place the same unsafe field gets added to several endpoints at once.
    """
    yield root
    for dependency in root.dependencies:
        yield from _dependants(dependency)


def _unsafe_request_fields(app: FastAPI) -> Iterator[tuple[str, str, str, str, str]]:
    """Every unsafe request field on the app, as (method, path, location, field, reason).

    Before any exemption is applied — both the audit and the stale-exemption check need the same raw
    walk, and two copies of it would be two chances for them to disagree about what the surface is.
    """
    for mounted in mounted_routes(app):
        dependants = list(_dependants(mounted.route.dependant))
        for inherited in mounted.inherited:
            dependants.extend(_dependants(inherited))

        for method in sorted(mounted.route.methods or set()):
            for attribute, location in _LOCATIONS:
                for dependant in dependants:
                    for parameter in getattr(dependant, attribute, []):
                        for field, reason in _offences_in(
                            parameter.name,
                            parameter.field_info.annotation,
                            trail="",
                            seen=frozenset(),
                        ):
                            yield method, mounted.path, location, field, reason


def offences(app: FastAPI) -> list[Offence]:
    """Every request field on every route that would let a caller supply a verdict.

    Returned rather than raised so a test can inspect them and the startup assertion can report all
    of them at once. An author who has to rerun the process per offending field fixes the first and
    learns nothing about the rest.

    One entry per field, not per reason. A field can be unsafe twice over — `outcome: Outcome` is both
    a verdict-shaped name and a verdict type — and listing it twice reads as two problems to fix.
    """
    exempt = {filter_.target for filter_ in READ_ONLY_FILTERS}
    reasons: dict[tuple[str, str, str, str], list[str]] = {}
    for method, path, location, field, reason in _unsafe_request_fields(app):
        # A body field is never exempt. An exemption excuses reading a value back out, and nothing
        # arriving in a body is being read back.
        if location != "body" and (method, path, field) in exempt:
            continue
        found = reasons.setdefault((method, path, location, field), [])
        if reason not in found:
            found.append(reason)

    return [
        Offence(method=method, path=path, location=location, field=field, reason="; ".join(why))
        for (method, path, location, field), why in reasons.items()
    ]


def unused_read_only_filters(app: FastAPI) -> list[ReadOnlyFilter]:
    """Exemptions that no longer excuse anything on this app.

    A stale exemption is worse than a missing one: it reads as a considered decision, and the next
    author renaming a filter finds a list that appears to have thought about their case. Asserted in
    the tests rather than at startup — an exemption for a route that has gone is a tidiness problem,
    and refusing to boot over it would be the guard causing the outage.
    """
    live = {
        (method, path, field)
        for method, path, location, field, _ in _unsafe_request_fields(app)
        if location != "body"
    }
    return [filter_ for filter_ in READ_ONLY_FILTERS if filter_.target not in live]


def validate_read_only_filters(filters: Sequence[ReadOnlyFilter]) -> None:
    """Refuse an exemption that could excuse a write, or that excuses nothing in particular.

    Run at import, so the module will not load with a bad list — and written as a callable taking the
    list so a test can watch it fail. A guard whose test cannot fail on a wrong answer proves nothing.
    """
    for filter_ in filters:
        if filter_.method.upper() not in SAFE_METHODS:
            raise RuntimeError(
                f"the exemption for {filter_.method} {filter_.path} names an unsafe method. Only "
                f"{sorted(SAFE_METHODS)} may be exempted: a read cannot store what it was sent, and "
                "a write can. " + GOLDEN_RULE
            )
        if not filter_.reason.strip():
            raise RuntimeError(
                f"the exemption for {filter_.method} {filter_.path} field `{filter_.field}` gives "
                "no reason. An exemption nobody justified is one nobody can review."
            )

    targets = [filter_.target for filter_ in filters]
    if len(set(targets)) != len(targets):
        raise RuntimeError(
            "the same field is exempted twice, so which reason applies depends on list order"
        )


validate_read_only_filters(READ_ONLY_FILTERS)


def assert_no_verdict_fields(app: FastAPI) -> None:
    """Refuse to serve an API where any route would accept a verdict from its caller.

    Called at the end of the application factory and asserted in the tests. Both, deliberately: a
    test alone is bypassed by not running the tests, and a startup check alone gives no author a way
    to see the rule before they deploy.

    Raises `ClientVerdictAccepted`, naming every offending field and explaining the rule — because
    the obvious fix for "forbidden type in request model" is to rename the field, and that is the one
    fix that leaves the hole open.
    """
    found = offences(app)
    if not found:
        return

    listed = "\n".join(f"  - {offence.describe()}" for offence in found)
    raise ClientVerdictAccepted(
        f"{len(found)} request field(s) would let a caller supply a verdict:\n{listed}\n\n"
        f"{GOLDEN_RULE}\n\n"
        "If the field is genuinely a read-only filter over results this service already produced, "
        "add it to READ_ONLY_FILTERS in app/api/guards.py with the reason written out. Only safe "
        "methods may be exempted, and the exemption has to name a live route."
    )
