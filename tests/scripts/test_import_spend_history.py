"""The spend-history importer (#1165): each call once, only adds, never loses an earlier call.

Synthetic "source databases" are schemas holding a bare `model_invocations` table; the target is
the migrated test schema. All values are synthetic.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import replace
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
    SourceRead,
    is_test_database,
    main,
    parse_purpose_overrides,
    read_source,
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
            target.url.update_query_dict({"options": f"-csearch_path={name}"}) for name in names
        )
    finally:
        with target.begin() as connection:
            for name in names:
                connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))


def _schema(source: URL) -> str:
    options = source.query["options"]
    assert isinstance(options, str)
    return options.split("=", 1)[1]


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
    with target.begin() as connection:
        connection.execute(
            text(
                f'INSERT INTO "{_schema(source)}".model_invocations '
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


def _named(names: dict[str, str]) -> Callable[[URL], SourceRead]:
    """A reader that gives each source schema a database name, for purpose overrides."""

    def reader(url: URL) -> SourceRead:
        return replace(read_source(url), database=names.get(_schema(url)))

    return reader


def _failing(url: URL) -> SourceRead:
    return SourceRead("unreachable", "unreachable", skipped="could not be read (OperationalError)")


def _rows(target: Engine, project_id: UUID) -> list[tuple[object, ...]]:
    with Session(target) as session:
        return [
            (
                row.call_id,
                row.occurred_on.isoformat(),
                row.model_id,
                row.route,
                row.purpose,
                row.input_tokens,
                row.output_tokens,
                row.cost_micros,
                row.priced_later,
            )
            for row in session.scalars(
                select(AiSpendHistory)
                .where(AiSpendHistory.project_id == project_id)
                .order_by(
                    AiSpendHistory.occurred_on, AiSpendHistory.model_id, AiSpendHistory.cost_micros
                )
            )
        ]


def _target_call(target: Engine, project_id: UUID | None) -> UUID:
    """A call in the target, tied to a drawing set of `project_id` (None: another project's)."""
    with session_factory(target)() as session:
        if project_id is None:
            other = Project(name="Another synthetic project")
            session.add(other)
            session.flush()
            project_id = other.id
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
        return held.id


def test_each_call_is_counted_once_across_copies(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    shared, only_a, only_b = uuid4(), uuid4(), uuid4()
    for source in sources:
        _add(target, source, shared, cost=1_000)
    _add(target, sources[0], only_a, cost=2_000)
    _add(target, sources[1], only_b, cost=4_000)

    report = run_import(target_url=target.url, project_id=project_id, source_urls=sources)

    assert (report.rows_read, report.unique_calls, len(report.calls)) == (4, 3, 3)
    rows = _rows(target, project_id)
    assert {row[0] for row in rows} == {shared, only_a, only_b}
    assert sum(row[7] for row in rows) == 7_000  # type: ignore[misc]
    assert {row[1:5] for row in rows} == {
        ("2026-01-05", "anthropic.claude-opus-5-5", "unknown", "reading")
    }


def test_calls_the_projects_reviews_count_are_left_out_and_the_rest_imported(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    counted = _target_call(target, project_id)
    loose = _target_call(target, None)  # in the target, but another project's call
    _add(target, sources[0], counted, cost=999_999)
    _add(target, sources[0], loose, cost=7)
    _add(target, sources[0], uuid4(), cost=5)

    report = run_import(target_url=target.url, project_id=project_id, source_urls=sources)

    assert (report.unique_calls, report.counted_by_project, len(report.calls)) == (3, 1, 2)
    assert (report.target_calls, report.target_calls_in_project) == (2, 1)
    assert sorted(row[7] for row in _rows(target, project_id)) == [5, 7]  # type: ignore[type-var]


def test_calls_are_classified_by_utc_day_model_route_and_purpose(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    a = sources[0]
    _add(target, a, uuid4(), at=DAY2, prompt="claude-slot-span-v2", cost=1)
    _add(target, a, uuid4(), at=DAY1, prompt="slot-row-choice-v2", cost=2)
    _add(target, a, uuid4(), model="anthropic.claude-sonnet-5-5", prompt="review-assistant-v3")
    _add(target, a, uuid4(), model="us.amazon.nova-pro-v1:0", prompt="findings-composer-v3")
    _add(target, a, uuid4(), model="qwen.qwen3-vl-235b-a22b", prompt="dimension-reader-teaching-v2")

    run_import(target_url=target.url, project_id=project_id, source_urls=sources)

    assert sorted(row[1:5] for row in _rows(target, project_id)) == [
        ("2026-01-05", "anthropic.claude-opus-5-5", "unknown", "row-choice"),
        ("2026-01-05", "anthropic.claude-sonnet-5-5", "openrouter", "assistant"),
        ("2026-01-05", "qwen.qwen3-vl-235b-a22b", "bedrock", "reading"),
        ("2026-01-05", "us.amazon.nova-pro-v1:0", "bedrock", "findings"),
        ("2026-01-06", "anthropic.claude-opus-5-5", "unknown", "reading"),
    ]


def test_a_model_comparison_is_marked_by_its_source_database(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    shared = uuid4()
    _add(target, sources[0], uuid4(), model="mistral.mistral-large-3-675b-instruct")
    _add(target, sources[0], shared)
    _add(target, sources[1], shared)
    _add(target, sources[1], uuid4(), model="us.amazon.nova-2-lite-v1:0")
    reader = _named({_schema(sources[0]): "gv_comparison", _schema(sources[1]): "gv_real"})

    run_import(
        target_url=target.url,
        project_id=project_id,
        source_urls=sources,
        purpose_overrides=parse_purpose_overrides(["gv_comparison=bake-off"]),
        reader=reader,
    )

    purposes = {row[2]: row[4] for row in _rows(target, project_id)}
    assert purposes == {
        "mistral.mistral-large-3-675b-instruct": "bake-off",
        # A call held by the comparison database is a comparison call, wherever else it is copied.
        "anthropic.claude-opus-5-5": "bake-off",
        "us.amazon.nova-2-lite-v1:0": "reading",
    }


@pytest.mark.parametrize("value", ["gv_x", "gv_x=fun", "=bake-off"])
def test_a_bad_purpose_override_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="--purpose-override"):
        parse_purpose_overrides([value])


def test_running_again_adds_nothing_and_changes_nothing(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    _add(target, sources[0], uuid4())
    _add(target, sources[1], uuid4(), at=DAY2, cost=None)

    run_import(target_url=target.url, project_id=project_id, source_urls=sources)
    first = _rows(target, project_id)
    again = run_import(target_url=target.url, project_id=project_id, source_urls=sources)

    assert _rows(target, project_id) == first
    assert (len(first), again.already_in_history, len(again.calls)) == (2, 2, 0)


def test_disjoint_sources_imported_one_after_the_other_keep_both(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    from_a, from_b = uuid4(), uuid4()
    _add(target, sources[0], from_a, cost=10)
    _add(target, sources[1], from_b, cost=20)

    run_import(target_url=target.url, project_id=project_id, source_urls=sources[:1])
    run_import(target_url=target.url, project_id=project_id, source_urls=sources[1:])

    assert {(row[0], row[7]) for row in _rows(target, project_id)} == {(from_a, 10), (from_b, 20)}


def test_a_source_that_fails_to_read_keeps_the_earlier_calls(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    _add(target, sources[0], uuid4(), cost=10)
    run_import(target_url=target.url, project_id=project_id, source_urls=sources[:1])
    before = _rows(target, project_id)

    report = run_import(
        target_url=target.url, project_id=project_id, source_urls=sources[:1], reader=_failing
    )

    assert report.sources[0].skipped == "could not be read (OperationalError)"
    assert _rows(target, project_id) == before


def test_a_failed_call_with_no_tokens_costs_nothing_and_unpriced_stays_unpriced(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    a = sources[0]
    _add(target, a, uuid4(), tokens=(0, 0), cost=None, outcome="failed")
    _add(target, a, uuid4(), tokens=(5, 0), cost=None, outcome="failed")
    _add(target, a, uuid4(), cost=None)
    _add(target, a, uuid4(), cost=300)

    report = run_import(target_url=target.url, project_id=project_id, source_urls=sources)

    assert sorted((row[7] is None, row[7] or 0) for row in _rows(target, project_id)) == [
        (False, 0),
        (False, 300),
        (True, 0),
        (True, 0),
    ]
    assert report.totals()["unpriced"] == 2


def test_a_pre_754_zero_with_tokens_is_priced_later_never_free(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    a = sources[0]
    # Before #754 every call was stored as 0, failed or not.
    _add(target, a, uuid4(), model="amazon.nova-pro-v1:0", tokens=(1000, 100), cost=0)
    _add(
        target,
        a,
        uuid4(),
        model="us.amazon.nova-2-lite-v1:0",
        tokens=(1000, 0),
        cost=0,
        outcome="rejected",
    )
    _add(target, a, uuid4(), model="anthropic.claude-haiku-4-5-20251001-v1:0", cost=0)
    _add(target, a, uuid4(), model="amazon.nova-pro-v1:0", tokens=(0, 0), cost=0, outcome="failed")

    report = run_import(target_url=target.url, project_id=project_id, source_urls=sources)

    costs = {(row[2], row[5]): (row[7], row[8]) for row in _rows(target, project_id)}
    assert costs == {
        ("amazon.nova-pro-v1:0", 1000): (1120, True),  # 1000 * 0.0008/1k + 100 * 0.0032/1k
        ("us.amazon.nova-2-lite-v1:0", 1000): (330, True),
        ("anthropic.claude-haiku-4-5-20251001-v1:0", 100): (None, False),  # no published rate
        ("amazon.nova-pro-v1:0", 0): (0, False),  # used nothing: a true zero
    }
    totals = report.totals()
    assert (totals["priced_later"], totals["priced_later_usd"], totals["unpriced"]) == (
        2,
        "0.001450",
        1,
    )


def test_a_source_without_a_cost_column_is_priced_from_the_published_rates(
    target: Engine, project_id: UUID, sources: tuple[URL, URL]
) -> None:
    with target.begin() as connection:
        connection.execute(
            text(f'ALTER TABLE "{_schema(sources[0])}".model_invocations DROP COLUMN cost_micros')
        )
        connection.execute(
            text(
                f'INSERT INTO "{_schema(sources[0])}".model_invocations (id, created_at, model_id, '
                "prompt_id, input_tokens, output_tokens, outcome) VALUES "
                "(:id, :at, 'amazon.nova-pro-v1:0', 'dimension-reader-v1', 1000, 100, 'ok')"
            ),
            {"id": uuid4(), "at": DAY1},
        )

    run_import(target_url=target.url, project_id=project_id, source_urls=sources)

    [row] = _rows(target, project_id)
    assert (row[7], row[8]) == (1120, True)


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

    assert report.totals()["usd"] == "1.234567"
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
    assert len(report.calls) == 1


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
        ("gv959test", True),
        ("gv1018tests", True),
        ("gvclaudetest", True),
        ("GV_TEST_upper", True),
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
        5433: ["postgres", "hatchet", "gvtest_x", "gv959test", "real_a", "live_both"],
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
