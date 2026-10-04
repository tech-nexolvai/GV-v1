#!/usr/bin/env python3
"""Score the reading agent on a human-read key: the reader pair alone, and with the agent (#757).

Every threshold is stated on the command line or read from a `scripts/demo.sh`-style settings file
— none has a default — and the scorecard says which were used. Paid calls go through the production
adapter, paced under each reader's quota. The per-crop working file holds the client's values: write
it under `data/`, which is never committed.

    python scripts/agent_scorecard.py --key data/goldset/reading-key-2026-09-30 \\
        --reader-settings scripts/demo.sh --stage-dpi 300 --sharper-dpi 450 \\
        --label-gap-pt 4 --max-label-pt 40 --max-steps 6 --budget-usd 0.50 \\
        --output data/goldset/reading-key-2026-09-30/agent_scorecard.md

Give `--key` once per key — one key per drawing (#867) — and every key's crops are scored into one
scorecard, each on its own drawing. A crop id two keys share is refused.

**A pair reader shown an upright, sharper picture (#907)** needs `--sharper-picture-dpi`, the
deployment's `GV_VISION_SHARPER_PICTURE_DPI`; it has no default. **`--budget-usd` is required**: the
run stops before any call once it has spent that much, writes what it scored, and says it stopped.
`--pair-only` scores the reader pair without the agent.
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
    "bedrock-nova-2-lite-taught": 18,
    "bedrock-ministral-3-3b": 60,
    "bedrock-mistral-large-3": 30,
    "bedrock-qwen3-vl-235b": 30,
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
    parser.add_argument("--pair", default="bedrock-qwen3-vl-235b,bedrock-nova-2-lite-taught")
    parser.add_argument("--primary", default="bedrock-nova-2-lite")
    parser.add_argument("--escalation", default="bedrock-mistral-large-3", help="#757 D-A2")
    parser.add_argument("--rates", type=Path, default=Path("deploy/model_rates.us-east-1.json"))
    parser.add_argument("--only", help="comma-separated crop ids, for a dry run")
    parser.add_argument(
        "--sharper-picture-dpi",
        type=int,
        help="GV_VISION_SHARPER_PICTURE_DPI, for a pair reader shown an upright, sharper picture",
    )
    parser.add_argument("--budget-usd", type=Decimal, required=True, help="stop before exceeding")
    parser.add_argument("--pair-only", action="store_true", help="score the pair without the agent")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from app.runs.rates import call_cost_micros, load_model_rates
    from eval.experiments.agent_scorecard import Pacer, SpendCap, SpendCapReached
    from extraction.glyph_bands import FractionBarGeometry
    from extraction.models.nova import (
        INFERENCE_PROFILE_PREFIX,
        ReaderPicture,
        vision_config_for_extractor,
    )
    from scripts.glyph_inventory import read_missing_space, read_reader_settings
    from workflow.reader_pictures import PictureSettings
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
                turned_aspect_min=Decimal(reader["GV_READER_FRACTION_TURNED_ASPECT_MIN"]),
            ),
            # The pages' printed text is read as the stage reads it (#907, #912).
            missing_space=read_missing_space(args.reader_settings),
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
        rates = load_model_rates(args.rates)
        if not args.budget_usd.is_finite() or args.budget_usd <= 0:
            raise ScorecardError("--budget-usd must be more than zero dollars")
        cap = SpendCap(
            cap_micros=int(args.budget_usd * 1_000_000),
            price=lambda model, tokens_in, tokens_out: call_cost_micros(
                rates, model, tokens_in, tokens_out
            ),
        )
        first, second = args.pair.split(",")
        names = (
            {first, second} if args.pair_only else {first, second, args.primary, args.escalation}
        )
        configs = {}
        for name in names:
            config = vision_config_for_extractor(name)
            if config is None:
                raise ScorecardError(f"{name} is not a defined reader with a measured space")
            if rates.rate_for(config.model_id) is None:
                raise ScorecardError(f"{args.rates} has no price for {config.model_id}")
            configs[name] = config
        # **One quota per model**, whichever reader asks it: Nova 2 Lite's two readers share one.
        pacers: dict[str, Pacer] = {}
        readers: dict[str, BedrockCropReader] = {}
        for name, config in sorted(configs.items()):
            model = config.model_id.removeprefix(INFERENCE_PROFILE_PREFIX)
            shared = [other for other, c in configs.items() if c.model_id == config.model_id]
            if model not in pacers:
                pacers[model] = Pacer(min(CALLS_PER_MINUTE[other] for other in shared))
            readers[name] = BedrockCropReader(
                config, calls_per_minute=CALLS_PER_MINUTE[name], pacer=pacers[model], cap=cap
            )
        sharper = sorted(
            name
            for name in (first, second)
            if configs[name].picture is ReaderPicture.UPRIGHT_SHARPER
        )
        pictures = (
            None
            if args.sharper_picture_dpi is None
            else PictureSettings(sharper_dpi=args.sharper_picture_dpi)
        )
        if sharper and pictures is None:
            raise ScorecardError(
                f"{sharper} are shown an upright, sharper picture: state --sharper-picture-dpi"
            )
        if pictures is not None and pictures.sharper_dpi <= args.stage_dpi:
            raise ScorecardError("--sharper-picture-dpi must be above --stage-dpi")
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
    except (ScorecardError, OSError, KeyError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2

    from storage.local import LocalStore

    results = []
    unplaced: dict[str, str] = {}
    stopped: str | None = None
    total = sum(len(key.crops) for key in keys)
    with tempfile.TemporaryDirectory() as directory:
        store = LocalStore(root=Path(directory), ticket_secret=b"scorecard crops are never served")
        for key in keys:
            for crop in key.crops:
                try:
                    result = score_crop(
                        crop,
                        key.pages[crop.page_index],
                        store=store,
                        pair=(readers[first], readers[second]),
                        readers=(
                            {}
                            if args.pair_only
                            else {
                                VlmRole.PRIMARY: readers[args.primary],
                                VlmRole.ESCALATION: readers[args.escalation],
                            }
                        ),
                        settings=settings,
                        key_dpi=key.key_dpi,
                        margin_pt=VISION_CROP_CONTEXT_MARGIN_PT,
                        pictures=pictures,
                        run_agent=not args.pair_only,
                    )
                except SpendCapReached as error:
                    stopped = str(error)
                    break
                except ScorecardError as error:
                    # A crop the stage could not cut — narrower than its own margin, or off its
                    # page — is reported, never scored and never the end of the run.
                    unplaced[crop.crop_id] = str(error)
                    print(f"{crop.crop_id} not scored: {error}", file=sys.stderr, flush=True)
                    continue
                if cap.tripped:
                    # Cut inside the agent's graph, which turned the refusal into an abstention.
                    stopped = "the cap was reached inside the agent's run on this crop"
                    break
                results.append(result)
                print(
                    f"{len(results)}/{total} {crop.crop_id} "
                    f"${Decimal(cap.spent_micros) / 1_000_000}",
                    file=sys.stderr,
                    flush=True,
                )
            if stopped is not None:
                break

    spent = Decimal(cap.spent_micros) / 1_000_000
    header = (
        f"## Reading agent scorecard — {datetime.datetime.now().astimezone().date().isoformat()}\n\n"
        f"Pair: {first} + {second}"
        + (
            " (pair only, no agent). "
            if args.pair_only
            else (
                f". Agent: primary {args.primary}, escalation {args.escalation}; "
                f"{args.max_steps} steps; sharper look at {args.sharper_dpi} dpi; whole label "
                f"gathered within {args.label_gap_pt} pt, at most {args.max_label_pt} pt. "
            )
        )
        + f"Crops cut as the stage cuts them, at {args.stage_dpi} dpi"
        + (
            f"; readers shown the label upright and sharper ({', '.join(sharper)}) see it "
            f"rendered at {pictures.sharper_dpi} dpi"
            if pictures is not None and sharper
            else ""
        )
        + f". Reader thresholds: {args.reader_settings}. "
        f"Keys: {', '.join(directory.name for directory in args.key)}. "
        f"Spent ${spent} in {cap.calls} calls, under a cap of ${args.budget_usd}."
        + (f"\n\n**STOPPED at the cap after {len(results)} crops: {stopped}.**" if stopped else "")
        + (
            f"\n\nNot scored, because the stage could not cut them: {', '.join(sorted(unplaced))}."
            if unplaced
            else ""
        )
    )

    def cost(model: str, tokens_in: int, tokens_out: int) -> int:
        micros = call_cost_micros(rates, model, tokens_in, tokens_out)
        if micros is None:
            raise ScorecardError(f"{args.rates} has no price for {model}")
        return micros

    args.output.write_text(render_markdown(results, rates=cost, header=header), encoding="utf-8")
    # An unplaced crop is in the working file with no reading, so a replay of it sees the key's
    # every crop and that this one was read by nobody.
    rows = results_json(results) + [
        {"crop": crop_id, "pair": [], "not_scored": reason} for crop_id, reason in unplaced.items()
    ]
    args.output.with_suffix(".json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"wrote {args.output} and {args.output.with_suffix('.json')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
