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


#: `tone_instructions` in `.coderabbit.yaml`, from CodeRabbit's published schema
#: (https://storage.googleapis.com/coderabbit_public_assets/schema.v2.json). Hardcoded rather than
#: fetched: a test that needs the network fails for reasons that have nothing to do with the repository.
CODERABBIT_TONE_LIMIT = 250


def test_the_coderabbit_config_is_actually_used() -> None:
    """The reviewer config must parse and fit its schema, or it is silently thrown away.

    This is a regression test for a real and quiet failure. `.coderabbit.yaml` was added on 2026-08-13
    (`4c37420`) with a 431-character `tone_instructions` against a 250-character cap, so CodeRabbit
    rejected **the whole file** and reviewed on defaults — profile CHILL, and none of the
    `path_instructions` for `verdict/`, `units/`, `rules/` or `evidence/` in force. Because it was over
    the cap from the day it was written, this config had never once applied to a review before it was
    fixed on 2026-08-21. Nothing failed: the only sign was one collapsed warning inside a PR comment.

    That is the worst shape a problem can have here — a safety control that is configured, believed to be
    working, and not running. So it is asserted, cheaply, with no network.
    """
    import yaml

    config_path = REPO_ROOT / ".coderabbit.yaml"
    assert config_path.is_file(), ".coderabbit.yaml is missing, so the reviewer runs on defaults"

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert isinstance(config, dict), ".coderabbit.yaml did not parse into a mapping"

    tone = config.get("tone_instructions", "")
    # The type check is not pedantry: `len()` also succeeds on a list, so `tone_instructions: []` would
    # have satisfied a length-only assertion while being a schema violation of exactly the kind that
    # discards the file.
    assert isinstance(tone, str), f"tone_instructions must be a string, got {type(tone).__name__}"
    assert len(tone) <= CODERABBIT_TONE_LIMIT, (
        f"tone_instructions is {len(tone)} characters, over the {CODERABBIT_TONE_LIMIT} the schema "
        "allows. CodeRabbit does not truncate it — it discards the entire config and reviews on "
        "defaults. Move the detail into a path_instructions entry, which allows 20000."
    )

    # The path rules are the substance of the file; losing them is what the cap above actually cost.
    entries = {
        entry["path"]: entry.get("instructions") for entry in config["reviews"]["path_instructions"]
    }
    for zone in ("**", "verdict/**", "units/**", "rules/**", "evidence/**"):
        assert zone in entries, f"{zone} has no review instructions"
        body = entries[zone]
        # A present-but-empty entry reads as coverage and reviews nothing, which is the failure this
        # whole test exists to catch, one level down.
        assert isinstance(body, str) and body.strip(), f"{zone} has an empty instructions block"

    # `**` carries the project-wide safety framing that used to live in `tone_instructions`. Asserted by
    # concept rather than by exact prose, so the wording can be improved but not quietly dropped.
    assert "false-PASS" in entries["**"], (
        "the project-wide instruction no longer mentions the false-PASS rate, which is the one thing "
        "every review of this repository is supposed to be looking for"
    )
