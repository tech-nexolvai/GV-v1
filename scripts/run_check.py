#!/usr/bin/env python3
"""Pariksha's run check for one package revision: print it, and save it as JSON (#1220).

Reads only, inside a read-only transaction, and calls no model. It lists every place the run's
safety rules did not hold (`eval/run_check.py`), with the agent each points to and the suggested
action; it changes nothing.

    DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5434/<database> \\
        .venv/bin/python scripts/run_check.py <revision-id> [--json report.json]

`--database-url` overrides `DATABASE_URL`. The report holds code's own sentences, ids, page
numbers and counts, never a drawing's values; a report about a client's drawings is still kept
out of the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from eval.run_check import RunCheckReport, UnknownRevision, run_check


def render(report: RunCheckReport) -> str:
    """The report as plain text, one item per paragraph."""
    lines = [
        f"Run check for package revision {report.package_revision_id}",
        f"Product: {report.product_type or 'not stated (every product)'}",
        f"Suggested action: {report.action}",
        "Looked at: " + ", ".join(f"{count} {what}" for what, count in report.examined),
        "",
    ]
    if not report.items:
        lines.append("Every check that could be computed held.")
    for number, item in enumerate(report.items, start=1):
        lines.append(f"{number}. [{item.check.value}] {item.code}: {item.found}")
        lines.append(f"   Agent: {item.agent.value}. Suggested: {item.suggested}.")
        if item.ids:
            lines.append("   " + "; ".join(f"{kind} {value}" for kind, value in item.ids))
    for entry in report.not_computable:
        lines.extend(("", f"Not computable yet ({entry.check.value}): {entry.reason}"))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("revision", type=UUID, help="the package revision id")
    parser.add_argument("--json", type=Path, help="also save the report as JSON to this file")
    parser.add_argument("--database-url", help="overrides DATABASE_URL")
    args = parser.parse_args(argv)
    url = args.database_url or os.environ.get("DATABASE_URL")
    if not url:
        parser.error("set DATABASE_URL or pass --database-url")

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            # First in the transaction, so nothing after it can write.
            connection.execute(text("SET TRANSACTION READ ONLY"))
            with Session(bind=connection) as session:
                try:
                    report = run_check(session, args.revision)
                except UnknownRevision as error:
                    print(f"run check: {error}", file=sys.stderr)
                    return 2
            connection.rollback()
    finally:
        engine.dispose()

    print(render(report))
    if args.json is not None:
        args.json.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main(sys.argv[1:]))
