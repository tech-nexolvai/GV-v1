"""Rebuilding a saved pack's labels for a $0 replay (2026-11-04): never the saved pack, never inside a
repository, no key left in the environment, no model or network code."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

import scripts.rebuild_pack_dims as rebuild

SCRIPT = Path(rebuild.__file__)


def _pack(folder: Path, names: list[str]) -> Path:
    folder.mkdir(parents=True)
    (folder / "page_map.json").write_text(json.dumps({"files": names, "pages": []}))
    (folder / "dims.json").write_text(
        json.dumps({"pages": [{"labels": [{"side": "vendor"}, {"side": "architect"}]}]})
    )
    (folder / "hires").mkdir()
    (folder / "hires" / "tile.png").write_bytes(b"x")
    (folder / ".dims-pages").mkdir()
    return folder


def test_an_output_folder_inside_the_repository_is_refused(tmp_path: Path) -> None:
    pack = _pack(tmp_path / "pack", ["set.pdf"])
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "set.pdf").write_bytes(b"%PDF")
    with pytest.raises(rebuild.Refused):
        rebuild.outside_git(rebuild.REPO / "replay")
    out = str(rebuild.REPO / "replay")
    assert rebuild.main([str(pack), str(tmp_path / "uploads"), "--out", out]) == rebuild.REFUSED


def test_an_upload_the_pack_names_must_be_there(tmp_path: Path) -> None:
    pack = _pack(tmp_path / "pack", ["set.pdf"])
    (tmp_path / "uploads").mkdir()
    with pytest.raises(rebuild.Refused):
        rebuild.pack_files(pack, tmp_path / "uploads")


def test_the_copy_leaves_the_saved_pack_alone_and_reads_every_page_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack = _pack(tmp_path / "pack", ["set.pdf"])
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "set.pdf").write_bytes(b"%PDF")
    seen: list[Any] = []

    def write_dims(out: Path, given: Path, names: list[str], pages: list[Any]) -> None:
        seen.append((out, given, names))
        assert not (out / "hires").exists() and not (out / ".dims-pages").exists()
        (out / "dims.json").write_text(json.dumps({"pages": [{"labels": [{"side": "vendor"}]}]}))

    from app.review.assistant import set_pack

    monkeypatch.setattr(set_pack, "write_dims", write_dims)
    monkeypatch.setenv("OPENROUTER_API_KEY", "made-up")
    out = tmp_path / "replay"
    assert rebuild.main([str(pack), str(uploads), "--out", str(out), "--workers", "1"]) == 0
    assert seen == [(out.resolve(), uploads.resolve(), ["set.pdf"])]
    assert (pack / "hires" / "tile.png").is_file()  # the saved pack is untouched
    assert rebuild.label_counts(pack) == {"vendor": 1, "architect": 1}
    assert rebuild.label_counts(out) == {"vendor": 1}
    # A second run never writes over the first.
    assert rebuild.main([str(pack), str(uploads), "--out", str(out)]) == rebuild.REFUSED


def test_keys_tokens_and_addresses_are_taken_out_of_the_environment() -> None:
    environ = {"PATH": "/bin", "OPENROUTER_API_KEY": "x", "DATABASE_URL": "y", "GV_TOKEN": "z"}
    assert sorted(rebuild.without_secrets(environ)) == [
        "DATABASE_URL",
        "GV_TOKEN",
        "OPENROUTER_API_KEY",
    ]
    assert environ == {"PATH": "/bin"}


def test_the_rebuild_imports_no_model_or_network_code() -> None:
    tree = ast.parse(SCRIPT.read_text())
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    for forbidden in ("openai", "anthropic", "httpx", "requests", "chat_provider", "urllib"):
        assert not any(forbidden in name for name in imported), forbidden
