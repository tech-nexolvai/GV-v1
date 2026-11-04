"""`scripts/call_log_summary.py`: an answer's call log added up by phase, kind, model and prompt
prefix (rebuild plan Step 1B). Invented lines only; no model call."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import call_log_summary as summary_script


def _line(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "phase": "helpers",
        "kind": "numbers",
        "model": "anthropic.claude-sonnet-5-5",
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "ephemeral_5m_input_tokens": None,
        "ephemeral_1h_input_tokens": None,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": "0",
        "cache_write_cost_usd": "0",
        "prefix_hash": "a" * 64,
    }
    return {**base, **fields}


LINES = [
    # Two helpers sharing one prefix: the first writes it, the second reads it.
    _line(
        input_tokens=100,
        cache_creation_input_tokens=10_000,
        ephemeral_5m_input_tokens=10_000,
        ephemeral_1h_input_tokens=0,
        output_tokens=500,
        cost_usd="0.045",
        cache_write_cost_usd="0.0375",
    ),
    _line(
        input_tokens=100,
        cache_read_input_tokens=10_000,
        output_tokens=500,
        cost_usd="0.0108",
    ),
    # The advocate on Opus with its own prefix, written once and never read.
    _line(
        phase="advocate",
        kind="advocate",
        model="anthropic.claude-opus-5-5",
        input_tokens=50,
        cache_creation_input_tokens=2000,
        output_tokens=100,
        cost_usd="0.02",
        cache_write_cost_usd="0.0125",
        prefix_hash="b" * 64,
    ),
]


def _write(path: Path, lines: list[dict[str, Any]], extra: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines) + extra, encoding="utf-8")
    return path


def test_totals_by_phase_kind_and_model_with_the_write_share() -> None:
    result = summary_script.summarise(LINES)

    overall = result["overall"]
    assert overall["calls"] == 3
    assert overall["input_tokens"] == 250
    assert overall["cache_creation_input_tokens"] == 12_000
    assert overall["ephemeral_5m_input_tokens"] == 10_000
    assert overall["ephemeral_1h_input_tokens"] == 0
    assert overall["cache_read_input_tokens"] == 10_000
    assert overall["output_tokens"] == 1100
    assert overall["cost_usd"] == Decimal("0.0758")
    # (0.0375 + 0.0125) / 0.0758
    assert overall["write_share_percent"] == Decimal("66.0")

    helpers = result["groups"]["phase"]["helpers"]
    assert helpers["calls"] == 2 and helpers["cost_usd"] == Decimal("0.0558")
    assert helpers["write_share_percent"] == Decimal("67.2")
    assert result["groups"]["kind"]["advocate"]["calls"] == 1
    opus = result["groups"]["model"]["anthropic.claude-opus-5-5"]
    assert opus["write_share_percent"] == Decimal("62.5")


def test_prefixes_count_the_calls_that_read_and_wrote_each() -> None:
    result = summary_script.summarise(LINES)

    assert result["distinct_prefixes"] == 2
    shared = result["prefixes"]["a" * 64]
    assert shared == {"calls": 2, "read": 1, "wrote": 1, "cache_read_input_tokens": 10_000}
    alone = result["prefixes"]["b" * 64]
    assert (alone["calls"], alone["read"], alone["wrote"]) == (1, 0, 1)
    # The most-sent prefix first.
    assert next(iter(result["prefixes"])) == "a" * 64


def test_a_folder_is_read_whole_and_bad_lines_are_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / "chat" / "calls" / "m1.jsonl", LINES[:2])
    _write(tmp_path / "chat" / "calls" / "m2.jsonl", LINES[2:], extra="not json\n[1, 2]\n\n")

    assert summary_script.main([str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "all: 3 calls" in out
    assert "advocate: 1 calls" in out
    assert "Prompt prefixes: 2 distinct" in out
    assert "aaaaaaaaaaaa: 2 calls, 1 read the cache, 1 wrote it" in out
    assert "2 line(s) were not JSON objects and were skipped" in out


def test_json_output_and_an_unpriced_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path / "m.jsonl", [_line(cost_usd=None, cache_write_cost_usd=None)])

    assert summary_script.main([str(path), "--json"]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed["overall"]["calls"] == 1 and printed["overall"]["unpriced"] == 1
    assert printed["overall"]["cost_usd"] == "0"
    assert printed["overall"]["write_share_percent"] is None
    assert printed["skipped_lines"] == 0


def test_nothing_to_read_exits_2(tmp_path: Path) -> None:
    assert summary_script.main([str(tmp_path)]) == 2
