"""Which project each page of an upload is in, grouped by code from the set index. Code, $0.

A real two-file upload can hold several projects in each file (four hotels in one vendor PDF and
one architect PDF), and the architect's file may hold a brand prototype besides the projects' own
drawings. The set index (`set_index.py`) reads each page by AI and keeps only what the page
prints: the project names, the kind of set and the words that say it, whether the page introduces
the pages after it (a cover, a divider). Code groups them here; no fixed word or layout is relied
on.

- **A page's project**: the project names it prints itself (title block, heading), or, when it
  prints none, those of the last page before it that introduces the pages after it (the read says
  so, or the page has no drawing and prints nothing but a project's name and its set's words: a
  divider). A page
  printing names of two projects (a cover listing every project) is in none; a name that is no
  project of the file (an office's own stamp) is left out.
- **Grouping names**: the names on the pages that introduce others are the file's projects (all
  names, when no page introduces others). A name belongs to the project whose own words it holds:
  a word of four letters or more that no other project's name uses (`NORTHFIELD`, never `HOTEL`),
  or the same letters and digits (`GH NORTHFIELD`).
- **Reference only**: a page whose own set kind (or, when it says none, the set kind of the page
  that introduced it) is a prototype, a brand standard or a reference (R1, Anant 2026-10-20: the
  prototype is reference only, never the architect's design to compare against).
- **The same project in both files** (`match`): the same letters and digits, or the words of a
  name that only one of the other file's projects uses.

Pure: plain values in and out. The work list (`worklist._pair_across`) uses it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "REFERENCE_KINDS",
    "Place",
    "key",
    "match",
    "places",
]

#: Set kinds (`set_index.SET_KINDS`) that are reference only.
REFERENCE_KINDS: Final = frozenset({"prototype", "brand standard", "reference"})
_NOT_WORD: Final = re.compile(r"[^a-z0-9]+")
_TOKEN: Final = re.compile(r"[A-Z0-9]+")
#: A project's own word: four letters or more.
_WORD: Final = re.compile(r"[a-z]{4,}")


@dataclass(frozen=True, slots=True)
class Place:
    """A page's place among its file's projects."""

    project: str | None = None
    """The project's name normalised (`key`), `None` when code cannot place the page."""
    name: str | None = None
    """The project's name as printed."""
    reference: bool = False
    """The page belongs to a prototype, brand standard or reference set (R1)."""


def key(text: object) -> str:
    """A name normalised for matching: lower case, every sign a space."""
    return " ".join(_NOT_WORD.sub(" ", str(text or "").lower()).split())


def _compact(text: str) -> str:
    return text.replace(" ", "")


def _own_words(project: str, others: Iterable[str]) -> set[str]:
    """The words of a project's name no other project's name uses."""
    theirs = {w for other in others if other != project for w in _WORD.findall(other)}
    return {w for w in _WORD.findall(project) if w not in theirs}


def match(project: str, candidates: Iterable[str], siblings: Iterable[str] = ()) -> str | None:
    """The one project of `candidates` that `project` names (see the module), or `None`.
    `siblings`: the project names of `project`'s own file: a word two of them use names neither
    ("PROJECT" in "PROJECT NORTH" and "PROJECT SOUTH")."""
    pool = sorted(set(candidates))
    same = [c for c in pool if _compact(c) == _compact(project)]
    if len(same) == 1:
        return same[0]
    shared = {w for s in set(siblings) if s != project for w in _WORD.findall(s)}
    mine = set(_WORD.findall(project)) - shared
    found = [c for c in pool if mine & _own_words(c, pool)]
    return found[0] if len(found) == 1 else None


def places(
    index: Mapping[str, Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    printed: Mapping[tuple[str, int], str] | None = None,
) -> dict[tuple[str, int], Place]:
    """Each page's place (`Place`) from the set index (`set_index.load`, checked reads) and the
    page map's pages (`entries`, for the order). `printed`: each page's printed words (its text and
    GV's typed boxes), to tell a divider (it prints nothing else) from a page of notes. A page the
    index has not read has no place."""
    read: dict[tuple[str, int], Mapping[str, Any]] = {}
    for one in index.values():
        if isinstance(one, Mapping) and isinstance(one.get("page"), int):
            read[(str(one.get("file")), int(one["page"]))] = one
    by_file: dict[str, list[int]] = {}
    drawn: set[tuple[str, int]] = set()
    for entry in entries:
        if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
            by_file.setdefault(str(entry.get("file")), []).append(int(entry["page"]))
            if entry.get("halves"):
                drawn.add((str(entry.get("file")), int(entry["page"])))

    def introduces(file: str, page: int, one: Mapping[str, Any]) -> bool:
        # A page with no drawing that prints nothing but a project's name and its set's words (a
        # divider) introduces the pages after it, whether or not the read said so.
        if one.get("introduces"):
            return True
        if (file, page) in drawn or not one.get("projects") or printed is None:
            return False
        said = {
            w
            for text in [*(one.get("projects") or []), *(one.get("set_words") or [])]
            for w in _TOKEN.findall(str(text).upper())
        }
        rest = [
            w
            for w in _TOKEN.findall(str(printed.get((file, page)) or "").upper())
            if w not in said and not w.isdigit()
        ]
        return not rest

    out: dict[tuple[str, int], Place] = {}
    for file, pages in by_file.items():
        ordered = sorted(set(pages))
        mine = [(p, read[(file, p)]) for p in ordered if (file, p) in read]
        anchors = sorted(
            {
                key(n)
                for p, one in mine
                if introduces(file, p, one)
                for n in one.get("projects") or []
            }
            or {key(n) for _p, one in mine for n in one.get("projects") or []}
        )
        names = {key(n): n for _p, one in mine for n in one.get("projects") or []}
        current = Place()
        for page in ordered:
            found = read.get((file, page))
            if found is None:
                out[(file, page)] = Place()
                continue
            one = found
            # A name that is no project of the file (an office's stamp) is left out; names of two
            # projects (a cover listing them) place the page in none.
            own = {match(key(n), anchors, anchors) for n in one.get("projects") or []} - {None}
            kind = str(one.get("set_kind") or "unknown")
            if len(own) == 1:
                anchor = next(iter(own))
                assert anchor is not None
                place = Place(
                    anchor,
                    names.get(anchor, anchor),
                    kind in REFERENCE_KINDS
                    or (kind == "unknown" and current.reference and current.project == anchor),
                )
            elif own:
                place = Place(reference=kind in REFERENCE_KINDS)
            else:
                reference = kind in REFERENCE_KINDS if kind != "unknown" else current.reference
                place = Place(current.project, current.name, reference)
            if introduces(file, page, one):
                current = place
            out[(file, page)] = place
    return out


def architect_projects(placed: Mapping[tuple[str, int], Place], files: Iterable[str]) -> set[str]:
    """The projects the architect's files hold (never a reference set's)."""
    wanted = set(files)
    return {
        p.project
        for (file, _page), p in placed.items()
        if file in wanted and p.project is not None and not p.reference
    }
