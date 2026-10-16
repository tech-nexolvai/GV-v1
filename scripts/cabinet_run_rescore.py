#!/usr/bin/env python3
"""Score the proposed cabinet runs on stored readings against the piece-kind keys ($0, read-only).

No model call and no write: it opens each stored database read-only, builds the runs
`app/review/cabinet_run_proposal.py` would propose from the newest stored reading of every package
revision that has one, and compares each with the keyed row on the same page (the `bala-kinds`
piece-kind keys, `pariksha-key/1`: each keyed piece's page hash, the row's widths left to right and
the kinds the key accepts).

    python scripts/cabinet_run_rescore.py \\
        --set "AI Set 1=<db name or URL>:~/gv-local/keys/bala-kinds/<key 1>.json" \\
        --set "AI Set 2=<db>:<key 2>" --set "AI Set 3=<db>:<key 3>" [--json <out>]

Per proposed run:
* **right**: the keyed row on that page has the same number of pieces and every width is equal,
  and every pre-selected kind (`filler`, only where the vendor printed it) is a kind the key accepts;
* **wrong**: a keyed row is on that page and a width or a pre-selected kind differs;
* **not keyed**: no keyed row on that page (never counted as right).
Per keyed row: proposed, or not proposed with the proposal's own reason.

A run is ready to ship only with 0 wrong. Keys stay local (outside git): they hold what client
drawings show.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models.document import Page
from app.models.evidence import ObservationCandidate
from app.models.package import PackageRevision
from app.review.cabinet_run_proposal import FILLER, proposed_runs


def _url(name: str) -> str:
    return name if "://" in name else f"postgresql+psycopg://gv:gv@localhost:5434/{name}"


_SOURCE = re.compile(r"form key (\S+), page (\d+)")


def _wall_to_wall_rows(form_key: Path | None) -> dict[int, list[Fraction]]:
    """The form key's wall-to-wall chains by page (`"915 [36] = 51 [2] | 813 [32] | 51 [2]"`).

    On a page where the stone runs into the walls, the countertop's row is not the cabinet run: the
    run is the wall-to-wall chain (fillers and cabinets between the walls), which the form key
    records beside the countertop's pieces. Both are accepted rows for that page.
    """
    if form_key is None:
        return {}
    from pariksha_kind_key import width_inches

    found: dict[int, list[Fraction]] = {}
    for countertop in json.loads(form_key.expanduser().read_text())["countertops"]:
        chain = countertop.get("wall_to_wall")
        if not isinstance(chain, str) or "=" not in chain:
            continue
        widths = [width_inches(part.strip()) for part in chain.partition("=")[2].split("|")]
        if all(width is not None for width in widths):
            found[int(countertop["page"])] = [width for width in widths if width is not None]
    return found


def _keyed_rows(
    path: Path, form_key: Path | None
) -> dict[str, list[tuple[list[Fraction], list[list[str]]]]]:
    """Keyed rows by page hash: each row's widths and, per piece, the kinds the key accepts.

    A page whose form-key entry also records a wall-to-wall chain accepts that chain too, with no
    kinds scored on it.
    """
    cases = json.loads(path.expanduser().read_text())["cases"]
    walls = _wall_to_wall_rows(form_key)
    rows: dict[tuple[str, str, tuple[str, ...]], dict[int, list[str]]] = defaultdict(dict)
    sources: dict[tuple[str, str, tuple[str, ...]], int] = {}
    for case in cases:
        page, _, piece = case["question_id"].partition(":")
        answer = case["answer"]
        kinds = list(answer.get("kinds", []))
        key = (case["page_sha256"], page, tuple(case["row_widths"]))
        rows[key][int(piece[5:])] = kinds
        matched = _SOURCE.search(str(case.get("source", "")))
        if matched is not None:
            sources[key] = int(matched.group(2))
    found: dict[str, list[tuple[list[Fraction], list[list[str]]]]] = defaultdict(list)
    for (sha, page, widths), by_piece in rows.items():
        found[sha].append(
            (
                [Fraction(width) for width in widths],
                [by_piece.get(i, []) for i in range(len(widths))],
            )
        )
        chain = walls.get(sources.get((sha, page, widths), -1))
        if chain is not None and (chain, [[] for _ in chain]) not in found[sha]:
            found[sha].append((chain, [[] for _ in chain]))
    return found


def score(database: str, key: Path, form_key: Path | None = None) -> dict[str, Any]:
    keyed = _keyed_rows(key, form_key)
    engine = create_engine(_url(database))
    result: dict[str, Any] = {"runs": [], "keyed_rows_not_proposed": [], "skipped": []}
    proposed_pages: set[str] = set()
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        session = Session(bind=connection)
        for revision in session.scalars(select(PackageRevision.id)):
            runs, skipped = proposed_runs(session, revision)
            for entry in skipped:
                result["skipped"].append({"page": entry.page, "why": entry.why})
            for run in runs:
                anchor_page = session.scalar(
                    select(Page.content_hash)
                    .join(ObservationCandidate, ObservationCandidate.page_id == Page.id)
                    .where(ObservationCandidate.id == run.row_id)
                )
                widths = [
                    Fraction(int(piece.width.numerator), int(piece.width.denominator))
                    for piece in run.pieces
                ]
                preselected = [piece.proposed for piece in run.pieces]
                verdict = "not keyed"
                for key_widths, key_kinds in keyed.get(anchor_page or "", []):
                    proposed_pages.add(anchor_page or "")
                    same = key_widths == widths and all(
                        chosen is None or chosen in kinds or (chosen == FILLER and not kinds)
                        for chosen, kinds in zip(preselected, key_kinds, strict=False)
                    )
                    verdict = "right" if same else "wrong"
                    if same:
                        break
                result["runs"].append(
                    {
                        "page": run.page,
                        "widths": [str(width) for width in widths],
                        "preselected": preselected,
                        "verdict": verdict,
                    }
                )
        for sha, rows in keyed.items():
            if sha not in proposed_pages:
                result["keyed_rows_not_proposed"].append(
                    {"page_sha256": sha[:12], "rows": [[str(w) for w in r[0]] for r in rows]}
                )
        session.close()
    engine.dispose()
    counts: defaultdict[str, int] = defaultdict(int)
    for run in result["runs"]:
        counts[run["verdict"]] += 1
    result["counts"] = dict(counts)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--set", action="append", required=True, help="NAME=DB:KEY[:FORM_KEY for wall-to-wall rows]"
    )
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    every: dict[str, Any] = {}
    wrong = 0
    for item in args.set:
        name, _, rest = item.partition("=")
        database, _, keys = rest.partition(":")
        key, _, form_key = keys.partition(":")
        scored = score(database, Path(key), Path(form_key) if form_key else None)
        every[name] = scored
        wrong += scored["counts"].get("wrong", 0)
        print(f"== {name}: {scored['counts']}")
        for run in scored["runs"]:
            print(
                f"   p{run['page']}: {run['widths']} pre={run['preselected']} -> {run['verdict']}"
            )
        print(f"   keyed pages not proposed: {len(scored['keyed_rows_not_proposed'])}")
        reasons: dict[str, int] = defaultdict(int)
        for entry in scored["skipped"]:
            reasons[entry["why"]] += 1
        for why, count in sorted(reasons.items(), key=lambda pair: -pair[1]):
            print(f"   not proposed x{count}: {why}")
    if args.json:
        args.json.write_text(json.dumps(every, indent=2))
    print(f"TOTAL wrong: {wrong}")
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
