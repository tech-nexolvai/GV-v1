"""The run check is a report only: nothing in `app/` or `workflow/` imports `eval/` (#1220, #1222).

A production path that reached `eval/run_check.py` could start acting on its report. Phase 1 adds no
such path, and this guard keeps it that way until a later phase adds one on purpose.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _imports_eval(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names if alias.name.split(".")[0] == "eval")
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and node.module.split(".")[0] == "eval"
        ):
            found.append(node.module)
    return found


def test_app_and_workflow_never_import_eval() -> None:
    offenders = {
        str(path.relative_to(REPO_ROOT)): names
        for package in ("app", "workflow")
        for path in sorted((REPO_ROOT / package).rglob("*.py"))
        if (names := _imports_eval(path))
    }
    assert offenders == {}


def test_the_guard_sees_an_import_of_eval(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text("from eval.run_check import run_check\nimport eval.release_gates\n")
    assert _imports_eval(probe) == ["eval.run_check", "eval.release_gates"]
