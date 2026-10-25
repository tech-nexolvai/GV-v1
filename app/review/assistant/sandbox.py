"""The assistant's locked shell: this package's PDFs, a scratch folder, and nothing else (owner, 2026-10-16).

The owner asked for the harness claude.ai uses on a drawing set: the model runs ordinary commands
(`pdfinfo`, `pdftotext`, `pdftoppm`, a short `python3` with pypdf or pdfplumber) on the PDFs, then
looks at the pictures it rendered. This module is where those commands run.

**What a command can reach.** One question gets one workspace, with claude.ai's folder names:
`/mnt/user-data/uploads` holds copies of THIS package revision's PDFs, read-only, under the names
they were uploaded with; `/home/claude` is an empty scratch folder the command may write. Nothing else of the host: no other package, no database, no key, no
environment variable of the server, and no network.

**Two backends:**

- **Docker** (`DockerSandbox`, the only one `auto` chooses): the local image `gv-assistant-sandbox`
  (`deploy/assistant_sandbox/Dockerfile`: python 3.12-slim, poppler-utils, pypdf, pdfplumber,
  pypdfium2), run per command with `--network none`, a read-only root, the uploads mounted
  read-only, the scratch folder writable, no capabilities, an unprivileged user, and CPU, memory,
  process and file-size limits. Docker passes the container no environment variable of the host.
- **macOS `sandbox-exec`** (`MacSandbox`, only when a setting asks for `macos` by name, for tests on
  a Mac; never a silent fallback, security audit 2026-10-18: it has no memory or process limit and
  can reach the Mac's system services): a deny-by-default profile that allows reading only the system's own programs, the
  Python runtime and the two folders, writing only `out/`, and no network. The command runs with an
  empty environment (a PATH and HOME only). It has no poppler: `pdftotext` and friends answer
  plainly that they are not here, and Python with pypdf, pdfplumber and pypdfium2 is. Its folders
  are real temporary folders, so the two folder names in a command are rewritten to them and back in
  the output, and the model sees the same paths as with Docker.

`view_path` is the `view` tool: a picture (shrunk to what the model reads), a text file with
numbered lines, or a folder listed two levels deep, from those two folders only.

Every command has a time limit (60 s here; the chat's setting gives 120 s) and its output is cut at
about 20 KB with "Output truncated", as claude.ai does. A command that runs over its time is killed
with its process group (or its container).

**Disk.** The writable folders are folders of the host, so a command could fill the Mac's disk (Postgres
and Docker then stop, as on 17 Oct). Two limits: one file may not grow past `MAX_FILE_BYTES` (the
kernel's file-size limit: the write fails with "File size limit exceeded"), and the writable folders
together may not grow past `MAX_FOLDER_BYTES` (code measures them while the command runs and stops
it, with a plain line, as soon as they do).
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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final, Protocol, Self

__all__ = [
    "DOCKER_FAILED_WORDS",
    "DOCKER_IMAGE",
    "MAX_FILE_BYTES",
    "MAX_FOLDER_BYTES",
    "MAX_OUTPUT_CHARS",
    "NO_SANDBOX_WORDS",
    "OUTPUTS",
    "SCRATCH",
    "SET_PACK",
    "SKILLS",
    "TIMEOUT_SECONDS",
    "UPLOADS",
    "ChatWorkspace",
    "DockerSandbox",
    "MacSandbox",
    "Mount",
    "NoSandbox",
    "Sandbox",
    "SandboxRefused",
    "SandboxResult",
    "SandboxUnavailable",
    "Viewed",
    "Workspace",
    "check_command",
    "cut_output",
    "folder_bytes",
    "open_sandbox",
    "page_folder",
    "quiet_pdf_warnings",
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
#: The set pack (`set_pack.py`, read-only): page map, drawings-only copies, page pictures.
SET_PACK: Final = "/mnt/user-data/set"
TIMEOUT_SECONDS: Final = 60
#: The largest file one command may write (the kernel's file-size limit).
MAX_FILE_BYTES: Final = 200_000_000
#: What the writable folders (a chat's home and outputs) may hold together, at most. A chat on a
#: 60-page set used about 30 MB (2026-10-17).
MAX_FOLDER_BYTES: Final = 1_000_000_000
#: How often the writable folders are measured while a command runs.
_DISK_POLL_SECONDS: Final = 0.25
MAX_OUTPUT_CHARS: Final = 20_000
#: The longest command accepted: a short script, never a file pasted in.
MAX_COMMAND_CHARS: Final = 8_000
_TRUNCATED: Final = "\n\n[Output truncated: {left} more characters not shown]"
_POPPLER: Final = ("pdfinfo", "pdftotext", "pdftoppm", "pdfimages", "pdftocairo", "pdffonts")


#: What the model and the screen are told when Docker stops answering in the middle of a chat.
DOCKER_FAILED_WORDS: Final = (
    "the secure sandbox (Docker) is not answering, so no command can run now. Go on with the "
    "other tools, and say that commands could not run"
)


#: What the shell tool answers when Docker was not running as the answer started (lazy Docker,
#: harness pre-check, change 11): the answer goes on with every other tool.
NO_SANDBOX_WORDS: Final = (
    "unavailable: the sandbox is not running, so no command can run in this answer. Go on with "
    "the other tools, and say that commands could not run"
)


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


#: A PDF reader's (poppler's) warning line: `Syntax Error (8309949): No font in show/space`.
_PDF_WARNING: Final = re.compile(
    r"^(?:Syntax Error|Syntax Warning|Internal Error)(?: \(\d+\))?: (?P<message>.*)$"
)
#: How many lines of one warning stay in the output; the repeats are counted on one line.
_WARNING_REPEATS_KEPT: Final = 1


def quiet_pdf_warnings(text: str) -> str:
    """The output with poppler's repeated warnings folded: each distinct warning once, then one
    line saying how many repeats were left out.

    A drawing set with fonts the reader has no data for prints the same few warnings for every
    text run (`Unknown font tag ...`, `No font in show/space`): on real runs (2026-10-17) they
    crowded the output the model reads and the step the reviewer sees."""
    if "Error" not in text and "Warning" not in text:
        return text
    seen: dict[str, int] = {}
    kept: list[str] = []
    hidden = 0
    for line in text.split("\n"):
        warning = _PDF_WARNING.match(line)
        if warning is None:
            kept.append(line)
            continue
        message = warning.group("message").strip()
        seen[message] = seen.get(message, 0) + 1
        if seen[message] <= _WARNING_REPEATS_KEPT:
            kept.append(line)
        else:
            hidden += 1
    if not hidden:
        return text
    note = f"[{hidden} repeated PDF reader warnings left out; they do not stop the command]"
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept) + "\n" + note + ("\n" if text.endswith("\n") else "")


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
        near = _near_miss(candidate, real)
        if near is None:
            raise SandboxRefused(f"there is no file or folder {text}{_similar(candidate, real)}")
        candidate = near
    return candidate


#: A page picture's number at the end of its name: pdftoppm's `<prefix>-<page>.png` (padded to the
#: digits of the PDF's last page) and the set pack's `p-NNN.png`.
_PAGE_SUFFIX: Final = re.compile(r"^(?P<prefix>.*-)(?P<number>\d+)(?P<suffix>\.[A-Za-z0-9]+)$")
#: Folder-name endings the model adds to a set pack's `pages/<file>` folder (the file's own name,
#: or its drawings-only copy's): `pages/A.pdf/`, `pages/A_drawings_only/`.
_FOLDER_ENDINGS: Final = (".pdf", "_drawings_only")


def _near_miss(candidate: Path, root: Path) -> Path | None:
    """The one existing file a missing path plainly means, or `None` (real runs, 2026-10-17: 11 of
    the 23 refused tool calls were these):

    - a set pack page folder written with the PDF's ending or as its drawings-only copy
      (`pages/A.pdf/p-003.png`, `pages/A_drawings_only/p-003.png` for `pages/A/p-003.png`);
    - a page picture whose number is padded differently (`zoom-046.png` for `zoom-46.png`);
    - a pdftoppm picture whose page number was guessed (`v27-1.png` when only `v27-27.png` was
      made): only when exactly one picture has that name before its number.

    Never outside `root` and never a guess between two files."""
    as_folder = _near_folder(candidate, root)
    if as_folder is not None:
        return as_folder
    folder = candidate.parent
    if not folder.is_dir():
        folder = _near_folder(folder, root) or folder
        if not folder.is_dir():
            return None
        moved = folder / candidate.name
        if moved.exists():
            return moved if root in moved.resolve().parents else None
    shape = _PAGE_SUFFIX.match(candidate.name)
    if shape is None:
        return None
    prefix, suffix = shape.group("prefix"), shape.group("suffix").lower()
    try:
        siblings = [
            child
            for child in folder.iterdir()
            if child.is_file()
            and child.name.startswith(prefix)
            and child.suffix.lower() == suffix
            and child.name[len(prefix) : -len(suffix)].isdigit()
        ]
    except OSError:
        return None
    number = int(shape.group("number"))
    same_page = [c for c in siblings if int(c.name[len(prefix) : -len(suffix)]) == number]
    chosen = same_page if same_page else siblings
    if len(chosen) != 1:
        return None
    found = chosen[0].resolve()
    return found if root in found.parents else None


def _near_folder(folder: Path, root: Path) -> Path | None:
    """`folder` with an ending the model added taken off, when that folder exists inside `root`."""
    name = folder.name
    for _ in _FOLDER_ENDINGS:
        for ending in _FOLDER_ENDINGS:
            if name.lower().endswith(ending.lower()):
                name = name[: -len(ending)]
    if not name or name == folder.name:
        return None
    found = (folder.parent / name).resolve()
    return found if found.is_dir() and root in found.parents else None


def _similar(candidate: Path, root: Path) -> str:
    """`" (there: a.png, b.png)"`: what the nearest existing folder holds, so the next try is
    right; empty when nothing useful is there."""
    folder = candidate.parent
    while not folder.is_dir() and folder != root and root in folder.parents:
        folder = folder.parent
    if not folder.is_dir() or (folder != root and root not in folder.parents):
        return ""
    try:
        names = sorted(child.name for child in folder.iterdir() if not child.name.startswith("."))
    except OSError:
        return ""
    stem = candidate.name.split("-")[0] if folder == candidate.parent else ""
    close = [name for name in names if stem and name.startswith(stem)] or names
    if not close:
        return ""
    more = f", and {len(close) - 8} more" if len(close) > 8 else ""
    return f" (that folder holds: {', '.join(close[:8])}{more})"


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


def page_folder(name: str) -> str:
    """An upload's page-picture folder in the set pack, as `set_pack.stem` makes it ("A B.pdf" ->
    "A_B"); that module loads a renderer, so it is not imported here."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("._") or "file"


@dataclass(frozen=True, slots=True)
class ChatWorkspace:
    """One chat's folders (#1463), kept for the whole chat like claude.ai's container: the set's
    PDFs (read-only), the home folder and the outputs folder (writable, kept across turns), and the
    GV review skill (read-only). The chat's `pictures/` and `files/` are beside them and never
    mounted. `close` keeps everything: a chat's folder outlives a message.

    `set_pack` is the set pack's folder (shared by the chats on the same PDFs), seen read-only at
    `/mnt/user-data/set`; until it exists the chat's own `set-pending/` stands in for it."""

    root: Path
    set_pack: Path | None = None

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

    @property
    def set_folder(self) -> Path:
        """What `/mnt/user-data/set` shows: the pack, or a folder whose `status.json` says it is
        not ready yet."""
        if self.set_pack is not None and self.set_pack.is_dir():
            return self.set_pack
        pending = self.root / "set-pending"
        if not (pending / "status.json").is_file():
            pending.mkdir(parents=True, exist_ok=True)
            (pending / "status.json").write_text(
                '{"state": "not_started", "note": "The set pack is not made for this chat."}',
                encoding="utf-8",
            )
        return pending

    def where_hint(self) -> str:
        """The fix line of a wrong path: where the files really are."""
        try:
            names = sorted(
                child.name for child in self.uploads.iterdir() if not child.name.startswith(".")
            )
        except OSError:
            names = []
        listed = ", ".join(names[:10]) or "none"
        # Every upload's own page folder: a folder guessed from the upload's name (its spaces
        # kept) was refused on a real run, 2026-10-23.
        folders = (
            "; ".join(f"{SET_PACK}/pages/{page_folder(n)}/p-003.png ({n})" for n in names[:4])
            if names
            else f"{SET_PACK}/pages/<upload name without .pdf>/p-003.png"
        )
        return (
            f"The uploads are at {UPLOADS}: {listed}; the set pack (page_map.json, "
            f"drawings-only PDFs, page pictures) is at {SET_PACK}/, a page's picture at "
            f"{folders}; your folder is {SCRATCH}."
        )

    @classmethod
    def open(cls, root: Path, set_pack: Path | None = None) -> ChatWorkspace:
        """The chat's folder, made on first use. The writable folders are open to the sandbox's
        unprivileged user; the others are the server's."""
        root.mkdir(parents=True, exist_ok=True)
        workspace = cls(
            root=root.resolve(), set_pack=None if set_pack is None else set_pack.resolve()
        )
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
            Mount(SET_PACK, self.set_folder, False),
        )

    def close(self) -> None:
        """Nothing to remove: the chat keeps its folder."""

    def resolve(self, path: str) -> Path:
        try:
            return _resolve(self.mounts, self.home, path)
        except SandboxRefused as refused:
            raise SandboxRefused(f"{refused}. {self.where_hint()}") from None

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


def folder_bytes(folders: Sequence[Path]) -> int:
    """The disk space the files under `folders` use (allocated blocks, so a sparse file counts
    what it really holds). Links are not followed; a file removed while counting is skipped."""
    total = 0
    pending = [Path(folder) for folder in folders]
    while pending:
        folder = pending.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    continue
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            blocks = getattr(info, "st_blocks", None)
            total += blocks * 512 if isinstance(blocks, int) else info.st_size
    return total


def _writable(workspace: _Workspace) -> list[Path]:
    return [mount.real for mount in workspace.mounts if mount.writable]


_DISK_FULL: Final = (
    "\n[Stopped: the chat's folders ({shown}) would hold more than {limit} MB. Delete files you "
    "no longer need (rm), then run the command again]"
)


class _DiskWatch:
    """Measures the writable folders while one command runs and calls `stop` once when they grow
    past the limit (`MAX_FOLDER_BYTES`, or what they already held when the command started, so a
    command that only deletes files still runs)."""

    def __init__(
        self,
        workspace: _Workspace,
        stop: Callable[[], None],
        limit: int = MAX_FOLDER_BYTES,
        every: float = _DISK_POLL_SECONDS,
    ) -> None:
        self._folders = _writable(workspace)
        self._shown = ", ".join(mount.shown for mount in workspace.mounts if mount.writable)
        self._limit_mb = limit // 1_000_000
        self._stop_command = stop
        self._every = every
        self._done = threading.Event()
        self.over = False
        self._most = max(limit, folder_bytes(self._folders))
        self._thread = threading.Thread(target=self._watch, name="gv-sandbox-disk", daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._done.set()
        self._thread.join(timeout=5)
        # A last look: a command that wrote fast and ended between two looks is still reported.
        if not self.over and folder_bytes(self._folders) > self._most:
            self.over = True

    def _watch(self) -> None:
        while not self._done.wait(self._every):
            if folder_bytes(self._folders) > self._most:
                self.over = True
                try:
                    self._stop_command()
                except Exception:  # noqa: BLE001 - the time limit still stops the command
                    _log.warning("assistant sandbox could not stop a command over the disk limit")
                return

    def note(self) -> str:
        return _DISK_FULL.format(shown=self._shown, limit=self._limit_mb) if self.over else ""


def check_command(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        raise SandboxRefused("the command is empty")
    if len(command) > MAX_COMMAND_CHARS:
        raise SandboxRefused(f"the command is longer than {MAX_COMMAND_CHARS} characters")
    if "\x00" in command:
        raise SandboxRefused("the command holds a NUL character")
    return command


#: `docker run`'s exit code when Docker itself failed (a command inside may exit 125 too, so its
#: error words are checked as well: `_docker_said`).
_DOCKER_OWN_FAILURE: Final = 125
_DOCKER_WORDS: Final = (
    "docker: ",
    "Cannot connect to the Docker daemon",
    "Error response from daemon",
    "error during connect",
)


def _docker_said(stderr: str) -> bool:
    """Whether `docker run`'s error output is Docker's own (not the command's)."""
    head = stderr.lstrip()
    return any(head.startswith(words) or words in head[:400] for words in _DOCKER_WORDS)


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
    max_file_bytes: int = MAX_FILE_BYTES
    max_folder_bytes: int = MAX_FOLDER_BYTES
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
            # One file may not grow past this (bytes): the write fails, the command goes on.
            "--ulimit",
            f"fsize={self.max_file_bytes}",
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
        watch = _DiskWatch(self.workspace, lambda: self._kill(name), self.max_folder_bytes)
        try:
            with watch:
                done = subprocess.run(
                    argv,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    env=_docker_cli_env(),
                )
        except subprocess.TimeoutExpired as expired:
            self._kill(name)
            text, cut = cut_output(
                quiet_pdf_warnings(_decode(expired.stdout) + _decode(expired.stderr))
            )
            return SandboxResult(
                output=text
                + watch.note()
                + f"\n[Stopped: the command ran over {self.timeout_seconds} s]",
                exit_code=None,
                timed_out=True,
                truncated=cut,
            )
        finally:
            with self._lock:
                self._running.discard(name)
        if done.returncode == _DOCKER_OWN_FAILURE and _docker_said(_decode(done.stderr)):
            # Docker itself failed (the daemon stopped, the image went): not the command's own
            # result. Its words name the server's paths, so they are logged, never shown.
            _log.warning("assistant sandbox: docker run failed: %s", _decode(done.stderr)[:300])
            raise SandboxUnavailable(DOCKER_FAILED_WORDS)
        text, cut = cut_output(quiet_pdf_warnings(_decode(done.stdout) + _decode(done.stderr)))
        return SandboxResult(output=text + watch.note(), exit_code=done.returncode, truncated=cut)


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
    max_file_bytes: int = MAX_FILE_BYTES
    max_folder_bytes: int = MAX_FOLDER_BYTES
    _running: set[int] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _shims(self) -> Path:
        shims = self.workspace.root / "bin"
        with self._lock:
            if not (shims / "python3").exists():
                shims.mkdir(exist_ok=True)
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
        # One profile file per command: helpers run commands at the same time.
        profile = self.workspace.root / f"profile-{uuid.uuid4().hex[:12]}.sb"
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
            # One file may not grow past the limit (bash counts 1024-byte blocks; without -S or -H
            # both limits are set, so the command cannot raise it again).
            f"ulimit -f {max(1, self.max_file_bytes // 1024)} || exit 1\n{real}",
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

        def stop() -> None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        watch = _DiskWatch(self.workspace, stop, self.max_folder_bytes)
        try:
            with watch:
                raw, _ = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            stop()
            raw, _ = process.communicate()
            text, cut = cut_output(quiet_pdf_warnings(self._to_shown(_decode(raw))))
            return SandboxResult(
                output=text
                + watch.note()
                + f"\n[Stopped: the command ran over {self.timeout_seconds} s]",
                exit_code=None,
                timed_out=True,
                truncated=cut,
            )
        finally:
            with self._lock:
                self._running.discard(process.pid)
            profile.unlink(missing_ok=True)
        text, cut = cut_output(quiet_pdf_warnings(self._to_shown(_decode(raw))))
        return SandboxResult(
            output=text + watch.note(), exit_code=process.returncode, truncated=cut
        )


@dataclass(slots=True)
class NoSandbox:
    """No shell: Docker (or its image) was not there when the answer started. The answer still
    runs (most of a full check's helpers never use the shell, 2026-10-25); only the shell tool
    answers `NO_SANDBOX_WORDS` to the agent that calls it. Nothing runs, so nothing is loosened:
    the real sandboxes keep every limit they have."""

    workspace: _Workspace
    kind: str = "none"

    def run(self, command: str) -> SandboxResult:
        del command
        raise SandboxUnavailable(NO_SANDBOX_WORDS)

    def kill_all(self) -> None:
        """Nothing runs."""


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
    """Docker with the built image (`auto` or `docker`), or macOS `sandbox-exec` only when `prefer`
    is `macos` by name (tests on a Mac); else `None`, and the chat says plainly that the secure
    sandbox is not running. `auto` never falls back to `sandbox-exec` (security audit 2026-10-18:
    it has no memory or process limit and can reach the Mac's system services).
    `docker_limits` is `(cpus, memory)`."""
    if prefer in ("auto", "docker"):
        if docker_image_ready():
            cpus, memory = docker_limits or ("1", "768m")
            return DockerSandbox(
                workspace, timeout_seconds=timeout_seconds, cpus=cpus, memory=memory
            )
        _log.warning("assistant sandbox: Docker or its image is not available; no shell")
        return None
    if prefer == "macos" and Path("/usr/bin/sandbox-exec").exists():
        return MacSandbox(
            workspace, python=python or sys.executable, timeout_seconds=timeout_seconds
        )
    _log.info("review assistant has no sandbox here (%s asked for)", prefer)
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
    if not real.is_dir() and not real.is_file():
        # A named pipe, socket or device the model made in its folder: reading it can wait for
        # ever (harness audit 2026-10-19: the answer never ended and Stop could not end it).
        raise SandboxRefused(f"{shown} is not a regular file or a folder, so it cannot be viewed")
    if real.is_dir():
        viewed = _listing(real, workspace)
    elif real.suffix.lower() in _IMAGE_SUFFIXES:
        viewed = _picture(real, shown, max_edge or _MAX_EDGE_PX)
    else:
        viewed = _text(real, shown, view_range)
    asked = path.strip().rstrip("/")
    if asked not in (shown, shown.removeprefix(SCRATCH + "/")):
        # A near miss (`_near_miss`): say which file this is, so the next path is right.
        viewed = replace(viewed, text=f"[There is no {asked}; this is {shown}.]\n{viewed.text}")
    return viewed
