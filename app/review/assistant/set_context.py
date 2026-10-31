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
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "CREATED_BY",
    "MAX_NOTES",
    "MAX_TAGS",
    "MIN_FAMILY_PAGES",
    "ONE_DIGIT_FAMILIES",
    "SCOPE",
    "TAG",
    "FinishDifference",
    "Outlier",
    "SetContext",
    "add_to_answer",
    "fold_repeats",
    "scope_notes",
    "tags_in",
]

_log = logging.getLogger(__name__)

#: Finish and material families a schedule numbers from 1 (ST-3, PL-1, WD-2): their codes may have
#: one digit. Any other prefix needs two or three, so a sheet or detail number (A-1, MW-1, S-1, D-2)
#: never reads as a tag (Bluebeam's own warning: 1-2 character tags give inaccurate results).
ONE_DIGIT_FAMILIES: Final = frozenset(
    {"ST", "WD", "PL", "PLAM", "LA", "LAM", "MT", "SS", "GL", "HW", "QZ", "SP", "PT", "WV", "VN"}
)
_ONE_DIGIT: Final = "|".join(sorted(ONE_DIGIT_FAMILIES, key=lambda f: (-len(f), f)))
#: A finish, material or item tag as drafters print it: letters, an optional dash, 2-3 digits (1-3
#: in a `ONE_DIGIT_FAMILIES` family), an optional letter and an optional ".X" suffix (WD-04, ST-02,
#: ST-3, PL-1, LA-007, MT-001.C, ML-018T.C, GVQZ-19, BOH-802.CT, SR433).
TAG: Final = re.compile(
    rf"\b(?:(?:{_ONE_DIGIT})-?\d{{1,3}}|[A-Z]{{1,5}}-?\d{{2,3}})[A-Z]?(?:\.[A-Z]{{1,3}})?\b"
)
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
#: A tag family (its letters: ST, WD, LA) is a finish family of the set when one of its codes is
#: printed on at least this many pages AND on at least `FAMILY_PAGE_SHARE` of the pages that print
#: any tag, with at most `MAX_FAMILY_CODES` codes in all: the set uses it throughout. Item numbers
#: (BOH-801.C: its own sheet and its countertop drawing), sheet numbers (MW-101 to MW-150: many
#: codes, one page each) and cross-references never reach it (the review of 2026-10-29: a looser
#: rule would have made every sheet's own number an "outlier").
MIN_FAMILY_PAGES: Final = 3
FAMILY_PAGE_SHARE: Final = 0.2
MAX_FAMILY_CODES: Final = 6
#: Who made the cross-sheet findings (the chat finding's `created_by`; a please-check maker).
CREATED_BY: Final = "set_check"
#: A tag's family, number, letter and suffix (`MT-001.C`: MT, 001, "", .C; `GRT-801T`: GRT, 801, T;
#: `ST-3`: ST, 3). One digit only in a `ONE_DIGIT_FAMILIES` family (`_split`).
_PARTS: Final = re.compile(r"^([A-Z]{1,5})-?(\d{1,3})([A-Z]?)(\.[A-Z]{1,3})?$")
#: The printed sizes listed for an item's other drawing, at most.
_MAX_SIZES: Final = 24
#: A seam as drafters print it on a countertop drawing.
_SEAM: Final = re.compile(r"\b(?:joint|seam)s?\b", re.IGNORECASE)
#: A printed inch size: `36"`, `23 1/4"`, `5'-2 1/2"`.
_INCHES: Final = re.compile(r"(?:\d+'-?\s?)?\d+(?:[ -]\d+/\d+)?\s?\"")


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


def _split(tag: str) -> re.Match[str] | None:
    """A tag's parts (`_PARTS`), or `None` for what `TAG` would not read (`A-1`, `MW-1`)."""
    found = _PARTS.match(tag)
    if found is None or (len(found.group(2)) == 1 and found.group(1) not in ONE_DIGIT_FAMILIES):
        return None
    return found


def family_of(tag: str) -> str | None:
    found = _split(tag)
    return found.group(1) if found else None


def item_key(tag: str) -> str | None:
    """What makes an item's drawings one item: family and number (`BOH-802.C` and `BOH-802.CT`,
    `GRT-801` and `GRT-801T`, `ML-018.C` and `ML-018T.C`: the same key)."""
    found = _split(tag)
    return f"{found.group(1)}-{int(found.group(2))}" if found else None


@dataclass(frozen=True, slots=True)
class Outlier:
    """A tag printed only on one page, in a family the set prints throughout (Kingsland p518 of
    2026-10-29: MT-002 here, MT-001.C on 14 other sheets): likely the wrong code."""

    tag: str
    family: str
    others: dict[str, list[int]]
    """The family's other codes, with the pages that print them."""


@dataclass(frozen=True, slots=True)
class FinishDifference:
    """A finish family tagged differently on an item's two drawings (La Quinta sheet 449 of
    2026-10-29: its cabinet sheet tags the top ST-03 / GVQZ-16, its countertop drawing ST-02 /
    GVQZ-19, which is what GV's reviewer wrote in)."""

    family: str
    here: list[str]
    there: list[str]
    other_page: int
    other_item: str


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
    _ready: threading.Event = field(default_factory=threading.Event)
    _started: bool = False
    #: Only for `_started`: `lock` is held for the whole read, and a reviewer asking whether the
    #: read began must never wait for it to end.
    _start_lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self) -> None:
        """Read the set in the background (once), so no reviewer waits for all of it: each one
        waits at most a bound (`ready`) and is reviewed on its own page when the set is slow."""
        with self._start_lock:
            if self._started or self.built:
                return
            self._started = True
        threading.Thread(target=self._read_all, name="set-context", daemon=True).start()

    def _read_all(self) -> None:
        try:
            self.build()
        except Exception:  # noqa: BLE001 - the pages are reviewed on their own
            _log.exception("set context: %s not read", self.file)
        finally:
            self._ready.set()

    def ready(self, wait: float, cancel: threading.Event | None = None) -> bool:
        """Whether the set was read within `wait` seconds (looks at `cancel` every 2 seconds)."""
        self.start()
        deadline = time.monotonic() + max(0.0, wait)
        while not self.built:
            if self._ready.is_set():  # the read ended and failed: never waited on again
                return False
            if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                return self.built
            self._ready.wait(min(2.0, max(0.0, deadline - time.monotonic())))
        return True

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

    # -- what the set prints, by family --

    def families(self) -> dict[str, dict[str, list[int]]]:
        """Every family's codes with their pages."""
        self.build()
        out: dict[str, dict[str, list[int]]] = defaultdict(dict)
        for tag, pages in self.tags.items():
            family = family_of(tag)
            if family is not None:
                out[family][tag] = pages
        return dict(out)

    def finish_families(self) -> set[str]:
        """The families the set prints throughout (`MIN_FAMILY_PAGES`, `FAMILY_PAGE_SHARE`,
        `MAX_FAMILY_CODES`), other trades' left out."""
        from app.review.assistant.decision_resolver import OTHER_TRADE_FAMILIES

        tagged = len({page for pages in self.tags.values() for page in pages})
        least = max(MIN_FAMILY_PAGES, int(FAMILY_PAGE_SHARE * tagged + 0.999))
        out: set[str] = set()
        for family, codes in self.families().items():
            # One letter spells cabinet codes (D22, W70, H34), never a finish; a code is one
            # code however it is spelled (ST03, ST-03).
            spelled = {re.sub(r"[-\s]", "", tag) for tag in codes}
            if (
                len(family) >= 2
                and family not in OTHER_TRADE_FAMILIES
                and len(spelled) <= MAX_FAMILY_CODES
                and max(len(pages) for pages in codes.values()) >= least
            ):
                out.add(family)
        return out

    def tags_on(self, page: int) -> list[str]:
        self.build()
        seen: dict[str, None] = {}
        for phrase in self.pages.get(page, []):
            for tag in tags_in(str(phrase.get("text") or "")):
                seen.setdefault(tag, None)
        return list(seen)

    def occurrences(self, page: int, tag: str) -> list[list[float]]:
        """The boxes of the phrases on `page` that print `tag`, in reading order."""
        self.build()
        return [
            [float(v) for v in phrase["box"]]
            for phrase in self.pages.get(page, [])
            if tag in tags_in(str(phrase.get("text") or ""))
        ]

    def outliers(self, page: int) -> list[Outlier]:
        """The tags only `page` prints, in a finish family of the set (`Outlier`)."""
        families, finish = self.families(), self.finish_families()
        out: list[Outlier] = []
        for tag in self.tags_on(page):
            family = family_of(tag)
            if family in finish and self.tags.get(tag) == [page]:
                others = {t: p for t, p in families[family].items() if t != tag}
                out.append(Outlier(tag, family, others))
        return out

    def item_tags(self, page: int) -> list[str]:
        """The item numbers `page` prints: tags outside the finish families, printed on at most
        two pages of the set (the item's own sheet and its other drawing)."""
        finish = self.finish_families()
        return [
            tag
            for tag in self.tags_on(page)
            if family_of(tag) is not None
            and family_of(tag) not in finish
            and len(self.tags.get(tag, [])) <= 2
        ]

    def pair_pages(self, page: int) -> list[tuple[int, str, str]]:
        """The other drawings of `page`'s item (`item_key`): (page, its tag, this page's tag)."""
        out: list[tuple[int, str, str]] = []
        for mine in self.item_tags(page):
            key = item_key(mine)
            for other, pages in self.tags.items():
                if other == mine or item_key(other) != key or family_of(other) != family_of(mine):
                    continue
                for number in pages:
                    if number != page and (number, other, mine) not in out:
                        out.append((number, other, mine))
        return sorted(out)

    def finish_differences(self, page: int) -> list[FinishDifference]:
        """The finish families `page` and its item's other drawing tag differently."""
        finish = self.finish_families()
        here = self.tags_on(page)
        out: list[FinishDifference] = []
        for other_page, other_tag, _mine in self.pair_pages(page):
            there = self.tags_on(other_page)
            for family in sorted(finish):
                a = sorted(t for t in here if family_of(t) == family)
                b = sorted(t for t in there if family_of(t) == family)
                # Only against a drawing that tags the family once (a sheet of several tops tags
                # several, and differs from every cabinet sheet), and only this page's odd codes.
                if a and len(b) == 1 and any(t not in b for t in a):
                    out.append(FinishDifference(family, a, b, other_page, other_tag))
        return out

    def seams(self, page: int) -> int:
        """How many seams (JOINT, SEAM) `page` prints: the countertop drawing marks where the top is
        joined, which is where a support or leg belongs (GV's reviewer, La Quinta 449 of
        2026-10-29: "move the support so its centre aligns with the seam line")."""
        self.build()
        return sum(
            1 for phrase in self.pages.get(page, []) if _SEAM.search(str(phrase.get("text") or ""))
        )

    def printed_sizes(self, page: int) -> list[str]:
        """The inch sizes `page` prints, as printed (for an item's other drawing), at most
        `_MAX_SIZES`, each once."""
        self.build()
        sizes: dict[str, None] = {}
        for phrase in self.pages.get(page, []):
            for found in _INCHES.finditer(str(phrase.get("text") or "")):
                sizes.setdefault(found.group(0).strip(), None)
        return list(sizes)[:_MAX_SIZES]

    def facts_for(self, page: int) -> str:
        """The facts of the rest of the set that bear on `page`, for its reviewer: its tags with
        the pages that print them, the tags only it prints, its item's other drawing (tags and
        sizes), the finish tags that differ between the two, and the "who supplies it" notes
        elsewhere (bounded). Pre-computed, so the reviewer spends no turn reading other pages (the
        run of 2026-10-29: given the pages as tools, reviewers read them and finished their own page
        less)."""
        self.build()
        lines = [f"{self.file} has {self.page_count} pages; you are reviewing p{page}."]
        if self.page_count <= 1:
            return lines[0] + " It is the only page."
        tags = self.tags_on(page)
        if tags:
            lines.append("Tags this page prints, with the other pages that print them:")
            for tag in tags[:MAX_TAGS]:
                others = [p for p in self.tags.get(tag, []) if p != page]
                lines.append(f"  {tag}: {_pages(others) if others else 'no other page'}")
        for found in self.outliers(page):
            family_codes = ", ".join(
                f"{t} ({len(p)} pages)"
                for t, p in sorted(found.others.items(), key=lambda i: -len(i[1]))
            )
            lines.append(
                f"Only this page prints {found.tag}; the set's other sheets print "
                f"{family_codes} in that family. A wrong code is likely (please check)."
            )
        for other_page, other_tag, mine in self.pair_pages(page):
            tags_there = ", ".join(self.tags_on(other_page)[:MAX_TAGS]) or "none"
            sizes = ", ".join(self.printed_sizes(other_page)) or "none"
            lines.append(
                f"This item's other drawing: p{other_page} ({other_tag}, for {mine}) prints tags "
                f"{tags_there}; sizes {sizes}."
            )
            joints = self.seams(other_page)
            if joints:
                here = self.seams(page)
                lines.append(
                    f"p{other_page} marks {joints} {'seam' if joints == 1 else 'seams'} (JOINT) in "
                    f"the top; this page marks {here or 'none'}. Read p{other_page} for where, and "
                    "check a support or leg sits under each."
                )
        for diff in self.finish_differences(page):
            lines.append(
                f"Finish {diff.family}: this page tags {', '.join(diff.here)}; its other drawing "
                f"p{diff.other_page} ({diff.other_item}) tags {', '.join(diff.there)}. One of them "
                "is wrong (please check)."
            )
        if self.notes:
            lines.append('"Who supplies it" notes the set prints, with their pages:')
            ordered = sorted(self.notes.items(), key=lambda item: (-len(item[1]), item[0]))
            for note, pages in ordered[:MAX_NOTES]:
                lines.append(f'  "{note}": {_pages(pages)}')
        if self.unread:
            lines.append(f"Pages not read: {_pages(self.unread)}.")
        return "\n".join(lines)

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


# ---- code's own cross-sheet please-checks ----------------------------------------------------------


def _box_dict(box: Sequence[float], page: int, file: str) -> dict[str, Any]:
    return {
        "page": page,
        "file": file,
        "x0": round(float(box[0]), 1),
        "y0": round(float(box[1]), 1),
        "x1": round(float(box[2]), 1),
        "y1": round(float(box[3]), 1),
    }


def _please_check(
    context: SetContext, page: int, tag: str, item: str, wrong: str, should_be: str | None
) -> dict[str, Any] | None:
    """One cross-sheet please-check (kind finish, never red), at the tag's first spot on the
    page, every other spot on the page listed under `also_on` (GV's reviewer marks each one)."""
    from app.review.assistant import likely_problems

    spots = context.occurrences(page, tag)
    if not spots:
        return None
    words = f"{likely_problems.LETTER_LEAD}: {item}: {wrong}"
    if should_be:
        words = f"{words.rstrip('.')}. It should be: {should_be}."
    mark = {
        "by": CREATED_BY,
        "proof": "likely_only",
        "kind": "finish",
        "letter": "D",
        "claim": "please_check",
        "sure": "likely",
        "why": wrong[:400],
        "words": words[:600],
    }
    return {
        "page": page,
        "item": item[:160],
        "category": "other",
        # Checked within the vendor's own set: no architect side (as the page reviewer's items).
        "architect": "n/a",
        "vendor": tag,
        "status": "check",
        "check_reason": "pairing_not_certain",
        "verified": False,
        "source": "architect",
        "why": wrong[:300],
        "vendor_box": _box_dict(spots[0], page, context.file),
        "also_on": [
            {"page": page, "vendor": tag, "vendor_box": _box_dict(spot, page, context.file)}
            for spot in spots[1:12]
        ],
        likely_problems.FIELD: mark,
    }


def check_page(context: SetContext, page: int) -> list[dict[str, Any]]:
    """Code's please-checks for one page: a tag only it prints in a family the set uses
    throughout (`Outlier`), and a finish family its item's other drawing tags differently
    (`FinishDifference`). Each names the tag, the other sheets and the likely right code; none
    can be red (no proof rule, no witness: `likely_only`)."""
    out: list[dict[str, Any]] = []
    for found in context.outliers(page):
        ranked = sorted(found.others.items(), key=lambda item: (-len(item[1]), item[0]))
        others = ", ".join(f"{t} ({len(p)} pages)" for t, p in ranked)
        finding = _please_check(
            context,
            page,
            found.tag,
            f"Tag {found.tag}: only this sheet prints it",
            f"{found.tag} is printed on this sheet only; the set's other sheets tag that family "
            f"{others}. A wrong code is likely.",
            (
                f"{ranked[0][0]}, as the other sheets print (or the finish schedule's code)"
                if ranked
                else None
            ),
        )
        if finding is not None:
            out.append(finding)
    for diff in context.finish_differences(page):
        for tag in [t for t in diff.here if t not in diff.there]:
            finding = _please_check(
                context,
                page,
                tag,
                f"Finish {diff.family}: this sheet and the item's other drawing differ",
                f"This sheet tags {', '.join(diff.here)}; the same item's other drawing "
                f"(p{diff.other_page}, {diff.other_item}) tags {', '.join(diff.there)}.",
                f"{', '.join(diff.there)} on both, or the schedule's code",
            )
            if finding is not None:
                out.append(finding)
    return out


def _already(finding: Mapping[str, Any], live: Sequence[Mapping[str, Any]]) -> bool:
    """The answer already raises this tag on this page (the page reviewer's finish item naming
    it): code's copy would be a second card."""
    from app.review.assistant import likely_problems

    tag = str(finding.get("vendor") or "")
    page = finding.get("page")
    for other in live:
        if other.get("page") != page or other.get("created_by") == CREATED_BY:
            continue
        mark = other.get(likely_problems.FIELD)
        kind = mark.get("kind") if isinstance(mark, Mapping) else None
        words = " ".join(str(other.get(k) or "") for k in ("item", "why", "vendor"))
        if kind == "finish" and tag and tag in tags_in(words):
            return True
    return False


#: A phrase names the same note on several sheets when it has at least this many words.
_NOTE_WORDS: Final = 3
#: Two cards on the same note are the same please-check only when their item names share at least
#: this share of their words (Jaccard): the dry run of 2026-10-29 folded different issues that
#: happened to point at the same material-table line (an edge, an MT-02 tag, the real ST-03 vs
#: ST-02 finding: 0.00-0.43) and the same "by Vendor" note's cards (0.50-1.00).
SAME_WORDS: Final = 0.5
_STOP: Final = frozenset(
    [
        "the",
        "a",
        "an",
        "of",
        "and",
        "to",
        "in",
        "on",
        "is",
        "it",
        "its",
        "this",
        "page",
        "no",
        "not",
        "by",
        "for",
        "with",
        "as",
        "at",
        "or",
        "be",
        "are",
        "any",
    ]
)


def _item_words(finding: Mapping[str, Any]) -> set[str]:
    return {
        w
        for w in _norm(str(finding.get("item") or "")).split()
        if w not in _STOP and not w.isdigit()
    }


def _alike(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    x, y = _item_words(a), _item_words(b)
    return bool(x and y) and len(x & y) / len(x | y) >= SAME_WORDS


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def _phrases_under(context: SetContext, page: int, box: Mapping[str, Any]) -> list[str]:
    """The notes (phrases of `_NOTE_WORDS` words or more) a finding's box sits on, longest first."""
    try:
        x0, y0, x1, y1 = (float(box[k]) for k in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return []
    out: dict[str, None] = {}
    for phrase in context.pages.get(page, []):
        raw = phrase.get("box")
        if not isinstance(raw, Sequence) or len(raw) != 4:
            continue
        b = [float(v) for v in raw]
        if not (b[0] < x1 + 4 and b[2] > x0 - 4 and b[1] < y1 + 4 and b[3] > y0 - 4):
            continue
        text = _norm(str(phrase.get("text") or ""))
        if len(text.split()) >= _NOTE_WORDS:
            out.setdefault(text, None)
    return sorted(out, key=len, reverse=True)


def fold_repeats(engine: Any, context: SetContext) -> int:
    """The page reviewer's please-checks of one kind that sit on the same printed note on several
    sheets (a standard note on every millwork sheet: the $2/page run of 2026-10-29 raised
    "mounting cleats ... provided by Vendor" on 6 sheets, GV's reviewer on none) folded into one
    card: the lowest page's keeps them under `also_on` (each sheet's spot, so the drawing still
    marks every sheet), the others leave the list. Never a red: only `likely_only` please-checks
    made by the page reviewer. Returns how many were folded away."""
    from app.review.assistant import likely_problems

    book = engine.book
    notes: dict[str, set[int]] = defaultdict(set)
    for page, found in context.pages.items():
        for phrase in found:
            text = _norm(str(phrase.get("text") or ""))
            if len(text.split()) >= _NOTE_WORDS:
                notes[text].add(page)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    with engine.finding_lock:
        for finding in book.live():
            mark = finding.get(likely_problems.FIELD)
            if (
                not isinstance(mark, Mapping)
                or mark.get("by") != "page_reviewer"
                or mark.get("proof") != "likely_only"
                or finding.get("status") != "check"
                or not isinstance(finding.get("page"), int)
                or not isinstance(finding.get("vendor_box"), Mapping)
            ):
                continue
            for note in _phrases_under(context, int(finding["page"]), finding["vendor_box"]):
                if len(notes.get(note, ())) >= 2:
                    groups[(str(mark.get("kind")), note)].append(finding)
                    break
        folded = 0
        taken: set[str] = set()
        clusters: list[list[dict[str, Any]]] = []
        for group in groups.values():
            members = sorted(
                (m for m in group if str(m["id"]) not in taken),
                key=lambda m: (int(m["page"]), str(m["id"])),
            )
            # The same wording too: cards on one note that say different things stay apart.
            mine: list[list[dict[str, Any]]] = []
            for member in members:
                home = next((c for c in mine if _alike(c[0], member)), None)
                if home is None:
                    mine.append([member])
                else:
                    home.append(member)
            clusters += [c for c in mine if len({m["page"] for m in c}) >= 2]
        for cluster in clusters:
            keeper, others = cluster[0], [m for m in cluster[1:] if m["page"] != cluster[0]["page"]]
            if not others:
                continue
            also = list(keeper.get("also_on") or [])
            for other in others:
                # Its own id, as every other fold names it (`one_card`, the resolver): the screen
                # keys each line by it.
                also.append(
                    {
                        "finding_id": str(other["id"]),
                        "page": other["page"],
                        "vendor": other.get("vendor"),
                        "vendor_box": dict(other["vendor_box"]),
                    }
                )
                also += [dict(o) for o in other.get("also_on") or [] if isinstance(o, Mapping)]
            kept = {**keeper, "also_on": also}
            if not book.replace(kept):
                continue
            engine._show_finding(kept)
            taken.add(str(keeper["id"]))
            for other in others:
                taken.add(str(other["id"]))
                if book.absorb(str(other["id"])):
                    engine._show_dropped(
                        str(other["id"]),
                        f"the same note on another sheet: listed under {keeper['id']}",
                    )
                    folded += 1
    return folded


def add_to_answer(engine: Any) -> int:
    """The engine's hook (a full check, at its finish, $0): code's cross-sheet please-checks for
    every vendor page the check planned, added unless the answer already raises the tag; the
    look's mark laid on each (never red). Returns how many were added."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return 0
    from app.review.assistant import likely_problems
    from app.review.assistant.page_reviewer import set_context_of

    book = engine.book
    main = book.main_file
    if not main:
        return 0
    reviews = getattr(engine, "_page_reviews", None)
    if reviews is None:
        return 0  # the page reviewer is off: no set was read
    try:
        context = set_context_of(engine, reviews, str(main))
        if not context.ready(0):
            context.build()
        if not context.built:
            return 0
    except Exception:  # noqa: BLE001 - the set's pages not read: nothing to check, as before
        _log.warning("set check: the set's pages could not be read")
        return 0
    plan = getattr(engine, "_plan", None)
    pages = sorted({int(p) for job in getattr(plan, "jobs", ()) for p in job.pages}) or list(
        range(1, context.page_count + 1)
    )
    added = 0
    with engine.finding_lock:
        live = book.live()
        for page in pages:
            for finding in check_page(context, page):
                if _already(finding, live):
                    continue
                mark = finding.pop(likely_problems.FIELD)
                also = finding.pop("also_on", [])
                stored = book.add_code_finding(finding, created_by=CREATED_BY)
                if stored is None:
                    continue
                also = [{**entry, "finding_id": stored["id"]} for entry in also]
                marked = {**stored, likely_problems.FIELD: mark, "also_on": also}
                if book.replace(marked):
                    engine._show_finding(marked)
                    live.append(marked)
                    added += 1
    # After code's own checks: their duplicate test (`_already`) sees every reviewer card first.
    try:
        folded = fold_repeats(engine, context)
        if folded:
            _log.warning("set check: %d repeated please-checks folded into one card each", folded)
    except Exception:  # noqa: BLE001 - the cards stay as the reviewers left them
        _log.exception("set check: the repeated please-checks were not folded")
    return added
