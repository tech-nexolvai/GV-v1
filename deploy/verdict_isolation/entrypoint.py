"""Start the verdict service, but only if it is provably isolated.

The container runs this, not the service. The preflight either passes and this `exec`s the real
process, or it raises and nothing starts. Two properties follow from `exec` rather than a subprocess
call: the verdict service inherits PID 1 and the container's signal handling unchanged, and this
supervisor does not stay resident where it could later be asked to do something.

`docs/DESIGN_CONTROLS.md` §2.3 — a verdict process that finds itself with network access fails to
start rather than running degraded. Checking from outside is what makes "fails to start" literal:
a check living inside the process shares that process's fate, so the failure mode where the service
comes up and the check quietly did not run is not available here.
"""

from __future__ import annotations

import os
import sys

from deploy.verdict_isolation.preflight import IsolationBroken, assert_isolated


def main(argv: list[str] | None = None) -> int:
    """Verify isolation, then hand the process over to the verdict service."""
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.stderr.write(
            "usage: entrypoint.py <command> [args...]\n"
            "Nothing was given to run. This refuses rather than defaulting to a command, because a "
            "default here would decide what the isolated process is — which is the one thing the "
            "run configuration must state explicitly.\n"
        )
        return 2

    try:
        assert_isolated()
    except IsolationBroken as error:
        sys.stderr.write(
            f"REFUSING TO START — the verdict process is not isolated.\n\n{error}\n\n"
            "Nothing has been started. See docs/DESIGN_CONTROLS.md section 2.3 and "
            "docs/decisions/F1_6_RUNTIME_ISOLATION.md.\n"
        )
        return 1

    os.execvp(argv[0], argv)


if __name__ == "__main__":  # pragma: no cover - exercised as a process, not by import
    raise SystemExit(main())
