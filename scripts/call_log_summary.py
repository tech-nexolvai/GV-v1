#!/usr/bin/env python3
"""Where an answer's model money went, from its call log (rebuild plan Step 1B).

Every model call of a review assistant answer is one line of `<chat folder>/calls/<message
id>.jsonl` (`app/review/assistant/call_log.py`). This script adds the lines up:

- **by phase, by kind and by model:** calls, input tokens, cache writes (and their 5-minute and
  1-hour parts, where the service reported the split), cache reads, output tokens, cost, and the
  share of the cost that was cache writes (`cache_write_cost_usd` over `cost_usd`);
- **by prompt prefix** (`prefix_hash`): how many calls sent each prefix, how many of them read
  the prompt cache (cache read above 0) and how many wrote it, so a prefix written often and read
  rarely stands out.

No model call, no cost, nothing written. Lines that are not JSON objects are counted and skipped.

    # one message's log, or every log under a folder (a chat's `calls/`, a run's folder)
    python scripts/call_log_summary.py <file-or-folder>
    python scripts/call_log_summary.py <file-or-folder> --json

Exit codes: 0 done, 2 nothing to read.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

#: The token counts each total adds up (the log's own field names).
TOKENS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "ephemeral_5m_input_tokens",
    "ephemeral_1h_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)
#: The groupings, in the order they are shown.
GROUPS = ("phase", "kind", "model")
#: The short column names of the text report.
_SHORT = {
    "input_tokens": "input",
    "cache_creation_input_tokens": "write",
    "ephemeral_5m_input_tokens": "write 5m",
    "ephemeral_1h_input_tokens": "write 1h",
    "cache_read_input_tokens": "read",
    "output_tokens": "output",
}


def log_files(target: Path) -> list[Path]:
    """`target` itself when it is a file; else every `*.jsonl` under it, sorted."""
    if target.is_file():
        return [target]
    return sorted(path for path in target.rglob("*.jsonl") if path.is_file())


def read_lines(paths: Iterable[Path]) -> tuple[list[dict[str, Any]], int]:
    """Every line that is a JSON object, and how many were not."""
    rows: list[dict[str, Any]] = []
    bad = 0
    for path in paths:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                bad += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                bad += 1
    return rows, bad


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _money(value: object) -> Decimal:
    if not isinstance(value, str):
        return Decimal(0)
    try:
        return Decimal(value)
    except InvalidOperation:
        return Decimal(0)


def _empty() -> dict[str, Any]:
    return {
        "calls": 0,
        **{name: 0 for name in TOKENS},
        "cost_usd": Decimal(0),
        "cache_write_cost_usd": Decimal(0),
        "unpriced": 0,
    }


def _add(total: dict[str, Any], row: Mapping[str, Any]) -> None:
    total["calls"] += 1
    for name in TOKENS:
        total[name] += _int(row.get(name))
    total["cost_usd"] += _money(row.get("cost_usd"))
    total["cache_write_cost_usd"] += _money(row.get("cache_write_cost_usd"))
    if row.get("cost_usd") is None:
        total["unpriced"] += 1


def _share(total: Mapping[str, Any]) -> Decimal | None:
    cost: Decimal = total["cost_usd"]
    if not cost:
        return None
    write: Decimal = total["cache_write_cost_usd"]
    return (write / cost * 100).quantize(Decimal("0.1"))


def summarise(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The totals: all lines, each grouping (`GROUPS`) and each prompt prefix. Each line counts
    as one call (a refusal fallback's two models are two lines of one try)."""
    overall = _empty()
    groups: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in GROUPS}
    prefixes: dict[str, dict[str, Any]] = {}
    for row in rows:
        _add(overall, row)
        for name in GROUPS:
            key = str(row.get(name) if row.get(name) is not None else "(none)")
            _add(groups[name].setdefault(key, _empty()), row)
        digest = row.get("prefix_hash")
        if isinstance(digest, str) and digest:
            entry = prefixes.setdefault(
                digest, {"calls": 0, "read": 0, "wrote": 0, "cache_read_input_tokens": 0}
            )
            entry["calls"] += 1
            entry["read"] += 1 if _int(row.get("cache_read_input_tokens")) > 0 else 0
            entry["wrote"] += 1 if _int(row.get("cache_creation_input_tokens")) > 0 else 0
            entry["cache_read_input_tokens"] += _int(row.get("cache_read_input_tokens"))
    for totals in [overall, *(t for group in groups.values() for t in group.values())]:
        totals["write_share_percent"] = _share(totals)
    return {
        "overall": overall,
        "groups": groups,
        "prefixes": dict(sorted(prefixes.items(), key=lambda item: (-item[1]["calls"], item[0]))),
        "distinct_prefixes": len(prefixes),
    }


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _row(name: str, totals: Mapping[str, Any]) -> str:
    share = totals["write_share_percent"]
    counts = " ".join(f"{_SHORT[t]} {totals[t]}" for t in TOKENS)
    return (
        f"  {name}: {totals['calls']} calls · {counts} · ${totals['cost_usd']}"
        f" · writes {'-' if share is None else f'{share}%'} of cost"
        + (f" · {totals['unpriced']} unpriced" if totals["unpriced"] else "")
    )


def render(summary: Mapping[str, Any], bad: int = 0) -> str:
    """The summary as plain text."""
    lines = ["All calls", _row("all", summary["overall"])]
    for name in GROUPS:
        lines.append(f"By {name}")
        for key, totals in sorted(summary["groups"][name].items()):
            lines.append(_row(key, totals))
    lines.append(f"Prompt prefixes: {summary['distinct_prefixes']} distinct")
    for digest, entry in summary["prefixes"].items():
        lines.append(
            f"  {digest[:12]}: {entry['calls']} calls, {entry['read']} read the cache, "
            f"{entry['wrote']} wrote it ({entry['cache_read_input_tokens']} tokens read)"
        )
    if bad:
        lines.append(f"{bad} line(s) were not JSON objects and were skipped")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("target", type=Path, help="a call log (.jsonl) or a folder of them")
    parser.add_argument("--json", action="store_true", help="print the totals as JSON")
    args = parser.parse_args(argv)
    paths = log_files(args.target)
    rows, bad = read_lines(paths)
    if not rows:
        print(f"No call log lines found at {args.target}", file=sys.stderr)
        return 2
    summary = summarise(rows)
    if args.json:
        print(json.dumps(_plain({**summary, "skipped_lines": bad}), indent=2))
    else:
        print(render(summary, bad))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
