"""How a full check's AI work is scheduled on a big drawing set (#1874): areas in parallel chains,
and the expected time before Run.

Anant (2026-10-22): the full check must work for any PDF size and finish a big set in about 20-25
minutes. On a 60-page set the roster's phases run one after the other for the whole set (all page
helpers, then all gap closers, then the verifier, one lead review, the look-again passes); on a
188-page set each of those global waits holds every area back for the slowest one, and one lead
review cannot reach more than `LEAD_MAX_PAGES` pages. The production pipeline research
(2026-10-22) found the same: remove the global waits, run per-area chains in parallel, and wait
only for the final reduce.

- **Bands.** A big set's page groups (the plan's own: an area stays together) are cut into
  contiguous bands of about `BAND_PAGES` content pages (`bands_of`). Each band runs its own chain
  in parallel: its page helpers, then its gap closers, its verifier, its own lead review (Opus) and
  its look-again passes, each step as soon as the band's previous one ended
  (`full_check_roster`). Only the set-wide steps wait for all bands: the tag table, code's proving
  steps, the devil's advocate, the escalation, code's finish and the files.
- **Money.** A band's steps spend its part of the price: the page share of the phases it runs
  (`Band.fraction`), money a step leaves rolling on to the band's next step, never the set-wide
  steps' money (the roster's own rules decide that). Every specialist keeps its own cap.
- **One band = today's check.** A set of up to `BANDS_FROM` content pages has one band: the
  roster runs exactly as before.
- **How many run at once** is not decided here: each model's limiter (`rate_limits`) lets as
  many specialists run as the provider allows.
- **The expected time** (`expected_minutes`): from the plan's page groups, bands and the call
  times measured on the live server (21-22 Oct), shown with the price before Run.

Plain code on the plan: nothing is read or drawn here.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.review.assistant.full_check_plan import _partition

__all__ = [
    "BANDS_FROM",
    "BAND_PAGES",
    "Band",
    "bands_of",
    "expected_minutes",
    "run_chains",
]

_log = logging.getLogger(__name__)

#: A band's content pages, about (a lead review takes at most 16 pages of it, the biggest first).
BAND_PAGES: Final = 40
#: Sets with more content pages than this are cut into bands; up to it, one chain as before.
BANDS_FROM: Final = 60

# The expected time (#1874), from the live server's timings lines of 21-22 Oct 2026 (median
# seconds a call; calls a specialist) and the fake-AI scale test of the same week (code's steps).
#: A page helper's time: its calls (about 6) at about 9 s, plus the slowest of many (a tail).
_HELPER_S: Final = 75.0
_HELPER_TAIL_S: Final = 90.0
#: The gap closers, the verifier and a lead review (Opus, low effort), per band.
_GAPS_S: Final = 110.0
_PROVERS_S: Final = 60.0
_LEAD_S: Final = 150.0
_AGAIN_S: Final = 90.0
#: The set-wide steps after the bands: the proving reads, the advocate, the escalation and code's
#: finish grow with the pages (about this many seconds a content page) on top of a fixed part.
_REDUCE_S: Final = 240.0
_REDUCE_PAGE_S: Final = 2.0


@dataclass(slots=True)
class Band:
    """One chain's page groups: its pages, its jobs (numbers and drawn-things), its part of the
    price, and the specialists it ran (their money spent)."""

    number: int
    pages: tuple[int, ...]
    jobs: tuple[Any, ...]
    fraction: float
    agents: list[Any] = field(default_factory=list)

    def spent(self) -> int:
        """What the band's specialists spent so far (millionths of $)."""
        return sum(int(getattr(agent, "spent", 0) or 0) for agent in list(self.agents))

    def holds(self, page: object) -> bool:
        return page in self.pages


def bands_of(jobs: Sequence[Any], most: int = BAND_PAGES, every: int = BANDS_FROM) -> list[Band]:
    """The plan's jobs cut into bands of about `most` content pages, in page order; one band (all
    the jobs) when the set has at most `every` content pages. A band is cut only between page
    groups (the drawn-things jobs' pages, or the numbers jobs' when there are none), and a numbers
    job goes with the band of its pages."""
    groups: list[tuple[int, ...]] = []
    for job in jobs:
        if getattr(job, "lens", None) in ("numbers",) and any(
            getattr(other, "lens", None) not in ("numbers", None) for other in jobs
        ):
            continue
        pages = tuple(job.pages)
        if pages not in groups:
            groups.append(pages)
    total = len({page for pages in groups for page in pages})
    if total <= every or len(groups) < 2:
        pages = tuple(sorted({page for job in jobs for page in job.pages}))
        return [Band(1, pages, tuple(jobs), 1.0)]
    count = min(len(groups), max(2, round(total / most)))
    # The planner's own cut (`full_check_plan`): contiguous, the largest band smallest, then the
    # most even.
    ends = _partition([len(pages) for pages in groups], count)
    starts = [0, *ends[:-1]]
    cut = [groups[start:end] for start, end in zip(starts, ends, strict=True)]
    bands: list[Band] = []
    for number, chosen in enumerate(cut, start=1):
        pages = tuple(sorted({page for group in chosen for page in group}))
        own = set(pages)
        mine = tuple(job for job in jobs if set(job.pages) <= own)
        bands.append(Band(number, pages, mine, len(pages) / total))
    placed = {id(job) for band in bands for job in band.jobs}
    for job in jobs:
        if id(job) not in placed:  # a numbers job across two bands: the band of its first page
            band = next((b for b in bands if job.pages and job.pages[0] in b.pages), bands[-1])
            band.jobs = (*band.jobs, job)
    return bands


def expected_minutes(content_pages: int, at_once: int = 32) -> int:
    """About how long a full check of `content_pages` content pages takes, in whole minutes
    (reading done at upload), with `at_once` Sonnet places (`rate_limits`): the page helpers
    (two per group of 3-5 pages; more than the places wait for one), each band's chain after them,
    then the set-wide steps. A plan check's estimate, never a promise."""
    if content_pages <= 0:
        return 0
    places = max(2, at_once)
    size = min(5, max(3, math.ceil(content_pages / max(1, places // 2))))
    helpers = 2 * math.ceil(content_pages / size)
    waves = max(1, math.ceil(helpers / places))
    bands = 1 if content_pages <= BANDS_FROM else max(2, round(content_pages / BAND_PAGES))
    chain = _HELPER_S * waves + _HELPER_TAIL_S + _GAPS_S + _PROVERS_S + _LEAD_S + _AGAIN_S
    if bands == 1:
        # One chain over the whole set: its lead review and look-again grow with the pages.
        chain += _LEAD_S * max(0.0, (content_pages - BAND_PAGES) / BAND_PAGES)
    reduce = _REDUCE_S + _REDUCE_PAGE_S * content_pages
    return max(1, math.ceil((chain + reduce) / 60))


def run_chains(chains: Sequence[Callable[[], None]], name: str = "gv-roster-band") -> list[str]:
    """Runs each chain on its own thread and waits for all; a chain that raises is logged and
    named in the list returned (the others go on)."""
    failed: list[str] = []
    lock = threading.Lock()

    def guarded(index: int, chain: Callable[[], None]) -> None:
        try:
            chain()
        except Exception as error:  # noqa: BLE001 - one band failing never stops the others
            _log.exception("assistant roster chain %d failed", index + 1)
            with lock:
                failed.append(f"band {index + 1}: {type(error).__name__}")

    threads = [
        threading.Thread(target=guarded, args=(index, chain), name=f"{name}-{index + 1}")
        for index, chain in enumerate(chains)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return failed
