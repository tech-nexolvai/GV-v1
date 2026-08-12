"""The verdict path must have no route to a model, retrieval, network or database.

`AGENTS.md` §2.1 — *"No path from AI/OCR/retrieval into the verdict."* This is the single
most important invariant in the product: it is what makes a PASS mean "exact arithmetic on
qualified evidence" rather than "a model thought so". Until now it was protected only by
discipline, and discipline does not survive a well-meaning refactor six months from now.

The specification asserted here is the import table in `docs/DESIGN.md` §2, ratified by
ADR-0002. Each row becomes a test.

Two properties matter and are checked separately:

* **Transitivity.** `verdict/` importing `rules/` is fine; `verdict/` importing `rules/`
  which imports `boto3` is not. A direct-import check would miss that entirely, and it is
  the realistic way isolation actually breaks.
* **`units/` imports nothing.** That package is the load-bearing constraint of the whole
  scheme — `verdict/` can safely depend on it *because* it cannot reach anything. If
  `units/` ever grows a dependency, the isolation is silently gone.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every top-level package in this repo.
PROJECT_PACKAGES = {
    "app",
    "eval",
    "evidence",
    "extraction",
    "reports",
    "retrieval",
    "rules",
    "units",
    "verdict",
    "workflow",
}

# Third-party modules the verdict path must never reach, by category. The point is not an
# exhaustive list of every network library — it is the specific capabilities AGENTS.md §2.1
# forbids: talking to a model, retrieving, going out to a network, or hitting a database.
FORBIDDEN_FOR_VERDICT = {
    # models / AI
    "boto3",
    "botocore",
    "openai",
    "anthropic",
    "langchain",
    "langgraph",
    "transformers",
    "torch",
    "sentence_transformers",
    # retrieval
    "bm25s",
    "pgvector",
    "faiss",
    "qdrant_client",
    # network
    "requests",
    "httpx",
    "urllib3",
    "aiohttp",
    "socket",
    "http",
    "urllib",
    "ftplib",
    "telnetlib",
    "smtplib",
    # database / ORM
    "sqlalchemy",
    "psycopg",
    "psycopg2",
    "asyncpg",
    "alembic",
    "sqlite3",
    # extraction
    "pdfplumber",
    "pypdfium2",
    "pikepdf",
    "cv2",
    "shapely",
    "paddleocr",
    "doctr",
    # process / dynamic execution
    "subprocess",
    "importlib",
}


def _py_files(package: str) -> list[Path]:
    root = REPO_ROOT / package
    return sorted(root.rglob("*.py")) if root.is_dir() else []


def _imports_in(path: Path) -> set[str]:
    """Top-level module names imported by one file. Relative imports stay in-package and
    are resolved by the caller walking that package anyway."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def transitive_imports(package: str) -> dict[str, list[str]]:
    """Every module reachable from `package`, mapped to the import chain that reached it.

    Walks into project packages so a forbidden import two hops away is still caught.
    """
    chains: dict[str, list[str]] = {}
    seen_pkgs: set[str] = set()
    queue: list[tuple[str, list[str]]] = [(package, [package])]

    while queue:
        pkg, chain = queue.pop()
        if pkg in seen_pkgs:
            continue
        seen_pkgs.add(pkg)
        for file in _py_files(pkg):
            for mod in _imports_in(file):
                if mod == pkg:
                    continue
                chains.setdefault(mod, [*chain, mod])
                if mod in PROJECT_PACKAGES:
                    queue.append((mod, [*chain, mod]))
    return chains


def _uses_dynamic_execution(package: str) -> list[str]:
    offenders: list[str] = []
    for file in _py_files(package):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"eval", "exec", "compile", "__import__"}
            ):
                rel = file.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{node.lineno} calls {node.func.id}()")
    return offenders


# ---------------------------------------------------------------------------
# DESIGN.md §2, row by row
# ---------------------------------------------------------------------------


def test_verdict_reaches_nothing_forbidden() -> None:
    chains = transitive_imports("verdict")
    offenders = [
        f"{mod}  (via {' -> '.join(chain)})"
        for mod, chain in sorted(chains.items())
        if mod in FORBIDDEN_FOR_VERDICT
    ]
    assert not offenders, (
        "verdict/ can reach forbidden modules:\n  "
        + "\n  ".join(offenders)
        + "\n\nAGENTS.md §2.1: no path from AI, OCR or retrieval into the verdict. "
        "The verdict service has no model credentials, no retrieval access, no database "
        "and no outbound network. See docs/DESIGN.md §2."
    )


def test_units_imports_only_the_standard_library() -> None:
    """`units/` is the load-bearing constraint: verdict/ may depend on it precisely because
    it cannot reach anything. One dependency here and the isolation is gone."""
    chains = transitive_imports("units")
    offenders = [
        f"{mod}  (via {' -> '.join(chain)})"
        for mod, chain in sorted(chains.items())
        if mod not in sys.stdlib_module_names and mod != "units"
    ]
    assert not offenders, (
        "units/ imports something outside the standard library:\n  "
        + "\n  ".join(offenders)
        + "\n\nADR-0002: units/ must import stdlib only. It is what allows verdict/ to "
        "import it safely."
    )


def test_verdict_does_not_import_extraction_or_retrieval() -> None:
    chains = transitive_imports("verdict")
    banned = {"extraction", "retrieval", "evidence", "app", "workflow", "reports"}
    offenders = [
        f"{mod}  (via {' -> '.join(chain)})"
        for mod, chain in sorted(chains.items())
        if mod in banned
    ]
    assert not offenders, "verdict/ imports a package it must not:\n  " + "\n  ".join(offenders)


def test_rules_does_not_reach_extraction_or_network() -> None:
    chains = transitive_imports("rules")
    banned = {"extraction", "retrieval", "requests", "httpx", "boto3", "sqlalchemy"}
    offenders = [
        f"{mod}  (via {' -> '.join(chain)})"
        for mod, chain in sorted(chains.items())
        if mod in banned
    ]
    assert not offenders, "rules/ reaches something forbidden:\n  " + "\n  ".join(offenders)


def test_no_dynamic_execution_in_the_decision_path() -> None:
    """`AGENTS.md` §2.2 — no `eval`, no executable rule text. Rules select from a typed
    registry; a rule file supplies a *name*, never code."""
    offenders = _uses_dynamic_execution("verdict") + _uses_dynamic_execution("rules")
    assert not offenders, (
        "Dynamic execution in the decision path:\n  "
        + "\n  ".join(offenders)
        + "\n\nAGENTS.md §2.2 prohibits eval/exec. Operations come from the typed registry."
    )


# ---------------------------------------------------------------------------
# The guard must be proven, not assumed
# ---------------------------------------------------------------------------


def test_guard_detects_a_forbidden_import(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A guard that never fires looks identical to a clean codebase.

    Builds a synthetic package containing `import boto3` two hops away and asserts the
    transitive walk finds it — including the chain, so the failure message is actionable.
    """
    (tmp_path / "fakeverdict").mkdir()
    (tmp_path / "fakerules").mkdir()
    (tmp_path / "fakeverdict" / "__init__.py").write_text("import fakerules\n")
    (tmp_path / "fakerules" / "__init__.py").write_text("import boto3\n")

    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "PROJECT_PACKAGES", {"fakeverdict", "fakerules"})

    chains = transitive_imports("fakeverdict")
    assert "boto3" in chains, "the transitive walk failed to find a two-hop forbidden import"
    assert chains["boto3"] == [
        "fakeverdict",
        "fakerules",
        "boto3",
    ], f"chain should name every hop, got {chains['boto3']}"


def test_guard_detects_eval(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "fakeverdict").mkdir()
    (tmp_path / "fakeverdict" / "__init__.py").write_text("x = eval('1+1')\n")
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

    offenders = _uses_dynamic_execution("fakeverdict")
    assert offenders and "eval()" in offenders[0], f"eval not detected, got {offenders}"
