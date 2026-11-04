#!/usr/bin/env python3
"""A MODELLED estimate of a full check's prompt-cache writes and reads under the `legacy` and the
`shared` prompt layouts (rebuild plan Step 5). No model call, no cost, nothing written.

It is a model, not a measurement. It starts from the one big paid run that was measured (Portage,
2026-11-04: 40 vendor pages, $37.43, per phase from the worker log; the research note
`08_cache_usage_live_log.md`), scales it to the pages asked for, and changes only what the
`shared` layout changes, each change with its own stated assumption (`Assumptions`). It writes the
calls of both layouts as lines in the call log's own format (`app/review/assistant/call_log.py`:
phase, kind, model, the four token counts, `cost_usd`, `cache_write_cost_usd`, `prefix_hash`)
and adds them up with the call log's own summary (`scripts/call_log_summary.py`), so a real log
from a paid run can be put beside it the same way.

What it changes for `shared`:

- **one-off calls** (second reads, third reads, the blind string readings and other one-off
  Opus calls): no breakpoint on the newest block, so what each wrote to the cache is sent as
  plain input instead (1 time the input price, not 1.25);
- **the page reviewer's warm-up race**: the share of pages whose first call wrote the whole
  shared prefix again (1 of 40 measured) reads it instead;
- **the specialists' prefix**: legacy, each Sonnet specialist's first call wrote its kind's
  prefix (on OpenRouter the warm-up never ran, and the agents of a wave start together); shared,
  one warm-up per wave writes the one shared prefix (larger: the union of the knowledge) and
  every specialist reads it;
- **wrap-ups**: legacy, a forced wrap-up (`tool_choice: none`) wrote the whole history again;
  shared, it reads it and writes only its new turn;
- **the page pictures** two helpers on the same pages shared: the second reads them;
- **effort**: the Sonnet specialists all run at the highest effort their kinds used (medium), so
  the page helpers that ran at low write more output (a cost, counted).

    python scripts/cache_cost_model.py              # 50 pages
    python scripts/cache_cost_model.py --pages 40 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from call_log_summary import render, summarise

RATES_FILE = ROOT / "deploy" / "model_rates.us-east-1.json"
MODELS = {
    "opus": "anthropic.claude-opus-5-5",
    "sonnet": "anthropic.claude-sonnet-5-5",
    "haiku": "anthropic.claude-haiku-5-5",
}
#: The measured run's pages.
MEASURED_PAGES = 40


@dataclass(frozen=True, slots=True)
class Phase:
    """One phase of the measured run: its model, calls and token totals (the worker log's
    roll-up; tokens there are rounded to the nearest thousand except the page reviewers')."""

    name: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    one_off: bool = False
    """Its calls are one-off calls (one message, never sent again)."""


#: The measured run (Portage, 2026-11-04, $37.43), per phase: research note 08, "Key question 2".
MEASURED: tuple[Phase, ...] = (
    Phase("page_review", "opus", 295, 746, 269_375, 23_181_843, 1_770_367),
    Phase("gap_closers", "sonnet", 238, 562, 161_000, 7_290_000, 1_065_000),
    Phase("whole_look", "opus", 58, 192, 79_000, 683_000, 546_000),
    Phase("helpers", "sonnet", 195, 498, 102_000, 5_696_000, 650_000),
    Phase("other", "opus", 43, 172, 40_000, 49_000, 469_000, one_off=True),
    Phase("third_reads", "sonnet", 49, 196, 2_000, 6_000, 190_000, one_off=True),
    Phase("lead_review", "opus", 11, 26, 8_000, 325_000, 37_000),
    Phase("escalation", "sonnet", 31, 70, 9_000, 406_000, 55_000),
    Phase("provers", "sonnet", 10, 22, 2_000, 129_000, 19_000),
    Phase("second_reads", "haiku", 148, 592, 5_000, 0, 566_000, one_off=True),
    Phase("prewarm", "sonnet", 2, 8, 10, 13_000, 22_000),
    Phase("finish_checks", "haiku", 2, 8, 723, 0, 2_000, one_off=True),
    Phase("planning", "haiku", 1, 4, 86, 0, 2_000, one_off=True),
)


@dataclass(frozen=True, slots=True)
class Assumptions:
    """What the model assumes where the measured run cannot say (per 40 pages unless named);
    each is a number the call log of one paid run will measure."""

    reviewer_prefix_tokens: int = 43_548
    """The page reviewers' shared prefix (measured: each page's first call read exactly this)."""
    reviewer_misses_per_40_pages: float = 1.0
    """First calls that wrote that prefix again (measured: page 5)."""
    sonnet_specialists_per_40_pages: int = 60
    """Sonnet specialists started (page helpers, tag readers, gap closers, verifiers)."""
    legacy_prefix_tokens: int = 13_000
    """A kind's own prefix, tools and system (the audit: about 13k)."""
    shared_prefix_tokens: int = 16_000
    """The one shared prefix (the union of the knowledge files: larger)."""
    waves_per_check: int = 3
    """Times the specialists start together (first phase, gap closers, verifiers): one warm-up
    each under `shared`."""
    legacy_cold_share: float = 1.0
    """The share of specialists whose first call wrote their prefix under `legacy` (OpenRouter:
    no warm-up, a wave starts together)."""
    wrap_up_share: float = 0.25
    """The share of Sonnet specialists that end with a forced wrap-up call."""
    wrap_up_history_tokens: int = 35_000
    """Their history at that call (Sonnet's measured average prompt is 29.5k a call)."""
    wrap_up_new_tokens: int = 1_000
    """What a wrap-up adds to the history (its note)."""
    picture_pairs_per_40_pages: int = 8
    """Page helpers on the same pages as another (numbers and drawn things)."""
    picture_group_tokens: int = 11_500
    """The overview pictures of one helper's pages (5 pages, about 2.3k tokens each)."""
    helper_output_rise: float = 0.30
    """More output from the page helpers that ran at low effort and now run at medium."""


def _rates() -> dict[str, dict[str, Decimal]]:
    data = json.loads(RATES_FILE.read_text(encoding="utf-8"))["rates"]
    return {short: {k: Decimal(v) for k, v in data[key].items()} for short, key in MODELS.items()}


def _line(
    phase: str,
    model: str,
    rates: Mapping[str, Mapping[str, Decimal]],
    *,
    prefix: str,
    input_tokens: int = 0,
    write: int = 0,
    read: int = 0,
    output: int = 0,
) -> dict[str, Any]:
    """One modelled call as a call-log line (`call_log.records`' fields that the summary reads)."""
    rate = rates[model]
    per = Decimal(1000)
    cost = (
        Decimal(input_tokens) * rate["input_per_1k_tokens"]
        + Decimal(write) * rate["cache_write_per_1k_tokens"]
        + Decimal(read) * rate["cache_read_per_1k_tokens"]
        + Decimal(output) * rate["output_per_1k_tokens"]
    ) / per
    write_cost = Decimal(write) * (rate["cache_write_per_1k_tokens"] - rate["input_per_1k_tokens"])
    return {
        "phase": phase,
        "kind": phase,
        "model": MODELS[model],
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": write,
        "ephemeral_5m_input_tokens": write,
        "ephemeral_1h_input_tokens": 0,
        "cache_read_input_tokens": read,
        "output_tokens": output,
        "cost_usd": f"{cost:f}",
        "cache_write_cost_usd": f"{write_cost / per:f}",
        "prefix_hash": prefix,
    }


def _spread(total: float, count: int) -> list[int]:
    """`total` split over `count` calls in whole tokens (the remainder on the first)."""
    if count <= 0:
        return []
    each = int(total) // count
    return [int(total) - each * (count - 1), *([each] * (count - 1))]


def rows(layout: str, pages: int, assume: Assumptions | None = None) -> list[dict[str, Any]]:
    """The modelled calls of one check of `pages` vendor pages under `layout`, as call-log lines:
    the measured phases scaled to the pages, then (under `shared`) each change the layout makes."""
    if layout not in ("legacy", "shared"):
        raise ValueError(f"layout is legacy or shared, not {layout!r}")
    a = assume or Assumptions()
    rates = _rates()
    scale = pages / MEASURED_PAGES
    shared = layout == "shared"
    out: list[dict[str, Any]] = []
    for phase in MEASURED:
        calls = max(1, round(phase.calls * scale))
        write = phase.cache_creation_input_tokens * scale
        read = phase.cache_read_input_tokens * scale
        fresh = phase.input_tokens * scale
        output = phase.output_tokens * scale
        if shared and phase.one_off:
            # No breakpoint on a one-off call's newest block: its write is plain input.
            fresh, write = fresh + write, 0
        if shared and phase.name == "page_review":
            # The warm-up race fixed: a missed first call reads the shared prefix.
            missed = a.reviewer_misses_per_40_pages * scale * a.reviewer_prefix_tokens
            write -= missed
            read += missed
        if shared and phase.name == "helpers":
            output *= 1 + a.helper_output_rise
        for n, (i, w, r, o) in enumerate(
            zip(
                _spread(fresh, calls),
                _spread(max(0.0, write), calls),
                _spread(read, calls),
                _spread(output, calls),
                strict=True,
            )
        ):
            prefix = f"{layout}:{phase.name}:{n if phase.one_off else 0}"
            out.append(
                _line(
                    phase.name,
                    phase.model,
                    rates,
                    prefix=prefix,
                    input_tokens=i,
                    write=w,
                    read=r,
                    output=o,
                )
            )
    if shared:
        out += _shared_changes(a, scale, rates)
    return out


def _shared_changes(
    a: Assumptions, scale: float, rates: Mapping[str, Mapping[str, Decimal]]
) -> list[dict[str, Any]]:
    """The Sonnet specialists' changes under `shared`, as correction lines: what `legacy` wrote
    and `shared` reads instead (a negative write, a positive read), and the warm-ups. (A
    correction line is counted as one call by the summary: the call counts of `shared` are not
    modelled.)"""
    agents = a.sonnet_specialists_per_40_pages * scale
    lines: list[dict[str, Any]] = []
    # Each specialist's own prefix write (legacy) becomes a read of the one shared prefix.
    legacy_written = agents * a.legacy_cold_share * a.legacy_prefix_tokens
    lines.append(
        _line(
            "specialist_prefix",
            "sonnet",
            rates,
            prefix="shared:specialists",
            write=-round(legacy_written),
            read=round(agents * a.shared_prefix_tokens),
        )
    )
    for _wave in range(a.waves_per_check):
        lines.append(
            _line(
                "prewarm",
                "sonnet",
                rates,
                prefix="shared:specialists",
                write=a.shared_prefix_tokens,
                output=16,
            )
        )
    # A wrap-up reads its history instead of writing it again.
    wraps = agents * a.wrap_up_share
    lines.append(
        _line(
            "wrap_up",
            "sonnet",
            rates,
            prefix="shared:wrap_up",
            write=-round(wraps * (a.wrap_up_history_tokens - a.wrap_up_new_tokens)),
            read=round(wraps * a.wrap_up_history_tokens),
        )
    )
    # The second helper on the same pages reads their pictures.
    pairs = a.picture_pairs_per_40_pages * scale
    lines.append(
        _line(
            "page_pictures",
            "sonnet",
            rates,
            prefix="shared:pictures",
            write=-round(pairs * a.picture_group_tokens),
            read=round(pairs * a.picture_group_tokens),
        )
    )
    return lines


def write_spend(lines: Sequence[Mapping[str, Any]]) -> Decimal:
    """What the cache writes cost at their full price (the research note's "51% of the bill";
    the summary's `cache_write_cost_usd` is only what they cost above plain input)."""
    rates = _rates()
    short = {key: name for name, key in MODELS.items()}
    return sum(
        (
            Decimal(int(line["cache_creation_input_tokens"]))
            * rates[short[line["model"]]]["cache_write_per_1k_tokens"]
            / 1000
            for line in lines
        ),
        Decimal(0),
    )


def compare(pages: int, assume: Assumptions | None = None) -> dict[str, Any]:
    """Both layouts' totals (the call log summary's), their cache writes' full price and share
    of the cost, and what `shared` saves."""
    legacy_lines = rows("legacy", pages, assume)
    shared_lines = rows("shared", pages, assume)
    legacy = summarise(legacy_lines)
    shared = summarise(shared_lines)
    for totals, lines in ((legacy, legacy_lines), (shared, shared_lines)):
        spend = write_spend(lines)
        totals["overall"]["write_spend_usd"] = spend
        totals["overall"]["write_spend_percent"] = (
            spend * 100 / Decimal(totals["overall"]["cost_usd"])
        ).quantize(Decimal("0.1"))
    saved = Decimal(legacy["overall"]["cost_usd"]) - Decimal(shared["overall"]["cost_usd"])
    return {
        "label": "MODELLED from the 2026-11-04 measured run; not a measurement",
        "pages": pages,
        "assumptions": asdict(assume or Assumptions()),
        "legacy": legacy["overall"],
        "shared": shared["overall"],
        "saved_usd": f"{saved:.2f}",
        "saved_percent": f"{saved * 100 / Decimal(legacy['overall']['cost_usd']):.1f}",
        "legacy_report": render(legacy),
        "shared_report": render(shared),
    }


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return f"{value:f}"
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pages", type=int, default=50)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.pages <= 0:
        parser.error("--pages must be above 0")
    result = compare(args.pages)
    if args.json:
        print(
            json.dumps(
                {k: _plain(v) for k, v in result.items() if not k.endswith("_report")}, indent=2
            )
        )
        return 0
    legacy, shared = result["legacy"], result["shared"]
    print(f"{result['label']}: one full check of {args.pages} vendor pages")
    for name, totals in (("legacy", legacy), ("shared", shared)):
        print(
            f"  {name:7} cost ${Decimal(totals['cost_usd']):.2f}  "
            f"cache writes {totals['cache_creation_input_tokens']:,}  "
            f"cache reads {totals['cache_read_input_tokens']:,}  "
            f"writes cost ${totals['write_spend_usd']:.2f} "
            f"({totals['write_spend_percent']}% of the cost)"
        )
    print(f"  saved   ${result['saved_usd']} ({result['saved_percent']}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
