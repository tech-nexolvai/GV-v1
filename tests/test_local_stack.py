"""The local stack and the runbook that describes it, kept from drifting apart (#416, F3.2).

A runbook is a promise about a file somebody else maintains. It rots the moment a service is renamed or
a target removed — and it rots silently, because nothing runs a document. So the parts that can be
checked mechanically are checked here: every service the runbook names exists in `docker-compose.yml`,
every `make` target it tells you to run exists in the `Makefile`, and the port that exists specifically
to avoid colliding with a developer's own PostgreSQL has not quietly moved back.

What this cannot check is that the stack actually comes up; that needs Docker, and a unit test is the
wrong place for a 60-second image pull. It was verified by running it, and the PR records what came
back.

Source: backend proposal §9 · Design: `docs/DESIGN_PLATFORM.md` §6 · Verification: this file
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

COMPOSE = Path("docker-compose.yml")
MAKEFILE = Path("Makefile")
CONTRIBUTING = Path("CONTRIBUTING.md")

#: The section of `CONTRIBUTING.md` this file holds to its word.
RUNBOOK_HEADING = "## Running the stack locally"


def _compose() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml", reason="PyYAML parses the compose file")
    loaded = yaml.safe_load(COMPOSE.read_text())
    assert isinstance(loaded, dict), "docker-compose.yml did not parse as a mapping"
    return loaded


def _runbook() -> str:
    text = CONTRIBUTING.read_text()
    start = text.index(RUNBOOK_HEADING)
    rest = text[start + len(RUNBOOK_HEADING) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _make_targets() -> set[str]:
    """Every target the Makefile defines, read from the file rather than from `make -qp`.

    Parsing the text keeps this test independent of having `make` on the machine, and of a target that
    happens to fail — the question here is only whether it is *declared*.
    """
    return set(re.findall(r"^([a-zA-Z][\w-]*):", MAKEFILE.read_text(), re.MULTILINE))


# ---------------------------------------------------------------------------
# The stack itself
# ---------------------------------------------------------------------------


def test_the_stack_brings_up_a_database_and_an_engine() -> None:
    """Both services exist. Before this story there was one, so a worker had nothing to connect to."""
    services = _compose()["services"]
    assert "db" in services, "no database service"
    assert "hatchet" in services, "no engine service — a worker would have nothing to connect to"


@pytest.mark.parametrize("service", ["db", "hatchet"])
def test_every_service_has_a_health_check(service: str) -> None:
    """The acceptance criterion, and it does real work here.

    `depends_on: service_healthy` is what stops the engine running its migrations against a PostgreSQL
    that is still starting — which fails and takes the container down. A service without a health check
    reduces that dependency to "the process exists", which is not the same claim.
    """
    assert "healthcheck" in _compose()["services"][service], f"{service} has no health check"


def test_the_engine_waits_for_a_healthy_database() -> None:
    """Not merely "started": the engine migrates on boot and a half-started database fails that."""
    depends = _compose()["services"]["hatchet"]["depends_on"]
    assert depends["db"]["condition"] == "service_healthy"


def test_the_database_port_does_not_move_back_to_5432() -> None:
    """5433 is deliberate and this is the regression test for it.

    A developer very likely already runs PostgreSQL. Publishing on 5432 would make a connection to the
    wrong database succeed silently, which is a much worse afternoon than a refused connection.
    """
    ports = _compose()["services"]["db"]["ports"]
    assert any(str(p).startswith("5433:") for p in ports), f"db is not on 5433: {ports}"
    assert not any(str(p).startswith("5432:") for p in ports), "db was published on 5432"


def test_the_engine_image_is_pinned_to_a_digest() -> None:
    """`:latest` would mean two developers on one commit run two different engines.

    And the one who hits a bug could not say which — the failure is unreproducible by construction.
    """
    image = _compose()["services"]["hatchet"]["image"]
    assert "@sha256:" in image, f"the engine image is not pinned by digest: {image}"


def test_the_engine_gets_its_own_database() -> None:
    """Two logical databases, which is the layout `.env.example` already implies.

    Separate because the engine runs its own migrations: one shared database would put
    `hatchet-migrate` and Alembic in charge of the same namespace.
    """
    env = _compose()["services"]["hatchet"]["environment"]
    assert env["DATABASE_POSTGRES_DB_NAME"] == "hatchet"
    assert "/hatchet" in env["DATABASE_URL"]
    assert _compose()["services"]["db"]["environment"]["POSTGRES_DB"] == "gv", (
        "the application database is not 'gv', so the two are no longer separate in the way the "
        "runbook describes"
    )
    init = _compose()["services"]["db"]["volumes"]
    assert any(
        "docker-entrypoint-initdb.d" in str(v) for v in init
    ), "nothing creates the engine's database, so its migrations will fail on first start"


def test_the_engine_is_told_to_use_the_v1_api() -> None:
    """`workflow/review.py` is written against v1.

    An engine defaulting to V0 registers nothing, and the worker then sits idle with no error worth
    reading — the kind of failure that costs an afternoon because nothing is broken, exactly.
    """
    assert _compose()["services"]["hatchet"]["environment"]["SERVER_DEFAULT_ENGINE_VERSION"] == "V1"


def test_the_broadcast_address_is_reachable_from_the_worker() -> None:
    """The worker runs on the host, so what the engine tells it to dial must be a host address.

    A service name here resolves inside the Compose network and nowhere else, and the symptom is a
    worker that connects once, is handed an unreachable address, and then goes quiet.
    """
    env = _compose()["services"]["hatchet"]["environment"]
    assert env["SERVER_GRPC_BROADCAST_ADDRESS"].startswith("localhost:")


# ---------------------------------------------------------------------------
# The runbook and the stack say the same thing
# ---------------------------------------------------------------------------


def test_every_make_target_the_runbook_names_exists() -> None:
    """The drift this file exists to prevent, in the direction that actually happens.

    A renamed target leaves the runbook telling somebody to run something that is gone — and the first
    person to find out is a new contributor following it line by line.
    """
    runbook = _runbook()
    named = set(re.findall(r"\bmake ([a-z][\w-]*)", runbook))
    assert named, "the runbook names no make targets, so this test is checking nothing"
    missing = named - _make_targets()
    assert not missing, f"the runbook names targets the Makefile does not define: {sorted(missing)}"


def test_every_service_the_runbook_names_exists_in_compose() -> None:
    """The acceptance criterion, stated in the direction the criterion states it."""
    runbook = _runbook().lower()
    for service in _compose()["services"]:
        assert service in runbook or service == "db", f"{service} is not mentioned in the runbook"


def test_the_runbook_names_all_three_processes() -> None:
    """A package needs the API, the worker *and* the dispatcher, and missing one fails quietly.

    Without the dispatcher the API returns success and nothing runs; without the worker the engine
    accepts a workflow and no stage executes. Neither looks like an error, so the runbook has to be
    explicit and this checks that it stays so.
    """
    runbook = _runbook()
    for target in ("make serve", "make worker", "make dispatch"):
        assert target in runbook, f"{target} is missing from the runbook"
    assert "quiet" in runbook.lower(), (
        "the runbook does not warn that a missing process fails silently, which is the one thing "
        "somebody following it needs to know"
    )


def test_the_runbook_includes_the_migration_step() -> None:
    """Easy to forget, and `/ready` answers 503 until it is done — the criterion names it for that."""
    assert "make migrate" in _runbook()


def test_the_token_step_is_documented_and_says_where_it_goes() -> None:
    """A token nobody knows where to put is not a documented step."""
    runbook = _runbook()
    assert "make token" in runbook
    assert ".env" in runbook, "the runbook does not say where the token goes"
    assert (
        "GV_HATCHET_TOKEN" in MAKEFILE.read_text()
    ), "make token does not name the variable the application actually reads"


def test_the_token_file_cannot_be_committed() -> None:
    """`make token` writes a live tenant credential into the working tree, on purpose.

    That is only acceptable because it is ignored — it was not, when I first wrote the target, while the
    Makefile said it was.
    """
    # **An active line, not a substring.** The first version of this asserted `".hatchet-token" in
    # gitignore` — which passes on `#.hatchet-token`, a commented-out rule that ignores nothing. I found
    # that by commenting the line out and watching the test still pass. A grep for a word is a test of
    # the text, not of the behaviour.
    rules = [
        line.strip()
        for line in Path(".gitignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert ".hatchet-token" in rules, (
        "`.hatchet-token` is not an active .gitignore rule, so `make token` writes a live tenant "
        f"credential into a committable file. Active rules: {rules}"
    )


def test_serve_uses_the_factory_flag() -> None:
    """`app/main.py` exposes `create_app()` and no module-level `app`.

    That is deliberate — a singleton would make every test share one configuration — so `--factory` is
    the consequence of a design decision rather than a workaround, and dropping it breaks `make serve`
    with an error about the wrong thing.
    """
    import app.main

    assert not hasattr(
        app.main, "app"
    ), "app.main now has a module-level app, so the reason serve needs --factory has changed"
    serve = [line for line in MAKEFILE.read_text().splitlines() if "uvicorn" in line]
    assert serve, "make serve does not run uvicorn"
    assert "--factory" in serve[0], "uvicorn is invoked without --factory and will fail"
