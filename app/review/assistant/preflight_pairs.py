"""Code's free pairing estimate for the pre-flight report, worked out once in the background. $0.

With a separate architect's file, code pairs each vendor drawing by its own ranking before any
money is spent; the AI page matcher (paid) is asked only after Run (`page_match.py`). Building the
work list takes seconds to half a minute on a big set (about 25 s on a 188-page upload), too slow
for the Run popover's request, so it runs once in a background thread and its answer is kept in
the set pack (`preflight_pairs.json`) with a fingerprint of the files it was made from: the
popover shows "working out the pairs" meanwhile and asks again.

Each vendor page (with a vendor drawing) is:

- **paired**: code's own clear pick, or a saved matcher answer that agrees (`pairing.agree`);
- **to ask**: some drawing on it has candidates the matcher will be shown, no saved answer;
- **none possible**: code rules out every architect drawing for every drawing on it, or there is
  none to rule in: no matcher answer can pair it (certain).

`confirmed`: no drawing waits for a matcher answer, so the numbers are final, not an estimate.

Loaded by name (`importlib`): the work list reaches drawing code the API never imports
(`tests/api/test_no_heavy_work.py`).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from app.review.assistant.preflight import PairEstimate, ProjectGroup, ProjectSummary

__all__ = [
    "FILE",
    "INPUTS",
    "cached",
    "cached_projects",
    "estimate",
    "estimate_from",
    "project_summary",
    "start",
]

#: A view kind of the set index (`set_index.VIEW_KINDS`): a plan.
_PLAN: Final = "plan"

_log = logging.getLogger(__name__)

FILE: Final = "preflight_pairs.json"
#: The pack's files the estimate depends on (a change in any makes it again).
INPUTS: Final = (
    "page_map.json",
    "dims.json",
    "scales.json",
    "notes.json",
    "annotations.json",
    "page_match.json",
    "plan_answers.jsonl",
    "set_index.json",
    "architect_index.json",
    "status.json",
)
_RUNNING: set[str] = set()
_LOCK = threading.Lock()


#: Changes when what is kept changes (2: the projects grouping, the pages not matched for sure):
#: an older kept estimate is worked out again.
VERSION: Final = 2


def fingerprint(folder: Path) -> str:
    parts = [f"v{VERSION}"]
    for name in INPUTS:
        try:
            stat = (folder / name).stat()
        except OSError:
            parts.append(f"{name}:-")
            continue
        parts.append(f"{name}:{stat.st_size}:{stat.st_mtime_ns}")
    return "|".join(parts)


def estimate_from(work: Any, saved_pending: set[str]) -> PairEstimate:
    """The estimate from a work list (`worklist.build_worklist`) and the vendor drawings (refs)
    the matcher would still be asked about (`page_match.to_ask`)."""
    from app.review.assistant.full_check_plan import CrossPairs

    cross = CrossPairs.of(work)
    main = getattr(work, "file", None)
    asked_pages: set[int] = set()
    for pair in getattr(work, "across", ()):
        if pair.vendor.file != main:
            continue
        if pair.asked and pair.vendor.ref in saved_pending:
            asked_pages.add(pair.vendor.own_page)
    vendor_pages = set(cross.pairs) | set(cross.unpaired) | asked_pages
    for pair in getattr(work, "across", ()):
        if pair.vendor.file == main:
            vendor_pages.add(pair.vendor.own_page)
    paired = set(cross.pairs)
    to_ask = asked_pages - paired
    # A page with candidates the matcher already answered, still unpaired: not sure, never "no
    # architect drawing" (the architect's file may hold it).
    candidates = {
        pair.vendor.own_page
        for pair in getattr(work, "across", ())
        if pair.vendor.file == main and pair.asked
    }
    unsure = tuple(sorted((candidates - paired) - to_ask))
    none_possible = tuple(sorted(vendor_pages - paired - to_ask - set(unsure)))
    return PairEstimate(
        vendor_pages=len(vendor_pages),
        paired=len(paired),
        to_ask=len(to_ask),
        none_possible=none_possible,
        unsure=unsure,
        confirmed=not saved_pending,
    )


def estimate(folder: Path, main_file: str | None) -> PairEstimate | None:
    """Code's pairing estimate for the pack in `folder` (builds the work list: slow)."""
    from importlib import import_module

    worklist = import_module("app.review.assistant.simple_check.worklist")
    page_match = import_module("app.review.assistant.page_match")
    work = worklist.build_worklist(folder, main_file)
    if not getattr(work, "across", ()):
        return None
    asked = [pair for pair in work.across if pair.asked]
    pending = {pair.vendor.ref for pair in page_match.to_ask(asked, folder)[0]}
    return estimate_from(work, pending)


def _tidy(name: object) -> str:
    """A printed project name as shown: spaces tidied ("HOME 2 SUITES , KEMAH" -> "HOME 2 SUITES,
    KEMAH")."""
    return " ".join(str(name or "").split()).replace(" ,", ",")


def project_summary(folder: Path) -> ProjectSummary | None:
    """The upload's projects EXACTLY as the pairing groups them: `simple_check.projects.places`
    on the set index with the work list's own inputs (its page order, its printed words), the
    architect's projects from the architect file's drawn pages (never a reference set's), and a
    vendor project counted as in the architect's file by `projects.match` with its siblings (the
    work list's `_vendor_project`). `None` when the pack has no set index or no separate
    architect's file."""
    from importlib import import_module

    projects = import_module("app.review.assistant.simple_check.projects")
    worklist = import_module("app.review.assistant.simple_check.worklist")

    def read(name: str) -> Any:
        try:
            return json.loads((folder / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    index = worklist._index_pages(read(worklist.INDEX_FILE))
    page_map = read("page_map.json")
    if not index or not isinstance(page_map, Mapping):
        return None
    roles = (
        page_map.get("upload_roles") if isinstance(page_map.get("upload_roles"), Mapping) else {}
    )
    architect_files = {str(f) for f, role in (roles or {}).items() if role == "architect"}
    files = [str(f) for f in page_map.get("files") or [] if f]
    main = next((f for f in files if f not in architect_files), None)
    if not architect_files or main is None:
        return None
    entries = [
        e
        for e in page_map.get("pages") or []
        if isinstance(e, Mapping) and isinstance(e.get("page"), int)
    ]
    drawn = {(str(e.get("file")), int(e["page"])) for e in entries if e.get("halves")}
    printed = worklist._printed_words(
        worklist._by_page(read("dims.json")), read("annotations.json")
    )
    placed = projects.places(index, entries, printed)
    architect: dict[str, str] = {}
    for (file, page), place in placed.items():
        if (
            file in architect_files
            and (file, page) in drawn
            and place.project
            and not place.reference
        ):
            architect.setdefault(place.project, _tidy(place.name or place.project))
    vendor_pages: dict[str, list[int]] = {}
    vendor_names: dict[str, str] = {}
    vendor_sources: dict[str, set[str | None]] = {}
    for (file, page), place in sorted(placed.items()):
        if file != main or place.project is None or place.reference:
            continue
        vendor_pages.setdefault(place.project, []).append(page)
        vendor_names.setdefault(place.project, _tidy(place.name or place.project))
        vendor_sources.setdefault(place.project, set()).add(place.source)
    # Rivals are the projects of the same source file (`projects.siblings_of`, as the pairing).
    groups = tuple(
        ProjectGroup(
            name=vendor_names[key],
            vendor_pages=tuple(sorted(pages)),
            architect=any(
                projects.match(key, architect, projects.siblings_of(placed, main, source))
                is not None
                for source in sorted(vendor_sources[key], key=str)
            ),
        )
        for key, pages in sorted(vendor_pages.items())
    )
    views: dict[str, list[Mapping[str, Any]]] = {}
    #: A project with a drawn architect page whose views were not read (pen strokes, a time
    #: limit): what its drawings show is unknown, so it is never called plans-only or sizeless.
    unread: set[str] = set()
    for (file, page), place in placed.items():
        if file not in architect_files or place.project not in architect or place.reference:
            continue
        one = index.get(f"{file}#{page}") or {}
        found = [v for v in one.get("views") or [] if isinstance(v, Mapping)]
        if (file, page) in drawn and not found:
            unread.add(str(place.project))
        views.setdefault(place.project, []).extend(found)
    views = {key: found for key, found in views.items() if key not in unread}
    only_plans = tuple(
        architect[key]
        for key, found in sorted(views.items())
        if found
        and all(
            str(v.get("kind")) == _PLAN and (v.get("whole_floor") or not v.get("prints_sizes"))
            for v in found
        )
    )
    no_sizes = tuple(
        architect[key]
        for key, found in sorted(views.items())
        if found
        and architect[key] not in only_plans
        and not any(v.get("prints_sizes") for v in found)
    )
    return ProjectSummary(groups, tuple(sorted(architect.values())), only_plans, no_sizes)


def _summary_dict(found: ProjectSummary | None) -> dict[str, Any] | None:
    if found is None:
        return None
    return {
        "vendor": [
            {"name": g.name, "vendor_pages": list(g.vendor_pages), "architect": g.architect}
            for g in found.vendor
        ],
        "architect": list(found.architect),
        "only_plans": list(found.only_plans),
        "no_sizes": list(found.no_sizes),
    }


def _summary_of(saved: Any) -> ProjectSummary | None:
    if not isinstance(saved, Mapping):
        return None
    return ProjectSummary(
        vendor=tuple(
            ProjectGroup(
                str(g.get("name")),
                tuple(int(p) for p in g.get("vendor_pages") or []),
                bool(g.get("architect")),
            )
            for g in saved.get("vendor") or []
            if isinstance(g, Mapping)
        ),
        architect=tuple(str(n) for n in saved.get("architect") or []),
        only_plans=tuple(str(n) for n in saved.get("only_plans") or []),
        no_sizes=tuple(str(n) for n in saved.get("no_sizes") or []),
    )


def cached_projects(folder: Path) -> ProjectSummary | None:
    """The kept project grouping, when it was made from the pack's files as they are now."""
    try:
        saved = json.loads((folder / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(saved, Mapping) or saved.get("key") != fingerprint(folder):
        return None
    return _summary_of(saved.get("projects"))


def _as_dict(found: PairEstimate | None) -> dict[str, Any] | None:
    if found is None:
        return None
    return {
        "vendor_pages": found.vendor_pages,
        "paired": found.paired,
        "to_ask": found.to_ask,
        "none_possible": list(found.none_possible),
        "unsure": list(found.unsure),
        "confirmed": found.confirmed,
    }


def _of(saved: Mapping[str, Any]) -> PairEstimate:
    return PairEstimate(
        vendor_pages=int(saved.get("vendor_pages") or 0),
        paired=int(saved.get("paired") or 0),
        to_ask=int(saved.get("to_ask") or 0),
        none_possible=tuple(int(p) for p in saved.get("none_possible") or []),
        unsure=tuple(int(p) for p in saved.get("unsure") or []),
        confirmed=bool(saved.get("confirmed")),
    )


def cached(folder: Path) -> tuple[str, PairEstimate | None]:
    """(`ready` | `stale` | `none`, the kept estimate): `ready` when it was made from the pack's
    files as they are now."""
    try:
        saved = json.loads((folder / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "none", None
    if not isinstance(saved, Mapping):
        return "none", None
    found = saved.get("estimate")
    estimate_now = _of(found) if isinstance(found, Mapping) else None
    state = "ready" if saved.get("key") == fingerprint(folder) else "stale"
    return state, estimate_now


def _work(folder: Path, main_file: str | None) -> None:
    key = fingerprint(folder)
    try:
        found = estimate(folder, main_file)
    except Exception as failed:  # noqa: BLE001 - the report then says nothing of the pairs
        _log.warning("pre-flight pairing estimate failed: %s", type(failed).__name__)
        found = None
    try:
        grouped = project_summary(folder)
    except Exception as failed:  # noqa: BLE001 - the report then says nothing of the projects
        _log.warning("pre-flight project grouping failed: %s", type(failed).__name__)
        grouped = None
    try:
        temporary = folder / f".{FILE}.{threading.get_ident()}"
        temporary.write_text(
            json.dumps(
                {"key": key, "estimate": _as_dict(found), "projects": _summary_dict(grouped)}
            ),
            encoding="utf-8",
        )
        temporary.replace(folder / FILE)
    except OSError:
        _log.warning("pre-flight pairing estimate could not be kept")
    finally:
        with _LOCK:
            _RUNNING.discard(str(folder))


def start(folder: Path, main_file: str | None) -> bool:
    """Starts working out the estimate in the background (once per pack at a time). Returns
    whether it is being worked out now."""
    with _LOCK:
        if str(folder) in _RUNNING:
            return True
        _RUNNING.add(str(folder))
    threading.Thread(
        target=_work, args=(folder, main_file), name="gv-preflight-pairs", daemon=True
    ).start()
    return True
