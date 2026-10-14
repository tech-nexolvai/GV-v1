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
        --version <v> --db <database> --store <artifact store root> [--name smriti] \\
        [--base ~/gv-local/<existing key>.json ...] [--library ~/gv-local/keys/library.json] \\
        [--vendor-file ~/gv-local/smriti/vendor-errors.jsonl]

The confirmations file (`smriti-confirmations/2`) is what the review page's "Copy answers" gives;
its format is in `eval/smriti/confirm.py`. Each key is `<out-dir>/<agent>-<name>-<version>.json`,
checked by its Pariksha loader and registered in the key library (`--library`, else the
`GV_KEY_LIBRARY` setting; there is no default). `--db` is the database the candidates came from:
each Yes is checked against the stored pictures of its question, a box against the rows its
picture numbers, and a piece kind's row widths are read from the stored row. `--store` is the
artifact store's root: each of those pictures is read there and its bytes checked against the
fingerprint the question recorded; a missing or different picture refuses the Yes. The confirmer
must be on the local confirmers list (`GV_SMRITI_CONFIRMERS`, apart from the reviewers list), never
an AI, an agent or code, and never the reviewer who decided the case. A conflict with a base key or
with any key already registered for that agent is refused before anything is written. Corrections
the person says the vendor drew wrong never enter a key: they are appended once each, with the
printed text the person read, to the local vendor-errors file. A damaged vendor-errors file is
refused cleanly before anything is written.

Candidates, confirmations, keys and the library hold client values (pages, what the reviewer
decided): keep them outside any repository. Every output (the keys folder and each key, the
library, the vendor-errors file) is checked before anything is written, and one at or inside any
git working tree is refused. A key is written once, never overwritten.

Exit codes: 0 done, 2 refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from pariksha_scorecard import REFUSED, Refused, _database_url, _outside_git, _scorer_version
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from eval.pariksha.keys import KeyFormatError
from eval.smriti.actors import ReviewersFileError, confirmers_from_environment
from eval.smriti.candidates import (
    AGENT_ORDER,
    Candidate,
    CandidateExport,
    CandidatesFileError,
    read_candidates,
    render_counts,
    write_candidates,
)
from eval.smriti.confirm import (
    ConfirmationRefused,
    Confirmed,
    VendorError,
    check_store,
    confirm_candidates,
    ready_candidates,
    stored_questions,
    stored_row_widths,
    who_confirms,
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

#: Each line of the local vendor-errors file (the later vendor report reads it).
VENDOR_ERRORS_FORMAT = "smriti-vendor-error/1"
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


def _outside_any_git(path: Path) -> Path:
    """The path, resolved, or a refusal when it is a git working tree's own top folder or lies
    anywhere inside one (this repository or any other)."""
    resolved = _outside_git(path)
    for folder in (resolved, *resolved.parents):
        if (folder / ".git").exists():
            raise Refused(
                f"{path} is inside a git working tree ({folder}); Smriti's keys, library and "
                "vendor errors hold client values, so write them outside any repository (for "
                "example under ~/gv-local/)"
            )
    return resolved


def default_vendor_file() -> Path:
    """Where vendor errors are kept for the vendor report: `~/gv-local/smriti/vendor-errors.jsonl`."""
    return Path.home() / "gv-local" / "smriti" / "vendor-errors.jsonl"


def _stored(
    url: str, export: CandidateExport, store_root: Path
) -> tuple[
    dict[str, list[str]], dict[str, int], dict[str, list[str | None]], dict[str, str | None]
]:
    """Read-only: each candidate's stored pictures and box count, each piece's row widths, and,
    for every picture a ready candidate's question showed, whether its bytes in the artifact store
    match the fingerprint the question recorded."""
    engine = create_engine(_database_url(url))
    try:
        with Session(engine) as session:
            try:
                session.execute(text("SET TRANSACTION READ ONLY"))
                questions = stored_questions(session, export.candidates)
                widths = stored_row_widths(session, export.candidates)
            finally:
                session.rollback()
    finally:
        engine.dispose()
    pictures = {cid: [sha for _n, sha, _k in q.pictures] for cid, q in questions.items()}
    boxes = {cid: q.boxes for cid, q in questions.items() if q.boxes is not None}
    ready = [candidate.candidate_id for candidate in ready_candidates(export)]
    return pictures, boxes, widths, check_store(store_root, questions, ready)


def _vendor_seen(path: Path) -> set[tuple[str, str]]:
    """The vendor errors already kept, by (candidate, candidates file), or a clean refusal when
    the file is damaged. Read before anything is written, so a damaged file writes nothing."""
    seen: set[tuple[str, str]] = set()
    if not path.exists():
        return seen
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise Refused(
            f"the vendor-errors file {path} cannot be read ({type(error).__name__}); nothing was "
            "written. Fix or move that local file, then confirm again"
        ) from error
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            known = json.loads(line)
        except json.JSONDecodeError:
            known = None
            why = "not JSON"
        else:
            why = (
                f"not a {VENDOR_ERRORS_FORMAT} line"
                if not isinstance(known, dict) or known.get("format") != VENDOR_ERRORS_FORMAT
                else ""
            )
        if why:
            raise Refused(
                f"the vendor-errors file {path} is damaged: line {number} is {why}; nothing was "
                "written. Fix or move that local file, then confirm again"
            )
        assert isinstance(known, dict)
        seen.add((str(known.get("candidate_id")), str(known.get("candidates_sha256"))))
    return seen


def _save_vendor_errors(
    errors: tuple[VendorError, ...],
    path: Path,
    *,
    seen: set[tuple[str, str]],
    database: str,
    candidates_sha256: str,
) -> int:
    """Append each vendor error once (one JSON line) to the local file the vendor report reads."""
    fresh = [error for error in errors if (error.candidate_id, candidates_sha256) not in seen]
    if not fresh:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    when = datetime.now().astimezone().isoformat()
    with path.open("a", encoding="utf-8") as handle:
        for error in fresh:
            record = {
                "format": VENDOR_ERRORS_FORMAT,
                "database": database,
                "candidates_sha256": candidates_sha256,
                **{name: value for name, value in asdict(error).items()},
                "saved_at": when,
            }
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return len(fresh)


def _write_keys(
    confirmed: Confirmed, out_dir: Path, *, library: Path, confirmed_by: str, source: str
) -> list[str]:
    """Check every key against the library first, then write each once and register it."""
    shelf = read_library(library, missing_ok=True)
    paths = {
        agent: _outside_any_git(out_dir / f"{agent}-{key.name}-{key.version}.json")
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
    # Every place this writes is checked first, so nothing is ever written inside git.
    out_dir = _outside_any_git(args.out_dir)
    library = _outside_any_git(library_path(args.library))
    vendor_file = _outside_any_git(
        args.vendor_file if args.vendor_file is not None else default_vendor_file()
    )
    if args.db is None:
        raise Refused(
            "give --db, the database the candidates came from: each Yes is checked against the "
            "stored pictures of its question, and a piece kind's row widths come from there"
        )
    if args.store is None:
        raise Refused(
            "give --store, the artifact store's root: each Yes is checked against the picture "
            "bytes kept there, not only against the database's list of pictures"
        )
    store_root = args.store.expanduser().resolve()
    if not store_root.is_dir():
        raise Refused(f"the artifact store {store_root} does not exist; give the store's root")
    confirmers = confirmers_from_environment()
    candidates_path = args.candidates.expanduser()
    export = read_candidates(candidates_path)
    try:
        confirmations = json.loads(args.confirmations.expanduser().read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise Refused(f"{args.confirmations.name} is not JSON: {error}") from error
    bases = [read_any_key(path.expanduser()) for path in args.base or ()]
    pictures, boxes, widths, store = _stored(args.db, export, store_root)
    candidates_sha256 = _candidates_sha256(candidates_path)
    confirmed = confirm_candidates(
        export,
        confirmations,
        candidates_sha256=candidates_sha256,
        confirmers=confirmers,
        pictures=pictures,
        store=store,
        boxes=boxes,
        row_widths=widths,
        bases=bases,
        name=args.name,
        version=args.version,
    )
    # A damaged vendor-errors file is refused before any key is written.
    seen = _vendor_seen(vendor_file) if confirmed.vendor else set()
    print(
        f"Smriti confirmations for {export.database}: {len(confirmed.joined)} answered Yes and "
        f"written, {len(confirmed.vendor)} vendor error(s) kept out of the AI's keys, "
        f"{len(confirmed.kept_out)} kept out"
    )
    for candidate_id, why in confirmed.kept_out:
        print(f"  kept out: {candidate_id}: {why}")
    confirmed_by = who_confirms(confirmations["confirmed_by"], confirmers)
    if confirmed.keys:
        source = f"smriti candidates of {export.database} exported {export.exported_at}"
        for line in _write_keys(
            confirmed, out_dir, library=library, confirmed_by=confirmed_by, source=source
        ):
            print(line)
    else:
        print("  no key written: nothing was answered Yes for the AI's keys")
    if confirmed.vendor:
        saved = _save_vendor_errors(
            confirmed.vendor,
            vendor_file,
            seen=seen,
            database=export.database,
            candidates_sha256=candidates_sha256,
        )
        print(
            f"  vendor drew it wrong (kept out of the AI's keys): {len(confirmed.vendor)}, "
            f"{saved} newly saved for the vendor report: {vendor_file}"
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
        "--db", help="the database the candidates came from (stored pictures, row widths)"
    )
    confirm.add_argument(
        "--store",
        type=Path,
        help="the artifact store's root: each Yes's picture bytes are checked there",
    )
    confirm.add_argument(
        "--base",
        type=Path,
        action="append",
        help="an existing key whose cases are kept (one per agent; may repeat)",
    )
    confirm.add_argument("--library", help="the key library (default: the GV_KEY_LIBRARY setting)")
    confirm.add_argument(
        "--vendor-file",
        type=Path,
        help="where vendor errors are kept (default ~/gv-local/smriti/vendor-errors.jsonl)",
    )
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
        ReviewersFileError,
        FileExistsError,
        FileNotFoundError,
    ) as error:
        print(f"smriti: {error}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
