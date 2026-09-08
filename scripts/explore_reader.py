"""Send hand-chosen regions of a drawing through the model seam and print what came back.

EXPLORATORY, and it needs a model running locally. Nothing here produces a verdict, scores anything,
or touches the gold set: it renders the boxes a person listed in a manifest, sends each through
`extraction.models.openmodel.OpenModelAdapter`, and prints the reading beside what that person read
off the same crop by eye. That comparison is a first signal about a reader, not a measurement of one.

**What it needs.** A local Ollama with a vision model pulled, and the model named:

    ollama pull minicpm-v && ollama serve
    GV_OPENMODEL_ID=minicpm-v:latest python scripts/explore_reader.py REGIONS.json

`minicpm-v` publishes vision and no tool support, so this exercises the adapter's JSON-schema
strategy (#536). A tool-capable endpoint takes the same path through the forced-tool one; the
`--strategy` flag pins either explicitly. Any OpenAI-compatible endpoint works through
`GV_OPENMODEL_BASE_URL` and `GV_OPENMODEL_API_KEY`.

**No drawing content is in this file and none belongs here.** The PDF path, the crop boxes, the human
readings and the render resolution all come from the manifest, which lives under `data/` and is not
tracked. So do the results.

The manifest is JSON:

    {"pdf": "data/drawings/…/SET.pdf", "page": 3, "render_dpi": 600,
     "regions": [{"id": "iso-33", "box_pt": [295, 191, 336, 211], "truth": "33\\"",
                  "difficulty": "isolated, horizontal"}]}

`box_pt` is `(left, bottom, right, top)` in PDF points, y upward from the bottom of the page, which
is the coordinate system the file itself uses. `render_dpi` is per manifest rather than fixed because
resolution changes the answer — a rotated `8' - 6"` read as `10.8` at 150 dpi and `8'-6''` at 600, and
these sheets are vector so the detail at 600 is real rather than interpolated (`VISION_CROP_DPI`).

Source: issue #539, from the `AI_Set 2` reader probe.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from extraction.models.context import AssembledContext
from extraction.models.openmodel import (
    OpenModelAdapter,
    OpenModelAdapterError,
    OpenModelConfig,
    OpenModelRequest,
    StructuredOutputStrategy,
)
from extraction.rasterise import VISION_CROP_DPI
from extraction.vector_first import crop_box_pt


@dataclass
class Sink:
    """Keep the seam's own attempt records, which are where a refusal explains itself."""

    invocations: list[Any] = field(default_factory=list)
    rejections: list[Any] = field(default_factory=list)

    def record(self, invocation: Any) -> None:
        self.invocations.append(invocation)

    def record_rejection(self, rejection: Any) -> None:
        self.rejections.append(rejection)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="JSON listing the PDF, page and crop boxes")
    parser.add_argument("--out", type=Path, help="write the readings here as JSON")
    parser.add_argument("--only", help="run one region by id")
    parser.add_argument(
        "--strategy",
        choices=[member.value for member in StructuredOutputStrategy],
        default=StructuredOutputStrategy.AUTO.value,
        help="how the model is made to answer; 'auto' asks the endpoint what it can do",
    )
    parser.add_argument(
        "--include-markup",
        action="store_true",
        help=(
            "render the reviewer's annotations into the crop as well. Off by default: a crop of two "
            "superimposed labels produced a reading that was neither of them"
        ),
    )
    return parser.parse_args()


def _config(arguments: argparse.Namespace) -> OpenModelConfig:
    model_id = os.environ.get("GV_OPENMODEL_ID")
    if not model_id:
        sys.exit("set GV_OPENMODEL_ID to the model to probe, and make sure it is running")
    return OpenModelConfig(
        model_id=model_id,
        prompt_id="dimension-reader-v2",
        template_id="bounded-crop-v1",
        connect_timeout_seconds=int(os.environ.get("GV_OPENMODEL_CONNECT_TIMEOUT", "10")),
        read_timeout_seconds=int(os.environ.get("GV_OPENMODEL_READ_TIMEOUT", "300")),
        max_attempts=1,
        base_url=os.environ.get("GV_OPENMODEL_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.environ.get("GV_OPENMODEL_API_KEY") or None,
        strategy=StructuredOutputStrategy(arguments.strategy),
    )


def main() -> int:
    arguments = _arguments()
    manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    data = Path(manifest["pdf"]).read_bytes()
    page_index = int(manifest["page"]) - 1
    dpi = int(manifest.get("render_dpi", VISION_CROP_DPI))
    config = _config(arguments)

    regions = [
        region
        for region in manifest["regions"]
        if arguments.only is None or region["id"] == arguments.only
    ]
    print(
        f"model={config.model_id} strategy={arguments.strategy} page={manifest['page']} dpi={dpi} "
        f"regions={len(regions)}",
        flush=True,
    )

    readings: list[dict[str, Any]] = []
    for region in regions:
        box = tuple(Decimal(str(value)) for value in region["box_pt"])
        crop = crop_box_pt(
            data,
            page_index,
            box,  # type: ignore[arg-type]
            dpi=dpi,
            include_markup=arguments.include_markup,
        )
        sink = Sink()
        adapter = OpenModelAdapter.from_environment(config, sink)
        request = OpenModelRequest(
            candidate_id=f"explore-{region['id']}",
            page=page_index,
            crop=crop,
            image_format="png",
            # Empty on purpose. Any nearby text on these sheets is either the reviewer's own markup —
            # feeding the model the other layer's answer — or outlined vector the reader cannot read
            # either.
            context=AssembledContext(nearby_text=(), nearby_geometry=()),
            bound_pt=box[2] - box[0],
        )
        started = time.monotonic()
        row: dict[str, Any] = {
            "id": region["id"],
            "truth": region.get("truth", ""),
            "difficulty": region.get("difficulty", ""),
            "crop_bytes": len(crop),
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
                "refusal_reason": sink.rejections[-1].reason if sink.rejections else None,
                "refused_payload": (
                    sink.rejections[-1].raw_response[:400] if sink.rejections else None
                ),
            }
        row["seconds"] = round(time.monotonic() - started, 1)
        readings.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    if arguments.out:
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        arguments.out.write_text(
            json.dumps(
                {
                    "exploratory": (
                        "readings against what a person read off the same crop. No rule ran, "
                        "nothing is scored, and no reading carries a semantic type."
                    ),
                    "model": config.model_id,
                    "strategy": arguments.strategy,
                    "render_dpi": dpi,
                    "include_markup": arguments.include_markup,
                    "readings": readings,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"written: {arguments.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
