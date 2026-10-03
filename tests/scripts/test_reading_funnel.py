"""The funnel report counts rows, never reads a client dimension, and survives an empty database.

**What is deliberately not tested here, and why.** There is no test that seeds candidates and checks
the counts. Reaching `observation_candidates` means satisfying four enforced foreign keys —
projects → packages → documents → source_artifacts → document_versions → pages, plus task_runs for
the extraction run — which is forty lines of setup unrelated to this script's subject and one more
thing to repair whenever any of those schemas move. The property that actually breaks is *"a column
was renamed and the query is now invalid"*, and running every statement against the real migrated
schema catches that. The counts against real data are demonstrated on issue #651 instead, which is
where a before/after comparison belongs anyway.

Source: issue #651.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from reading_funnel import (
    FUNNEL_SQL,
    QUERIES,
    RUN_SPAN_SQL,
    QuotaConfig,
    collect,
    parse_quota_config,
    render,
    render_markdown,
)

#: Columns whose contents are the client's drawing rather than this repository's own vocabulary.
#: `raw_text` is the token printed on the sheet, `polygon` is where on their drawing it sits, and
#: `semantic_guess` would be a claim about what it means. A report selecting any of them could not be
#: pasted into a public issue, which is this script's entire purpose.
CLIENT_DERIVED = ("raw_text", "polygon", "semantic_guess", "source_author", "assembled_context")

ALL_SQL = [*QUERIES, ("funnel", FUNNEL_SQL), ("run_span", RUN_SPAN_SQL)]


def _identifiers(sql: str) -> set[str]:
    """Every bare identifier the statement mentions, lowercased.

    Deliberately coarse — it matches words anywhere rather than parsing a select list. A precise
    parser would miss a column smuggled into a `WHERE` or a `substring(...)`, which is exactly where
    one would arrive by accident.
    """
    return set(re.findall(r"[a-z_][a-z0-9_]*", sql.lower()))


@pytest.mark.parametrize("name,sql", ALL_SQL)
def test_no_query_reads_a_client_derived_column(name: str, sql: str) -> None:
    """**The safety property, asserted rather than promised in a docstring.**"""
    leaked = sorted(column for column in CLIENT_DERIVED if column in _identifiers(sql))

    assert not leaked, f"{name} names client-derived column(s): {leaked}"


@pytest.mark.parametrize("name,sql", ALL_SQL)
def test_the_dimension_is_only_ever_counted(name: str, sql: str) -> None:
    """`value_numerator` may be counted and never selected.

    Counting it asks *whether* a candidate parsed; selecting it asks *what to*, and the second is
    the client's dimension. This is the one column the denylist above cannot simply forbid, because
    the funnel's second row is exactly `count(value_numerator)`.
    """
    # The optional `alias.` is why this is a regex rather than a prefix check: the queries join, so
    # the column is written `count(oc.value_numerator)` and a bare `endswith("count(")` reads the
    # alias as a violation. Counting both and comparing states the rule exactly — every mention is
    # inside a `count()` — rather than relaxing it.
    counted = re.compile(r"count\(\s*(?:[a-z_][a-z0-9_]*\.)?value_numerator\s*\)", re.IGNORECASE)

    assert len(counted.findall(sql)) == len(
        re.findall(r"value_numerator", sql, re.IGNORECASE)
    ), f"{name} mentions value_numerator outside count(): only counting it is permitted"


def test_every_query_is_valid_against_the_migrated_schema(postgres_engine: Engine) -> None:
    """Each statement runs on a real schema, so a renamed column fails here rather than in a demo.

    This is the failure this module exists to catch: the queries are strings, so nothing else in the
    build would notice `corroboration_lane` being renamed until somebody ran the script in front of
    an audience.
    """
    report = collect(postgres_engine)

    assert set(report) == {name for name, _ in QUERIES} | {"funnel", "run_span"}


def test_an_empty_database_reports_zeroes_without_raising(postgres_engine: Engine) -> None:
    """A database with no extraction in it has a funnel; it is all zeroes.

    Raising would make the first run on a new checkout look like a broken script rather than an empty
    one — and the first run is when somebody decides whether to trust the tool.
    """
    report = collect(postgres_engine)

    assert report["funnel"]["found"] == 0
    assert report["funnel"]["read_in_parts"] == 0
    assert report["funnel"]["passes"] == 0
    for name, _ in QUERIES:
        assert report[name] == [], f"{name} should be empty on a fresh database"


def test_an_empty_funnel_states_no_rate_it_cannot_compute(postgres_engine: Engine) -> None:
    """Zero of zero prints a dash, not `0.0%`.

    Zero-of-zero is not a zero survival rate. Printing `0.0%` would claim a measurement nobody took,
    which on a *reading accuracy* report is the worst available rounding error.
    """
    rendered = render(collect(postgres_engine))

    assert "READING FUNNEL" in rendered
    assert "0.0%" not in rendered
    assert "—" in rendered


def test_the_headline_reports_survival_rates_against_what_was_found() -> None:
    """The percentages divide by fragments found, not by the previous row.

    Per-row survival would flatter the result: 20 of 35 associations reads as 57%, while 20 of 876
    fragments — the honest denominator, and the one that says how much of the sheet we actually
    read — is 2.3%.
    """
    report: dict[str, Any] = {
        "funnel": {
            "found": 1000,
            "with_a_value": 100,
            "associated": 50,
            "sealed": 20,
            "findings": 9,
            "passes": 0,
        }
    }

    rendered = render(report)

    assert "10.0%" in rendered  # 100 of 1000, not of itself
    assert "5.0%" in rendered  # 50 of 1000, not 50 of 100
    assert "2.0%" in rendered  # 20 of 1000, not 20 of 50


def test_render_tolerates_a_report_missing_a_section() -> None:
    """A partial report still prints.

    `collect` always fills every key, so this only bites a caller assembling the dict by hand — but a
    renderer that raised would turn a migration-lagged database into a crash instead of a gap.
    """
    rendered = render(
        {
            "funnel": {
                "found": 1,
                "with_a_value": 1,
                "associated": 0,
                "sealed": 0,
                "findings": 0,
                "passes": 0,
            }
        }
    )

    assert "(no rows)" in rendered


def test_render_prints_rejected_model_calls_by_reason() -> None:
    """Input: rejection breakdown. Outcome: visible reason. Why: counts without cause hide failures."""

    rendered = render(
        {
            "funnel": {
                "found": 0,
                "with_a_value": 0,
                "associated": 0,
                "sealed": 0,
                "findings": 0,
                "passes": 0,
            },
            "model_rejections_by_reason": [
                {
                    "bucket": "schema_validation_failed",
                    "status": "amazon.nova-2-lite-v1:0",
                    "rows": 74,
                    "input_tokens": 12000,
                    "output_tokens": 0,
                    "cost_micros": 91,
                }
            ],
        }
    )

    assert "MODEL REJECTIONS BY REASON" in rendered
    assert "schema_validation_failed" in rendered
    assert "rows=74" in rendered


def _markdown_report() -> dict[str, Any]:
    return {
        "funnel": {
            "found": 1000,
            "with_a_value": 100,
            "associated": 50,
            "sealed": 2,
            "findings": 18,
            "passes": 0,
        },
        "run_span": {"wall_minutes": Decimal(10)},
        "candidates_by_extractor": [
            {
                "bucket": "bedrock-nova-2-lite",
                "found": 20,
                "with_a_value": 5,
                "with_confidence": 0,
            },
            {
                "bucket": "rapidocr",
                "found": 80,
                "with_a_value": 0,
                "with_confidence": 80,
            },
        ],
        "model_invocations": [
            {
                "bucket": "amazon.nova-2-lite-v1:0",
                "status": "ok",
                "rows": 60,
                "input_tokens": 600,
                "output_tokens": 30,
                "cost_micros": 0,
                "latency_ms": 120000,
            },
            {
                "bucket": "us.amazon.nova-2-lite-v1:0",
                "status": "rejected",
                "rows": 140,
                "input_tokens": 1400,
                "output_tokens": 0,
                "cost_micros": 0,
                "latency_ms": 240000,
            },
            {
                "bucket": "amazon.nova-2-lite-v1:0",
                "status": "failed",
                "rows": 100,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_micros": 0,
                "latency_ms": 60000,
            },
        ],
    }


def test_markdown_report_has_the_report_shape_and_refuses_accuracy() -> None:
    """Markdown is the checked-in report shape, not a silent accuracy score."""
    rendered = render_markdown(
        _markdown_report(),
        quota_config=QuotaConfig(requests_per_minute={"nova-2-lite": Decimal(20)}),
        measured_date=date(2026, 9, 30),
    )

    assert rendered.startswith("# What the reading layer actually did — measured, 2026-09-30")
    assert "## The run" in rendered
    assert "## The funnel" in rendered
    assert "## What each route contributed" in rendered
    assert "## What the model calls cost" in rendered
    assert "### Per reader" in rendered
    assert "## Calls/min against the applied quota" in rendered
    assert "## What this measurement is not" in rendered
    assert "not an accuracy measurement" in rendered
    assert "No client dimension appears" in rendered


def test_markdown_reports_model_time_by_outcome() -> None:
    """Outcome split includes ok/rejected/failed latency, so waste is visible."""
    rendered = render_markdown(
        _markdown_report(),
        quota_config=QuotaConfig(requests_per_minute={"nova-2-lite": Decimal(20)}),
        measured_date=date(2026, 9, 30),
    )

    assert "| `ok` | 60 | 20.0% | 600 | 2.0 min |" in rendered
    assert "| `rejected` | 140 | 46.7% | 1,400 | 4.0 min |" in rendered
    assert "| `failed` | 100 | 33.3% | 0 | 1.0 min |" in rendered


def test_markdown_reports_calls_per_minute_against_configured_quota() -> None:
    """The quota comparison uses supplied config instead of a baked-in service quota."""
    rendered = render_markdown(
        _markdown_report(),
        quota_config=QuotaConfig(requests_per_minute={"nova-2-lite": Decimal(20)}),
        measured_date=date(2026, 9, 30),
    )

    assert "| `nova-2-lite` | 30.0 | 20 | 150.0% |" in rendered


def test_markdown_says_when_a_reader_quota_is_not_configured() -> None:
    """Missing quota input abstains from comparison instead of inventing a ceiling."""
    rendered = render_markdown(
        _markdown_report(),
        quota_config=QuotaConfig(requests_per_minute={}),
        measured_date=date(2026, 9, 30),
    )

    assert "| `nova-2-lite` | — | not configured | — |" in rendered
    assert "Quota comparison is incomplete" in rendered


def test_quota_config_rejects_missing_or_invalid_numbers() -> None:
    """Quota values are configuration and must be positive numeric facts."""

    assert parse_quota_config('{"nova-2-lite":"20"}').requests_per_minute == {
        "nova-2-lite": Decimal(20)
    }
    with pytest.raises(TypeError, match="JSON object"):
        parse_quota_config("[]")
    with pytest.raises(ValueError, match="greater than zero"):
        parse_quota_config('{"nova-2-lite":0}')
    with pytest.raises(TypeError, match="positive number"):
        parse_quota_config('{"nova-2-lite":true}')


def test_stacked_fractions_read_in_parts_have_their_own_line() -> None:
    """**Every one is a reading a person must still tick** (#848), so the count is shown on its own
    rather than folded into what was found; a report put together without it shows a dash."""
    report: dict[str, Any] = {
        "funnel": {
            "found": 10,
            "with_a_value": 4,
            "associated": 0,
            "sealed": 0,
            "read_in_parts": 4,
            "findings": 0,
            "passes": 0,
        }
    }

    for rendered in (
        render(report),
        render_markdown(report, quota_config=QuotaConfig(requests_per_minute={})),
    ):
        (line,) = [line for line in rendered.splitlines() if "read in parts" in line]
        assert line.split() == ["stacked", "fractions", "read", "in", "parts", "4"]
    del report["funnel"]["read_in_parts"]
    (line,) = [line for line in render(report).splitlines() if "read in parts" in line]
    assert line.split()[-1] == "—"


def test_the_line_counts_the_route_by_the_name_it_records_under() -> None:
    """Written out in the script so it needs no extraction import; held to the route's own name."""
    from reading_funnel import FRACTION_PARTS_EXTRACTOR as COUNTED

    from extraction.fraction_parts import FRACTION_PARTS_EXTRACTOR

    assert COUNTED == FRACTION_PARTS_EXTRACTOR
    assert f"'{FRACTION_PARTS_EXTRACTOR}'" in FUNNEL_SQL
