"""What a recorded AI call was for and which way it went, from its model id and prompt id (#1165).

The Usage page's "Spend so far" counts this project's own calls and earlier calls recorded in other
local databases (`ai_spend_history`, filled by `scripts/import_spend_history.py`). Both are grouped by
model, provider route and purpose, and both are classified here, so the two can never disagree.

**Only what the record says.** A call row holds its model id and prompt id, not the provider it was
sent through. Where the model id names a Bedrock model (`amazon.*`, `qwen.*`, `mistral.*`, a
`us.`-style inference profile, a dated `-v1:0` id), the route is Bedrock. The review assistant has
only ever gone through OpenRouter. The two Claude readers have gone through Anthropic's own API and,
since #1094, through OpenRouter, and their rows do not say which: those are `unknown`, never a guess.

Pure functions, no database and no network.
"""

from __future__ import annotations

import re
from typing import Final, Literal

Route = Literal["bedrock", "openrouter", "anthropic", "unknown"]
Purpose = Literal["reading", "row-choice", "chat", "assistant", "bake-off", "other"]

ROUTES: Final[tuple[Route, ...]] = ("bedrock", "openrouter", "anthropic", "unknown")
PURPOSES: Final[tuple[Purpose, ...]] = (
    "reading",
    "row-choice",
    "chat",
    "assistant",
    "bake-off",
    "other",
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

#: Prompts of the readers that read a drawing's numbers, walls or pairings.
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
#: The bounded-crop reader that ran in production on Nova 2 Lite; every other model asked the same
#: question was a comparison (the 2026-09/10 model bake-offs), not a reading of a set.
_DIMENSION_READER_MODEL: Final = "us.amazon.nova-2-lite-v1:0"


def infer_route(model_id: str, prompt_id: str) -> Route:
    """Which provider route a call went through, as far as its record shows; else `unknown`."""
    if prompt_id.startswith("review-assistant"):
        return "openrouter"
    bare = _PROFILE.sub("", model_id)
    if bare != model_id or bare.startswith(_BEDROCK_FAMILIES) or _BEDROCK_VERSION.search(bare):
        return "bedrock"
    return "unknown"


def infer_purpose(model_id: str, prompt_id: str) -> Purpose:
    """What a call was for, from its prompt id (and, for the bounded-crop reader, its model)."""
    if prompt_id.startswith("review-assistant"):
        return "assistant"
    if prompt_id.startswith("reviewer-chat"):
        return "chat"
    if prompt_id.startswith("slot-row-choice"):
        return "row-choice"
    if prompt_id.startswith("dimension-reader-teaching"):
        return "bake-off"
    if prompt_id.startswith("dimension-reader"):
        return "reading" if model_id == _DIMENSION_READER_MODEL else "bake-off"
    if prompt_id.startswith(_READING_PROMPTS):
        return "reading"
    return "other"


def charged_cost_micros(
    cost_micros: int | None, outcome: str, input_tokens: int, output_tokens: int
) -> int | None:
    """A call's cost in millionths of a dollar, or `None` when no price was recorded.

    A failed call that used no tokens was not charged, so it costs 0 even with no price recorded.
    """
    if cost_micros is None and outcome == "failed" and input_tokens == 0 and output_tokens == 0:
        return 0
    return cost_micros


__all__ = [
    "PURPOSES",
    "ROUTES",
    "Purpose",
    "Route",
    "charged_cost_micros",
    "infer_purpose",
    "infer_route",
]
