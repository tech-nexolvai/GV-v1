"""The kill harness: run a package review in a subprocess and stop it dead mid-stage (#217, C4.5).

The issue asks for this first, and it is the right order. Every other claim in C4.5 — that a restart redoes
no paid work, that partial evidence is never half-present — is a claim about what survives a process that
did not get to finish. Asserting that from inside the process cannot be convincing: a `raise` unwinds
cleanly, runs `except` blocks and lets SQLAlchemy tidy up. A `SIGKILL` does none of that, which is what a
worker losing its box actually looks like.

**How the kill lands at a known point.** The child announces each stage on stdout as it enters it, and the
parent kills on reading the name it was waiting for. No sleeps, so there is no timing to tune and no
flakiness to chase — the alternative, "sleep 200ms then kill", fails on a loaded machine and passes
vacuously on a fast one.

**Why a subprocess rather than a thread.** A thread cannot be killed in a way that resembles a lost worker;
`SIGKILL` to a child gives PostgreSQL exactly the situation it must handle — a connection that vanishes
with a transaction open, which the server rolls back.

Source: issue #217 · Verification: `tests/workflow/test_durability.py`
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from workflow.review import STAGES

REPO_ROOT = Path(__file__).resolve().parents[2]

#: How long to wait for the child to reach the stage we mean to kill it in.
#:
#: **Enforced, not merely documented.** The first version named this constant and then used a blocking
#: `readline()`, so a child that hung — or one whose stage never announced itself — hung the whole suite
#: with a timeout sitting unused two screens above. Review caught the contradiction. Every read now waits
#: against a deadline and the harness gives up loudly.
#:
#: Generous, because too small is a flaky test and too large costs nothing: the wait ends as soon as the
#: child speaks.
REACH_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class Killed:
    """What the harness observed before the child died."""

    stages_entered: tuple[str, ...]
    killed_in: str
    exit_code: int | None

    @property
    def completed_stages(self) -> tuple[str, ...]:
        """The stages the child got through before the one it died in.

        Not read from the database on purpose — this is what the *child* believed, so a test can compare it
        against what the database kept and notice a disagreement.
        """
        return self.stages_entered[:-1]


# The child. Kept as source rather than a module so the harness is readable in one place, and so it cannot
# be imported accidentally by the test process — importing it would run a workflow.
_CHILD = '''
import os, sys
from uuid import UUID
from sqlalchemy import create_engine
from app.db.session import session_factory, unit_of_work
from workflow.review import STAGES, run_stage

url = os.environ["GV_KILL_DATABASE_URL"]
revision_id = UUID(os.environ["GV_KILL_REVISION"])
run_id = UUID(os.environ["GV_KILL_RUN"])
factory = session_factory(create_engine(url))


def _announce(name):
    """Announce this stage, wait to be killed, and return the shape the protocol asks for."""
    print(name, flush=True)
    # If the parent never kills us we carry on, so a harness bug shows up as a test that fails rather
    # than one that hangs.
    sys.stdin.readline()


class Announcing:
    """Says which stage it is in, then waits. Every method written out.

    Not `__getattr__`: that answered to any name at all, so a typo in a test would have been announced as
    a stage rather than refused — and it is the same looseness that once let this stub return a mapping
    where `extract_pages` must return a sequence, which `join_pages` counted as a page nobody read.
    """

    def ingest(self, session, package_revision_id):
        _announce("ingest")
        return {"ran": "ingest"}

    def extract_pages(self, session, package_revision_id):
        _announce("extract_pages")
        return ()

    def match(self, session, package_revision_id):
        _announce("match")
        return {"ran": "match"}

    def validate_evidence(self, session, package_revision_id):
        _announce("validate_evidence")
        return {"ran": "validate_evidence"}

    def run_checks(self, session, package_revision_id):
        _announce("run_checks")
        return {"ran": "run_checks"}

    def generate_outputs(self, session, package_revision_id):
        _announce("generate_outputs")
        return {"ran": "generate_outputs"}


stages = Announcing()
for stage, state in STAGES:
    with unit_of_work(factory) as session:
        run_stage(
            session,
            stage=stage,
            state=state,
            package_revision_id=revision_id,
            workflow_run_id=run_id,
            stages=stages,
        )
print("finished", flush=True)
'''


def kill_at(
    step: str,
    *,
    database_url: str,
    package_revision_id: UUID,
    workflow_run_id: UUID,
) -> Killed:
    """Run the workflow in a subprocess and `SIGKILL` it as it enters `step`.

    Returns what the child got through, so a test can compare the child's own account against what the
    database kept. Raises if the child never reaches the step — a harness that silently killed nothing
    would make every assertion after it meaningless.
    """
    if step not in {name for name, _ in STAGES}:
        raise ValueError(f"{step!r} is not a stage; the harness would wait forever")

    environment = dict(
        os.environ,
        GV_KILL_DATABASE_URL=database_url,
        GV_KILL_REVISION=str(package_revision_id),
        GV_KILL_RUN=str(workflow_run_id),
        PYTHONPATH=str(REPO_ROOT),
    )
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", _CHILD],
        cwd=REPO_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    entered: list[str] = []
    deadline = time.monotonic() + REACH_TIMEOUT_SECONDS
    try:
        if child.stdout is None or child.stdin is None:  # pragma: no cover - Popen was given both
            raise AssertionError("the child was started without pipes")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"the child did not reach {step!r} within {REACH_TIMEOUT_SECONDS}s; it entered "
                    f"{entered}"
                )
            ready, _, _ = select.select([child.stdout], [], [], remaining)
            if not ready:
                continue
            line = child.stdout.readline()
            if not line:
                raise AssertionError(
                    f"the child stopped before reaching {step!r}; it entered {entered}"
                )
            name = line.strip()
            entered.append(name)
            if name == step:
                # Not terminate(): SIGTERM lets Python unwind, which is the tidy exit this test is
                # specifically not about.
                child.kill()
                break
            if name == "finished":
                raise AssertionError(f"the workflow finished without entering {step!r}")
            child.stdin.write("go\n")
            child.stdin.flush()
    finally:
        child.kill()
        child.wait(timeout=REACH_TIMEOUT_SECONDS)
        # Closed explicitly: `Popen` keeps both pipes open, so without this every call to this harness
        # leaks two file descriptors — and it is called once per stage, per parametrised test.
        for pipe in (child.stdin, child.stdout):
            if pipe is not None:
                pipe.close()

    return Killed(
        stages_entered=tuple(entered),
        killed_in=step,
        exit_code=child.returncode,
    )
