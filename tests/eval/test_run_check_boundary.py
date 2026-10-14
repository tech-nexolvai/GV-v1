"""Production never imports `eval/` (#1220, #1222, #1228).

Since #1228 the run check's logic lives in `workflow/pariksha_run_check.py`, because Nina's gate
(production) enforces it; `eval/run_check.py` only re-exports it. This guard keeps every production
package (not tests or scripts) from reaching `eval/`.
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


PRODUCTION: tuple[str, ...] = (
    "app",
    "workflow",
    "verdict",
    "extraction",
    "evidence",
    "rules",
    "reports",
    "retrieval",
    "units",
    "vocabulary",
    "storage",
)


def test_production_never_imports_eval() -> None:
    offenders = {
        str(path.relative_to(REPO_ROOT)): names
        for package in PRODUCTION
        for path in sorted((REPO_ROOT / package).rglob("*.py"))
        if (names := _imports_eval(path))
    }
    assert offenders == {}


def test_the_guard_sees_an_import_of_eval(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text("from eval.run_check import run_check\nimport eval.release_gates\n")
    assert _imports_eval(probe) == ["eval.run_check", "eval.release_gates"]


def test_the_run_check_moved_to_production_and_eval_re_exports_it() -> None:
    import eval.run_check as reexport
    import workflow.pariksha_run_check as production

    assert reexport.run_check is production.run_check
    assert reexport.decision_faults is production.decision_faults
