"""The key library never switches anything: no product code can reach it (#1268).

The library and its compare only judge a change and print the verdict. Switching a prompt, a
model or a setting stays a person's decision, so nothing in `app/`, `workflow/`, `extraction/`,
`rules/` or `verdict/` may import them, directly or through any other module it imports.

The walk is per module, not per package: importing `a.b.c` also runs `a/__init__.py` and
`a/b/__init__.py`, so those are followed too.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PRODUCT = ("app", "workflow", "extraction", "rules", "verdict")
JUDGES = ("eval.smriti.library", "eval.smriti.library_compare", "scripts.pariksha_library")


def _module_file(root: Path, name: str) -> Path | None:
    base = root.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _module_name(root: Path, path: Path) -> str:
    parts = path.relative_to(root).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(root: Path, path: Path) -> set[str]:
    """Every module name one file imports, with `from x import y` read as `x.y` too."""
    package = _module_name(root, path)
    if path.name != "__init__.py":
        package = package.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".") if package else []
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*parts, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return {name for name in found if name}


def reached(root: Path, packages: tuple[str, ...]) -> dict[str, list[str]]:
    """Every module the packages' files reach, with the chain of imports that reached it."""
    chains: dict[str, list[str]] = {}
    queue: list[tuple[str, list[str]]] = []
    for package in packages:
        folder = root / package
        if folder.is_dir():
            for file in sorted(folder.rglob("*.py")):
                name = _module_name(root, file)
                queue.append((name, [name]))
    while queue:
        name, chain = queue.pop()
        if name in chains:
            continue
        chains[name] = chain
        file = _module_file(root, name)
        if file is None:
            continue
        for imported in _imports(root, file):
            parts = imported.split(".")
            # Importing a.b.c runs a/__init__.py and a/b/__init__.py as well.
            for end in range(1, len(parts) + 1):
                module = ".".join(parts[:end])
                if module not in chains:
                    queue.append((module, [*chain, module]))
    return chains


def offenders(root: Path) -> list[str]:
    chains = reached(root, PRODUCT)
    return [
        " -> ".join(chains[name])
        for name in sorted(chains)
        if name in JUDGES or name.rpartition(".")[2] == "pariksha_library"
    ]


def test_nothing_in_the_product_reaches_the_key_library_or_its_compare() -> None:
    assert offenders(REPO) == []


def test_the_walk_really_reaches_the_product_modules() -> None:
    """Not vacuous: the walk sees the product's own modules and what they import."""
    chains = reached(REPO, PRODUCT)
    assert "workflow" in chains
    assert "extraction.slot_reader.walls" in chains
    assert "sqlalchemy" in chains


def test_the_guard_catches_an_import_two_hops_away(tmp_path: Path) -> None:
    # `reports` is not product code: only the chain through it reaches the library.
    for folder in ("app", "reports", "eval/smriti"):
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "__init__.py").write_text("")
    (tmp_path / "eval" / "__init__.py").write_text("")
    (tmp_path / "eval" / "smriti" / "library.py").write_text("SHELF = 1\n")
    (tmp_path / "app" / "screen.py").write_text("from reports.step import run\n")
    (tmp_path / "reports" / "step.py").write_text(
        "from eval.smriti import library\n\ndef run() -> int:\n    return library.SHELF\n"
    )

    assert offenders(tmp_path) == ["app.screen -> reports.step -> eval.smriti.library"]


def test_the_guard_catches_a_package_init_that_pulls_the_library_in(tmp_path: Path) -> None:
    (tmp_path / "workflow").mkdir()
    (tmp_path / "workflow" / "__init__.py").write_text("")
    (tmp_path / "workflow" / "step.py").write_text("import eval.smriti.export\n")
    (tmp_path / "eval" / "smriti").mkdir(parents=True)
    (tmp_path / "eval" / "__init__.py").write_text("")
    (tmp_path / "eval" / "smriti" / "__init__.py").write_text("from .library import SHELF\n")
    (tmp_path / "eval" / "smriti" / "library.py").write_text("SHELF = 1\n")
    (tmp_path / "eval" / "smriti" / "export.py").write_text("")

    assert offenders(tmp_path) == ["workflow.step -> eval.smriti -> eval.smriti.library"]


def test_the_guard_catches_the_script_imported_by_name(tmp_path: Path) -> None:
    (tmp_path / "extraction").mkdir()
    (tmp_path / "extraction" / "reader.py").write_text("import pariksha_library\n")

    assert offenders(tmp_path) == ["extraction.reader -> pariksha_library"]
