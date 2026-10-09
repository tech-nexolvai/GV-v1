#!/usr/bin/env python3
"""Run the architect reader on every page of a drawing and score it against a hand key (#1052).

No AI, no database, no cost: the pure reader (`extraction/architect/reader.py`) on each page, then
`eval/architect_reader.py` on the result. The key and the output hold the client's values, so both
stay outside the repository (`data/goldset/`, `~/gv-local/`):

    python scripts/score_architect_reader.py data/drawings/aiset2/AI_Set_2.pdf \\
        data/goldset/arch-key-ai-set-2/arch_key.json --out ~/gv-local/type1-build/1052/aiset2

Prints, per page: each drawing's two role judgments, then the key widths found, held (with the
reason) and missing, and every value read that the key does not list — each of those is for a
person to check on the page. `--out` also writes `score.json` and `score.md` there.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pdfplumber

from eval.architect_reader import ReadSpan, key_pages, score_pages
from extraction.architect.reader import (
    MEASURED_ARCHITECT_SETTINGS,
    ArchitectPage,
    read_architect_page,
)

#: The resolution pixel boxes are given at. It decides nothing the scorer looks at.
DPI = 150


def _inches(value: Fraction | None) -> str:
    if value is None:
        return "-"
    whole, rest = divmod(value, 1)
    if rest == 0:
        return f"{whole}"
    return f"{whole} {rest}" if whole else f"{rest}"


def _spans(page: ArchitectPage) -> list[ReadSpan]:
    return [
        ReadSpan(
            text=span.text or "",
            printed_inches=span.printed_inches,
            inches=span.inches,
            held_reason=span.held_reason,
            on_outline=span.on_outline,
        )
        for row in page.rows
        for span in row.spans
        if span.text is not None
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path)
    parser.add_argument("key", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    data = args.pdf.read_bytes()
    with pdfplumber.open(io.BytesIO(data)) as document:
        count = len(document.pages)
    pages = [
        read_architect_page(data, index, settings=MEASURED_ARCHITECT_SETTINGS, dpi=DPI)
        for index in range(count)
    ]
    key = key_pages(json.loads(args.key.read_text()))
    card = score_pages(key, {page.page_index + 1: _spans(page) for page in pages})

    lines: list[str] = [f"# Architect reader on {args.pdf.name}", ""]
    report: dict[str, object] = {"drawing": args.pdf.name, "totals": dict(card.totals), "pages": []}
    by_number = {page.page_index + 1: page for page in pages}
    for score in card.pages:
        page = by_number.get(score.page)
        lines.append(f"## Page {score.page}")
        views = []
        for view in () if page is None else page.views:
            judgment = view.judgment
            line = (
                f"- drawing {view.annotation_index}: heading "
                f"{judgment.heading_role.value if judgment.heading_role else 'silent'}, content "
                f"{judgment.content.role.value if judgment.content.role else 'silent'}"
                f" → {judgment.agreed.value if judgment.agreed else 'not decided'}"
                f" ({judgment.reason}); read: {view.read}; scale: {view.scale_reason}"
            )
            lines.append(line)
            views.append(
                {
                    "annotation_index": view.annotation_index,
                    "heading": judgment.heading_role and judgment.heading_role.value,
                    "content": judgment.content.role and judgment.content.role.value,
                    "labels_lean": judgment.content.labels_lean
                    and judgment.content.labels_lean.value,
                    "agreed": judgment.agreed and judgment.agreed.value,
                    "reason": judgment.reason,
                    "read": view.read,
                    "scale_note": view.scale_note,
                    "paste_factor": None if view.paste_factor is None else str(view.paste_factor),
                    "points_per_inch": (
                        None if view.points_per_inch is None else str(view.points_per_inch)
                    ),
                    "scale_reason": view.scale_reason,
                }
            )
        lines.append(
            f"- key widths {score.key_count}: found {len(score.found)}, held {len(score.held)}, "
            f"missing {len(score.missing)}; extra usable {len(score.extra_usable)}, "
            f"extra held {len(score.extra_held)}"
        )
        if score.found:
            lines.append("  - found: " + ", ".join(_inches(value) for value in score.found))
        for value, reason in score.held:
            lines.append(f"  - held {_inches(value)}: {reason}")
        if score.missing:
            lines.append("  - missing: " + ", ".join(_inches(value) for value in score.missing))
        for value, text in score.extra_usable:
            lines.append(f"  - read, not in key (check on the page): {text!r} = {_inches(value)}")
        for value, text, reason in score.extra_held:
            lines.append(f"  - held, not in key: {text!r} = {_inches(value)}: {reason}")
        spans = [] if page is None else _spans(page)
        lines.append(
            "  - on the outline: "
            + (
                ", ".join(f"{span.text!r} {span.on_outline}" for span in spans)
                if spans
                else "nothing read"
            )
        )
        lines.append("")
        report["pages"].append(  # type: ignore[union-attr]
            {
                "page": score.page,
                "views": views,
                "found": [str(value) for value in score.found],
                "held": [[str(value), reason] for value, reason in score.held],
                "missing": [str(value) for value in score.missing],
                "extra_usable": [[str(value), text] for value, text in score.extra_usable],
                "extra_held": [
                    [None if value is None else str(value), text, reason]
                    for value, text, reason in score.extra_held
                ],
                "spans": [
                    {
                        "text": span.text,
                        "printed": (
                            None if span.printed_inches is None else str(span.printed_inches)
                        ),
                        "usable": None if span.inches is None else str(span.inches),
                        "held": span.held_reason,
                        "on_outline": span.on_outline,
                    }
                    for span in spans
                ],
            }
        )
    totals = card.totals
    lines.insert(
        2,
        f"**Totals:** key widths {totals['key_widths']}, found {totals['found']}, held "
        f"{totals['held']}, missing {totals['missing']}, extra usable {totals['extra_usable']}, "
        f"extra held {totals['extra_held']}.\n",
    )
    text = "\n".join(lines)
    print(text)
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "score.md").write_text(text)
        (args.out / "score.json").write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
