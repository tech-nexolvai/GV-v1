#!/usr/bin/env python3
"""The depth and sink reader's code skills on real pages, at $0 (#1256).

Reads one PDF (never modified) and calls no model and no network. For every vendor panel on each
page (the drawing GV's heading names the vendor's, `extraction/panels.py`), it prints:

* the views found on the panel and code's judgment of each one's kind, with its reason;
* the candidate spans per axis, with what each end meets;
* the view's scale from its horizontal overall, where the overall's value is real text;
* chains and whether they close exactly, where every value is real text;
* the sink outlines on plan views, and whether a cut-out could be automatic.

    .venv/bin/python scripts/depth_dry_run.py --pdf <set.pdf> [--pages 7 15 ...] \\
        --out ~/gv-local/<folder> [--pictures] [--vendor-file] [--database <URL>]

`--vendor-file` says the PDF is the vendor's own file (uploaded in the vendor's slot), so every
pasted drawing is a vendor panel. `--database` (optional) narrows the pages to those the stored document's own vendor views are on,
read inside a read-only transaction (`eval/pariksha/stored._read_only`); the stored document is the
one whose sha256 is the PDF's. `--pictures` also writes each view's pictures (heading hidden,
numbered spans, lettered sink outlines) as the readers would see them.

The report and the pictures hold a client's drawing values: `--out` inside a git working tree is
refused. Exit codes: 0 done, 2 refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from evidence.crop import RenderedPage
from extraction.annotations import read_markup_layer
from extraction.architect.reader import (
    MEASURED_ARCHITECT_SETTINGS,
    _open_page,
    _stamps,
)
from extraction.architect.text import TextChar, find_phrases
from extraction.depth.pictures import (
    MarkedSpan,
    lettered_outlines_picture,
    numbered_spans_picture,
    view_picture,
)
from extraction.depth.spans import (
    MEASURED_SPAN_SETTINGS,
    Span,
    closure_check,
    cutout_judgment,
    drawn_length_check,
    find_spans,
    overall_horizontal,
    sink_outlines,
    text_lanes,
    view_scale,
)
from extraction.depth.view_kind import (
    MEASURED_VIEW_SETTINGS,
    ViewKind,
    code_view_kind,
    find_vendor_views,
    kind_from_heading,
)
from extraction.geometry.dimension_lines import Axis
from extraction.geometry.rows import Box
from extraction.panels import propose_panel_roles
from extraction.rasterise import render_page
from extraction.stamp_text import stamps_only

REFUSED = 2
#: The pictures' resolution: enough for a person to check them by eye, small on disk.
_PICTURE_DPI = 100
_MAXIMUM_PIXELS = 40_000_000
_UNSTORED = UUID(int=0)


class Refused(Exception):
    """The script will not do what was asked; the message says why."""


def _inside_git_working_tree(path: Path) -> bool:
    existing = path
    while not existing.exists() and existing != existing.parent:
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


def outside_git(path: Path) -> Path:
    """`path`, resolved, or a refusal when it lies in a git working tree (client values)."""
    resolved = path.expanduser().resolve()
    if resolved == REPO or REPO in resolved.parents or _inside_git_working_tree(resolved):
        raise Refused(
            f"{path} is inside a git working tree; the dry run's report holds a client's drawing "
            "values, so write it outside any repository (for example under ~/gv-local/)"
        )
    return resolved


def stored_vendor_pages(database: str, sha256: str) -> list[int]:
    """The 1-based pages holding the stored document's vendor views, read-only."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.models.document import DocumentVersion, Page
    from app.models.drawing import DrawingView
    from eval.pariksha.stored import _read_only

    engine = create_engine(database)
    try:
        with Session(engine) as session:
            _read_only(session)
            indices = session.scalars(
                select(Page.index)
                .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
                .join(DrawingView, DrawingView.page_id == Page.id)
                .where(DocumentVersion.sha256 == sha256, DrawingView.role == "shop")
                .distinct()
            ).all()
            session.rollback()
    finally:
        engine.dispose()
    return sorted(index + 1 for index in indices)


@dataclass
class ViewReport:
    number: int
    heading: str | None
    found_by: str
    separated: bool
    code_kind: str | None
    kind_reason: str
    heading_kind: str | None
    bands: list[dict[str, Any]]
    spans: list[dict[str, Any]]
    scale: str | None
    scale_reason: str
    closures: list[dict[str, Any]]
    sinks: list[dict[str, Any]]
    unclassified: int


@dataclass
class PageReport:
    page: int
    panels: int
    notes: list[str] = field(default_factory=list)
    views: list[ViewReport] = field(default_factory=list)


def _fraction(value: Fraction | None) -> str | None:
    return None if value is None else str(value)


def _chars_in(chars: Sequence[TextChar], box: Box) -> list[TextChar]:
    return [
        char
        for char in chars
        if box.x0 <= char.box.centre_x <= box.x1 and box.top <= char.box.centre_y <= box.bottom
    ]


def _span_row(span: Span) -> dict[str, Any]:
    return {
        "n": span.number,
        "axis": span.axis.value,
        "offset_pt": str(span.offset_pt),
        "start_pt": str(span.start_pt),
        "end_pt": str(span.end_pt),
        "length_pt": str(span.length_pt),
        "low_end": span.low_end.kind.value,
        "high_end": span.high_end.kind.value,
    }


def survey_page(
    data: bytes,
    page_number: int,
    *,
    pictures: Path | None,
    content_hash: str,
    vendor_file: bool = False,
) -> PageReport:
    """Everything the code skills find on one page's vendor panels.

    With `vendor_file`, the file is the vendor's own (uploaded in the vendor's slot), so every
    pasted drawing that is not inside another is a vendor panel; otherwise a panel is the vendor's
    only when GV's heading names it so.
    """
    index = page_number - 1
    layers = read_markup_layer(data, index, document_version_id=_UNSTORED, dpi=_PICTURE_DPI)
    proposals = propose_panel_roles(layers.vendor_stamps, layers.markup)
    stamps, _pictures = _stamps(data, index)
    if vendor_file:
        boxes = [box for box, _factor in stamps.values()]
        vendor = [
            box
            for box in boxes
            if not any(
                other != box
                and other.x0 <= box.x0
                and box.x1 <= other.x1
                and other.top <= box.top
                and box.bottom <= other.bottom
                for other in boxes
            )
        ]
    else:
        vendor = [
            stamps[proposal.annotation_index][0]
            for proposal in proposals
            if proposal.role == "shop" and proposal.annotation_index in stamps
        ]
    report = PageReport(page=page_number, panels=len(vendor))
    if not vendor:
        report.notes.append(
            "no vendor panel: no pasted drawing"
            if vendor_file
            else "no vendor panel: no drawing on this page has the vendor's heading "
            "(use --vendor-file for the vendor's own file)"
        )
        return report
    page = _open_page(
        stamps_only(data, index),
        index,
        drawing_boxes=tuple(box for box, _factor in stamps.values()),
        rows=MEASURED_ARCHITECT_SETTINGS.rows,
        settings=MEASURED_ARCHITECT_SETTINGS,
        dpi=_PICTURE_DPI,
    )
    rendered: RenderedPage | None = None
    if pictures is not None:
        rendered = render_page(
            data,
            index,
            document_version_id=_UNSTORED,
            page_content_hash=content_hash,
            dpi=_PICTURE_DPI,
            maximum_pixels=_MAXIMUM_PIXELS,
            vendor_only=True,
        )
    for panel_number, panel in enumerate(vendor, start=1):
        survey = find_vendor_views(page.chars, page.ink, panel, MEASURED_VIEW_SETTINGS)
        report.notes.extend(f"panel {panel_number}: {note}" for note in survey.notes)
        panel_phrases = [
            phrase.text
            for phrase in find_phrases(_chars_in(page.chars, panel), MEASURED_VIEW_SETTINGS.text)
        ]
        for view in survey.views:
            found = find_spans(page.ink, view.extent, MEASURED_SPAN_SETTINGS)
            heading = kind_from_heading(view.heading)
            judged = code_view_kind(heading, found.bands)
            phrases = find_phrases(_chars_in(page.chars, view.extent), MEASURED_VIEW_SETTINGS.text)
            lanes = text_lanes(found.spans, phrases, MEASURED_SPAN_SETTINGS)
            values = {
                number: lane.inches for number, lane in lanes.items() if lane.inches is not None
            }
            rows = []
            for span in found.spans:
                row = _span_row(span)
                lane = lanes[span.number]
                row["text_inches"] = _fraction(lane.inches)
                row["text_reason"] = lane.reason
                rows.append(row)
            overall = overall_horizontal(found.spans)
            scale_text: str | None = None
            if overall is None:
                scale_reason = "no horizontal span in this view"
            elif overall.number not in values:
                scale_reason = (
                    f"the overall (span {overall.number}) has no text value: "
                    + lanes[overall.number].reason
                )
            else:
                scale = view_scale(overall, values[overall.number])
                scale_text = f"{float(scale.points_per_inch):.4f} pt/in"
                scale_reason = f"from span {overall.number}'s text value"
                for row, span in zip(rows, found.spans, strict=True):
                    if span.number in values and span.number != overall.number:
                        check = drawn_length_check(
                            span,
                            values[span.number],
                            scale,
                            tolerance=MEASURED_SPAN_SETTINGS.drawn_length_tolerance,
                        )
                        row["drawn_agrees"] = check.agrees
            closures = []
            for closure in found.closures:
                checked = closure_check(closure, values)
                closures.append(
                    {
                        "axis": closure.overall.axis.value,
                        "overall": closure.overall.number,
                        "parts": [part.number for part in closure.parts],
                        "closes": checked.closes,
                        "reason": checked.reason,
                    }
                )
            sinks: list[dict[str, Any]] = []
            plan_like = ViewKind.PLAN in (judged.kind, heading.kind)
            if plan_like:
                found_sinks = sink_outlines(page.ink, view.extent, phrases, MEASURED_SPAN_SETTINGS)
                for sink_number, sink in enumerate(found_sinks, start=1):
                    judgment = cutout_judgment(sink, panel_phrases)
                    sinks.append(
                        {
                            "marker": sink.marker,
                            "outlines": [
                                {
                                    "letter": outline.letter,
                                    "drawn_as": outline.drawn_as,
                                    "width_pt": str(outline.box.width),
                                    "height_pt": str(outline.box.height),
                                }
                                for outline in sink.outlines
                            ],
                            "automatic": judgment.automatic,
                            "reason": judgment.reason,
                        }
                    )
                    if rendered is not None and pictures is not None:
                        crop = page.pixels(view.extent)
                        outlines = [
                            (outline.letter, page.pixels(outline.box)) for outline in sink.outlines
                        ]
                        (
                            pictures
                            / f"p{page_number}-v{panel_number}.{view.number}-sink{sink_number}.png"
                        ).write_bytes(
                            lettered_outlines_picture(
                                rendered, crop, outlines, thickness=2, badge=14, max_side=1400
                            )
                        )
            report.views.append(
                ViewReport(
                    number=view.number,
                    heading=view.heading,
                    found_by=view.found_by,
                    separated=view.separated,
                    code_kind=None if judged.kind is None else judged.kind.value,
                    kind_reason=judged.reason,
                    heading_kind=None if heading.kind is None else heading.kind.value,
                    bands=[
                        {"axis": band.axis.value, "source": band.source, "at_edge": band.at_edge}
                        for band in found.bands
                    ],
                    spans=rows,
                    scale=scale_text,
                    scale_reason=scale_reason,
                    closures=closures,
                    sinks=sinks,
                    unclassified=found.unclassified,
                )
            )
            if rendered is not None and pictures is not None:
                crop = page.pixels(view.extent)
                headings = [] if view.heading_box is None else [page.pixels(view.heading_box)]
                stem = pictures / f"p{page_number}-v{panel_number}.{view.number}"
                stem.with_name(stem.name + "-view.png").write_bytes(
                    view_picture(rendered, crop, headings, pad=3, max_side=1400)
                )
                stem.with_name(stem.name + "-spans.png").write_bytes(
                    numbered_spans_picture(
                        rendered,
                        crop,
                        [_marked(span, page.pixels) for span in found.spans],
                        thickness=3,
                        badge=16,
                        max_side=1400,
                    )
                )
    return report


def _marked(span: Span, pixels: Any) -> MarkedSpan:
    def at(along: Decimal) -> tuple[int, int]:
        x, y = span.point(along)
        left, top, _right, _bottom = pixels(Box(x, y, x, y))
        return left, top

    return MarkedSpan(span.number, span.axis, at(span.start_pt), at(span.end_pt))


def summary(report: PageReport) -> str:
    lines = [f"p{report.page}: {report.panels} vendor panel(s), {len(report.views)} view(s)"]
    lines.extend(f"  note: {note}" for note in report.notes)
    for view in report.views:
        horizontal = sum(1 for span in view.spans if span["axis"] == Axis.HORIZONTAL.value)
        vertical = len(view.spans) - horizontal
        lines.append(
            f"  view {view.number} {view.heading!r} ({view.found_by}"
            + ("" if view.separated else ", not separated")
            + f"): code kind {view.code_kind or 'unknown'}; spans H {horizontal} V {vertical}; "
            f"bands {len(view.bands)}; scale {view.scale or 'none'}"
        )
        lines.append(f"    kind: {view.kind_reason}")
        if view.scale is None:
            lines.append(f"    scale: {view.scale_reason}")
        for closure in view.closures:
            lines.append(
                f"    closure {closure['axis']} overall {closure['overall']} parts "
                f"{closure['parts']}: {closure['reason']}"
            )
        for sink in view.sinks:
            lines.append(
                f"    sink ({sink['marker']}): {len(sink['outlines'])} outline(s); "
                + ("automatic" if sink["automatic"] else "held")
                + f": {sink['reason']}"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--pages", type=int, nargs="*", help="1-based pages (default: every page)")
    parser.add_argument("--out", type=Path, required=True, help="a folder outside any repository")
    parser.add_argument("--pictures", action="store_true")
    parser.add_argument("--database", help="SQLAlchemy URL of a stored run (read-only)")
    parser.add_argument(
        "--vendor-file",
        action="store_true",
        help="the PDF is the vendor's own file: every pasted drawing is a vendor panel",
    )
    args = parser.parse_args(argv)
    try:
        out = outside_git(args.out)
    except Refused as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return REFUSED
    data = args.pdf.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    import pdfplumber

    with pdfplumber.open(args.pdf) as document:
        page_count = len(document.pages)
    pages = list(args.pages or range(1, page_count + 1))
    if args.database:
        stored = stored_vendor_pages(args.database, sha256)
        print(f"stored vendor pages: {stored or 'none (no stored document has this sha256)'}")
        if stored and not args.pages:
            pages = stored
    out.mkdir(parents=True, exist_ok=True)
    pictures = out / "pictures" if args.pictures else None
    if pictures is not None:
        pictures.mkdir(exist_ok=True)
    reports = []
    for number in pages:
        report = survey_page(
            data, number, pictures=pictures, content_hash=sha256, vendor_file=args.vendor_file
        )
        reports.append(report)
        print(summary(report), flush=True)
    stem = args.pdf.stem
    (out / f"{stem}-depth-dry-run.json").write_text(
        json.dumps(
            [
                {
                    "page": report.page,
                    "panels": report.panels,
                    "notes": report.notes,
                    "views": [view.__dict__ for view in report.views],
                }
                for report in reports
            ],
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
