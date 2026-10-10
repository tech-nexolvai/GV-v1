"""Import earlier AI spend recorded in other local databases into a project's history (#1165).

Earlier reading runs, bake-offs and proofs recorded every model call (model, tokens, cost) in
`model_invocations` of other local databases, so the Usage page never saw them. This reads those
records, counts each call once, and writes one `ai_spend_history` row per (UTC day, model, purpose)
for the given project. The Usage page's "Spend so far" then includes them.

**Each call once.** Most of those databases are copies of each other, and a copy keeps every call's
`id`, so a call is counted once by its id however many copies hold it. Calls the target database
already holds are left out: the Usage page counts those already (when tied to the project).

**Only counts.** It reads `id, created_at, model_id, prompt_id, input_tokens, output_tokens,
cost_micros, outcome` (and leaves out reused stored answers, which made no call). Never a prompt, an
answer, a raw reply or anything from a drawing. Sources are opened read-only.

**Safe to run again.** Rows are keyed by `day|model|purpose` per project and updated in place, so a
second run with the same sources writes the same rows.

A failed call that used no tokens was not charged: it counts as a call costing 0, not as unpriced.

Usage:

    python scripts/import_spend_history.py --target-url <url> --project-id <uuid> \\
        --scan-servers localhost:5433,localhost:5434 --dry-run
    python scripts/import_spend_history.py --target-url <url> --project-id <uuid> \\
        --source-url <url> --source-url <url>

`--scan-servers` lists every database on each server (with the target URL's user and password) and
skips templates, `postgres`, `hatchet`, test databases (names starting `gvtest` or `gvpytest`, or
containing `pytest` or `_tests`), any `--skip-db`, and the target itself. Connection passwords are
never printed.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import URL, make_url

from app.usage_history import charged_cost_micros, infer_purpose, infer_route

DEFAULT_LABEL = "Earlier runs on this machine"
#: Databases that never hold this app's records.
SYSTEM_DATABASES = frozenset({"postgres", "hatchet", "template0", "template1"})
#: The only columns read from a source's `model_invocations`.
READ_COLUMNS = (
    "id",
    "created_at",
    "model_id",
    "prompt_id",
    "input_tokens",
    "output_tokens",
    "cost_micros",
    "outcome",
)


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One recorded model call: what it was, and what it used and cost. Nothing else."""

    id: UUID
    created_at: datetime
    model_id: str
    prompt_id: str
    input_tokens: int
    output_tokens: int
    cost_micros: int | None
    outcome: str


@dataclass(frozen=True, slots=True)
class SourceRead:
    """What one source database gave: its calls, or why it gave none."""

    name: str
    calls: tuple[CallRecord, ...] = ()
    skipped: str | None = None


@dataclass(slots=True)
class HistoryRow:
    """One (UTC day, model, purpose) group, ready for `ai_spend_history`."""

    occurred_on: date
    model_id: str
    route: str
    purpose: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0
    unpriced_calls: int = 0

    @property
    def source_key(self) -> str:
        return f"{self.occurred_on.isoformat()}|{self.model_id}|{self.purpose}"


@dataclass(slots=True)
class ImportReport:
    sources: list[SourceRead] = field(default_factory=list)
    rows_read: int = 0
    unique_calls: int = 0
    already_in_target: int = 0
    imported_calls: int = 0
    rows: list[HistoryRow] = field(default_factory=list)
    target_calls: int = 0
    target_calls_in_project: int = 0
    stale_rows: int = 0
    target_has_history_table: bool = False
    written: bool = False

    def totals(self) -> dict[str, object]:
        by_model: defaultdict[str, Counter[str]] = defaultdict(Counter)
        by_purpose: defaultdict[str, Counter[str]] = defaultdict(Counter)
        for row in self.rows:
            for bucket in (by_model[row.model_id], by_purpose[row.purpose]):
                bucket["calls"] += row.calls
                bucket["cost_micros"] += row.cost_micros
                bucket["unpriced_calls"] += row.unpriced_calls
        return {
            "sources_with_calls": sum(1 for source in self.sources if source.calls),
            "sources_read": sum(1 for source in self.sources if source.skipped is None),
            "sources_skipped": sum(1 for source in self.sources if source.skipped is not None),
            "rows_read": self.rows_read,
            "unique_calls": self.unique_calls,
            "already_in_target": self.already_in_target,
            "imported_calls": self.imported_calls,
            "history_rows": len(self.rows),
            "total_usd": _usd(sum(row.cost_micros for row in self.rows)),
            "unpriced_calls": sum(row.unpriced_calls for row in self.rows),
            "input_tokens": sum(row.input_tokens for row in self.rows),
            "output_tokens": sum(row.output_tokens for row in self.rows),
            "by_model": {
                model: {
                    "calls": values["calls"],
                    "usd": _usd(values["cost_micros"]),
                    "unpriced": values["unpriced_calls"],
                }
                for model, values in sorted(
                    by_model.items(), key=lambda item: -item[1]["cost_micros"]
                )
            },
            "by_purpose": {
                purpose: {
                    "calls": values["calls"],
                    "usd": _usd(values["cost_micros"]),
                    "unpriced": values["unpriced_calls"],
                }
                for purpose, values in sorted(by_purpose.items())
            },
            "target_calls": self.target_calls,
            "target_calls_not_in_project": self.target_calls - self.target_calls_in_project,
            "stale_rows_left_as_they_were": self.stale_rows,
            "target_has_history_table": self.target_has_history_table,
            "written": self.written,
        }


def _usd(micros: int) -> str:
    return format((Decimal(micros) / Decimal(1_000_000)).quantize(Decimal("0.000001")), "f")


def is_test_database(name: str) -> bool:
    """A database the test suites make: never a record of real spend."""
    return name.startswith(("gvtest", "gvpytest")) or "pytest" in name or "_tests" in name


def _same_database(left: URL, right: URL) -> bool:
    return (
        (left.host or "localhost") == (right.host or "localhost")
        and (left.port or 5432) == (right.port or 5432)
        and left.database == right.database
    )


def _list_databases(url: URL) -> list[str]:
    engine = create_engine(url.set(database="postgres"))
    try:
        with engine.connect() as connection:
            return [
                str(name)
                for name in connection.execute(
                    text("SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1")
                ).scalars()
            ]
    finally:
        engine.dispose()


def scan_server_urls(
    template: URL,
    servers: Iterable[str],
    *,
    target: URL,
    skip: Iterable[str] = (),
    list_databases: Callable[[URL], list[str]] = _list_databases,
) -> list[URL]:
    """Every database on the servers worth reading, with the template's user and password."""
    skipped = set(skip)
    urls: list[URL] = []
    for server in servers:
        host, _, port = server.strip().rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"a server is host:port, got {server!r}")
        server_url = template.set(host=host, port=int(port), database="postgres")
        for name in list_databases(server_url):
            candidate = server_url.set(database=name)
            if (
                name in SYSTEM_DATABASES
                or name in skipped
                or is_test_database(name)
                or _same_database(candidate, target)
            ):
                continue
            urls.append(candidate)
    return urls


def _display(url: URL) -> str:
    """Where a database is, never with its password."""
    return f"{url.host or 'localhost'}:{url.port or 5432}/{url.database}"


def read_source(url: URL, *, name: str | None = None) -> SourceRead:
    """One database's recorded calls, read-only; skipped (with the reason) if it has none."""
    label = name or _display(url)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            if connection.execute(text("SELECT to_regclass('model_invocations')")).scalar() is None:
                return SourceRead(label, skipped="no model_invocations table")
            columns = set(
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'model_invocations'"
                    )
                ).scalars()
            )
            missing = set(READ_COLUMNS) - columns - {"cost_micros"}
            if missing:
                return SourceRead(label, skipped="model_invocations is missing columns")
            cost = "cost_micros" if "cost_micros" in columns else "NULL::bigint AS cost_micros"
            # A reused stored answer (#1112) made no call; only its marker is looked at.
            reused = (
                " WHERE (reader_question_packet ->> 'reused_from') IS NULL"
                if "reader_question_packet" in columns
                else ""
            )
            rows = connection.execute(
                text(
                    "SELECT id, created_at, model_id, prompt_id, input_tokens, output_tokens, "
                    f"{cost}, outcome FROM model_invocations{reused}"
                )
            ).all()
            connection.rollback()
    except Exception as error:  # noqa: BLE001 - only the error's kind is shown, never its words
        return SourceRead(label, skipped=f"could not be read ({type(error).__name__})")
    finally:
        engine.dispose()
    return SourceRead(
        label,
        calls=tuple(
            CallRecord(
                id=row.id,
                created_at=row.created_at,
                model_id=row.model_id,
                prompt_id=row.prompt_id,
                input_tokens=int(row.input_tokens),
                output_tokens=int(row.output_tokens),
                cost_micros=None if row.cost_micros is None else int(row.cost_micros),
                outcome=row.outcome,
            )
            for row in rows
        ),
    )


def unique_calls(sources: Iterable[SourceRead]) -> dict[UUID, CallRecord]:
    """Each call once, by id: the first source to hold it wins (copies hold the same record)."""
    calls: dict[UUID, CallRecord] = {}
    for source in sources:
        for call in source.calls:
            calls.setdefault(call.id, call)
    return calls


def group_calls(calls: Iterable[CallRecord]) -> list[HistoryRow]:
    """One row per (UTC day, model, purpose), oldest first."""
    rows: dict[tuple[date, str, str], HistoryRow] = {}
    for call in calls:
        created = call.created_at
        day = (created if created.tzinfo else created.replace(tzinfo=UTC)).astimezone(UTC).date()
        purpose = infer_purpose(call.model_id, call.prompt_id)
        row = rows.get((day, call.model_id, purpose))
        if row is None:
            row = rows[(day, call.model_id, purpose)] = HistoryRow(
                occurred_on=day,
                model_id=call.model_id,
                route=infer_route(call.model_id, call.prompt_id),
                purpose=purpose,
            )
        cost = charged_cost_micros(
            call.cost_micros, call.outcome, call.input_tokens, call.output_tokens
        )
        row.calls += 1
        row.input_tokens += call.input_tokens
        row.output_tokens += call.output_tokens
        row.cost_micros += cost or 0
        row.unpriced_calls += int(cost is None)
    return [rows[key] for key in sorted(rows)]


def run_import(
    *,
    target_url: URL,
    project_id: UUID,
    source_urls: Sequence[URL],
    source_label: str = DEFAULT_LABEL,
    dry_run: bool = False,
    reader: Callable[[URL], SourceRead] = read_source,
) -> ImportReport:
    """Read the sources, count each call once, leave out the target's own, and upsert the rows."""
    from app.api.usage import _invocation_revision
    from app.models import AiSpendHistory, ModelInvocation, Package, PackageRevision, Project

    report = ImportReport()
    for url in source_urls:
        if _same_database(url, target_url) and url.query == target_url.query:
            report.sources.append(SourceRead(_display(url), skipped="the target itself"))
            continue
        report.sources.append(reader(url))
    report.rows_read = sum(len(source.calls) for source in report.sources)
    calls = unique_calls(report.sources)
    report.unique_calls = len(calls)

    engine = create_engine(target_url)
    try:
        with engine.connect() as connection:
            if dry_run:
                connection.execute(text("SET TRANSACTION READ ONLY"))
            if (
                connection.execute(select(Project.id).where(Project.id == project_id)).first()
                is None
            ):
                raise ValueError(f"project {project_id} is not in the target database")
            target_ids = set(connection.execute(select(ModelInvocation.id)).scalars())
            report.target_calls = len(target_ids)
            revision = _invocation_revision()
            report.target_calls_in_project = int(
                connection.execute(
                    select(func.count())
                    .select_from(ModelInvocation)
                    .join(revision, revision.c.invocation_id == ModelInvocation.id)
                    .join(PackageRevision, PackageRevision.id == revision.c.revision_id)
                    .join(Package, Package.id == PackageRevision.package_id)
                    .where(Package.project_id == project_id)
                ).scalar_one()
            )
            fresh = [call for call_id, call in calls.items() if call_id not in target_ids]
            report.already_in_target = len(calls) - len(fresh)
            report.imported_calls = len(fresh)
            report.rows = group_calls(fresh)
            report.target_has_history_table = (
                connection.execute(text("SELECT to_regclass('ai_spend_history')")).scalar()
                is not None
            )
            if report.target_has_history_table:
                keys = {row.source_key for row in report.rows}
                existing = connection.execute(
                    select(AiSpendHistory.source_key).where(
                        AiSpendHistory.project_id == project_id,
                        AiSpendHistory.source_label == source_label,
                    )
                ).scalars()
                report.stale_rows = sum(1 for key in existing if key not in keys)
            elif not dry_run:
                raise ValueError(
                    "the target database has no ai_spend_history table: migrate it first "
                    "(alembic upgrade head)"
                )
            if dry_run:
                connection.rollback()
                return report
            now = datetime.now(UTC)
            for row in report.rows:
                values: Mapping[str, object] = {
                    "occurred_on": row.occurred_on,
                    "model_id": row.model_id,
                    "route": row.route,
                    "purpose": row.purpose,
                    "calls": row.calls,
                    "input_tokens": row.input_tokens,
                    "output_tokens": row.output_tokens,
                    "cost_micros": row.cost_micros,
                    "unpriced_calls": row.unpriced_calls,
                    "source_label": source_label,
                }
                statement = insert(AiSpendHistory).values(
                    id=uuid4(),
                    created_at=now,
                    project_id=project_id,
                    source_key=row.source_key,
                    **values,
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=["project_id", "source_key"], set_=dict(values)
                    )
                )
            connection.commit()
            report.written = True
    finally:
        engine.dispose()
    return report


def _print_report(report: ImportReport, *, as_json: bool) -> None:
    totals = report.totals()
    if as_json:
        totals["sources"] = [
            {"name": source.name, "calls": len(source.calls), "skipped": source.skipped}
            for source in report.sources
        ]
        print(json.dumps(totals, indent=2))
        return
    print("Sources:")
    for source in report.sources:
        status = source.skipped or f"{len(source.calls)} recorded calls"
        print(f"  {source.name}: {status}")
    print()
    print(f"Recorded call rows read:        {totals['rows_read']}")
    print(f"Unique calls (each id once):    {totals['unique_calls']}")
    print(f"Already in the target database: {totals['already_in_target']}")
    print(f"Calls for the history:          {totals['imported_calls']}")
    print(f"History rows (day/model/purpose): {totals['history_rows']}")
    print(f"Total cost (priced calls):      ${totals['total_usd']}")
    print(f"Calls with no price:            {totals['unpriced_calls']}")
    print(
        f"Target calls not tied to this project (not on Usage): "
        f"{totals['target_calls_not_in_project']} of {totals['target_calls']}"
    )
    print()
    print("By model:")
    by_model = totals["by_model"]
    assert isinstance(by_model, dict)
    for model, values in by_model.items():
        print(
            f"  {model}: {values['calls']} calls, ${values['usd']}, {values['unpriced']} unpriced"
        )
    print("By purpose:")
    by_purpose = totals["by_purpose"]
    assert isinstance(by_purpose, dict)
    for purpose, values in by_purpose.items():
        print(
            f"  {purpose}: {values['calls']} calls, ${values['usd']}, {values['unpriced']} unpriced"
        )
    if report.stale_rows:
        print(
            f"\n{report.stale_rows} history row(s) from an earlier import are not in this one; "
            "they were left as they were."
        )
    if not report.target_has_history_table:
        print("\nThe target has no ai_spend_history table yet: migrate it before a real import.")
    print("\nWritten." if report.written else "\nDry run: nothing was written.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--target-url", required=True, help="the database to write the history to")
    parser.add_argument("--project-id", required=True, help="the project the history belongs to")
    parser.add_argument(
        "--source-url", action="append", default=[], help="a database to read (repeatable)"
    )
    parser.add_argument(
        "--scan-servers",
        help="host:port list; every database on them is read (except templates, tests, target)",
    )
    parser.add_argument(
        "--skip-db", action="append", default=[], help="a database name to leave out (repeatable)"
    )
    parser.add_argument("--source-label", default=DEFAULT_LABEL, help="short words for the rows")
    parser.add_argument("--dry-run", action="store_true", help="print the totals, write nothing")
    parser.add_argument("--json", action="store_true", help="print the totals as JSON")
    args = parser.parse_args(argv)
    if not args.source_label.strip():
        parser.error("--source-label must say something")
    try:
        project_id = UUID(args.project_id)
    except ValueError:
        parser.error("--project-id must be a UUID")
    target = make_url(args.target_url)
    sources = [make_url(url) for url in args.source_url]
    if args.scan_servers:
        servers = [server for server in args.scan_servers.split(",") if server.strip()]
        sources.extend(scan_server_urls(target, servers, target=target, skip=args.skip_db))
    if not sources:
        parser.error("give --source-url or --scan-servers")
    try:
        report = run_import(
            target_url=target,
            project_id=project_id,
            source_urls=sources,
            source_label=args.source_label.strip(),
            dry_run=args.dry_run,
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    _print_report(report, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
