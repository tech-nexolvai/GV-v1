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
    ... scripts/reading_funnel.py --markdown      # commit-ready report

Request quotas for the markdown quota table are configuration, not code. Supply them as JSON:

    GV_READING_FUNNEL_REQUEST_QUOTAS='{"nova-2-lite":"<applied-rpm>"}' \
      ... scripts/reading_funnel.py --markdown

Source: issue #651. Verification: `tests/scripts/test_reading_funnel.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

#: Truncation width for a refusal sentence. The reasons are code-authored templates that embed a
#: count ("none of the 12 dimension line(s)…"), so grouping on the whole string would split one
#: cause across as many rows as there were lines on the page. The prefix is the cause.
REASON_WIDTH: Final = 52

REQUEST_QUOTAS_ENV: Final = "GV_READING_FUNNEL_REQUEST_QUOTAS"
INFERENCE_PROFILE_PREFIX: Final = "us."

READER_DEFINITIONS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("nova-pro", "amazon.nova-pro-v1:0", "bedrock-nova-pro", "nova-pro"),
    ("nova-2-lite", "amazon.nova-2-lite-v1:0", "bedrock-nova-2-lite", "nova-2-lite"),
    (
        "ministral-3-3b",
        "mistral.ministral-3-3b-instruct",
        "bedrock-ministral-3-3b",
        "ministral",
    ),
    (
        "claude-haiku-4-5",
        "anthropic.claude-haiku-4-5-20251001-v1:0",
        "bedrock-claude-haiku-4-5",
        "claude-haiku-4-5",
    ),
)

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
               count(oc.value_numerator) AS with_a_value,
               count(oc.confidence) AS with_confidence
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
               coalesce(sum(mi.cost_micros), 0) AS cost_micros,
               coalesce(sum(mi.latency_ms), 0) AS latency_ms
        FROM model_invocations mi
        GROUP BY 1, 2
        ORDER BY count(*) DESC
        """,
    ),
    (
        "model_rejections_by_reason",
        """
        SELECT coalesce(mi.rejection_reason, '(unknown)') AS bucket,
               mi.model_id AS status,
               count(*) AS rows,
               coalesce(sum(mi.input_tokens), 0) AS input_tokens,
               coalesce(sum(mi.output_tokens), 0) AS output_tokens,
               coalesce(sum(mi.cost_micros), 0) AS cost_micros,
               coalesce(sum(mi.latency_ms), 0) AS latency_ms
        FROM model_invocations mi
        WHERE mi.outcome = 'rejected'
        GROUP BY 1, 2
        ORDER BY count(*) DESC
        """,
    ),
)

#: The route that reads a stacked fraction piece by piece (#848), named as
#: `extraction.fraction_parts.FRACTION_PARTS_EXTRACTOR` names it. Written out rather than imported so
#: this script stays free of the extraction stack; a test holds the two to the same string.
FRACTION_PARTS_EXTRACTOR: Final = "extraction.fraction_parts"

#: The headline the rest is evidence for. Separate from `QUERIES` because it is one row, not a
#: breakdown, and because it is the line a person actually reads.
FUNNEL_SQL: Final = f"""
SELECT
  (SELECT count(*) FROM observation_candidates)                                  AS found,
  (SELECT count(value_numerator) FROM observation_candidates)                    AS with_a_value,
  (SELECT count(*) FROM observation_associations WHERE refusal_reason IS NULL)   AS associated,
  (SELECT count(*) FROM canonical_observations)                                  AS sealed,
  (SELECT count(*) FROM observation_candidates oc
     JOIN extraction_runs er ON er.id = oc.extraction_run_id
    WHERE er.extractor = '{FRACTION_PARTS_EXTRACTOR}')                  AS read_in_parts,
  (SELECT count(*) FROM findings)                                                AS findings,
  (SELECT count(*) FROM findings WHERE outcome = 'PASS')                         AS passes
"""

RUN_SPAN_SQL: Final = """
SELECT count(*) AS model_calls,
       min(created_at) AS first_model_call_at,
       max(created_at) AS last_model_call_at,
       coalesce(
         extract(epoch FROM max(created_at) - min(created_at)) / 60.0,
         0
       ) AS wall_minutes
FROM model_invocations
"""


@dataclass(frozen=True, slots=True)
class QuotaConfig:
    """Operator-supplied request quotas, keyed by reader label/model/extractor."""

    requests_per_minute: dict[str, Decimal]


def collect(engine: Engine) -> dict[str, Any]:
    """Every count this script reports, from one connection.

    Returns plain Python types so the caller can render or serialise without a live session — a
    report that needed the database open to be printed could not be attached to anything.
    """
    report: dict[str, Any] = {}
    with engine.connect() as connection:
        row = connection.execute(text(FUNNEL_SQL)).mappings().one()
        report["funnel"] = dict(row)
        row = connection.execute(text(RUN_SPAN_SQL)).mappings().one()
        report["run_span"] = dict(row)
        for name, sql in QUERIES:
            rows = connection.execute(text(sql)).mappings().all()
            report[name] = [dict(item) for item in rows]
    return report


def parse_quota_config(raw: str) -> QuotaConfig:
    """Parse configured request quotas without inventing operational defaults."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{REQUEST_QUOTAS_ENV} must be a JSON object") from error
    if not isinstance(payload, dict):
        raise TypeError(f"{REQUEST_QUOTAS_ENV} must be a JSON object")

    quotas: dict[str, Decimal] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{REQUEST_QUOTAS_ENV} keys must be non-empty strings")
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise TypeError(f"{REQUEST_QUOTAS_ENV}[{key!r}] must be a positive number")
        try:
            quota = Decimal(str(value))
        except InvalidOperation as error:
            raise ValueError(f"{REQUEST_QUOTAS_ENV}[{key!r}] must be a positive number") from error
        if quota <= 0:
            raise ValueError(f"{REQUEST_QUOTAS_ENV}[{key!r}] must be greater than zero")
        quotas[key.strip()] = quota
    return QuotaConfig(requests_per_minute=quotas)


def quota_config_from_environment(environ: dict[str, str] | None = None) -> QuotaConfig:
    """Read request quotas from the process configuration."""
    source = os.environ if environ is None else environ
    raw = source.get(REQUEST_QUOTAS_ENV, "").strip()
    if not raw:
        return QuotaConfig(requests_per_minute={})
    return parse_quota_config(raw)


def _percent(part: int, whole: int) -> str:
    """A survival rate, or a dash when there is nothing to divide by.

    A dash rather than `0.0%`: an empty database has no survival rate, and printing one would read
    as a measured result rather than an absent one.
    """
    if whole <= 0:
        return "—"
    return f"{100 * part / whole:.1f}%"


def _read_in_parts(funnel: dict[str, Any]) -> str:
    """The stacked fractions read piece by piece (#848): their own line, because every one is a
    reading a person must still tick, never evidence. A count, not a share of what was found, and a
    dash where the report was put together without it."""
    value = funnel.get("read_in_parts")
    shown = "—" if value is None else str(int(value))
    return f"  {'stacked fractions read in parts':<34}{shown:>8}"


def _minutes(milliseconds: int | Decimal) -> Decimal:
    return Decimal(str(milliseconds)) / Decimal(60000)


def _format_minutes(minutes: Decimal) -> str:
    return f"{minutes:.1f} min"


def _format_int(value: Any) -> str:
    return f"{int(value):,}"


def _format_decimal(value: Decimal, places: int = 1) -> str:
    return f"{value:.{places}f}"


def _normal_model_id(model_id: str) -> str:
    return model_id.removeprefix(INFERENCE_PROFILE_PREFIX)


def _reader_for_model(model_id: str) -> tuple[str, str, str, str]:
    normal = _normal_model_id(model_id)
    for key, known_model, extractor, label in READER_DEFINITIONS:
        if normal == known_model:
            return key, known_model, extractor, label
    return normal, normal, "", normal


def _display_for_extractor(extractor: str) -> str:
    for _key, _known_model, known_extractor, label in READER_DEFINITIONS:
        if extractor == known_extractor:
            return label
    return extractor.removeprefix("bedrock-")


def _quota_for_reader(
    quota_config: QuotaConfig, *, key: str, model_id: str, extractor: str, label: str
) -> Decimal | None:
    for candidate in (label, key, model_id, _normal_model_id(model_id), extractor):
        quota = quota_config.requests_per_minute.get(candidate)
        if quota is not None:
            return quota
    return None


def _outcome_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}
    for row in report.get("model_invocations", ()):
        outcome = str(row.get("status") or "(unknown)")
        bucket = outcomes.setdefault(
            outcome,
            {
                "outcome": outcome,
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_micros": 0,
                "latency_ms": 0,
            },
        )
        bucket["calls"] += int(row.get("rows") or 0)
        bucket["input_tokens"] += int(row.get("input_tokens") or 0)
        bucket["output_tokens"] += int(row.get("output_tokens") or 0)
        bucket["cost_micros"] += int(row.get("cost_micros") or 0)
        bucket["latency_ms"] += int(row.get("latency_ms") or 0)
    order = {"ok": 0, "rejected": 1, "failed": 2}
    return sorted(outcomes.values(), key=lambda item: order.get(str(item["outcome"]), 99))


def _reader_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    readers: dict[str, dict[str, Any]] = {}
    for row in report.get("model_invocations", ()):
        model_id = str(row.get("bucket") or "")
        key, normal_model, extractor, label = _reader_for_model(model_id)
        item = readers.setdefault(
            label,
            {
                "label": label,
                "key": key,
                "model_id": normal_model,
                "extractor": extractor,
                "calls": 0,
                "latency_ms": 0,
                "accepted": 0,
                "values": 0,
            },
        )
        item["calls"] += int(row.get("rows") or 0)
        item["latency_ms"] += int(row.get("latency_ms") or 0)

    for row in report.get("candidates_by_extractor", ()):
        label = _display_for_extractor(str(row.get("bucket") or ""))
        if label not in readers:
            continue
        readers[label]["accepted"] = int(row.get("found") or 0)
        readers[label]["values"] = int(row.get("with_a_value") or 0)

    return sorted(readers.values(), key=lambda item: str(item["label"]))


def _wall_minutes(report: dict[str, Any]) -> Decimal:
    span = report.get("run_span") or {}
    try:
        return Decimal(str(span.get("wall_minutes") or 0))
    except InvalidOperation:
        return Decimal(0)


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
        _read_in_parts(funnel),
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
    lines.extend(
        [
            "",
            "NOT AN ACCURACY MEASUREMENT",
            "-" * 58,
            "  This report has no ground truth. It reports counts, rates, latency and cost only.",
        ]
    )
    return "\n".join(lines)


def render_markdown(
    report: dict[str, Any],
    *,
    quota_config: QuotaConfig,
    measured_date: date | None = None,
) -> str:
    """The report as markdown, framed for a checked-in before/after comparison."""
    dated = measured_date or datetime.now(UTC).date()
    funnel = report["funnel"]
    found = int(funnel["found"])
    wall_minutes = _wall_minutes(report)
    outcome_rows = _outcome_rows(report)
    total_calls = sum(int(row["calls"]) for row in outcome_rows)
    total_tokens = sum(int(row["input_tokens"]) for row in outcome_rows)
    total_latency = sum(int(row["latency_ms"]) for row in outcome_rows)

    lines = [
        f"# What the reading layer actually did — measured, {dated.isoformat()}",
        "",
        (
            "**This is the reading funnel, recorded.** Every figure below was read from the "
            "database; nothing here is estimated and nothing here says whether a reading is correct."
        ),
        "",
        (
            "**No client dimension appears in this file.** The drawings stay under `data/` and the "
            "values stay in the database; what is recorded here is counts, rates, latency and costs."
        ),
        "",
        "## The run",
        "",
        "| | |",
        "|---|---|",
        "| Source | the configured `GV_DATABASE_URL` database |",
        f"| Generated | {dated.isoformat()} |",
        f"| Model calls | {_format_int(total_calls)} |",
        f"| Wall clock | {_format_decimal(wall_minutes)} minutes, first-to-last model invocation |",
        "",
        "## The funnel",
        "",
        "```",
        f"  {'text fragments found':<34}{found:>8}",
        (
            f"  {'...that parse to a number':<34}{int(funnel['with_a_value']):>8}   "
            f"{_percent(int(funnel['with_a_value']), found)}"
        ),
        (
            f"  {'...attached to a dimension line':<34}{int(funnel['associated']):>8}   "
            f"{_percent(int(funnel['associated']), found)}"
        ),
        (
            f"  {'...sealed as usable evidence':<34}{int(funnel['sealed']):>8}   "
            f"{_percent(int(funnel['sealed']), found)}"
        ),
        _read_in_parts(funnel),
        "",
        f"  {'findings':<34}{int(funnel['findings']):>8}",
        f"  {'...of which PASS':<34}{int(funnel['passes']):>8}",
        "```",
        "",
        (
            "The percentages are survival rates through the reading funnel. They are not accuracy, "
            "because this tool has no ground truth."
        ),
        "",
        "## What each route contributed",
        "",
        "| Route | Candidates | With a value | Carries a confidence |",
        "|---|---:|---:|---:|",
    ]

    for row in report.get("candidates_by_extractor", ()):
        route = str(row.get("bucket") or "")
        lines.append(
            "| "
            f"`{route}` | {_format_int(row.get('found') or 0)} | "
            f"{_format_int(row.get('with_a_value') or 0)} | "
            f"{_format_int(row.get('with_confidence') or 0)} |"
        )

    lines.extend(
        [
            "",
            "## What the model calls cost",
            "",
            "| Outcome | Calls | Share | Input tokens | Model time |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in outcome_rows:
        calls = int(row["calls"])
        lines.append(
            "| "
            f"`{row['outcome']}` | {_format_int(calls)} | {_percent(calls, total_calls)} | "
            f"{_format_int(row['input_tokens'])} | "
            f"{_format_minutes(_minutes(row['latency_ms']))} |"
        )
    lines.append(
        "| **total** | "
        f"**{_format_int(total_calls)}** |  | **{_format_int(total_tokens)}** | "
        f"**{_format_minutes(_minutes(total_latency))}** |"
    )

    lines.extend(
        [
            "",
            "### Per reader",
            "",
            "| Reader | Calls | Model time | Answers accepted | Values |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    reader_rows = _reader_rows(report)
    for row in reader_rows:
        lines.append(
            "| "
            f"`{row['label']}` | {_format_int(row['calls'])} | "
            f"{_format_minutes(_minutes(row['latency_ms']))} | "
            f"{_format_int(row['accepted'])} | {_format_int(row['values'])} |"
        )

    lines.extend(
        [
            "",
            "## Calls/min against the applied quota",
            "",
            "| Reader | Calls/min | Quota | % of quota |",
            "|---|---:|---:|---:|",
        ]
    )
    missing: list[str] = []
    for row in reader_rows:
        quota = _quota_for_reader(
            quota_config,
            key=str(row["key"]),
            model_id=str(row["model_id"]),
            extractor=str(row["extractor"]),
            label=str(row["label"]),
        )
        if quota is None:
            missing.append(str(row["label"]))
            lines.append(f"| `{row['label']}` | — | not configured | — |")
            continue
        calls_per_minute = (
            Decimal(int(row["calls"])) / wall_minutes if wall_minutes > 0 else Decimal(0)
        )
        quota_share = Decimal(100) * calls_per_minute / quota
        lines.append(
            "| "
            f"`{row['label']}` | {_format_decimal(calls_per_minute)} | "
            f"{_format_decimal(quota, 0)} | {_format_decimal(quota_share)}% |"
        )
    if missing:
        lines.extend(
            [
                "",
                f"Quota comparison is incomplete: `{REQUEST_QUOTAS_ENV}` did not include "
                + ", ".join(f"`{reader}`" for reader in missing)
                + ".",
            ]
        )

    lines.extend(
        [
            "",
            "## What this measurement is not",
            "",
            (
                "- **It is not an accuracy measurement.** Nothing here says a reading is correct; "
                "the tool has no ground truth to score against (#666)."
            ),
            (
                "- **It is not a client-data extract.** This report carries counts, rates, latency "
                "and costs only, never drawing dimensions."
            ),
            "",
            "## Reproducing it",
            "",
            "```bash",
            "scripts/demo.sh",
            "# upload the package through the API, then:",
            "python scripts/drain_outbox.py",
            f'{REQUEST_QUOTAS_ENV}=\'{{"nova-2-lite":"<applied-rpm>"}}\' \\',
            "  GV_DATABASE_URL=... python scripts/reading_funnel.py --markdown",
            "```",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--database-url",
        help="overrides GV_DATABASE_URL; the application setting is used when absent",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="emit a dated markdown report, ready to commit",
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        help="measurement date for --markdown, YYYY-MM-DD; defaults to today",
    )
    args = parser.parse_args(argv)

    if args.json and args.markdown:
        parser.error("--json and --markdown are mutually exclusive")

    url = args.database_url
    if url is None:
        from app.config import Settings

        url = Settings().database_url  # type: ignore[call-arg]

    engine = create_engine(url)
    try:
        report = collect(engine)
    finally:
        engine.dispose()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    elif args.markdown:
        try:
            quota_config = quota_config_from_environment()
        except (TypeError, ValueError) as error:
            parser.error(str(error))
        print(
            render_markdown(
                report,
                quota_config=quota_config,
                measured_date=args.date,
            )
        )
    else:
        print(render(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main(sys.argv[1:]))
