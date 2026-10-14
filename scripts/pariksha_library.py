#!/usr/bin/env python3
"""Pariksha over the key library: one local list of every answer key, and a change judged by all (#1268).

No model call, no cost. The list (`eval/smriti/library.py`) registers key files where they are;
`compare` scores a change's BEFORE and AFTER stored runs (read-only) against every registered key
of an agent, with Pariksha's own loaders, scorers and compares (`eval/smriti/library_compare.py`).

    # register a key: its agent, kind, name, version, hash and case count come from the key itself
    python scripts/pariksha_library.py register --library ~/gv-local/keys/library.json \\
        --key ~/gv-local/<key>.json --confirmed-by "<who keyed or confirmed it>" \\
        --label "<how far to trust it, e.g. not human-confirmed>" [--source "<where from>"]

    python scripts/pariksha_library.py list --library ~/gv-local/keys/library.json

    # judge a change: exits 0 ACCEPT, 1 REJECT, 2 refused, 3 NOT MEASURED (nothing measured)
    python scripts/pariksha_library.py compare --library ~/gv-local/keys/library.json \\
        --agent rekha \\
        --before-db <name or URL> [--before-run [AGENT=]<run id>]... --before-code <commit> \\
        --after-db <name or URL> [--after-run [AGENT=]<run id>]... --after-code <commit> \\
        [--kind-source product|face-tag] [--cards ~/gv-local/<folder>] [--detail]

`--library` may instead come from the `GV_KEY_LIBRARY` setting; there is no default, and a list
inside a git working tree is refused. `--agent rekha` takes every Rekha key (rows, walls, views);
`--agent rekha-walls` only the walls keys. `--before-run` / `--after-run` name the extraction run to
read: a bare id for every key, or `AGENT=<id>` for one key agent (the pieces of a run often sit in
another extraction run than its reader questions). Without one, a database with a single run of
that kind is read, and one with several is refused with the list. `--db` names work as in
`scripts/pariksha_scorecard.py` (a full URL, or a name on the server `DATABASE_URL` points at).

Every verdict names, per key, who confirmed it and its label, and lists every key and page that was
NOT MEASURED (never counted as a pass). `--cards` also writes each scorecard (outside git). The
script prints the verdict and writes no setting, prompt id or environment value: switching stays
a person's decision.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pariksha_scorecard import REFUSED, _database_url, _scorer_version
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from eval.pariksha.kinds import PRODUCT, SOURCES
from eval.pariksha.scorecard import ScorecardError, write_scorecard
from eval.pariksha.stored import StoredRunError
from eval.regression import IncomparableRuns
from eval.runs import MissingProvenanceError
from eval.smriti.library import (
    KEY_KINDS,
    LibraryEntry,
    LibraryError,
    describe,
    key_pages,
    library_path,
    load_entry_key,
    outside_git,
    read_library,
    register,
    write_library,
)
from eval.smriti.library_compare import (
    KeyResult,
    Side,
    StoredRunRef,
    judge_key,
    judge_library,
    measure,
    unmeasurable,
)

#: Who a key can be for: a key agent, or a person-agent naming all of theirs (`rekha` = rows,
#: walls and views).
AGENTS = tuple(sorted(set(KEY_KINDS) | {agent.split("-", 1)[0] for agent in KEY_KINDS}))
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _register(args: argparse.Namespace) -> int:
    path = library_path(args.library)
    outside_git(path)
    library = read_library(path, missing_ok=True)
    entry, key = describe(
        args.key, confirmed_by=args.confirmed_by, label=args.label, source=args.source or ""
    )
    updated = register(library, entry, key)
    if updated is library:
        print(f"already registered: {entry}")
        return 0
    write_library(updated)
    print(
        f"registered: {entry}, {entry.cases} case(s); confirmed by {entry.confirmed_by}; {entry.label}"
    )
    print(f"  library: {updated.path} ({len(updated.entries)} key(s))")
    return 0


def _list(args: argparse.Namespace) -> int:
    library = read_library(library_path(args.library))
    print(f"Key library {library.path}: {len(library.entries)} key(s)")
    for entry in library.entries:
        print(f"  {entry}, {entry.cases} case(s)")
        print(f"    confirmed by {entry.confirmed_by}; {entry.label}")
        print(f"    {entry.path}" + (f" (from {entry.source})" if entry.source else ""))
    return 0


def _runs(values: Sequence[str] | None) -> dict[str, UUID]:
    runs: dict[str, UUID] = {}
    for value in values or ():
        agent, _, run = value.rpartition("=")
        if agent and agent not in KEY_KINDS:
            raise LibraryError(
                f"--*-run {value!r}: {agent!r} is not a key agent {tuple(KEY_KINDS)}"
            )
        if agent in runs:
            raise LibraryError(f"--*-run names two runs for {agent or 'every agent'}")
        try:
            runs[agent] = UUID(run)
        except ValueError as error:
            raise LibraryError(f"--*-run {value!r}: {run!r} is not a run id") from error
    return runs


def _side(
    args: argparse.Namespace, entry: LibraryEntry, ref: StoredRunRef, session: Session
) -> Side:
    return measure(
        session,
        entry,
        load_entry_key(entry),
        ref,
        kind_source=args.kind_source,
        scorer_version=_scorer_version(),
    )


def _write_cards(folder: Path, label: str, entry: LibraryEntry, side: Side) -> None:
    if side.card is None:
        return
    name = _SAFE.sub("-", f"{label}-{entry.agent}-{entry.name}-{entry.version}-{entry.sha256[:12]}")
    write_scorecard(side.card, folder / f"{name}.json")


def _compare(args: argparse.Namespace) -> int:
    library = read_library(library_path(args.library))
    cards = None if args.cards is None else outside_git(args.cards, "a scorecard folder")
    entries = library.for_agent(args.agent)
    refs = {
        "before": StoredRunRef(
            database=args.before_db, code_version=args.before_code, runs=_runs(args.before_run)
        ),
        "after": StoredRunRef(
            database=args.after_db, code_version=args.after_code, runs=_runs(args.after_run)
        ),
    }
    engines = {label: create_engine(_database_url(ref.database)) for label, ref in refs.items()}
    results: list[KeyResult] = []
    try:
        for entry in entries:
            pages = key_pages(load_entry_key(entry))
            why_not = unmeasurable(entry)
            if why_not:
                empty = Side(card=None, pages=frozenset())
                results.append(judge_key(entry, pages, empty, empty, unmeasurable=why_not))
                continue
            sides: dict[str, Side] = {}
            for label, ref in refs.items():
                with Session(engines[label]) as session:
                    named = StoredRunRef(
                        database=engines[label].url.database or "unknown",
                        code_version=ref.code_version,
                        runs=ref.runs,
                    )
                    sides[label] = _side(args, entry, named, session)
                if cards is not None:
                    _write_cards(cards, label, entry, sides[label])
            results.append(judge_key(entry, pages, sides["before"], sides["after"]))
    finally:
        for engine in engines.values():
            engine.dispose()
    verdict = judge_library(args.agent, results)
    print(f"library: {library.path}")
    for label, ref in refs.items():
        runs = (
            ", ".join(f"{agent or 'all'}={run}" for agent, run in ref.runs.items())
            or "the only run"
        )
        print(f"{label}: {engines[label].url.database}, code {ref.code_version}, runs {runs}")
    if any(entry.agent == "bala-kinds" for entry in entries):
        print(f"piece kinds from: {args.kind_source}")
    print(verdict.render(detail=args.detail))
    return verdict.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("register", help="register a key file on the local list")
    add.add_argument("--library", type=Path, help="the list (or set GV_KEY_LIBRARY)")
    add.add_argument("--key", type=Path, required=True)
    add.add_argument("--confirmed-by", required=True, help="who keyed or confirmed the key")
    add.add_argument("--label", required=True, help="how far to trust it, printed with verdicts")
    add.add_argument("--source", help="where the key came from")
    add.set_defaults(handler=_register)

    show = commands.add_parser("list", help="print the registered keys")
    show.add_argument("--library", type=Path, help="the list (or set GV_KEY_LIBRARY)")
    show.set_defaults(handler=_list)

    judge = commands.add_parser("compare", help="judge a change by every key of an agent")
    judge.add_argument("--library", type=Path, help="the list (or set GV_KEY_LIBRARY)")
    judge.add_argument("--agent", choices=AGENTS, required=True)
    for side in ("before", "after"):
        judge.add_argument(f"--{side}-db", required=True, help=f"the {side} run's database")
        judge.add_argument(
            f"--{side}-run",
            action="append",
            help="the extraction run: an id, or AGENT=id for one key agent (repeatable)",
        )
        judge.add_argument(
            f"--{side}-code", required=True, help=f"the commit of the code that made the {side} run"
        )
    judge.add_argument("--kind-source", choices=SOURCES, default=PRODUCT)
    judge.add_argument("--cards", type=Path, help="also write every scorecard into this folder")
    judge.add_argument("--detail", action="store_true", help="print each key's full comparison")
    judge.set_defaults(handler=_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        LibraryError,
        StoredRunError,
        ScorecardError,
        IncomparableRuns,
        MissingProvenanceError,
        FileExistsError,
    ) as error:
        # A refusal is not a verdict: 0 ACCEPT, 1 REJECT, 3 NOT MEASURED, 2 "will not judge this".
        print(f"pariksha library: {error}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
