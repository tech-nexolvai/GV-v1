"""Run the vector-first reader over one page of one PDF and write what it found.

EXPLORATORY. It produces readings, not verdicts: nothing here runs a rule, and nothing it writes is
scored against an answer, because for the drawings this exists to read there are no reviewed answers
yet (#274). Its output is for a person to look at.

    python scripts/read_vector_first.py DRAWING.pdf --page 3 --out data/exploration/page3.json

Without `--model` it does the deterministic half only — the reviewer's markup, the line geometry and
the plan of regions — and never opens a socket. With `--model minicpm-v:latest` (or whatever
`GV_OPENMODEL_ID` names) each planned region is cropped at 600 dpi and sent through the #536 seam,
one call per region.

**No drawing content is in this file and none belongs here.** The PDF path, the page and every
threshold are arguments; the readings go to the file `--out` names. Client drawings live under
`data/`, which is not tracked, and so should anything read out of them.

The five lengths are required arguments rather than defaults for the reason
`extraction/geometry/text_association.py` gives at length: they are empirical, one sheet cannot fix
them, and a default in a script is how today's guess becomes tomorrow's ground truth. The values in
`--help` are the ones the first real set was read with, quoted there so a run can be repeated and
not so it can be assumed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from extraction.annotations import PageLayers, read_annotation_layers
from extraction.rasterise import VISION_CROP_DPI
from extraction.vector_first import VectorFirstPage, plan_reads, region_crop


@dataclass
class _Sink:
    """Keep the seam's own records, which are where a refusal explains itself."""

    invocations: list[Any]
    rejections: list[Any]

    def record(self, invocation: Any) -> None:
        self.invocations.append(invocation)

    def record_rejection(self, rejection: Any) -> None:
        self.rejections.append(rejection)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--page", type=int, required=True, help="1-based, as printed on the sheet")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--dpi", type=int, required=True, help="stored-coordinate resolution, e.g. 150"
    )
    parser.add_argument(
        "--line-minimum-pt",
        type=Decimal,
        required=True,
        help="a path run at least this long is line-work (12 on AI_Set 2)",
    )
    parser.add_argument(
        "--glyph-maximum-pt",
        type=Decimal,
        required=True,
        help="a path smaller than this in both axes may be part of a glyph (12 on AI_Set 2)",
    )
    parser.add_argument(
        "--glyph-gap-pt",
        type=Decimal,
        required=True,
        help="small paths this close together are one text run (2.5 on AI_Set 2)",
    )
    parser.add_argument(
        "--proximity-limit",
        type=Decimal,
        required=True,
        help="stored units; a region this near line-work is worth reading (0.01 on AI_Set 2)",
    )
    parser.add_argument(
        "--minimum-paths",
        type=int,
        required=True,
        help="clusters with fewer paths than this are set aside (2 on AI_Set 2)",
    )
    parser.add_argument(
        "--margin-pt",
        type=Decimal,
        default=Decimal(2),
        help="page context around each crop, in points",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("GV_OPENMODEL_ID"),
        help="read the planned regions through the #536 seam; omit for the deterministic half only",
    )
    parser.add_argument("--limit", type=int, default=0, help="read at most this many regions")
    parser.add_argument(
        "--order",
        choices=("page", "paths"),
        default="page",
        help=(
            "which regions --limit keeps: 'page' takes them in the order the file draws them, "
            "'paths' takes the densest clusters first. A label is drawn with more glyph paths than "
            "a patch of hatching, so 'paths' is the better sample of what the reader does on real "
            "labels — it is a general property of glyphs, not a fact about any one sheet"
        ),
    )
    return parser.parse_args()


def _layers(arguments: argparse.Namespace, data: bytes, version: UUID) -> PageLayers:
    return read_annotation_layers(
        data,
        arguments.page - 1,
        document_version_id=version,
        dpi=arguments.dpi,
        line_minimum_pt=arguments.line_minimum_pt,
        glyph_maximum_pt=arguments.glyph_maximum_pt,
        glyph_gap_pt=arguments.glyph_gap_pt,
    )


def _read_regions(
    arguments: argparse.Namespace, data: bytes, plan: VectorFirstPage
) -> list[dict[str, Any]]:
    """One call per planned region, through the seam, recording whatever came back."""
    from extraction.models.context import AssembledContext
    from extraction.models.openmodel import (
        OpenModelAdapter,
        OpenModelAdapterError,
        OpenModelConfig,
        OpenModelRequest,
    )

    config = OpenModelConfig(
        model_id=arguments.model,
        prompt_id="dimension-reader-v2",
        template_id="bounded-crop-v1",
        connect_timeout_seconds=int(os.environ.get("GV_OPENMODEL_CONNECT_TIMEOUT", "10")),
        read_timeout_seconds=int(os.environ.get("GV_OPENMODEL_READ_TIMEOUT", "300")),
        max_attempts=1,
        base_url=os.environ.get("GV_OPENMODEL_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.environ.get("GV_OPENMODEL_API_KEY") or None,
    )

    planned = list(plan.to_read)
    if arguments.order == "paths":
        # Stable: ties keep page order, so two runs of the same file read the same regions.
        planned.sort(key=lambda entry: entry.region.point_count, reverse=True)
    if arguments.limit:
        planned = planned[: arguments.limit]
    readings: list[dict[str, Any]] = []
    for index, entry in enumerate(planned):
        crop = region_crop(
            data,
            plan.page_index,
            entry.region,
            dpi=VISION_CROP_DPI,
            margin_pt=arguments.margin_pt,
        )
        sink = _Sink([], [])
        adapter = OpenModelAdapter.from_environment(config, sink)
        request = OpenModelRequest(
            candidate_id=f"vector-first-p{plan.page_index}-r{index}",
            page=plan.page_index,
            crop=crop,
            image_format="png",
            # Empty on purpose. The nearby *markup* is the reviewer's own correction and feeding it
            # to the model would be handing it the answer from the other layer — the one thing this
            # path exists to keep separate.
            context=AssembledContext(nearby_text=(), nearby_geometry=()),
            bound_pt=arguments.margin_pt,
        )
        started = time.monotonic()
        row: dict[str, Any] = {
            "region": index,
            "crop_bytes": len(crop),
            "path_count": entry.region.path_count,
            "point_count": entry.region.point_count,
            "lines_near": len(entry.lines_near),
            "image_extent": [[point.x, point.y] for point in entry.region.image_extent],
        }
        try:
            candidate = adapter.extract(request)
            row |= {
                "reading": candidate.raw_text,
                "unit_guess": candidate.unit_guess.value if candidate.unit_guess else None,
                "semantic_guess": candidate.semantic_guess,
                "outcome": "ok",
            }
        except OpenModelAdapterError as error:
            row |= {
                "reading": None,
                "outcome": type(error).__name__,
                "why": str(error),
                "refused_payload": (
                    sink.rejections[-1].raw_response[:400] if sink.rejections else None
                ),
                "refusal_reason": (sink.rejections[-1].reason if sink.rejections else None),
            }
        row["seconds"] = round(time.monotonic() - started, 1)
        readings.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    return readings


def main() -> int:
    arguments = _arguments()
    data = arguments.pdf.read_bytes()
    version = uuid4()

    layers = _layers(arguments, data, version)
    plan = plan_reads(
        layers,
        proximity_limit=arguments.proximity_limit,
        minimum_paths=arguments.minimum_paths,
    )

    print(
        f"page {arguments.page}: {len(plan.markup)} markup notes (exact text), "
        f"{len(plan.drawing_segments)} line segments, {len(plan.to_read)} regions to read, "
        f"{len(plan.set_aside)} set aside, {len(layers.refusals)} refusals",
        flush=True,
    )

    output: dict[str, Any] = {
        "exploratory": (
            "readings, not verdicts. No rule ran, nothing is scored, and no reading carries a "
            "semantic type."
        ),
        "pdf": arguments.pdf.name,
        "page": arguments.page,
        "document_version_id": str(version),
        "settings": {
            "dpi": arguments.dpi,
            "line_minimum_pt": str(arguments.line_minimum_pt),
            "glyph_maximum_pt": str(arguments.glyph_maximum_pt),
            "glyph_gap_pt": str(arguments.glyph_gap_pt),
            "proximity_limit": str(arguments.proximity_limit),
            "minimum_paths": arguments.minimum_paths,
            "margin_pt": str(arguments.margin_pt),
            "order": arguments.order,
            "limit": arguments.limit,
            "vision_crop_dpi": VISION_CROP_DPI,
            "model": arguments.model,
        },
        # The two layers, side by side and not merged. Where they disagree, both are here.
        "reviewer_markup": [
            {
                "text": note.text,
                "author": note.author,
                "subtype": note.subtype,
                "annotation_index": note.annotation_index,
                "image_extent": [[point.x, point.y] for point in note.image_extent],
            }
            for note in plan.markup
        ],
        "vendor_drawing": {
            "segments": len(plan.drawing_segments),
            "regions_planned": len(plan.to_read),
            "regions_set_aside": [
                {"path_count": entry.region.path_count, "reason": entry.reason}
                for entry in plan.set_aside
            ],
            "readings": [],
        },
        "layer_refusals": [
            {
                "annotation_index": item.annotation_index,
                "subtype": item.subtype,
                "reason": item.reason,
            }
            for item in layers.refusals
        ],
    }

    if arguments.model:
        output["vendor_drawing"]["readings"] = _read_regions(arguments, data, plan)
    else:
        print("no --model: the deterministic half only, nothing was sent anywhere", flush=True)

    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"written: {arguments.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
