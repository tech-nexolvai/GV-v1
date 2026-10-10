"""Import earlier AI calls recorded in other local databases into a project's history (#1165).

Earlier reading runs, bake-offs and proofs recorded every model call (model, tokens, cost) in
`model_invocations` of other local databases, so the Usage page never saw them. This reads those
records and adds one `ai_spend_history` row per call for the given project. The Usage page's
"Spend so far" then includes them.

**Each call once.** Most of those databases are copies of each other, and a copy keeps every call's
`id`, so a call is counted once by its id however many copies hold it. Calls this project's reviews
already count (calls in the target database tied to one of the project's drawing sets) are left out;
any other call in the target is imported like the rest, so nothing is invisible.

**Only adds.** The history is append-only, one row per call id: importing again adds the calls not
seen before and changes nothing already there. A later import from other sources, or a source that
cannot be read, never loses an earlier call.

**Only counts.** It reads `id, created_at, model_id, prompt_id, input_tokens, output_tokens,
cost_micros, outcome` (and leaves out reused stored answers, which made no call). Never a prompt, an
answer, a raw reply or anything from a drawing. Sources are opened read-only.

**Priced like the Usage page** (`app.usage_history.charged_cost`): a failed call that used no tokens
costs 0; a call stored as $0 although it used tokens (every call before #754, 2026-09-30) is priced
from `deploy/model_rates.us-east-1.json` and marked "priced later", or unpriced if its model has no
published price. Never $0 for a call that used tokens.

**What a call was for** comes from its prompt id. A model comparison (bake-off) asked the production
prompts, so it is marked per source database: `--purpose-override <database>=bake-off`.

Usage:

    python scripts/import_spend_history.py --target-url <url> --project-id <uuid> \\
        --scan-servers localhost:5433,localhost:5434 \\
        --purpose-override gv_readers_check=bake-off --dry-run
    python scripts/import_spend_history.py --target-url <url> --project-id <uuid> \\
        --source-url <url> --source-url <url>

`--scan-servers` lists every database on each server (with the target URL's user and password) and
skips templates, `postgres`, `hatchet`, test databases (any name containing "test", or starting
`gvtest` or `gvpytest`), any `--skip-db`, and the target itself. Connection passwords are never
printed.
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
from typing import cast, get_args
from uuid import UUID, uuid4

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import URL, make_url

from app.usage_history import Purpose, charged_cost, infer_purpose, infer_route

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
#: Rows sent to the target in one statement.
_BATCH = 500


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
    database: str | None = None
    calls: tuple[CallRecord, ...] = ()
    skipped: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryCall:
    """One call as it goes into `ai_spend_history`."""

    call_id: UUID
    occurred_on: date
    model_id: str
    route: str
    purpose: str
    input_tokens: int
    output_tokens: int
    cost_micros: int | None
    priced_later: bool


@dataclass(slots=True)
class ImportReport:
    sources: list[SourceRead] = field(default_factory=list)
    rows_read: int = 0
    unique_calls: int = 0
    counted_by_project: int = 0
    already_in_history: int = 0
    calls: list[HistoryCall] = field(default_factory=list)
    target_calls: int = 0
    target_calls_in_project: int = 0
    target_has_history_table: bool = False
    written: bool = False

    def totals(self) -> dict[str, object]:
        def tally(into: Counter[str], call: HistoryCall) -> None:
            into["calls"] += 1
            into["cost_micros"] += call.cost_micros or 0
            into["unpriced"] += int(call.cost_micros is None)
            into["priced_later"] += int(call.priced_later)
            into["priced_later_micros"] += (call.cost_micros or 0) if call.priced_later else 0

        overall: Counter[str] = Counter()
        by_model: defaultdict[str, Counter[str]] = defaultdict(Counter)
        by_purpose: defaultdict[str, Counter[str]] = defaultdict(Counter)
        for call in self.calls:
            for bucket in (overall, by_model[call.model_id], by_purpose[call.purpose]):
                tally(bucket, call)

        def shown(values: Counter[str]) -> dict[str, object]:
            return {
                "calls": values["calls"],
                "usd": _usd(values["cost_micros"]),
                "unpriced": values["unpriced"],
                "priced_later": values["priced_later"],
                "priced_later_usd": _usd(values["priced_later_micros"]),
            }

        return {
            "sources_with_calls": sum(1 for source in self.sources if source.calls),
            "sources_read": sum(1 for source in self.sources if source.skipped is None),
            "sources_skipped": sum(1 for source in self.sources if source.skipped is not None),
            "rows_read": self.rows_read,
            "unique_calls": self.unique_calls,
            "counted_by_project_reviews": self.counted_by_project,
            "already_in_history": self.already_in_history,
            "new_calls": len(self.calls),
            **{key: value for key, value in shown(overall).items() if key != "calls"},
            "input_tokens": sum(call.input_tokens for call in self.calls),
            "output_tokens": sum(call.output_tokens for call in self.calls),
            "by_model": {
                model: shown(values)
                for model, values in sorted(
                    by_model.items(), key=lambda item: -item[1]["cost_micros"]
                )
            },
            "by_purpose": {
                purpose: shown(values) for purpose, values in sorted(by_purpose.items())
            },
            "target_calls": self.target_calls,
            "target_calls_not_in_project": self.target_calls - self.target_calls_in_project,
            "target_has_history_table": self.target_has_history_table,
            "written": self.written,
        }


def _usd(micros: int) -> str:
    return format((Decimal(micros) / Decimal(1_000_000)).quantize(Decimal("0.000001")), "f")


def is_test_database(name: str) -> bool:
    """A database the test suites (or a test run) made: never a record of real spend."""
    lowered = name.lower()
    return "test" in lowered or lowered.startswith(("gvtest", "gvpytest"))


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


def parse_purpose_overrides(values: Iterable[str]) -> dict[str, Purpose]:
    """`<database>=<purpose>` pairs; a purpose outside the known set is refused."""
    known = set(get_args(Purpose))
    overrides: dict[str, Purpose] = {}
    for value in values:
        database, _, purpose = value.partition("=")
        if not database.strip() or purpose.strip() not in known:
            raise ValueError(
                f"--purpose-override is <database>=<purpose> with a purpose from "
                f"{', '.join(sorted(known))}; got {value!r}"
            )
        overrides[database.strip()] = cast(Purpose, purpose.strip())
    return overrides


def _display(url: URL) -> str:
    """Where a database is, never with its password."""
    return f"{url.host or 'localhost'}:{url.port or 5432}/{url.database}"


def read_source(url: URL) -> SourceRead:
    """One database's recorded calls, read-only; skipped (with the reason) if it has none."""
    label = _display(url)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            if connection.execute(text("SELECT to_regclass('model_invocations')")).scalar() is None:
                return SourceRead(label, url.database, skipped="no model_invocations table")
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
                return SourceRead(
                    label, url.database, skipped="model_invocations is missing columns"
                )
            cost = "cost_micros" if "cost_micros" in columns else "0::bigint AS cost_micros"
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
        return SourceRead(
            label, url.database, skipped=f"could not be read ({type(error).__name__})"
        )
    finally:
        engine.dispose()
    return SourceRead(
        label,
        url.database,
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


def unique_calls(
    sources: Iterable[SourceRead], overrides: Mapping[str, Purpose] | None = None
) -> dict[UUID, tuple[CallRecord, Purpose | None]]:
    """Each call once, by id (copies hold the same record), with its source's purpose override.

    A call held by any overridden source takes that source's purpose.
    """
    overrides = overrides or {}
    calls: dict[UUID, tuple[CallRecord, Purpose | None]] = {}
    for source in sources:
        override = overrides.get(source.database or "")
        for call in source.calls:
            known = calls.get(call.id)
            if known is None or (known[1] is None and override is not None):
                calls[call.id] = (known[0] if known else call, override)
    return calls


def history_call(call: CallRecord, override: Purpose | None = None) -> HistoryCall:
    """One call as a history row: its UTC day, route, purpose and charged cost."""
    created = call.created_at
    day = (created if created.tzinfo else created.replace(tzinfo=UTC)).astimezone(UTC).date()
    cost = charged_cost(
        call.model_id, call.cost_micros, call.outcome, call.input_tokens, call.output_tokens
    )
    return HistoryCall(
        call_id=call.id,
        occurred_on=day,
        model_id=call.model_id,
        route=infer_route(call.model_id, call.prompt_id),
        purpose=override or infer_purpose(call.prompt_id),
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        cost_micros=cost.micros,
        priced_later=cost.priced_later,
    )


def run_import(
    *,
    target_url: URL,
    project_id: UUID,
    source_urls: Sequence[URL],
    source_label: str = DEFAULT_LABEL,
    purpose_overrides: Mapping[str, Purpose] | None = None,
    dry_run: bool = False,
    reader: Callable[[URL], SourceRead] = read_source,
) -> ImportReport:
    """Read the sources, count each call once, leave out what the project already counts, and add
    the calls the history does not hold yet."""
    from app.api.usage import _invocation_revision
    from app.models import AiSpendHistory, ModelInvocation, Package, PackageRevision, Project

    report = ImportReport()
    for url in source_urls:
        if _same_database(url, target_url) and url.query == target_url.query:
            report.sources.append(
                SourceRead(_display(url), url.database, skipped="the target itself")
            )
            continue
        report.sources.append(reader(url))
    report.rows_read = sum(len(source.calls) for source in report.sources)
    calls = unique_calls(report.sources, purpose_overrides)
    report.unique_calls = len(calls)

    engine = create_engine(target_url)
    try:
        with engine.connect() as connection:
            if dry_run:
                connection.execute(text("SET TRANSACTION READ ONLY"))
            project = connection.execute(select(Project.id).where(Project.id == project_id))
            if project.first() is None:
                raise ValueError(f"project {project_id} is not in the target database")
            report.target_calls = int(
                connection.execute(text("SELECT count(*) FROM model_invocations")).scalar_one()
            )
            revision = _invocation_revision()
            counted = set(
                connection.execute(
                    select(ModelInvocation.id)
                    .join(revision, revision.c.invocation_id == ModelInvocation.id)
                    .join(PackageRevision, PackageRevision.id == revision.c.revision_id)
                    .join(Package, Package.id == PackageRevision.package_id)
                    .where(Package.project_id == project_id)
                ).scalars()
            )
            report.target_calls_in_project = len(counted)
            report.target_has_history_table = (
                connection.execute(text("SELECT to_regclass('ai_spend_history')")).scalar()
                is not None
            )
            if not report.target_has_history_table and not dry_run:
                raise ValueError(
                    "the target database has no ai_spend_history table: migrate it first "
                    "(alembic upgrade head)"
                )
            held: set[UUID] = set()
            if report.target_has_history_table:
                held = set(
                    connection.execute(
                        select(AiSpendHistory.call_id).where(
                            AiSpendHistory.project_id == project_id
                        )
                    ).scalars()
                )
            report.counted_by_project = sum(1 for call_id in calls if call_id in counted)
            report.already_in_history = sum(
                1 for call_id in calls if call_id in held and call_id not in counted
            )
            report.calls = [
                history_call(call, override)
                for call_id, (call, override) in sorted(
                    calls.items(), key=lambda item: (item[1][0].created_at, str(item[0]))
                )
                if call_id not in counted and call_id not in held
            ]
            if dry_run:
                connection.rollback()
                return report
            now = datetime.now(UTC)
            rows = [
                {
                    "id": uuid4(),
                    "created_at": now,
                    "project_id": project_id,
                    "call_id": call.call_id,
                    "occurred_on": call.occurred_on,
                    "model_id": call.model_id,
                    "route": call.route,
                    "purpose": call.purpose,
                    "input_tokens": call.input_tokens,
                    "output_tokens": call.output_tokens,
                    "cost_micros": call.cost_micros,
                    "priced_later": call.priced_later,
                    "source_label": source_label,
                }
                for call in report.calls
            ]
            for start in range(0, len(rows), _BATCH):
                connection.execute(
                    insert(AiSpendHistory)
                    .values(rows[start : start + _BATCH])
                    .on_conflict_do_nothing(index_elements=["project_id", "call_id"])
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
    print(f"Recorded call rows read:          {totals['rows_read']}")
    print(f"Unique calls (each id once):      {totals['unique_calls']}")
    print(f"Counted by this project's reviews: {totals['counted_by_project_reviews']}")
    print(f"Already in the history:           {totals['already_in_history']}")
    print(f"New calls for the history:        {totals['new_calls']}")
    print(f"Their cost (priced calls):        ${totals['usd']}")
    print(
        f"  of which priced later from published rates: {totals['priced_later']} calls, "
        f"${totals['priced_later_usd']}"
    )
    print(f"Calls with no price:              {totals['unpriced']}")
    print(
        f"Target calls not tied to this project (imported like the rest): "
        f"{totals['target_calls_not_in_project']} of {totals['target_calls']}"
    )

    def lines(title: str, groups: object) -> None:
        print(f"\n{title}:")
        assert isinstance(groups, dict)
        for name, values in groups.items():
            later = (
                f", {values['priced_later']} priced later (${values['priced_later_usd']})"
                if values["priced_later"]
                else ""
            )
            print(
                f"  {name}: {values['calls']} calls, ${values['usd']}, "
                f"{values['unpriced']} unpriced{later}"
            )

    lines("By model", totals["by_model"])
    lines("By purpose", totals["by_purpose"])
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
    parser.add_argument(
        "--purpose-override",
        action="append",
        default=[],
        help="<database>=<purpose>: every call from that database has this purpose (repeatable)",
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
    try:
        overrides = parse_purpose_overrides(args.purpose_override)
    except ValueError as error:
        parser.error(str(error))
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
            purpose_overrides=overrides,
            dry_run=args.dry_run,
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    _print_report(report, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
