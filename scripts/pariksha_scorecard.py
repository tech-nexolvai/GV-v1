#!/usr/bin/env python3
"""Pariksha: score a reading agent from a stored run, store the scorecard, compare two (#1219).

No model call, no cost: the stored answers of a run that already happened are read again
(read-only) and scored against a `pariksha-key/1` key (format: `eval/pariksha/__init__.py`).

    # score: Akshara (labels), Rekha (row choice) or Vastu (architect reader)
    python scripts/pariksha_scorecard.py score --agent akshara \\
        --db <database name or URL> --key ~/gv-local/<key>.json [--run <extraction run id>] \\
        --code-version <commit of the code that made the run> --out ~/gv-local/<card>.json

    # compare: exits 0 on ACCEPT (better, 0 new wrong), 1 on REJECT, 2 on a refusal
    python scripts/pariksha_scorecard.py compare <before.json> <after.json>

`--db` is a full SQLAlchemy URL, or a database name on the server `DATABASE_URL` points at. A
database holding several runs needs `--run`; the refusal lists them. Vastu also takes
`--document-sha256` when the run read more than one document.

`--code-version` is the stored run's commit, or `unrecorded` (flagged: not attributable).

    # score the phase-5 agents (#1257): view kind or depth and sink values, from an answers file
    python scripts/pariksha_scorecard.py score --agent akshara-depth \\
        --key ~/GV-v1/data/goldset/depth-key-ai-set-3/akshara-depth.json \\
        --answers ~/gv-local/<answers>.json --code-version <commit> --out ~/gv-local/<card>.json

`rekha-view` and `akshara-depth` read the readers' answers from a `pariksha-depth-answers/1` file
(format: `eval/pariksha/depth.py`), not from a database: reading a stored run waits for the reader
(part 3). Their summary and compare print the key's provenance ("key read by Claude twice, not by a
person") and the key's values per status and field. Their compare is `eval/pariksha/depth.py`'s:
an expected hold counts as right, and a value given where the key holds is agreed-wrong.

`--with-try-again` (#1309, Akshara and Rekha): judge each held or split case on the last try the run
asked of it (the reading's try-again, `GV_TRY_AGAIN_GATES`). Compare such a scorecard with one made
without it, of the same run, to see what trying again changed: held -> agreed-right is fixed,
held -> agreed-wrong is a new wrong answer and rejects.

`--smriti <candidates file>` (from `scripts/smriti_candidates.py export`, #1232) also prints how many
reviewer decisions concern this agent; printed only, never stored in the scorecard.

Keys and scorecards hold client values (what the readers read): keep both outside any repository.
An `--out` inside a git working tree is refused. Exit codes: 0 ACCEPT, 1 REJECT, 2 refused.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from eval.pariksha.compare import Comparison, compare_scorecards
from eval.pariksha.depth import (
    DEPTH_AGENTS,
    KEY_PROVENANCE,
    compare_depth,
    describe_key,
    read_depth_answers,
    read_depth_key,
    score_depth,
)
from eval.pariksha.keys import AnswerKey, ArchitectAnswerKey, KeyFormatError, read_key
from eval.pariksha.score import UNRECORDED, Scorecard, score_akshara, score_rekha, score_vastu
from eval.pariksha.scorecard import ScorecardError, read_scorecard, write_scorecard
from eval.pariksha.stored import StoredRunError, load_architect_run, load_stored_run
from eval.regression import IncomparableRuns
from eval.runs import MissingProvenanceError
from eval.smriti.candidates import CandidatesFileError, agent_line, read_candidates

#: Exit codes: ACCEPT 0, REJECT 1, a refusal (tampered file, other agent or key, unreadable
#: answers, bad provenance, an output path inside a git working tree) 2.
REFUSED = 2


def _database_url(value: str) -> URL:
    if "://" in value:
        return make_url(value)
    base = os.environ.get("DATABASE_URL") or os.environ.get("GV_DATABASE_URL")
    if not base:
        raise SystemExit(
            f"--db {value!r} is a database name, and neither DATABASE_URL nor GV_DATABASE_URL "
            "says which server it is on; give a full URL instead"
        )
    return make_url(base).set(database=value)


def _scorer_version() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--short=12", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


class Refused(Exception):
    """The script will not do what was asked; the message says why. Exit code 2."""


def _inside_git_working_tree(path: Path) -> bool:
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if existing.is_file():
        existing = existing.parent
    try:
        answer = subprocess.run(
            ["git", "-C", str(existing), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return answer.returncode == 0 and answer.stdout.strip() == "true"


def _outside_git(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == REPO or REPO in resolved.parents or _inside_git_working_tree(resolved.parent):
        raise Refused(
            f"{path} is inside a git working tree; a scorecard holds what the readers read on "
            "client drawings, so write it outside any repository (for example under ~/gv-local/)"
        )
    return resolved


def summary(card: Scorecard) -> str:
    lines = [
        f"Pariksha scorecard: {card.agent} on key {card.key_name} {card.key_version}",
        (
            f"  run {card.source.get('database')}/{card.source.get('extraction_run_id')}, "
            f"code {card.code_version}"
            + (" (NOT ATTRIBUTABLE)" if card.code_version == UNRECORDED else "")
            + f", prompts {card.extractor_versions.get('prompt_ids')}, "
            f"models {card.extractor_versions.get('model_ids')}"
        ),
        f"  reader settings: {card.extractor_versions.get('reader_settings', 'none (code only)')}",
    ]
    for name, metric in card.metrics.items():
        shown = (
            "not measured"
            if metric.value is None
            else f"{metric.numerator}/{metric.denominator}"
            + (
                f" = {float(metric.value):.0f}"
                if name.startswith("cost")
                else f" ({float(metric.value) * 100:.1f}%)"
            )
        )
        lines.append(f"  {name}: {shown}")
    return "\n".join(lines)


def _score_depth(args: argparse.Namespace) -> int:
    """`rekha-view` and `akshara-depth`: the readers' answers come from a file (#1257)."""
    if args.answers is None or args.db is not None or args.run is not None:
        raise Refused(
            f"{args.agent} is scored from --answers (a pariksha-depth-answers/1 file), not --db or "
            "--run: reading its answers from a stored run waits for the reader"
        )
    if args.smriti is not None or args.document_sha256 is not None:
        raise Refused(f"--smriti and --document-sha256 do not apply to {args.agent}")
    out = _outside_git(args.out)
    key = read_depth_key(args.key.expanduser())
    if key.agent != args.agent:
        raise Refused(f"{args.key} is not a key for {args.agent}")
    answers = read_depth_answers(args.answers.expanduser())
    card = score_depth(
        key, answers, code_version=args.code_version, scorer_version=_scorer_version()
    )
    write_scorecard(card, out)
    print(summary(card))
    print(describe_key(key))
    print(f"  written: {out}")
    return 0


def _score(args: argparse.Namespace) -> int:
    if args.with_try_again and args.agent not in ("akshara", "rekha"):
        raise Refused(f"--with-try-again applies to akshara and rekha, not {args.agent}")
    if args.agent in DEPTH_AGENTS:
        return _score_depth(args)
    if args.db is None or args.answers is not None:
        raise Refused(f"{args.agent} is scored from a stored run: give --db, not --answers")
    out = _outside_git(args.out)
    # Read before scoring, so a refused candidates file never leaves a scorecard behind.
    decisions = None if args.smriti is None else read_candidates(args.smriti.expanduser())
    key = read_key(args.key.expanduser())
    expected = ArchitectAnswerKey if args.agent == "vastu" else AnswerKey
    if not isinstance(key, expected) or (isinstance(key, AnswerKey) and key.agent != args.agent):
        raise Refused(f"{args.key} is not a key for {args.agent}")
    url = _database_url(args.db)
    engine = create_engine(url)
    database = url.database or "unknown"
    try:
        with Session(engine) as session:
            try:
                card: Scorecard
                if isinstance(key, ArchitectAnswerKey):
                    stored_spans = load_architect_run(
                        session,
                        database=database,
                        extraction_run_id=args.run,
                        document_sha256=args.document_sha256 or key.document_sha256,
                    )
                    card = score_vastu(
                        key,
                        stored_spans,
                        code_version=args.code_version,
                        scorer_version=_scorer_version(),
                    )
                else:
                    stored = load_stored_run(
                        session,
                        agent=args.agent,
                        database=database,
                        extraction_run_id=args.run,
                        with_try_again=args.with_try_again,
                    )
                    scorer = score_akshara if args.agent == "akshara" else score_rekha
                    card = scorer(
                        key,
                        stored,
                        code_version=args.code_version,
                        scorer_version=_scorer_version(),
                        with_try_again=args.with_try_again,
                    )
            finally:
                session.rollback()
    finally:
        engine.dispose()
    write_scorecard(card, out)
    print(summary(card))
    if decisions is not None:
        # Printed beside the scorecard, never stored in it: the decisions come from another file.
        print(agent_line(decisions, args.agent))
    print(f"  written: {out}")
    return 0


#: Scorecards with their own compare (#1250): this one does not know their extra reasons to reject
#: (new wrong wall ink, a new wrong automatic layout, an unverified row), so it would accept a change
#: their own compare rejects. It refuses them and names the command to use.
OWN_COMPARE = {
    "rekha-walls": "scripts/pariksha_walls.py compare",
    "bala-kinds": "scripts/pariksha_kinds.py compare",
}


def _compare(args: argparse.Namespace) -> int:
    before = read_scorecard(args.before.expanduser())
    after = read_scorecard(args.after.expanduser())
    for card in (before, after):
        if card.agent in OWN_COMPARE:
            raise Refused(
                f"a {card.agent} scorecard has its own compare, which knows its rules: use "
                f"{OWN_COMPARE[card.agent]} (before: {before.agent}, after: {after.agent})"
            )
    comparison: Comparison
    if before.agent in DEPTH_AGENTS or after.agent in DEPTH_AGENTS:
        # Refuses a card of any other agent, and two cards of different agents.
        comparison = compare_depth(before, after)
        print(f"key: {KEY_PROVENANCE}")
    else:
        comparison = compare_scorecards(before, after)
    print(comparison.render())
    return 0 if comparison.accepted else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    score = commands.add_parser("score", help="score a stored run into a new scorecard file")
    score.add_argument(
        "--agent", choices=("akshara", "rekha", "vastu", *DEPTH_AGENTS), required=True
    )
    score.add_argument(
        "--db", help="the stored run's database: URL or name (akshara, rekha, vastu)"
    )
    score.add_argument(
        "--answers",
        type=Path,
        help="rekha-view, akshara-depth: the readers' answers (pariksha-depth-answers/1)",
    )
    score.add_argument("--key", type=Path, required=True)
    score.add_argument("--run", type=UUID, help="the extraction run, when the database has several")
    score.add_argument("--document-sha256", help="Vastu: the document, when the run read several")
    score.add_argument(
        "--code-version", required=True, help="the commit of the code that made the stored run"
    )
    score.add_argument("--out", type=Path, required=True, help="the new scorecard file")
    score.add_argument(
        "--with-try-again",
        action="store_true",
        help="akshara, rekha: judge each held case on the last try the run asked of it (#1309)",
    )
    score.add_argument(
        "--smriti",
        type=Path,
        help="a Smriti candidates file: also print the reviewer decisions about this agent",
    )
    score.set_defaults(handler=_score)
    compare = commands.add_parser("compare", help="compare two scorecards: before, after")
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
        CandidatesFileError,
    ) as error:
        # A refusal is not a verdict: 0 is ACCEPT, 1 is REJECT, 2 is "will not judge this".
        print(f"pariksha: {error}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
