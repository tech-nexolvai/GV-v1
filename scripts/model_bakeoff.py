#!/usr/bin/env python3
"""Run the model-reading bake-off over a human-read crop set: vision readers, the shape reader, or both.

The shape reader (#756) reads the drawing's paths under each crop with a template set a person
labelled. Add it with `--glyph-templates`; its match settings have no defaults and must be stated.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.experiments.model_bakeoff import (
    BakeoffAdapter,
    ModelBakeoffError,
    adapters_from_specs,
    load_crops,
    load_model_specs,
    render_csv,
    render_markdown,
    run_bakeoff,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("crops", help="gold case directory with PDFs + answer_key.json")
    parser.add_argument(
        "models",
        nargs="?",
        help="JSON model manifest with model ids and token prices; omit to run only the shape reader",
    )
    parser.add_argument(
        "--polygon-dpi",
        type=int,
        help=(
            "DPI of the answer-key polygon frame, only for a key that does not record one (#835); "
            "a key that records its frame is refused if this differs"
        ),
    )
    parser.add_argument("--format", choices=("markdown", "csv"), default="markdown")
    parser.add_argument("--output", type=Path, help="write the scorecard here instead of stdout")
    parser.add_argument(
        "--region", help="Bedrock region; defaults to the adapter/AWS configuration"
    )
    parser.add_argument("--connect-timeout", type=int, default=10)
    parser.add_argument("--read-timeout", type=int, default=120)
    parser.add_argument(
        "--glyph-templates",
        type=Path,
        help="a template set from `glyph_inventory.py build`: add the shape reader (#756)",
    )
    glyph_settings = {
        "--glyph-max-distance": "largest chamfer distance a character may match at, in pixels",
        "--glyph-margin": "how much nearer than a different character its template must be",
        "--glyph-size-ratio": "how far a character's size may be from its template's, >= 1",
        "--glyph-label-gap-pt": "how close a character must be to join a label, in points",
        "--glyph-max-label-pt": "how large a label may grow, in points",
    }
    for name, meaning in glyph_settings.items():
        parser.add_argument(name, type=Decimal, help=f"{meaning} (required with --glyph-templates)")
    args = parser.parse_args(argv)
    if args.models is None and args.glyph_templates is None:
        parser.error("give a model manifest, --glyph-templates, or both")
    missing = [
        name
        for name in glyph_settings
        if args.glyph_templates is not None
        and getattr(args, name.lstrip("-").replace("-", "_")) is None
    ]
    if missing:
        parser.error("--glyph-templates needs " + ", ".join(missing) + " (none has a default)")

    try:
        crops = load_crops(args.crops, polygon_dpi=args.polygon_dpi)
        adapters: list[BakeoffAdapter] = []
        if args.models is not None:
            adapters.extend(
                adapters_from_specs(
                    load_model_specs(args.models),
                    region_name=args.region,
                    connect_timeout_seconds=args.connect_timeout,
                    read_timeout_seconds=args.read_timeout,
                )
            )
        if args.glyph_templates is not None:
            from eval.experiments.glyph_bakeoff import GlyphBakeoffAdapter

            case = json.loads((Path(args.crops) / "answer_key.json").read_text(encoding="utf-8"))
            adapters.append(
                GlyphBakeoffAdapter.from_template_set(
                    args.glyph_templates,
                    pdf=(Path(args.crops) / case["shop"]).read_bytes(),
                    maximum_distance=args.glyph_max_distance,
                    minimum_margin=args.glyph_margin,
                    maximum_size_ratio=args.glyph_size_ratio,
                    label_gap_pt=args.glyph_label_gap_pt,
                    maximum_label_pt=args.glyph_max_label_pt,
                )
            )
        scorecard = run_bakeoff(adapters, crops)
    except ModelBakeoffError as error:
        print(error, file=sys.stderr)
        return 2

    rendered = render_csv(scorecard) if args.format == "csv" else render_markdown(scorecard)
    if args.output is None:
        print(rendered, end="" if rendered.endswith("\n") else "\n")
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
