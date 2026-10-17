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

Everything here is plain code on the page map: no PDF is opened, nothing is drawn.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Final

from app.review.assistant.chat_findings import page_ranges

__all__ = [
    "MAX_JOB_PAGES",
    "MIN_JOB_PAGES",
    "PAGE_MAP_WAIT_SECONDS",
    "FanOutPlan",
    "HelperJob",
    "pack_ready",
    "plan_jobs",
    "read_page_map",
    "rules_summary",
    "wait_for_page_map",
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

    @property
    def description(self) -> str:
        words = f"Pages {page_ranges(self.pages)}"
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
        return sum(len(job.pages) for job in self.jobs)


@dataclass(slots=True)
class _Unit:
    pages: list[int]
    area: str | None
    key: tuple[str | None, str | None]
    architect: list[int]


# ---- the page map ---------------------------------------------------------------------------------


def read_page_map(folder: Path | None) -> dict[str, Any] | None:
    """The pack's `page_map.json`, or `None` when it is not there (yet) or not readable."""
    if folder is None:
        return None
    try:
        loaded = json.loads((folder / "page_map.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) and isinstance(loaded.get("pages"), list) else None


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
    """Whether the whole pack is made (its page pictures too)."""
    return folder is not None and _pack_state(folder)[0] == "ready"


def wait_for_page_map(
    folder: Path | None,
    stop: threading.Event,
    timeout: float = PAGE_MAP_WAIT_SECONDS,
) -> dict[str, Any] | None:
    """The page map once the pack has written it (its `page_map` step is done, or the pack is
    ready); `None` when there is no pack, it failed, `stop` is set or `timeout` passes."""
    if folder is None or not folder.is_dir():
        return None
    ends = monotonic() + timeout
    while True:
        state, done = _pack_state(folder)
        if state == "ready" or "page_map" in done:
            return read_page_map(folder)
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
    page_map: Mapping[str, Any] | None, main_file: str | None, parallel: int
) -> FanOutPlan | None:
    """The full check's helper jobs (see the module's words); `None` when the page map gives no
    drawing to compare (then the lead plans, as in a chat)."""
    if not page_map:
        return None
    entries = _entries(page_map, main_file)
    if not entries:
        return None
    content = [e for e in entries if e.get("halves")]
    if not content:
        return None
    skipped = tuple((int(e["page"]), DIVIDER_NOTE) for e in entries if not e.get("halves"))
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
        looks = sorted({p for _unit, extra in chosen for p in extra} - set(pages))
        reference = [(file, p) for p in looks]
        wanted = {area.lower() for area in areas}
        reference += [
            (str(e.get("file")), int(e["page"]))
            for e in others
            if isinstance(e.get("page"), int) and (_area(e) or "").lower() in wanted
        ]
        jobs.append(HelperJob(number, pages, file, tuple(reference), tuple(areas)))
    return FanOutPlan(tuple(jobs), skipped)


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
