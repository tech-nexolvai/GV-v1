"""Installing every operation, and proving the list cannot drift.

`app/api/operations.py` reports what the engine can do. If that report is assembled from a list
somebody maintains by hand, it stops being true the first time a module is added and nobody
remembers — which is the failure C2.4's acceptance criterion names outright.

So the tests here are about *completeness*, not about any particular operation. They fail when a
module defines specs that installing everything does not produce.
"""

from __future__ import annotations

import pathlib

import pytest

from verdict.operations import declared_specs, installed_names, register_all
from verdict.registry import REGISTRY


def test_registration_covers_every_module() -> None:
    """**The anti-drift test.** Compares what the package *declares* against what installing it
    *produces*, so a module whose installer is missing, misnamed, or simply never called shows up
    here rather than as an endpoint quietly reporting fewer operations than exist.
    """
    register_all()

    declared = {spec.name for spec in declared_specs()}
    assert declared, "no specs were discovered at all, so this test would pass vacuously"
    missing = declared - installed_names()
    assert not missing, (
        f"these operations are declared but never installed: {sorted(missing)}. The registry is "
        "what the engine resolves against and what the API reports, so an operation that exists in "
        "a module and not in the registry is one the engine cannot run."
    )


def test_every_declared_spec_has_a_distinct_name() -> None:
    """Two specs sharing a name means one silently wins, and which one depends on import order."""
    names = [spec.name for spec in declared_specs()]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"declared more than once: {duplicates}"


def test_registering_twice_is_a_no_op() -> None:
    """The API may install on first use and a test may install in a fixture. Doing both must not
    raise, or the order in which two callers happen to run decides whether the app starts."""
    register_all()
    before = dict(REGISTRY)
    register_all()
    assert dict(REGISTRY) == before


def test_no_operations_module_is_left_out_of_the_list() -> None:
    """**The anti-drift test, and the reason a written-out list is acceptable in `verdict/`.**

    `verdict/operations/__init__.py` names its modules explicitly because `importlib` is forbidden
    inside `verdict/` — dynamic import is a path by which unreviewed code could reach the thing that
    decides PASS or FAIL, and `tests/test_verdict_isolation.py` enforces that. A test may import
    dynamically where the decision path may not, so the cost of the written list is paid here.

    Add a module and forget the list, and this fails.
    """
    import verdict.operations as package

    directory = pathlib.Path(package.__file__).parent
    on_disk = {
        path.stem
        for path in directory.glob("*.py")
        if path.stem != "__init__" and not path.stem.startswith("_")
    }

    assert on_disk, "found no operation modules at all, so this test would pass vacuously"
    missing = on_disk - package.REGISTERED_MODULES
    assert not missing, (
        f"these operation modules are not in `_MODULES`: {sorted(missing)}. They exist on disk, so "
        "the engine's authors think they are real, and nothing installs them."
    )


def test_discovery_finds_the_modules_rather_than_a_hard_coded_list() -> None:
    """If discovery silently found nothing, every test above would pass while proving nothing — the
    same shape as an enumeration asserting an empty list. This pins that it really walks the package.
    """
    register_all()
    installed = installed_names()

    assert len(installed) >= 4, f"suspiciously few operations discovered: {sorted(installed)}"
    for spec in declared_specs():
        assert REGISTRY[spec.name].version == spec.version


@pytest.mark.parametrize("attribute", ["declared_specs", "register_all", "installed_names"])
def test_the_package_exposes_what_the_api_layer_needs(attribute: str) -> None:
    """The API imports these by name. Renaming one without updating the caller is a runtime failure
    at import, which is early — but only if something asserts the names exist."""
    from verdict import operations

    assert callable(getattr(operations, attribute))
