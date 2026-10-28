"""Querying findings (#222, D1.1): what the list must never quietly leave out.

Two claims carry this story, and both fail silently when they are wrong — which is why most of what
follows is failure paths rather than a happy path.

**The default list contains the abstentions.** A `REVIEW REQUIRED` missing from a list looks exactly
like a check that passed, and a package that looks clean gets signed off. So there is a test for the
default, and a separate one proving the outcome filter can actually narrow — a "default includes
everything" test passes just as well against an endpoint that ignores the filter entirely.

**Paging never skips a finding.** The interesting case is not paging a static table; it is paging one
that is being written to. The keyset test inserts a finding that sorts *inside* an already-served
page and asserts the reviewer still sees every original row exactly once.

The PostgreSQL tests skip without `DATABASE_URL` and run on CI. Everything above them runs anywhere.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import (
    CheckRun,
    Finding,
    PackageRevision,
    RuleDefinition,
    RuleSnapshot,
)
from app.schemas.findings import (
    OUTCOME_ORDER,
    SEVERITY_ORDER,
    validate_sort_orders,
)
from tests.app.postgres_fixture import alembic_config
from verdict.outcomes import ABSTAINING_OUTCOMES, Outcome, Severity

pytest_plugins = ("tests.app.postgres_fixture",)

DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"

#: A fixed instant, so the tests control the tie-break rather than the clock.
EPOCH = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The sort key, which is what makes paging safe
# ---------------------------------------------------------------------------


def test_the_documented_order_ranks_every_outcome_and_severity() -> None:
    """An unranked value shares a bucket with every other unranked one, so the sort stops being
    total — and a keyset cursor over a non-total order skips and repeats rows."""
    validate_sort_orders(SEVERITY_ORDER, OUTCOME_ORDER)
    assert set(SEVERITY_ORDER) == set(Severity)
    assert set(OUTCOME_ORDER) == set(Outcome)


def test_the_order_guard_refuses_a_key_that_misses_a_value() -> None:
    """The test above asserts today's tuples are complete, so it would still pass with the guard
    deleted. This one watches the guard fail."""
    with pytest.raises(RuntimeError, match="does not rank every value"):
        validate_sort_orders((Severity.CRITICAL,), OUTCOME_ORDER)
    with pytest.raises(RuntimeError, match="does not rank every value"):
        validate_sort_orders(SEVERITY_ORDER, (Outcome.PASS,))


def test_the_order_guard_refuses_a_value_ranked_twice() -> None:
    """Two ranks for one value means its position depends on which comparison runs first."""
    with pytest.raises(RuntimeError, match="ranked twice"):
        validate_sort_orders((*SEVERITY_ORDER, Severity.CRITICAL), OUTCOME_ORDER)


def test_failures_rank_above_abstentions_and_abstentions_above_passes() -> None:
    """`DESIGN_PRODUCT.md` §3.2 applied to a list. A REVIEW REQUIRED under forty passes is a check
    nobody reads, which is the same outcome as not reporting it."""
    position = {outcome: rank for rank, outcome in enumerate(OUTCOME_ORDER)}
    assert position[Outcome.FAIL] == 0
    for abstention in ABSTAINING_OUTCOMES:
        assert position[abstention] < position[Outcome.PASS]


def test_v1_flags_rank_first_among_severities() -> None:
    assert SEVERITY_ORDER[0] is Severity.FLAG


# ---------------------------------------------------------------------------
# The cursor
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bad arguments, refused before anything is read
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    """A migrated, throwaway schema and one session over it.

    The same session is handed to the endpoint, so a row written here is visible to the request
    without a commit — the tests are about ordering and paging, not about transaction visibility.
    """
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _snapshot(
    session: Session, *, check_type: str = "internal", product_type: str = "countertop"
) -> RuleSnapshot:
    definition = RuleDefinition(rule_id=f"CT-WIDTH-{uuid4().hex[:8]}")
    session.add(definition)
    session.flush()
    canonical = json.dumps({"id": definition.rule_id, "version": "1.0.0"}, separators=(",", ":"))
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=canonical,
        product_type=product_type,
        check_type=check_type,
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _finding(
    session: Session,
    revision: PackageRevision,
    snapshot: RuleSnapshot,
    outcome: Outcome,
    severity: Severity = Severity.CRITICAL,
    *,
    created_at: datetime | None = None,
) -> Finding:
    """One finding and the check run it came from.

    A run per finding, because `findings.check_run_id` is unique — one finding per run, or "what did
    this check decide?" would have two answers.
    """
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version="verdict-1.2.3",
    )
    session.add(run)
    session.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=outcome.value,
        severity=severity.value,
        trace={},
        parameter_set_versions={"countertop": "3"},
        created_at=created_at if created_at is not None else EPOCH,
    )
    session.add(finding)
    session.flush()
    return finding


# ---------------------------------------------------------------------------
# The summary — what a package list shows before anyone opens a finding
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Reading a reviewer's decisions back (#472)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Superseded runs (#477)
# ---------------------------------------------------------------------------
