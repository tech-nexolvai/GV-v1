"""Fail CI if duration-based pytest shards do not cover collection exactly once."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path


def parse_node_ids(output: str) -> list[str]:
    """Extract pytest's quiet collection lines, excluding summary and warning text."""
    return [
        line.strip() for line in output.splitlines() if line.startswith("tests/") and "::" in line
    ]


def validate_partition(all_node_ids: Sequence[str], shards: Sequence[Sequence[str]]) -> None:
    """Require nonempty shards whose contents are disjoint and equal full collection."""
    if not all_node_ids:
        raise ValueError("full pytest collection is empty")
    if len(set(all_node_ids)) != len(all_node_ids):
        raise ValueError("full pytest collection contains duplicate node IDs")
    if not shards:
        raise ValueError("no pytest shards were configured")

    for index, shard in enumerate(shards, start=1):
        if not shard:
            raise ValueError(f"pytest shard {index} is empty")
        if len(set(shard)) != len(shard):
            raise ValueError(f"pytest shard {index} contains duplicate node IDs")

    counts = Counter(node_id for shard in shards for node_id in shard)
    duplicates = sorted(node_id for node_id, count in counts.items() if count != 1)
    missing = sorted(set(all_node_ids) - counts.keys())
    unexpected = sorted(counts.keys() - set(all_node_ids))
    if duplicates or missing or unexpected:
        raise ValueError(
            "pytest shard coverage mismatch: "
            f"duplicate={len(duplicates)}, missing={len(missing)}, "
            f"unexpected={len(unexpected)}"
        )


def _collect(command: Sequence[str], *, cwd: Path) -> list[str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = (result.stdout + result.stderr)[-4000:]
        raise RuntimeError(f"collection command failed ({result.returncode}):\n{detail}")
    node_ids = parse_node_ids(result.stdout)
    if not node_ids:
        raise RuntimeError(f"collection command produced no node IDs:\n{result.stdout[-2000:]}")
    return node_ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--splits", type=int, default=4)
    parser.add_argument("--durations-path", default=".test_durations")
    args = parser.parse_args()
    if args.splits < 1:
        parser.error("--splits must be at least 1")

    root = Path(__file__).resolve().parents[1]
    base = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    duration_args = ["--durations-path", args.durations_path]
    full = _collect(base + duration_args, cwd=root)
    shards = [
        _collect(
            base
            + [
                "--splits",
                str(args.splits),
                "--group",
                str(group),
                "--splitting-algorithm",
                "least_duration",
            ]
            + duration_args,
            cwd=root,
        )
        for group in range(1, args.splits + 1)
    ]
    try:
        validate_partition(full, shards)
    except ValueError as exc:
        print(f"pytest shard coverage check failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"pytest shard coverage verified: {len(full)} tests, "
        f"{args.splits} nonempty disjoint shards, exact union"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
