#!/usr/bin/env python3
"""Smriti: list reviewer decisions as candidate Pariksha key cases; a person confirms them (#1232, #1271).

No model call, no cost, nothing changed: the database is only read (read-only transaction).

    # export every reviewer decision of a database as candidates (all need a person's confirmation)
    python scripts/smriti_candidates.py export --db <database name or URL> \\
        --out ~/gv-local/<candidates>.json

    # list them for a person, per agent
    python scripts/smriti_candidates.py list ~/gv-local/<candidates>.json [--agent rekha] [--all]

    # build the local Yes/No page for a person (scripts/smriti_review_page.py), then write what
    # they answered Yes into one new local key per agent, registered in the key library
    python scripts/smriti_candidates.py confirm ~/gv-local/<candidates>.json \\
        --confirmations ~/gv-local/<confirmations>.json --out-dir ~/gv-local/keys/smriti/ \\
        --version <v> [--name smriti] [--db <database>] [--base ~/gv-local/<existing key>.json ...] \\
        [--library ~/gv-local/keys/library.json]

The confirmations file (`smriti-confirmations/2`) is what the review page's "Copy answers" gives;
its format is in `eval/smriti/confirm.py`. Each key is `<out-dir>/<agent>-<name>-<version>.json`,
checked by its Pariksha loader and registered in the key library (`--library`, else the
`GV_KEY_LIBRARY` setting; there is no default). A piece kind's row widths are read from the stored
row, so `--db` is needed when one is confirmed. A conflict with a base key or with any key already
registered for that agent is refused before anything is written.

Candidates, confirmations, keys and the library hold client values (pages, what the reviewer
decided): keep them outside any repository. An output inside a git working tree is refused. A file
is written once, never overwritten.

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
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from eval.pariksha.keys import KeyFormatError
from eval.smriti.candidates import (
    AGENT_ORDER,
    Candidate,
    CandidatesFileError,
    read_candidates,
    render_counts,
    write_candidates,
)
from eval.smriti.confirm import (
    ConfirmationRefused,
    Confirmed,
    confirm_candidates,
    stored_row_widths,
)
from eval.smriti.export import export_candidates
from eval.smriti.library import (
    KEY_KINDS,
    LibraryEntry,
    LibraryError,
    describe,
    library_path,
    read_any_key,
    read_library,
    register,
    write_library,
)

__all__ = ["CONFIRMED_LABEL", "REFUSED", "main"]

#: How far to trust a key Smriti writes, printed beside every verdict it takes part in.
CONFIRMED_LABEL = "a person confirmed each case against the picture the AI saw (Smriti review page)"


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


def _candidates_sha256(path: Path) -> str:
    """The candidates file's own hash (written in it, checked by `read_candidates`)."""
    stored = json.loads(path.read_text(encoding="utf-8")).get("sha256")
    if not isinstance(stored, str):
        raise Refused(f"{path.name} carries no hash")
    return stored


def _row_widths(url: str, candidates: tuple[Candidate, ...]) -> dict[str, list[str | None]]:
    engine = create_engine(_database_url(url))
    try:
        with Session(engine) as session:
            try:
                session.execute(text("SET TRANSACTION READ ONLY"))
                return stored_row_widths(session, candidates)
            finally:
                session.rollback()
    finally:
        engine.dispose()


def _write_keys(
    confirmed: Confirmed, out_dir: Path, *, library: Path, confirmed_by: str, source: str
) -> list[str]:
    """Check every key against the library first, then write each once and register it."""
    shelf = read_library(library, missing_ok=True)
    paths = {
        agent: out_dir / f"{agent}-{key.name}-{key.version}.json"
        for agent, key in confirmed.keys.items()
    }
    for agent, path in paths.items():
        if path.exists():
            raise FileExistsError(f"{path} already exists; a key is written once (new --version)")
    # A conflict with any registered key of the agent is refused before anything is written.
    dry = shelf
    for agent, key in confirmed.keys.items():
        entry = LibraryEntry(
            agent=agent,
            kind=KEY_KINDS[agent],
            path=str(paths[agent]),
            name=key.name,
            version=key.version,
            sha256=key.sha256,
            cases=len(key.cases),
            confirmed_by=confirmed_by,
            label=CONFIRMED_LABEL,
            source=source,
        )
        dry = register(dry, entry, key)
    out_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for agent, path in paths.items():
        with path.open("x", encoding="utf-8") as handle:
            json.dump(confirmed.documents[agent], handle, indent=1, ensure_ascii=False)
            handle.write("\n")
        entry, written = describe(
            path, confirmed_by=confirmed_by, label=CONFIRMED_LABEL, source=source
        )
        shelf = register(shelf, entry, written)
        lines.append(
            f"  key {entry.name} {entry.version} for {agent}: {entry.cases} case(s), sha256 "
            f"{entry.sha256[:12]}; written: {path}"
        )
    write_library(shelf)
    lines.append(f"  registered in the key library: {shelf.path}")
    return lines


def _confirm(args: argparse.Namespace) -> int:
    out_dir = _outside_git(args.out_dir)
    library = _outside_git(library_path(args.library))
    candidates_path = args.candidates.expanduser()
    export = read_candidates(candidates_path)
    try:
        confirmations = json.loads(args.confirmations.expanduser().read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise Refused(f"{args.confirmations.name} is not JSON: {error}") from error
    bases = [read_any_key(path.expanduser()) for path in args.base or ()]
    widths = {} if args.db is None else _row_widths(args.db, export.candidates)
    confirmed = confirm_candidates(
        export,
        confirmations,
        candidates_sha256=_candidates_sha256(candidates_path),
        row_widths=widths,
        bases=bases,
        name=args.name,
        version=args.version,
    )
    print(
        f"Smriti confirmations for {export.database}: {len(confirmed.joined)} answered Yes and "
        f"written, {len(confirmed.vendor)} vendor error(s) kept out of the AI's keys, "
        f"{len(confirmed.kept_out)} kept out"
    )
    for candidate_id in confirmed.vendor:
        print(f"  vendor drew it wrong (for the vendor report, not the AI's key): {candidate_id}")
    for candidate_id, why in confirmed.kept_out:
        print(f"  kept out: {candidate_id}: {why}")
    if not confirmed.keys:
        print("  no key written: nothing was answered Yes")
        return 0
    source = f"smriti candidates of {export.database} exported {export.exported_at}"
    confirmed_by = str(confirmations["confirmed_by"]).strip()
    for line in _write_keys(
        confirmed, out_dir, library=library, confirmed_by=confirmed_by, source=source
    ):
        print(line)
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
    confirm = commands.add_parser(
        "confirm", help="write what a person answered Yes into one key per agent, registered"
    )
    confirm.add_argument("candidates", type=Path)
    confirm.add_argument(
        "--confirmations", type=Path, required=True, help="the review page's copied answers"
    )
    confirm.add_argument("--out-dir", type=Path, required=True, help="where the new keys go")
    confirm.add_argument("--version", required=True, help="the new keys' version")
    confirm.add_argument("--name", default="smriti", help="the new keys' name (default smriti)")
    confirm.add_argument(
        "--db", help="the database the candidates came from: a piece kind's stored row widths"
    )
    confirm.add_argument(
        "--base",
        type=Path,
        action="append",
        help="an existing key whose cases are kept (one per agent; may repeat)",
    )
    confirm.add_argument("--library", help="the key library (default: the GV_KEY_LIBRARY setting)")
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
        LibraryError,
        FileExistsError,
        FileNotFoundError,
    ) as error:
        print(f"smriti: {error}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
