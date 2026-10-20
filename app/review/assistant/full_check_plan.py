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
from app.review.assistant.simple_check.pairing import UNPAIRED

__all__ = [
    "DRAWN_JOB_CAP_MICROS",
    "DRAWN_TOTAL_CAP_MICROS",
    "MAX_JOB_PAGES",
    "MIN_JOB_PAGES",
    "PAGE_MAP_WAIT_SECONDS",
    "CrossPairs",
    "FanOutPlan",
    "HelperJob",
    "drawn_job_cap",
    "pack_ready",
    "pair_lines",
    "plan_assumptions",
    "plan_jobs",
    "read_page_map",
    "rules_summary",
    "wait_for_page_map",
    "with_lenses",
]

MIN_JOB_PAGES: Final = 3
MAX_JOB_PAGES: Final = 5
#: How long a full check waits for the set pack's page map (it is made in about 15 s, once per
#: upload); without it the lead plans the helpers itself, as in a chat.
PAGE_MAP_WAIT_SECONDS: Final = 60.0
_POLL_SECONDS: Final = 0.25
#: What a job's description may say of its areas.
_MAX_DESCRIPTION: Final = 100
DIVIDER_NOTE: Final = "divider or general page: no drawing pasted on it (code)"
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

    @classmethod
    def of(cls, work: Any) -> CrossPairs:
        """From a work list: its items paired across the uploads, and its vendor drawings skipped
        as not paired (a page with any pair counts as paired)."""
        pairs: dict[int, list[tuple[str, int]]] = {}
        words: dict[int, str] = {}
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
        return cls({p: tuple(v) for p, v in pairs.items()}, unpaired, words)


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


def _split_long(units: Sequence[_Unit]) -> list[tuple[_Unit, list[int]]]:
    """Units of at most `MAX_JOB_PAGES` pages, each with the pages it looks at only: a long unit
    is cut, and its later parts look at its architect pages."""
    out: list[tuple[_Unit, list[int]]] = []
    for unit in units:
        if len(unit.pages) <= MAX_JOB_PAGES:
            out.append((unit, []))
            continue
        for start in range(0, len(unit.pages), MAX_JOB_PAGES):
            part = unit.pages[start : start + MAX_JOB_PAGES]
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
        paired_pages: list[tuple[int, tuple[tuple[str, int], ...]]] = []
        unpaired: list[tuple[int, str]] = []
        if across is not None:
            for page in pages:
                if page in across.pairs:
                    paired_pages.append((page, across.pairs[page]))
                    reference += [p for p in across.pairs[page] if p not in reference]
                elif page in across.unpaired:
                    unpaired.append((page, across.unpaired[page]))
        jobs.append(
            HelperJob(
                number,
                pages,
                file,
                tuple(dict.fromkeys(reference)),
                tuple(areas),
                pairs=tuple(paired_pages),
                unpaired=tuple(unpaired),
            )
        )
    return FanOutPlan(tuple(jobs), skipped)


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
            "makes false differences): record each one Not compared with that reason."
        )
    return lines


def drawn_job_cap(jobs: int) -> int:
    """Each drawn-things helper's money: `DRAWN_JOB_CAP_MICROS`, less when there are so many that
    together they would pass `DRAWN_TOTAL_CAP_MICROS`."""
    return min(DRAWN_JOB_CAP_MICROS, DRAWN_TOTAL_CAP_MICROS // max(1, jobs))


def with_lenses(plan: FanOutPlan) -> FanOutPlan:
    """The plan with two helpers per job (#1569): the numbers jobs first (numbered as before),
    then a drawn-things job for each, on the same pages, with its money cap and its partner."""
    count = len(plan.jobs)
    cap = drawn_job_cap(count)
    numbers = [replace(job, lens="numbers", partner=count + job.number) for job in plan.jobs]
    drawn = [
        replace(job, number=count + job.number, lens="drawn", partner=job.number, cap_micros=cap)
        for job in plan.jobs
    ]
    return FanOutPlan((*numbers, *drawn), plan.skipped)


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
