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

    python scripts/check.py            # everything
    python scripts/check.py --fast     # skip semgrep, for a tight edit loop
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Extras CI installs for the main job. Kept in step with `.github/workflows/ci.yml`.
REQUIRED_EXTRAS = ("dev", "rules", "platform")

#: One importable module per extra, to detect drift without shelling out to pip.
EXTRA_PROBES = {
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
        "repo hygiene (no client data)", _python("-m", "pytest", "tests/test_repo_hygiene.py", "-q")
    ),
    Step("licence policy", _python("-m", "pytest", "tests/test_licences.py", "-q")),
    Step("ruff", _python("-m", "ruff", "check", ".")),
    Step("black", _python("-m", "black", "--check", ".")),
    Step("mypy (strict)", _python("-m", "mypy", "verdict", "rules", "evidence")),
    Step("verdict isolation", _python("-m", "pytest", "tests/test_verdict_isolation.py", "-q")),
    Step("risk-control traceability", _python("-m", "pytest", "tests/test_risk_controls.py", "-q")),
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
    Step("tests", _python("-m", "pytest", "-q")),
]


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
    args = parser.parse_args()

    print("Running the CI check chain locally")
    print("=" * 64)

    if missing := check_environment():
        print(
            f"\n  ENVIRONMENT DRIFT — this interpreter is missing: {', '.join(missing)}\n"
            f"  CI installs .[{','.join(REQUIRED_EXTRAS)}] and you do not have all of it.\n"
            "  Tests will skip or fail to collect, and a green run here would not mean much.\n\n"
            f'      pip install -e ".[{",".join(REQUIRED_EXTRAS)}]"\n'
        )
        return 2

    if not os.environ.get("DATABASE_URL"):
        print(
            "\n  No DATABASE_URL — the PostgreSQL tests will skip.\n"
            "  CI runs them, so a mismatch between a model and its migration will surface there\n"
            "  rather than here. To run them locally:\n\n"
            "      docker compose up -d db\n"
            "      export DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5433/gv\n"
        )

    failures: list[str] = []
    skipped_steps: list[str] = []

    for step in STEPS:
        if args.fast and step.name.startswith("semgrep"):
            skipped_steps.append(step.name)
            continue
        print(f"\n  ---- {step.name} ----")
        try:
            result = subprocess.run(
                step.command, cwd=REPO_ROOT, capture_output=True, text=True, check=False
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
    if skipped_steps:
        print(f"  not run: {', '.join(skipped_steps)}")
    if failures:
        print(f"  FAILED: {', '.join(failures)}")
        return 1
    print("  All checks passed. This is what CI will run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
