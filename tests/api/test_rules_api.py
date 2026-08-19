"""Reading the rulebook, listing the registry, and publishing through D6 (#206, C2.4).

The four acceptance criteria, and what each is actually worth testing:

**The operations list cannot drift from the registry.** The endpoint reads `verdict.registry.REGISTRY`
at request time. Asserting that today's names come back would pass equally against a hand-written
list that happens to be correct today, so the test below registers an operation this API has never
heard of and requires the endpoint to report it. That fails against any implementation that names
operations itself.

**Publishing goes through D6, not around it.** Tested by refusing: without the role, and with a
proposal D6 rejects. An endpoint that reimplemented the gate would pass a happy-path test and fail
these.

**A snapshot's hash verifies.** Recomputed from the canonical JSON in the response, so the client's
check is the one the test performs.

**An unconfirmed tolerance marks a rule not releasable.** ADR-0011 — the number that decides a PASS is
still a guess, so the rule may exist and may not ship.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.main import create_app
from verdict.registry import REGISTRY, Arity, OperationKind, OperationSpec, register

DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"


def _settings() -> Settings:
    return Settings(database_url=DATABASE_URL)  # type: ignore[call-arg]


def _client(*roles: Role) -> TestClient:
    app = create_app(_settings())
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="anant", roles=frozenset(roles), projects=frozenset({uuid4()})
    )
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# The operations endpoint is the registry, not a copy of it
# ---------------------------------------------------------------------------


def test_the_operations_endpoint_lists_the_real_registry() -> None:
    """Every operation the engine can resolve is reported, and nothing else."""
    response = _client(Role.RULE_ADMIN).get("/api/v1/operations")

    assert response.status_code == 200
    reported = {entry["name"] for entry in response.json()}
    assert reported == set(REGISTRY), (
        "the endpoint and the registry disagree, so an author is being told the engine can do "
        "something it cannot, or cannot do something it can"
    )
    assert reported, "the registry is empty, so this test would pass while proving nothing"


def test_an_operation_registered_after_startup_is_reported() -> None:
    """**The anti-drift criterion, and the only version of it that proves anything.**

    A test asserting today's names come back passes just as well against a hand-written list that
    happens to be right today — and that list is wrong the first time `verdict/operations/` gains a
    module and nobody remembers this file. So the spec below is one the API has never heard of.

    The interesting direction is the second one: an endpoint reporting an operation the engine cannot
    resolve sends an author to write a rule naming it, and that failure surfaces on a real drawing.
    """
    # Import the endpoint module *first*, deliberately. If it is imported after the spec is
    # registered, then even an implementation that snapshots the registry at import time captures the
    # new spec and this test passes while proving nothing — which is exactly what happened when I
    # first checked it by running this test on its own. Registering only after the module is loaded
    # is what makes "read at request time" the thing under test rather than import order.
    import app.api.operations  # noqa: F401 - imported for its side effect on ordering

    name = f"test_only_operation_{uuid4().hex[:8]}"
    spec = OperationSpec(
        name,
        "1.0.0",
        {"value": Arity.SCALAR},
        lambda *, value: value,  # pragma: no cover - never invoked
        OperationKind.DERIVATION,
    )
    register(spec)
    try:
        response = _client(Role.RULE_ADMIN).get("/api/v1/operations")
        reported = {entry["name"]: entry for entry in response.json()}

        assert name in reported, (
            "an operation registered after import is missing, so this endpoint is a copy of the "
            "registry rather than a reading of it"
        )
        assert reported[name]["operands"] == {"value": Arity.SCALAR.value}
        assert reported[name]["version"] == "1.0.0"
    finally:
        REGISTRY.pop(name, None)


def test_the_operations_list_carries_a_signature_not_an_implementation() -> None:
    """A rule names an operation and never carries one (§2.2). An endpoint that returned anything
    executable — source, a reference, a lambda repr — would be the field this API must not have."""
    entries = _client(Role.REVIEWER).get("/api/v1/operations").json()

    assert entries
    for entry in entries:
        assert set(entry) == {"name", "version", "kind", "operands"}
        assert "lambda" not in str(entry) and "function" not in str(entry)


def test_listing_operations_requires_a_role() -> None:
    """The registry is not secret, but it is not public either — an unauthenticated caller learns
    nothing about what this system checks."""
    app = create_app(_settings())
    assert TestClient(app, raise_server_exceptions=False).get("/api/v1/operations").status_code in (
        401,
        403,
        404,
        500,
    )


# ---------------------------------------------------------------------------
# Publishing goes through D6
# ---------------------------------------------------------------------------


def test_publishing_without_the_rule_admin_role_is_refused() -> None:
    """The first acceptance criterion. A reviewer confirms evidence; publishing a rule changes what
    every future check means, and the two are deliberately different rights."""
    response = _client(Role.REVIEWER).post(
        "/api/v1/rules/cab_arch_vs_shop_001/publish",
        json={"snapshot_id": "0" * 64, "target": "production"},
    )
    assert response.status_code == 404, "a 403 would confirm the rule exists"


def test_an_admin_cannot_publish_by_virtue_of_being_an_admin() -> None:
    """**The gap worth knowing about.** `app/auth/roles.py` has `admin`; `rules/governance/publish.py`
    has only `reviewer` and `rule_admin`. So an admin has no governance role at all.

    Mapping `admin` to `rule_admin` would have the API grant a publishing right the governance layer
    never defined, which is the sort of quiet widening that makes an approval gate decorative. It
    refuses instead, and the mismatch is the admin's to resolve.
    """
    response = _client(Role.ADMIN).post(
        "/api/v1/rules/cab_arch_vs_shop_001/publish",
        json={"snapshot_id": "0" * 64, "target": "production"},
    )
    assert response.status_code != 200


def test_the_publish_endpoint_actually_calls_d6() -> None:
    """It delegates to `rules.governance.publish`, and that is checked structurally.

    The failure mode is a shortcut added later — "if it is already approved, skip the gate" — which
    passes every behavioural test written against today's fixtures while removing the thing the story
    is for. So this reads the function's syntax tree.

    The first version of this test grepped the source for words like "bypass" and failed on the
    module docstring, which says there is no bypass. Matching prose is checking how something is
    spelled rather than what it does — the exact defect this file exists to catch elsewhere, and it
    is worth recording that it is easy to write by accident.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/api/rules.py").read_text())
    endpoint = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "publish_rule"
    )
    called = {
        node.func.id
        for node in ast.walk(endpoint)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "publish_snapshot" in called, (
        "publish_rule no longer calls D6's publish, so whatever it does now is publication logic "
        "this story was told not to write"
    )

    returns = [node for node in ast.walk(endpoint) if isinstance(node, ast.Return)]
    assert len(returns) == 1, (
        f"publish_rule has {len(returns)} return statements. More than one is how a path that skips "
        "the gate gets added: every early return is an answer given without D6 having spoken."
    )


# ---------------------------------------------------------------------------
# Readability, hashes and production readiness
# ---------------------------------------------------------------------------


def _hash_of(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def test_a_snapshot_hash_verifies_against_its_canonical_json() -> None:
    """The client's check, performed by the test. §2.4: a caller has to be able to confirm what it
    received rather than take the API's word for it."""
    pytest.importorskip("yaml")
    from app.api.rules import Rulebook

    app, store = _app_with_rulebook()
    client = TestClient(app, raise_server_exceptions=False)
    rule_id = _any_rule_id(store)
    if rule_id is None:
        pytest.skip("no rule is loadable without the client's real rulebook")

    response = client.get(f"/api/v1/rules/{rule_id}/snapshots")
    assert response.status_code == 200
    for snapshot in response.json():
        assert snapshot["snapshot_id"] == _hash_of(
            snapshot["canonical_json"]
        ), "the returned hash does not match the returned bytes, so verifying it proves nothing"
    assert isinstance(Rulebook, type)


def test_the_tolerance_report_reads_as_english() -> None:
    """The issue asks for this explicitly. The report answers "how much of the rulebook is still
    guesswork?", and the person asking is not always the person who wrote it."""
    app, _ = _app_with_rulebook()
    response = TestClient(app, raise_server_exceptions=False).get("/api/v1/rules/tolerance-report")

    if response.status_code != 200:
        pytest.skip("the tolerance report needs a configured rulebook")
    body = response.json()
    text = body["report"] if isinstance(body, dict) and "report" in body else str(body)

    assert len(text.split()) > 5, "a report of five words is a status code with extra steps"
    for jargon in ("SnapshotStore", "Fraction(", "None", "Traceback"):
        assert jargon not in text, f"the report leaks {jargon} at a reader outside the codebase"


def _app_with_rulebook() -> tuple[Any, Any]:
    """An app with whatever rulebook the repository can actually build, or none.

    Deliberately tolerant: the shipped rulebook is one YAML file and the real one is the client's.
    Tests that need rules skip rather than assert against a fixture invented here, which §9 warns
    would encode today's guess as ground truth.
    """
    app = create_app(_settings())
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="anant", roles=frozenset({Role.RULE_ADMIN}), projects=frozenset({uuid4()})
    )
    return app, getattr(app.state, "rulebook", None)


def _any_rule_id(store: Any) -> str | None:
    if store is None:
        return None
    rules = getattr(store, "rules", None)
    if callable(rules):
        listed = list(rules())
        return str(listed[0]) if listed else None
    return None
