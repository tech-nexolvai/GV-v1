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

**What establishes isolation, and what merely corroborates it.** Worth being exact, because two
earlier versions of this module were not. Dialling an address and finding it unreachable shows that
*that address* is unreachable; it says nothing about anywhere else, and no number of samples turns
that into "no egress except the database". The claim is established from the kernel routing tables,
which are the complete list of places a packet can be sent: every route must stay inside the
permitted range, there must be no default route in either its gateway or on-link form, and IPv6 must
offer nothing globally routable. `assert_no_route_off_the_network` checks that; `probe_egress`
corroborates it, and exists so that a routing table which disagrees with reality is caught rather
than believed.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from pathlib import Path
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
    if not 1 <= port <= 65535:
        # Not pedantry: a port above 65535 makes `socket.connect` raise `OverflowError`, which is
        # not an `OSError`, so it would escape the probe's handling and surface as a traceback
        # instead of the refusal this module promises. A control that crashes instead of refusing
        # is one whose contract nobody can rely on.
        raise IsolationBroken(f"{EGRESS_PROBE_VARIABLE} port {port} is outside 1-65535.")
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
    connect: Callable[[str, int, float], None] | None = None,
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
        dial(address, port, timeout)
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


#: Where Linux publishes the kernel routing tables. Read rather than probed, because they answer a
#: different and stronger question — see `assert_no_route_off_the_network`.
ROUTE_TABLE: Final = "/proc/net/route"
IPV6_ROUTE_TABLE: Final = "/proc/net/ipv6_route"

#: IPv6 destinations a route may legitimately cover without granting egress: loopback, link-local
#: and multicast. Anything else is somewhere a packet can actually go.
_IPV6_LOCAL: Final = (
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fe80::/10"),
    ipaddress.IPv6Network("ff00::/8"),
    ipaddress.IPv6Network("::/128"),
)

ReadTable = Callable[[], str]


def _read_route_table() -> str:
    return Path(ROUTE_TABLE).read_text(encoding="ascii")


def _read_ipv6_route_table() -> str:
    return Path(IPV6_ROUTE_TABLE).read_text(encoding="ascii")


def _little_endian_network(destination: str, mask: str) -> ipaddress.IPv4Network:
    """Decode one `/proc/net/route` row. Both columns are little-endian hex, not dotted quads."""
    address = ipaddress.IPv4Address(struct.unpack("<I", bytes.fromhex(destination))[0])
    netmask = ipaddress.IPv4Address(struct.unpack("<I", bytes.fromhex(mask))[0])
    return ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)


def assert_no_route_off_the_network(
    allowlist: Iterable[ipaddress.IPv4Network],
    *,
    read: ReadTable | None = None,
    read_ipv6: ReadTable | None = None,
) -> None:
    """Refuse unless every route the kernel holds stays inside the permitted range.

    **Why this exists, and why the probe alone was not enough.** Dialling one address and finding it
    unreachable shows that *that address* is unreachable. It says nothing about the rest of the
    internet: a network could block the probe target and still route somewhere else entirely, and the
    probe would report success. The claim "no egress except the database" cannot be established by
    sampling, however many addresses are sampled.

    The routing table answers it properly, because it is the complete list of places a packet can be
    sent. Three things have to be checked and an earlier version of this function checked only the
    first, which made it weaker than it read:

    * **Any default route, gateway or not.** Requiring a non-zero gateway missed the on-link form
      (`default dev eth0`), which grants exactly the same reachability.
    * **Every other route, against the allowlist.** A route to some unrelated subnet is egress even
      though it is not a default route, so the permitted range has to be passed in and compared —
      otherwise this function is only ever checking for one shape of mistake.
    * **IPv6.** `/proc/net/route` is IPv4 only. A container with working IPv6 would have sailed
      through a check that never looked. An absent IPv6 table is proof the stack is off, which is
      the one absence here that is an answer rather than an unknown.

    Unreadable IPv4 table means unknown, and unknown is refused (D4). On a developer machine there is
    no `/proc/net/route` and this refuses — correctly, because a laptop is not an isolated network.
    """
    permitted = tuple(allowlist)
    reader = read if read is not None else _read_route_table
    try:
        table = reader()
    except OSError as error:
        raise IsolationBroken(
            f"the kernel routing table at {ROUTE_TABLE} could not be read, so where this process can "
            "send a packet is unknown. Unknown is refused: an unverifiable control is a failed one. "
            "This is the expected result outside a Linux container, and the verdict service is not "
            "meant to run outside one."
        ) from error

    escapes: list[str] = []
    for line in table.splitlines()[1:]:  # the first line is the column header
        fields = line.split()
        if len(fields) < 8:
            continue
        interface, destination, gateway, mask = fields[0], fields[1], fields[2], fields[7]
        if interface == "lo":
            continue
        try:
            network = _little_endian_network(destination, mask)
        except (ValueError, struct.error):
            escapes.append(f"{interface}: unparseable route {destination}/{mask}")
            continue
        if network.prefixlen == 0:
            # A default route, with or without a gateway. The on-link form carries no gateway and
            # grants the same reachability, which is why the gateway is not part of this test.
            escapes.append(f"{interface}: default route (via {gateway})")
        elif not any(network.subnet_of(allowed) for allowed in permitted):
            escapes.append(f"{interface}: route to {network}, which is outside the permitted range")

    if escapes:
        raise IsolationBroken(
            "the kernel can send packets outside the permitted range — "
            + "; ".join(escapes)
            + f". Permitted: {[str(n) for n in permitted]}. Isolation here is a property of the "
            "network, not of a firewall rule: give the verdict service a network with no gateway "
            "(`internal: true`) whose subnet the allowlist covers, rather than one it is merely "
            "discouraged from using."
        )

    _assert_no_ipv6_route(read_ipv6)


def _assert_no_ipv6_route(read: ReadTable | None = None) -> None:
    """Refuse if IPv6 offers a way out. An absent table means the stack is off, which is an answer."""
    reader = read if read is not None else _read_ipv6_route_table
    try:
        table = reader()
    except OSError:
        # Not the same as the IPv4 case. There, an unreadable table hides routes that may exist;
        # here, the file's absence *is* the fact that there is no IPv6 stack to route anything.
        return

    reachable: list[str] = []
    for line in table.splitlines():
        fields = line.split()
        if len(fields) < 10:
            continue
        destination, prefix_length, interface = fields[0], fields[1], fields[-1]
        if interface == "lo":
            continue
        try:
            address = ipaddress.IPv6Address(int(destination, 16))
            network = ipaddress.IPv6Network((address, int(prefix_length, 16)), strict=False)
        except ValueError:
            reachable.append(f"{interface}: unparseable IPv6 route {destination}/{prefix_length}")
            continue
        if not any(network.subnet_of(local) for local in _IPV6_LOCAL):
            reachable.append(f"{interface}: IPv6 route to {network}")

    if reachable:
        raise IsolationBroken(
            "IPv6 offers a way out of this network — "
            + "; ".join(reachable)
            + ". The database is reached over IPv4, so IPv6 has no legitimate destination here. "
            "Disable it for this container rather than relying on nothing choosing to use it."
        )


def assert_isolated(
    environment: Mapping[str, str] | None = None,
    *,
    connect: Callable[[str, int, float], None] | None = None,
    read_routes: ReadTable | None = None,
    read_ipv6_routes: ReadTable | None = None,
) -> None:
    """Raise `IsolationBroken` unless this process is provably isolated.

    Called by the entrypoint before the verdict service is `exec`ed. Every failure raises rather than
    warns: `docs/DESIGN_CONTROLS.md` §2.3 requires a verdict process that finds itself with network
    access to fail to start rather than run degraded, and a warning is how a control becomes a line
    in a log nobody reads.
    """
    environment = os.environ if environment is None else environment

    check_environment(environment)
    allowlist = parse_allowlist(
        environment.get(DB_ALLOWLIST_VARIABLE), variable=DB_ALLOWLIST_VARIABLE
    )
    address, port = parse_probe_target(environment.get(EGRESS_PROBE_VARIABLE))

    if _within(address, allowlist):
        raise IsolationBroken(
            f"the egress probe target {address} is inside the permitted range "
            f"{[str(n) for n in allowlist]}. Probing an address the process is allowed to reach "
            "proves nothing at all — it would report success in exactly the case this check exists "
            "to catch."
        )

    # The property first: every route the kernel holds stays inside the permitted range. This is
    # what establishes "no egress except the database". The probe below cannot, because one
    # unreachable address says nothing about the others.
    assert_no_route_off_the_network(allowlist, read=read_routes, read_ipv6=read_ipv6_routes)

    # Then the corroboration. If the routing table says there is no way out and a packet reaches
    # something anyway, one of the two is wrong and neither should be trusted.
    state = probe_egress(address, port, connect=connect)
    if state is EgressState.REACHABLE:
        raise IsolationBroken(
            f"{address}:{port} is reachable, so this process has egress beyond the database — and it "
            "reached it despite a routing table that says it cannot, so the two disagree. The "
            "verdict service must not run: a path out is a path by which something other than exact "
            "arithmetic on qualified evidence could influence a verdict."
        )
    if state is EgressState.INDETERMINATE:
        raise IsolationBroken(
            f"whether {address}:{port} is reachable could not be determined — the connection was "
            "dropped silently rather than refused or routed. That is not the same as isolation, and "
            "an unverifiable control is a failed one. Give the process no route to anything but the "
            "database, rather than a rule that discards its packets."
        )


def _within(address: str, networks: Iterable[ipaddress.IPv4Network]) -> bool:
    candidate = ipaddress.IPv4Address(address)
    return any(candidate in network for network in networks)
