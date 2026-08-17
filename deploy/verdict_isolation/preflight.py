"""The runtime half of verdict isolation: refuse to start unless the process really is alone.

`AGENTS.md` §2.9 and `docs/DESIGN_CONTROLS.md` §2.3 require the verdict process to have no egress
except the database and no model or retrieval credentials in its environment. `tests/
test_verdict_isolation.py` proves the *static* half — nothing in `verdict/` can import its way to a
model, a retriever, a socket or a database. That guard cannot say anything about the process that
actually runs: an import graph is not a network.

**Why this lives under `deploy/` and not in `verdict/`.** The static guard forbids `socket` inside
`verdict/`, and an egress probe cannot be written without it. Putting the check in `verdict/startup.
py`, as #257's plan first suggested, would have meant deleting `socket` from that list — the runtime
control destroying the static one it was meant to complete. `deploy/verdict_isolation/` is where
`docs/DESIGN_CONTROLS.md` §1 already placed these artifacts, and the guard never walks it, so the
probe is free to use whatever it needs. See `docs/decisions/F1_6_RUNTIME_ISOLATION.md` D1.

**Why it runs before the process rather than inside it.** The entrypoint calls this and only then
`exec`s the verdict service. A process that is not isolated does not start degraded — it does not
start. That is stronger than checking from within, where the check and the thing being checked share
a fate.

Three rulings shape what follows, all from `docs/decisions/F1_6_RUNTIME_ISOLATION.md`:

* **D3 — addresses are pinned, names are refused.** The permitted destination is given as an IP or
  CIDR, never a hostname. Resolving a name is itself egress, and it leaves a name-to-address step a
  poisoned resolver could redirect. The stricter reading is deliberate: it is the only one where "no
  egress except the database" is literally true rather than true-if-you-trust-the-resolver.
* **D4 — what cannot be determined is refused.** Reachable, blocked and *indeterminate* are three
  outcomes, not two. An unverifiable safety control is a failed one.
* **D5 — the environment is an allow-list.** A deny-list of known model providers fails open the
  first time somebody adds a provider nobody enumerated.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Final

#: Config naming the permitted destination, as IP or CIDR. Required — see `assert_isolated`.
DB_ALLOWLIST_VARIABLE: Final = "GV_VERDICT_DB_ALLOWLIST"

#: Config naming the address the probe dials to prove egress is absent. Required, and an IP.
EGRESS_PROBE_VARIABLE: Final = "GV_VERDICT_EGRESS_PROBE"

#: Everything the verdict process is allowed to see. Not a deny-list of model providers: the
#: invariant is "no external credentials at all" (`AGENTS.md` §5), and only an allow-list can carry
#: an absolute absence. A deny-list is one unlisted vendor away from failing open.
#:
#: The run configuration must **build** an environment containing only these. Inheriting the host
#: environment and deleting the dangerous names is the failure mode this exists to prevent — it is
#: correct exactly until somebody introduces a name the scrubber never heard of.
PERMITTED_ENVIRONMENT: Final[frozenset[str]] = frozenset(
    {
        "DATABASE_URL",
        DB_ALLOWLIST_VARIABLE,
        EGRESS_PROBE_VARIABLE,
        # Process essentials. None of these carry authority to reach anything.
        "HOME",
        "HOSTNAME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PWD",
        "PYTHONHASHSEED",
        "PYTHONPATH",
        "TZ",
    }
)

#: How long the probe waits before it stops being able to tell. Short: a verdict process must not
#: hang on startup, and a long wait does not make an ambiguous answer any less ambiguous.
PROBE_TIMEOUT_SECONDS: Final = 2.0


class IsolationBroken(Exception):
    """The process is not isolated, or cannot prove that it is. Either way it must not start."""


class EgressState(StrEnum):
    """What the probe learned. Three values, because two would hide the interesting one."""

    BLOCKED = "blocked"
    """The kernel has no route. Definitive — this is the state we require."""

    REACHABLE = "reachable"
    """Something answered, so egress exists. The control has failed."""

    INDETERMINATE = "indeterminate"
    """No answer arrived and no route error came back — a silent drop, or a slow network, and
    nothing here can tell those apart. Treated as failure under D4: an unverifiable control is not a
    passed one."""


def parse_allowlist(raw: str | None, *, variable: str) -> tuple[ipaddress.IPv4Network, ...]:
    """Parse pinned addresses, refusing anything that would need resolving.

    A hostname is rejected rather than looked up. The lookup would be egress performed by the very
    check that is meant to prove egress is impossible, and it would leave the destination decided by
    whatever answered the query.
    """
    if raw is None or not raw.strip():
        raise IsolationBroken(
            f"{variable} is not set. The permitted destination has to be stated — there is no "
            "default, because a default here would be a guess about which network the process is "
            "on, and guessing is how a control quietly stops applying."
        )

    networks: list[ipaddress.IPv4Network] = []
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        try:
            networks.append(ipaddress.IPv4Network(entry, strict=False))
        except ValueError as error:
            raise IsolationBroken(
                f"{variable} entry {entry!r} is not an IP address or CIDR block. Hostnames are "
                "refused: resolving one is itself egress, and it leaves a name-to-address step that "
                "a poisoned resolver could point somewhere else. Pin the address."
            ) from error

    if not networks:
        raise IsolationBroken(f"{variable} is set but names no addresses.")
    return tuple(networks)


def parse_probe_target(raw: str | None) -> tuple[str, int]:
    """Parse `address:port` for the probe. The address is an IP for the same reason as above."""
    if raw is None or not raw.strip():
        raise IsolationBroken(
            f"{EGRESS_PROBE_VARIABLE} is not set. Without somewhere to dial, the absence of egress "
            "is assumed rather than tested, and an assumed control is the thing this whole story "
            "exists to replace."
        )
    address, separator, port_text = raw.strip().rpartition(":")
    if not separator:
        raise IsolationBroken(f"{EGRESS_PROBE_VARIABLE} must be 'address:port', got {raw!r}.")
    try:
        ipaddress.IPv4Address(address)
    except ValueError as error:
        raise IsolationBroken(
            f"{EGRESS_PROBE_VARIABLE} address {address!r} is not an IP address. Hostnames are "
            "refused here too — resolving one would be the egress the probe is trying to disprove."
        ) from error
    try:
        port = int(port_text)
    except ValueError as error:
        raise IsolationBroken(
            f"{EGRESS_PROBE_VARIABLE} port {port_text!r} is not a number."
        ) from error
    return address, port


def check_environment(environment: Mapping[str, str]) -> None:
    """Refuse if the process holds anything outside the allow-list.

    The message names what was found. A control that says only "environment not permitted" sends
    somebody to read this file to find out what it objected to.
    """
    unexpected = sorted(set(environment) - PERMITTED_ENVIRONMENT)
    if unexpected:
        raise IsolationBroken(
            f"the verdict process environment holds {len(unexpected)} variable(s) it is not "
            f"permitted: {unexpected}. Nothing outside the allow-list may be present — the run "
            "configuration must build this environment rather than inherit one and remove the "
            "dangerous names, because removal only ever covers the names somebody thought of."
        )


def probe_egress(
    address: str,
    port: int,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    connect: object = None,
) -> EgressState:
    """Dial an address that must be unreachable, and report which of the three states applies.

    `connect` exists for tests, which cannot rely on the network state of whatever machine they run
    on. Production passes nothing and gets the real socket.

    Reading the errors matters more than it looks. `ENETUNREACH`/`EHOSTUNREACH` mean the kernel
    holds no route at all — that is the definitive answer and the one a properly built network
    namespace gives. A refusal or a completed connection means a packet reached something, so egress
    exists. A timeout means a firewall silently dropped it *or* the network is merely slow, and
    nothing distinguishes those from here.

    That last case refusing to start has a real consequence worth stating plainly: a deployment that
    blocks egress with a DROP rule will not start, while one that gives the process no route at all
    will. That is intended. It pushes the deployment towards the configuration whose isolation can
    be demonstrated rather than the one that merely looks isolated from the inside.
    """
    dial = connect if connect is not None else _connect
    try:
        dial(address, port, timeout)  # type: ignore[operator]
    except OSError as error:
        import errno

        if error.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN}:
            return EgressState.BLOCKED
        if error.errno in {errno.ETIMEDOUT} or isinstance(error, TimeoutError):
            return EgressState.INDETERMINATE
        # Refused, reset, or anything else that implies a peer: something was there to answer.
        return EgressState.REACHABLE
    return EgressState.REACHABLE


def _connect(address: str, port: int, timeout: float) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect((address, port))


def assert_isolated(
    environment: Mapping[str, str] | None = None,
    *,
    connect: object = None,
) -> None:
    """Raise `IsolationBroken` unless this process is provably isolated.

    Called by the entrypoint before the verdict service is `exec`ed. Every failure raises rather
    than warns: `docs/DESIGN_CONTROLS.md` §2.3 requires a verdict process that finds itself with
    network access to fail to start rather than run degraded, and a warning is how a control becomes
    a line in a log nobody reads.
    """
    environment = os.environ if environment is None else environment

    check_environment(environment)
    allowlist = parse_allowlist(
        environment.get(DB_ALLOWLIST_VARIABLE), variable=DB_ALLOWLIST_VARIABLE
    )
    address, port = parse_probe_target(environment.get(EGRESS_PROBE_VARIABLE))

    if _within(address, allowlist):
        raise IsolationBroken(
            f"the egress probe target {address} is inside the permitted range {[str(n) for n in allowlist]}. "
            "Probing an address the process is allowed to reach proves nothing at all — it would "
            "report success in exactly the case this check exists to catch."
        )

    state = probe_egress(address, port, connect=connect)
    if state is EgressState.REACHABLE:
        raise IsolationBroken(
            f"{address}:{port} is reachable, so this process has egress beyond the database. "
            "The verdict service must not run: a path out is a path by which something other than "
            "exact arithmetic on qualified evidence could influence a verdict."
        )
    if state is EgressState.INDETERMINATE:
        raise IsolationBroken(
            f"whether {address}:{port} is reachable could not be determined — the connection was "
            "dropped silently rather than refused or routed. That is not the same as isolation, "
            "and an unverifiable control is a failed one. Give the process no route to anything "
            "but the database, rather than a rule that discards its packets."
        )


def _within(address: str, networks: Iterable[ipaddress.IPv4Network]) -> bool:
    candidate = ipaddress.IPv4Address(address)
    return any(candidate in network for network in networks)
