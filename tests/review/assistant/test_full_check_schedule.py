"""A big set's full check in parallel band chains, with as many specialists as the provider
allows, and its expected time before Run (#1874).

Scripted models and a synthetic page map: no network, no paid call.

- **Bands.** A big set's page groups are cut into contiguous bands of about `BAND_PAGES` content
  pages; a set of up to `BANDS_FROM` pages is one band (the roster runs as before).
- **Same work, same answer.** A set run as two band chains (each its own helpers, gap closers,
  verifier, lead review and look-again) ends with the same findings, groups and proofs as one
  chain, and each lead review takes only its band's pages.
- **Specialists at once.** A roster full check starts all its specialists; each waits for a place
  of its model's limiter, so no more than the limiter's places run together.
- **The expected time** grows slowly with the pages, within Anant's target for big sets.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import full_check_roster
from app.review.assistant.full_check_schedule import (
    BAND_PAGES,
    BANDS_FROM,
    Band,
    bands_of,
    expected_minutes,
    run_chains,
)
from tests.review.assistant.test_full_check_roster import _clean_ink, _findings, _Roster, _roster


def _job(number: int, pages: range | tuple[int, ...], lens: str) -> Any:
    return SimpleNamespace(number=number, pages=tuple(pages), lens=lens)


def _plan(groups: int, size: int = 5) -> list[Any]:
    numbers = [
        _job(n + 1, range(n * size + 1, n * size + size + 1), "numbers") for n in range(groups)
    ]
    drawn = [_job(groups + n + 1, j.pages, "drawn") for n, j in enumerate(numbers)]
    return [*numbers, *drawn]


def test_a_set_up_to_the_threshold_is_one_band_as_before() -> None:
    jobs = _plan(BANDS_FROM // 5)
    bands = bands_of(jobs)
    assert len(bands) == 1
    assert bands[0].jobs == tuple(jobs) and bands[0].fraction == 1.0


def test_a_big_set_is_cut_into_contiguous_bands_of_about_band_pages() -> None:
    jobs = _plan(38)  # 190 content pages
    bands = bands_of(jobs)
    assert len(bands) == round(190 / BAND_PAGES)
    pages = [page for band in bands for page in band.pages]
    assert pages == sorted(pages) == list(range(1, 191))  # contiguous, each page once
    assert all(abs(len(band.pages) - 190 / len(bands)) <= 5 for band in bands)
    assert sum(band.fraction for band in bands) == pytest.approx(1.0)
    placed = [job for band in bands for job in band.jobs]
    assert sorted(job.number for job in placed) == [job.number for job in jobs]
    for band in bands:  # a numbers job goes with its drawn-things partner's band
        assert all(set(job.pages) <= set(band.pages) for job in band.jobs)


def test_band_chains_run_together_and_one_failing_never_stops_the_others() -> None:
    started: list[float] = []

    def chain() -> None:
        started.append(time.monotonic())
        time.sleep(0.1)

    def broken() -> None:
        raise ValueError("no")

    begun = time.monotonic()
    failed = run_chains([chain, broken, chain, chain])
    assert failed == ["band 2: ValueError"]
    assert len(started) == 3 and time.monotonic() - begun < 0.3  # at once, not one after another


def test_the_expected_time_stays_within_the_target_as_the_set_grows() -> None:
    small, set3, big, huge = (
        expected_minutes(5),
        expected_minutes(45),
        expected_minutes(190),
        expected_minutes(480),
    )
    assert 0 < small <= set3 <= big <= huge
    assert big <= 25 and huge <= 40
    assert expected_minutes(0) == 0
    assert expected_minutes(480, at_once=64) <= huge  # a higher tier: never slower


def _key(finding: Mapping[str, Any]) -> tuple[Any, ...]:
    proof = finding.get("proof") or {}
    critic = finding.get("critic") or {}
    return (
        finding["page"],
        finding["item"],
        finding["status"],
        finding.get("trust_level"),
        critic.get("result"),
        proof.get("group"),
    )


def test_two_band_chains_end_with_the_same_findings_as_one_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clean_ink(monkeypatch)
    one = _roster(tmp_path / "one", _Roster())
    single = one.run()
    assert "parallel chains" not in caplog.text

    cut: list[list[Band]] = []

    def two_bands(jobs: Any) -> list[Band]:
        bands = bands_of(jobs, most=2, every=1)
        cut.append(bands)
        return bands

    monkeypatch.setattr(full_check_roster, "bands_of", two_bands)
    script = _Roster()
    banded = _roster(tmp_path / "two", script)
    with caplog.at_level("WARNING", logger="app.review.assistant.full_check_roster"):
        outcome = banded.run()

    assert len(cut[0]) == 2 and "2 bands of" in caplog.text
    assert sorted(map(_key, _findings(outcome))) == sorted(map(_key, _findings(single)))
    assert outcome.stopped == single.stopped is None
    # Each band's specialists stayed on its own pages.
    for band in cut[0]:
        for agent in band.agents:
            assert set(agent.pages or ()) <= set(band.pages), agent.description
    assert script.calls["tags"] == 1


def test_a_band_spends_only_its_part_of_its_phases(tmp_path: Path) -> None:
    harness = _roster(tmp_path, _Roster())
    run = full_check_roster._Run(harness.engine)
    run.plan = full_check_roster.PricePlan.balanced(10_000_000, 4, 40)
    band = Band(1, tuple(range(1, 11)), (), 0.25)
    budgets = run.plan.budgets
    whole = sum(budgets[n] for n in ("numbers", "drawn", "gaps", "provers", "lead"))
    lead = run._band_room(band, "lead")
    assert lead <= int(0.25 * whole)
    # The gap closers keep the band's verifier and lead parts.
    gaps = run._band_room(band, "gaps")
    assert gaps <= int(0.25 * whole) - int(0.25 * (budgets["provers"] + budgets["lead"]))
    band.agents.append(SimpleNamespace(spent=gaps))
    assert run._band_room(band, "gaps") == 0


def test_a_full_check_runs_no_more_specialists_at_once_than_the_limiter_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    inner = _Roster()
    lock = threading.Lock()
    seen = {"now": 0, "most": 0}

    def slow(params: Mapping[str, Any]) -> Any:
        with lock:
            seen["now"] += 1
            seen["most"] = max(seen["most"], seen["now"])
        try:
            time.sleep(0.02)
            return inner(params)
        finally:
            with lock:
                seen["now"] -= 1

    harness = _roster(tmp_path, inner)
    harness.provider.helper_answer = slow
    engine = harness.engine
    engine.settings = dataclasses.replace(engine.settings, calls_at_once=2, full_check_agents=50)
    assert engine._agents_at_once == 50  # every specialist starts; the limiter decides
    outcome = harness.run()
    assert outcome.stopped is None
    assert 1 <= seen["most"] <= 2 + 2  # two specialists, and at most two single calls beside them
