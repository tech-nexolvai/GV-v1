"""How much of the architect's drawing the direct compare can see, by code and for free.

For each combined-sheet PDF given, every page is read with `extraction/compare/page.py` and the
counts are printed: architect views decided by code, every dimension the architect printed, how
many sit on a drawn dimension line (widths and heights), how many have a value the compare may use
(two judgments: the text reads exactly and the drawn length agrees), how many are held and why,
the vendor's views with a printed scale and labelled dimension lines, and how many architect
dimensions pair with exactly one vendor line by geometry (`extraction/compare/pair.py`), with the
reasons the others do not.

No model, no database, no network: $0. It prints counts only, never a value or a title off the
drawing, so its output may be pasted anywhere. `--json PATH` also writes the per-view detail
(which does hold the printed values) to a local file: keep that file out of git.

Usage: `python scripts/direct_compare_coverage.py SET1.pdf SET2.pdf ... [--json OUT.json]`
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections import Counter
from pathlib import Path

import pdfplumber

from extraction.compare.dimensions import NOT_ON_A_LINE, Axis
from extraction.compare.page import MEASURED_COMPARE_SETTINGS, ComparePage, read_compare_page
from extraction.compare.pair import MEASURED_PAIR_SETTINGS, PairStatus, pair_page

_DPI = 150


def _reason_group(reason: str) -> str:
    if reason == NOT_ON_A_LINE:
        return "no dimension line under it"
    if "length drawn" in reason:
        return "drawn length disagrees"
    if "more than one label" in reason:
        return "two labels on one span"
    if "scale is not known" in reason:
        return "view scale unknown"
    return "label does not read as one firm dimension"


def _pair_group(reason: str) -> str:
    if "named the vendor's" in reason:
        return "no vendor drawing on the sheet"
    if "draws no" in reason:
        return "the vendor draws no line this long"
    if "only match for" in reason:
        return "one vendor line matches several architect dimensions"
    if "dimensions are drawn this long" in reason:
        return "several vendor lines are drawn this long"
    if "does not line up" in reason:
        return "vendor view has no scale and does not line up"
    return "the architect's dimension is held or on no line"


def tally(pages: list[ComparePage]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for page in pages:
        counts["pages with an architect view"] += bool(page.architect)
        for view in page.architect:
            counts["architect views"] += 1
            counts["architect views with a scale"] += view.points_per_inch is not None
            for dim in view.dimensions:
                counts["architect dimensions"] += 1
                if dim.span is not None:
                    counts[f"on a {dim.span.axis.value} line"] += 1
                if dim.inches is not None:
                    counts["usable value (two judgments)"] += 1
                    if dim.span is not None and dim.span.axis is Axis.VERTICAL:
                        counts["usable heights"] += 1
                else:
                    counts[f"held: {_reason_group(dim.held_reason or '')}"] += 1
        for vendor in page.vendor:
            counts["vendor views"] += 1
            counts["vendor views with a printed scale"] += vendor.points_per_inch is not None
            counts["vendor labelled dimension spans"] += len(vendor.lines)
        for pairing in pair_page(page, MEASURED_PAIR_SETTINGS):
            if pairing.status is PairStatus.PAIRED:
                counts[f"paired ({pairing.how})"] += 1
            else:
                counts[f"not paired: {_pair_group(pairing.reason)}"] += 1
    return counts


def _detail(page: ComparePage) -> dict[str, object]:
    return {
        "page": page.page_index + 1,
        "notes": list(page.notes),
        "architect": [
            {
                "scale": view.scale_note,
                "points_per_inch": (
                    None if view.points_per_inch is None else str(view.points_per_inch)
                ),
                "dimensions": [
                    {
                        "A": dim.number,
                        "text": dim.text,
                        "inches": None if dim.inches is None else str(dim.inches),
                        "axis": None if dim.span is None else dim.span.axis.value,
                        "held": dim.held_reason,
                    }
                    for dim in view.dimensions
                ],
            }
            for view in page.architect
        ],
        "vendor": [
            {
                "heading": view.heading,
                "points_per_inch": (
                    None if view.points_per_inch is None else str(view.points_per_inch)
                ),
                "scale_reason": view.scale_reason,
                "lines": len(view.lines),
            }
            for view in page.vendor
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdfs", nargs="+", type=Path)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)
    detail: dict[str, object] = {}
    for number, path in enumerate(args.pdfs, start=1):
        data = path.read_bytes()
        with pdfplumber.open(io.BytesIO(data)) as document:
            count = len(document.pages)
        pages = [
            read_compare_page(data, index, settings=MEASURED_COMPARE_SETTINGS, dpi=_DPI)
            for index in range(count)
        ]
        print(f"set {number} ({count} pages):")
        for key, value in sorted(tally(pages).items()):
            print(f"  {key}: {value}")
        detail[str(number)] = [_detail(page) for page in pages if page.architect]
    if args.json is not None:
        args.json.write_text(json.dumps(detail, indent=1))
        print(f"detail written to {args.json} (holds drawing values: keep it local)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
