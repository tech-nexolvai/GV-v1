"""What a chat answer is about: the whole drawing set, or some pages or one area (#1520).

A narrow question ("do the fillers on the bussing station, page 8, meet GV's filler rule?") must
get a narrow answer: its coverage panel, GV Review's recorded results and the trust counts cover
its pages only. Only a whole-set answer gets the whole-set machinery (the "every page accounted
for" send-back, the recorded differences seeded or sent back, the all-pages coverage panel).

The scope is worked out by code, never by the model, $0:

- `whole_set`: a full check; a question about the whole set ("the whole set", "every page", "all
  the drawings"); a question that names no page and no area of the set; or an
  answer that worked on more than `WIDEN_PAGES` pages outside the ones the question named.
- `pages`: the pages the question names ("page 8", "p.8", "pages 27-28", "sheets 3 and 5"), or a
  full check's picked pages (`ChatMessageIn.pages`), plus the pages the answer worked on (its
  coverage and its own findings).
- `area`: an area of the set the question names, matched against the set pack's `page_map.json`
  (the typed area titles, e.g. "4. Bussing station"), and the pages that area is on.

The shape, stored on the message (its `scope` block, the trust report's `scope`, the message's
`scope` field) and streamed as the SSE `scope` event:
`{kind: "whole_set"|"pages"|"area", pages: [int], area: str|None, why: str, label: str}`.
`pages` is empty for `whole_set`. `why` is one of `SCOPE_WHYS`.

Verification: `tests/review/assistant/test_chat_scope.py`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from app.review.assistant.chat_findings import page_ranges
from app.review.assistant.chat_tools import named_pages
from app.review.assistant.chat_trust import Recorded

__all__ = [
    "SCOPE_KINDS",
    "SCOPE_WHYS",
    "WIDEN_PAGES",
    "area_pages",
    "in_scope",
    "is_whole_set",
    "load_page_map",
    "question_of",
    "question_scope",
    "scope_note",
    "scoped_pages",
    "scoped_recorded",
    "widened",
]

SCOPE_KINDS: Final = ("whole_set", "pages", "area")
SCOPE_WHYS: Final = (
    "full_check",
    "whole_set_words",
    "nothing_named",
    "wide_work",
    "pages_named",
    "area_named",
)
#: An answer that works on more pages than this outside the question's own becomes whole-set work.
WIDEN_PAGES: Final = 5

#: Words that ask about the whole set, even beside a page ("page 8 and every other page").
_WHOLE_SET: Final = re.compile(
    r"\b(?:whole|entire|full|complete)\s+(?:drawing\s+)?(?:set|package|submittal|pdf|file)\b"
    r"|\b(?:all|every|each)\s+(?:of\s+)?(?:the\s+)?(?:other\s+)?"
    r"(?:pages?|sheets?|drawings?|areas?)\b"
    r"|\bfull\s+check\b|\bacross\s+the\s+(?:set|package|drawings)\b",
    re.IGNORECASE,
)
#: A typed area title's numbering ("4. Bussing station", "2.Market area-2").
_NUMBERING: Final = re.compile(r"^\s*\d{1,3}\s*[.)]\s*")
_NOT_WORD: Final = re.compile(r"[^0-9a-z]+")
#: An area name shorter than this is not matched (too likely to be an ordinary word).
_MIN_AREA_CHARS: Final = 4


def _words(text: str) -> str:
    return " ".join(_NOT_WORD.sub(" ", text.lower()).split())


def question_of(user_content: Sequence[Mapping[str, Any]]) -> str:
    """The reviewer's question: the user turn's last text (the app's notes come before it)."""
    texts = [
        str(block.get("text") or "")
        for block in user_content
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    return texts[-1] if texts else ""


def load_page_map(folder: Path) -> Mapping[str, Any] | None:
    """The set pack's `page_map.json`, or `None` when it is not made (yet) or cannot be read."""
    try:
        loaded = json.loads((folder / "page_map.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, Mapping) else None


def area_pages(
    page_map: Mapping[str, Any] | None, main_file: str | None = None
) -> dict[str, tuple[str, list[int]]]:
    """Each area of the set by its plain name: (its title as typed, without the numbering; its
    pages). Only the vendor's file's pages when the map names files and `main_file` is given."""
    out: dict[str, tuple[str, list[int]]] = {}
    for entry in (page_map or {}).get("pages") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("page"), int):
            continue
        if main_file and entry.get("file") and entry.get("file") != main_file:
            continue
        title = entry.get("area")
        if not isinstance(title, str) or not title.strip():
            continue
        shown = " ".join(_NUMBERING.sub("", title).split())
        key = _words(shown)
        if len(key) < _MIN_AREA_CHARS:
            continue
        _title, pages = out.setdefault(key, (shown, []))
        if entry["page"] not in pages:
            pages.append(entry["page"])
    return out


def _named_area(
    question: str, areas: Mapping[str, tuple[str, list[int]]]
) -> tuple[str, list[int]] | None:
    """The areas the question names, longest name first so "buffet island" is not also "buffet":
    their titles joined, and their pages."""
    text = f" {_words(question)} "
    titles: list[str] = []
    pages: list[int] = []
    for key in sorted(areas, key=len, reverse=True):
        if f" {key} " not in text:
            continue
        text = text.replace(f" {key} ", " | ")
        title, found = areas[key]
        titles.append(title)
        pages += [page for page in found if page not in pages]
    if not titles:
        return None
    return " and ".join(titles), sorted(pages)


def _label(kind: str, pages: Sequence[int], area: str | None) -> str:
    if kind == "whole_set":
        return "the whole set"
    where = f"page {pages[0]}" if len(pages) == 1 else f"pages {page_ranges(list(pages))}"
    if area and kind == "area":
        return f"{area} ({where})" if pages else area
    return f"{where} ({area})" if area else where


def _scope(
    kind: str, why: str, pages: Sequence[int] = (), area: str | None = None
) -> dict[str, Any]:
    shown = sorted(set(pages)) if kind != "whole_set" else []
    return {
        "kind": kind,
        "pages": shown,
        "area": area,
        "why": why,
        "label": _label(kind, shown, area),
    }


def question_scope(
    question: str,
    *,
    mode: str = "chat",
    picked: Sequence[int] = (),
    page_map: Mapping[str, Any] | None = None,
    page_count: int = 0,
    main_file: str | None = None,
) -> dict[str, Any]:
    """The scope the question asks for, before any work (see the module's docstring). `picked`:
    a full check's picked pages (a fix's test), its scope."""
    if mode == "full_check":
        return (
            _scope("pages", "full_check", picked) if picked else _scope("whole_set", "full_check")
        )
    text = question or ""
    if _WHOLE_SET.search(text):
        return _scope("whole_set", "whole_set_words")
    pages = [page for page in named_pages(text) if not page_count or page <= page_count]
    area = _named_area(text, area_pages(page_map, main_file))
    if pages:
        return _scope("pages", "pages_named", pages, area[0] if area else None)
    if area is not None and area[1]:
        return _scope("area", "area_named", area[1], area[0])
    return _scope("whole_set", "nothing_named")


def widened(scope: Mapping[str, Any], used: Iterable[int]) -> dict[str, Any]:
    """The scope once the answer's work is known: a page or area scope takes in the pages the
    answer worked on, or becomes `whole_set` when they are more than `WIDEN_PAGES` outside it."""
    if scope.get("kind") == "whole_set":
        return dict(scope)
    own = set(scope.get("pages") or [])
    extra = {page for page in used if isinstance(page, int) and page > 0} - own
    if len(extra) > WIDEN_PAGES:
        return _scope("whole_set", "wide_work")
    if not extra:
        return dict(scope)
    return _scope(str(scope["kind"]), str(scope["why"]), sorted(own | extra), scope.get("area"))


def is_whole_set(scope: Mapping[str, Any] | None) -> bool:
    return scope is None or scope.get("kind") == "whole_set"


def scoped_pages(scope: Mapping[str, Any] | None) -> frozenset[int] | None:
    """The pages of a page or area scope; `None` for the whole set."""
    if is_whole_set(scope):
        return None
    assert scope is not None
    return frozenset(int(page) for page in scope.get("pages") or [])


def in_scope(scope: Mapping[str, Any] | None, page: object) -> bool:
    """Whether a page is in the scope (every page is, for the whole set; a finding with no page
    is kept)."""
    pages = scoped_pages(scope)
    return pages is None or not isinstance(page, int) or page in pages


def scope_note(scope: Mapping[str, Any]) -> str | None:
    """What the lead is told about a page or area scope (none for the whole set)."""
    if is_whole_set(scope):
        return None
    return (
        f"The app scoped this question to {scope['label']}. Answer about those pages (open "
        "another page only when the question needs it). For GV Review's recorded results, call "
        "review_results with a page; mention them only when they bear on the question."
    )


def scoped_recorded(recorded: Recorded, pages: frozenset[int] | None) -> Recorded:
    """GV Review's recorded differences (and checks waiting for a job value) on a page or area
    answer's pages only; all of them for the whole set (`pages` None). Each check's result by its
    id stays whole: a rules finding is matched to its check by id, wherever the check's page is."""
    if pages is None or not recorded.readable:
        return recorded
    return Recorded(
        [item for item in recorded.differences if item.get("page") in pages],
        recorded.results,
        waiting=[item for item in recorded.waiting if item.get("page") in pages],
    )
