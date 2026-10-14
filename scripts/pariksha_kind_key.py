#!/usr/bin/env python3
"""Build a Pariksha piece-kind key (`pariksha-key/1`, agent `bala-kinds`) from a confirmed form key (#1250).

Reads only local files: a person-confirmed form key (`form_key.json`: each countertop's page and
its pieces from left to right, each with the key's word for its kind) and the drawing's PDF. Writes
the key outside any repository (the key holds what client drawings show).

    python scripts/pariksha_kind_key.py --form-key <form_key.json> --pdf <drawing>.pdf \\
        --name "<key name>" --version 1 --out ~/gv-local/<kind key>.json \\
        [--page-map ~/gv-local/<map>.json]

* Each piece becomes one case: the page's content hash (read from the PDF the way every run
  records it), `p<page index>:piece<n>`, the row's piece count, and the kinds the key's word
  accepts (`eval.pariksha.kinds.truth_kinds`). A word that names no kind (a section, a countertop
  segment, unknown) is kept as `not_scored`, never dropped.
* `--page-map` keys another PDF that repeats the same drawings: `{"<page in --pdf>": <page in the
  form key>}`, page numbers from 1. Only the mapped pages are keyed.
* A countertop entry's `superseded` part is history; its current pieces are the ones keyed.

Source: issue #1250 · Verification: `tests/scripts/test_pariksha_kinds_scripts.py`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pariksha_scorecard import REFUSED, Refused, _outside_git

from eval.pariksha.keys import KEY_FORMAT, KeyFormatError
from eval.pariksha.kinds import KINDS_AGENT, KINDS_FAMILY, load_kinds_key, truth_kinds


def _word(piece: object) -> str:
    """The key's word for a piece: `[text, kind]` (older keys) or `{"kind": ...}`."""
    if isinstance(piece, list) and len(piece) == 2 and isinstance(piece[1], str):
        return piece[1]
    if isinstance(piece, Mapping) and isinstance(piece.get("kind", ""), str):
        return str(piece.get("kind", ""))
    raise KeyFormatError(f"a form-key piece is [text, kind] or {{kind: ...}}, not {piece!r}")


def _ordered(pieces: Sequence[object]) -> list[object]:
    orders = [piece.get("order") if isinstance(piece, Mapping) else None for piece in pieces]
    if all(isinstance(order, int | str) and str(order).isdigit() for order in orders):
        return [piece for _order, piece in sorted(zip((int(str(o)) for o in orders), pieces))]
    return list(pieces)


def kind_key_document(
    form_key: Mapping[str, object],
    page_hashes: Mapping[int, str],
    *,
    name: str,
    version: str,
    source: str,
    page_map: Mapping[int, int] | None = None,
) -> dict[str, object]:
    """The key; `page_hashes` maps each page number (from 1) of the keyed PDF to its hash."""
    countertops = form_key.get("countertops")
    if not isinstance(countertops, list):
        raise KeyFormatError("a form key lists its countertops")
    by_page: dict[int, list[object]] = {}
    for countertop in countertops:
        if not isinstance(countertop, Mapping) or not isinstance(countertop.get("page"), int):
            raise KeyFormatError(f"a countertop entry names its page: {countertop!r}")
        pieces = countertop.get("pieces")
        if isinstance(pieces, list) and pieces:
            page = int(countertop["page"])
            if page in by_page:
                raise KeyFormatError(f"the form key has two countertops with pieces on page {page}")
            by_page[page] = _ordered(pieces)
    targets = (
        {page: page for page in by_page}
        if page_map is None
        else {target: keyed for target, keyed in page_map.items() if keyed in by_page}
    )
    cases: list[dict[str, object]] = []
    for target, keyed in sorted(targets.items()):
        if target not in page_hashes:
            raise Refused(f"page {target} is not in the PDF")
        pieces = by_page[keyed]
        for index, piece in enumerate(pieces):
            truth = truth_kinds(_word(piece))
            cases.append(
                {
                    "family": KINDS_FAMILY,
                    "page_sha256": page_hashes[target],
                    "question_id": f"p{target - 1}:piece{index}",
                    "pieces": len(pieces),
                    "answer": (
                        {"kinds": list(truth.kinds)}
                        if truth.kinds
                        else {"not_scored": truth.not_scored}
                    ),
                    "source": f"{source}, page {keyed} piece {index + 1}: {_word(piece)!r}",
                }
            )
    if not cases:
        raise KeyFormatError("the form key keys no piece on the pages asked for")
    document: dict[str, object] = {
        "format": KEY_FORMAT,
        "agent": KINDS_AGENT,
        "name": name,
        "version": version,
        "cases": cases,
    }
    load_kinds_key(document)  # the key it writes is one the scorer reads
    return document


def pdf_page_hashes(data: bytes) -> dict[int, str]:
    """Each page's content hash (page number from 1), as every run records it."""
    from extraction.reader import read_pages

    return {page.index + 1: hashlib.sha256(page.content).hexdigest() for page in read_pages(data)}


def _page_map(path: Path) -> dict[int, int]:
    data = json.loads(path.read_text())
    if not isinstance(data, Mapping) or not all(
        str(target).isdigit() and isinstance(keyed, int) for target, keyed in data.items()
    ):
        raise KeyFormatError('a page map is {"<page in --pdf>": <page in the form key>}')
    return {int(target): int(keyed) for target, keyed in data.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--form-key", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--page-map", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        out = _outside_git(args.out)
        document = kind_key_document(
            json.loads(args.form_key.expanduser().read_text()),
            pdf_page_hashes(args.pdf.expanduser().read_bytes()),
            name=args.name,
            version=args.version,
            source=f"form key {args.form_key.parent.name}",
            page_map=None if args.page_map is None else _page_map(args.page_map.expanduser()),
        )
        with out.open("x", encoding="utf-8") as handle:
            json.dump(document, handle, indent=1, ensure_ascii=False)
            handle.write("\n")
    except (Refused, KeyFormatError, FileExistsError, json.JSONDecodeError) as error:
        print(f"pariksha kind key: {error}", file=sys.stderr)
        return REFUSED
    cases = document["cases"]
    assert isinstance(cases, list)
    scored = sum(1 for case in cases if "kinds" in case["answer"])
    print(f"wrote {out}: {len(cases)} pieces, {scored} with a kind to score")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
