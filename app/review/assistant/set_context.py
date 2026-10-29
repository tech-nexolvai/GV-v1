"""What the other pages of a drawing set print, for the page reviewer (2026-10-29). Not the AI
project index (`set_index`): plain code on printed text.

The page reviewer saw one page only. On the client's La Quinta and Kingsland pages GV's reviewer
wrote the right finish code (27 of 32 finish comments) and who supplies a part (4 of 4 scope
comments) from what the set's OTHER pages print: the same part's tag on the neighbouring sheets,
the countertop drawing of the same item, "BIN BY GC/OWNER" on the next sheet. Code reads every
page's text layer once per check (the drawings-only copy, so GV's own markup is never read) and
gives each reviewer:

- `summary(page)`: the tags and item numbers the set prints, with the pages that print them, and
  the "who supplies it" notes, with their pages (bounded, so a large set stays short);
- `phrases(file, page)`: any page's text layer, for the reviewer's `set_page_text` tool.

Plain code on printed text: no model call, nothing decided. What the reviewer makes of it is a
please-check like every other page-review issue (never red).
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = ["MAX_NOTES", "MAX_TAGS", "SCOPE", "TAG", "SetContext", "scope_notes", "tags_in"]

_log = logging.getLogger(__name__)

#: A finish, material or item tag as drafters print it: letters, an optional dash, 2-3 digits, an
#: optional letter and an optional ".X" suffix (WD-04, ST-02, LA-007, MT-001.C, ML-018T.C,
#: GVQZ-19, BOH-802.CT, SR433).
TAG: Final = re.compile(r"\b[A-Z]{1,5}-?\d{2,3}[A-Z]?(?:\.[A-Z]{1,3})?\b")
#: A "who supplies it" note: by GV, the GC, the owner or others; not in contract; provided by.
SCOPE: Final = re.compile(
    r"\b(?:by|provided by|supplied by|furnished by|installed by)\s+"
    r"(?:g\.?v\.?|g\.?c\.?|owner|others|other trades?|general contractor|millwork)\b"
    r"|\bn\.?\s?i\.?\s?c\.?\b|\bnot in contract\b|\bOFCI\b|\bOFOI\b|\bCFCI\b",
    re.IGNORECASE,
)
#: The summary's bounds: the tags printed on the most pages, and the notes, at most this many.
MAX_TAGS: Final = 60
MAX_NOTES: Final = 30
_MAX_PAGES_LISTED: Final = 12


def tags_in(text: str) -> list[str]:
    return TAG.findall(text)


def scope_notes(phrases: Sequence[Mapping[str, Any]]) -> list[str]:
    """The phrases that say who supplies something, as printed (at most 80 characters each)."""
    out: list[str] = []
    for phrase in phrases:
        text = " ".join(str(phrase.get("text") or "").split())
        if text and SCOPE.search(text) and text[:80] not in out:
            out.append(text[:80])
    return out


def _pages(pages: Sequence[int]) -> str:
    shown = ", ".join(f"p{p}" for p in pages[:_MAX_PAGES_LISTED])
    more = len(pages) - _MAX_PAGES_LISTED
    return shown + (f" (+{more} more)" if more > 0 else "")


@dataclass
class SetContext:
    """Every page's phrases of one file, read once (`build`), and what they print."""

    file: str
    read: Callable[[int], list[dict[str, Any]]]
    """One page's phrases (the reviewer's own text reading of the drawings-only copy)."""
    page_count: int
    pages: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    tags: dict[str, list[int]] = field(default_factory=dict)
    notes: dict[str, list[int]] = field(default_factory=dict)
    unread: list[int] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    built: bool = False

    def build(self) -> None:
        """Read every page (once; a page that cannot be read is listed, never fatal)."""
        with self.lock:
            if self.built:
                return
            tags: dict[str, list[int]] = defaultdict(list)
            notes: dict[str, list[int]] = defaultdict(list)
            for page in range(1, self.page_count + 1):
                try:
                    found = self.read(page)
                except Exception:  # noqa: BLE001 - that page is left out of the summary
                    _log.warning("set context: page %s of %s not read", page, self.file)
                    self.unread.append(page)
                    continue
                self.pages[page] = found
                text = " ".join(str(p.get("text") or "") for p in found)
                for tag in Counter(tags_in(text)):
                    tags[tag].append(page)
                for note in scope_notes(found):
                    notes[note].append(page)
            self.tags = dict(tags)
            self.notes = dict(notes)
            self.built = True

    def phrases(self, page: int) -> list[dict[str, Any]]:
        self.build()
        if page not in self.pages:
            raise ValueError(
                f"page {page} of {self.file} could not be read"
                if 1 <= page <= self.page_count
                else f"there is no page {page} (the set has pages 1 to {self.page_count})"
            )
        return self.pages[page]

    def summary(self, page: int) -> str:
        """What the set's pages print, for page `page`'s reviewer (`page_reviewer`)."""
        self.build()
        lines = [
            (
                f"{self.file} has {self.page_count} pages; you are reviewing p{page}. What the "
                "set's pages print (from their text layers):"
            )
        ]
        if self.page_count <= 1:
            return lines[0] + " (this is the only page)."
        ranked = sorted(self.tags.items(), key=lambda item: (-len(item[1]), item[0]))
        alone = sorted(tag for tag, pages in self.tags.items() if pages == [page])
        if alone:
            # A tag no other sheet prints: where a drafter used the wrong code (Kingsland p518 of
            # 2026-10-29: PLAM-007 and MT-002 here, LA-007 and MT-001.C on every other sheet).
            lines.append(f"Printed only on this page (p{page}): {', '.join(alone[:MAX_TAGS])}")
        if ranked:
            lines.append("Tags and item numbers, with the pages that print them:")
            for tag, pages in ranked[:MAX_TAGS]:
                lines.append(f"  {tag}: {_pages(pages)}")
            if len(ranked) > MAX_TAGS:
                lines.append(f"  (+{len(ranked) - MAX_TAGS} more tags printed on fewer pages)")
        if self.notes:
            lines.append('"Who supplies it" notes, with their pages:')
            ordered = sorted(self.notes.items(), key=lambda item: (-len(item[1]), item[0]))
            for note, pages in ordered[:MAX_NOTES]:
                lines.append(f'  "{note}": {_pages(pages)}')
        if self.unread:
            lines.append(f"Pages not read: {_pages(self.unread)}.")
        return "\n".join(lines)
