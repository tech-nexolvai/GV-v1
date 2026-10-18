"""The labels' own process gets no key (`set_pack._dims`, security audit 2026-10-18).

That process parses the set's PDFs. It used to start with the server's whole environment, keys and
the database's address included. Now it gets an allow-list: what Python needs, this code's folder,
and the labels' own settings. Made-up values only; no model call, $0.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import set_pack

_MADE_UP = {
    "ANTHROPIC_API_KEY": "made-up-anthropic",
    "OPENROUTER_API_KEY": "made-up-openrouter",
    "GV_ANTHROPIC_API_KEY": "made-up-gv",
    "AWS_SECRET_ACCESS_KEY": "made-up-aws",
    "AWS_SESSION_TOKEN": "made-up-session",
    "GITHUB_TOKEN": "made-up-github",
    "GV_DATABASE_URL": "postgresql+psycopg://gv:made-up@localhost:5434/x",
    "GV_DEV_TICKET_SECRET": "made-up-ticket",
    "GV_DIM_LABELS_DRAWN_BUDGET_S": "7",
    "GV_DIM_LABELS_TOKEN": "made-up-even-with-the-prefix",
}


def _no_secrets(environment: dict[str, str]) -> None:
    for name, value in _MADE_UP.items():
        if name == "GV_DIM_LABELS_DRAWN_BUDGET_S":
            continue
        assert name not in environment, name
        assert value not in environment.values(), name


def test_the_allow_list_keeps_what_python_needs_and_no_key() -> None:
    root = Path("/made/up/root")
    kept = set_pack.child_environment(
        root, {**_MADE_UP, "PATH": "/usr/bin", "HOME": "/home/x", "PYTHONPATH": "/extra"}
    )
    _no_secrets(kept)
    assert kept["PATH"] == "/usr/bin" and kept["HOME"] == "/home/x"
    assert kept["PYTHONPATH"].split(":") == [str(root), "/extra"]
    assert kept["GV_DIM_LABELS_DRAWN_BUDGET_S"] == "7"


def test_the_labels_process_is_started_with_the_scrubbed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in _MADE_UP.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GV_DIM_LABELS_IN_PROCESS", raising=False)
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.update(kwargs, argv=argv)
        (tmp_path / "dims.json").write_text("{}")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(set_pack.subprocess, "run", fake_run)
    set_pack._dims(tmp_path, tmp_path, ["made-up.pdf"], [])
    assert seen["argv"][1:3] == ["-m", "app.review.assistant.dim_labels"]
    _no_secrets(seen["env"])
    assert seen["stdin"] == subprocess.DEVNULL


def test_a_real_child_sees_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real process started with that environment: its own `os.environ` has no key, and it can
    still import this code."""
    for name, value in _MADE_UP.items():
        monkeypatch.setenv(name, value)
    root = Path(set_pack.__file__).resolve().parents[3]
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, os; import app.review.assistant.finding_checks; "
                "print(json.dumps(dict(os.environ)))"
            ),
        ],
        env=set_pack.child_environment(root),
        cwd=root,
        capture_output=True,
        check=True,
        timeout=60,
    )
    _no_secrets(json.loads(done.stdout))
