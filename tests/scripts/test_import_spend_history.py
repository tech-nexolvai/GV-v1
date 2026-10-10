"""The spend-history importer (#1165): each call once, never the target's own, safe to re-run.

Two synthetic "source databases" are two schemas holding a bare `model_invocations` table; the
target is the migrated test schema. All values are synthetic.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import AiSpendHistory, ModelInvocation, Package, PackageRevision, Project
from scripts.import_spend_history import (
    is_test_database,
    main,
    run_import,
    scan_server_urls,
)
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

DAY1 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
DAY2 = datetime(2026, 1, 6, 23, 30, tzinfo=UTC)


@pytest.fixture
def target(postgres_engine: Engine) -> Iterator[Engine]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    yield postgres_engine


@pytest.fixture
def project_id(target: Engine) -> UUID:
    with session_factory(target)() as session:
        project = Project(name="Synthetic project")
        session.add(project)
        session.commit()
        return project.id


@pytest.fixture
def sources(target: Engine) -> Iterator[tuple[URL, URL]]:
    """Two source schemas, each with a bare `model_invocations` like an older database's."""
    names = (f"spend_src_a_{uuid4().hex[:8]}", f"spend_src_b_{uuid4().hex[:8]}")
    base = target.url
    with target.begin() as connection:
        for name in names:
            connection.execute(text(f'CREATE SCHEMA "{name}"'))
            connection.execute(
                text(
                    f'CREATE TABLE "{name}".model_invocations ('
                    "id uuid PRIMARY KEY, created_at timestamptz NOT NULL, model_id text NOT NULL, "
                    "prompt_id text NOT NULL, template_id text NOT NULL DEFAULT 'synthetic', "
                    "input_tokens int NOT NULL, output_tokens int NOT NULL, cost_micros bigint, "
                    "outcome text NOT NULL, private_raw_response text, "
                    "reader_question_packet jsonb)"
                )
            )
    try:
        yield tuple(  # type: ignore[misc]
            base.update_query_dict({"options": f"-csearch_path={name}"}) for name in names
        )
    finally:
        with target.begin() as connection:
            for name in names:
                connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))


def _add(
    target: Engine,
    source: URL,
    call_id: UUID,
    *,
    at: datetime = DAY1,
    model: str = "anthropic.claude-opus-5-5",
    prompt: str = "claude-slot-span-v2",
    tokens: tuple[int, int] = (100, 10),
    cost: int | None = 1_000,
    outcome: str = "ok",
    packet: str | None = None,
) -> None:
    schema = source.query["options"]
    assert isinstance(schema, str)
    with target.begin() as connection:
        connection.execute(
            text(
                f'INSERT INTO "{schema.split("=", 1)[1]}".model_invocations '
                "(id, created_at, model_id, prompt_id, input_tokens, output_tokens, cost_micros, "
                "outcome, private_raw_response, reader_question_packet) VALUES "
                "(:id, :at, :model, :prompt, :inp, :out, :cost, :outcome, 'SECRET ANSWER', "
                "CAST(:packet AS jsonb))"
            ),
            {
                "id": call_id,
                "at": at,
                "model": model,
                "prompt": prompt,
                "inp": tokens[0],
                "out": tokens[1],
                "cost": cost,
                "outcome": outcome,
                "packet": packet,
            },
        )


def _rows(target: Engine, project_id: UUID) -> list[tuple[object, ...]]:
    with Session(target) as session:
        return [
            (
                row.id,
                row.occurred_on.isoformat(),
                row.model_id,
                row.route,
                row.purpose,
                row.calls,
                row.input_tokens,
                row.output_tokens,
                row.cost_micros,
                row.unpriced_calls,
                row.source_key,
            )
            for row in session.scalars(
                select(AiSpendHistory)
                .where(AiSpendHistory.project_id == project_id)
                .order_by(AiSpendHistory.source_key)
            )
        ]


def test_each_call_is_counted_once_across_copies(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    shared, only_a, only_b = uuid4(), uuid4(), uuid4()
    for source in sources:
        _add(target, source, shared, cost=1_000)
    _add(target, sources[0], only_a, cost=2_000)
    _add(target, sources[1], only_b, cost=4_000)

    report = run_import(target_url=target.url, project_id=project_id, source_urls=sources)

    assert (report.rows_read, report.unique_calls, report.imported_calls) == (4, 3, 3)
    [row] = _rows(target, project_id)
    assert row[1:] == (
        "2026-01-05",
        "anthropic.claude-opus-5-5",
        "unknown",
        "reading",
        3,
        300,
        30,
        7_000,
        0,
        "2026-01-05|anthropic.claude-opus-5-5|reading",
    )


def test_calls_the_target_already_holds_are_left_out(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    with session_factory(target)() as session:
        package = Package(project_id=project_id, vendor="Synthetic", product_type="countertop")
        session.add(package)
        session.flush()
        revision = PackageRevision(package_id=package.id, revision_number=1, state="CREATED")
        session.add(revision)
        session.flush()
        held = ModelInvocation(
            package_revision_id=revision.id,
            model_id="anthropic.claude-opus-5-5",
            prompt_id="claude-slot-span-v2",
            template_id="synthetic",
            input_tokens=1,
            output_tokens=1,
            cost_micros=1,
            latency_ms=1,
            outcome="ok",
        )
        session.add(held)
        session.commit()
        held_id = held.id
    _add(target, sources[0], held_id, cost=999_999)
    _add(target, sources[0], uuid4(), cost=5)

    report = run_import(target_url=target.url, project_id=project_id, source_urls=sources)

    assert (report.unique_calls, report.already_in_target, report.imported_calls) == (2, 1, 1)
    assert report.target_calls == report.target_calls_in_project == 1
    [row] = _rows(target, project_id)
    assert row[8] == 5


def test_calls_are_grouped_by_utc_day_model_and_purpose(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    a = sources[0]
    _add(target, a, uuid4(), at=DAY1, prompt="claude-slot-span-v2")
    _add(target, a, uuid4(), at=DAY1, prompt="claude-counter-break-v2")
    _add(target, a, uuid4(), at=DAY1, prompt="slot-row-choice-v2")
    _add(target, a, uuid4(), at=DAY2, prompt="claude-slot-span-v2")
    _add(target, a, uuid4(), at=DAY1, model="anthropic.claude-sonnet-5-5")
    _add(
        target,
        a,
        uuid4(),
        at=DAY1,
        model="anthropic.claude-sonnet-5-5",
        prompt="review-assistant-v3",
    )
    _add(
        target,
        a,
        uuid4(),
        at=DAY1,
        model="mistral.mistral-large-3-675b-instruct",
        prompt="dimension-reader-v1",
    )

    run_import(target_url=target.url, project_id=project_id, source_urls=sources)

    assert [(row[1], row[2], row[3], row[4], row[5]) for row in _rows(target, project_id)] == [
        ("2026-01-05", "anthropic.claude-opus-5-5", "unknown", "reading", 2),
        ("2026-01-05", "anthropic.claude-opus-5-5", "unknown", "row-choice", 1),
        ("2026-01-05", "anthropic.claude-sonnet-5-5", "openrouter", "assistant", 1),
        ("2026-01-05", "anthropic.claude-sonnet-5-5", "unknown", "reading", 1),
        ("2026-01-05", "mistral.mistral-large-3-675b-instruct", "bedrock", "bake-off", 1),
        ("2026-01-06", "anthropic.claude-opus-5-5", "unknown", "reading", 1),
    ]


def test_running_again_gives_the_same_rows(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    _add(target, sources[0], uuid4())
    _add(target, sources[1], uuid4(), at=DAY2, cost=None)

    run_import(target_url=target.url, project_id=project_id, source_urls=sources)
    first = _rows(target, project_id)
    run_import(target_url=target.url, project_id=project_id, source_urls=sources)

    assert _rows(target, project_id) == first
    assert len(first) == 2


def test_a_newer_import_updates_the_same_row(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    _add(target, sources[0], uuid4(), cost=10)
    run_import(target_url=target.url, project_id=project_id, source_urls=sources[:1])
    [before] = _rows(target, project_id)
    _add(target, sources[1], uuid4(), cost=20)

    run_import(target_url=target.url, project_id=project_id, source_urls=sources)

    [after] = _rows(target, project_id)
    assert after[0] == before[0]
    assert (after[5], after[8]) == (2, 30)


def test_a_failed_call_with_no_tokens_costs_nothing_and_a_priced_one_counts(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    a = sources[0]
    _add(target, a, uuid4(), tokens=(0, 0), cost=None, outcome="failed")
    _add(target, a, uuid4(), tokens=(5, 0), cost=None, outcome="failed")
    _add(target, a, uuid4(), cost=None)
    _add(target, a, uuid4(), cost=300)

    report = run_import(target_url=target.url, project_id=project_id, source_urls=sources)

    [row] = _rows(target, project_id)
    assert (row[5], row[8], row[9]) == (4, 300, 2)
    assert report.totals()["unpriced_calls"] == 2


def test_a_reused_answer_made_no_call(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    _add(target, sources[0], uuid4(), packet='{"reused_from": "x"}', cost=0, tokens=(0, 0))
    _add(target, sources[0], uuid4(), packet='{"packet_sha256": "a"}')

    report = run_import(target_url=target.url, project_id=project_id, source_urls=sources)

    assert report.unique_calls == 1


def test_a_dry_run_writes_nothing(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    _add(target, sources[0], uuid4(), cost=1_234_567)

    report = run_import(
        target_url=target.url, project_id=project_id, source_urls=sources, dry_run=True
    )

    assert report.totals()["total_usd"] == "1.234567"
    assert not report.written
    assert _rows(target, project_id) == []


def test_a_database_without_the_table_is_skipped_not_fatal(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    empty = target.url.update_query_dict({"options": "-csearch_path=no_such_schema"})
    _add(target, sources[0], uuid4())

    report = run_import(
        target_url=target.url, project_id=project_id, source_urls=(empty, sources[0])
    )

    assert report.sources[0].skipped == "no model_invocations table"
    assert report.imported_calls == 1


def test_an_unknown_project_is_refused(target: Engine, sources: tuple[URL, URL]) -> None:
    with pytest.raises(ValueError, match="not in the target"):
        run_import(target_url=target.url, project_id=uuid4(), source_urls=sources)


def test_the_printed_totals_carry_no_password_and_no_answer(
    target: Engine,
    project_id: UUID,
    sources: tuple[URL, URL],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _add(target, sources[0], uuid4())
    password = target.url.password
    assert password

    code = main(
        [
            "--target-url",
            target.url.render_as_string(hide_password=False),
            "--project-id",
            str(project_id),
            "--source-url",
            sources[0].render_as_string(hide_password=False),
            "--dry-run",
            "--json",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert f":{password}@" not in out
    assert "SECRET ANSWER" not in out
    assert json.loads(out)["unique_calls"] == 1


@pytest.mark.parametrize(
    ("name", "is_test"),
    [
        ("gvtest", True),
        ("gvtest_gv_v1_1165", True),
        ("gvpytest963", True),
        ("my_pytest_db", True),
        ("gvverify_demo_polish_tests", True),
        ("live_both", False),
        ("gvclaudeproof1008", False),
        ("orproof_set1", False),
    ],
)
def test_test_databases_are_recognised(name: str, is_test: bool) -> None:
    assert is_test_database(name) is is_test


def test_a_scan_skips_templates_system_test_named_and_the_target() -> None:
    template = make_url("postgresql+psycopg://someone:hunter2@localhost:5434/live_both")
    listed = {
        5433: ["postgres", "hatchet", "gvtest_x", "gvpytest1", "real_a", "live_both"],
        5434: ["postgres", "live_both", "real_b", "skip_me", "x_tests"],
    }

    urls = scan_server_urls(
        template,
        ["localhost:5433", "localhost:5434"],
        target=template,
        skip=["skip_me"],
        list_databases=lambda url: listed[url.port or 0],
    )

    assert [(url.port, url.database) for url in urls] == [
        (5433, "real_a"),
        (5433, "live_both"),
        (5434, "real_b"),
    ]
    assert all(url.username == "someone" for url in urls)


def test_a_server_must_be_host_and_port() -> None:
    template = make_url("postgresql+psycopg://someone@localhost:5434/t")
    with pytest.raises(ValueError, match="host:port"):
        scan_server_urls(template, ["localhost"], target=template, list_databases=lambda url: [])
