"""How big a model prompt is and what a call costs, and the page snapshot a prompt carries.

Moved here unchanged on 2026-10-28 from `app/review/assistant/agent.py` (the old review assistant),
because the live chat uses them. `agent.py` imports them back, so its existing importers keep
working until they are pointed here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import ROUND_CEILING, Decimal
from typing import Any, Final

from app.review.assistant.records import ReviewSnapshot
from app.runs.rates import ModelRates, call_cost_micros

#: The longest side of a picture a helper is shown (Anthropic's suggested size, about 2,100
#: tokens for a sheet): helpers look at many pages; the lead's checks keep the full 2576.
HELPER_PICTURE_EDGE_PX: Final = 1568


_PIXELS_PER_TOKEN: Final = 750


_TEXT_BYTES_PER_TOKEN: Final = 2


_REQUEST_OVERHEAD_TOKENS: Final = 500


def _page_snapshot(snapshot: ReviewSnapshot, page: int) -> ReviewSnapshot:
    return snapshot.model_copy(
        update={
            "countertops": tuple(c for c in snapshot.countertops if c.page_number == page),
            "other_checks": tuple(f for f in snapshot.other_checks if page in f.pages),
            "pages_without_countertop": tuple(
                p for p in snapshot.pages_without_countertop if p.page_number == page
            ),
            "rows_not_checked": tuple(
                p for p in snapshot.rows_not_checked if p.page_number == page
            ),
            "review_items": tuple(i for i in snapshot.review_items if i.page_number == page),
        }
    )


#: What a cached prompt token costs against an uncached one (Anthropic's prompt caching prices:
#: a 5-minute cache write is 1.25x, a cache read 0.1x).
_CACHE_WRITE: Final = Decimal("1.25")


_CACHE_READ: Final = Decimal("0.1")


def billed_input_tokens(
    usage: Mapping[str, Any], factors: tuple[Decimal, Decimal] | None = None
) -> int:
    """The prompt as uncached-token equivalents: uncached + 1.25 x cache writes + 0.1 x cache
    reads (or the model's own `factors`, `ModelRate.cache_factors`), so the input price gives what
    the call cost."""
    write_factor, read_factor = factors or (_CACHE_WRITE, _CACHE_READ)
    uncached = int(usage.get("input_tokens") or 0)
    written = int(usage.get("cache_creation_input_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    # Rounded up: Decimal's `//` rounds toward zero, so `-(-x // 1)` would round down.
    return (
        uncached
        + int((written * write_factor).to_integral_value(rounding=ROUND_CEILING))
        + int((read * read_factor).to_integral_value(rounding=ROUND_CEILING))
    )


def _picture_tokens(width: int, height: int) -> int:
    return -(-(width * height) // _PIXELS_PER_TOKEN)


def _strip_images(value: Any) -> Any:
    if isinstance(value, Mapping):
        if value.get("type") == "image":
            return None
        return {key: _strip_images(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_images(item) for item in value]
    return value


def prompt_tokens_upper(body: Mapping[str, Any], pictures: Sequence[tuple[int, int]]) -> int:
    """An upper bound on a request's prompt tokens: text at one token per 2 bytes, each picture at
    width x height / 750."""
    text = json.dumps(_strip_images(dict(body)), ensure_ascii=False, default=str)
    tokens = _REQUEST_OVERHEAD_TOKENS + -(-len(text.encode("utf-8")) // _TEXT_BYTES_PER_TOKEN)
    return tokens + sum(_picture_tokens(w, h) for w, h in pictures)


def estimate_micros(
    body: Mapping[str, Any],
    pictures: Sequence[tuple[int, int]],
    model_id: str,
    rates: ModelRates | None,
    cached_tokens: int = 0,
    previous_tokens: int = 0,
) -> int | None:
    """An upper bound on one turn's cost; `None` when the model has no stated price.

    **Cache-aware.** `cached_tokens` is the previous turn's real prompt length (from its usage),
    which this turn reads from the cache at 0.1x, pictures included; `previous_tokens` is this
    estimate's upper bound for that previous prompt, so only what was added since (this bound
    minus that one) is priced as a cache write (1.25x). Plus the whole output allowance. A missed
    cache costs more than this estimate; the spend then recorded is the real one, so the next
    check sees it."""
    tokens = prompt_tokens_upper(body, pictures)
    added = max(0, tokens - previous_tokens) if cached_tokens else tokens
    billed = billed_input_tokens(
        {"cache_read_input_tokens": cached_tokens, "cache_creation_input_tokens": added}
    )
    return call_cost_micros(rates, model_id, billed, int(body.get("max_tokens", 0)))
