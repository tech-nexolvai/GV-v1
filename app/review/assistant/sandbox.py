"""The assistant's locked shell: this package's PDFs, a scratch folder, and nothing else (owner, 2026-10-16).

The owner asked for the harness claude.ai uses on a drawing set: the model runs ordinary commands
(`pdfinfo`, `pdftotext`, `pdftoppm`, a short `python3` with pypdf or pdfplumber) on the PDFs, then
looks at the pictures it rendered. This module is where those commands run.

**What a command can reach.** One question gets one workspace, with claude.ai's folder names:
`/mnt/user-data/uploads` holds copies of THIS package revision's PDFs, read-only, under the names
they were uploaded with; `/home/claude` is an empty scratch folder the command may write. Nothing else of the host: no other package, no database, no key, no
environment variable of the server, and no network.

**Two backends, the first that is available:**

- **Docker** (`DockerSandbox`, the primary): the local image `gv-assistant-sandbox`
  (`deploy/assistant_sandbox/Dockerfile`: python 3.12-slim, poppler-utils, pypdf, pdfplumber,
  pypdfium2), run per command with `--network none`, a read-only root, the uploads mounted
  read-only, the scratch folder writable, no capabilities, an unprivileged user, and CPU, memory and process
  limits. Docker passes the container no environment variable of the host.
- **macOS `sandbox-exec`** (`MacSandbox`, the fallback when Docker is not running or the image is
  not built): a deny-by-default profile that allows reading only the system's own programs, the
  Python runtime and the two folders, writing only `out/`, and no network. The command runs with an
  empty environment (a PATH and HOME only). It has no poppler: `pdftotext` and friends answer
  plainly that they are not here, and Python with pypdf, pdfplumber and pypdfium2 is. Its folders
  are real temporary folders, so the two folder names in a command are rewritten to them and back in
  the output, and the model sees the same paths as with Docker.

`view_path` is the `view` tool: a picture (shrunk to what the model reads), a text file with
numbered lines, or a folder listed two levels deep, from those two folders only.

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
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

__all__ = [
    "DOCKER_IMAGE",
    "MAX_OUTPUT_CHARS",
    "OUTPUTS",
    "SCRATCH",
    "SKILLS",
    "TIMEOUT_SECONDS",
    "UPLOADS",
    "ChatWorkspace",
    "DockerSandbox",
    "MacSandbox",
    "Mount",
    "Sandbox",
    "SandboxRefused",
    "SandboxResult",
    "SandboxUnavailable",
    "Viewed",
    "Workspace",
    "check_command",
    "cut_output",
    "open_sandbox",
    "safe_file_name",
    "view_path",
]

_log = logging.getLogger(__name__)

DOCKER_IMAGE: Final = "gv-assistant-sandbox"
#: Where the model sees the files, as on claude.ai.
UPLOADS: Final = "/mnt/user-data/uploads"
SCRATCH: Final = "/home/claude"
#: The chat's folder for files handed to the reviewer (`present_files`), as on claude.ai.
OUTPUTS: Final = "/mnt/user-data/outputs"
#: The GV review skill (read-only), as claude.ai mounts its skills.
SKILLS: Final = "/mnt/skills/gv-review"
TIMEOUT_SECONDS: Final = 60
MAX_OUTPUT_CHARS: Final = 20_000
#: The longest command accepted: a short script, never a file pasted in.
MAX_COMMAND_CHARS: Final = 8_000
_TRUNCATED: Final = "\n\n[Output truncated: {left} more characters not shown]"
_POPPLER: Final = ("pdfinfo", "pdftotext", "pdftoppm", "pdfimages", "pdftocairo", "pdffonts")


class SandboxUnavailable(RuntimeError):
    """The sandbox cannot run here (Docker not running, image not built): the assistant answers
    from the records only and says so."""


class SandboxRefused(ValueError):
    """A request the sandbox does not run: an empty or over-long command, a path outside the two
    folders."""


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
class Mount:
    """One folder the sandbox sees: where the model sees it (`shown`), where it really is, and
    whether a command may write there."""

    shown: str
    real: Path
    writable: bool


def _resolve(mounts: Sequence[Mount], default: Path, path: str) -> Path:
    """`path` as the model writes it (a shown folder's path, or relative to the default folder) as
    a real path inside one of the mounts, or `SandboxRefused`: nothing else is ever read back, a
    link out of them included."""
    text = path.strip()
    if not text:
        raise SandboxRefused("the path is empty")
    allowed = " and ".join(mount.shown for mount in mounts)
    for mount in mounts:
        if text == mount.shown or text.startswith(mount.shown + "/"):
            real, relative = mount.real, text[len(mount.shown) :].lstrip("/")
            break
    else:
        if text.startswith("/"):
            raise SandboxRefused(f"only {allowed} can be viewed")
        real, relative = default, text
    candidate = (real / relative).resolve()
    if candidate != real and real not in candidate.parents:
        raise SandboxRefused(f"only {allowed} can be viewed")
    if not candidate.exists():
        raise SandboxRefused(f"there is no file or folder {text}")
    return candidate


def _shown(mounts: Sequence[Mount], real: Path) -> str:
    for mount in mounts:
        if real == mount.real or mount.real in real.parents:
            return mount.shown + (
                "/" + str(real.relative_to(mount.real)) if real != mount.real else ""
            )
    return str(real)


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

    @property
    def mounts(self) -> tuple[Mount, ...]:
        return (Mount(UPLOADS, self.work, False), Mount(SCRATCH, self.out, True))

    @property
    def home(self) -> Path:
        """The real folder a command starts in (the model's `/home/claude`)."""
        return self.out

    def close(self) -> None:
        """Remove the folders; closing twice is harmless."""
        if self.work.exists():
            self.work.chmod(0o755)
        shutil.rmtree(self.root, ignore_errors=True)

    def resolve(self, path: str) -> Path:
        """`path` as the model writes it (`/home/claude/p-03.png`, `/mnt/user-data/uploads/x.pdf`,
        or relative to the scratch folder) as a real path inside one of the two folders, or
        `SandboxRefused`: nothing else is ever read back, a link out of them included."""
        return _resolve(self.mounts, self.out, path)

    def shown(self, real: Path) -> str:
        """A real path inside the workspace as the model sees it."""
        return _shown(self.mounts, real)


@dataclass(frozen=True, slots=True)
class ChatWorkspace:
    """One chat's folders (#1463), kept for the whole chat like claude.ai's container: the set's
    PDFs (read-only), the home folder and the outputs folder (writable, kept across turns), and the
    GV review skill (read-only). The chat's `pictures/` and `files/` are beside them and never
    mounted. `close` keeps everything: a chat's folder outlives a message."""

    root: Path

    @property
    def uploads(self) -> Path:
        return self.root / "uploads"

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"

    @property
    def skills(self) -> Path:
        return self.root / "skills" / "gv-review"

    @property
    def pictures(self) -> Path:
        return self.root / "pictures"

    @property
    def files(self) -> Path:
        return self.root / "files"

    @classmethod
    def open(cls, root: Path) -> ChatWorkspace:
        """The chat's folder, made on first use. The writable folders are open to the sandbox's
        unprivileged user; the others are the server's."""
        root.mkdir(parents=True, exist_ok=True)
        workspace = cls(root=root.resolve())
        for folder in (workspace.uploads, workspace.skills, workspace.pictures, workspace.files):
            folder.mkdir(parents=True, exist_ok=True)
        for folder in (workspace.home, workspace.outputs):
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(0o777)
        return workspace

    @property
    def mounts(self) -> tuple[Mount, ...]:
        return (
            Mount(UPLOADS, self.uploads, False),
            Mount(SCRATCH, self.home, True),
            Mount(OUTPUTS, self.outputs, True),
            Mount(SKILLS, self.skills, False),
        )

    def close(self) -> None:
        """Nothing to remove: the chat keeps its folder."""

    def resolve(self, path: str) -> Path:
        return _resolve(self.mounts, self.home, path)

    def shown(self, real: Path) -> str:
        return _shown(self.mounts, real)


class _Workspace(Protocol):
    """What a sandbox needs of a workspace: its folders and where commands start."""

    @property
    def root(self) -> Path: ...

    @property
    def mounts(self) -> tuple[Mount, ...]: ...

    @property
    def home(self) -> Path: ...

    def resolve(self, path: str) -> Path: ...

    def shown(self, real: Path) -> str: ...

    def close(self) -> None: ...


class Sandbox(Protocol):
    """Where the `bash` tool runs. Tests pass a fake or the real one."""

    kind: str

    @property
    def workspace(self) -> _Workspace: ...

    def run(self, command: str) -> SandboxResult: ...

    def kill_all(self) -> None:
        """Stop every command running now (a cancelled chat message); harmless when none runs."""


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


#: What the `docker` command itself needs to find the daemon. These reach the Docker CLI on the
#: host, never the container: `docker run` passes a container only the `--env` it is given.
_DOCKER_CLI_VARIABLES: Final = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")


def _docker_cli_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in _DOCKER_CLI_VARIABLES if name in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/local/bin")
    return env


@dataclass(slots=True)
class DockerSandbox:
    """One `docker run` per command in the locked image."""

    workspace: _Workspace
    image: str = DOCKER_IMAGE
    timeout_seconds: int = TIMEOUT_SECONDS
    docker: str = "docker"
    kind: str = "docker"
    cpus: str = "1"
    memory: str = "768m"
    pids: int = 128
    _running: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def argv(self, command: str, name: str) -> list[str]:
        mounts: list[str] = []
        for mount in self.workspace.mounts:
            spec = f"type=bind,source={mount.real},target={mount.shown}"
            mounts += ["--mount", spec if mount.writable else spec + ",readonly"]
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
            self.cpus,
            "--memory",
            self.memory,
            "--memory-swap",
            self.memory,
            "--pids-limit",
            str(self.pids),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            *mounts,
            "--workdir",
            SCRATCH,
            "--env",
            f"HOME={SCRATCH}",
            self.image,
            "bash",
            "-c",
            command,
        ]

    def _kill(self, name: str) -> None:
        subprocess.run(
            [self.docker, "kill", name],
            capture_output=True,
            timeout=20,
            check=False,
            env=_docker_cli_env(),
        )

    def kill_all(self) -> None:
        with self._lock:
            names = sorted(self._running)
        for name in names:
            try:
                self._kill(name)
            except (OSError, subprocess.TimeoutExpired):
                _log.warning("assistant sandbox could not stop a container")

    def run(self, command: str) -> SandboxResult:
        name = f"gv-assistant-{uuid.uuid4().hex[:12]}"
        argv = self.argv(check_command(command), name)
        with self._lock:
            self._running.add(name)
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                env=_docker_cli_env(),
            )
        except subprocess.TimeoutExpired as expired:
            self._kill(name)
            text, cut = cut_output(_decode(expired.stdout) + _decode(expired.stderr))
            return SandboxResult(
                output=text + f"\n[Stopped: the command ran over {self.timeout_seconds} s]",
                exit_code=None,
                timed_out=True,
                truncated=cut,
            )
        finally:
            with self._lock:
                self._running.discard(name)
        text, cut = cut_output(_decode(done.stdout) + _decode(done.stderr))
        return SandboxResult(output=text, exit_code=done.returncode, truncated=cut)


def _profile(python_roots: Sequence[str], mounts: Sequence[Mount]) -> str:
    reads = " ".join(f'(subpath "{root}")' for root in python_roots)
    folders = " ".join(f'(subpath (param "M{n}"))' for n in range(len(mounts)))
    writes = " ".join(
        f'(subpath (param "M{n}"))' for n, mount in enumerate(mounts) if mount.writable
    )
    return f"""(version 1)
(deny default)
(allow process-exec process-fork)
(allow signal (target same-sandbox))
(allow sysctl-read)
(allow mach-lookup)
(allow file-read-metadata)
(allow file-read* (literal "/") (subpath "/usr") (subpath "/bin") (subpath "/System") \
(subpath "/Library/Apple") (subpath "/private/var/db/dyld") (subpath "/dev") {reads} \
{folders} (subpath (param "SHIMS")))
(allow file-write* {writes} (literal "/dev/null"))
(deny network*)
"""


_MISSING_TOOL: Final = """#!/bin/sh
echo "{name}: not available in this sandbox (no poppler here). Use python3 with pypdf, \
pdfplumber or pypdfium2 instead." >&2
exit 127
"""


def _literal(text: str) -> Callable[[re.Match[str]], str]:
    """A `re.sub` replacement that inserts `text` as it is (no backslash escapes)."""
    return lambda _match: text


@dataclass(slots=True)
class MacSandbox:
    """`sandbox-exec` with a deny-by-default profile, for a Mac without the Docker image."""

    workspace: _Workspace
    python: str = sys.executable
    timeout_seconds: int = TIMEOUT_SECONDS
    kind: str = "macos-sandbox-exec"
    _running: set[int] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

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

    def _by_length(self) -> list[Mount]:
        return sorted(self.workspace.mounts, key=lambda mount: len(mount.shown), reverse=True)

    def _to_real(self, command: str) -> str:
        for mount in self._by_length():
            pattern = r"(?<![\w./-])" + re.escape(mount.shown) + r"(?=/|\b)"
            command = re.sub(pattern, _literal(str(mount.real)), command)
        return command

    def _to_shown(self, text: str) -> str:
        for mount in sorted(self.workspace.mounts, key=lambda m: len(str(m.real)), reverse=True):
            text = text.replace(str(mount.real), mount.shown)
        return text

    def kill_all(self) -> None:
        with self._lock:
            groups = sorted(self._running)
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def run(self, command: str) -> SandboxResult:
        real = self._to_real(check_command(command))
        mounts = self.workspace.mounts
        profile = self.workspace.root / "profile.sb"
        profile.write_text(_profile(self._python_roots(), mounts))
        shims = self._shims()
        defines: list[str] = []
        for n, mount in enumerate(mounts):
            defines += ["-D", f"M{n}={mount.real}"]
        argv = [
            "/usr/bin/sandbox-exec",
            *defines,
            "-D",
            f"SHIMS={shims}",
            "-f",
            str(profile),
            "/bin/bash",
            "-c",
            real,
        ]
        home = self.workspace.home
        env = {
            "PATH": f"{shims}:/usr/bin:/bin",
            "HOME": str(home),
            "TMPDIR": str(home),
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        process = subprocess.Popen(
            argv,
            cwd=home,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        with self._lock:
            self._running.add(process.pid)
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
        finally:
            with self._lock:
                self._running.discard(process.pid)
        text, cut = cut_output(self._to_shown(_decode(raw)))
        return SandboxResult(output=text, exit_code=process.returncode, truncated=cut)


def docker_image_ready(image: str = DOCKER_IMAGE, docker: str = "docker") -> bool:
    """Whether Docker answers and the sandbox image is built (a quick local check, no pull)."""
    if shutil.which(docker) is None:
        return False
    try:
        done = subprocess.run(
            [docker, "image", "inspect", image],
            env=_docker_cli_env(),
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def open_sandbox(
    workspace: _Workspace,
    *,
    python: str | None = None,
    prefer: str = "auto",
    timeout_seconds: int = TIMEOUT_SECONDS,
    docker_limits: tuple[str, str] | None = None,
) -> Sandbox | None:
    """The first available backend: Docker with the built image, else macOS `sandbox-exec`, else
    `None` (the assistant then answers without the shell). `docker_limits` is `(cpus, memory)`."""
    if prefer in ("auto", "docker") and docker_image_ready():
        cpus, memory = docker_limits or ("1", "768m")
        return DockerSandbox(workspace, timeout_seconds=timeout_seconds, cpus=cpus, memory=memory)
    if prefer in ("auto", "macos") and Path("/usr/bin/sandbox-exec").exists():
        return MacSandbox(
            workspace, python=python or sys.executable, timeout_seconds=timeout_seconds
        )
    _log.info("review assistant has no sandbox here (no Docker image, no sandbox-exec)")
    return None


# ---- the `view` tool -----------------------------------------------------------------------------

#: Claude's picture limits (the model reads a picture this size unresized).
_MAX_EDGE_PX: Final = 2576
_MAX_VISUAL_TOKENS: Final = 4784
_TILE_PX: Final = 28
_MAX_PICTURE_BYTES: Final = 40_000_000
_MAX_DECODE_PIXELS: Final = 100_000_000
_IMAGE_SUFFIXES: Final = (".png", ".jpg", ".jpeg", ".gif", ".webp")
#: A text file longer than this is shown from its start and its end (claude.ai's 16,000).
MAX_VIEW_CHARS: Final = 16_000
_MAX_TEXT_BYTES: Final = 5_000_000
_MAX_LISTED: Final = 500


@dataclass(frozen=True, slots=True)
class Viewed:
    """What `view` shows: a picture (PNG bytes and sizes) or text (a file's lines, a listing)."""

    text: str
    png: bytes | None = None
    width: int = 0
    height: int = 0
    original: tuple[int, int] = (0, 0)


def _fit(width: int, height: int, max_edge: int = _MAX_EDGE_PX) -> tuple[int, int]:
    """The largest size, keeping the shape, that the model reads unresized and whose long side
    is at most `max_edge`."""
    max_edge = min(max_edge, _MAX_EDGE_PX)
    scale = min(1.0, max_edge / max(width, height, 1))
    while True:
        w = max(1, min(max_edge, round(width * scale)))
        h = max(1, min(max_edge, round(height * scale)))
        tiles = -(-w // _TILE_PX) * -(-h // _TILE_PX)
        if tiles <= _MAX_VISUAL_TOKENS and w <= max_edge and h <= max_edge:
            return w, h
        scale *= 0.95


def _picture(path: Path, shown: str, max_edge: int = _MAX_EDGE_PX) -> Viewed:
    from io import BytesIO

    from PIL import Image  # loaded only when a picture is viewed

    if path.stat().st_size > _MAX_PICTURE_BYTES:
        raise SandboxRefused("the picture file is too large")
    Image.MAX_IMAGE_PIXELS = _MAX_DECODE_PIXELS
    try:
        with Image.open(path) as opened:
            opened.load()
            original = opened.size
            picture = opened.convert("RGB")
    except Image.DecompressionBombError:
        raise SandboxRefused("the picture has too many pixels; render it smaller") from None
    except (OSError, ValueError):
        raise SandboxRefused("the file is not a picture this can open") from None
    size = _fit(*picture.size, max_edge=max_edge)
    if size != picture.size:
        picture = picture.resize(size, Image.Resampling.LANCZOS)
    out = BytesIO()
    picture.save(out, format="PNG", optimize=True)
    note = f"Picture {shown}, {size[0]} x {size[1]} px"
    if size != original:
        note += (
            f" (shrunk from {original[0]} x {original[1]} px; render an area sharper to read it)"
        )
    return Viewed(text=note, png=out.getvalue(), width=size[0], height=size[1], original=original)


def _text(path: Path, shown: str, view_range: object) -> Viewed:
    if path.stat().st_size > _MAX_TEXT_BYTES:
        raise SandboxRefused("the file is too large to view; use bash_tool (head, grep)")
    raw = path.read_bytes()
    if b"\x00" in raw[:8192] or raw[:5] == b"%PDF-":
        raise SandboxRefused(
            f"{shown} is not a text file; use bash_tool (pdfinfo, pdftotext, pdftoppm) on it"
        )
    lines = raw.decode("utf-8", errors="backslashreplace").splitlines()
    start, end = 1, len(lines)
    if view_range is not None:
        if (
            not isinstance(view_range, list)
            or len(view_range) != 2
            or not all(isinstance(n, int) and not isinstance(n, bool) for n in view_range)
        ):
            raise SandboxRefused("view_range must be [start_line, end_line]")
        start, end = view_range[0], (len(lines) if view_range[1] == -1 else view_range[1])
        if start < 1 or end < start:
            raise SandboxRefused("view_range must start at 1 or later and not end before it")
    numbered = "\n".join(f"{n:6}\t{lines[n - 1]}" for n in range(start, min(end, len(lines)) + 1))
    if len(numbered) > MAX_VIEW_CHARS:
        half = MAX_VIEW_CHARS // 2
        numbered = (
            numbered[:half] + "\n\n[... middle of the file not shown ...]\n\n" + numbered[-half:]
        )
    return Viewed(text=f"{shown}:\n{numbered}" if numbered else f"{shown} is empty")


def _listing(path: Path, workspace: _Workspace) -> Viewed:
    found: list[str] = []
    for child in sorted(path.iterdir()):
        if child.name.startswith(".") or child.name == "node_modules":
            continue
        found.append(workspace.shown(child) + ("/" if child.is_dir() else ""))
        if child.is_dir():
            for inner in sorted(child.iterdir()):
                if not inner.name.startswith("."):
                    found.append(workspace.shown(inner) + ("/" if inner.is_dir() else ""))
        if len(found) >= _MAX_LISTED:
            found.append(f"[... more than {_MAX_LISTED} entries; use bash_tool (ls, find)]")
            break
    return Viewed(text=f"{workspace.shown(path)}:\n" + ("\n".join(found) or "(empty)"))


def view_path(
    workspace: _Workspace, path: str, view_range: object = None, max_edge: int | None = None
) -> Viewed:
    """The `view` tool: a picture, a text file with numbered lines, or a folder two levels deep.

    `max_edge` shrinks a picture further (helpers see pictures at most 1568 px a side, the lead
    at the model's full 2576)."""
    real = workspace.resolve(path)
    shown = workspace.shown(real)
    if real.is_dir():
        return _listing(real, workspace)
    if real.suffix.lower() in _IMAGE_SUFFIXES:
        return _picture(real, shown, max_edge or _MAX_EDGE_PX)
    return _text(real, shown, view_range)
