"""Perform a gold-set run, or say exactly why one cannot be performed.

`CLAUDE.md` makes this a merge gate: *"No change to `verdict/`/`rules/`/`evidence/` merges without
unit tests and a gold-set run that does not regress critical false-PASS."* Until now there was no way
to perform one — `eval/metrics.py:compute_all` had no caller outside its own tests — so the gate was a
sentence describing something nobody could do, and every change to those directories merged without
it.

    python scripts/run_gold_set.py              # the synthetic lane, which works today
    python scripts/run_gold_set.py --gold       # the real lane, which reports its blockers

**The two lanes are not interchangeable and this script will not let them be confused.** The synthetic
lane proves the deterministic engine turns known operands into the right verdict. It says nothing
about whether the system reads a real drawing correctly, because no case in it has a drawing — so its
critical false-PASS rate is a fact about arithmetic, not the project's safety number. The banner says
so on every run rather than trusting whoever reads it to remember.

Exit 0 when every case matched its authored expectation, 1 when one did not, 2 when the requested lane
cannot run at all.

Verification: `tests/eval/test_harness.py`
"""

from __future__ import annotations

import argparse
import sys

from eval.harness import GoldLaneUnavailable, run_gold, run_synthetic, summary
from eval.metrics import report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gold",
        action="store_true",
        help="run the real lane instead of the synthetic one (currently blocked; reports why)",
    )
    args = parser.parse_args(argv)

    try:
        run = run_gold() if args.gold else run_synthetic()
    except GoldLaneUnavailable as unavailable:
        # Exit 2, not 1. A caller has to be able to tell "the run found a problem" from "there was no
        # run", and a CI step that treated them alike would report a passing gold set on a repository
        # that has never had one.
        print(str(unavailable), file=sys.stderr)
        return 2

    print(summary(run))
    print()
    print(report(run.metrics))

    if run.disagreed:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main() in tests
    raise SystemExit(main())
