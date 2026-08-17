"""The runtime half of verdict isolation (#257, F1.6).

`tests/test_verdict_isolation.py` proves nothing in `verdict/` can *import* its way to a model, a
retriever, a socket or a database. That is a statement about source code. This file is about the
process: what it may hold in its environment, and what it may reach.

Every test here is a refusal test. The control is only worth anything if it says no, so the cases
that matter are the ones where it must — and the failure mode being guarded against is not a crash,
it is a verdict process that comes up looking fine while quietly able to reach something.

The rulings under test are in `docs/decisions/F1_6_RUNTIME_ISOLATION.md`.
"""

from __future__ import annotations

import errno
import ipaddress
from pathlib import Path

import pytest
import yaml

from deploy.verdict_isolation.preflight import (
    DB_ALLOWLIST_VARIABLE,
    EGRESS_PROBE_VARIABLE,
    PERMITTED_ENVIRONMENT,
    EgressState,
    IsolationBroken,
    assert_isolated,
    check_environment,
    parse_allowlist,
    parse_probe_target,
    probe_egress,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: An environment that should pass: the database pinned by address, a probe target outside it.
ISOLATED = {
    "DATABASE_URL": "postgresql+psycopg://gv:gv@10.83.0.2:5432/gv",
    DB_ALLOWLIST_VARIABLE: "10.83.0.2/32",
    EGRESS_PROBE_VARIABLE: "1.1.1.1:443",
    "PATH": "/usr/local/bin:/usr/bin",
}


def _no_route(address: str, port: int, timeout: float) -> None:
    """A kernel with no route to anywhere — what `internal: true` produces."""
    raise OSError(errno.ENETUNREACH, "Network is unreachable")


def _reachable(address: str, port: int, timeout: float) -> None:
    """A connection that completes. Egress exists."""
    return


def _refused(address: str, port: int, timeout: float) -> None:
    """Something answered and said no — which still means a packet got there."""
    raise OSError(errno.ECONNREFUSED, "Connection refused")


def _dropped(address: str, port: int, timeout: float) -> None:
    """Silence. A DROP rule, or a slow network, and nothing can tell which."""
    raise TimeoutError("timed out")


# ---------------------------------------------------------------------------
# The environment is an allow-list (D5)
# ---------------------------------------------------------------------------


def test_an_isolated_process_starts() -> None:
    """The control has to be able to say yes, or the refusals below prove nothing."""
    assert_isolated(ISOLATED, connect=_no_route)


def test_a_model_credential_is_refused() -> None:
    with pytest.raises(IsolationBroken, match="not permitted"):
        assert_isolated({**ISOLATED, "ANTHROPIC_API_KEY": "sk-x"}, connect=_no_route)


def test_a_retrieval_credential_is_refused() -> None:
    with pytest.raises(IsolationBroken, match="not permitted"):
        assert_isolated({**ISOLATED, "QDRANT_API_KEY": "x"}, connect=_no_route)


def test_a_provider_nobody_enumerated_is_refused() -> None:
    """The reason this is an allow-list and not a deny-list. No list of known model vendors contains
    this name, and it is refused anyway — a deny-list fails open the first time a new provider ships
    a variable somebody forgot to add."""
    with pytest.raises(IsolationBroken, match="not permitted"):
        assert_isolated({**ISOLATED, "FUTURE_VENDOR_TOKEN": "x"}, connect=_no_route)


def test_the_refusal_names_what_it_found() -> None:
    """A control that says only "environment not permitted" makes somebody go and read the source to
    learn what it objected to."""
    with pytest.raises(IsolationBroken, match="AWS_SECRET_ACCESS_KEY"):
        check_environment({**ISOLATED, "AWS_SECRET_ACCESS_KEY": "x"})


def test_the_allow_list_holds_no_credential_shaped_names() -> None:
    """A guard on the allow-list itself. It is the one place where adding a line silently widens
    what the verdict process may hold."""
    suspicious = [
        name
        for name in PERMITTED_ENVIRONMENT
        if any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CRED"))
    ]
    assert not suspicious, f"credential-shaped names in the allow-list: {suspicious}"


# ---------------------------------------------------------------------------
# Addresses are pinned, names are refused (D3)
# ---------------------------------------------------------------------------


def test_a_hostname_in_the_allowlist_is_refused() -> None:
    """Resolving it would be egress performed by the check that exists to prove egress is
    impossible, and it leaves a name-to-address step a poisoned resolver could redirect."""
    with pytest.raises(IsolationBroken, match="not an IP address or CIDR"):
        parse_allowlist("db.internal", variable=DB_ALLOWLIST_VARIABLE)


def test_a_hostname_as_the_probe_target_is_refused() -> None:
    with pytest.raises(IsolationBroken, match="not an IP address"):
        parse_probe_target("example.com:443")


def test_an_absent_allowlist_is_refused_rather_than_defaulted() -> None:
    """A default would be a guess about which network the process is on, and that is how a control
    quietly stops applying."""
    with pytest.raises(IsolationBroken, match="not set"):
        parse_allowlist(None, variable=DB_ALLOWLIST_VARIABLE)


def test_an_absent_probe_target_is_refused() -> None:
    """Without somewhere to dial, the absence of egress is assumed rather than tested."""
    with pytest.raises(IsolationBroken, match="not set"):
        parse_probe_target(None)


@pytest.mark.parametrize("pinned", ["10.83.0.2/32", "10.83.0.0/24", "10.83.0.2"])
def test_addresses_and_cidr_blocks_are_both_accepted(pinned: str) -> None:
    assert parse_allowlist(pinned, variable=DB_ALLOWLIST_VARIABLE)


def test_probing_an_address_the_process_may_reach_is_refused() -> None:
    """The trap this closes. Pointing the probe at the database would report success in exactly the
    case the check exists to catch — a process with egress everywhere still reaches its database."""
    inside = {**ISOLATED, EGRESS_PROBE_VARIABLE: "10.83.0.2:5432"}
    with pytest.raises(IsolationBroken, match="inside the permitted range"):
        assert_isolated(inside, connect=_no_route)


# ---------------------------------------------------------------------------
# Three states, not two (D4)
# ---------------------------------------------------------------------------


def test_no_route_is_the_definitive_answer() -> None:
    assert probe_egress("1.1.1.1", 443, connect=_no_route) is EgressState.BLOCKED


@pytest.mark.parametrize("dial", [_reachable, _refused])
def test_anything_that_answers_means_egress_exists(dial: object) -> None:
    """A refusal is not isolation. Something received the packet in order to refuse it."""
    assert probe_egress("1.1.1.1", 443, connect=dial) is EgressState.REACHABLE


def test_a_silent_drop_is_indeterminate_not_blocked() -> None:
    """The distinction the whole ruling turns on. A timeout is a DROP rule or a slow network, and
    nothing here can tell them apart."""
    assert probe_egress("1.1.1.1", 443, connect=_dropped) is EgressState.INDETERMINATE


def test_a_reachable_network_refuses_to_start() -> None:
    with pytest.raises(IsolationBroken, match="is reachable"):
        assert_isolated(ISOLATED, connect=_reachable)


def test_an_indeterminate_network_refuses_to_start() -> None:
    """Fail-closed. "Cannot determine" is not "isolated", and an unverifiable safety control is a
    failed one — the same reason `NOT_MEASURED` is never treated as a pass in the release gates."""
    with pytest.raises(IsolationBroken, match="could not be determined"):
        assert_isolated(ISOLATED, connect=_dropped)


# ---------------------------------------------------------------------------
# The isolation lives in the run configuration (#257 acceptance)
# ---------------------------------------------------------------------------


def _compose() -> dict:
    path = REPO_ROOT / "deploy" / "verdict_isolation" / "compose.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_verdict_network_has_no_gateway() -> None:
    """`internal: true` is the control itself. Without it the preflight is checking a property
    nothing established, and the first person to edit this file would silently remove it."""
    assert _compose()["networks"]["verdict"]["internal"] is True


def test_the_run_configuration_builds_the_environment_rather_than_inheriting_one() -> None:
    """`env_file` or a bare `- VAR` pass-through would hand the process whatever the host holds.
    The preflight would then refuse it — correctly, but at startup, where a deployment discovers it
    at the worst moment."""
    verdict = _compose()["services"]["verdict"]
    assert "env_file" not in verdict
    assert isinstance(verdict["environment"], dict), "a list form permits host pass-through entries"


def test_every_variable_the_run_configuration_sets_is_permitted() -> None:
    """The two halves have to agree. If the compose file sets something the preflight refuses, the
    service cannot start and the mistake is found in production rather than here."""
    supplied = set(_compose()["services"]["verdict"]["environment"])
    assert (
        supplied <= PERMITTED_ENVIRONMENT
    ), f"not in the allow-list: {sorted(supplied - PERMITTED_ENVIRONMENT)}"


def test_the_configured_probe_target_is_outside_the_configured_allowlist() -> None:
    """The same trap as above, caught in the configuration rather than at startup."""
    environment = _compose()["services"]["verdict"]["environment"]
    allowlist = parse_allowlist(environment[DB_ALLOWLIST_VARIABLE], variable=DB_ALLOWLIST_VARIABLE)
    address, _ = parse_probe_target(environment[EGRESS_PROBE_VARIABLE])
    assert not any(ipaddress.IPv4Address(address) in network for network in allowlist)


def test_the_entrypoint_runs_the_preflight_before_anything_else() -> None:
    """If the container's entrypoint is not this, the preflight is a file nobody executes."""
    assert _compose()["services"]["verdict"]["entrypoint"] == [
        "python",
        "-m",
        "deploy.verdict_isolation.entrypoint",
    ]


def test_the_database_is_reached_by_address_not_by_name() -> None:
    environment = _compose()["services"]["verdict"]["environment"]
    assert "@10.83.0.2:" in environment["DATABASE_URL"]


# ---------------------------------------------------------------------------
# The static half is untouched (D1)
# ---------------------------------------------------------------------------


def test_the_preflight_lives_outside_the_verdict_package() -> None:
    """The point of D1. `socket` is forbidden inside `verdict/`, so putting the probe there would
    have meant deleting it from that list — the runtime control destroying the static one."""
    assert not (REPO_ROOT / "verdict" / "startup.py").exists()
    assert (REPO_ROOT / "deploy" / "verdict_isolation" / "preflight.py").is_file()


def test_socket_is_still_forbidden_inside_the_verdict_package() -> None:
    from tests.test_verdict_isolation import FORBIDDEN_FOR_VERDICT

    assert "socket" in FORBIDDEN_FOR_VERDICT
