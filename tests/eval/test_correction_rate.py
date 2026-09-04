"""Reviewer correction rate is derived from reviewed evidence and its immutable ledger (#236)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from eval.correction_rate import CorrectionRateKey, correction_rate, report

NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)


def _session(*rows: tuple[str, str, str, int, int]) -> Session:
    session = MagicMock(spec=Session)
    session.execute.return_value = rows
    return session


def test_rates_are_exact_and_attributable_to_check_extractor_and_version() -> None:
    """Input: two grouped ledger counts. Outcome: exact, independently attributable rates."""

    results = correction_rate(
        _session(
            ("internal", "paddleocr", "2.8.1", 1, 3),
            ("internal", "paddleocr", "2.9.0", 2, 3),
        ),
        window=timedelta(days=30),
        now=NOW,
    )

    old = results[CorrectionRateKey("internal", "paddleocr", "2.8.1")]
    new = results[CorrectionRateKey("internal", "paddleocr", "2.9.0")]
    assert old.value == Fraction(1, 3)
    assert (old.numerator, old.denominator) == (1, 3)
    assert new.value == Fraction(2, 3)
    assert new.value > old.value


def test_same_version_name_on_different_extractors_never_collapses() -> None:
    """Input: two readers both called v1. Outcome: separate keys rather than a blended rate."""

    results = correction_rate(
        _session(
            ("internal", "paddleocr", "1", 1, 4),
            ("internal", "nova", "1", 3, 4),
        ),
        window=timedelta(days=7),
        now=NOW,
    )

    assert len(results) == 2
    assert results[CorrectionRateKey("internal", "paddleocr", "1")].value == Fraction(1, 4)
    assert results[CorrectionRateKey("internal", "nova", "1")].value == Fraction(3, 4)


def test_query_reads_the_ledger_and_counts_only_reviewed_evidence_actions() -> None:
    """Input: a metric request. Outcome: SQL derives both counts from authoritative rows."""

    session = _session()
    correction_rate(session, window=timedelta(days=1), now=NOW)

    statement = session.execute.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect())).lower()
    assert "correction_ledger" in sql
    assert "review_actions" in sql
    assert "evidence_supporting_candidates" in sql
    assert "observation_candidates" in sql
    assert "extraction_runs" in sql
    params = statement.compile(dialect=postgresql.dialect()).params
    action_values = next(value for value in params.values() if isinstance(value, list))
    assert action_values == ["confirm", "correct"]


def test_no_reviewed_readings_is_not_measured() -> None:
    """Input: no qualifying actions. Outcome: loud NOT MEASURED, never a zero rate."""

    results = correction_rate(_session(), window=timedelta(days=30), now=NOW)

    assert results == {}
    assert "NOT MEASURED" in report(results)
    assert "0.0%" not in report(results)


@pytest.mark.parametrize("window", [timedelta(0), timedelta(seconds=-1)])
def test_an_empty_or_negative_window_is_refused(window: timedelta) -> None:
    """Input: a window containing no possible evidence. Outcome: refusal, not a clean metric."""

    with pytest.raises(ValueError, match="positive duration"):
        correction_rate(_session(), window=window, now=NOW)


def test_a_naive_timestamp_is_refused() -> None:
    """Input: local-clock time. Outcome: refusal before a database query can be issued."""

    with pytest.raises(ValueError, match="timezone-aware"):
        correction_rate(
            _session(),
            window=timedelta(days=1),
            now=NOW.replace(tzinfo=None),
        )


def test_report_states_the_denominator_and_every_group() -> None:
    """Input: grouped counts. Outcome: readable output says what the percentage divides by."""

    results = correction_rate(
        _session(("cross_document", "nova", "2026-08", 1, 2)),
        window=timedelta(days=30),
        now=NOW,
    )
    rendered = report(results)

    assert "corrected readings / confirmed-or-corrected readings" in rendered
    assert "cross_document" in rendered
    assert "nova 2026-08" in rendered
    assert "1/2" in rendered
