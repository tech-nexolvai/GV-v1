"""What "the facts of a page" are, and how much of them a set pack already holds (Phase 1).

The fact cache is only worth building if the facts exist. This module says what a page's facts are
and harvests them from a set pack, which is where the project already keeps per-page reading:
`dims.json` (every printed number, with its box, side, route and origin), `page_texts.json` (the
typed text), `page_map.json` (what each page is and which half is whose) and the scale notes. All
of it is produced by code, for nothing, and all of it is already cached per page inside the pack
(`.dims-pages/`), so the probe reuses that machinery instead of a parallel store.

**GV's own markup is not a fact.** A pack also holds `annotations.json`, the reviewer's boxes and
notes on the drawings. Those are the *answer*, never the reading source: ingesting them is
circular and is not what production sees (settled 2026-09-09). This module counts them so a page
can say "a reviewer wrote here" and never copies their text into a fact.

`coverage` is the measurement the owner asked for: of the evidence a question list needs, how much
is already on disk for nothing. It reports per page which kinds of fact are present, so the report
can say what share of question-side iteration the cache can serve without a paid read.

Nothing here calls a model, opens a drawing or touches a database; it reads JSON a pack already
wrote.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = [
    "FACT_KINDS",
    "PageFacts",
    "architect_cover",
    "coverage",
    "harvest_pack",
    "harvest_page",
    "pack_pages",
    "upload_named",
]

#: The kinds of fact a question can be asked of. A question needs one or more of these; a question
#: that needs a kind this page has none of cannot be answered without a paid read.
FACT_KINDS: Final = (
    "vendor_numbers",  # printed sizes on the vendor's own sheet
    "architect_numbers",  # printed sizes on the architect's sheet
    "page_text",  # the typed words of the page
    "phrases",  # view titles and headings, with their boxes
    "scale_notes",  # the printed scale
    "sheet_map",  # what the page is, its halves and sheet numbers
    "pictures",  # pasted views, as boxes
)

#: The extraction that produced these facts, for the cache key. Bump when the harvest changes
#: what it writes, which is what retires a cached page.
HARVEST_VERSION: Final = "pack-harvest-1"


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class PageFacts:
    """One page, read into facts. No client text beyond what the drawing itself prints."""

    file: str
    page: int
    role: str
    facts: dict[str, Any]
    #: How many reviewer marks sit on this page. Their text is never read into a fact.
    gv_markup: int = 0

    @property
    def kinds(self) -> frozenset[str]:
        """The kinds of fact this page actually has something of."""
        return frozenset(kind for kind in FACT_KINDS if self.facts.get(kind))

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "page": self.page,
            "role": self.role,
            "harvest": HARVEST_VERSION,
            "gv_markup": self.gv_markup,
            "facts": self.facts,
        }


def pack_pages(pack: Path) -> list[tuple[str, int, str]]:
    """Every page of a pack as (file, page, role). Role is `vendor`, `architect` or `unknown`."""
    page_map = _load(pack / "page_map.json") or {}
    out: list[tuple[str, int, str]] = []
    for page in page_map.get("pages") or []:
        out.append(
            (
                str(page.get("file") or ""),
                int(page.get("page") or 0),
                str(page.get("role") or "unknown"),
            )
        )
    return out


def _number(label: dict[str, Any]) -> dict[str, Any]:
    """One printed size, kept to the fields a question can reason about."""
    return {
        "id": label.get("id"),
        "text": label.get("text"),
        "inches": label.get("inches"),
        "box": label.get("box"),
        "orientation": label.get("orientation"),
        "route": label.get("route"),
        "origin": label.get("origin"),
        "flags": label.get("flags"),
        "note": label.get("note"),
    }


def harvest_page(pack: Path, file: str, page: int) -> PageFacts:
    """Everything a pack already knows about one page, as facts."""
    dims = _load(pack / "dims.json") or {}
    texts = _load(pack / "page_texts.json") or {}
    page_map = _load(pack / "page_map.json") or {}
    marks = _load(pack / "annotations.json") or []

    entry: dict[str, Any] = {}
    for item in dims.get("pages") or []:
        if str(item.get("file")) == file and int(item.get("page") or 0) == page:
            entry = item
            break
    labels = entry.get("labels") or []
    vendor = [_number(label) for label in labels if label.get("side") == "vendor"]
    architect = [_number(label) for label in labels if label.get("side") == "architect"]

    text = ""
    for item in texts.get("pages") or []:
        if str(item.get("file")) == file and int(item.get("page") or 0) == page:
            text = str(item.get("text") or "")
            break

    mapped: dict[str, Any] = {}
    role = "unknown"
    for item in page_map.get("pages") or []:
        if str(item.get("file")) == file and int(item.get("page") or 0) == page:
            mapped = {
                "halves": item.get("halves"),
                "headings": item.get("headings"),
                "sheet_numbers": item.get("sheet_numbers"),
                "section": item.get("section"),
                "area": item.get("area"),
            }
            role = str(item.get("role") or "unknown")
            break

    gv_markup = sum(
        1
        for mark in marks
        if isinstance(mark, dict)
        and str(mark.get("file")) == file
        and int(mark.get("page") or 0) == page
    )

    facts: dict[str, Any] = {
        "vendor_numbers": vendor,
        "architect_numbers": architect,
        "page_text": text,
        "phrases": entry.get("phrases") or [],
        "scale_notes": entry.get("scale_notes") or [],
        "sheet_map": mapped,
        "pictures": ((entry.get("pictures") or {}).get("boxes")) or [],
        "drawn": entry.get("drawn"),
    }
    return PageFacts(file=file, page=page, role=role, facts=facts, gv_markup=gv_markup)


def upload_named(pack: Path, role: str) -> str | None:
    """The uploaded file the pack gave this role: `vendor` or `architect`.

    A probe's page numbers are page numbers of the vendor *upload*, which is not the same as the
    pages the map calls `vendor`: a title sheet or a schedule inside the vendor's own file is
    marked `general`, and leaving those out would hide a dozen pages of a job.
    """
    page_map = _load(pack / "page_map.json") or {}
    for name, named in (page_map.get("upload_roles") or {}).items():
        if str(named) == role:
            return str(name)
    return None


def harvest_pack(
    pack: Path,
    *,
    role: str | None = "vendor",
    only: frozenset[int] | None = None,
) -> list[PageFacts]:
    """Harvest a pack's pages.

    `role` names an **upload**, so `vendor` means every page of the vendor's own file. `only`
    narrows it to the pages a probe would run, which is how coverage is reported for one probe
    rather than for a whole job.
    """
    file = upload_named(pack, role) if role else None
    return [
        harvest_page(pack, page_file, page)
        for page_file, page, _ in pack_pages(pack)
        if (file is None or page_file == file) and (only is None or page in only)
    ]


def architect_cover(pack: Path, vendor_pages: list[int], paired: dict[int, int]) -> dict[str, Any]:
    """For a probe's vendor pages, whether the architect sheet they must be compared with is read.

    This is the honest half of a cache-coverage figure. A question the vendor's own sheet can
    answer — do these parts add up, is this cut-out the size the rule gives — needs only that
    page's facts. A question that compares with the architect needs the paired sheet's numbers
    too, and that sheet has to be both **known** (the pairing truth names it) and **read** (the
    pack holds numbers for it). Either can be missing, and the two failures look nothing alike.
    """
    file = upload_named(pack, "architect")
    known = [page for page in vendor_pages if page in paired]
    read = 0
    for page in known:
        if file is None:
            continue
        facts = harvest_page(pack, file, paired[page])
        if facts.facts.get("vendor_numbers") or facts.facts.get("architect_numbers"):
            read += 1
    return {
        "vendor_pages": len(vendor_pages),
        "architect_page_known": len(known),
        "architect_page_read": read,
        "no_architect_page": sorted(set(vendor_pages) - set(known)),
    }


@dataclass(frozen=True)
class Coverage:
    """How much of the evidence a question list needs is already on disk, for nothing."""

    pages: int
    per_kind: dict[str, int]
    complete_pages: int
    empty_pages: tuple[int, ...]

    def share(self, kind: str) -> float | None:
        return self.per_kind.get(kind, 0) / self.pages if self.pages else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "pages_with": self.per_kind,
            "pages_with_everything": self.complete_pages,
            "pages_with_no_numbers": list(self.empty_pages),
            "share_with": {
                kind: (None if self.share(kind) is None else round(self.share(kind) or 0.0, 3))
                for kind in FACT_KINDS
            },
        }


def coverage(harvested: list[PageFacts], *, needed: tuple[str, ...] = FACT_KINDS) -> Coverage:
    """Per kind, how many of these pages have any of it.

    A page with no `vendor_numbers` is the honest hole: no question about a printed size can be
    answered there from cached facts, however good the question is.
    """
    counts: Counter[str] = Counter()
    complete = 0
    empty: list[int] = []
    for page in harvested:
        kinds = page.kinds
        for kind in FACT_KINDS:
            if kind in kinds:
                counts[kind] += 1
        if set(needed) <= kinds:
            complete += 1
        if not page.facts.get("vendor_numbers") and not page.facts.get("architect_numbers"):
            empty.append(page.page)
    return Coverage(
        pages=len(harvested),
        per_kind=dict(counts),
        complete_pages=complete,
        empty_pages=tuple(empty),
    )
