#!/usr/bin/env python3
"""Pariksha piece kinds: score the kinds a stored run names, store the scorecard, compare (#1250).

No model call, no cost: the pieces a run already read (and the vendor's text layers on their pages)
are read again, read-only, and the kind one source names for each keyed piece is scored right,
wrong or unknown against a local kind key (`eval/pariksha/kinds.py`; build one with
`scripts/pariksha_kind_key.py`).

    # --source product: the kinds the run stored; face-tag: the measured face-tag rule
    python scripts/pariksha_kinds.py score --source product --db <database name or URL> \\
        --key ~/gv-local/<kind key>.json [--run <extraction run id>] \\
        --code-version <commit of the code that made the run> --out ~/gv-local/<card>.json

    # exits 0 on ACCEPT (better, 0 new wrong), 1 on REJECT, 2 on a refusal
    python scripts/pariksha_kinds.py compare <before.json> <after.json>

`--db`, `--run`, `--code-version` and `--out` work as in `scripts/pariksha_scorecard.py`. Keys and
scorecards hold client values: keep both outside any repository (an `--out` inside a git working
tree is refused).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pariksha_scorecard import (
    REFUSED,
    Refused,
    _database_url,
    _outside_git,
    _scorer_version,
    summary,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from eval.pariksha.keys import KeyFormatError
from eval.pariksha.kinds import (
    SOURCES,
    compare_kinds,
    load_stored_pieces,
    read_kinds_key,
    score_kinds,
)
from eval.pariksha.scorecard import ScorecardError, read_scorecard, write_scorecard
from eval.pariksha.stored import StoredRunError
from eval.regression import IncomparableRuns
from eval.runs import MissingProvenanceError


def _score(args: argparse.Namespace) -> int:
    out = _outside_git(args.out)
    key = read_kinds_key(args.key.expanduser())
    url = _database_url(args.db)
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            try:
                stored = load_stored_pieces(
                    session, database=url.database or "unknown", extraction_run_id=args.run
                )
            finally:
                session.rollback()
    finally:
        engine.dispose()
    card = score_kinds(
        key,
        stored,
        source=args.source,
        code_version=args.code_version,
        scorer_version=_scorer_version(),
    )
    write_scorecard(card, out)
    print(summary(card))
    print(f"  kind source: {args.source}")
    print(f"  written: {out}")
    return 0


def _compare(args: argparse.Namespace) -> int:
    comparison = compare_kinds(
        read_scorecard(args.before.expanduser()), read_scorecard(args.after.expanduser())
    )
    print(comparison.render())
    return 0 if comparison.accepted else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    score = commands.add_parser("score", help="score a stored run into a new scorecard file")
    score.add_argument("--source", choices=SOURCES, required=True)
    score.add_argument("--db", required=True, help="the stored run's database: URL or name")
    score.add_argument("--key", type=Path, required=True)
    score.add_argument("--run", type=UUID, help="the extraction run, when the database has several")
    score.add_argument(
        "--code-version", required=True, help="the commit of the code that made the stored run"
    )
    score.add_argument("--out", type=Path, required=True, help="the new scorecard file")
    score.set_defaults(handler=_score)
    compare = commands.add_parser("compare", help="compare two kind scorecards: before, after")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.set_defaults(handler=_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        Refused,
        KeyFormatError,
        StoredRunError,
        ScorecardError,
        IncomparableRuns,
        MissingProvenanceError,
        FileExistsError,
    ) as error:
        # A refusal is not a verdict: 0 is ACCEPT, 1 is REJECT, 2 is "will not judge this".
        print(f"pariksha kinds: {error}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
