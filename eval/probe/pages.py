"""Which pages a recall probe runs on (harness rebuild, Phase 1).

A full job is 18 to 52 vendor pages and most of them carry no known problem, so running all of
them to see whether a change finds more is paying for silence. The probe runs the smallest page set
that can still see the problems being tested, plus a few **control pages** that carry no known
problem at all.

The controls are the half of the measurement that stops a cheat. Recall alone is maximised by
flagging everything, and nothing in a miss table can tell you that happened. A control page has
nothing to find, so every flag raised on one is noise, and `score` reports flags per control page
beside recall. A change that buys recall by shouting shows up there immediately.

**A problem needs either all of its pages or any one of them**, and which it is depends on the
kind. A cross-sheet conflict *is* the disagreement between two sheets: give the run one of them
and there is nothing to notice, so every page is needed. Every other kind is a defect printed on a
page, and where a miss names several pages it is the same defect repeated on each, so any one of
them tests whether the question fires. `RELATIONAL` holds the kinds of the first sort.

**Fewer problems, far fewer pages.** The cover for one job's whole miss list is most of the job
(setA: 30 of 52). The cover for one *kind* is three to five pages. A change usually targets one
family of problems, so `select(..., kinds=...)` is the cheap shape and the whole-job cover is the
expensive one; `scripts/recall_probe.py plan` prints both.

Nothing here reads a drawing, a run, a database or the network.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from eval.exam.truth import Miss

__all__ = [
    "JOBS",
    "RELATIONAL",
    "Selection",
    "Unit",
    "control_pages",
    "load_jobs",
    "select",
    "units_for",
]

DIR: Final = Path(__file__).resolve().parent
JOBS: Final = DIR / "jobs.tsv"

#: Kinds of problem that are a disagreement *between* sheets. The defect is not on any one page, so
#: a probe that leaves one of them out cannot see it however good the finding stage is.
RELATIONAL: Final = frozenset({"cross-sheet conflict"})


@dataclass(frozen=True)
class Unit:
    """One known problem, as the probe has to cover it."""

    miss_id: str
    kind: str
    severity: str
    pages: frozenset[int]
    #: True when every page is needed (the problem is the disagreement between them).
    relational: bool

    def covered_by(self, pages: frozenset[int]) -> bool:
        """Whether this page set can see the problem at all."""
        if self.relational:
            return self.pages <= pages
        return bool(self.pages & pages)

    def partly_by(self, pages: frozenset[int]) -> bool:
        """A relational problem with some but not all of its sheets: not a fair test."""
        return self.relational and bool(self.pages & pages) and not self.pages <= pages


def load_jobs(path: Path | None = None) -> dict[str, int]:
    """Each job's vendor page count, which is all the probe needs to pick control pages."""
    rows = (path or JOBS).read_text(encoding="utf-8").splitlines()
    return {row["job"]: int(row["vendor_pages"]) for row in csv.DictReader(rows, delimiter="\t")}


def units_for(
    job: str,
    misses: list[Miss],
    *,
    kinds: frozenset[str] | None = None,
    severities: frozenset[str] | None = None,
    miss_ids: frozenset[str] | None = None,
) -> list[Unit]:
    """The known problems of one job the probe is being asked to cover, newest filters last."""
    out: list[Unit] = []
    for miss in misses:
        if miss.job != job:
            continue
        if kinds is not None and miss.kind not in kinds:
            continue
        if severities is not None and miss.severity not in severities:
            continue
        if miss_ids is not None and miss.miss_id not in miss_ids:
            continue
        pages = frozenset(miss.pages)
        if not pages:
            continue
        out.append(
            Unit(
                miss_id=miss.miss_id,
                kind=miss.kind,
                severity=miss.severity,
                pages=pages,
                relational=miss.kind in RELATIONAL or len(pages) == 1,
            )
        )
    return sorted(out, key=lambda unit: unit.miss_id)


def _order(units: list[Unit]) -> list[int]:
    """Pages, best first: at each step the page that newly covers the most problems.

    Deterministic — ties go to the lower page number — so the same ask gives the same probe, and
    truncating the list at `max_pages` keeps the pages that buy the most.
    """
    chosen: set[int] = set()
    out: list[int] = []
    left = {page for unit in units for page in unit.pages}
    while left:
        frozen = frozenset(chosen)
        before = sum(unit.covered_by(frozen) for unit in units)
        if before == len(units):
            break
        best, gain = None, -1
        for page in sorted(left):
            after = frozenset(chosen | {page})
            score = sum(unit.covered_by(after) for unit in units) - before
            # Pages of a relational problem buy nothing until the last of them is in, so a page
            # that buys nothing yet still counts: break the tie by how many problems want it.
            score = score * 1000 + sum(page in unit.pages for unit in units)
            if score > gain:
                best, gain = page, score
        if best is None:  # pragma: no cover - `left` is non-empty, so a best always exists
            break
        chosen.add(best)
        out.append(best)
        left.discard(best)
    return _pruned(out, units)


def _pruned(order: list[int], units: list[Unit]) -> list[int]:
    """Drop any page the cover does not actually need.

    A greedy pass picks a page that buys nothing yet because a relational problem still wants it,
    and a later pick can make it redundant. Dropping those is pages nobody pays for. The order of
    what is left is untouched, so `max_pages` still keeps the most valuable pages first.
    """
    kept = list(order)
    full = sum(unit.covered_by(frozenset(kept)) for unit in units)
    for page in reversed(order):
        without = [item for item in kept if item != page]
        if sum(unit.covered_by(frozenset(without)) for unit in units) == full:
            kept = without
    return kept


def control_pages(job_pages: int, used: frozenset[int], want: int) -> tuple[int, ...]:
    """`want` pages of the job that no known problem names, spread evenly over the set.

    Evenly spread rather than random: a probe has to be repeatable, and a run-to-run difference
    must come from the finding stage, never from a different page landing in the control sample.
    """
    free = [page for page in range(1, job_pages + 1) if page not in used]
    if want <= 0 or not free:
        return ()
    if want >= len(free):
        return tuple(free)
    step = len(free) / want
    picked = sorted({free[min(len(free) - 1, int(index * step))] for index in range(want)})
    # Rounding can collide; fill from the front with whatever is left.
    for page in free:
        if len(picked) >= want:
            break
        if page not in picked:
            picked.append(page)
    return tuple(sorted(picked))


@dataclass(frozen=True)
class Selection:
    """The pages one probe runs, and what that page set can and cannot see."""

    job: str
    problem_pages: tuple[int, ...]
    #: Pages with no known problem, where every flag raised is noise.
    control_pages: tuple[int, ...]
    #: Problems this page set can see in full.
    covered: tuple[str, ...]
    #: Relational problems with some of their sheets in and some out: not a fair test, so they are
    #: left out of the recall denominator and reported on their own.
    partly: tuple[str, ...]
    #: Problems asked for that this page set cannot see at all.
    uncovered: tuple[str, ...]
    #: Every problem that was asked for, covered or not.
    asked: tuple[str, ...]

    @property
    def pages(self) -> tuple[int, ...]:
        """Every page the run is given, problem pages and controls together."""
        return tuple(sorted(set(self.problem_pages) | set(self.control_pages)))

    @property
    def scoreable(self) -> int:
        """The recall denominator: problems this page set is a fair test of."""
        return len(self.covered)


def select(
    job: str,
    misses: list[Miss],
    *,
    vendor_pages: int,
    kinds: frozenset[str] | None = None,
    severities: frozenset[str] | None = None,
    miss_ids: frozenset[str] | None = None,
    max_pages: int | None = None,
    controls: int = 2,
) -> Selection:
    """The smallest page set that covers the asked-for problems, plus `controls` quiet pages.

    `max_pages` caps the problem pages at the most valuable ones; the problems that drop out are
    reported in `uncovered` rather than silently lost, because a recall score whose denominator
    moved is not a score.
    """
    units = units_for(job, misses, kinds=kinds, severities=severities, miss_ids=miss_ids)
    ordered = _order(units)
    if max_pages is not None:
        ordered = ordered[: max(0, max_pages)]
    problem = frozenset(ordered)
    covered = tuple(unit.miss_id for unit in units if unit.covered_by(problem))
    partly = tuple(unit.miss_id for unit in units if unit.partly_by(problem))
    uncovered = tuple(
        unit.miss_id
        for unit in units
        if not unit.covered_by(problem) and unit.miss_id not in set(partly)
    )
    named = frozenset(page for unit in units_for(job, misses) for page in unit.pages)
    return Selection(
        job=job,
        problem_pages=tuple(sorted(problem)),
        control_pages=control_pages(vendor_pages, named, controls),
        covered=covered,
        partly=partly,
        uncovered=uncovered,
        asked=tuple(unit.miss_id for unit in units),
    )
