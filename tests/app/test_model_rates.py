"""What a model call cost — from a stated price file, exactly, and never a stand-in zero (#700)."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from app.runs import rates as rates_module
from app.runs.rates import (
    MODEL_RATES_ENV,
    call_cost_micros,
    load_model_rates,
    rates_from_environment,
)

REPO = Path(__file__).resolve().parents[2]
SHIPPED = REPO / "deploy" / "model_rates.us-east-1.json"


def _price_file(tmp_path: Path, **overrides: object) -> Path:
    doc: dict[str, object] = {
        "source": "a test price list",
        "retrieved": "2026-09-30",
        "currency": "USD",
        "rates": {
            "vendor.model-a": {"input_per_1k_tokens": "0.0001", "output_per_1k_tokens": "0.0001"},
        },
    }
    doc.update(overrides)
    path = tmp_path / "rates.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_the_shipped_price_file_is_usd_dated_and_covers_every_reader() -> None:
    from extraction.models.nova import VISION_READERS

    shipped = load_model_rates(SHIPPED)

    assert shipped.currency == "USD"
    assert shipped.retrieved == date(2026, 9, 30)
    assert "price list" in shipped.source
    for reader in VISION_READERS:
        if reader.coordinate_measured:
            assert shipped.rate_for(reader.model_id) is not None, reader.model_id


def test_the_cost_is_exact_and_rounded_once() -> None:
    """682 in and 63 out at $0.0001 per 1,000 tokens is $0.0000745 = 74.5 millionths, rounded half-up
    once to 75. No float is involved anywhere."""
    shipped = load_model_rates(SHIPPED)

    assert call_cost_micros(shipped, "mistral.ministral-3-3b-instruct", 682, 63) == 75


def test_an_inference_profile_id_is_priced_as_its_model() -> None:
    """The call that answers is `us.amazon.nova-2-lite-v1:0` (#702); it is the same model."""
    shipped = load_model_rates(SHIPPED)

    assert call_cost_micros(shipped, "us.amazon.nova-2-lite-v1:0", 1666, 84) == 781


def test_an_unpriced_model_is_unknown_never_zero() -> None:
    shipped = load_model_rates(SHIPPED)

    assert call_cost_micros(shipped, "vendor.not-in-the-file", 1000, 50) is None
    assert call_cost_micros(None, "mistral.ministral-3-3b-instruct", 1000, 50) is None


def test_a_call_that_used_no_tokens_cost_nothing_whatever_the_file_says() -> None:
    """A refused call before any tokens is not billed. That zero is a fact, not a gap."""
    assert call_cost_micros(None, "vendor.anything", 0, 0) == 0


@pytest.mark.parametrize("price", [0.0001, 1, None, True])
def test_a_price_that_is_not_a_decimal_string_is_refused(tmp_path: Path, price: object) -> None:
    """A JSON number is read as a binary float, and a price that went through one is not the price."""
    path = _price_file(
        tmp_path,
        rates={"vendor.model-a": {"input_per_1k_tokens": price, "output_per_1k_tokens": "0.0001"}},
    )

    with pytest.raises(TypeError, match="decimal string"):
        load_model_rates(path)


def test_a_negative_price_is_refused(tmp_path: Path) -> None:
    path = _price_file(
        tmp_path,
        rates={"vendor.model-a": {"input_per_1k_tokens": "-1", "output_per_1k_tokens": "0.0001"}},
    )

    with pytest.raises(ValueError, match="non-negative"):
        load_model_rates(path)


def test_a_price_file_in_another_currency_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="USD"):
        load_model_rates(_price_file(tmp_path, currency="INR"))


@pytest.mark.parametrize("field", ["source", "retrieved", "currency", "rates"])
def test_a_price_file_must_state_where_when_and_what(tmp_path: Path, field: str) -> None:
    path = _price_file(tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    del doc[field]
    path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(ValueError, match=field):
        load_model_rates(path)


def test_with_no_price_file_stated_every_cost_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MODEL_RATES_ENV, raising=False)

    assert rates_from_environment() is None


def test_a_stated_price_file_that_is_missing_raises_rather_than_pricing_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A typo in the path must not quietly turn every cost into unknown."""
    rates_module._cached.cache_clear()
    monkeypatch.setenv(MODEL_RATES_ENV, str(tmp_path / "missing.json"))

    with pytest.raises(FileNotFoundError):
        rates_from_environment()


def test_no_writer_records_a_literal_zero_cost() -> None:
    """**The defect #700 fixed, held.** Both writers passed `cost_micros=0` whatever the tokens were,
    and the budget reported a free system. A literal zero in production code is how it comes back.
    """
    offenders = [
        f"{path.relative_to(REPO)}:{number}"
        for folder in ("app", "workflow", "extraction", "eval")
        for path in (REPO / folder).rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if re.search(r"\bcost_micros\s*=\s*0\b", line)
    ]

    assert offenders == []
