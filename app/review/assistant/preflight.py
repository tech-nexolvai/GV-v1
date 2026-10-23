"""The pre-flight health report: what code found in an upload before any money is spent. $0.

The owner should never have to read logs to learn that a PDF broke a check. Before Run, the Run
popover shows a short list made by code from what the set pack already holds ("54 pages matched to
the architect · 25 need the architect's drawing · 2 pages can't be read"), each line with three
plain facts: **what was found**, **whether the check goes on**, and **who acts** (you, GV, the
vendor, the architect, or nobody).

**Only three things stop the whole check before money is spent (owner, 2026-10-23)**, plus the
existing wait for the reading:

1. **Files swapped**: the file uploaded as the architect's looks like a vendor shop drawing, or the
   same file was uploaded as both (`_swapped`).
2. **Another project**: the architect's file looks like another job: the set index's projects
   (`set_index.json` / `architect_index.json`, read by another builder) share none with the
   vendor's, or, in a two-file set, no vendor page has any architect page that could match it.
3. **A broken file**: a PDF that is damaged, locked with a password, empty, or could not be read
   at all.

Everything else is a plain note on the pages it touches; the other pages go on at full speed.

**Pairing numbers before and after Run.** With a separate architect's file, code's own pairing is
free and is known before Run; the AI page matcher (paid) runs only after Run. So before Run the
numbers are code's **estimate** and say so ("about 18 already matched by code; 78 are matched when
the check runs"); the pages that have no architect page that could ever match are certain. Once
the matcher's answers are saved in the pack, the same lines say the confirmed numbers.

**The architect index (optional).** When the set pack holds the set index another builder adds
(per page: the project as printed, the set kind, sheet, titled views, whether they print sizes),
this report adds the project lines (several projects, the architect's file with only floor plans
for a project, a vendor project with no architect drawings) and the "another project" stop. When
it is not there, those lines are simply absent.

Pure: plain values in (`Inputs`), a `Report` out; `gather` reads the pack's small files. The
pairing estimate is worked out elsewhere (`preflight_pairs`, the heavy work list) and passed in.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

__all__ = [
    "BIG_UPLOAD_PAGES",
    "INDEX_FILES",
    "STOP_KINDS",
    "Inputs",
    "Line",
    "PairEstimate",
    "ProjectGroup",
    "ProjectSummary",
    "Report",
    "build",
    "duplicate_pages",
    "gather",
    "load_index",
]

Level = Literal["stop", "warn", "note", "ok"]
Who = Literal["you", "GV", "the vendor", "the architect", "nobody"]

#: The only kinds of problem that stop the whole check before money is spent (owner, 2026-10-23).
STOP_KINDS: Final = ("files_swapped", "other_project", "broken_file", "reading")
#: The set index's file in the set pack (the pairing builder's name first, then the planned one).
INDEX_FILES: Final = ("set_index.json", "architect_index.json")
#: An upload this big gets a "takes longer" line.
BIG_UPLOAD_PAGES: Final = 150
#: A page with at least this many dimension labels says something about its file's side.
_SIDE_LABELS: Final = 5
#: Feet-and-inches as printed ("3'-6"", "12'").
_FEET: Final = re.compile(r"\d\s*'\s*-?\s*\d|\d\s*'(?!')")
_SHOP_WORDS: Final = re.compile(r"\bSHOP\s+DRAWINGS?\b|\bFOR\s+APPROVAL\b", re.IGNORECASE)
#: Health check (`extraction/page_health`): a scanned page and its threshold of typed characters.
_SCANNED_SHARE: Final = 0.5
_TYPED_FEW: Final = 20
#: Markups on an ordinary (not combined) vendor file from this many on: GV's notes are on it.
_MARKUPS_MANY: Final = 10
#: A page "in millimetres": at least this many millimetre-only values not read on it.
_MM_ONLY: Final = 5
#: Duplicates: a page's text at least this long (characters), the same labels too.
_DUPLICATE_TEXT: Final = 100


@dataclass(frozen=True, slots=True)
class Line:
    """One line of the report: what was found, whether the check goes on, who acts."""

    id: str
    level: Level
    words: str
    goes_on: bool = True
    who: Who = "nobody"
    todo: str | None = None
    """What to do, in a few words, when someone has to act."""
    pages: tuple[int, ...] = ()
    estimate: bool = False

    def out(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "level": self.level,
            "words": self.words,
            "goes_on": self.goes_on,
            "who": self.who,
            "todo": self.todo,
            "pages": list(self.pages),
            "estimate": self.estimate,
        }


@dataclass(frozen=True, slots=True)
class PairEstimate:
    """Code's pairing of the vendor pages with a separate architect's file (`preflight_pairs`):
    the vendor pages already paired (code's own pick, or a saved matcher answer), those the
    matcher will be asked about, and those no architect page could match (certain)."""

    vendor_pages: int
    paired: int
    to_ask: int
    none_possible: tuple[int, ...] = ()
    """Pages no architect page can match: code rules out every one, or the drawing is needed."""
    unsure: tuple[int, ...] = ()
    """Pages the matcher answered but code and it did not agree on one page: not paired, but
    the architect's file may hold the drawing (never "no architect drawing")."""
    confirmed: bool = False
    """Every page's pairing is final (no page waits for the matcher)."""


@dataclass(frozen=True, slots=True)
class ProjectGroup:
    """One project of the vendor's file, as the pairing groups the set index's names."""

    name: str
    vendor_pages: tuple[int, ...] = ()
    architect: bool = False
    """The architect's file holds drawings of it (the pairing's own match)."""


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    """The upload's projects by the pairing's own grouping (`preflight_pairs.project_summary`:
    `simple_check.projects.places` on the set index, exactly as the work list calls it)."""

    vendor: tuple[ProjectGroup, ...] = ()
    architect: tuple[str, ...] = ()
    only_plans: tuple[str, ...] = ()
    no_sizes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Inputs:
    """What the report is made from (all plain values; `gather` reads them from the pack)."""

    listing: Sequence[Mapping[str, Any]] = ()
    page_map: Mapping[str, Any] | None = None
    main_file: str | None = None
    reading_state: str = "none"
    reading_total: int = 0
    unreadable: Mapping[tuple[str, int], str] = field(default_factory=dict)
    reading_why: str | None = None
    health: Any = None
    dims: Any = None
    scales: Any = None
    coverage: Any = None
    texts: Mapping[tuple[str, int], str] = field(default_factory=dict)
    stickers: int = 0
    combined: Mapping[str, bool] | None = None
    index: Mapping[str, Mapping[str, Any]] | None = None
    """The set index's checked reads (only each page's set kind is read here)."""
    projects: ProjectSummary | None = None
    pairs: PairEstimate | None = None
    skipped: Sequence[tuple[int, str]] = ()
    round_doubts: int = 0
    missing_values: Sequence[str] = ()
    products: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class Report:
    lines: tuple[Line, ...]
    duplicates: Mapping[int, int] = field(default_factory=dict)
    """A vendor page that is an exact copy of an earlier one -> that earlier page."""
    facts: Mapping[str, Any] = field(default_factory=dict)
    """The plain facts the supervisor is shown (`plan_supervisor`)."""

    @property
    def stops(self) -> tuple[Line, ...]:
        return tuple(line for line in self.lines if line.level == "stop")

    def out(self) -> dict[str, Any]:
        return {
            "lines": [line.out() for line in self.lines],
            "stops": [line.id for line in self.stops],
            "estimate": any(line.estimate for line in self.lines),
        }


# ---- small readers ----------------------------------------------------------------------------


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pages(document: Any) -> list[Mapping[str, Any]]:
    pages = document.get("pages") if isinstance(document, Mapping) else None
    return [p for p in pages or [] if isinstance(p, Mapping) and isinstance(p.get("page"), int)]


def load_index(folder: Path | None) -> dict[str, dict[str, Any]] | None:
    """The set index's checked reads per page (`file#page`), or `None` when the pack has none."""
    if folder is None:
        return None
    for name in INDEX_FILES:
        document = _read(folder / name)
        pages = document.get("pages") if isinstance(document, Mapping) else None
        if isinstance(pages, Mapping) and pages:
            return {str(k): dict(v) for k, v in pages.items() if isinstance(v, Mapping)}
    return None


def _count(number: int, one: str, many: str | None = None) -> str:
    return f"{number} {one if number == 1 else (many or one + 's')}"


def _page_list(pages: Sequence[int], most: int = 6) -> str:
    shown = ", ".join(str(p) for p in sorted(pages)[:most])
    more = len(pages) - most
    return f"{shown} and {more} more" if more > 0 else shown


def _role(page_map: Mapping[str, Any] | None, file: str) -> str | None:
    roles = (page_map or {}).get("upload_roles") if isinstance(page_map, Mapping) else None
    return str(roles.get(file)) if isinstance(roles, Mapping) and roles.get(file) else None


def _files(inputs: Inputs) -> list[str]:
    files = (inputs.page_map or {}).get("files") if isinstance(inputs.page_map, Mapping) else None
    names = [str(f) for f in files or [] if f]
    return names or [str(item.get("name")) for item in inputs.listing if item.get("name")]


def _architect_kind(item: Mapping[str, Any]) -> bool:
    """Uploaded as the architect's drawings (`set_pack._is_architect_kind`'s rule)."""
    return str(item.get("kind") or "").strip().lower().startswith("the architect")


def _architect_files(inputs: Inputs) -> list[str]:
    """The files uploaded as the architect's: the page map's roles, else the uploads' kinds."""
    by_role = [f for f in _files(inputs) if _role(inputs.page_map, f) == "architect"]
    by_kind = [
        str(item.get("name"))
        for item in inputs.listing
        if _architect_kind(item) and str(item.get("name")) != inputs.main_file
    ]
    return list(dict.fromkeys([*by_role, *by_kind]))


def _vendor_entries(inputs: Inputs) -> list[Mapping[str, Any]]:
    """The main (vendor) file's pages with a vendor drawing on them."""
    out = []
    for entry in _pages(inputs.page_map):
        if inputs.main_file and str(entry.get("file")) != inputs.main_file:
            continue
        halves = [h for h in entry.get("halves") or [] if isinstance(h, Mapping)]
        if any(str(h.get("role")) == "vendor" for h in halves):
            out.append(entry)
    return out


# ---- the stops ----------------------------------------------------------------------------------


def _broken(inputs: Inputs) -> list[Line]:
    """Damaged, locked or empty files (stop when no page of a file can be read); some pages of a
    file not read (a note)."""
    lines: list[Line] = []
    for item in inputs.listing:
        name = str(item.get("name") or "a file")
        problem = item.get("intake_problem")
        if problem:
            lines.append(
                Line(
                    "broken_file",
                    "stop",
                    f"“{name}” could not be opened: {problem}.",
                    goes_on=False,
                    who="you",
                    todo="Upload a copy that opens (no password, not damaged).",
                )
            )
        elif int(item.get("pages") or 0) <= 0:
            lines.append(
                Line(
                    "broken_file",
                    "stop",
                    f"“{name}” has no pages.",
                    goes_on=False,
                    who="you",
                    todo="Upload the full PDF.",
                )
            )
    if inputs.reading_state == "failed" and not lines:
        lines.append(
            Line(
                "broken_file",
                "stop",
                f"The drawings could not be read ({inputs.reading_why or 'the reading failed'}).",
                goes_on=False,
                who="you",
                todo="Upload the set again; if it happens again, ask GV support.",
            )
        )
    return lines


def _labels(inputs: Inputs) -> dict[tuple[str, int], list[Mapping[str, Any]]]:
    return {
        (str(p.get("file")), int(p["page"])): [
            label for label in p.get("labels") or [] if isinstance(label, Mapping)
        ]
        for p in _pages(inputs.dims)
    }


def _side_signs(
    inputs: Inputs, file: str, labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]]
) -> tuple[int, int, int]:
    """(pages that look like an architect's, pages that look like a vendor's, pages with enough
    labels to tell) of one file. An architect's page: feet-and-inches labels are most of its
    labels, or it prints an architectural scale ("1/4" = 1'-0""). A vendor's page: millimetres
    printed with most of its sizes, or "shop drawing" / "for approval" printed on it."""
    architect = vendor = told = 0
    notes = {
        (str(p.get("file")), int(p["page"])): p.get("scale_notes") or []
        for p in _pages(inputs.dims)
    }
    pages = sorted({page for (name, page) in labels if name == file})
    for page in pages:
        mine = labels[(file, page)]
        printed = inputs.texts.get((file, page), "")
        scale_note = any("'" in str((n or {}).get("text", "")) for n in notes.get((file, page), []))
        if len(mine) < _SIDE_LABELS and not scale_note and not _SHOP_WORDS.search(printed):
            continue
        told += 1
        feet = sum(1 for label in mine if _FEET.search(str(label.get("text") or "")))
        mm = sum(1 for label in mine if label.get("mm") is not None)
        if scale_note or (mine and feet * 2 >= len(mine)):
            architect += 1
        elif (mine and mm * 2 >= len(mine)) or _SHOP_WORDS.search(printed):
            vendor += 1
    return architect, vendor, told


def _swapped(inputs: Inputs) -> list[Line]:
    """The file uploaded as the architect's looks like a vendor shop drawing (stop): the same file
    as the vendor's; or, on its own pages, no sign of an architect's drawing at all and vendor
    signs on at least half of the pages that tell; or the set index reads most of its pages as a
    shop drawing. Conservative: a file with too little to tell is never stopped."""
    by_name = {str(item.get("name")): item for item in inputs.listing}
    vendor = by_name.get(inputs.main_file or "")
    if vendor is not None and any(
        _architect_kind(item)
        and item is not vendor
        and item.get("sha256")
        and item.get("sha256") == vendor.get("sha256")
        for item in inputs.listing
    ):
        return [_swapped_line("it is the same file as the vendor's")]
    architects = _architect_files(inputs)
    if not architects:
        return []
    labels = _labels(inputs)
    for name in architects:
        item = by_name.get(name)
        same = (
            vendor is not None
            and item is not None
            and vendor.get("sha256")
            and vendor.get("sha256") == item.get("sha256")
        )
        architect_signs, vendor_signs, told = _side_signs(inputs, name, labels)
        kinds = [
            str(entry.get("set_kind") or "")
            for entry in (inputs.index or {}).values()
            if str(entry.get("file")) == name
        ]
        shop = sum(1 for kind in kinds if kind == "shop drawing")
        by_index = len(kinds) >= 3 and shop * 10 >= len(kinds) * 6
        # Every page that tells reads like a shop drawing and none like the architect's; on a
        # longer file, at least half of them and none like the architect's.
        by_code = architect_signs == 0 and (
            (told >= 1 and vendor_signs == told) or (told >= 3 and vendor_signs * 2 >= told)
        )
        if same:
            return [_swapped_line("it is the same file as the vendor's")]
        if by_index or by_code:
            return [_swapped_line("its pages read like shop drawings, not the architect's design")]
    return []


def _swapped_line(why: str) -> Line:
    return Line(
        "files_swapped",
        "stop",
        f"The file uploaded as the architect's looks like a vendor shop drawing ({why}).",
        goes_on=False,
        who="you",
        todo="Upload the architect's drawings as the architect's file.",
    )


def _other_project(inputs: Inputs) -> list[Line]:
    """The architect's file looks like another project (stop): by the pairing's own grouping of
    the set index (`ProjectSummary`), the architect's file holds projects but none of the vendor's;
    or, in a two-file set, not one vendor page has an architect page that could match it."""
    summary = inputs.projects
    if (
        summary is not None
        and summary.vendor
        and summary.architect
        and not any(group.architect for group in summary.vendor)
    ):
        return [
            Line(
                "other_project",
                "stop",
                "The architect's file looks like another project: it holds "
                f"{_names(summary.architect)}; the vendor's file holds "
                f"{_names([group.name for group in summary.vendor])}.",
                goes_on=False,
                who="you",
                todo="Upload the architect's drawings of this job.",
            )
        ]
    pairs = inputs.pairs
    # Every vendor page of the file is paired across the uploads (none has the architect's drawing
    # on its own sheet or in its area) and not one could be: certain, never a page still to ask.
    if (
        pairs is not None
        and pairs.vendor_pages > 0
        and pairs.vendor_pages >= len(_vendor_entries(inputs))
        and pairs.paired == 0
        and pairs.to_ask == 0
        and len(pairs.none_possible) >= pairs.vendor_pages
    ):
        return [
            Line(
                "other_project",
                "stop",
                "No vendor page has an architect page that could match it: the architect's file "
                "looks like another project, or lacks these items.",
                goes_on=False,
                who="you",
                todo="Upload the architect's drawings of these items.",
            )
        ]
    return []


# ---- the notes ----------------------------------------------------------------------------------


def _pairing_lines(inputs: Inputs) -> list[Line]:
    lines: list[Line] = []
    architects = set(_architect_files(inputs))
    if (
        architects
        and inputs.dims is not None
        and not any(mine for (file, _page), mine in _labels(inputs).items() if file in architects)
    ):
        lines.append(
            Line(
                "no_sizes",
                "warn",
                "The architect's file prints no sizes code could read (it may hold only plans or notes): "
                "vendor pages are checked only against the vendor's own drawings and GV's rules.",
                who="the architect",
                todo="Ask for the architect's dimensioned elevations of these items.",
            )
        )
    pairs = inputs.pairs
    if pairs is not None and pairs.vendor_pages:
        total = pairs.vendor_pages
        if pairs.confirmed:
            lines.append(
                Line(
                    "matched",
                    "ok",
                    f"{pairs.paired} of {_count(total, 'vendor page')} matched to the architect's "
                    "drawings.",
                )
            )
        else:
            lines.append(
                Line(
                    "matched",
                    "ok",
                    f"Code's first guess: {pairs.paired} of {_count(total, 'vendor page')} "
                    f"{'has' if total == 1 else 'have'} a likely architect page. When the "
                    "check runs, AI confirms each"
                    + (f" and tries the other {pairs.to_ask}" if pairs.to_ask else "")
                    + "; the final numbers come with the result.",
                    estimate=True,
                )
            )
        if pairs.none_possible and len(pairs.none_possible) < total:
            missing = len(pairs.none_possible)
            # Before the set's pages are indexed (each page's project and views), code's own
            # ruling-out is only a guess: the index changes the candidates (2026-10-23).
            guess = not pairs.confirmed and inputs.index is None
            have = "has" if missing == 1 else "have"
            words = (
                f"About {_count(missing, 'vendor page')} may have no architect drawing in this "
                "upload (an estimate: the final number comes with the result)."
                if guess
                else f"{_count(missing, 'vendor page')} {have} no architect drawing in this "
                "upload: checked only against the vendor's own drawings and GV's rules."
            )
            lines.append(
                Line(
                    "needs_drawing",
                    "warn" if missing * 2 > total and not guess else "note",
                    words,
                    who="GV",
                    todo="Send the architect's drawings of these items.",
                    pages=tuple(pairs.none_possible),
                    estimate=guess,
                )
            )
        if pairs.unsure:
            lines.append(
                Line(
                    "not_sure",
                    "note",
                    f"{_count(len(pairs.unsure), 'vendor page')} could not be matched to an "
                    "architect page for sure: not compared with the architect.",
                    pages=tuple(pairs.unsure),
                )
            )
        return lines
    if _architect_files(inputs):
        return lines
    # One file of GV's combined sheets: the architect's drawing is on the page or in its area.
    vendor = _vendor_entries(inputs)
    if not vendor:
        return lines
    from app.review.assistant.plan_check import architect_paired

    with_architect = [
        int(e["page"])
        for e in vendor
        if architect_paired(inputs.page_map, inputs.main_file, int(e["page"]))
    ]
    without = sorted({int(e["page"]) for e in vendor} - set(with_architect))
    lines.append(
        Line(
            "matched",
            "ok",
            f"{_count(len(with_architect), 'vendor page')} with the architect's drawing beside "
            f"{'it' if len(with_architect) == 1 else 'them'}.",
        )
    )
    if without:
        lines.append(
            Line(
                "needs_drawing",
                "warn" if len(without) * 2 > len(vendor) else "note",
                f"{_count(len(without), 'vendor page')} without an architect drawing in "
                "its area: checked only against the vendor's own drawings and GV's rules.",
                who="GV",
                todo="Add the architect's drawing of these areas.",
                pages=tuple(without),
            )
        )
    return lines


def _names(names: Sequence[str], most: int = 4) -> str:
    """Project names in one line: a semicolon between them (a name may hold a comma)."""
    shown = list(dict.fromkeys(names))
    more = len(shown) - most
    text = "; ".join(shown[:most])
    return f"{text} and {more} more" if more > 0 else text


def _project_lines(inputs: Inputs) -> list[Line]:
    """From the pairing's own grouping of the set index (`ProjectSummary`, never raw printed
    names): several projects in one line, a vendor project the architect's file has no drawings
    of, an architect project with only floor plans or no sizes."""
    summary = inputs.projects
    if summary is None:
        return []
    lines: list[Line] = []
    if len(summary.vendor) > 1:
        lines.append(
            Line(
                "several_projects",
                "note",
                f"Several projects in one upload: {_names([g.name for g in summary.vendor], 8)}. "
                "Each vendor page is matched only within its own project.",
            )
        )
    if summary.architect and any(group.architect for group in summary.vendor):
        for group in summary.vendor:
            if group.architect:
                continue
            lines.append(
                Line(
                    "project_missing",
                    "warn",
                    f"The architect's file has no drawings of {group.name}: its "
                    f"{_count(len(group.vendor_pages), 'vendor page')} "
                    f"{'is' if len(group.vendor_pages) == 1 else 'are'} checked only against the "
                    "vendor's own drawings and GV's rules.",
                    who="GV",
                    todo=f"Send the architect's drawings of {group.name}.",
                    pages=group.vendor_pages,
                )
            )
    for name in summary.only_plans:
        lines.append(
            Line(
                "only_plans",
                "warn",
                f"The architect's file has only floor plans for {name}: sizes of the millwork "
                "itself can't be compared there.",
                who="the architect",
                todo=f"Ask for the millwork elevations of {name}.",
            )
        )
    for name in summary.no_sizes:
        lines.append(
            Line(
                "no_sizes",
                "warn",
                f"The architect's drawings of {name} print no sizes of the millwork.",
                who="the architect",
                todo=f"Ask for dimensioned elevations of {name}.",
            )
        )
    return lines


def duplicate_pages(
    main_file: str | None, dims: Any, texts: Mapping[tuple[str, int], str]
) -> dict[int, int]:
    """A vendor page that is an exact copy of an earlier page of the same file (the same printed
    text, at least `_DUPLICATE_TEXT` characters, and the same dimension labels) -> that page."""
    if not main_file:
        return {}
    labels = {
        int(p["page"]): tuple(sorted(str(label.get("text")) for label in p.get("labels") or []))
        for p in _pages(dims)
        if str(p.get("file")) == main_file
    }
    seen: dict[str, int] = {}
    out: dict[int, int] = {}
    for (file, page), text in sorted(texts.items()):
        if file != main_file:
            continue
        flat = " ".join(str(text).split())
        if len(flat) < _DUPLICATE_TEXT:
            continue
        key = hashlib.sha256(
            (flat + "\x00" + "\x01".join(labels.get(page, ()))).encode("utf-8")
        ).hexdigest()
        if key in seen:
            out[page] = seen[key]
        else:
            seen[key] = page
    return out


def _reading_lines(inputs: Inputs) -> list[Line]:
    lines: list[Line] = []
    architects = set(_architect_files(inputs))
    for side in ("vendor", "architect"):
        found = {
            (file, page): why
            for (file, page), why in inputs.unreadable.items()
            if (file in architects) == (side == "architect")
        }
        if inputs.reading_state != "ready" or not found:
            continue
        first = next(iter(found.values()))
        why = first.split(": ", 1)[1] if ": " in first else ""
        pages = sorted(page for (_file, page) in found)
        lines.append(
            Line(
                "unreadable",
                "note",
                f"{_count(len(pages), f'{side} page')} can't be read"
                f"{f' ({why})' if why and len(why) < 90 else ''}: "
                f"{'not checked' if side == 'vendor' else 'not used for matching'}.",
                pages=tuple(pages) if side == "vendor" else (),
            )
        )
    not_read = [
        int(p.get("page") or 0)
        for item in inputs.listing
        for p in item.get("pages_not_read") or []
        if isinstance(p, Mapping)
    ]
    if not_read:
        lines.append(
            Line(
                "pages_broken",
                "note",
                f"{_count(len(not_read), 'page')} of the PDF could not be opened: not checked.",
                who="you",
                todo="Upload a clean copy if you have one.",
                pages=tuple(sorted(not_read)),
            )
        )
    return lines


def _health_lines(inputs: Inputs) -> list[Line]:
    scanned: list[int] = []
    drawn: list[int] = []
    for page in _pages(inputs.health):
        if inputs.main_file and str(page.get("file")) != inputs.main_file:
            continue
        facts = page.get("facts") if isinstance(page.get("facts"), Mapping) else {}
        assert isinstance(facts, Mapping)
        typed = int(facts.get("typed_text") or 0)
        if float(facts.get("picture_share") or 0) > _SCANNED_SHARE and typed < _TYPED_FEW:
            scanned.append(int(page["page"]))
        elif facts.get("drawn_letters"):
            drawn.append(int(page["page"]))
    lines: list[Line] = []
    if scanned:
        lines.append(
            Line(
                "scanned",
                "note",
                f"{_count(len(scanned), 'page')} {'is a scanned picture' if len(scanned) == 1 else 'are scanned pictures'}: "
                "numbers are read from the picture, so fewer of them are read.",
                pages=tuple(scanned),
            )
        )
    if drawn:
        lines.append(
            Line(
                "hand_drawn",
                "note",
                f"{_count(len(drawn), 'page')} with hand-drawn numbers: fewer of them are read.",
                pages=tuple(drawn),
            )
        )
    return lines


def _units_and_scale(inputs: Inputs) -> list[Line]:
    mm_pages = []
    for page in _pages(inputs.coverage):
        if inputs.main_file and str(page.get("file")) != inputs.main_file:
            continue
        mm = sum(
            int(spot.get("count") or 1)
            for spot in page.get("not_read") or []
            if isinstance(spot, Mapping) and spot.get("reason") == "mm_row"
        )
        sides = page.get("sides") if isinstance(page.get("sides"), Mapping) else {}
        assert isinstance(sides, Mapping)
        read = sum(
            int((s or {}).get("read") or 0) for s in sides.values() if isinstance(s, Mapping)
        )
        if mm >= _MM_ONLY and read * 2 < mm:
            mm_pages.append(int(page["page"]))
    measured = {
        int(p["page"])
        for p in _pages(inputs.scales)
        if str(p.get("file")) == inputs.main_file and p.get("scales")
    }
    vendor = {int(e["page"]) for e in _vendor_entries(inputs)}
    labelled = {
        page
        for (file, page), mine in _labels(inputs).items()
        if file == inputs.main_file and len(mine) >= _SIDE_LABELS
    }
    no_scale = sorted((vendor & labelled) - measured) if inputs.scales is not None else []
    lines: list[Line] = []
    if mm_pages:
        lines.append(
            Line(
                "millimetres",
                "note",
                f"{_count(len(mm_pages), 'page')} in millimetres only: those sizes are not "
                "compared (GV compares inches).",
                who="the vendor",
                todo="Ask for sizes in inches.",
                pages=tuple(mm_pages),
            )
        )
    if no_scale:
        lines.append(
            Line(
                "no_scale",
                "note",
                f"No scale found on {_count(len(no_scale), 'page')}: their sizes are compared "
                "by the printed numbers only.",
                pages=tuple(no_scale),
            )
        )
    return lines


def _upload_lines(inputs: Inputs, duplicates: Mapping[int, int]) -> list[Line]:
    lines: list[Line] = []
    if duplicates:
        copies = sorted(duplicates)
        firsts = [duplicates[p] for p in copies]
        shown = ", ".join(
            f"page {p} = page {f}" for p, f in zip(copies[:4], firsts[:4], strict=True)
        )
        more = f" and {len(copies) - 4} more" if len(copies) > 4 else ""
        lines.append(
            Line(
                "duplicates",
                "note",
                f"{_count(len(copies), 'vendor page')} {'is an exact copy' if len(copies) == 1 else 'are exact copies'} "
                f"of an earlier page ({shown}{more}): each is checked once.",
                pages=tuple(copies),
            )
        )
    # GV's own combined review sheets carry GV's headings and notes by design; a vendor file
    # uploaded beside an architect's file should be the vendor's own.
    combined = (inputs.combined or {}).get(inputs.main_file or "") and not _architect_files(inputs)
    markups = sum(
        int((p.get("facts") or {}).get("markups") or 0)
        for p in _pages(inputs.health)
        if str(p.get("file")) == inputs.main_file and isinstance(p.get("facts"), Mapping)
    )
    if not combined and (inputs.stickers or markups >= _MARKUPS_MANY):
        lines.append(
            Line(
                "gv_markup",
                "note",
                "The vendor's file already has GV's notes on it: they are left out of the check.",
                who="you",
                todo="Upload the vendor's clean file if you have it.",
            )
        )
    if inputs.round_doubts:
        lines.append(
            Line(
                "round_unknown",
                "note",
                f"{_count(inputs.round_doubts, 'drawing')} {'shows' if inputs.round_doubts == 1 else 'show'} "
                "no round number: taken as the round most likely.",
            )
        )
    total = sum(int(item.get("pages") or 0) for item in inputs.listing)
    if total > BIG_UPLOAD_PAGES:
        lines.append(Line("big", "note", f"Big upload ({total} pages): the check takes longer."))
    if inputs.skipped:
        lines.append(
            Line(
                "skipped",
                "ok",
                f"{_count(len(inputs.skipped), 'page')} skipped: title, divider or "
                "general pages with no drawing to compare.",
            )
        )
    return lines


def _inputs_lines(inputs: Inputs) -> list[Line]:
    lines = [
        Line(
            "no_spec",
            "note",
            "Not checked: materials, grade and hardware (no spec book or schedules uploaded).",
            who="GV",
            todo="Send the spec book and schedules if these should be checked.",
        )
    ]
    if inputs.missing_values:
        lines.append(
            Line(
                "values_needed",
                "note",
                f"Ask GV for these values: {', '.join(inputs.missing_values)}. The checks that "
                "need them wait for them.",
                who="GV",
                todo="Enter them under the set's settings.",
            )
        )
    return lines


def _pages_line(inputs: Inputs) -> Line | None:
    vendor = len(_vendor_entries(inputs))
    architect_files = set(_architect_files(inputs))
    architect = sum(
        1
        for e in _pages(inputs.page_map)
        if str(e.get("file")) in architect_files and e.get("halves")
    )
    if not vendor and not architect:
        return None
    words = f"{_count(vendor, 'vendor page')} to check"
    if architect:
        words += f" · {_count(architect, 'architect page')}"
    return Line("pages", "ok", words + ".")


def build(inputs: Inputs) -> Report:
    """The report (see the module): the stops first, then what was found, then the notes."""
    duplicates = duplicate_pages(inputs.main_file, inputs.dims, inputs.texts)
    stops = [*_broken(inputs), *_swapped(inputs)]
    if not stops:
        stops.extend(_other_project(inputs))
    pages = _pages_line(inputs)
    found = [
        *([pages] if pages else []),
        *_pairing_lines(inputs),
        *_project_lines(inputs),
        *_reading_lines(inputs),
        *_health_lines(inputs),
        *_units_and_scale(inputs),
        *_upload_lines(inputs, duplicates),
        *_inputs_lines(inputs),
    ]
    lines = (*stops, *found)
    return Report(lines=lines, duplicates=duplicates, facts=_facts(inputs, lines))


def _facts(inputs: Inputs, lines: Sequence[Line]) -> dict[str, Any]:
    """The plain facts the supervisor sees: no file names' paths, no picture."""
    summary = inputs.projects
    pairs = inputs.pairs
    return {
        "files": [
            {"role": _role(inputs.page_map, f) or "vendor", "pages": int(item.get("pages") or 0)}
            for f in _files(inputs)
            for item in inputs.listing
            if str(item.get("name")) == f
        ],
        "vendor_pages": len(_vendor_entries(inputs)),
        "pairing": (
            None
            if pairs is None
            else {
                "paired": pairs.paired,
                "to_be_matched_by_ai": pairs.to_ask,
                "no_possible_architect_page": len(pairs.none_possible),
                "final": pairs.confirmed,
            }
        ),
        "architect_projects": [] if summary is None else list(summary.architect),
        "vendor_projects": (
            []
            if summary is None
            else [
                {
                    "name": g.name,
                    "pages": len(g.vendor_pages),
                    "architect_has_drawings": g.architect,
                }
                for g in summary.vendor
            ]
        ),
        "products": list(inputs.products),
        "findings_so_far": [
            {"what": line.words, "stops": not line.goes_on, "who": line.who} for line in lines
        ],
    }


# ---- reading the pack ---------------------------------------------------------------------------


def _stickers(folder: Path, main_file: str | None) -> int:
    count = 0
    for path in folder.glob("*_stickers.json"):
        document = _read(path)
        if isinstance(document, Mapping) and str(document.get("file")) == main_file:
            count += len(document.get("stickers") or [])
    return count


def _combined(annotations: Any) -> dict[str, bool] | None:
    """Each file with a pasted drawing (a PDF stamp) is one of GV's combined review sheets (the
    work list's `combined_files` rule, read here without it); `None` with no annotations list."""
    if not isinstance(annotations, list):
        return None
    return {
        str(a.get("file")): True
        for a in annotations
        if isinstance(a, Mapping) and a.get("kind") == "Stamp"
    }


def gather(
    folder: Path,
    *,
    listing: Sequence[Mapping[str, Any]],
    main_file: str | None,
    texts: Mapping[tuple[str, int], str] | None = None,
    pairs: PairEstimate | None = None,
    projects: ProjectSummary | None = None,
    skipped: Sequence[tuple[int, str]] = (),
    missing_values: Sequence[str] = (),
    products: Sequence[str] = (),
) -> Inputs:
    """The report's inputs from the set pack in `folder` (its small and middle files; never a PDF)."""
    from app.review.assistant.full_check_plan import read_page_map, reading_of
    from app.review.assistant.plan_check import doubts, read_answers

    page_map = read_page_map(folder)
    reading = reading_of(folder)
    round_doubts = sum(
        1
        for d in doubts(page_map, main_file, read_answers(folder), limit=None)
        if d.kind == "round"
    )
    annotations = _read(folder / "annotations.json")
    return Inputs(
        listing=listing,
        page_map=page_map,
        main_file=main_file,
        reading_state=reading.state,
        reading_total=reading.total,
        unreadable=dict(reading.unreadable),
        reading_why=reading.why,
        health=_read(folder / "health.json"),
        dims=_read(folder / "dims.json"),
        scales=_read(folder / "scales.json"),
        coverage=_read(folder / "reading_coverage.json"),
        texts=dict(texts or {}),
        stickers=_stickers(folder, main_file),
        combined=_combined(annotations),
        index=load_index(folder),
        projects=projects,
        pairs=pairs,
        skipped=skipped,
        round_doubts=round_doubts,
        missing_values=missing_values,
        products=products,
    )
