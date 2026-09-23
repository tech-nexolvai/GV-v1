"""What the reading layer actually produced, as counts you can paste into an issue.

**Why this exists.** The funnel from "text found on a drawing" to "a reading a rule can use" is the
number that decides whether this product works, and until now it was reconstructed by hand with ad-hoc
SQL every time anybody asked. Two people asking the same question got different answers, because they
grouped differently. This is one query, so a before/after comparison is a comparison.

**It measures and never improves.** Nothing here changes a threshold, a prompt or a gate. That
separation is the point: a baseline taken with the same run that altered the thing being measured is
not a baseline.

**Its output is safe to paste in public.** Every column selected is either a count, a code-authored
enum (`extractor`, `outcome`, `corroboration_lane`) or a refusal sentence this repository wrote. No
`raw_text`, no `polygon`, no dimension value is read — which is what lets the numbers go in a GitHub
issue while the drawings stay under `data/`. `tests/scripts/test_reading_funnel.py` asserts that by
scanning the SQL rather than trusting this paragraph.

Usage:

    GV_DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5433/gv .venv/bin/python scripts/reading_funnel.py
    ... scripts/reading_funnel.py --json          # same numbers, for a diff

Source: issue #651. Verification: `tests/scripts/test_reading_funnel.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

#: Truncation width for a refusal sentence. The reasons are code-authored templates that embed a
#: count ("none of the 12 dimension line(s)…"), so grouping on the whole string would split one
#: cause across as many rows as there were lines on the page. The prefix is the cause.
REASON_WIDTH: Final = 52

#: Every query this script runs. Kept as data, in one place, because the safety property — that no
#: client-derived column is ever selected — is asserted by a test that reads this tuple. A query
#: added anywhere else would not be checked.
QUERIES: Final[tuple[tuple[str, str], ...]] = (
    (
        "candidates_by_document_kind",
        """
        SELECT d.kind AS bucket,
               count(oc.id) AS found,
               count(oc.value_numerator) AS with_a_value
        FROM documents d
        JOIN document_versions dv ON dv.document_id = d.id
        LEFT JOIN observation_candidates oc ON oc.document_version_id = dv.id
        GROUP BY d.kind
        ORDER BY d.kind
        """,
    ),
    (
        "candidates_by_extractor",
        """
        SELECT er.extractor AS bucket,
               count(oc.id) AS found,
               count(oc.value_numerator) AS with_a_value
        FROM extraction_runs er
        LEFT JOIN observation_candidates oc ON oc.extraction_run_id = er.id
        GROUP BY er.extractor
        ORDER BY er.extractor
        """,
    ),
    (
        "corroboration_lanes",
        """
        SELECT coalesce(oc.corroboration_lane, '(none)') AS bucket,
               coalesce(oc.corroboration_status, '(none)') AS status,
               count(*) AS rows
        FROM observation_candidates oc
        GROUP BY 1, 2
        ORDER BY count(*) DESC
        """,
    ),
    (
        "associations",
        f"""
        SELECT CASE
                 WHEN oa.refusal_reason IS NULL THEN 'ACCEPTED'
                 ELSE substring(oa.refusal_reason FROM 1 FOR {REASON_WIDTH})
               END AS bucket,
               count(*) AS rows
        FROM observation_associations oa
        GROUP BY 1
        ORDER BY count(*) DESC
        """,
    ),
    (
        "sealed_observations",
        """
        SELECT co.document_role AS bucket,
               co.status AS status,
               count(*) AS rows
        FROM canonical_observations co
        GROUP BY 1, 2
        ORDER BY count(*) DESC
        """,
    ),
    (
        "findings_by_outcome",
        """
        SELECT f.outcome AS bucket, count(*) AS rows
        FROM findings f
        GROUP BY 1
        ORDER BY count(*) DESC
        """,
    ),
    (
        "model_invocations",
        """
        SELECT mi.model_id AS bucket,
               mi.outcome AS status,
               count(*) AS rows,
               coalesce(sum(mi.input_tokens), 0) AS input_tokens,
               coalesce(sum(mi.output_tokens), 0) AS output_tokens,
               coalesce(sum(mi.cost_micros), 0) AS cost_micros
        FROM model_invocations mi
        GROUP BY 1, 2
        ORDER BY count(*) DESC
        """,
    ),
)

#: The headline the rest is evidence for. Separate from `QUERIES` because it is one row, not a
#: breakdown, and because it is the line a person actually reads.
FUNNEL_SQL: Final = """
SELECT
  (SELECT count(*) FROM observation_candidates)                                  AS found,
  (SELECT count(value_numerator) FROM observation_candidates)                    AS with_a_value,
  (SELECT count(*) FROM observation_associations WHERE refusal_reason IS NULL)   AS associated,
  (SELECT count(*) FROM canonical_observations)                                  AS sealed,
  (SELECT count(*) FROM findings)                                                AS findings,
  (SELECT count(*) FROM findings WHERE outcome = 'PASS')                         AS passes
"""


def collect(engine: Engine) -> dict[str, Any]:
    """Every count this script reports, from one connection.

    Returns plain Python types so the caller can render or serialise without a live session — a
    report that needed the database open to be printed could not be attached to anything.
    """
    report: dict[str, Any] = {}
    with engine.connect() as connection:
        row = connection.execute(text(FUNNEL_SQL)).mappings().one()
        report["funnel"] = dict(row)
        for name, sql in QUERIES:
            rows = connection.execute(text(sql)).mappings().all()
            report[name] = [dict(item) for item in rows]
    return report


def _percent(part: int, whole: int) -> str:
    """A survival rate, or a dash when there is nothing to divide by.

    A dash rather than `0.0%`: an empty database has no survival rate, and printing one would read
    as a measured result rather than an absent one.
    """
    if whole <= 0:
        return "—"
    return f"{100 * part / whole:.1f}%"


def render(report: dict[str, Any]) -> str:
    """The report as text, headline first."""
    funnel = report["funnel"]
    found = int(funnel["found"])

    def step(label: str, key: str) -> str:
        """One funnel row: the count, and what share of what was found survived to it."""
        value = int(funnel[key])
        return f"  {label:<34}{value:>8}   {_percent(value, found)}"

    lines = [
        "READING FUNNEL",
        "=" * 58,
        f"  {'text fragments found':<34}{found:>8}",
        step("...that parse to a number", "with_a_value"),
        step("...attached to a dimension line", "associated"),
        step("...sealed as usable evidence", "sealed"),
        "",
        f"  {'findings':<34}{int(funnel['findings']):>8}",
        f"  {'...of which PASS':<34}{int(funnel['passes']):>8}",
    ]
    for name, _ in QUERIES:
        rows: Sequence[dict[str, Any]] = report.get(name, ())
        lines.extend(["", name.replace("_", " ").upper(), "-" * 58])
        if not rows:
            lines.append("  (no rows)")
            continue
        for item in rows:
            bucket = str(item.get("bucket") or "(none)")
            rest = "  ".join(f"{key}={value}" for key, value in item.items() if key != "bucket")
            lines.append(f"  {bucket:<{REASON_WIDTH + 2}}{rest}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--database-url",
        help="overrides GV_DATABASE_URL; the application setting is used when absent",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    url = args.database_url
    if url is None:
        from app.config import Settings

        url = Settings().database_url  # type: ignore[call-arg]

    engine = create_engine(url)
    try:
        report = collect(engine)
    finally:
        engine.dispose()

    print(json.dumps(report, indent=2, default=str) if args.json else render(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main(sys.argv[1:]))
