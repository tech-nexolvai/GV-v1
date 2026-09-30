"""What one model call cost, from a stated price file and the tokens the provider counted (#700).

**Every call used to be recorded as costing nothing.** `cost_micros` was written as a literal `0` in
both places that record a model call, so `app/budget/attribution.py` — which reads only that column —
reported that the system cost nothing to run, and a free call and an unpriced one looked the same.

**Prices are stated, never guessed.** A deployment points `GV_MODEL_RATES_FILE` at a JSON price file
that names its own source and the date it was read. Nothing here carries a rate of its own.

**An unknown price is recorded as unknown.** With no price file, or a model the file does not list,
the cost is `None` (`NULL` in the table) — never `0`. A call that used no tokens did cost nothing,
and records `0`: that zero is a fact, not a gap.

**Exact arithmetic.** Rates are decimal strings in the file (a float is refused), multiplied out in
`Decimal`, and rounded half-up once, to whole millionths of the currency, per call.

The price file's shape::

    {"source": "AWS Bedrock public price list, us-east-1, standard on-demand",
     "retrieved": "2026-09-30", "currency": "USD",
     "rates": {"mistral.ministral-3-3b-instruct":
                   {"input_per_1k_tokens": "0.0001", "output_per_1k_tokens": "0.0001"}}}

Source: issue #700. Verification: tests/app/test_model_rates.py.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Final

__all__ = [
    "MODEL_RATES_ENV",
    "ModelRate",
    "ModelRates",
    "call_cost_micros",
    "load_model_rates",
    "rates_from_environment",
]

#: The variable a deployment sets to the path of its price file.
MODEL_RATES_ENV: Final = "GV_MODEL_RATES_FILE"

#: A cross-region inference profile id is the model id with this in front; it is priced as the model.
#: Kept here rather than imported from `extraction.models.nova` so the API can use this module
#: without importing the model layer (`tests/api/test_no_heavy_work.py`).
_PROFILE_PREFIX: Final = "us."

_MICROS: Final = Decimal(1_000_000)
_THOUSAND: Final = Decimal(1_000)


@dataclass(frozen=True, slots=True)
class ModelRate:
    """One model's price per thousand tokens, in the file's currency."""

    input_per_1k_tokens: Decimal
    output_per_1k_tokens: Decimal


@dataclass(frozen=True, slots=True)
class ModelRates:
    """A whole price file: the rates, and where and when they came from."""

    source: str
    retrieved: date
    currency: str
    rates: Mapping[str, ModelRate]

    def rate_for(self, model_id: str) -> ModelRate | None:
        """The rate for a model id, treating its inference-profile id as the same model."""
        return self.rates.get(model_id) or self.rates.get(model_id.removeprefix(_PROFILE_PREFIX))


def _decimal(value: object, *, where: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str):
        raise TypeError(
            f'{where} must be a decimal string such as "0.0001", not {value!r}: a JSON number is '
            "read as a binary float, and a price that went through one is no longer the price"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{where} is not a decimal number: {value!r}") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{where} must be a finite, non-negative price, not {value!r}")
    return parsed


def load_model_rates(path: str | Path) -> ModelRates:
    """Read and check a price file. Refuses anything it cannot read exactly, naming what."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("a price file is a JSON object")
    for field in ("source", "retrieved", "currency", "rates"):
        if field not in raw:
            raise ValueError(f"a price file must state {field!r}")
    source, currency = raw["source"], raw["currency"]
    if not isinstance(source, str) or not source.strip():
        raise ValueError("a price file must say where its prices came from")
    if currency != "USD":
        raise ValueError(
            f"prices must be in USD, not {currency!r}: cost_micros and the budget report are in "
            "USD millionths, and a second currency would be added to the first"
        )
    retrieved = date.fromisoformat(str(raw["retrieved"]))
    if not isinstance(raw["rates"], dict) or not raw["rates"]:
        raise ValueError("a price file must list at least one model")
    rates: dict[str, ModelRate] = {}
    for model_id, entry in raw["rates"].items():
        if not isinstance(entry, dict):
            raise TypeError(f"the rate for {model_id!r} must be an object")
        rates[model_id] = ModelRate(
            input_per_1k_tokens=_decimal(
                entry.get("input_per_1k_tokens"), where=f"{model_id} input_per_1k_tokens"
            ),
            output_per_1k_tokens=_decimal(
                entry.get("output_per_1k_tokens"), where=f"{model_id} output_per_1k_tokens"
            ),
        )
    return ModelRates(
        source=source, retrieved=retrieved, currency=currency, rates=MappingProxyType(rates)
    )


@cache
def _cached(path: str) -> ModelRates:
    return load_model_rates(path)


def rates_from_environment() -> ModelRates | None:
    """The deployment's price file, or `None` when it states none — and then every priced call is
    recorded as unknown. A path that is set but unreadable raises: a typo must not quietly price
    everything as unknown."""
    path = os.environ.get(MODEL_RATES_ENV, "").strip()
    return _cached(path) if path else None


def call_cost_micros(
    rates: ModelRates | None, model_id: str, input_tokens: int, output_tokens: int
) -> int | None:
    """What one call cost, in whole millionths of a US dollar, or `None` when the price is unknown.

    A call that used no tokens cost nothing, whatever the price file says — Bedrock bills tokens, and
    a refused call before any tokens is not billed. That `0` is known, so it is recorded as `0`.
    """
    for name, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if input_tokens == 0 and output_tokens == 0:
        return 0
    rate = None if rates is None else rates.rate_for(model_id)
    if rate is None:
        return None
    usd = (
        Decimal(input_tokens) * rate.input_per_1k_tokens
        + Decimal(output_tokens) * rate.output_per_1k_tokens
    ) / _THOUSAND
    return int((usd * _MICROS).to_integral_value(rounding=ROUND_HALF_UP))
