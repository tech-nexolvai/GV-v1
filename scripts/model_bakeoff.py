#!/usr/bin/env python3
"""Run the Phase C model-reading bake-off over a local crop manifest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.experiments.model_bakeoff import (
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
    parser.add_argument("models", help="JSON model manifest with model ids and token prices")
    parser.add_argument(
        "--polygon-dpi",
        type=int,
        required=True,
        help="DPI of the answer-key polygon coordinate frame",
    )
    parser.add_argument("--format", choices=("markdown", "csv"), default="markdown")
    parser.add_argument("--output", type=Path, help="write the scorecard here instead of stdout")
    parser.add_argument(
        "--region", help="Bedrock region; defaults to the adapter/AWS configuration"
    )
    parser.add_argument("--connect-timeout", type=int, default=10)
    parser.add_argument("--read-timeout", type=int, default=120)
    args = parser.parse_args(argv)

    try:
        crops = load_crops(args.crops, polygon_dpi=args.polygon_dpi)
        specs = load_model_specs(args.models)
        scorecard = run_bakeoff(
            adapters_from_specs(
                specs,
                region_name=args.region,
                connect_timeout_seconds=args.connect_timeout,
                read_timeout_seconds=args.read_timeout,
            ),
            crops,
        )
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
