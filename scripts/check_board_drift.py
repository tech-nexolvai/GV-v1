"""Fail when an issue's `status:` label and its contract `status:` disagree.

Two things claim to say whether a story may be worked on. `scripts/issue_gate.py` reads the **contract**
in the issue body. Every human, board view, filter and search reads the **label**. Nothing has ever
made them agree, so they drift — and which one is believed depends entirely on who is looking.

A sweep on 2026-08-19 found eighteen disagreements across 135 open issues, and the drift was almost
all one way: fifteen had the label more optimistic than the contract. Twelve of those said
`status:ready` on the board while the gate would stop the work outright. That is the expensive
direction — somebody filters for ready work, picks one up, reads it, plans it, and only then runs the
gate and is told no.

This is a board check, not a code check, so it can fail on a pull request that changed nothing. That
is deliberate and it is the point: the alternative is a sweep somebody remembers to run, and the last
one was the first one.

Usage: `python scripts/check_board_drift.py` — needs `gh` authenticated (`GH_TOKEN` in CI).

Verification: `tests/test_board_drift.py`
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from issue_gate import STATUS, parse_contract

LABEL_PREFIX = "status:"


@dataclass(frozen=True, slots=True)
class Disagreement:
    """One issue whose two answers differ, and what each of them says."""

    number: int
    title: str
    label: str
    contract: str

    def __str__(self) -> str:
        return (
            f"  #{self.number:<5} label says {self.label:<18} contract says {self.contract:<18} "
            f"{self.title[:44]}"
        )


def disagreements(issues: list[dict]) -> list[Disagreement]:
    """Every issue where the label and the contract do not say the same thing.

    Pure, so the comparison is tested without a network call. An issue with no parsable contract is
    not a disagreement — `scripts/issue_gate.py` already refuses those as MALFORMED, and reporting
    them twice would train people to skim this list.
    """
    found = []
    for issue in issues:
        labels = [label["name"] for label in issue.get("labels", [])]
        stated = sorted(
            name.removeprefix(LABEL_PREFIX) for name in labels if name.startswith(LABEL_PREFIX)
        )
        contract = parse_contract(issue.get("body") or "")
        if contract is None or "status" not in contract:
            continue

        declared = str(contract["status"]).strip()
        if declared not in STATUS:
            continue  # issue_gate reports this as MALFORMED; one voice per defect

        # No label and two labels are both disagreements. An issue carrying `status:ready` *and*
        # `status:deferred` is not half-right — a filter matches it either way, which is worse than
        # carrying neither, because it looks decided.
        if stated != [declared]:
            found.append(
                Disagreement(
                    number=issue["number"],
                    title=issue.get("title", ""),
                    label="/".join(stated) if stated else "(none)",
                    contract=declared,
                )
            )
    return sorted(found, key=lambda entry: entry.number)


def fetch_open_issues() -> list[dict]:
    """Every open issue, with the fields the comparison needs."""
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            "number,title,body,labels",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return list(json.loads(result.stdout))


def main() -> int:
    try:
        issues = fetch_open_issues()
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        # Loud, and a failure. A board check that quietly passes when it could not read the board is
        # a green tick meaning "not checked", which is the most misleading result available.
        print(f"::error::could not read the issue board: {error}")
        return 1

    found = disagreements(issues)
    if not found:
        print(f"OK — all {len(issues)} open issues agree with their contracts.")
        return 0

    print(f"{len(found)} issue(s) whose label and contract disagree:\n")
    for entry in found:
        print(entry)
    print(
        "\nThe contract is the one that decides: scripts/issue_gate.py reads it, and the label is "
        "what everyone else reads.\nFix whichever is wrong, but they have to say the same thing — a "
        "board that reads 'ready' on work the gate\nstops is how planning time gets spent on issues "
        "that were never available."
    )
    for entry in found:
        print(f"::error::#{entry.number} label={entry.label} contract={entry.contract}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
