"""An older copy of a sheet in the same upload: only the newest is checked. $0, code only.

A vendor sometimes uploads the same sheet twice in one file: an older revision beside the new one
(`REV 0` and `REV 1`), or a re-issue printed again. A reviewer reviews the newest and ignores the
superseded one (owner, 2026-10-23; research row U10). Checking both would compare the old sizes
too, and an old size that differs from the new one would look like the vendor contradicting
itself. So code finds such pages and leaves the older one out, with a plain note:

    Page 12 is an older copy of page 30: not checked.

**The same sheet** (all must hold; anything less is two sheets, both checked):

- both are vendor drawings of the same file, of no GV round (GV's combined sheets have their own
  rounds, `revision_diff`), and neither is an exact copy (`preflight.duplicate_pages` says those);
- they print the same sheet number(s) the page map read (`sheet_numbers`, at least one);
- their printed words, less numbers and revision marks, are nearly the same (`SAME_WORDS`), or
  all the same when no revision mark tells them apart (`SAME_WORDS_NO_MARK`: a vendor reuses one
  sheet number for the same piece in two hotels, with only the hotel's name changed);
- when the set index is at hand, they are in the same part of the set (no divider page that
  starts a new project between them) and print no different project names (`sections_of`).

**Which is newer**, the first that tells:

1. the revision mark printed in the title block (`REV 1`, `REVISION B`, `REV. NO. 2`): the higher
   one (numbers with numbers, letters with letters); the same mark on both: not a revision pair;
2. the latest date printed on each (month/day/year, as US title blocks print it);
3. else the later page of the file.

Pure: plain values in (the page map, each page's printed text), a mapping out.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

__all__ = [
    "DUPLICATE_TEXT",
    "SAME_WORDS",
    "SAME_WORDS_NO_MARK",
    "Older",
    "exact_copies",
    "older_copies",
]

#: Two pages with revision marks that differ are one sheet when this share of their words is
#: the same (numbers and revision marks left out).
SAME_WORDS: Final = 0.6
#: With no revision mark to tell them apart, the words must be the same.
SAME_WORDS_NO_MARK: Final = 1.0
#: An exact copy: a page's text at least this long (characters), the same labels too.
DUPLICATE_TEXT: Final = 100
#: A revision mark: `REV 0`, `REV. 1`, `REV: B`, `REVISION 2`, `REV NO. 3`, `REV #4` (never the
#: words `REVISIONS` or `REV DATE`).
_MARK: Final = re.compile(
    r"\bREV(?:ISION)?(?:\.|:|\s)\s*(?:NO\.?\s*)?#?\s*([A-Z]|\d{1,2})\b(?![./-]\d)",
    re.IGNORECASE,
)
_DATE: Final = re.compile(r"(?<![\d/.-])(\d{1,2})[/.-](\d{1,2})[/.-](\d{2}|\d{4})(?![\d/.-])")
_WORD: Final = re.compile(r"[A-Za-z][A-Za-z&'-]{1,}")
_REV_WORDS: Final = frozenset({"REV", "REVISION", "REVISIONS", "NO"})


@dataclass(frozen=True, slots=True)
class Older:
    """A vendor page that is an older copy of a later-checked sheet."""

    page: int
    newer: int
    why: str
    """How code knows which is newer ("REV 0; page 30 is REV 1")."""

    @property
    def words(self) -> str:
        """The plain note: "Page 12 is an older copy of page 30 (REV 0; page 30 is REV 1): not
        checked."""
        return f"Page {self.page} is an older copy of page {self.newer} ({self.why}): not checked."

    @property
    def reason(self) -> str:
        """The same, as a page's skipped reason reads: "an older copy of page 30 (...): not
        checked (code)"."""
        return f"an older copy of page {self.newer} ({self.why}): not checked (code)"


def exact_copies(
    file: str | None, dims: Any, texts: Mapping[tuple[str, int], str]
) -> dict[int, int]:
    """A page of `file` that is an exact copy of an earlier page of it (the same printed text, at
    least `DUPLICATE_TEXT` characters, and the same dimension labels) -> that page."""
    if not file:
        return {}
    labels = {
        int(p["page"]): tuple(sorted(str(label.get("text")) for label in p.get("labels") or []))
        for p in ((dims or {}).get("pages") or [] if isinstance(dims, Mapping) else [])
        if isinstance(p, Mapping) and str(p.get("file")) == file
    }
    seen: dict[str, int] = {}
    out: dict[int, int] = {}
    for (name, page), text in sorted(texts.items()):
        if name != file:
            continue
        flat = " ".join(str(text).split())
        if len(flat) < DUPLICATE_TEXT:
            continue
        key = hashlib.sha256(
            (flat + "\x00" + "\x01".join(labels.get(page, ()))).encode("utf-8")
        ).hexdigest()
        if key in seen:
            out[page] = seen[key]
        else:
            seen[key] = page
    return out


def _mark(text: str) -> tuple[str, int, str] | None:
    """The page's revision as (kind, rank, printed): the highest mark it prints (a revision table
    lists the earlier ones too), `None` when it prints none or mixes numbers and letters."""
    found = [m.group(1).upper() for m in _MARK.finditer(text)]
    if not found:
        return None
    kinds = {"number" if f.isdigit() else "letter" for f in found}
    if len(kinds) != 1:
        return None
    kind = kinds.pop()
    best = max(found, key=lambda f: int(f) if f.isdigit() else ord(f))
    return kind, int(best) if best.isdigit() else ord(best), best


def _latest_date(text: str) -> date | None:
    out: list[date] = []
    for found in _DATE.finditer(text):
        month, day, year = (int(found.group(i)) for i in (1, 2, 3))
        if year < 100:
            year += 2000
        try:
            out.append(date(year, month, day))
        except ValueError:
            continue
    return max(out) if out else None


def _words(text: str) -> frozenset[str]:
    return frozenset(
        w.upper() for w in _WORD.findall(text) if w.upper().strip("'-") not in _REV_WORDS
    )


def _same_share(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _vendor_pages(page_map: Mapping[str, Any], file: str) -> dict[int, Mapping[str, Any]]:
    out: dict[int, Mapping[str, Any]] = {}
    for entry in page_map.get("pages") or []:
        if not isinstance(entry, Mapping) or str(entry.get("file")) != file:
            continue
        halves = [h for h in entry.get("halves") or [] if isinstance(h, Mapping)]
        if not halves or not all(h.get("role") == "vendor" for h in halves):
            continue
        if entry.get("rounds") or any(h.get("round") for h in halves):
            continue  # GV's rounds: compared as rounds, never left out here
        if entry.get("skip"):
            continue
        out[int(entry["page"])] = entry
    return out


def _newer(first: int, second: int, texts: Mapping[int, str]) -> tuple[int, int, str] | None:
    """(older, newer, why) for two pages of one sheet, or `None` when it is not a revision pair."""
    a, b = texts[first], texts[second]
    marks = _mark(a), _mark(b)
    share = _same_share(_words(a), _words(b))
    if marks[0] is not None and marks[1] is not None:
        if marks[0][0] != marks[1][0] or marks[0][1] == marks[1][1] or share < SAME_WORDS:
            return None
        older, newer = (first, second) if marks[0][1] < marks[1][1] else (second, first)
        old_mark, new_mark = (marks[0], marks[1]) if older == first else (marks[1], marks[0])
        return older, newer, f"REV {old_mark[2]}; page {newer} is REV {new_mark[2]}"
    if marks[0] is not None or marks[1] is not None or share < SAME_WORDS_NO_MARK:
        return None
    dates = _latest_date(a), _latest_date(b)
    if dates[0] is not None and dates[1] is not None and dates[0] != dates[1]:
        older, newer = (first, second) if dates[0] < dates[1] else (second, first)
        when = dates[1] if newer == second else dates[0]
        return older, newer, f"page {newer} is dated later, {when:%m/%d/%Y}"
    return first, second, f"the same sheet printed again; page {second} is the later page"


def sections_of(index: Any, file: str | None) -> dict[int, tuple[int, frozenset[str]]]:
    """Each page of `file` placed in the set by the set index's checked reads (`set_index.json`'s
    pages, keyed `file#page`, or that file's `pages`): (how many divider pages that start a new
    project or set come before it or on it, the project names it prints). Empty without one."""
    pages = index.get("pages") if isinstance(index, Mapping) and "pages" in index else index
    if not isinstance(pages, Mapping) or not file:
        return {}
    reads = {
        int(read["page"]): read
        for read in pages.values()
        if isinstance(read, Mapping)
        and str(read.get("file")) == file
        and isinstance(read.get("page"), int)
    }
    out: dict[int, tuple[int, frozenset[str]]] = {}
    part = 0
    for page in sorted(reads):
        read = reads[page]
        if read.get("introduces"):
            part += 1
        names = frozenset(
            " ".join(str(n).upper().split()) for n in read.get("projects") or [] if str(n).strip()
        )
        out[page] = (part, names)
    return out


def _apart(first: int, second: int, sections: Mapping[int, tuple[int, frozenset[str]]]) -> bool:
    """Whether the set index puts the two pages in different parts or projects of the set."""
    a, b = sections.get(first), sections.get(second)
    if a is None or b is None:
        return False
    return a[0] != b[0] or bool(a[1] and b[1] and a[1] != b[1])


def older_copies(
    page_map: Mapping[str, Any] | None,
    texts: Mapping[tuple[str, int], str],
    file: str | None,
    *,
    exact: Mapping[int, int] | None = None,
    index: Any = None,
) -> dict[int, Older]:
    """Each vendor page of `file` that is an older copy of another page of the same sheet (see
    the module), with the newest page it gives way to. `exact`: the exact copies already found
    (`preflight.duplicate_pages`), never counted again. `index`: the set index's checked reads
    (`sections_of`): two pages it puts in different parts of the set are never one sheet."""
    if not page_map or not file:
        return {}
    sections = sections_of(index, file)
    pages = _vendor_pages(page_map, file)
    exact = exact or {}
    sheets: dict[str, list[int]] = {}
    for page, entry in sorted(pages.items()):
        if page in exact or (file, page) not in texts:
            continue
        for sheet in entry.get("sheet_numbers") or []:
            sheets.setdefault(str(sheet).upper(), []).append(page)
    page_text = {page: str(texts[(file, page)]) for page in pages if (file, page) in texts}
    newer_of: dict[int, tuple[int, str]] = {}
    for _sheet, members in sorted(sheets.items()):
        for i, first in enumerate(members):
            for second in members[i + 1 :]:
                if _sheets(pages[first]) != _sheets(pages[second]) or _apart(
                    first, second, sections
                ):
                    continue
                found = _newer(first, second, page_text)
                if found is None:
                    continue
                older, newer, why = found
                known = newer_of.get(older)
                if known is None or known[0] < newer:
                    newer_of[older] = (newer, why)
    # The newest of each sheet: follow a page's newer copy to the end of the chain.
    out: dict[int, Older] = {}
    for older, (newer, why) in sorted(newer_of.items()):
        seen = {older}
        while newer in newer_of and newer not in seen:
            seen.add(newer)
            newer = newer_of[newer][0]
        if newer != older:
            if newer != newer_of[older][0]:
                why = f"{why}; page {newer} is newer still"
            out[older] = Older(older, newer, why)
    return out


def _sheets(entry: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(str(s).upper() for s in entry.get("sheet_numbers") or [])
