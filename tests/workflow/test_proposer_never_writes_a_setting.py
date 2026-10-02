"""The code that finds a setting can point at a passage, and can never write the setting (#849).

Two guards, each proven to fire before it is trusted:

- **`gv-proposer-never-writes-a-setting`**, the semgrep rule. It forbids building a
  `ParameterValue`, a user input or a company-standard set, and calling a settings store, in
  `workflow/parameter_proposals.py`, the value-hunter (`workflow/value_hunter*`, not written yet)
  and `retrieval/`. Planted violations must be caught, and the shipped files must pass.
- **No import of `app.api`**, where the settings are stored (`_store` in `app/api/measurements.py`
  and `app/api/company_settings.py`). Walked transitively, module by module, from every file the
  rule covers.

Semgrep is installed in CI's safety-guards job, which runs this file; elsewhere the semgrep half
skips. The shipped files are copied into a temporary tree and scanned there, so the result does not
depend on the path the repository is checked out at.

Source: issue #849, plan step 3.2 on #798.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES = REPO_ROOT / ".semgrep" / "gv-rules.yaml"
RULE_ID = "gv-proposer-never-writes-a-setting"

#: Every top-level package in this repo, so the walk knows what to descend into.
PROJECT_PACKAGES = frozenset(
    {
        "app",
        "eval",
        "evidence",
        "extraction",
        "reports",
        "retrieval",
        "rules",
        "units",
        "verdict",
        "vocabulary",
        "workflow",
    }
)

#: One file per place the rule covers, the value-hunter's two possible shapes included.
COVERED = (
    "workflow/parameter_proposals.py",
    "workflow/value_hunter.py",
    "workflow/value_hunter/plan.py",
    "retrieval/package_text.py",
    "retrieval/lanes/exact.py",
)

#: Each call the rule forbids, as a bare name and through a module.
FORBIDDEN_CALLS = (
    "ParameterValue(value, provenance, set_by, set_at)",
    "parameters.ParameterValue(value, provenance, set_by, set_at)",
    "user_input(value, set_by='a', set_at=now)",
    "parameters.user_input(value, set_by='a', set_at=now)",
    "user_input_set('project', 1, values)",
    "parameters.user_input_set('project', 1, values)",
    "seed_company_standards(values)",
    "parameters.seed_company_standards(values)",
    "_store(session, values)",
    "measurements._store(session, values)",
)


def _semgrep(root: Path) -> list[dict[str, object]]:
    """This rule's findings in the tree at `root`, from semgrep's JSON."""
    semgrep = shutil.which("semgrep")
    if semgrep is None:
        pytest.skip("semgrep is installed in the CI safety-guards job")
    completed = subprocess.run(
        [semgrep, "--config", str(RULES), "--json", "--quiet", "--metrics=off", "."],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    output = json.loads(completed.stdout)
    assert not output["errors"], output["errors"]
    # The rule id is prefixed with the config's path, so it is matched by its end.
    return [finding for finding in output["results"] if str(finding["check_id"]).endswith(RULE_ID)]


def _line(finding: dict[str, object]) -> int:
    start = finding["start"]
    assert isinstance(start, dict)
    return int(start["line"])


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_the_rule_catches_every_planted_write(tmp_path: Path) -> None:
    """**Outcome: every forbidden call, in every covered place, is a finding.**"""
    for relative in COVERED:
        _write(tmp_path, relative, "\n".join(FORBIDDEN_CALLS) + "\n")

    findings = _semgrep(tmp_path)

    found = {(str(finding["path"]), _line(finding)) for finding in findings}
    expected = {
        (relative, line) for relative in COVERED for line in range(1, len(FORBIDDEN_CALLS) + 1)
    }
    assert found == expected


def test_the_rule_leaves_other_code_and_other_places_alone(tmp_path: Path) -> None:
    """Reading a stored setting and filing a pointer are allowed in the covered places, and a
    setting is written where settings are written. Outcome: no finding."""
    allowed = (
        "rows = session.scalars(select(ParameterValue))\n"
        "pointer = ParameterProposal(setting_name='countertop_overhang')\n"
    )
    for relative in COVERED:
        _write(tmp_path, relative, allowed)
    _write(tmp_path, "app/api/company_settings.py", "\n".join(FORBIDDEN_CALLS) + "\n")
    _write(tmp_path, "tests/retrieval/test_x.py", "\n".join(FORBIDDEN_CALLS) + "\n")

    assert _semgrep(tmp_path) == []


def test_the_shipped_files_pass_the_rule(tmp_path: Path) -> None:
    """The real proposer and the real `retrieval/`, scanned as shipped. Outcome: no finding."""
    shipped = [
        REPO_ROOT / "workflow" / "parameter_proposals.py",
        *sorted((REPO_ROOT / "workflow").glob("value_hunter*")),
        REPO_ROOT / "retrieval",
    ]
    for source in shipped:
        target = tmp_path / source.relative_to(REPO_ROOT)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    assert (tmp_path / "workflow" / "parameter_proposals.py").is_file()

    assert _semgrep(tmp_path) == []


# ---------------------------------------------------------------------------
# No import of app.api
# ---------------------------------------------------------------------------


def _module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _file_for(module: str, root: Path) -> Path | None:
    base = root / Path(*module.split("."))
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    candidate = base.with_suffix(".py")
    return candidate if candidate.exists() else None


def _imports_in(path: Path) -> set[str]:
    """Dotted names one file imports, anywhere in it: `from x import y` gives `x` and `x.y`, so a
    submodule imported by name is followed too."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _reachable(files: Iterable[Path], root: Path) -> dict[str, list[str]]:
    """Every module reachable from `files`, with a shortest chain that reaches it."""
    queue = [(_module_name(path, root), [_module_name(path, root)]) for path in files]
    chains: dict[str, list[str]] = {}
    seen: set[str] = set()
    while queue:
        module, chain = queue.pop(0)
        if module in seen:
            continue
        seen.add(module)
        file = _file_for(module, root)
        if file is None:
            continue
        for imported in sorted(_imports_in(file)):
            chains.setdefault(imported, [*chain, imported])
            if imported.split(".")[0] in PROJECT_PACKAGES and imported not in seen:
                queue.append((imported, [*chain, imported]))
    return chains


def _covered_files(root: Path) -> list[Path]:
    files = [
        root / "workflow" / "parameter_proposals.py",
        *sorted((root / "retrieval").rglob("*.py")),
    ]
    for hunter in sorted((root / "workflow").glob("value_hunter*")):
        files.extend(sorted(hunter.rglob("*.py")) if hunter.is_dir() else [hunter])
    return [path for path in files if path.is_file()]


def _api_reached(root: Path) -> list[str]:
    chains = _reachable(_covered_files(root), root)
    return [
        " -> ".join(chain)
        for module, chain in sorted(chains.items())
        if module == "app.api" or module.startswith("app.api.")
    ]


def test_the_proposer_and_retrieval_never_import_the_api() -> None:
    """Outcome: no module the rule covers can reach `app.api`, however many hops away."""
    files = _covered_files(REPO_ROOT)
    assert REPO_ROOT / "workflow" / "parameter_proposals.py" in files
    assert len(files) > 2

    assert _api_reached(REPO_ROOT) == []


def test_the_walk_finds_an_api_import_two_hops_away(tmp_path: Path) -> None:
    """A guard that never fires looks identical to a clean tree. Outcome: the planted chain is
    found and named hop by hop."""
    _write(tmp_path, "workflow/__init__.py", "")
    _write(tmp_path, "workflow/parameter_proposals.py", "from app.helpers import tidy\n")
    _write(tmp_path, "retrieval/__init__.py", "")
    _write(tmp_path, "app/__init__.py", "")
    _write(tmp_path, "app/helpers.py", "def tidy():\n    from app.api import measurements\n")
    _write(tmp_path, "app/api/__init__.py", "")
    _write(tmp_path, "app/api/measurements.py", "")

    assert _api_reached(tmp_path) == [
        "workflow.parameter_proposals -> app.helpers -> app.api",
        "workflow.parameter_proposals -> app.helpers -> app.api.measurements",
    ]
