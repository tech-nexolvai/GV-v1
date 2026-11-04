"""`scripts/cache_cost_model.py`: the MODELLED cache writes and reads of a full check under the
`legacy` and `shared` prompt layouts (rebuild plan Step 5). Pure arithmetic; no model call."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import cache_cost_model as model
import call_log_summary as summary_script


def test_legacy_at_the_measured_size_is_the_measured_run() -> None:
    result = model.compare(model.MEASURED_PAGES)
    legacy = result["legacy"]
    # The measured run: $37.43, 5,393k tokens written, cache writes 51.2% of the cost.
    assert abs(Decimal(legacy["cost_usd"]) - Decimal("37.43")) < Decimal("0.05")
    assert legacy["cache_creation_input_tokens"] == sum(
        phase.cache_creation_input_tokens for phase in model.MEASURED
    )
    assert legacy["write_spend_percent"] == Decimal("51.2")


def test_shared_writes_less_and_costs_less_and_says_it_is_modelled() -> None:
    result = model.compare(50)
    assert result["label"].startswith("MODELLED")
    assert result["shared"]["cache_creation_input_tokens"] < (
        result["legacy"]["cache_creation_input_tokens"]
    )
    assert Decimal(result["saved_usd"]) > 0
    assert result["shared"]["write_spend_percent"] < result["legacy"]["write_spend_percent"]


def test_one_off_calls_write_nothing_under_shared() -> None:
    shared = model.rows("shared", 50)
    one_off = {phase.name for phase in model.MEASURED if phase.one_off}
    assert one_off and all(
        line["cache_creation_input_tokens"] == 0 for line in shared if line["phase"] in one_off
    )
    legacy = model.rows("legacy", 50)
    assert any(
        line["cache_creation_input_tokens"] > 0 for line in legacy if line["phase"] in one_off
    )


def test_its_lines_are_call_log_lines_the_summary_reads() -> None:
    lines = model.rows("legacy", 10)
    for line in lines:
        assert set(summary_script.TOKENS) <= set(line)
        assert {"phase", "kind", "model", "cost_usd", "prefix_hash"} <= set(line)
    assert summary_script.summarise(lines)["overall"]["calls"] == len(lines)


def test_a_layout_it_does_not_know_is_refused() -> None:
    with pytest.raises(ValueError):
        model.rows("other", 10)


def test_main_prints_both_layouts_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert model.main(["--pages", "50"]) == 0
    out = capsys.readouterr().out
    assert "MODELLED" in out and "legacy" in out and "shared" in out
    assert model.main(["--pages", "50", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["pages"] == 50 and "assumptions" in parsed
