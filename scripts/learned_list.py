#!/usr/bin/env python3
"""GV's learned list: what the reviewer's Agree / Not a problem clicks propose, and the approvals
(plan "Why not all 50", Phase 4, M13c; `app/review/assistant/learned.py` says what each file is).

No model call, no cost. The database is only read.

    # count the decisions and write proposals.json + examples.json (run on a slow cadence)
    python scripts/learned_list.py mine --db <database name or URL> [--dir ~/gv-local/learned]

    # Anant approves a proposed type (or takes an approval back)
    python scripts/learned_list.py approve "D: metal finish tag missing" --by <name> [--note ...]
    python scripts/learned_list.py revoke "D: metal finish tag missing" --by <name>

    # what is proposed, approved and protected now
    python scripts/learned_list.py show [--dir ~/gv-local/learned]

`--dir` defaults to the `GV_ASSISTANT_LEARNED_DIR` setting, the same folder the app reads. It holds
GV's words about client drawings, so a folder inside any git working tree is refused. Only
decisions by names on the local real-reviewer list (`GV_SMRITI_REVIEWERS`) are counted, and only a
name on the local confirmers list (`GV_SMRITI_CONFIRMERS`) may approve or revoke: the same lists
Smriti uses (decided 2026-10-15). A proposal changes nothing until it is approved.

Exit codes: 0 done, 2 refused.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from pariksha_scorecard import REFUSED, Refused, _database_url, _outside_git
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.review.assistant import learned
from eval.smriti.actors import (
    REVIEWER,
    ReviewersFileError,
    confirmers_from_environment,
    reviewers_from_environment,
)


def _folder(value: str | None) -> Path:
    raw = value or os.environ.get(learned.ENV_DIR, "").strip()
    if not raw:
        raise Refused(f"give --dir, or set {learned.ENV_DIR} to a folder under ~/gv-local")
    folder = _outside_git(Path(raw))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _write(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load(path: Path, fmt: str, empty: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return dict(empty)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("format") != fmt:
        raise Refused(f"{path} is not {fmt}; it was left as it is")
    return raw


def _events(folder: Path) -> list[dict[str, Any]]:
    raw = _load(
        folder / learned.APPROVED_FILE,
        learned.APPROVED_FORMAT,
        {"format": learned.APPROVED_FORMAT, "events": []},
    )
    return [e for e in raw.get("events") or [] if isinstance(e, dict)]


def mine(args: argparse.Namespace) -> int:
    folder = _folder(args.dir)
    reviewers = reviewers_from_environment()
    if reviewers.names is None:
        print(
            "No real-reviewer list (GV_SMRITI_REVIEWERS): every decision is shown as not counted, "
            "so nothing can be proposed."
        )
    never = learned.load_never()
    engine = create_engine(_database_url(args.db))
    try:
        with Session(engine) as session:
            try:
                session.execute(text("SET TRANSACTION READ ONLY"))
                cards = learned.load_cards(session)
            finally:
                session.rollback()
    finally:
        engine.dispose()
    approved = learned.approved_types(_events(folder))
    proposals, examples = learned.files_of(
        cards, never, lambda by: reviewers.status(by) == REVIEWER, approved
    )
    _write(folder / learned.PROPOSALS_FILE, proposals)
    _write(folder / learned.EXAMPLES_FILE, examples)
    proposed = [row for row in proposals["types"] if row["status"] == "proposed"]
    print(
        f"{proposals['decisions_counted']} decisions counted, "
        f"{proposals['decisions_not_counted']} not counted (not on the real-reviewer list); "
        f"{len(proposed)} type(s) proposed, {len(examples['examples'])} past wording(s) kept."
    )
    for row in proposed:
        print(f"  proposed: {row['type']} (dismissed {row['dismissed']}, accepted 0)")
    for key in proposals["stale_approvals"]:
        print(f"  approved but accepted since: {key} (look at it; revoke if it should come back)")
    return 0


def decide(args: argparse.Namespace, action: str) -> int:
    folder = _folder(args.dir)
    confirmers = confirmers_from_environment()
    if not confirmers.knows(args.by):
        raise Refused(
            f"{args.by!r} is not on the local confirmers list (GV_SMRITI_CONFIRMERS); only Anant "
            "approves the learned list"
        )
    proposals = _load(folder / learned.PROPOSALS_FILE, learned.PROPOSALS_FORMAT, {"types": []})
    try:
        events = learned.approve(
            _events(folder),
            args.type,
            by=args.by,
            proposed=proposals.get("types") or [],
            never=learned.load_never(),
            action=action,
            note=getattr(args, "note", "") or "",
        )
    except learned.LearnedRefused as refused:
        raise Refused(str(refused)) from None
    _write(folder / learned.APPROVED_FILE, {"format": learned.APPROVED_FORMAT, "events": events})
    print(f"{action}d: {args.type}")
    return 0


def show(args: argparse.Namespace) -> int:
    folder = _folder(args.dir)
    proposals = _load(folder / learned.PROPOSALS_FILE, learned.PROPOSALS_FORMAT, {"types": []})
    approved = learned.approved_types(_events(folder))
    print(f"Approved (applied): {', '.join(sorted(approved)) or 'none'}")
    for row in proposals.get("types") or []:
        if row.get("status") in ("proposed", "protected"):
            extra = f" - never: {row['never_suppress']}" if row.get("never_suppress") else ""
            print(
                f"  {row['status']}: {row['type']} (dismissed {row['dismissed']}, accepted "
                f"{row['accepted']}){extra}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("mine", help="count the decisions; write proposals and past wordings")
    one.add_argument("--db", required=True)
    one.add_argument("--dir")
    for name in ("approve", "revoke"):
        each = sub.add_parser(name, help=f"{name} one type (Anant only)")
        each.add_argument("type")
        each.add_argument("--by", required=True)
        each.add_argument("--note", default="")
        each.add_argument("--dir")
    listing = sub.add_parser("show", help="what is proposed, approved and protected")
    listing.add_argument("--dir")
    args = parser.parse_args(argv)
    try:
        if args.command == "mine":
            return mine(args)
        if args.command in ("approve", "revoke"):
            return decide(args, args.command)
        return show(args)
    except (Refused, ReviewersFileError, learned.LearnedRefused) as refused:
        print(f"Refused: {refused}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
