"""What a recorded AI call was for, which way it went, and what it cost (#1165).

The Usage page's "Spend so far" counts this project's own calls and earlier calls recorded in other
local databases (`ai_spend_history`, filled by `scripts/import_spend_history.py`). Both are classified
and priced here, so the two can never disagree.

**Route: only what the record says.** A call row holds its model id and prompt id, not the provider
it was sent through. Where the model id names a Bedrock model (`amazon.*`, `qwen.*`, `mistral.*`, a
`us.`-style inference profile, a dated `-v1:0` id), the route is Bedrock. The review assistant has
only ever gone through OpenRouter. The two Claude readers have gone through Anthropic's own API and,
since #1094, through OpenRouter, and their rows do not say which: those are `unknown`, never a guess.

**Purpose: from the prompt id.** A model comparison (bake-off) cannot be told from a call's own
record (the comparisons asked the production prompts), so the importer marks it per source database
(`--purpose-override <db>=bake-off`).

**Cost: never $0 for a call that used tokens.** Until #754 (2026-09-30, migration 0053) every call
was stored with a cost of 0 whatever it used. Since #754 a call that used tokens is stored with its
price or as unknown (`NULL`), never 0, so a stored 0 with tokens can only be one of those earlier
rows, whatever its date or database. It is priced again from the published price file
(`deploy/model_rates.us-east-1.json`, with the recorder's own pricing code) and marked "priced
later"; with no published price for its model it is unpriced. A failed call that used no tokens was
not charged and costs 0.

No database and no network here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final, Literal

from app.runs.rates import ModelRates, call_cost_micros, load_model_rates

Route = Literal["bedrock", "openrouter", "anthropic", "unknown"]
Purpose = Literal["reading", "row-choice", "chat", "assistant", "findings", "bake-off", "other"]

ROUTES: Final[tuple[Route, ...]] = ("bedrock", "openrouter", "anthropic", "unknown")
PURPOSES: Final[tuple[Purpose, ...]] = (
    "reading",
    "row-choice",
    "chat",
    "assistant",
    "findings",
    "bake-off",
    "other",
)

#: The published prices, read with the recorder's own code (`app/runs/rates.py`).
PUBLISHED_RATES_FILE: Final = (
    Path(__file__).resolve().parent.parent / "deploy" / "model_rates.us-east-1.json"
)

#: A Bedrock cross-region inference profile in front of a model id: `us.amazon.nova-pro-v1:0`.
_PROFILE: Final = re.compile(r"^(?:us|eu|apac|global|us-gov)\.")
#: Model families only reachable here through Bedrock, by their Bedrock model ids.
_BEDROCK_FAMILIES: Final = (
    "amazon.",
    "qwen.",
    "mistral.",
    "moonshotai.",
    "meta.",
    "deepseek.",
    "cohere.",
    "ai21.",
)
#: A Bedrock model version suffix: `anthropic.claude-haiku-4-5-20251001-v1:0`.
_BEDROCK_VERSION: Final = re.compile(r"-v\d+:\d+$")

#: Prompts of the readers that read a drawing's numbers, walls or pairings (including the
#: `dimension-reader-teaching` prompt of the Qwen3-VL + Nova 2 Lite pair, #907).
_READING_PROMPTS: Final = (
    "claude-slot-span",
    "claude-counter-break",
    "slot-walls",
    "slot-crop",
    "form-reader",
    "piece-digits",
    "arch-pair",
    "dimension-reader",
)


def infer_route(model_id: str, prompt_id: str) -> Route:
    """Which provider route a call went through, as far as its record shows; else `unknown`."""
    if prompt_id.startswith("review-assistant"):
        return "openrouter"
    bare = _PROFILE.sub("", model_id)
    if bare != model_id or bare.startswith(_BEDROCK_FAMILIES) or _BEDROCK_VERSION.search(bare):
        return "bedrock"
    return "unknown"


def infer_purpose(prompt_id: str) -> Purpose:
    """What a call was for, from its prompt id. Model comparisons are marked by source instead."""
    if prompt_id.startswith("review-assistant"):
        return "assistant"
    if prompt_id.startswith("reviewer-chat"):
        return "chat"
    if prompt_id.startswith("slot-row-choice"):
        return "row-choice"
    if prompt_id.startswith("findings-composer"):
        return "findings"
    if prompt_id.startswith(_READING_PROMPTS):
        return "reading"
    return "other"


@cache
def published_rates() -> ModelRates | None:
    """The published price file, or `None` when it cannot be read (then nothing is priced later)."""
    try:
        return load_model_rates(PUBLISHED_RATES_FILE)
    except (OSError, ValueError, TypeError):
        return None


@dataclass(frozen=True, slots=True)
class ChargedCost:
    """A call's cost in millionths of a dollar (`None`: no price), and whether it was priced later
    from the published rates because its own record predates real costs (#754)."""

    micros: int | None
    priced_later: bool = False


def charged_cost(
    model_id: str,
    cost_micros: int | None,
    outcome: str,
    input_tokens: int,
    output_tokens: int,
    *,
    rates: ModelRates | None = None,
) -> ChargedCost:
    """What a recorded call cost, counted the same way everywhere (see the module docstring).

    - No recorded price: unpriced, except a failed call that used no tokens (not charged: 0).
    - A stored 0 with tokens (a pre-#754 row): priced from `rates` (the published file when not
      given) and marked `priced_later`; unpriced if its model has no published rate.
    - Otherwise the recorded cost.
    """
    used_tokens = input_tokens > 0 or output_tokens > 0
    if cost_micros is None:
        return ChargedCost(0 if outcome == "failed" and not used_tokens else None)
    if cost_micros == 0 and used_tokens:
        source = published_rates() if rates is None else rates
        micros = call_cost_micros(source, model_id, input_tokens, output_tokens)
        return ChargedCost(micros, priced_later=micros is not None)
    return ChargedCost(cost_micros)


__all__ = [
    "PUBLISHED_RATES_FILE",
    "PURPOSES",
    "ROUTES",
    "ChargedCost",
    "Purpose",
    "Route",
    "charged_cost",
    "infer_purpose",
    "infer_route",
    "published_rates",
]
