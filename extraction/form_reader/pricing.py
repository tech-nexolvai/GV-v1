"""Fail-closed price coverage checks for the configured form-reader pair."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class RateLookup(Protocol):
    def rate_for(self, model_id: str) -> object | None: ...


def require_priced_readers(reader_ids: Sequence[str], rates: RateLookup | None) -> None:
    """Refuse to start any paid form-reader call if its exact model has no stated rate."""
    missing = [
        model_id for model_id in reader_ids if rates is None or rates.rate_for(model_id) is None
    ]
    if missing:
        raise ValueError(
            "form-reader calls are disabled because these reader models have no stated price: "
            + ", ".join(missing)
        )
