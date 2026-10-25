"""Which code made an answer (change 7 of the final plan): the running code's git commit.

Every stored answer carries it (`chat_store` stamps the answer's trust and accuracy blocks with
`code_version`), and the full check's report names it on its cover. So a reviewer, or the test
bank's replay of a live answer, can tell whether an answer was made by today's code or an older
one ("is this stale?").

The commit is read once per process: `GV_CODE_VERSION` when the deployment sets it, else
`git rev-parse --short HEAD` in this repository. When neither answers (no git, not a checkout),
it is `None` and nothing fails.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Final

__all__ = ["ENV", "code_version"]

_log = logging.getLogger(__name__)

#: The deployment's own word for the commit it runs (a short or full hash).
ENV: Final = "GV_CODE_VERSION"
_REPO: Final = Path(__file__).resolve().parents[3]
#: A commit hash as `git` prints it, or as a deployment may write it.
_HASH: Final = re.compile(r"^[0-9a-f]{7,40}$")
_SHORT: Final = 8


def _shown(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if not _HASH.match(text):
        return None
    return text[:_SHORT]


def _from_git(repo: Path) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        done = subprocess.run(
            [git, "-C", str(repo), "rev-parse", f"--short={_SHORT}", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return _shown(done.stdout)


def read_code_version(env: str | None = None, repo: Path = _REPO) -> str | None:
    """The commit from `env` (the variable's value) when it is a hash, else from git, else
    `None`. Never raises."""
    try:
        return _shown(env) or _from_git(repo)
    except Exception:  # noqa: BLE001 - an answer is never lost to its stamp
        _log.warning("assistant code version could not be read")
        return None


@cache
def code_version() -> str | None:
    """The running code's commit (8 characters), read once per process; `None` when unknown."""
    return read_code_version(os.environ.get(ENV))
