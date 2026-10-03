#!/usr/bin/env python3
"""Score the reading agent on a human-read key: the reader pair alone, and with the agent (#757).

Every threshold is stated on the command line or read from a `scripts/demo.sh`-style settings file
— none has a default — and the scorecard says which were used. Paid calls go through the production
adapter, paced under each reader's quota. The per-crop working file holds the client's values: write
it under `data/`, which is never committed.

    python scripts/agent_scorecard.py --key data/goldset/reading-key-2026-09-30 \\
        --reader-settings scripts/demo.sh --stage-dpi 300 --sharper-dpi 450 \\
        --label-gap-pt 4 --max-label-pt 40 --max-steps 6 \\
        --output data/goldset/reading-key-2026-09-30/agent_scorecard.md

Give `--key` once per key — one key per drawing (#867) — and every key's crops are scored into one
scorecard, each on its own drawing. A crop id two keys share is refused.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.experiments.agent_scorecard import (
    BedrockCropReader,
    KeyCrop,
    PageGeometry,
    ScorecardError,
    ScorecardPage,
    build_pages,
    key_frame_dpi,
    load_key,
    render_markdown,
    results_json,
    score_crop,
)
from extraction.agent.tools import VlmRole

#: How fast each reader may be asked. Nova 2 Lite's cross-region profile allows 20 a minute on this
#: account (#716); the others are paced well under their published limits.
CALLS_PER_MINUTE = {
    "bedrock-nova-2-lite": 18,
    "bedrock-ministral-3-3b": 60,
    "bedrock-mistral-large-3": 30,
}


@dataclass(frozen=True, slots=True)
class LoadedKey:
    """One key's crops, the frame they are in, and its drawing's pages as the stage reads them."""

    crops: tuple[KeyCrop, ...]
    key_dpi: int
    pages: dict[int, ScorecardPage]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--key",
        type=Path,
        action="append",
        required=True,
        help="a key directory — answer_key.json, crops.csv, its PDF; repeat for each key",
    )
    parser.add_argument("--reader-settings", type=Path, required=True)
    parser.add_argument(
        "--key-dpi",
        type=int,
        help=(
            "the frame crops.csv is in, only for a key that does not record one (#835); a key "
            "that records its frame is refused if this differs"
        ),
    )
    parser.add_argument("--stage-dpi", type=int, required=True, help="the stage's render")
    parser.add_argument("--sharper-dpi", type=int, required=True)
    parser.add_argument("--label-gap-pt", type=Decimal, required=True)
    parser.add_argument("--max-label-pt", type=Decimal, required=True)
    parser.add_argument("--max-steps", type=int, required=True)
    parser.add_argument("--pair", default="bedrock-nova-2-lite,bedrock-ministral-3-3b")
    parser.add_argument("--primary", default="bedrock-nova-2-lite")
    parser.add_argument("--escalation", default="bedrock-mistral-large-3", help="#757 D-A2")
    parser.add_argument("--rates", type=Path, default=Path("deploy/model_rates.us-east-1.json"))
    parser.add_argument("--only", help="comma-separated crop ids, for a dry run")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from app.runs.rates import call_cost_micros, load_model_rates
    from extraction.glyph_bands import FractionBarGeometry
    from extraction.models.nova import vision_config_for_extractor
    from scripts.glyph_inventory import read_reader_settings
    from workflow.reading_agent import ReadingAgentSettings
    from workflow.stages import VISION_CROP_CONTEXT_MARGIN_PT

    try:
        reader = read_reader_settings(args.reader_settings)
        geometry = PageGeometry(
            line_minimum_pt=Decimal(reader["GV_READER_LINE_MINIMUM_PT"]),
            glyph_maximum_pt=Decimal(reader["GV_READER_GLYPH_MAXIMUM_PT"]),
            glyph_gap_pt=Decimal(reader["GV_READER_GLYPH_GAP_PT"]),
            fraction_bar=FractionBarGeometry(
                bar_thickness_max_pt=Decimal(reader["GV_READER_FRACTION_BAR_THICKNESS_MAX_PT"]),
                bar_length_min_pt=Decimal(reader["GV_READER_FRACTION_BAR_LENGTH_MIN_PT"]),
                reach_pt=Decimal(reader["GV_READER_FRACTION_REACH_PT"]),
                glyph_min_pt=Decimal(reader["GV_READER_FRACTION_GLYPH_MIN_PT"]),
                glyph_max_pt=Decimal(reader["GV_READER_FRACTION_GLYPH_MAX_PT"]),
                proportion_max=Decimal(reader["GV_READER_FRACTION_PROPORTION_MAX"]),
                character_gap_pt=Decimal(reader["GV_READER_FRACTION_CHARACTER_GAP_PT"]),
            ),
        )
        settings = ReadingAgentSettings(
            max_steps=args.max_steps,
            max_vlm_escalations=1,
            sharper_dpi=args.sharper_dpi,
            primary_reader=args.primary,
            escalation_reader=args.escalation,
            label_gap_pt=args.label_gap_pt,
            maximum_label_pt=args.max_label_pt,
        )
        readers: dict[str, BedrockCropReader] = {}
        for name in {*args.pair.split(","), args.primary, args.escalation}:
            config = vision_config_for_extractor(name)
            if config is None:
                raise ScorecardError(f"{name} is not a defined reader with a measured space")
            readers[name] = BedrockCropReader(config, calls_per_minute=CALLS_PER_MINUTE[name])
        first, second = args.pair.split(",")
        keys: list[LoadedKey] = []
        seen: dict[str, Path] = {}
        for directory in args.key:
            crops = load_key(directory)
            for crop in crops:
                if crop.crop_id in seen:
                    raise ScorecardError(
                        f"crop {crop.crop_id} is in both {seen[crop.crop_id]} and {directory}; "
                        "every crop of the keys scored together needs its own id"
                    )
                seen[crop.crop_id] = directory
            key_dpi = key_frame_dpi(
                directory, key_dpi=args.key_dpi, margin_pt=VISION_CROP_CONTEXT_MARGIN_PT
            )
            if args.only:
                wanted = {crop_id.strip() for crop_id in args.only.split(",")}
                crops = tuple(crop for crop in crops if crop.crop_id in wanted)
            case = json.loads((directory / "answer_key.json").read_text(encoding="utf-8"))
            keys.append(
                LoadedKey(
                    crops=crops,
                    key_dpi=key_dpi,
                    pages=build_pages(
                        (directory / case["shop"]).read_bytes(),
                        [crop.page_index for crop in crops],
                        version_id=UUID(case["provenance"]["documents"][0]["document_version_id"]),
                        dpi=args.stage_dpi,
                        geometry=geometry,
                        reach=settings.reach(geometry.glyph_gap_pt),
                    ),
                )
            )
        rates = load_model_rates(args.rates)
    except (ScorecardError, OSError, KeyError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2

    from storage.local import LocalStore

    results = []
    total = sum(len(key.crops) for key in keys)
    with tempfile.TemporaryDirectory() as directory:
        store = LocalStore(root=Path(directory), ticket_secret=b"scorecard crops are never served")
        for key in keys:
            for crop in key.crops:
                result = score_crop(
                    crop,
                    key.pages[crop.page_index],
                    store=store,
                    pair=(readers[first], readers[second]),
                    readers={
                        VlmRole.PRIMARY: readers[args.primary],
                        VlmRole.ESCALATION: readers[args.escalation],
                    },
                    settings=settings,
                    key_dpi=key.key_dpi,
                    margin_pt=VISION_CROP_CONTEXT_MARGIN_PT,
                )
                results.append(result)
                print(f"{len(results)}/{total} {crop.crop_id}", file=sys.stderr, flush=True)

    header = (
        f"## Reading agent scorecard — {datetime.datetime.now().astimezone().date().isoformat()}\n\n"
        f"Pair: {first} + {second}. Agent: primary {args.primary}, escalation {args.escalation}; "
        f"{args.max_steps} steps; sharper look at {args.sharper_dpi} dpi; whole label gathered "
        f"within {args.label_gap_pt} pt, at most {args.max_label_pt} pt. Crops cut as the stage "
        f"cuts them, at {args.stage_dpi} dpi. Reader thresholds: {args.reader_settings}. "
        f"Keys: {', '.join(directory.name for directory in args.key)}."
    )

    def cost(model: str, tokens_in: int, tokens_out: int) -> int:
        micros = call_cost_micros(rates, model, tokens_in, tokens_out)
        if micros is None:
            raise ScorecardError(f"{args.rates} has no price for {model}")
        return micros

    args.output.write_text(render_markdown(results, rates=cost, header=header), encoding="utf-8")
    args.output.with_suffix(".json").write_text(
        json.dumps(results_json(results), indent=1), encoding="utf-8"
    )
    print(f"wrote {args.output} and {args.output.with_suffix('.json')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
