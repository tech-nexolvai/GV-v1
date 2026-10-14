#!/usr/bin/env python3
"""Build a Pariksha walls key (`pariksha-key/1`, agent `rekha-walls`) from a local walls truth (#1250).

Reads only local files: the walls truth, an optional list of settled or unsettled "drawn" ends, and
the drawing's PDF. Writes the key outside any repository (the key holds what client drawings show).

    python scripts/pariksha_walls_key.py --truth ~/gv-local/<walls truth>.json --set s3 \\
        --pdf ~/gv-local/<drawing>.pdf [--drawn ~/gv-local/<drawn ends>.json] \\
        --name "<key name>" --version 1 --out ~/gv-local/<walls key>.json

**The walls truth** (the #1204 proof's shape): `{"<set>|<page>|<row>": [<layout>, {"L": 0|1,
"R": 0|1}]}`, one entry per keyed row: the page number (from 1), the row candidate's number on the
page (from 1, in code's row order), GV's layout for it (`back_left_right`, `back_only`, `island`,
or `corner`, which is the back wall and the one end that has a wall) and whether each end has a wall
on site (0 or 1). These come from GV's markup, so they are the `site` truth. A corner whose ends
are both walls or neither is a key error, never turned into a layout.

**The drawn ends** (`--drawn`): `[{"page": 7, "side": "right", "drawn": "key_unsure",
"note": "..."}]`. Every end not listed is drawn as it is on site. An end whose drawing is not
settled yet is `key_unsure`: listed in every scorecard and never scored, never guessed. A drawn end
is named by page and side, so it holds for every keyed row of that page: fine while the truth keys
one row per page, and a known limit when a page has two keyed rows.

**From the PDF**, with the product's own code, at $0: each page's content hash (the hash every run
records for the page) and each keyed row's picture box, the box a run stores on the row's wall
candidate (`page_rows_and_ink` with the measured row settings, `wall_pictures` with E3's wall
settings, at 300 dpi). A run that asked about another row then scores "not asked", never right.

Source: issue #1250 · Verification: `tests/scripts/test_pariksha_walls_scripts.py`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pariksha_scorecard import REFUSED, Refused, _outside_git

from eval.pariksha.keys import KEY_FORMAT, KeyFormatError
from eval.pariksha.walls import DRAWN, WALLS_AGENT, WALLS_FAMILY, load_walls_key

#: The dpi every run renders a page at; the boxes a run stores are in its pixels.
DPI = 300
_SIDE = {"left": "L", "right": "R"}


@dataclass(frozen=True, slots=True)
class TruthRow:
    page: int
    """From 1."""
    row: int
    """The row candidate's number on the page, from 1."""
    layout: str
    left: bool
    right: bool

    @property
    def site_layout(self) -> str:
        if self.layout != "corner":
            return self.layout
        if self.left == self.right:
            # `truth_rows` refuses this; never guess a corner's side.
            raise KeyFormatError(f"page {self.page} row {self.row}: a corner has one end wall")
        return "back_and_right" if self.right else "back_and_left"


def truth_rows(data: Mapping[str, object], set_name: str) -> list[TruthRow]:
    """The keyed rows of one set, from the walls truth's `{"<set>|<page>|<row>": ...}` entries."""
    rows: list[TruthRow] = []
    for name, value in data.items():
        parts = name.split("|")
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            raise KeyFormatError(f"walls truth entry {name!r} is not '<set>|<page>|<row>'")
        if parts[0] != set_name:
            continue
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not isinstance(value[0], str)
            or not isinstance(value[1], Mapping)
            or set(value[1]) != {"L", "R"}
        ):
            raise KeyFormatError(f"walls truth entry {name!r} is not [layout, {{L, R}}]")
        ends = value[1]["L"], value[1]["R"]
        if not all(type(end) is int and end in (0, 1) for end in ends):
            raise KeyFormatError(f"walls truth entry {name!r}: L and R are 0 or 1, not {ends!r}")
        left, right = (bool(end) for end in ends)
        if value[0] == "corner" and left == right:
            raise KeyFormatError(
                f"walls truth entry {name!r} is a corner with "
                f"{'both ends' if left else 'neither end'} a wall: a corner is the back wall and "
                "exactly one end wall. Fix the truth; the side is never guessed"
            )
        rows.append(
            TruthRow(page=int(parts[1]), row=int(parts[2]), layout=value[0], left=left, right=right)
        )
    if not rows:
        raise KeyFormatError(f"the walls truth has no row of set {set_name!r}")
    return sorted(rows, key=lambda row: (row.page, row.row))


def drawn_ends(data: object) -> dict[tuple[int, str], tuple[str, str]]:
    """`(page, side)` → (drawn, note), from the `--drawn` list."""
    if not isinstance(data, list):
        raise KeyFormatError("the drawn ends are a list of {page, side, drawn, note}")
    ends: dict[tuple[int, str], tuple[str, str]] = {}
    for entry in data:
        if not isinstance(entry, Mapping) or not set(entry) <= {"page", "side", "drawn", "note"}:
            raise KeyFormatError(f"a drawn end is {{page, side, drawn, note}}, not {entry!r}")
        page, side, drawn = entry.get("page"), entry.get("side"), entry.get("drawn")
        if not isinstance(page, int) or side not in _SIDE or drawn not in DRAWN:
            raise KeyFormatError(
                f"a drawn end needs a page number, a side (left or right) and drawn in {DRAWN}, "
                f"not {entry!r}"
            )
        if (page, side) in ends:
            raise KeyFormatError(f"page {page} {side} is listed twice in the drawn ends")
        ends[(page, str(side))] = (str(drawn), str(entry.get("note", "")))
    return ends


def walls_key_document(
    rows: Sequence[TruthRow],
    drawn: Mapping[tuple[int, str], tuple[str, str]],
    geometry: Mapping[tuple[int, int], tuple[str, tuple[int, int, int, int]]],
    *,
    name: str,
    version: str,
    source: str,
) -> dict[str, object]:
    """The key: one case per row; `geometry` gives each (page, row)'s page hash and row box."""
    keyed = {(row.page, side) for row in rows for side in _SIDE}
    stray = sorted(set(drawn) - keyed)
    if stray:
        raise KeyFormatError(f"drawn ends of rows the truth does not key: {stray}")
    cases: list[dict[str, object]] = []
    for row in rows:
        page_sha256, box = geometry[(row.page, row.row)]
        ends: dict[str, dict[str, str]] = {}
        for side, letter in _SIDE.items():
            site = "wall" if (row.left if letter == "L" else row.right) else "open"
            end = {"drawn": site, "site": site}
            if (row.page, side) in drawn:
                end["drawn"], note = drawn[(row.page, side)]
                if note:
                    end["note"] = note
            ends[side] = end
        cases.append(
            {
                "family": WALLS_FAMILY,
                "page_sha256": page_sha256,
                "question_id": f"p{row.page - 1}:walls",
                "row_box": ",".join(str(value) for value in box),
                "left": ends["left"],
                "right": ends["right"],
                "site_layout": row.site_layout,
                "source": f"{source}; row {row.row} of the page",
            }
        )
    document: dict[str, object] = {
        "format": KEY_FORMAT,
        "agent": WALLS_AGENT,
        "name": name,
        "version": version,
        "cases": cases,
    }
    load_walls_key(document)  # the key it writes is one the scorer reads
    return document


def pdf_geometry(
    data: bytes, rows: Sequence[TruthRow]
) -> dict[tuple[int, int], tuple[str, tuple[int, int, int, int]]]:
    """Each keyed row's page hash and row picture box, by the product's own code."""
    from extraction.geometry.rows import MEASURED_SETTINGS
    from extraction.reader import read_pages
    from extraction.rows import page_rows_and_ink
    from extraction.slot_reader.walls import E3_WALL_SETTINGS, wall_pictures

    pages = read_pages(data)
    found: dict[tuple[int, int], tuple[str, tuple[int, int, int, int]]] = {}
    for page in sorted({row.page for row in rows}):
        if not 1 <= page <= len(pages):
            raise Refused(f"page {page} is not in the PDF ({len(pages)} pages)")
        page_sha256 = hashlib.sha256(pages[page - 1].content).hexdigest()
        rows_and_ink = page_rows_and_ink(data, page - 1, dpi=DPI, settings=MEASURED_SETTINGS)
        candidates = rows_and_ink.candidates.rows.candidates
        for row in (row for row in rows if row.page == page):
            if not 1 <= row.row <= len(candidates):
                raise Refused(f"page {page} has {len(candidates)} row candidates, not {row.row}")
            picture = wall_pictures(
                candidates[row.row - 1], rows_and_ink.ink, settings=E3_WALL_SETTINGS
            ).row
            x0, y0 = rows_and_ink.to_pixels(picture.x0, picture.top)
            x1, y1 = rows_and_ink.to_pixels(picture.x1, picture.bottom)
            found[(page, row.row)] = (page_sha256, (x0, y0, x1, y1))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--set", required=True, help="the set the truth's entries name (s1, s2..)")
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--drawn", type=Path, help="the ends whose drawing differs or is unsure")
    parser.add_argument("--name", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        out = _outside_git(args.out)
        rows = truth_rows(json.loads(args.truth.expanduser().read_text()), args.set)
        drawn = (
            {}
            if args.drawn is None
            else drawn_ends(json.loads(args.drawn.expanduser().read_text()))
        )
        geometry = pdf_geometry(args.pdf.expanduser().read_bytes(), rows)
        document = walls_key_document(
            rows,
            drawn,
            geometry,
            name=args.name,
            version=args.version,
            source=f"walls truth {args.truth.name} ({args.set})",
        )
        with out.open("x", encoding="utf-8") as handle:
            json.dump(document, handle, indent=1, ensure_ascii=False)
            handle.write("\n")
    except (Refused, KeyFormatError, FileExistsError, json.JSONDecodeError) as error:
        print(f"pariksha walls key: {error}", file=sys.stderr)
        return REFUSED
    unsure = [
        f"page {page} {side}" for (page, side), (said, _) in drawn.items() if said == "key_unsure"
    ]
    print(f"wrote {out}: {len(rows)} rows; drawn not settled at {', '.join(unsure) or 'no end'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
