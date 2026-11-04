"""What has to be true before a probe is allowed to spend anything (Phase 1).

Three checks, each of them bought with a wasted run:

- **Docker, with the sandbox image.** The engine's shell tool runs in a container. Without it the
  run still finishes, quietly, with the shell answering that it is unavailable — and its findings
  are then not comparable with a run that had one, so the probe is measuring two things at once
  (Anant, 2026-10-29).
- **More than 5 GB free.** Docker's own engine stops itself when the disk fills; $6.39 went that
  way on 2026-10-29.
- **The app answering.** A probe asks the running server to do the check, exactly as the screen
  does. It never starts the app: a probe that started a server would be running code nobody chose,
  under settings nobody saw. If the app is not up, the probe says so and stops.

Deliberately not checked: the battery. Anant, 2026-10-15: don't block or nag.

Every check is read-only — a version string, a free-space figure, a socket that is opened and
closed. Nothing here writes anything, starts anything or spends anything.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

__all__ = [
    "MIN_FREE_GB",
    "SANDBOX_IMAGE",
    "Report",
    "api_ready",
    "check",
    "docker_ready",
    "free_gb",
]

#: Anant, 2026-10-29: more than 5 GB free before any paid run.
MIN_FREE_GB: Final = 5.0

#: The image the engine's shell tool runs in.
SANDBOX_IMAGE: Final = "gv-assistant-sandbox"

_TIMEOUT_S: Final = 20


def _run(args: list[str]) -> tuple[bool, str]:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as failed:
        return False, str(failed)
    return done.returncode == 0, (done.stdout or done.stderr or "").strip()


def docker_ready() -> tuple[bool, str]:
    """Whether the Docker engine answers and holds the sandbox image."""
    ok, said = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    if not ok:
        return False, f"Docker is not answering ({said.splitlines()[0] if said else 'no reply'})"
    engine = said.splitlines()[0] if said else "?"
    has_image, _ = _run(["docker", "image", "inspect", SANDBOX_IMAGE])
    if not has_image:
        return False, f"Docker {engine} is up but the {SANDBOX_IMAGE} image is missing"
    return True, f"Docker {engine}, {SANDBOX_IMAGE} present"


def free_gb(path: Path) -> float:
    """Free space on the volume holding `path`, in GB, walking up to a folder that exists."""
    where = path.resolve()
    while not where.exists() and where != where.parent:
        where = where.parent
    return shutil.disk_usage(where).free / 1_000_000_000


def api_ready(api: str, *, timeout_s: float = 5.0) -> tuple[bool, str]:
    """Whether something is listening where the app should be. Never starts it."""
    parts = urlsplit(api if "//" in api else f"//{api}")
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True, f"{host}:{port} is listening"
    except OSError as failed:
        return False, f"nothing is listening on {host}:{port} ({failed}); start the app yourself"


@dataclass
class Report:
    """What the preflight found. `ok` is false if any check a paid run needs failed."""

    ok: bool = True
    lines: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def add(self, passed: bool, said: str) -> None:
        self.lines.append(f"{'ok  ' if passed else 'STOP'} {said}")
        if not passed:
            self.ok = False
            self.problems.append(said)


def check(
    *,
    api: str | None,
    disk_path: Path,
    min_free_gb: float = MIN_FREE_GB,
    want_docker: bool = True,
) -> Report:
    """Run every check a paid probe needs. `api` None: the app is not needed (a dry run)."""
    report = Report()
    if want_docker:
        report.add(*docker_ready())
    free = free_gb(disk_path)
    report.add(
        free > min_free_gb,
        f"{free:.1f} GB free on the probe's disk (needs more than {min_free_gb})",
    )
    if api is not None:
        report.add(*api_ready(api))
    return report
