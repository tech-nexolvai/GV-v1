"""The assistant's locked shell (`app/review/assistant/sandbox.py`). No model call, $0.

What is asserted, on the backend this machine has (each real-sandbox test is skipped where its
backend is missing; the Docker ones need the built `gv-assistant-sandbox` image):

- no network: a socket connection is refused;
- only /work and /out: /work cannot be written, the user's home and another folder cannot be read,
  and a file outside /out is never viewed;
- a command over its time limit is stopped and says so;
- long output is cut with "Output truncated";
- no server environment variable reaches the command;
- Docker's command line keeps the locks (no network, read-only root and /work, no capabilities,
  limits, an unprivileged user), checked without Docker.

All file names and contents here are made up.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.sandbox import (
    MAX_OUTPUT_CHARS,
    DockerSandbox,
    MacSandbox,
    SandboxRefused,
    Workspace,
    cut_output,
    docker_image_ready,
    safe_file_name,
)

_MAC = sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists()


@pytest.fixture
def workspace() -> Any:
    space = Workspace.create([("Made-up set.pdf", b"%PDF-1.4 made up")])
    yield space
    space.close()


def _backends(workspace: Workspace) -> list[Any]:
    found: list[Any] = []
    if _MAC:
        found.append(MacSandbox(workspace, timeout_seconds=5))
    if docker_image_ready():
        found.append(DockerSandbox(workspace, timeout_seconds=15))
    if not found:
        pytest.skip("no sandbox backend on this machine (no sandbox-exec, no Docker image)")
    return found


def test_no_network(workspace: Workspace) -> None:
    for sandbox in _backends(workspace):
        result = sandbox.run(
            "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), 3); "
            "print('connected')\""
        )
        assert "connected" not in result.output
        assert result.exit_code != 0


def test_only_work_and_out(workspace: Workspace, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("made-up secret")
    for sandbox in _backends(workspace):
        listed = sandbox.run("ls /work")
        assert listed.output.split() == ["Made-up", "set.pdf"]
        wrote = sandbox.run("echo x > /work/new.txt && echo wrote")
        assert "wrote" not in wrote.output
        read = sandbox.run(f"cat {secret}; cat ~/.ssh/known_hosts")
        assert "made-up secret" not in read.output
        scratch = sandbox.run("echo kept > /out/note.txt && cat /out/note.txt")
        assert scratch.output.strip() == "kept"
    assert (workspace.out / "note.txt").read_text().strip() == "kept"


def test_no_server_environment(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "made-up-key-value")
    for sandbox in _backends(workspace):
        result = sandbox.run("env")
        assert "made-up-key-value" not in result.output
        assert "OPENROUTER" not in result.output


def test_a_long_command_is_stopped(workspace: Workspace) -> None:
    for sandbox in _backends(workspace):
        sandbox.timeout_seconds = 2
        result = sandbox.run("sleep 30; echo never")
        assert result.timed_out and result.exit_code is None
        assert "never" not in result.output
        assert "ran over 2 s" in result.output


def test_long_output_is_cut(workspace: Workspace) -> None:
    for sandbox in _backends(workspace):
        result = sandbox.run("yes made-up | head -c 60000")
        assert result.truncated
        assert "Output truncated" in result.output
        assert len(result.output) < MAX_OUTPUT_CHARS + 200


def test_cut_output_keeps_short_text() -> None:
    assert cut_output("short") == ("short", False)
    text, cut = cut_output("x" * 30, limit=10)
    assert cut and text.startswith("x" * 10) and "20 more characters" in text


@pytest.mark.skipif(not _MAC, reason="macOS sandbox-exec only")
def test_the_mac_fallback_says_poppler_is_missing(workspace: Workspace) -> None:
    result = MacSandbox(workspace).run("pdftotext '/work/Made-up set.pdf' -")
    assert result.exit_code == 127
    assert "not available in this sandbox" in result.output
    assert "python3" in result.output


@pytest.mark.parametrize(
    "path", ["/work/Made-up set.pdf", "/etc/hosts", "../work/x", "/out/../work/x", "/out/"]
)
def test_only_files_in_out_are_read_back(workspace: Workspace, path: str) -> None:
    with pytest.raises(SandboxRefused):
        workspace.out_file(path)


def test_a_link_out_of_out_is_refused(workspace: Workspace) -> None:
    (workspace.out / "link.png").symlink_to(workspace.work / "Made-up set.pdf")
    with pytest.raises(SandboxRefused):
        workspace.out_file("/out/link.png")


def test_file_names_stay_plain() -> None:
    assert safe_file_name("../../etc/passwd", "f.pdf") == "passwd.pdf"
    assert safe_file_name(".hidden.pdf", "f.pdf") == "hidden.pdf"
    assert safe_file_name("A set; rm -rf.pdf", "f.pdf") == "A set_ rm -rf.pdf"
    assert safe_file_name(None, "document-1.pdf") == "document-1.pdf"


def test_docker_command_line_keeps_the_locks(workspace: Workspace) -> None:
    argv = DockerSandbox(workspace).argv("pdfinfo /work/x.pdf", "gv-assistant-made-up")
    joined = " ".join(argv)
    for lock in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit 128",
        "--memory 768m",
        "--cpus 1",
        "--user 65534:65534",
        f"source={workspace.work},target=/work,readonly",
        f"source={workspace.out},target=/out",
    ):
        assert lock in joined
    assert argv[-3:] == ["bash", "-c", "pdfinfo /work/x.pdf"]
    # No host environment is passed in: only the one HOME for the container's user.
    assert [argv[i + 1] for i, item in enumerate(argv) if item == "--env"] == ["HOME=/out"]
