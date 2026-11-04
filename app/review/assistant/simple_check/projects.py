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
- **One job, its names read whole and in parts** (`_jobs`): the set index reads one title block
  differently from one sheet (and one build) to the next: "HOTEL NORTH, SPRINGFIELD" whole on
  some, "HOTEL NORTH" and "SPRINGFIELD" as two names on others. A file's names are grouped into
  jobs before anything is placed: two names are one job when every word of one is in the other,
  step by step (so "HOTEL NORTH" and "SPRINGFIELD" join through "HOTEL NORTH SPRINGFIELD"), but
  never two jobs (each printed on sheets of its own and never beside the other on one sheet: two
  hotels a cover lists as "HOTEL NORTH + HOTEL SOUTH" stay two, and that name of both is no
  project), and never a name a third job holds too (the brand alone, in a file of two hotels of
  the brand, names neither).
- **Grouping names**: the jobs of the names on the pages that introduce others are the file's
  projects (every job, when no page introduces others). A page is in the project of its names.
- **Never a project**: GV's own name (`OWN_NAMES`, its stamp on the drawings it sends on), and a
  job printed only beside another project's name, never as a page's only one, while another
  project is (an office's stamp on every sheet).
- **Reference only**: a page whose own set kind (or, when it says none, the set kind of the page
  that introduced it) is a prototype, a brand standard or a reference (R1, Anant 2026-10-20: the
  prototype is reference only, never the architect's design to compare against).
- **The same project in both files** (`match`): the same letters and digits, or the words of a
  job (every name the index read of it in its file, `vocabulary`) that only one of the other
  file's projects uses. With one project in the other file, a job sharing a word of four letters
  or more with it is in it, unless another job of its own file shares a word with it that this
  one lacks (that one is the project's; this one is another job of the same town).
- **One job, two wordings** (`same_job`): within one file, a name whose words are all in another
  name of the file (and in no third) is the same job read in part.

Pure: plain values in and out. The work list (`worklist._pair_across`) uses it.
"""

from __future__ import annotations

import itertools
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

__all__ = [
    "OWN_NAMES",
    "REFERENCE_KINDS",
    "Place",
    "key",
    "match",
    "places",
    "same_job",
    "siblings_of",
    "vocabulary",
]

#: Set kinds (`set_index.SET_KINDS`) that are reference only.
REFERENCE_KINDS: Final = frozenset({"prototype", "brand standard", "reference"})
#: GV's own name (`key`): its stamp on the drawings it sends on, never a project of either file.
OWN_NAMES: Final = frozenset({"graniti vicentia"})
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
    source: str | None = None
    """The file a page of a joined upload came from (`set_pack._mark_sources`), else `None`:
    two names from different files are never rival projects of one file (`siblings_of`)."""
    words: frozenset[str] = frozenset()
    """Every word of the project's names the set index read in its file (`_jobs`: its name read
    whole and in parts), what `match` compares; empty: the words of `project`."""


def key(text: object) -> str:
    """A name normalised for matching: lower case, every sign a space."""
    return " ".join(_NOT_WORD.sub(" ", str(text or "").lower()).split())


def _compact(text: str) -> str:
    return text.replace(" ", "")


def _own_words(project: str, others: Iterable[str]) -> set[str]:
    """The words of a project's name no other project's name uses."""
    theirs = {w for other in others if other != project for w in _WORD.findall(other)}
    return {w for w in _WORD.findall(project) if w not in theirs}


def match(
    project: str,
    candidates: Iterable[str],
    siblings: Iterable[str] = (),
    words: Mapping[str, Iterable[str]] | None = None,
) -> str | None:
    """The one project of `candidates` that `project` names (see the module), or `None`.
    `siblings`: the project names of `project`'s own file: a word two of them use names neither
    ("PROJECT" in "PROJECT NORTH" and "PROJECT SOUTH"). `words`: each project's words
    (`vocabulary`: its name read whole and in parts), else a name's own.

    One project in the other file, and only words the siblings share with it ("HN - RIVERTON -
    PUBLIC AREAS" and "HN-LUXE - RIVERTON", one job's two wordings, share only RIVERTON): it is
    `project`'s when they share a word, unless a sibling shares a word with it that `project`
    lacks (that sibling is its job; `project` is another job of the same town: "MAPLE SUITES
    RIVERTON" beside "STAR SUITES RIVERTON", for the architect's "STAR SUITES RIVERTON")."""
    pool = sorted(set(candidates))
    same = [c for c in pool if _compact(c) == _compact(project)]
    if len(same) == 1:
        return same[0]

    def said(name: str) -> str:
        return " ".join(sorted(words[name])) if words and words.get(name) else name

    shared = {w for s in set(siblings) if s != project for w in _WORD.findall(said(s))}
    mine = set(_WORD.findall(said(project))) - shared
    found = [c for c in pool if mine & _own_words(said(c), [said(o) for o in pool if o != c])]
    if found or len(pool) != 1:
        return found[0] if len(found) == 1 else None
    theirs = set(_WORD.findall(said(pool[0])))
    every = set(_WORD.findall(said(project)))
    rivals = [set(_WORD.findall(said(s))) for s in set(siblings) if s != project]
    if every & theirs and not any((rival & theirs) - every for rival in rivals):
        return pool[0]
    return None


def vocabulary(placed: Mapping[tuple[str, int], Place]) -> dict[str, frozenset[str]]:
    """Each project's words (`Place.words`) by its name (`Place.project`): `match`'s `words`."""
    out: dict[str, set[str]] = {}
    for place in placed.values():
        if place.project is not None:
            out.setdefault(place.project, set()).update(place.words or place.project.split())
    return {name: frozenset(found) for name, found in out.items()}


def same_job(one: str, other: str, projects: Iterable[str] = ()) -> bool:
    """Whether two project names (normalised, `key`) name one job: the same name, or every word of
    one is in the other ("HOTEL NORTH" and "HOTEL NORTH, SPRINGFIELD": the set index read the
    name whole on some sheets and in part on others, so code holds both as projects of the file),
    when no other of the file's `projects` holds every word of the shorter one too (the brand
    alone, in a file of two hotels of the brand, names neither). A word of one the other lacks on
    both sides ("HOTEL NORTH" and "HOTEL SOUTH") is another job."""
    if one == other:
        return True
    a, b = set(one.split()), set(other.split())
    if not (a <= b or b <= a):
        return False
    shorter = a & b  # one holds every word of the other: the shorter name's words
    return not any(shorter <= set(p.split()) for p in set(projects) - {one, other})


def _jobs(pages: Sequence[tuple[frozenset[str], bool]]) -> dict[str, str]:
    """One file's names (`key`) grouped into jobs (see the module): each name -> its job (one of its
    names). `pages`: each page's names, and whether it introduces the pages after it (a cover's
    list is no evidence that names are one job, nor that they are two)."""
    names = sorted({name for found, _opens in pages for name in found})
    words = {name: frozenset(name.split()) for name in names}
    sheets = [found for found, opens in pages if not opens]
    seen: dict[tuple[str, str], bool] = {}

    def holds(name: str, part: str) -> bool:
        return words[part] <= words[name]

    def alone(one: str, other: str) -> bool:
        # One is printed on a sheet that does not print the other.
        return any(
            any(holds(n, one) for n in found) and not any(holds(n, other) for n in found)
            for found in sheets
        )

    def apart(one: str, other: str) -> bool:
        # Two jobs: each printed on sheets of its own, never as two names of one sheet (a title
        # block read "HOTEL NORTH" and "SPRINGFIELD" apart is one job's).
        if (one, other) not in seen:
            beside = any(
                any(holds(n, one) and not holds(n, other) for n in found)
                and any(holds(n, other) and not holds(n, one) for n in found)
                for found in sheets
            )
            seen[(one, other)] = seen[(other, one)] = (
                not beside and alone(one, other) and alone(other, one)
            )
        return seen[(one, other)]

    job = {name: name for name in names}

    def find(name: str) -> str:
        while job[name] != name:
            name = job[name]
        return name

    parts = [
        (short, long)
        for short, long in itertools.permutations(names, 2)
        if words[short] < words[long]
        or (short < long and (words[short] == words[long] or _compact(short) == _compact(long)))
    ]
    joined = True
    while joined:
        joined = False
        for short, long in parts:
            one, other = find(short), find(long)
            if one == other:
                continue
            # A third job holding every word of the shorter names neither (the brand alone); a
            # longer name holding another job's name too is both jobs' (a cover's), joining neither.
            if any(
                find(t) not in (one, other)
                and ((holds(t, short) and apart(t, long)) or (holds(long, t) and apart(t, short)))
                for t in names
            ):
                continue
            mine = [n for n in names if find(n) == one]
            theirs = [n for n in names if find(n) == other]
            if any(apart(a, b) for a in mine for b in theirs):
                continue
            job[other] = one
            joined = True
    return {name: find(name) for name in names}


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
        said = {
            p: {
                k: str(n) for n in one.get("projects") or [] if (k := key(n)) and k not in OWN_NAMES
            }
            for p, one in mine
        }
        opens = {p for p, one in mine if introduces(file, p, one)}
        job = _jobs([(frozenset(said[p]), p in opens) for p, _one in mine])
        named = {k for p in opens for k in said[p]} or set(job)
        anchors = {job[k] for k in named}
        # A job printed only beside another project's name, while another project is printed
        # alone, is no project (an office's stamp on the sheets of each job).
        solo = {
            next(iter(own))
            for p, _one in mine
            if len(own := {job[k] for k in said[p]} & anchors) == 1
        }
        anchors &= solo or anchors
        # A name holding the names of two other projects is both projects' (a cover's or a shared
        # sheet's "HOTEL NORTH + HOTEL SOUTH"): no project, and its page is in none.
        both = {
            job[k]
            for k in job
            if len({job[n] for n in job if set(n.split()) <= set(k.split())} & anchors - {job[k]})
            >= 2
        }
        anchors -= both
        printed_on = Counter(k for p, _one in mine for k in said[p])
        names = {k: n for p, _one in mine for k, n in said[p].items()}
        first: dict[str, str] = {}
        words: dict[str, set[str]] = {}
        for k in sorted(job, key=lambda k: (k not in named, -printed_on[k], -len(k.split()), k)):
            first.setdefault(job[k], k)
            words.setdefault(job[k], set()).update(k.split())
        current = Place()
        for page in ordered:
            found = read.get((file, page))
            if found is None:
                out[(file, page)] = Place()
                continue
            one = found
            # A name of no project of the file (an office's stamp) is left out; names of two
            # projects (a cover listing them) place the page in none.
            here = {job[k] for k in said.get(page, {})}
            own = here & anchors
            kind = str(one.get("set_kind") or "unknown")
            if len(own) == 1 and not here & both:
                anchor = first[next(iter(own))]
                place = Place(
                    anchor,
                    names.get(anchor, anchor),
                    kind in REFERENCE_KINDS
                    or (kind == "unknown" and current.reference and current.project == anchor),
                    words=frozenset(words[job[anchor]]),
                )
            elif own or here & both:
                place = Place(reference=kind in REFERENCE_KINDS)
            else:
                reference = kind in REFERENCE_KINDS if kind != "unknown" else current.reference
                place = Place(current.project, current.name, reference, words=current.words)
            if introduces(file, page, one):
                current = place
            out[(file, page)] = place
    sources = {
        (str(e.get("file")), int(e["page"])): str(e["source"])
        for e in entries
        if isinstance(e, Mapping) and isinstance(e.get("page"), int) and e.get("source")
    }
    for where, place in out.items():
        if where in sources:
            out[where] = replace(place, source=sources[where])
    return out


def siblings_of(placed: Mapping[tuple[str, int], Place], file: str, source: str | None) -> set[str]:
    """The projects of `file` that are rivals of a page from `source` (`match`'s `siblings`):
    every project of the file, or, in a joined upload, those of pages from the same source file
    (the vendor's cabinets and GV's stone tops name one job two ways)."""
    return {
        p.project
        for (f, _page), p in placed.items()
        if f == file and p.project is not None and (source is None or p.source == source)
    }


def architect_projects(placed: Mapping[tuple[str, int], Place], files: Iterable[str]) -> set[str]:
    """The projects the architect's files hold (never a reference set's)."""
    wanted = set(files)
    return {
        p.project
        for (file, _page), p in placed.items()
        if file in wanted and p.project is not None and not p.reference
    }
