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


def test_band_chains_wait_for_the_final_pairs_and_never_send_an_unpaired_page_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A big set's bands keep #1883/#1885 as one chain does: the page matcher's final pairs are
    in before any band's gap closers, once; an unpaired page is Not compared from the start and
    no band's gap closer or lead review is sent to compare it."""
    from app.review.assistant.page_match import CrossPairs
    from tests.review.assistant.test_unpaired_pages import WHY, _coverage, _Matched, _run_with

    finished: list[int] = []

    class _Counted(_Matched):
        def finish(self) -> CrossPairs:
            finished.append(1)
            return super().finish()

    cut: list[list[Band]] = []

    def two_bands(jobs: Any) -> list[Band]:
        bands = bands_of(jobs, most=2, every=1)
        cut.append(bands)
        return bands

    monkeypatch.setattr(full_check_roster, "bands_of", two_bands)
    matcher = _Counted(CrossPairs(pairs={2: (("arch.pdf", 9),)}, unpaired={5: WHY}))

    outcome, engine, _script, seen = _run_with(tmp_path, monkeypatch, matcher)

    assert len(cut[0]) == 2
    assert finished == [1]  # the final pairs once, for every band
    assert all(5 not in pages for pages in (*seen["close"], *seen["lead"])), seen
    assert _coverage(outcome)[5]["status"] == "not_comparable"
    assert len([g for g in engine.extra_gaps if g["page"] == 5]) == 1


def test_band_chains_run_the_whole_drawing_looks_as_one_chain_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole-drawing looks (`whole_look`) run in a big set's bands too: one per area, beside
    its band's helpers, their claims applied by code (the same Likely problems as one chain)."""
    import dataclasses as dc
    import json as js

    from app.review.assistant import likely_problems, proof, whole_look
    from tests.review.assistant.chat_fakes import reply, text
    from tests.review.assistant.test_full_check_roster import SONNET
    from tests.review.assistant.test_whole_look import _claim, _side

    class _WithLooks(_Roster):
        def __call__(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
            if params["system"][0]["text"].startswith("You are an experienced millwork reviewer"):
                self._seen("whole_look", params)
                claim = _claim(
                    "count",
                    _side(3, None, "1 upper cabinet"),
                    _side(1, None, "2 upper cabinets"),
                    item="Upper cabinets",
                    vendor_spot={"x0": 0, "y0": 0, "x1": 50, "y1": 50},
                )
                return reply(text(js.dumps({"claims": [claim]})), model=SONNET)
            return super().__call__(params)

    def run(folder: Path, banded: bool) -> tuple[Any, _WithLooks, list[list[Band]]]:
        cut: list[list[Band]] = []
        if banded:

            def two_bands(jobs: Any) -> list[Band]:
                bands = bands_of(jobs, most=2, every=1)
                cut.append(bands)
                return bands

            monkeypatch.setattr(full_check_roster, "bands_of", two_bands)
        script = _WithLooks()
        harness = _roster(folder, script)
        harness.engine.settings = dc.replace(harness.engine.settings, whole_look_model=SONNET)
        return harness.run(), script, cut

    _clean_ink(monkeypatch)
    single, one_script, _ = run(tmp_path / "one", banded=False)
    outcome, script, cut = run(tmp_path / "two", banded=True)

    assert len(cut[0]) == 2
    assert script.calls["whole_look"] == one_script.calls["whole_look"] == 2
    likely = [f for f in _findings(outcome) if likely_problems.FIELD in f]
    assert sorted(f["page"] for f in likely) == [3, 5]
    assert all(proof.proof_group(f) == "likely_problem" for f in likely)
    assert all(f["created_by"] == whole_look.CREATED_BY for f in likely)
    assert sorted(map(_key, _findings(outcome))) == sorted(map(_key, _findings(single)))
