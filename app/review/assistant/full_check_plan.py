"""A full check's helper jobs, planned by code from the set pack's page map (#1512).

A full check used to start with the lead reading the page map and briefing helpers itself: a few
slow turns of the most expensive model, and helpers that came in waves. Code can make the same
split at once from `page_map.json` (`set_pack.py`), so every helper starts in the first seconds:

- **Divider and general pages** (no pasted drawing on them) are not compared: code records them as
  `not_comparable` in the coverage, with the reason.
- **An area stays together.** The pages of one area or title GV typed ("2. Buffet": the
  architect's drawing, the vendor's rounds, a brand prototype) are one unit, in page order; a page
  with no area typed is a unit of its own. A unit longer than `MAX_JOB_PAGES` is cut, and each later
  part gets the unit's architect pages to look at (not to compare again).
- **Jobs of 3-5 pages, balanced.** The units are cut into contiguous jobs, as many as it takes so no
  job passes `MAX_JOB_PAGES` and, when the set is large enough, about one job per helper that may
  run at once; among the cuts with the fewest pages in the largest job, the most even one is kept.
- **Other uploads** (an architect's PDF beside the vendor's): their pages of the same area go to
  that area's job, to look at.

- **Two lenses per group (#1569)**: with `with_lenses`, each job is run twice, by a NUMBERS helper
  (sizes and strings) and a DRAWN-THINGS helper (notes, doors and drawers, hardware, extent, the
  brand prototype, finish tags) on Sonnet at low effort, each drawn-things helper within its own
  money cap (`drawn_job_cap`). The numbers jobs come first, so when helpers queue past the parallel
  limit the drawn-things ones wait, not the numbers.
- **The reviewer's plan check (#1569)**: the answers given in the Run popover (`plan_check.py`)
  are laid over the page map when it is read: a drawing's role or round, a page paired with an
  architect page by hand (`pair_page`, given to look at), a page skipped (recorded as not
  comparable).

Everything here is plain code on the page map: no PDF is opened, nothing is drawn.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import monotonic
from typing import Any, Final

from app.review.assistant.chat_findings import page_ranges
from app.review.assistant.plan_check import (
    apply_answers,
    assumptions,
    read_answers,
    with_assumptions,
)
from app.review.assistant.product_scope import ProductScope, outside_unit
from app.review.assistant.simple_check.pairing import UNPAIRED

__all__ = [
    "DRAWN_JOB_CAP_MICROS",
    "DRAWN_TOTAL_CAP_MICROS",
    "MAX_JOB_PAGES",
    "MIN_JOB_PAGES",
    "NO_VENDOR_NOTE",
    "PAGE_MAP_WAIT_SECONDS",
    "UNPAIRED_NEEDS",
    "UNPAIRED_NOTE",
    "UNPAIRED_RETRY_NOTE",
    "UNPAIRED_WARN_SHARE",
    "CrossPairs",
    "FanOutPlan",
    "HelperJob",
    "Reading",
    "drawn_job_cap",
    "mostly_unpaired",
    "no_vendor_pages",
    "pack_ready",
    "pair_lines",
    "plan_assumptions",
    "plan_jobs",
    "read_page_map",
    "reading_of",
    "rules_summary",
    "scoped_plan",
    "unpaired_note_why",
    "unpaired_words",
    "wait_for_page_map",
    "wait_for_reading",
    "with_lenses",
    "with_pairs",
]

_log = logging.getLogger(__name__)

MIN_JOB_PAGES: Final = 3
MAX_JOB_PAGES: Final = 5
#: How long a full check waits for the set pack's page map (it is made in about 15 s, once per
#: upload); without it the lead plans the helpers itself, as in a chat.
PAGE_MAP_WAIT_SECONDS: Final = 60.0
_POLL_SECONDS: Final = 0.25
#: What a job's description may say of its areas.
_MAX_DESCRIPTION: Final = 100
DIVIDER_NOTE: Final = "divider or general page: no drawing pasted on it (code)"
#: A page whose page map shows drawings but no vendor drawing (#1837): its coverage note.
NO_VENDOR_NOTE: Final = (
    "no vendor drawing to compare: only the architect's or the brand prototype's drawing is on "
    "this page; it is the reference for its area's vendor pages (code)"
)
#: The drawing roles that are never the vendor's (`set_pack`'s page map).
_NOT_VENDOR_ROLES: Final = frozenset({"architect", "brand_prototype"})
#: The rules summary in a brief: the picked products' checks, one line each, at most this long.
_MAX_RULES_SUMMARY: Final = 2_500
_MAX_RULE_LINE: Final = 220
_CHECK_HEADING: Final = re.compile(r"^###\s+(.+?)\s*$")
#: A drawn-things helper's money (#1569): at most this much each, and all of them together at
#: most `DRAWN_TOTAL_CAP_MICROS` (research estimate: about $1.5-2.5 a 60-page set).
DRAWN_JOB_CAP_MICROS: Final = 350_000
DRAWN_TOTAL_CAP_MICROS: Final = 3_000_000
_LENS_WORDS: Final = {"numbers": "numbers", "drawn": "drawn things"}


@dataclass(frozen=True, slots=True)
class HelperJob:
    """One helper's part of the set."""

    number: int
    pages: tuple[int, ...]
    """The vendor's file's pages it compares (its coverage)."""
    file: str
    reference: tuple[tuple[str, int], ...] = ()
    """Pages to look at, not to compare again: (file, page)."""
    areas: tuple[str, ...] = ()
    lens: str = "both"
    """`both` (one helper per group), or `numbers` / `drawn` (#1569)."""
    partner: int | None = None
    """The other lens's job on the same pages (its number)."""
    fold: tuple[int, ...] = ()
    """When the numbers helpers have smaller page groups than the drawn-things helper
    (`GV_ASSISTANT_NUMBERS_HELPER_PAGES` below 5): the other jobs of its fold group, the
    drawn-things job and the numbers jobs on its pages, whose duplicates are folded once all have
    reported. Empty: one partner, as before."""
    cap_micros: int | None = None
    """A drawn-things helper's own money cap."""
    pairs: tuple[tuple[int, tuple[tuple[str, int], ...]], ...] = ()
    """Its pages paired across separate uploads (#1798): (page, the architect's (file, page)s)."""
    unpaired: tuple[tuple[int, str], ...] = ()
    """Its pages no architect drawing was paired with across the uploads, and why (#1798)."""

    @property
    def description(self) -> str:
        words = f"Pages {page_ranges(self.pages)}"
        if self.lens in _LENS_WORDS:
            words += f" · {_LENS_WORDS[self.lens]}"
        if self.areas:
            words += " · " + ", ".join(self.areas)
        return words if len(words) <= _MAX_DESCRIPTION else words[: _MAX_DESCRIPTION - 1] + "…"


@dataclass(frozen=True, slots=True)
class FanOutPlan:
    jobs: tuple[HelperJob, ...]
    skipped: tuple[tuple[int, str], ...]
    """Pages code records as not comparable: (page, why)."""

    @property
    def pages(self) -> int:
        """The pages compared (each page once, whatever its lenses)."""
        return len({page for job in self.jobs for page in job.pages})


@dataclass(frozen=True, slots=True)
class CrossPairs:
    """The vendor pages paired across separate uploads (#1798), from the work list
    (`simple_check.worklist`, code and the AI page matcher agree): each page's architect
    (file, page)s, and the pages not paired with the reason."""

    pairs: Mapping[int, tuple[tuple[str, int], ...]] = field(default_factory=dict)
    unpaired: Mapping[int, str] = field(default_factory=dict)
    words: Mapping[int, str] = field(default_factory=dict)
    """How each paired page was paired (plain words)."""
    references: Mapping[int, tuple[tuple[str, int], ...]] = field(default_factory=dict)
    """Each paired page's brand prototype (file, page)s: reference only, never its shown page
    (`reference_page`), but a spot on one is on the item's own drawings."""
    file: str | None = None
    """The vendor's file the pairs' page numbers are of (the work list's file): an item on another
    vendor file (an older round uploaded on its own) is not on these pages."""

    @classmethod
    def of(cls, work: Any) -> CrossPairs:
        """From a work list: its items paired across the uploads, and its vendor drawings skipped
        as not paired (a page with any pair counts as paired)."""
        pairs: dict[int, list[tuple[str, int]]] = {}
        words: dict[int, str] = {}
        references: dict[int, list[tuple[str, int]]] = {}
        for item in getattr(work, "items", ()):
            if item.paired_by is None:
                continue
            page = item.vendor.own_page
            found = pairs.setdefault(page, [])
            for drawing in item.architect:
                where = (drawing.file, drawing.own_page)
                if where not in found:
                    found.append(where)
            words.setdefault(page, item.paired_by)
            kept = references.setdefault(page, [])
            for drawing in getattr(item, "reference", ()):
                where = (drawing.file, drawing.own_page)
                if where not in kept:
                    kept.append(where)
        unpaired: dict[int, str] = {}
        main = getattr(work, "file", None)
        for skipped in getattr(work, "skipped", ()):
            if (
                skipped.file == main
                and skipped.drawing
                and skipped.reason.startswith(UNPAIRED)
                and skipped.page not in pairs
            ):
                unpaired.setdefault(skipped.page, skipped.reason)
        return cls(
            {p: tuple(v) for p, v in pairs.items()},
            unpaired,
            words,
            {p: tuple(v) for p, v in references.items() if v},
            str(main) if main else None,
        )


@dataclass(slots=True)
class _Unit:
    pages: list[int]
    area: str | None
    key: tuple[str | None, str | None]
    architect: list[int]


# ---- the page map ---------------------------------------------------------------------------------


def _raw_page_map(folder: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads((folder / "page_map.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("pages"), list):
        return None
    return loaded


def read_page_map(
    folder: Path | None, *, assume: bool = False, main_file: str | None = None
) -> dict[str, Any] | None:
    """The pack's `page_map.json` with the reviewer's plan-check answers laid over it
    (`plan_check.apply_answers`), or `None` when it is not there (yet) or not readable. With
    `assume` (a full check, #1581), code's likely answer is laid over every question nobody
    answered too (`plan_check.with_assumptions`): the run never waits for a click."""
    if folder is None:
        return None
    loaded = _raw_page_map(folder)
    if loaded is None:
        return None
    # A page the reading could not read (#1872) is never compared blind: it is skipped, with why.
    unreadable = reading_of(folder).unreadable
    for entry in loaded.get("pages") or []:
        if isinstance(entry, dict):
            why = unreadable.get((str(entry.get("file")), int(entry.get("page") or 0)))
            if why and not entry.get("skip"):
                entry["skip"] = why
    if assume:
        return with_assumptions(loaded, main_file, read_answers(folder))
    return apply_answers(loaded, read_answers(folder))


def plan_assumptions(folder: Path | None, main_file: str | None) -> list[str]:
    """What code assumed about the page map for a full check, one plain line each, for the cloud
    drawing's cover ("page 28, the lower drawing (no heading) is the vendor's drawing.")."""
    if folder is None:
        return []
    loaded = _raw_page_map(folder)
    if loaded is None:
        return []
    answers = read_answers(folder)
    answered = apply_answers(loaded, answers)
    return [words for _doubt, _value, words in assumptions(answered, main_file, answers)]


def _pack_state(folder: Path) -> tuple[str | None, list[str]]:
    try:
        status = json.loads((folder / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, []
    if not isinstance(status, dict):
        return None, []
    done = status.get("done")
    return status.get("state"), [str(d) for d in done] if isinstance(done, list) else []


# ---- the reading, page by page (#1872) -------------------------------------------------------

#: The set pack's reading table (`set_pack.READING_FILE`; that module is not imported here).
READING_FILE: Final = "reading.json"
#: How often a wait for the reading looks again (seconds).
READING_POLL_SECONDS: Final = 2.0


@dataclass(frozen=True, slots=True)
class Reading:
    """Where the reading of a set stands (`reading_of`): `state` is `reading` (pages still being
    read), `ready` (every page read or known unreadable: the labels are settled), `failed` (the
    pack could not be made, or not one page was read: `why`), or `none` (no pack). `done` of
    `total` pages are read or known unreadable; `ready` holds the pages read (file, page) and
    `unreadable` each page that could not be read, with why ("page N could not be read: ...")."""

    state: str
    done: int = 0
    total: int = 0
    ready: frozenset[tuple[str, int]] = frozenset()
    unreadable: dict[tuple[str, int], str] = field(default_factory=dict)
    why: str | None = None


def reading_of(folder: Path | None) -> Reading:
    """The reading's state now, from the pack's `status.json` and its reading table: the one
    place a full check (and the plan, and the live card) asks whether a page may be compared."""
    if folder is None or not Path(folder).is_dir():
        return Reading("none")
    status: dict[str, Any] = {}
    try:
        loaded = json.loads((folder / "status.json").read_text(encoding="utf-8"))
        status = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        pass
    try:
        table = json.loads((folder / READING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        table = None
    rows = table.get("pages") if isinstance(table, dict) else None
    ready: set[tuple[str, int]] = set()
    unreadable: dict[tuple[str, int], str] = {}
    waiting: list[tuple[str, int]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        key = (str(row.get("file")), int(row.get("page") or 0))
        if row.get("state") == "ready":
            ready.add(key)
        elif row.get("state") == "failed":
            unreadable[key] = str(row.get("error") or f"page {key[1]} could not be read")
        else:
            waiting.append(key)
    state = status.get("state")
    total = len(rows) if isinstance(rows, list) else 0
    if not total:
        mapped = _raw_page_map(folder)
        total = len((mapped or {}).get("pages") or [])
    if state == "failed":
        why = str(status.get("reason") or status.get("error") or "the drawings could not be read")
        return Reading("failed", len(ready) + len(unreadable), total, frozenset(), {}, why)
    if state in ("ready", "ready_without_labels"):
        if state == "ready_without_labels":
            # The labels stopped (a time limit): the pages not read by then are unreadable.
            stopped = str(status.get("labels_error") or "stopped")
            for key in waiting:
                unreadable[key] = f"page {key[1]} could not be read: the reading {stopped}"
            if rows is None or not ready:
                why = f"the drawings could not be read in full (the reading {stopped})"
                return Reading("failed", 0, total, frozenset(), unreadable, why)
        return Reading("ready", total, total, frozenset(ready), unreadable)
    return Reading("reading", len(ready) + len(unreadable), total, frozenset(ready), unreadable)


def wait_for_reading(
    folder: Path | None,
    stop: threading.Event,
    *,
    on_progress: Callable[[Reading], None] | None = None,
    poll: float = READING_POLL_SECONDS,
) -> Reading:
    """Wait until the reading of the set is finished (`ready`) or `failed`, telling `on_progress`
    each time the count of pages read changes. Returns the reading (`state` `stopped` when `stop`
    was set first). Costs nothing: no AI call is made while it waits. The pack's own time limits
    end every reading, so this ends too."""
    seen = -1
    while True:
        reading = reading_of(folder)
        if reading.state in ("ready", "failed", "none"):
            if on_progress is not None and reading.state == "ready":
                on_progress(reading)
            return reading
        if on_progress is not None and reading.done != seen:
            seen = reading.done
            on_progress(reading)
        if stop.wait(poll):
            return Reading("stopped", reading.done, reading.total)


def pack_ready(folder: Path | None) -> bool:
    """Whether the whole pack is made (its page pictures too; its labels may have failed: #1768
    `ready_without_labels`)."""
    return folder is not None and _pack_state(folder)[0] in ("ready", "ready_without_labels")


def pack_without_labels(folder: Path | None) -> bool:
    """Whether the pack's labels may be missing or partial (#1768), so code's own pairs are too:
    any state but `ready` (its labels step broke or ran out of time, or the pack is being made
    again: a labels time-out is rebuilt by the full check's own message, `ensure_pack`). False
    when the set has no pack folder at all."""
    return folder is not None and folder.is_dir() and _pack_state(folder)[0] != "ready"


def wait_for_page_map(
    folder: Path | None,
    stop: threading.Event,
    timeout: float = PAGE_MAP_WAIT_SECONDS,
    *,
    assume: bool = False,
    main_file: str | None = None,
) -> dict[str, Any] | None:
    """The page map once the pack has written it (its `page_map` step is done, or the pack is
    ready); `None` when there is no pack, it failed, `stop` is set or `timeout` passes. `assume`:
    with code's likely answers over the questions nobody answered (`read_page_map`)."""
    if folder is None or not folder.is_dir():
        return None
    ends = monotonic() + timeout
    while True:
        state, done = _pack_state(folder)
        if state == "ready" or "page_map" in done:
            return read_page_map(folder, assume=assume, main_file=main_file)
        if state == "failed" or monotonic() >= ends:
            return None
        if stop.wait(_POLL_SECONDS):
            return None


# ---- the split ------------------------------------------------------------------------------------


def _entries(page_map: Mapping[str, Any], main_file: str | None) -> list[Mapping[str, Any]]:
    entries = [e for e in page_map.get("pages") or [] if isinstance(e, Mapping)]
    files = {str(e.get("file")) for e in entries}
    if main_file is None or main_file not in files:
        if len(files) != 1:
            return []
        main_file = next(iter(files))
    chosen = [
        e for e in entries if str(e.get("file")) == main_file and isinstance(e.get("page"), int)
    ]
    return sorted(chosen, key=lambda e: int(e["page"]))


def no_vendor_pages(page_map: Mapping[str, Any] | None, main_file: str | None) -> frozenset[int]:
    """The main file's pages that hold drawings but no vendor drawing (#1837): every drawing on
    them is the architect's or the brand prototype's (a drawing of unknown role keeps the page
    in). Nothing on such a page can be compared: it is the reference for its area's vendor pages,
    so it is never a page "not compared" and is never sent on for another look. The paid run of
    2026-10-21 kept four such pages "not compared after 2 more looks" and the check not for
    sending."""
    if not page_map:
        return frozenset()
    out: set[int] = set()
    for entry in _entries(page_map, main_file):
        halves = [h for h in entry.get("halves") or [] if isinstance(h, Mapping)]
        if halves and all(str(h.get("role")) in _NOT_VENDOR_ROLES for h in halves):
            out.add(int(entry["page"]))
    return frozenset(out)


def _area(entry: Mapping[str, Any]) -> str | None:
    area = entry.get("area")
    return " ".join(str(area).split()) or None if isinstance(area, str) else None


def _units(content: Sequence[Mapping[str, Any]]) -> list[_Unit]:
    units: list[_Unit] = []
    for entry in content:
        page = int(entry["page"])
        area = _area(entry)
        section = entry.get("section") if isinstance(entry.get("section"), str) else None
        key = (section, area)
        roles = {str(half.get("role")) for half in entry.get("halves") or []}
        last = units[-1] if units else None
        if last is not None and area is not None and last.key == key and page == last.pages[-1] + 1:
            last.pages.append(page)
            if "architect" in roles:
                last.architect.append(page)
            continue
        units.append(_Unit([page], area, key, [page] if "architect" in roles else []))
    return units


def _split_long(units: Sequence[_Unit], most: int = MAX_JOB_PAGES) -> list[tuple[_Unit, list[int]]]:
    """Units of at most `most` pages (`MAX_JOB_PAGES`), each with the pages it looks at only: a
    long unit is cut, and its later parts look at its architect pages."""
    out: list[tuple[_Unit, list[int]]] = []
    for unit in units:
        if len(unit.pages) <= most:
            out.append((unit, []))
            continue
        for start in range(0, len(unit.pages), most):
            part = unit.pages[start : start + most]
            looks = [] if start == 0 else [p for p in unit.architect if p not in part]
            out.append((_Unit(part, unit.area, unit.key, []), looks))
    return out


def _partition(sizes: Sequence[int], groups: int) -> list[int]:
    """Where to cut `sizes` (in order) into `groups` contiguous groups: the cut with the smallest
    largest group, then the most even (smallest sum of squares). Returns each group's end index."""
    count = len(sizes)
    groups = max(1, min(groups, count))
    prefix = [0]
    for size in sizes:
        prefix.append(prefix[-1] + size)
    infinite = (math.inf, math.inf)
    # best[k][j]: the best (largest, sum of squares) for the first j units in k groups.
    best = [[infinite] * (count + 1) for _ in range(groups + 1)]
    cut = [[0] * (count + 1) for _ in range(groups + 1)]
    best[0][0] = (0, 0)
    for k in range(1, groups + 1):
        for j in range(k, count - (groups - k) + 1):
            for i in range(k - 1, j):
                before = best[k - 1][i]
                if before == infinite:
                    continue
                size = prefix[j] - prefix[i]
                score = (max(before[0], size), before[1] + size * size)
                if score < best[k][j]:
                    best[k][j], cut[k][j] = score, i
    ends: list[int] = []
    j = count
    for k in range(groups, 0, -1):
        ends.append(j)
        j = cut[k][j]
    return list(reversed(ends))


def plan_jobs(
    page_map: Mapping[str, Any] | None,
    main_file: str | None,
    parallel: int,
    outside: Callable[[Sequence[int]], str | None] | None = None,
    across: CrossPairs | None = None,
) -> FanOutPlan | None:
    """The full check's helper jobs (see the module's words); `None` when the page map gives no
    drawing to compare (then the lead plans, as in a chat). `outside` (a strict product scope,
    `product_scope.outside_unit`): why an area's pages hold only products the check does not
    cover; those pages are recorded as not compared, with that reason, and get no helper.
    `across` (#1798): the vendor pages paired across separate uploads (code and the AI page
    matcher agree, `cross_pairs`): each job looks at its pages' paired architect pages, and is
    told which of its pages are paired with which, and which are not paired."""
    if not page_map:
        return None
    entries = _entries(page_map, main_file)
    if not entries:
        return None
    content = [e for e in entries if e.get("halves") and not e.get("skip")]
    if not content:
        return None
    skipped = tuple(
        (int(e["page"]), str(e["skip"]) if e.get("skip") else DIVIDER_NOTE)
        for e in entries
        if not e.get("halves") or e.get("skip")
    )
    if outside is not None:
        left: dict[int, str] = {}
        for unit in _units(content):
            why = outside(tuple(unit.pages))
            if why is not None:
                left.update(dict.fromkeys(unit.pages, why))
        if left and len(left) < len(content):
            content = [e for e in content if int(e["page"]) not in left]
            skipped = tuple(sorted((*skipped, *left.items())))
    paired = {
        int(e["page"]): int(e["pair_page"]) for e in content if isinstance(e.get("pair_page"), int)
    }
    parts = _split_long(_units(content))
    sizes = [len(unit.pages) for unit, _looks in parts]
    total = sum(sizes)
    target = min(MAX_JOB_PAGES, max(MIN_JOB_PAGES, math.ceil(total / max(1, parallel))))
    groups = max(1, math.ceil(total / target))
    while True:
        ends = _partition(sizes, groups)
        starts = [0, *ends[:-1]]
        largest = max(sum(sizes[a:b]) for a, b in zip(starts, ends, strict=True))
        if largest <= MAX_JOB_PAGES or groups >= len(sizes):
            break
        groups += 1
    file = str(content[0].get("file"))
    others = [
        e
        for e in page_map.get("pages") or []
        if isinstance(e, Mapping) and str(e.get("file")) != file and e.get("halves")
    ]
    jobs: list[HelperJob] = []
    for number, (start, end) in enumerate(zip(starts, ends, strict=True), start=1):
        chosen = parts[start:end]
        pages = tuple(p for unit, _looks in chosen for p in unit.pages)
        areas: list[str] = []
        for unit, _looks in chosen:
            if unit.area and unit.area not in areas:
                areas.append(unit.area)
        looks = sorted(
            (
                {p for _unit, extra in chosen for p in extra}
                | {paired[p] for p in pages if p in paired}
            )
            - set(pages)
        )
        reference = [(file, p) for p in looks]
        wanted = {area.lower() for area in areas}
        reference += [
            (str(e.get("file")), int(e["page"]))
            for e in others
            if isinstance(e.get("page"), int) and (_area(e) or "").lower() in wanted
        ]
        jobs.append(
            with_pairs(
                HelperJob(number, pages, file, tuple(dict.fromkeys(reference)), tuple(areas)),
                across,
            )
        )
    return FanOutPlan(tuple(jobs), skipped)


def with_pairs(job: HelperJob, across: CrossPairs | None) -> HelperJob:
    """The job told which of its pages are paired across separate uploads (#1798) and with
    which architect pages (those pages added to what it looks at), and which are not paired and
    why. A full check plans its jobs without them and adds them as each job starts, once the AI
    page matcher has decided its pages (#1883); `plan_jobs(across=...)` gives the same jobs.
    """
    if across is None:
        return job
    reference = list(job.reference)
    paired_pages: list[tuple[int, tuple[tuple[str, int], ...]]] = []
    unpaired: list[tuple[int, str]] = []
    for page in job.pages:
        if page in across.pairs:
            paired_pages.append((page, across.pairs[page]))
            reference += [p for p in across.pairs[page] if p not in reference]
        elif page in across.unpaired:
            unpaired.append((page, across.unpaired[page]))
    return replace(
        job,
        reference=tuple(dict.fromkeys(reference)),
        pairs=tuple(paired_pages),
        unpaired=tuple(unpaired),
    )


def scoped_plan(
    page_map: Mapping[str, Any] | None,
    main_file: str | None,
    parallel: int,
    scope: ProductScope,
    texts: Callable[[Sequence[str]], Mapping[tuple[str, int], str]] | None,
    across: CrossPairs | None = None,
) -> tuple[FanOutPlan | None, int]:
    """A full check's helper jobs for the products in scope, and the pages the whole set would
    have compared: an area whose every page names only out-of-scope products gets no helper
    (`product_scope.outside_unit`, read from the pages' printed words by `texts(files)`, $0). The
    plan's `pages` are the content pages in scope the price counts (#1823); the full check and the
    plan check before it both count them here."""
    plan = plan_jobs(page_map, main_file, parallel, across=across)
    if plan is None or scope.every or texts is None:
        return plan, 0 if plan is None else plan.pages
    files = [str(name) for name in (page_map or {}).get("files") or [] if name]
    if not files and main_file:
        files = [main_file]
    try:
        read = texts(files)
    except Exception as failed:  # noqa: BLE001 - every area is then checked
        _log.warning("assistant chat page words not read: %s", type(failed).__name__)
        return plan, plan.pages
    words = {page: text for (name, page), text in read.items() if name == main_file}
    scoped = plan_jobs(
        page_map,
        main_file,
        parallel,
        outside=lambda pages: outside_unit(pages, words, scope),
        across=across,
    )
    return scoped, plan.pages


def pair_lines(
    pairs: Sequence[tuple[int, Sequence[tuple[str, int]]]],
    unpaired: Sequence[tuple[int, str]],
    uploads: str,
) -> list[str]:
    """A helper's brief lines for its pages paired across separate uploads (#1798), and for those
    no architect drawing could be agreed on: empty when there are neither."""
    lines: list[str] = []
    if pairs:
        paired = "; ".join(
            f"your p.{page} with "
            + ", ".join(f"{uploads}/{name} p.{number}" for name, number in where)
            for page, where in pairs
        )
        lines.append(
            "PAIRED ACROSS THE UPLOADS (code and the AI page matcher agree on which architect "
            f"drawing shows the same piece): {paired}. Compare each of these pages only with its "
            "paired architect pages."
        )
    if unpaired:
        left = "; ".join(f"p.{page}: {why}" for page, why in unpaired)
        lines.append(
            "NOT PAIRED (no architect drawing of the same piece could be agreed on): "
            f"{left}. Do not pick an architect drawing for these pages yourself (a wrong pair "
            "makes false differences) and compare nothing on them with an architect: record "
            "each one Not compared with that reason. Still do the checks that need no "
            "architect there: record each vendor dimension string (its parts and its printed "
            "overall) as usual."
        )
    return lines


# ---- pages no architect drawing could be paired with (#1885) --------------------------------------

#: More than this share of the vendor pages the check compares with no architect drawing paired
#: across the uploads (#1885): the summary says so in its first line. Past half, the check can never
#: be a complete one and the cause is almost always the set itself (the architect's file is for
#: another job, or lacks these items), which only the reviewer can fix; under half, the unpaired
#: pages are listed in the Not-compared list like any other page that could not be compared.
UNPAIRED_WARN_SHARE: Final = 0.5
#: An unpaired page's coverage note: final, not "open".
UNPAIRED_NOTE: Final = "Not compared with the architect: no architect drawing found for it ({why})"
#: A page whose pairing did not finish (the page matcher had no answer): Continue asks again.
UNPAIRED_RETRY_NOTE: Final = (
    "not compared with the architect: no architect drawing paired with it yet ({why}); Continue "
    "asks again"
)
#: An unpaired page's line in the Not-compared list (the set's rows): what would settle it.
UNPAIRED_NEEDS: Final = (
    "the architect's drawing of this item: upload it, or check that the architect's file is for "
    "the same project"
)

#: The pairing's reasons in plain words, for the reviewer's screen and the files (vendor-facing:
#: never "AI", never code's drawing ids). The first pattern that matches gives the words.
_UNPAIRED_PLAIN: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # #1889, "drawing needed" said up front: the architect's file lacks it, or only outlines it.
    (
        re.compile(r"no drawings of that project", re.IGNORECASE),
        "the architect's file has no drawings of its project",
    ),
    (
        re.compile(r"only as a tag on a plan", re.IGNORECASE),
        "the architect shows it only as a tag on a plan",
    ),
    (
        re.compile(
            r"only as an outline|print(?:s)? no sizes|shows this item without sizes", re.IGNORECASE
        ),
        "the architect shows this item without sizes",
    ),
    (
        re.compile(r"no architect drawing to pair it with", re.IGNORECASE),
        "the architect's file has no drawing to pair it with",
    ),
    (
        re.compile(r"ruled out|found no architect drawing of the same item", re.IGNORECASE),
        "no drawing of the same item was found in the architect's file",
    ),
    (
        re.compile(r"prints its dimensions|too few dimensions", re.IGNORECASE),
        "no architect drawing prints its dimensions",
    ),
    (
        re.compile(r"match its dimensions equally", re.IGNORECASE),
        "two architect drawings match it equally",
    ),
    (
        re.compile(r"code found no clear architect drawing", re.IGNORECASE),
        "no architect drawing could be identified for it",
    ),
)
_UNPAIRED_DEFAULT: Final = "the architect's drawing of this item could not be identified for sure"


def unpaired_words(reason: str) -> str:
    """A pairing reason (`pairing.UNPAIRED`: ...) in plain words: "no drawing of the same item was
    found in the architect's file", never the matcher's or code's working words."""
    for pattern, words in _UNPAIRED_PLAIN:
        if pattern.search(reason or ""):
            return words
    return _UNPAIRED_DEFAULT


def unpaired_note_why(note: str | None) -> str | None:
    """The plain reason of a page's final "no architect drawing found" coverage note (#1885:
    `UNPAIRED_NOTE`, the words in its brackets), else `None` (any other note, or none). The result
    screen's "Drawing needed" reads it (`ChatCoveragePageOut.drawing_needed`)."""
    head = UNPAIRED_NOTE.split("{", 1)[0]
    text = " ".join(str(note or "").split())
    if not text.startswith(head):
        return None
    why = text[len(head) :].split(")", 1)[0].strip()
    return why or _UNPAIRED_DEFAULT


def mostly_unpaired(unpaired: int, pages: int) -> bool:
    """Whether more than `UNPAIRED_WARN_SHARE` of the `pages` compared have no architect drawing
    paired with them (#1885): the summary then says so first, in plain words."""
    return pages > 0 and unpaired > pages * UNPAIRED_WARN_SHARE


def drawn_job_cap(jobs: int) -> int:
    """Each drawn-things helper's money: `DRAWN_JOB_CAP_MICROS`, less when there are so many that
    together they would pass `DRAWN_TOTAL_CAP_MICROS`."""
    return min(DRAWN_JOB_CAP_MICROS, DRAWN_TOTAL_CAP_MICROS // max(1, jobs))


def with_lenses(
    plan: FanOutPlan,
    numbers_pages: int = MAX_JOB_PAGES,
    page_map: Mapping[str, Any] | None = None,
) -> FanOutPlan:
    """The plan with two helpers per job (#1569): the numbers jobs first (numbered as before),
    then a drawn-things job for each, on the same pages, with its money cap and its partner.

    `numbers_pages` below `MAX_JOB_PAGES` (`GV_ASSISTANT_NUMBERS_HELPER_PAGES`, with the page map):
    only the numbers jobs are cut smaller, inside each job and by the same rule (an area stays
    together unless it is longer than a group), so a drawn-things job keeps its pages, its money
    cap and its count; it folds with the numbers jobs on its pages once all have reported (`fold`).
    The default gives today's plan exactly."""
    count = len(plan.jobs)
    cap = drawn_job_cap(count)
    if numbers_pages >= MAX_JOB_PAGES or page_map is None:
        numbers = [replace(job, lens="numbers", partner=count + job.number) for job in plan.jobs]
        drawn = [
            replace(
                job, number=count + job.number, lens="drawn", partner=job.number, cap_micros=cap
            )
            for job in plan.jobs
        ]
        return FanOutPlan((*numbers, *drawn), plan.skipped)
    parts = [_smaller(job, max(1, numbers_pages), page_map) for job in plan.jobs]
    total = sum(len(subs) for subs in parts)
    numbers = []
    drawn = []
    for job, subs in zip(plan.jobs, parts, strict=True):
        own = total + job.number
        first = len(numbers) + 1
        mine = tuple(range(first, first + len(subs)))
        for number, sub in zip(mine, subs, strict=True):
            fold = () if len(subs) == 1 else tuple(n for n in (*mine, own) if n != number)
            numbers.append(replace(sub, number=number, lens="numbers", partner=own, fold=fold))
        drawn.append(
            replace(
                job,
                number=own,
                lens="drawn",
                partner=mine[0],
                cap_micros=cap,
                fold=() if len(subs) == 1 else mine,
            )
        )
    return FanOutPlan((*numbers, *drawn), plan.skipped)


def _smaller(job: HelperJob, most: int, page_map: Mapping[str, Any]) -> list[HelperJob]:
    """One job's pages cut into numbers jobs of at most `most` pages, by the planner's own rule:
    its areas (`_units`) stay whole unless longer than `most` (`_split_long`), cut where the
    largest group is smallest and the groups most even (`_partition`). Each part looks at the
    job's own references, its paired architect pages and its long area's architect pages."""
    if len(job.pages) <= most:
        return [job]
    wanted = set(job.pages)
    entries = [e for e in _entries(page_map, job.file) if int(e["page"]) in wanted]
    if len(entries) != len(job.pages):
        return [job]  # the page map no longer matches the job: it is kept whole
    paired = {
        int(e["page"]): int(e["pair_page"]) for e in entries if isinstance(e.get("pair_page"), int)
    }
    pieces = _split_long(_units(entries), most)
    sizes = [len(unit.pages) for unit, _looks in pieces]
    groups = max(1, math.ceil(sum(sizes) / most))
    while True:
        ends = _partition(sizes, groups)
        starts = [0, *ends[:-1]]
        largest = max(sum(sizes[a:b]) for a, b in zip(starts, ends, strict=True))
        if largest <= most or groups >= len(sizes):
            break
        groups += 1
    out: list[HelperJob] = []
    for start, end in zip(starts, ends, strict=True):
        chosen = pieces[start:end]
        pages = tuple(p for unit, _looks in chosen for p in unit.pages)
        areas = tuple(dict.fromkeys(unit.area for unit, _looks in chosen if unit.area))
        looks = sorted(
            (
                {p for _unit, extra in chosen for p in extra}
                | {paired[p] for p in pages if p in paired}
            )
            - set(pages)
        )
        out.append(
            replace(
                job,
                pages=pages,
                areas=areas,
                reference=tuple(dict.fromkeys((*job.reference, *((job.file, p) for p in looks)))),
                pairs=tuple(pair for pair in job.pairs if pair[0] in pages),
                unpaired=tuple(item for item in job.unpaired if item[0] in pages),
            )
        )
    return out


# ---- the rules summary ----------------------------------------------------------------------------


def rules_summary(rules_md: str) -> str:
    """The picked products' checks from `rules.md`, one line each ("ID: title: what it checks"),
    at most about 2,500 characters; empty when it lists none."""
    lines: list[str] = []
    heading: str | None = None
    for raw in rules_md.splitlines():
        line = raw.strip()
        if line.startswith("## Other products"):
            break
        found = _CHECK_HEADING.match(line)
        if found:
            if heading is not None:
                lines.append(heading)
            heading = found.group(1)
            continue
        if heading is not None and line.startswith("- What it checks:"):
            what = line.removeprefix("- What it checks:").strip()
            lines.append(f"{heading}: {what}")
            heading = None
    if heading is not None:
        lines.append(heading)
    out: list[str] = []
    used = 0
    for line in lines:
        shown = line if len(line) <= _MAX_RULE_LINE else line[: _MAX_RULE_LINE - 1] + "…"
        if used + len(shown) > _MAX_RULES_SUMMARY:
            out.append(f"(and {len(lines) - len(out)} more in rules.md)")
            break
        out.append(f"- {shown}")
        used += len(shown) + 3
    return "\n".join(out)
