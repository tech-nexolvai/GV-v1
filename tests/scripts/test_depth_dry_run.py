"""The depth dry run (#1256): read-only, model-free, and it never writes a client's values into a
repository."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import scripts.depth_dry_run as dry_run

SCRIPT = Path(dry_run.__file__)


def test_an_output_folder_inside_the_repository_is_refused() -> None:
    with pytest.raises(dry_run.Refused):
        dry_run.outside_git(dry_run.REPO / "proof")
    assert dry_run.main(["--pdf", "made-up.pdf", "--out", str(dry_run.REPO / "proof")]) == (
        dry_run.REFUSED
    )


def test_an_output_folder_outside_any_repository_is_accepted(tmp_path: Path) -> None:
    assert dry_run.outside_git(tmp_path / "proof") == (tmp_path / "proof").resolve()


def test_the_dry_run_imports_no_model_or_network_code() -> None:
    tree = ast.parse(SCRIPT.read_text())
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden = (
        "slot_reader",
        "bedrock",
        "anthropic",
        "openai",
        "boto3",
        "httpx",
        "requests",
        "sutra",
    )
    assert not [name for name in imported if any(word in name for word in forbidden)]
