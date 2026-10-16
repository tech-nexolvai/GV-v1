"""The assistant's locked shell: this package's PDFs, a scratch folder, and nothing else (owner, 2026-10-16).

The owner asked for the harness claude.ai uses on a drawing set: the model runs ordinary commands
(`pdfinfo`, `pdftotext`, `pdftoppm`, a short `python3` with pypdf or pdfplumber) on the PDFs, then
looks at the pictures it rendered. This module is where those commands run.

**What a command can reach.** One question gets one workspace: `work/` holds copies of THIS package
revision's PDFs, read-only, under the names they were uploaded with; `out/` is an empty scratch
folder the command may write. Nothing else of the host: no other package, no database, no key, no
environment variable of the server, and no network.

**Two backends, the first that is available:**

- **Docker** (`DockerSandbox`, the primary): the local image `gv-assistant-sandbox`
  (`deploy/assistant_sandbox/Dockerfile`: python 3.12-slim, poppler-utils, pypdf, pdfplumber,
  pypdfium2), run per command with `--network none`, a read-only root, `work` mounted read-only at
  `/work`, `out` at `/out`, no capabilities, an unprivileged user, and CPU, memory and process
  limits. Docker passes the container no environment variable of the host.
- **macOS `sandbox-exec`** (`MacSandbox`, the fallback when Docker is not running or the image is
  not built): a deny-by-default profile that allows reading only the system's own programs, the
  Python runtime and the two folders, writing only `out/`, and no network. The command runs with an
  empty environment (a PATH and HOME only). It has no poppler: `pdftotext` and friends answer
  plainly that they are not here, and Python with pypdf, pdfplumber and pypdfium2 is. Its folders
  are real temporary folders, so `/work` and `/out` in a command are rewritten to them and back in
  the output, and the model sees the same paths as with Docker.

Every command has a time limit (60 s by default) and its output is cut at about 20 KB with
"Output truncated", as claude.ai does. A command that runs over its time is killed with its
process group (or its container).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

__all__ = [
    "DOCKER_IMAGE",
    "MAX_OUTPUT_CHARS",
    "TIMEOUT_SECONDS",
    "DockerSandbox",
    "MacSandbox",
    "Sandbox",
    "SandboxRefused",
    "SandboxResult",
    "Workspace",
    "check_command",
    "cut_output",
    "open_sandbox",
    "safe_file_name",
]

_log = logging.getLogger(__name__)

DOCKER_IMAGE: Final = "gv-assistant-sandbox"
TIMEOUT_SECONDS: Final = 60
MAX_OUTPUT_CHARS: Final = 20_000
#: The longest command accepted: a short script, never a file pasted in.
MAX_COMMAND_CHARS: Final = 8_000
_TRUNCATED: Final = "\n\n[Output truncated: {left} more characters not shown]"
_POPPLER: Final = ("pdfinfo", "pdftotext", "pdftoppm", "pdfimages", "pdftocairo", "pdffonts")


class SandboxRefused(ValueError):
    """A request the sandbox does not run: an empty or over-long command, a path outside `/out`."""


@dataclass(frozen=True, slots=True)
class SandboxResult:
    output: str
    """Standard output and error together, cut at `MAX_OUTPUT_CHARS`."""
    exit_code: int | None
    """`None` when the command was killed for running over its time."""
    timed_out: bool = False
    truncated: bool = False


def cut_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    """The output as claude.ai shows it: the first `limit` characters, then how much was left out."""
    if len(text) <= limit:
        return text, False
    return text[:limit] + _TRUNCATED.format(left=len(text) - limit), True


_SAFE_NAME: Final = re.compile(r"[^A-Za-z0-9._ ()+-]")


def safe_file_name(name: str | None, fallback: str) -> str:
    """The uploaded name, kept to a plain file name: no folder, no leading dot, no odd character."""
    base = Path(name or "").name.strip()
    cleaned = _SAFE_NAME.sub("_", base).lstrip(".").strip()
    if not cleaned:
        cleaned = fallback
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned[:120]


@dataclass(frozen=True, slots=True)
class Workspace:
    """One question's folders. `close` removes them."""

    root: Path
    work: Path
    out: Path
    file_names: tuple[str, ...]

    @classmethod
    def create(cls, files: Sequence[tuple[str, bytes]]) -> Workspace:
        root = Path(tempfile.mkdtemp(prefix="gv-assistant-")).resolve()
        work, out = root / "work", root / "out"
        work.mkdir()
        out.mkdir()
        names: list[str] = []
        for index, (name, data) in enumerate(files, start=1):
            chosen = safe_file_name(name, f"document-{index}.pdf")
            while chosen in names:
                chosen = f"{Path(chosen).stem}-{index}.pdf"
            path = work / chosen
            path.write_bytes(data)
            path.chmod(0o444)
            names.append(chosen)
        work.chmod(0o555)
        # Docker's unprivileged user writes here.
        out.chmod(0o777)
        return cls(root=root, work=work, out=out, file_names=tuple(names))

    def close(self) -> None:
        """Remove the folders; closing twice is harmless."""
        if self.work.exists():
            self.work.chmod(0o755)
        shutil.rmtree(self.root, ignore_errors=True)

    def out_file(self, path: str) -> Path:
        """`path` (as the model writes it, `/out/p-03.png`) as a real file inside `out`, or
        `SandboxRefused`: nothing outside the scratch folder is ever read back."""
        text = path.strip()
        if text.startswith("/out/"):
            relative = text[len("/out/") :]
        elif text.startswith("out/"):
            relative = text[len("out/") :]
        elif not text.startswith("/") and ".." not in Path(text).parts:
            relative = text
        else:
            raise SandboxRefused("only files in /out can be viewed")
        candidate = (self.out / relative).resolve()
        if candidate == self.out or self.out not in candidate.parents:
            raise SandboxRefused("only files in /out can be viewed")
        if not candidate.is_file():
            raise SandboxRefused(f"there is no file {text}")
        return candidate


class Sandbox(Protocol):
    """Where the `bash` tool runs. Tests pass a fake or the real one."""

    kind: str
    workspace: Workspace

    def run(self, command: str) -> SandboxResult: ...


def check_command(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        raise SandboxRefused("the command is empty")
    if len(command) > MAX_COMMAND_CHARS:
        raise SandboxRefused(f"the command is longer than {MAX_COMMAND_CHARS} characters")
    if "\x00" in command:
        raise SandboxRefused("the command holds a NUL character")
    return command


def _decode(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


@dataclass(slots=True)
class DockerSandbox:
    """One `docker run` per command in the locked image."""

    workspace: Workspace
    image: str = DOCKER_IMAGE
    timeout_seconds: int = TIMEOUT_SECONDS
    docker: str = "docker"
    kind: str = "docker"

    def argv(self, command: str, name: str) -> list[str]:
        return [
            self.docker,
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=64m",
            "--cpus",
            "1",
            "--memory",
            "768m",
            "--memory-swap",
            "768m",
            "--pids-limit",
            "128",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--mount",
            f"type=bind,source={self.workspace.work},target=/work,readonly",
            "--mount",
            f"type=bind,source={self.workspace.out},target=/out",
            "--workdir",
            "/out",
            "--env",
            "HOME=/out",
            self.image,
            "bash",
            "-c",
            command,
        ]

    def run(self, command: str) -> SandboxResult:
        name = f"gv-assistant-{uuid.uuid4().hex[:12]}"
        argv = self.argv(check_command(command), name)
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")},
            )
        except subprocess.TimeoutExpired as expired:
            subprocess.run(
                [self.docker, "kill", name], capture_output=True, timeout=20, check=False
            )
            text, cut = cut_output(_decode(expired.stdout) + _decode(expired.stderr))
            return SandboxResult(
                output=text + f"\n[Stopped: the command ran over {self.timeout_seconds} s]",
                exit_code=None,
                timed_out=True,
                truncated=cut,
            )
        text, cut = cut_output(_decode(done.stdout) + _decode(done.stderr))
        return SandboxResult(output=text, exit_code=done.returncode, truncated=cut)


def _profile(python_roots: Sequence[str]) -> str:
    reads = " ".join(f'(subpath "{root}")' for root in python_roots)
    return f"""(version 1)
(deny default)
(allow process-exec process-fork)
(allow signal (target same-sandbox))
(allow sysctl-read)
(allow mach-lookup)
(allow file-read-metadata)
(allow file-read* (literal "/") (subpath "/usr") (subpath "/bin") (subpath "/System") \
(subpath "/Library/Apple") (subpath "/private/var/db/dyld") (subpath "/dev") {reads} \
(subpath (param "WORK")) (subpath (param "OUT")) (subpath (param "SHIMS")))
(allow file-write* (subpath (param "OUT")) (literal "/dev/null"))
(deny network*)
"""


_MISSING_TOOL: Final = """#!/bin/sh
echo "{name}: not available in this sandbox (no poppler here). Use python3 with pypdf, \
pdfplumber or pypdfium2 instead." >&2
exit 127
"""


@dataclass(slots=True)
class MacSandbox:
    """`sandbox-exec` with a deny-by-default profile, for a Mac without the Docker image."""

    workspace: Workspace
    python: str = sys.executable
    timeout_seconds: int = TIMEOUT_SECONDS
    kind: str = "macos-sandbox-exec"

    def _shims(self) -> Path:
        shims = self.workspace.root / "bin"
        if not shims.exists():
            shims.mkdir()
            for name in ("python3", "python"):
                path = shims / name
                path.write_text(f'#!/bin/sh\nexec "{self.python}" "$@"\n')
                path.chmod(0o555)
            for name in _POPPLER:
                path = shims / name
                path.write_text(_MISSING_TOOL.format(name=name))
                path.chmod(0o555)
        return shims

    def _python_roots(self) -> list[str]:
        roots = {str(Path(self.python).parent.parent)}
        roots.add(str(Path(os.path.realpath(self.python)).parent.parent))
        roots.add(sys.base_prefix)
        roots.add(sys.prefix)
        return sorted(roots)

    def _to_real(self, command: str) -> str:
        work, out = str(self.workspace.work), str(self.workspace.out)
        command = re.sub(r"(?<![\w./-])/work(?=/|\b)", work, command)
        return re.sub(r"(?<![\w./-])/out(?=/|\b)", out, command)

    def _to_shown(self, text: str) -> str:
        return text.replace(str(self.workspace.work), "/work").replace(
            str(self.workspace.out), "/out"
        )

    def run(self, command: str) -> SandboxResult:
        real = self._to_real(check_command(command))
        profile = self.workspace.root / "profile.sb"
        if not profile.exists():
            profile.write_text(_profile(self._python_roots()))
        shims = self._shims()
        argv = [
            "/usr/bin/sandbox-exec",
            "-D",
            f"WORK={self.workspace.work}",
            "-D",
            f"OUT={self.workspace.out}",
            "-D",
            f"SHIMS={shims}",
            "-f",
            str(profile),
            "/bin/bash",
            "-c",
            real,
        ]
        env = {
            "PATH": f"{shims}:/usr/bin:/bin",
            "HOME": str(self.workspace.out),
            "TMPDIR": str(self.workspace.out),
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        process = subprocess.Popen(
            argv,
            cwd=self.workspace.out,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            raw, _ = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raw, _ = process.communicate()
            text, cut = cut_output(self._to_shown(_decode(raw)))
            return SandboxResult(
                output=text + f"\n[Stopped: the command ran over {self.timeout_seconds} s]",
                exit_code=None,
                timed_out=True,
                truncated=cut,
            )
        text, cut = cut_output(self._to_shown(_decode(raw)))
        return SandboxResult(output=text, exit_code=process.returncode, truncated=cut)


def docker_image_ready(image: str = DOCKER_IMAGE, docker: str = "docker") -> bool:
    """Whether Docker answers and the sandbox image is built (a quick local check, no pull)."""
    if shutil.which(docker) is None:
        return False
    try:
        done = subprocess.run(
            [docker, "image", "inspect", image],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def open_sandbox(
    workspace: Workspace, *, python: str | None = None, prefer: str = "auto"
) -> Sandbox | None:
    """The first available backend: Docker with the built image, else macOS `sandbox-exec`, else
    `None` (the assistant then answers without the shell)."""
    if prefer in ("auto", "docker") and docker_image_ready():
        return DockerSandbox(workspace)
    if prefer in ("auto", "macos") and Path("/usr/bin/sandbox-exec").exists():
        return MacSandbox(workspace, python=python or sys.executable)
    _log.info("review assistant has no sandbox here (no Docker image, no sandbox-exec)")
    return None
