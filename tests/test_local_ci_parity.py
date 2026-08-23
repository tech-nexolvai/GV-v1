"""The local check chain must not quietly fall behind `.github/workflows/ci.yml`.

`scripts/check.py` exists so a developer can know, before pushing, what CI will say. That promise
holds only while the two agree, and nothing structural made them agree — the script listed its steps
and the workflow listed its own, and a step added to one could sit unmirrored in the other
indefinitely. A local chain that is *nearly* CI is worse than an obviously partial one, because it
is trusted.

So this reads both and fails when a command CI runs has no counterpart locally. Every exception is
named in `GITHUB_ONLY_STEPS` with a reason, which is the point: the exceptions become a short
reviewable list rather than an unknown gap.

**This test does not check that the commands are identical.** CI runs `pytest -q` through a shell
that tolerates exit code 5; the local chain runs it directly. Insisting on string equality would
mean rewriting one to match the other's incidental shape, and the thing worth protecting is that
every *check* is present, not that every *invocation* matches character for character.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from scripts.check import GITHUB_ONLY, STEPS, ci_python_version

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: CI steps deliberately not mirrored locally, each with the reason. Anything here is a decision;
#: anything missing from both this and the local chain is a gap.
GITHUB_ONLY_STEPS = {
    # Needs the network and a live third party. Reproducing it locally would make a wifi outage look
    # like a broken repository, which is how a check earns being ignored.
    "Config still satisfies the published schema": "network — CodeRabbit's live schema",
    # Runner setup, not a check. Nothing to mirror: the local chain runs in whatever environment the
    # developer has, and reports when that differs from what CI pins.
    "Install": "runner setup",
    "Install test tooling": "runner setup",
    "Install semgrep": "runner setup",
}

#: Commands CI runs that the local chain covers under a different name or as part of a wider step.
#: Keyed by a distinctive fragment of the CI command, valued with why it is already covered.
COVERED_INDIRECTLY = {
    "git ls-files --error-unmatch data/": (
        "tests/test_repo_hygiene.py::test_no_client_data_is_tracked asserts the same thing, and the "
        "local chain runs that module first"
    ),
    "tests/app/test_float_column_guard.py": (
        "collected by the full `pytest -q` step, which the local chain runs"
    ),
}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _ci_steps() -> list[tuple[str, str]]:
    """Every named `run:` step in the workflow, as (job, name)."""
    steps: list[tuple[str, str]] = []
    for job_name, job in _workflow()["jobs"].items():
        for step in job.get("steps", []):
            if "run" in step and "name" in step:
                steps.append((job_name, step["name"]))
    return steps


def _ci_commands() -> list[tuple[str, str]]:
    """Every `run:` block in the workflow, as (name, command text)."""
    blocks: list[tuple[str, str]] = []
    for job in _workflow()["jobs"].values():
        for step in job.get("steps", []):
            if "run" in step:
                blocks.append((step.get("name", "(unnamed)"), step["run"]))
    return blocks


def _local_commands() -> str:
    return "\n".join(" ".join(step.command) for step in STEPS)


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "command"), _ci_commands(), ids=lambda v: str(v)[:40])
def test_every_ci_command_is_covered_locally(name: str, command: str) -> None:
    """A check CI runs is a check the local chain runs, or an entry in one of the two allow-lists.

    Input: one `run:` block from the workflow. Outcome: it maps to a local step. Why: a developer
    reading "All checks passed" must not be missing something CI will reject.
    """
    if name in GITHUB_ONLY_STEPS:
        return

    local = _local_commands()

    for fragment in COVERED_INDIRECTLY:
        if fragment in command:
            return

    # The meaningful part of a CI command is the tool and its target: `pytest tests/x.py`,
    # `ruff check .`, `mypy verdict rules evidence`. Compare on those tokens rather than on the
    # surrounding shell, which differs for reasons that are not checks.
    targets = re.findall(r"(?:pytest|ruff|black|mypy|semgrep|pip)\s+[^\n|&;]+", command)
    assert targets, (
        f"CI step {name!r} runs a command this test cannot classify:\n{command}\n"
        "Either add it to the local chain, or record it in GITHUB_ONLY_STEPS with a reason."
    )

    for target in targets:
        tool = target.split()[0]
        # `pip install` is setup, not a check. `pip check` is a check.
        if tool == "pip" and "install" in target:
            continue
        assert tool in local, (
            f"CI runs {tool!r} (in step {name!r}) and scripts/check.py does not.\n"
            f"CI command: {target.strip()}\n"
            "Add a Step to STEPS, or record the exception in GITHUB_ONLY_STEPS."
        )


def test_every_pytest_module_ci_names_is_run_locally() -> None:
    """CI names specific test modules in their own steps; each must be reachable locally.

    Those steps exist because a targeted failure is legible in a log where a suite-wide failure is
    not. Locally they may be covered by the full run — what matters is that none is absent.
    """
    local = _local_commands()
    for name, command in _ci_commands():
        if name in GITHUB_ONLY_STEPS:
            continue
        for module in re.findall(r"tests/[\w/]+\.py", command):
            covered = module in local or any(f in command for f in COVERED_INDIRECTLY)
            assert (
                covered or "-m pytest" in local
            ), f"CI runs {module} in step {name!r} and no local step reaches it."


def test_mypy_covers_the_same_packages_locally_as_on_ci() -> None:
    """The tool-level check above would miss a package added to one mypy invocation and not the other.

    That is the realistic drift: a new package joins the tree, CI type-checks it, and the local chain
    keeps checking the old list while still reporting "mypy" as covered. Compare the arguments.
    """
    ci_packages: set[str] = set()
    for _name, command in _ci_commands():
        for invocation in re.findall(r"mypy\s+([^\n|&;]+)", command):
            ci_packages.update(invocation.split())

    local_packages: set[str] = set()
    for step in STEPS:
        if "mypy" in step.command:
            local_packages.update(step.command[step.command.index("mypy") + 1 :])

    missing = ci_packages - local_packages
    assert not missing, (
        f"CI type-checks {sorted(missing)} and the local chain does not. "
        "Add them to the matching mypy Step in scripts/check.py."
    )


def test_the_strict_mypy_packages_are_the_safety_critical_ones() -> None:
    """Strict mode must cover exactly the zones AGENTS.md calls safety-critical.

    Locally *and* on CI. A package silently dropped from the strict list keeps type-checking and
    stops being held to the standard the golden rules assume.
    """
    expected = {"verdict", "rules", "evidence"}

    strict_local = {
        frozenset(step.command[step.command.index("mypy") + 1 :])
        for step in STEPS
        if "mypy" in step.command and not step.advisory
    }
    assert (
        expected in strict_local
    ), f"the local strict mypy step must cover exactly {sorted(expected)}; found {strict_local}"

    strict_ci = {
        frozenset(inv.split())
        for name, command in _ci_commands()
        for inv in re.findall(r"mypy\s+([^\n|&;]+)", command)
        if "strict" in name.lower()
    }
    assert (
        expected in strict_ci
    ), f"CI's strict mypy step must cover exactly {sorted(expected)}; found {strict_ci}"


def test_the_python_version_is_read_from_the_workflow_not_restated() -> None:
    """`scripts/check.py` must derive the pinned version, so a bump cannot leave it stale.

    Input: the workflow. Outcome: one unambiguous version. Why: the script reports drift against
    this value, and a hard-coded copy would report drift against history.
    """
    pinned = ci_python_version()
    assert pinned is not None, (
        "The workflow does not name exactly one python-version. scripts/check.py reads it to report "
        "interpreter drift; an ambiguous answer makes that report meaningless."
    )
    assert re.fullmatch(r"\d+\.\d+", pinned), f"unexpected version format: {pinned!r}"


def test_the_declared_floor_is_not_above_the_version_ci_tests() -> None:
    """`requires-python` must not promise support for a version CI never exercises."""
    import tomllib

    declared = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"][
        "requires-python"
    ]
    floor = re.search(r"(\d+\.\d+)", declared)
    assert floor, f"cannot read a floor from requires-python={declared!r}"
    pinned = ci_python_version()
    assert pinned is not None
    assert tuple(int(p) for p in floor.group(1).split(".")) == tuple(
        int(p) for p in pinned.split(".")
    ), (
        f"pyproject declares {floor.group(1)} as the floor and CI tests {pinned}. "
        "Whichever is wrong, they must agree — CI is what proves the promise."
    )


# ---------------------------------------------------------------------------
# The exceptions stay honest
# ---------------------------------------------------------------------------


def test_every_github_only_exception_still_exists_in_the_workflow() -> None:
    """A stale exception is worse than none: it silently excuses a check nobody runs."""
    names = {name for _job, name in _ci_steps()}
    for excused in GITHUB_ONLY_STEPS:
        assert excused in names, (
            f"GITHUB_ONLY_STEPS excuses {excused!r}, which the workflow no longer has. "
            "Remove the exception."
        )


def test_the_script_documents_its_github_only_checks_to_the_developer() -> None:
    """The exceptions must be visible at the end of a run, not only in this test file."""
    assert GITHUB_ONLY, "scripts/check.py must tell a developer what it did not check"
    assert any(
        "schema" in name.lower() for name in GITHUB_ONLY
    ), "the weekly published-schema check is the clearest GitHub-only case and must be named"


def test_indirect_coverage_claims_name_a_real_local_step() -> None:
    """Each 'covered elsewhere' claim must point at something the local chain actually runs."""
    local = _local_commands()
    assert "tests/test_repo_hygiene.py" in local, (
        "COVERED_INDIRECTLY claims the data/ guard is covered by the hygiene module, "
        "but the local chain does not run it"
    )
    assert "-m pytest" in local, (
        "COVERED_INDIRECTLY claims the float-column guard is covered by the full test run, "
        "but the local chain has no full pytest step"
    )
