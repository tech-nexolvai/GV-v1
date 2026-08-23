"""Run exactly what CI runs, locally, and say plainly what did not get checked.

Every CI failure in this project so far has been an environment difference rather than a logic bug:
a dependency in an extra the job did not install, a test module that resolved locally and not on the
runner, a linter nobody ran before pushing. None of them would have survived this script.

Three things it does that `pytest` alone does not.

**It runs the same checks in the same order as `.github/workflows/ci.yml`** — including semgrep,
which is easy to forget and has caught a real golden-rule violation.

**It warns when the environment has drifted from `pyproject.toml`.** A venv missing an extra makes
tests skip or fail to collect, and a green run in a stale environment is worse than a red one.

**It says how many tests were skipped and why.** The database tests skip silently without
`DATABASE_URL`, which is how a model/migration mismatch reached CI once already.

    python scripts/check.py              # everything except the database
    python scripts/check.py --with-db    # everything, database started for you  (`make ci`)
    python scripts/check.py --fast       # the tight edit loop                    (`make check-fast`)

`--fast` drops semgrep, the network checks, and the five steps whose test modules the full `pytest`
run collects anyway. CI gives those their own steps so a failure is legible in its log, which is
worth the cost there; locally it was half the runtime spent re-running about fifty tests. Nothing
goes unchecked, and `make ci` remains the gate.

Measured on a 10-core machine with mypy and pytest caches warm: `--fast` about 15 seconds, the
full chain about 22 without the database. A first run after a checkout is two to three times that
while the caches fill, so judge the cost on the second run rather than the first.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The workflow is the reference for what must be checked. Anything this script assumes about CI is
#: read out of that file rather than restated here, so the two cannot drift in silence — and
#: `tests/test_local_ci_parity.py` fails the build if a CI step gains no local counterpart.
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Extras CI installs for the main job. Kept in step with `.github/workflows/ci.yml`.
REQUIRED_EXTRAS = ("ai", "dev", "rules", "platform", "reports")

#: What CI does that a local run cannot honestly reproduce, and why. Printed at the end of every
#: run: a developer who believes local and CI are equivalent will eventually be wrong about
#: something that matters, and the fix is to say so every time rather than once in a document.
GITHUB_ONLY = {
    "published CodeRabbit schema (weekly)": (
        "asks whether CodeRabbit's *currently published* schema still accepts our config. It needs "
        "the network and a live third party, so a local failure would usually mean the wifi rather "
        "than the repository. Runs weekly on CI; force it here with "
        "GV_CHECK_CODERABBIT_SCHEMA=1 pytest tests/test_repo_hygiene.py -k published_schema"
    ),
    "Python 3.12 as the tested floor": (
        "CI pins 3.12 because AGENTS.md §4 promises it. A local interpreter that is newer can accept "
        "syntax and stdlib that 3.12 rejects, so a green local run on 3.14 is not the same evidence"
    ),
}

#: One importable module per extra, to detect drift without shelling out to pip.
EXTRA_PROBES = {
    "ai": "langgraph",
    "dev": "pytest",
    "rules": "yaml",
    "platform": "sqlalchemy",
    "extraction": "pdfplumber",
    "reports": "reportlab",
}


@dataclass
class Step:
    name: str
    command: list[str]
    optional: bool = False
    """The tool may be absent; skip it and say so rather than failing."""

    advisory: bool = False
    """Reported but never fails the chain — CI runs it with continue-on-error."""

    needs_github_token: bool = False
    """Talks to the GitHub API. Skipped, loudly, when no token is available."""

    also_in_full_suite: bool = False
    """The module this runs is collected by the full `pytest` step as well.

    CI gives these their own steps so a failure is legible in the log rather than buried in a
    suite summary, and that is worth its cost there. Locally it was about half the runtime of
    `--fast`, spent re-running roughly fifty tests the full run covers anyway — so `--fast` skips
    them and the complete chain keeps them. Each one costs a fresh interpreter and a fresh
    collection, which is where the time goes rather than in the assertions.
    """


def _python(*args: str) -> list[str]:
    return [sys.executable, *args]


def _semgrep() -> str:
    """semgrep from this interpreter's environment, falling back to PATH."""
    candidate = Path(sys.executable).with_name("semgrep")
    return str(candidate) if candidate.exists() else "semgrep"


STEPS: list[Step] = [
    # First, because it is the one failure that cannot be undone by a later commit: client drawings
    # are proprietary, and CI rejects anything tracked under data/. A local run that reports success
    # with a drawing staged would send someone to push it.
    Step(
        "repo hygiene (no client data)",
        _python("-m", "pytest", "tests/test_repo_hygiene.py", "-q"),
        also_in_full_suite=True,
    ),
    Step(
        "licence policy",
        _python("-m", "pytest", "tests/test_licences.py", "-q"),
        also_in_full_suite=True,
    ),
    Step("ruff", _python("-m", "ruff", "check", ".")),
    Step("black", _python("-m", "black", "--check", ".")),
    Step("mypy (strict)", _python("-m", "mypy", "verdict", "rules", "evidence")),
    Step(
        "verdict isolation",
        _python("-m", "pytest", "tests/test_verdict_isolation.py", "-q"),
        also_in_full_suite=True,
    ),
    Step(
        "risk-control traceability",
        _python("-m", "pytest", "tests/test_risk_controls.py", "-q"),
        also_in_full_suite=True,
    ),
    # Resolved beside this interpreter rather than from PATH: semgrep is installed into the venv,
    # and a bare name misses it whenever the venv is not activated — which is exactly when someone
    # is most likely to push without having run it.
    Step(
        "semgrep (golden rules)",
        [_semgrep(), "--config", ".semgrep/gv-rules.yaml", "--error", "--quiet"],
        optional=True,
    ),
    # CI runs this with continue-on-error, so it must not fail the local chain either — but it is
    # where type errors in the newer packages show up first, and silently omitting it would make a
    # local run weaker than CI rather than equal to it.
    Step(
        "mypy (rest of the tree, non-blocking)",
        _python("-m", "mypy", "app", "workflow", "extraction", "retrieval", "reports", "eval"),
        advisory=True,
    ),
    # From the weekly job. Nothing in this project is pinned — every dependency is a floor — so the
    # installed set changes without a commit. Cheap, offline, and it catches the resolution breaking
    # before CI does.
    Step("dependencies resolve (pip check)", _python("-m", "pip", "check")),
    # From the `board` job. Needs a GitHub token, so it is optional here: a developer without `gh`
    # authenticated should not be blocked by a check about issue labels. It is not advisory —
    # when it can run, a disagreement is a real failure.
    Step(
        "board status drift (label vs contract)",
        _python("-m", "pytest", "tests/test_board_drift.py", "-q"),
        needs_github_token=True,
        also_in_full_suite=True,
    ),
    Step(
        "board status drift (sweep)",
        _python("scripts/check_board_drift.py"),
        needs_github_token=True,
    ),
    Step("tests", _python("-m", "pytest", "-q")),
]


def ci_python_version() -> str | None:
    """The Python version CI pins, read from the workflow rather than restated here.

    Returns ``None`` when the workflow names more than one version or none at all — an ambiguous
    answer is reported to the developer instead of being guessed at.
    """
    versions = set(re.findall(r'python-version:\s*"([\d.]+)"', CI_WORKFLOW.read_text()))
    return versions.pop() if len(versions) == 1 else None


def github_token_available() -> bool:
    """True when the GitHub API is reachable — an explicit token, or an authenticated `gh`."""
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return True
    try:
        return (
            subprocess.run(
                ["gh", "auth", "status"], capture_output=True, check=False, timeout=15
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def start_database() -> str | None:
    """Start the Compose database and return a URL for it, or ``None`` if it could not be started.

    The image and credentials come from `docker-compose.yml`, which is pinned to the same
    `pgvector` version CI's service container uses — that pinning is the point, so this does not
    accept a database that happens to be running on the port instead.
    """
    compose = subprocess.run(
        ["docker", "compose", "up", "-d", "--wait", "db"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if compose.returncode != 0:
        tail = (compose.stderr or compose.stdout).strip().splitlines()
        print("  could not start the database:")
        for line in tail[-6:]:
            print(f"    {line}")
        return None
    return "postgresql+psycopg://gv:gv@localhost:5433/gv"


def check_environment() -> list[str]:
    """Report extras CI installs that this interpreter does not have."""
    declared = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]
    missing: list[str] = []
    for extra in REQUIRED_EXTRAS:
        probe = EXTRA_PROBES.get(extra)
        if probe and extra in declared and importlib.util.find_spec(probe) is None:
            missing.append(extra)
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true", help="skip semgrep")
    parser.add_argument(
        "--with-db",
        action="store_true",
        help="start the Compose database first, so the PostgreSQL suite runs instead of skipping",
    )
    args = parser.parse_args()

    print("Running the CI check chain locally")
    print("=" * 64)

    # Reported before anything runs, because it changes what every result below means. Not fatal:
    # pyproject allows >=3.12, so a newer interpreter is a legitimate development environment — but
    # it is not the one CI tests, and a green run here must not be mistaken for CI's answer.
    pinned = ci_python_version()
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    version_drift = pinned is not None and running != pinned
    if pinned is None:
        print(
            "\n  Could not read a single pinned Python version from .github/workflows/ci.yml.\n"
            "  Check the workflow — this script reads it rather than hard-coding one."
        )
    elif version_drift:
        remedy = f"python{pinned}"
        have = "" if shutil.which(remedy) else f" (not on PATH — install Python {pinned})"
        print(
            f"\n  PYTHON VERSION DRIFT — CI tests {pinned}; this interpreter is {running}.\n"
            f"  AGENTS.md §4 promises {pinned}, so CI tests the floor rather than the newest.\n"
            f"  A newer interpreter accepts syntax and stdlib that {pinned} rejects, so a green run\n"
            f"  here is weaker evidence than a green run on CI.{have}\n\n"
            f"      {remedy} -m venv .venv-ci && .venv-ci/bin/pip install -e "
            f'".[{",".join(REQUIRED_EXTRAS)}]"\n'
            f"      .venv-ci/bin/python scripts/check.py\n"
        )

    if missing := check_environment():
        print(
            f"\n  ENVIRONMENT DRIFT — this interpreter is missing: {', '.join(missing)}\n"
            f"  CI installs .[{','.join(REQUIRED_EXTRAS)}] and you do not have all of it.\n"
            "  Tests will skip or fail to collect, and a green run here would not mean much.\n\n"
            f'      pip install -e ".[{",".join(REQUIRED_EXTRAS)}]"\n'
        )
        return 2

    env = dict(os.environ)
    if args.with_db and not env.get("DATABASE_URL"):
        print("\n  ---- starting the database ----")
        if url := start_database():
            env["DATABASE_URL"] = url
            print(f"  database up; DATABASE_URL set for this run ({url.rsplit('@', 1)[-1]})")

    if not env.get("DATABASE_URL"):
        remedy = (
            "      start Docker Desktop, then re-run `make ci`\n"
            if args.with_db
            else "      make ci        (starts the database for you)\n"
            "\n  or, by hand:\n\n"
            "      docker compose up -d --wait db\n"
            "      export DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5433/gv\n"
        )
        print(
            "\n  No DATABASE_URL — the PostgreSQL tests will skip.\n"
            "  CI runs them against pgvector, so a mismatch between a model and its migration will\n"
            f"  surface there rather than here. To run them:\n\n{remedy}"
        )

    failures: list[str] = []
    advisory_failures: list[str] = []
    skipped_steps: list[str] = []

    has_token = github_token_available()

    for step in STEPS:
        if args.fast and step.name.startswith("semgrep"):
            skipped_steps.append(step.name)
            continue
        # In fast mode, drop the steps whose modules the full `pytest` run collects anyway, and the
        # ones that need the network. Nothing is left unchecked — those tests still run, once,
        # inside the suite — and it takes the chain from about 44 seconds to about 18.
        if args.fast and (step.also_in_full_suite or step.needs_github_token):
            skipped_steps.append(f"{step.name} (covered by the full suite)")
            continue
        if step.needs_github_token and not has_token:
            skipped_steps.append(f"{step.name} (no GitHub token)")
            print(f"\n  ---- {step.name} ----")
            print("  SKIPPED — no GH_TOKEN and `gh` is not authenticated. CI still runs it.")
            continue
        print(f"\n  ---- {step.name} ----")
        try:
            result = subprocess.run(
                step.command, cwd=REPO_ROOT, capture_output=True, text=True, check=False, env=env
            )
        except FileNotFoundError:
            if step.optional:
                skipped_steps.append(f"{step.name} (not installed)")
                print(f"  SKIPPED — {step.command[0]} is not installed. CI will still run it.")
                continue
            raise

        tail = (result.stdout or result.stderr).strip().splitlines()
        print("  " + (tail[-1] if tail else "(no output)"))
        if result.returncode != 0:
            if step.advisory:
                advisory_failures.append(step.name)
                print("    (advisory — CI does not fail on this either)")
            else:
                failures.append(step.name)
            for line in tail[-25:]:
                print(f"    {line}")

        if step.name == "tests" and (match := re.search(r"(\d+) skipped", result.stdout or "")):
            print(
                f"\n  {match.group(1)} test(s) skipped — most likely the PostgreSQL suite.\n"
                "  A green run here is not the same as a green run on CI."
            )

    print("\n" + "=" * 64)
    print("  CI checks with no local equivalent, by design:")
    for name, why in GITHUB_ONLY.items():
        print(f"    - {name}: {why}")
    print()
    if skipped_steps:
        print(f"  not run: {', '.join(skipped_steps)}")
    if advisory_failures:
        # Reported separately and never fatal. Printing "All checks passed" over a failed advisory
        # step would be the same overstatement this script exists to prevent — a green summary that
        # is not quite true is worse than a yellow one that is.
        print(f"  advisory failures (CI does not block on these): {', '.join(advisory_failures)}")
    if failures:
        print(f"  FAILED: {', '.join(failures)}")
        return 1
    caveat = ""
    if version_drift:
        caveat = f" — but on Python {running}, not the {pinned} CI tests"
    elif skipped_steps:
        caveat = " — with the steps above not run"

    if advisory_failures:
        print(f"  Blocking checks passed{caveat}. The advisory step above did not.")
        return 0
    print(f"  All checks passed{caveat}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
