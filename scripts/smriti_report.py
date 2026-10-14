#!/usr/bin/env python3
"""Smriti's learner report: per agent, its AI answers, the person decisions, corrections (AI misread
or vendor drew it), overturned automatic results and how useful its inspector's stops were (#1272).

No model call, no cost, nothing changed: the database is only read (read-only transaction).

    python scripts/smriti_report.py --db <database name or URL> [--window-days 3650] \\
        [--vendors] [--out ~/gv-local/<report>.txt]

`NOT MEASURED` is shown wherever there is nothing to divide by, never 0%. The real-reviewer list is
the local file `GV_SMRITI_REVIEWERS` names (see `eval/smriti/actors.py`); without it, every name
counts as "other". `--vendors` adds, per recorded vendor, the FAILs a person confirmed or signed
beside every FAIL (`reports/vendor_patterns.py`, `confirmed_only`), counting only names on the
same local list: report only. The report holds
client values (pages, names): `--out` inside a git working tree is refused, and a file is written
once, never overwritten.

Exit codes: 0 done, 2 refused.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from pariksha_scorecard import REFUSED, Refused, _database_url, _outside_git, _scorer_version
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.package import Package
from eval.smriti.actors import ReviewersFileError, reviewers_from_environment
from eval.smriti.report import learner_report, render_report
from reports.vendor_patterns import vendor_patterns

__all__ = ["REFUSED", "main", "vendor_lines"]


def vendor_lines(
    session: Session, window: timedelta, now: datetime, reviewers: frozenset[str] | None
) -> list[str]:
    """Per recorded vendor: every FAIL, and those a real reviewer (a name on the local list)
    confirmed or signed. Report only. With no list, none counts, and the lines say why."""
    vendors = sorted(
        {
            vendor
            for vendor in session.scalars(select(Package.vendor).where(Package.vendor.is_not(None)))
            if vendor and vendor.strip()
        }
    )
    lines = [
        (
            f"Vendor report (reports/vendor_patterns.py, the last {window.days} days; report only, "
            "never a rule key): only FAILs a person confirmed or signed count"
        )
    ]
    if not vendors:
        lines.append("  no package names a vendor")
    for vendor in vendors:
        every = vendor_patterns(session, vendor, window, now=now)
        confirmed = vendor_patterns(
            session, vendor, window, now=now, confirmed_only=True, reviewers=reviewers
        )
        all_fails = sum(len(found) for found in every.recurring_failures.values())
        kept = sum(len(found) for found in confirmed.recurring_failures.values())
        lines.append(
            f"  {vendor}: {kept} person-confirmed FAILs over {len(confirmed.recurring_failures)} "
            f"rule versions (of {all_fails} FAILs in all; trend {confirmed.trend})"
            + (f"; {confirmed.not_counted}" if confirmed.not_counted else "")
        )
    return lines


def _report(args: argparse.Namespace) -> int:
    out = None if args.out is None else _outside_git(args.out)
    if args.window_days <= 0:
        raise Refused("--window-days must be positive")
    window = timedelta(days=args.window_days)
    url = _database_url(args.db)
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            try:
                report = learner_report(
                    session,
                    database=url.database or "unknown",
                    code_version=_scorer_version(),
                    window=window,
                )
                text = render_report(report)
                if args.vendors:
                    now = datetime.now(UTC)
                    names = reviewers_from_environment().names
                    text += "\n\n" + "\n".join(vendor_lines(session, window, now, names))
            finally:
                session.rollback()
    finally:
        engine.dispose()
    print(text)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("x", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"written: {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--db", required=True, help="the database: URL or name")
    parser.add_argument(
        "--window-days",
        type=int,
        default=3650,
        help="how far back eval/correction_rate.py and the vendor report look",
    )
    parser.add_argument(
        "--vendors", action="store_true", help="add the person-confirmed vendor report"
    )
    parser.add_argument("--out", type=Path, help="also write the report to this new local file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _report(args)
    except (Refused, ReviewersFileError, FileExistsError) as error:
        print(f"smriti report: {error}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
