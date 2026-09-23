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
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from reading_funnel import FUNNEL_SQL, QUERIES, collect, render

#: Columns whose contents are the client's drawing rather than this repository's own vocabulary.
#: `raw_text` is the token printed on the sheet, `polygon` is where on their drawing it sits, and
#: `semantic_guess` would be a claim about what it means. A report selecting any of them could not be
#: pasted into a public issue, which is this script's entire purpose.
CLIENT_DERIVED = ("raw_text", "polygon", "semantic_guess", "source_author", "assembled_context")

ALL_SQL = [*QUERIES, ("funnel", FUNNEL_SQL)]


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

    assert set(report) == {name for name, _ in QUERIES} | {"funnel"}


def test_an_empty_database_reports_zeroes_without_raising(postgres_engine: Engine) -> None:
    """A database with no extraction in it has a funnel; it is all zeroes.

    Raising would make the first run on a new checkout look like a broken script rather than an empty
    one — and the first run is when somebody decides whether to trust the tool.
    """
    report = collect(postgres_engine)

    assert report["funnel"]["found"] == 0
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
