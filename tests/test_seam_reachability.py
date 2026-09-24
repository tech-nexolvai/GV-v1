"""Pipeline seams must be reached by the production path that claims them.

A unit test proves a function works. It does not prove the pipeline ever calls it.
This guard is the positive twin of ``tests/test_verdict_isolation.py``: walk the
first-party call graph from a declared entry point and fail when a seam has no
route outside its own tests.

Source: issue #661. Verification: this file.
"""

from __future__ import annotations

import ast
import re
import sys
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pytest import MonkeyPatch

REPO_ROOT = Path(__file__).resolve().parent.parent

PROJECT_PACKAGES = {
    "app",
    "eval",
    "evidence",
    "extraction",
    "reports",
    "retrieval",
    "rules",
    "storage",
    "units",
    "verdict",
    "vocabulary",
    "workflow",
}


@dataclass(frozen=True, slots=True)
class DeferredSeam:
    """A deliberately unwired seam, with the issue that owns that decision."""

    reason: str


@dataclass(frozen=True, slots=True)
class PipelineSeam:
    """One pipeline seam and the production entry point expected to reach it."""

    seam: str
    entry_point: str
    deferred: DeferredSeam | None = None


PIPELINE_SEAMS: tuple[PipelineSeam, ...] = (
    PipelineSeam(
        seam="extraction.layout.classify_layout",
        entry_point="workflow.stages.DatabaseStages.extract_pages",
    ),
    PipelineSeam(
        seam="extraction.chains.validate_closure",
        entry_point="workflow.stages.DatabaseStages.extract_pages",
        deferred=DeferredSeam(
            "Dimension-chain closure was built in #122 under open Epic #27, but no production "
            "stage creates DimensionChain objects yet; leave this recorded until that pipeline "
            "wiring exists."
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ModuleIndex:
    """Static call data for one first-party module."""

    module: str
    imports: dict[str, str]
    calls: dict[str, set[str]]
    defined: set[str]


@dataclass(frozen=True, slots=True)
class CallGraph:
    """First-party static calls, keyed by fully qualified function or method."""

    edges: dict[str, set[str]]

    def reachable(self, entry_point: str) -> set[str]:
        """All first-party functions and methods reachable from ``entry_point``."""

        seen: set[str] = set()
        queue: deque[str] = deque([entry_point])
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            queue.extend(sorted(self.edges.get(current, set()) - seen))
        return seen


def _py_files(package: str) -> list[Path]:
    root = REPO_ROOT / package
    return sorted(root.rglob("*.py")) if root.is_dir() else []


def _module_name(path: Path) -> str:
    relative = path.relative_to(REPO_ROOT).with_suffix("")
    parts = relative.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _first_party(module: str) -> bool:
    return module.split(".")[0] in PROJECT_PACKAGES


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _first_party(alias.name):
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and _first_party(node.module):
            for alias in node.names:
                if alias.name == "*":
                    continue
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _call_names(function: ast.AsyncFunctionDef | ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name is not None:
            names.add(name)
    return names


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        if parent is None:
            return node.attr
        return f"{parent}.{node.attr}"
    return None


def _module_index(path: Path) -> ModuleIndex:
    module = _module_name(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = _import_aliases(tree)
    calls: dict[str, set[str]] = {}
    defined: set[str] = set()

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            qualname = f"{module}.{node.name}"
            defined.add(qualname)
            calls[qualname] = _call_names(node)
        elif isinstance(node, ast.ClassDef):
            class_name = f"{module}.{node.name}"
            defined.add(class_name)
            for child in node.body:
                if isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    qualname = f"{class_name}.{child.name}"
                    defined.add(qualname)
                    calls[qualname] = _call_names(child)

    return ModuleIndex(module=module, imports=imports, calls=calls, defined=defined)


def build_call_graph() -> CallGraph:
    """Build a static first-party call graph.

    This is deliberately conservative. If a seam is reached only through dynamic
    dispatch or a registry, declare that registry as the entry point instead of
    loosening this walk until it stops catching real misses.
    """

    modules = [_module_index(path) for package in PROJECT_PACKAGES for path in _py_files(package)]
    by_module = {module.module: module for module in modules}
    all_defined = {name for module in modules for name in module.defined}
    edges: dict[str, set[str]] = {}
    for module in modules:
        for caller, call_names in module.calls.items():
            edges[caller] = {
                resolved
                for raw_name in call_names
                for resolved in [_resolve_call(raw_name, caller, module, by_module, all_defined)]
                if resolved is not None
            }
    return CallGraph(edges)


def _resolve_call(
    raw_name: str,
    caller: str,
    module: ModuleIndex,
    by_module: dict[str, ModuleIndex],
    all_defined: set[str],
) -> str | None:
    if raw_name.startswith("self."):
        method = f"{'.'.join(caller.split('.')[:-1])}.{raw_name.removeprefix('self.')}"
        return method if method in all_defined else None

    if raw_name in module.imports:
        imported = module.imports[raw_name]
        return imported if imported in all_defined else None

    root, _, rest = raw_name.partition(".")
    if root in module.imports:
        imported = f"{module.imports[root]}.{rest}" if rest else module.imports[root]
        return imported if imported in all_defined else None

    local = f"{module.module}.{raw_name}"
    if local in all_defined:
        return local

    parts = raw_name.split(".")
    for length in range(len(parts), 0, -1):
        prefix = ".".join(parts[:length])
        if prefix not in by_module:
            continue
        candidate = f"{prefix}.{'.'.join(parts[length:])}" if length < len(parts) else prefix
        if candidate in all_defined:
            return candidate
    return None


def _deferred_reason_errors(seam: PipelineSeam) -> list[str]:
    if seam.deferred is None:
        return []
    reason = seam.deferred.reason.strip()
    errors: list[str] = []
    if not reason:
        errors.append(f"{seam.seam} is marked deferred but has no written reason")
    elif not re.search(r"#\d+", reason):
        errors.append(f"{seam.seam} is marked deferred but its reason names no issue number")
    return errors


def _reachability_errors(seams: Iterable[PipelineSeam], graph: CallGraph) -> list[str]:
    errors: list[str] = []
    for seam in seams:
        errors.extend(_deferred_reason_errors(seam))
        if seam.deferred is not None:
            continue
        reached = graph.reachable(seam.entry_point)
        if seam.seam not in reached:
            errors.append(
                f"{seam.seam} is not reachable from {seam.entry_point}. "
                f"Wire that entry point to the seam, or mark it deferred with a reason and issue."
            )
    return errors


def test_declared_pipeline_seams_are_reached_or_explicitly_deferred() -> None:
    """Every declared seam is either reached from production or deferred on purpose."""

    graph = build_call_graph()

    errors = _reachability_errors(PIPELINE_SEAMS, graph)

    assert not errors, "Pipeline seam reachability failures:\n  " + "\n  ".join(errors)


def test_layout_classifier_is_reached_from_the_extraction_stage() -> None:
    """The live manifest includes the #660 seam that used to be test-only."""

    graph = build_call_graph()

    assert "extraction.layout.classify_layout" in graph.reachable(
        "workflow.stages.DatabaseStages.extract_pages"
    )


def test_guard_detects_an_unwired_seam() -> None:
    graph = CallGraph(
        {
            "workflow.fake.Stage.extract_pages": {"workflow.fake.Stage._read_document"},
            "workflow.fake.Stage._read_document": set(),
        }
    )
    seam = PipelineSeam(
        seam="extraction.fake.classify_layout",
        entry_point="workflow.fake.Stage.extract_pages",
    )

    errors = _reachability_errors((seam,), graph)

    expected = (
        "extraction.fake.classify_layout is not reachable from "
        "workflow.fake.Stage.extract_pages. Wire that entry point to the seam, or mark it "
        "deferred with a reason and issue."
    )
    assert errors == [expected]


def test_deferred_seam_requires_a_written_reason_and_issue_number() -> None:
    graph = CallGraph({"workflow.fake.Stage.extract_pages": set()})
    missing_reason = PipelineSeam(
        seam="extraction.fake.validate_closure",
        entry_point="workflow.fake.Stage.extract_pages",
        deferred=DeferredSeam(""),
    )
    missing_issue = PipelineSeam(
        seam="extraction.fake.validate_closure",
        entry_point="workflow.fake.Stage.extract_pages",
        deferred=DeferredSeam("Intentionally deferred until the extraction route exists."),
    )

    errors = _reachability_errors((missing_reason, missing_issue), graph)

    assert errors == [
        "extraction.fake.validate_closure is marked deferred but has no written reason",
        "extraction.fake.validate_closure is marked deferred but its reason names no issue number",
    ]


def test_call_graph_follows_imported_function_aliases(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A rename-safe guard follows the AST import, not a grep hit on the old name."""

    (tmp_path / "workflow").mkdir()
    (tmp_path / "extraction").mkdir()
    (tmp_path / "workflow" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "extraction" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "extraction" / "layout.py").write_text(
        "def classify_layout():\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "workflow" / "stages.py").write_text(
        "from extraction.layout import classify_layout as classify\n\n"
        "class DatabaseStages:\n"
        "    def extract_pages(self):\n"
        "        self._read_document()\n"
        "    def _read_document(self):\n"
        "        classify()\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "PROJECT_PACKAGES", {"workflow", "extraction"})

    graph = build_call_graph()

    assert "extraction.layout.classify_layout" in graph.reachable(
        "workflow.stages.DatabaseStages.extract_pages"
    )
