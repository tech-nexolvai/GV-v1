#!/usr/bin/env python3
"""Smriti: list reviewer decisions as candidate Pariksha key cases; a person confirms them (#1232).

No model call, no cost, nothing changed: the database is only read (read-only transaction).

    # export every reviewer decision of a database as candidates (all need a person's confirmation)
    python scripts/smriti_candidates.py export --db <database name or URL> \\
        --out ~/gv-local/<candidates>.json

    # list them for a person, per agent
    python scripts/smriti_candidates.py list ~/gv-local/<candidates>.json [--agent rekha] [--all]

    # write the candidates a person confirmed into a new local key file
    python scripts/smriti_candidates.py confirm ~/gv-local/<candidates>.json \\
        --confirmations ~/gv-local/<confirmations>.json --out ~/gv-local/<key>.json \\
        [--base ~/gv-local/<existing key>.json]

The confirmations file format is in `eval/smriti/confirm.py`. Candidates, confirmations and keys
hold client values (pages, what the reviewer decided): keep them outside any repository. An `--out`
inside a git working tree is refused. A file is written once, never overwritten.

Exit codes: 0 done, 2 refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from pariksha_scorecard import REFUSED, Refused, _database_url, _outside_git, _scorer_version
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from eval.pariksha.keys import AnswerKey, KeyFormatError, dump_key, read_key
from eval.smriti.candidates import (
    AGENT_ORDER,
    Candidate,
    CandidatesFileError,
    read_candidates,
    render_counts,
    write_candidates,
)
from eval.smriti.confirm import ConfirmationRefused, confirm_candidates
from eval.smriti.export import export_candidates

__all__ = ["REFUSED", "main"]


def _export(args: argparse.Namespace) -> int:
    out = _outside_git(args.out)
    url = _database_url(args.db)
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            try:
                export = export_candidates(
                    session, database=url.database or "unknown", code_version=_scorer_version()
                )
            finally:
                session.rollback()
    finally:
        engine.dispose()
    write_candidates(export, out)
    print(render_counts(export))
    print(f"  written: {out}")
    return 0


def _describe(candidate: Candidate) -> list[str]:
    where = f"page {candidate.page_number}" if candidate.page_number is not None else "page unknown"
    lines = [
        f"- {candidate.candidate_id}  [{candidate.status}]",
        (
            f"    {candidate.agent}: {candidate.corrects}; {candidate.kind} by "
            f"{candidate.decided_by} on {candidate.decided_at[:10]}, {where}"
        ),
        f"    recorded: {json.dumps(dict(candidate.recorded), ensure_ascii=False)}",
    ]
    if candidate.question is not None:
        lines.append(f"    question: {json.dumps(dict(candidate.question))}")
    if not candidate.convertible:
        lines.append(f"    not convertible: {candidate.not_convertible}")
    elif candidate.answer is not None:
        lines.append(f"    answer (from the decision): {json.dumps(dict(candidate.answer))}")
    else:
        lines.append(f"    the person states: {candidate.answer_needed}")
    return lines


def _list(args: argparse.Namespace) -> int:
    export = read_candidates(args.candidates.expanduser())
    print(render_counts(export))
    shown = [
        candidate
        for candidate in export.candidates
        if (args.agent is None or candidate.agent == args.agent)
        and (args.all or (candidate.current and candidate.convertible))
    ]
    order = {agent: index for index, agent in enumerate(AGENT_ORDER)}
    shown.sort(key=lambda c: (order.get(c.agent, len(order)), c.table, c.decided_at))
    print(
        f"\n{len(shown)} candidate(s)"
        + ("" if args.all else " that can become key cases (--all lists every record)")
        + ":"
    )
    for candidate in shown:
        print("\n".join(_describe(candidate)))
    return 0


def _confirm(args: argparse.Namespace) -> int:
    out = _outside_git(args.out)
    export = read_candidates(args.candidates.expanduser())
    try:
        confirmations = json.loads(args.confirmations.expanduser().read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise Refused(f"{args.confirmations.name} is not JSON: {error}") from error
    base = None
    if args.base is not None:
        loaded = read_key(args.base.expanduser())
        if not isinstance(loaded, AnswerKey):
            raise Refused(f"{args.base} is not an Akshara or Rekha key")
        base = loaded
    key = confirm_candidates(export, confirmations, base=base)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as handle:
        json.dump(dump_key(key), handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    print(
        f"key {key.name} {key.version} for {key.agent}: {len(key.cases)} case(s), "
        f"sha256 {key.sha256[:12]}; written: {out}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="export reviewer decisions as candidates")
    export.add_argument("--db", required=True, help="the database: URL or name")
    export.add_argument("--out", type=Path, required=True, help="the new candidates file")
    export.set_defaults(handler=_export)
    listing = commands.add_parser("list", help="list the candidates for a person")
    listing.add_argument("candidates", type=Path)
    listing.add_argument("--agent", help="only this agent's candidates")
    listing.add_argument(
        "--all", action="store_true", help="every record, not only those that can become cases"
    )
    listing.set_defaults(handler=_list)
    confirm = commands.add_parser("confirm", help="write the confirmed candidates into a key")
    confirm.add_argument("candidates", type=Path)
    confirm.add_argument("--confirmations", type=Path, required=True)
    confirm.add_argument("--out", type=Path, required=True, help="the new key file")
    confirm.add_argument("--base", type=Path, help="an existing key whose cases are kept")
    confirm.set_defaults(handler=_confirm)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        Refused,
        ConfirmationRefused,
        CandidatesFileError,
        KeyFormatError,
        FileExistsError,
        FileNotFoundError,
    ) as error:
        print(f"smriti: {error}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
