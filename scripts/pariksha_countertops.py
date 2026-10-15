#!/usr/bin/env python3
"""The countertop-level scorecard, the honest metric, from a stored run (#1321).

No model call, no cost. For each keyed countertop: did the app decide it by itself (a PASS or FAIL
before any person acted), and was that decision right by the keys? The rules are in
`eval/pariksha/countertops.py`.

    python scripts/pariksha_countertops.py --db <database name or URL> [--revision <id>] \\
        (--library ~/gv-local/keys/library.json | --key <rows or number key>...) \\
        --row-boxes ~/gv-local/keys/<box map>.json [--row-boxes ...] \\
        --out ~/gv-local/<folder>/countertops-<database>.json

`--library` takes every Rekha rows key and every Akshara number key on the list; `--key` (repeatable)
takes the named keys instead. The database is only read. The scorecard names client pages by their
hashes: it is written once, never inside a git working tree.

Exit codes: 0 done, 2 refused.

Source: issue #1321 · Verification: `tests/scripts/test_pariksha_countertops.py`
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pariksha_scorecard import REFUSED, Refused, _database_url, _outside_git
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.document import Page
from eval.pariksha.countertops import (
    card_document,
    load_countertop_findings,
    render_countertops,
    score_countertops,
    stored_row_pages,
)
from eval.pariksha.keys import AnswerKey, KeyFormatError, read_key
from eval.pariksha.row_geometry import RowBoxesError, read_row_boxes
from eval.smriti.library import LibraryError, library_path, load_entry_key, read_library


def _keys(args: argparse.Namespace) -> list[AnswerKey]:
    if args.library is not None and args.key:
        raise Refused("give --library or --key, not both")
    loaded: list[object]
    if args.key:
        loaded = [read_key(path.expanduser()) for path in args.key]
    else:
        library = read_library(library_path(args.library))
        loaded = [
            load_entry_key(entry)
            for entry in library.entries
            if entry.agent in ("rekha", "akshara")
        ]
    keys = [key for key in loaded if isinstance(key, AnswerKey)]
    if len(keys) != len(loaded):
        raise Refused("only Rekha rows keys and Akshara number keys are used here")
    return keys


def _main(args: argparse.Namespace) -> int:
    out = _outside_git(args.out)
    if out.exists():
        raise Refused(f"{out} already exists: a scorecard is written once")
    keys = _keys(args)
    rows = [key for key in keys if key.agent == "rekha"]
    if not rows:
        raise Refused("no Rekha rows key given: nothing names a countertop")
    row_boxes = read_row_boxes(args.row_boxes) if args.row_boxes else None
    engine = create_engine(_database_url(args.db))
    try:
        with Session(engine) as session:
            try:
                findings = load_countertop_findings(session, package_revision_id=args.revision)
                read_pages = stored_row_pages(session)
                names = {
                    content_hash: f"page {index + 1}"
                    for index, content_hash in session.execute(
                        select(Page.index, Page.content_hash)
                    )
                }
            except ValueError as error:
                raise Refused(str(error)) from error
            finally:
                session.rollback()
    finally:
        engine.dispose()
    card = score_countertops(
        rows,
        findings,
        row_boxes=row_boxes,
        label_keys=[key for key in keys if key.agent == "akshara"],
        read_pages=read_pages,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x") as handle:
        handle.write(json.dumps(card_document(card), indent=1) + "\n")
    print(f"countertops of {args.db}: {len(findings)} countertop row(s) with a width finding")
    print(render_countertops(card, names))
    print(f"  written: {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--db", required=True, help="the stored run's database: URL or name")
    parser.add_argument(
        "--revision", type=UUID, help="the package revision, when there are several"
    )
    parser.add_argument("--library", type=Path, help="the key list (or set GV_KEY_LIBRARY)")
    parser.add_argument("--key", type=Path, action="append", help="a key file (repeatable)")
    parser.add_argument("--row-boxes", type=Path, action="append", help="a box map (repeatable)")
    parser.add_argument("--out", type=Path, required=True, help="the new scorecard file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _main(args)
    except (
        Refused,
        KeyFormatError,
        LibraryError,
        RowBoxesError,
        FileExistsError,
        ValueError,
    ) as error:
        print(f"pariksha countertops: {error}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
