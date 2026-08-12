"""Proprietary client material must never enter version control.

`data/` holds Graniti Vicentia's drawings and checklists, and `eval/gold_set/cases/` will
hold the reviewed answer key. Both are commercially sensitive and contractually not ours
to publish. `.gitignore` covers them, but an ignore rule is a convention — this turns it
into a build failure.

The check is deliberately about what is *tracked*, not what is ignored: a file that has
been force-added with `git add -f` is tracked despite matching an ignore rule, and that
is exactly the mistake worth catching.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Paths that must never contain a tracked file.
FORBIDDEN_PREFIXES = (
    "data/",
    "eval/gold_set/cases/",
)

# Filenames/extensions that must never be tracked anywhere in the tree.
FORBIDDEN_PATTERNS = (
    ".env",
    ".pem",
    ".key",
    ".dwg",
    ".xlsx",
)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def test_no_client_data_is_tracked() -> None:
    offenders = [f for f in _tracked_files() if any(f.startswith(p) for p in FORBIDDEN_PREFIXES)]
    assert not offenders, (
        "Proprietary client material is tracked by git:\n  "
        + "\n  ".join(offenders)
        + "\n\nRemove it with `git rm --cached <file>`. Client drawings, checklists and "
        "gold-set cases must never be committed."
    )


def test_no_secrets_or_source_documents_are_tracked() -> None:
    offenders = []
    for f in _tracked_files():
        name = Path(f).name
        if name == ".env" or any(
            f.endswith(ext) for ext in FORBIDDEN_PATTERNS if ext.startswith(".") and ext != ".env"
        ):
            offenders.append(f)
    assert not offenders, (
        "Secrets or proprietary source documents are tracked:\n  "
        + "\n  ".join(offenders)
        + "\n\n`.env.example` is fine; a real `.env` is not."
    )


def test_env_example_exists_but_env_does_not() -> None:
    """A template must exist so nobody invents their own variable names, and the real
    file must not be committed alongside it."""
    assert (REPO_ROOT / ".env.example").is_file(), ".env.example is missing"
    assert ".env" not in _tracked_files(), "a real .env is tracked — remove it immediately"


def test_gitignore_covers_the_sensitive_paths() -> None:
    """The ignore rules are the first line of defence; the tests above are the second.
    Both matter — the rules stop the mistake, the tests catch it if the rules are edited."""
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for needed in ("data/", "eval/gold_set/cases/", ".env", ".claude/settings.local.json"):
        assert needed in text, f".gitignore no longer covers {needed!r}"
