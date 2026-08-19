"""Reviewed deterministic verdict operations, and the one call that installs all of them.

Every module here defines its specs in a `*_SPECS` tuple and installs them with its own
`register_*_operations()`. Nothing called those functions, so importing this package left
`verdict.registry.REGISTRY` **empty** — the engine only saw an operation if some caller had happened
to install it first. `register_all()` is that missing call, and C2.4 (#206) needs it because the API
has to answer "what operations exist?" from the registry rather than from a list of its own.

**Why this list is written out rather than discovered.** The obvious implementation walks the package
with `pkgutil` and imports what it finds. `tests/test_verdict_isolation.py` refuses it, and the
refusal is right: `importlib` is on the forbidden list for `verdict/` because dynamic import is a path
by which code nobody reviewed can reach the thing that decides PASS or FAIL. AGENTS.md §2.1 wants the
verdict's reachable surface to be knowable by reading it, and a package that imports whatever is
sitting in its own directory is not.

So the cost is a list that could go stale, and that cost is paid where it is safe to pay:
`tests/verdict/test_operations_registration.py` walks the directory — a test may import dynamically,
`verdict/` may not — and fails if a module exists that this list does not name. Drift is caught
without dynamic import ever entering the decision path.

Source: `docs/DESIGN_PLATFORM.md` §4.1 · Verification: `tests/verdict/test_operations_registration.py`
"""

from __future__ import annotations

from collections.abc import Callable

from verdict.operations import aggregate, alignment, pairwise, scalar
from verdict.registry import REGISTRY, OperationSpec

#: Every module of operations, with the specs it declares and the installer that registers them.
#:
#: Written out, and checked by a test that reads the directory. Adding a module means adding a line
#: here; forgetting to is a test failure rather than an endpoint that quietly reports fewer
#: operations than the engine can run.
_MODULES: tuple[tuple[str, tuple[OperationSpec, ...], Callable[[], None]], ...] = (
    ("aggregate", aggregate.AGGREGATE_SPECS, aggregate.register_aggregate_operations),
    ("alignment", alignment.ALIGNMENT_SPECS, alignment.register_alignment_operations),
    ("pairwise", pairwise.PAIRWISE_SPECS, pairwise.register_pairwise_operations),
    ("scalar", scalar.SCALAR_SPECS, scalar.register_scalar_operations),
)

#: The module names this package knows about. The test compares it against the directory.
REGISTERED_MODULES: frozenset[str] = frozenset(name for name, _, _ in _MODULES)


def declared_specs() -> tuple[OperationSpec, ...]:
    """Every spec these modules declare, whether or not it has been installed.

    The "should" against which the registry's "is" can be compared, which is the only way to tell a
    registry that is complete from one that is merely consistent with itself.
    """
    return tuple(spec for _, specs, _ in _MODULES for spec in specs)


def register_all() -> None:
    """Install every operation this package defines. Safe to call more than once.

    `verdict.registry.register()` refuses **every** re-registration, including an identical one. That
    strictness is deliberate — it makes a spec quietly replaced by another of the same name
    impossible — and it is not weakened here. Instead a module's installer is skipped when everything
    it declares is already present, so a second call does nothing.

    A *partially* installed module is not skipped: its installer runs and `register()` raises. That
    is the case worth failing on, because it means two callers disagree about what is registered and
    the operation that loses is the one the engine will not find.

    The installers are called rather than the specs registered directly here, so that
    `declared_specs()` and the registry remain two independent readings that a test can compare.
    """
    for _, specs, installer in _MODULES:
        declared = {spec.name for spec in specs}
        if declared and declared <= set(REGISTRY):
            continue
        installer()


def installed_names() -> frozenset[str]:
    """What is in the registry right now, so a caller need not import the registry itself."""
    return frozenset(REGISTRY)
