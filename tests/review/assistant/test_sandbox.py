"""The assistant's locked shell (`app/review/assistant/sandbox.py`). No model call, $0.

What is asserted, on the backend this machine has (each real-sandbox test is skipped where its
backend is missing; the Docker ones need the built `gv-assistant-sandbox` image):

- no network: a socket connection is refused;
- only the uploads and the scratch folder: the uploads cannot be written, the user's home and another folder cannot be read,
  and nothing outside the two folders is read back;
- a command over its time limit is stopped and says so;
- long output is cut with "Output truncated";
- no server environment variable reaches the command;
- Docker's command line keeps the locks (no network, read-only root and uploads, no capabilities,
  limits, an unprivileged user, a file-size limit), checked without Docker;
- `auto` never falls back to macOS `sandbox-exec` (security audit 2026-10-18): without Docker there
  is no shell; `sandbox-exec` runs only when `macos` is named;
- disk: a 300 MB write fails cleanly at the file-size limit, and a command that fills the writable
  folders past their limit is stopped with a plain line (the next command still runs);
- PDF text: pdfplumber in the sandbox reads a label a PDF stores as `(15 1\\/2")` as `15 1/2"`,
  as the app's own readers do (#1921), from a made-up PDF.

All file names and contents here are made up.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Final

import pytest

from app.review.assistant import sandbox as sandbox_module
from app.review.assistant.sandbox import (
    MAX_FILE_BYTES,
    MAX_OUTPUT_CHARS,
    PYTHON_STARTUP,
    SCRATCH,
    UPLOADS,
    DockerSandbox,
    MacSandbox,
    SandboxRefused,
    Workspace,
    cut_output,
    docker_image_ready,
    folder_bytes,
    open_sandbox,
    python_startup,
    safe_file_name,
)
from extraction import pdf_strings
from tests.extraction.test_pdf_strings import _pdf

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
        listed = sandbox.run(f"ls {UPLOADS}")
        assert listed.output.split() == ["Made-up", "set.pdf"]
        wrote = sandbox.run(f"echo x > {UPLOADS}/new.txt && echo wrote")
        assert "wrote" not in wrote.output
        read = sandbox.run(f"cat {secret}; cat ~/.ssh/known_hosts")
        assert "made-up secret" not in read.output
        scratch = sandbox.run(f"echo kept > {SCRATCH}/note.txt && cat {SCRATCH}/note.txt")
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
    result = MacSandbox(workspace).run(f"pdftotext '{UPLOADS}/Made-up set.pdf' -")
    assert result.exit_code == 127
    assert "not available in this sandbox" in result.output
    assert "python3" in result.output


@pytest.mark.parametrize(
    "path", ["/etc/hosts", "/work/x", f"{SCRATCH}/../../etc/hosts", "../x", f"{UPLOADS}/../x", ""]
)
def test_only_the_two_folders_are_read_back(workspace: Workspace, path: str) -> None:
    with pytest.raises(SandboxRefused):
        workspace.resolve(path)


def test_paths_resolve_inside_the_two_folders(workspace: Workspace) -> None:
    (workspace.out / "p-1.png").write_bytes(b"x")
    assert workspace.resolve(f"{UPLOADS}/Made-up set.pdf") == workspace.work / "Made-up set.pdf"
    assert workspace.resolve(f"{SCRATCH}/p-1.png") == workspace.out / "p-1.png"
    assert workspace.resolve("p-1.png") == workspace.out / "p-1.png"
    assert workspace.shown(workspace.out / "p-1.png") == f"{SCRATCH}/p-1.png"


def test_a_link_out_of_the_folders_is_refused(workspace: Workspace, tmp_path: Path) -> None:
    (workspace.out / "link.png").symlink_to(tmp_path)
    with pytest.raises(SandboxRefused):
        workspace.resolve(f"{SCRATCH}/link.png")


def test_file_names_stay_plain() -> None:
    assert safe_file_name("../../etc/passwd", "f.pdf") == "passwd.pdf"
    assert safe_file_name(".hidden.pdf", "f.pdf") == "hidden.pdf"
    assert safe_file_name("A set; rm -rf.pdf", "f.pdf") == "A set_ rm -rf.pdf"
    assert safe_file_name(None, "document-1.pdf") == "document-1.pdf"


def test_docker_command_line_keeps_the_locks(workspace: Workspace) -> None:
    argv = DockerSandbox(workspace).argv(f"pdfinfo {UPLOADS}/x.pdf", "gv-assistant-made-up")
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
        f"--ulimit fsize={MAX_FILE_BYTES}",
        f"source={workspace.work},target={UPLOADS},readonly",
        f"source={workspace.out},target={SCRATCH}",
        f"source={workspace.root / 'python'},target={PYTHON_STARTUP},readonly",
    ):
        assert lock in joined
    assert argv[-3:] == ["bash", "-c", f"pdfinfo {UPLOADS}/x.pdf"]
    # No host environment is passed in: only HOME for the container's user, and where its Python
    # finds the start-up file.
    assert [argv[i + 1] for i, item in enumerate(argv) if item == "--env"] == [
        f"HOME={SCRATCH}",
        f"PYTHONPATH={PYTHON_STARTUP}",
    ]


# ---- PDF text with an escaped slash (#1921) ------------------------------------------------------

#: A made-up label as some CAD programs store it: every `/`, `[` and `]` behind a backslash.
_ESCAPED_LABEL: Final = rb'BT /F1 10 Tf 1 0 0 1 20 100 Tm (15 1\/2" \[394mm\]) Tj ET'
_READ_WITH_PDFPLUMBER: Final = (
    "import pdfplumber; print(pdfplumber.open('{path}').pages[0].extract_text())"
)


@pytest.fixture
def escaped() -> Any:
    space = Workspace.create([("Made-up escaped.pdf", _pdf(_ESCAPED_LABEL))])
    yield space
    space.close()


def test_the_sandbox_python_starts_with_the_escaped_text_fix(escaped: Workspace) -> None:
    folder = python_startup(escaped)
    startup = folder / "sitecustomize.py"
    assert startup.read_text() == Path(pdf_strings.__file__).read_text()
    assert not startup.stat().st_mode & 0o222
    # A changed fix is written again; an unchanged one is left as it is.
    startup.chmod(0o644)
    startup.write_text("# an older copy\n")
    assert python_startup(escaped) == folder
    assert startup.read_text() == Path(pdf_strings.__file__).read_text()
    assert [path.name for path in folder.iterdir()] == ["sitecustomize.py"]


def test_python_started_like_the_sandbox_reads_the_escaped_slash(
    escaped: Workspace, tmp_path: Path
) -> None:
    """This test's own Python has the fix (`app` imports it); a fresh one, started from an empty
    folder with only the sandbox's `PYTHONPATH`, has nothing of the app's but the start-up file."""
    pdf = escaped.work / "Made-up escaped.pdf"
    done = subprocess.run(
        [sys.executable, "-c", _READ_WITH_PDFPLUMBER.format(path=pdf)],
        cwd=tmp_path,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(python_startup(escaped)),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert done.stdout.strip() == '15 1/2" [394mm]'


def test_a_command_in_the_sandbox_reads_the_escaped_slash(escaped: Workspace) -> None:
    pdf = f"{UPLOADS}/Made-up escaped.pdf"
    for sandbox in _backends(escaped):
        result = sandbox.run(f'python3 -c "{_READ_WITH_PDFPLUMBER.format(path=pdf)}"')
        assert result.output.strip() == '15 1/2" [394mm]', sandbox.kind
        result = sandbox.run(
            f"python3 -c \"import pypdf; print(pypdf.PdfReader('{pdf}').pages[0].extract_text())\""
        )
        assert result.output.strip() == '15 1/2" [394mm]', sandbox.kind
        if sandbox.kind == "docker":
            assert sandbox.run(f"pdftotext '{pdf}' -").output.strip() == '15 1/2" [394mm]'


# ---- no silent fallback (security audit 2026-10-18) ----------------------------------------------


@pytest.mark.parametrize("prefer", ["auto", "docker"])
def test_without_docker_there_is_no_shell_never_the_mac_sandbox(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, prefer: str
) -> None:
    monkeypatch.setattr(sandbox_module, "docker_image_ready", lambda *a, **k: False)
    assert open_sandbox(workspace, prefer=prefer) is None


def test_docker_is_chosen_when_it_answers(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_module, "docker_image_ready", lambda *a, **k: True)
    chosen = open_sandbox(workspace, prefer="auto", docker_limits=("2", "2g"))
    assert isinstance(chosen, DockerSandbox) and chosen.memory == "2g"


def test_the_mac_sandbox_runs_only_when_named(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_module, "docker_image_ready", lambda *a, **k: False)
    chosen = open_sandbox(workspace, prefer="macos")
    if _MAC:
        assert isinstance(chosen, MacSandbox)
    else:
        assert chosen is None


def test_the_settings_default_never_names_the_mac_sandbox() -> None:
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["assistant_sandbox"].default == "auto"
    assert fields["review_assistant_sandbox"].default == "auto"


# ---- disk ------------------------------------------------------------------------------------------

_WRITE_300_MB = "dd if=/dev/zero of=big.bin bs=1048576 count=300; echo exit=$?"


def test_a_300_mb_write_fails_cleanly(workspace: Workspace) -> None:
    """One file may not grow past `MAX_FILE_BYTES`: the write stops there, the command says so,
    and the sandbox goes on."""
    for sandbox in _backends(workspace):
        result = sandbox.run(_WRITE_300_MB)
        assert "exit=0" not in result.output, result.output
        # Linux bash says "File size limit exceeded", macOS bash "Filesize limit exceeded".
        said = re.search(r"file ?size limit exceeded|file too large", result.output, re.IGNORECASE)
        assert said is not None, result.output
        big = workspace.out / "big.bin"
        assert not big.exists() or big.stat().st_size <= MAX_FILE_BYTES
        big.unlink(missing_ok=True)
        assert sandbox.run("echo still here").output.strip() == "still here"


def test_filling_the_folders_past_their_limit_is_stopped(workspace: Workspace) -> None:
    """Many files under the file-size limit: code measures the folders and stops the command."""
    for sandbox in _backends(workspace):
        sandbox.max_folder_bytes = 30_000_000
        sandbox.timeout_seconds = 30
        result = sandbox.run(
            "for i in $(seq 1 40); do dd if=/dev/zero of=f$i.bin bs=1048576 count=4 2>/dev/null;"
            " sleep 0.05; done; echo finished"
        )
        assert "finished" not in result.output
        assert "would hold more than 30 MB" in result.output
        # Stopped soon after the limit (one poll's writes over it at most).
        assert folder_bytes([workspace.out]) < 30_000_000 + 25_000_000
        cleaned = sandbox.run("rm -f f*.bin; echo cleaned")
        assert cleaned.output.strip() == "cleaned"


def test_the_disk_watch_stops_a_command_and_says_so(tmp_path: Path) -> None:
    """Without any sandbox: the watch measures the writable folders while something writes."""
    out = tmp_path / "out"
    out.mkdir()
    space = Workspace(root=tmp_path, work=tmp_path, out=out, file_names=())
    stopped = threading.Event()
    watch = sandbox_module._DiskWatch(space, stopped.set, limit=2_000_000, every=0.02)
    with watch:
        for n in range(10):
            (out / f"f{n}.bin").write_bytes(os.urandom(500_000))
            if stopped.wait(0.1):
                break
    assert stopped.is_set() and watch.over
    assert "would hold more than 2 MB" in watch.note() and SCRATCH in watch.note()
    quiet = sandbox_module._DiskWatch(space, lambda: None, limit=50_000_000, every=0.02)
    with quiet:
        (out / "small.txt").write_text("made up")
    assert not quiet.over and quiet.note() == ""


def test_a_command_may_delete_when_the_folders_are_already_over(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "old.bin").write_bytes(os.urandom(3_000_000))
    space = Workspace(root=tmp_path, work=tmp_path, out=out, file_names=())
    watch = sandbox_module._DiskWatch(space, lambda: None, limit=1_000_000, every=0.02)
    with watch:
        (out / "old.bin").unlink()
    assert not watch.over
