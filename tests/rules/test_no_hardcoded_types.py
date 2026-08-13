"""Prevent semantic type strings from escaping the vocabulary module.

Source: issue #46 and ``docs/DESIGN.md`` section 4.
Verification: this file scans production packages and proves the guard with a
synthetic violation.
"""

from __future__ import annotations

import ast
from pathlib import Path

from rules.semantic_types import SemanticType

REPO_ROOT = Path(__file__).resolve().parents[2]
VOCABULARY_FILE = REPO_ROOT / "rules" / "semantic_types.py"
PRODUCTION_PACKAGES = (
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
)


def _production_python_files() -> list[Path]:
    """Return Python files where semantic types must use enum members."""

    files: list[Path] = []
    for package in PRODUCTION_PACKAGES:
        root = REPO_ROOT / package
        if root.is_dir():
            files.extend(root.rglob("*.py"))
    return sorted(path for path in files if path != VOCABULARY_FILE)


def find_hardcoded_semantic_types(paths: list[Path]) -> list[str]:
    """Report exact semantic values written as literals in the supplied files."""

    replacements = {member.value: f"SemanticType.{member.name}" for member in SemanticType}
    offenders: list[str] = []

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            replacement = replacements.get(node.value)
            if replacement is not None:
                try:
                    location = path.relative_to(REPO_ROOT)
                except ValueError:
                    location = path
                offenders.append(
                    f"{location}:{node.lineno} hard-codes {node.value!r}; use {replacement}"
                )

    return sorted(offenders)


def test_production_code_has_no_hardcoded_semantic_types() -> None:
    """Known semantic values must be referenced through the central enum."""

    offenders = find_hardcoded_semantic_types(_production_python_files())

    assert not offenders, "Hard-coded semantic type strings found:\n  " + "\n  ".join(offenders)


def test_guard_detects_hardcoded_type_and_names_replacement(tmp_path: Path) -> None:
    """Prove the guard fails usefully instead of merely passing a clean tree."""

    example = tmp_path / "example.py"
    example.write_text('semantic_type = "cabinet_width"\n', encoding="utf-8")

    offenders = find_hardcoded_semantic_types([example])

    assert len(offenders) == 1
    assert "example.py:1" in offenders[0]
    assert "'cabinet_width'" in offenders[0]
    assert "SemanticType.CABINET_WIDTH" in offenders[0]


def test_guard_ignores_comments_and_larger_explanatory_strings(tmp_path: Path) -> None:
    """Only exact string literals count; prose and comments are not semantic values."""

    example = tmp_path / "explanation.py"
    example.write_text(
        '# cabinet_width is discussed here\nmessage = "use cabinet_width carefully"\n',
        encoding="utf-8",
    )

    assert find_hardcoded_semantic_types([example]) == []
